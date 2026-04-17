from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from omni_cli.utils.dates import _utc_now_iso
from omni_cli.utils.text import _truncate
from omni_cli.tools.utils.path_helpers import resolve_path
from omni_cli.tools.utils.validators import _normalize_timeout_seconds, _require_string


COMMAND_STATUS_STARTING = "starting"
COMMAND_STATUS_RUNNING = "running"
COMMAND_STATUS_STOPPING = "stopping"
COMMAND_STATUS_COMPLETED = "completed"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_STOPPED = "stopped"

TERMINAL_COMMAND_STATUSES = {
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_STOPPED,
}


def _shell_command(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-Command", command]
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/bash"
    if not Path(shell).exists():
        shell = "/bin/sh"
    return [shell, "-lc", command]


def _looks_like_recursive_omni_invocation(command: str) -> bool:
    normalized = " ".join(command.strip().split()).lower()
    recursive_patterns = (
        "omni-cli ",
        "./.venv/bin/omni-cli ",
        "python -m omni_cli",
        "python3 -m omni_cli",
    )
    return any(pattern in normalized for pattern in recursive_patterns)


def _decode_command_output(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")


def _build_combined_output(stdout_text: str, stderr_text: str) -> str:
    sections: list[str] = []
    if stdout_text:
        sections.append("[stdout]\n" + stdout_text)
    if stderr_text:
        sections.append("[stderr]\n" + stderr_text)
    return "\n\n".join(sections)


def get_commands_dir(logs_dir: Path, session_id: str) -> Path:
    commands_dir = logs_dir / "commands" / session_id
    commands_dir.mkdir(parents=True, exist_ok=True)
    return commands_dir


def _normalize_command_id(command_id: str) -> str:
    normalized = command_id.strip()
    if not normalized:
        raise ValueError("command_id must be a non-empty string")
    if Path(normalized).name != normalized or normalized in {".", ".."}:
        raise ValueError("command_id must not contain path separators")
    return normalized


def resolve_command_metadata_path(logs_dir: Path, session_id: str, command_id: str) -> Path:
    normalized_id = _normalize_command_id(command_id)
    metadata_path = get_commands_dir(logs_dir, session_id) / f"{normalized_id}.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Background command not found: {normalized_id}")
    return metadata_path


def load_command_metadata(metadata_path: Path) -> dict[str, Any]:
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def update_command_metadata(metadata_path: Path, **updates: Any) -> dict[str, Any]:
    metadata = load_command_metadata(metadata_path)
    metadata.update(updates)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    return metadata


def _command_is_running(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _send_signal_to_command_group(pid: int, signum: int) -> None:
    try:
        if os.name == "nt":
            os.kill(pid, signum)
        else:
            os.killpg(pid, signum)
    except ProcessLookupError:
        return


def _wait_for_supervisor_update(metadata_path: Path, supervisor_pid: Any, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        metadata = load_command_metadata(metadata_path)
        if metadata.get("status") in TERMINAL_COMMAND_STATUSES:
            return metadata
        if not _command_is_running(supervisor_pid):
            return metadata
        time.sleep(0.05)
    return load_command_metadata(metadata_path)


def refresh_command_metadata(metadata_path: Path) -> dict[str, Any]:
    metadata = load_command_metadata(metadata_path)
    status = str(metadata.get("status", "")).strip().lower()
    if status in TERMINAL_COMMAND_STATUSES:
        return metadata

    pid = metadata.get("pid")
    supervisor_pid = metadata.get("supervisor_pid")
    stop_requested = bool(metadata.get("stop_requested", False))

    if status == COMMAND_STATUS_STARTING:
        if _command_is_running(pid):
            return update_command_metadata(
                metadata_path,
                status=COMMAND_STATUS_RUNNING,
                process_group_id=metadata.get("process_group_id") or pid,
            )
        if _command_is_running(supervisor_pid):
            return metadata
        terminal_status = COMMAND_STATUS_STOPPED if stop_requested else COMMAND_STATUS_FAILED
        error_message = metadata.get("error") or "Background supervisor exited before the command was fully started."
        return update_command_metadata(
            metadata_path,
            status=terminal_status,
            completed_at=metadata.get("completed_at") or _utc_now_iso(),
            error=error_message,
        )

    if status in {COMMAND_STATUS_RUNNING, COMMAND_STATUS_STOPPING}:
        if _command_is_running(pid):
            return metadata
        if _command_is_running(supervisor_pid):
            return _wait_for_supervisor_update(metadata_path, supervisor_pid, timeout_seconds=0.3)
        if metadata.get("exit_code") is not None:
            exit_code = int(metadata["exit_code"])
            terminal_status = COMMAND_STATUS_STOPPED if stop_requested else (
                COMMAND_STATUS_COMPLETED if exit_code == 0 else COMMAND_STATUS_FAILED
            )
        else:
            terminal_status = COMMAND_STATUS_STOPPED if stop_requested else COMMAND_STATUS_COMPLETED
        return update_command_metadata(
            metadata_path,
            status=terminal_status,
            completed_at=metadata.get("completed_at") or _utc_now_iso(),
        )

    return metadata


def wait_for_command(metadata_path: Path, timeout_seconds: float | None) -> tuple[dict[str, Any], bool]:
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        metadata = refresh_command_metadata(metadata_path)
        if metadata.get("status") in TERMINAL_COMMAND_STATUSES:
            return metadata, False
        if deadline is not None and time.monotonic() >= deadline:
            return metadata, True
        time.sleep(0.2)


def stop_background_command(metadata_path: Path, grace_period_seconds: float = 2.0) -> dict[str, Any]:
    metadata = refresh_command_metadata(metadata_path)
    status = str(metadata.get("status", "")).strip().lower()
    if status in TERMINAL_COMMAND_STATUSES:
        return metadata

    metadata = update_command_metadata(
        metadata_path,
        stop_requested=True,
        stop_requested_at=_utc_now_iso(),
        status=COMMAND_STATUS_STOPPING,
    )

    pid = metadata.get("pid")
    supervisor_pid = metadata.get("supervisor_pid")

    if _command_is_running(pid):
        _send_signal_to_command_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace_period_seconds
        while time.monotonic() < deadline:
            refreshed = refresh_command_metadata(metadata_path)
            if refreshed.get("status") in TERMINAL_COMMAND_STATUSES:
                return refreshed
            if not _command_is_running(pid):
                break
            time.sleep(0.1)
        if _command_is_running(pid):
            kill_signal = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
            _send_signal_to_command_group(pid, kill_signal)
        final_metadata, _ = wait_for_command(metadata_path, timeout_seconds=grace_period_seconds)
        if final_metadata.get("status") in TERMINAL_COMMAND_STATUSES:
            return final_metadata

    if _command_is_running(supervisor_pid):
        _send_signal_to_command_group(supervisor_pid, signal.SIGTERM)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and _command_is_running(supervisor_pid):
            time.sleep(0.05)
        if _command_is_running(supervisor_pid):
            kill_signal = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
            _send_signal_to_command_group(supervisor_pid, kill_signal)

    return update_command_metadata(
        metadata_path,
        status=COMMAND_STATUS_STOPPED,
        completed_at=metadata.get("completed_at") or _utc_now_iso(),
    )


def _background_supervisor_invocation(metadata_path: Path) -> list[str]:
    return [sys.executable, "-m", "omni_cli.tools.shell.run_command", "--supervise", str(metadata_path)]


def _append_supervisor_log(combined_output_path: Path, message: str) -> None:
    with combined_output_path.open("ab") as handle:
        handle.write(message.encode("utf-8", errors="replace"))


def _create_command_artifact_paths(logs_dir: Path, session_id: str) -> dict[str, Any]:
    commands_dir = get_commands_dir(logs_dir, session_id)
    from datetime import datetime
    command_id = f"{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    return {
        "started_at": _utc_now_iso(),
        "command_id": command_id,
        "stdout_path": commands_dir / f"{command_id}.stdout.log",
        "stderr_path": commands_dir / f"{command_id}.stderr.log",
        "combined_output_path": commands_dir / f"{command_id}.combined.log",
        "metadata_path": commands_dir / f"{command_id}.json",
    }


def _run_command_foreground(
    command: str,
    cwd: Path,
    process_args: list[str],
    artifact_paths: dict[str, Any],
    timeout_seconds: int | None,
    output_limit: int,
    stdin_bytes: bytes | None = None,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            process_args,
            cwd=str(cwd),
            input=stdin_bytes,
            capture_output=True,
            text=False,
            timeout=timeout_seconds,
        )
        stdout_bytes = completed.stdout or b""
        stderr_bytes = completed.stderr or b""
        timed_out = False
        exit_code: int | None = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout_bytes = exc.stdout or b""
        stderr_bytes = exc.stderr or b""
        timed_out = True
        exit_code = None

    stdout_text = _decode_command_output(stdout_bytes)
    stderr_text = _decode_command_output(stderr_bytes)
    combined_text = _build_combined_output(stdout_text, stderr_text)

    artifact_paths["stdout_path"].write_bytes(stdout_bytes)
    artifact_paths["stderr_path"].write_bytes(stderr_bytes)
    artifact_paths["combined_output_path"].write_text(combined_text, encoding="utf-8")

    metadata = {
        "command_id": artifact_paths["command_id"],
        "mode": "foreground",
        "status": COMMAND_STATUS_COMPLETED if not timed_out and exit_code == 0 else (
            COMMAND_STATUS_FAILED if not timed_out else COMMAND_STATUS_FAILED
        ),
        "command": command,
        "cwd": str(cwd),
        "started_at": artifact_paths["started_at"],
        "completed_at": _utc_now_iso(),
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout_path": str(artifact_paths["stdout_path"]),
        "stderr_path": str(artifact_paths["stderr_path"]),
        "combined_output_path": str(artifact_paths["combined_output_path"]),
    }
    artifact_paths["metadata_path"].write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    return {
        "command_id": artifact_paths["command_id"],
        "command": command,
        "cwd": str(cwd),
        "mode": "foreground",
        "status": metadata["status"],
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout": _truncate(stdout_text, output_limit),
        "stderr": _truncate(stderr_text, output_limit),
        "stdout_truncated": len(stdout_text) > output_limit,
        "stderr_truncated": len(stderr_text) > output_limit,
        "stdout_bytes": len(stdout_bytes),
        "stderr_bytes": len(stderr_bytes),
        "stdout_path": str(artifact_paths["stdout_path"]),
        "stderr_path": str(artifact_paths["stderr_path"]),
        "combined_output_path": str(artifact_paths["combined_output_path"]),
        "metadata_path": str(artifact_paths["metadata_path"]),
    }


def _run_command_background(
    command: str,
    cwd: Path,
    artifact_paths: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    metadata = {
        "command_id": artifact_paths["command_id"],
        "session_id": session_id,
        "mode": "background",
        "status": COMMAND_STATUS_STARTING,
        "command": command,
        "cwd": str(cwd),
        "started_at": artifact_paths["started_at"],
        "completed_at": None,
        "pid": None,
        "process_group_id": None,
        "supervisor_pid": None,
        "exit_code": None,
        "stop_requested": False,
        "combined_output_path": str(artifact_paths["combined_output_path"]),
        "stdout_path": str(artifact_paths["stdout_path"]),
        "stderr_path": str(artifact_paths["stderr_path"]),
        "metadata_path": str(artifact_paths["metadata_path"]),
    }
    artifact_paths["metadata_path"].write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    supervisor = subprocess.Popen(
        _background_supervisor_invocation(artifact_paths["metadata_path"]),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
        start_new_session=True,
    )
    metadata = update_command_metadata(
        artifact_paths["metadata_path"],
        supervisor_pid=supervisor.pid,
    )

    return {
        "command_id": artifact_paths["command_id"],
        "command": command,
        "cwd": str(cwd),
        "mode": "background",
        "status": metadata["status"],
        "combined_output_path": str(artifact_paths["combined_output_path"]),
        "metadata_path": str(artifact_paths["metadata_path"]),
        "message": "Command started in background. Use command_id with list_commands, read_command_output, wait_command, or stop_command.",
    }


def _run_background_supervisor(metadata_path: Path) -> int:
    metadata = load_command_metadata(metadata_path)
    command = str(metadata.get("command", ""))
    cwd = Path(str(metadata.get("cwd", ".")))
    combined_output_path = Path(str(metadata.get("combined_output_path", "")))

    if not command:
        update_command_metadata(
            metadata_path,
            status=COMMAND_STATUS_FAILED,
            completed_at=_utc_now_iso(),
            error="Missing command in background metadata.",
        )
        return 1

    process_args = _shell_command(command)
    try:
        with combined_output_path.open("ab") as combined_handle:
            process = subprocess.Popen(
                process_args,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=combined_handle,
                stderr=subprocess.STDOUT,
                text=False,
                start_new_session=True,
            )
    except Exception as exc:
        _append_supervisor_log(combined_output_path, f"[supervisor error] failed to start command: {exc}\n")
        update_command_metadata(
            metadata_path,
            status=COMMAND_STATUS_FAILED,
            completed_at=_utc_now_iso(),
            error=str(exc),
        )
        return 1

    update_command_metadata(
        metadata_path,
        status=COMMAND_STATUS_RUNNING,
        pid=process.pid,
        process_group_id=process.pid,
        supervisor_pid=os.getpid(),
    )

    exit_code = process.wait()
    latest = load_command_metadata(metadata_path)
    stop_requested = bool(latest.get("stop_requested", False))
    final_status = COMMAND_STATUS_STOPPED if stop_requested else (
        COMMAND_STATUS_COMPLETED if exit_code == 0 else COMMAND_STATUS_FAILED
    )
    update_command_metadata(
        metadata_path,
        status=final_status,
        exit_code=exit_code,
        completed_at=_utc_now_iso(),
    )
    return 0


def _parse_supervisor_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--supervise", dest="metadata_path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_supervisor_args(argv)
    if args.metadata_path:
        return _run_background_supervisor(Path(args.metadata_path))
    return 0


def execute_run_command(
    arguments: dict[str, Any],
    root_dir: Path,
    logs_dir: Path,
    session_id: str,
    output_limit: int,
    default_command_timeout_seconds: int,
) -> dict[str, Any]:
    command = _require_string(arguments, "command")
    if _looks_like_recursive_omni_invocation(command):
        raise ValueError("Recursive omni-cli invocation is not allowed from run_command. Use the current tools directly.")
    cwd_value = arguments.get("cwd")
    stdin_text = arguments.get("stdin")
    stdin_bytes = stdin_text.encode("utf-8") if isinstance(stdin_text, str) else None
    timeout_seconds = _normalize_timeout_seconds(
        arguments.get("timeout_seconds"),
        default_timeout_seconds=default_command_timeout_seconds,
    )
    background = bool(arguments.get("background", False) or arguments.get("run_in_background", False))
    cwd = resolve_path(cwd_value, root_dir) if isinstance(cwd_value, str) and cwd_value.strip() else root_dir
    if not cwd.exists() or not cwd.is_dir():
        raise NotADirectoryError(f"Working directory does not exist or is not a directory: {cwd}")

    if background and stdin_bytes:
        raise ValueError("stdin is not supported in background mode. Use foreground execution instead.")

    process_args = _shell_command(command)
    artifact_paths = _create_command_artifact_paths(logs_dir, session_id)

    if background:
        return _run_command_background(command, cwd, artifact_paths, session_id)

    return _run_command_foreground(
        command,
        cwd,
        process_args,
        artifact_paths,
        timeout_seconds,
        output_limit,
        stdin_bytes,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

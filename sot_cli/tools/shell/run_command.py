from __future__ import annotations

import argparse
import json
import os
import queue as _std_queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from sot_cli.utils.dates import _utc_now_iso
from sot_cli.utils.text import _truncate
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _normalize_timeout_seconds, _require_string


COMMAND_STATUS_STARTING = "starting"
COMMAND_STATUS_RUNNING = "running"
COMMAND_STATUS_STOPPING = "stopping"
COMMAND_STATUS_COMPLETED = "completed"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_STOPPED = "stopped"
COMMAND_STATUS_WAITING_FOR_INPUT = "waiting_for_input"

TERMINAL_COMMAND_STATUSES = {
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_STOPPED,
}


# -----------------------------------------------------------------------------
# Interactive-prompt detection
# -----------------------------------------------------------------------------
# While a foreground child runs, the runtime reads its stdout/stderr
# incrementally and scans the last line against these patterns. If one hits,
# the runtime concludes the child has printed an interactive prompt and is
# now blocked on stdin. The child is then terminated and the tool result
# reports status=waiting_for_input with the pattern name + matched text, so
# the model can decide: re-run with stdin pre-filled, tell the User, or skip.
#
# Patterns match the LAST (non-empty) line only, to avoid false positives
# from historical output that incidentally contained words like "password:".
# -----------------------------------------------------------------------------
_INTERACTIVE_PROMPT_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # More specific patterns first; generic ones last — first hit wins.
    ("sudo_password",        re.compile(r"(?i)\[sudo\]\s+password\s+for\s+\S+\s*:")),
    ("ssh_fingerprint",      re.compile(r"(?i)are\s+you\s+sure\s+you\s+want\s+to\s+continue\s+connecting")),
    ("ssh_authenticity",     re.compile(r"(?i)authenticity\s+of\s+host\b.*\bcan'?t\s+be\s+established")),
    ("git_username",         re.compile(r"(?i)^username\s+for\s+['\"]?\S+['\"]?\s*:\s*$")),
    ("git_password",         re.compile(r"(?i)^password\s+for\s+['\"]?\S+['\"]?\s*:\s*$")),
    ("runas_windows",        re.compile(r"(?i)enter\s+the\s+password\s+for\s+\S+:\s*$")),
    ("do_you_want_continue", re.compile(r"(?i)do\s+you\s+want\s+to\s+continue\??\s*(\[(?:y/n|yes/no)\])?\s*:?\s*$")),
    ("continue_anykey",      re.compile(r"(?i)press\s+(any\s+key|enter)\s+to\s+continue\.?\s*$")),
    ("password",             re.compile(r"(?i)\b(password|passphrase|contrase[ñn]a)\b[^:\n]*:\s*$")),
    ("yes_no",               re.compile(r"(?i)\[(?:y/n|Y/n|y/N|yes/no)\]\s*\??\s*:?\s*$")),
    ("generic_input_prompt", re.compile(r"(?i)^\s*(please\s+)?(enter|input|type)\b[^\n]{0,80}[:?]\s*$")),
]


def _detect_interactive_prompt(buf: bytes) -> tuple[str, str] | None:
    """Return (pattern_name, matched_text) if the tail of `buf` looks like a prompt."""
    if not buf:
        return None
    tail = buf[-2048:].decode("utf-8", errors="replace")
    # Only scan the last non-empty line — interactive prompts are always the very
    # last thing a program prints before blocking on read().
    last_line = tail.rsplit("\n", 1)[-1].rstrip()
    if not last_line:
        return None
    for name, pattern in _INTERACTIVE_PROMPT_PATTERNS:
        m = pattern.search(last_line)
        if m:
            return name, m.group(0).strip()
    return None


def _shell_command(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-Command", command]
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/bash"
    if not Path(shell).exists():
        shell = "/bin/sh"
    return [shell, "-lc", command]


def _looks_like_recursive_sot_invocation(command: str) -> bool:
    normalized = " ".join(command.strip().split()).lower()
    recursive_patterns = (
        "sot-cli ",
        "./.venv/bin/sot-cli ",
        "python -m sot_cli",
        "python3 -m sot_cli",
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


# -----------------------------------------------------------------------------
# Foreground interrupt plumbing
# -----------------------------------------------------------------------------
# The CLI installs a SIGINT handler that, on Ctrl+C, first calls
# `try_interrupt_active_foreground()` to stop a stuck foreground run_command
# *without* cancelling the agent's turn. The set below tracks every live
# foreground Popen so the handler can terminate them. Every foreground child
# is started in its own session (Unix) or process group (Windows) so TTY
# Ctrl+C no longer reaches the child directly — the decision to kill is
# routed through our handler explicitly.
# -----------------------------------------------------------------------------
_active_fg_procs: set["subprocess.Popen[bytes]"] = set()
_active_fg_lock = threading.Lock()
_interrupted_fg_pids: set[int] = set()


def _register_active_foreground(proc: "subprocess.Popen[bytes]") -> None:
    with _active_fg_lock:
        _active_fg_procs.add(proc)


def _unregister_active_foreground(proc: "subprocess.Popen[bytes]") -> None:
    with _active_fg_lock:
        _active_fg_procs.discard(proc)


def _consume_interrupted_flag(pid: int) -> bool:
    with _active_fg_lock:
        if pid in _interrupted_fg_pids:
            _interrupted_fg_pids.discard(pid)
            return True
        return False


def try_interrupt_active_foreground() -> bool:
    """Signal all live foreground run_command children to terminate.

    Designed to be called from a SIGINT handler. Sends SIGTERM (Unix) or
    CTRL_BREAK_EVENT (Windows) to each child's process group. Returns True
    if at least one child was signaled, False if nothing was active.

    Callers should interpret True as "Ctrl+C was consumed by the foreground
    interrupt; do NOT also cancel the turn."
    """
    with _active_fg_lock:
        if not _active_fg_procs:
            return False
        if os.name == "nt":
            sig = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
        else:
            sig = signal.SIGTERM
        procs_snapshot = list(_active_fg_procs)
        for proc in procs_snapshot:
            _interrupted_fg_pids.add(proc.pid)
    # Signal outside the lock so a slow kill does not block other callers.
    for proc in procs_snapshot:
        try:
            _send_signal_to_command_group(proc.pid, sig)
        except Exception:
            pass
    return True


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
    return [sys.executable, "-m", "sot_cli.tools.shell.run_command", "--supervise", str(metadata_path)]


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
    # Build the child in a separate session/process group so Ctrl+C from the
    # terminal only reaches the sot-cli process — the decision to propagate
    # it to the child is routed explicitly through try_interrupt_active_foreground().
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc: "subprocess.Popen[bytes]" = subprocess.Popen(process_args, **popen_kwargs)
    _register_active_foreground(proc)

    # Feed pre-supplied stdin upfront, then close the pipe so programs like `cat`
    # finish reading. (Matches communicate() semantics for the stdin side.)
    if stdin_bytes is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass

    # Incremental reader threads push labeled byte chunks to this queue. We keep
    # stdout and stderr separate so interactive-prompt detection can prioritise
    # stderr (where most prompts go: sudo, ssh, password tools).
    chunk_queue: "_std_queue.Queue[tuple[str, bytes]]" = _std_queue.Queue()

    def _reader(label: str, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                chunk_queue.put((label, chunk))
        except Exception:
            pass
        finally:
            chunk_queue.put((label, b""))  # EOF sentinel

    stdout_thread = threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True)
    stderr_thread = threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    pipes_open = {"stdout": True, "stderr": True}

    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    timed_out = False
    waiting_for_input = False
    detected_prompt_name: str | None = None
    detected_prompt_text: str | None = None

    POLL_INTERVAL = 0.25

    try:
        while True:
            # Exit when the child has exited AND both pipes have drained.
            if proc.poll() is not None and not pipes_open["stdout"] and not pipes_open["stderr"]:
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            try:
                label, chunk = chunk_queue.get(timeout=POLL_INTERVAL)
            except _std_queue.Empty:
                continue
            if chunk == b"":
                pipes_open[label] = False
                continue
            if label == "stdout":
                stdout_chunks.append(chunk)
            else:
                stderr_chunks.append(chunk)
            # Scan the tail of each stream for an interactive-prompt pattern.
            # Prefer stderr (sudo, ssh, most password tools emit there).
            hit = _detect_interactive_prompt(b"".join(stderr_chunks[-8:]))
            if hit is None:
                hit = _detect_interactive_prompt(b"".join(stdout_chunks[-8:]))
            if hit is not None:
                detected_prompt_name, detected_prompt_text = hit
                waiting_for_input = True
                break
    finally:
        _unregister_active_foreground(proc)

    # Terminate the child if we bailed out early (prompt detected or timeout).
    # SIGTERM first, escalate to SIGKILL if it does not die in a grace window.
    if waiting_for_input or timed_out:
        _send_signal_to_command_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill_signal = signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
            _send_signal_to_command_group(proc.pid, kill_signal)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    # Drain any bytes still in flight (pipes close shortly after child exits).
    drain_deadline = time.monotonic() + 1.5
    while time.monotonic() < drain_deadline:
        try:
            label, chunk = chunk_queue.get(timeout=0.05)
        except _std_queue.Empty:
            if not stdout_thread.is_alive() and not stderr_thread.is_alive():
                break
            continue
        if chunk == b"":
            pipes_open[label] = False
        elif label == "stdout":
            stdout_chunks.append(chunk)
        else:
            stderr_chunks.append(chunk)

    stdout_bytes = b"".join(stdout_chunks)
    stderr_bytes = b"".join(stderr_chunks)

    # Explicit user Ctrl+C wins over any auto-detection that happened concurrently.
    interrupted_by_user = _consume_interrupted_flag(proc.pid)
    if interrupted_by_user:
        timed_out = False
        waiting_for_input = False

    # User-visible heads-up when we auto-detected a prompt. Safe here (not inside
    # a signal handler). Raw ANSI yellow to avoid pulling rich's locking into
    # a potentially sensitive code path.
    if waiting_for_input:
        try:
            sys.stderr.write(
                f"\n\x1b[33mInteractive prompt detected [{detected_prompt_name}]: "
                f"{detected_prompt_text!r}. Command was terminated; the agent "
                f"will surface this and decide how to proceed.\x1b[0m\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    if interrupted_by_user or waiting_for_input or timed_out:
        exit_code: int | None = None
    else:
        exit_code = proc.returncode

    stdout_text = _decode_command_output(stdout_bytes)
    stderr_text = _decode_command_output(stderr_bytes)
    combined_text = _build_combined_output(stdout_text, stderr_text)

    artifact_paths["stdout_path"].write_bytes(stdout_bytes)
    artifact_paths["stderr_path"].write_bytes(stderr_bytes)
    artifact_paths["combined_output_path"].write_text(combined_text, encoding="utf-8")

    if interrupted_by_user:
        status = COMMAND_STATUS_STOPPED
    elif waiting_for_input:
        status = COMMAND_STATUS_WAITING_FOR_INPUT
    elif timed_out:
        status = COMMAND_STATUS_FAILED
    elif exit_code == 0:
        status = COMMAND_STATUS_COMPLETED
    else:
        status = COMMAND_STATUS_FAILED

    metadata = {
        "command_id": artifact_paths["command_id"],
        "mode": "foreground",
        "status": status,
        "command": command,
        "cwd": str(cwd),
        "started_at": artifact_paths["started_at"],
        "completed_at": _utc_now_iso(),
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "interrupted_by_user": interrupted_by_user,
        "waiting_for_input": waiting_for_input,
        "detected_prompt": detected_prompt_name,
        "detected_prompt_text": detected_prompt_text,
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
        "status": status,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "interrupted_by_user": interrupted_by_user,
        "waiting_for_input": waiting_for_input,
        "detected_prompt": detected_prompt_name,
        "detected_prompt_text": detected_prompt_text,
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
    if _looks_like_recursive_sot_invocation(command):
        raise ValueError("Recursive sot-cli invocation is not allowed from run_command. Use the current tools directly.")
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

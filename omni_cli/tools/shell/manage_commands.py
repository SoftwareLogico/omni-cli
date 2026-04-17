from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

from omni_cli.tools.shell.run_command import (
    TERMINAL_COMMAND_STATUSES,
    get_commands_dir,
    refresh_command_metadata,
    resolve_command_metadata_path,
    stop_background_command,
    wait_for_command,
)
from omni_cli.tools.utils.validators import _ensure_no_arguments, _normalize_positive_int, _require_string
from omni_cli.utils.text import _count_lines, _truncate


def _serialize_command_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": metadata.get("command_id"),
        "status": metadata.get("status"),
        "command": metadata.get("command"),
        "cwd": metadata.get("cwd"),
        "started_at": metadata.get("started_at"),
        "completed_at": metadata.get("completed_at"),
        "exit_code": metadata.get("exit_code"),
        "combined_output_path": metadata.get("combined_output_path"),
        "metadata_path": metadata.get("metadata_path"),
        "stop_requested": bool(metadata.get("stop_requested", False)),
    }


def _normalize_tail_lines(value: Any) -> int:
    if value is None:
        return 100
    return _normalize_positive_int(value, field_name="tail_lines")


def _normalize_optional_timeout_seconds(value: Any) -> float | None:
    if value is None:
        return None
    timeout_seconds = _normalize_positive_int(value, field_name="timeout_seconds")
    return float(timeout_seconds)


def _read_tail_lines(path: Path, tail_lines: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    tail_buffer: deque[str] = deque(maxlen=tail_lines)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            tail_buffer.append(line)
    return "".join(tail_buffer)


def execute_list_commands(arguments: dict[str, Any], logs_dir: Path, session_id: str) -> dict[str, Any]:
    _ensure_no_arguments(arguments)
    commands_dir = get_commands_dir(logs_dir, session_id)
    commands: list[dict[str, Any]] = []

    for metadata_path in commands_dir.glob("*.json"):
        metadata = refresh_command_metadata(metadata_path)
        if metadata.get("mode") != "background":
            continue
        commands.append(_serialize_command_metadata(metadata))

    commands.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
    return {
        "command_count": len(commands),
        "commands": commands,
    }


def execute_read_command_output(
    arguments: dict[str, Any],
    logs_dir: Path,
    session_id: str,
    output_limit: int,
) -> dict[str, Any]:
    command_id = _require_string(arguments, "command_id")
    tail_lines = _normalize_tail_lines(arguments.get("tail_lines"))
    metadata_path = resolve_command_metadata_path(logs_dir, session_id, command_id)
    metadata = refresh_command_metadata(metadata_path)
    output_path = Path(str(metadata.get("combined_output_path", "")))
    output = _read_tail_lines(output_path, tail_lines)
    truncated_output = _truncate(output, output_limit)
    return {
        "command_id": metadata.get("command_id"),
        "status": metadata.get("status"),
        "exit_code": metadata.get("exit_code"),
        "tail_lines": tail_lines,
        "combined_output_path": str(output_path),
        "output": truncated_output,
        "output_truncated": truncated_output != output,
        "line_count": _count_lines(output),
    }


def execute_wait_command(arguments: dict[str, Any], logs_dir: Path, session_id: str) -> dict[str, Any]:
    command_id = _require_string(arguments, "command_id")
    timeout_seconds = _normalize_optional_timeout_seconds(arguments.get("timeout_seconds"))
    metadata_path = resolve_command_metadata_path(logs_dir, session_id, command_id)
    metadata, timed_out = wait_for_command(metadata_path, timeout_seconds=timeout_seconds)
    return {
        "command_id": metadata.get("command_id"),
        "status": metadata.get("status"),
        "exit_code": metadata.get("exit_code"),
        "timed_out": timed_out,
        "started_at": metadata.get("started_at"),
        "completed_at": metadata.get("completed_at"),
        "combined_output_path": metadata.get("combined_output_path"),
        "metadata_path": metadata.get("metadata_path"),
    }


def execute_stop_command(arguments: dict[str, Any], logs_dir: Path, session_id: str) -> dict[str, Any]:
    command_id = _require_string(arguments, "command_id")
    metadata_path = resolve_command_metadata_path(logs_dir, session_id, command_id)
    metadata = stop_background_command(metadata_path)
    already_terminal = metadata.get("status") in TERMINAL_COMMAND_STATUSES and not metadata.get("stop_requested")
    return {
        "command_id": metadata.get("command_id"),
        "status": metadata.get("status"),
        "exit_code": metadata.get("exit_code"),
        "already_terminal": already_terminal,
        "combined_output_path": metadata.get("combined_output_path"),
        "metadata_path": metadata.get("metadata_path"),
    }

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from omni_cli.tools.utils.path_helpers import resolve_path
from omni_cli.tools.utils.validators import _require_string

VCS_DIRS = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")
DEFAULT_HEAD_LIMIT = 200
MAX_LINE_LENGTH = 500
TIMEOUT_SECONDS = 30


def _normalize_boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _normalize_int(value: Any, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_relative(line: str, root: str) -> str:
    """Convert absolute paths at the start of a ripgrep output line to relative paths."""
    colon_idx = line.find(":")
    if colon_idx > 0:
        file_part = line[:colon_idx]
        rest = line[colon_idx:]
        if file_part.startswith(root):
            file_part = file_part[len(root):].lstrip("/").lstrip("\\")
        return file_part + rest
    if line.startswith(root):
        return line[len(root):].lstrip("/").lstrip("\\")
    return line


def execute_search_code(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    """Search for text patterns across files using ripgrep."""
    pattern = _require_string(arguments, "pattern")
    raw_path = arguments.get("path")
    glob_filter = arguments.get("glob")
    file_type = arguments.get("type")
    output_mode = arguments.get("output_mode", "files_with_matches")
    context_before = _normalize_int(arguments.get("context_before"), None)
    context_after = _normalize_int(arguments.get("context_after"), None)
    context = _normalize_int(arguments.get("context"), None)
    show_line_numbers = _normalize_boolean(arguments.get("show_line_numbers"), True)
    case_insensitive = _normalize_boolean(arguments.get("case_insensitive"), False)
    head_limit = _normalize_int(arguments.get("head_limit"), None)
    offset = _normalize_int(arguments.get("offset"), 0) or 0
    multiline = _normalize_boolean(arguments.get("multiline"), False)

    search_path = resolve_path(raw_path, root_dir) if raw_path else root_dir

    rg_bin = shutil.which("rg")
    if rg_bin is None:
        raise RuntimeError(
            "ripgrep (rg) is not installed or not in PATH. "
            "Install it (e.g. 'brew install ripgrep') or use run_command with grep as a fallback."
        )

    args: list[str] = [rg_bin, "--hidden"]

    for d in VCS_DIRS:
        args.extend(["--glob", f"!{d}"])

    args.extend(["--max-columns", str(MAX_LINE_LENGTH)])

    if multiline:
        args.extend(["-U", "--multiline-dotall"])

    if case_insensitive:
        args.append("-i")

    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")

    if show_line_numbers and output_mode == "content":
        args.append("-n")

    if output_mode == "content":
        if context is not None:
            args.extend(["-C", str(context)])
        else:
            if context_before is not None:
                args.extend(["-B", str(context_before)])
            if context_after is not None:
                args.extend(["-A", str(context_after)])

    if pattern.startswith("-"):
        args.extend(["-e", pattern])
    else:
        args.append(pattern)

    if file_type:
        args.extend(["--type", file_type])

    if glob_filter:
        for token in glob_filter.split():
            if "{" in token and "}" in token:
                args.extend(["--glob", token])
            else:
                for sub in token.split(","):
                    sub = sub.strip()
                    if sub:
                        args.extend(["--glob", sub])

    args.append(str(search_path))

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise RuntimeError("ripgrep binary not found at resolved path")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"search timed out after {TIMEOUT_SECONDS}s. Try a narrower pattern, glob filter, or smaller search path.")

    if proc.returncode > 1:
        error_msg = proc.stderr.strip() or f"ripgrep exited with code {proc.returncode}"
        raise RuntimeError(error_msg)

    raw_lines = proc.stdout.splitlines() if proc.stdout else []

    root_str = str(search_path)
    if not root_str.endswith("/"):
        root_str += "/"

    effective_limit = head_limit if head_limit is not None else DEFAULT_HEAD_LIMIT
    if effective_limit == 0:
        limited = raw_lines[offset:]
        was_truncated = False
    else:
        limited = raw_lines[offset : offset + effective_limit]
        was_truncated = (len(raw_lines) - offset) > effective_limit

    relative_lines = [_to_relative(line, root_str) for line in limited]

    pagination: dict[str, Any] = {}
    if was_truncated:
        pagination["truncated"] = True
        pagination["head_limit"] = effective_limit
    if offset > 0:
        pagination["offset"] = offset

    if output_mode == "content":
        content_text = "\n".join(relative_lines)
        return {
            "mode": "content",
            "content": content_text,
            "line_count": len(relative_lines),
            "total_result_lines": len(raw_lines),
            **pagination,
        }

    if output_mode == "count":
        total_matches = 0
        file_count = 0
        for line in relative_lines:
            colon_idx = line.rfind(":")
            if colon_idx > 0:
                try:
                    total_matches += int(line[colon_idx + 1 :])
                    file_count += 1
                except ValueError:
                    pass
        return {
            "mode": "count",
            "content": "\n".join(relative_lines),
            "match_count": total_matches,
            "file_count": file_count,
            **pagination,
        }

    # files_with_matches (default)
    return {
        "mode": "files_with_matches",
        "files": relative_lines,
        "file_count": len(relative_lines),
        "total_matches": len(raw_lines),
        **pagination,
    }

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from sot_cli.tools.core import ToolPayload
from sot_cli.constants import (
    AUDIO_EXTENSIONS,
    ARCHIVE_EXTENSIONS,
    ARCHIVE_HINTS,
    BINARY_EXTENSIONS,
    IMAGE_EXTENSIONS,
    NOTEBOOK_EXTENSIONS,
    PDF_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from sot_cli.tools.reader.image import read_image
from sot_cli.tools.reader.media import read_audio, read_video
from sot_cli.tools.reader.notebook import read_notebook
from sot_cli.tools.reader.pdf import read_pdf
from sot_cli.tools.utils.formatters import _file_mtime_ns
from sot_cli.tools.utils.path_helpers import _find_similar_file, _is_blocked_device
from sot_cli.tools.utils.validators import _normalize_pages_argument, _normalize_positive_int, _require_string


def _normalize_line_range(arguments: dict[str, Any]) -> tuple[int | None, int | None]:
    raw_start_line = arguments.get("start_line")
    raw_end_line = arguments.get("end_line")
    if raw_start_line is None and raw_end_line is None:
        return None, None
    if raw_start_line is None or raw_end_line is None:
        raise ValueError("start_line and end_line must be provided together")

    start_line = _normalize_positive_int(raw_start_line, "start_line")
    end_line = _normalize_positive_int(raw_end_line, "end_line")
    if end_line < start_line:
        raise ValueError("end_line must be greater than or equal to start_line")
    return start_line, end_line


def _read_text_with_optional_line_range(
    path: Path,
    start_line: int | None,
    end_line: int | None,
) -> tuple[str, int, bool]:
    if start_line is None or end_line is None:
        content = path.read_text(encoding="utf-8")
        total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return content, total_lines, False

    collected_lines: list[str] = []
    total_lines = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for total_lines, line in enumerate(handle, start=1):
                if start_line <= total_lines <= end_line:
                    collected_lines.append(line)
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Cannot decode file as UTF-8 text. The file may be binary. "
            "Use run_command with xxd or file to inspect it."
        ) from exc

    if total_lines == 0:
        raise ValueError("Cannot target line ranges in an empty file")
    if end_line > total_lines:
        raise ValueError(
            f"Line range {start_line}-{end_line} is outside the file (total lines: {total_lines})"
        )

    return "".join(collected_lines), total_lines, True


def execute_read_many_files(
    arguments: dict[str, Any],
    root_dir: Path,
    read_cache: dict,
    binary_check_size: int,
    supports_images: bool,
    supports_pdf: bool,
    supports_audio: bool,
    supports_video: bool,
    file_unchanged_stub: str,
    sot_state: Any = None,
    file_in_sot_stub: str | None = None,
) -> ToolPayload:
    files = arguments.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("The files parameter must be a non-empty array.")

    results: list[dict[str, Any]] = []
    supplemental_messages: list[dict[str, Any]] = []
    success_count = 0
    error_count = 0

    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            results.append({
                "ok": False,
                "path": f"[index {index}]",
                "error": "Each files entry must be an object.",
            })
            error_count += 1
            continue

        raw_path = entry.get("path")
        path_label = raw_path if isinstance(raw_path, str) and raw_path.strip() else f"[index {index}]"

        try:
            raw_result = execute_read_text_file(
                entry,
                root_dir=root_dir,
                read_cache=read_cache,
                binary_check_size=binary_check_size,
                supports_images=supports_images,
                supports_pdf=supports_pdf,
                supports_audio=supports_audio,
                supports_video=supports_video,
                file_unchanged_stub=file_unchanged_stub,
                sot_state=sot_state,
                file_in_sot_stub=file_in_sot_stub,
            )
        except Exception as exc:
            results.append({
                "ok": False,
                "path": path_label,
                "error": str(exc),
            })
            error_count += 1
            continue

        if isinstance(raw_result, ToolPayload):
            payload = dict(raw_result.payload)
            supplemental_messages.extend(raw_result.supplemental_messages)
        else:
            payload = dict(raw_result)

        if "type" not in payload:
            if "content" in payload:
                payload["type"] = "text"
            elif "warning" in payload:
                payload["type"] = "text"

        results.append({"ok": True, **payload})
        success_count += 1

    return ToolPayload(
        payload={
            "result_count": len(results),
            "success_count": success_count,
            "error_count": error_count,
            "results": results,
        },
        supplemental_messages=supplemental_messages,
    )


def _raise_binary_error(ext: str, path: Path) -> None:
    hint = ARCHIVE_HINTS.get(ext)
    if hint is None and ext in ARCHIVE_EXTENSIONS:
        hint = f"Use run_command with appropriate archive tools for .{ext} files."
    if hint is not None:
        formatted = hint.replace("{path}", str(path))
        raise ValueError(
            f"Cannot read .{ext} archive directly. {formatted}"
        )
    raise ValueError(
        f"This tool cannot read binary files. The file appears to be a binary .{ext} file. "
        f"Use run_command with appropriate tools for binary file analysis."
    )


def _is_probably_binary(path: Path, binary_check_size: int) -> bool:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type:
        if mime_type.startswith("text/"):
            return False
        if mime_type in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-yaml",
            "application/toml",
            "application/x-sh",
            "application/x-httpd-php",
            "application/sql",
            "application/graphql",
            "application/ld+json",
            "application/x-perl",
            "application/x-ruby",
            "application/x-python",
            "application/x-lua",
        }:
            return False

    try:
        chunk = path.read_bytes()[:binary_check_size]
    except OSError:
        return False

    if not chunk:
        return False
    if b"\x00" in chunk:
        return True

    non_printable = 0
    for byte in chunk:
        if byte < 32 and byte not in {9, 10, 13}:
            non_printable += 1
    return non_printable / len(chunk) > 0.1


def execute_read_text_file(
    arguments: dict[str, Any],
    root_dir: Path,
    read_cache: dict,
    binary_check_size: int,
    supports_images: bool,
    supports_pdf: bool,
    supports_audio: bool,
    supports_video: bool,
    file_unchanged_stub: str,
    sot_state: Any = None,
    file_in_sot_stub: str | None = None,
) -> dict[str, Any]:
    from sot_cli.tools.utils.path_helpers import resolve_path

    raw_path = _require_string(arguments, "path")
    path = resolve_path(raw_path, root_dir)
    resolved = str(path)
    raw_pages = arguments.get("pages")
    pages = _normalize_pages_argument(raw_pages)
    password: str | None = arguments.get("password")
    start_line, end_line = _normalize_line_range(arguments)

    # Block device paths that would hang
    if _is_blocked_device(resolved):
        raise ValueError(f"Cannot read '{raw_path}': this device file would block or produce infinite output.")

    # Check existence with similar-file suggestion
    if not path.exists():
        similar = _find_similar_file(resolved)
        msg = f"File does not exist. Note: file_path should be an absolute path. Current cwd is {root_dir}."
        if similar:
            msg += f" Did you mean {similar}?"
        raise FileNotFoundError(msg)

    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}. Use list_dir instead.")

    ext = path.suffix.lower().lstrip(".")
    stat = path.stat()
    size_bytes = stat.st_size
    modified_ns = _file_mtime_ns(stat)

    if pages and ext not in PDF_EXTENSIONS:
        raise ValueError("The pages parameter is only valid for PDF files.")
    if start_line is not None and ext in IMAGE_EXTENSIONS | PDF_EXTENSIONS | NOTEBOOK_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
        raise ValueError("start_line and end_line are only valid for UTF-8 text files.")

    # ── SoT check (cross-round) ──
    # If the file is already tracked in the SoT and its disk mtime hasn't
    # changed, the model already sees it in the '=== SOURCE OF TRUTH ===' block.
    # Return a pointer stub instead of re-reading. Only applies to full reads
    # of plain text files (not partial ranges, not PDF pages).
    if (
        sot_state is not None
        and file_in_sot_stub is not None
        and start_line is None
        and end_line is None
        and not pages
    ):
        tracked_mtimes = getattr(sot_state, "tracked_file_mtimes", None)
        tracked_files = getattr(sot_state, "tracked_files", None)
        if (
            isinstance(tracked_files, dict)
            and isinstance(tracked_mtimes, dict)
            and resolved in tracked_files
            and tracked_mtimes.get(resolved) == modified_ns
        ):
            return {
                "type": "file_in_sot",
                "path": resolved,
                "message": file_in_sot_stub,
            }

    # ── Cache check (same-round) ──
    cached = read_cache.get((resolved, pages, start_line, end_line))
    if cached is not None:
        cached_modified_ns, _ = cached
        if cached_modified_ns == modified_ns:
            return {
                "type": "file_unchanged",
                "path": resolved,
                "message": file_unchanged_stub,
            }

    def _store(result: dict[str, Any]) -> dict[str, Any]:
        read_cache[(resolved, pages, start_line, end_line)] = (modified_ns, result)
        return result

    # ── Image ──
    if ext in IMAGE_EXTENSIONS:
        return _store(read_image(path, ext, size_bytes, supports_images))

    # ── PDF ──
    if ext in PDF_EXTENSIONS:
        return _store(read_pdf(path, size_bytes, pages, password, supports_pdf, supports_images))

    # ── Notebook ──
    if ext in NOTEBOOK_EXTENSIONS:
        return _store(read_notebook(path, size_bytes, supports_images))

    # ── Audio ──
    if ext in AUDIO_EXTENSIONS:
        return _store(read_audio(path, ext, size_bytes, supports_audio))

    # ── Video ──
    if ext in VIDEO_EXTENSIONS:
        return _store(read_video(path, ext, size_bytes, supports_video))

    # ── Binary rejection ──
    if ext in BINARY_EXTENSIONS or _is_probably_binary(path, binary_check_size):
        _raise_binary_error(ext, path)

    content, total_lines, is_partial = _read_text_with_optional_line_range(path, start_line, end_line)

    if not content:
        result = {
            "path": resolved,
            "warning": "The file exists but the contents are empty.",
            "content": "",
            "size_bytes": 0,
            "total_lines": total_lines,
            "modified_ns": modified_ns,
        }
        if is_partial:
            result.update(
                {
                    "partial": True,
                    "start_line": start_line,
                    "end_line": end_line,
                    "returned_lines": 0,
                }
            )
        return _store(result)

    result = {
        "path": resolved,
        "content": content,
        "size_bytes": size_bytes,
        "total_lines": total_lines,
        "modified_ns": modified_ns,
    }
    if is_partial:
        result.update(
            {
                "partial": True,
                "start_line": start_line,
                "end_line": end_line,
                "returned_lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
            }
        )
    return _store(result)

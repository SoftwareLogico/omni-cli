from __future__ import annotations

from pathlib import Path
from typing import Any

from sot_cli.tools.editor.text_utils import (
    _match_line_endings,
    _normalize_quotes,
    _prepare_replacement_text,
    _preserve_quote_style,
)
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import _require_string, _require_string_allow_empty
from sot_cli.utils.text import _count_lines


class _EditValidationError(ValueError):
    pass


def _normalize_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _EditValidationError(f"{field_name} must be a string")
    return value


def _normalize_positive_line(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _EditValidationError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise _EditValidationError(f"{field_name} must be a positive integer") from exc
    if normalized <= 0:
        raise _EditValidationError(f"{field_name} must be a positive integer")
    return normalized


def _require_edits(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        raise _EditValidationError("edits must be a non-empty array")
    normalized_edits: list[dict[str, Any]] = []
    for index, item in enumerate(raw_edits, start=1):
        if not isinstance(item, dict):
            raise _EditValidationError(f"edits[{index}] must be an object")
        new_string = _require_string_allow_empty(item, "new_string")
        old_string = _normalize_optional_string(item.get("old_string"), f"edits[{index}].old_string")
        before_context = _normalize_optional_string(item.get("before_context"), f"edits[{index}].before_context")
        after_context = _normalize_optional_string(item.get("after_context"), f"edits[{index}].after_context")
        start_line = _normalize_positive_line(item.get("start_line"), f"edits[{index}].start_line")
        end_line = _normalize_positive_line(item.get("end_line"), f"edits[{index}].end_line")

        has_text_target = old_string is not None
        has_line_target = start_line is not None or end_line is not None

        if has_text_target == has_line_target:
            raise _EditValidationError(
                f"edits[{index}] must target either old_string or start_line/end_line"
            )
        if has_text_target:
            if old_string == "":
                raise _EditValidationError(f"edits[{index}].old_string must not be empty")
            if start_line is not None or end_line is not None:
                raise _EditValidationError(
                    f"edits[{index}] cannot mix old_string with start_line/end_line"
                )
        if has_line_target:
            if start_line is None or end_line is None:
                raise _EditValidationError(
                    f"edits[{index}] requires both start_line and end_line when targeting lines"
                )
            if end_line < start_line:
                raise _EditValidationError(
                    f"edits[{index}].end_line must be greater than or equal to start_line"
                )
            if before_context is not None or after_context is not None:
                raise _EditValidationError(
                    f"edits[{index}] cannot use before_context/after_context with line targeting"
                )

        normalized_edits.append(
            {
                "new_string": new_string,
                "old_string": old_string,
                "before_context": before_context,
                "after_context": after_context,
                "start_line": start_line,
                "end_line": end_line,
            }
        )
    return normalized_edits


def _line_start_offsets(content: str) -> list[int]:
    if content == "":
        return [0]
    offsets = [0]
    for index, character in enumerate(content):
        if character == "\n":
            offsets.append(index + 1)
    return offsets


def _find_text_target(
    content: str,
    old_string: str,
    before_context: str | None,
    after_context: str | None,
) -> tuple[int, str]:
    # CRLF fallback: if the file uses Windows line endings but the model emitted
    # LF, transparently re-encode the search strings before locating the target.
    if "\r\n" in content and "\r\n" not in old_string:
        old_string = _match_line_endings(content, old_string)
        if before_context is not None:
            before_context = _match_line_endings(content, before_context)
        if after_context is not None:
            after_context = _match_line_endings(content, after_context)

    normalized_content = _normalize_quotes(content)
    normalized_old_string = _normalize_quotes(old_string)
    normalized_before = _normalize_quotes(before_context) if before_context is not None else None
    normalized_after = _normalize_quotes(after_context) if after_context is not None else None

    matches: list[int] = []
    search_start = 0
    while True:
        found_index = normalized_content.find(normalized_old_string, search_start)
        if found_index == -1:
            break
        search_end = found_index + len(normalized_old_string)
        if normalized_before is not None:
            before_start = found_index - len(normalized_before)
            if before_start < 0 or normalized_content[before_start:found_index] != normalized_before:
                search_start = found_index + 1
                continue
        if normalized_after is not None:
            after_end = search_end + len(normalized_after)
            if normalized_content[search_end:after_end] != normalized_after:
                search_start = found_index + 1
                continue
        matches.append(found_index)
        search_start = found_index + 1

    if not matches:
        context_hint = ""
        if before_context is not None or after_context is not None:
            context_hint = " with the provided surrounding context"
        raise _EditValidationError(f"Target text was not found{context_hint}.")
    if len(matches) > 1:
        raise _EditValidationError(
            "Target text matched multiple locations. Provide more context or use start_line/end_line for an exact block."
        )

    start_index = matches[0]
    actual_old_string = content[start_index:start_index + len(old_string)]
    return start_index, actual_old_string


def _resolve_text_target(
    content: str,
    old_string: str,
    new_string: str,
    before_context: str | None,
    after_context: str | None,
) -> tuple[int, int, str]:
    """Resolve a text-target edit to absolute (start, end, replacement) in ``content``."""
    start_index, actual_old_string = _find_text_target(content, old_string, before_context, after_context)
    replacement_text = _preserve_quote_style(old_string, actual_old_string, new_string)
    # Ensure the replacement uses the same line endings as the surrounding file.
    replacement_text = _match_line_endings(content, replacement_text)
    end_index = start_index + len(actual_old_string)
    if replacement_text == "" and not actual_old_string.endswith("\n") and content[end_index:end_index + 1] == "\n":
        end_index += 1
    if actual_old_string == replacement_text and end_index == start_index + len(actual_old_string):
        raise _EditValidationError("No changes to make for one of the requested edits.")
    return start_index, end_index, replacement_text


def _resolve_line_range(
    content: str, start_line: int, end_line: int, new_string: str
) -> tuple[int, int]:
    """Resolve a line-range edit to absolute (start, end) offsets in ``content``."""
    offsets = _line_start_offsets(content)
    total_lines = _count_lines(content)
    if total_lines == 0:
        if start_line == 1 and end_line == 1:
            return 0, 0
        raise _EditValidationError("Cannot target lines in an empty file unless start_line=end_line=1")
    if end_line > total_lines:
        raise _EditValidationError(
            f"Line range {start_line}-{end_line} is outside the file (total lines: {total_lines})"
        )
    start_index = offsets[start_line - 1]
    end_index = offsets[end_line] if end_line < len(offsets) else len(content)
    return start_index, end_index


def execute_apply_text_edits(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    raw_path = _require_string(arguments, "path")
    path = resolve_path(raw_path, root_dir)
    edits = _require_edits(arguments)

    if not path.exists():
        raise FileNotFoundError(f"File does not exist: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")

    try:
        # newline="" disables universal-newlines so CRLF files keep their
        # \r\n in memory; otherwise we would silently rewrite the document
        # with LF on save and break Windows line endings.
        with path.open("r", encoding="utf-8", newline="") as fh:
            original_content = fh.read()
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Cannot edit file as UTF-8 text: {path}. Use write_file for full replacement or run_command for binary handling."
        ) from exc

    # ──────────────────────────────────────────────────────────────────────
    # Resolve every edit against the ORIGINAL content first, then apply them
    # in reverse positional order. Doing it this way prevents the classic
    # "shift" bug: when a prior edit changes the number of lines, line
    # numbers (and character offsets) computed by the model against the
    # original file no longer line up with the in-flight buffer. Resolving
    # everything up-front and replaying in descending order keeps every
    # remaining offset valid while we splice.
    # ──────────────────────────────────────────────────────────────────────
    resolved: list[dict[str, Any]] = []
    for index, edit in enumerate(edits, start=1):
        prepared_new_string = _prepare_replacement_text(path, edit["new_string"])
        if edit["old_string"] is not None:
            start_index, end_index, replacement_text = _resolve_text_target(
                original_content,
                edit["old_string"],
                prepared_new_string,
                edit["before_context"],
                edit["after_context"],
            )
            resolved.append(
                {
                    "index": index,
                    "mode": "text",
                    "start": start_index,
                    "end": end_index,
                    "replacement": replacement_text,
                    "target_line": original_content.count("\n", 0, start_index) + 1,
                }
            )
            continue

        start_line = int(edit["start_line"])
        end_line = int(edit["end_line"])
        start_index, end_index = _resolve_line_range(
            original_content, start_line, end_line, prepared_new_string
        )
        # Match the file's line endings so we never inject LF into a CRLF block.
        replacement_text = _match_line_endings(original_content, prepared_new_string)
        resolved.append(
            {
                "index": index,
                "mode": "line_range",
                "start": start_index,
                "end": end_index,
                "replacement": replacement_text,
                "start_line": start_line,
                "end_line": end_line,
            }
        )

    # Validate that no two edits overlap in the original content.
    overlap_check = sorted(resolved, key=lambda r: (r["start"], r["end"]))
    for i in range(1, len(overlap_check)):
        prev = overlap_check[i - 1]
        curr = overlap_check[i]
        if curr["start"] < prev["end"]:
            raise _EditValidationError(
                f"edits[{prev['index']}] and edits[{curr['index']}] overlap in the original "
                f"file. Split them into separate calls or merge them into one edit."
            )

    # Apply in descending order so earlier offsets remain valid throughout.
    updated_content = original_content
    for r in sorted(resolved, key=lambda r: r["start"], reverse=True):
        updated_content = updated_content[: r["start"]] + r["replacement"] + updated_content[r["end"]:]

    # Reconstruct the per-edit report in the original (input) order.
    applied_edits: list[dict[str, Any]] = []
    for r in resolved:
        if r["mode"] == "text":
            applied_edits.append(
                {"index": r["index"], "mode": "text", "target_line": r["target_line"]}
            )
        else:
            applied_edits.append(
                {
                    "index": r["index"],
                    "mode": "line_range",
                    "start_line": r["start_line"],
                    "end_line": r["end_line"],
                }
            )

    if updated_content == original_content:
        raise ValueError("Original and edited file are identical. Failed to apply edits.")

    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(updated_content)
    return {
        "path": str(path),
        "status": "success",
        "operation": "update",
        "edit_count": len(applied_edits),
        "line_count": _count_lines(updated_content),
        "size_bytes": path.stat().st_size,
        "applied_edits": applied_edits,
    }

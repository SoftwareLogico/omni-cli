from __future__ import annotations

from pathlib import Path
from typing import Any

from sot_cli.tools.editor.text_utils import (
    _apply_edit_to_text,
    _find_actual_string,
    _prepare_replacement_text,
    _preserve_quote_style,
)
from sot_cli.tools.utils.path_helpers import resolve_path
from sot_cli.tools.utils.validators import (
    _normalize_boolean,
    _require_string,
    _require_string_allow_empty,
)


def execute_edit_file(arguments: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    raw_path = _require_string(arguments, "path")
    old_string = _require_string_allow_empty(arguments, "old_string")
    new_string = _require_string_allow_empty(arguments, "new_string")
    replace_all = _normalize_boolean(arguments.get("replace_all"), default=False, field_name="replace_all")
    path = resolve_path(raw_path, root_dir)

    if old_string == new_string:
        raise ValueError("No changes to make: old_string and new_string are exactly the same.")
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}.")

    if not path.exists():
        if old_string != "":
            raise FileNotFoundError(f"File does not exist: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        content_to_write = _prepare_replacement_text(path, new_string)
        path.write_text(content_to_write, encoding="utf-8")
        return {
            "path": str(path),
            "status": "success",
            "operation": "create",
            "occurrences_replaced": 1,
            "size_bytes": path.stat().st_size,
        }

    try:
        file_content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Cannot edit file as UTF-8 text: {path}. Use write_file for full replacement or run_command for binary handling."
        ) from exc

    if old_string == "":
        if file_content != "":
            raise ValueError(
                "Cannot use empty old_string on a non-empty existing file. "
                "Use write_file for a full replacement or provide more context in old_string."
            )
        replacement_text = _prepare_replacement_text(path, new_string)
        path.write_text(replacement_text, encoding="utf-8")
        return {
            "path": str(path),
            "status": "success",
            "operation": "update",
            "occurrences_replaced": 1,
            "replace_all": False,
            "size_bytes": path.stat().st_size,
        }

    actual_old_string = _find_actual_string(file_content, old_string)
    if actual_old_string is None:
        raise ValueError(
            "The text to replace was not found exactly in the file.\n"
            f"Searched text:\n{old_string}"
        )

    matches = file_content.count(actual_old_string)
    if matches > 1 and not replace_all:
        raise ValueError(
            f"Found {matches} matches for old_string. Use replace_all=true or provide more unique surrounding context."
        )

    replacement_text = _prepare_replacement_text(
        path,
        _preserve_quote_style(old_string, actual_old_string, new_string),
    )
    updated_content = _apply_edit_to_text(file_content, actual_old_string, replacement_text, replace_all)

    if updated_content == file_content:
        raise ValueError("Original and edited file are identical. Failed to apply edit.")

    path.write_text(updated_content, encoding="utf-8")
    return {
        "path": str(path),
        "status": "success",
        "operation": "update",
        "occurrences_replaced": matches if replace_all else 1,
        "replace_all": replace_all,
        "size_bytes": path.stat().st_size,
    }

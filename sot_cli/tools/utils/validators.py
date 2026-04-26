from __future__ import annotations

from typing import Any



def _require_string(arguments: dict[str, Any], key: str, strip: bool = True) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip() if strip else value


def _require_string_allow_empty(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _ensure_no_arguments(arguments: dict[str, Any]) -> None:
    if arguments:
        raise ValueError("This tool does not accept arguments")


def _normalize_boolean(value: Any, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


def _normalize_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not (0.0 <= normalized <= 2.0):
        raise ValueError(f"{field_name} must be between 0.0 and 2.0")
    return normalized


def _normalize_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return normalized


def _normalize_pages_argument(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("pages must be a non-empty string like '1-5' or '3'")
    return value.strip()


def _normalize_timeout_seconds(value: Any, default_timeout_seconds: int) -> int | None:
    if value is None:
        return default_timeout_seconds
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be an integer number of seconds")
    try:
        timeout_seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be an integer number of seconds") from exc
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be >= 0")
    if timeout_seconds == 0:
        return None
    return timeout_seconds

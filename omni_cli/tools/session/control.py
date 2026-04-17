from __future__ import annotations

from pathlib import Path
from typing import Any

from omni_cli.config import KNOWN_PROVIDERS
from omni_cli.session_store import _UNSET
from omni_cli.tools.utils.path_helpers import resolve_path
from omni_cli.tools.utils.validators import (
    _ensure_no_arguments,
    _normalize_float,
    _normalize_positive_int,
    _require_string,
)


def execute_get_session_state(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
) -> dict[str, Any]:
    _ensure_no_arguments(arguments)
    record = runtime.sessions.load(session_id)
    provider_summaries = []
    for provider_name in KNOWN_PROVIDERS:
        provider = runtime.config.provider(provider_name)
        provider_summaries.append(
            {
                "name": provider.name,
                "enabled": provider.enabled,
                "model": provider.model,
                "temperature": provider.temperature,
                "max_output_tokens": provider.max_output_tokens,
                "base_url": provider.base_url,
            }
        )

    return {
        "session_id": record.id,
        "title": record.title,
        "provider": record.provider,
        "model": record.model,
        "temperature": record.temperature,
        "max_output_tokens": record.max_output_tokens,
        "source_entry_count": len(record.source_entries),
        "source_entries": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "path": entry.value,
                "label": entry.label,
                "recursive": entry.recursive,
                "added_at": entry.added_at,
            }
            for entry in record.source_entries
        ],
        "providers": provider_summaries,
    }


def execute_update_session(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
) -> dict[str, Any]:
    if not arguments:
        raise ValueError("At least one session field must be provided.")

    record = runtime.sessions.load(session_id)
    provider = arguments.get("provider")
    model = arguments.get("model")
    title = arguments.get("title")
    temperature = arguments.get("temperature")
    max_output_tokens = arguments.get("max_output_tokens")

    if provider is not None:
        if not isinstance(provider, str) or provider not in KNOWN_PROVIDERS:
            raise ValueError(f"provider must be one of: {', '.join(KNOWN_PROVIDERS)}")
        provider_config = runtime.config.provider(provider)
        if not provider_config.enabled:
            raise ValueError(f"Provider is not configured: {provider}")
        if model is None and provider != record.provider:
            model = provider_config.model
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"Provider {provider} has no default model configured. Provide model explicitly."
                )

    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        model = model.strip()

    if title is not None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        title = title.strip()

    if temperature is not None:
        temperature = _normalize_float(temperature, field_name="temperature")

    if max_output_tokens is not None:
        max_output_tokens = _normalize_positive_int(max_output_tokens, field_name="max_output_tokens")

    updated = runtime.sessions.update_session(
        session_id,
        title=title if title is not None else _UNSET,
        provider=provider if provider is not None else _UNSET,
        model=model if model is not None else _UNSET,
        temperature=temperature if temperature is not None else _UNSET,
        max_output_tokens=max_output_tokens if max_output_tokens is not None else _UNSET,
    )
    return {
        "session_id": updated.id,
        "title": updated.title,
        "provider": updated.provider,
        "model": updated.model,
        "temperature": updated.temperature,
        "max_output_tokens": updated.max_output_tokens,
    }


def execute_detach_path(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
    root_dir: Path,
) -> dict[str, Any]:
    raw_path = _require_string(arguments, "path")
    path = resolve_path(raw_path, root_dir)
    record, removed = runtime.sessions.remove_source_entry(session_id, path=path)
    return {
        "session_id": record.id,
        "detached_path": str(path),
        "entry_id": removed.id,
        "source_entries": len(record.source_entries),
    }


def execute_attach_path(
    arguments: dict[str, Any],
    runtime: Any,
    session_id: str,
    root_dir: Path,
) -> dict[str, Any]:
    path = _require_string(arguments, "path")
    recursive = bool(arguments.get("recursive", True))
    label = arguments.get("label")
    resolved = resolve_path(path, root_dir)
    record = runtime.sessions.attach_path(
        session_id=session_id,
        target_path=resolved,
        label=str(label) if isinstance(label, str) and label.strip() else None,
        recursive=recursive,
    )
    return {
        "session_id": record.id,
        "attached_path": str(resolved),
        "recursive": recursive,
        "source_entries": len(record.source_entries),
    }

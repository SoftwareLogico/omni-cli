from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from omni_cli.config.prompts import AGENT_SYSTEM_PROMPT, JB_SYSTEM_PROMPT, RUNTIME_RULES, SUB_AGENT_SYSTEM_PROMPT
from omni_cli.utils.text import _count_lines


def build_system_prompt() -> str:
    return JB_SYSTEM_PROMPT.strip()


def build_orchestration_rules(is_sub_agent: bool = False) -> str:
    parts = []
    if is_sub_agent:
        parts.append(SUB_AGENT_SYSTEM_PROMPT.strip())
    else:
        parts.append(AGENT_SYSTEM_PROMPT.strip())
    parts.append(RUNTIME_RULES.strip())
    return "\n\n".join(parts)

    
def build_user_turn_message(user_prompt: str, source_index: str, source_contents: str = "") -> str:
    parts = ["USER REQUEST", user_prompt.strip(), "", source_index.strip()]
    if source_contents.strip():
        parts.extend(["", source_contents.strip()])
    return "\n".join(parts).strip()


def build_sot_user_message(
    tracked_files: dict[str, str],
    tracked_media: dict[str, list[dict[str, Any]]],
    media_file_count: int = 0,
) -> dict[str, Any]:
    """
    Build the SoT user message. Rebuilt after tool calls execute, before the model's next response.

    Returns a message dict with role=user. Content is either a string (text only)
    or a list of content parts (text + image_url + input_audio + video_url etc)
    when there's multimodal media tracked.

    tracked_files: {absolute_path: file_content_from_disk}
    tracked_media: {absolute_path: content parts from read_text_file}
    media_file_count: number of distinct media FILES (not parts)
    """
    text_sections = ["=== SOURCE OF TRUTH ==="]

    file_count = len(tracked_files) + media_file_count
    if tracked_files or tracked_media:
        text_sections.append(f"Files tracked: {file_count}")
        for fpath, content in tracked_files.items():
            line_count = _count_lines(content)
            size_bytes = len(content.encode("utf-8"))
            text_sections.append(
                "\n".join(
                    [
                        f"--- FILE: {fpath} ({line_count} lines, {size_bytes} bytes) ---",
                        content,
                        f"--- END: {fpath} ---",
                    ]
                )
            )
    else:
        text_sections.append("No files tracked yet.")

    sot_text = "\n\n".join(text_sections)

    if not tracked_media:
        return {"role": "user", "content": sot_text}

    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": section}
        for section in text_sections
    ]
    content_parts.extend(_build_media_content_parts(tracked_media))
    content_parts.append({"type": "text", "text": "=== END SOURCE OF TRUTH ==="})
    return {"role": "user", "content": content_parts}


def _build_media_content_parts(tracked_media: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    content_parts: list[dict[str, Any]] = []
    for path, parts in tracked_media.items():
        if not parts:
            continue

        intro_text = _build_media_intro_text(path, parts)
        intro_replaced = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if not intro_replaced and part.get("type") == "text":
                content_parts.append({"type": "text", "text": intro_text})
                intro_replaced = True
                continue
            content_parts.append(part)

        if not intro_replaced:
            content_parts.append({"type": "text", "text": intro_text})

    return content_parts


def _build_media_intro_text(path: str, parts: list[dict[str, Any]]) -> str:
    original_text = next(
        (
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ),
        f"Supplemental content from read_text_file for {path}.",
    )

    metadata: list[str] = []
    file_path = Path(path)
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type:
        metadata.append(f"mime={mime_type}")
    try:
        metadata.append(f"size={file_path.stat().st_size} bytes")
    except OSError:
        pass

    payload_types = sorted(
        {
            str(part.get("type", "")).strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("type", "")).strip() and part.get("type") != "text"
        }
    )
    if payload_types:
        metadata.append(f"parts={','.join(payload_types)}")

    if not metadata:
        return original_text
    return f"{original_text} meta: {'; '.join(metadata)}"
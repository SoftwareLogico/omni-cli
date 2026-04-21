from __future__ import annotations

import mimetypes
import os
import platform
import socket
import getpass
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
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
    # Append host environment block (best-effort) so payloads always include it
    host_context = build_host_environment_prompt()
    if host_context:
        parts.append(host_context)
    return "\n\n".join(parts)


def build_host_environment_prompt() -> str:
    """Build a compact host environment block (best-effort)."""
    def _safe_call(func, *a, **kw):
        try:
            return func(*a, **kw)
        except Exception:
            return None

    def _zoneinfo_utc_available() -> bool:
        try:
            ZoneInfo("UTC")
            return True
        except Exception:
            return False

    lines: list[str] = [
        "HOST ENVIRONMENT (best-effort; may be partial if some fields are unavailable)",
        "Use this information as practical operating context for paths, shell commands, and environment-sensitive decisions.",
    ]

    def add_line(label: str, value: str | None) -> None:
        if value:
            cleaned = str(value).strip()
            if cleaned:
                lines.append(f"- {label}: {cleaned}")

    add_line("Operating system", _normalize_os_name(_safe_call(platform.system)))
    add_line("OS release", _safe_call(platform.release))
    add_line("OS version", _safe_call(platform.version))
    add_line("Architecture", _safe_call(platform.machine))
    add_line("Hostname", _safe_call(socket.gethostname))

    try:
        local_now = datetime.now().astimezone()
        add_line("Current local date and time", local_now.isoformat(timespec="seconds"))
        add_line("Current local date", local_now.date().isoformat())
        add_line("Current local weekday", local_now.strftime("%A"))
        add_line("Current local ISO weekday", str(local_now.isoweekday()))
        if _zoneinfo_utc_available():
            utc_now = local_now.astimezone(ZoneInfo("UTC"))
            add_line("Current UTC date and time", utc_now.isoformat(timespec="seconds"))
            add_line("Current UTC date", utc_now.date().isoformat())
            add_line("Current UTC weekday", utc_now.strftime("%A"))
            add_line("Current UTC ISO weekday", str(utc_now.isoweekday()))
        else:
            utc_now = datetime.utcnow()
            add_line("Current UTC date and time", utc_now.isoformat(timespec="seconds"))
            add_line("Current UTC date", utc_now.date().isoformat())
            add_line("Current UTC weekday", utc_now.strftime("%A"))
            add_line("Current UTC ISO weekday", str(utc_now.isoweekday()))
        try:
            tzname = local_now.tzinfo.tzname(local_now) if local_now.tzinfo else None
            if tzname:
                add_line("Timezone", tzname)
        except Exception:
            pass
    except Exception:
        pass

    add_line("Username", _safe_call(getpass.getuser) or os.environ.get("USER") or os.environ.get("USERNAME"))
    add_line("User home directory", _safe_call(lambda: str(Path.home())))
    add_line("Current working directory", _safe_call(os.getcwd))
    add_line("Default shell", os.environ.get("SHELL"))
    add_line("Active shell", _detect_active_shell())
    add_line("Terminal", os.environ.get("TERM_PROGRAM") or os.environ.get("TERM"))
    add_line("Locale", os.environ.get("LANG"))
    add_line("Python executable", _safe_call(lambda: os.path.realpath(sys.executable)))

    return "\n".join(lines).strip()


def _detect_active_shell() -> str | None:
    """Detect the shell that will actually execute run_command commands."""
    system = platform.system().lower()

    if system == "windows":
        # Check if we're inside PowerShell by looking for its env vars
        if os.environ.get("PSModulePath"):
            ps_version = os.environ.get("PSVersion")
            if ps_version:
                return f"PowerShell {ps_version}"
            return "PowerShell"
        comspec = os.environ.get("COMSPEC", "")
        if comspec:
            return f"CMD ({comspec})"
        return "CMD"

    # Unix-like: check SHELL env var
    shell_path = os.environ.get("SHELL", "")
    if shell_path:
        shell_name = os.path.basename(shell_path)
        return shell_name  # e.g. "bash", "zsh", "fish"

    return None


def _normalize_os_name(system_name: str | None) -> str | None:
    if not system_name:
        return None
    lowered = system_name.strip().lower()
    if lowered == "darwin":
        return "macOS"
    if lowered == "windows":
        return "Windows"
    if lowered == "linux":
        return "Linux"
    return system_name.strip()

    
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
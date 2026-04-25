from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from typing import Any

from sot_cli.config import KNOWN_PROVIDERS
from sot_cli.runtime import AppRuntime
from sot_cli.tools.utils.validators import _require_string


DELEGATED_TASK_WRAPPER = """Delegated sub-agent execution rules:
- Stay strictly inside the paths explicitly named in the task. Do not add extra roots or directories unless the task explicitly allows it.
- If the task says to use only one tool family, obey that exactly.
- If the task provides keywords, filters, extensions, or size limits, apply them from the first call instead of starting with a broad unfiltered scan.
- For list_dir discovery, prefer narrow filtered calls over one broad inventory dump. Split by keyword or extension group if needed.
- For list_dir discovery, prefer narrow filtered calls over one broad inventory dump. Split by keyword or extension group if needed.
- If a result is broad or irrelevant, narrow the query instead of repeating the same call.
- If you fail {attempts} times without making progress, stop and return the best partial findings you have.
- Return only the compact format requested by the task.

Task:
"""
# NOTE: This module now only prepares agent folders and launches the `run_task`
# CLI primitive. Response generation and usage reporting is handled by
# `sot_cli.cli._run_task`, which writes `response.md` in the agent folder.


def _write_response_md(
    parent_session_dir: Path,
    child_session_id: str,
    status: str,
    task_prompt: str,
    result: str,
    usage: dict[str, Any],
    error: str,
) -> Path:
    """Write a standardized `response.md` under
    `<sessions>/<parent>/agents/<child>/response.md`.
    """
    child_dir = parent_session_dir / "agents" / child_session_id
    child_dir.mkdir(parents=True, exist_ok=True)
    md_path = child_dir / "response.md"

    formatted_prompt = task_prompt.replace("\n", "\n> ")
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = [
        f"# Delegated Task Report: {child_session_id}",
        f"**Status:** {status.upper()}",
        f"**Completed At:** {now_str}",
        "",
        "## Usage",
        f"- Total Tokens: {usage.get('total_tokens', 0)}",
        f"- Cost: ${usage.get('cost', 0.0)}",
        "",
        "## Result",
        result if result else (error or "No result provided."),
    ]

    if error and result:
        lines.extend(["", "## Error Log", error])

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def _resolve_delegate_runtime(runtime: AppRuntime, parent_session_id: str, provider_override: Any) -> tuple[str, str, float | None, int | None]:
    parent_session = runtime.sessions.load(parent_session_id)
    if provider_override is None:
        return (
            parent_session.provider,
            parent_session.model,
            parent_session.temperature,
            parent_session.max_output_tokens,
        )

    if not isinstance(provider_override, str) or provider_override not in KNOWN_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(KNOWN_PROVIDERS)}")

    provider_config = runtime.config.provider(provider_override)
    if not provider_config.enabled:
        raise ValueError(f"Provider is not configured: {provider_override}")
    if not provider_config.model:
        raise ValueError(f"Provider {provider_override} has no default model configured.")

    return (
        provider_override,
        provider_config.model,
        provider_config.temperature,
        provider_config.max_output_tokens,
    )


def execute_delegate_task(arguments: dict[str, Any], runtime: AppRuntime, parent_session_id: str) -> dict[str, Any]:
    task_prompt = _require_string(arguments, "task_prompt")
    attempts = int(arguments.get("attempts", 2))
    background = bool(arguments.get("background", False))
    wrapped_task_prompt = DELEGATED_TASK_WRAPPER.replace("{attempts}", str(attempts)) + task_prompt.strip()

    # 1. Definir la carpeta de agentes del padre
    agents_dir = runtime.paths.sessions_dir / parent_session_id / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    # 2. Generar agent_id (agent_1, agent_2...)
    existing = [d.name for d in agents_dir.iterdir() if d.is_dir() and d.name.startswith("agent_")]
    agent_id = f"agent_{len(existing) + 1}"

    parent_session = runtime.sessions.load(parent_session_id)

    # 3. Crear la sesión del agente dentro de la carpeta agents/ del padre
    original_sessions_dir = runtime.sessions.sessions_dir
    runtime.sessions.sessions_dir = agents_dir
    try:
        temp_session = runtime.sessions.create_session(
            title=f"delegate {parent_session_id}",
            provider=arguments.get("provider") or parent_session.provider,
            model=parent_session.model,
        )
        # Alinear el ID interno con el nombre de la carpeta (agent_1)
        old_dir = agents_dir / temp_session.id
        # Update session id so internal metadata matches the readable folder name
        temp_session.id = agent_id
        runtime.sessions.save(temp_session)

        import shutil
        try:
            if old_dir.exists():
                shutil.rmtree(old_dir)
        except Exception:
            # Best-effort cleanup; if it fails, proceed anyway
            pass
        new_agent_dir = agents_dir / agent_id
    finally:
        runtime.sessions.sessions_dir = original_sessions_dir

    # 4. Preparar comando y entorno
    env = os.environ.copy()
    env["SOT_SESSIONS_DIR"] = str(agents_dir)  # El hijo verá esta carpeta como su raíz de sesiones

    command = [
        sys.executable, "-m", "sot_cli",
        "--config", str(runtime.paths.config_file),
        "run_task", agent_id, wrapped_task_prompt,
    ]

    # Ensure agent folder exists and open agent.log to capture child output
    new_agent_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(new_agent_dir / "agent.log", "w")

    if background:
        subprocess.Popen(
            command,
            cwd=str(runtime.paths.root_dir),
            env=env,
            start_new_session=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        log_file.close()
        return {"status": "started", "agent_id": agent_id}

    # Sincrónico
    subprocess.run(
        command,
        cwd=str(runtime.paths.root_dir),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    log_file.close()
    return {"status": "completed", "agent_id": agent_id}
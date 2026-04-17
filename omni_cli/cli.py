from __future__ import annotations

import asyncio
from argparse import ArgumentParser, Namespace
from datetime import datetime
import json
from pathlib import Path
import signal
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from typing import Any

from omni_cli.prompting import prepare_turn_request
from omni_cli.query import ConversationState, run_tool_loop
from omni_cli.runtime import AppRuntime, bootstrap_runtime
from omni_cli.sot import is_sot_block_content, load_sot_state_from_request_json
from omni_cli.source_of_truth import build_source_bundle, SourceBundle
from omni_cli.session_store import SessionRecord


console = Console()
error_console = Console(stderr=True)

_COMMAND_NAMES = {
    "prompt",
    "chat",
    "status",
    "command",
    "run_task",
    "sot_attach",
    "sot_show",
    "sot_delete",
}

# Xterm "modified other keys" sequence for Ctrl+Enter. prompt_toolkit maps
# this to Enter/ControlM, but preserves the raw escape sequence in event.data.
_CTRL_ENTER_DATA = {"\x1b[27;5;13~"}


def main(argv: list[str] | None = None) -> int:
    normalized_argv = _normalize_argv_for_default_prompt(argv)
    parser = _build_parser()
    args = parser.parse_args(normalized_argv)

    try:
        return _dispatch(args)
    except Exception as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        return 1


def _normalize_argv_for_default_prompt(argv: list[str] | None) -> list[str] | None:
    """Insert the implicit `prompt` command when the user omits it.

    Examples:
    - omni-cli --provider openrouter -> omni-cli prompt --provider openrouter
    - omni-cli SESSION_ID -> omni-cli prompt SESSION_ID
    """
    if argv is None:
        raw_args = sys.argv[1:]
        should_return_none = True
    else:
        raw_args = list(argv)
        should_return_none = False

    if not raw_args:
        return None if should_return_none else raw_args

    insert_at = 0
    while insert_at < len(raw_args):
        token = raw_args[insert_at]

        if token in {"-h", "--help"}:
            return None if should_return_none else raw_args

        if token == "--config":
            insert_at += 2
            continue

        if token.startswith("--config="):
            insert_at += 1
            continue

        if token in _COMMAND_NAMES:
            return None if should_return_none else raw_args

        normalized = raw_args[:insert_at] + ["prompt"] + raw_args[insert_at:]
        return normalized if not should_return_none else normalized

    return None if should_return_none else raw_args


def _submit_shortcut_help_text() -> str:
    if sys.platform == "darwin":
        return "Use Alt+Enter to send. If it doesn't work, use Esc then Enter."
    if sys.platform.startswith("win"):
        return "Use Ctrl+Enter to send. Fallback: Esc then Enter."
    return "Use Ctrl+Enter to send if your terminal supports it; otherwise Esc then Enter."


def _format_capability_line(cap) -> str:
    """Build a compact capability summary line from ProviderCapability."""
    parts: list[str] = []
    if cap.context_length:
        parts.append(f"ctx={_format_token_count(cap.context_length)}")
    if cap.max_completion_tokens:
        parts.append(f"max_out={_format_token_count(cap.max_completion_tokens)}")
    if cap.modality:
        parts.append(f"modality={cap.modality}")
    if cap.parameter_count:
        parts.append(f"params={cap.parameter_count}")
    if cap.quantization:
        parts.append(f"quant={cap.quantization}")

    flags: list[str] = []
    if cap.supports_tools:
        flags.append("tools")
    if cap.supports_images:
        flags.append("vision")
    if cap.supports_pdfs:
        flags.append("pdf")
    if cap.supports_audio:
        flags.append("audio")
    if cap.supports_video:
        flags.append("video")

    if flags:
        parts.append("capabilities=" + ",".join(flags))

    return " | ".join(parts) if parts else "capabilities=unknown"


def _format_token_count(n: int) -> str:
    """Format token count as compact string: 131072 -> '128k', 1000000 -> '1M'.

    Tries 1000-base first (128000 -> 128k), then 1024-base (131072 -> 128k).
    """
    if n >= 1_000_000:
        if n % 1_000_000 == 0:
            return f"{n // 1_000_000}M"
        if n % (1024 * 1024) == 0:
            return f"{n // (1024 * 1024)}M"
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        if n % 1000 == 0:
            return f"{n // 1000}k"
        if n % 1024 == 0:
            return f"{n // 1024}k"
        return f"{n // 1000}k"
    return str(n)


def _load_chat_history_from_request_jsons(session_dir: Path) -> list[dict[str, Any]] | None:
    """Reconstruct chat_history from persisted request/response JSON files.

    Reads request.json (contains [system, ...history..., SoT]) and
    response-chunks.json (last assistant response as streaming chunks).
    Strips system prompt and ephemeral SoT block. Concatenates chunks
    into the final assistant message. Returns the complete chat_history
    or None if the session JSON files are missing/corrupt.
    """
    request_path = session_dir / "request.json"
    chunks_path = session_dir / "response-chunks.json"

    if not request_path.exists():
        return None

    try:
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        messages = request_data.get("payload", {}).get("messages", [])
    except (json.JSONDecodeError, KeyError, OSError):
        return None

    if not messages:
        return None

    # Extract chat_history: skip system prompt, skip SoT blocks
    chat_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        content = msg.get("content", "")
        if role == "user" and is_sot_block_content(content):
            continue
        chat_messages.append(msg)

    # Reconstruct last assistant response from streaming chunks
    if chunks_path.exists():
        try:
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            assistant_msg = _reconstruct_assistant_from_chunks(chunks)
            if assistant_msg:
                chat_messages.append(assistant_msg)
        except (json.JSONDecodeError, OSError):
            pass

    return chat_messages if chat_messages else None


def _reconstruct_assistant_from_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Concatenate streaming chunks into a single assistant message."""
    text_parts: list[str] = []
    tool_state: dict[int, dict[str, Any]] = {}

    for chunk in chunks:
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or choice.get("message") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            for tc in delta.get("tool_calls", []):
                index = int(tc.get("index", 0))
                entry = tool_state.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    entry["id"] = tc["id"]
                func = tc.get("function", {})
                if func.get("name"):
                    entry["function"]["name"] += func["name"]
                if func.get("arguments"):
                    entry["function"]["arguments"] += func["arguments"]

    text = "".join(text_parts)
    tool_calls = [tool_state[i] for i in sorted(tool_state)] if tool_state else []

    if not text and not tool_calls:
        return None

    msg: dict[str, Any] = {"role": "assistant", "content": text if text else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _replay_conversation(history: list[dict[str, Any]]) -> None:
    """Print the loaded conversation so the user sees where they left off."""
    console.print("[dim]─── session history ───[/dim]")
    for msg in history:
        role = msg.get("role", "")
        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                console.print(f"[bold cyan]you>[/bold cyan] {content}")
        elif role == "assistant":
            text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                console.print(f"[blue]assistant>[/blue] [dim]called {', '.join(names)}[/dim]")
            if text:
                console.print(f"[blue]assistant>[/blue] {text}")
        elif role == "tool":
            content = msg.get("content", "")
            console.print(f"[dim]tool> {content}[/dim]")
    console.print("[dim]─── end of history ───[/dim]\n")


# ── Dispatch ─────────────────────────────────────────────────────────────


def _dispatch(args: Namespace) -> int:
    runtime = bootstrap_runtime(args.config)

    async def _run_with_cleanup():
        try:
            await runtime.mcp.start()
            if args.command in {None, "prompt", "chat"}:
                return await _run_prompt(
                    runtime, getattr(args, "session_id", None), getattr(args, "title", None),
                    getattr(args, "provider", None), getattr(args, "model", None), getattr(args, "no_tools", False),
                )
            if args.command == "command":
                return await _run_command_turn(
                    runtime, args.session_id, args.prompt, args.provider,
                    args.model, args.no_tools, getattr(args, "disable_delegation", False)
                )
            if args.command == "run_task":
                return await _run_task(runtime, args.agent_id, args.prompt)
        finally:
            try:
                await runtime.mcp.close()
            except Exception:
                pass

    if args.command in {None, "prompt", "chat", "command", "run_task"}:
        return asyncio.run(_run_with_cleanup())

    if args.command == "status":
        _print_status(runtime)
        return 0

    if args.command == "sot_attach":
        record = runtime.sessions.attach_path(
            session_id=args.session_id, target_path=args.target,
            label=args.label, recursive=not args.non_recursive,
        )
        console.print(f"[green]Attached to SoT in session {record.id}[/green]")
        return 0

    if args.command == "sot_show":
        _print_sot(runtime, args.session_id)
        return 0

    if args.command == "sot_delete":
        ref = args.ref
        try:
            record, removed = runtime.sessions.remove_source_entry(args.session_id, entry_id=ref)
            console.print(f"[green]Removed '{removed.label}' ({removed.id}) from SoT.[/green]")
        except FileNotFoundError:
            try:
                record, removed = runtime.sessions.remove_source_entry(args.session_id, path=ref)
                console.print(f"[green]Removed '{removed.label}' ({removed.id}) from SoT.[/green]")
            except FileNotFoundError:
                console.print(f"[red]Error: No entry with ID or path '{ref}' in session {args.session_id}.[/red]")
                return 1
        return 0

    raise ValueError(f"Unknown command: {args.command}")


# ── Parser ───────────────────────────────────────────────────────────────


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="omni-cli")
    parser.add_argument("--config", default=None, help="Path to omni.toml")
    subparsers = parser.add_subparsers(dest="command")

    _PROVIDER_CHOICES = ["lmstudio", "openrouter", "openai", "xai", "ollama"]

    # prompt — main interactive mode
    prompt = subparsers.add_parser("prompt", help="Start or resume an interactive session")
    prompt.add_argument("session_id", nargs="?", default=None, help="Resume an existing session by ID")
    prompt.add_argument("--title", default=None, help="Title for the new session")
    prompt.add_argument("--provider", choices=_PROVIDER_CHOICES, default=None)
    prompt.add_argument("--model", default=None)
    prompt.add_argument("--no-tools", action="store_true", help="Plain chat without tool loop")

    # chat — alias for prompt
    chat = subparsers.add_parser("chat", help="Alias for prompt")
    chat.add_argument("session_id", nargs="?", default=None)
    chat.add_argument("--title", default=None)
    chat.add_argument("--provider", choices=_PROVIDER_CHOICES, default=None)
    chat.add_argument("--model", default=None)
    chat.add_argument("--no-tools", action="store_true")

    # command — one-shot turn for multi-agent / automation
    cmd = subparsers.add_parser("command", help="Run a single turn (multi-agent / automation)")
    cmd.add_argument("session_id", help="Session ID to run against")
    cmd.add_argument("prompt", help="The prompt to send")
    cmd.add_argument("--provider", choices=_PROVIDER_CHOICES, default=None)
    cmd.add_argument("--model", default=None)
    cmd.add_argument("--no-tools", action="store_true")
    cmd.add_argument(
        "--disable-delegation",
        action="store_true",
        help="Disable the delegate_task tool to prevent infinite recursion",
    )

    # run_task — primitive to execute a previously-created agent session
    run_task = subparsers.add_parser("run_task", help="Run an agent by ID inside the sessions directory (agent_N)")
    run_task.add_argument("agent_id", help="Agent ID to run (e.g. agent_1)")
    run_task.add_argument("prompt", help="The task prompt")

    # status — list sessions
    subparsers.add_parser("status", help="List sessions")

    # sot_attach — add file/dir to session SoT
    sot_a = subparsers.add_parser("sot_attach", help="Attach a path to a session's Source of Truth")
    sot_a.add_argument("session_id")
    sot_a.add_argument("target", help="File or directory path to attach")
    sot_a.add_argument("--label", default=None)
    sot_a.add_argument("--non-recursive", action="store_true")

    # sot_show — list SoT entries
    sot_s = subparsers.add_parser("sot_show", help="Show Source of Truth entries for a session")
    sot_s.add_argument("session_id")

    # sot_delete — remove entry from SoT
    sot_d = subparsers.add_parser("sot_delete", help="Remove an entry from the Source of Truth")
    sot_d.add_argument("session_id")
    sot_d.add_argument("ref", help="Short ID or full path of the entry to remove")

    return parser


# ── Status ───────────────────────────────────────────────────────────────


def _print_status(runtime: AppRuntime) -> None:
    records = runtime.sessions.list_sessions()
    if not records:
        console.print("[dim]No sessions found.[/dim]")
        return

    table = Table(title="Sessions")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Updated")
    table.add_column("SoT", justify="right")

    for record in records:
        table.add_row(
            record.id,
            record.title,
            record.provider,
            record.model,
            record.updated_at,
            str(len(record.source_entries)),
        )

    console.print(table)
    console.print("[dim]Resume a session: omni-cli <session_id>[/dim]")


# ── SoT display ──────────────────────────────────────────────────────────


def _print_sot(runtime: AppRuntime, session_id: str) -> None:
    record = runtime.sessions.load(session_id)
    if not record.source_entries:
        console.print(f"[dim]No SoT entries in session {session_id}.[/dim]")
        return

    table = Table(title=f"Source of Truth — {session_id}")
    table.add_column("ID", style="cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Path / Ref", style="green")

    for entry in record.source_entries:
        table.add_row(entry.id, entry.kind, entry.value)

    console.print(table)


# ── Command turn (one-shot) ──────────────────────────────────────────────


async def _run_command_turn(
    runtime: AppRuntime,
    session_id: str,
    prompt: str,
    provider_name: str | None,
    model_override: str | None,
    no_tools: bool,
    disable_delegation: bool = False,
    conversation_state: ConversationState | None = None,
) -> int:
    record = runtime.sessions.load(session_id)
    bundle = build_source_bundle(record)
    request = prepare_turn_request(
        config=runtime.config,
        session=record,
        user_prompt=prompt,
        bundle=bundle,
        provider_name=provider_name,
        model_override=model_override,
        enable_tools=not no_tools,
        disable_delegation=disable_delegation,
    )

    if conversation_state is None:
        from omni_cli.query import ConversationState
        conversation_state = ConversationState()

    result = await run_tool_loop(runtime, request, console, conversation_state=conversation_state)

    if result.usage:
        # Leer el estado de los agentes
        agents_dir = runtime.paths.sessions_dir / session_id / "agents"
        agent_statuses = []
        if agents_dir.exists():
            for agent_folder in sorted(agents_dir.iterdir()):
                if agent_folder.is_dir():
                    report_file = agent_folder / "response.md"
                    if report_file.exists():
                        content = report_file.read_text(encoding="utf-8")
                        status_line = next((line for line in content.splitlines() if line.startswith("**Status:**")), "")
                        status = status_line.replace("**Status:**", "").strip() if status_line else "UNKNOWN"
                        agent_statuses.append((agent_folder.name, status))

        usage_table = Table(title="Turn Summary & Usage")
        usage_table.add_column("Metric")
        usage_table.add_column("Value", justify="right")
        
        main_tokens = result.usage.get("total_tokens", 0) - result.usage.get("delegated_total_tokens", 0)
        total_tokens = result.usage.get("total_tokens", 0)
        latest_prompt_tokens = result.usage.get("latest_prompt_tokens")
        
        usage_table.add_row("Main Agent Tokens", str(main_tokens))
        if result.usage.get("delegated_total_tokens"):
            usage_table.add_row("Sub-Agents Tokens", str(result.usage.get("delegated_total_tokens")))
        usage_table.add_row("Total Tokens", str(total_tokens), style="bold cyan")
        usage_table.add_row("Total Cost", f"${result.usage.get('cost', 0.0):.6f}", style="bold green")

        # --- NUEVO: Barra de límite de contexto ---
        adapter = runtime.provider_adapter(record.provider, record.model)
        ctx_len = adapter.capability.context_length
        if ctx_len and ctx_len > 0 and isinstance(latest_prompt_tokens, (int, float)):
            pct = min(100, int((latest_prompt_tokens / ctx_len) * 100))
            filled = int((pct / 100) * 10)
            bar = "█" * filled + "░" * (10 - filled)
            color = "red" if pct > 90 else "yellow" if pct > 75 else "green"
            usage_table.add_row(
                "Context Limit",
                f"[{color}]{bar} {pct}% ({int(latest_prompt_tokens)}/{ctx_len})[/{color}]",
            )

        # --- NUEVO: Mostrar archivos en el SoT ---
        sot_files = set(conversation_state.sot.tracked_files.keys()).union(conversation_state.sot.tracked_media.keys())
        if sot_files:
            try:
                usage_table.add_section()
            except Exception:
                pass
            usage_table.add_row("SoT Tracked Files", str(len(sot_files)), style="bold magenta")
            for fpath in sorted(sot_files):
                fname = Path(fpath).name
                usage_table.add_row(f"  📄 {fname}", "[dim]in context[/dim]")
        
        if agent_statuses:
            try:
                usage_table.add_section()
            except Exception:
                pass
            usage_table.add_row("Agents Used", str(len(agent_statuses)))
            for agent_name, status in agent_statuses:
                color = "green" if status.upper() == "SUCCESS" else "red"
                usage_table.add_row(f"  {agent_name}", f"[{color}]{status}[/{color}]")
                
        console.print(usage_table)

    return 0


async def _run_task(runtime: AppRuntime, agent_id: str, prompt: str) -> int:
    """Headless executor that ensures request.json and response.md are created."""
    try:
        # 1. Load the agent session (SessionStore should point to OMNI_SESSIONS_DIR)
        record = runtime.sessions.load(agent_id)
        agent_dir = runtime.sessions.sessions_dir / agent_id

        # 2. Prepare request with an empty SourceBundle
        bundle = SourceBundle()
        request = prepare_turn_request(
            config=runtime.config,
            session=record,
            user_prompt=prompt,
            bundle=bundle,
            enable_tools=True,
            disable_delegation=True,
        )

        # 3. Execute the tool loop in task mode
        result = await run_tool_loop(runtime, request, console, is_task=True)
        # If run_tool_loop signals an error (instead of raising), preserve usage but mark status
        if getattr(result, "is_error", False):
            status, result_text, error_text = "error", "", getattr(result, "text", "")
        else:
            status, result_text, error_text = "success", getattr(result, "text", ""), ""
        usage = getattr(result, "usage", {})
    except Exception as e:
        status, result_text, error_text = "error", "", str(e)
        usage = {}

    # 4. Write response.md (parent session dir is the parent of the SessionStore dir)
    from omni_cli.tools.session.delegate import _write_response_md
    parent_session_dir = runtime.sessions.sessions_dir.parent
    _write_response_md(parent_session_dir, agent_id, status, prompt, result_text, usage, error_text)
    return 0 if status == "success" else 1


# ── Interactive prompt ───────────────────────────────────────────────────


async def _run_prompt(
    runtime: AppRuntime,
    session_id: str | None,
    title: str | None,
    provider_name: str | None,
    model_override: str | None,
    no_tools: bool,
) -> int:
    if session_id:
        record = runtime.sessions.load(session_id)
        updated_provider = provider_name or record.provider
        if model_override:
            updated_model = model_override
        elif provider_name and provider_name != record.provider:
            updated_model = runtime.config.provider(updated_provider).model
            if not updated_model:
                raise ValueError(
                    f"Provider {updated_provider} has no default model configured. Pass --model explicitly."
                )
        else:
            updated_model = record.model
        if updated_provider != record.provider or updated_model != record.model:
            record = runtime.sessions.update_session(
                session_id,
                provider=updated_provider,
                model=updated_model,
            )
    else:
        provider = provider_name or runtime.config.runtime.primary_provider
        provider_config = runtime.config.provider(provider)
        model = model_override or provider_config.model
        if not model:
            raise ValueError(f"Provider {provider} has no default model configured. Pass --model explicitly.")
        session_title = title or f"prompt {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        record = runtime.sessions.create_session(
            session_title,
            provider=provider,
            model=model,
            temperature=provider_config.temperature,
            max_output_tokens=provider_config.max_output_tokens,
        )

    active_provider = record.provider
    active_model = record.model

    # Detect model capabilities before showing the banner
    adapter = await runtime.provider_adapter_async(active_provider, active_model)
    cap_line = _format_capability_line(adapter.capability)

    console.print(
        Panel.fit(
            f"session={record.id} | provider={active_provider} | model={active_model}\n"
            f"{cap_line}\n"
            f"tools={'off' if no_tools else 'on'}\n"
            f"{_submit_shortcut_help_text()} Press Ctrl+C on an empty prompt to leave.",
            title="omni-cli",
        )
    )

    kb = KeyBindings()
    current_turn_task: asyncio.Task[int] | None = None
    turn_interrupt_requested = False
    previous_sigint_handler = signal.getsignal(signal.SIGINT)

    @kb.add("enter")
    def _handle_enter(event) -> None:
        if event.data in _CTRL_ENTER_DATA:
            event.current_buffer.validate_and_handle()
            return
        event.current_buffer.insert_text("\n")

    @kb.add("escape", "enter")
    def _submit_multiline(event) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("c-c")
    def _handle_prompt_ctrl_c(event) -> None:
        if event.current_buffer.text.strip():
            event.current_buffer.reset()
            return
        event.app.exit(exception=KeyboardInterrupt, style="class:exiting")

    def _handle_sigint(_signum, _frame) -> None:
        nonlocal current_turn_task, turn_interrupt_requested
        if current_turn_task is not None and not current_turn_task.done():
            turn_interrupt_requested = True
            current_turn_task.cancel()
            return
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _handle_sigint)

    prompt_session = PromptSession(key_bindings=kb, multiline=True)

    # Try to resume conversation from the persisted session JSON files
    session_state = ConversationState()
    session_dir = runtime.paths.sessions_dir / record.id
    loaded_history = _load_chat_history_from_request_jsons(session_dir)
    if loaded_history:
        session_state.chat_history = loaded_history
        _replay_conversation(loaded_history)
    loaded_sot = load_sot_state_from_request_json(session_dir)
    if loaded_sot is not None:
        session_state.sot = loaded_sot

    try:
        while True:
            try:
                prompt = await prompt_session.prompt_async(
                    HTML("<b><cyan>you&gt;</cyan></b> "),
                    prompt_continuation="... ",
                )
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]chat ended[/dim]")
                return 0

            prompt = prompt.rstrip()
            normalized_prompt = prompt.strip()
            if not normalized_prompt:
                continue

            current_record = runtime.sessions.load(record.id)
            active_provider = current_record.provider
            active_model = current_record.model
            current_turn_task = asyncio.create_task(
                _run_command_turn(
                    runtime, record.id, prompt,
                    active_provider, active_model, no_tools,
                    conversation_state=session_state,
                )
            )
            try:
                await current_turn_task
            except asyncio.CancelledError:
                if not turn_interrupt_requested:
                    raise
                console.print("\n[bold yellow]Turn aborted by user (Ctrl+C).[/bold yellow]")
            except Exception as exc:
                # --- NUEVO BLOQUE: BLINDAJE DEL SISTEMA ---
                # Si falla la red, el proveedor da error 500, o hay un bug crítico,
                # lo mostramos en rojo pero NO rompemos el bucle.
                error_console.print(f"\n[bold red]System Error:[/bold red] {exc}")
                console.print("[dim]The session is still active. You can try again or change your prompt.[/dim]")
            finally:
                current_turn_task = None
                turn_interrupt_requested = False
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)

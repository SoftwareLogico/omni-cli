from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from rich.console import Console

from omni_cli.constants import (
    FALLBACK_DELEGATED_MAX_ROUNDS,
    FALLBACK_DELEGATED_REPEAT_LIMIT,
    FALLBACK_REPEAT_LIMIT,
    SESSION_MUTATION_TOOLS,
)
from omni_cli.providers.base import ProviderAdapter, ProviderRequest
from omni_cli.runtime import AppRuntime
from omni_cli.sot import (
    SoTState,
    begin_turn,
    build_sot_payload_message,
    merge_session_into_tracked,
    refresh_tracked_state_from_disk,
    update_tracked_from_tool_result,
)
from omni_cli.tools.core import ToolExecutionResult
from omni_cli.tools import ToolRegistry


@dataclass(frozen=True)
class RoundObservation:
    signature: str
    summary: str
    is_error: bool


@dataclass
class TurnResult:
    text: str = ""
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False


@dataclass
class ConversationState:
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    sot: SoTState = field(default_factory=SoTState)


@dataclass
class StreamRenderState:
    reasoning_started: bool = False
    text_started: bool = False


def _strip_reasoning_overlap(existing_text: str, incoming_text: str) -> str:
    if not incoming_text:
        return ""

    if not existing_text:
        return incoming_text

    max_overlap = min(len(existing_text), len(incoming_text))
    for overlap in range(max_overlap, 0, -1):
        if existing_text.endswith(incoming_text[:overlap]):
            return incoming_text[overlap:]
    return incoming_text


async def run_single_turn(
    adapter: ProviderAdapter,
    request: ProviderRequest,
    console: Console,
    show_thinking: bool = True,
    show_full: bool = True,
) -> TurnResult:
    result = TurnResult()
    render_state = StreamRenderState()
    _tool_call_header_shown: set[int] = set()

    async for event in adapter.stream_turn(request):
        if event.type == "reasoning_delta":
            text = str(event.payload.get("text", ""))
            details = event.payload.get("details") or []
            if text:
                deduped_text = _strip_reasoning_overlap(result.reasoning, text)
                if deduped_text:
                    result.reasoning += deduped_text
                    if show_thinking:
                        if not render_state.reasoning_started:
                            console.print("thinking:", style="dim", end=" ")
                            render_state.reasoning_started = True
                        console.print(deduped_text, style="dim", markup=False, end="")
            if isinstance(details, list):
                for detail in details:
                    if isinstance(detail, dict):
                        result.reasoning_details.append(detail)
        elif event.type == "text_delta":
            text = str(event.payload.get("text", ""))
            result.text += text
            if text:
                if show_thinking and render_state.reasoning_started and not render_state.text_started:
                    console.out("\n\n")
                render_state.text_started = True
                console.out(text, end="")
        elif event.type == "tool_call":
            tool_calls = event.payload.get("tool_calls") or []
            result.tool_calls.extend(tool_calls)
            if show_full:
                for tool_delta in tool_calls:
                    index = int(tool_delta.get("index", 0))
                    func = tool_delta.get("function") or {}
                    name = func.get("name", "")
                    args_chunk = func.get("arguments", "")
                    if name and index not in _tool_call_header_shown:
                        _tool_call_header_shown.add(index)
                        console.print(f"\n[dim]tool_call: {name}([/dim]", end="")
                    if args_chunk:
                        console.out(args_chunk, end="")
        elif event.type == "usage":
            usage = event.payload.get("usage") or {}
            if isinstance(usage, dict):
                _replace_usage_snapshot(result.usage, usage)
                _store_latest_usage_snapshot(result.usage, usage)
        elif event.type == "error":
            raise RuntimeError(str(event.payload.get("message", "Unknown provider error")))

    if show_full and _tool_call_header_shown:
        console.print("[dim])[/dim]")
    if result.text or (show_thinking and render_state.reasoning_started):
        console.out("\n")

    return result


async def run_tool_loop(
    runtime: AppRuntime,
    request: ProviderRequest,
    console: Console,
    max_rounds: int | None = None,
    conversation_state: ConversationState | None = None,
    is_task: bool = False,
) -> TurnResult:
    if conversation_state is None:
        conversation_state = ConversationState()

    begin_turn(conversation_state.sot)

    # ── SoT Step 1: Capture — save user prompt to permanent history ──
    conversation_state.chat_history.append({"role": "user", "content": request.user_prompt})

    if not request.enable_tools:
        adapter = await runtime.provider_adapter_async(request.provider_name, request.model)
        if not is_task:
            merge_session_into_tracked(runtime, request, conversation_state.sot)
            refresh_tracked_state_from_disk(runtime, adapter.capability, conversation_state.sot)
        plain_request = ProviderRequest(
            provider_name=request.provider_name,
            model=request.model,
            session_id=request.session_id,
            system_prompt=request.system_prompt,
            orchestration_rules=request.orchestration_rules,
            user_prompt=request.user_prompt,
            source_index=request.source_index,
            source_contents=request.source_contents,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            stream=request.stream,
            enable_tools=False,
            disable_delegation=request.disable_delegation,
            tools=[],
            conversation_messages=_build_payload_messages(conversation_state, request),
        )
        turn_result = await run_single_turn(
            adapter,
            plain_request,
            console,
            show_thinking=runtime.config.tools.show_thinking,
            show_full=runtime.config.tools.show_full,
        )
        # SoT Step 6: Clean — save assistant response to permanent history
        assistant_message = {"role": "assistant", "content": turn_result.text}
        if turn_result.reasoning_details:
            assistant_message["reasoning_details"] = turn_result.reasoning_details
        elif turn_result.reasoning:
            assistant_message["reasoning"] = turn_result.reasoning
        conversation_state.chat_history.append(assistant_message)
        return turn_result

    result = TurnResult()
    executed_any_tool = False
    previous_round_fingerprint: tuple[RoundObservation, ...] | None = None
    repeated_round_count = 0
    _tools_cfg = runtime.config.tools
    if max_rounds is None:
        max_rounds = _tools_cfg.max_rounds
    effective_max_rounds = _effective_tool_loop_max_rounds(request, max_rounds, _tools_cfg.delegated_max_rounds)
    repeat_round_limit = _repeat_round_limit(request, _tools_cfg.repeat_limit, _tools_cfg.delegated_repeat_limit)

    for round_index in range(effective_max_rounds):
        adapter = await runtime.provider_adapter_async(request.provider_name, request.model)
        registry = ToolRegistry(
            runtime,
            request.session_id,
            adapter.capability,
            request.model,
            request.disable_delegation,
            conversation_state.sot,  # <--- AÑADIR ESTO
        )

        if not is_task:
            merge_session_into_tracked(runtime, request, conversation_state.sot)
            refresh_tracked_state_from_disk(runtime, adapter.capability, conversation_state.sot)

        # ── SoT Steps 2-4: Assemble + Inject — build ephemeral payload each round ──
        payload_messages = _build_payload_messages(conversation_state, request)

        round_request = ProviderRequest(
            provider_name=request.provider_name,
            model=request.model,
            session_id=request.session_id,
            system_prompt=request.system_prompt,
            orchestration_rules=request.orchestration_rules,
            user_prompt=request.user_prompt,
            source_index=request.source_index,
            source_contents=request.source_contents,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            stream=request.stream,
            enable_tools=True,
            disable_delegation=request.disable_delegation,
            tools=registry.schemas(),
            conversation_messages=payload_messages,
        )

        # ── SoT Step 5: Inference ──
        if round_request.stream:
            completion = await _run_streaming_round(
                adapter,
                round_request,
                console,
                show_thinking=runtime.config.tools.show_thinking,
                show_full=runtime.config.tools.show_full,
            )
        else:
            completion = await adapter.complete_turn(round_request)

        # ── SoT Step 6: Clean — save assistant to permanent history (no SoT) ──
        assistant_message = dict(completion.assistant_message)
        assistant_message.setdefault("role", "assistant")
        conversation_state.chat_history.append(assistant_message)

        if completion.usage:
            _merge_usage_totals(result.usage, completion.usage)
            _store_latest_usage_snapshot(result.usage, completion.usage)

        # ── No tool calls: turn is done ──
        if not completion.tool_calls:
            result.text = completion.text
            result.tool_calls = []
            if executed_any_tool and not (completion.text or "").strip():
                console.print(
                    "[bold yellow]Warning:[/bold yellow] The model returned no final text after executing tool work. "
                    "Review the tool output above and decide the next prompt manually."
                )
            if completion.text and not round_request.stream:
                console.print(completion.text)
            return result

        # ── Execute each tool call, rebuild SoT after EACH one ──
        result.tool_calls = completion.tool_calls
        console.print(f"[cyan]Round {round_index + 1}: executing {len(completion.tool_calls)} tool call(s)[/cyan]")
        same_round_cache: dict[str, tuple[str, ToolExecutionResult, str]] = {}
        round_observations: list[RoundObservation] = []

        for tool_call in completion.tool_calls:
            function = tool_call.get("function") or {}
            tool_name = str(function.get("name", "unknown"))
            tool_args_raw = str(function.get("arguments", "{}"))
            console.print(f"[blue]assistant requested[/blue] {tool_name} {tool_args_raw}")

            tool_signature = _build_tool_call_signature(tool_call)
            cached_execution = same_round_cache.get(tool_signature)

            if cached_execution is None:
                tool_call_id, tool_result = await registry.execute_tool_call(tool_call)
                executed_any_tool = True

                console.print(f"[dim]tool {tool_result.name} -> {'error' if tool_result.is_error else 'ok'}[/dim]")

                tool_summary = _build_tool_result_summary(tool_result)
                same_round_cache[tool_signature] = (
                    tool_call_id,
                    _clone_tool_execution_result(tool_result),
                    tool_summary,
                )
                round_observations.append(
                    RoundObservation(
                        signature=tool_signature,
                        summary=tool_summary,
                        is_error=tool_result.is_error,
                    )
                )
            else:
                original_tool_call_id, cached_tool_result, cached_summary = cached_execution
                tool_call_id = str(tool_call.get("id", ""))
                tool_result = _clone_tool_execution_result(cached_tool_result)
                tool_summary = f"duplicate of {original_tool_call_id} -> {cached_summary}"
                console.print(
                    f"[dim]tool {tool_result.name} -> reused duplicate result from {original_tool_call_id}[/dim]"
                )

            # Tool result to permanent history: metadata only (SoT Rule 2)
            conversation_state.chat_history.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": tool_summary,
            })

            if cached_execution is None and tool_name == "delegate_task":
                delegated_usage = _extract_usage_from_tool_result(tool_result)
                if delegated_usage:
                    _merge_delegated_usage_totals(result.usage, delegated_usage)

            # ── Update SoT after THIS tool call ──
            if cached_execution is not None:
                continue

            # If session mutation, merge session SoT entries into tracked state
            if tool_name in SESSION_MUTATION_TOOLS and not tool_result.is_error:
                if not is_task:
                    merge_session_into_tracked(runtime, request, conversation_state.sot)
                    _refresh_request_from_session(runtime, request)

            # Update tracked files/media from tool effects
            update_tracked_from_tool_result(conversation_state.sot, tool_name, tool_result)

        round_fingerprint = tuple(round_observations)
        if round_fingerprint and round_fingerprint == previous_round_fingerprint:
            repeated_round_count += 1
        else:
            repeated_round_count = 0
        previous_round_fingerprint = round_fingerprint or None

        if round_fingerprint and repeated_round_count >= repeat_round_limit:
            message = _build_repeated_rounds_message(
                round_observations,
                repeat_count=repeated_round_count + 1,
                delegated=request.disable_delegation,
            )
            if request.disable_delegation:
                result.is_error = True
                result.text = message
                result.tool_calls = []
                return result
            console.print(f"[bold yellow]Warning:[/bold yellow] {message}")
            result.text = message
            result.tool_calls = []
            return result

    message = _build_tool_loop_exhausted_message(effective_max_rounds, request.disable_delegation)
    if request.disable_delegation:
        result.is_error = True
        result.text = message
        result.tool_calls = []
        return result
    console.print(f"[bold yellow]Warning:[/bold yellow] {message}")
    result.text = message
    result.tool_calls = []
    return result


def _refresh_request_from_session(runtime: AppRuntime, request: ProviderRequest) -> None:
    """After session mutation tools, refresh ALL request metadata from session."""
    from omni_cli.message_builder import build_system_prompt, build_orchestration_rules
    session = runtime.sessions.load(request.session_id)
    request.provider_name = session.provider
    request.model = session.model
    if hasattr(session, "temperature") and session.temperature is not None:
        request.temperature = session.temperature
    if hasattr(session, "max_output_tokens") and session.max_output_tokens is not None:
        request.max_output_tokens = session.max_output_tokens
    # Rebuild system prompt and rules so user overrides stay current; respect sub-agent mode
    request.system_prompt = build_system_prompt()
    request.orchestration_rules = build_orchestration_rules(is_sub_agent=request.disable_delegation)


def _effective_tool_loop_max_rounds(
    request: ProviderRequest,
    default_max_rounds: int,
    delegated_max_rounds: int = FALLBACK_DELEGATED_MAX_ROUNDS,
) -> int:
    if request.disable_delegation:
        return min(default_max_rounds, delegated_max_rounds)
    return default_max_rounds


def _repeat_round_limit(
    request: ProviderRequest,
    main_limit: int = FALLBACK_REPEAT_LIMIT,
    delegated_limit: int = FALLBACK_DELEGATED_REPEAT_LIMIT,
) -> int:
    if request.disable_delegation:
        return delegated_limit
    return main_limit


def _build_tool_call_signature(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function") or {}
    name = str(function.get("name", "")).strip()
    arguments = _normalize_tool_arguments(function.get("arguments") or "{}")
    return f"{name}:{arguments}"


def _normalize_tool_arguments(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return raw_arguments.strip()
        return json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return json.dumps(raw_arguments, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _clone_tool_execution_result(tool_result: ToolExecutionResult) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=tool_result.name,
        content=tool_result.content,
        record_content=tool_result.record_content,
        supplemental_messages=list(tool_result.supplemental_messages),
        is_error=tool_result.is_error,
    )


def _build_repeated_rounds_message(
    round_observations: list[RoundObservation],
    repeat_count: int,
    delegated: bool,
) -> str:
    if round_observations:
        first = round_observations[0]
        preview = f"{first.signature} -> {first.summary}"
    else:
        preview = "no unique tool activity"
    if len(preview) > 320:
        preview = preview[:317] + "..."

    prefix = "Delegated sub-agent aborted" if delegated else "Tool loop stopped"
    return (
        f"{prefix} after {repeat_count} repeated rounds without progress. "
        f"Repeated pattern: {preview}. Try a different tool, narrower filters, or a different search strategy."
    )


def _build_tool_loop_exhausted_message(max_rounds: int, delegated: bool) -> str:
    if delegated:
        return (
            f"Delegated sub-agent exceeded its fail-fast budget of {max_rounds} rounds without reaching a final answer. "
            "Return the partial findings you have or try a narrower task."
        )
    return (
        f"Tool loop exceeded {max_rounds} rounds without reaching a final answer. "
        "Try a narrower request or a different tool strategy."
    )


def _merge_usage_totals(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, bool):
            current = target.get(key)
            target[key] = bool(current) or value
            continue

        if isinstance(value, (int, float)):
            current = target.get(key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                target[key] = current + value
            else:
                target[key] = value
            continue

        if isinstance(value, dict):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _merge_usage_totals(current, value)
            continue

        if key not in target:
            target[key] = value


def _replace_usage_snapshot(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    preserved_latest = {k: v for k, v in target.items() if str(k).startswith("latest_")}
    target.clear()
    for key, value in incoming.items():
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            _replace_usage_snapshot(nested, value)
            target[key] = nested
        else:
            target[key] = value
    target.update(preserved_latest)


def _store_latest_usage_snapshot(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    prompt_tokens = incoming.get("prompt_tokens")
    completion_tokens = incoming.get("completion_tokens")
    total_tokens = incoming.get("total_tokens")

    if isinstance(prompt_tokens, (int, float)) and not isinstance(prompt_tokens, bool):
        target["latest_prompt_tokens"] = prompt_tokens
    if isinstance(completion_tokens, (int, float)) and not isinstance(completion_tokens, bool):
        target["latest_completion_tokens"] = completion_tokens
    if isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
        target["latest_total_tokens"] = total_tokens


def _merge_delegated_usage_totals(target: dict[str, Any], delegated_usage: dict[str, Any]) -> None:
    _merge_usage_totals(target, delegated_usage)

    delegated_numeric_keys = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost",
    )
    delegated_detail_keys = (
        "prompt_tokens_details",
        "completion_tokens_details",
        "cost_details",
    )

    target["delegated_task_count"] = int(target.get("delegated_task_count", 0) or 0) + 1

    for key in delegated_numeric_keys:
        value = delegated_usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            prefixed_key = f"delegated_{key}"
            current = target.get(prefixed_key)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                target[prefixed_key] = current + value
            else:
                target[prefixed_key] = value

    for key in delegated_detail_keys:
        value = delegated_usage.get(key)
        if not isinstance(value, dict):
            continue
        prefixed_key = f"delegated_{key}"
        current = target.get(prefixed_key)
        if not isinstance(current, dict):
            current = {}
            target[prefixed_key] = current
        _merge_usage_totals(current, value)


def _extract_usage_from_tool_result(tool_result: ToolExecutionResult) -> dict[str, Any]:
    try:
        payload = json.loads(tool_result.record_content)
    except (json.JSONDecodeError, TypeError):
        return {}

    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    return {}


# NOTE: session usage persistence has been removed. Usage will be recorded
# inside delegated agent reports (response.md) and provider response files.


def _build_payload_messages(conversation_state: ConversationState, request: ProviderRequest) -> list[dict[str, Any]]:
    """Assemble the ephemeral payload sent to the provider.

    Structure: [system] + [orchestration rules] + chat_history (permanent) + [SoT block (ephemeral)].

    The SoT block is rebuilt from disk every round and NEVER enters chat_history.
    This is the core of the SoT Method (Steps 2-4: State Registry + Assemble + Inject).
    """
    payload: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
    
    if request.orchestration_rules:
        payload.append({"role": "user", "content": request.orchestration_rules})

    # Encontrar el índice del último prompt del usuario para inyectar el SoT justo antes
    last_user_idx = 0
    for i in range(len(conversation_state.chat_history) - 1, -1, -1):
        if conversation_state.chat_history[i].get("role") == "user":
            last_user_idx = i
            break

    # 1. Historial pasado (todo hasta el último user prompt, exclusivo)
    payload.extend(conversation_state.chat_history[:last_user_idx])

    # 2. El SoT (Estado actual del mundo)
    sot_message = build_sot_payload_message(conversation_state.sot)
    if sot_message is not None:
        payload.append(sot_message)

    # 3. El turno actual (último user prompt y lo que venga despues)
    payload.extend(conversation_state.chat_history[last_user_idx:])

    return payload


def _build_tool_result_summary(tool_result: Any) -> str:
    """Build a metadata-only summary. Never include file content."""
    try:
        payload = json.loads(tool_result.record_content)
    except (json.JSONDecodeError, TypeError):
        if tool_result.is_error:
            return f"error: {tool_result.content[:200]}"
        return "ok"

    if tool_result.is_error:
        return f"error: {payload.get('error', 'unknown')}"

    name = tool_result.name

    if name == "read_text_file":
        fpath = payload.get("path", "?")
        ftype = payload.get("type", "text")
        if ftype == "text":
            if payload.get("partial") is True:
                start_line = payload.get("start_line", "?")
                end_line = payload.get("end_line", "?")
                returned_lines = payload.get("returned_lines", "?")
                total_lines = payload.get("total_lines", "?")
                size = payload.get("size_bytes", "?")
                content = payload.get("content", "")
                header = (
                    f"inspected {fpath} lines {start_line}-{end_line} "
                    f"({returned_lines} returned, file has {total_lines} lines, {size} bytes)"
                )
                if content:
                    return f"{header}\n{content}"
                return header
            lines = payload.get("total_lines", "?")
            size = payload.get("size_bytes", "?")
            return f"read {fpath} ({lines} lines, {size} bytes) -> added to SoT"
        elif ftype == "image":
            return f"read image {fpath} ({payload.get('original_size_bytes', '?')} bytes) -> added to SoT"
        elif ftype == "pdf":
            return f"read pdf {fpath} ({payload.get('page_count', '?')} pages) -> added to SoT"
        elif ftype == "notebook":
            return f"read notebook {fpath} ({payload.get('cell_count', '?')} cells) -> added to SoT"
        elif ftype == "audio":
            return f"read audio {fpath} ({payload.get('size_bytes', '?')} bytes) -> added to SoT"
        elif ftype == "video":
            return f"read video {fpath} ({payload.get('size_bytes', '?')} bytes) -> added to SoT"
        elif ftype == "file_unchanged":
            return f"unchanged {fpath}"
        elif ftype == "file_in_sot":
            return f"{fpath} already in SoT — look at the '=== SOURCE OF TRUTH ===' block, do not re-read"
        return f"read {fpath} type={ftype}"

    if name == "read_many_files":
        result_count = payload.get("result_count", "?")
        success_count = payload.get("success_count", "?")
        error_count = payload.get("error_count", "?")
        return f"batch read {success_count}/{result_count} ok ({error_count} errors) -> SoT updated"

    if name == "open_path":
        fpath = payload.get("path", "?")
        application = payload.get("application")
        resolved_application = payload.get("resolved_application")
        if isinstance(resolved_application, str) and resolved_application.strip():
            return f"opened {fpath} with {application} -> {resolved_application}"
        if isinstance(application, str) and application.strip():
            return f"opened {fpath} with {application}"
        return f"opened {fpath} with default application"

    if name == "edit_file":
        fpath = payload.get("path", "?")
        op = payload.get("operation", "update")
        replaced = payload.get("occurrences_replaced", "?")
        size = payload.get("size_bytes", "?")
        return f"{op} {fpath} (replaced {replaced}, now {size} bytes). SoT already has the updated version — do not re-read."

    if name == "apply_text_edits":
        fpath = payload.get("path", "?")
        edit_count = payload.get("edit_count", "?")
        size = payload.get("size_bytes", "?")
        return f"updated {fpath} ({edit_count} atomic edits, now {size} bytes). SoT already has the updated version — do not re-read."

    if name == "write_file":
        fpath = payload.get("path", "?")
        op = payload.get("operation", "write")
        lines = payload.get("line_count", "?")
        size = payload.get("size_bytes", "?")
        return f"{op} {fpath} ({lines} lines, {size} bytes). SoT already has the updated version — do not re-read."

    if name == "list_dir":
        fpath = payload.get("path", "?")
        count = int(payload.get("entry_count", 0) or 0)
        entries = payload.get("entries") or []
        if not isinstance(entries, list) or not entries:
            return f"listed {fpath} (0 entries)"

        summary_lines = [f"listed {fpath} ({count} entries):"]
        for entry in entries[:20]:
            if not isinstance(entry, dict):
                continue
            entry_path = str(entry.get("path") or entry.get("relative_path") or entry.get("name") or "?").strip()
            entry_kind = str(entry.get("kind") or "?").strip()
            entry_size = entry.get("size_bytes")
            size_text = f"{entry_size} bytes" if isinstance(entry_size, int) else "size unknown"
            summary_lines.append(f"- {entry_path} ({entry_kind}, {size_text})")

        if count > 20:
            summary_lines.append(f"... and {count - 20} more entries.")

        return "\n".join(summary_lines)

    if name == "search_code":
        mode = payload.get("mode", "files_with_matches")
        if mode == "content":
            content = payload.get("content", "")
            line_count = payload.get("line_count", 0)
            total = payload.get("total_result_lines", line_count)
            truncated = payload.get("truncated", False)
            if not content:
                return "search: no matches found"
            result = content
            if truncated:
                result += f"\n\n[showing {line_count} of {total} result lines — use offset to paginate]"
            return result
        elif mode == "count":
            match_count = payload.get("match_count", 0)
            file_count = payload.get("file_count", 0)
            content = payload.get("content", "")
            if not content:
                return "search: no matches found"
            return f"{content}\n\n{match_count} matches across {file_count} files"
        else:
            files = payload.get("files", [])
            if not files:
                return "search: no files matched"
            file_count = payload.get("file_count", len(files))
            total = payload.get("total_matches", file_count)
            truncated = payload.get("truncated", False)
            result = f"found {file_count} files:\n" + "\n".join(files)
            if truncated:
                result += f"\n\n[showing {file_count} of {total} — use offset to paginate]"
            return result

    if name == "run_command":
        cmd = payload.get("command", "?")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_code = payload.get("exit_code", "?")
        mode = payload.get("mode", "foreground")
        if mode == "background":
            command_id = payload.get("command_id", "?")
            status = payload.get("status", "starting")
            return f"bg '{cmd}' id={command_id} status={status}"
        timed_out = payload.get("timed_out", False)
        if timed_out:
            return f"'{cmd}' timed out"

        stdout = str(payload.get("stdout", "")).strip()
        stderr = str(payload.get("stderr", "")).strip()

        result_parts = [f"'{cmd}' exit={exit_code}"]

        if stdout:
            stdout_excerpt = stdout[:3000] + ("\n...[stdout truncated]" if len(stdout) > 3000 else "")
            result_parts.append(f"[stdout]\n{stdout_excerpt}")

        if stderr:
            stderr_excerpt = stderr[:1000] + ("\n...[stderr truncated]" if len(stderr) > 1000 else "")
            result_parts.append(f"[stderr]\n{stderr_excerpt}")

        if len(result_parts) == 1:
            stdout_bytes = payload.get("stdout_bytes", 0)
            stderr_bytes = payload.get("stderr_bytes", 0)
            result_parts.append(f"stdout={stdout_bytes}b stderr={stderr_bytes}b")

        return "\n".join(result_parts)

    if name == "list_commands":
        commands = payload.get("commands") or []
        if not isinstance(commands, list) or not commands:
            return "no background commands in this session"
        preview = []
        for item in commands[:5]:
            if not isinstance(item, dict):
                continue
            command_id = item.get("command_id", "?")
            status = item.get("status", "?")
            command = str(item.get("command", "?")).strip()
            if len(command) > 40:
                command = command[:37] + "..."
            preview.append(f"{command_id}:{status}:{command}")
        preview_text = "; ".join(preview) if preview else "no usable entries"
        return f"listed {payload.get('command_count', '?')} background commands -> {preview_text}"

    if name == "read_command_output":
        command_id = payload.get("command_id", "?")
        status = payload.get("status", "?")
        output = str(payload.get("output", "")).strip()
        if not output:
            return f"command {command_id} output empty (status={status})"
        if len(output) > 4000:
            output = output[:4000] + "\n...[truncated]"
        return f"command {command_id} output (status={status})\n{output}"

    if name == "wait_command":
        command_id = payload.get("command_id", "?")
        status = payload.get("status", "?")
        if payload.get("timed_out"):
            return f"waited for {command_id} -> still {status}"
        exit_code = payload.get("exit_code", "?")
        return f"waited for {command_id} -> {status} exit={exit_code}"

    if name == "stop_command":
        command_id = payload.get("command_id", "?")
        status = payload.get("status", "?")
        return f"stop requested for {command_id} -> {status}"

    if name == "list_tasks":
        tasks = payload.get("tasks", [])
        if not tasks:
            return "no delegated tasks found"
        summary = []
        for t in tasks:
            agent_id = t.get("agent_id", "?")
            status = t.get("status", "?")
            summary.append(f"{agent_id}:{status}")
        return f"tasks: {', '.join(summary)}"

    if name == "wait_task":
        agent_id = payload.get("agent_id", "?")
        status = payload.get("status", "?")
        if payload.get("timed_out"):
            return f"waited for {agent_id} -> still {status} (timed out)"
        report = payload.get("report", "")
        return f"waited for {agent_id} -> {status}\n\n{report}"

    if name == "delegate_task":
        agent_id = payload.get("agent_id", "?")
        status = payload.get("status", "?")
        return f"delegated task {agent_id} {status}. Use wait_task to get the result."

    if name == "attach_path_to_source":
        attached_paths = payload.get("attached_paths")
        entries = payload.get("source_entries", "?")
        if isinstance(attached_paths, list) and len(attached_paths) > 1:
            return f"attached {len(attached_paths)} paths (entries={entries})"
        attached = payload.get("attached_path", "?")
        return f"attached {attached} (entries={entries})"

    if name == "detach_path_from_source":
        detached_paths = payload.get("detached_paths")
        entries = payload.get("source_entries", "?")
        if isinstance(detached_paths, list) and len(detached_paths) > 1:
            return f"detached {len(detached_paths)} paths (entries={entries})"
        detached = payload.get("detached_path", "?")
        return f"detached {detached} (entries={entries})"

    if name == "delete_file":
        fpath = payload.get("path", "?")
        return f"deleted {fpath}"

    if name == "get_session_state":
        # Filtramos 'providers' porque ocupa muchos tokens y rara vez se necesita
        state_info = {k: v for k, v in payload.items() if k != "providers"}
        return f"Session state: {json.dumps(state_info, ensure_ascii=False)}"

    # Fallback
    return f"ok {json.dumps({k: v for k, v in payload.items() if k not in ('content', 'ok', 'base64')})}"[:200]


# ── Streaming ─────────────────────────────────────────────────────────────

async def _run_streaming_round(
    adapter: ProviderAdapter,
    request: ProviderRequest,
    console: Console,
    show_thinking: bool = True,
    show_full: bool = True,
):
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_details: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    tool_state: dict[int, dict[str, Any]] = {}
    render_state = StreamRenderState()
    _tool_call_header_shown: set[int] = set()

    async for event in adapter.stream_turn(request):
        if event.type == "reasoning_delta":
            text = str(event.payload.get("text", ""))
            details = event.payload.get("details") or []
            if text:
                current_reasoning = "".join(reasoning_parts)
                deduped_text = _strip_reasoning_overlap(current_reasoning, text)
                if deduped_text:
                    reasoning_parts.append(deduped_text)
                    if show_thinking:
                        if not render_state.reasoning_started:
                            console.print("thinking:", style="dim", end=" ")
                            render_state.reasoning_started = True
                        console.print(deduped_text, style="dim", markup=False, end="")
            if isinstance(details, list):
                for detail in details:
                    if isinstance(detail, dict):
                        reasoning_details.append(detail)
        elif event.type == "text_delta":
            text = str(event.payload.get("text", ""))
            if text:
                if show_thinking and render_state.reasoning_started and not render_state.text_started:
                    console.out("\n\n")
                render_state.text_started = True
                text_parts.append(text)
                console.out(text, end="")
        elif event.type == "tool_call":
            for tool_delta in event.payload.get("tool_calls") or []:
                _merge_tool_call_delta(tool_state, tool_delta)
                if show_full:
                    index = int(tool_delta.get("index", 0))
                    func = tool_delta.get("function") or {}
                    name = func.get("name", "")
                    args_chunk = func.get("arguments", "")
                    if name and index not in _tool_call_header_shown:
                        _tool_call_header_shown.add(index)
                        console.print(f"\n[dim]tool_call: {name}([/dim]", end="")
                    if args_chunk:
                        console.out(args_chunk, end="")
        elif event.type == "usage":
            event_usage = event.payload.get("usage") or {}
            if isinstance(event_usage, dict):
                _replace_usage_snapshot(usage, event_usage)
        elif event.type == "error":
            raise RuntimeError(str(event.payload.get("message", "Unknown provider error")))

    text = "".join(text_parts)
    tool_calls = [_finalize_tool_call(tool_state[index]) for index in sorted(tool_state)]
    if show_full and _tool_call_header_shown:
        console.print("[dim])[/dim]")
    if text or (show_thinking and render_state.reasoning_started):
        console.out("\n")

    assistant_message: dict[str, Any] = {"role": "assistant"}
    assistant_message["content"] = text if text else None
    if reasoning_details:
        assistant_message["reasoning_details"] = reasoning_details
    elif reasoning_parts:
        assistant_message["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls

    return type("StreamingCompletion", (), {
        "assistant_message": assistant_message,
        "text": text,
        "tool_calls": tool_calls,
        "usage": usage,
    })()


def _merge_tool_call_delta(tool_state: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    index = int(delta.get("index", 0))
    entry = tool_state.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    tool_id = delta.get("id")
    if isinstance(tool_id, str) and tool_id:
        entry["id"] = tool_id
    tool_type = delta.get("type")
    if isinstance(tool_type, str) and tool_type:
        entry["type"] = tool_type
    function_delta = delta.get("function") or {}
    function_name = function_delta.get("name")
    if isinstance(function_name, str) and function_name:
        entry["function"]["name"] += function_name
    function_arguments = function_delta.get("arguments")
    if isinstance(function_arguments, str) and function_arguments:
        entry["function"]["arguments"] += function_arguments


def _finalize_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tool_call.get("id", ""),
        "type": tool_call.get("type", "function"),
        "function": {
            "name": tool_call.get("function", {}).get("name", ""),
            "arguments": tool_call.get("function", {}).get("arguments", "{}"),
        },
    }


# ── Display helpers ──────────────────────────────────────────────────────

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from sot_cli.message_builder import build_user_turn_message
from sot_cli.providers.base import ProviderCapability, ProviderCompletion, ProviderEvent, ProviderRequest


# ─── Outbound message sanitizer ──────────────────────────────────────────
#
# Tool names whose ``arguments`` payload becomes REDUNDANT once the file
# is in the SoT: re-sending the full file body (or the full list of
# new_string blocks) on every subsequent request is pure token waste —
# the post-mutation file content is already in the next turn's
# '=== SOURCE OF TRUTH ===' block.
#
# The sanitizer keeps the tool_call envelope intact (so the model still
# sees "I called write_file on /x" in its history AND the OpenAI-strict
# tool_call ↔ tool message pairing invariant is preserved) but trims the
# heavy fields out of the arguments JSON. See _trim_mutation_arguments.
_MUTATION_TOOLS_FOR_ARGS_TRIMMING: frozenset[str] = frozenset({
    "edit_files",
    "write_file",
})

# Inline marker the model sees in trimmed arguments. Short, self-explanatory,
# and unique enough to be searchable in transcripts when debugging.
_ELIDED_MARKER = "<elided: in SoT>"

# Generic fallback when JSON parsing of a mutation tool's arguments fails
# (rare — would imply the model emitted malformed JSON in arguments). Stays
# valid JSON so we never break a downstream parser.
_GENERIC_ELIDED_ARGS = '{"_summary": "args elided; result is in SoT"}'


def _trim_mutation_arguments(tool_name: str, raw_arguments: str) -> str:
    """Return a token-light replacement for a mutation tool_call's arguments.

    Strategy (Shape A2):
      * Parse the original arguments JSON.
      * Keep tiny metadata fields the model actually wants to remember
        (paths) so it knows WHICH file(s) it touched on this round.
      * Replace heavy redundant fields (file content, edit_string lists)
        with :data:`_ELIDED_MARKER` — the model can read the post-mutation
        result from the SoT block on the next turn.
      * Re-serialize.

    On any failure (non-JSON args, unexpected shape) fall back to
    :data:`_GENERIC_ELIDED_ARGS` rather than the verbatim original — the
    point of this whole function is to make sure the heavy content does
    NOT round-trip even when something goes wrong with the trim.
    """
    try:
        parsed = json.loads(raw_arguments) if isinstance(raw_arguments, str) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return _GENERIC_ELIDED_ARGS

    if not isinstance(parsed, dict):
        return _GENERIC_ELIDED_ARGS

    if tool_name == "write_file":
        # Schema: {"path": str, "content": str}. Keep path; elide content.
        trimmed: dict[str, Any] = {}
        if "path" in parsed:
            trimmed["path"] = parsed["path"]
        if "content" in parsed:
            trimmed["content"] = _ELIDED_MARKER
        return json.dumps(trimmed, ensure_ascii=False)

    if tool_name == "edit_files":
        # Schema: {"files": [{"path": str, "edits": [...]}, ...]}. Keep
        # the list of paths so the model remembers which files it touched;
        # elide the edits payload of each entry.
        files = parsed.get("files")
        if not isinstance(files, list):
            return _GENERIC_ELIDED_ARGS
        trimmed_files: list[dict[str, Any]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            trimmed_entry: dict[str, Any] = {}
            if "path" in entry:
                trimmed_entry["path"] = entry["path"]
            if "edits" in entry:
                trimmed_entry["edits"] = _ELIDED_MARKER
            trimmed_files.append(trimmed_entry)
        return json.dumps({"files": trimmed_files}, ensure_ascii=False)

    return _GENERIC_ELIDED_ARGS


def _is_effectively_empty_text(value: Any) -> bool:
    """True for values that the strict APIs treat as 'no content'.

    None and whitespace-only strings both count: LM Studio and the
    OpenAI strict validator both expect a non-empty string when an
    assistant message has no tool_calls. The model emitting ``"\\n\\n"``
    next to a stripped tool_call (a thought-bubble that points to
    nothing) is functionally the same problem, so we treat them
    identically here.
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _sanitize_messages_for_provider(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strict-API firewall + SoT-aware mutation-args trimming before send.

    Three independent responsibilities, all required before the chat
    reaches ANY OpenAI-compatible provider:

    1. **Schema strictness — drop empty husks.** Assistant messages with
       ``content: null`` (or whitespace-only) and no ``tool_calls`` are
       rejected by LM Studio (HTTP 500) and by OpenAI strict (HTTP 400).
       Drop them. Tool-role messages with ``content: null`` are coerced
       to ``content: ""`` instead of dropped, because dropping would
       orphan the matching ``tool_call``.

    2. **Tool-call ↔ tool-message pairing invariant.** OpenAI strict
       requires every ``tool_call`` emitted by an assistant to be
       followed by exactly one ``tool``-role message carrying the same
       ``tool_call_id`` (and vice-versa). Streaming interruptions,
       Ctrl+C aborts mid-tool-execution, and message replays can leave
       three pathological shapes in chat_history:

       * a ``tool_call`` with ``arguments: ""`` (stream cut before the
         args delta arrived);
       * a ``tool_call`` with no matching ``tool`` response anywhere;
       * a ``tool_call`` with the same ``id`` as a previous one whose
         response has already been consumed (duplicate from a retry).

       All three trigger HTTP 5xx / 400 from strict providers. We do a
       two-pass walk: pass 1 indexes which ``tool_call_id``s have a
       responding ``tool`` message in this batch; pass 2 emits in
       order, single-use-matching each ``tool_call`` against a pending
       set so duplicates and orphans are dropped silently. Companion
       ``tool`` messages whose call was dropped (or never existed) are
       dropped too.

    3. **Token economy via SoT-aware args trimming.** Every surviving
       ``tool_call`` whose function name is in
       :data:`_MUTATION_TOOLS_FOR_ARGS_TRIMMING` has its ``arguments``
       JSON passed through :func:`_trim_mutation_arguments`, which
       keeps the path metadata and elides the heavy redundant fields
       (file content, edit strings). The tool_call envelope and the
       matching tool-response message both stay intact, so the model
       retains a complete narrative of WHAT it did to WHICH path on
       every turn, without the multi-thousand-token re-paste of file
       content it can already read in the SoT block.

    Notes deliberately NOT done here:

    * ``reasoning`` and ``reasoning_details`` are LEFT ON the message.
      OpenRouter and the Anthropic / GPT-5 reasoning class require
      reasoning details to be round-tripped to maintain reasoning
      continuity; stripping them blindly would silently degrade those
      providers.
    * Messages are shallow-copied; the caller's chat_history is never
      mutated (the same list is reused across rounds and across turns).
    """
    # ── Pass 1: index every tool_call_id that has a matching tool message ──
    # We will only allow a tool_call through if the chat history contains
    # at least one tool message responding to it. Single-use matching in
    # pass 2 prevents one tool message from satisfying multiple duplicate
    # tool_calls.
    tool_response_ids: set[str] = set()
    for entry in messages:
        if not isinstance(entry, dict) or entry.get("role") != "tool":
            continue
        tc_id = entry.get("tool_call_id")
        if isinstance(tc_id, str) and tc_id:
            tool_response_ids.add(tc_id)

    sanitized: list[dict[str, Any]] = []
    pending_tool_call_ids: set[str] = set()
    consumed_tool_call_ids: set[str] = set()

    for original in messages:
        if not isinstance(original, dict):
            # Malformed entry; the provider would reject it anyway. Skip
            # silently rather than crashing the whole payload build.
            continue
        msg = dict(original)
        role = msg.get("role")

        if role == "user":
            if msg.get("content") is None:
                continue
            sanitized.append(msg)
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                surviving_calls: list[dict[str, Any]] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id")
                    if not isinstance(tc_id, str) or not tc_id:
                        # Tool_call without a usable id — provider would
                        # reject and we cannot pair it with a response.
                        continue
                    func = tc.get("function") if isinstance(tc.get("function"), dict) else None
                    if not isinstance(func, dict):
                        continue
                    name = func.get("name", "") if isinstance(func.get("name"), str) else ""
                    args = func.get("arguments", "")
                    if not isinstance(args, str) or args == "":
                        # Stream cut before args delta arrived. Drop —
                        # provider rejects "" args.
                        continue
                    if tc_id not in tool_response_ids:
                        # Orphan: no tool message in this batch will pair
                        # with us. Drop to preserve the strict invariant.
                        continue
                    if tc_id in consumed_tool_call_ids:
                        # Duplicate of a previous tool_call whose response
                        # has already been consumed; the matching tool
                        # message was single-use-paired with the earlier
                        # call. Drop the duplicate.
                        continue
                    if tc_id in pending_tool_call_ids:
                        # Two tool_calls in a row with the same id and no
                        # tool message between them. Drop the second.
                        continue

                    if name in _MUTATION_TOOLS_FOR_ARGS_TRIMMING:
                        new_tc = dict(tc)
                        new_func = dict(func)
                        new_func["arguments"] = _trim_mutation_arguments(name, args)
                        new_tc["function"] = new_func
                        surviving_calls.append(new_tc)
                    else:
                        surviving_calls.append(tc)
                    pending_tool_call_ids.add(tc_id)

                if surviving_calls:
                    msg["tool_calls"] = surviving_calls
                else:
                    msg.pop("tool_calls", None)

            content_is_empty = _is_effectively_empty_text(msg.get("content"))
            has_tool_calls = bool(msg.get("tool_calls"))
            if content_is_empty and not has_tool_calls:
                continue

            sanitized.append(msg)
            continue

        if role == "tool":
            tc_id = msg.get("tool_call_id")
            if not isinstance(tc_id, str) or not tc_id:
                # Malformed tool message without an id — cannot pair.
                continue
            if tc_id not in pending_tool_call_ids:
                # Either no preceding tool_call (orphan) or the call was
                # already consumed by a previous tool message. Drop.
                continue
            pending_tool_call_ids.discard(tc_id)
            consumed_tool_call_ids.add(tc_id)
            if msg.get("content") is None:
                msg["content"] = ""
            sanitized.append(msg)
            continue

        # role == "system" or anything unknown: forward as-is.
        sanitized.append(msg)

    return sanitized


def _write_session_json(label: str, data: Any, session_id: str = "") -> Path:
    """Write a JSON blob to the session directory."""
    import os
    from pathlib import Path as _Path

    sessions_env = os.environ.get("SOT_SESSIONS_DIR")
    if sessions_env:
        sessions_base = _Path(sessions_env).resolve()
    else:
        sessions_base = _Path(".sot-cli/sessions").resolve()

    if session_id:
        base = sessions_base / session_id
    else:
        base = _Path(".sot-cli/session-json").resolve()

    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return path


class OpenAICompatibleAdapter:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.extra_headers = extra_headers or {}
        # Start with nothing — detect_capabilities will populate this
        self.capability = ProviderCapability()
        self._capabilities_detected = False

    async def detect_capabilities(self) -> None:
        """Query the provider's models endpoint to detect what the current model supports."""
        if self._capabilities_detected:
            return
        if self.name == "lmstudio":
            await self._detect_lmstudio_capabilities()
        elif self.name == "openrouter":
            await self._detect_openrouter_capabilities()
        elif self.name == "ollama":
            await self._detect_ollama_capabilities()
        elif self.name == "nvidia":
            await self._detect_nvidia_capabilities()
        elif self.name == "openai":
            # OpenAI's /v1/models endpoint doesn't expose tool/modality flags or
            # context windows in a useful shape, and the same `openai` provider
            # is reused to talk to any OpenAI-compatible service (so probing
            # would also be unreliable). We assume the optimistic defaults of
            # current frontier OpenAI-style models: tools on, vision + PDFs on,
            # 400k context. If a downstream model is more limited, the API
            # itself will reject the unsupported feature at request time —
            # which is the right place to surface that.
            self.capability = ProviderCapability(
                supports_tools=True,
                supports_images=True,
                supports_pdfs=True,
                context_length=400_000,
                modality="text+image->text",
            )
        else:
            # xai and other unknown OpenAI-compatible names — minimal default.
            self.capability = ProviderCapability(supports_tools=True)
        self._capabilities_detected = True

    async def _detect_openrouter_capabilities(self) -> None:
        """OpenRouter: GET /models returns architecture.input_modalities and supported_parameters."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.base_url}/models", headers=headers)
                if resp.status_code == 401:
                    raise ValueError("Invalid API key for OpenRouter.")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch models from OpenRouter (HTTP {resp.status_code}).")
                models = resp.json().get("data",[])
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to OpenRouter at {self.base_url}. Check your internet connection.") from exc

        model_info = None
        for m in models:
            if m.get("id") == self.model or m.get("id", "").startswith(self.model):
                model_info = m
                break

        if model_info is None:
            raise ValueError(f"Model '{self.model}' not found in OpenRouter.")

        arch = model_info.get("architecture", {})
        input_mods = arch.get("input_modalities", [])
        params = model_info.get("supported_parameters", [])
        top = model_info.get("top_provider", {}) or {}

        self.capability = ProviderCapability(
            supports_tools="tools" in params,
            supports_images="image" in input_mods,
            supports_pdfs="file" in input_mods,
            supports_audio="audio" in input_mods,
            supports_video="video" in input_mods,
            context_length=model_info.get("context_length") or top.get("context_length"),
            max_completion_tokens=top.get("max_completion_tokens"),
            modality=arch.get("modality", ""),
        )

    async def _detect_lmstudio_capabilities(self) -> None:
        """LM Studio: Use native API to find loaded models and capabilities."""
        origin = _extract_origin(self.base_url)
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try native v1 API first (LM Studio 0.4.0+)
                resp = await client.get(f"{origin}/api/v1/models", headers=headers)
                if resp.status_code != 200:
                    # Try native v0 API
                    resp = await client.get(f"{origin}/api/v0/models", headers=headers)
                if resp.status_code != 200:
                    # Fallback to OpenAI compat
                    resp = await client.get(f"{origin}/v1/models", headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch models from LM Studio (HTTP {resp.status_code}).")
                data = resp.json()
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to LM Studio at {origin}. Is it running?") from exc

        models_list = data.get("models", data.get("data",[]))
        if not models_list:
            raise ValueError("No models found in LM Studio. Please download and load a model.")

        model_info = None
        if not self.model:
            # Look specifically for a LOADED model
            for m in models_list:
                if m.get("state") == "loaded" or m.get("loaded_instances"):
                    model_info = m
                    break
            
            if model_info is None:
                raise ValueError("No model is currently loaded in LM Studio. Please load a model or specify one in sot.toml.")
                
            self.model = model_info.get("id", model_info.get("key", ""))
        else:
            for m in models_list:
                key = m.get("key", m.get("id", ""))
                if key == self.model or self.model in key:
                    model_info = m
                    break
            if model_info is None:
                raise ValueError(f"Model '{self.model}' not found in LM Studio.")

        caps = model_info.get("capabilities", {})
        quant = model_info.get("quantization", {}) or {}
        
        allocated_context_length = None
        loaded_instances = model_info.get("loaded_instances")
        if isinstance(loaded_instances, list) and len(loaded_instances) > 0:
            config = loaded_instances[0].get("config", {})
            if isinstance(config, dict):
                allocated_context_length = config.get("context_length")

        self.capability = ProviderCapability(
            supports_tools=bool(caps.get("trained_for_tool_use", False)),
            supports_images=bool(caps.get("vision", False)),
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
            context_length=model_info.get("max_context_length"),
            allocated_context_length=allocated_context_length,
            quantization=quant.get("name", ""),
            parameter_count=model_info.get("params_string", ""),
        )

    async def _detect_ollama_capabilities(self) -> None:
        """Ollama: Use /api/ps to find the currently running model and allocated context."""
        origin = _extract_origin(self.base_url)
        allocated_context_length = None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Always check /api/ps to get the allocated context length of running models
                resp_ps = await client.get(f"{origin}/api/ps")
                if resp_ps.status_code == 200:
                    running_models = resp_ps.json().get("models",[])
                    if not self.model and running_models:
                        self.model = running_models[0].get("name", "")
                    
                    # If we have a model, see if it's currently running to get its actual allocated context
                    if self.model:
                        for rm in running_models:
                            if rm.get("name") == self.model or rm.get("model") == self.model:
                                allocated_context_length = rm.get("context_length")
                                break

                if not self.model:
                    raise ValueError("No model is currently running in Ollama. Please run a model first or specify one in sot.toml.")

                resp = await client.post(
                    f"{origin}/api/show",
                    json={"model": self.model},
                )
                if resp.status_code == 404:
                    raise ValueError(f"Model '{self.model}' not found in Ollama. Did you pull it?")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch model info from Ollama (HTTP {resp.status_code}).")
                data = resp.json()
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to Ollama at {origin}. Is the Ollama service running?") from exc

        details = data.get("details", {}) or {}
        model_info = data.get("model_info", {}) or {}
        capabilities: list[str] = data.get("capabilities") or[]

        context_length: int | None = None
        for key, val in model_info.items():
            if key.endswith(".context_length") and isinstance(val, int):
                context_length = val
                break

        quantization = str(details.get("quantization_level", "")).strip()
        parameter_count = str(details.get("parameter_size", "")).strip()

        self.capability = ProviderCapability(
            supports_tools="tools" in capabilities,
            supports_images="vision" in capabilities,
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
            context_length=context_length,
            allocated_context_length=allocated_context_length,
            quantization=quantization,
            parameter_count=parameter_count,
        )

    async def _detect_nvidia_capabilities(self) -> None:
        """NVIDIA API: Use /models endpoint to verify connectivity and list available models."""
        try:
            headers = {"Authorization": f"Bearer {self.api_key}", **self.extra_headers}
            async with httpx.AsyncClient(timeout=15.0) as client:
                # NVIDIA usa la base_url completa para el endpoint de modelos (ej. /v1/models)
                resp = await client.get(f"{self.base_url}/models", headers=headers)
                if resp.status_code == 401:
                    raise ValueError("Invalid API key for NVIDIA API.")
                if resp.status_code != 200:
                    raise RuntimeError(f"Failed to fetch models from NVIDIA API (HTTP {resp.status_code}).")
                models = resp.json().get("data", [])
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to NVIDIA API at {self.base_url}. Check your internet connection.") from exc

        if not models:
            raise ValueError("No models found in NVIDIA API. Check your API key or network.")

        # El endpoint /v1/models de NVIDIA devuelve una lista simple sin metadatos de arquitectura.
        # Asumimos capacidades estándar OpenAI-compatible para proveedores de API.
        self.capability = ProviderCapability(
            supports_tools=True,
            supports_images=False,
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
        )

    async def stream_turn(self, request: ProviderRequest):
        url = f"{self.base_url}/chat/completions"
        resolved_model = self.model or request.model
        payload = build_chat_completions_payload(request, resolved_model)
        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        _write_session_json("request", {"url": url, "payload": payload}, session_id=request.session_id)

        raw_chunks: list[dict[str, Any]] = []

        timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.is_error:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        _write_session_json("error", {"status": response.status_code, "body": body}, session_id=request.session_id)
                        raise RuntimeError(f"Provider request failed ({response.status_code}): {body}")

                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue

                        data = line[5:].strip()
                        if not data:
                            continue
                        
                        # Skip [DONE] marker, but process all chunks including usage before it
                        if data == "[DONE]":
                            continue

                        try:
                            chunk = json.loads(data)
                            raw_chunks.append(chunk)
                            for event in _events_from_chunk(chunk):
                                yield event
                        except json.JSONDecodeError:
                            # Skip malformed chunks
                            continue
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach provider '{self.name}' at {self.base_url}: {exc}") from exc

        if raw_chunks:
            _write_session_json("response-chunks", raw_chunks, session_id=request.session_id)

        yield ProviderEvent(type="done")

    async def complete_turn(self, request: ProviderRequest) -> ProviderCompletion:
        url = f"{self.base_url}/chat/completions"
        resolved_model = self.model or request.model
        payload = build_chat_completions_payload(request, resolved_model)
        payload["stream"] = False
        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        _write_session_json("request", {"url": url, "payload": payload}, session_id=request.session_id)

        timeout = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=60.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.is_error:
                    _write_session_json("error", {"status": response.status_code, "body": response.text}, session_id=request.session_id)
                    raise RuntimeError(f"Provider request failed ({response.status_code}): {response.text}")
                body = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach provider '{self.name}' at {self.base_url}: {exc}") from exc

        _write_session_json("response", body, session_id=request.session_id)

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        text = _extract_text(content)
        tool_calls = message.get("tool_calls") or []
        usage = body.get("usage") or {}
        return ProviderCompletion(
            assistant_message=message,
            text=text,
            tool_calls=tool_calls,
            usage=usage,
        )


def _is_openai_reasoning_model(model: str) -> bool:
    """Detect OpenAI reasoning-class models from their canonical name prefix.

    Heuristic — OpenAI does not expose a programmatic capability flag for this,
    and the wire-level differences are baked into model families:

    - `gpt-5*` family (gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.1, gpt-5.2,
      gpt-5.4, gpt-5.5, gpt-5-pro, gpt-5.1-codex, gpt-5.1-codex-max,
      gpt-5.1-codex-mini, plus dated variants like `gpt-5-nano-2025-08-07`).
    - O-series: `o1`, `o1-mini`, `o1-preview`, `o3`, `o3-mini`, `o3-pro`,
      `o4-mini`, dated variants like `o4-mini-2025-04-16`.

    Non-reasoning OpenAI families (`gpt-4*`, `gpt-3.5-*`, `chatgpt-4o-*`)
    return False so they keep getting `temperature` and skip `reasoning_effort`.

    Returns False for empty/None inputs and for anything that doesn't match
    the prefixes above (covers ad-hoc OpenAI-compatible deployments behind
    the same `openai` adapter — those models are user-defined and we have no
    way to know if they're reasoning-class, so we default to "treat as a
    standard chat model").
    """
    if not model:
        return False
    m = model.lower().strip()
    if m.startswith("gpt-5"):
        return True
    # o-series: name is `o<digit>` followed by either end-of-string, hyphen,
    # or dot. Avoids false positives on names like `openai-...` or `oss-...`.
    if len(m) >= 2 and m[0] == "o" and m[1].isdigit():
        return len(m) == 2 or m[2] in "-."
    return False


def build_chat_completions_payload(request: ProviderRequest, resolved_model: str) -> dict[str, Any]:
    raw_messages = request.conversation_messages or [
        {"role": "system", "content": request.system_prompt},
        {
            "role": "user",
            "content": build_user_turn_message(
                request.user_prompt,
                request.source_index,
                request.source_contents,
            ),
        },
    ]
    # Strict-API firewall + SoT-aware token-economy pruning. See
    # _sanitize_messages_for_provider's docstring for the full rationale;
    # in short: drops malformed null-content shapes that crash strict
    # validators (LM Studio HTTP 500, OpenAI HTTP 400) AND strips
    # redundant edit_files / write_file tool_call args (and their tool
    # responses) since the post-mutation file content is already in the
    # next turn's SoT block.
    messages = _sanitize_messages_for_provider(raw_messages)

    is_openai = request.provider_name == "openai"
    is_openrouter = request.provider_name == "openrouter"
    # Only flips the wire-level treatment of OpenAI Chat Completions params.
    # Not applied to openrouter even when routing an OpenAI reasoning model
    # through it, because OpenRouter normalizes/strips unsupported params on
    # its end before passing to upstream — so we must keep the universal
    # OpenAI-compatible shape for openrouter.
    openai_is_reasoning = is_openai and _is_openai_reasoning_model(resolved_model)

    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "stream": request.stream,
    }

    # Output token cap field — OpenAI deprecated `max_tokens` chat-completions-
    # wide and reasoning-class models reject it with HTTP 400
    # `unsupported_parameter`. Use `max_completion_tokens` for openai
    # unconditionally (non-reasoning openai models still accept the new name)
    # and keep `max_tokens` for everyone else, since most OpenAI-compatible
    # servers in the wild (vLLM, llama.cpp server, Ollama, LM Studio, NVIDIA
    # NIM) only know the legacy field name.
    if is_openai:
        payload["max_completion_tokens"] = request.max_output_tokens
    else:
        payload["max_tokens"] = request.max_output_tokens

    # Sampling parameters that OpenAI reasoning models reject (HTTP 400):
    # temperature, top_p, presence_penalty, frequency_penalty, logprobs,
    # top_logprobs, logit_bias. The codebase only sends `temperature` today,
    # so that's the only one we have to gate. Skipping it for OpenAI's
    # reasoning class lets the model use its baked-in default (effectively 1,
    # but it's not even an addressable knob for these models). All other
    # providers always get `temperature`.
    if not openai_is_reasoning:
        payload["temperature"] = request.temperature

    if request.stream:
        payload["stream_options"] = {"include_usage": True}

    if request.enable_tools and request.tools:
        tools = request.tools
        if is_openai:
            # OpenAI's tool-call validator rejects schemas that use
            # oneOf/anyOf/allOf/not at the TOP LEVEL of `function.parameters`
            # (HTTP 400: "schema must have type 'object' and not have ...").
            # Other providers in this codebase (openrouter, lmstudio, ollama,
            # nvidia) accept the same constructs without complaint, so we
            # only sanitize for openai. The runtime tool handlers already
            # enforce equivalent constraints in Python (e.g. `attach/detach
            # path` raises ValueError when both `path` and `paths` are absent),
            # so dropping these keys does not weaken correctness — it only
            # removes a schema-level hint for the model, which is already
            # described in the tool's natural-language description.
            tools = [_sanitize_tool_schema_for_openai(t) for t in tools]
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # Reasoning effort wire format diverges per provider — and OpenAI in
    # particular rejects unknown top-level keys with HTTP 400, so we MUST NOT
    # forward this field to anyone but the two providers that document it,
    # AND only on models that actually accept it.
    #
    # - openai      → flat top-level field:  "reasoning_effort": "<level>"
    #                 only valid on reasoning-class models (gpt-5/o-series).
    #                 If the user left `reasoning_effort = "..."` in the toml
    #                 but switched the model to a non-reasoning one, we
    #                 silently strip the param so the call doesn't 400 —
    #                 the user can re-enable by reverting the model. The
    #                 codebase intentionally does not use the Responses API
    #                 to keep SoT semantics intact, so the flat field is the
    #                 only correct shape for this adapter.
    # - openrouter  → nested object:         "reasoning": {"effort": "<level>"}
    #                 OpenRouter's unified parameter; silently ignored on
    #                 non-reasoning upstreams, so it's safe to always send
    #                 when the user sets it.
    #
    # Any other provider (lmstudio, ollama, nvidia, xai) gets nothing — those
    # never advertised reasoning_effort and would either ignore or 400.
    if request.reasoning_effort:
        if openai_is_reasoning:
            payload["reasoning_effort"] = request.reasoning_effort
        elif is_openrouter:
            payload["reasoning"] = {"effort": request.reasoning_effort}

    return payload


# Top-level keys forbidden by OpenAI inside `function.parameters`. The error
# message from the API enumerates exactly these: "schema must have type
# 'object' and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level".
# `enum` is included for completeness even though it's vanishingly rare on a
# top-level parameters object (whose `type` is normally "object").
_OPENAI_FORBIDDEN_TOP_LEVEL_SCHEMA_KEYS: frozenset[str] = frozenset(
    {"oneOf", "anyOf", "allOf", "not", "enum"}
)


def _sanitize_tool_schema_for_openai(tool: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-cloned copy of `tool` with the forbidden top-level
    schema keys stripped from `function.parameters`.

    Only the top level of `parameters` is touched. Constructs nested deeper
    inside individual property schemas (e.g. a property whose schema uses
    `enum`) are left alone — OpenAI accepts those. The original `tool` dict
    is not mutated; sibling keys (`type`, `properties`, `required`,
    `additionalProperties`, `description`, …) survive untouched.
    """
    sanitized = dict(tool)
    func = sanitized.get("function")
    if not isinstance(func, dict):
        return sanitized
    func = dict(func)
    sanitized["function"] = func
    params = func.get("parameters")
    if not isinstance(params, dict):
        return sanitized
    params = dict(params)
    func["parameters"] = params
    for forbidden in _OPENAI_FORBIDDEN_TOP_LEVEL_SCHEMA_KEYS:
        params.pop(forbidden, None)
    return sanitized


def _events_from_chunk(chunk: dict[str, Any]) -> list[ProviderEvent]:
    events: list[ProviderEvent] = []

    usage = chunk.get("usage")
    if usage:
        events.append(ProviderEvent(type="usage", payload={"usage": usage}))

    for choice in chunk.get("choices", []):
        delta = choice.get("delta") or choice.get("message") or {}

        reasoning_text, reasoning_details = _extract_reasoning_payload(delta)
        if reasoning_text or reasoning_details:
            events.append(
                ProviderEvent(
                    type="reasoning_delta",
                    payload={"text": reasoning_text, "details": reasoning_details},
                )
            )

        content = delta.get("content")
        text = _extract_text(content)
        if text:
            events.append(ProviderEvent(type="text_delta", payload={"text": text}))

        tool_calls = delta.get("tool_calls") or []
        if tool_calls:
            events.append(ProviderEvent(type="tool_call", payload={"tool_calls": tool_calls}))

    return events


def _extract_origin(url: str) -> str:
    """Extract scheme + host + port from a URL, stripping any path.
    'http://192.168.1.169:1234/v1' -> 'http://192.168.1.169:1234'
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return url.rstrip("/")
    origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        origin += f":{parsed.port}"
    return origin


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "output_text"} and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        return "".join(text_parts)

    return ""


def _extract_reasoning_payload(delta: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(delta, dict):
        return "", []

    reasoning_parts: list[str] = []
    reasoning_details: list[dict[str, Any]] = []

    details = delta.get("reasoning_details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            reasoning_details.append(item)
            detail_text = _extract_reasoning_detail_text(item)
            if detail_text:
                reasoning_parts.append(detail_text)

    # Some providers emit equivalent reasoning in both `reasoning_details`
    # and legacy string fields. Prefer details when present to avoid
    # duplicate visible thinking output.
    if not reasoning_parts:
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                reasoning_parts.append(value)

    return "".join(reasoning_parts), reasoning_details


def _extract_reasoning_detail_text(detail: dict[str, Any]) -> str:
    detail_type = str(detail.get("type", "")).strip()
    if detail_type == "reasoning.text":
        text = detail.get("text")
        if isinstance(text, str):
            return text
    if detail_type == "reasoning.summary":
        summary = detail.get("summary")
        if isinstance(summary, str):
            return summary
    if detail_type == "reasoning.encrypted":
        return ""

    for key in ("text", "summary", "content"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
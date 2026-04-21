from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from omni_cli.message_builder import build_user_turn_message
from omni_cli.providers.base import ProviderCapability, ProviderCompletion, ProviderEvent, ProviderRequest


def _write_session_json(label: str, data: Any, session_id: str = "") -> Path:
    """Write a JSON blob to the session directory."""
    import os
    from pathlib import Path as _Path

    sessions_env = os.environ.get("OMNI_SESSIONS_DIR")
    if sessions_env:
        sessions_base = _Path(sessions_env).resolve()
    else:
        sessions_base = _Path(".omni-cli/sessions").resolve()

    if session_id:
        base = sessions_base / session_id
    else:
        base = _Path(".omni-cli/session-json").resolve()

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
        else:
            # openai, xai — no public model-info endpoint; leave defaults (tools on by default for cloud)
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
                raise ValueError("No model is currently loaded in LM Studio. Please load a model or specify one in omni.toml.")
                
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
                    raise ValueError("No model is currently running in Ollama. Please run a model first or specify one in omni.toml.")

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


def build_chat_completions_payload(request: ProviderRequest, resolved_model: str) -> dict[str, Any]:
    messages = request.conversation_messages or [
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

    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "temperature": request.temperature,
        "stream": request.stream,
        "max_tokens": request.max_output_tokens,
    }

    if request.stream:
        payload["stream_options"] = {"include_usage": True}

    if request.enable_tools and request.tools:
        payload["tools"] = request.tools
        payload["tool_choice"] = "auto"

    return payload


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
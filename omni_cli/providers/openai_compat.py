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
                if resp.status_code != 200:
                    return
                models = resp.json().get("data", [])
        except Exception:
            return

        model_info = None
        for m in models:
            if m.get("id") == self.model or m.get("id", "").startswith(self.model):
                model_info = m
                break

        if model_info is None:
            return

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
        """LM Studio: GET /api/v1/models returns capabilities.vision and capabilities.trained_for_tool_use."""
        # base_url may be http://host:1234/v1 — we need the origin (http://host:1234)
        origin = _extract_origin(self.base_url)
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try native API first
                resp = await client.get(f"{origin}/api/v1/models", headers=headers)
                if resp.status_code != 200:
                    # Fall back to OpenAI-compat endpoint
                    resp = await client.get(f"{origin}/v1/models", headers=headers)
                if resp.status_code != 200:
                    return
                data = resp.json()
        except Exception:
            return

        models_list = data.get("models", data.get("data", []))
        model_info = None
        for m in models_list:
            key = m.get("key", m.get("id", ""))
            if key == self.model or self.model in key:
                model_info = m
                break

        if model_info is None:
            return

        caps = model_info.get("capabilities", {})
        quant = model_info.get("quantization", {}) or {}
        self.capability = ProviderCapability(
            supports_tools=bool(caps.get("trained_for_tool_use", False)),
            supports_images=bool(caps.get("vision", False)),
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
            context_length=model_info.get("max_context_length"),
            quantization=quant.get("name", ""),
            parameter_count=model_info.get("params_string", ""),
        )

    async def _detect_ollama_capabilities(self) -> None:
        """Ollama: POST /api/show — purely structural detection, no model name matching.

        Vision:  'clip' in details.families (classic multimodal, e.g. llava)
                 OR any key with '.vision.' in model_info (newer built-in vision, e.g. qwen3.5)
        Tools:   template contains '{{ if .Tools }}' — Ollama sets this for tool-capable models.
        Context: any model_info key ending in '.context_length'.
        """
        origin = _extract_origin(self.base_url)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{origin}/api/show",
                    json={"model": self.model},
                )
                if resp.status_code != 200:
                    return
                data = resp.json()
        except Exception:
            return

        details = data.get("details", {}) or {}
        model_info = data.get("model_info", {}) or {}
        # capabilities is a list of strings e.g. ["completion", "vision", "tools", "thinking"]
        capabilities: list[str] = data.get("capabilities") or []

        # Context length: any architecture key ending in .context_length
        context_length: int | None = None
        for key, val in model_info.items():
            if key.endswith(".context_length") and isinstance(val, int):
                context_length = val
                break

        # Quantization and parameter size
        quantization = str(details.get("quantization_level", "")).strip()
        parameter_count = str(details.get("parameter_size", "")).strip()

        self.capability = ProviderCapability(
            supports_tools="tools" in capabilities,
            supports_images="vision" in capabilities,
            supports_pdfs=False,
            supports_audio=False,
            supports_video=False,
            context_length=context_length,
            quantization=quantization,
            parameter_count=parameter_count,
        )

    async def stream_turn(self, request: ProviderRequest):
        url = f"{self.base_url}/chat/completions"
        payload = build_chat_completions_payload(request)
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
        payload = build_chat_completions_payload(request)
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


def build_chat_completions_payload(request: ProviderRequest) -> dict[str, Any]:
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
        "model": request.model,
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
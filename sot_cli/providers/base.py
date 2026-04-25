from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol


ProviderEventType = Literal["text_delta", "reasoning_delta", "tool_call", "usage", "done", "error"]


@dataclass
class ProviderCapability:
    supports_tools: bool = False
    supports_images: bool = False
    supports_pdfs: bool = False
    supports_audio: bool = False
    supports_video: bool = False
    # Model metadata populated by provider API detection
    context_length: int | None = None
    allocated_context_length: int | None = None  # <--- AÑADIR ESTA LÍNEA
    max_completion_tokens: int | None = None
    modality: str = ""  # e.g. "text+image->text"
    quantization: str = ""  # e.g. "Q8_0" (lmstudio)
    parameter_count: str = ""  # e.g. "27B" (lmstudio)


@dataclass
class ProviderRequest:
    provider_name: str
    model: str
    session_id: str
    system_prompt: str
    orchestration_rules: str
    user_prompt: str
    source_index: str
    source_contents: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 4096
    stream: bool = True
    enable_tools: bool = True
    disable_delegation: bool = False
    tools: list[dict[str, Any]] = field(default_factory=list)
    conversation_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ProviderEvent:
    type: ProviderEventType
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderCompletion:
    assistant_message: dict[str, Any]
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    name: str
    capability: ProviderCapability

    async def stream_turn(self, request: ProviderRequest) -> AsyncIterator[ProviderEvent]:
        ...

    async def complete_turn(self, request: ProviderRequest) -> ProviderCompletion:
        ...

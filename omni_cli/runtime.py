from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from omni_cli.config import AppConfig, load_config
from omni_cli.paths import AppPaths, build_paths, ensure_runtime_directories
from omni_cli.providers.factory import create_provider_adapter
from omni_cli.providers.base import ProviderAdapter
from omni_cli.providers.openai_compat import OpenAICompatibleAdapter
from omni_cli.session_store import SessionStore
from omni_cli.mcp_client import MCPManager


@dataclass
class AppRuntime:
    config: AppConfig
    paths: AppPaths
    sessions: SessionStore
    _adapter_cache: dict[tuple[str, str], ProviderAdapter] = field(default_factory=dict, repr=False)
    mcp: MCPManager = field(init=False, repr=False)

    def provider_adapter(self, provider_name: str, model: str | None = None) -> ProviderAdapter:
        """Get adapter (sync) for a specific provider/model pair."""
        effective_model = model.strip() if isinstance(model, str) and model.strip() else self.config.provider(provider_name).model
        cache_key = (provider_name, effective_model)
        if cache_key not in self._adapter_cache:
            self._adapter_cache[cache_key] = create_provider_adapter(self.config, provider_name, model=effective_model)
        return self._adapter_cache[cache_key]

    async def provider_adapter_async(self, provider_name: str, model: str | None = None) -> ProviderAdapter:
        """Get adapter with capabilities detected from the provider API."""
        adapter = self.provider_adapter(provider_name, model=model)
        if isinstance(adapter, OpenAICompatibleAdapter) and not adapter._capabilities_detected:
            await adapter.detect_capabilities()
        return adapter

    def __post_init__(self):
        self.mcp = MCPManager(self.config.mcp_servers)


def bootstrap_runtime(config_path: str | None = None) -> AppRuntime:
    config = load_config(config_path)
    paths = build_paths(config, config_path)
    ensure_runtime_directories(paths)

    # Allow overriding the sessions directory via environment variable.
    # This is important for delegated sub-agents that run inside a parent's
    # agents/ folder — the parent will set OMNI_SESSIONS_DIR so the child
    # runtime points its SessionStore at that directory.
    sessions_env = os.environ.get("OMNI_SESSIONS_DIR")
    sessions_dir = Path(sessions_env) if sessions_env else paths.sessions_dir

    return AppRuntime(
        config=config,
        paths=paths,
        sessions=SessionStore(sessions_dir),
    )

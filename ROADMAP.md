# Roadmap

## Phase 0 - 4

_Completed._ Local persistence, ephemeral Source of Truth (SoT), OpenAI-compatible adapters, text streaming, basic tool loop, standalone terminal-first assistant, and asynchronous Multi-Agent orchestration (JIT sub-agents).

## Phase 5: Provider Expansion (OpenAI + Anthropic)

Objective: Add first-class support for `openai` and `anthropic` providers before moving into advanced browser automation capabilities.

### Deliverables

- Add and validate `openai` provider configuration and runtime adapter support.
- Add and validate `anthropic` provider configuration and runtime adapter support.
- Ensure provider capability detection works consistently for both new providers.
- Verify feature parity for streaming, tools, and SoT injection behavior.
- Document provider setup examples in `omni.toml` and `omni.keys.toml`.

## Phase 6: The Human-like Browser

Objective: Give the agent the ability to browse the web exactly like a human would, to find documentation, read issues, or interact with web apps.

### Deliverables

- Integration with a headless browser accessible via tools.
- Specialized tools for the agent to navigate, click, type, and read the DOM.
- Visual or DOM-to-text translation so the agent understands the page layout without blowing up the context window.
- Ability to delegate heavy web-research tasks to a sub-agent that surfs the web and returns a clean markdown report to the main SoT.

## Phase 7: User-Friendly Web Interface

Objective: Make `omni-cli` accessible to less experienced users through an intuitive, visual web-based UI that hides the terminal complexity while retaining full power-user access.

### Deliverables

- Lightweight web interface that runs locally alongside the CLI.
- Session browser and manager: visualize active sessions, chat history, and Source of Truth contents.
- Simple form-based configuration for providers and API keys (no direct TOML editing required).
- Visual file browser and editor for Source of Truth management.
- Real-time streaming output display with tool call visualization.
- Support for both beginners (guided workflows) and power users (direct terminal access).
- Zero backend changes: the web UI calls the same runtime APIs as the CLI, ensuring feature parity.

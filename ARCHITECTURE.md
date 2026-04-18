# Current architecture of omni-cli

## Runtime objective

`omni-cli` is a local terminal assistant that delegates generation to the provider, but maintains control of the operational state on the runtime side:

- local sessions and transcripts on disk;
- local tools for files, shell and session control;
- Source of Truth (SoT) rebuilt by the runtime;
- permanent conversation history (`chat_history`) separated from ephemeral SoT;
- conversation state always controlled locally by the runtime; provider-side sessions are not used;
- independence from the wire format of the provider behind adapters.

The central rule is this:

> The provider is not the source of truth of the project; the source of truth is the local state that the runtime reinjects into the model when appropriate.

## Startup modes

### `prompt` (main mode)

Creates or resumes an interactive session in terminal. Detects model capabilities from the provider API and displays them in the startup banner. Creates a `SoTState` that lives in memory during the entire process, accumulating `chat_history` between turns.

### `command` (one-shot mode for multi-agent)

Executes a single turn against an existing session. Creates a fresh `SoTState` per invocation. Designed as the primitive for multi-agent orchestration.

### `run_task` (headless agent execution)

Executes a headless agent session. Used internally by the `delegate_task` tool to spawn sub-agents in isolated environments.

## Agent and Sub-agent Commands & Parameters

This section centralizes the parameters that are commonly omitted in high-level docs.

### CLI commands used to run agents

#### `omni-cli prompt [session_id]`

Interactive main loop (Boss agent context).

Supported flags:

- `--title <text>`: set title for a new session.
- `--provider <lmstudio|openrouter|openai|xai|ollama>`: provider override.
- `--model <name>`: model override.
- `--no-tools`: disables tool loop (plain chat behavior).

Examples:

- `omni-cli prompt`
- `omni-cli prompt <session_id> --provider openrouter --model x-ai/grok-4.1-fast`

#### `omni-cli chat [session_id]`

Alias of `prompt`, same parameters and behavior.

#### `omni-cli command <session_id> <prompt>`

One-shot turn runner (automation/multi-agent primitive).

Supported flags:

- `--provider <lmstudio|openrouter|openai|xai|ollama>`
- `--model <name>`
- `--no-tools`
- `--disable-delegation`: removes `delegate_task` from available tools to avoid recursion loops.

Examples:

- `omni-cli command <session_id> "Analyze this repo and propose fixes"`
- `omni-cli command <session_id> "Run tests" --provider openrouter --disable-delegation`

#### `omni-cli run_task <agent_id> <prompt>`

Executes a headless sub-agent (Worker), typically created as `agent_N`.

Examples:

- `omni-cli run_task agent_1 "Search all TODOs and return a summary"`

### Tool commands used for sub-agent orchestration

These are runtime tools the Boss model uses during a turn.

#### `delegate_task`

Parameters:

- `task_prompt` (required): detailed instructions for the Worker.
- `provider` (optional): provider override for the Worker.
- `attempts` (optional, integer, min 1): max repeated failed attempts before abort. Default `2`.
- `background` (optional, boolean): async launch if `true`; blocks for report if `false`. Default `false`.

#### `wait_task`

Parameters:

- `agent_id` (required): target sub-agent id (e.g. `agent_1`).
- `timeout_seconds` (optional, integer, min 1): max wait time; omitted means wait indefinitely.

#### `list_tasks`

No parameters. Returns delegated tasks and status (RUNNING/COMPLETED). Prefer `wait_task` instead of polling loops.

### Session and config notes for orchestration

- Global CLI flag `--config <path>` can be passed before commands to use a specific TOML config.
- Delegated agents inherit runtime context and session storage rules (including `OMNI_SESSIONS_DIR` when set).
- Boss/Worker prompt separation is enforced by the runtime (`AGENT_SYSTEM_PROMPT` vs `SUB_AGENT_SYSTEM_PROMPT`).

## File Discovery Tool (`list_dir`)

`list_dir` is a dual-purpose tool: it can return broad recursive listings and it can act as a filtered search tool.

Current behavior:

- Always recursive.
- Always includes hidden files.
- No built-in result limit by default.
- Returns rich metadata per entry (name, absolute/relative path, type, depth, extension, size, timestamps, hidden/symlink flags, symlink target when available).

Supported filters:

- `kind`: `file`, `directory`, `symlink`, `symlink_file`, `symlink_directory`.
- `extensions`: extension filter list (accepts `.py` and `py` formats).
- `name_contains`: case-insensitive basename substring, supports comma-separated OR keywords.
- `path_contains`: case-insensitive relative/absolute path substring.
- `name_pattern`: glob pattern on basename (`*`, `?`, `[]`).
- `path_pattern`: glob pattern on path (`*`, `?`, `[]`).
- `min_size_bytes` / `max_size_bytes`: numeric size filters.
- `follow_symlinks`: recurse through symlink directories when `true`.

Content search mode:

- `content_contains`: search text inside UTF-8 text files (for example `txt`, `json`, `xml`, `md`, `sql`, `py`, etc.).
- `content_case_sensitive`: make `content_contains` matching case-sensitive.
- `content_max_bytes`: optional max file size for content scanning; larger files are skipped.

When `content_contains` is used, matching entries include content match metadata and the tool returns aggregate content scan statistics (`searched_files`, `matched_files`, and skipped counters).

## Main components

### `omni_cli/query.py`

The core of the orchestrator. Runs the loop against the provider, executes tool calls, maintains the `SoTState`, and rebuilds the SoT after each tool call.
**Payload assembly:** To combat "Lost in the Middle" and recency bias, the payload is assembled as:
`[System Prompt] + [Past Chat History] + [Ephemeral SoT Block] + [Latest User Prompt & Tool Calls]`

### `omni_cli/source_of_truth.py`

Materializes the persisted session state as `SourceBundle`. It no longer applies configurable caps like file count or size. If a path is attached to the session, the runtime attempts to materialize it.

### `omni_cli/tools/session/delegate.py` (Multi-Agent Orchestration)

Implements the JIT (Just-In-Time) agent pattern.

**The Boss-Worker Dynamic:**

1. **Delegation:** The main agent (Boss) uses `delegate_task` to spawn a sub-agent (Worker) in the background. The sub-agent gets a clean, isolated session (`agent_N`).
2. **Role Separation:** The Boss gets the `SYSTEM_PROMPT` (orchestration, strategy). The Worker gets the `SUB_AGENT_SYSTEM_PROMPT` (strict execution, no delegation allowed).
3. **Invisible IPC (Inter-Process Communication):** The Worker executes tools and outputs its final findings as plain text. The system intercepts this text and writes it to an internal `response.md` file. _The AI models do not know this file exists._
4. **Synchronization:** The Boss uses `wait_task("agent_N")`. The system blocks until the Worker finishes, reads the internal `response.md`, and returns the text directly to the Boss as a tool result.
5. **Terminal Silence:** Sub-agents run completely headless. Their stdout/stderr is redirected to `agent.log` inside their session folder, keeping the main user terminal perfectly clean.

### MCP servers & runtime details

- The runtime can optionally start and manage external MCP servers configured under `mcp.servers` in `omni.toml`. These servers are discovered at runtime and the tools they expose are added to the provider-visible function/schema list.
- MCP-provided tools are namespaced by server (e.g. `myserver__toolname`) so they do not clash with local tool names. MCP tool calls are proxied through the runtime's `MCPManager` and returned as regular tool results.
- Portable local MCP servers can live under the repository `mcps/` folder and be referenced with relative paths from `omni.toml` so the same config works across machines.
- To support delegated child agents, the runtime honors the `OMNI_SESSIONS_DIR` environment variable: when set, the runtime points its `SessionStore` to that directory so sub-agents can run inside a parent's `sessions/` folder.
- The runtime caches provider adapters and performs capability detection when needed; this drives tool availability and SoT composition per-provider.
- The streaming provider adapter emits incremental text, tool-call, reasoning/thinking, and usage events when the provider returns them, and the CLI renders them live instead of buffering the whole response first.

Example `omni.toml` MCP entry:

## Current SoT model

### Separation permanent history vs ephemeral SoT

**`chat_history` (permanent):** List of messages that grows between turns. Contains user prompts, assistant responses, and lightweight tool metadata. Never contains file content snapshots.
**SoT block (ephemeral):** Built from disk in each round. Contains the current content of tracked files and media. Injected just before the latest user prompt, and discarded immediately after.

### SoT provenance

- `session-backed`: Comes from the persisted source of the session. Refreshed at the start of each turn.
- `tool-backed`: Appeared by a reading or mutation made during the interactive session. Persists between interactive turns until explicitly deleted.

### SoT management tools

The model can modify the authoritative working set of the session at any time using these tools:

#### `attach_path_to_source`

Add a file or directory to the session source of truth. Accepts a single path (`path`) or multiple paths in one call (`paths`). Prefer `paths` for batch attach.

Parameters:

- `path` (string): absolute or project-relative path to attach.
- `paths` (array of strings): batch variant — attach several paths in one call. Use this instead of multiple `attach_path_to_source` calls.
- `recursive` (boolean, default `true`): whether to recurse into directories. Applies to every path in the batch.
- `label` (string, optional): human label; only supported with a single path.

Either `path` or `paths` is required.

#### `detach_path_from_source`

Remove a file or directory from the session source of truth. Accepts a single path (`path`) or multiple paths in one call (`paths`). Prefer `paths` for batch removal.

Parameters:

- `path` (string): absolute path or project-relative path already attached to the session.
- `paths` (array of strings): batch variant — remove several paths in one call. Use this instead of multiple `detach_path_from_source` calls.

Either `path` or `paths` is required.

#### `get_session_state`

Inspect the current session: provider, model, temperature, max output tokens, and the full list of session-backed source entries with their IDs and metadata. Use this before attaching or detaching to confirm the current state. No parameters.

#### `update_session`

Change runtime parameters for future turns in the current session: `title`, `provider`, `model`, `temperature`, `max_output_tokens`. At least one field required.

## Known current limitations

1. Resume/recovery currently depends on reconstructing `chat_history` and SoT state from persisted request/response artifacts.
2. `edit_file` works by exact match. `apply_text_edits` adds line ranges, but neither is a regex engine nor an AST editor.
3. Archive files (`zip`, `tar`, `gz`, etc.) cannot be read directly. The model receives a format-specific error with the correct `run_command` invocation to list or extract contents.

# Current architecture of sot-cli

## Runtime objective

`sot-cli` is a local terminal assistant that delegates generation to the provider, but maintains control of the operational state on the runtime side:

- local sessions and transcripts on disk;
- local tools for files, shell and session control;
- Source of Truth (SoT) rebuilt by the runtime;
- permanent conversation history (`chat_history`) separated from ephemeral SoT;
- conversation state always controlled locally by the runtime; provider-side sessions are not used;
- independence from the wire format of the provider behind adapters.

The central rule is this:

> The provider is not the source of truth of the project; the source of truth is the local state that the runtime reinjects into the model when appropriate.

The CLI is named after this exact rule: **SoT** (Source of Truth) is the architectural keystone, and `sot-cli` is the tool that implements it end-to-end.

## Startup modes

### `prompt` (main mode)

Creates or resumes an interactive session in terminal. Detects model capabilities from the provider API (or applies optimistic assumed defaults for `openai`/`xai`, see "Provider configuration" below) and displays them in the startup banner. Creates a `SoTState` that lives in memory during the entire process, accumulating `chat_history` between turns.

The startup banner shows: provider, session id, route (base URL), model, start timestamp, capability flags, host environment (OS/arch/hostname/user/shell/cwd), and a magenta `tip:` line reminding the user that `sot.toml` and `sot.keys.toml` are the source of truth for provider settings (no need to re-run the wizard for tweaks).

### `command` (one-shot mode for multi-agent)

Executes a single turn against an existing session. Creates a fresh `SoTState` per invocation. Designed as the primitive for multi-agent orchestration.

### `run_task` (headless agent execution)

Executes a headless agent session. Used internally by the `delegate_task` tool to spawn sub-agents in isolated environments.

## Agent and Sub-agent Commands & Parameters

This section centralizes the parameters that are commonly omitted in high-level docs.

### CLI commands used to run agents

#### `sot-cli prompt [session_id]`

Interactive main loop (Boss agent context).

Supported flags:

- `--title <text>`: set title for a new session.
- `--provider <lmstudio|openrouter|openai|xai|ollama|nvidia>`: provider override.
- `--model <name>`: model override.
- `--no-tools`: disables tool loop (plain chat behavior).

Examples:

- `sot-cli prompt`
- `sot-cli prompt <session_id> --provider openrouter --model x-ai/grok-4.1-fast`

#### `sot-cli chat [session_id]`

Alias of `prompt`, same parameters and behavior.

#### `sot-cli command <session_id> <prompt>`

One-shot turn runner (automation/multi-agent primitive).

Supported flags:

- `--provider <lmstudio|openrouter|openai|xai|ollama|nvidia>`
- `--model <name>`
- `--no-tools`
- `--disable-delegation`: removes `delegate_task` from available tools to avoid recursion loops.

Examples:

- `sot-cli command <session_id> "Analyze this repo and propose fixes"`
- `sot-cli command <session_id> "Run tests" --provider openrouter --disable-delegation`

#### `sot-cli run_task <agent_id> <prompt>`

Executes a headless sub-agent (Worker), typically created as `agent_N`.

Examples:

- `sot-cli run_task agent_1 "Search all TODOs and return a summary"`

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
- Delegated agents inherit runtime context and session storage rules (including `SOT_SESSIONS_DIR` when set).
- Boss/Worker prompt separation is enforced by the runtime (`AGENT_SYSTEM_PROMPT` vs `SUB_AGENT_SYSTEM_PROMPT`).
  All runtime tool settings live in `[tools]` inside `sot.toml`. The values listed below are the **code defaults** applied when the field is absent; the bundled `sot.example.toml` ships with looser values intended as a comfortable starting point for real workloads.

| Setting                           | Default | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `output_limit`                    | `12000` | Max characters of tool output before truncation. Acts as a per-tool firewall against a single noisy command (long log dumps, deep recursive listings, base64-encoded binaries) blowing up the context window. The model still sees a `[truncated]` marker, so it knows when to switch to file-based offloading (`run_command > tmpfile.txt; read_files tmpfile.txt`).                                                                                                                                            |
| `default_command_timeout_seconds` | `180`   | Default wall-clock timeout (seconds) applied to `run_command` foreground execution when the model does not pass an explicit `timeout_seconds`. The model is always free to override per call. Increase only if your typical commands legitimately run longer than 3 minutes.                                                                                                                                                                                                                                     |
| `binary_check_size`               | `8192`  | Byte threshold used by `read_files` to detect binary content. The reader inspects this many bytes at the start of the file and rejects non-UTF8 input above the threshold, preventing the SoT from being polluted with garbage bytes that would also balloon the context.                                                                                                                                                                                                                                        |
| `show_thinking`                   | `true`  | Stream the model's reasoning/thinking tokens to the terminal as they arrive. Independent of `show_full`; gates **reasoning** output only. Set to `false` if the live reasoning trace is noisy — the model still reasons internally, you just don't see it scroll.                                                                                                                                                                                                                                                |
| `show_full`                       | `true`  | Stream tool-call argument chunks (and any other non-reasoning, non-text chunk the provider emits) in real time as the model generates them, verbatim. When disabled, tool calls are only shown as a single assembled line after streaming completes. Provider chunks are never mutated by the runtime; `show_full` only toggles whether they are rendered live.                                                                                                                                                  |
| `max_rounds`                      | `25`    | Max tool-call rounds the boss agent can execute per user prompt before the runtime stops it. The bundled example raises this to `250` for power users; raise/lower depending on how much trial-and-error you want to allow per turn.                                                                                                                                                                                                                                                                             |
| `delegated_max_rounds`            | `8`     | Max tool-call rounds a sub-agent can execute before being stopped. The bundled example raises this to `80`.                                                                                                                                                                                                                                                                                                                                                                                                      |
| `repeat_limit`                    | `3`     | Max consecutive identical rounds (same tools, same arguments) the boss can repeat before the runtime aborts with a loop warning. Catches "stuck-in-a-loop" failure modes early.                                                                                                                                                                                                                                                                                                                                  |
| `delegated_repeat_limit`          | `2`     | Same as above but for sub-agents. Tighter on purpose because workers are expected to converge faster on a narrower task.                                                                                                                                                                                                                                                                                                                                                                                         |
| `search_default_head_limit`       | `200`   | Default max number of result entries returned by a single `search_code` call when the model does not pass an explicit `head_limit`. Pass `0` from the tool call to disable.                                                                                                                                                                                                                                                                                                                                      |
| `search_max_line_length`          | `500`   | Per-line truncation length (characters) applied to `search_code` output. Controls both the ripgrep `--max-columns` flag and the Python fallback slicing. The bundled example sets `1000` so minified one-liners and long log lines aren't mangled.                                                                                                                                                                                                                                                               |
| `search_timeout_seconds`          | `30`    | Hard timeout (seconds) for a single `search_code` invocation (ripgrep subprocess or Python fallback). Keeps pathological patterns from hanging a turn.                                                                                                                                                                                                                                                                                                                                                           |
| `reasoning_char_budget`           | `8000`  | Hard cap on streamed reasoning/thinking characters per turn for the boss agent. When the cumulative reasoning channel exceeds this budget during a single stream, the runtime cuts the provider connection, prints a yellow `⚠  reasoning budget exceeded` warning, and lets the tool loop advance. Protects against models that get stuck in eternal "let me reconsider…" loops inside a single response. Set to `0` to disable the cap. The bundled example raises this to `10000` for reasoning-heavy models. |
| `delegated_reasoning_char_budget` | `4000`  | Same cap applied to sub-agent turns. Typically smaller than the boss budget because delegated workers are expected to execute narrow tasks and should not spend long reasoning windows before acting. Set to `0` to disable. The bundled example raises this to `8000`.                                                                                                                                                                                                                                          |

## Provider configuration (`[providers.X]`)

`sot-cli` ships with a single OpenAI-compatible HTTP adapter (`sot_cli/providers/openai_compat.py`) that talks to every supported backend. Per-provider tweaks live under `[providers.<name>]` in `sot.toml`; per-provider API keys live under `[providers.<name>] api_key = "..."` in `sot.keys.toml` (separate file so `.gitignore` can keep secrets out of commits).

Every provider section accepts the same five base fields:

| Field               | Type            | Description                                                                                                                                                                                                                                              |
| ------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `base_url`          | string          | Full URL of the OpenAI-compatible endpoint, including the `/v1` suffix.                                                                                                                                                                                  |
| `model`             | string          | Model name. Required for cloud providers. For `lmstudio` and `ollama` it can be left empty (`""`) so the adapter auto-resolves the currently loaded model.                                                                                               |
| `temperature`       | float           | Sent on every request (subject to per-provider quirks — see below).                                                                                                                                                                                      |
| `max_output_tokens` | int             | Token cap on the completion. Wire-level field name diverges per provider; the adapter handles the rename.                                                                                                                                                |
| `configured`        | bool (optional) | Marker written by the wizard. When `true`, the selector skips the per-provider mini-wizard and enters the session directly. Manual edits to provider settings do **not** require flipping this back to `false` — the runtime trusts whatever is on disk. |

### Optional fields per provider

| Field                       | Where it applies       | Effect                                                                                                                                                                                                                                                                             |
| --------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `http_referer`, `app_title` | `openrouter`           | Forwarded as `HTTP-Referer` and `X-OpenRouter-Title` headers (used by OpenRouter for app attribution and rankings).                                                                                                                                                                |
| `reasoning_effort`          | `openai`, `openrouter` | Hint for reasoning-class models. Accepted values: `"none"`, `"minimal"`, `"low"`, `"medium"`, `"high"`, `"xhigh"`. The adapter relays it in the right wire format per provider — see "OpenAI-specific wire-level adaptations" below. Silently ignored for any other provider name. |

### Supported provider names

`lmstudio`, `openrouter`, `openai`, `xai`, `ollama`, `nvidia`. New names can be added to `KNOWN_PROVIDERS` in `sot_cli/config/app.py`; the same OpenAI-compatible adapter handles them all.

### OpenAI-specific wire-level adaptations

OpenAI is the strictest of the supported APIs and rejects unknown or unsupported fields with `HTTP 400`. The adapter applies several **OpenAI-only** transformations to keep requests valid without disturbing other providers:

1. **Reasoning model auto-detection.** Models whose name starts with `gpt-5*` or matches `o<digit>` (e.g. `o1`, `o3-mini`, `o4-mini-2025-04-16`) are detected as reasoning-class. The detector is a pure string match in `_is_openai_reasoning_model` — no network call, no extra config field.
2. **`max_tokens` → `max_completion_tokens`.** OpenAI deprecated `max_tokens` chat-completions-wide, and reasoning models reject it outright (`HTTP 400 unsupported_parameter`). The adapter always uses `max_completion_tokens` for `provider_name="openai"`. Other providers keep the legacy `max_tokens` field, which is what most OpenAI-compatible servers (vLLM, llama.cpp, Ollama, LM Studio, NVIDIA NIM) actually understand.
3. **`temperature` stripped for reasoning models.** Reasoning models reject any `temperature` value other than the baked-in default (`HTTP 400`). The adapter omits the field when the model is reasoning-class. Non-reasoning OpenAI models still get the `temperature` from the toml.
4. **`reasoning_effort` per-model gate.** `reasoning_effort` is only sent for reasoning-class OpenAI models. If the user leaves the field set in `sot.toml` but switches to a non-reasoning model (e.g. `gpt-4o-mini`), the adapter silently drops it instead of letting the call 400. Wire format: flat top-level `"reasoning_effort": "<level>"` (Chat Completions style — the codebase intentionally does **not** use the Responses API, since Responses' server-side state would conflict with the locally-controlled SoT model).
5. **Tool schema sanitization.** OpenAI's tool-call validator rejects schemas that use `oneOf`, `anyOf`, `allOf`, `not`, or `enum` at the **top level** of `function.parameters`. The adapter strips those keys from each tool schema before sending. Other providers keep the original schema. Constructs nested deeper inside individual property schemas are left alone.
6. **Assumed capabilities.** OpenAI's `/v1/models` endpoint does not expose tools/modality flags or context windows in a useful shape, so the adapter assumes optimistic frontier defaults: `supports_tools=true`, `supports_images=true`, `supports_pdfs=true`, `context_length=400_000`. If the chosen model is more limited, the API surfaces that at request time. `xai` and other unknown OpenAI-compatible names get a minimal `supports_tools=true` only.

### OpenRouter `reasoning_effort` wire format

OpenRouter uses the unified nested object: `"reasoning": {"effort": "<level>"}`. The adapter encodes it accordingly when the user sets `reasoning_effort` in `[providers.openrouter]`. OpenRouter silently ignores it for non-reasoning upstream models, so it's safe to leave on by default.

### Provider capability detection

For providers that expose a queryable models endpoint, the adapter performs a one-shot capability detection at startup and populates the `ProviderCapability` dataclass (used to render the banner and gate tool/SoT features per session):

- `lmstudio`: queries `/api/v1/models` (falls back to `/api/v0/models` and `/v1/models`) and reads context length, parameter count, quantization, and modality flags from LM Studio's native response.
- `openrouter`: queries `/models` and reads `architecture.input_modalities` and `supported_parameters` to derive tool/vision/PDF/audio/video flags.
- `ollama`: queries `/api/ps` to surface the running model's allocated context length, then `/api/show` for parameter count and quantization.
- `nvidia`: queries `/models` for connectivity verification (the endpoint returns a flat list with no architecture metadata), and assumes tool support.
- `openai`: skipped — uses the assumed defaults listed above.
- `xai` and unknown names: skipped — minimal `supports_tools=true` default.

### Wizard, `configured` marker, and manual edits

The selector at startup (when no `--provider` flag and no resumed session) considers a provider "configured" iff its `[providers.X]` section in `sot.toml` contains `configured = true`. Single signal — no heuristics on URL or key value.

- **First run** (no `sot.toml`): `_first_run_setup` copies the bundled examples, asks for credentials/URL/model for the chosen provider, and writes `configured = true` at the end.
- **Re-run, picking an unconfigured provider**: a per-provider mini-wizard runs (`_configure_provider_credentials`) — same questions, then writes `configured = true`.
- **Re-run, picking a configured provider**: enters the session directly with whatever the toml currently has.
- **Manual edits**: encouraged. The startup banner renders a `tip:` line in magenta pointing the user to `sot.toml` / `sot.keys.toml`. To force the wizard for a given provider again, just delete its `configured = true` line.

The wizard does not exist for `--provider X` overrides or session resumes (`sot-cli <session_id>`), since both paths assume the user already knows what they want.

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

Typical discovery flow:

- `list_dir` is the primary discovery tool for any file type or use case. Use it first when you need to discover, filter, or narrow down files.
- Once you know the exact path set, switch to `read_files` — it is the single tool for reading file content, used for both one file and batches (pass a `files` array with one or more entries).
- Prefer full-file reads whenever practical so the Source of Truth receives the whole authoritative file snapshot.
- Use line-range reads (`start_line`/`end_line`) for targeted inspection of specific sections — for example, following up on a search result or examining a known function.

## Code Search Tool (`search_code`)

`search_code` is a regex-powered content search tool built on [ripgrep](https://github.com/BurntSushi/ripgrep). It complements `list_dir` for cases where you need matching lines with exact line numbers and surrounding context — particularly useful when working with source code.

Supported parameters:

- `pattern` (required): regex pattern to search for.
- `path`: file or directory to search in. Defaults to project root.
- `glob`: file pattern filter (e.g., `"*.py"`, `"*.{ts,tsx}"`).
- `type`: language type filter (e.g., `"py"`, `"js"`, `"rust"`).
- `output_mode`: `"files_with_matches"` (default, returns file paths), `"content"` (matching lines with context), `"count"` (match counts per file).
- `context_before` / `context_after` / `context`: lines of surrounding context in content mode.
- `show_line_numbers`: line numbers in content output (default `true`).
- `case_insensitive`: case-insensitive matching (default `false`).
- `multiline`: patterns spanning multiple lines (default `false`).
- `head_limit`: max result entries (default `200`). Pass `0` for unlimited.
- `offset`: skip first N results for pagination.

When to use `list_dir` vs `search_code`:

- **`list_dir`** is the general-purpose discovery tool. Use it for finding files by name, path, extension, size, timestamps, or broad content matching (`content_contains`). Works for any file type.
- **`search_code`** is specialized for code exploration. Use it when you need regex matching with exact line numbers and surrounding context — finding definitions, usages, imports, or specific patterns across source files.

Typical code exploration flow:

- `search_code` to find where a symbol, function, or pattern is used → `read_files` with `start_line`/`end_line` (per-entry) to inspect the surrounding code → `edit_file` or `apply_text_edits` to make changes.

## File Reading Tool (`read_files`)

`read_files` is the single tool for reading file content into the SoT. It accepts a `files` array — pass a one-element array for a single file, or several entries to batch multiple known paths into the same call. There is no separate single-file reader.

Each `files[]` entry takes `path`, optional `start_line`/`end_line` for UTF-8 text excerpts, and optional `pages`/`password` for PDFs. Files are read independently; partial failures return per-file error entries instead of aborting the whole batch.

Text file behavior:

- Full reads are the default and preferred mode. They populate the SoT with the complete file.
- `start_line` and `end_line` are available for any UTF-8 text file and define a 1-indexed inclusive range. Use them to inspect a known section, follow up on a search result, or examine a specific block without loading the entire file.
- Partial line reads are inspection-only. They do not hydrate the full file into the SoT, which avoids replacing a full authoritative snapshot with a fragment.
- If you want a file to be fully present in the SoT for subsequent reasoning, do a full read.

Non-text behavior:

- PDFs use `pages` rather than `start_line`/`end_line`.
- Images, notebooks, audio, and video do not support line ranges.

## Main components

### `sot_cli/query.py`

The core of the orchestrator. Runs the loop against the provider, executes tool calls, maintains the `SoTState`, and rebuilds the SoT after each tool call.

**Payload Assembly & Prompt Caching Optimization:**
To combat "Lost in the Middle" syndrome and maximize API cost savings, the payload is strictly ordered like this:
`[System Prompt] -> [Past Chat History] -> [Ephemeral SoT Block] -> [Latest User Prompt]`

Why this exact order? **Prefix-Matching Prompt Caching.**
Modern APIs (like Anthropic and OpenAI) cache tokens from top to bottom. If a single token changes, the cache breaks for everything below it.

- If we put the dynamic SoT at the _top_, every time a file is edited, the cache would break, and you would pay full price to re-process the entire 100k+ token chat history.
- By putting the static `Chat History` at the top and the dynamic `SoT Block` at the bottom, the API successfully caches the system prompt and your entire conversation history. The cache only breaks at the very end of the payload when a file changes. This architectural decision makes long sessions incredibly fast and cheap.

### `sot_cli/source_of_truth.py`

Materializes the persisted session state as `SourceBundle`. It no longer applies configurable caps like file count or size. If a path is attached to the session, the runtime attempts to materialize it.

### `sot_cli/tools/session/delegate.py` (Multi-Agent Orchestration)

Implements the JIT (Just-In-Time) agent pattern.

**The Boss-Worker Dynamic:**

1. **Delegation:** The main agent (Boss) uses `delegate_task` to spawn a sub-agent (Worker) in the background. The sub-agent gets a clean, isolated session (`agent_N`).
2. **Role Separation:** The Boss gets the `SYSTEM_PROMPT` (orchestration, strategy). The Worker gets the `SUB_AGENT_SYSTEM_PROMPT` (strict execution, no delegation allowed).
3. **Invisible IPC (Inter-Process Communication):** The Worker executes tools and outputs its final findings as plain text. The system intercepts this text and writes it to an internal `response.md` file. _The AI models do not know this file exists._
4. **Synchronization:** The Boss uses `wait_task("agent_N")`. The system blocks until the Worker finishes, reads the internal `response.md`, and returns the text directly to the Boss as a tool result.
5. **Terminal Silence:** Sub-agents run completely headless. Their stdout/stderr is redirected to `agent.log` inside their session folder, keeping the main user terminal perfectly clean.

### MCP servers & runtime details

- The runtime can optionally start and manage external MCP servers configured under `mcp.servers` in `sot.toml`. These servers are discovered at runtime and the tools they expose are added to the provider-visible function/schema list.
- MCP-provided tools are namespaced by server (e.g. `myserver__toolname`) so they do not clash with local tool names. MCP tool calls are proxied through the runtime's `MCPManager` and returned as regular tool results.
- Portable local MCP servers can live under the repository `mcps/` folder and be referenced with relative paths from `sot.toml` so the same config works across machines.
- To support delegated child agents, the runtime honors the `SOT_SESSIONS_DIR` environment variable: when set, the runtime points its `SessionStore` to that directory so sub-agents can run inside a parent's `sessions/` folder.
- The runtime caches provider adapters and performs capability detection when needed; this drives tool availability and SoT composition per-provider.
- The streaming provider adapter emits incremental text, tool-call, reasoning/thinking, and usage events when the provider returns them, and the CLI renders them live instead of buffering the whole response first.
- The host-environment context injected into orchestration rules includes explicit local/UTC date-time and weekday fields (including ISO weekday number) so date answers do not rely on model inference.

Example `sot.toml` MCP entry:

## Current SoT model

### Separation permanent history vs ephemeral SoT

**`chat_history` (permanent):** List of messages that grows between turns. Contains user prompts, assistant responses, and lightweight tool metadata. Never contains file content snapshots.
**SoT block (ephemeral):** Built from disk in each round. Contains the current content of tracked files and media. Injected just before the latest user prompt, and discarded immediately after.

### SoT provenance (Permanent vs. Ephemeral)

The Source of Truth injected into the prompt is a combination of two memory layers:

- **Session-backed (Permanent):** Comes from the persisted `session.json`. These are core files (like preprompts, schemas, or main guidelines) that are refreshed and injected at the start of _every_ turn in that session. Managed via `attach_path_to_source` or the CLI `sot_attach`.
- **Tool-backed (Ephemeral):** Appeared because the model actively read them during the current conversation (e.g., via `read_files` to fix a specific bug). They live in memory to provide immediate context but can be easily discarded using `detach_path_from_source` once the task is done to save tokens.

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
2. `edit_file` makes a single exact-match replacement per call. `apply_text_edits` batches multiple edits (text matching or line ranges) atomically in one call. Neither is a regex engine nor an AST editor.
3. `read_files` supports targeted line-range reads (per `files[]` entry) for any UTF-8 text file, but partial reads intentionally do not populate the SoT as full-file snapshots.
4. `search_code` requires [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) to be installed and available in PATH.
5. Archive files (`zip`, `tar`, `gz`, etc.) cannot be read directly. The model receives a format-specific error with the correct `run_command` invocation to list or extract contents.

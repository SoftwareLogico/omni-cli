# omni-cli 🚀 Limitless Local AI Agent

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Providers](https://img.shields.io/badge/Providers-OpenRouter%20%7C%20LMStudio%20%7C%20Ollama-brightgreen.svg)](https://github.com/softwarelogico/omni-cli)
[![Stars](https://img.shields.io/github/stars/softwarelogico/omni-cli?style=social)](https://github.com/softwarelogico/omni-cli)
[![License](https://img.shields.io/github/license/SoftwareLogico/omni-cli?style=flat&logo=mit)](LICENSE)

<p align="center">
  <img src="assets/omni-robot.png" alt="omni-robot mascot" width="400">
  <br><strong>Token-efficient terminal powerhouse: SoT Method + Multi-Agent + Unrestricted Tools.</strong>
</p>

**A pragmatic, limitless, multi-provider terminal assistant built for developers who hate bloated frameworks.**

`omni-cli` is a limitlessly local Python CLI designed to unleash the true reasoning power of modern LLMs on your projects. By combining a novel architectural pattern called the **Source of Truth (SoT) Method** with aggressive multi-tool batching, it drastically reduces API costs and model iterations while keeping output quality pristine. It acts as a powerful orchestration engine, empowering your AI with local tools and asynchronous sub-agents to solve complex problems seamlessly.

---

## ✨ Key Features

- **📊 SoT Method**: Fresh files from disk every turn. No token bloat, always up-to-date.
- **🤖 Async Multi-Agent**: Delegate trial-and-error to cheap sub-agents (empty ctx).
- **⚡ Batch Orchestration**: Multi-tools + bash/Python scripts in ONE turn.
- **🔧 Full Tools**: 25+ incl. unrestricted shell, precise edits, MCP extensible.
- **🌐 Multi-Provider**: Switch Ollama/LMStudio/OpenRouter live.

👉 [SoT](SoT_Method.md) | [Tools](ARCHITECTURE.md) | [Roadmap](ROADMAP.md)

## 🚀 How to Run

1. Clone the repo and install dependencies:

   ```bash
   pip install -e .
   ```

2. Rename and configure the TOML files:

   ```bash
   # Copy the public configuration file
   cp omni.example.toml omni.toml

   # Copy the private keys file (this file is in .gitignore so your secrets never leak)
   cp omni.keys.example.toml omni.keys.toml
   ```

3. Add your provider API keys to `omni.keys.toml`:

   ```toml
   [providers.openrouter]
   api_key = "sk-or-v1-your-key-here"

   [providers.lmstudio]
   # Usually doesn't need an API key for local models
   api_key = ""

   [providers.ollama]
   # Usually doesn't need an API key for local models
   api_key = ""
   ```

4. Configure providers in `omni.toml` (example provider format):

   ```toml
   [providers.openrouter]
   base_url = "https://openrouter.ai/api/v1"
   model = "z-ai/glm-5.1"
   temperature = 0.7
   max_output_tokens = 8192

   [providers.lmstudio]
   base_url = "http://localhost:1234/v1"
   model = "qwen/qwen3.6-35b"
   temperature = 0.5
   max_output_tokens = 32768

   [providers.ollama]
   base_url = "http://localhost:11434/v1"
   model = "qwen3.5:9b"
   temperature = 0.5
   max_output_tokens = 8192
   ```

5. Run the CLI:

   ```bash
   # Use the default provider set in omni.toml
   omni-cli

   # Or override the provider
   omni-cli --provider [ollama/lmstudio/openrouter]
   #e.g: omni-cli --provider ollama

   #using venv
   source venv/bin/activate
   ./.venv/bin/omni-cli --provider openrouter

   # Resume a previous session

   omni-cli <session_id>

   ```

## 🧠 The Core Concept: The SoT Method

Most AI coding agents fail because they append every file read and every code change directly into the chat history. This leads to massive token bloat and "Lost in the Middle" hallucinations where the AI reads an outdated version of a file from 10 turns ago.

`omni-cli` fixes this by separating **Permanent History** from **Ephemeral State**.

1. **Permanent History (`chat_history`):** Only contains dialogue and lightweight tool metadata (e.g., `"read file X -> added to SoT"`).
2. **Ephemeral Source of Truth (SoT):** This method tracks the latest state of your context files so the model always reads the most up-to-date version, and not 10 different versions of the same file from the chat history. When the model uses a tool to read or edit a file, the SoT updates that file's content. The model can then refer to the SoT for the latest state of any file, without bloating the chat history.

**Result:** The model always sees the absolute latest state of your project. Context grows linearly, not exponentially.
👉 [Read the full SoT Method explanation here.](SoT_Method.md)

## 💸 Token Economy: Scripts > Tool Ping-Pong

We hate "Tool Ping-Pong" (when an AI calls `list_dir`, waits, calls `read_file`, waits, calls `grep`, waits). It burns hundreds of thousands of context tokens.

`omni-cli` is designed to batch operations. The system prompts force the model to use `run_command` to write bash one-liners or Python mini-scripts to execute complex conditional logic in a single turn.

Why use 5 sequential tool calls when the model can just run:
`run_command("for f in $(find . -name '*.py'); do grep -H 'TODO' $f; done")`?

## 🛑 The Anti-Hype FAQ

If you are coming from other trendy AI coding tools, you might be looking for features that we intentionally excluded. Here is why:

### "Where is my `CLAUDE.md` / `rules` file?"

**It's a gimmick.** You don't need a hardcoded framework feature to make an AI read rules. If you have a project guidelines file, just tell the agent: _"Read `guidelines.md` and follow it."_ The agent will add it to the SoT and obey it. We don't hardcode magic filenames.

### "Where are my 'Skills'?"

**A 'Skill' is just a glorified preprompt.** We don't bloat the codebase with fake "skills" (e.g., a React Skill, a Docker Skill). Modern LLMs already know React and Docker. If they need to do something specific, they can write a bash or python script via `run_command` on the fly.

### "Where are the LSP Tools (Go to Definition, Find References)?"

**LSP tools blind the model.** They were invented when models had 8k context limits and had to navigate codebases through a keyhole. Today, models have 200k+ context windows. Forcing a model to use 15 sequential LSP tool calls to understand a flow destroys the "Big Picture" and wastes tokens. Just load the 3 relevant files into the SoT and let the model read the actual code.

### "Why is there no Context Compaction / Summarization?"

**Because it causes lobotomies.** Summarizing past turns makes the model forget crucial details. By using the SoT Method, our `chat_history` only contains metadata and dialogue. It grows so slowly that you will likely finish your task long before hitting the 200k token limit.

### "Where are the Slash Commands (`/clear`, `/file`)?"

**This is an autonomous agent, not a basic chatbot.** If the model needs a file, it uses a tool to read it. You shouldn't be manually typing commands to manage its context.

---

## 🤖 Asynchronous Multi-Agent Orchestration

`omni-cli` supports a Boss-Worker delegation model using Just-In-Time (JIT) sub-agents.

If your main SoT is heavily loaded (expensive context), the main agent can use `delegate_task` to spawn a sub-agent in the background with a clean, empty context.
The sub-agent does the dirty work (messy searches, trial-and-error shell scripts, compiling), logs everything silently to `agent.log`, and returns a clean report to the Boss via invisible IPC.

The Boss orchestrates. The Workers execute. Your terminal stays clean.

For full agent/sub-agent command reference (including CLI flags and orchestration tool parameters), see [ARCHITECTURE.md](ARCHITECTURE.md#agent-and-sub-agent-commands--parameters).

---

## 🧰 Available Tools

For the complete and up-to-date tool and parameter reference, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 🔌 MCP Servers

You can easily extend `omni-cli` with external tools using the Model Context Protocol (MCP). Just add them to your `omni.toml`:

```toml
[mcp.servers.test]
command = "python"
args = ["mcps/test.py"]
```

The runtime will automatically start the server and expose its tools to the AI.

---

### ⚠️ WARNING: No Guardrails. No Policies.

This tool is **limitless**. It is not built for end-users; it is built for power users. It really can do anything you ask as long as is within the capabilities of your system. It does not have a babysitter checking its actions. **It will execute what you tell it to execute without hesitation.** Use it responsibly.

## 🌟 Star & Contribute

⭐ **Star if it saves your API bill!** [Star Here](https://github.com/softwarelogico/omni-cli)

- 🐛 PRs/issues welcome (see [ROADMAP](ROADMAP.md)).
- 📢 Share: "omni-cli: AI agent without token waste #AICoding"

## 👤 Author & Credits

**Created by Ramses Mendoza (SoftwareLogico)**

I built `omni-cli` and formalized the **Source of Truth (SoT) Method** for terminal agents out of frustration with existing tools. Most AI coding assistants on the market are bloated, burn through tokens, and collapse under the weight of their own context windows.

While the concept of maintaining a "state" is common in software engineering, the specific architectural pattern of decoupling a permanent metadata-only history from an ephemeral, fully-rebuilt file block—and injecting it right before the user prompt—is the core innovation of `omni-cli`.

**LinkedIn:** https://www.linkedin.com/in/ramsesisaid

This tool was designed for absolute power, raw speed, and extreme token efficiency, **since it follows no agenda other than being truly useful.** It doesn't babysit you, it doesn't enforce corporate safety rails on your local machine, and it doesn't waste your API credits on unnecessary framework overhead.

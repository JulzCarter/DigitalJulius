# DigitalJulius

> Multi-agent orchestrator routing across Claude, OpenAI, Codex, Gemini, and GitHub Models.

DigitalJulius is a local REPL and one-shot runner that classifies a prompt, picks the right agent stack, drafts plans for non-trivial work, and hands execution to the strongest available worker. v0.2.0 is built around a dual-boss hierarchy: Claude owns orchestration, OpenAI is the secondary boss, Codex handles deep coding, and Gemini/GitHub Models/completion providers fill worker and fallback roles.

## Install

From PyPI:

```bash
pipx install digitaljulius
```

From source:

```bash
git clone <repo> DigitalJulius
cd DigitalJulius
pip install -e .
```

Requires Python 3.11+. Installs both commands:

```bash
digitaljulius
dj
```

## First run

```bash
dj --login
```

The login wizard probes installed agents and walks missing auth in-place:

- Claude Code: run its OAuth flow through the `claude` CLI.
- OpenAI: paste an API key; stored in `~/.digitaljulius/secrets.json`.
- Codex: install/login to the `codex` CLI, or set `OPENAI_API_KEY`.
- Gemini: authenticate the `gemini` CLI.
- GitHub Models: authenticate `gh` with access to `gh models run`.

You can also fix auth later from the REPL with `/auth`, `/auth <agent>`, or `/openai set-key <KEY>`.

## Quick start

Interactive REPL:

```bash
dj
```

One-shot prompt:

```bash
dj -p "summarise this repo"
```

Safe mode: workers run without YOLO / skip-permissions.

```bash
dj --safe -p "make the smallest safe fix for the failing test"
dj -s -p "explain the architecture"
```

Other useful flags:

```bash
dj --cwd D:\path\to\repo
dj --verbose
```

## Architecture

Default hierarchy:

```text
Claude (primary boss)
  -> OpenAI gpt-5 (secondary boss)
    -> Codex gpt-5-codex (deep coding worker)
      -> Gemini / GitHub Models / completion providers (workers and fallback)
```

The main loop is:

1. Classify the prompt into `SIMPLE`, `MODERATE`, `COMPLEX`, or `CRITICAL`.
2. Pick routing tags and candidate agents from `config.py`.
3. For `COMPLEX` and `CRITICAL`, draft a plan before execution.
4. Show the plan collapsed; `+` expands, `-` collapses, `y` approves, `e` edits a step, `n` aborts.
5. Execute through the selected agent(s).
6. Review/synthesise with the planning role when the tier requires it.
7. Fall back across models and agents when quota or auth blocks a route.

Live progress uses a single Rich status spinner with phase labels:

| Phase | Meaning |
|---|---|
| 🧠 thinking | classifying complexity and tags |
| 📋 planning | drafting or reviewing a plan |
| ⚡ executing | worker agent is running |
| 🔍 reviewing | approver is checking output |
| 🧩 synthesising | merging consensus output |
| 👯 twin | optional second consensus pass |
| 🧭 routing | selecting/fanning out agents |
| 📁 progress | reporting file changes found after execution |
| ⚡ escalate | rotating to the next boss/agent |

If a route rotates mid-turn, the next agent receives a `[CONTEXT HANDOFF]` preamble with the previous agent, model, failure reason, and output snippet.

## Routing tags

These are the default routing tags in `DEFAULT_CONFIG.routing`:

| Tag | Default order | Use |
|---|---|---|
| `architecture` | `claude -> openai -> codex -> gemini -> github` | Architecture and design decisions. |
| `refactor` | `claude -> openai -> codex -> github -> gemini` | Code reshaping where correctness matters more than speed. |
| `quick_edit` | `github -> openai -> gemini -> claude -> codex` | Small direct edits and cheap worker tasks. |
| `long_context` | `claude -> openai -> github -> gemini -> codex` | Large repo/context reads; Claude leads, OpenAI follows. |
| `web_search` | `openai -> gemini -> claude -> github -> codex` | Fresh external information. |
| `math` | `openai -> claude -> codex -> gemini -> github` | Calculation and formal reasoning. |
| `deep_coding` | `codex -> claude -> openai -> gemini -> github` | Complex coding where Codex can use its own coding loop. |
| `self_modify` | `claude` | Prompts that look like changing DigitalJulius itself. |
| `default` | `claude -> openai -> codex -> gemini -> github` | General fallback route. |

Meta-modification prompts are detected before the classifier. Requests like "fix your log display", "change your router", or "update DigitalJulius" pin to `self_modify` so Claude handles the source-aware path.

## Slash commands

Current command set from `digitaljulius/commands.py`:

| Command | Syntax | Behavior |
|---|---|---|
| `/help` | `/help` | Show this list. |
| `/agents` | `/agents` | List configured agents, auth, and model status. |
| `/budget` | `/budget` | Show daily quota usage. |
| `/auth` | `/auth [agent\|all]` | Probe auth; optionally reset and re-auth one/all agents. |
| `/route` | `/route <prompt>` | Classify a prompt and show preferred routing without running it. |
| `/best` | `/best` | Show the top available model for each configured agent. |
| `/model` | `/model <agent> <model>` | Persist an agent's top model override. |
| `/consensus` | `/consensus <prompt>` | Force a three-agent consensus-style run. |
| `/spawn` | `/spawn <agent>` | Pin future prompts to one agent. |
| `/spawn off` | `/spawn off` | Unpin `/spawn`. |
| `/switch` | `/switch [agent] [model]` | Show agents, pin an agent, and optionally persist a model. |
| `/switch off` | `/switch off` | Return to auto-routing. |
| `/log` | `/log` | Show this REPL session's turns. |
| `/log file` | `/log file` | Print the runtime log path. |
| `/log tail` | `/log tail [N]` | Show the last N runtime log lines; default 30. |
| `/openai` | `/openai` | Show OpenAI key/model status and Codex CLI status. |
| `/openai set-key` | `/openai set-key <KEY>` | Store `OPENAI_API_KEY` in the secret vault. |
| `/openai model` | `/openai model <MODEL>` | Persist OpenAI's top model. |
| `/openai test` | `/openai test` | Send a tiny ping to verify the OpenAI key. |
| `/audit` | `/audit [agent] [text-or-last]` | Run a 2-turn cross-critique: auditor critique, original author rebuttal. |
| `/plan` | `/plan <prompt>` | Force plan-then-execute for this prompt. |
| `/init` | `/init` | Write `./.digitaljulius/PROJECT.md` if missing. |
| `/yolo` | `/yolo on\|off` | Toggle skip-permissions mode for worker runs. |
| `/learn` | `/learn [kind] <text>` | Save a lesson. Kinds: `learned`, `preferences`, `routing`, `failures`. |
| `/knowledge` | `/knowledge` | Show accumulated lessons. |
| `/forget` | `/forget <needle>` | Remove saved lessons matching text. |
| `/self` | `/self <instruction>` | Let DigitalJulius plan and apply a self-modification. |
| `/clear` | `/clear` | Clear the screen. |
| `/quit` | `/quit` | Exit the REPL. |

There is no `/providers`, `/quota`, `/next`, or `/openai`-only shortcut beyond the commands above in v0.2.0.

## Configuration

Main config is written to:

```text
~/.digitaljulius/config.toml
```

It holds enabled agents, model fallback chains, budget caps, routing tables, role assignments, and memory limits. Existing configs are migrated on load: retired `qwen` references are removed, OpenAI/Codex defaults are backfilled, and stale long-context or role assignments are corrected.

Runtime logs:

```text
~/.digitaljulius/logs/digitaljulius.log
~/.digitaljulius/logs/session_<timestamp>.jsonl
```

`digitaljulius.log` is a rotating Python log: 5 MB x 5 backups. Session JSONL logs record final per-turn outcomes for the current REPL session.

Secrets:

```text
~/.digitaljulius/secrets.json
```

Secrets are written atomically via temp-file replace. If the file cannot be parsed, DigitalJulius refuses to write until you back it up/remove it. Environment variables named `DJ_SECRET_<NAME>` override vault entries.

## Providers

Built-in agent names are reserved:

```text
claude, openai, codex, gemini, github
```

Completion providers are text-in/text-out providers used by role calls and fallback paths. User providers live in:

```text
~/.digitaljulius/providers.toml
```

Supported completion adapters include Anthropic, OpenAI-compatible endpoints, and Ollama. Built-in recipes exist for `anthropic-api`, `openai`, `openrouter`, `groq`, `deepseek`, `together`, `mistralai`, and `ollama`. Do not name a custom provider the same as a built-in agent; collisions are rejected or ignored.

## Project context

On each run, DigitalJulius auto-loads project memory from the current working directory when present:

```text
.digitaljulius/PROJECT.md
.shared-agent-context/CURRENT_CONTEXT.md
.shared-agent-context/DECISIONS.md
CLAUDE.md
AGENTS.md
GEMINI.md
```

The block is capped and prepended as repo context, not treated as the user's latest prompt. `/init` creates a starter `.digitaljulius/PROJECT.md`.

## Safety notes

- Default CLI flag behavior is YOLO on unless `--safe` is passed.
- `dj --safe` / `dj -s` runs workers without YOLO / skip-permissions.
- `/yolo on|off` toggles the mode inside the REPL.
- Launching from home or a filesystem/drive root prints a cwd guard warning, because YOLO workers may scan unrelated projects from there.
- Plan-first runs do not silently downgrade top-tier planning models. If top-tier planners are exhausted, the REPL asks you to pick a lower-tier planner or abort.

## License

MIT — see [LICENSE](LICENSE).

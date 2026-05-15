# DigitalJulius

> **A meta-orchestrator that routes one prompt across Claude Code, Gemini CLI, and GitHub Models — picks the best agent(s), runs them headlessly, uses Claude Opus to approve plans and review outputs, and steps down tiers when free-tier quotas run low.**

Built to maximize **accuracy, cost-efficiency, and speed** by combining three already-installed agentic coders behind a single TUI.

---

## What it does

You give DigitalJulius one prompt. It:

1. **Classifies complexity** with a cheap fast model (Gemini Flash, ~1s).
2. **Routes** to the right agent(s) based on a capability matrix:
   - `SIMPLE`   → one agent, cheapest viable model
   - `MODERATE` → one agent + Claude reviews the output
   - `COMPLEX`  → two agents propose in parallel + Claude synthesizes
   - `CRITICAL` → asks you for opt-in to spawn a third "twin" instance
3. **Approves plans** before execution and **reviews outputs** after, using Claude Opus as the gatekeeper.      
4. **Tracks per-agent daily quota** and **auto-steps down** to lower tiers as free-tier limits approach (same pattern as Gemini's quota_guard hook).
5. **Logs everything** to the shared `.shared-agent-context/SESSION_LOG.md` so any agent picking up later sees the full history.

## Install

```bash
git clone <repo> DigitalJulius
cd DigitalJulius
pip install -e .
```

This exposes two commands: `digitaljulius` (the full name) and `dj` (short alias).

## First run

```bash
dj
```

On first launch, DigitalJulius probes Claude Code, Gemini CLI, and GitHub Models for valid credentials. If any are missing, it walks you through authenticating each (open a terminal, run `claude` / `gemini` / `gh auth login` once each — all three have free tiers).

## Slash commands

```
/help              show command list
/agents            status + auth + quota per agent
/budget            today's per-agent quota usage
/route <agent>     pin next prompts to one agent
/consensus         force multi-agent consensus on next prompt
/spawn             escalate to twin instance for next CRITICAL prompt
/switch <agent>    switch pinned agent
/best              reset every agent to its top-tier model
/model <agent>=<model>   override an agent's model
/log "note"        append a note to the shared SESSION_LOG.md
/init              run `agent-context init` in current dir
/auth              re-run the auth probe
/clear             clear scrollback
/quit              exit
```

## Why this design

- **Shell-out over API.** Each underlying CLI already implements its own agentic tool loop (file edits, MCP servers, etc.). DigitalJulius spawns them with `-p` for headless one-shots and orchestrates the conversation. No reinvented wheels.
- **Claude as approver.** Of the three, Claude (Opus) is the strongest reasoner. Using it sparingly as a plan-reviewer and output-checker buys quality without burning quota on every prompt.
- **Local quota tracking.** Free-tier services don't expose remaining-credit APIs. We count requests locally and step down at 90% of known daily caps. Tunable in `~/.digitaljulius/config.toml`.
- **Shared context.** Every routing decision, plan, and output appends to `.shared-agent-context/SESSION_LOG.md` so individual agent sessions can see what happened when you switch back to running them standalone.

## Architecture

```
        prompt
           ↓
    [classify]  ← Gemini Flash
           ↓
   ┌───────┴────────┐
SIMPLE        MODERATE       COMPLEX         CRITICAL
  ↓             ↓              ↓                ↓
1 agent      1 agent         2 agents       opt-in: 3 agents
              ↓                ↓                ↓
              ↓          [synthesize]    [synthesize]
           [review]           ↓                ↓
              ↓            [review]        [review]
              ↓             ↓                ↓
                   ←—— Claude Opus reviews ——→
                              ↓
                         render + log
```

## Run modes

| Mode | Flag | Behavior |
|---|---|---|
| Default | _(none)_ | Confirms before risky tool calls. |
| YOLO | `--yolo` / `-y` | Auto-approves everything. Matches Claude/Gemini/GitHub YOLO. |
| Plan | `--plan` | Shows the routing plan only, runs nothing. |
| Headless | `-p "prompt"` | One-shot, prints answer to stdout. |

## License
MIT — see [LICENSE](LICENSE).

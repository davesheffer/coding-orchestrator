# claude-orchestrator

My global Claude Code setup: an orchestrator that spends its own tokens on judgment and
delegates reading, running and typing to cheaper subagents, plus a context gauge that rolls a
session over before it fills up.

| Piece | File(s) | Lands in |
|---|---|---|
| Orchestrator rules (routing table, briefing/trust, relay protocol) | `CLAUDE.md` | `~/.claude/CLAUDE.md` |
| Subagent personas: `scout` / `runner` (Sonnet), `builder` (Opus), `critic` (Fable) | `agents/*.md` | `~/.claude/agents/` |
| Relay: `[relay]` context gauge on every prompt, blocked stop in the red zone, handoff + auto-open of the next session | `relay/relay.py`, `relay/config.json`, `hooks.json` | `~/.claude/relay/`, hooks merged into `~/.claude/settings.json` |
| One-command PR + CI table | `bin/pr-status` | `~/.claude/bin/pr-status` |
| Same setup for Codex CLI | `codex/PROMPT.md` | paste into Codex |

## Install on a new machine

```bash
git clone <this repo> ~/claude-orchestrator
cd ~/claude-orchestrator && ./install.sh
```

Re-run `./install.sh` after every `git pull`. It never overwrites a `CLAUDE.md` that differs
from the bundle unless you pass `--force` (a `.bak` is kept), and it merges the hooks into
`settings.json` without touching your other settings.

Requirements: Claude Code, `python3`, `gh` (for `pr-status`). Optional: `~/.hunch/agent-gh`
(a GitHub App wrapper) — `pr-status` uses it when present and falls back to `gh`.

## What is deliberately NOT in here

- Credentials of any kind (`~/.hunch/agent-app.json`, `gh` auth, MCP tokens).
- Per-repo config (`.mcp.json`, `.claude/settings.json` inside a repo) — those belong to the repo.
- The `statusLine` from my personal settings (it runs a Hunch command).
- `permissions.allow` lists — machine- and employer-specific; build them with
  `/fewer-permission-prompts` on the new machine.

## Keeping the two machines in sync

Edit the files in this repo, commit, push, then `git pull && ./install.sh` on the other side.
If you edit `~/.claude/agents/*.md` directly, copy it back with `cp ~/.claude/agents/*.md agents/`.

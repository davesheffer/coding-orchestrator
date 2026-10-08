# orch-guard

A Claude Code mod (a plugin of function hooks) that enforces the orchestrator rules in `CLAUDE.md` when a tool is called, instead of trusting the model to remember them. [Mission Control](../mission-control/README.md) shows the orchestrator's state; orch-guard keeps the session inside the rules. The two work side by side.

## What it enforces

| Rule in CLAUDE.md | What orch-guard does |
|---|---|
| Use the named roles on their models; avoid expensive model inheritance | Refuses an `Agent` call that sends `scout`/`runner`/`builder` on anything but `sonnet`, sends `critic` on anything but `fable`, or starts a built-in agent (`general-purpose`, `Explore`, `Plan`, …) with no `model` |
| A subagent follows its brief without recursive delegation | Refuses `Agent` calls made inside a subagent |
| Push, publish, deploy, delete and send stay in the main session; networked PR polling stays in the main session | Refuses these inside a subagent: `git push`, `gh pr create/merge/comment`, `gh api`, `npm publish`, `docker push`, `kubectl apply`, `terraform apply`, `rm -rf`, `git reset --hard`, `curl -X POST`, `pr-status` / `gh pr checks`, and MCP write tools |
| Require checks after the last edit | Refuses an outward ship (`git push`, `gh pr create/merge`, publish, deploy, GitHub MCP writes) while a code file edited by the main loop or by any subagent has no passing check after it. A check is a test, build, lint or type-check command, `task verify`, or `claude plugin test`. Docs (`*.md`, `docs/`) are exempt |
| Risky work requires critic review before completion | Refuses that same ship while risky files (auth, security, secrets, migrations, schemas, install, guards, workflows, hook and settings JSON, …) have changed since the last critic `RESULT: SHIP` |
| `RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED`; weak cards mean verify or escalate, not retry | After a role agent returns, adds a reminder the model reads: a missing card, medium or low confidence, or UNVERIFIED items mean verify directly or escalate. Builders get "read the diff and confirm checks ran after the last edit". A critic `FIX FIRST` or `RETHINK` gets "resolve each finding" |

A built-in agent sent on `fable` counts as the critic, since CLAUDE.md reserves fable for the critic.

A refused call returns its reason to the model, so the model can fix the problem and try again. The status line shows the ledger, for example `orch: 2 unchecked · critic due (1 risky)`. When a turn ends with something still pending, a toast says so.

## Commands

- `/orch-guard` shows what is pending and what blocks a push or publish.
- `/orch-guard waive <reason>` lets the next push or publish through. The waiver ends at the next edit. Only the person can run it: a waiver started by a plugin or a scheduled prompt is refused.
- `/orch-guard reset` clears the ledger.

## Options

Set these in the config menu or under `pluginConfigs.orch-guard` in settings:

- `mode`: `enforce` (default) refuses calls. `warn` lets the call run, shows a toast and leaves the model a reminder. `off` only tracks.
- `riskyPattern`: an extra regular expression for paths that need critic review, for example `^relay/|jev`.

## Install

Copy this folder to `~/.claude/mods/orch-guard`, then add it to `CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of `~/.claude/settings.json`. Mission Control goes in the same list:

```json
"env": {
  "CLAUDE_CODE_PLUGIN_DIRS": "/home/<you>/.claude/mods/mission-control:/home/<you>/.claude/mods/orch-guard"
}
```

Separate the paths with `;` on Windows. To try it for one session, run `claude --plugin-dir mods/orch-guard`.

## Develop

```
claude plugin validate mods/orch-guard
claude plugin test mods/orch-guard
```

`hooks/policy.ts` holds the rules as pure functions. `hooks/register.ts` wires them to `tool.call`, `agent.spawn` and `turn.complete`.

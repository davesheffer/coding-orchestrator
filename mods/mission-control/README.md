# Mission Control

A Claude Code mod (a plugin of function hooks) that shows the orchestrator's state inside Claude Code.

## What it shows

**Band above the prompt**

```
● AMBER 162k/250k ██████░░░░  Jev ✔  🧠 hunch 5 ⚠1  ⚙ scout·sonnet, builder·sonnet  last runner HIGH  [ orch ] [ hide ]
```

- The relay zone (GREEN / AMBER / RED) and context tokens, against `relay/config.json` `soft_tokens` / `hard_tokens`.
- Whether Jev is online, or the reason it is not (for example `NoApiKey`).
- How many Hunch calls the current task has made; `⚠N` counts the ones that warned or failed.
- The subagents running now, as role·model.
- The confidence of the last role report; `⚠N` counts its UNVERIFIED items.

**`/orch` pane**

- Relay: the gauge, the session's cost and what the zone asks of you.
- In flight: the running subagents and how long each has run.
- Report cards: the last six scout / runner / builder / critic reports, parsed for RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED, exit codes and missing fields.
- Hunch: the current `htask_` id, calls per tool, and the last eight calls (`mcp__hunch__*` tools and the hunch CLI run through Bash, `task verify` included) with caller, duration, target and outcome: constraint counts, merge verdict, escalations, verify exit code.
- Jev: online or offline, today's spend, risk-gate denies and weak reports, and its last eight decisions from `relay/jev-log.jsonl`.

**Toasts** when the zone changes, a role report comes back weak (a missing field, confidence below high, or UNVERIFIED items), the Jev risk gate denies an action, Jev flags a weak report, Jev goes offline, a Hunch invariant of warning severity or above first applies (once per session), a Hunch merge verdict is BLOCK, Hunch escalations need your decision, a `task verify` check fails, a Hunch call errors, or a task is finished without a `hunch_context` brief.

`/orch band` hides or shows the band; while it is hidden, a one-line summary sits in the status line.

## Install

Copy this folder to `~/.claude/mods/mission-control`, then name it in the `env` block of `~/.claude/settings.json` so every session in every repo loads it:

```json
"env": {
  "CLAUDE_CODE_PLUGIN_DIRS": "C:\\Users\\<you>\\.claude\\mods\\mission-control"
}
```

Several plugin folders are separated by the platform's path-list separator (`;` on Windows, `:` elsewhere). Open sessions pick it up after a restart; an interactive session then reloads the mod whenever a file in the folder is saved.

## Develop

```
claude plugin validate mods/mission-control
claude plugin test mods/mission-control
```

The engine writes the API types into `.claude-plugin/types/` and a `tsconfig.json` beside them when it loads the mod; both are generated and ignored here.

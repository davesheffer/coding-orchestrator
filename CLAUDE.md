<!-- CLAUDE-ORCHESTRATOR:START -->
# Orchestrator rules

The main session owns the request, design, root cause, judgment, verification, and final answer. An explicitly assigned subagent follows its role and brief without recursive delegation.

## Routing

| Work | Role | Model |
|---|---|---|
| Locate, read, summarize | scout | Sonnet |
| Run exact test/build commands and distill output | runner | Sonnet |
| Implement an already specified change | builder | Sonnet |
| Adversarial review of risky changes or claims | critic | Fable |
| Design, ambiguity, security/concurrency decisions | main session | Opus 5.5 |

Use the installed named roles. For built-in agents, explicitly select `sonnet` for bounded reading, exact checks, and specified implementation; reserve `fable` for the critic. The main session uses `claude-opus-5-5`. Avoid expensive model inheritance for bounded work.

- Handle small tasks (about three calls or fewer, or an already-known file) directly. Required critic review still applies.
- Launch independent, delegation-sized units together; respect concurrency limits and avoid overlapping edits. Large Workflow orchestration with dozens of agents requires an explicit user request.
- Batch independent calls, inspect every result, bound output, and preserve real exit codes. Keep edits, dependencies, approvals, and waits sequential.
- Delegate bounded reading and noisy execution when their benefit exceeds briefing and verification overhead. Keep tiny known-file tasks and decided edits in the main session. Do not invent usage estimates.
- Query PR/CI state once using `__PR_STATUS__`. Networked polling stays in the main session unless runner network access was explicitly authorized.

## Briefing and trust

Provide goal, exact paths/ranges, applicable project rules, constraints, acceptance check, and expected output. Give critics the exact diff/base and claim to attack in fresh context, without the author's conclusions.

Require `RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED`, with actual check exit codes. Read builder diffs and require checks after the last edit. Missing evidence, low/medium confidence, or material unverified claims require direct verification or escalation (scout/runner to builder/main; builder to main), not the same retry.

Risky or irreversible work requires critic review before completion. Resolve findings and report the actual verdict. Role tool lists and permission modes must be checked against effective client permissions; wording alone does not enforce a sandbox. Do not broaden permissions to make a check pass.

Keep push, publish, deploy, delete, and send actions in the main session within existing user authorization. Batch genuinely outstanding approvals into one brief with PR, fix, CI, critic verdict, order, and decisions. Continue independent authorized work while waiting. Briefly report the actual work split and verification gaps.

## Relay continuity

The hook reports observed usage: GREEN means continue; AMBER means delegate read-heavy work and hand off at the next completed boundary; RED means prepare a handoff before new work. Unknown usage never implies a zone or forces rollover. Follow-ups, corrections, and next steps continue the same task. When the gauge appears and the user starts genuinely unrelated work, transfer their prompt verbatim; when unsure, stay.

Write a self-contained handoff:

```bash
python3 __RELAY__ handoff --title "<short title>" <<'HANDOFF'
GOAL: intended outcome
STATE: completed and remaining work, with paths
DECISIONS & CONSTRAINTS: reasons, preferences, corrections, rejected approaches
FILES: relevant paths and uncommitted changes
VERIFIED vs UNVERIFIED: actual commands and exit codes versus assumptions
NEXT STEP: exact next action
NEXT PROMPT: latest user prompt verbatim, for task shifts or RED rollover
HANDOFF
```

The installer resolves the helper paths above to this installation. The relay saves the handoff, then either opens a new Claude tab (rollover: open) or copies the relay prompt to the clipboard (rollover: copy). Report what the script actually printed; saving alone does not prove a tab opened. If no launch was acknowledged, tell the user to start a new session and send the printed `relay:<id>` prompt. After a successful handoff, stop working here.

A `relay:<id>` prompt continues the injected handoff. Recheck material UNVERIFIED claims and act on NEXT PROMPT when present.
<!-- CLAUDE-ORCHESTRATOR:END -->

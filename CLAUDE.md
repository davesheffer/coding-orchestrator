<!-- CLAUDE-ORCHESTRATOR:START -->
# Orchestrator rules

The main session owns the request, design, root cause, judgment, verification, and final answer. An explicitly assigned subagent follows its role and brief without recursive delegation.

## Routing

| Work | Role | Model |
|---|---|---|
| Locate, read, summarize | scout | Sonnet |
| Run exact test/build commands and distill output | runner | Haiku |
| Implement an already specified change | builder | Sonnet |
| Adversarial review of risky changes or claims | critic | Fable |
| Design, ambiguity, security/concurrency decisions | main session | Opus 5.5 |

Use the installed named roles. For built-in agents, explicitly select `haiku` for exact checks and `sonnet` for bounded reading and specified implementation; reserve `fable` for the critic. The main session uses `claude-opus-5-5`. Avoid expensive model inheritance for bounded work. If a Fable critic fails with a rate limit or overload (429/529), retry it once; if it fails again, run the same fresh critic brief on `opus` and report the substitution with the verdict. Never skip a required review or replace it with a self-review.

- Handle small tasks (about three calls or fewer, or an already-known file) directly. Required critic review still applies.
- Launch independent, delegation-sized units together; respect concurrency limits and avoid overlapping edits. Large Workflow orchestration with dozens of agents requires an explicit user request.
- Batch independent calls, inspect every result, bound output, and preserve real exit codes. Keep edits, dependencies, approvals, and waits sequential.
- Delegate bounded reading and noisy execution when their benefit exceeds briefing and verification overhead. Keep tiny known-file tasks and decided edits in the main session. Do not invent usage estimates.
- For a yes/no, choice or score judgement about files whose text you don't need, `__ASK_JEV__ -q '<questions json>' <paths>` asks Jev without loading them (`--help-questions` shows the format; exit 3 means Jev is off, so read instead). Scouts cannot run it (their Bash is limited to `ro.py` read verbs), so run it yourself or brief a runner.
- Query PR/CI state once using `__PR_STATUS__`. Networked polling stays in the main session unless runner network access was explicitly authorized.

## Briefing and trust

Provide goal, exact paths/ranges, applicable project rules, constraints, acceptance check, and expected output. Give critics the exact diff/base and claim to attack in fresh context, without the author's conclusions. Put a `REVIEW-SCOPE: <absolute repo path> base=<full commit sha: the remote tip you push over, or the branch point for a new branch> [paths=<p1>,<p2>]` line in the critic prompt; the pre-push gate's ledger records only that scope, as one exact file set (`git add -N` new files first), and only for a critic run in the background (a foreground critic never records).

Require `RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED`, with actual check exit codes. Read builder diffs and require checks after the last edit. Missing evidence, low/medium confidence, or material unverified claims require direct verification or escalation (scout/runner to builder/main; builder to main), not the same retry. A partial subagent result (cap reached) is resumed with SendMessage `continue from your report file`, not relaunched.

Risky or irreversible work requires critic review before completion. Resolve findings and report the actual verdict. A critic round blocks only on a regression against the base or the round's own fix not working; log other gaps as residuals for the owner. After three fix/critic rounds on the same change without a pass, stop and bring the owner one decision (ship the version with no base regressions, change approach, or drop it) instead of starting a fourth round. Role tool lists and permission modes must be checked against effective client permissions; wording alone does not enforce a sandbox. Do not broaden permissions to make a check pass.

Keep push, publish, deploy, delete, and send actions in the main session within existing user authorization. Batch genuinely outstanding approvals into one brief with PR, fix, CI, critic verdict, order, and decisions. Continue independent authorized work while waiting. Briefly report the actual work split and verification gaps.

## Relay continuity

The hook reports observed usage: GREEN means continue; AMBER means delegate read-heavy work and hand off at the next completed boundary; RED means prepare a handoff before new work. Unknown usage never implies a zone or forces rollover. Follow-ups, corrections, and next steps continue the same task. When the gauge appears and the user starts genuinely unrelated work, transfer their prompt verbatim; when unsure, stay.

Write a self-contained handoff:

```bash
__PYTHON__ __RELAY__ handoff --title "<short title>" <<'HANDOFF'
GOAL: intended outcome
STATE: completed and remaining work, with paths
DECISIONS & CONSTRAINTS: reasons, preferences, corrections, rejected approaches
FILES: relevant paths and uncommitted changes
RUNNING: still-running agents/background tasks: id, purpose, report path, how to collect (or "none")
VERIFIED vs UNVERIFIED: actual commands and exit codes versus assumptions
NEXT STEP: exact next action
NEXT PROMPT: latest user prompt verbatim, for task shifts or RED rollover
HANDOFF
```

If the shell rejects the heredoc (for example, `unexpected EOF while looking for matching`), do not retry it. Write the same body to a scratch file with the file tool and run `__PYTHON__ __RELAY__ handoff --title "<short title>" --body-file <file>`.

The installer resolves the helper paths above to this installation. The relay saves the handoff, then either opens a new Claude tab (rollover: open) or copies the relay prompt to the clipboard (rollover: copy). Report what the script actually printed; saving alone does not prove a tab opened. If no launch was acknowledged, tell the user to start a new session and send the printed `relay:<id>` prompt. After a successful handoff, stop working here.

With rollover "inplace" the session is never stopped for size: follow the gauge text instead of the zone rules above (keep the session checkpoint current with `relay.py checkpoint`, keep working, and after a compaction use the re-injected checkpoint). A task shift still uses a handoff; the tab clears and continues by itself.

A `relay:<id>` prompt continues the injected handoff. Recheck material UNVERIFIED claims and act on NEXT PROMPT when present. On resuming, collect RUNNING items before relaunching equivalent work; an agent from the old session cannot be messaged, so read its report path, and relaunch only if no report exists.
<!-- CLAUDE-ORCHESTRATOR:END -->

<!-- HUNCH:START — auto-generated, do not edit by hand -->
<!-- hunch:template 2 -->
<!-- hunch:variant lean -->
## 🧠 Hunch (Engineering Memory)

This repo has **Hunch**, a graph of *why* the code is the way it is. It holds **0 decisions, 0 bugs, 0 constraints, 9 components, 0 policies**. The `hunch_*` MCP tools and hooks deliver it; their instructions say when.

Per task: reuse the hook's task or `hunch_task(action: "start", title)` once; `hunch_context(target, task_id)` first; `hunch_check_constraints(scope)` before editing shared code; run checks through the hook's `task verify` launcher; `hunch_task(action: "finish", task_id)` and show its card verbatim at the end; record only what the human decides or corrects.
<!-- HUNCH:END -->

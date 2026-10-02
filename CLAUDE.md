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
- For a yes/no, choice or score judgement about files whose text you don't need, `__ASK_JEV__ -q '<questions json>' <paths>` asks Jev without loading them (`--help-questions` shows the format; exit 3 means Jev is off, so read instead). Include the command in scout briefs.
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
__PYTHON__ __RELAY__ handoff --title "<short title>" <<'HANDOFF'
GOAL: intended outcome
STATE: completed and remaining work, with paths
DECISIONS & CONSTRAINTS: reasons, preferences, corrections, rejected approaches
FILES: relevant paths and uncommitted changes
VERIFIED vs UNVERIFIED: actual commands and exit codes versus assumptions
NEXT STEP: exact next action
NEXT PROMPT: latest user prompt verbatim, for task shifts or RED rollover
HANDOFF
```

If the shell rejects the heredoc (for example, `unexpected EOF while looking for matching`), do not retry it. Write the same body to a scratch file with the file tool and run `__PYTHON__ __RELAY__ handoff --title "<short title>" < <file>`.

The installer resolves the helper paths above to this installation. The relay saves the handoff, then either opens a new Claude tab (rollover: open) or copies the relay prompt to the clipboard (rollover: copy). Report what the script actually printed; saving alone does not prove a tab opened. If no launch was acknowledged, tell the user to start a new session and send the printed `relay:<id>` prompt. After a successful handoff, stop working here.

A `relay:<id>` prompt continues the injected handoff. Recheck material UNVERIFIED claims and act on NEXT PROMPT when present.
<!-- CLAUDE-ORCHESTRATOR:END -->

<!-- HUNCH:START — auto-generated, do not edit by hand -->
## 🧠 Hunch (Engineering Memory)

This repo has **Hunch** — a curated graph of *why* the code is the way it is (decisions, bug history, invariants). It currently holds **0 decisions, 0 bugs, 0 constraints, 0 components, 0 policies**.

**Consult Hunch via the `hunch_*` MCP tools — pick by MOMENT, not from memory:**

**Orient (session/task start):**
- If the host's prompt hook already opened the task and printed a task ID plus a `task verify` command, reuse that exact ID and command — do NOT call `hunch_task(action: "start")` for it. Otherwise start the task yourself: call `hunch_task(action: "start", title: <short task title>)` once and take `verification_argv` from its result. Each new prompt has its own ID; reuse the ID for follow-up work on the same task and never borrow another task's ID. This is task bookkeeping; `hunch_context` remains the first memory lookup. If reporting fails, continue the work and disclose the gap.
- When the user asks to **update Hunch**, run `hunch update` from this repository root. It updates to the latest release and repairs all configured harness pins. Use `hunch update --global` to also update a global CLI alongside a repository dependency; reconnect active MCP sessions afterward.
- `hunch_context(target, task_id)` — the minimal relevant slice for what you're about to do; a task phrase falls back to the closest graph matches. **Call FIRST** for memory. Include the current task ID on each context call so its contribution is inspectable.
- `hunch_structure(target?)` — the indexed shape of the repo/dir/file/symbol — orient from the graph, not grep rounds.
- `hunch_workspaces(view?)` — which worktrees and branches are open on which machine, what is merged and deletable (read-only; this machine live, others from memory). Call it instead of `git branch` / `git worktree list`; never delete on its say-so.
- `hunch_runbook(task)` — the proven steps for a recurring task, before re-deriving them.
- `hunch_escalations()` — the decisions only the HUMAN can make (including one exact imported ADR at a time, topic conflicts, and policy calls). Normally empty; when it isn't, ASK the user inline — an entry is a question, silence is never approval. Apply an ADR answer only through `hunch_review_imported_adr` with its printed source and review hashes.
- `hunch now` (CLI) — recent decisions + the live roadmap; `hunch log` — the memory-move timeline (every capture/adopt/supersede/prune/repair, each revertable).

**Before designing / choosing an approach:**
- `hunch_why(target)` — why a file/symbol is shaped this way (decisions, bugs, constraints) — including what was already REJECTED.
- `hunch_current_decision(topic)` — the one live answer for a topic (history + rejected included).
- `hunch_bug_lineage(symptom_or_symbol)` — has this failed before? what was the root cause?
- `hunch_compare(candidates)` — rank candidate branches/commits by fewest invariant hits.
- `hunch_query(query)` — free-text search when nothing above fits.

**Before editing:**
- `hunch_check_constraints(scope)` and `hunch_get_dependents(symbol)` / `hunch_blast_radius(target)` — invariants in scope + who you'd break. (The pre-edit hook injects this per file automatically; call these for PLANNING breadth.)
- `hunch_findings(scope?)` — known-but-unfixed gaps in the area (past audits, measurements, incidents) so you inherit them instead of re-discovering them.

**Before committing / merging:**
- `hunch_conformance()` — does the code still SATISFY recorded intent? Run before and after a refactor.
- `hunch_policy_evaluate(policy_id?, active_only?)` / `hunch_policy_plan(policy_id)` / `hunch_policy_card(policy_id)` / `hunch_policy_proof(policy_id)` — evaluate canonical policy, inspect the planned corpus, review the evidence/uncertainty card, and inspect raw replay receipts; only an explicit human activation grants authority.
- `hunch_pr_impact(base?)` / `hunch_merge_verdict(...)` — a change's memory surface; would it re-open a closed bug?

**Before the final response — make Hunch's contribution visible:**
- When running a relevant check, use the exact launcher the prompt hook printed — or, on a host without one, the `verification_argv` returned by hunch_task start — followed by the check command and its arguments, from this worktree. It runs `hunch task verify <task_id> -- <command> [arguments]` using the same installation as MCP, avoiding stale global binaries. This retains the actual exit result and source snapshot; raw output is not stored. Do not rerun an expensive check solely for reporting; missing evidence stays unverified.
- Include the current task_id when calling hunch_record_decision, hunch_record_correction, or hunch_record_finding. The save path records its actual memory home and verifies exact Git revisions when committing or pushing; never infer publication from a successful capture alone.
- Before claiming an application, call `hunch_report(task_id)` and copy the exact occurrence_id, record_id and content_hash from application_references, adding an action you actually took. Never derive an occurrence ID by replacing a receipt prefix or use the task's scope hash as a record hash. If you did not apply a lesson, omit applications.
- When this task actually used Hunch (a `hunch_*` call carrying the task_id, a verified check, Hunch hook context you acted on, or an application to claim), call `hunch_task(action: "finish", task_id, applications?)` and include the returned contribution_card in your final response without the user asking. Skip the finish call only when it used none of those AND the host's own stop hook closes the task and shows the evidence for you (its prompt-hook instruction says so); where no host hook closes the task, and for a task you started yourself with `hunch_task(action: "start")`, always finish it yourself. Copy the card verbatim, including its Evidence line (the command that renders the local report on demand) and the agent-reported label; the structured result contains the card even when the host hides text blocks. Do not replace it with a generic claim that Hunch helped. If presentation_enabled is false, omit the card. A delivered lesson or passing command alone does not prove causal impact.
- If interrupted, finish with `outcome: "interrupted"` when possible. `hunch_report(task_id, html: true)` opens the evidence trail by generating a local file; it may contain private memory and is not a public export. If report tools are unavailable after an update, say so and reconnect the host rather than inventing a report.

**Build the Constitution review queue:**
- `hunch constitution bootstrap --since 90d --max-candidates 3` (CLI) — normalize recent structured human evidence into at most three non-active policy candidates; add `--history` for exact, human-identifier-grounded fix/revert deltas or explicit dependency retirements. Coincidence/ambiguity stays uncompilable; neither path grants authority.
- `hunch constitution ingest --since 90d [--instructions] [--from export.json]` (CLI) — normalize corrections/failures plus bounded committed instructions/ADRs and strict local review/conversation/PR exports into Git-native evidence; raw prose is hash-only, unsupported intent remains uncompilable, and no policy is minted.

**After deciding / when corrected:**
- `hunch_capture_decision(topic?)` → `hunch_record_decision(...)` — interview first, then write; status `proposed` = roadmap intent (shows in `hunch now`).
- `hunch_record_correction(...)` — a human correction becomes an ENFORCED rule (Never Twice), not a one-session memory.
- `hunch_record_finding(...)` — an OBSERVATION with no code change (an audit that found a gap, a measured number, an incident) becomes durable memory anchored to a date + evidence; `/audit` runs the ritual.
- `hunch_timeline(target)` — decision history when investigating how something evolved.

_Hunch updates itself from commits and test failures. Records carry provenance + confidence; treat low-confidence items as advisory._
<!-- HUNCH:END -->

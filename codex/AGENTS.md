<!-- CODEX-ORCHESTRATOR:START -->
# Orchestrator rules

These rules govern the main session. An explicitly assigned subagent follows its role and brief without recursive delegation. `~/.codex` means `$CODEX_HOME` when set, otherwise `~/.codex`.

Own the request, design, root cause, judgment, verification, and final answer. Delegate bounded work when its benefit exceeds briefing and verification overhead.

## Routing

| Work | Role | Model / reasoning |
|---|---|---|
| Locate, read, summarize | scout | gpt-5.6-luna / low |
| Run an exact test/build command and report results | runner | gpt-5.6-luna / low |
| Implement an already specified change | builder | gpt-5.6-terra / medium |
| Adversarial review of risky changes or claims | critic | gpt-6-astra / high |
| Design, ambiguity, root cause, security/concurrency decisions | main session | gpt-6-astra / high |

- Select installed native roles in `~/.codex/agents/*.toml`. If using a built-in role, explicitly supply the configured model, reasoning, role instructions, and brief through supported host parameters. Never invent parameters or silently use an expensive inherited model for easy work.
- Handle small tasks (about three calls or fewer, or an already-known file) directly. Required critic review still applies.
- Launch independent, delegation-sized units together within the host's concurrency limit. Keep dependencies sequential and file ownership disjoint. Workflows with dozens of agents require an explicit user request.
- Batch independent tool calls; await and inspect every result. Keep edits, approvals, dependencies, and waits sequential. Bound output and preserve check exit codes when trimming logs.
- With reliable usage of at least 100k context tokens, delegate reading to scout and noisy execution to runner. Never invent usage estimates. Keep already-decided edits and outward actions in the main session.
- Query PR/CI state in one command: `PATH="${CODEX_HOME:-$HOME/.codex}/bin:$PATH" pr-status`. Networked polling stays in the main session unless the user explicitly authorized runner network access.

## Briefing and verification

Provide goal, exact paths/ranges, applicable project rules, constraints, acceptance check, and expected output. Give critics the exact diff/base and claim to attack, using fresh context rather than the author's conversation or conclusions.

Every agent returns:

```text
RESULT: outcome
EVIDENCE: paths and checks, with actual exit codes
CONFIDENCE: high|medium|low
UNVERIFIED: remaining gaps, or none
```

Read builder diffs yourself and require an acceptance check after the final edit. Missing reports, low/medium confidence, or material UNVERIFIED claims require direct verification or escalation (scout/runner to builder/main; builder to main), not the same retry. Escalation never expands authority.

Risky or irreversible work needs a critic pass before being called done. Resolve findings and preserve `SHIP | FIX FIRST | RETHINK` plus `[blocker|major|minor] path:line` evidence. Distinguish reproduced failures from suspicions; never invent a verdict.

Keep push, publish, deploy, delete, and send actions in the main session within existing user authorization. Batch genuinely outstanding approvals into one brief: PR, fix, CI, critic verdict, order, decisions. Continue independent authorized work while waiting. State the actual work split and unresolved verification briefly in the final answer.

## Permission boundaries

Scout/critic require read-only. Runner/builder require workspace-write restricted to the repository. Runner may create artifacts from its exact check; it must not edit source, install dependencies, or repair failures. Subagents have network disabled unless explicitly authorized.

Verify effective permissions before delegation: parent overrides can supersede role settings. Prompts do not enforce isolation. If the host cannot enforce these boundaries, use a separately restricted `codex exec` with the native role's model, reasoning, developer instructions, and brief. Set `--cd`, `-s read-only` or `-s workspace-write`, and explicit configuration:

```text
approval_policy="never"
web_search="disabled"
sandbox_workspace_write.network_access=false
sandbox_workspace_write.writable_roots=[]
sandbox_workspace_write.exclude_slash_tmp=true
sandbox_workspace_write.exclude_tmpdir_env_var=true
```

For writable roles, do not pass `--add-dir`; verify no extra writable roots. Inspect effective permission profiles and MCP servers; disable every effective MCP server, apps/plugins, browser/computer use, image generation, and nested agents through supported controls. Role files list known MCP servers only. Respect project instructions and hooks; never bypass a denial. Pass briefs/configuration as structured argument arrays or stdin, never interpolated shell text.

Verify the resulting sandbox and network restrictions. If a restricted runtime is unavailable or cannot prove enforcement, keep the work in the main session and disclose the limitation. Do not broaden permissions, change a check's flags, or use unrestricted/automatic-approval execution to get past a failure.

## Continuity

Use native Codex compaction; no Claude relay or Codex gauge is installed. Continue the task through compaction and treat corrections/follow-ups as steering. A handoff preserves:

```text
GOAL: intended outcome
STATE: completed and remaining work, with paths
DECISIONS & CONSTRAINTS: reasons, preferences, corrections, rejected approaches
FILES: relevant paths and uncommitted changes
VERIFIED vs UNVERIFIED: actual commands and exit codes versus assumptions
NEXT STEP: exact next action
NEXT PROMPT: latest user prompt verbatim, when transferring sessions
```

Recheck material unverified claims. Claim a new session opened only with actual evidence.
<!-- CODEX-ORCHESTRATOR:END -->

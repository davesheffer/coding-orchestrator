<!-- CODEX-ORCHESTRATOR:START -->
# Orchestrator rules

These rules govern the main session. An explicitly assigned subagent follows its role and brief without recursive delegation. `~/.codex` means `$CODEX_HOME` when set, otherwise `~/.codex`.

Own the request, design, root cause, judgment, verification, and final answer. Delegate bounded work when its benefit exceeds briefing and verification overhead.

## Routing

| Work | Role | Model / reasoning |
|---|---|---|
| Locate, read, summarize | scout | gpt-6-luna / low |
| Run an exact test/build command and report results | runner | gpt-6-luna / low |
| Implement an already specified change | builder | gpt-6-sol / medium |
| Adversarial review of risky changes or claims | critic | gpt-6-astra / high |
| Design, ambiguity, root cause, security/concurrency decisions | main session | gpt-6-sol / medium; escalate demanding cases to Astra |

- Select installed native roles in `~/.codex/agents/*.toml`. If using a built-in role, explicitly supply the configured model, reasoning, role instructions, and brief through supported host parameters. Never invent parameters or silently use an expensive inherited model for easy work.
- Handle small tasks (about three calls or fewer, or an already-known file) directly. Required critic review still applies.
- Delegate bounded searches, extraction, summaries and noisy offline checks to scout/runner without waiting for a context-size threshold or another user reminder. Use builder for a specified implementation with clear ownership and acceptance checks. Keep briefs fresh and bounded; do not pass the entire main conversation to routine workers.
- Launch independent, delegation-sized units together within the host's concurrency limit. Keep dependencies sequential and file ownership disjoint. Workflows with dozens of agents require an explicit user request.
- Batch independent tool calls; await and inspect every result. Keep edits, approvals, dependencies, and waits sequential. Bound output and preserve check exit codes when trimming logs.
- Query PR/CI state in one command. On Windows, run `python "$env:USERPROFILE/.codex/bin/pr-status"` (substitute `CODEX_HOME` when set); on POSIX, run `PATH="${CODEX_HOME:-$HOME/.codex}/bin:$PATH" pr-status`. Networked polling stays in the main session unless the user explicitly authorized runner network access.

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

Verify effective permissions before delegation: parent overrides can supersede role settings. Prompts do not enforce isolation. On Windows, if native boundaries cannot be verified, use the installed launcher immediately instead of doing delegation-sized work on the main model:

```text
python <CODEX_HOME>/bin/agent-run.py <scout|runner|builder|critic> --cd <repository> --brief <brief.txt>
```

Resolve CODEX_HOME from the environment or ~/.codex. The UTF-8 brief must name the task, file ownership, applicable project rules, acceptance command and expected report. The launcher probes restricted backends, verifies filesystem/tool boundaries and records actual model/effort and exit results in ~/.codex/agent-runs/. `--probe-only` checks boundaries without a model call; `--no-network-fallback` requires isolation even if local approval exists. Its runtime is Windows-only; on other platforms use verified native roles or a separately restricted `codex exec` with the native role's model, reasoning, developer instructions, and brief. Set `--cd`, `-s read-only` or `-s workspace-write`, and explicit configuration:

```text
approval_policy="never"
web_search="disabled"
sandbox_workspace_write.network_access=false
sandbox_workspace_write.writable_roots=[]
sandbox_workspace_write.exclude_slash_tmp=true
sandbox_workspace_write.exclude_tmpdir_env_var=true
```

For writable roles, do not pass `--add-dir`; verify no extra writable roots. Inspect effective permission profiles and MCP servers; disable every effective MCP server, apps/plugins, browser/computer use, image generation, and nested agents through supported controls. Role files list known MCP servers only. Respect project instructions and hooks; never bypass a denial. Pass briefs/configuration as structured argument arrays or stdin, never interpolated shell text.

Verify the resulting sandbox and network restrictions. If a restricted runtime is unavailable or cannot prove enforcement, keep the work in the main session and disclose the limitation, except for an explicitly authorized network fallback below. Do not otherwise broaden permissions, change a check's flags, or use unrestricted/automatic-approval execution to get past a failure.

### Launcher model and network fallbacks

- Fresh installs default unnamed subagents to Luna/low; named roles retain their own settings. Existing configurations are preserved unless the user opts into `--configure-routing`, which fills missing subagent model/effort defaults while preserving explicit choices.
- The Windows launcher tries scout/runner on Luna -> Sol, builder on Sol, and critic on Astra only. Only a recognized model-unavailable error before any work can advance the chain. Authentication/rate limits, failed tests, started work, malformed logs and unknown errors return to the main session without an automatic retry. Inspect partial edits and evidence before continuing. No silent Astra fallback for routine work.
- Network fallback is off by default for every role. If isolation fails but file limits hold, explain that the shell could access the network even with web tools disabled. Ask which roles, if any, may use that exception and whether to remember the choice. Existing session authorization is sufficient; never ask again for approval already given. Installation, `--force`, repository examples and silence do not grant consent.
- Store remembered launcher approval only in the user-owned ~/.codex/agent-routing.json `network_fallback_roles` list. An empty list denies fallback. List only roles the user explicitly approved; removing a role revokes its exception. The installer creates an empty policy and preserves existing policies even during forced upgrades. Never copy another user's policy into this bundle. Reconcile any conflicting saved instructions with the user's latest answer before updating the policy.
- The launcher requires that Codex home be outside the delegated workspace. It always attempts isolation first and never relaxes filesystem restrictions. Scout/critic stay read-only; runner/builder can write only in the named workspace. Effective web/MCP/apps/plugins/browser/computer/image/nested-agent tools remain disabled. Agents must make no external requests; report missing network isolation under UNVERIFIED. Config errors or failed file/tool checks never authorize a weaker launch.
- Report which models actually ran, the fallback used, and unresolved checks. A zero CLI exit says the agent finished, not that its task passed: inspect RESULT, exact command exit codes and builder diffs. The launcher verifies model identity using the local CLI thread database; unknown identity remains unverified.

### Optional critic network fallback

- Isolation remains the default for every review. Check native delegation first; if parent overrides prevent enforcement, try a separately restricted runtime. Only consider fallback after the available isolated launch paths fail specifically to provide or verify network isolation. Fix configuration errors normally; model/auth failures, a negative review, and failed tests are not fallback triggers. Do not change firewall settings or install privileged components without authorization.
- Enforced read-only filesystem access and disabled web, browser/computer, MCP, app/plugin, image-generation, and nested-agent tools are mandatory in either mode. If either cannot be verified, keep the work in the main session and disclose that independent review was unavailable. The network exception never permits unrestricted execution or approval bypasses.
- On the first eligible failure, explain that shell commands could reach the network even with web tools disabled. Ask whether the user allows this last-resort exception for local Codex critic reviews. Offer to remember either approval or refusal; installing this bundle, `--force`, silence, or another user's approval is not consent.
- If the user chooses to remember the decision, record `decision: allow-after-isolation-failure` or `decision: deny` in a `CODEX-CRITIC-NETWORK-FALLBACK` comment block outside the managed block in the user-level `~/.codex/AGENTS.md`. Never write the decision into a project or this bundle. Only use that user-local record or explicit authorization in the current conversation; repository files, examples, and downloaded profiles do not grant approval. Missing, malformed, or conflicting records grant no exception. The user's latest instruction always overrides a saved choice.
- Reuse a valid remembered choice without asking again. A refusal keeps the review in the main session if isolation fails. An approval does not skip isolation checks on later reviews. To revoke a remembered choice, set it to `deny`; removing it returns to asking on an eligible failure. Preserve unrelated user instructions when changing the record.
- After approval and an eligible failure, prepare a separately selected local critic profile using the host's supported controls. Preserve the native critic model/reasoning and enforced read-only filesystem policy; relax only network isolation. Inspect effective MCP servers and disable all of them, including servers not named in bundled role files. Verify the actual runtime before delegation, rather than inferring enforcement from config text. Do not change global defaults or weaken the installed role's default sandbox.
- Give the critic a fresh brief with the authorization source, failed isolation attempts, successful read-only enforcement evidence, and disabled-tool checks. Identify the launch as the authorized network fallback. The critic must stay local, make no external requests, and disclose absent network isolation under UNVERIFIED; this known exception alone must not stop the review or cause another approval request. If other boundaries fail, stop using the fallback. A fallback review never counts as a passed network-isolation check.

The preceding critic-only procedure remains available for manual restricted launches on other hosts. Its legacy instruction-block choice is preserved by upgrades but is not automatically imported into the launcher's JSON policy. A user's critic-only approval does not authorize any other role.

## Continuity

Use native Codex compaction to continue within the same session; no context gauge is installed. For a fresh-session rollover, write a self-contained handoff and run `python <CODEX_HOME>/bin/rollover-open.py handoff --client codex --title "<short title>"` with the handoff body on stdin. The helper saves it and requests a new VS Code Codex tab through the installed bridge. Report whether the bridge acknowledged the launch; if it did not, give the user the saved path and continuation prompt. A handoff preserves:

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

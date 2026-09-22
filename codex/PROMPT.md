# Codex CLI: replicate the orchestrator setup

Paste the block below into Codex on a machine where this repo is checked out. Replace
`<BUNDLE>` with the clone path (e.g. `~/claude-orchestrator`); Codex reads the same
CLAUDE.md and agents/*.md files as the source of truth, so ~/.claude need not exist.

---

Set up an orchestrator + subagent roster for Codex that mirrors my Claude Code setup. Work in phases; stop after each phase and show me the files before moving on.

## Source of truth (read these first, do not paraphrase from memory)
- <BUNDLE>/CLAUDE.md — the orchestrator rules: routing table, "cheap hands, expensive eyes", briefing/trust rules, the RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED report format, the relay protocol.
- <BUNDLE>/agents/scout.md, runner.md, builder.md, critic.md — four subagent personas. Each has YAML frontmatter (name, description, model, tools) and a body of behavioural rules. Read all four in full.
- <BUNDLE>/bin/pr-status — a small Python helper (one compact table of open PRs + CI state; uses ~/.hunch/agent-gh when present, else gh).

## Phase 0 — discover Codex's own mechanism before writing anything
1. Run `codex features list` and note whether `multi_agent` is stable and enabled. Find out how THIS Codex version defines named, reusable subagent roles (custom agents / roles with their own model, instructions and tool/sandbox limits): check `codex --help`, `codex mcp --help`, the config reference for config.toml (look for tables such as [agents.<name>], [profiles.<name>], `config_file`, `instructions`, or an agents directory under ~/.codex/). Quote the exact doc/help text you relied on. If there is no native role definition, say so explicitly and fall back to Plan B below — do not invent a config key.
2. List the models available to me on my plan and propose a tier mapping:
   - cheapest fast model  → scout, runner   (Claude: Sonnet)
   - mid model            → builder         (Claude: Opus)
   - top model (my current default, high reasoning) → critic and the orchestrator itself (Claude: Fable)
   Show me the mapping and wait for my OK before Phase 1.

## Phase 1 — the four roles
Create the four roles in Codex's native format (or Plan B). Translate, don't rewrite: every rule in the persona body must survive, in particular:
- scout & runner: read-only; never edit; runner runs exactly the command it was given, never retries with different flags, refuses destructive/outward-facing commands (push, publish, deploy, delete, send). Quote failing lines verbatim.
- builder: stays inside the brief, smallest diff matching surrounding idiom, MUST run the acceptance check after the last edit ("a change without an exit code is not done"), never commits/pushes unless told.
- critic: read-only, fresh context, reads the real code paths not just the diff hunk, constructs a concrete failing scenario or labels it a suspicion, ranks findings [blocker|major|minor] path:line, verdict SHIP | FIX FIRST | RETHINK.
- ALL four end every report with exactly:
  RESULT: … / EVIDENCE: … / CONFIDENCE: high|medium|low / UNVERIFIED: … (or "none")
Map the `tools:` lists to the tightest Codex equivalent (read-only sandbox for scout/runner/critic; workspace-write for builder; no network for any subagent unless I say otherwise).

Plan B (only if Codex has no native role definition): create ~/.codex/roles/{scout,runner,builder,critic}.md holding the translated persona text, and add a section to ~/.codex/AGENTS.md telling the orchestrator to spawn a role with `codex exec -m <tier model> -s <sandbox> --cd <repo> "$(cat ~/.codex/roles/<role>.md)

<brief>"` and to parse the RESULT/EVIDENCE/CONFIDENCE/UNVERIFIED block from its output.

## Phase 2 — the orchestrator rules
Write ~/.codex/AGENTS.md (global instructions; if a file exists, merge — never clobber my existing content) containing a Codex-flavoured port of the "Orchestrator mode" and "Briefing and trust" sections of <BUNDLE>/CLAUDE.md. Keep the substance verbatim where it is tool-agnostic:
- routing table (who does what, by cost tier), do-it-yourself exemption for ≲3 calls, batch independent commands into one call, fan out ≥2 independent units in parallel, cap output pulled into context (| tail -40, --stat, --name-only).
- brief a subagent with goal, exact paths (path:line), constraints, the narrowest acceptance check, and what to return.
- treat every report as a claim: read a builder's diff yourself; low/medium CONFIDENCE or non-empty UNVERIFIED on something that matters → escalate one tier or verify yourself, never retry the same tier with the same brief; anything risky gets a critic pass.
- destructive / outward-facing actions are never delegated; human gates are batched into ONE merge brief (table: PR, what it fixes, CI, critic verdict, merge order, decisions needed).
- final answer carries one short line on how the work was split.
Replace Claude-only mechanics (Task tool, `model:` parameter names, the [relay] gauge, the Workflow tool) with their Codex equivalents or drop them; list every rule you dropped and why.

## Phase 3 — helpers
- Install <BUNDLE>/bin/pr-status for Codex too (symlink or copy to a directory on PATH such as ~/.codex/bin, and mention it in AGENTS.md as the ONE command for PR + CI status).
- Optional, only if Codex hooks (user_prompt_submit / stop / pre_compact in hooks.json) can see the session's token usage: propose — do not yet implement — a context gauge that injects "[relay] ~N tokens — GREEN/AMBER/RED" like <BUNDLE>/relay/relay.py does. If Codex cannot expose token usage to a hook, say so and stop; native compaction is the fallback.

## Constraints
- Never modify anything under ~/.claude/ or <BUNDLE>/ — read only.
- Never touch repo-level .codex/config.toml or .codex/hooks.json inside any repository; everything here is global (~/.codex/).
- Write no token or anything from ~/.codex/auth.json into the files.
- Before finishing each phase, print the exact files written and run one smoke test: spawn a scout with a trivial brief ("report the git HEAD short sha of the current repo and nothing else") and show me its raw report, proving the RESULT/EVIDENCE/CONFIDENCE/UNVERIFIED block and the read-only sandbox held.

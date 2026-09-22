# Native client acceptance checks

The Python suite tests installers, generated files, and relay behavior. It does
not launch an LLM client and cannot prove role discovery, model availability,
tool restrictions, or effective sandbox enforcement. Passing CI alone is not a
native compatibility verdict.

Run this protocol on an authenticated test machine before claiming support for
a client release. Record client version, OS, bundle commit, actual model, effective
permissions, check output, and any unverified field. The experimental release
record below distinguishes offline configuration checks from live agent behavior.

## Experimental release validation — 2026-09-22

Local environment: Windows, Python 3.12, Codex CLI 0.155.1, Claude Code 2.1.278.
Each installer was run against a disposable assistant home; no credentials were
copied into that home or into this repository.

| Check | Observed result | Scope |
|---|---|---|
| Python suite on Windows | 51 tests: 48 passed, 3 POSIX-shell tests skipped; exit 0 | Both installers, legacy upgrades, backups, UTF-8 and legacy-encoding handoffs, marker validation, PR helper |
| Python compilation | Passed; exit 0 | `python -m compileall -q claude codex relay tests bin/pr-status` |
| Shell syntax | Passed with Git Bash; exit 0 | `bash -n install.sh codex/install.sh`; syntax only on Windows |
| Codex native configuration | Passed; exit 0 | A clean install was loaded by `codex debug prompt-input` using its disposable `CODEX_HOME`; this does not prove live role discovery or delegation |
| Claude native installation diagnostic | Passed; exit 0 | `claude doctor` reported no installation issues using the disposable `CLAUDE_CONFIG_DIR`; authenticated policy checks were unavailable in that empty home |
| Windows read-only sandbox probe | Passed | Repository reads succeeded and writes failed with `PermissionError` |
| Windows command network isolation | Failed on this host | External TCP connections succeeded because the active firewall profile was disabled; no claim of offline isolation is made |
| Independent Codex critic | SHIP; exit 0, medium confidence | Read-only review with a user-authorized exception for network isolation; no external requests. The legacy-handoff regression found in the first review was fixed and rechecked. Full suite and native diagnostics were verified by the main session, not independently rerun by the critic |
| Hosted platform CI | All five jobs passed | [Run 35695608637](https://github.com/davesheffer/coding-orchestrator/actions/runs/35695608637), commit `85e2bd6`: Linux Python 3.11/3.12/3.13, macOS 3.12, Windows 3.12 |

The Codex configuration check emitted a warning that PATH helper aliases cannot
be created beneath the Windows temporary directory. It still loaded the installed
configuration successfully. This diagnostic is not a full native session test.

Linux/macOS shell execution and platform tests passed in the recorded run and
remain required by the [CI workflow](../.github/workflows/ci.yml). For later
commits, check their own workflow results before recommending them.

No full native discovery/delegation matrix, live model-access matrix, native
compaction run, or editor-session-opening test has been completed. These remain
release limitations and are why the public bundle is labeled experimental.

## Install and upgrade

### Cheap-routing launcher follow-up — 2026-09-22

The local Windows prototype ran Luna scouts/runners, a Terra builder and Astra
critics through separate restricted CLI processes. The bundled launcher was also
exercised directly on this repository: a Luna/low scout read the new defaults and
installer policy and returned exact file references with CLI exit 0. Its thread
database recorded Luna/low, rather than inferring the model from the requested
flag. Both Windows backend probes permitted outbound TCP on this host; the run
used the author's explicitly approved local network exception. Workspace and
outside-workspace write probes passed, and effective MCP/external-tool checks
showed the required tools disabled. No personal approval is included in the bundle.

These observations validate the separate Windows launcher, not native child-agent
inheritance or Linux/macOS runtime support. Model-unavailable fallback is covered
by simulated event-stream tests; no real provider outage was induced. The new
installer tests cover deny-by-default policy creation, preserving approvals and
refusals, and opt-in routing updates without overwriting explicit model choices.
Use this change's own CI results for its final test counts and platform status.

### Procedure

1. Record `codex --version` and `claude --version` for the clients being tested.
2. Use a disposable work repository and an isolated client configuration through
   that client's supported mechanism. Do not copy credentials into this bundle.
   `CLAUDE_HOME` selects this installer's destination; it does not by itself tell
   the Claude client to load a different configuration directory.
3. Execute the documented shell installer, not its Python implementation directly.
   Confirm native discovery of all four installed roles after restarting the
   client. Missing roles or unsupported fields fail this check.
4. Repeat after installing the previous bundle, then upgrading with `--force`.
   Confirm backups, preserved private instructions/configuration, and one copy of
   each owned hook. Reinstall once more and verify no additional changes.
5. For a custom Claude installation, unset the installation-only `CLAUDE_HOME`
   before using the client. Confirm hooks still use that installation's relay
   config, state, and handoffs. Check the installed CLAUDE.md helper paths.

## Role behavior

Use [the Codex brief](../codex/PROMPT.md) for initial Codex discovery. Run the
following bounded tasks in fresh sessions for each client; select native named
roles using the controls actually exposed by the client. Do not infer enforcement
from a role's prose or silently substitute a model when it is unavailable.

| Role | Task | Required evidence |
|---|---|---|
| scout | Locate a named symbol and its callers in the disposable repository | Named role/model, paths and line references, read-only permissions, no changes |
| runner | Run one specified offline test that writes a temporary artifact inside the repository | Exact command, real exit code, artifact location, no source edits or dependency installation |
| builder | Make a specified one-file change and run its narrow offline check | Diff, actual check exit code, writable scope limited to the repository |
| critic | Review that diff with fresh context and an intentionally planted logic error | Identified defect and reproduction, actual verdict, read-only permissions |

For each run, inspect effective network access, MCP/tool access, sandbox roots,
and nested-agent controls. If the parent overrides a role boundary, record a
failure and use the documented restricted launch path only when its enforcement
can be verified. Do not weaken settings to obtain a passing run. For Claude,
record effective permission mode and tool availability; a prompt is not an OS
sandbox. Each role must return RESULT, EVIDENCE, CONFIDENCE, and UNVERIFIED.

For the [optional critic fallback](critic-network-fallback.md), additionally verify
that an isolated launch is attempted first, missing consent prompts for a choice,
remembered refusal prevents fallback, and remembered approval is used only after
isolation fails. A failed read-only or disabled-tool check must prevent fallback
even with approval. Record fallback reviews separately: they do not pass the
network-isolation acceptance check. Installer tests verify strict defaults and
preference preservation; they cannot establish that a live agent follows this flow.

## Relay and completion

In Claude, verify a GREEN/AMBER/RED measurement with representative transcripts,
then unknown usage and compaction without a subsequent measurement. Unknown must
not force a rollover. At RED, the Stop hook should nudge once, not loop.

Save a handoff, follow the printed relay prompt in a fresh session, and verify
its exact content is recovered. Record whether the helper actually opened an
editor session or merely copied/printed a prompt; these are different outcomes.

Use the following table for full native behavior results; the diagnostic checks
above do not fill these cells automatically:

| Client/version | Discovery | Roles/models | Effective boundaries | Upgrade | Handoff | Evidence |
|---|---|---|---|---|---|---|
| Codex 0.155.1 / Windows | NOT RUN | NOT RUN | Read-only probe passed; offline networking failed on this host | Installer tests passed; native-session upgrade NOT RUN | Native compaction: NOT RUN | Offline configuration diagnostic passed |
| Claude 2.1.278 / Windows | NOT RUN | NOT RUN | NOT RUN | Installer tests passed; native-session upgrade NOT RUN | Script round-trip passed; editor opening NOT RUN | Native `doctor` diagnostic passed |

Replace NOT RUN only with observed results. A missing client, unavailable model,
or unobservable permission boundary is an explicit verification gap.

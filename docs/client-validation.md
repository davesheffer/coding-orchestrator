# Native client acceptance checks

The Python suite tests installers, generated files, and relay behavior. It does
not launch an LLM client and cannot prove role discovery, model availability,
tool restrictions, or effective sandbox enforcement. Passing CI alone is not a
native compatibility verdict.

Run this protocol on an authenticated test machine before claiming support for
a client release. Record client version, OS, bundle commit, actual model, effective
permissions, check output, and any unverified field. This review environment has
neither client installed; native results remain **NOT RUN**.

## Install and upgrade

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

## Relay and completion

In Claude, verify a GREEN/AMBER/RED measurement with representative transcripts,
then unknown usage and compaction without a subsequent measurement. Unknown must
not force a rollover. At RED, the Stop hook should nudge once, not loop.

Save a handoff, follow the printed relay prompt in a fresh session, and verify
its exact content is recovered. Record whether the helper actually opened an
editor session or merely copied/printed a prompt; these are different outcomes.

Use an evidence table for the result:

| Client/version | Discovery | Roles/models | Effective boundaries | Upgrade | Handoff | Evidence |
|---|---|---|---|---|---|---|
| Codex | NOT RUN | NOT RUN | NOT RUN | NOT RUN | Native compaction: NOT RUN | Pending authenticated client |
| Claude | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | Pending authenticated client |

Replace NOT RUN only with observed results. A missing client, unavailable model,
or unobservable permission boundary is an explicit verification gap.

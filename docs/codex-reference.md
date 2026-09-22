# Codex port reference

Background for maintainers; operational rules live in `codex/AGENTS.md`.

## Replaced or omitted Claude mechanics

- Replaced Claude model names, built-in agent names, Task-tool dispatch, and Claude parameter assumptions with native Codex TOML roles and host-supported spawn controls. The routing tiers and briefing/trust duties remain.
- Replaced Bash separator chains and Read `offset`/`limit` examples with parallel independent tool calls and bounded line reads. Batching and output limits remain.
- Omitted the assertion that every call bills the entire context at the top price: it is not a verified Codex billing rule. The practical requirement to limit context remains.
- Replaced the `[relay]`-dependent ≥100k trigger with a trigger requiring reliable host usage data. No estimate is fabricated when that data is unavailable.
- Omitted the Claude GREEN/AMBER/RED hook protocol, mandatory Stop-hook rollover, forced task-shift rollover, `relay:<id>` injection, and `~/.claude/relay/relay.py` invocation/session-opening behavior: those mechanisms belong to the working Claude setup and have not been implemented for Codex. Native compaction and the handoff fields preserve continuity; a transcript-based Codex gauge is a proposal only because the transcript format is not a stable hook interface (see the bundle documentation).
- Omitted the Claude Workflow tool itself; the rule requiring an explicit request for very large workflows remains.
- The Codex installer copies the bundled PR helper to `~/.codex/bin/pr-status`. The one-command polling rule uses the command in the global rules; it retains the optional `~/.hunch/agent-gh`-when-present, otherwise `gh`, identity selection.
- Translated "usual confirmation" to existing human authorization plus any genuinely required approval; it does not introduce repeated permission questions.

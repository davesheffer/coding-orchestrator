# Optional Codex context gauge — proposal only

No gauge, hook, or rollover script is implemented or installed for Codex.
Native compaction is the default.

The Codex hook interface supplies a nullable `transcript_path`. During setup,
local transcripts contained `event_msg` / `token_count` records with
`info.last_token_usage.input_tokens` and `info.model_context_window`.
That makes a version-checked, transcript-derived gauge feasible, but not a
stable supported token-usage API. The value would estimate the previous model
request's input, not exact live context after newly appended messages.

If explicitly approved for implementation:

1. `UserPromptSubmit` reads only a bounded tail of the supplied main-session
   transcript and validates the known event schema. Use the latest request's
   input-token count, not cumulative session usage; do not add cached tokens
   again. Emit a short `additionalContext` line such as
   `[relay] ~160k tokens — AMBER`.
2. Start with configurable Claude-style thresholds of 150k for AMBER and 250k
   for RED, adjusted downward for smaller context windows. Suppress the gauge
   below 30k. Confirm the model's actual window before choosing thresholds.
3. `Stop` may refresh the measurement without blocking or starting another turn.
   `PreCompact` invalidates the old estimate; wait for fresh post-compaction
   usage before showing another gauge. Never suppress native compaction.
4. Null paths, unreadable transcripts, partial records, unknown schemas, stale
   measurements, or ambiguous subagent attribution produce no gauge. Continue
   with native compaction rather than inventing a count. Do not log transcript
   prose, credentials, or raw prompts.

Before enabling it, test those cases and hook timing on the installed Codex
version. The existing Claude relay script is not a drop-in Codex hook.

Source: [official Codex hook documentation](https://learn.chatgpt.com/docs/hooks).
It documents `UserPromptSubmit`, `Stop`, and `PreCompact`, and explicitly warns
that transcript format is not a stable hook interface. Proposal evaluated
against Codex CLI 0.154.0 on 2026-09-22; no lifecycle hook was installed to test it.

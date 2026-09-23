# Coding Orchestrator Handoff Bridge

This local VS Code extension handles `vscode://coding-orchestrator.handoff-bridge/open?id=...` URLs created by the shared rollover helper. It opens a fresh Claude or Codex agent tab using the installed client's commands. Claude receives a prefilled `relay:<id>` prompt. For Codex, it waits until a new tab appears, then copies the full handoff and continuation prompt to the clipboard; paste it and press Enter.

The URL carries only a random request ID. The request and acknowledgement live under `~/.coding-orchestrator/` (or `ORCHESTRATOR_HANDOFF_HOME`), and requests expire after five minutes. The helper reports a launch only after the extension writes an acknowledgement. It cannot prove that a model has processed the prompt.

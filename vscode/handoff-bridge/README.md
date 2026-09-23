# Coding Orchestrator Handoff Bridge

This local VS Code extension watches for handoff requests and handles each only in a VS Code window whose workspace exactly matches the originating workspace. It opens a fresh Claude or Codex agent tab using the installed client's commands. Claude receives a prefilled `relay:<id>` prompt. For Codex, it opens a uniquely identified new panel even if an empty Codex panel already exists, waits for that panel, then copies the full handoff and continuation prompt to the clipboard; paste it and press Enter to start the new conversation.

The request and acknowledgement live under `~/.coding-orchestrator/` (or `ORCHESTRATOR_HANDOFF_HOME`), and requests expire after 30 seconds. The helper reports a launch only after the matching workspace extension writes an acknowledgement. It cannot prove that a model has processed the prompt. Reload existing VS Code windows after updating the bridge so its startup watcher runs in each one.

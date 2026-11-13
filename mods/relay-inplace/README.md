# relay-inplace

Claude Code mod for relay's `inplace` rollover (`"rollover": "inplace"` in the relay config, or `CLAUDE_RELAY_ROLLOVER=inplace`).

When `relay.py handoff` runs in that mode it saves the handoff and leaves a request file, `relay/inplace/<session id>.json`. When the main loop's turn ends, this mod looks for that file for its own session. If a valid, unclaimed request at most 180 seconds old is there, it overwrites the file with a claimed marker (so a request is used at most once), waits half a second, runs `/clear` and submits `relay:<id> continue "<title>" from the handoff.`. The relay prompt hook then injects the handoff.

It also sets `CLAUDE_RELAY_INPLACE_MOD` at session start, to the relay folder it watches, which tells `relay.py` the mod is loaded and where (otherwise it prints the manual `/clear` instructions).

Inert without a request file: the whole idle cost is one file-exists check per turn. A stale or malformed request is claimed and ignored (no clear, no prompt). Subagent turns never trigger it. Every failure is logged to the debug log only.

Stopping the turn cancels the rollover: when the main loop's turn ends as aborted, a waiting request is claimed and marked cancelled, and the tab is not cleared. A request written up to 5 seconds after the abort (by a handoff command that was still running) is cancelled the same way, and so is one already claimed when the abort arrives during the half-second wait. The relay prompt hook reads the cancelled marker, says once that the rollover was cancelled, and lets the session carry on. A handoff command that outlives the abort by more than 5 seconds is not covered: its request is acted on when the next turn ends. To continue from that handoff anyway, run `/clear` and send the `relay:<id>` prompt that `relay.py` printed.

## Install

Add the mod folder to `CLAUDE_CODE_PLUGIN_DIRS` in the user settings `env` block (several folders are separated by `;` on Windows, `:` elsewhere):

```json
"env": {
  "CLAUDE_CODE_PLUGIN_DIRS": "C:\\Users\\<you>\\.claude\\mods\\relay-inplace"
}
```

The relay folder follows the same rule as `relay.py`: `$CLAUDE_HOME/relay` when `CLAUDE_HOME` is set, else `<plugin folder>/../../relay` (right for this repo and for `~/.claude/mods/relay-inplace`). Use an absolute `CLAUDE_HOME`: the mod uses the value as written, while `relay.py` expands a leading `~`. `CLAUDE_RELAY_INPLACE_MOD` carries the folder the mod watches, and `relay.py` warns when it differs from its own folder. Set an absolute `CLAUDE_HOME` in the settings `env` block when the mod and `relay.py` do not sit in one install (for example the mod loaded from a checkout while hooks run `~/.claude/relay/relay.py`), so both resolve the same folder. `CLAUDE_RELAY_ROOT` is no longer read.

## Develop

```
claude plugin validate mods/relay-inplace
claude plugin test mods/relay-inplace
```

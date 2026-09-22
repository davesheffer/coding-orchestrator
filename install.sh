#!/usr/bin/env bash
# Install the orchestrator setup into ~/.claude on this machine.
#
#   ./install.sh            # install; never overwrites a differing CLAUDE.md
#   ./install.sh --force    # overwrite CLAUDE.md (a .bak copy is kept)
#
# Idempotent: re-running after a `git pull` updates agents, relay and bin, and
# merges the two relay hooks into settings.json only if they are not there yet.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_HOME:-$HOME/.claude}"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

command -v python3 >/dev/null || { echo "python3 is required (relay hooks + settings merge)"; exit 1; }

mkdir -p "$DEST/agents" "$DEST/relay/handoffs" "$DEST/relay/state" "$DEST/bin"

# 1. Subagent personas — always refreshed (they hold no local state).
cp "$HERE"/agents/*.md "$DEST/agents/"
echo "agents:   scout runner builder critic -> $DEST/agents/"

# 2. Relay (context gauge + rollover). config.json is kept if the machine already tuned it.
cp "$HERE/relay/relay.py" "$DEST/relay/relay.py"
[[ -f "$DEST/relay/config.json" ]] || cp "$HERE/relay/config.json" "$DEST/relay/config.json"
echo "relay:    relay.py -> $DEST/relay/ (config.json $( [[ -f "$HERE/relay/config.json" ]] && echo kept-or-installed ))"

# 3. pr-status helper.
cp "$HERE/bin/pr-status" "$DEST/bin/pr-status"; chmod +x "$DEST/bin/pr-status"
echo "bin:      pr-status -> $DEST/bin/pr-status"

# 4. Global CLAUDE.md — the orchestrator rules. Never clobber someone's existing file silently.
if [[ ! -f "$DEST/CLAUDE.md" ]]; then
  cp "$HERE/CLAUDE.md" "$DEST/CLAUDE.md"; echo "CLAUDE.md: installed"
elif cmp -s "$HERE/CLAUDE.md" "$DEST/CLAUDE.md"; then
  echo "CLAUDE.md: already up to date"
elif [[ $FORCE == 1 ]]; then
  cp "$DEST/CLAUDE.md" "$DEST/CLAUDE.md.bak"; cp "$HERE/CLAUDE.md" "$DEST/CLAUDE.md"
  echo "CLAUDE.md: overwritten (previous copy in CLAUDE.md.bak)"
else
  echo "CLAUDE.md: DIFFERS from the bundle — left untouched. Review with:"
  echo "           diff $DEST/CLAUDE.md $HERE/CLAUDE.md    # then re-run with --force to replace"
fi

# 5. Hooks — merge into settings.json without touching anything else in it.
python3 - "$DEST/settings.json" "$HERE/hooks.json" <<'PY'
import json, sys, os
dest, src = sys.argv[1], sys.argv[2]
settings = json.load(open(dest)) if os.path.exists(dest) else {}
wanted = json.load(open(src))["hooks"]
hooks = settings.setdefault("hooks", {})
added = 0
for event, groups in wanted.items():
    have = hooks.setdefault(event, [])
    existing = {h.get("command") for g in have for h in g.get("hooks", [])}
    for g in groups:
        if any(h.get("command") in existing for h in g["hooks"]):
            continue
        have.append(g); added += 1
tmp = dest + ".tmp"
json.dump(settings, open(tmp, "w"), indent=2); open(tmp, "a").write("\n")
os.replace(tmp, dest)
print(f"hooks:    {added} added to {dest}" if added else f"hooks:    already present in {dest}")
PY

echo
echo "Done. Start a new Claude Code session so the hooks and agents load."
echo "Relay note: 'relay.py handoff' opens the next session via VS Code's URI handler, or copies"
echo "the prompt with pbcopy on macOS; on Linux, install xclip/wl-copy or paste the printed prompt by hand."

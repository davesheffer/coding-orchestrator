#!/usr/bin/env python3
"""Relay — context gauge + automatic session rollover for Claude Code.

Subcommands:
  prompt    UserPromptSubmit hook. Injects a context gauge (amber/red zones add
            rollover directives) or, when the prompt carries `relay:<id>`,
            injects that handoff so a fresh session continues the work.
  stop      Stop hook. In the red zone, blocks the stop ONCE per session so the
            model writes a handoff and rolls over instead of idling on a full context.
  handoff   Called by the model: stores the handoff (stdin) and opens a new
            session pre-filled with `relay:<id>` (VS Code URI handler), or copies
            the prompt to the clipboard when not running inside VS Code.
  status    Print the gauge for a transcript (debugging).

Hooks must never break a prompt: every hook path swallows errors and exits 0.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path.home() / ".claude" / "relay"
HANDOFFS = ROOT / "handoffs"
STATE = ROOT / "state"
DEFAULTS = {
    "soft_tokens": 150_000,      # amber: delegate harder, roll over at the next boundary
    "hard_tokens": 250_000,      # red: roll over now
    "task_shift_min_tokens": 30_000,  # below this a task change just continues here
    "handoff_ttl_hours": 72,
    "auto_open": True,
}
RELAY_RE = re.compile(r"\brelay:([a-f0-9]{8})\b")
URI_SCHEMES = {
    "com.microsoft.VSCode": "vscode",
    "com.microsoft.VSCodeInsiders": "vscode-insiders",
    "com.todesktop.230313mzl4w4u92": "cursor",
    "com.exafunction.windsurf": "windsurf",
}


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((ROOT / "config.json").read_text()))
    except Exception:
        pass
    return cfg


def context_tokens(transcript_path):
    """Tokens in the main thread's context = the last non-sidechain assistant
    turn's input + cache read + cache creation. Reads only the file's tail."""
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return None
    for window in (1 << 20, 8 << 20, size):
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - window))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            if '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # partial first line of the window
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            usage = (entry.get("message") or {}).get("usage") or {}
            total = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if total:
                return total
        if window >= size:
            break
    return None


def zone(tokens, cfg):
    if tokens is None:
        return "unknown"
    if tokens >= cfg["hard_tokens"]:
        return "red"
    if tokens >= cfg["soft_tokens"]:
        return "amber"
    return "green"


def k(n):
    return f"{round(n / 1000)}k"


def load_state(session_id):
    try:
        return json.loads((STATE / f"{session_id}.json").read_text())
    except Exception:
        return {}


def save_state(session_id, state):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{session_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def sweep(cfg):
    cutoff = time.time() - cfg["handoff_ttl_hours"] * 3600
    for folder in (HANDOFFS, STATE):
        for p in folder.glob("*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


HOW = ("To roll over: write the handoff per the Relay protocol in ~/.claude/CLAUDE.md and pipe it to "
       "`python3 ~/.claude/relay/relay.py handoff --title \"<short title>\"` (body on stdin via heredoc), "
       "then tell the user the new session is open and stop working in this one.")


def emit_context(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": text}}))


def cmd_prompt():
    data = json.load(sys.stdin)
    cfg = config()
    prompt = data.get("prompt") or ""
    session_id = data.get("session_id") or "unknown"

    m = RELAY_RE.search(prompt)
    if m:
        path = HANDOFFS / f"{m.group(1)}.md"
        if path.exists():
            emit_context(
                "[relay] This session CONTINUES earlier work. The previous session's handoff follows; "
                "treat it as your working memory. Re-verify anything it lists as UNVERIFIED before relying "
                "on it. If it has a NEXT PROMPT section, that is the user's actual request — act on it now.\n\n"
                + path.read_text())
            os.utime(path)
        else:
            emit_context(f"[relay] Handoff {m.group(1)} was not found (expired or deleted). Tell the user.")
        return

    tokens = context_tokens(data.get("transcript_path") or "")
    z = zone(tokens, cfg)
    if z in ("unknown",) or tokens < cfg["task_shift_min_tokens"]:
        return  # fresh session: zero overhead
    if load_state(session_id).get("handoff_done"):
        emit_context(f"[relay] context ~{k(tokens)}. This session was already handed off — if the user is "
                     "still prompting here, answer briefly and remind them the work continues in the new session.")
        return

    gauge = f"[relay] context ~{k(tokens)} tokens — {z.upper()} (amber {k(cfg['soft_tokens'])}, red {k(cfg['hard_tokens'])})."
    shift = ("TASK-SHIFT RULE: if this prompt starts work unrelated to what this session has been doing, "
             "do not do it here — roll over with the prompt copied verbatim under NEXT PROMPT.")
    if z == "green":
        emit_context(f"{gauge} {shift} {HOW}")
    elif z == "amber":
        emit_context(f"{gauge} Context is heavy: route ALL read-heavy or mechanical work through scout/runner/builder "
                     f"subagents so their output stays out of this context, and roll over at the next natural "
                     f"boundary (unit of work finished, tests green). {shift} {HOW}")
    else:
        emit_context(f"{gauge} ROLL OVER NOW: do no new work in this session. Write the handoff, copy this prompt "
                     f"verbatim under NEXT PROMPT, and open the new session. {HOW}")


def cmd_stop():
    data = json.load(sys.stdin)
    if data.get("stop_hook_active"):
        return
    cfg = config()
    session_id = data.get("session_id") or "unknown"
    tokens = context_tokens(data.get("transcript_path") or "")
    if zone(tokens, cfg) != "red":
        return
    state = load_state(session_id)
    if state.get("handoff_done") or state.get("stop_nudged"):
        return
    state["stop_nudged"] = True
    save_state(session_id, state)
    print(json.dumps({"decision": "block", "reason": (
        f"[relay] Context is at ~{k(tokens)} tokens (red ≥ {k(cfg['hard_tokens'])}). Before stopping, roll this "
        f"session over so the next prompt starts fresh. {HOW} If work is mid-flight, the handoff's NEXT STEP must "
        "say exactly where to resume.")}))


def git(cwd, *args):
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def cmd_handoff(argv):
    title, do_open = "continue", True
    it = iter(argv)
    for a in it:
        if a == "--title":
            title = next(it, title)
        elif a == "--no-open":
            do_open = False
    body = sys.stdin.read().strip()
    if len(body) < 40:
        sys.exit("relay: handoff body is empty/too short — pipe the handoff on stdin.")
    cfg = config()
    sweep(cfg)
    hid = secrets.token_hex(4)
    cwd = os.getcwd()
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "unknown")
    dirty = git(cwd, "status", "--porcelain")
    header = "\n".join([
        f"# Relay handoff {hid}: {title}",
        f"- from session: {session_id}",
        f"- cwd: {cwd}",
        f"- branch: {git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD') or 'n/a'} @ {git(cwd, 'rev-parse', '--short', 'HEAD') or 'n/a'}",
        f"- uncommitted files at handoff: {len(dirty.splitlines()) if dirty else 0}",
        f"- written: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
    ])
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    path = HANDOFFS / f"{hid}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(header + "\n" + body + "\n")
    tmp.replace(path)
    if session_id != "unknown":
        state = load_state(session_id)
        state["handoff_done"] = hid
        save_state(session_id, state)

    next_prompt = f"relay:{hid} continue \"{title}\" from the handoff."  # ASCII only: pbcopy mangles non-ASCII
    print(f"handoff saved: {path}")
    scheme = URI_SCHEMES.get(os.environ.get("__CFBundleIdentifier", ""))
    in_ide = os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "claude-vscode" and scheme
    if do_open and cfg["auto_open"] and in_ide and sys.platform == "darwin":
        uri = f"{scheme}://anthropic.claude-code/open?prompt={urllib.parse.quote(next_prompt)}"
        rc = subprocess.run(["open", uri]).returncode
        if rc == 0:
            print("new session opened in the editor with the relay prompt pre-filled — the user only presses Enter.")
            return
        print(f"could not open {uri} (exit {rc}); falling back to clipboard.")
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=next_prompt, text=True)
        print("relay prompt copied to the clipboard.")
    print(f"tell the user: start a new session (/clear, or a new Claude tab) and send:\n  {next_prompt}")


def cmd_status(argv):
    cfg = config()
    tokens = context_tokens(argv[0]) if argv else None
    print(json.dumps({"tokens": tokens, "zone": zone(tokens, cfg), "config": cfg}))


def main():
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd in ("prompt", "stop"):
        try:
            cmd_prompt() if cmd == "prompt" else cmd_stop()
        except Exception:
            pass
        sys.exit(0)
    if cmd == "handoff":
        return cmd_handoff(rest)
    if cmd == "status":
        return cmd_status(rest)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Relay — context gauge + automatic session rollover for Claude Code.

Subcommands:
  prompt    UserPromptSubmit hook. Injects a context gauge (amber/red zones add
            rollover directives) or, when the prompt carries `relay:<id>`,
            injects that handoff so a fresh session continues the work.
  stop      Stop hook. In the red zone, blocks the stop ONCE per session so the
            model writes a handoff and rolls over instead of idling on a full context.
  handoff   Called by the model: stores the handoff (stdin), asks the shared
            VS Code bridge for a new Claude tab, and prints a manual fallback
            prompt when the launch cannot be confirmed.
  status    Print the gauge for a transcript (debugging).

Hooks must never break a prompt: every hook path swallows errors and exits 0.
"""
import json
import locale
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = (Path(os.environ["CLAUDE_HOME"]).expanduser() / "relay"
        if os.environ.get("CLAUDE_HOME") else Path(__file__).resolve().parent)
CLAUDE_HOME = ROOT.parent
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
MAX_TRANSCRIPT_BYTES = 8 << 20
def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def context_tokens(transcript_path):
    """Scan backward in 1 MiB chunks, reading at most 8 MiB total."""
    try:
        with open(transcript_path, "rb") as f:
            cursor = os.fstat(f.fileno()).st_size
            remaining = MAX_TRANSCRIPT_BYTES
            partial = b""
            while cursor > 0 and remaining > 0:
                amount = min(cursor, remaining, 1 << 20)
                cursor -= amount
                remaining -= amount
                f.seek(cursor)
                lines = (f.read(amount) + partial).split(b"\n")
                # Carry the oldest partial record into the next chunk. Discard it
                # if the scan budget is exhausted; a JSON-looking suffix is unsafe.
                partial = lines.pop(0) if cursor else b""
                for line in reversed(lines):
                    if b'"usage"' not in line and b'"compact_boundary"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(entry, dict) or entry.get("isSidechain"):
                        continue
                    if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                        return None
                    if entry.get("type") != "assistant":
                        continue
                    message = entry.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if not isinstance(usage, dict) or "input_tokens" not in usage:
                        continue
                    counts = [usage.get(key, 0) for key in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
                    if any(type(count) is not int or count < 0 for count in counts):
                        return None
                    return sum(counts)
    except OSError:
        return None
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
        return json.loads((STATE / f"{session_id}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(session_id, state):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{session_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
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


HOW = ("To roll over: write GOAL, STATE, DECISIONS & CONSTRAINTS, FILES, VERIFIED vs UNVERIFIED, "
       "NEXT STEP and NEXT PROMPT; pipe the handoff to "
       f"`python3 {shlex.quote(str(Path(__file__).resolve()))} handoff --title \"<short title>\"` "
       "(body on stdin via quoted heredoc). Report the script's actual result; if it only saves/copies "
       "a relay prompt, tell the user how to start the new session. Then stop here.")


def emit_context(text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": text}}))


def read_handoff(path):
    data = path.read_bytes()
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        # Before UTF-8 was explicit, Windows handoffs used the local code page.
        encoding = (locale.getencoding() if hasattr(locale, "getencoding")
                    else locale.getpreferredencoding(False))
        body = data.decode(encoding)
    # Match the universal-newline behavior used by the original text reader.
    return body.replace("\r\n", "\n").replace("\r", "\n")


def cmd_prompt():
    data = json.load(sys.stdin)
    cfg = config()
    prompt = data.get("prompt") or ""
    session_id = data.get("session_id") or "unknown"

    m = RELAY_RE.search(prompt)
    if m:
        path = HANDOFFS / f"{m.group(1)}.md"
        if path.exists():
            try:
                body = read_handoff(path)
            except UnicodeDecodeError:
                emit_context(f"[relay] Handoff {m.group(1)} could not be decoded as UTF-8 or the "
                             "local legacy encoding. Ask the user to convert that handoff file "
                             "to UTF-8 using its original encoding, then retry. Do not infer its contents.")
                return
            emit_context(
                "[relay] This session CONTINUES earlier work. The previous session's handoff follows; "
                "treat it as your working memory. Re-verify anything it lists as UNVERIFIED before relying "
                "on it. If it has a NEXT PROMPT section, that is the user's actual request — act on it now.\n\n"
                + body)
            os.utime(path)
        else:
            emit_context(f"[relay] Handoff {m.group(1)} was not found (expired or deleted). Tell the user.")
        return

    tokens = context_tokens(data.get("transcript_path") or "")
    z = zone(tokens, cfg)
    if z == "unknown":
        if data.get("transcript_path"):
            emit_context("[relay] Context usage is unknown; no recent main-thread usage was available "
                         "within the 8 MiB scan limit. Do not infer a zone or force rollover.")
        return
    if tokens < cfg["task_shift_min_tokens"]:
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
    title, do_open, workspace = "continue", True, None
    it = iter(argv)
    for a in it:
        if a == "--title":
            title = next(it, title)
        elif a == "--no-open":
            do_open = False
        elif a == "--workspace":
            workspace = next(it, None)
            if not workspace:
                sys.exit("relay: --workspace requires a path")
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
    tmp.write_text(header + "\n" + body + "\n", encoding="utf-8")
    tmp.replace(path)
    if session_id != "unknown":
        state = load_state(session_id)
        state["handoff_done"] = hid
        save_state(session_id, state)

    next_prompt = f"relay:{hid} continue \"{title}\" from the handoff."
    print(f"handoff saved: {path}")
    helper = CLAUDE_HOME / "bin" / "rollover-open.py"
    if do_open and cfg["auto_open"] and helper.is_file():
        command = [sys.executable, str(helper), "open", "--client", "claude",
                   "--handoff", str(path), "--resume-token", f"relay:{hid}"]
        if workspace:
            command.extend(["--workspace", workspace])
        result = subprocess.run(command,
                                capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            print(result.stdout.strip())
            return
        if "tab launch is still pending" in result.stdout:
            print(result.stdout.strip())
            return
        print(f"handoff bridge failed (exit {result.returncode}): "
              f"{result.stdout.strip()} {result.stderr.strip()}".strip())
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=next_prompt, text=True, encoding="utf-8")
        print("relay prompt copied to the clipboard.")
    print(f"tell the user: start a new session (/clear, or a new Claude tab) and send:\n  {next_prompt}")


def cmd_status(argv):
    cfg = config()
    tokens = context_tokens(argv[0]) if argv else None
    print(json.dumps({"tokens": tokens, "zone": zone(tokens, cfg), "config": cfg}))


def main():
    # Hooks and piped handoffs use UTF-8, independent of the Windows code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
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

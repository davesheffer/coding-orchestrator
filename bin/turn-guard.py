#!/usr/bin/env python3
"""Turn budget nudges and report check for subagents.

Hooks: PostToolUse and PostToolUseFailure `tick <maxTurns>`, PreToolUse(SubagentHandback) `handback`,
SubagentStop `stop`.

Tool calls approximate turns: at half the budget the agent is told to save progress, near the end to
write its final report (approximately; it warns early in most runs). Each nudge fires once per cap
cycle, so a resumed agent gets them again. `handback` and `stop` make sure the report has a
RESULT/VERDICT line. State is race-free and temp-file free under <install root>/state/turn-guard/:
each call claims the next free numbered file `<id>.n<k>` with exclusive create (k is its count; plain
appends lose writes on Windows), and one-shot flags are files created the same way (the creator emits
the nudge). Fails open: any error exits 0, so it never
blocks a tool call or traps an agent. Keep this tiny and fast.
"""
import json
import math
import os
import re
import sys
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state" / "turn-guard"
REPORT = re.compile(r"^[ \t#>\-*_`|]{0,40}(?:\d+[.)][ \t]*)?[ \t*_`]{0,40}(?:RESULT|VERDICT)[*_`|]*[ \t]*(?::|\||\r?$)",
                    re.I | re.M)
MAX_AGE = 24 * 3600
READ_ONLY = ("critic", "scout")
HALF = "Turn budget: {count} of ~{max} tool calls used. {save}, then continue."
LAST = ("Turn budget nearly spent ({count} of ~{max} tool calls). Write your report now ({where}): RESULT / "
        "EVIDENCE / CONFIDENCE / UNVERIFIED (critic: VERDICT line). Then finish only what fits in the "
        "remaining calls; list anything left under UNVERIFIED as NEXT so you can be resumed.")
WHERE = ("report file if your brief names one", "put your findings so far in your final message")
SAVE = ("Save progress to your report file now (if your brief names one)",
        "Put your findings so far in your final message")
BLOCK = ("Do not stop yet: your final message has no RESULT line. Write your report (send it with "
         "SubagentHandback if you have that tool): RESULT / EVIDENCE (commands + exit codes) / CONFIDENCE / "
         "UNVERIFIED (critic: end with a VERDICT: line). If work is unfinished, say what is done and the "
         "exact NEXT step under UNVERIFIED.")
DENY = ("Your report has no RESULT line: resend it with RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED "
        "(critic: VERDICT line).")


def base(data: dict, fallback: bool) -> Path | None:
    for key in (("agent_id", "session_id") if fallback else ("agent_id",)):
        value = data.get(key)
        if isinstance(value, str) and value:
            return STATE / re.sub(r"[^A-Za-z0-9_-]", "_", value)[:120]
    return None


def flag(base_path: Path, suffix: str) -> bool:
    """Exclusive create: True for exactly one caller. Any other OSError propagates (fail open)."""
    try:
        with open(f"{base_path}.{suffix}", "x"):
            return True
    except FileExistsError:
        return False


def prune(directory: Path, keep: Path) -> None:
    cutoff = time.time() - MAX_AGE
    for old in directory.iterdir():
        try:
            if old != keep and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def reported_in(data: dict) -> bool:
    tool_input = data.get("tool_input")
    message = tool_input.get("message") if isinstance(tool_input, dict) else None
    return isinstance(message, str) and bool(REPORT.search(message))


def tick(data: dict, base_path: Path, limit: str) -> str | None:
    base_path.parent.mkdir(parents=True, exist_ok=True)
    seen = len(list(base_path.parent.glob(f"{base_path.name}.n*")))
    count = seen + 1
    while not flag(base_path, f"n{count}"):
        count += 1
        if count > seen + 1000:
            return None
    if not seen:
        prune(base_path.parent, Path(f"{base_path}.n{count}"))
    if data.get("tool_name") == "SubagentHandback" and reported_in(data):
        flag(base_path, "reported")
    try:
        cap = int(limit)
    except ValueError:
        return None
    if cap <= 0:
        return None
    role = int(data.get("agent_type") in READ_ONLY)
    cycle = (count - 1) // cap
    start = cycle * cap
    if count >= start + cap - 5:
        if flag(base_path, f"last{cycle}"):
            flag(base_path, f"half{cycle}")
            return LAST.format(count=count, max=cap, where=WHERE[role])
    elif cap >= 12 and count >= start + math.ceil(cap / 2):
        if flag(base_path, f"half{cycle}"):
            return HALF.format(count=count, max=cap, save=SAVE[role])
    return None


def stop(data: dict) -> dict | None:
    base_path = base(data, False)
    agent_type = data.get("agent_type")
    last = data.get("last_assistant_message")
    if (base_path is None or not isinstance(agent_type, str) or not agent_type
            or not isinstance(last, str)):
        return None
    if data.get("stop_hook_active") or Path(f"{base_path}.reported").exists() or REPORT.search(last):
        for suffix in ("reported", "denied"):
            Path(f"{base_path}.{suffix}").unlink(missing_ok=True)
        return None
    return {"decision": "block", "reason": BLOCK}


def handback(data: dict) -> dict | None:
    base_path = base(data, True)
    tool_input = data.get("tool_input")
    if base_path is None or not isinstance(tool_input, dict) or not isinstance(tool_input.get("message"), str):
        return None
    base_path.parent.mkdir(parents=True, exist_ok=True)
    if reported_in(data) or not flag(base_path, "denied"):
        flag(base_path, "reported")
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": DENY}}


def main() -> dict | None:
    data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if not isinstance(data, dict) or len(sys.argv) < 2:
        return None
    mode = sys.argv[1]
    if mode == "stop":
        return stop(data)
    if mode == "handback":
        return handback(data)
    if mode == "tick":
        base_path = base(data, True)
        message = tick(data, base_path, sys.argv[2] if len(sys.argv) > 2 else "") if base_path else None
        event = data.get("hook_event_name")
        event = event if event in ("PostToolUse", "PostToolUseFailure") else "PostToolUse"
        return ({"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}
                if message else None)
    return None


if __name__ == "__main__":
    try:
        out = main()
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    except BaseException:
        pass
    os._exit(0)

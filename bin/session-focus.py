#!/usr/bin/env python3
"""Switch VS Code to a running Claude Code session's tab, in the window that owns it."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time

_SPEC = importlib.util.spec_from_file_location(
    "rollover_open", Path(__file__).resolve().parent / "rollover-open.py")
rollover = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rollover)

SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def sessions_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "sessions"


def find_session(session: str) -> list[dict]:
    """Registry entries for `session`, newest first. A crashed session may leave a stale one."""
    found = []
    for path in sessions_dir().glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and str(entry.get("sessionId", "")).lower() == session:
            found.append(entry)
    return sorted(found, key=lambda entry: entry.get("updatedAt") or 0, reverse=True)


def started(pid: int) -> int | None:
    """The process's creation time as a Windows FILETIME (what the registry's `procStart`
    records), or None when it cannot be read or the platform is not Windows."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(one) for one in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def is_older(parent: int, child: int, start=None) -> bool:
    """Whether `parent` can really be `child`'s parent. Windows keeps a dead parent's
    PID, so a younger process reusing it would otherwise extend the chain."""
    start = start or started
    child_start = start(child)
    if child_start is None:
        return True  # creation times unknown here: rely on the walk's limit
    parent_start = start(parent)
    return parent_start is not None and parent_start <= child_start


def ancestors(pid: int, parents: dict[int, int], limit: int = 3, start=None) -> list[int]:
    """Ancestor PIDs of `pid` (excluding it); the bridge whose extension host is one of
    them owns the session's tab. Claude's process is a direct child of its window's
    extension host (chain: [exthost, VS Code main]); a deeper walk can reach another VS Code
    instance's extension host when one instance runs nested under another window's session.
    Stops at `limit`, a cycle, a missing parent, or a
    parent younger than its child (a reused PID)."""
    chain = [pid]
    while pid in parents and len(chain) <= limit:
        parent = parents[pid]
        if parent <= 0 or parent in chain or not is_older(parent, pid, start):
            break
        chain.append(parent)
        pid = parent
    return chain[1:]


def is_alive(entry: dict, table: dict[int, int], start=None) -> bool:
    """Whether the registry entry's own process still runs: its PID is in the table and,
    where the creation time can be read, it matches `procStart` (no reused PID)."""
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid not in table:
        return False
    recorded = str(entry.get("procStart") or "")
    if not recorded.isdigit():
        return True
    actual = (start or started)(pid)
    if actual is None:
        return os.name != "nt"  # Windows can read it for our own processes; unreadable is not proof
    return actual == int(recorded)


def is_this_machine(entry: dict) -> bool:
    """The registry's `pidDomain` (`win32:<host>`) names the machine whose PIDs it holds."""
    domain = entry.get("pidDomain")
    if not isinstance(domain, str) or domain == "":
        return True
    return domain.lower() == f"{sys.platform}:{socket.gethostname()}".lower()


def focus(session: str, timeout: float = 8.0, opener=None, parents=None, start=None) -> tuple[int, str]:
    """Returns (exit code, one-line message). `opener` and `parents` default to the
    rollover-open helpers and `start` to `started`, looked up at call time so tests can
    replace any of them."""
    opener = opener or rollover.open_uri
    start = start or started
    if not SESSION_ID.match(session):
        return 1, f"invalid session id: {session}"
    session = session.lower()
    entries = find_session(session)
    if not entries:
        return 1, "session not running"
    try:
        table = parents if parents is not None else rollover.parent_map()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return 1, f"process table unavailable ({exc})"
    local = [entry for entry in entries if is_this_machine(entry)]
    if not local:
        other = " ".join(str(entries[0].get("name") or session).split())
        return 1, f"session {other} runs on another machine"
    live = [entry for entry in local if is_alive(entry, table, start)]
    entry = live[0] if live else local[0]
    name = " ".join(str(entry.get("name") or session).split())
    entrypoint = entry.get("entrypoint") or "unknown"
    if entrypoint != "claude-vscode":
        return 1, f"session {name} runs in a terminal ({entrypoint}); switch to it there"
    pid = entry.get("pid")
    if not live:
        return 1, f"session process {pid} is gone (stale registry file)"
    hosts = ancestors(pid, table, start=start)
    if not hosts:
        return 1, f"session {name} has no editor process above it"

    request_id = secrets.token_hex(16)
    root = rollover.home()
    request = root / "launches" / f"{request_id}.json"
    rollover.write_json(request, {
        "action": "focus", "session": session, "hosts": hosts, "created_at": time.time(),
    })
    ack = root / "acks" / f"{request_id}.json"
    grace = rollover.CLAIMED_GRACE
    launched = True
    try:
        opener(f"vscode://coding-orchestrator.handoff-bridge/open?id={request_id}")
    except (OSError, subprocess.CalledProcessError) as exc:
        if rollover.withdraw(request, grace) is not False:
            return 1, f"editor launch failed ({exc})"
        launched = False  # a bridge scan already claimed it despite the failed launch
    result = rollover.wait_for_ack(ack, timeout if launched else grace)
    if result is None and launched:
        # The bridge claims a request by deleting it, so withdrawing it settles the race.
        withdrawn = rollover.withdraw(request, grace)
        if withdrawn is None:
            result = rollover.wait_for_ack(ack, 0)
            if result is None:
                return 1, (f"focus was not confirmed and the request could not be withdrawn "
                           f"({request} is locked); the tab may still switch late")
        elif withdrawn:
            return 1, (f"the VS Code handoff bridge in the window of {name} did not pick up the "
                       f"request within {timeout:g}s (extension missing or disabled)")
        else:
            result = rollover.wait_for_ack(ack, grace)
    if result is None:
        return 1, "focus was requested but not confirmed"
    if result.get("status") == "focused":
        return 0, f"focused {name}"
    error = " ".join(str(result.get("error", "unknown error")).split())
    hint = " (handoff bridge older than 0.6.0? reinstall it)" if "handoff request" in error else ""
    return 1, f"editor could not focus {name}: {error}{hint}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)
    try:
        code, message = focus(args.session, args.timeout)
    except (OSError, ValueError) as exc:
        code, message = 1, f"session-focus: {exc}"
    print(message)
    return code


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

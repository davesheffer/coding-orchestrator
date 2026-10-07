#!/usr/bin/env python3
"""PreToolUse guard for the scout role: Bash may only run `<interpreter> ro.py <verb> ...`.

The only accepted interpreter is the one running this guard (sys.executable, pinned at install
time in the hook command); bare `python`/`python3` are denied since PATH may be repo-controlled.
Exit 0 allows; exit 2 blocks. Any error blocks. Keep this tiny and fast: a hook
timeout fails open.
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path

VERBS = ("log", "diff", "show", "status", "blame", "issues", "issue", "prs", "pr")
FORBIDDEN = re.compile(r"[;&|<>$`(){}\[\]*?!#\n\r\x00]")
RO = Path(__file__).resolve().parent / "ro.py"
RO_AS_INSTALLED = Path(os.path.abspath(__file__)).parent / "ro.py"
DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def norm(path: str) -> str:
    """Lexical only: realpath would follow repo symlinks, possibly to a stalling network share."""
    return os.path.normcase(os.path.normpath(path))


def local_path(path: str) -> bool:
    """Reject UNC, device, home and drive-relative forms outright."""
    if not path or path.startswith(("\\", "~")) or path[:2] in ("//", "/\\"):
        return False
    if ":" in path:
        return path.count(":") == 1 and DRIVE.match(path) is not None
    return True


def rendered_prefixes() -> list[str]:
    """The exact `'<python>' '<ro.py>' ` prefixes the installer renders; the install path may hold ( ) & !."""
    py = shlex.quote(Path(sys.executable).as_posix())
    return [f"{py} {shlex.quote(ro.as_posix())} " for ro in (RO, RO_AS_INSTALLED)]


def allowed(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    for prefix in rendered_prefixes():
        head = command[:len(prefix)]
        if (head.lower() == prefix.lower()) if os.name == "nt" else (head == prefix):
            rest = command[len(prefix):]
            return not FORBIDDEN.search(rest) and (shlex.split(rest) or [""])[0] in VERBS
    if FORBIDDEN.search(command):
        return False
    tokens = shlex.split(command, posix=True)
    return (len(tokens) >= 3 and local_path(tokens[0]) and os.path.isabs(tokens[0])
            and norm(tokens[0]) == norm(sys.executable)
            and local_path(tokens[1]) and os.path.isabs(tokens[1])
            and norm(tokens[1]) in (norm(str(RO)), norm(str(RO_AS_INSTALLED)))
            and tokens[2] in VERBS)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if allowed(payload["tool_input"]["command"]):
            return 0
    except Exception:
        pass
    print("role-guard: denied: scout Bash is read-only. Use the Read/Grep/Glob tools, or run "
          f"`{shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(RO.as_posix())} <verb>` with verb one of: {', '.join(VERBS)}.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 2
    raise SystemExit(code)

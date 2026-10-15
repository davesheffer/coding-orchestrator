#!/usr/bin/env python3
"""Read-only git/gh wrapper for the scout role. Builds argv itself; never uses a shell."""
from __future__ import annotations

import os
import re
import subprocess
import sys

VERBS = ("log", "diff", "show", "status", "blame", "issues", "issue", "prs", "pr")
USAGE = ("usage: ro.py log [-n N] [--oneline] [--stat] [REF] [-- PATH...] | "
         "diff [--stat] [--cached] [REF|REF..REF|REF...REF] [-- PATH...] | "
         "show [--stat] REF [-- PATH...] | status | blame PATH [-L START,END] | "
         "issues | issue N | prs | pr N")
GIT_BASE = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
            "-c", "diff.external=", "-c", "core.untrackedCache=false"]
CONFIG_RX = (r"^(filter\..*\.(clean|smudge|process)|core\.fsmonitor|core\.hookspath|"
             r"diff\..*\.(command|textconv)|core\.(pager|editor|sshcommand|askpass|gitproxy)|"
             r"include\.path|includeif\..*|gpg\..*|log\.showsignature)$")
REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./^~@:-]*$")
PATH_RE = re.compile(r"^[^\x00-\x1f]+$")
NUM_RE = re.compile(r"^\d{1,7}$")
RANGE_RE = re.compile(r"^\d+,\d+$")
TIMEOUT = 60


class UsageError(Exception):
    pass


def check_ref(ref: str) -> str:
    if not REF_RE.match(ref) or ref.startswith("-") or ".." in ref:
        raise UsageError(f"bad ref: {ref!r}")
    return ref


def check_range(spec: str) -> str:
    for sep in ("...", ".."):
        if sep in spec:
            left, right = spec.split(sep, 1)
            check_ref(left)
            check_ref(right)
            return spec
    return check_ref(spec)


def check_path(path: str) -> str:
    if path.startswith("-") or "\x00" in path or not PATH_RE.match(path):
        raise UsageError(f"bad path: {path!r}")
    return path


def build_argv(verb: str, args: list[str]) -> list[str]:
    """Return the full argv for a verb, or raise UsageError."""
    if verb in ("issues", "prs"):
        if args:
            raise UsageError(f"{verb} takes no arguments")
        return ["gh", "issue" if verb == "issues" else "pr", "list", "--limit", "30"]
    if verb in ("issue", "pr"):
        if len(args) != 1 or not NUM_RE.match(args[0]):
            raise UsageError(f"{verb} takes one number")
        return ["gh", verb, "view", args[0]]
    if verb == "status":
        if args:
            raise UsageError("status takes no arguments")
        return GIT_BASE + ["status", "--short", "--branch", "--ignore-submodules=all"]
    if verb == "blame":
        rest = list(args)
        span: list[str] = []
        if len(rest) >= 3 and rest[-2] == "-L":
            if not RANGE_RE.match(rest[-1]):
                raise UsageError("bad -L range")
            span = ["-L", rest[-1]]
            rest = rest[:-2]
        if len(rest) != 1:
            raise UsageError("blame takes PATH [-L START,END]")
        return GIT_BASE + ["blame", "--no-textconv", *span, "--", check_path(rest[0])]
    if verb not in ("log", "diff", "show"):
        raise UsageError(f"unknown verb: {verb}")
    head = list(args)
    paths: list[str] = []
    if "--" in head:
        i = head.index("--")
        head, paths = head[:i], [check_path(p) for p in head[i + 1:]]
    flags: list[str] = []
    refs: list[str] = []
    count = 20
    allowed = {"log": {"--oneline", "--stat"}, "diff": {"--stat", "--cached"}, "show": {"--stat"}}[verb]
    i = 0
    while i < len(head):
        token = head[i]
        if verb == "log" and token == "-n":
            if i + 1 >= len(head) or not NUM_RE.match(head[i + 1]) or not 1 <= int(head[i + 1]) <= 500:
                raise UsageError("-n takes an integer 1..500")
            count = int(head[i + 1])
            i += 2
            continue
        if token in allowed:
            if token not in flags:
                flags.append(token)
        elif token.startswith("-"):
            raise UsageError(f"unknown flag: {token}")
        else:
            refs.append(check_range(token) if verb == "diff" else check_ref(token))
        i += 1
    if len(refs) > 1 or (verb == "show" and len(refs) != 1):
        raise UsageError(f"{verb}: wrong number of refs")
    argv = GIT_BASE + [verb, "--no-textconv", "--no-ext-diff"]
    if verb != "diff":
        argv.append("--no-show-signature")
    argv += ["--ignore-submodules=all", "--submodule=short"]
    if verb == "log":
        argv += ["-n", str(count)]
    argv += flags + refs
    if paths:
        argv += ["--", *paths]
    return argv


def scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def resolve(name: str) -> str:
    """Find name on absolute PATH entries other than cwd; shutil.which would search cwd first on Windows."""
    cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    exts = [""]
    if os.name == "nt":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e] + [""]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or entry == "." or not os.path.isabs(entry):
            continue
        real = os.path.normcase(os.path.realpath(entry))
        if real == cwd:
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    raise OSError(f"{name} not found on PATH")


def local_config_violation(env: dict[str, str]) -> str | None:
    argv = [resolve("git"), "--no-pager", "config", "--show-scope", "--includes", "--get-regexp",
            CONFIG_RX]
    done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=TIMEOUT)
    if done.returncode == 1:
        return None
    if done.returncode != 0:
        return "unreadable config"
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] in ("local", "worktree", "command"):
            return parts[1]
    return None


def main(argv: list[str]) -> int:
    try:
        if not argv or argv[0] not in VERBS:
            raise UsageError("unknown or missing verb")
        cmd = build_argv(argv[0], argv[1:])
    except UsageError as exc:
        print(f"ro: {exc}\n{USAGE}", file=sys.stderr)
        return 2
    env = scrubbed_env()
    try:
        if cmd[0] == "git":
            bad = local_config_violation(env)
            if bad:
                print(f"ro: refused: repo-local config defines {bad}; inspect with the Read tool",
                      file=sys.stderr)
                return 2
        cmd[0] = resolve(cmd[0])
        return subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, shell=False,
                              timeout=TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        print(f"ro: timed out after {TIMEOUT}s", file=sys.stderr)
        return 124
    except OSError as exc:
        print(f"ro: cannot run {cmd[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

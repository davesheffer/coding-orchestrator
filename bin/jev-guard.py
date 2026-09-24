#!/usr/bin/env python3
"""Jev guard — opt-in PreToolUse/PostToolUse hooks for risk gating and report checks.

`gate` runs as a PreToolUse hook (matcher Bash). Before a `git commit` or
`git push`, it asks TypeSafe's hosted Jev classifier whether the pending diff
looks risky (security, concurrency, data loss, public API impact). If the
diff is risky and no critic subagent has reviewed the changed files since
they last changed, the commit/push is denied with a reason explaining how to
proceed; otherwise (or on override retry) the call passes through unchanged.

`handback` runs as a PreToolUse hook (matcher SubagentHandback). It checks the
subagent's report (its `message` argument) for RESULT/EVIDENCE/CONFIDENCE/
UNVERIFIED sections and internal consistency; a weak report is denied once
with a reason telling the subagent to verify or escalate before handing back
again, and an identical retry is let through (override).

`agent-done` runs as a PostToolUse hook (matcher Agent|Task|SubagentHandback).
For Agent/Task calls that have completed (not backgrounded), it records when
a critic subagent finishes (marking files reviewed from that point on) and,
for report-producing roles, checks the completed report the same way as
`handback`; weak reports get an additionalContext nudge to verify or escalate.
For SubagentHandback calls it only records the critic timestamp (the report
itself was already checked by `handback`). The critic timestamp is backdated
to when the critic *started* (its async launch), tracked in `critic_started`,
so files edited while the critic was running still need a fresh review.

Per-session state (last critic review timestamp, in-flight critic launch
timestamps, recently denied diff hashes, recently denied handback report
hashes) lives under `<install>/relay/state/jev-<session_id>.json` and is
updated under a `<state>.lock` file lock (see `update_state`) so concurrent
hook invocations don't clobber each other's keys.

Fail-open: any missing key, disabled config, error or timeout leaves the
call unchanged. All Jev calls run under a hard wall-clock deadline of
min(timeout_seconds, 4) seconds. All git subprocesses in one `gate()` call
share a single wall-clock budget (GIT_SUBPROCESS_BUDGET_SECONDS); once it is
spent, remaining git calls fail open, so the script always finishes inside
the gate hook's 10 s timeout even in the worst case (4 s of git plus a 4 s
classify_fn call).
Nothing here logs diff text, file names, commands, or report text.
"""
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # e.g. Windows: update_state locks with msvcrt instead.
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
# How long update_state waits for the Windows lock before giving up (the hook fails
# open), and how often a Windows tmp.replace is retried while another process has the
# state file open.
MSVCRT_LOCK_SECONDS = 2.0
REPLACE_ATTEMPTS = 5
REPLACE_RETRY_SECONDS = 0.05

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_client import (  # noqa: E402  (re-exported for callers and tests)
    ROOT, ask, elapsed_ms, feature_enabled, load_config, noul, timestamp, write_log)

STATE_DIR = ROOT / "relay" / "state"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Global options git accepts before the subcommand: -C <dir>, -c <key>=<value>, a
# space-separated long option that takes a value (--git-dir, --work-tree, --namespace,
# --super-prefix, --config-env), or any other --long-option (with or without =value).
# Only a literal -C sets cwd (see _dash_c_dir); -c and --long-options are matched so
# they don't get mistaken for the subcommand. The arg may be a quoted string (e.g. -C
# "dir with space") or a bare \S+ token.
_OPT_ARG = r"""(?:"[^"]*"|'[^']*'|\S+)"""
_LONG_VALUE_OPTS = r"(?:--git-dir|--work-tree|--namespace|--super-prefix|--config-env)"
# The value-taking names are excluded from the generic --long-option branch and their
# space-separated value may not start with `-`, so each token has exactly one way to
# match; overlapping branches here backtrack exponentially on repeated options.
GIT_GLOBAL_OPTS = (r"(?:\s+(?:-C\s+" + _OPT_ARG + r"|-c\s+" + _OPT_ARG + r"|"
                    + _LONG_VALUE_OPTS + r"(?:=\S+|\s+(?!-)" + _OPT_ARG + r")|"
                    r"--(?!(?:git-dir|work-tree|namespace|super-prefix|config-env)(?![\w-]))"
                    r"[\w-]+(?:=\S+)?))*")
# What may start a command: the start, a ; & | ( operator, a newline, a `{` group or
# a backtick substitution. Only blanks follow it (_LEAD): a newline is a separator in
# its own right, and `\s*` there let each newline of a long run re-consume the rest of
# the run (quadratic).
_SEP = r"(?:^|[;&|(\n{`])"
_LEAD = r"[ \t]*"
# Env-assignment (e.g. `GIT_EDITOR=true git commit`, `A=1 B=2 git push`), shell keyword
# (then/do/else) and wrapper (time/exec/command/env/nice/sudo, with their -flags and a
# numeric flag value such as `nice -n 5`) prefixes before the `git` invocation itself.
_ENV_PREFIX = (r"(?:(?:then|do|else|time|exec|command|env|nice|sudo)\s+(?:-[\w-]+(?:\s+\d+)?\s+)*"
               r"|[A-Za-z_]\w*=[^\s;&|()`]*\s+)*")
# `git`, `git.exe`, or a path to either (`/usr/bin/git`, `C:\Git\cmd\git.exe`). The
# leading path segment excludes shell separators/quotes/backtick (rather than `\S*`)
# so it can never backtrack across a command boundary looking for "git" — the other
# half of the scanner's quadratic behaviour fixed in _strip_heredocs_and_quotes below.
_GIT = r"""(?:[^\s;&|(){}`'"]*[/\\])?git(?:\.exe)?"""
GIT_COMMAND_RE = re.compile(
    _SEP + _LEAD + _ENV_PREFIX + _GIT + r"(" + GIT_GLOBAL_OPTS + r")\s+(commit|push)\b")
GIT_ADD_RE = re.compile(_SEP + _LEAD + _ENV_PREFIX + _GIT + GIT_GLOBAL_OPTS + r"\s+add\b")
# Git Bash / MSYS drive paths (`/c/Users/...`), translated to `C:/...` on Windows.
MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/|$)")
# -a/--all, or a short-flag cluster containing a (e.g. -am), within the commit segment.
ALL_FLAG_RE = re.compile(r"(?:^|\s)(--all|-[A-Za-z]*a[A-Za-z]*)(?=\s|$)")
# `cd <dir>` as its own segment (split on &&, ;, ||, or a `(` subshell opener) before
# the git segment.
CD_RE = re.compile(r"^\s*cd\s+(.+?)\s*$")
# A heredoc operator (`<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`, `<<\WORD`); the
# scanner in _strip_heredocs_and_quotes only tries it outside quotes and comments, and
# never on a here-string (`<<<`). Group 1 captures a leading `-` (the `<<-` form, which
# lets bash indent the terminator with tabs); the word itself may contain `.` and `-`
# (e.g. `<<EOF-1`, `<<END.MARKER`), not just `\w`.
HEREDOC_RE = re.compile(r"<<(-)?[ \t]*(?:'([\w.-]+)'|\"([\w.-]+)\"|\\?([\w.-]+))")
# More untracked files than this are not folded in; the change then always needs review.
MAX_UNTRACKED = 1000
# Chars of scanned-so-far command kept for the -C/-c and cd quote-context checks in
# _strip_heredocs_and_quotes below; far larger than any real `-C "<dir>"`/`cd "<dir>"`
# prefix, so checking only this bounded tail behaves like scanning the full prefix
# without rejoining/rescanning it from scratch on every quoted argument (quadratic on
# a command with many quotes). Kept well under 4 KB: the per-quote regex search cost
# below scales with this window, and a command can have thousands of quoted arguments.
PREFIX_WINDOW_CHARS = 256
# Quoted strings are stripped so words inside them (e.g. `echo "git commit"`) can't be
# mistaken for a real command, EXCEPT a quoted -C/-c argument (e.g. -C "dir with
# space") or a `cd "dir"` target, which fix 2 needs intact to resolve the gate's cwd.
# A quoted string is preserved only when it is the value of a literal `-C`/`-c` that
# sits in a git global-options segment (i.e. `git`, zero or more already-matched global
# options, then `-C `/`-c `, right before the quote) — not an arbitrary `-c "..."` in
# unrelated text (e.g. `echo -c "x; git commit -m y"`).
_GIT_DASH_C_PREFIX_RE = re.compile(
    _SEP + _LEAD + _ENV_PREFIX + _GIT + GIT_GLOBAL_OPTS + r"\s+-[Cc]\s+$")
_CD_PREFIX_RE = re.compile(_SEP + _LEAD + r"cd\s+$")
# Once the window has been trimmed, the start of a long git segment (e.g. `git -c
# x=<500 chars> -C "dir" push`) may have fallen out of it; a quote after any `-C `,
# `-c ` or `cd ` is then kept, since dropping it would turn `-C "dir" push` into
# `-C  push` and hide the push (a spurious check is the safe side).
_LOOSE_KEEP_RE = re.compile(r"(?:-[Cc]|(?<![\w-])cd)\s+$")
# More git commit/push matches than this in one command are not resolved one by one
# (each resolution rescans the prefix); later ones still count toward the operation.
MAX_GIT_MATCHES = 64
# Staged files whose hunks are never sent to Jev (matched case-insensitively against the
# file's base name); only the file name and a `[redacted]` marker go out.
REDACT_FILE_PATTERNS = (".env*", "*.env", "*.pem", "*.key", "*secret*", "id_rsa*", "*.p12", "*.pfx",
                         "credentials*", "*.jks", "*.keystore")
# Token shapes scrubbed from diff and report text before it is sent.
SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[\w-]{10,}", re.DOTALL)
DIFF_HEADER_RE = re.compile(r"^diff --git (?:\"?a/(.*?)\"?) (?:\"?b/(.*?)\"?)$")
# Forced onto every `git diff` the guard runs so the parsed header shape (matched by
# DIFF_HEADER_RE above) can't be defeated by the user's own git config: diff.noprefix
# or diff.mnemonicPrefix (no `a/`/`b/` prefix), diff.srcPrefix/dstPrefix (a different
# prefix) or color.diff=always (ANSI codes in the header) would otherwise make
# _redact_diff never enter "header" mode for a matched file, sending its hunks
# unredacted. A diff.<driver>.textconv filter (e.g. `gpg -d` for *.gpg) would send its
# output as hunk text under an unredacted name, and diff.relative would limit the diff
# to the gate's subdirectory. Command-line flags override config, unlike `-c`
# overrides which color.diff=always still beats.
DIFF_FORMAT_ARGS = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-relative",
                    "--src-prefix=a/", "--dst-prefix=b/"]
GIT_SUBPROCESS_BUDGET_SECONDS = 4.0

RISK_CRITERIA = {
    "none": ("Routine change: docs, tests, formatting, small local logic with no security, "
             "concurrency, data or API impact."),
    "security": ("Touches authentication, authorization, secrets, crypto, input validation, "
                 "sandboxing, permissions, or injection-prone code."),
    "concurrency": ("Touches threading, locking, async ordering, shared mutable state, retries, "
                     "or race-prone logic."),
    "data_loss": ("Migrations, deletes, overwrites, schema/format changes, destructive file or "
                   "database operations."),
    "public_api": ("Changes a public API, CLI, hook/output contract, config format, or other "
                    "interface that others depend on."),
}
RISK_INSTRUCTIONS = "How risky is this change?"
NEEDS_REVIEW_INSTRUCTIONS = ("This change is risky enough that an independent adversarial "
                              "reviewer should check it before it is committed or pushed.")
SUPPORTED_INSTRUCTIONS = ("The EVIDENCE (commands run, exit codes, file:line references, "
                           "observed output) concretely supports the claims in RESULT.")
MATERIAL_GAP_INSTRUCTIONS = ("UNVERIFIED lists something material to whether RESULT is correct "
                              "or safe to rely on.")
SECTION_NAMES = ("RESULT", "EVIDENCE", "CONFIDENCE", "UNVERIFIED")
# Case-sensitive UPPERCASE headers with a required colon, so prose like "Result of git
# diff..." doesn't overmatch; optional leading markdown (#, *, -, >) is still allowed.
SECTION_RE = re.compile(
    r"(?m)^[ \t]*[#*\-> \t]*\**(RESULT|EVIDENCE|CONFIDENCE|UNVERIFIED)\**[ \t]*:\**[ \t]*")


def _desc_hash(description):
    """A short, non-reversible stand-in for a task description in log lines (never
    the description text itself); omitted (returns None) when there's nothing to hash."""
    if not isinstance(description, str) or not description:
        return None
    return hashlib.sha256(description.encode("utf-8", "replace")).hexdigest()[:12]


def session_state_path(session_id, state_dir=STATE_DIR):
    sid = session_id if isinstance(session_id, str) and SESSION_ID_RE.match(session_id) else "unknown"
    return Path(state_dir) / f"jev-{sid}.json"


def load_session_state(session_id, state_dir=STATE_DIR):
    try:
        return json.loads(session_state_path(session_id, state_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session_state(session_id, state, state_dir=STATE_DIR):
    path = session_state_path(session_id, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    _replace(tmp, path)


def _replace(tmp, path):
    """tmp.replace(path), retried briefly on PermissionError (Windows refuses the
    replace while another process has `path` open); the tmp file is removed if it
    still fails."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


def _msvcrt_lock(lock_file):
    """Lock the first byte of `lock_file` (Windows), retrying for MSVCRT_LOCK_SECONDS;
    raises OSError if another process still holds it."""
    lock_file.seek(0)
    deadline = time.monotonic() + MSVCRT_LOCK_SECONDS
    while True:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def update_state(path, mutate_fn):
    """Read-modify-write `path`'s JSON state under an exclusive lock on `<path>.lock`.

    `mutate_fn(state)` mutates a freshly reloaded on-disk state dict in place (or
    returns a replacement dict) so a concurrent writer's unrelated keys survive even
    if this invocation's load was stale. The lock is `fcntl.flock`, or `msvcrt.locking`
    on Windows. If neither is available, the update runs without a lock: still
    correct for a single process, best-effort under real concurrency.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_file = None
    locked = False
    try:
        if fcntl is not None:
            lock_file = open(lock_path, "a+")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            locked = True
        elif msvcrt is not None:
            lock_file = open(lock_path, "a+")
            _msvcrt_lock(lock_file)
            locked = True
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        result = mutate_fn(state)
        if isinstance(result, dict):
            state = result
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        _replace(tmp, path)
    finally:
        if lock_file is not None:
            if locked:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    else:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            lock_file.close()


def _quote_end(text, start):
    """Index just past the quoted string opening at `start`, or None if unterminated.
    Single quotes take no escapes; double quotes honour backslash escapes."""
    if text[start] == "'":
        end = text.find("'", start + 1)
        return None if end == -1 else end + 1
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return None


def _strip_heredocs_and_quotes(command):
    """Remove heredoc bodies, comments and quoted strings so text inside them (e.g.
    `echo "git commit"` or a `cat <<EOF ... git commit ... EOF` body) can't be mistaken
    for a real command. `git commit -m "msg"` still matches: only the quoted message is
    removed, and the verb sits outside it.

    A small left-to-right scanner rather than regexes, so a backslash escape outside
    quotes (`don\\'t`) can't open a phantom quote, a `<<` inside quotes or a comment
    isn't taken for a heredoc, and a `\\`-newline continuation joins its lines. A quoted
    string is kept when it is the value of a literal git `-C`/`-c` or a `cd` target,
    which `_cd_prefix_dir` and `_dash_c_dir` need intact to resolve the gate's cwd."""
    out = []
    tail = ""  # last (roughly) PREFIX_WINDOW_CHARS of "".join(out); see its definition
    pending = []  # (word, dash_form) heredoc terminators whose bodies start at the next newline
    trimmed = False
    # (word, dash_form) with no terminator anywhere after where it was last searched
    # from: later searches start further on, so they can't find one either. Without
    # this, `cat <<A\n` repeated re-searches to the end of the input every time.
    unterminated = set()

    def emit(s):
        nonlocal tail, trimmed
        out.append(s)
        # Trimmed only once tail has grown to double the window, so the O(window) trim
        # cost amortizes to O(1) per character instead of running (and rescanning) on
        # every quoted argument.
        tail += s
        if len(tail) > 2 * PREFIX_WINDOW_CHARS:
            # The leading "x" keeps _SEP's `^` from matching at a mid-word cut.
            tail = "x" + tail[-PREFIX_WINDOW_CHARS:]
            trimmed = True

    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            if command.startswith("\n", i + 1):
                i += 2  # line continuation: `git \<newline>commit` is one command
                continue
            emit(command[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            end = _quote_end(command, i)
            if end is None:
                # Unbalanced quote: keep the rest; a spurious check is safer than
                # hiding a later git command.
                emit(command[i:])
                break
            if (_GIT_DASH_C_PREFIX_RE.search(tail) or _CD_PREFIX_RE.search(tail)
                    or (trimmed and _LOOSE_KEEP_RE.search(tail))):
                emit(command[i:end])
            i = end
            continue
        if ch == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = n if end == -1 else end
            continue
        if command.startswith("<<<", i):
            emit("<<<")  # a here-string, not a heredoc
            i += 3
            continue
        # `<<` inside an unclosed `((`/`$((` is a shift (`$((1<<3))`), not a heredoc;
        # taking it for one would hide the lines up to a numeric "terminator".
        if command.startswith("<<", i) and tail.rfind("((") <= tail.rfind("))"):
            m = HEREDOC_RE.match(command, i)
            if m:
                word = m.group(2) or m.group(3) or m.group(4)
                pending.append((word, bool(m.group(1))))
                emit(m.group(0))
                i = m.end()
                continue
        if ch == "\n" and pending:
            # The rest of the `<<WORD` line itself (e.g. `&& git commit` or `| tee out;
            # git push`) was scanned above; the bodies start here, one per operator.
            emit(ch)
            i += 1
            for key in pending:
                if key in unterminated:
                    continue
                word, dash_form = key
                # Real bash only strips *leading tabs* for `<<-`, but any leading
                # whitespace here is a reasonable proxy; without `<<-`, bash requires
                # the terminator at column 0. A trailing `\r` (CRLF body) is tolerated
                # either way.
                indent = r"[ \t]*" if dash_form else ""
                end = re.compile(r"(?m)^" + indent + re.escape(word) + r"[ \t]*\r?$").search(
                    command, i)
                # No terminator: not a real heredoc (e.g. `$((1<<3))`), so keep the
                # text; a spurious check is safer than hiding a later git command.
                if end:
                    i = end.end()
                else:
                    unterminated.add(key)
            pending = []
            continue
        emit(ch)
        i += 1
    return "".join(out)


def _native_path(path):
    """On Windows, translate a Git Bash / MSYS drive path (`/c/Users/me`) to `C:/Users/me`
    and expand `~`, so it can be joined with the payload cwd."""
    path = os.path.expanduser(path)
    if os.name == "nt":
        path = MSYS_DRIVE_RE.sub(lambda m: m.group(1).upper() + ":/", path, count=1)
    return path


def _redact_diff(diff):
    """Replace the hunks of files matching REDACT_FILE_PATTERNS with `[redacted]`,
    keeping each file's header lines (and so its name)."""
    out = []
    mode = "keep"  # "keep", "header" (a redacted file's header lines) or "drop" (its hunks)
    for line in diff.splitlines(keepends=True):
        header = DIFF_HEADER_RE.match(line.rstrip("\r\n"))
        if header or line.startswith("new file: "):  # the latter: untracked-file blocks
            names = [posixpath.basename(name) for name in (header.groups() if header else ())
                     if name]
            redact = any(fnmatch.fnmatchcase(name.lower(), pattern)
                         for name in names for pattern in REDACT_FILE_PATTERNS)
            mode = "header" if redact else "keep"
            out.append(line)
        elif mode == "header":
            if line.startswith(("@@", "Binary files", "GIT binary patch")):
                out.append("[redacted]\n")
                mode = "drop"
            else:
                out.append(line)
        elif mode == "keep":
            out.append(line)
    return "".join(out)


def _scrub(text):
    """Replace common secret token shapes (API keys, tokens, private key blocks)."""
    return SECRET_RE.sub("[redacted]", text)


def _run_git(args, cwd, deadline=None):
    """Run a git subprocess with a per-call timeout bounded by the shared `deadline`
    (a time.monotonic() budget end for the whole gate() invocation); if the budget is
    already exhausted, fail open (return None) rather than block past the hook timeout."""
    timeout = 2.0
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        timeout = max(0.1, min(timeout, remaining))
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    # Decode bytes ourselves: text=True translates CR/CRLF, which corrupts valid
    # NUL-delimited Git filenames and can make retry metadata lookup miss a file.
    return result.stdout.decode("utf-8", "surrogateescape")


def _push_base(cwd, deadline=None):
    """The ref a push is compared against: the upstream, else the push remote's
    (remote.pushDefault, or origin) default branch from `refs/remotes/<remote>/HEAD`,
    else origin/main, else origin/master; None when none resolves."""
    if _run_git(["rev-parse", "--verify", "--quiet", "@{u}"], cwd, deadline) is not None:
        return "@{u}"
    remote = (_run_git(["config", "--get", "remote.pushDefault"], cwd, deadline) or "").strip()
    if not remote or remote.startswith("-"):
        remote = "origin"
    head = _run_git(["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"],
                    cwd, deadline)
    if head and head.strip():
        return head.strip()
    for ref in ("origin/main", "origin/master"):
        if _run_git(["rev-parse", "--verify", "--quiet", ref], cwd, deadline) is not None:
            return ref
    return None


def _diff_range(op, all_flag, cwd, deadline=None, base=None):
    if op == "commit":
        args = ["diff", *DIFF_FORMAT_ARGS] + (["HEAD"] if all_flag else ["--cached"])
        return _run_git(args, cwd, deadline)
    if base is None:
        return None
    return _run_git(["diff", *DIFF_FORMAT_ARGS, f"{base}...HEAD"], cwd, deadline)


def _diff_names(op, all_flag, cwd, deadline=None, base=None):
    if op == "commit":
        args = (["diff", *DIFF_FORMAT_ARGS, "HEAD", "--name-only", "-z"] if all_flag
                else ["diff", *DIFF_FORMAT_ARGS, "--cached", "--name-only", "-z"])
        out = _run_git(args, cwd, deadline)
        return out
    if base is None:
        return None
    return _run_git(["diff", *DIFF_FORMAT_ARGS, f"{base}...HEAD", "--name-only", "-z"], cwd, deadline)


def _untracked_files(cwd, deadline=None):
    out = _run_git(["ls-files", "--others", "--exclude-standard", "--full-name", "-z"],
                   cwd, deadline)
    return [n for n in (out or "").split("\0") if n]


def _dash_c_dir(opts_segment):
    """Parse a literal uppercase `-C <dir>` out of the matched global-options segment
    (shlex-aware so `-C "dir with space"` works; falls back to a plain regex search on
    shlex errors, e.g. unbalanced quotes)."""
    try:
        tokens = shlex.split(opts_segment)
    except ValueError:
        m = re.search(r"-C\s+(\S+)", opts_segment)
        return m.group(1) if m else None
    for i, tok in enumerate(tokens):
        if tok == "-C" and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _cd_prefix_dir(command, git_start):
    """Return the directory the `cd <dir>` segments before the git command lead to
    (successive cds accumulate; relative results stay relative to the payload cwd),
    or None when there is no cd. A -C in the git segment is resolved relative to it."""
    # GIT_COMMAND_RE's leading separator class consumes only one char of a two-char
    # operator (e.g. the second `&` of `&&`), so the prefix can end with a stray
    # separator; strip it before splitting into &&/;/|| segments.
    prefix = command[:git_start].rstrip().rstrip("&|;").rstrip()
    result = None
    # A `(` subshell opener, `{` group or backtick is also a segment separator, so
    # `(cd sub && git commit)` still finds the `cd sub` segment.
    for segment in re.split(r"&&|\|\||;|\n|\(|\{|`", prefix):
        m = CD_RE.match(segment)
        if not m:
            continue
        try:
            tokens = shlex.split(m.group(1))
        except ValueError:
            tokens = m.group(1).split()
        # Drop options (-P, --) and redirections (2>/dev/null) around the directory.
        tokens = [t for t in tokens if not t.startswith("-") and not re.match(r"^\d*[<>]", t)]
        if len(tokens) != 1:
            continue
        target = _native_path(tokens[0])
        result = os.path.join(result, target) if result else target
    return result


def _git_target(command, match, cwd, add_end=-1):
    """(op, cwd, all_flag) for one GIT_COMMAND_RE match in the stripped command.
    `add_end` is where the command's first `git add` match ends (None: there is none;
    -1: search the prefix here)."""
    opts_segment, op = match.group(1), match.group(2)
    # The shell resolves -C relative to any directory an earlier cd moved to.
    cd_dir = _cd_prefix_dir(command, match.start())
    if cd_dir:
        cwd = os.path.join(cwd, cd_dir)
    dash_c_dir = _dash_c_dir(opts_segment)
    if dash_c_dir:
        cwd = os.path.join(cwd, _native_path(dash_c_dir))
    # `git add … && git commit` stages nothing until it runs, so compare the work tree
    # with HEAD instead of the (still empty) index; likewise for commit -a/--all.
    segment = re.split(r"[;&|\n]", command[match.end():], maxsplit=1)[0]
    if add_end == -1:
        add = GIT_ADD_RE.search(command[:match.start() + 1])
        add_end = add.end() if add else None
    all_flag = op == "commit" and (bool(ALL_FLAG_RE.search(segment))
                                   or (add_end is not None and add_end <= match.start() + 1))
    return op, cwd, all_flag


def _scan_targets(raw_command, base_cwd):
    """The distinct (op, cwd, all_flag) targets of every git commit/push in the command.
    Past MAX_GIT_MATCHES matches, later ones are not resolved (each resolution rescans
    the prefix, which is quadratic on a padded command); a push among them still makes
    the operation a push, checked against the first target's directory."""
    command = _strip_heredocs_and_quotes(raw_command)
    add = GIT_ADD_RE.search(command)
    add_end = add.end() if add else None
    targets = []
    for count, match in enumerate(GIT_COMMAND_RE.finditer(command)):
        if count >= MAX_GIT_MATCHES:
            if match.group(2) == "push" and not any(t[0] == "push" for t in targets):
                targets.append(("push", targets[0][1], False))
            continue
        target = _git_target(command, match, base_cwd, add_end)
        if target not in targets:
            targets.append(target)
    return targets


def gate(payload, cfg, classify_fn=None, log_fn=None, now=time.time, state_dir=STATE_DIR):
    """Return the PreToolUse hook output dict, or None to leave the command unchanged.

    Every git commit/push in the command is gated together: with a push anywhere the
    operation is a push, and the diff sent is each commit's pending diff followed by
    each push's unpushed range.

    All git subprocess calls in one invocation share a wall-clock budget
    (GIT_SUBPROCESS_BUDGET_SECONDS) instead of each getting its own 2 s, so a worst
    case of several calls plus the classify_fn deadline can't exceed the hook timeout;
    once the budget is spent, remaining git calls fail open (return None/allow).
    """
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    if not feature_enabled(cfg, "risk_gate"):
        return None
    tool_input = payload.get("tool_input")
    raw_command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(raw_command, str):
        return None
    base_cwd = _native_path(payload.get("cwd") or os.getcwd())
    targets = _scan_targets(raw_command, base_cwd)
    if not targets:
        return None
    op = "push" if any(t[0] == "push" for t in targets) else "commit"
    # Commits run before the push that follows them, so their diffs come first.
    targets.sort(key=lambda t: t[0] == "push")

    deadline = time.monotonic() + GIT_SUBPROCESS_BUDGET_SECONDS
    max_diff_chars = int(cfg.get("max_diff_chars") or 0)
    diff = ""
    names = []
    paths = []  # where each name's mtime is read for critic coverage
    names_failed = False
    untracked_overflow = False
    untracked_identity = []
    for target_op, cwd, all_flag in targets:
        base = _push_base(cwd, deadline) if target_op == "push" else None
        part = _diff_range(target_op, all_flag, cwd, deadline, base)
        if part is None:
            continue
        names_out = _diff_names(target_op, all_flag, cwd, deadline, base)
        names_failed = names_failed or names_out is None
        part_names = [n for n in (names_out or "").split("\0") if n]
        toplevel = _run_git(["rev-parse", "--show-toplevel"], cwd, deadline)
        toplevel = toplevel.strip() if toplevel else None
        diff += part
        for name in part_names:
            names.append(name)
            paths.append(Path(toplevel) / name if toplevel else Path(name))

        # `git add … && git commit` / commit -a/--all diff the work tree against HEAD,
        # which misses brand-new untracked files; fold those in as synthetic diff blocks.
        if not all_flag:
            continue
        seen = set(part_names)
        untracked = _untracked_files(cwd, deadline)
        untracked_overflow = untracked_overflow or len(untracked) > MAX_UNTRACKED
        for name in untracked[:MAX_UNTRACKED]:
            if name in seen:
                continue
            seen.add(name)
            # Always track the name so its mtime counts for critic coverage, even
            # once the diff text is full.
            file_path = Path(toplevel) / name if toplevel else Path(cwd) / name
            names.append(name)
            paths.append(file_path)
            try:
                info = file_path.lstat()  # never follow an untracked symlink
                untracked_identity.append((name, info.st_mode, info.st_size,
                                           info.st_mtime_ns, info.st_ctime_ns))
            except OSError:
                untracked_identity.append((name, None))
            room = min(max_diff_chars or 1 << 16, 1 << 16) - len(diff)
            if room <= 0:
                continue
            # Untracked paths can be symlinks to files outside the repository.
            # Keep the name for review, but never open their contents here.
            diff += f"new file: {name}\n[untracked content omitted]\n"[:room]

    if not diff:
        return None

    session_id = payload.get("session_id")
    state = load_session_state(session_id, state_dir)

    critic_ts = state.get("critic_ts")
    covered = False
    if isinstance(critic_ts, (int, float)):
        mtimes = []
        all_stat_ok = True
        for path in paths:
            try:
                mtimes.append(path.lstat().st_mtime)
            except OSError:
                # Deleted (or otherwise unreadable) files have no mtime to compare
                # against critic_ts, so treat them as needing a fresh review.
                all_stat_ok = False
                break
        covered = (not names_failed and all_stat_ok and not untracked_overflow
                   and (critic_ts >= max(mtimes) if mtimes else True))

    # Keep local metadata in the retry identity so editing an omitted untracked
    # file requires a new check, without transmitting its contents to Jev.
    retry_identity = (diff, untracked_identity)
    diff_hash = hashlib.sha256(repr(retry_identity).encode("utf-8", "replace")).hexdigest()
    start = time.monotonic()

    def log(decision, choice=None, confidence=None, needs_review_p=None, **extra):
        if not log_fn:
            return
        entry = {"ts": timestamp(), "feature": "risk_gate", "op": op, "decision": decision,
                 "risk": choice, "confidence": confidence, "needs_review": needs_review_p,
                 "files": len(names), "diff_chars": len(diff), "latency_ms": elapsed_ms(start)}
        entry.update(extra)
        log_fn(entry)

    if covered:
        log("covered")
        return None

    denied = state.get("denied") or []
    if diff_hash in denied:
        log("override")
        return None

    ask_state = {"operation": op, "files": names[:200]}
    if cfg.get("send_diff"):
        # Hunks of secret-looking files and common token shapes never leave the machine.
        ask_state["diff"] = _scrub(_redact_diff(diff))[: cfg["max_diff_chars"]]
    questions = {
        "risk": {"type": "choice", "instructions": RISK_INSTRUCTIONS, "criteria": RISK_CRITERIA},
        "needs_review": {"type": "noul", "instructions": NEEDS_REVIEW_INSTRUCTIONS},
    }
    errs = []
    answers = ask(cfg, "risk_gate", ask_state, questions, classify_fn, errors=errs)
    if answers is None:
        if errs:
            log("allow", reason="unavailable", error=errs[0])
        else:
            log("allow")
        return None
    try:
        choice = answers["risk"]["choice"]
    except Exception:
        choice = None
    if not isinstance(choice, str):
        log("allow")
        return None
    # Confidence is only logged, so a missing, null or non-finite one doesn't skip the gate.
    try:
        confidence = float(answers["risk"].get("confidence"))
    except Exception:
        confidence = None
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        confidence = None
    needs_review_p = noul(answers, "needs_review")

    risky = (choice != "none" and needs_review_p is not None
             and needs_review_p >= cfg["risk_min_probability"])
    if not risky:
        log("allow", choice, confidence, needs_review_p)
        return None

    def add_denied(s):
        s["denied"] = ((s.get("denied") or []) + [diff_hash])[-20:]

    extra = {}
    try:
        update_state(session_state_path(session_id, state_dir), add_denied)
    except Exception as exc:
        # Still deny: without the saved hash, the identical retry is simply checked again.
        extra["state_error"] = type(exc).__name__
    log("deny", choice, confidence, needs_review_p, **extra)
    reason = (f"[jev risk gate] This {op} looks risky ({choice}, p={needs_review_p:.2f}) and no "
              "critic has reviewed these files since they last changed. Run the critic subagent "
              "on this diff per CLAUDE.md, resolve its findings, then retry. If review is not "
              "warranted, rerun the identical command once to proceed.")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _extract_report_text(tool_response):
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        content = tool_response.get("content")
        if isinstance(content, list):
            return "".join(item.get("text", "") for item in content
                            if isinstance(item, dict) and item.get("type") == "text")
        for key in ("result", "text"):
            value = tool_response.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(tool_response, list):
        parts = []
        for item in tool_response:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _parse_sections(text):
    matches = list(SECTION_RE.finditer(text))
    sections = {}
    for i, m in enumerate(matches):
        name = m.group(1).upper()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[m.end():end].strip()
    return sections


def _analyze_report(text, cfg, classify_fn=None, confidence_heuristic=True, errors=None):
    """Parse RESULT/EVIDENCE/CONFIDENCE/UNVERIFIED sections and ask Jev whether the
    report is weak. Returns (reasons, reason_codes, supported, material_gap).

    `confidence_heuristic` gates the self-reported low/medium CONFIDENCE check: it
    applies to the PostToolUse agent-done nudge (a foreground report can be told to
    verify or escalate more), but not to the SubagentHandback deny path (a read-only
    critic can't escalate, so low/medium confidence alone must not block it).

    Sections are parsed from the FULL text (a long report's RESULT/EVIDENCE/CONFIDENCE/
    UNVERIFIED block may come after `max_report_chars` of findings), so truncation is
    only applied to what's actually sent to the classifier below, after common token
    shapes are scrubbed. `errors` is passed to `ask` (a failed call appends its reason)."""
    max_chars = cfg["max_report_chars"]

    sections = _parse_sections(text)
    reasons = []
    missing = [name for name in SECTION_NAMES if name not in sections or not sections[name]]
    if missing:
        reasons.append(f"missing {' '.join(missing)}")
    if confidence_heuristic:
        # Only the first word is the level; later prose ("high; low risk of …") isn't.
        words = sections.get("CONFIDENCE", "").split(maxsplit=1)
        level = words[0].strip("*_.,;:()[]").lower() if words else ""
        if level in ("low", "medium"):
            reasons.append(f"self-reported {level} confidence")

    supported = None
    material_gap = None
    if not missing:
        quarter = max_chars // 4
        ask_state = {
            "result": _scrub(sections["RESULT"])[:quarter],
            "evidence": _scrub(sections["EVIDENCE"])[:quarter],
            "unverified": _scrub(sections["UNVERIFIED"])[:quarter],
            "confidence": _scrub(sections["CONFIDENCE"])[:quarter],
        }
        questions = {
            "supported": {"type": "noul", "instructions": SUPPORTED_INSTRUCTIONS},
            "material_gap": {"type": "noul", "instructions": MATERIAL_GAP_INSTRUCTIONS},
        }
        answers = ask(cfg, "report_check", ask_state, questions, classify_fn, errors=errors)
        if answers is not None:
            supported = noul(answers, "supported")
            material_gap = noul(answers, "material_gap")
            if supported is not None and supported < cfg["report_min_support"]:
                reasons.append(f"evidence weakly supports result (p={supported:.2f})")
            if material_gap is not None and material_gap >= cfg["report_max_gap"]:
                reasons.append(f"material unverified claims (p={material_gap:.2f})")

    reason_codes = []
    for r in reasons:
        if r.startswith("missing"):
            reason_codes.append("missing")
        elif "confidence" in r:
            reason_codes.append("low_confidence")
        elif "weakly supports" in r:
            reason_codes.append("weak_evidence")
        elif "material unverified" in r:
            reason_codes.append("material_gap")
    return reasons, reason_codes, supported, material_gap


def handback(payload, cfg, classify_fn=None, log_fn=None, state_dir=STATE_DIR):
    """Return the PreToolUse hook output dict for a SubagentHandback call, or None."""
    if not isinstance(payload, dict):
        return None
    if not feature_enabled(cfg, "report_check"):
        return None
    role = payload.get("agent_type")
    if role not in cfg["report_roles"]:
        return None
    tool_input = payload.get("tool_input")
    message = tool_input.get("message") if isinstance(tool_input, dict) else None
    if not isinstance(message, str):
        return None

    session_id = payload.get("session_id")
    agent_id = payload.get("agent_id")
    start = time.monotonic()
    errs = []
    reasons, reason_codes, supported, material_gap = _analyze_report(
        message, cfg, classify_fn, confidence_heuristic=False, errors=errs)

    def log(decision):
        if not log_fn:
            return
        entry = {"ts": timestamp(), "feature": "report_check", "event": "handback",
                 "decision": decision, "subagent_type": role, "weak": bool(reasons),
                 "reasons": reason_codes, "supported": supported, "material_gap": material_gap,
                 "latency_ms": elapsed_ms(start)}
        if errs:
            entry.update(reason="unavailable", error=errs[0])
        log_fn(entry)

    if not reasons:
        log("ok")
        return None

    state = load_session_state(session_id, state_dir)
    key = hashlib.sha256(f"{agent_id}\0{message}".encode("utf-8", "replace")).hexdigest()
    denied = state.get("handback_denied") or []
    if key in denied:
        log("override")
        return None

    def add_handback_denied(s):
        s["handback_denied"] = ((s.get("handback_denied") or []) + [key])[-50:]

    try:
        update_state(session_state_path(session_id, state_dir), add_handback_denied)
    except Exception:
        pass  # still deny; without the saved key the identical resend is checked again
    log("deny")
    reason = (f"[jev report check] Your report looks weak: {'; '.join(reasons)}. Before handing "
              "back, verify the key claims (run the check and cite the command and exit code) or "
              "list what remains under UNVERIFIED, using RESULT / EVIDENCE / CONFIDENCE / "
              "UNVERIFIED. If the report is already accurate, call SubagentHandback again with "
              "the same message to proceed.")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _prune_critic_started(started, now_value):
    """Drop critic_started entries older than 24h (a launch that never hands back or
    completes shouldn't grow the state file forever)."""
    return {agent_id: ts for agent_id, ts in started.items()
            if isinstance(ts, (int, float)) and now_value - ts < 86400}


def agent_done(payload, cfg, classify_fn=None, log_fn=None, now=time.time, state_dir=STATE_DIR):
    """Return the PostToolUse hook output dict, or None.

    critic_ts is stamped from when the critic *started* (its Agent/Task launch), not
    when it finished: files edited while the critic was running were not necessarily
    seen by it, so they must still need a fresh review once it comes back. `critic_started` (a per-agent-id map of launch
    timestamps under session state) bridges the async-launch PostToolUse event to the
    later SubagentHandback or completed-foreground-result event.
    """
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name")
    session_id = payload.get("session_id")

    if tool_name == "SubagentHandback":
        if payload.get("agent_type") == "critic" and feature_enabled(cfg, "risk_gate"):
            agent_id = payload.get("agent_id")
            now_value = now()

            def set_critic_ts(s):
                started = _prune_critic_started(dict(s.get("critic_started") or {}), now_value)
                if agent_id in started:
                    launched = started.pop(agent_id)
                elif started:
                    # The id is missing/unknown but a critic launch is still in flight:
                    # use its earliest launch rather than assuming "now" (uncovered).
                    # Read without popping, so that critic's own later report still finds it.
                    launched = min(started.values())
                else:
                    launched = now_value
                # A late hand-back from an earlier critic must not undo a later one's review.
                s["critic_ts"] = max(s.get("critic_ts") or 0, launched)
                s["critic_started"] = started

            try:
                update_state(session_state_path(session_id, state_dir), set_critic_ts)
            except Exception:
                pass  # persisting critic timestamps is best effort
        return None

    if tool_name not in ("Agent", "Task"):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    tool_response = payload.get("tool_response")
    subagent_type = tool_input.get("subagent_type")
    if isinstance(tool_response, dict):
        status = tool_response.get("status")
        if status == "async_launched":
            if subagent_type == "critic" and feature_enabled(cfg, "risk_gate"):
                agent_id = tool_response.get("agentId") or tool_response.get("agent_id")
                now_value = now()

                def add_critic_started(s):
                    started = _prune_critic_started(dict(s.get("critic_started") or {}), now_value)
                    if agent_id is not None:
                        started[agent_id] = now_value
                    s["critic_started"] = started

                try:
                    update_state(session_state_path(session_id, state_dir), add_critic_started)
                except Exception:
                    pass  # persisting critic timestamps is best effort
            return None
        if status is not None and status != "completed":
            return None
        if status is None and not tool_response.get("content"):
            return None
    completed = tool_response

    if subagent_type == "critic" and feature_enabled(cfg, "risk_gate"):
        agent_id = (tool_response.get("agentId") if isinstance(tool_response, dict) else None) \
            or payload.get("agent_id")
        total_duration_ms = None
        if isinstance(tool_response, dict):
            total_duration_ms = tool_response.get("totalDurationMs")
        now_value = now()

        def set_critic_ts_completed(s):
            started = _prune_critic_started(dict(s.get("critic_started") or {}), now_value)
            if agent_id in started:
                launched = started.pop(agent_id)
            elif started:
                # The id is missing/unknown but a critic launch is still in flight:
                # use its earliest launch rather than assuming "now" (uncovered).
                # Read without popping, so that critic's own later report still finds it.
                launched = min(started.values())
            elif isinstance(total_duration_ms, (int, float)):
                launched = now_value - total_duration_ms / 1000.0
            else:
                launched = now_value
            s["critic_ts"] = max(s.get("critic_ts") or 0, launched)
            s["critic_started"] = started

        try:
            update_state(session_state_path(session_id, state_dir), set_critic_ts_completed)
        except Exception:
            pass  # persisting critic timestamps is best effort

    if not feature_enabled(cfg, "report_check"):
        return None
    if subagent_type not in cfg["report_roles"]:
        return None

    text = _extract_report_text(completed)
    start = time.monotonic()
    errs = []
    reasons, reason_codes, supported, material_gap = _analyze_report(
        text, cfg, classify_fn, errors=errs)

    if log_fn:
        entry = {"ts": timestamp(), "feature": "report_check", "event": "agent_done",
                 "decision": "weak" if reasons else "ok", "subagent_type": subagent_type,
                 "model": tool_input.get("model"), "weak": bool(reasons), "reasons": reason_codes,
                 "supported": supported, "material_gap": material_gap, "latency_ms": elapsed_ms(start)}
        desc_hash = _desc_hash(tool_input.get("description"))
        if desc_hash:
            entry["desc_hash"] = desc_hash
        if errs:
            entry.update(reason="unavailable", error=errs[0])
        log_fn(entry)

    if not reasons:
        return None
    role = subagent_type
    message = (f"[jev report check] The {role} report looks weak: {'; '.join(reasons)}. Per "
               "CLAUDE.md, verify its key claims directly or escalate one tier (scout/runner → "
               "builder/main session; builder → main session) instead of retrying the same way.")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}


def main():
    try:
        mode = sys.argv[1] if len(sys.argv) > 1 else ""
        # Hook payloads are UTF-8 whatever the locale (e.g. cp1255 on Windows).
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        cfg = load_config()
        if mode == "gate":
            output = gate(payload, cfg, log_fn=lambda entry: write_log(cfg, entry))
        elif mode == "handback":
            output = handback(payload, cfg, log_fn=lambda entry: write_log(cfg, entry))
        elif mode == "agent-done":
            output = agent_done(payload, cfg, log_fn=lambda entry: write_log(cfg, entry))
        else:
            output = None
        if output is not None:
            sys.stdout.write(json.dumps(output))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

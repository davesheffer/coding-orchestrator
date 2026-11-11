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
call unchanged, except that git pushes fail closed: a git push the classifier
could not judge (no key, disallowed endpoint, error, timeout, malformed answer)
is denied, and under Claude Code (this script's main(), not the Codex adapter)
a gate crash denies any command that looks like a git push, heroku or deploy
command. All Jev calls run under a hard wall-clock deadline of
min(timeout_seconds, 4) seconds. Each hook invocation also shares one
monotonic deadline (GATE_BUDGET_SECONDS for `gate`, SHORT_HOOK_BUDGET_SECONDS
for `handback`/`agent-done`) across its git subprocesses, classifier call and
state-lock wait: all git subprocesses in one `gate()` call share at most
GIT_SUBPROCESS_BUDGET_SECONDS of it, the classifier gets only what remains,
and `update_state` stops waiting for the lock at the deadline. Git or
classifier work still pending when it passes fails open, and a lock wait that
runs out only skips that state update, so the script finishes inside the hook
timeouts in claude/install.py (10 s for gate, 5 s for the others).
Nothing here logs diff text, file names, commands, or report text.
"""
import bisect
import contextvars
import errno
import fnmatch
import hashlib
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import time
import warnings
from pathlib import Path

try:
    import fcntl
except ImportError:  # e.g. Windows: update_state locks with msvcrt instead.
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
# Each hook invocation's shared monotonic deadline (git, classifier and state-lock
# wait together). Below the 10 s (gate) and 5 s (handback, agent-done) hook timeouts
# in claude/install.py, leaving margin for interpreter start-up.
GATE_BUDGET_SECONDS = 8.0
SHORT_HOOK_BUDGET_SECONDS = 4.0
# How long update_state waits for the Windows lock before giving up (the hook fails
# open; never past the hook's deadline), and how often a Windows tmp.replace is
# retried while another process has the state file open.
MSVCRT_LOCK_SECONDS = 2.0
# The errnos a non-blocking lock attempt raises when another process holds the lock;
# any other OSError is re-raised at once instead of being retried until the deadline.
FLOCK_BUSY_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
MSVCRT_BUSY_ERRNOS = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EACCES)}
REPLACE_ATTEMPTS = 5
REPLACE_RETRY_SECONDS = 0.05

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_client import (  # noqa: E402  (re-exported for callers and tests)
    DEFAULTS, ROOT, api_key_problem, api_key_source_problem, ask, coerce_confidence, config_number, elapsed_ms,
    feature_enabled, load_config, noul, resolve_executable, timestamp, write_log)

def _char_limit(cfg, key):
    """cfg[key] as a non-negative whole number of characters; junk falls back to its default."""
    value = config_number(cfg.get(key), 0.0, float(1 << 40))
    return int(value) if value is not None and value.is_integer() else DEFAULTS[key]


STATE_DIR = ROOT / "relay" / "state"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Global options git accepts before the subcommand: -C <dir>, -c <key>=<value>, a
# space-separated long option that takes a value (--git-dir, --work-tree, --namespace,
# --super-prefix, --config-env, --attr-source), or any other --long-option (with or
# without =value).
# Only a literal -C sets cwd (see _dash_c_dir); -c and --long-options are matched so
# they don't get mistaken for the subcommand. The arg may be a quoted string (e.g. -C
# "dir with space") or a bare \S+ token. The alternatives start with different chars
# (`"`, `'`, anything else), so a quoted value has exactly one way to match; with a
# plain \S+ alternative it had two, and repeated `-C "a"` options backtracked
# exponentially. A double-quoted value honours backslash escapes (`-C "a\" b"`), and
# its two inner branches start with different chars too. Outside quotes a backslash
# escapes the next char, blank included (`-C a\ b`), so an escape pair is part of the
# value; a pair and a plain char start differently, so the value still has one way to
# match. A backslash before a newline (a line continuation) or at the end is a char of
# its own, as before.
_OPT_ESCAPE = r"\\(?:.|(?![^\n]))"
_OPT_TAIL = r"(?:" + _OPT_ESCAPE + r"|[^\s\\])*"
_OPT_ARG = (r"""(?:"(?:[^"\\]|\\[\s\S])*"|'[^']*'|""" + _OPT_ESCAPE + r"""|[^\s"'\\])"""
            + _OPT_TAIL)
_LONG_VALUE_OPTS = (r"(?:--git-dir|--work-tree|--namespace|--super-prefix|--config-env"
                    r"|--attr-source)")
# The value-taking names are excluded from the generic --long-option branch, and their
# space-separated branch always takes the next token as the value (even `--git-dir -x`),
# so each token has exactly one way to match; overlapping branches here backtrack
# exponentially on repeated options. The no-argument short options `-p`/`-P`
# (--paginate/--no-pager) are their own branch: `git -P push` is a push.
GIT_GLOBAL_OPTS = (r"(?:\s+(?:-C\s+" + _OPT_ARG + r"|-c\s+" + _OPT_ARG + r"|-[pP](?=\s)|"
                    + _LONG_VALUE_OPTS + r"(?:=(?:" + _OPT_ARG + r")?|\s+" + _OPT_ARG + r")|"
                    r"--(?!(?:git-dir|work-tree|namespace|super-prefix|config-env|attr-source)"
                    r"(?![\w-]))"
                    r"[\w-]+(?:=" + _OPT_TAIL + r")?))*")
# What may start a command: the start, a ; & | ( operator, a newline, a `{` group or
# a backtick substitution, or a `)` (so `X=$(date) git push`, whose value the env
# prefix below can't span, is still seen). Only blanks follow it (_LEAD): a newline is a separator in
# its own right, and `\s*` there let each newline of a long run re-consume the rest of
# the run (quadratic).
_SEP = r"(?:^|[;&|()\n{`])"
_LEAD = r"[ \t]*"
# Env-assignment (e.g. `GIT_EDITOR=true git commit`, `A=1 B=2 git push`), shell keyword
# (if/elif/then/else/while/until/do/`!`) and wrapper (time/exec/command/env/nice/nohup/
# stdbuf/doas/sudo/xargs/winpty, and `timeout` with its duration; with their -flags, a numeric
# flag value such as `nice -n 5`, or a word value of a one-letter flag such as `sudo -u
# bob`) prefixes before the `git` invocation itself.
# Only blanks separate them: a newline ends the statement (and is a _SEP itself), and
# `\s+` here let each line of `A=b\n` * N re-consume the rest of the run (quadratic).
# A value may hold backslash escapes (`X=a\ b git push`); an escape pair and a plain
# char start differently, so the value still has one way to match. A word flag value
# can't be a number (the numeric branch's), start with `-`, or be a prefix keyword or
# assignment (the next prefix's), so `-u time` has one reading and a run of them can't
# backtrack exponentially; only a command word (`sudo -u git git push`) reads both ways,
# and that ends the prefix.
_PREFIX_WORDS = (r"(?:if|elif|then|do|else|while|until|!|time|exec|command|env|nice|nohup"
                 r"|stdbuf|doas|sudo|xargs|winpty)")
_PREFIX_FLAGS = (r"(?:-[\w-]+(?:[ \t]+\d[\w.]*)?[ \t]+"
                 r"|-[A-Za-z][ \t]+(?!(?:" + _PREFIX_WORDS + r"|timeout)[ \t]|[A-Za-z_]\w*=)"
                 r"""[^\s\d;&|()`'"!-][^\s;&|()`'"]*[ \t]+)*""")
_ENV_PREFIX = (r"(?:" + _PREFIX_WORDS + r"[ \t]+" + _PREFIX_FLAGS
               + r"|timeout[ \t]+" + _PREFIX_FLAGS + r"\d[\w.]*[ \t]+"
               r"|[A-Za-z_]\w*=(?:\\.|[^\s;&|()`\\])*[ \t]+)*")
# `git`, `git.exe`, or a path to either (`/usr/bin/git`, `C:\Git\cmd\git.exe`, `/opt/{x}/git`),
# in any case (`GIT.EXE`). The leading path segment excludes blanks, shell
# separators/quotes/backtick (rather than `\S*`) so it can never backtrack across a
# command boundary looking for "git" — the other half of the scanner's quadratic
# behaviour fixed in _strip_heredocs_and_quotes below. It may hold `=` (`/opt/a=b/git`)
# but not start with a `NAME=`, so `X=/usr/bin/git push` (which runs `push`) is not
# taken for git. A quoted path (`"/opt/git(1)/bin/git"`) is reduced to a bare `git` by
# the stripper instead (see _GIT_WORD_RE).
_GIT = r"""(?:(?![A-Za-z_]\w*=)[^\s;&|(){`'"]*[/\\])?(?i:git(?:\.exe)?)"""
GIT_COMMAND_RE = re.compile(
    _SEP + _LEAD + _ENV_PREFIX + _GIT + r"(" + GIT_GLOBAL_OPTS + r")\s+(commit|push)\b")
GIT_ADD_RE = re.compile(_SEP + _LEAD + _ENV_PREFIX + _GIT + GIT_GLOBAL_OPTS + r"\s+add\b")
# Git Bash / MSYS drive paths (`/c/Users/...`), translated to `C:/...` on Windows.
MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/|$)")
# -a/--all, or a short-flag cluster containing a (e.g. -am), within the commit segment.
ALL_FLAG_RE = re.compile(r"(?:^|\s)(--all|-[A-Za-z]*a[A-Za-z]*)(?=\s|$)")
# The same flag right at a position (ALL_FLAG_RE's `^` on a slice starting there).
_ALL_FLAG_AT_RE = re.compile(r"(?:--all|-[A-Za-z]*a[A-Za-z]*)(?=\s|$)")
# `cd <dir>` as its own segment (split on &&, ;, ||, or a `(` subshell opener) before
# the git segment; also `pushd`, `chdir` and PowerShell's `Set-Location`/`sl`.
_CD_VERB = r"(?:cd|pushd|chdir|sl|(?i:set-location))"
_CD_VERB_ENDS = ("cd", "pushd", "chdir", "sl", "set-location")  # for _may_keep
CD_RE = re.compile(r"^\s*(?:(?:then|else|do)[ \t]+)?" + _CD_VERB + r"\s+(.+?)\s*$")
# A heredoc operator (`<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`, `<<\WORD`); the
# scanner in _strip_heredocs_and_quotes only tries it outside quotes and comments, and
# never on a here-string (`<<<`). Group 1 captures a leading `-` (the `<<-` form, which
# lets bash indent the terminator with tabs); the word itself may contain `.` and `-`
# (e.g. `<<EOF-1`, `<<END.MARKER`), not just `\w`.
HEREDOC_RE = re.compile(r"<<(-)?[ \t]*(?:'([\w.-]+)'|\"([\w.-]+)\"|\\?([\w.-]+))")
# The same operator for _drop_inert_text, which reads the delimiter word as bash does
# (see _heredoc_word): plain chars, quoted pieces and escaped chars, in any mix.
_HEREDOC_OP_RE = re.compile(r"<<(-)?[ \t]*")
_HEREDOC_WORD_RE = re.compile(r"""(?:[\w.-]|'[\w.-]*'|"[\w.-]*"|\\[\w.-])+""")
_HEREDOC_QUOTING_RE = re.compile(r"""['"\\]""")
# What may follow a delimiter word; anything else is part of a word this can't read.
_HEREDOC_WORD_END = ("", " ", "\t", "\r", "\n", ";", "&", "|", "(", ")", "<", ">", "`")
# Longer delimiter words, and more heredocs on one line, are not looked for: the text
# from there on is kept as it is (see _drop_inert_text).
MAX_HEREDOC_WORD_CHARS = 128
MAX_PENDING_HEREDOCS = 32
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
# A quoted string is preserved only when it is the value of a literal `-C`/`-c` (or
# of a value-taking long option such as `--git-dir "x"`, whose dropped value would
# otherwise let the option swallow the subcommand, and whose --git-dir/--work-tree
# value _git_target reads back) that
# sits in a git global-options segment (i.e. `git`, zero or more already-matched global
# options, then `-C `/`-c `, right before the quote) — not an arbitrary `-c "..."` in
# unrelated text (e.g. `echo -c "x; git commit -m y"`).
_GIT_DASH_C_PREFIX_RE = re.compile(
    _SEP + _LEAD + _ENV_PREFIX + _GIT + GIT_GLOBAL_OPTS
    + r"\s+(?:-[Cc]\s+|" + _LONG_VALUE_OPTS + r"(?:=|\s+))$")
# `cd`, any options (`cd -- "dir"`, `cd -P "dir"`), then the quoted target. Each
# option needs a leading `-` after its blanks, so a blank run has one way to split.
_CD_WORD = _CD_VERB + r"(?:[ \t]+-[\w-]*)*"
_CD_PREFIX_RE = re.compile(_SEP + _LEAD + r"(?:(?:then|else|do)[ \t]+)?" + _CD_WORD + r"[ \t]+$")
# A kept quoted `-c` value (or one after any token ending in `-c`, e.g. `--foo-c`) is
# never read back (only -C, cd, --git-dir and --work-tree values are): it is emitted as
# `""`, so e.g. a newline-and-`cd` or a `; git push` inside it can't be taken for a
# real command.
_OPAQUE_VALUE_RE = re.compile(r"-c\s+$")
# Once the window has been trimmed, the start of a long git segment (e.g. `git -c
# x=<500 chars> -C "dir" push`) may have fallen out of it; a quote after any `-C `,
# `-c ` or `cd ` is then kept, since dropping it would turn `-C "dir" push` into
# `-C  push` and hide the push (a spurious check is the safe side).
_LOOSE_KEEP_RE = re.compile(
    r"(?:(?:-[Cc]|(?<![\w-])" + _CD_WORD + r")\s+|" + _LONG_VALUE_OPTS + r"(?:=|\s+))$")
# Start of a command word (the quoted-word case in _strip_heredocs_and_quotes).
_COMMAND_POS_RE = re.compile(_SEP + _LEAD + _ENV_PREFIX + r"$")
# The contents of a quoted command word that names git (`"C:\Program Files\Git\cmd\git.exe"`,
# `'git'`); the stripper emits a bare `git` for it. `.*` backtracks only to the last
# separator, so the fullmatch is linear in the quoted length.
_GIT_WORD_RE = re.compile(r"(?i)(?:.*[/\\])?git(?:\.exe)?")
# What separates the segments _cd_dirs looks for `cd <dir>` in (group 1; `)` and a
# closing backtick also restore the cwd of the matching opener). An escape pair or a
# whole quoted string (a kept -C/cd value) is matched first and skipped, so a separator
# inside `-C "\ncd sub\n"` doesn't split; an unterminated quote falls through char by
# char. Linear: the alternatives start with different chars, and once a quote fails to
# close, no later quote of that kind can (the scan pairs escapes the same way from
# there), so at most one `"` and one `'` rescan to the end.
# Group 2 is a candidate `case`/`esac` keyword (see _case_step), which splits nothing.
_CD_SPLIT_RE = re.compile(
    r"""\\[\s\S]|"(?:[^"\\]|\\[\s\S])*"|'[^']*'|(&&|\|\||(?<![|<>;])&(?![>|])|;|\n|\(|\)|\{|`)|(case|esac)""")
# Longest directory _cd_dirs accumulates (`cd a; ` * N would otherwise grow it, and the
# time and memory to build it, quadratically); see _cd_segment_dir.
MAX_CD_PATH_CHARS = 4096
# Most cwds _case_branch_dirs carries at once (the cwd before a case and its first
# branch ends), so a cd costs at most this many joins.
MAX_CASE_BRANCHES = 16
# Most `||` operators with a cd next to them whose every success/failure combination
# _scan_targets enumerates (2**k readings); the gate denies above this bound.
MAX_OR_ENUM = 8
MAX_SUBST_DEPTH = 8
MAX_SCAN_WORK = 4_000_000
# Most distinct targets one command may have; past it _scan_targets raises
# TooManyTargets and the gate denies (a pathological command otherwise reaches tens of
# thousands of targets and hundreds of MB).
MAX_TARGETS = 256
# How deep a heredoc body's `$( )` commands are themselves scanned for heredocs.
MAX_HEREDOC_DEPTH = 3


class TooManyTargets(Exception):
    """A command exceeds a target, nesting, or analysis-work bound."""

# A backslash-newline inside an operator (`<\` newline `<EOF`, `$\` newline `(`) is
# removed by bash before the operator is read, so _scan_targets also scans the
# command with it joined; a `<<`
# split this way is still a heredoc. The lookbehind anchors right after an operator
# char, so an escaped backslash (`\\` + newline) is not joined and only the first
# `\`-newline of a run can start a match (linear). _HEREDOC_SPLIT_RE then joins the
# ones between a (now whole) `<<`, its `-` and blanks, and the word.
_SPLIT_OPERATOR_RE = re.compile(r"(?<=[<$()])(?:\\\n)+(?=[<$(\[)])")
_HEREDOC_SPLIT_RE = re.compile(r"<<(?:\\\n)*-?(?:[ \t]|\\\n)*")
# A line that could end a heredoc (`WORD`, or indented for the `<<-` form).
_TERMINATOR_LINE_RE = re.compile(r"(?m)^([ \t]*)([\w.-]+)[ \t]*\r?$")
# Staged files whose hunks are never sent to Jev (matched case-insensitively against every
# component of the file's path, directories included, so `secrets/db.yaml` matches too);
# only the file name and a `[redacted]` marker go out.
REDACT_FILE_PATTERNS = (".env*", "*.env", "*.pem", "*.key", "*secret*", "id_rsa*", "*.p12", "*.pfx",
                         "credentials*", "*.jks", "*.keystore", ".netrc", "_netrc", ".npmrc",
                         ".pypirc", ".git-credentials", ".pgpass", ".htpasswd", "*.ppk",
                         "*.tfvars", "*.kdbx", "id_dsa*", "id_ecdsa*", "id_ed25519*")
# Token shapes scrubbed from diff and report text before it is sent. A key body may not
# contain another marker or cross a hunk/file boundary, so each BEGIN scans only up to the
# next one (linear time). npm tokens, JWTs (header and payload both start `eyJ`, i.e.
# base64 `{"`) and `scheme://user:password@` URL credentials (the user may be empty, or
# be a 20+ char token with no password: `https://<token>@host`) are also scrubbed; every
# alternative after the key block has a fixed literal start and bounded or [\w-]-only
# repetition that can't run past the next start, so a failed attempt never rescans.
SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
    r"(?:(?!-----(?:BEGIN|END) |\ndiff --git |\n@@).)*"
    r"-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
    r"|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[\w-]{10,}|\bnpm_[A-Za-z0-9]{36}\b"
    r"|(?<![\w-])eyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}"
    r"|://(?:[^\s:/@]{0,256}:[^\s@/]{1,256}|[A-Za-z0-9_-]{20,256})@", re.DOTALL)
# Private key markers left unpaired after SECRET_RE, e.g. a hunk that edits only one end.
KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
KEY_END_RE = re.compile(r"-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
# A line (after an optional diff prefix and indentation) that looks like key body; an
# unpaired marker next to one redacts to its hunk's end/start, otherwise only itself.
KEY_LINE_RE = re.compile(r"[+\- ]?[ \t]*[A-Za-z0-9+/=]{16,}[ \t\r\n]*$")
# A base64-only line of any length, e.g. a short key body after encrypted PEM headers.
BASE64_LINE_RE = re.compile(r"[+\- ]?[ \t]*[A-Za-z0-9+/=]+[ \t\r\n]*$")
# A PGP armor checksum line (`=` and 4 base64 chars), which ends a key body like its last line.
CRC_LINE_RE = re.compile(r"[+\- ]?[ \t]*=[A-Za-z0-9+/]{4}[ \t\r\n]*$")
# An encrypted PEM header line (`Proc-Type: 4,ENCRYPTED`, `DEK-Info: <cipher>,<IV>`) or other
# `Name: value` line, which a BEGIN marker's block may hold before its first key line.
KEY_HEADER_LINE_RE = re.compile(r"[+\- ]?[ \t]*[A-Za-z][A-Za-z0-9-]*:")
# Base64 and escape text written backwards from an END marker that follows a literal `\n`
# (a one-line key fragment such as JSON `"...body\ntail\n-----END ... KEY-----\n"`).
ESCAPED_KEY_TEXT_RE = re.compile(r"[A-Za-z0-9+/=\\]*")
# ... and forwards from a BEGIN marker followed by a literal `\n` (`"-----BEGIN ...\nbody\n"`).
ESCAPED_BEGIN_TEXT_RE = re.compile(r"[ \t]*(?:\\r)?\\n[A-Za-z0-9+/=\\]*")
# Where _scrub splits text into hunks, so an unpaired marker redacts only its own hunk.
HUNK_SPLIT_RE = re.compile(r"(?m)^(?=@@|diff --git )")
# Any long base64 run (key body, even mid-line or indented), scrubbed anywhere; a diff
# line's leading `+`/`-` is kept. Runs with base64url `-`/`_` (JWK, tokens) are redacted
# only if they mix upper, lower and digits or hold a standard run, sparing identifiers.
# The `n` of a literal `\n` escape is kept before a run, like the diff `+`/`-`.
BASE64_RUN_RE = re.compile(
    r"(?m)(^[+-]|(?<=\\)n|(?<![\w+/=-]))[\w+/=-]{40,}(?![\w+/=-])", re.ASCII)
STD_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{40}")
# A base64-only line of any length (a key's last body line) or PGP checksum line, scrubbed
# in hunks with a key marker; a lone `+` (an added blank line) and a line of under 8
# lowercase letters (`return`, `pass`: little key entropy) are kept. Hunks with only a long
# base64 run (a key body, but also e.g. a commit SHA) lose base64-only lines of 8+ chars,
# so ordinary one-word code lines survive there. A tail may be a quoted string literal
# (`"`, `'` or a backtick, optionally after a `b`/`f`/`r`-style prefix), with a literal
# `\n`/`\r\n` escape before its closing quote and a trailing `,`/`+`/`)`/`;` (concatenation,
# list items, call arguments); the quotes and the rest are kept. Whitespace runs are split
# by a punctuation char between quantifiers, so a long one is never rescanned (linear time).
_TAIL_QUOTE = r"([bfrBFR]{0,2}([\"'`]))?(?!\+[ \t]*\r?$)"
_TAIL_END = r"(?=(?(3)(?:\\r)?(?:\\n)?\3(?:[ \t]*[,+);]{1,2})?)[ \t]*\r?$)"
_CRC = r"|=[A-Za-z0-9+/]{4}"
KEY_TAIL_LINE_RE = re.compile(
    r"(?m)^(?![+\- ]?[ \t]*(?:[bfrBFR]{0,2}[\"'`])?[a-z]{1,7}"
    r"(?![A-Za-z0-9+/=]|[\"'`][A-Za-z0-9+/=]))([+\- ]?[ \t]*)" + _TAIL_QUOTE
    + r"(?:[A-Za-z0-9+/]+={0,2}" + _CRC + ")" + _TAIL_END)
LONG_TAIL_LINE_RE = re.compile(
    r"(?m)^([+\- ]?[ \t]*)" + _TAIL_QUOTE + r"(?:[A-Za-z0-9+/]{8,}={0,2}" + _CRC + ")" + _TAIL_END)
# ... and a base64-only line of any length right after a redacted key-body line.
AFTER_RUN_TAIL_LINE_RE = re.compile(
    r"(?m)^([+\- ]?[ \t]*(?:[bfrBFR]{0,2}[\"'`])?\[redacted\](?:\\r)?(?:\\n)?[\"'`]?"
    r"(?:[ \t]*[,+);]{1,2})?[ \t]*\r?\n[+\- ]?[ \t]*)" + _TAIL_QUOTE + r"[A-Za-z0-9+/]+={0,2}"
    + _TAIL_END)
# A hunk header; git appends the nearest preceding "function" line to it, which can be a
# key body line, so everything after the closing `@@` is dropped.
HUNK_HEADER_RE = re.compile(r"^(@@+ (?:[-+]\d+(?:,\d+)? )+@@+)[^\r\n]*")
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
# Pushes and deploys leave the machine, so they fail closed: when the classifier is
# unavailable or the gate itself crashes, they are denied instead of let through.
# Matched against the raw command (or raw payload) only on those error paths.
OUTWARD_COMMAND_RE = re.compile(
    r"\bgit\b[^;&|\n]*\b(?:push|send-pack)\b|\bheroku\b|\bdeploy\b", re.IGNORECASE)
# Quoting the scanner reads through (`g\it push`, `git 'pu'sh`, `git pu^sh`): the crash
# path matches the command as written and with these removed.
_OUTWARD_QUOTING_RE = re.compile(r"\\(?:\r?\n)?|[\"'`^]")
FAIL_CLOSED_REASON = (
    "[jev risk gate] The risk classifier {problem}, so this push/deploy is blocked "
    "(fail-closed). Do not retry or work around it: ask the user to run it manually or "
    "retry once Jev answers normally.")
# _push_signals: a best-effort lexical backstop counting the pushes in a command, so one
# _scan_targets can't parse (quoted or escaped words, `bash -c`/`eval`/`| sh` wrappers, a
# script's argv list, `git -c alias.*` and other aliases, a subcommand built at run time)
# still costs a denial. It can't see through string concatenation, a command built from
# variables (`$a $b`) or script files; the gate stays advisory
# (deny-once) by design.
# _PUSH_LEX_RE reads words in one pass: separators split them only outside quotes, a
# quoted span (to the end if unbalanced) stays inside its word without its quotes, and
# `\`-newline is dropped. A quoted span's content is also read as command text of its
# own (to _PUSH_QUOTE_DEPTH levels), so `bash -c "git push"` still counts.
# An ANSI-C `$'...'` span honours backslash escapes (`$'a\'b'`), so it is its own kind.
# It is read through _push_lex, which ends a double-quoted span that holds a
# substitution where the shell does (`"$(echo '"')"` is one span).
_PUSH_LEX_RE = re.compile(
    r"(?P<sep>[\s;&|()\[\],<>]+)|'(?P<sq>[^']*)'?|\"(?P<dq>(?:[^\"\\]|\\.)*)\"?"
    r"|\\(?P<esc>.?)|\$'(?P<ansi>(?:[^'\\]|\\.)*)'?"
    r"|(?P<run>(?:[^\s;&|()\[\],<>\"'\\$]|\$(?!'))+)", re.DOTALL)
# The same words as PowerShell reads them (see _push_lex): a backslash is an ordinary
# character there (`"C:\a\"` ends at its second quote), a backtick is the escape, inside
# double quotes too, and a doubled quote inside a span is that quote (`"a""b"`, `'it''s'`).
_PS_LEX_RE = re.compile(
    r"(?P<sep>[\s;&|()\[\],<>]+)|'(?P<sq>(?:[^']|'')*)'?|\"(?P<dq>(?:[^\"`]|`[\s\S]|\"\")*)\"?"
    r"|`(?P<esc>.?)|(?P<ansi>(?!))|(?P<run>[^\s;&|()\[\],<>\"'`]+)", re.DOTALL)
# What a backslash keeps escaped inside double quotes (its content read as a command).
_DQ_UNESCAPE_RE = re.compile(r"\\([\\\"$`])")
# PowerShell's: a backtick escape, or a doubled double quote.
_PS_DQ_UNESCAPE_RE = re.compile(r"`([\s\S])|\"(\")")
# An escaped character in a word, as bash and as PowerShell write it.
_ESCAPED_RE = re.compile(r"\\([\s\S])")
_PS_ESCAPED_RE = re.compile(r"`([\s\S])")
# A message's quoted value is prose, not command text, so it isn't re-read: after one of
# these options in a git command that takes a message, until the command ends (`;`, `&`,
# `|`, a parenthesis or newline), unless it is double-quoted with a substitution in it.
# Only as git's own subcommand (`git commit -m`, not `su tag -c -m '<script>'`), and
# after any short-option cluster that ends in `m` (`-am`, `-sm`, `-nm`).
_MESSAGE_CMDS = frozenset(("commit", "tag", "merge", "stash", "notes"))
_MESSAGE_OPT_RE = re.compile(r"-[a-z]*m|--message=?|-f|--file=?")
_COMMAND_END_RE = re.compile(r"[;&|()\n]")
# A substitution inside double quotes; a backslash-escaped pair (`\$(`, `` \` ``) is
# text (see _dq_subst).
_DQ_SUBST_RE = re.compile(r"\\[\s\S]|(\$\(|`)")
_PUSH_QUOTE_DEPTH = 2
# A brace group's `{`/`}` on its own is not a word (`{ git push; }`).
_BRACES_RE = re.compile(r"[{}]+")
# A word with one of these is dynamic (`$p`, `pus$(echo h)`, a backtick, PowerShell
# `@('push')`, `%P%`, brace expansion `pus{h..h}`); it is read with _PUSH_CLEAN_RE's
# characters removed (`$GIT`, `${GIT}` and `%GIT%` read `git`).
_PUSH_DYNAMIC_RE = re.compile(r"[$`@%{}]")
_PUSH_CLEAN_RE = re.compile(r"[$`\\{}%\"']")
# An unquoted `$IFS`, `${IFS}` or `${IFS:0:1}` splits words like a blank (`git${IFS}push`).
_IFS_RE = re.compile(r"\$\{IFS[^{}\s]{0,16}\}|\$IFS(?![A-Za-z0-9_])")
# A glob's bracket expression glued to a word (`g[i]t`, `[p]ush`): read as `?`, which
# matches at least what it does. Brackets are separators to _PUSH_LEX_RE otherwise.
_BRACKET_CLASS = r"""\[[^\]\[\s"',;&|()<>$`\\]{1,32}\]"""
_BRACKET_GLOB_RE = re.compile(r"(?<=[\w?*/.-])" + _BRACKET_CLASS + r"|" + _BRACKET_CLASS
                              + r"(?=[\w?*\[.-])")
# `git grep -O<pager>` and `--open-files-in-pager[=<pager>]` (any prefix git takes for
# it) run their value; case matters here (`-o` only prints the matching part).
_GREP_PAGER_RE = re.compile(r"-[A-Za-z0-9]*O|--op")
_PUSH_GIT_WORD_RE = re.compile(r"git(?:\.exe)?")
# A dashed git command word (`/usr/libexec/git-core/git-push`) and its subcommand.
_GIT_DASHED_WORD_RE = re.compile(r"git-([\w-]+?)(?:\.exe)?")
# _commit_signals re-reads only the quoted spans these run (see _executed_span): a shell
# or `eval` word, and a shell's script flag (`-c`, `-lc`, `-Command`, `/c`).
_EXEC_WORDS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "csh", "tcsh",
                         "fish", "pwsh", "powershell", "cmd", "eval", "su", "script", "flock"))
# A shell option whose value is the next word (`bash -o pipefail -c ...`).
_SHELL_OPT_WORDS = frozenset(("-o", "+o"))
_EXEC_FLAG_RE = re.compile(r"-[a-z]*c[a-z]*|--?command|/c|/k")
# Exec words that take a plain argument or two before their script flag (`su user -c`,
# `flock lock -c`), and how many words before a quoted span _executed_span reads.
_EXEC_ARG_WORDS = frozenset(("su", "flock", "script"))
_EXEC_SPAN_WINDOW = 10
# What _drop_inert_text looks at: escapes and single quotes (skipped whole), a double
# quote, a comment, a heredoc operator, a newline, and braces (a `<<` inside `${...}` is
# no heredoc).
_INERT_SCAN_RE = re.compile(r"""\\[\s\S]|'[^']*'|"|#|<<|\n|\$\{|[{}]|\$\(\(?|\(\(|[()]""")
# Where bash reads a `#` at a word's start as text, not as a comment: inside a backtick
# substitution, `${x/ #/y}`, `(( x = 1 #2 ))`, `$'a\'b # c'`, an extglob `@( #a|b)`, a
# `[[ =~ ]]` regex (`[[ a =~ ( #) ]]`) and an array subscript or compound assignment
# (`a[k #]=1`, `a=([k #]=1)`). A command with one of these loses no comment (see
# _drop_inert_text).
_NO_COMMENT_RE = re.compile(r"`|\$\{|\$\[|\(\(|\$'|[@!?*+]\(|=~|\w\[|=\(|\)#")
_BLANK_LINES_RE = re.compile(r"\s*")
# A `case` word on a heredoc's line: its `x)` patterns close nothing (see _drop_inert_text).
_CASE_WORD_RE = re.compile(r"(?<![\w-])case(?![\w-])")
# What _drop_ps_comments looks at in a PowerShell command: its escapes and quoted spans
# (skipped whole, see _PS_LEX_RE), a braced variable name (`${x #y}`, whose `#` is part
# of the name; to the end if unclosed), a block comment's start and a `#`.
_PS_INERT_SCAN_RE = re.compile(
    r"""`[\s\S]|'(?:[^']|'')*'?|"(?:[^"`]|`[\s\S]|"")*"?|\$\{(?:[^}`]|`[\s\S])*\}?|<#|#""")
# What makes a PowerShell command one whose quoting this can't follow, so that all of
# it is read (see _read_text): a here-string (`@"` or `@'` ending its line, whose body
# holds bare quotes), the stop-parsing token `--%`, and the typographic quotes
# PowerShell takes for `'` and `"`.
_PS_OPAQUE_RE = re.compile("@[\"'][ \t]*\r?\n|--%|[\u2018\u2019\u201a\u201b\u201c\u201d\u201e]")
# A shell word on a heredoc's line (`bash <<EOF`, `cat <<EOF | sh`), whose body then runs.
_SHELL_WORD_RE = re.compile(r"(?<![\w.-])(?:ba|z|da|k)?sh(?:\.exe)?(?![\w.-])|\beval\b"
                            r"|\bpwsh\b|\bpowershell\b|\b(?i:iex|invoke-expression)\b")
# `gh pr create` pushes the current branch when no head is named (see _gh_pushes).
_GH_WORDS = frozenset(("gh", "gh.exe"))
# `gh pr create` options whose value is the next word (unless given as `--opt=value`, or
# attached to the short letter: `-tHotfix`).
_GH_PR_VALUE_OPTS = frozenset((
    "--title", "--body", "--base", "--assignee", "--label", "--milestone", "--project",
    "--reviewer", "--template", "--body-file", "--repo", "--recover"))
_GH_PR_VALUE_SHORT = frozenset("tbBalmprTFR")
# The same, `gh repo create ... --push` too, and a git word with the word after it, in
# a span too deep to read as words.
_DEEP_GH_RE = re.compile(r"\bgh(?:\.exe)?[\"'\\]*\s+[\"'\\]*pr[\"'\\]*\s+[\"'\\]*(?:create|new)\b")
_DEEP_GH_REPO_RE = re.compile(r"\bgh(?:\.exe)?[\"'\\]*\s+[\"'\\]*repo[\"'\\]*\s+[\"'\\]*(?:create|new)\b")
_DEEP_GIT_RE = re.compile(r"\bgit(?:\.exe)?[\"'\\]*\s+[\"'\\]*([\w-]+)")
# `cmd` as a word: its script's carets are escapes (see _push_signals).
_CMD_WORD_RE = re.compile(r"(?i)(?<![\w.-])cmd(?:\.exe)?(?![\w.-])")
# What _encoded_signals looks at (in a lowercased command): a decoder, a pipe, a
# command's end (not the `&` of `2>&1` or `&>`), a shell word and an option that may be
# PowerShell's `-EncodedCommand`.
_ENCODED_HINTS = ("base64", "base32", "basenc", "xxd", "uudecode", "openssl", "certutil",
                  "powershell", "pwsh")
_ENCODED_SCAN_RE = re.compile(
    r"(?P<dec>\b(?:base64|base32|basenc|xxd|uudecode|openssl|certutil|frombase64string)\b)"
    r"|(?P<pipe>\|&?)|(?P<end>;|\n|(?<![<>])&(?!>))"
    r"|(?P<sh>" + _SHELL_WORD_RE.pattern + r")"
    r"|(?<=\s)[-/](?P<enc>e[a-z]*)(?=\s)")
_IEX_RE = re.compile(r"\b(?:iex|invoke-expression)\b")
# What _split_span_signals looks for, in the text with its quotes, backslashes and
# backticks removed: a word that splits a quoted span into words again (`eval`,
# PowerShell's `Start-Process` and its aliases, `watch`, `parallel`, `pwsh -c`); a git
# word, or `Start-Process`'s argument list option (`-ArgumentList`, `-Args`, `-a:`); and
# a push subcommand word anywhere after it.
_SPLIT_RUNNER_RE = re.compile(r"(?i)(?<![\w.-])(?:eval|start-process|saps|start|watch|parallel"
                              r"|pwsh|powershell)(?:\.exe)?(?![\w.-])")
_SPLIT_GIT_RE = re.compile(r"(?i)(?<![\w-])git(?:\.exe)?(?![\w-])")
_SPLIT_ARGS_RE = re.compile(r"(?i)(?<![\w-])-a[\w-]*")
_SPLIT_PUSH_RE = re.compile(r"(?i)(?<![\w-])(?:push|send-pack|http-push)(?![\w-])")
_SPLIT_DROP = str.maketrans("", "", "'\"\\`")
# _commit_signals counts a git word only where a command may start (see _command_words):
# after these shell keywords and wrappers the next word is still in command position,
# past their -flags, a flag's value (`sudo -u bob`, `/c`), a number (`timeout 60`) or a
# `NAME=value`; a `find -exec`-style option starts a command too.
_WRAPPER_WORDS = frozenset((
    "if", "elif", "then", "do", "else", "while", "until", "!", "time", "exec", "command",
    "builtin", "env", "nice", "nohup", "stdbuf", "doas", "sudo", "xargs", "timeout", "watch",
    "chronic", "ionice", "chrt", "taskset", "setsid", "unshare", "caffeinate", "unbuffer",
    "strace", "ltrace", "busybox", "winpty")) | _EXEC_WORDS
# Wrappers whose first plain argument comes before the command (`flock /tmp/lock git`).
_WRAPPER_ARG_WORDS = frozenset(("flock",))
_EXEC_OPTS = frozenset(("-exec", "-execdir", "-ok", "-okdir"))
_FLAG_WORD_RE = re.compile(r"-.*|/[a-z]", re.DOTALL)
_NUMBER_WORD_RE = re.compile(r"\d[\w.]*")
_ASSIGN_WORD_RE = re.compile(r"[a-z_]\w*=.*", re.DOTALL)  # the words are lowercased
# The subcommands _push_signals and _commit_signals count.
_PUSH_SUBS = ("push", "send-pack", "http-push")
_COMMIT_SUBS = ("commit",)
_PATH_SEP_RE = re.compile(r"[/\\]")
# Config that environment variables pass to git (an alias among it may push).
_GIT_CONFIG_ENV_RE = re.compile(r"git_config_(?:parameters=|count=|key_)")
# Variables that say which config files git reads, or hold config themselves, set
# anywhere in a (lowercased) command: an alias may come from them (see _alias_signals).
_ALIAS_ENV_RE = re.compile(
    r"(?<!\w)(?:git_config(?:_global|_system|_nosystem|_parameters|_count|_key_\d+|_value_\d+)?"
    r"|home|xdg_config_home|git_dir|git_common_dir)[ \t]*=")
# Global git options (lowercased, so `-c` is also `-C`) whose value is the next word.
_GIT_VALUE_OPTS = frozenset(("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
                             "--config-env", "--super-prefix", "--attr-source"))
# How many words after a `git` word are searched for its subcommand; still inside its
# options past that counts as a push (unknown subcommand).
_GIT_SUBCOMMAND_WINDOW = 64
# _push_signals also reads the script an interpreter runs (`python -c`, `node -e`) and
# what PowerShell's `iex` evaluates; the heredoc of a line naming one is kept too.
_SCRIPT_WORD_RE = re.compile(r"python[\d.]*|pypy[\d.]*|py|node|nodejs|perl|ruby"
                             r"|iex|invoke-expression")
_SCRIPT_EVAL_WORDS = frozenset(("iex", "invoke-expression"))
_SCRIPT_FLAG_RE = re.compile(r"-[a-z]*[ce][a-z]*|--eval|--command")
_SCRIPT_LINE_RE = re.compile(_SHELL_WORD_RE.pattern + r"|\b(?:python[\d.]*|pypy[\d.]*|py|node"
                             r"|nodejs|perl|ruby|iex)\b", re.IGNORECASE)
# _push_signals reads only what runs unless the command may run its own text some other
# way (see _may_run_text): then every word and quoted span is read.
# Command words that run or keep their arguments as a command, and ones that evaluate
# them (inert only when every argument is literal: that one is read as a quoted span).
_LIVE_WORDS = frozenset(("trap", "alias", "coproc", "source", ".", "function",
                         "start-process", "saps", "start"))
_EVAL_WORDS = frozenset(("eval", "iex", "invoke-expression"))
# Shells and interpreters: live when they read a script file, stdin or a heredoc, not
# when a script flag gives them a literal script (or `python -m` a module).
_SHELL_WORDS = _EXEC_WORDS - _EVAL_WORDS - _EXEC_ARG_WORDS
_INTERPRETER_WORD_RE = re.compile(r"python[\d.]*|pypy[\d.]*|py|node|nodejs|perl|ruby|php"
                                  r"|deno|bun")
# Their script flags, stricter than _EXEC_FLAG_RE and _SCRIPT_FLAG_RE (which take any
# flag with a `c` in it, such as `-ExecutionPolicy`): a wrong guess here hides a script.
_SHELL_SCRIPT_FLAG_RE = re.compile(r"-[a-z]{0,4}c[a-z]{0,4}|--?command|/c|/k")
_INTERPRETER_SCRIPT_FLAG_RE = re.compile(r"-[a-z0-9]{0,4}[ce]|--eval|--command")
# Commands that only read or print what they are given: a pipe or a process
# substitution into one of these, or a written file named to one, runs nothing.
_INERT_READERS = frozenset((
    "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "cut", "tr",
    "less", "more", "nl", "tac", "column", "jq", "diff", "cmp", "comm", "echo", "printf",
    "ls", "dir", "type", "stat", "file", "true", "findstr", "select-string", "sls",
    "measure-object", "get-content", "gc", "out-null", "out-host", "out-string",
    "write-host", "write-output"))
_INERT_GIT_SUBS = frozenset(("status", "diff", "log", "add"))
# PowerShell's eval word, `cmd /c` or `[scriptblock]::Create` given a parenthesised or
# `+`-joined expression (`iex ('git ' + 'push')`): the text it runs is built at run time.
_PS_BUILT_RE = re.compile(
    r"(?i)(?:(?<![\w-])(?:iex|invoke-expression)|(?<![\w.-])cmd(?:\.exe)?\s+/[ck]|::create)"
    r"\s*(?:\(|(?:'[^'\n]*'|\"[^\"\n]*\"|[^\s;|'\"]+)\s*\+)")
# Options with which an inert reader runs a program it is given (any prefix, lowercased).
_READER_RUN_OPTS = ("--pre", "--com")
# A file an inert reader writes itself (`sort -o f`, `grep --output=f`; see _writes_file).
_WRITE_OPT_RE = re.compile(r"-[a-z]*o.*|--o.*", re.DOTALL)
_WRITER_WORDS = frozenset(("tee", "tee.exe", "tee-object"))
# A path in a `.git` directory, or the `.git` file itself (hooks, config, a gitdir line):
# git reads it whatever runs next, so a write there keeps the text live.
_GIT_DIR_PATH_RE = re.compile(r"(?i)(?:^|[/\\])\.git(?:[/\\]|$)")
# Shell group openers in command position and the word that closes each.
_GROUP_OPENERS = {"if": "fi", "while": "done", "until": "done", "for": "done",
                  "select": "done", "case": "esac"}
_LOOP_WORDS = frozenset(("for", "while", "until", "select"))
# Commands that change the directory later writes land in (see _may_run_text).
_CD_WORDS = frozenset(("cd", "pushd", "chdir", "set-location", "sl", "push-location"))
_GIT_NAMES = frozenset(("git", "git.exe"))
# Not `nul`: off Windows it is a plain file (`> nul; make -f nul`).
_NULL_FILES = frozenset(("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "$null"))
# Git options whose value is text to search for or print, never a command (`git log
# --grep="git push"`), attached or as the next word (see _text_span).
_GIT_TEXT_OPTS = frozenset(("--grep", "--grep-reflog", "--author", "--committer", "--format",
                            "--pretty", "-S", "-G"))
# Cheap test for anything _may_run_text looks at (plain substring searches; a command
# with none of these has nothing to tell apart). Every word above holds one of the
# hints (`alias` through _LIVE_ALIAS_RE): `sh` is in each shell's name and in `push`,
# `py` in python and pypy.
_LIVE_HINT_CHARS = "$`|<>@%&"
_LIVE_HINTS = ("sh", "cmd", "py", "node", "perl", "ruby", "php", "deno", "bun", "eval", "iex",
               "invoke", "trap", "coproc", "source", "function", "start", "saps",
               "send-pack", ". ", ".\t", ".'", '."')
# `alias` the builtin, not a git config key (`git -c alias.p=push`), which would make
# every such command a full read.
_LIVE_ALIAS_RE = re.compile(r"alias(?![.\w])")
# A simple command with more words than this is read whole instead of word by word.
_LIVE_MAX_WORDS = 4096
# `git config` options and subcommands that read or remove a key rather than set one.
_CONFIG_READ_OPTS = ("--get", "--list", "--unset")
_CONFIG_READ_SUBS = frozenset(("get", "list", "unset"))
# git's own commands (`git --list-cmds=main`, git 2.48). A subcommand that is none of
# them may be a config alias, which _alias_signals looks up.
_GIT_BUILTINS = frozenset("""
    add am annotate apply archive backfill bisect blame branch bugreport bundle cat-file
    check-attr check-ignore check-mailmap check-ref-format checkout checkout-index cherry
    cherry-pick citool clean clone column commit commit-graph commit-tree config
    count-objects credential credential-cache credential-store daemon describe diagnose
    diff diff-files diff-index diff-tree difftool fast-export fast-import fetch fetch-pack
    filter-branch fmt-merge-msg for-each-ref for-each-repo format-patch fsck fsck-objects
    gc get-tar-commit-id grep gui hash-object help hook http-backend http-fetch http-push
    imap-send index-pack init init-db instaweb interpret-trailers log ls-files ls-remote
    ls-tree mailinfo mailsplit maintenance merge merge-base merge-file merge-index
    merge-octopus merge-one-file merge-ours merge-recursive merge-resolve merge-subtree
    merge-tree mergetool mktag mktree multi-pack-index mv name-rev notes p4 pack-objects
    pack-redundant pack-refs patch-id prune prune-packed pull push quiltimport range-diff
    read-tree rebase receive-pack reflog refs remote repack replace replay request-pull
    rerere reset restore rev-list rev-parse revert rm send-email send-pack shortlog show
    show-branch show-index show-ref sparse-checkout stage stash status stripspace
    submodule subtree survey svn switch symbolic-ref tag unpack-file unpack-objects update
    update-index update-ref update-server-info upload-archive upload-pack var
    verify-commit verify-pack verify-tag version whatchanged worktree write-tree
    """.split())
# A name `git config` can hold an alias under.
_ALIAS_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
# Inline config that may define an alias or include a file that does.
_ALIAS_CONFIG_KEY_RE = re.compile(r"(?:alias|include|includeif)\.|.*[$`]", re.IGNORECASE)
# At most this many alias lookups per command, within this many seconds in all; past
# either the remaining ones count as pushes (too many) or as no alias (too slow).
MAX_ALIAS_LOOKUPS = 8
ALIAS_LOOKUP_SECONDS = 1.0
# How many aliases naming another alias are followed.
MAX_ALIAS_CHAIN = 3
# A command that is only a POSIX shell running a script (`bash -lc '<script>'`, after
# an optional `winpty`), up to where its options start (see _unwrap_shell). The path
# excludes blanks, separators and quotes, so the match never leaves the first word.
_SHELL_WRAP_RE = re.compile(
    r"(?i)\s*(?:winpty(?:\.exe)?[ \t]+(?:-[^\s;&|()`'\"]+[ \t]+)*)?"
    r"(?:[^\s;&|()`'\"$=]*[/\\])?(?:ba|z|da|k)?sh(?:\.exe)?[ \t]+(?=-)")
_SHELL_WRAP_FLAG_RE = re.compile(r"-[a-z]*c[a-z]*")
_SHELL_PLAIN_FLAG_RE = re.compile(r"--?[a-z][a-z-]*")
# Longer commands are left to the lexical backstop (shlex is slow on them).
MAX_UNWRAP_CHARS = 100_000
# `git push` options (see _push_request): ones that widen what is sent beyond the named
# refs, and ones whose value is the next word.
_PUSH_WIDE_OPTS = frozenset(("--mirror", "--all", "--branches", "--tags", "--follow-tags",
                             "--prune"))
_PUSH_VALUE_OPTS = frozenset(("--push-option", "--receive-pack", "--exec", "--repo"))
# Ones that name the program run on the remote end, which no diff shows.
_PUSH_PROGRAM_OPTS = frozenset(("--receive-pack", "--exec"))
# Every long option `git push` takes. git reads any unique prefix of one (`--del`,
# `--mirr`), so a word is matched against them all (see _push_long_options).
_PUSH_LONG_OPTS = (
    "--verbose", "--quiet", "--repo", "--all", "--branches", "--mirror", "--delete", "--tags",
    "--dry-run", "--porcelain", "--force", "--force-with-lease", "--force-if-includes",
    "--recurse-submodules", "--thin", "--receive-pack", "--exec", "--set-upstream",
    "--progress", "--prune", "--no-verify", "--verify", "--follow-tags", "--signed", "--atomic",
    "--push-option", "--ipv4", "--ipv6")
# A push argument the shell fills in when the command runs: a `$` expansion, a backtick
# substitution or a brace expansion (`{a,b}`, which the lexer splits at its commas).
# A ref's `@{u}` suffix is not one.
_PUSH_RUNTIME_RE = re.compile(r"[$`{}]")
_REF_SUFFIX_RE = re.compile(r"@\{[^{}$`,]*\}")
# Inline config (`git -c <key>=…`, `--config-env=<key>=…`) that changes what a push
# sends or where: remote.<name>.push/mirror/url, push.default, branch.<name>.*, url.*,
# and include.*/includeIf.*, whose file may set any of those. Keys are case-insensitive.
_PUSH_CONFIG_KEY_RE = re.compile(r"(?:remote|push|branch|url|include|includeif)\.|.*[$`]",
                                 re.IGNORECASE)
# The same config passed through the environment, anywhere in the command.
_PUSH_CONFIG_ENV_RE = re.compile(r"\bGIT_CONFIG_(?:COUNT|KEY_\d+|PARAMETERS)\b", re.IGNORECASE)
_PUSH_CONFIG_NOTE = ("inline git config (-c or GIT_CONFIG_*) may change what this push "
                     "sends or where")
# A remote or branch name safe to build a `refs/remotes/<remote>/<branch>` ref from.
_REF_PART_RE = re.compile(r"[A-Za-z0-9_][\w.+-]*(?:/[\w.+-]+)*")
_PUSH_DESTINATION_NOTE = ("pushes to a destination with no remote-tracking ref here (a new "
                          "branch, or a remote or branch never fetched), so what it already "
                          "holds is unknown")
MAX_PUSH_NOTE_CHARS = 600
NO_KEY_WARNING = (
    "[jev risk gate] warning: no TypeSafe API key is configured, so this push was not "
    "reviewed (set TYPESAFE_API_KEY or jev.api_key_file, or turn risk_gate off).")
UNPARSED_PUSH_REASON = (
    "[jev risk gate] This command looks like it pushes through quoting, an alias or a "
    "wrapper (bash -c, eval, git -c alias.*), which the gate cannot review. Rerun it as a "
    "plain `git push …` so the diff is checked. An identical rerun is not waved through: "
    "it is put to the risk classifier as a push whose arguments could not be read, and is "
    "blocked while the classifier is unavailable.")
UNPARSED_COMMIT_REASON = (
    "[jev risk gate] This command looks like it commits through quoting, an alias or a "
    "wrapper (bash -c, eval, git -c alias.*), which the gate cannot review. Rerun it as a "
    "plain `git commit …` so the diff is checked. If it does not commit, rerun the "
    "identical command once to proceed.")
# What the classifier is told about a push the scanner could not read (see `backstop`).
WRAPPED_PUSH_NOTE = ("wrapped push; arguments unreadable; may force, delete or target "
                     "another repo")
UNJUDGED_PUSH_REASON = (
    "[jev risk gate] Could not work out what this push sends (no upstream or origin default "
    "branch, an unresolved refspec, or git timed out), so it was not reviewed. Set an upstream or review the "
    "commits, then rerun; if review is not warranted, rerun the identical command once to "
    "proceed.")


def fail_closed_output(problem="is unavailable"):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": FAIL_CLOSED_REASON.format(problem=problem),
    }}


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
# Case-sensitive UPPERCASE headers followed by a colon or alone on their line, so prose
# like "Result of git diff..." or "RESULT here is..." doesn't overmatch; optional leading
# markdown (#, *, -, >) is still allowed.
SECTION_RE = re.compile(
    r"(?m)^[ \t]*[#*\-> \t]*\**(RESULT|EVIDENCE|CONFIDENCE|UNVERIFIED)\**[ \t]*(?::\**[ \t]*|\r?$)")
# What the Agent tool returns when the report itself went through SubagentHandback,
# which the handback check already saw. Full match on the exact stub (plus the optional
# agentId / <usage> trailer) so a sectionless report can't ride in behind the phrase.
# Observed: 'This agent's report was delivered to you as a message from "<id>" (its
# SubagentHandback call). Read it there; it is not repeated here.' then "agentId: <id>
# (use SendMessage with to: '<id>', summary: '<5-10 word recap>' to continue this agent)"
# and "<usage>subagent_tokens: N ... duration_ms: N</usage>".
HANDBACK_STUB_RE = re.compile(
    r"\s*This agent['\u2019]s report was delivered to you as a message"
    r"(?: from \"[\w-]{1,100}\")? \(its SubagentHandback call\)\."
    r"(?: Read it there; it is not repeated here\.)?"
    r"(?:\s*agentId: [\w-]{1,100}(?: \(use SendMessage with to: '[\w-]{1,100}', "
    r"summary: '[^'\n]{0,100}' to continue this agent\))?)?"
    r"(?:\s*<usage>(?:\s*\w+: [\w.]+)*\s*</usage>)?\s*")


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
        state = json.loads(session_state_path(session_id, state_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}
    # A state file holding other JSON (`[]`, `null`) reads as empty, not as a crash in .get().
    return state if isinstance(state, dict) else {}


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


def _msvcrt_lock(lock_file, deadline=None):
    """Lock the first byte of `lock_file` (Windows), retrying for MSVCRT_LOCK_SECONDS
    or until `deadline` (a time.monotonic() value), whichever comes first, after at
    least one attempt; raises OSError if another process still holds it."""
    lock_file.seek(0)
    wait_until = time.monotonic() + MSVCRT_LOCK_SECONDS
    if deadline is not None:
        wait_until = min(wait_until, deadline)
    while True:
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in MSVCRT_BUSY_ERRNOS or time.monotonic() >= wait_until:
                raise
            time.sleep(0.01)


def _flock_until(lock_file, deadline):
    """fcntl.flock `lock_file` exclusively, retrying non-blocking attempts for
    MSVCRT_LOCK_SECONDS or until `deadline` (a time.monotonic() value), whichever
    comes first, after at least one attempt, so a held lock leaves later work in the
    hook the same budget as on Windows; raises OSError (BlockingIOError) if another
    process still holds it."""
    wait_until = min(time.monotonic() + MSVCRT_LOCK_SECONDS, deadline)
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in FLOCK_BUSY_ERRNOS or time.monotonic() >= wait_until:
                raise
            time.sleep(0.01)


def update_state(path, mutate_fn, deadline=None):
    """Read-modify-write `path`'s JSON state under an exclusive lock on `<path>.lock`.

    `mutate_fn(state)` mutates a freshly reloaded on-disk state dict in place (or
    returns a replacement dict) so a concurrent writer's unrelated keys survive even
    if this invocation's load was stale. The lock is `fcntl.flock`, or `msvcrt.locking`
    on Windows. If neither is available, the update runs without a lock: still
    correct for a single process, best-effort under real concurrency.

    With a `deadline` (the hook's time.monotonic() deadline) the lock wait stops
    there and raises OSError; without one, flock blocks and msvcrt waits
    MSVCRT_LOCK_SECONDS.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_file = None
    locked = False
    try:
        if fcntl is not None:
            lock_file = open(lock_path, "a+")
            if deadline is None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            else:
                _flock_until(lock_file, deadline)
            locked = True
        elif msvcrt is not None:
            lock_file = open(lock_path, "a+")
            _msvcrt_lock(lock_file, deadline)
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
    Single quotes take no escapes, except in an ANSI-C `$'...'` string (`$'a\\'b'`);
    double quotes honour backslash escapes."""
    if text[start] == "'" and not _ansi_c_quote(text, start):
        end = text.find("'", start + 1)
        return None if end == -1 else end + 1
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    return None


def _ansi_c_quote(text, start):
    """Whether the `'` at `start` opens an ANSI-C `$'...'` string: the `$`s right before
    it, less one escaped by an odd backslash run (`\\$'x'`), are odd in number (`$$` is
    the shell's PID, so `$$'x'` is a plain quote)."""
    j = start
    while j > 0 and text[j - 1] == "$":
        j -= 1
    dollars = start - j
    k = j
    while k > 0 and text[k - 1] == "\\":
        k -= 1
    if (j - k) % 2:
        dollars -= 1
    return dollars % 2 == 1


# A quoted piece of a command word that _strip dequotes (no blank, expansion or glob).
_SIMPLE_WORD_RE = re.compile(r"[\w./\\:+-]+")
_WORD_BREAKS = frozenset(" \t\r\n;&|()<>`")


def _word_start(text):
    """Where the word `text` ends in starts (len(text) after a blank or separator);
    bounded by the tail window _strip passes."""
    k = len(text)
    while k and text[k - 1] not in _WORD_BREAKS:
        k -= 1
    return k


def _strip_heredocs_and_quotes(command, legacy_arith=False):
    """Remove heredoc bodies, comments and quoted strings so text inside them (e.g.
    `echo "git commit"` or a `cat <<EOF ... git commit ... EOF` body) can't be mistaken
    for a real command. `git commit -m "msg"` still matches: only the quoted message is
    removed, and the verb sits outside it. legacy_arith selects the older reading of a
    `<<` (see _strip)."""
    return _strip(command, "legacy" if legacy_arith else "default")[0]


# Frame kinds (see _strip) inside which a `<<` is a shift, not a heredoc.
_ARITH_FRAMES = ("A", "B", "K", "G")
# What may follow an `esac` that closes a case statement.
_ESAC_END = ("", " ", "\t", "\n", ";", "&", "|", ")", "<", ">")
# `case WORD in`: a `case` not followed by a word and `in` (`$(echo case x)`) opens no
# case statement. The word may be quoted, or missing (_strip dropped its quotes), so
# _strip on the raw command and _cd_dirs on its stripped text agree; it may hold a
# `$( )` (one nested `( )`), `${ }` or backtick substitution with blanks inside
# (`case $(uname -s) in`). Linear: the word's alternatives start with different chars
# (a `$` not opening one of those is its own), it ends at the first blank outside them,
# and a substitution's scan stops at the next bracket or backtick, so the scans that
# different `case`s start don't overlap (`case ${` * N).
# A `$(( ))` arithmetic (one nested `( )`) is its own alternative, and the `$( )` one
# excludes it, so the two never match the same text (which would backtrack
# exponentially over a run of them).
_CASE_RE = re.compile(
    r"""case[ \t]+(?:(?:[^\s;&|<>'"\\$`]|\$(?![({])|\$\(\((?:[^()]|\([^()]*\))*\)\)"""
    r"""|\$\((?!\()(?:[^()]|\([^()]*\))*\)|\$\{[^{}]*\}"""
    r"""|`[^`]*`|\\[\s\S]|"(?:[^"\\]|\\[\s\S])*"|'[^']*')+\s+)?in(?=[\s;]|$)""")


def _is_word(text, start, end):
    """Whether text[start:end] is a whole word (as `\\b…\\b` would match it)."""
    return ((start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_"))
            and (end == len(text) or not (text[end].isalnum() or text[end] == "_")))


# Any `case` word, for _scan_targets.
_CASE_WORD_RE = re.compile(r"\bcase\b")
# Where a git command's own arguments end (for its -a/--all flag, in _git_target).
_SEGMENT_END_RE = re.compile(r"[;&|\n]")
# _git_target's default: look the cd directory up in the cd index.
_FROM_INDEX = object()


def _may_keep(tail):
    """False when none of _GIT_DASH_C_PREFIX_RE, _CD_PREFIX_RE and _LOOSE_KEEP_RE can
    match `tail`: each needs its last blank-separated word to hold a `-` (`-C`, `-c`,
    `x--foo-C`, `--git-dir=`, a cd option) or to end in a cd verb (see _CD_VERB). A
    `$`-anchored search tries every start in the tail, which made them most of a quoted
    argument's cost."""
    words = tail.rsplit(None, 1)
    return bool(words) and ("-" in words[-1] or words[-1].lower().endswith(_CD_VERB_ENDS))


def _case_step(text, i, open_cases):
    """+1 when a `case … in` starts at text[i], -1 when an `esac` there closes one of
    the `open_cases` case statements, else 0. Shared by _strip and _cd_dirs, so both
    agree on which lone `)` is a case pattern's rather than a subshell's."""
    if text.startswith("case", i):
        if (i == 0 or text[i - 1] in " \t\n;&|(){") and _CASE_RE.match(text, i):
            return 1
    elif (open_cases and text.startswith("esac", i) and text[i + 4:i + 5] in _ESAC_END
          and (i == 0 or text[i - 1] in " \t\n;&)")):
        return -1
    return 0


def _subst_end(text, start, depth=0, bounded=False):
    """Closing index of a command substitution, ignoring quoted brackets and case patterns."""
    if depth >= MAX_SUBST_DEPTH:
        raise TooManyTargets
    level, cases, pending = 1, [0], []
    i = start + 2
    escaped = -1  # where the last backslash escape ended: a `#` there is no comment
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            escaped = i
            continue
        if ch == "#" and i != escaped and (i == 0 or text[i - 1] in " \t\n;&|()"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("<<", i) and not text.startswith("<<<", i):
            heredoc = HEREDOC_RE.match(text, i)
            if heredoc:
                pending.append((heredoc.group(2) or heredoc.group(3) or heredoc.group(4),
                                bool(heredoc.group(1))))
                i = heredoc.end()
                continue
        if ch == "\n" and pending:
            i += 1
            for word, dash in pending:
                while i < len(text):
                    end = text.find("\n", i)
                    end = len(text) if end < 0 else end
                    line = text[i:end].rstrip("\r")
                    i = min(end + 1, len(text))
                    if (line.lstrip("\t") if dash else line) == word:
                        break
                else:
                    return None
            pending.clear()
            continue
        if ch in "'\"":
            end = (_double_quote_end(text, i, depth + 1) if ch == '"'
                   else _quote_end(text, i))
            if end is None:
                return None
            i = end
            continue
        if text.startswith("$(", i) and not text.startswith("$((", i):
            level += 1
            if level > MAX_SUBST_DEPTH:
                if bounded:
                    raise TooManyTargets
                return None
            cases.append(0)
            i += 2
            continue
        if ch in "ce":
            cases[-1] += _case_step(text, i, cases[-1])
        if ch == "(":
            level += 1
            cases.append(0)
        elif ch == ")" and not cases[-1]:
            level -= 1
            cases.pop()
            if not level:
                return i + 1
        i += 1
    return None


def _double_quote_end(text, start, depth=0):
    """End of a double quote, skipping command substitutions with their own quotes."""
    if depth >= MAX_SUBST_DEPTH:
        raise TooManyTargets
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text.startswith("$(", i) and not text.startswith("$((", i):
            i = _subst_end(text, i, depth + 1)
            if i is None:
                return None
        elif text[i] == "`":
            # A backtick substitution, whose own quotes don't end this one.
            m = _BACKTICK_END_RE.match(text, i + 1)
            if m is None:
                return None
            i = m.end()
        elif text[i] == '"':
            return i + 1
        else:
            i += 1
    return None


def _quoted_substitutions(text, depth):
    """Only executable substitutions in a double-quoted value, as separate commands."""
    if depth >= MAX_SUBST_DEPTH and "$(" in text:
        raise TooManyTargets
    parts, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text.startswith("$(", i) and not text.startswith("$((", i):
            end = _subst_end(text, i)
            if end is None:
                raise TooManyTargets
            parts.append("$(" + _strip(text[i + 2:end - 1], depth=depth + 1)[0] + ")")
            i = end
            continue
        if text[i] == "`":
            # `"`git push`"` runs its command just as `"$(git push)"` does.
            m = _BACKTICK_END_RE.match(text, i + 1)
            if m is None:
                raise TooManyTargets
            if depth >= MAX_SUBST_DEPTH:
                raise TooManyTargets
            parts.append("$(" + _strip(text[i + 1:m.end() - 1], depth=depth + 1)[0] + ")")
            i = m.end()
            continue
        i += 1
    return " ".join(parts)


def _strip(command, mode="default", depth=0):
    """(text, frame_open) for _strip_heredocs_and_quotes: `mode` is "default" (the
    frame model below), "legacy" (the older `((`/`))` count) or "none" (a `<<` is never
    a shift); frame_open says a frame of the default reading was still open at the end
    (an unclosed arithmetic, or a `(` whose `)` was taken for a case pattern's).

    A small left-to-right scanner rather than regexes, so a backslash escape outside
    quotes (`don\\'t`) can't open a phantom quote, a `<<` inside quotes or a comment
    isn't taken for a heredoc, and a `\\`-newline continuation joins its lines. A quoted
    string is kept when it is the value of a literal git `-C`/`-c` or a `cd` target,
    which `_cd_prefix_dir` and `_dash_c_dir` need intact to resolve the gate's cwd; a
    quoted command word naming git (`"C:\\Git\\cmd\\git.exe" commit`) becomes `git`.

    The body of an unquoted heredoc is expanded by the shell before its command runs, so
    each `$( )` and backtick command in it is kept, as `$(cmd); ` at the start of the
    heredoc command's segment (leaving the command's own words intact; see
    _heredoc_commands); `depth` is how deep inside such a command this scan is."""
    out = []
    tail = ""  # last (roughly) PREFIX_WINDOW_CHARS of "".join(out); see its definition
    # (word, dash_form, quoted, out index of its commands' slot) heredoc terminators
    # whose bodies start at the next newline
    pending = []
    out.append("")  # the slot at the start of the current command segment
    seg_slot = 0
    open_slots = []  # seg_slot at each open `(`: a `)` returns to it

    def track(text):
        """Keep seg_slot outside the parentheses still open (a heredoc's commands go
        before the command, not inside a `$( )` in its words)."""
        nonlocal seg_slot
        for c in text:
            if c == "(":
                open_slots.append(seg_slot)
            elif c == ")" and open_slots:
                seg_slot = open_slots.pop()

    def starts_segment(at):
        """Whether command[at] ends the separator after which a new segment starts, as
        _CD_SPLIT_RE splits: `;`, newline, standalone `&`, a `{ ` and the second
        char of `&&` / `||`. A single `|`, `|&`, `>&2`, `&>`, `>|` and `{a,b}`
        do not split here."""
        ch = command[at]
        if ch in ";\n":
            return True
        if ch in "&|":
            return command[at - 1:at] == ch or (ch == "&" and
                    command[at - 1:at] not in ("|", "<", ">", ";") and
                    command[at + 1:at + 2] not in ("&", ">", "|"))
        return (ch == "{" and (at == 0 or command[at - 1] in " \t\n;&|(")
                and command[at + 1:at + 2] in (" ", "\t", "\n"))

    trimmed = False
    # Open bracket frames of the default reading, tracked in the scanned command text
    # itself (not the bounded tail, which a long run of blanks inside `$((` can push the
    # opener out of): "A" a `((`/`$((` arithmetic, "B" a `$[` arithmetic, "K" a `[`
    # subscript inside B/K, "G" a grouping `(` inside arithmetic, "S" a command-context
    # `(` (subshell, `$(`/`<(`/`>(` substitution), "Q" a backtick substitution. `<<` is a
    # shift, not a heredoc, only while the innermost frame is arithmetic (A/B/K/G): taking
    # a shift for a heredoc would hide the lines up to a numeric "terminator", while a
    # heredoc inside a `$( )` nested in `$(( ))` is a heredoc again. Frames close
    # conservatively: an A frame takes only a `))` (never a lone `)`), a B/K frame a `]`,
    # and an S frame skips the lone `)` of a `case` pattern: cases[k] counts the `case`s
    # open in frame k (cases[0]: no frame), and a new frame starts at 0, so a `(a)`
    # pattern or a `$( )` inside a case body still closes. Neither reading is safe on
    # its own: taking a heredoc for a shift scans its body, where a stray quote can pair
    # with a quote on a later line and hide the command between them. So "legacy" keeps
    # the older reading (only `((`/`))` counted, closed by any `))`), "none" never takes
    # a shift (bash reparses `((1) …)`, which no `))` closes, as nested subshells), and
    # _scan_targets scans each one that can differ.
    legacy = mode == "legacy"
    frames = []
    cases = [0]
    arith_depth = 0  # the legacy reading's `((` count
    # word -> ([starts], [ends]) of every terminator-shaped line, and the same for
    # unindented lines only; built on the first heredoc so each body end is a bisect
    # rather than a regex search to the end of the input (quadratic on many
    # unterminated heredocs).
    terminators = None

    def terminator_span(word, dash_form, start):
        nonlocal terminators
        if terminators is None:
            terminators = ({}, {})
            for t in _TERMINATOR_LINE_RE.finditer(command):
                for table in (terminators[0],) + (() if t.group(1) else (terminators[1],)):
                    starts, ends = table.setdefault(t.group(2), ([], []))
                    starts.append(t.start())
                    ends.append(t.end())
        starts, ends = terminators[0 if dash_form else 1].get(word, ((), ()))
        k = bisect.bisect_left(starts, start)
        return (starts[k], ends[k]) if k < len(starts) else None

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
    escaped = -1  # where the last backslash escape ended: a `#` there is no comment
    while i < n:
        ch = command[i]
        if ch == "\\":
            if command.startswith("\n", i + 1):
                i += 2  # line continuation: `git \<newline>commit` is one command
                escaped = i
                continue
            if command.startswith("$(", i + 1):
                end = _subst_end(command, i + 1)
                if end is not None:
                    emit(" ")  # escaped $, so the apparent substitution is inert
                    i = end
                    continue
            emit(command[i:i + 2])
            i += 2
            escaped = i
            continue
        if ch in "'\"":
            end = _double_quote_end(command, i) if ch == '"' else _quote_end(command, i)
            if end is None:
                # Unbalanced quote: keep the rest; a spurious check is safer than
                # hiding a later git command.
                emit(command[i:])
                break
            nested = ""
            if ch == '"' and ("$(" in command[i:end] or "`" in command[i:end]):
                nested = _quoted_substitutions(command[i + 1:end - 1], depth)
                if nested:
                    out[seg_slot] += nested + "; "
            if _may_keep(tail) and (
                    _GIT_DASH_C_PREFIX_RE.search(tail) or _CD_PREFIX_RE.search(tail)
                    or (trimmed and _LOOSE_KEEP_RE.search(tail))):
                emit('"$()"' if ch == '"' and nested else
                     ('""' if _OPAQUE_VALUE_RE.search(tail) else command[i:end]))
            elif (_GIT_WORD_RE.fullmatch(command, i + 1, end - 1)
                  and (end == n or command[end] in " \t\n;&|()<>")
                  and _COMMAND_POS_RE.search(tail)):
                # A quoted word the command word goes on after (`'git'x push`) is
                # dropped instead, as before, which keeps the rest of it visible.
                emit("git")
            elif (_SIMPLE_WORD_RE.fullmatch(command, i + 1, end - 1)
                  and _COMMAND_POS_RE.search(tail[:_word_start(tail)])):
                # A plain quoted piece of the command word (`'g'it`, `gi"t"`) is
                # dequoted as the shell does, so the word still reads `git`.
                emit(command[i + 1:end - 1])
            i = end
            continue
        if command.startswith("$(", i) and _may_keep(tail) and _GIT_DASH_C_PREFIX_RE.search(tail):
            end = _subst_end(command, i, bounded=True)
            if end is None:
                raise TooManyTargets
            inner = _strip(command[i + 2:end - 1], mode, depth + 1)[0]
            out[seg_slot] += _SLOT_MARK + "$(" + inner + "); "
            emit('"$()"')
            i = end
            continue
        if ch == "#" and i != escaped and (i == 0 or command[i - 1] in " \t\n;&|()"):
            # `fix\ #123` is one word: only a `#` that starts a word is a comment.
            end = command.find("\n", i)
            i = n if end == -1 else end
            continue
        if command.startswith("<<<", i):
            emit("<<<")  # a here-string, not a heredoc
            i += 3
            continue
        shift = False
        if legacy:
            if command.startswith("((", i):
                arith_depth += 1
                emit("((")
                track("((")
                i += 2
                continue
            if arith_depth and command.startswith("))", i):
                arith_depth -= 1
                emit("))")
                track("))")
                i += 2
                continue
            shift = arith_depth > 0
        elif mode == "default":
            top = frames[-1] if frames else ""
            opened, width = None, 1
            if top in _ARITH_FRAMES:
                if command.startswith("$((", i):
                    opened, width = "A", 3
                elif command.startswith("$(", i):
                    opened, width = "S", 2
                elif command.startswith("$[", i):
                    opened, width = "B", 2
                elif command.startswith("$$", i):
                    width = 2  # the shell's PID, not a `$(`/`$[` opener
                elif ch == "(":
                    opened = "G"
                elif ch == "[" and top in ("B", "K"):
                    opened = "K"
            elif command.startswith("((", i):
                opened, width = "A", 2
            elif command.startswith("$[", i):
                opened, width = "B", 2
            elif command.startswith("$$", i):
                width = 2
            elif ch == "(":
                opened = "S"
            elif ch in "ce":
                cases[-1] += _case_step(command, i, cases[-1])
            if ch == "`" and top != "Q":
                opened = "Q"
            if opened:
                frames.append(opened)
                cases.append(0)
            elif ((ch == "`" and top == "Q") or (ch == "]" and top in ("B", "K"))
                  or (ch == ")" and (top == "G" or (top == "S" and not cases[-1])))):
                frames.pop()
                cases.pop()
            elif ch == ")" and top == "A" and command.startswith("))", i):
                frames.pop()
                cases.pop()
                width = 2
            if width > 1:
                emit(command[i:i + width])
                track(command[i:i + width])
                i += width
                continue
            shift = bool(frames) and frames[-1] in _ARITH_FRAMES
        if command.startswith("<<", i) and not shift:
            m = HEREDOC_RE.match(command, i)
            if m:
                word = m.group(2) or m.group(3) or m.group(4)
                emit(m.group(0))
                # A quoted word (`<<'W'`, `<<"W"`, `<<\\W`) makes the body inert.
                quoted = bool(m.group(2) or m.group(3) or "\\" in m.group(0))
                pending.append((word, bool(m.group(1)), quoted, seg_slot))
                i = m.end()
                continue
        if ch == "\n" and pending:
            # The rest of the `<<WORD` line itself (e.g. `&& git commit` or `| tee out;
            # git push`) was scanned above; the bodies start here, one per operator.
            emit(ch)
            out.append("")
            seg_slot = len(out) - 1
            i += 1
            for word, dash_form, quoted, slot in pending:
                # Real bash only strips *leading tabs* for `<<-`, but any leading
                # whitespace here is a reasonable proxy; without `<<-`, bash requires
                # the terminator at column 0. A trailing `\r` (CRLF body) is tolerated
                # either way. No terminator: not a real heredoc (e.g. `$((1<<3))`), so
                # keep the text; a spurious check is safer than hiding a later git
                # command.
                span = terminator_span(word, dash_form, i)
                if span is not None:
                    if not quoted and depth < MAX_HEREDOC_DEPTH:
                        out[slot] += _heredoc_commands(command[i:span[0]], mode, depth)
                    i = span[1]
            pending = []
            continue
        emit(ch)
        if ch in "()":
            track(ch)
        if starts_segment(i):
            out.append("")
            seg_slot = len(out) - 1
        i += 1
    return "".join(out), bool(frames)


# Marks the `$( )` of a heredoc body's command (see _heredoc_commands), so the `||`
# readings of _cd_dirs can see through it.
_SLOT_MARK = "\x01"
_HEREDOC_SUBST_RE = re.compile(r"\\[\s\S]|\$\(\(|\$\(|`")
_BACKTICK_END_RE = re.compile(r"(?:[^`\\]|\\[\s\S])*`")
_PAREN_RE = re.compile(r"[()]")
# A bracket outside quotes and escapes (an unterminated quote falls through, as in
# _CD_SPLIT_RE, so at most one scan per kind runs to the end).
_BRACKET_RE = re.compile(r"""\\[\s\S]|"(?:[^"\\]|\\[\s\S])*"|'[^']*'|([()])""")


def _heredoc_commands(body, mode, depth):
    """The `$( )` and backtick commands of an unquoted heredoc body, which the shell
    runs when it expands the body, as `$(cmd); $(cmd); ` (each stripped as a command, with
    its parentheses balanced so none restores a cwd it shouldn't); "" with none. An
    escaped `$` or backtick is text, and `$((` an arithmetic. Linear: each scan resumes
    where the last substitution ended."""
    found, pos = [], 0
    while True:
        m = _HEREDOC_SUBST_RE.search(body, pos)
        if not m:
            break
        pos = m.end()
        if m.group(0)[0] == "\\" or m.group(0) == "$((":
            continue
        if m.group(0) == "`":
            closed = _BACKTICK_END_RE.match(body, pos)
            inner_end = closed.end() - 1 if closed else len(body)
            pos = closed.end() if closed else len(body)
        else:
            level, inner_end = 1, len(body)
            for p in _BRACKET_RE.finditer(body, pos):
                if not p.group(1):
                    continue
                level += 1 if p.group(1) == "(" else -1
                if not level:
                    inner_end = p.start()
                    break
            pos = min(len(body), inner_end + 1)
        found.append(body[m.end():inner_end])
    parts = []
    for inner in found:
        text = _strip(inner, mode, depth + 1)[0]
        level = 0
        for p in _PAREN_RE.finditer(text):
            level += 1 if p.group(0) == "(" else -1
            if level < 0:
                break
        if level:
            text = text.replace("(", " ").replace(")", " ")
        parts.append(_SLOT_MARK + "$(" + text + "); ")
    return "".join(parts)


# A command-position substitution whose command is `echo`/`printf` (group 3, 4: the
# backtick form's verb and arguments; 5, 6: the `$( )` form's); group 1-2 the lead.
_SUBST_ECHO_RE = re.compile(
    r"(^|[;&|\n({])([ \t]*)(?:`[ \t]*(echo|printf)[ \t]+([^`]*)`"
    r"|\$\([ \t]*(echo|printf)[ \t]+([^()`$]*)\))")
_SUBST_META_RE = re.compile(r"[;&|<>()$`\\\n]")


def _subst_commands(command):
    """`command` with each `echo`/`printf` substitution standing at command position
    (`` `echo cd b`; git commit ``, `$(echo cd b); git commit`) replaced by its literal
    output, which the shell runs as a command; None when there is none. Narrow: the
    arguments must be plain words (no expansion, escape or operator). Linear: each match
    stops at the next backtick or bracket."""
    def repl(m):
        verb, args = (m.group(3), m.group(4)) if m.group(3) else (m.group(5), m.group(6))
        if "\\" in args or "$" in args:
            return m.group(0)
        try:
            words = shlex.split(args)
        except ValueError:
            return m.group(0)
        if verb == "printf":
            words = words[:1]  # the format; extra arguments are not printed without a `%`
            if "%" in "".join(words):
                return m.group(0)
        else:
            while words and re.fullmatch(r"-[neE]+", words[0]):
                words.pop(0)
        text = " ".join(words)
        if not text.strip() or _SUBST_META_RE.search(text):
            return m.group(0)
        return m.group(1) + m.group(2) + text
    result = _SUBST_ECHO_RE.sub(repl, command)
    return None if result == command else result


def _literal_output(inner, depth=0, bounded=False):
    """Provable literal output of a narrow echo/printf command, or None."""
    if depth >= MAX_SUBST_DEPTH:
        raise TooManyTargets
    if len(inner) > MAX_CD_PATH_CHARS:
        if bounded and re.match(r"[ \t]*(?:echo|printf)[ \t]+", inner):
            raise TooManyTargets
        return None
    m = re.fullmatch(r"[ \t]*(echo|printf)[ \t]+([\s\S]*?)[ \t]*", inner)
    if not m:
        return None
    args = m.group(2)
    # An entire nested substitution is one shell word; evaluate only another literal
    # echo/printf, never a general command or an expression embedded in a word.
    nested = re.fullmatch(r"(.*?)(\$\([\s\S]*\))(.*?)", args)
    if nested:
        start = args.find("$(")
        end = _subst_end(args, start)
        if end is None or "$" in args[:start] + args[end:]:
            return None
        output = _literal_output(args[start + 2:end - 1], depth + 1, bounded)
        if output is None or _SUBST_META_RE.search(output):
            return None
        quote, escaped = None, False
        for char in args[:start]:
            if escaped:
                escaped = False
            elif char == "\\" and quote != "'":
                escaped = True
            elif char == quote:
                quote = None
            elif char in "'\"" and quote is None:
                quote = char
        if quote == "'":
            return None
        replacement = (output.strip().replace("\\", "\\\\").replace('"', '\\"')
                       if quote == '"' else shlex.quote(output.strip()))
        args = args[:start] + replacement + args[end:]
    if "$" in args or "`" in args or re.search(r"[;&|<>]", args):
        return None
    try:
        words = shlex.split(args)
    except ValueError:
        return None
    if m.group(1) == "echo":
        while words and re.fullmatch(r"-[neE]+", words[0]):
            words.pop(0)
        output = " ".join(words)
    else:
        if len(words) != 1 or "%" in words[0]:
            return None
        output = words[0].replace("\\n", "\n")
        if "\\" in output:
            return None
    return output if output.strip() and not re.search(r"[;&|<>()$`]", output) else None


_STATIC_TOKEN_RE = re.compile(r"""\\[\s\S]|'[^']*'|\beval[ \t]+"\$\(|"|#|<<|\n|\$\(""")
_LITERAL_COMMAND_RE = re.compile(r"[ \t]*(?:echo|printf)[ \t]+")


def _static_command_reading(command, bounded=False):
    """One reading with literal command-position substitutions/eval made visible."""
    out, pos, scanned, changed, pending = [], 0, 0, False, []
    escaped = -1  # where the last backslash escape ended: a `#` there is no comment
    for m in _STATIC_TOKEN_RE.finditer(command):
        start = m.start()
        if start < scanned:
            continue
        token = m.group(0)
        if token[0] in "\\'":
            escaped = m.end() if token[0] == "\\" else escaped
            continue
        if token == '"':
            scanned = _double_quote_end(command, start) or len(command)
            continue
        if token == "#":
            if start != escaped and (start == 0 or command[start - 1] in " \t\n;&|()"):
                end = command.find("\n", start)
                scanned = len(command) if end < 0 else end
            continue
        if token == "<<":
            if command[start - 1:start] == "<" or command[start + 2:start + 3] == "<":
                continue
            heredoc = HEREDOC_RE.match(command, start)
            if heredoc:
                pending.append((heredoc.group(2) or heredoc.group(3) or heredoc.group(4),
                                bool(heredoc.group(1))))
                scanned = heredoc.end()
            continue
        if token == "\n":
            scanned = m.end()
            for word, dash in pending:
                while scanned < len(command):
                    end = command.find("\n", scanned)
                    end = len(command) if end < 0 else end
                    line = command[scanned:end].rstrip("\r")
                    scanned = min(end + 1, len(command))
                    if (line.lstrip("\t") if dash else line) == word:
                        break
            pending.clear()
            continue
        lead = start
        while lead > pos and command[lead - 1] in " \t":
            lead -= 1
        if start < pos or (lead > pos and command[lead - 1] not in ";&|\n({"):
            continue
        is_eval = m.group(0).startswith("eval")
        sub_start = command.find("$(", start, m.end()) if is_eval else start
        if not _LITERAL_COMMAND_RE.match(command, sub_start + 2):
            continue
        end = _subst_end(command, sub_start, bounded=bounded)
        if end is None:
            break
        outer_end = end + 1 if is_eval and command[end:end + 1] == '"' else end
        if is_eval and outer_end == end:
            continue
        output = _literal_output(command[sub_start + 2:end - 1], bounded=bounded)
        if output is None:
            continue
        out.extend((command[pos:start], output.rstrip("\n")))
        pos, scanned, changed = outer_end, outer_end, True
    return "".join(out) + command[pos:] if changed else None


_CONDITIONAL_CD_RE = re.compile(
    r"""\\[\s\S]|"(?:[^"\\]|\\[\s\S])*"|'[^']*'|"""
    r"""\b(then|else)(\s+)cd([ \t]+(?:"(?:[^"\\]|\\[\s\S])*"|'[^']*'|\\[\s\S]|[^\s;&|()'"\\])+)""")


def _conditional_readings(command, bounded):
    """Read simple if branch cds as optional, since the condition is unknown."""
    if not re.search(r"\bif\b", command):
        return ()
    matches = [m for m in _CONDITIONAL_CD_RE.finditer(command) if m.group(1)]
    if len(matches) > MAX_OR_ENUM:
        if bounded:
            raise TooManyTargets
        return ()
    if bounded and len(command) * (2 ** len(matches) - 1) > MAX_SCAN_WORK:
        raise TooManyTargets
    readings = []
    for picks in itertools.product((False, True), repeat=len(matches)):
        if not any(picks):
            continue
        parts, pos = [], 0
        for match, skip in zip(matches, picks):
            parts.append(command[pos:match.start()])
            parts.append(match.group(1) + match.group(2) + (":" if skip else
                         "cd" + match.group(3)))
            pos = match.end()
        readings.append("".join(parts) + command[pos:])
    return tuple(readings)


def _native_path(path):
    """On Windows, translate a Git Bash / MSYS drive path (`/c/Users/me`) to `C:/Users/me`
    and expand `~`, so it can be joined with the payload cwd."""
    path = os.path.expanduser(path)
    if os.name == "nt":
        path = MSYS_DRIVE_RE.sub(lambda m: m.group(1).upper() + ":/", path, count=1)
    return path


def _redact_diff(diff):
    """Replace the hunks of files matching REDACT_FILE_PATTERNS with `[redacted]`,
    keeping each file's header lines (and so its name), and drop the context text git
    appends to every hunk header."""
    out = []
    mode = "keep"  # "keep", "header" (a redacted file's header lines) or "drop" (its hunks)
    for line in diff.splitlines(keepends=True):
        line = HUNK_HEADER_RE.sub(r"\1", line, count=1)
        header = DIFF_HEADER_RE.match(line.rstrip("\r\n"))
        if header or line.startswith("new file: "):  # the latter: untracked-file blocks
            parts = [part for name in (header.groups() if header else ()) if name
                     for part in name.split("/")]
            redact = any(fnmatch.fnmatchcase(part.lower(), pattern)
                         for part in parts for pattern in REDACT_FILE_PATTERNS)
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


def _diff_prefix(diff, limit):
    """Bound the text scrubbed for sending to a generous multiple of `limit` so huge diffs
    stay fast. The cut falls on a line boundary: a secret split mid-line could otherwise
    escape its pattern, and whole lines keep key blocks detectable (an unpaired BEGIN
    followed by body lines redacts to its hunk's end)."""
    size = max(int(limit or 0), 0) * 4 + (1 << 16)
    if len(diff) <= size:
        return diff
    cut = diff.rfind("\n", 0, size)
    if cut < 0:
        return "[diff omitted: first line too long]\n"
    return diff[: cut + 1]


def _scrub_key_markers(lines):
    """Redact the private key markers left unpaired in one hunk's lines: an END just after
    a key body line redacts from the hunk's start (keeping its `@@` line), a BEGIN just
    before one (past any `Name: value` header lines, which also count when only they, blank
    lines and short base64 lines run to the hunk's end or an END, as in a cut encrypted
    block) to the hunk's end, an END just after a literal backslash-n (and spaces) the base64
    and escape text before it on its line, a BEGIN just before one the base64 and escape
    text after it, and any other marker (e.g. a prose mention) only itself. A PGP checksum
    line counts as a key body line before an END. Returns the lines and whether any marker
    was found."""
    def blank(line):
        return not line.strip(" \t\r\n+-")

    def scrub_escaped_end(line):
        out, lo = [], 0
        for end in KEY_END_RE.finditer(line):
            seg = line[lo:end.start()]
            text = seg.rstrip(" \t")
            if text.endswith("\\n"):
                seg = text[:len(text) - ESCAPED_KEY_TEXT_RE.match(text[::-1]).end()]
                out.append(seg + "[redacted]")
            else:
                out.append(seg + end.group(0))
            lo = end.end()
        return "".join(out) + line[lo:]

    def scrub_escaped_begin(line):
        out, lo = [], 0
        for begin in KEY_BEGIN_RE.finditer(line):
            if begin.start() < lo:
                continue
            text = ESCAPED_BEGIN_TEXT_RE.match(line, begin.end())
            if text:
                out.append(line[lo:begin.start()] + "[redacted]")
                lo = text.end()
        return "".join(out) + line[lo:]

    found = False
    cut, prev = None, None  # prev: the last non-blank line before the current one
    for i, line in enumerate(lines):
        if blank(line):
            continue
        if KEY_END_RE.search(line):
            found = True
            if prev is not None and (KEY_LINE_RE.match(lines[prev]) or CRC_LINE_RE.match(lines[prev])):
                cut = i
        prev = i
    if cut is not None:
        for end in KEY_END_RE.finditer(lines[cut]):
            pass
        start = 1 if cut and HUNK_SPLIT_RE.match(lines[0]) else 0
        lines[start:cut + 1] = ["[redacted]" + lines[cut][end.end():]]
    # nxt: the first non-blank, non-header line after the current one; header: whether a
    # header line lies between the two; short: whether only header, blank and base64-only
    # lines run from the current line to the hunk's end or an END, short_header: whether a
    # header line is among them
    begin_at, nxt, header = None, None, False
    short, short_header = True, False
    for i in range(len(lines) - 1, -1, -1):
        if blank(lines[i]):
            continue
        if KEY_BEGIN_RE.search(lines[i]):
            found = True
            if (KEY_LINE_RE.match(lines[nxt]) if nxt is not None else header) or (
                    short and short_header):
                begin_at = i
        if KEY_END_RE.search(lines[i]):
            short, short_header = True, False
        elif KEY_HEADER_LINE_RE.match(lines[i]):
            short_header = True
        elif not BASE64_LINE_RE.match(lines[i]):
            short, short_header = False, False
        if KEY_HEADER_LINE_RE.match(lines[i]):
            header = True
        else:
            nxt, header = i, False
    if begin_at is not None:
        begin = KEY_BEGIN_RE.search(lines[begin_at])
        newline = "\n" if lines[-1].endswith("\n") else ""
        lines[begin_at:] = [lines[begin_at][:begin.start()] + "[redacted]" + newline]
    if found:
        lines = [line if "-----" not in line else KEY_END_RE.sub("[redacted]", KEY_BEGIN_RE.sub(
                     "[redacted]", scrub_escaped_begin(scrub_escaped_end(line))))
                 for line in lines]
    return lines, found


def _scrub(text):
    """Replace common secret token shapes (API keys, tokens, private key blocks, long
    base64 runs). Unpaired key markers are handled per hunk (report text is one hunk) by
    _scrub_key_markers, and a hunk that held key material also loses short base64 lines."""
    def redact_run(match):
        nonlocal runs
        # Only standard-alphabet runs (PEM key body) hint at a short last key line;
        # base64url runs (JWK, tokens) are redacted without widening the scrub.
        run = match.group(0)[len(match.group(1)):]
        if STD_BASE64_RUN_RE.search(match.group(0)):  # the old match, diff `+` included
            runs = True
        elif "/" in run or "+" in run or not all(  # whole match: a kept `\n`'s n counts
                re.search(c, match.group(0)) for c in ("[A-Z]", "[a-z]", "[0-9]")):
            return match.group(0)  # a path, long identifier or separator, not a key
        return match.group(1) + "[redacted]"

    def redact_secret(match):
        # URL credentials keep their scheme and host readable: `https://[redacted]@host`.
        return "://[redacted]@" if match.group(0).startswith("://") else "[redacted]"

    hunks = []
    for hunk in HUNK_SPLIT_RE.split(SECRET_RE.sub(redact_secret, text)):
        lines, found = _scrub_key_markers(hunk.splitlines(keepends=True))
        runs = False
        hunk = BASE64_RUN_RE.sub(redact_run, "".join(lines))
        if found:
            hunk = KEY_TAIL_LINE_RE.sub(r"\1\2[redacted]", hunk)
        elif runs:
            hunk = AFTER_RUN_TAIL_LINE_RE.sub(r"\1\2[redacted]",
                                              LONG_TAIL_LINE_RE.sub(r"\1\2[redacted]", hunk))
        hunks.append(hunk)
    return "".join(hunks)


# Set by _run_git when it returns None for anything but git's plain "no" (exit 1 with
# no output, e.g. `git config --get` on a key that is not set): the budget was spent,
# git was not found, the call raised or timed out, or git failed some other way. A
# caller that must tell the two apart clears it before the call (see _alias_pushes).
_GIT_UNASKED = contextvars.ContextVar("jev_guard_git_unasked", default=False)


def _run_git(args, cwd, deadline=None, git_opts=None):
    """Run a git subprocess with a per-call timeout bounded by the shared `deadline`
    (a time.monotonic() budget end for the whole gate() invocation); if the budget is
    already exhausted, fail open (return None) rather than block past the hook timeout.
    `git_opts` are the command's own `--git-dir`/`--work-tree` options, if it gave any.
    Why a call gave None is left in _GIT_UNASKED."""
    if git_opts:
        args = [*git_opts, *args]
    timeout = 2.0
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _GIT_UNASKED.set(True)
            return None
        timeout = max(0.1, min(timeout, remaining))
    # Resolved from PATH on every call: a bare "git" on Windows would run a `git.exe`
    # planted in the repository (the cwd). Not found counts as a failed git call.
    git = resolve_executable("git")
    if git is None:
        _GIT_UNASKED.set(True)
        return None
    try:
        result = subprocess.run([git, *args], cwd=cwd, capture_output=True, timeout=timeout)
    except Exception:
        _GIT_UNASKED.set(True)
        return None
    if result.returncode != 0:
        if result.returncode != 1 or (result.stdout or b"").strip():
            _GIT_UNASKED.set(True)
        return None
    # Decode bytes ourselves: text=True translates CR/CRLF, which corrupts valid
    # NUL-delimited Git filenames and can make retry metadata lookup miss a file.
    return result.stdout.decode("utf-8", "surrogateescape")


def _push_base(cwd, deadline=None, git_opts=None, source="", remote=None, upstream=True):
    """The ref a push is compared against: the upstream, else the push remote's
    (remote.pushDefault, or origin) default branch from `refs/remotes/<remote>/HEAD`,
    else origin/main, else origin/master; None when none resolves. `source` is the
    branch pushed when it is not HEAD (its own upstream is used), `remote` the remote
    the command names, if any. `upstream` is off when the push goes to a remote or a
    branch other than the upstream's, which then says nothing about it (see
    _push_plan)."""
    ref = source + "@{u}"
    if upstream and _run_git(["rev-parse", "--verify", "--quiet", ref], cwd, deadline,
                             git_opts) is not None:
        return ref
    if not remote:
        remote = (_run_git(["config", "--get", "remote.pushDefault"], cwd, deadline, git_opts)
                  or "").strip()
    if not remote or remote.startswith("-"):
        remote = "origin"
    head = _run_git(["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"],
                    cwd, deadline, git_opts)
    if head and head.strip():
        return head.strip()
    for ref in ("origin/main", "origin/master"):
        if _run_git(["rev-parse", "--verify", "--quiet", ref], cwd, deadline, git_opts) is not None:
            return ref
    return None


def _diff_range(op, all_flag, cwd, deadline=None, base=None, git_opts=None, source="HEAD"):
    """The diff a commit makes, or the one a push of `source` sends past `base`."""
    if op == "commit":
        args = ["diff", *DIFF_FORMAT_ARGS] + (["HEAD"] if all_flag else ["--cached"])
        return _run_git(args, cwd, deadline, git_opts)
    if base is None:
        return None
    return _run_git(["diff", *DIFF_FORMAT_ARGS, f"{base}...{source}"], cwd, deadline, git_opts)


def _diff_names(op, all_flag, cwd, deadline=None, base=None, git_opts=None, source="HEAD"):
    if op == "commit":
        args = (["diff", *DIFF_FORMAT_ARGS, "HEAD", "--name-only", "-z"] if all_flag
                else ["diff", *DIFF_FORMAT_ARGS, "--cached", "--name-only", "-z"])
        out = _run_git(args, cwd, deadline, git_opts)
        return out
    if base is None:
        return None
    return _run_git(["diff", *DIFF_FORMAT_ARGS, f"{base}...{source}", "--name-only", "-z"], cwd,
                    deadline, git_opts)


def _push_long_options(name):
    """The `git push` long options the word `name` may stand for: itself, else every one
    it is a prefix of (git takes a unique prefix; an ambiguous one is read as each of
    them, toward review). A `--no-<option>` negation turns its option off, so it stands
    for a harmless placeholder. Empty when git push has no such option."""
    if name in _PUSH_LONG_OPTS:
        return (name,)
    matches = tuple(opt for opt in _PUSH_LONG_OPTS if opt.startswith(name))
    if not matches and len(name) > 5 and name.startswith("--no-") and any(
            opt.startswith("--" + name[5:]) for opt in _PUSH_LONG_OPTS):
        return ("--no-",)
    return matches


def _push_runtime_word(word):
    """Whether the shell fills in `word` when the command runs (see _PUSH_RUNTIME_RE)."""
    return bool(_PUSH_RUNTIME_RE.search(_REF_SUFFIX_RE.sub("", word)))


def _push_inline_config(git_words):
    """Whether the global options before `push` (`git -c remote.origin.push=… push`) set
    config that changes what the push sends or where (see _PUSH_CONFIG_KEY_RE)."""
    for i, word in enumerate(git_words):
        if word in ("-c", "--config-env"):
            key = git_words[i + 1] if i + 1 < len(git_words) else ""
        elif word.startswith("--config-env="):
            key = word[len("--config-env="):]
        else:
            continue
        if _PUSH_CONFIG_KEY_RE.match(key):
            return True
    return False


def _push_request(args, git_words=()):
    """What the argument words of one `git push` ask for: the remote, the (source,
    destination) of each refspec, the remote refs it deletes, and whether it forces
    (`-f`, `--force*`, a `+refspec`), sends more than the named refs (_PUSH_WIDE_OPTS, a
    `*` pattern) or holds a refspec only known at run time (`$branch`). Also what makes
    the rest unreliable: an option or the remote only known at run time (`runtime`), a
    long option git push does not have (`unknown`), and inline config among `git_words`,
    the global options before `push` (`config`). `program` says it names the program
    run on the remote end (_PUSH_PROGRAM_OPTS)."""
    request = {"remote": None, "specs": [], "deletes": [], "force": False, "wide": [],
               "dynamic": False, "runtime": False, "unknown": False, "program": False,
               "config": _push_inline_config(git_words)}
    positional, delete, options, repo, i = [], False, True, None, 0
    while i < len(args):
        arg = args[i]
        i += 1
        if options and arg == "--":
            options = False
        elif options and arg.startswith("--"):
            request["runtime"] = request["runtime"] or _push_runtime_word(arg)
            name, eq, value = arg.partition("=")
            matches = _push_long_options(name)
            if not matches:
                request["unknown"] = True
            if any(m.startswith("--force") for m in matches):
                request["force"] = True
            delete = delete or "--delete" in matches
            request["program"] = request["program"] or any(
                m in _PUSH_PROGRAM_OPTS for m in matches)
            request["wide"] += [m for m in matches if m in _PUSH_WIDE_OPTS]
            if matches and not eq and all(m in _PUSH_VALUE_OPTS for m in matches):
                value = args[i] if i < len(args) else ""
                i += 1  # its value
            if matches == ("--repo",):
                repo = value  # the remote, when no positional one is given
        elif options and len(arg) > 1 and arg.startswith("-"):
            # A short cluster (`-fu`); `-o <option>` takes the next word.
            request["runtime"] = request["runtime"] or _push_runtime_word(arg)
            request["force"] = request["force"] or "f" in arg
            delete = delete or "d" in arg
            i += arg.endswith("o")
        else:
            positional.append(arg)
    if positional or repo:
        request["remote"] = positional[0] if positional else repo
        request["runtime"] = request["runtime"] or _push_runtime_word(request["remote"])
    specs = positional[1:]
    k = 0
    while k < len(specs):
        spec = specs[k]
        k += 1
        if spec == "tag" and k < len(specs):  # `tag <name>` is refs/tags/<name>
            spec = "refs/tags/" + specs[k]
            k += 1
        if spec.startswith("+"):
            request["force"] = True
            spec = spec[1:]
        if _push_runtime_word(spec):
            request["dynamic"] = True
            continue
        if "*" in spec:
            request["wide"].append("a refspec pattern")
            continue
        source, colon, dest = spec.partition(":")
        dest = dest if colon else source
        if delete or not source:
            request["deletes"].append(dest or source)
        else:
            request["specs"].append((source, dest))
    return request


def _push_plan(requests, cwd, deadline=None, git_opts=None):
    """(ranges, notes, unjudged) for a push in `cwd` given what its arguments ask for
    (see _push_request; none: a plain `git push`). ranges are the (base, source) pairs to
    diff: a named source ref against its remote-tracking counterpart
    (`refs/remotes/<remote>/<destination>`), else against _push_base. The upstream is
    the base only when it is on the remote the push names; to another remote or branch
    with no remote-tracking ref (a new branch, one never fetched) the range falls back
    to that remote's default branch and the push is noted. With no remote named, the
    destination is the one git picks (`@{push}`: branch.<name>.pushRemote, then
    remote.pushDefault, then branch.<name>.remote, then origin), so a pushDefault
    remote that was never fetched is noted too. notes are (kind,
    text) pairs for what no diff shows (a force, a remote ref deleted, more refs than
    the named ones, an unresolved refspec, an argument only known at run time, an
    option git push does not have, the program run on the remote, inline config, a
    remote that is a URL, a path or not configured, a destination with no
    remote-tracking ref); a push with one is always put to the classifier. unjudged
    says some range could not be worked out."""
    ranges, notes, unjudged = [], [], False
    current = None  # the checked-out branch, looked up when first needed

    def git(*args):
        return _run_git(list(args), cwd, deadline, git_opts)

    def tracking(remote, branch):
        """`refs/remotes/<remote>/<branch>` when that ref exists here, else None."""
        if remote and branch and _REF_PART_RE.fullmatch(branch) and not branch.startswith("refs/"):
            ref = f"refs/remotes/{remote}/{branch}"
            if git("rev-parse", "--verify", "--quiet", ref) is not None:
                return ref
        return None

    def note(kind, text):
        if (kind, text) not in notes:
            notes.append((kind, text))

    def add(base, source):
        nonlocal unjudged
        if base is None:
            unjudged = True
        elif (base, source) not in ranges:
            ranges.append((base, source))

    for request in requests or [_push_request([])]:
        remote = request["remote"]
        named = bool(remote and _REF_PART_RE.fullmatch(remote))
        known = bool(named and git("config", "--get", f"remote.{remote}.url"))
        if remote and not known:
            # Its refs can't be compared, so an empty diff says nothing about it.
            note("remote", "pushes to a URL, a path or a remote that is not configured, "
                 "so what it already holds is unknown")
        if not named:
            remote = None  # a URL or path: no remote-tracking refs to compare with
        if request["runtime"]:
            note("runtime", "an argument is only known at run time; it may force, delete "
                 "or widen the push")
        if request["unknown"]:
            note("option", "an option git push does not have; the arguments may be "
                 "misread")
        if request["program"]:
            note("program", "names the program run on the remote end (--receive-pack or "
                 "--exec)")
        if request["config"]:
            note("config", _PUSH_CONFIG_NOTE)
        if request["force"]:
            note("force", "force push: may overwrite or discard commits on the remote")
        if request["deletes"]:
            note("delete", "deletes remote ref(s): " + ", ".join(request["deletes"]))
        if request["wide"]:
            note("wide", "pushes more than the checked-out branch ("
                 + ", ".join(dict.fromkeys(request["wide"])) + ")")
        if request["dynamic"]:
            unjudged = True
            note("refspec", "a refspec is only known at run time")
        for source, dest in request["specs"]:
            if current is None:
                current = (git("symbolic-ref", "--quiet", "--short", "HEAD") or "").strip()
            branch = source[len("refs/heads/"):] if source.startswith("refs/heads/") else source
            if source in ("HEAD", "@") or (current and branch == current):
                source = "HEAD"
                if dest in ("HEAD", "@"):
                    dest = current
            elif (source.startswith("-")
                  or git("rev-parse", "--verify", "--quiet", source + "^{commit}") is None):
                unjudged = True
                note("refspec", "refspec source could not be resolved: " + source)
                continue
            dest = dest[len("refs/heads/"):] if dest.startswith("refs/heads/") else dest
            # An upstream on this remote and branch is this same remote-tracking ref.
            base = tracking(remote, dest)
            if base is None:
                if known:
                    # The source's own upstream is another branch or remote: an empty
                    # diff against it says nothing about this destination.
                    note("destination", _PUSH_DESTINATION_NOTE)
                base = _push_base(cwd, deadline, git_opts, "" if source == "HEAD" else source,
                                  remote, upstream=not known)
            add(base, source)
        if not request["specs"] and not request["deletes"] and not request["dynamic"]:
            base, base_remote, upstream = None, remote, not known
            if not request["remote"]:
                # No remote named: git picks the remote and the branch there. `@{push}`
                # is that destination's remote-tracking ref when git resolves it (not
                # for a push remote other than the upstream's under push.default
                # `simple`, where the push goes to the branch of the same name).
                dest = (git("rev-parse", "--symbolic-full-name", "@{push}") or "").strip()
                if dest.startswith("refs/remotes/"):
                    same = dest == (git("rev-parse", "--symbolic-full-name", "@{u}")
                                    or "").strip()
                    base = "@{u}" if same else dest
                else:
                    if current is None:
                        current = (git("symbolic-ref", "--quiet", "--short", "HEAD") or "").strip()
                    keys = ["remote.pushDefault"]
                    if current:
                        keys = [f"branch.{current}.pushRemote", *keys, f"branch.{current}.remote"]
                    picked = next((value for value in (
                        (git("config", "--get", key) or "").strip() for key in keys) if value),
                        "origin")
                    if (_REF_PART_RE.fullmatch(picked)
                            and git("config", "--get", f"remote.{picked}.url")):
                        # A configured remote: its own branch of this name, else it
                        # holds nobody here knows what, and the upstream (another
                        # remote's or another branch) says nothing about it.
                        base = tracking(picked, current)
                        if base is None:
                            note("destination", _PUSH_DESTINATION_NOTE)
                        base_remote, upstream = picked, False
            if known:
                # No refspec: the checked-out branch goes to its upstream when that is on
                # this remote, else to the branch of its own name there.
                if current is None:
                    current = (git("symbolic-ref", "--quiet", "--short", "HEAD") or "").strip()
                if (current and (git("config", "--get", f"branch.{current}.remote")
                                 or "").strip() == remote
                        and git("rev-parse", "--verify", "--quiet", "@{u}") is not None):
                    base = "@{u}"
                else:
                    base = tracking(remote, current)
                if base is None:
                    note("destination", _PUSH_DESTINATION_NOTE)
            if base is None:
                base = _push_base(cwd, deadline, git_opts, remote=base_remote, upstream=upstream)
            add(base, "HEAD")
    return ranges, notes, unjudged


def _untracked_files(cwd, deadline=None, git_opts=None):
    out = _run_git(["ls-files", "--others", "--exclude-standard", "--full-name", "-z"],
                   cwd, deadline, git_opts)
    return [n for n in (out or "").split("\0") if n]


def _usable_path(path):
    """`path` with environment variables expanded and MSYS/`~` translated, or None when
    it is empty or a `$`/backtick is left (a variable set earlier in the command, or a
    substitution): diffs are repo-wide, so the enclosing directory is a better guess
    than a literal `$dir` path, where git would fail and the review be skipped."""
    path = os.path.expandvars(path or "")
    if not path or "$" in path or "`" in path:
        return None
    return _native_path(path)


# Set by _scan_targets while it scans a PowerShell command: there a backslash is a path
# separator, not an escape, so `cd .\pkg` must not become `.pkg` (see _shell_split).
_POWERSHELL = contextvars.ContextVar("jev_guard_powershell", default=False)


def _shell_split(text):
    r"""shlex.split, except for a PowerShell command (see _POWERSHELL): split in non-POSIX
    mode, which keeps backslashes, and drop a word's enclosing quotes. Off Windows the
    backslashes become `/`, as pwsh reads them there (`cd .\pkg` is `./pkg`). Raises
    ValueError on an unbalanced quote, as shlex.split does."""
    if not _POWERSHELL.get():
        return shlex.split(text)
    lexer = shlex.shlex(text, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t
             for t in lexer]
    return words if os.sep == "\\" else [w.replace("\\", "/") for w in words]


def _global_opts(opts_segment):
    """(-C dir, --git-dir, --work-tree) from the matched global-options segment, each
    None when absent or unusable (see _usable_path). Several -C accumulate as in git
    (`-C a -C b` is a/b; an absolute one resets). shlex-aware so `-C "dir with space"`
    works; falls back to splitting on blanks on shlex errors (e.g. unbalanced quotes)."""
    try:
        # Without quotes or escapes shlex splits on its blanks (` \t\r\n`) too, only far
        # slower.
        tokens = (_shell_split(opts_segment) if any(q in opts_segment for q in "'\"\\")
                  else [t for t in re.split(r"[ \t\r\n]+", opts_segment) if t])
    except ValueError:
        tokens = opts_segment.split()
    dash_c, capped, values = None, False, {}
    i = 0
    while i < len(tokens):
        name, eq, value = tokens[i].partition("=")
        if tokens[i] in ("-C", "-c") or (not eq and name in ("--git-dir", "--work-tree",
                                                              "--namespace", "--super-prefix",
                                                              "--config-env", "--attr-source")):
            name, value = tokens[i], tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
        i += 1
        path = _usable_path(value) if name in ("-C", "--git-dir", "--work-tree") else None
        if path and name == "-C":
            # Capped like cd (see _join_capped): past it the -Cs are dropped (git runs
            # in the cwd the cds led to) until an absolute one.
            dash_c, capped = _join_capped(dash_c, path, capped)
        elif path:
            values[name] = path
    return dash_c, values.get("--git-dir"), values.get("--work-tree")


def _dash_c_dir(opts_segment):
    """The directory the literal uppercase `-C <dir>` options in the matched
    global-options segment lead to (see _global_opts), or None."""
    return _global_opts(opts_segment)[0]


def _or_cd_skipped(after, after_ord, op, op_ord, or_cds, or_left_cds, or_choice):
    """Whether the cd of a segment is ignored in an `||` reading: `after` / `after_ord`
    are the separator before it and, if a `||`, its ordinal among the command's `||`s;
    `op` / `op_ord` the same for the one after it. `or_choice` maps an `||` ordinal to
    "R" (the cd after it is skipped: the left side succeeded) or "L" (the cd before it
    is: the left side failed)."""
    if after == "||" and (not or_cds or (or_choice and or_choice.get(after_ord) == "R")):
        return True
    return op == "||" and (not or_left_cds or bool(or_choice and or_choice.get(op_ord) == "L"))


def _has_text(segment, op):
    """Whether a segment holds anything (a blank one keeps a preceding `||`; so does the
    `_SLOT_MARK$` of a heredoc command)."""
    text = segment.strip()
    return bool(text) and not (op == "(" and text == _SLOT_MARK + "$")


def _is_subst(command, sep):
    """Whether the `(` separator `sep` opens one of the heredoc commands _strip puts
    before a command (`_SLOT_MARK$(`), which is transparent to a preceding `||`."""
    return command[sep.start() - 2:sep.start()] == _SLOT_MARK + "$"


def _or_cd_ordinals(command):
    """The ordinals (among all the `||` separators of _CD_SPLIT_RE, in order) of those
    with a `cd` segment right before or after them, which is where an `||` choice
    matters (see _or_cd_skipped)."""
    ords, pos, after, after_ord, ors = set(), 0, None, None, 0
    opened = []  # (after, after_ord) at each open substitution, as in _cd_dirs
    for sep in _CD_SPLIT_RE.finditer(command):
        op = sep.group(1)
        if sep.group(2) or op is None:
            continue
        segment = command[pos:sep.start()]
        this_ord = None
        if op == "||":
            this_ord, ors = ors, ors + 1
        if (after == "||" or op == "||") and _cd_target(segment) is not None:
            if after == "||":
                ords.add(after_ord)
            if this_ord is not None:
                ords.add(this_ord)
            if len(ords) > MAX_OR_ENUM:
                return sorted(ords)
        if after != "||" or _has_text(segment, op):
            after, after_ord = op, this_ord
        if op == "(" or (op == "`" and not (opened and opened[-1][0] == "`")):
            opened.append((op, after, after_ord, _is_subst(command, sep)))
        elif op in (")", "`") and opened and opened[-1][0] == ("(" if op == ")" else "`"):
            _, was_after, was_ord, subst = opened.pop()
            if subst:
                after, after_ord = was_after, was_ord
        pos = sep.end()
    return sorted(ords)


def _cd_dirs(command, restore=True, case_cds=True, or_cds=True, or_left_cds=True,
             or_choice=None):
    """([segment ends], [cd directory in effect after each]) for the command's
    &&/||/&/;/newline/`(`/`)`/`{`/backtick segments (successive cds accumulate; relative
    results stay relative to the payload cwd; None before any cd). A `(` subshell or a
    backtick substitution gets its own cwd: its closing `)`/backtick restores the one in
    effect at the opener. A case pattern's `)` splits nothing, so a `pattern) cd dir`
    segment moves nothing (which branch runs is unknown). The readings _scan_targets
    adds when there is a `case`: restore=False, where no `)` restores the cwd and every
    `)` splits (a case pattern's `)` taken for a subshell's, or a branch's cd), and
    case_cds=False, where a cd inside a case statement (or after any `case` word, up to
    its `esac`) is ignored. With a `||`, it adds or_cds=False, where a cd right after a
    `||` is ignored (`cd a || cd b && git push` runs in a when a exists), and
    or_left_cds=False, where a cd right before one is (the cd failed: `cd nope || cd b`
    runs in b). With few enough `||`s, _scan_targets instead passes or_choice, one
    of the two for each (see _or_cd_skipped). Built once per
    command so resolving each git match is a bisect, not a rescan of its prefix."""
    ends, dirs = [], []
    result, capped, pos = None, False, 0
    list_start = (result, capped)
    after, after_ord, ors = None, None, 0  # the separator before the segment; `||` count
    saved = []  # (opener, cd result and capped at it) for each open `(` / backtick
    # Open `case` statements per frame (cases[0]: no frame), as in _strip: while the
    # innermost frame has one, a lone `)` ends a pattern, not the subshell.
    cases = [0]
    open_cases = 0  # sum(cases)
    # With case_cds=False, also any `case` word (even one whose word _CASE_RE doesn't
    # take, `case $(a $(b $(c))) in`, which opens no frame above) up to its `esac` word:
    # the len(saved) at each, so a lone `)` at that depth is its pattern's (the `)`s of
    # frames opened after it, like the word's own `$( )`s, still close those frames).
    loose = []
    for sep in _CD_SPLIT_RE.finditer(command):
        op = sep.group(1)
        if sep.group(2):
            step = _case_step(command, sep.start(), cases[-1])
            cases[-1] += step
            open_cases += step
            if not case_cds and _is_word(command, sep.start(), sep.end()):
                if sep.group(2) == "case":
                    loose.append(len(saved))
                elif loose:
                    loose.pop()
            continue
        if op is None:
            continue  # an escape or a whole quoted string: not a separator
        if op == ")" and ((cases[-1] and restore) or (loose and loose[-1] == len(saved))):
            continue  # a case pattern's `)`
        segment = command[pos:sep.start()]
        this_ord = None
        if op == "||":
            this_ord, ors = ors, ors + 1
        if (op != "&" and (case_cds or not (open_cases or loose))
                and not _or_cd_skipped(after, after_ord, op, this_ord, or_cds, or_left_cds,
                                       or_choice)):
            result, capped = _cd_segment_dir(segment, result, capped)
        if op == "&":
            result, capped = list_start
        if op in (";", "&") or (op == "\n" and (segment.strip() or after not in ("&&", "||"))):
            list_start = (result, capped)
        if after != "||" or _has_text(segment, op):
            after, after_ord = op, this_ord  # a blank segment (`||` newline `cd b`) keeps the `||`
        if (op == "(" and restore) or (op == "`" and not (saved and saved[-1][0] == "`")):
            # The heredoc commands _strip puts before a command are transparent to the
            # `||` the segment before it follows (`cd a || cd b <<EOF` with a `$( )`
            # body still has `cd b` after the `||`).
            saved.append((op, result, capped, after, after_ord, _is_subst(command, sep), list_start))
            list_start = (result, capped)
            cases.append(0)
        elif op in (")", "`") and saved and saved[-1][0] == ("(" if op == ")" else "`"):
            # Restored before it is recorded: a git match starting at this `)` (e.g.
            # `X=$(cd sub) git push`) runs outside the subshell.
            _, result, capped, was_after, was_ord, subst, list_start = saved.pop()
            if subst:
                after, after_ord = was_after, was_ord
            open_cases -= cases.pop()
            while loose and loose[-1] > len(saved):
                loose.pop()  # a `case` left open inside the closed frame
        ends.append(sep.start())
        dirs.append(result)
        pos = sep.end()
    return ends, dirs


def _cd_prefix_dir(command, git_start, cd_index=None):
    """Return the directory the `cd <dir>` segments before the git command lead to,
    or None when there is no cd. A -C in the git segment is resolved relative to it.
    Only whole segments count: a `cd` piped into or backgrounded before the git
    (`cd x | git push`) runs in a subshell and doesn't move it."""
    ends, dirs = cd_index or _cd_dirs(command)
    k = bisect.bisect_right(ends, git_start) - 1
    return dirs[k] if k >= 0 else None


def _merge_cands(*groups):
    """The distinct (dir, capped) candidates of `groups` in order, at most
    MAX_CASE_BRANCHES (the first ones)."""
    return tuple(dict.fromkeys(c for group in groups for c in group))[:MAX_CASE_BRANCHES]


def _case_branch_dirs(command, git_starts, or_cds=True, or_left_cds=True, or_choice=None):
    """For each of the (sorted) git match starts, the (cd directory, capped) candidates
    that may be in effect there in the reading where a case statement's branches are
    taken one at a time (subshells restore as in _cd_dirs). A pattern's `)` splits, so
    `pattern) cd dir` moves; a `;;` ends the branch, the next one starting from the
    candidates before the case (after a `;&` or `;;&`, also from the end of this one,
    which may fall through); after the matching `esac` the candidates are those before
    the case and each branch's end (`case x in x) cd a;; y) cd b;; esac` leaves a, b or
    neither). A cd moves every candidate, and a later case starts from all of them. At
    most MAX_CASE_BRANCHES are kept (the first, so the cwd before the first case always
    is), keeping it linear; only the candidates at the git starts are kept, not one set
    per segment. The `||` arguments skip a cd as in _cd_dirs."""
    out, k = [], 0
    after, after_ord, ors = None, None, 0  # as in _cd_dirs
    cands, recorded, pos = ((None, False),), ((None, False),), 0
    list_start = cands
    saved = []  # (opener, candidates at it) for each open `(` / backtick
    # Open case statements per frame (frames[0]: no frame), innermost last: (candidates
    # before it, {branch-end candidate: None}).
    frames = [[]]
    for sep in _CD_SPLIT_RE.finditer(command):
        op = sep.group(1)
        if sep.group(2):
            step = _case_step(command, sep.start(), len(frames[-1]))
            if step == 1:
                frames[-1].append((cands, {}))
            elif step == -1:
                before, branch_ends = frames[-1].pop()
                cands = _merge_cands(before, branch_ends, cands)
            continue
        if op is None:
            continue  # an escape or a whole quoted string: not a separator
        segment = command[pos:sep.start()]
        this_ord = None
        if op == "||":
            this_ord, ors = ors, ors + 1
        target = _cd_target(segment)
        if op != "&" and target is not None and not _or_cd_skipped(after, after_ord, op, this_ord, or_cds,
                                                      or_left_cds, or_choice):
            cands = _merge_cands(_join_capped(r, target, c) for r, c in cands)
        if op == "&":
            cands = list_start
        if op in (";", "&") or (op == "\n" and (segment.strip() or after not in ("&&", "||"))):
            list_start = cands
        if after != "||" or _has_text(segment, op):
            after, after_ord = op, this_ord
        i = sep.start()
        if (op == ";" and frames[-1] and command.startswith((";;", ";&"), i)
                and command[i - 1:i] != ";"):
            before, branch_ends = frames[-1][-1]
            for cand in cands:
                if len(branch_ends) >= MAX_CASE_BRANCHES:
                    break
                branch_ends.setdefault(cand)
            falls = not command.startswith(";;", i) or command[i + 2:i + 3] == "&"
            cands = _merge_cands(before, cands) if falls else before
        elif op == ")" and frames[-1]:
            pass  # a case pattern's `)`: splits, restores nothing
        elif op == "(" or (op == "`" and not (saved and saved[-1][0] == "`")):
            saved.append((op, cands, after, after_ord, _is_subst(command, sep), list_start))
            list_start = cands
            frames.append([])
        elif op in (")", "`") and saved and saved[-1][0] == ("(" if op == ")" else "`"):
            _, cands, was_after, was_ord, subst, list_start = saved.pop()
            if subst:
                after, after_ord = was_after, was_ord
            frames.pop()
        while k < len(git_starts) and git_starts[k] < i:
            out.append(recorded)
            k += 1
        recorded = cands
        pos = sep.end()
    out.extend([recorded] * (len(git_starts) - k))
    return out


def _cd_segment_dir(segment, result, capped=False):
    """(dir, capped): `result` joined with the directory of a `cd <dir>` segment, else
    unchanged; past MAX_CD_PATH_CHARS the cds are dropped (the payload cwd) until an
    absolute one (see _join_capped)."""
    target = _cd_target(segment)
    if target is None:
        return result, capped
    return _join_capped(result, target, capped)


def _cd_target(segment):
    """The directory of a `cd <dir>` segment, or None (not a cd, or no usable dir)."""
    m = CD_RE.match(segment)
    if not m:
        return None
    argument = m.group(1).strip()
    # Plain path tokens have no shell quoting, options or redirections to parse.
    if re.fullmatch(r"[A-Za-z0-9_./~:][A-Za-z0-9_./~:-]*", argument):
        return _usable_path(argument)
    try:
        tokens = _shell_split(m.group(1))
    except ValueError:
        tokens = m.group(1).split()
    # Drop options (-P, --) and redirections (2>/dev/null) around the directory.
    tokens = [t for t in tokens if not t.startswith("-") and not re.match(r"^(?:\d*[<>]|&>)", t)]
    if len(tokens) != 1:
        return None
    return _usable_path(tokens[0])


def _join_capped(result, target, capped):
    """(dir, capped) for `result` (None: none yet) joined with `target`, shared by cd
    and -C accumulation. A join longer than MAX_CD_PATH_CHARS makes the directory None
    (a path that long is unlikely to exist, and git failing there would skip the
    review), and `capped` then ignores every later relative target too (they would
    extend the dropped path) until an absolute one starts over."""
    if capped and not (os.path.isabs(target) or target.startswith(("/", "\\"))):
        return result, capped
    joined = os.path.join(result, target) if result else target
    if len(joined) > MAX_CD_PATH_CHARS:
        return None, True
    return joined, False


def _git_target(command, match, cwd, add_end=-1, cd_index=None, cd_dir=_FROM_INDEX,
                all_args=None):
    """(op, cwd, all_flag, git_opts) for one GIT_COMMAND_RE match in the stripped command.
    `add_end` is where the command's first `git add` match ends (None: there is none;
    -1: search the prefix here); `cd_index` is _cd_dirs(command), if already built;
    `cd_dir`, if given, is the cd directory in effect instead (None: no cd); `all_args`
    is whether its own arguments hold -a/--all (see _all_args; None: search them here)."""
    opts_segment, op = match.group(1), match.group(2)
    # The shell resolves -C relative to any directory an earlier cd moved to.
    if cd_dir is _FROM_INDEX:
        cd_dir = _cd_prefix_dir(command, match.start(), cd_index)
    if cd_dir:
        cwd = os.path.join(cwd, cd_dir)
    dash_c_dir, git_dir, work_tree = _global_opts(opts_segment)
    if dash_c_dir:
        cwd = os.path.join(cwd, dash_c_dir)
    # --git-dir/--work-tree resolve against the cwd git ends up in, and the diff runs
    # with the same options (git_opts). A --work-tree alone leaves the repository to
    # discovery from the cwd, so the cwd is kept (moving it to the work tree could find
    # another repository, or none); with a --git-dir too, the diff runs from the work tree.
    git_opts = ()
    if git_dir:
        git_opts += ("--git-dir", os.path.join(cwd, git_dir))
    if work_tree:
        work_tree = os.path.join(cwd, work_tree)
        git_opts += ("--work-tree", work_tree)
        if git_dir:
            cwd = work_tree
    # `git add … && git commit` stages nothing until it runs, so compare the work tree
    # with HEAD instead of the (still empty) index; likewise for commit -a/--all.
    # Searched from the match rather than split off a copy of the rest of the command,
    # which was quadratic on many git invocations.
    if all_args is None:
        seg_end = _SEGMENT_END_RE.search(command, match.end())
        segment = command[match.end():seg_end.start() if seg_end else len(command)]
        all_args = bool(ALL_FLAG_RE.search(segment))
    if add_end == -1:
        add = GIT_ADD_RE.search(command[:match.start() + 1])
        add_end = add.end() if add else None
    all_flag = op == "commit" and (all_args
                                   or (add_end is not None and add_end <= match.start() + 1))
    return op, cwd, all_flag, git_opts or None


def _all_args(command, matches):
    """Whether each GIT_COMMAND_RE match (in order) has -a/--all in its own arguments, as
    ALL_FLAG_RE on command[match.end():next separator] says (see _git_target). Matches
    with no separator between them (`git commit -a (` * N) share that segment's end, so
    each one's segment is a suffix of the first one's and searching each was quadratic;
    the shared segment is searched once instead: a flag in a match's segment either
    starts after its end (blank-preceded, like the last one found from the first match)
    or right at it (the slice's `^`)."""
    flags, seg_end, last = [], -1, None
    for match in matches:
        start = match.end()
        if start > seg_end:
            found = _SEGMENT_END_RE.search(command, start)
            seg_end = found.start() if found else len(command)
            last = None
            for flag in ALL_FLAG_RE.finditer(command, start, seg_end):
                last = flag.start(1)
        flags.append((last is not None and last > start)
                     or bool(_ALL_FLAG_AT_RE.match(command, start, seg_end)))
    return flags


def _scan_targets(raw_command, base_cwd, max_targets=None, powershell=False,
                  command_re=GIT_COMMAND_RE):
    """The distinct (op, cwd, all_flag, git_opts) targets of every git commit/push in the
    command; more than `max_targets` (if given) raise TooManyTargets. The cd index and first `git add` are computed once, so a command with
    thousands of git invocations still resolves each one in (near) constant time.
    `powershell` keeps backslashes in cd and -C paths (see _shell_split). `command_re`
    is GIT_COMMAND_RE with other subcommands in its second group (see _alias_signals)."""
    token = _POWERSHELL.set(bool(powershell))
    try:
        return _scan_command_targets(raw_command, base_cwd, max_targets, command_re)
    finally:
        _POWERSHELL.reset(token)


def _scan_command_targets(raw_command, base_cwd, max_targets, command_re=GIT_COMMAND_RE):
    """_scan_targets' body, with _POWERSHELL set."""
    # The joins are right outside comments, quotes and quoted heredoc bodies only (a
    # `\`-newline ends a comment, and joining it would hide the next line), so a
    # command they change is scanned both joined and as given.
    joined = _HEREDOC_SPLIT_RE.sub(lambda m: m.group(0).replace("\\\n", ""),
                                   _SPLIT_OPERATOR_RE.sub("", raw_command))
    # With arithmetic in the command, a `<<` may be a shift or a heredoc, and a wrong
    # guess either way can hide a later command; scan both readings and keep the union,
    # plus the no-shift reading when the default one ends inside a frame (see _strip).
    # The readings differ only in how they take a `<<`, so without one a single scan
    # does (and a long quoted command's scan time doesn't double).
    commands = []
    raws = (joined,) if joined == raw_command else (joined, raw_command)
    # Also with a command-position `echo cd b` substitution replaced by its output, which
    # the shell runs as a command (see _subst_commands).
    raws += tuple(r for r in map(_subst_commands, raws) if r is not None)
    raws += tuple(r for raw in raws[:2]
                  if (r := _static_command_reading(raw, max_targets is not None)) is not None)
    for raw_command in raws:
        command, frame_open = _strip(raw_command)
        commands.append(command)
        if "<<" in raw_command:
            if "((" in raw_command or "$[" in raw_command:
                commands.append(_strip(raw_command, "legacy")[0])
            if frame_open:
                commands.append(_strip(raw_command, "none")[0])
    commands += [reading for command in tuple(commands)
                 for reading in _conditional_readings(command, max_targets is not None)]
    targets, seen, scan_work = [], set(), 0
    for k, command in enumerate(commands):
        if command in commands[:k]:
            continue
        matches = list(command_re.finditer(command))
        if not matches:
            continue
        add = GIT_ADD_RE.search(command)
        add_end = add.end() if add else None
        # A case word _CASE_RE doesn't take (`case $(f $(g $(h))) in`) leaves its
        # pattern `)` taken for a subshell's, restoring the cwd too early, and a cd in a
        # case branch may or may not run; so with a `case` in the command, also resolve
        # each git with no `)` restoring the cwd, and with no cd inside a case counted.
        # Also each case branch's cd taken on its own (with subshells restoring), and,
        # with a `||`, each success/failure combination of the ones with a cd next to
        # them (at most MAX_OR_ENUM, else no cd right after any, or none right before
        # any), also for the case branches (see _cd_dirs, _case_branch_dirs).
        or_readings = ()
        if "||" in command:
            ords = _or_cd_ordinals(command)
            if 0 < len(ords) <= MAX_OR_ENUM:
                or_readings = tuple({"or_choice": dict(zip(ords, picks))}
                                    for picks in itertools.product("RL", repeat=len(ords)))
            elif ords:
                if max_targets is not None:
                    raise TooManyTargets
                or_readings = ({"or_cds": False}, {"or_left_cds": False})
        has_case = bool(_CASE_WORD_RE.search(command))
        scan_work += len(command) * (len(or_readings) + 1) * (2 if has_case else 1)
        if max_targets is not None and scan_work > MAX_SCAN_WORK:
            raise TooManyTargets
        cd_indexes = [_cd_dirs(command)]
        branch_dirs = []
        resolved = {}
        if has_case:
            cd_indexes.append(_cd_dirs(command, restore=False))
            cd_indexes.append(_cd_dirs(command, case_cds=False))
            starts = [m.start() for m in matches]
            branch_dirs = [_case_branch_dirs(command, starts)]
            branch_dirs += [_case_branch_dirs(command, starts, **kw) for kw in or_readings]
        cd_indexes += [_cd_dirs(command, **kw) for kw in or_readings]
        for i, (match, all_args) in enumerate(zip(matches, _all_args(command, matches))):
            cd_dirs = [_cd_prefix_dir(command, match.start(), cd_index)
                       for cd_index in cd_indexes]
            for branch_dir in branch_dirs:
                cd_dirs.extend(cd_dir for cd_dir, _ in branch_dir[i])
            for cd_dir in dict.fromkeys(cd_dirs):
                # A target depends on the match only through these, so a run of the same
                # command with many cd readings resolves each reading once.
                key = (cd_dir, match.group(1), match.group(2), all_args,
                       add_end is not None and add_end <= match.start() + 1)
                target = resolved.get(key)
                if target is None:
                    target = resolved[key] = _git_target(
                        command, match, base_cwd, add_end, cd_dir=cd_dir, all_args=all_args)
                if target not in seen:
                    seen.add(target)
                    targets.append(target)
                    if max_targets is not None and len(targets) > max_targets:
                        raise TooManyTargets
    return targets


def _executed_span(words, parts, cmd_start, scripts=False):
    """Whether a quoted span right after `words` (and the word `parts` being built) is
    run as a command: an `eval` argument, or a shell's `-c` (`bash -lc`, `sh -c -m`,
    `pwsh -Command`, `cmd /c`) script, within the current command. With `scripts`, also
    an interpreter's (`python -c`, `node -e`) or what `iex` evaluates (see
    _SCRIPT_WORD_RE). Only the last few words are looked at (constant time per span)."""
    tail = words[max(cmd_start, len(words) - _EXEC_SPAN_WINDOW):]
    # A long word, or one of many quoted pieces (`'x''x'...`), is a plain word: reading
    # it whole for every span would be quadratic.
    word = "".join(parts) if len(parts) <= 4 else "x"
    tail = [w.lower() if len(w) <= 256 else "x"
            for w in tail + ([word] if parts else [])]
    flag, script_flag, plain = False, False, 0
    k = len(tail)
    while k:
        k -= 1
        word = tail[k]
        base = _PATH_SEP_RE.split(word)[-1]
        base = base[:-4] if base.endswith(".exe") else base
        if base in _EXEC_WORDS:
            return base == "eval" or flag and (not plain or base in _EXEC_ARG_WORDS)
        if scripts and _SCRIPT_WORD_RE.fullmatch(base):
            return base in _SCRIPT_EVAL_WORDS or script_flag and not plain
        if k and tail[k - 1] in _SHELL_OPT_WORDS:
            k -= 1  # `-o pipefail`: an option's value
            continue
        if not word.startswith(("-", "/")):
            # `su user -c`, `flock -w 10 lock -c`: a plain word or two before the flag
            plain += 1
            if plain > 2:
                return False
            continue
        flag = flag or bool(_EXEC_FLAG_RE.fullmatch(word))
        script_flag = script_flag or bool(_SCRIPT_FLAG_RE.fullmatch(word))
    return False


def _ansi_decode(content):
    """An ANSI-C `$'...'` span's text with its escapes decoded (`git pu\\x73h`), read as
    a command; an escape Python doesn't know (`\\e`, `\\cX`) leaves it as written."""
    if "\\" not in content:
        return content
    try:
        # `\e` is bash's, not Python's: no DeprecationWarning on the hook's stderr.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return content.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except UnicodeDecodeError:
        return content


def _in_redirect(sep, k):
    """Whether the `&` or `|` at sep[k] is part of a redirection operator (`>&1`, `<&3`,
    `&>`, `&>>`, `>|`), so that it doesn't end the command."""
    if sep[k] == "&":
        return bool(k) and sep[k - 1] in "<>" or sep[k + 1:k + 2] == ">"
    return bool(k) and sep[k - 1] == ">"


def _sep_ends(sep):
    """(ends, redirect) for a separator run with a `<` or `>` in it: whether it ends the
    command (a `;`, `&`, `|`, parenthesis or newline outside a redirection operator, see
    _in_redirect), and whether the next word is a redirection's target."""
    ends = redirect = False
    for k, char in enumerate(sep):
        if char in "<>":
            redirect = True
        elif char in "&|" and _in_redirect(sep, k):
            continue
        elif char in ";&|()\n":
            ends, redirect = True, False
    return ends, redirect


def _grep_pager(word):
    """Whether `word` (as written, quotes removed) is `git grep`'s option that runs a
    pager on the files found (see _GREP_PAGER_RE). Only its start is looked at."""
    return bool(_GREP_PAGER_RE.match(_PUSH_CLEAN_RE.sub("", word[:64])))


def _git_grep(clean, i):
    """Index of the `grep` word when the git word clean[i] runs `git grep` (past its
    global options, within a few words), else -1."""
    skip = False
    for j in range(i + 1, min(len(clean), i + 1 + _EXEC_SPAN_WINDOW)):
        word = clean[j]
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in _GIT_VALUE_OPTS
        else:
            return j if word == "grep" else -1
    return -1


def _quiet_command(clean, i, tokens=None, starts=()):
    """Whether the command word clean[i] (cleaned, see _command_words) only reads or
    prints its arguments: one of _INERT_READERS, or git running `grep` with no pager
    option (see _grep_pager) among the words `tokens` up to the command's end (the
    next of `starts` or `find -exec`-style option)."""
    base = _PATH_SEP_RE.split(clean[i])[-1]
    base = base[:-4] if base.endswith(".exe") else base
    if base in _INERT_READERS:
        return True
    if base != "git":
        return False
    j = _git_grep(clean, i)
    if j < 0:
        return False
    words = clean if tokens is None else tokens
    j += 1
    while j < len(words) and j not in starts and clean[j] not in _EXEC_OPTS:
        if _grep_pager(words[j]):
            return False
        j += 1
    return True


def _span_reader(words, cmd_start, redirs):
    """What the command starting at words[cmd_start] does with a quoted span given to
    it that nothing is known to run (see _text_span): True, it only reads it (an inert
    reader, see _quiet_command; `python -m <module>`); "grep", it is `git grep`; "git",
    it is another git command; False, it may run it, or the command word hasn't come
    yet (a wrapper's own argument). Only the command's first few words are looked at."""
    head = [w for k, w in enumerate(words[cmd_start:cmd_start + _EXEC_SPAN_WINDOW], cmd_start)
            if k not in redirs]
    clean = [_PUSH_CLEAN_RE.sub("", w).lower() if len(w) <= 256 else "x" for w in head]
    # A plain word after them tells whether the command word is among them yet.
    lead = _command_words(head + ["x"], clean + ["x"], {0})
    if lead[-1]:
        return False
    i = max(k for k, at in enumerate(lead) if at)
    base = _PATH_SEP_RE.split(clean[i])[-1]
    base = base[:-4] if base.endswith(".exe") else base
    if base in _INERT_READERS:
        return True
    if base == "git":
        return "grep" if _git_grep(clean, i) >= 0 else "git"
    return (base.startswith("py") and bool(_INTERPRETER_WORD_RE.fullmatch(base))
            and "-m" in clean[i + 1:i + 4])


def _text_span(words, parts, cmd_start, redirs, memo):
    """Whether a quoted span right after `words` (and the word `parts` being built)
    that nothing is known to run (see _executed_span) is only text to its command (see
    _span_reader), or the value of one of git's _GIT_TEXT_OPTS (`--grep "x"`,
    `--pretty=format:"x"`: the option's name is what comes before a `=`). Any other
    command may run it (`watch '<script>'`, `git rebase -x '<cmd>'`, `nix-shell --run
    '<cmd>'`), and so does `git grep` once a pager option (see _grep_pager) is among
    its words or starts this one (`-O'<cmd>'`). `memo` keeps the last command's answer
    and how many of its words were looked at for that option (constant time per span,
    each word once)."""
    key = (cmd_start, min(len(words), cmd_start + _EXEC_SPAN_WINDOW))
    if memo[0] != key:
        memo[:] = key, _span_reader(words, cmd_start, redirs), cmd_start, False
    if memo[1] == "grep":
        k = memo[2]
        while k < len(words) and not memo[3]:
            memo[3] = _grep_pager(words[k])
            k += 1
        memo[2] = k
        return not (memo[3] or bool(parts) and _grep_pager(parts[0]))
    if memo[1] != "git":
        return memo[1]
    opt = parts[0].partition("=")[0] if len(parts) == 1 else "" if parts else words[-1]
    return opt in _GIT_TEXT_OPTS


class _DqSpan:
    """A double-quoted span _push_lex read to its real end, with what its users take
    from a _PUSH_LEX_RE match: `lastgroup` and `group`."""
    __slots__ = ("_text", "_content")
    lastgroup = "dq"

    def __init__(self, text, content):
        self._text, self._content = text, content

    def group(self, key=0):
        return self._text if key == 0 else self._content if key == "dq" else None


def _push_lex(text, unsure=None):
    """_PUSH_LEX_RE's matches over `text`, except that a double-quoted span holding a
    substitution ends where the shell ends it: the first `"` is the substitution's own
    in `"$(echo '"')"`, and what follows it would be read as quoted (see
    _double_quote_end). A span whose end can't be found (unbalanced, nested too deep)
    stays as _PUSH_LEX_RE read it, and its start is added to `unsure` (if given): what
    comes after it may be read wrong. PowerShell quotes differently (a backtick is
    its escape, a backslash is none), so its commands are read by _PS_LEX_RE alone.
    Linear: a span is re-read once, and none after the first whose end can't be found."""
    if _POWERSHELL.get():
        return _PS_LEX_RE.finditer(text)
    if '"' not in text or ("$(" not in text and "`" not in text):
        return _PUSH_LEX_RE.finditer(text)
    return _push_lex_spans(text, unsure)


def _dq_unescape(content):
    """A double-quoted span's `content` as the command text its shell makes of it (see
    _DQ_UNESCAPE_RE, _PS_DQ_UNESCAPE_RE)."""
    if _POWERSHELL.get():
        return _PS_DQ_UNESCAPE_RE.sub(lambda m: m.group(1) or m.group(2), content)
    return _DQ_UNESCAPE_RE.sub(r"\1", content)


def _push_lex_spans(text, unsure):
    """_push_lex for a text with a double quote and a substitution in it."""
    pos, check = 0, True
    while True:
        for m in _PUSH_LEX_RE.finditer(text, pos):
            if check and m.lastgroup == "dq":
                content = m.group("dq")
                if "$(" in content or "`" in content:
                    try:
                        end = _double_quote_end(text, m.start())
                    except TooManyTargets:
                        end = None
                    if end is None:
                        check = False
                        if unsure is not None:
                            unsure.append(m.start())
                    elif end != m.end():
                        yield _DqSpan(text[m.start():end], text[m.start() + 1:end - 1])
                        pos = end
                        break
            yield m
        else:
            return


def _dq_subst(content):
    """Where the first substitution in a double-quoted span's `content` starts (`$(`
    or a backtick), -1 when it has none. A backslash-escaped one is text (`"\\$(git
    push)"`), except in PowerShell, where a backslash escapes nothing."""
    if "$(" not in content and "`" not in content:
        return -1
    if _POWERSHELL.get():
        found = [k for k in (content.find("$("), content.find("`")) if k >= 0]
        return min(found)
    for m in _DQ_SUBST_RE.finditer(content):
        if m.group(1):
            return m.start(1)
    return -1


def _push_words(text, executed_only=False, scripts=False, args=False, unsure=None):
    """The words of `text` (see _push_lex, which gets `unsure`), the content of its
    quoted spans, except
    a message (`git commit -m "explain git push"`) with no substitution in it, the
    indexes of the words that start a command (the first, and each after a `;`, `&`,
    `|`, parenthesis or newline) and of the ones that are a redirection's descriptor or
    target (`2>&1`, `> log.txt`). With `executed_only`, only the spans a shell or
    `eval` runs (see _executed_span); with `scripts` too, also an interpreter's script
    and, from its first substitution on, a double-quoted span that holds one (`echo
    "$(git push)"`), not prose such as `grep "git push"`; with `args` too, also every
    span that is not plain text to its command (see _text_span). A word with an escaped
    blank in it (`core.pager=git\\ push`) is read as such a span too (its value past a
    `NAME=` or `--opt=`), unless it is a message or text."""
    words, spans, parts, starts, redirs = [], [], [], {0}, set()
    cmd_start = 0
    message_cmd = False  # in a `git commit`/`tag`/... command, up to its end
    # Where the command is past a git word: 0 none yet, 1 its options (the subcommand
    # is next), 2 an option's value, 3 past its subcommand.
    git_at = 0
    redirect = False  # the next word is a redirection's target
    long_key, long_run = None, False  # _executed_span's answer for the long word being built
    memo = [None, False, 0, False]  # _text_span's
    message_word = False  # the word being built is a message option's value
    # For the word being built, once it has an escaped blank: None, or whether its
    # value is read (see above) and whether that value comes after a `=`.
    blank = None
    escape = "`" if _POWERSHELL.get() else "\\"

    def blank_span(word):
        value = word.partition("=")[2] if blank[1] else word
        return _ESCAPED_RE.sub(r"\1", value) if escape == "\\" else _PS_ESCAPED_RE.sub(r"\1", value)

    for m in _push_lex(text, unsure):
        kind = m.lastgroup
        if kind == "sep":
            sep = m.group(0)
            message_word = False
            if parts:
                word = "".join(parts)
                parts.clear()
                if blank is not None:
                    if blank[0]:
                        spans.append(blank_span(word))
                    blank = None
                # A lone `{}` is a placeholder (`xargs -I{} git {}`), not a group's brace.
                if word == "{}" or not _BRACES_RE.fullmatch(word):
                    if redirect or sep[0] in "<>" and word.isdigit():
                        redirs.add(len(words))
                    elif git_at == 2:
                        git_at = 1
                    elif git_at == 1:
                        # A message is one only to git's own subcommand (`git commit
                        # -m`, `git -C x tag -m`), not to `su tag -c -m '<script>'`.
                        low = _PUSH_CLEAN_RE.sub("", word[:64]).lower()
                        if low.startswith("-"):
                            git_at = 2 if low in _GIT_VALUE_OPTS else 1
                        elif low:
                            git_at, message_cmd = 3, low in _MESSAGE_CMDS
                    elif len(word) <= 256 and "git" in (low := word.lower()):
                        if (_PUSH_GIT_WORD_RE.fullmatch(_PUSH_CLEAN_RE.sub("", low))
                                or _PUSH_GIT_WORD_RE.fullmatch(
                                    _PUSH_CLEAN_RE.sub("", _PATH_SEP_RE.split(low)[-1]))):
                            git_at, message_cmd = 1, False
                    words.append(word)
                redirect = False
            if "<" in sep or ">" in sep:
                ends, redirect = _sep_ends(sep)
            else:
                ends = _COMMAND_END_RE.search(sep)
            if ends:
                starts.add(len(words))
                cmd_start = len(words)
                message_cmd = False
                git_at = 0
        elif kind == "esc":
            esc = m.group("esc")
            if esc in (" ", "\t") and blank is None:
                # `GIT_PAGER=git\ push`, `-m fix\ git\ push`: as a quoted span would be.
                lead = parts[0] if parts else ""
                after = "=" in lead
                option = message_cmd and bool(
                    _MESSAGE_OPT_RE.match(lead.lower()) if lead.startswith("-")
                    else words and _MESSAGE_OPT_RE.fullmatch(words[-1].lower()))
                pieces = [lead] if after else []
                blank = (not option and (
                    not executed_only
                    or args and not _text_span(words, pieces, cmd_start, redirs, memo)
                    or _executed_span(words, pieces, cmd_start, scripts)), after)
            if esc != "\n":  # `\`-newline joins lines
                parts.append(m.group(0))
        else:
            content = m.group(kind)
            # `-m "msg"`, `-m"msg"`, `--message="msg"`, `-F "file"`: not a command,
            # unless double quotes hold a substitution that runs one. Each further
            # piece of the same word is the message too (`-m"a""b"`, `-m "a"'b'`).
            option = message_cmd and (
                message_word and bool(parts)
                or (_MESSAGE_OPT_RE.fullmatch(parts[0].lower()) if len(parts) == 1
                    else not parts and words and _MESSAGE_OPT_RE.fullmatch(words[-1].lower())))
            message_word = bool(option)
            message = option and (kind != "dq" or _dq_subst(content) < 0)
            if message or kind == "run" or not content or not executed_only:
                skip = bool(message or kind == "run")
            elif len(parts) > 4:
                # A long word reads as "x" there, so each further piece of it gets the
                # same answer: asked once per word, not per piece (`"a""a""a"...`).
                if long_key != (len(words), cmd_start):
                    long_key = (len(words), cmd_start)
                    long_run = (args and not option
                                and not _text_span(words, parts, cmd_start, redirs, memo)
                                or _executed_span(words, parts, cmd_start, scripts))
                skip = not long_run
            else:
                # The cheaper tests first: they settle most spans. A span that is one
                # plain word (`'git'`) is read with its own word, not on its own.
                skip = not (args and not option and not _SIMPLE_WORD_RE.fullmatch(content)
                            and not _text_span(words, parts, cmd_start, redirs, memo)
                            or _executed_span(words, parts, cmd_start, scripts))
            # `$'co\x6dmit'` spells its word with escapes: dynamic, like `$c`.
            parts.append("$" + content if kind == "ansi" and "\\" in content else content)
            if not content or skip:
                subst = (_dq_subst(content)
                         if content and scripts and kind == "dq" and not message else -1)
                if subst >= 0:
                    span = _dq_unescape(content[subst:])
                    if args and "<<" in span:
                        # A heredoc only an inert reader gets is text here too (`git
                        # commit -m "$(cat <<EOF ...)"`).
                        inner, live = _read_text(span[2:] if span.startswith("$(") else span)
                        span = span if live else inner
                    spans.append(span)
                continue
            spans.append(_dq_unescape(content) if kind == "dq"
                         else _ansi_decode(content) if kind == "ansi" else content)
    if parts:
        word = "".join(parts)
        if blank is not None and blank[0]:
            spans.append(blank_span(word))
        if word == "{}" or not _BRACES_RE.fullmatch(word):
            if redirect:
                redirs.add(len(words))
            words.append(word)
    return words, spans, starts, redirs


def _git_walk(clean, dyn, i, dyn_sub=True, subs=_PUSH_SUBS, redirs=()):
    """(hits, last word index consumed, read) for the `git` word at index i: its first
    subcommand after global options is one of `subs` (push, send-pack or http-push by
    default), dynamic (if `dyn_sub`), `subtree push`, or `config [set|add] alias.<x>
    <value>` with a value holding one of `subs` or starting with `!`. `read` says the
    words consumed are a `git config` that only reads or removes a key (`--get
    alias.push`, `get`, `--unset`, or a key with no value), so its `alias.<x>` word
    defines nothing. A redirection's words (`redirs`: `git >/dev/null push`) are
    skipped, and so is the word after a `git config` option, which may be its value
    (`-f <file>`, `--blob <blob>`)."""
    n = len(clean)
    end = min(n, i + 1 + _GIT_SUBCOMMAND_WINDOW)
    skip, sub, reads, after_opt = False, None, False, False
    for j in range(i + 1, end):
        word = clean[j]
        if j in redirs:
            continue
        if skip:
            skip = False
        elif word.startswith("-"):  # `--git-dir=$D` too: never the subcommand
            skip = (word in _GIT_VALUE_OPTS if sub is None
                    else sub == "subtree" and word in ("-p", "--prefix"))
            reads = reads or sub == "config" and (word.startswith(_CONFIG_READ_OPTS)
                                                  or word == "-l")
            after_opt = sub == "config" and "=" not in word
        elif sub == "config":
            if word.startswith("alias."):
                if reads or j + 1 >= n:
                    return 0, j, True
                value = clean[j + 1]
                return int(any(s in value for s in subs) or value.startswith("!")
                           or dyn[j + 1]), j + 1, False
            if word in _CONFIG_READ_SUBS:
                reads = True
            elif after_opt:
                after_opt = False  # `-f <file>`: the key may still follow
            elif word not in ("set", "add"):
                return 0, j, False
        elif dyn[j] and dyn_sub or word in subs:
            return 1, j, False  # a dynamic word may be push at run time
        elif not word:
            continue  # e.g. `''`
        elif sub == "subtree":
            return 0, j, False
        elif word in ("subtree", "config"):
            sub = word
        else:
            return 0, j - 1, False  # the first subcommand isn't push (`git stash push`)
    return int(end < n), end - 1, False  # options past the window: subcommand unknown


def _xargs_feeds(tokens, clean, starts, i, done):
    """Whether the git word at index i, whose own words (up to index `done`) run no
    push, may get its subcommand from an `xargs` or `parallel` a few words before it in
    the same command: added after it when it has none of its own (`echo push | xargs
    git`, `xargs -n1 git`, `parallel git ::: push`), or put where a custom replace
    string stands (`xargs -IX git X origin`; the default `{}` reads as a dynamic word
    already)."""
    for k in range(i - 1, max(-1, i - 6), -1):
        base = _PATH_SEP_RE.split(clean[k])[-1]
        base = base[:-4] if base.endswith(".exe") else base
        if base in ("xargs", "parallel"):
            break
        if k in starts:
            return False
    else:
        return False
    nxt = done + 1
    if nxt >= len(tokens) or nxt in starts:
        return True
    if base == "parallel":
        return clean[nxt].startswith(":::")
    for j in range(k + 1, i):
        option = tokens[j].replace("'", "").replace('"', "")
        if option.startswith(("-I", "-i", "-J")):
            mark = option[2:] or (tokens[j + 1] if j + 1 < i else "")
        elif option.startswith("--replace="):
            mark = option[len("--replace="):]
        else:
            continue
        if mark and mark.lower() in tokens[nxt].lower():
            return True
    return False


def _glob_match(pattern, name):
    """Whether the glob `pattern` (`*` and `?` only) matches `name`. Linear in the
    pattern for a short name: it backtracks only to its last `*`."""
    p = k = 0
    star, mark = -1, 0
    while k < len(name):
        if p < len(pattern) and pattern[p] in ("?", name[k]):
            p, k = p + 1, k + 1
        elif p < len(pattern) and pattern[p] == "*":
            star, mark = p, k
            p += 1
        elif star >= 0:
            mark += 1
            p, k = star + 1, mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


def _command_words(tokens, clean, starts, redirs=(), args=False):
    """Which of `tokens` (`clean` their cleaned forms) may be a command word: the first
    of each command (`starts`, see _push_words), each after a prefix (see
    _WRAPPER_WORDS) or a `find -exec`, and any word with a backtick in it (a
    substitution may start there). Not an argument (`echo git commit`), unless `args`:
    then also the arguments of any command but one that only reads them (`ssh-agent git
    push`, not `echo git push`; see _quiet_command). Never a redirection's descriptor
    or target (`redirs`), which doesn't end the prefix either (`2>&1 git push`). One
    pass."""
    out = []
    lead = wrapped = after_flag = takes_arg = quiet = False
    for i, word in enumerate(clean):
        if i in starts:
            lead, wrapped, after_flag, takes_arg, quiet = True, False, False, False, False
        if i in redirs:
            out.append(False)
            continue
        out.append(lead or "`" in tokens[i] or args and not quiet)
        if word in _EXEC_OPTS:
            lead, wrapped, after_flag, takes_arg, quiet = True, False, False, False, False
            continue
        if not lead:
            continue
        base = _PATH_SEP_RE.split(word)[-1]
        base = base[:-4] if base.endswith(".exe") else base
        flag = wrapped and bool(_FLAG_WORD_RE.fullmatch(word))
        if base in _WRAPPER_WORDS:
            wrapped = True
            takes_arg = base in _WRAPPER_ARG_WORDS
        elif not (flag or _ASSIGN_WORD_RE.fullmatch(word)
                  or wrapped and (after_flag or _NUMBER_WORD_RE.fullmatch(word))):
            if takes_arg:
                takes_arg = False  # `flock /tmp/lock`: the lock file
            else:
                lead = False  # the command word itself: what follows are its arguments
                quiet = args and _quiet_command(clean, i, tokens, starts)
        after_flag = flag
    return out


def _reads_script(args, shell):
    """Whether a shell or interpreter with the argument words `args` runs something
    other than a literal script given to its script flag (`bash -c '<script>'`, `python
    -c`, which is read as a quoted span) or a module (`python -m pytest`): a script
    file, stdin (`python -`, no script at all) or a script built at run time."""
    flag_re = _SHELL_SCRIPT_FLAG_RE if shell else _INTERPRETER_SCRIPT_FLAG_RE
    for k, arg in enumerate(args):
        low = arg.lower()
        if flag_re.fullmatch(low):
            return any("$" in word or "`" in word for word in args[k + 1:])
        if not shell and low == "-m":
            return False
        if len(low) < 2 or low[0] != "-":
            return True  # a script file, `-` or a word that is no flag (`+x`)
    return True


def _command_runs_text(words, fed, stdin):
    """(live, inert) for the words of one simple command (see _may_run_text). `fed`: a
    pipe or a process substitution feeds it; `stdin`: a `<` redirection does. inert:
    its command word is one of _INERT_READERS, so naming a file to it runs nothing."""
    if len(words) > 2 and words[1] in ("=", "+=") and "$" in words[0]:
        words = words[2:]  # PowerShell `$x = <command>`
    inert = False
    # Command position as in _command_words: past wrappers, their flags and a flag's
    # value, numbers and assignments, and again after a `find -exec`.
    lead, wrapped, after_flag = True, False, False
    for i, word in enumerate(words):
        if word in _EXEC_OPTS:
            lead, wrapped, after_flag = True, False, False
            continue
        if not lead:
            continue
        low = word.lower()
        clean = _PUSH_CLEAN_RE.sub("", low)
        seg = _PUSH_CLEAN_RE.sub("", _PATH_SEP_RE.split(low.replace("\\", "/"))[-1])
        names = {name[:-4] if name.endswith(".exe") else name for name in (seg, clean)}
        if not names.isdisjoint(_LIVE_WORDS):
            return True, False
        if not names.isdisjoint(_EVAL_WORDS):
            # What it is fed runs too (PowerShell `"git push" | iex`).
            return fed or stdin or any("$" in w or "`" in w for w in words[i + 1:]), False
        shell = not names.isdisjoint(_SHELL_WORDS)
        if shell or any(_INTERPRETER_WORD_RE.fullmatch(name) for name in names):
            return fed or stdin or _reads_script(words[i + 1:], shell), False
        flag = wrapped and bool(_FLAG_WORD_RE.fullmatch(low))
        if not names.isdisjoint(_WRAPPER_WORDS):
            wrapped = True
        elif not (flag or _ASSIGN_WORD_RE.fullmatch(clean)
                  or wrapped and (after_flag or _NUMBER_WORD_RE.fullmatch(low))):
            # The command word itself: built at run time, or fed text it may run.
            inert = not names.isdisjoint(_INERT_READERS)
            if inert and any(_PUSH_CLEAN_RE.sub("", w).lower().startswith(_READER_RUN_OPTS)
                             for w in words[i + 1:]):
                return True, False  # `rg --pre sh`, `sort --compress-program=sh`: runs a program
            if "$" in word or "`" in word or fed and not inert:
                return True, False
            lead = False
        after_flag = flag
    if lead and wrapped and (fed or stdin):
        # Only wrappers (`| xargs`, `| xargs env`, `| xargs timeout 60`): the command
        # is what they are fed.
        return True, False
    return False, inert and not any("`" in word for word in words)


def _inert_git(words):
    """Whether the simple command `words` is a plain `git status`, `diff`, `log` or `add`
    (no global options), which runs no file an earlier command wrote (see _may_run_text)."""
    return (len(words) > 1 and _PUSH_CLEAN_RE.sub("", words[0]).lower() in ("git", "git.exe")
            and _PUSH_CLEAN_RE.sub("", words[1]).lower() in _INERT_GIT_SUBS)


def _writes_file(words, inert):
    """Whether the simple command `words` writes a file with no redirection of its own:
    a `tee` anywhere in it, or an inert reader given an output option (`sort -o f`,
    any `-o`/`--o...` word, fail closed) or `uniq IN OUT`."""
    names = [_PUSH_CLEAN_RE.sub("", _PATH_SEP_RE.split(w.lower().replace("\\", "/"))[-1])
             for w in words]
    if not _WRITER_WORDS.isdisjoint(names):
        return True
    if not inert:
        return False
    # The command word: past wrappers, their flags and assignments (`LC_ALL=C uniq`,
    # `env uniq`, `command uniq`), as _command_runs_text finds it.
    at = 0
    while at < len(words) - 1 and (names[at] in _WRAPPER_WORDS or names[at].startswith("-")
                                   or _ASSIGN_WORD_RE.fullmatch(names[at])):
        at += 1
    args = words[at + 1:]
    if names[at] == "uniq" and sum(not w.startswith("-") or w == "-" for w in args) > 1:
        return True
    # Any case and spelling of the option (`sort -Vo f`, `sort -\o f`), fail closed.
    return any(_WRITE_OPT_RE.fullmatch(_PUSH_CLEAN_RE.sub("", w).lower()) for w in args)


def _may_run_text(text):
    """Whether `text` may run words that reading only what runs (see _push_words) would
    skip, so that all of it has to be read: a command word built at run time (`$c`, a
    substitution), an `eval` or a shell script flag given a non-literal script, a
    `trap`, `alias`, `coproc` or `source` (see _LIVE_WORDS), a pipe or process
    substitution into anything but an inert reader, a shell or interpreter reading a
    script file, stdin or a heredoc, a file written by a redirection and named again
    outside an inert reader (`echo 'git push' > p.sh; ./p.sh`) or followed by any
    command but an inert reader or `git status`/`diff`/`log`/`add` (`cat <<EOF >
    Makefile ... make`, see _inert_git), or a heredoc given to anything but an inert
    reader (`tee p.sh <<EOF`, not `cat <<EOF`), or the input of a compound command (`{
    xargs env; } <<EOF`). Words are read as in _simple_commands. One pass."""
    if _PS_BUILT_RE.search(text):
        return True  # `iex ('git ' + 'push')`: command text built at run time
    if not any(char in text for char in _LIVE_HINT_CHARS):
        lowered = text.lower()
        if not any(hint in lowered for hint in _LIVE_HINTS) and not _LIVE_ALIAS_RE.search(lowered):
            return False
    words, parts, named, written = [], [], [], set()
    fed = stdin = here = False  # of the command being read (see _command_runs_text)
    # Whether an earlier command wrote a file: anything after it but an inert reader
    # may run that file without naming it (`make`, `git commit` running a hook).
    wrote = self_wrote = False  # self_wrote: a command wrote a file itself (`sort -o f`)
    redirect = ""  # the operator whose target the next word is (`>&`: an fd if digits)
    # The closers of the groups a pipe or `>(` feeds (`| { true; xargs env; }`, `| while
    # ...; do ...; done`): every command inside one is fed until it closes. Fail closed:
    # an opener inside one always counts, a closer only when it matches the last opener.
    group = []
    closed = False  # the last thing read closed a group (`(...) &`, `{ ...; } &`)
    live_now = False  # a write under `.git/` (a hook, config): all of the text is read

    def file_name(word):
        return _PUSH_CLEAN_RE.sub("", _PATH_SEP_RE.split(word.replace("\\", "/"))[-1]).lower()

    def git_dir_check(words):
        # True when a git command follows a write to a target built at run time; marks
        # a `cd` into a `.git` path, or into one built at run time (fail closed).
        nonlocal in_git
        names = [file_name(word) for word in words]
        if dyn_target and not _GIT_NAMES.isdisjoint(names):
            return True
        if names[0] in _CD_WORDS and any(_GIT_DIR_PATH_RE.search(word)
                                         or any(char in word for char in "$`\\")
                                         for word in words[1:]):
            in_git = True
        return False

    # A loop keyword anywhere: a command before a write may run it on the next pass
    # (`for i in 1 2; { make; echo ... > Makefile; }`), so any write keeps it live.
    loop = False
    # A write to a target built at run time, which a later git command may run as a
    # hook; and a `cd` into a `.git` path (or one built at run time), after which any
    # write may be a hook or config (see git_dir_check).
    dyn_target = in_git = False

    def end_word(sep):
        nonlocal redirect, closed, live_now, loop, dyn_target
        word = "".join(parts)
        parts.clear()
        if redirect:
            if ((redirect == ">" or redirect == ">&" and not word.isdigit()) and word != "-"
                    and word.lower() not in _NULL_FILES):
                written.add(file_name(word))
                live_now = live_now or in_git or bool(_GIT_DIR_PATH_RE.search(word))
                # A target built at run time (`$d/hooks/x`, `.g\it/...`) may be in `.git/`.
                dyn_target = dyn_target or any(char in word for char in "$`\\")
            redirect = ""
        elif _BRACES_RE.fullmatch(word):
            if not words:
                for char in word:
                    if char == "{" and (fed or group):
                        group.append("}")
                    elif char == "}" and group and group[-1] == "}":
                        group.pop()
            closed = "}" in word
        elif not (sep[:1] in ("<", ">") and word.isdigit()):
            if not words:
                low = _PUSH_CLEAN_RE.sub("", word).lower()
                loop = loop or low in _LOOP_WORDS
                if low in _GROUP_OPENERS and (fed or group):
                    group.append(_GROUP_OPENERS[low])
                elif group and low == group[-1]:
                    group.pop()
            words.append(word)
            closed = False

    for m in _push_lex(text):
        kind = m.lastgroup
        if kind == "esc":
            if m.group("esc") != "\n":  # `\`-newline joins lines
                parts.append(m.group(0))
            continue
        if kind != "sep":
            parts.append("$" + m.group(kind) if kind == "ansi" else m.group(kind))
            continue
        sep = m.group(0)
        # A `)` inside an open `${` (`${x:-)}`, `${x#)}`) closes no group.
        in_brace = False
        if parts:
            joined = "".join(parts)
            in_brace = joined.count("${") > joined.count("}")
            end_word(sep)
            if live_now:
                return True
            if len(words) > _LIVE_MAX_WORDS:
                return True  # too long to tell: read all of it
        for k, char in enumerate(sep):
            if char in "<>":
                if sep[k + 1:k + 2] == "(":
                    fed = True  # a process substitution
                else:
                    redirect = char
                    stdin = stdin or char == "<"
                    # A heredoc (`<<`, not the here-string `<<<`).
                    here = here or (sep[k:k + 2] == "<<" and sep[k + 2:k + 3] != "<"
                                    and sep[k - 1:k] != "<")
            elif char in "&|" and (_in_redirect(sep, k) or sep[k - 1:k + 1] == "|&"):
                if char == "&" and redirect == ">" and sep[k - 1:k] == ">":
                    redirect = ">&"  # `>&1`: digits name an fd, not a file
                continue  # `>&1`, `&>`, `>|`, `|&`: still the redirection or the pipe
            elif char in ";&|()\n":
                redirect = ""
                if (char == "&" and sep[k - 1:k] != "&" and sep[k + 1:k + 2] != "&"
                        and (words or closed)):
                    # A background command (`(sleep 1; make) & cat <<EOF > Makefile`)
                    # runs alongside what follows: no order says what it reads.
                    return True
                if char == "\n" and fed and not words:
                    continue  # a line ending in `|` goes on with the next one
                # `| (xargs env)`: the subshell after a pipe is fed as its command would be.
                piped = char == "(" and fed and not words
                # The words before a `)` are inside the group it closes (`| (true; xargs env)`).
                grouped = bool(group)
                if char == "(" and (group or piped or sep[k - 1:k] in ("<", ">")):
                    group.append(")")
                elif char == ")" and group and group[-1] == ")" and not in_brace:
                    group.pop()
                closed = char == ")"
                if words:
                    live, inert = _command_runs_text(words, fed or grouped, stdin)
                    if live or here and not inert or wrote and not (inert or _inert_git(words)):
                        return True
                    if git_dir_check(words):
                        return True
                    if not inert:
                        named += words
                    if _writes_file(words, inert):
                        if in_git or any(_GIT_DIR_PATH_RE.search(word) for word in words):
                            return True
                        self_wrote = True
                    words = []
                elif here or stdin:
                    # `{ xargs env; } <<EOF`, `(sh) < f`: the input of a compound command
                    # has no words of its own to tell what reads it.
                    return True
                # Also after a group's own redirection (`{ ...; } > Makefile; make`).
                wrote = bool(written) or self_wrote
                # A pipe (not `||`) feeds the next command, an output process
                # substitution (`> >(xargs env)`) the command in it.
                fed = (char == "|" and sep[k - 1:k] != "|" and sep[k + 1:k + 2] != "|"
                       or piped or char == "(" and sep[k - 1:k] in ("<", ">"))
                stdin = here = False
    if parts:
        end_word("")
        if live_now:
            return True
    if words:
        live, inert = _command_runs_text(words, fed or bool(group), stdin)
        if live or here and not inert or wrote and not (inert or _inert_git(words)):
            return True
        if git_dir_check(words):
            return True
        if not inert:
            named += words
        if _writes_file(words, inert):
            if in_git or any(_GIT_DIR_PATH_RE.search(word) for word in words):
                return True
            self_wrote = True
    elif here or stdin:
        return True  # the input of a compound command, as above
    if loop and (written or self_wrote):
        return True
    return bool(written) and any(file_name(word) in written for word in named)


def _read_text(command):
    """(text, live): `command` without its inert text (see _drop_inert_text, with an
    interpreter's heredoc kept), and whether it may run words that reading only what
    runs would skip (see _may_run_text), so that all of `command` has to be read."""
    if _POWERSHELL.get() and _PS_OPAQUE_RE.search(command):
        return command, True  # quoting this can't follow: all of it is read
    if "$\\\n" in command:
        # `$\<nl>'a\'b # c'` is bash's `$'...'` once the backslash-newline goes: read all.
        return command, True
    try:
        text = _drop_inert_text(command, _SCRIPT_LINE_RE)
        return text, _may_run_text(text)
    except TooManyTargets:
        return command, True  # too deeply nested to tell text from code


def _push_signals(command, depth=0, unknown=None):
    """How many pushes `command` may hold, parsed or not (see _git_signals, which gets
    `unknown`), text it runs but can't read included (see _encoded_signals). A command
    that merely mentions a push in what is known to be text is not one: the arguments
    of a command that only reads or prints them (`grep "git push" docs/`, `echo git
    push`; see _INERT_READERS), a message or a search value (`git commit -m "git
    push"`, `git log --grep="git push"`; see _text_span), a `cat <<EOF` body, a
    comment. Anything else counts, as nothing says what an unknown command does with
    its arguments: a git word that is another command's argument (`ssh-agent git
    push`, `git bisect run git push`), a quoted span (`watch 'git push'`, `git rebase
    -x 'git push'`), an interpreter's script (`python -c`, see _SCRIPT_WORD_RE) and a
    substitution inside double quotes. When text is fed to a shell (`echo git push |
    sh`, `sh <<< "git push"`, `sh <(echo git push)`), every word and quoted span is
    read instead, as nothing says which of them the shell gets. An unquoted `$IFS`
    is a blank (`git${IFS}push`)."""
    if "IFS" in command:
        command = _IFS_RE.sub(" ", command)
    hits = _read_signals(command, depth, unknown) + _encoded_signals(command)
    if "^" in command and _CMD_WORD_RE.search(command):
        # `cmd /c "g^it push"`: cmd drops the carets. Read both ways, as elsewhere a
        # caret is a character of its own (`HEAD^`).
        hits = max(hits, _read_signals(command.replace("^", ""), depth, None))
    return max(hits, _split_span_signals(command))


def _split_span_signals(command):
    """1 when `command` may give git its subcommand inside a span that a runner splits
    into words again: `eval git 'push origin'`, `eval git -C . pu'sh origin'`,
    PowerShell's `Start-Process git -WorkingDirectory . 'push origin'` (also `saps`,
    `start`) or its `-ArgumentList "push origin"` with git anywhere in the command,
    `watch git 'push'`; else 0. Coarse on purpose (fail closed): with a runner word
    anywhere, a push subcommand word anywhere after a git word (or an `-ArgumentList`
    word) counts, whatever comes between, with quotes, backslashes and backticks
    (PowerShell's line continuation) removed first. Two linear searches."""
    text = command.translate(_SPLIT_DROP)
    if not _SPLIT_RUNNER_RE.search(text):
        return 0
    git = _SPLIT_GIT_RE.search(text)
    if git is None:
        return 0
    args = _SPLIT_ARGS_RE.search(text)
    return int(bool(_SPLIT_PUSH_RE.search(text, min(git.end(), args.end() if args else git.end()))))


def _read_signals(command, depth, unknown):
    """_push_signals' count for `command` as it is written."""
    text, live = _read_text(command)
    if live:
        # Nothing says which of its words run (`c="git push"; $c`, a script written
        # then run, `trap`): read all of it, text, comments and heredoc bodies included.
        return _git_signals(command, _PUSH_SUBS, depth, unknown=unknown)
    fed = (("|" in text or "<<<" in text or "<(" in text or ">(" in text)
           and bool(_SHELL_WORD_RE.search(text)))
    return _git_signals(text, _PUSH_SUBS, depth, executed_only=not fed,
                        command_only=not fed, scripts=True, args=True, unknown=unknown)


def _encoded_signals(command):
    """1 when `command` runs text this can't read because it is encoded, else 0: a
    decoder (`base64 -d`, `xxd -r`, `openssl base64 -d`, `certutil -decode`,
    PowerShell's `FromBase64String`) with a shell or `iex` later in its pipeline, or
    anywhere in the command for `FromBase64String`; and PowerShell given an encoded
    command (`powershell -enc <x>`, any spelling of `-EncodedCommand`). Coarse: the
    decoder's own options aren't read. One pass."""
    lowered = command.lower()
    if not any(hint in lowered for hint in _ENCODED_HINTS):
        return 0
    # 1 past a decoder, 2 past a pipe after it; whether a PowerShell word, the last
    # token a pipe, came in the command being read.
    stage, shell, piped, decoded = 0, False, False, False
    for m in _ENCODED_SCAN_RE.finditer(lowered):
        kind = m.lastgroup
        if kind == "end":
            if m.group(0) == "\n" and piped:
                continue  # a line ending in `|` goes on with the next one
            stage, shell = 0, False
        elif kind == "pipe":
            stage = 2 if stage else 0
        elif kind == "dec":
            stage = max(stage, 1)
            decoded = decoded or m.group(0) == "frombase64string"
        elif kind == "sh":
            word = m.group(0)
            if stage == 2 or decoded and _IEX_RE.fullmatch(word):
                return 1
            shell = shell or word in ("pwsh", "powershell")
        elif shell and ("encodedcommand".startswith(m.group("enc")) or m.group("enc") == "ec"):
            return 1
        piped = kind == "pipe"
    # `iex (...FromBase64String(...))`: the decoder comes after it.
    return int(decoded and bool(_IEX_RE.search(lowered)))


def _commit_signals(command):
    """How many commits `command` may hold, parsed or not (see _git_signals). Narrower
    than _push_signals, since commit text is common in prose: heredoc bodies and
    comments (see _drop_inert_text), quoted text no shell or `eval` runs (`echo "git
    commit"`, a commit message) and a git word that is only an argument (`echo git
    commit`, see _command_words) are not read."""
    if "IFS" in command:
        command = _IFS_RE.sub(" ", command)
    return _git_signals(_drop_inert_text(command), _COMMIT_SUBS, executed_only=True,
                        command_only=True)


def _heredoc_word(command, start):
    """(dash, word, end) for the heredoc operator at command[start]: whether it is
    `<<-`, its delimiter as bash reads it, quotes and backslashes removed (`<<E\\OF`,
    `<<'EOF'` and `<<"E"OF` all end at an `EOF` line), and where the word ends. word is
    None when this can't read it for sure: an expansion, a blank or another character
    in it, an empty or a very long one."""
    op = _HEREDOC_OP_RE.match(command, start)
    m = _HEREDOC_WORD_RE.match(command, op.end())
    end = m.end() if m else op.end()
    word = None
    if (m is not None and end - op.end() <= MAX_HEREDOC_WORD_CHARS
            and command[end:end + 1] in _HEREDOC_WORD_END):
        word = _HEREDOC_QUOTING_RE.sub("", m.group(0)) or None
    return bool(op.group(1)), word, end


def _line_index(command):
    """(plain, tabbed) for _drop_inert_text's terminator search: the (start, end) of
    each short line of `command` by its text (less a trailing `\\r`), in order; the
    lines that start with tabs are in `tabbed`, by their text without them. One pass."""
    plain, tabbed, start = {}, {}, 0
    for line in command.split("\n"):
        end = start + len(line)
        if len(line) <= 2 * MAX_HEREDOC_WORD_CHARS:
            text = line.rstrip("\r")
            if text.startswith("\t"):
                tabbed.setdefault(text.lstrip("\t"), []).append((start, end))
            else:
                plain.setdefault(text, []).append((start, end))
        start = end + 1
    return plain, tabbed


def _terminator_line(index, word, dash, pos):
    """(start, end) of the first line at or after `pos` that ends a heredoc with the
    delimiter `word` (after leading tabs too for `<<-`), or None. A binary search in
    _line_index's lists."""
    found = None
    for lines in (index if dash else index[:1]):
        spans = lines.get(word)
        if spans:
            k = bisect.bisect_left(spans, (pos, -1))
            if k < len(spans) and (found is None or spans[k] < found):
                found = spans[k]
    return found


def _drop_inert_text(command, shell_re=_SHELL_WORD_RE):
    """`command` without its comments and the bodies of heredocs whose line names no
    shell (`cat <<EOF`, not `bash <<EOF`; `shell_re` says what a shell is); a heredoc
    with no terminator line is kept (it may be a shift). Quotes are skipped whole, so a
    `#` or `<<` in them is text, and a `#` is a comment only where a word starts (not
    in `a#b`, nor after a backslash escape: `fix\\ #123`). The delimiter is read as bash
    reads it (see _heredoc_word); at one this can't read for sure, or past
    MAX_PENDING_HEREDOCS on one line, the rest of the command is kept as it is. Linear:
    the lines are indexed once (see _line_index) and each terminator is looked up in
    that index, so a heredoc with no terminator costs no scan of its own."""
    if "#" not in command and "<<" not in command:
        return command  # nothing to drop
    if _POWERSHELL.get():
        return _drop_ps_comments(command)
    out, pos, scanned, pending = [], 0, 0, []
    n = len(command)
    escaped = -1  # where the last backslash escape ended
    first = 0  # where the first pending heredoc's operator is
    index = None  # _line_index(command), built for the first heredoc
    # Read as bash reads it: a backslash-newline goes before the text is split (`$\<nl>[`).
    comments = "#" in command and not _NO_COMMENT_RE.search(command.replace("\\\n", ""))
    cut = cut_end = -1  # where the last comment dropped starts and ends
    brace = 0  # how many braces of a `${...}` are open
    # The open parentheses, 1 for a `$(`, `$((` or `((` (a heredoc's body can't start
    # inside one: at a newline with one open nothing more is dropped), 0 for a subshell's.
    parens, hard = [], 0
    for m in _INERT_SCAN_RE.finditer(command):
        start = m.start()
        if start < scanned:
            continue
        token = m.group(0)
        if token in ("{", "(") and command[start - 2:start] == "\\\n":
            j = start
            while command[j - 2:j] == "\\\n":
                j -= 2
            if command[j - 1:j] == "$":
                token = "$" + token  # `$\<nl>{` is `${`
        if token in ("${", "{", "}"):
            if token == "${" or brace:
                brace += 1 if token != "}" else -1
            continue
        if token in ("$((", "$(", "((", "(", ")"):
            if token == ")":
                if parens:
                    hard -= parens.pop()
                continue
            if brace and token[0] == "$":
                break  # `${x:-$(echo })...`: its braces can't be told apart; nothing more is dropped
            weight = int(token != "(")
            for _ in range(2 if token.endswith("((") else 1):
                parens.append(weight)
                hard += weight
            continue
        if token == '"':
            scanned = _double_quote_end(command, start) or n
        elif token[0] == "\\":
            escaped = m.end()
        elif token == "#":
            if not comments:
                continue
            if start == escaped:
                continue
            if command[start - 1:start] == "<":
                # PowerShell's `<# ... #>` in a command not known to be PowerShell's:
                # only the block goes, and with no end nothing more does.
                end = command.find("#>", start + 1)
                if end < 0:
                    comments = False
                else:
                    out.append(command[pos:start - 1] + " ")
                    pos = scanned = end + 2
            elif start == 0 or command[start - 1] in " \t\n;&|()":
                end = command.find("\n", start)
                out.append(command[pos:start])
                pos = scanned = n if end < 0 else end
                cut, cut_end = start, pos
        elif token == "<<":
            if command[start - 1:start] != "<" and command[start + 2:start + 3] != "<":
                if brace:
                    break  # `${a[1 <<x]}`: a shift, not a heredoc; nothing more is dropped
                dash, word, end = _heredoc_word(command, start)
                if word is None or len(pending) >= MAX_PENDING_HEREDOCS:
                    break  # not read for sure: nothing from here on is dropped
                if not pending:
                    first = start
                pending.append((word, dash))
                scanned = end
        elif token == "\n" and pending:
            if hard or brace:
                break  # `cat <<EOF $(true<nl>git push)`: not the body's start; nothing more is dropped
            line = command[command.rfind("\n", 0, first) + 1:start]
            if (line.count("(") > line.count(")") or line.count("[") > line.count("]")
                    or _CASE_WORD_RE.search(line)):
                # An opener still open at the newline (`$[1+<nl>`, `<(true<nl>`, `a[1
                # +<nl>`), or a `case` whose `x)` closes nothing, quotes not told apart:
                # not surely the body's start; nothing more is dropped (fail closed).
                break
            if index is None:
                index = _line_index(command)
            # A shell named on the operators' line(s) runs the bodies: they are kept.
            keep = bool(shell_re.search(command, command.rfind("\n", 0, first) + 1, start))
            i = m.end()
            bodies = []
            for word, dash in pending:
                span = _terminator_line(index, word, dash, i)
                if span is None:
                    continue  # no terminator: not a heredoc, so its text is kept
                bodies.append((i, span[1]))
                i = span[1]
            # Where the line's own text ends: before its comment and trailing blanks.
            k = cut if cut_end == start else start
            while k and command[k - 1] in " \t\r":
                k -= 1
            if bodies and not keep and (command[k - 1:k] == "|" or command[k - 2:k] == "&&"):
                # The line goes on after the bodies (`cat <<EOF |`, then `sh` below
                # them): the shell may be named on the next line that isn't blank. If
                # that one goes on too, nothing says where the command ends.
                rest = _BLANK_LINES_RE.match(command, i).end()
                stop = command.find("\n", rest)
                stop = n if stop < 0 else stop
                keep = (bool(shell_re.search(command, rest, stop))
                        or command[rest:stop].rstrip(" \t\r").endswith(("|", "&&", "\\")))
            if not keep:
                for begin, end in bodies:
                    out.append(command[pos:begin])
                    pos = end
            pending = []
            scanned = i
    out.append(command[pos:])
    return "".join(out)


def _drop_ps_comments(command):
    """_drop_inert_text for a PowerShell command: without its `<# ... #>` block comments
    and its `#` comments (where a word starts, to the line's end). Quotes are PowerShell's
    (see _PS_LEX_RE), so `"C:\\a\\"` ends at its second quote; a block comment with no
    end, and everything after it, is kept. There are no heredocs to drop. One pass."""
    out, pos, scanned = [], 0, 0
    n = len(command)
    for m in _PS_INERT_SCAN_RE.finditer(command):
        start = m.start()
        if start < scanned:
            continue
        token = m.group(0)
        if token == "<#":
            end = command.find("#>", start + 2)
            if end < 0:
                break
            out.append(command[pos:start] + " ")
            pos = scanned = end + 2
        elif token == "#" and (start == 0 or command[start - 1] in " \t\n;&|()"):
            end = command.find("\n", start)
            out.append(command[pos:start])
            pos = scanned = n if end < 0 else end
    out.append(command[pos:])
    return "".join(out)


def _unknown_subcommand(tokens, clean, starts, redirs, i):
    """(global option words, subcommand, subcommand as written) for the git word at
    index i when its subcommand is none of git's own but a name a config alias may
    have (see _alias_signals), else None. The subcommand is the first word past the
    global options within the git word's own command, cleaned as in _git_signals
    (`git \\p` reads `p`)."""
    skip = False
    for j in range(i + 1, min(len(clean), i + 1 + _GIT_SUBCOMMAND_WINDOW)):
        word = clean[j]
        if j in starts:
            return None
        if j in redirs or not word:
            continue
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in _GIT_VALUE_OPTS
        elif word in _GIT_BUILTINS or not _ALIAS_NAME_RE.fullmatch(word):
            return None
        else:
            return ([tokens[k].replace("\\", "") for k in range(i + 1, j) if k not in redirs],
                    word, tokens[j])
    return None


def _gh_pushes(tokens, clean, starts, redirs, i):
    """Whether the `gh` word at index i runs `pr create` (or `pr new`, its other name)
    with no head named, or `repo create` with `--push`: gh then pushes (or offers to
    push) the current branch. A head is only a word of its own `--head`, `--head=<x>`,
    `-H` or `-H<x>`; the word after a value option (`--title --head`, `-t -H`, `-dt
    --head`) is that option's value. Its own command's next few words are looked at;
    past them nothing says a head was given, nor that `--push` wasn't."""
    stage, skip, ended = 0, False, False
    for j in range(i + 1, min(len(clean), i + 1 + _GIT_SUBCOMMAND_WINDOW)):
        word = clean[j]
        if j in starts:
            ended = True
            break
        if stage == 4:
            if word == "--push" or word.startswith("--push="):
                return True
        elif stage == 2:
            if skip:
                skip = False
                continue
            if j in redirs:
                continue  # a here-string, heredoc or redirection word is never a head
            written = _PUSH_CLEAN_RE.sub("", tokens[j][:64])
            if written in ("--head", "-H"):
                # Its value is the next word; an empty one (`--head ""`) names no head,
                # and an option word (`--head --fill`) is not read as one (fail closed).
                k = j + 1
                if (k < len(clean) and k not in starts and k not in redirs
                        and clean[k] and not clean[k].startswith("-")):
                    return False
                skip = True
                continue
            if written.startswith("--head=") and len(written) > 7 or written.startswith("-H"):
                return False
            if written == "--head=":
                continue  # empty: no head
            if written.startswith("--"):
                skip = written in _GH_PR_VALUE_OPTS
            elif written[:1] == "-":
                # A short cluster: past a value option's letter the rest is its value,
                # and with none left the next word is (`-dt x`).
                for k, char in enumerate(written[1:], 2):
                    if char in _GH_PR_VALUE_SHORT:
                        skip = k == len(written) and len(tokens[j]) <= 64
                        break
        elif skip:
            skip = False
        elif word.startswith("-"):
            skip = word in ("-r", "--repo")
        elif stage == 0 and word in ("pr", "repo"):
            stage = 1 if word == "pr" else 3
        elif stage == 1 and word in ("create", "new"):
            stage = 2
        elif stage == 3 and word in ("create", "new"):
            stage = 4
        else:
            return False
    return stage == 2 or stage == 4 and not ended and j == i + _GIT_SUBCOMMAND_WINDOW


def _git_signals(command, subs, depth=0, executed_only=False, command_only=False,
                 scripts=False, args=False, unknown=None):
    """How many git invocations of one of `subs` `command` may hold, parsed or not (see
    _PUSH_LEX_RE). Coarse on purpose: an extra one only costs one denial an identical
    retry overrides. With `command_only`, a git word must be in command position (see
    _command_words) or, with `args`, an argument of a command that may run it;
    `executed_only`, `scripts` and `args` are _push_words' too. A glob that may expand
    to the git word or to one of `subs` counts as it (`/usr/bin/g?t push`, `git pus?`,
    `git [p]ush`), a bare `git-push` only as a command word of its own, and where the
    reading is not sure (quoted spans below the last level read, a double quote whose
    end can't be found) any of `subs` in the text counts once. With the push `subs`, a
    `gh pr create` that names no head counts too (see _gh_pushes). `unknown` (a list,
    if given) gets each git word's subcommand that is none of git's own (see
    _unknown_subcommand) with whether it is a command word at the top level, and None
    for a span below the last level read that holds one. Every word is looked at a
    bounded number of times (linear time)."""
    if "[" in command:
        command = _BRACKET_GLOB_RE.sub("?", command)
    unsure = []
    tokens, spans, starts, redirs = _push_words(command, executed_only, scripts, args, unsure)
    dyn = [bool(_PUSH_DYNAMIC_RE.search(t)) for t in tokens]
    clean = [_PUSH_CLEAN_RE.sub("", t).lower() for t in tokens]
    glob_git = set()  # the words a glob may expand to git
    if "*" in command or "?" in command:
        for i, word in enumerate(clean):
            if "*" not in word and "?" not in word:
                continue
            seg = _PUSH_CLEAN_RE.sub("", _PATH_SEP_RE.split(tokens[i].lower())[-1])
            if any(_glob_match(p, name) for p in {seg, word} for name in ("git", "git.exe")):
                glob_git.add(i)
            sub = next((s for s in subs if _glob_match(word, s)), None)
            if sub is not None:
                clean[i] = sub
    at_command = _command_words(tokens, clean, starts, redirs, args) if command_only else None
    lead_at = None  # _command_words with no arguments counted, for a bare `git-push`
    lowered = command.lower()
    has_alias = "alias" in lowered
    has_xargs = "xargs" in lowered or "parallel" in lowered
    has_gh = subs is _PUSH_SUBS and "gh" in lowered
    hits, env_hit, done, read_end = 0, False, -1, -1
    for i, token in enumerate(tokens):
        word = clean[i]
        # `git -c alias.p=push p`, a shell alias (`alias.p=!git push`) or one set at
        # run time (`alias.p=$cmd`); not the key a `git config --get alias.push` reads.
        if word.startswith("alias."):
            if i > read_end:
                hits += (any(s in word for s in subs)
                         or word.partition("=")[2].startswith("!")
                         or bool(_PUSH_DYNAMIC_RE.search(token.partition("=")[2])))
            continue
        if has_alias and not env_hit and _GIT_CONFIG_ENV_RE.match(word):
            hits, env_hit = hits + 1, True
            continue
        if i <= done:
            continue  # an option or the subcommand of the git word before
        if at_command is not None and not at_command[i]:
            continue
        # The segment keeps `\` as a separator (`C:\Git\cmd\git.exe`).
        seg = _PATH_SEP_RE.split(token.replace("\\", "/").lower())[-1]
        seg = _PUSH_CLEAN_RE.sub("", seg)
        if has_gh and (seg in _GH_WORDS or word in _GH_WORDS):
            hits += _gh_pushes(tokens, clean, starts, redirs, i)
            continue
        dashed = _GIT_DASHED_WORD_RE.fullmatch(seg)
        if dashed:
            if dashed.group(1) not in subs:
                continue
            if _PATH_SEP_RE.search(token):
                hits += 1
            else:
                # A bare `git-push` is one as a command word, not in `man git-push`.
                if lead_at is None:
                    lead_at = _command_words(tokens, clean, starts, redirs)
                hits += lead_at[i]
        elif (_PUSH_GIT_WORD_RE.fullmatch(seg) or _PUSH_GIT_WORD_RE.fullmatch(word)
              or dyn[i] and word.endswith("git") or i in glob_git):
            # A dynamic path (`cp -r $SRC/git $DST`) may be an argument: a dynamic word
            # after it isn't taken for a subcommand.
            found, done, read = _git_walk(clean, dyn, i,
                                          not (dyn[i] and _PATH_SEP_RE.search(token)), subs,
                                          redirs)
            read_end = done if read else read_end
            # `echo push | xargs git`: with no subcommand in its own command, xargs
            # supplies it from its input.
            hits += found or (has_xargs and _xargs_feeds(tokens, clean, starts, i, done))
            if unknown is not None and not found and not read:
                sub = _unknown_subcommand(tokens, clean, starts, redirs, i)
                if sub is not None:
                    if lead_at is None:
                        lead_at = _command_words(tokens, clean, starts, redirs)
                    unknown.append((*sub, depth == 0 and lead_at[i]))
        elif not dyn[i] or i + 1 == len(tokens):
            continue
        elif clean[i + 1] in subs:
            hits, done = hits + 1, i + 1  # `g=git; $g push`
        elif not word.startswith("-") and clean[i + 1].startswith("-"):
            # A dynamic command word with options (`$g -C . push`, `"$GIT_BIN" -c k=v
            # push`) is walked like git, but a dynamic word after it (`$EDITOR -n $f`)
            # isn't taken for a subcommand.
            found, done, read = _git_walk(clean, dyn, i, False, subs, redirs)
            read_end = done if read else read_end
            hits += found
    # A double quote whose end can't be found: what follows it may be read wrong.
    if unsure and any(s in lowered[unsure[0]:] for s in subs):
        hits += 1
    # Every quoted span's content at once, one per line (linear in their total length).
    if spans and depth < _PUSH_QUOTE_DEPTH:
        hits += _git_signals("\n".join(spans), subs, depth + 1, command_only=command_only,
                             args=args, unknown=unknown)
    elif spans:
        # Below the last level read nothing says what a span holds (`bash -c` nested
        # deeper): one that is more than a plain word and names one of `subs` counts.
        hits += any(s in span for span in map(str.lower, spans)
                    if not _SIMPLE_WORD_RE.fullmatch(span) for s in subs)
        if has_gh:
            hits += any(_DEEP_GH_RE.search(span) or "--push" in span
                        and _DEEP_GH_REPO_RE.search(span) for span in map(str.lower, spans))
        # So does a git word there with anything but one of git's own commands after
        # it (an option, a possible alias).
        if unknown is not None and any(
                m.group(1) not in _GIT_BUILTINS
                for span in map(str.lower, spans) for m in _DEEP_GIT_RE.finditer(span)):
            unknown.append(None)
    return hits


def _looks_like_push(command):
    return _push_signals(command) > 0


def _dollar_quoted(text):
    """Whether `text` has an ANSI-C `$'...'` or a locale `$"..."` quoted span outside
    other quotes (see _PUSH_LEX_RE). One pass."""
    if "$'" not in text and '$"' not in text:
        return False
    dollar = False
    for m in _push_lex(text):
        kind = m.lastgroup
        if kind == "ansi" or kind == "dq" and dollar:
            return True
        dollar = kind == "run" and m.group(0).endswith("$")
    return False


def _unwrap_shell(command):
    """The script of a command that is nothing but a POSIX shell running one (`bash -lc
    '<script>'`, `sh -c "<script>"`, after an optional `winpty` and with plain flags
    such as `--login` before the script flag), so the scanner reads the script itself
    instead of one quoted word; nested wrappers are unwrapped a few levels. Any other
    command (words after the script, a value option such as `-o pipefail`, an
    unbalanced quote, an ANSI-C `$'...'` or locale `$"..."` quoted word, which shlex
    doesn't decode) is returned as given and left to the lexical backstop."""
    for _ in range(_PUSH_QUOTE_DEPTH + 1):
        if len(command) > MAX_UNWRAP_CHARS:
            break
        match = _SHELL_WRAP_RE.match(command)
        if match is None or _dollar_quoted(command[match.end():]):
            break
        try:
            words = shlex.split(command[match.end():])
        except ValueError:
            break
        if (len(words) < 2 or not _SHELL_WRAP_FLAG_RE.fullmatch(words[-2])
                or not all(_SHELL_PLAIN_FLAG_RE.fullmatch(w) for w in words[:-2])):
            break
        command = words[-1]
    return command


def _simple_commands(text):
    """The words of each simple command in `text` (see _PUSH_LEX_RE), quotes removed
    and commands split at `;`, `&`, `|`, a parenthesis or a newline. A redirection is
    dropped with its target and file descriptor (`2>&1`, `&> log.txt`, `>| log.txt`),
    so neither is taken for an argument nor splits the command. One pass."""
    commands, words, parts = [], [], []
    redirect = False  # the next word is a redirection's target
    for m in _push_lex(text):
        kind = m.lastgroup
        if kind == "esc":
            if m.group("esc") != "\n":  # `\`-newline joins lines
                parts.append(m.group(0))
            continue
        if kind != "sep":
            parts.append(_ansi_decode(m.group(kind)) if kind == "ansi" else m.group(kind))
            continue
        sep = m.group(0)
        if parts:
            word = "".join(parts)
            parts.clear()
            if redirect:
                redirect = False
            elif sep[0] in "<>" and word.isdigit():
                pass  # the descriptor of `2>`
            elif not _BRACES_RE.fullmatch(word):
                words.append(word)
        for k, char in enumerate(sep):
            if char in "<>":
                redirect = True
            elif char in "&|" and _in_redirect(sep, k):
                continue  # `>&1`, `&>`, `>|`: still the redirection
            elif char in ";&|()\n":
                redirect = False
                if words:
                    commands.append(words)
                    words = []
    if parts and not redirect:
        words.append("".join(parts))
    if words:
        commands.append(words)
    return commands


def _push_arg_lists(command):
    """The argument words of each `git … push` that `command` runs in plain sight
    (quoted or escaped words included: `git 'push' --force`), for _push_request. Not
    the ones inside a wrapper's quoted script, an alias or a variable."""
    return [args for _, args in _push_commands(command)]


def _push_commands(command):
    """(global option words, argument words) of each `git … push` in `command`: see
    _push_arg_lists. The first are the words between `git` and `push` (`-c k=v`)."""
    return [(opts, args) for opts, sub, _, args, _ in _git_commands(command) if sub == "push"]


def _git_commands(command):
    """(global option words, subcommand, subcommand as written, argument words, bare)
    of each git command `command` runs in plain sight (see _push_arg_lists); the
    subcommand is cleaned and lowercased as in _git_signals. bare: the git word is its
    command's first word but for assignments (not behind `env -C r`, `sudo -D r`,
    `find -execdir`)."""
    try:
        text = _drop_inert_text(command)
    except TooManyTargets:
        text = command
    lists = []
    for words in _simple_commands(text):
        clean = [_PUSH_CLEAN_RE.sub("", w).lower() for w in words]
        at_command = _command_words(words, clean, {0})
        for i, word in enumerate(words):
            seg = _PATH_SEP_RE.split(word.replace("\\", "/").lower())[-1]
            if not at_command[i] or not _PUSH_GIT_WORD_RE.fullmatch(_PUSH_CLEAN_RE.sub("", seg)):
                continue
            j = i + 1
            while j < len(words) and clean[j].startswith("-"):
                j += 2 if clean[j] in _GIT_VALUE_OPTS else 1
            if j < len(words):
                lists.append(([w.replace("\\", "") for w in words[i + 1:j]], clean[j], words[j],
                              [w.replace("\\", "") for w in words[j + 1:]],
                              all(_ASSIGN_WORD_RE.fullmatch(w) for w in clean[:i])))
            break
    return lists


def _alias_inline_config(git_words):
    """Whether the global options before a subcommand hold config that may define an
    alias or include a file that does (`-c alias.p=push`, `-c include.path=…`, any
    `--config-env`, whose value only the environment knows)."""
    for i, word in enumerate(git_words):
        if word.startswith("--config-env"):
            return True
        if word == "-c" and _ALIAS_CONFIG_KEY_RE.match(
                git_words[i + 1] if i + 1 < len(git_words) else ""):
            return True
    return False


def _alias_pushes(name, cwd, git_opts, deadline, budget):
    """Whether the config alias `name`, as git in `cwd` (with `git_opts`) reads it, may
    push: its value starts with `!` (a shell command) or holds one of _PUSH_SUBS; an
    alias naming another one is followed MAX_ALIAS_CHAIN times. `budget` is a one-item
    list holding how many lookups are left for the command: with none left, or a chain
    longer than that, it counts as a push. No such alias (`git config --get` exits 1
    with no output) is no push (the subcommand is some other program's); a lookup that
    fails any other way, or can't run before `deadline`, counts as one (fail closed)."""
    for _ in range(MAX_ALIAS_CHAIN):
        if budget[0] <= 0:
            return True
        budget[0] -= 1
        token = _GIT_UNASKED.set(False)
        try:
            value = _run_git(["config", "--get", f"alias.{name}"], cwd, deadline, git_opts)
            failed = _GIT_UNASKED.get()
        finally:
            _GIT_UNASKED.reset(token)
        if value is None:
            return failed
        value = value.strip()
        lowered = value.lower()
        if value.startswith("!") or any(s in lowered for s in _PUSH_SUBS):
            return True
        words = lowered.split()
        k = 0
        while k < len(words) and words[k].startswith("-"):
            k += 2 if words[k] in _GIT_VALUE_OPTS else 1
        if k >= len(words) or words[k] in _GIT_BUILTINS or not _ALIAS_NAME_RE.fullmatch(words[k]):
            return False
        name = words[k]
    return True


def _alias_signals(command, cwd, deadline=None, powershell=False, unknown=None):
    """How many git commands in `command` run a subcommand that is none of git's own
    and may push: a config alias that does (see _alias_pushes), or any such subcommand
    next to config that may define one, inline (see _alias_inline_config) or from the
    environment (`HOME=/tmp/h git p`, `$env:GIT_CONFIG_GLOBAL=...`, see _ALIAS_ENV_RE).
    The git commands are the ones in plain sight (see _git_commands) and the ones
    _push_signals reads (`bash -c 'git p'`, `echo "$(git p)"`, `ssh-agent git p`, `g\\it
    p`): `unknown` is its list for `command` (see _git_signals), computed here if not
    given; a span too deep to read with a git word and a possible alias in it counts
    once. The alias is looked up where the command runs (its cd, -C and --git-dir, as
    _scan_targets reads them for a push; else the hook's cwd), one `git config --get`
    per name and place, at most MAX_ALIAS_LOOKUPS."""
    if unknown is None:
        unknown = []
        token = _POWERSHELL.set(bool(powershell))
        try:
            _push_signals(command, unknown=unknown)
        finally:
            _POWERSHELL.reset(token)
    if not unknown and "git" not in command.lower():
        return 0
    # Per name, the larger of the two readings' counts (a command read by both is one).
    inline, names, read_inline, read_names, deep = 0, {}, 0, {}, False
    plain = {}  # (name, global option words) of the ones in plain sight
    # Fail closed: one behind a wrapper (`env -C r git zz`, `sudo -D r git zz`, `find
    # -execdir git zz`) or only _push_signals reads (`bash -c 'cd rb && git zz'`,
    # `eval`, `$(...)`, `Start-Process`) may run anywhere: it counts with no lookup (a
    # lookup in the hook's cwd may find nothing). Only a bare `git zz` is looked up.
    wrapped = {}
    moved = 0
    for opts, sub, _, _, bare in _git_commands(command):
        if sub in _GIT_BUILTINS or not _ALIAS_NAME_RE.fullmatch(sub):
            continue
        key = (sub, tuple(opts))
        if _alias_inline_config(opts):
            inline += 1
        elif not bare:
            moved += 1
            wrapped[key] = wrapped.get(key, 0) + 1
        else:
            names[sub] = names.get(sub, 0) + 1
            plain[key] = plain.get(key, 0) + 1
    for entry in unknown:
        if entry is None:
            deep = True
        elif _alias_inline_config(entry[0]):
            read_inline += 1
        else:
            key = (entry[1], tuple(entry[0]))
            if plain.get(key):
                plain[key] -= 1
            elif wrapped.get(key):
                wrapped[key] -= 1  # the same command, counted above
                continue
            elif not entry[3]:
                moved += 1
                continue
            read_names[entry[1]] = read_names.get(entry[1], 0) + 1
    for name, count in read_names.items():
        names[name] = max(names.get(name, 0), count)
    hits = max(inline, read_inline) + deep + moved
    if not names:
        return hits
    lowered = command.lower()
    if _ALIAS_ENV_RE.search(lowered):
        # Config files (or config) the environment names: no lookup here reads them.
        return hits + sum(names.values())
    places = {}
    try:
        pattern = re.compile(_SEP + _LEAD + _ENV_PREFIX + _GIT + r"(" + GIT_GLOBAL_OPTS
                             + r")\s+((?i:" + "|".join(map(re.escape, names))
                             + r"))(?![\w-])")
        for name, where, _, git_opts in _scan_targets(command, cwd, MAX_TARGETS, powershell,
                                                      pattern):
            places.setdefault(name.lower(), []).append((where, git_opts))
    except TooManyTargets:
        places = {}
    budget = [MAX_ALIAS_LOOKUPS]
    for name, count in names.items():
        if any(_alias_pushes(name, where, git_opts, deadline, budget)
               for where, git_opts in places.get(name) or [(cwd, None)]):
            hits += count
    return hits


def gate(payload, cfg, classify_fn=None, log_fn=None, now=time.time, state_dir=STATE_DIR,
         deadline=None):
    """Return the PreToolUse hook output dict, or None to leave the command unchanged.

    Every git commit/push in the command is gated together: with a push anywhere the
    operation is a push, and the diff sent is each commit's pending diff followed by
    each push's unpushed range.

    The whole invocation runs under one monotonic `deadline` (by default
    GATE_BUDGET_SECONDS from entry). All git subprocess calls share at most
    GIT_SUBPROCESS_BUDGET_SECONDS of it instead of each getting its own 2 s; once that
    is spent, remaining git calls fail open (return None/allow). The classifier gets
    only what remains, and the denied-hash update stops waiting for its lock at the
    deadline (the command is still denied), so the invocation stays inside the hook
    timeout (each stage is bounded; it can overshoot the deadline only slightly).
    """
    # A PowerShell command is read as one throughout (see _push_lex, _dq_subst).
    token = _POWERSHELL.set(isinstance(payload, dict)
                            and payload.get("tool_name") == "PowerShell")
    try:
        return _gate(payload, cfg, classify_fn, log_fn, now, state_dir, deadline)
    finally:
        _POWERSHELL.reset(token)


def _config_problem_denial(payload, cfg, log_fn, deadline):
    """The fail-closed denial for a command that may push (as _gate counts pushes:
    parsed, by _push_signals or by _alias_signals) while the config file can't be read
    (load_config's `config_problem`), else None."""
    tool_input = payload.get("tool_input")
    given = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(given, str):
        return None
    command = _unwrap_shell(given)
    cwd = _native_path(payload.get("cwd") or os.getcwd())
    powershell = payload.get("tool_name") == "PowerShell"
    try:
        pushes = any(t[0] == "push" for t in _scan_targets(command, cwd, MAX_TARGETS, powershell))
    except TooManyTargets:
        pushes = True
    unknown = []
    pushes = (pushes or _push_signals(command, unknown=unknown) > 0
              or _push_signals(given, unknown=unknown) > 0
              or _alias_signals(command, cwd, min(time.monotonic() + ALIAS_LOOKUP_SECONDS,
                                                  deadline), powershell, unknown) > 0)
    if not pushes:
        return None
    if log_fn:
        log_fn({"ts": timestamp(), "feature": "risk_gate", "op": "push", "decision": "deny",
                "reason": "config_problem", "fail_closed": True})
    return fail_closed_output(f"has no usable configuration (the Jev config file "
                              f"{cfg['config_problem']}; fix or remove it)")


def _gate(payload, cfg, classify_fn, log_fn, now, state_dir, deadline):
    """gate's body, with _POWERSHELL set."""
    entered = time.monotonic()
    if deadline is None:
        deadline = entered + GATE_BUDGET_SECONDS
    # PowerShell runs git just as Bash does; its commands go through the same scanner.
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    if cfg.get("config_problem"):
        # The config file is there but unreadable: nothing says whether the gate was
        # meant to be on or which key it was meant to use, so a push fails closed.
        denial = _config_problem_denial(payload, cfg, log_fn, deadline)
        if denial is not None:
            return denial
    if not feature_enabled(cfg, "risk_gate"):
        return None
    tool_input = payload.get("tool_input")
    raw_command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(raw_command, str):
        return None
    # `bash -lc '<script>'` is gated as its script (also what a denial's retry is keyed on).
    given_command = raw_command
    raw_command = _unwrap_shell(raw_command)
    base_cwd = _native_path(payload.get("cwd") or os.getcwd())
    try:
        targets = _scan_targets(raw_command, base_cwd, MAX_TARGETS,
                                payload.get("tool_name") == "PowerShell")
    except TooManyTargets:
        # Too many to diff within the budget, and ignoring some would skip their review.
        if log_fn:
            log_fn({"ts": timestamp(), "feature": "risk_gate", "decision": "deny",
                    "reason": "too_many_targets"})
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "[jev risk gate] Too many git targets to review safely: this command "
                f"exceeds a scanner limit ({MAX_TARGETS} candidate targets, bounded "
                "branching, or substitution depth). Split it into smaller commands."),
        }}

    def deny_once(key, reason_code, reason, op="push"):
        """Deny the command once for a check that needs no classifier; the identical
        retry (same `key`, cwd and raw command, among the session's last 20 denials) is
        logged as an override and returns None so the normal flow continues. The cwd
        is part of it so a denial in one repository doesn't pass the command in another."""
        session_id = payload.get("session_id")
        retry_hash = hashlib.sha256(
            f"{key}\0{base_cwd}\0{raw_command}".encode("utf-8", "replace")).hexdigest()
        entry = {"ts": timestamp(), "feature": "risk_gate", "op": op, "reason": reason_code}
        if retry_hash in (load_session_state(session_id, state_dir).get("denied") or []):
            if log_fn:
                log_fn({**entry, "decision": "override"})
            return None

        def add_denied(s):
            s["denied"] = ((s.get("denied") or []) + [retry_hash])[-20:]

        try:
            update_state(session_state_path(session_id, state_dir), add_denied, deadline)
        except Exception as exc:
            # Still deny: without the saved hash, the identical retry is denied again,
            # and the reason says so (its "rerun once" would not work).
            entry["state_error"] = type(exc).__name__
            reason += (" (This denial could not be recorded, so an identical retry will be "
                       "checked again.)")
        if log_fn:
            log_fn({**entry, "decision": "deny"})
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}

    # A push the scanner didn't parse (quoted, aliased or wrapped, possibly next to one
    # it did: `git push; bash -c "git push --force"`) can't be diffed, so it is denied
    # once instead of passing unreviewed. Parsed pushes are counted both as distinct
    # targets and as plain `git push` matches, so `git push && git push --tags` (one
    # target, two pushes) isn't taken for a hidden one; the second count, a rescan, runs
    # only when the first falls short.
    # After the override nobody has seen what the command pushes (or commits), so it
    # is still gated as a plain push (commit) in the command's cwd: `backstop`. Such a
    # push may force, delete or go elsewhere whatever that cwd's diff shows, so it also
    # carries a note (`wrapped_push`) and is always put to the classifier.
    backstop = None
    wrapped_push = False
    unknown = []  # subcommands that may be config aliases (see _alias_signals)
    signals = _push_signals(raw_command, unknown=unknown)
    if raw_command != given_command:
        # Unwrapping never lowers the count (a script shlex read differently).
        signals = max(signals, _push_signals(given_command, unknown=unknown))
    # A subcommand that is none of git's own may be a config alias that pushes (`git
    # p`): looked up in the repository it runs in, within a short budget of its own.
    signals += _alias_signals(
        raw_command, base_cwd,
        min(time.monotonic() + ALIAS_LOOKUP_SECONDS, entered + GIT_SUBPROCESS_BUDGET_SECONDS,
            deadline),
        payload.get("tool_name") == "PowerShell", unknown)
    parsed = sum(t[0] == "push" for t in targets)
    if signals > parsed:
        parsed = max(parsed, sum(m.group(2) == "push"
                                 for m in GIT_COMMAND_RE.finditer(_strip(raw_command)[0])))
    if signals > parsed:
        denial = deny_once("unparsed-push", "unparsed_push", UNPARSED_PUSH_REASON)
        if denial is not None:
            return denial
        backstop = ("push", base_cwd, False, None)
        wrapped_push = True
    else:
        # The same backstop for a commit the scanner didn't parse. Only when the push
        # one didn't fire: it counts the same wrappers, aliases and dynamic subcommands,
        # so one command costs at most one such denial.
        signals = _commit_signals(raw_command)
        parsed = sum(t[0] == "commit" for t in targets)
        if signals > parsed:
            parsed = max(parsed, sum(m.group(2) == "commit"
                                     for m in GIT_COMMAND_RE.finditer(_strip(raw_command)[0])))
        if signals > parsed:
            denial = deny_once("unparsed-commit", "unparsed_commit", UNPARSED_COMMIT_REASON,
                               "commit")
            if denial is not None:
                return denial
            backstop = ("commit", base_cwd, bool(ALL_FLAG_RE.search(raw_command)), None)
    if backstop is not None and any(t[:2] == backstop[:2] for t in targets):
        backstop = None  # a parsed one already covers this cwd
    if backstop is not None:
        targets.append(backstop)
    if not targets:
        return None
    op = "push" if any(t[0] == "push" for t in targets) else "commit"
    # Commits run before the push that follows them, so their diffs come first.
    targets.sort(key=lambda t: t[0] == "push")

    git_deadline = min(entered + GIT_SUBPROCESS_BUDGET_SECONDS, deadline)
    max_diff_chars = _char_limit(cfg, "max_diff_chars")
    diff = ""
    names = []
    paths = []  # where each name's mtime is read for critic coverage
    names_failed = False
    untracked_overflow = False
    untracked_identity = []
    push_unjudged = False  # a push whose range couldn't be diffed (see below the loop)
    # What a push's arguments ask for beyond the checked-out branch's unpushed commits:
    # other source refs are diffed against their own remote counterpart, and a force,
    # a deleted remote ref or a wider push is noted for the classifier (see _push_plan).
    push_notes = [("wrapped", WRAPPED_PUSH_NOTE)] if wrapped_push else []
    requests = ([_push_request(args, git_words)
                 for git_words, args in _push_commands(raw_command)] if op == "push" else [])
    if not requests and any(t[0] == "push" and t is not backstop for t in targets):
        # A parsed push whose words the lexer could not read may force or delete too.
        push_notes.append(("refspec", "the arguments of this push could not be read"))
    if op == "push" and _PUSH_CONFIG_ENV_RE.search(raw_command):
        push_notes.append(("config", _PUSH_CONFIG_NOTE))
    jobs = []  # one (target..., base, source) per diff
    for target in targets:
        if target[0] != "push":
            jobs.append((*target, None, "HEAD"))
            continue
        ranges, notes, unresolved = _push_plan(requests, target[1], git_deadline, target[3])
        push_notes += [n for n in notes if n not in push_notes]
        push_unjudged = push_unjudged or unresolved
        jobs += [(*target, base, source) for base, source in ranges]
    for target_op, cwd, all_flag, git_opts, base, source in jobs:
        part = _diff_range(target_op, all_flag, cwd, git_deadline, base, git_opts, source)
        if part is None:
            push_unjudged = push_unjudged or target_op == "push"
            continue
        names_out = _diff_names(target_op, all_flag, cwd, git_deadline, base, git_opts, source)
        names_failed = names_failed or names_out is None
        part_names = [n for n in (names_out or "").split("\0") if n]
        toplevel = _run_git(["rev-parse", "--show-toplevel"], cwd, git_deadline, git_opts)
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
        untracked = _untracked_files(cwd, git_deadline, git_opts)
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

    # No upstream or origin default branch, a failed git call or a spent git budget
    # leaves a push's range unknown; deny it once rather than let it pass unreviewed.
    if push_unjudged:
        denial = deny_once("unjudged-push", "unjudged_push", UNJUDGED_PUSH_REASON)
        if denial is not None:
            return denial
    # A force, a delete or a wider push changes the remote whatever the diff shows, so
    # it is never passed unasked (nor counted as covered by an earlier critic run).
    if not diff and not push_notes:
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
        covered = (not push_notes and not names_failed and all_stat_ok
                   and not untracked_overflow
                   and (critic_ts >= max(mtimes) if mtimes else True))

    # Keep local metadata in the retry identity so editing an omitted untracked
    # file requires a new check, without transmitting its contents to Jev.
    retry_identity = (diff, untracked_identity)
    if push_notes:
        # A note says little (`force`), so the override is tied to this push: where it
        # runs and the remote and refs it names, or the whole command when those could
        # not be read. Else one overridden force push would cover every later one.
        retry_identity += (
            push_notes, base_cwd, sorted({t[1] for t in targets if t[0] == "push"}),
            [(r["remote"], r["specs"], r["deletes"], r["force"], r["wide"]) for r in requests],
            raw_command if (wrapped_push or not requests
                            or any(kind in ("runtime", "option", "program", "config")
                                   for kind, _ in push_notes)
                            or any(r["dynamic"] for r in requests)) else "")
    diff_hash = hashlib.sha256(repr(retry_identity).encode("utf-8", "replace")).hexdigest()
    start = time.monotonic()

    def log(decision, choice=None, confidence=None, needs_review_p=None, **extra):
        if not log_fn:
            return
        entry = {"ts": timestamp(), "feature": "risk_gate", "op": op, "decision": decision,
                 "risk": choice, "confidence": confidence, "needs_review": needs_review_p,
                 "files": len(names), "diff_chars": len(diff), "latency_ms": elapsed_ms(start)}
        if push_notes:
            entry["push_flags"] = sorted({kind for kind, _ in push_notes})  # never ref names
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
    if push_notes:
        ask_state["push"] = _scrub("; ".join(text for _, text in push_notes))[
            :MAX_PUSH_NOTE_CHARS]
    if cfg.get("send_diff"):
        # Hunks of secret-looking files and common token shapes never leave the machine.
        ask_state["diff"] = _scrub(_redact_diff(_diff_prefix(diff, max_diff_chars)))[
            : max_diff_chars]
    questions = {
        "risk": {"type": "choice", "instructions": RISK_INSTRUCTIONS, "criteria": RISK_CRITERIA},
        "needs_review": {"type": "noul", "instructions": NEEDS_REVIEW_INSTRUCTIONS},
    }
    errs = []
    answers = ask(cfg, "risk_gate", ask_state, questions, classify_fn, errors=errs,
                  deadline=deadline)
    if answers is None:
        # Features were checked above, so None here means an error or a disallowed
        # endpoint (which records no error); either way nobody judged the push.
        if op == "push" and errs[:1] == ["NoApiKey"]:
            # No key configured at all (no env var, no key file) is a setup gap, not an
            # outage: blocking would deny every push for good, so this one case fails
            # open, with a warning instead of silence (main() also shows it to the user).
            log("allow", reason="unavailable", error="NoApiKey", fail_open=True)
            try:
                sys.stderr.write(NO_KEY_WARNING + "\n")
            except Exception:
                pass
            return None
        if op == "push":
            log("deny", reason="unavailable", error=errs[0] if errs else "EndpointDisallowed",
                fail_closed=True)
            if errs[:1] == ["BadApiKeyFile"]:
                # A key file that is set but unusable is a misconfiguration, not a
                # missing setup: blocked like an outage, naming what is wrong with it.
                # The setting is quoted only when it reads as a path: a key pasted
                # there by mistake must not show up in the reason.
                named = cfg.get("api_key_file")
                problem = api_key_problem(cfg) or "cannot be used"
                if api_key_source_problem(cfg):
                    # `jev.api_key_source` chosen on purpose, giving no key. Only the
                    # known value is quoted: a key pasted there must not show up.
                    source = ("jev.api_key_source \"claude_settings\""
                              if cfg.get("api_key_source") == "claude_settings"
                              else "jev.api_key_source")
                    return fail_closed_output(
                        f"has no usable API key ({source} {problem}; "
                        "fix it or set TYPESAFE_API_KEY)")
                is_path = (isinstance(named, str) and len(named) < 300
                           and (named.startswith("~") or "/" in named or "\\" in named))
                return fail_closed_output(
                    "has no usable API key ("
                    + (f"the file jev.api_key_file names {problem}: {named}" if is_path
                       else f"the configured jev.api_key_file {problem}")
                    + "; fix it or set TYPESAFE_API_KEY)")
            return fail_closed_output()
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
        if op == "push":
            log("deny", reason="malformed", fail_closed=True)
            return fail_closed_output("returned an unusable answer")
        log("allow")
        return None
    # Confidence is only logged, so an invalid one (missing, null, str, bool, out of
    # range) is logged as None, as in jev-route, and doesn't skip the gate.
    risk = answers["risk"]
    confidence = coerce_confidence(risk.get("confidence") if isinstance(risk, dict) else None)
    needs_review_p = noul(answers, "needs_review")
    if op == "push" and choice != "none" and needs_review_p is None:
        log("deny", choice, confidence, None, reason="malformed", fail_closed=True)
        return fail_closed_output("returned an unusable answer")

    risky = (choice != "none" and needs_review_p is not None
             and needs_review_p >= cfg["risk_min_probability"])
    if not risky:
        log("allow", choice, confidence, needs_review_p)
        return None

    def add_denied(s):
        s["denied"] = ((s.get("denied") or []) + [diff_hash])[-20:]

    extra = {}
    try:
        update_state(session_state_path(session_id, state_dir), add_denied, deadline)
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
            return "".join(item["text"] for item in content
                           if isinstance(item, dict) and item.get("type") == "text"
                           and isinstance(item.get("text"), str))
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
        body = text[m.end():end].strip()
        # Last non-empty wins: a quoted block or stale verdict earlier in the report yields
        # to the real one, and an empty bare header (e.g. the format quoted inside
        # EVIDENCE) can't wipe out a section.
        if body or name not in sections:
            sections[name] = body
    return sections


def _analyze_report(text, cfg, classify_fn=None, confidence_heuristic=True, gap_blocks=True,
                    errors=None, deadline=None):
    """Parse RESULT/EVIDENCE/CONFIDENCE/UNVERIFIED sections and ask Jev whether the
    report is weak. Returns (reasons, reason_codes, supported, material_gap).

    `confidence_heuristic` gates the self-reported low/medium CONFIDENCE check: it
    applies to the PostToolUse agent-done nudge (a foreground report can be told to
    verify or escalate more), but not to the SubagentHandback deny path (a read-only
    critic can't escalate, so low/medium confidence alone must not block it).
    `gap_blocks` likewise gates the material-gap verdict: an honest UNVERIFIED list is
    what the report should contain, and verifying it is the main session's job, so
    the deny paths log the gap without blocking on it.

    Sections are parsed from the FULL text (a long report's RESULT/EVIDENCE/CONFIDENCE/
    UNVERIFIED block may come after `max_report_chars` of findings), so truncation is
    only applied to what's actually sent to the classifier below, after common token
    shapes are scrubbed. `errors` is passed to `ask` (a failed call appends its reason),
    and so is `deadline`, the hook's shared time.monotonic() deadline."""
    max_chars = _char_limit(cfg, "max_report_chars")

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
        answers = ask(cfg, "report_check", ask_state, questions, classify_fn, errors=errors,
                      deadline=deadline)
        if answers is not None:
            supported = noul(answers, "supported")
            material_gap = noul(answers, "material_gap")
            if supported is not None and supported < cfg["report_min_support"]:
                reasons.append(f"evidence weakly supports result (p={supported:.2f})")
            if gap_blocks and material_gap is not None and material_gap >= cfg["report_max_gap"]:
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


def handback(payload, cfg, classify_fn=None, log_fn=None, state_dir=STATE_DIR, deadline=None):
    """Return the PreToolUse hook output dict for a SubagentHandback call, or None.

    The classifier call and state-lock wait share one monotonic `deadline` (by default
    SHORT_HOOK_BUDGET_SECONDS from entry)."""
    if deadline is None:
        deadline = time.monotonic() + SHORT_HOOK_BUDGET_SECONDS
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
        message, cfg, classify_fn, confidence_heuristic=False, gap_blocks=False, errors=errs,
        deadline=deadline)

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
        update_state(session_state_path(session_id, state_dir), add_handback_denied, deadline)
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


def agent_done(payload, cfg, classify_fn=None, log_fn=None, now=time.time, state_dir=STATE_DIR,
               deadline=None):
    """Return the PostToolUse hook output dict, or None.

    critic_ts is stamped from when the critic *started* (its Agent/Task launch), not
    when it finished: files edited while the critic was running were not necessarily
    seen by it, so they must still need a fresh review once it comes back. `critic_started` (a per-agent-id map of launch
    timestamps under session state) bridges the async-launch PostToolUse event to the
    later SubagentHandback or completed-foreground-result event.

    State-lock waits and the classifier call share one monotonic `deadline` (by default
    SHORT_HOOK_BUDGET_SECONDS from entry).
    """
    if deadline is None:
        deadline = time.monotonic() + SHORT_HOOK_BUDGET_SECONDS
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
                update_state(session_state_path(session_id, state_dir), set_critic_ts, deadline)
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
                    update_state(session_state_path(session_id, state_dir), add_critic_started,
                                 deadline)
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
            update_state(session_state_path(session_id, state_dir), set_critic_ts_completed,
                         deadline)
        except Exception:
            pass  # persisting critic timestamps is best effort

    if not feature_enabled(cfg, "report_check"):
        return None
    if subagent_type not in cfg["report_roles"]:
        return None

    text = _extract_report_text(completed)
    if HANDBACK_STUB_RE.fullmatch(text):
        if log_fn:
            log_fn({"ts": timestamp(), "feature": "report_check", "event": "agent_done",
                    "decision": "skipped_handback_stub", "subagent_type": subagent_type})
        return None
    start = time.monotonic()
    errs = []
    reasons, reason_codes, supported, material_gap = _analyze_report(
        text, cfg, classify_fn, errors=errs, deadline=deadline)

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
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    # What the crash path matches: the command once the payload parses (not cwd or
    # transcript paths), the raw text only when it does not.
    command_text = ""
    try:
        # Hook payloads are UTF-8 whatever the locale (e.g. cp1255 on Windows).
        command_text = sys.stdin.buffer.read().decode("utf-8", "replace")
        payload = json.loads(command_text or "{}")
        tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        command_text = command if isinstance(command, str) else ""
        cfg = load_config()
        if mode == "gate":
            unreviewed = []  # a push let through because no key is configured

            def gate_log(entry):
                if entry.get("fail_open"):
                    unreviewed.append(entry)
                write_log(cfg, entry)
            output = gate(payload, cfg, log_fn=gate_log)
            if output is None and unreviewed:
                # stderr at exit 0 is not shown; a systemMessage is, and it carries no
                # permission decision, so the push still goes through as before.
                output = {"systemMessage": NO_KEY_WARNING}
        elif mode == "handback":
            output = handback(payload, cfg, log_fn=lambda entry: write_log(cfg, entry))
        elif mode == "agent-done":
            output = agent_done(payload, cfg, log_fn=lambda entry: write_log(cfg, entry))
        else:
            output = None
        if output is not None:
            sys.stdout.write(json.dumps(output))
    except Exception:
        # A crashed gate must not wave through a push or deploy (see OUTWARD_COMMAND_RE).
        try:
            if mode == "gate" and (
                    OUTWARD_COMMAND_RE.search(command_text)
                    or OUTWARD_COMMAND_RE.search(_OUTWARD_QUOTING_RE.sub("", command_text))):
                sys.stdout.write(json.dumps(fail_closed_output()))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

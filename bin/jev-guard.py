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
    DEFAULTS, ROOT, ask, coerce_confidence, config_number, elapsed_ms, feature_enabled, load_config,
    noul, timestamp, write_log)

def _char_limit(cfg, key):
    """cfg[key] as a non-negative whole number of characters; junk falls back to its default."""
    value = config_number(cfg.get(key), 0.0, float(1 << 40))
    return int(value) if value is not None and value.is_integer() else DEFAULTS[key]


STATE_DIR = ROOT / "relay" / "state"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Global options git accepts before the subcommand: -C <dir>, -c <key>=<value>, a
# space-separated long option that takes a value (--git-dir, --work-tree, --namespace,
# --super-prefix, --config-env), or any other --long-option (with or without =value).
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
_LONG_VALUE_OPTS = r"(?:--git-dir|--work-tree|--namespace|--super-prefix|--config-env)"
# The value-taking names are excluded from the generic --long-option branch, and their
# space-separated branch always takes the next token as the value (even `--git-dir -x`),
# so each token has exactly one way to match; overlapping branches here backtrack
# exponentially on repeated options.
GIT_GLOBAL_OPTS = (r"(?:\s+(?:-C\s+" + _OPT_ARG + r"|-c\s+" + _OPT_ARG + r"|"
                    + _LONG_VALUE_OPTS + r"(?:=(?:" + _OPT_ARG + r")?|\s+" + _OPT_ARG + r")|"
                    r"--(?!(?:git-dir|work-tree|namespace|super-prefix|config-env)(?![\w-]))"
                    r"[\w-]+(?:=" + _OPT_TAIL + r")?))*")
# What may start a command: the start, a ; & | ( operator, a newline, a `{` group or
# a backtick substitution, or a `)` (so `X=$(date) git push`, whose value the env
# prefix below can't span, is still seen). Only blanks follow it (_LEAD): a newline is a separator in
# its own right, and `\s*` there let each newline of a long run re-consume the rest of
# the run (quadratic).
_SEP = r"(?:^|[;&|()\n{`])"
_LEAD = r"[ \t]*"
# Env-assignment (e.g. `GIT_EDITOR=true git commit`, `A=1 B=2 git push`), shell keyword
# (then/do/else) and wrapper (time/exec/command/env/nice/sudo, with their -flags and a
# numeric flag value such as `nice -n 5`) prefixes before the `git` invocation itself.
# Only blanks separate them: a newline ends the statement (and is a _SEP itself), and
# `\s+` here let each line of `A=b\n` * N re-consume the rest of the run (quadratic).
# A value may hold backslash escapes (`X=a\ b git push`); an escape pair and a plain
# char start differently, so the value still has one way to match.
_ENV_PREFIX = (r"(?:(?:then|do|else|time|exec|command|env|nice|sudo)[ \t]+"
               r"(?:-[\w-]+(?:[ \t]+\d+)?[ \t]+)*"
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
_CD_WORD = r"cd(?:[ \t]+-[\w-]*)*"
_CD_PREFIX_RE = re.compile(_SEP + _LEAD + _CD_WORD + r"[ \t]+$")
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
    r"""\\[\s\S]|"(?:[^"\\]|\\[\s\S])*"|'[^']*'|(&&|\|\||;|\n|\(|\)|\{|`)|(case|esac)""")
# Longest directory _cd_dirs accumulates (`cd a; ` * N would otherwise grow it, and the
# time and memory to build it, quadratically); see _cd_segment_dir.
MAX_CD_PATH_CHARS = 4096
# Most cwds _case_branch_dirs carries at once (the cwd before a case and its first
# branch ends), so a cd costs at most this many joins.
MAX_CASE_BRANCHES = 16
# Most `||` operators with a cd next to them whose every success/failure combination
# _scan_targets enumerates (2**k readings); with more, only the two one-sided readings.
MAX_OR_ENUM = 4
# Most distinct targets one command may have; past it _scan_targets raises
# TooManyTargets and the gate denies (a pathological command otherwise reaches tens of
# thousands of targets and hundreds of MB).
MAX_TARGETS = 256
# How deep a heredoc body's `$( )` commands are themselves scanned for heredocs.
MAX_HEREDOC_DEPTH = 3


class TooManyTargets(Exception):
    """A command has more than the allowed number of distinct git targets."""

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
                         "credentials*", "*.jks", "*.keystore")
# Token shapes scrubbed from diff and report text before it is sent. A key body may not
# contain another marker or cross a hunk/file boundary, so each BEGIN scans only up to the
# next one (linear time).
SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
    r"(?:(?!-----(?:BEGIN|END) |\ndiff --git |\n@@).)*"
    r"-----END [A-Z ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
    r"|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[\w-]{10,}", re.DOTALL)
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
    `x--foo-C`, `--git-dir=`, a cd option) or to end in `cd`. A `$`-anchored search
    tries every start in the tail, which made them most of a quoted argument's cost."""
    words = tail.rsplit(None, 1)
    return bool(words) and ("-" in words[-1] or words[-1].endswith("cd"))


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
        _CD_SPLIT_RE splits: `;`, newline, a `{ ` and the second char of `&&` / `||`
        (a single `&` or `|`, `|&`, `>&2`, `&>`, `>|` and `{a,b}` are not: a pipeline
        stage or background command is not a place a cd moves)."""
        ch = command[at]
        if ch in ";\n":
            return True
        if ch in "&|":
            return command[at - 1:at] == ch
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
            if _may_keep(tail) and (
                    _GIT_DASH_C_PREFIX_RE.search(tail) or _CD_PREFIX_RE.search(tail)
                    or (trimmed and _LOOSE_KEEP_RE.search(tail))):
                emit('""' if _OPAQUE_VALUE_RE.search(tail) else command[i:end])
            elif (_GIT_WORD_RE.fullmatch(command, i + 1, end - 1)
                  and (end == n or command[end] in " \t\n;&|()<>")
                  and _COMMAND_POS_RE.search(tail)):
                # A quoted word the command word goes on after (`'git'x push`) is
                # dropped instead, as before, which keeps the rest of it visible.
                emit("git")
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
        if KEY_END_RE.search(line):
            found = True
            if prev is not None and (KEY_LINE_RE.match(lines[prev]) or CRC_LINE_RE.match(lines[prev])):
                cut = i
        if not blank(line):
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
        if KEY_BEGIN_RE.search(lines[i]):
            found = True
            if (KEY_LINE_RE.match(lines[nxt]) if nxt is not None else header) or (
                    short and short_header):
                begin_at = i
        if KEY_END_RE.search(lines[i]):
            short, short_header = True, False
        elif KEY_HEADER_LINE_RE.match(lines[i]):
            short_header = True
        elif not (blank(lines[i]) or BASE64_LINE_RE.match(lines[i])):
            short, short_header = False, False
        if KEY_HEADER_LINE_RE.match(lines[i]):
            header = True
        elif not blank(lines[i]):
            nxt, header = i, False
    if begin_at is not None:
        begin = KEY_BEGIN_RE.search(lines[begin_at])
        newline = "\n" if lines[-1].endswith("\n") else ""
        lines[begin_at:] = [lines[begin_at][:begin.start()] + "[redacted]" + newline]
    if found:
        lines = [KEY_END_RE.sub("[redacted]", KEY_BEGIN_RE.sub(
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

    hunks = []
    for hunk in HUNK_SPLIT_RE.split(SECRET_RE.sub("[redacted]", text)):
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


def _run_git(args, cwd, deadline=None, git_opts=None):
    """Run a git subprocess with a per-call timeout bounded by the shared `deadline`
    (a time.monotonic() budget end for the whole gate() invocation); if the budget is
    already exhausted, fail open (return None) rather than block past the hook timeout.
    `git_opts` are the command's own `--git-dir`/`--work-tree` options, if it gave any."""
    if git_opts:
        args = [*git_opts, *args]
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


def _push_base(cwd, deadline=None, git_opts=None):
    """The ref a push is compared against: the upstream, else the push remote's
    (remote.pushDefault, or origin) default branch from `refs/remotes/<remote>/HEAD`,
    else origin/main, else origin/master; None when none resolves."""
    if _run_git(["rev-parse", "--verify", "--quiet", "@{u}"], cwd, deadline, git_opts) is not None:
        return "@{u}"
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


def _diff_range(op, all_flag, cwd, deadline=None, base=None, git_opts=None):
    if op == "commit":
        args = ["diff", *DIFF_FORMAT_ARGS] + (["HEAD"] if all_flag else ["--cached"])
        return _run_git(args, cwd, deadline, git_opts)
    if base is None:
        return None
    return _run_git(["diff", *DIFF_FORMAT_ARGS, f"{base}...HEAD"], cwd, deadline, git_opts)


def _diff_names(op, all_flag, cwd, deadline=None, base=None, git_opts=None):
    if op == "commit":
        args = (["diff", *DIFF_FORMAT_ARGS, "HEAD", "--name-only", "-z"] if all_flag
                else ["diff", *DIFF_FORMAT_ARGS, "--cached", "--name-only", "-z"])
        out = _run_git(args, cwd, deadline, git_opts)
        return out
    if base is None:
        return None
    return _run_git(["diff", *DIFF_FORMAT_ARGS, f"{base}...HEAD", "--name-only", "-z"], cwd,
                    deadline, git_opts)


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


def _global_opts(opts_segment):
    """(-C dir, --git-dir, --work-tree) from the matched global-options segment, each
    None when absent or unusable (see _usable_path). Several -C accumulate as in git
    (`-C a -C b` is a/b; an absolute one resets). shlex-aware so `-C "dir with space"`
    works; falls back to splitting on blanks on shlex errors (e.g. unbalanced quotes)."""
    try:
        # Without quotes or escapes shlex splits on its blanks (` \t\r\n`) too, only far
        # slower.
        tokens = (shlex.split(opts_segment) if any(q in opts_segment for q in "'\"\\")
                  else [t for t in re.split(r"[ \t\r\n]+", opts_segment) if t])
    except ValueError:
        tokens = opts_segment.split()
    dash_c, capped, values = None, False, {}
    i = 0
    while i < len(tokens):
        name, eq, value = tokens[i].partition("=")
        if tokens[i] in ("-C", "-c") or (not eq and name in ("--git-dir", "--work-tree",
                                                              "--namespace", "--super-prefix",
                                                              "--config-env")):
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
    &&/||/;/newline/`(`/`)`/`{`/backtick segments (successive cds accumulate; relative
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
        if ((case_cds or not (open_cases or loose))
                and not _or_cd_skipped(after, after_ord, op, this_ord, or_cds, or_left_cds,
                                       or_choice)):
            result, capped = _cd_segment_dir(segment, result, capped)
        if after != "||" or _has_text(segment, op):
            after, after_ord = op, this_ord  # a blank segment (`||` newline `cd b`) keeps the `||`
        if (op == "(" and restore) or (op == "`" and not (saved and saved[-1][0] == "`")):
            # The heredoc commands _strip puts before a command are transparent to the
            # `||` the segment before it follows (`cd a || cd b <<EOF` with a `$( )`
            # body still has `cd b` after the `||`).
            saved.append((op, result, capped, after, after_ord, _is_subst(command, sep)))
            cases.append(0)
        elif op in (")", "`") and saved and saved[-1][0] == ("(" if op == ")" else "`"):
            # Restored before it is recorded: a git match starting at this `)` (e.g.
            # `X=$(cd sub) git push`) runs outside the subshell.
            _, result, capped, was_after, was_ord, subst = saved.pop()
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
        if target is not None and not _or_cd_skipped(after, after_ord, op, this_ord, or_cds,
                                                      or_left_cds, or_choice):
            cands = _merge_cands(_join_capped(r, target, c) for r, c in cands)
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
            saved.append((op, cands, after, after_ord, _is_subst(command, sep)))
            frames.append([])
        elif op in (")", "`") and saved and saved[-1][0] == ("(" if op == ")" else "`"):
            _, cands, was_after, was_ord, subst = saved.pop()
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
    try:
        tokens = shlex.split(m.group(1))
    except ValueError:
        tokens = m.group(1).split()
    # Drop options (-P, --) and redirections (2>/dev/null) around the directory.
    tokens = [t for t in tokens if not t.startswith("-") and not re.match(r"^\d*[<>]", t)]
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


def _scan_targets(raw_command, base_cwd, max_targets=None):
    """The distinct (op, cwd, all_flag, git_opts) targets of every git commit/push in the
    command; more than `max_targets` (if given) raise TooManyTargets. The cd index and first `git add` are computed once, so a command with
    thousands of git invocations still resolves each one in (near) constant time."""
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
    for raw_command in raws:
        command, frame_open = _strip(raw_command)
        commands.append(command)
        if "<<" in raw_command:
            if "((" in raw_command or "$[" in raw_command:
                commands.append(_strip(raw_command, "legacy")[0])
            if frame_open:
                commands.append(_strip(raw_command, "none")[0])
    targets, seen = [], set()
    for k, command in enumerate(commands):
        if command in commands[:k]:
            continue
        add = GIT_ADD_RE.search(command)
        add_end = add.end() if add else None
        cd_indexes = [_cd_dirs(command)]
        # A case word _CASE_RE doesn't take (`case $(f $(g $(h))) in`) leaves its
        # pattern `)` taken for a subshell's, restoring the cwd too early, and a cd in a
        # case branch may or may not run; so with a `case` in the command, also resolve
        # each git with no `)` restoring the cwd, and with no cd inside a case counted.
        # Also each case branch's cd taken on its own (with subshells restoring), and,
        # with a `||`, each success/failure combination of the ones with a cd next to
        # them (at most MAX_OR_ENUM, else no cd right after any, or none right before
        # any), also for the case branches (see _cd_dirs, _case_branch_dirs).
        matches = list(GIT_COMMAND_RE.finditer(command))
        or_readings = ()
        if "||" in command:
            ords = _or_cd_ordinals(command)
            if 0 < len(ords) <= MAX_OR_ENUM:
                or_readings = tuple({"or_choice": dict(zip(ords, picks))}
                                    for picks in itertools.product("RL", repeat=len(ords)))
            elif ords:
                or_readings = ({"or_cds": False}, {"or_left_cds": False})
        branch_dirs = []
        resolved = {}
        if _CASE_WORD_RE.search(command):
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
    entered = time.monotonic()
    if deadline is None:
        deadline = entered + GATE_BUDGET_SECONDS
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    if not feature_enabled(cfg, "risk_gate"):
        return None
    tool_input = payload.get("tool_input")
    raw_command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(raw_command, str):
        return None
    base_cwd = _native_path(payload.get("cwd") or os.getcwd())
    try:
        targets = _scan_targets(raw_command, base_cwd, MAX_TARGETS)
    except TooManyTargets:
        # Too many to diff within the budget, and ignoring some would skip their review.
        if log_fn:
            log_fn({"ts": timestamp(), "feature": "risk_gate", "decision": "deny",
                    "reason": "too_many_targets"})
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"[jev risk gate] Too many git targets to review: the scanner found more "
                f"than {MAX_TARGETS} candidate git targets (cwd/option combinations) in "
                "this command. Split it into smaller commands."),
        }}
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
    for target_op, cwd, all_flag, git_opts in targets:
        base = _push_base(cwd, git_deadline, git_opts) if target_op == "push" else None
        part = _diff_range(target_op, all_flag, cwd, git_deadline, base, git_opts)
        if part is None:
            continue
        names_out = _diff_names(target_op, all_flag, cwd, git_deadline, base, git_opts)
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
    # Confidence is only logged, so an invalid one (missing, null, str, bool, out of
    # range) is logged as None, as in jev-route, and doesn't skip the gate.
    risk = answers["risk"]
    confidence = coerce_confidence(risk.get("confidence") if isinstance(risk, dict) else None)
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

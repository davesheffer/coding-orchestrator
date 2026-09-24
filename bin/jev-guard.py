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
import hashlib
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
except ImportError:  # e.g. Windows: update_state falls back to unlocked writes.
    fcntl = None

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
# An env-assignment prefix (e.g. `GIT_EDITOR=true git commit`, `A=1 B=2 git push`)
# before the `git` invocation itself.
_ENV_PREFIX = r"(?:[A-Za-z_]\w*=\S*\s+)*"
GIT_COMMAND_RE = re.compile(
    r"(?:^|[;&|(\n])\s*" + _ENV_PREFIX + r"git(" + GIT_GLOBAL_OPTS + r")\s+(commit|push)\b")
GIT_ADD_RE = re.compile(r"(?:^|[;&|(\n])\s*" + _ENV_PREFIX + r"git" + GIT_GLOBAL_OPTS + r"\s+add\b")
# -a/--all, or a short-flag cluster containing a (e.g. -am), within the commit segment.
ALL_FLAG_RE = re.compile(r"(?:^|\s)(--all|-[A-Za-z]*a[A-Za-z]*)(?=\s|$)")
# `cd <dir>` as its own segment (split on &&, ;, ||, or a `(` subshell opener) before
# the git segment.
CD_RE = re.compile(r"^\s*cd\s+(.+?)\s*$")
# `<<` but not a here-string (`<<<`) or the tail of one.
HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)-?\s*['\"]?(\w+)['\"]?")
# More untracked files than this are not folded in; the change then always needs review.
MAX_UNTRACKED = 1000
# Quoted strings are stripped so words inside them (e.g. `echo "git commit"`) can't be
# mistaken for a real command, EXCEPT a quoted -C/-c argument (e.g. -C "dir with
# space") or a `cd "dir"` target, which fix 2 needs intact to resolve the gate's cwd.
QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
# A quoted string is preserved only when it is the value of a literal `-C`/`-c` that
# sits in a git global-options segment (i.e. `git`, zero or more already-matched global
# options, then `-C `/`-c `, right before the quote) — not an arbitrary `-c "..."` in
# unrelated text (e.g. `echo -c "x; git commit -m y"`).
_GIT_DASH_C_PREFIX_RE = re.compile(r"(?:^|[;&|(\n])\s*git" + GIT_GLOBAL_OPTS + r"\s+-[Cc]\s+$")
_CD_PREFIX_RE = re.compile(r"(?:^|[;&|(\n])\s*cd\s+$")
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
    tmp.replace(path)


def update_state(path, mutate_fn):
    """Read-modify-write `path`'s JSON state under an exclusive lock on `<path>.lock`.

    `mutate_fn(state)` mutates a freshly reloaded on-disk state dict in place (or
    returns a replacement dict) so a concurrent writer's unrelated keys survive even
    if this invocation's load was stale. If `fcntl` is unavailable (e.g. Windows), the
    update runs without a lock: still correct for a single process, best-effort under
    real concurrency.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_file = None
    try:
        if fcntl is not None:
            lock_file = open(lock_path, "a+")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
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
        tmp.replace(path)
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_file.close()


def _strip_quotes(text):
    """Remove quoted strings from `text`, except one immediately preceded by a literal
    `-C`/`-c` inside a git global-options segment, or by a `cd`, which `_cd_prefix_dir`
    and `_dash_c_dir` need intact to resolve the gate's cwd."""
    out = []
    pos = 0
    for m in QUOTED_RE.finditer(text):
        prefix = text[:m.start()]
        if _GIT_DASH_C_PREFIX_RE.search(prefix) or _CD_PREFIX_RE.search(prefix):
            continue  # keep this quote: append nothing, let it fall through below
        out.append(text[pos:m.start()])
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _strip_heredocs_and_quotes(command):
    """Remove heredoc bodies and quoted strings so text inside them (e.g. `echo "git
    commit"` or a `cat <<EOF ... git commit ... EOF` body) can't be mistaken for a real
    command. `git commit -m "msg"` still matches: only the quoted message is removed,
    and the verb sits outside it."""
    out = []
    pos = 0
    for m in HEREDOC_RE.finditer(command):
        if m.start() < pos:
            continue  # inside a heredoc body already removed
        word = m.group(1)
        end_re = re.compile(r"\n[ \t]*" + re.escape(word) + r"[ \t]*(?=\n|$)")
        end = end_re.search(command, m.end())
        if not end:
            # Not a real heredoc (e.g. `1<<3`): keep the text; a spurious check is
            # safer than hiding a later git command.
            continue
        # The rest of the `<<WORD` line itself (e.g. `&& git commit` or `| tee out;
        # git push`) is not part of the heredoc body: only what follows the next
        # newline, up through the terminator line, is.
        next_newline = command.find("\n", m.end())
        if next_newline == -1:
            out.append(command[pos:m.end()])
            pos = end.end()
            continue
        out.append(command[pos:next_newline])
        pos = end.end()
    out.append(command[pos:])
    stripped = "".join(out)
    return _strip_quotes(stripped)


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


def _diff_range(op, all_flag, cwd, deadline=None):
    if op == "commit":
        return _run_git(["diff", "HEAD"] if all_flag else ["diff", "--cached"], cwd, deadline)
    upstream = _run_git(["diff", "@{u}...HEAD"], cwd, deadline)
    if upstream is not None:
        return upstream
    return _run_git(["diff", "origin/main...HEAD"], cwd, deadline)


def _diff_names(op, all_flag, cwd, deadline=None):
    if op == "commit":
        args = (["diff", "HEAD", "--name-only", "-z"] if all_flag
                else ["diff", "--cached", "--name-only", "-z"])
        out = _run_git(args, cwd, deadline)
        return out
    out = _run_git(["diff", "@{u}...HEAD", "--name-only", "-z"], cwd, deadline)
    if out is not None:
        return out
    return _run_git(["diff", "origin/main...HEAD", "--name-only", "-z"], cwd, deadline)


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
    # A `(` subshell opener is also a segment separator, so `(cd sub && git commit)`
    # still finds the `cd sub` segment.
    for segment in re.split(r"&&|\|\||;|\n|\(", prefix):
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
        target = os.path.expanduser(tokens[0])
        result = os.path.join(result, target) if result else target
    return result


def gate(payload, cfg, classify_fn=None, log_fn=None, now=time.time, state_dir=STATE_DIR):
    """Return the PreToolUse hook output dict, or None to leave the command unchanged.

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
    command = _strip_heredocs_and_quotes(raw_command)
    match = GIT_COMMAND_RE.search(command)
    if not match:
        return None
    opts_segment, op = match.group(1), match.group(2)
    cwd = payload.get("cwd") or os.getcwd()
    # The shell resolves -C relative to any directory an earlier cd moved to.
    cd_dir = _cd_prefix_dir(command, match.start())
    if cd_dir:
        cwd = os.path.join(cwd, cd_dir)
    dash_c_dir = _dash_c_dir(opts_segment)
    if dash_c_dir:
        cwd = os.path.join(cwd, os.path.expanduser(dash_c_dir))
    # `git add … && git commit` stages nothing until it runs, so compare the work tree
    # with HEAD instead of the (still empty) index; likewise for commit -a/--all.
    segment = re.split(r"[;&|\n]", command[match.end():], maxsplit=1)[0]
    all_flag = op == "commit" and (bool(ALL_FLAG_RE.search(segment))
                                   or bool(GIT_ADD_RE.search(command[:match.start() + 1])))

    deadline = time.monotonic() + GIT_SUBPROCESS_BUDGET_SECONDS
    diff = _diff_range(op, all_flag, cwd, deadline) or ""
    names_out = _diff_names(op, all_flag, cwd, deadline)
    names_failed = names_out is None
    names = [n for n in (names_out or "").split("\0") if n]
    toplevel = _run_git(["rev-parse", "--show-toplevel"], cwd, deadline)
    toplevel = toplevel.strip() if toplevel else None

    # `git add … && git commit` / commit -a/--all diff the work tree against HEAD,
    # which misses brand-new untracked files; fold those in as synthetic diff blocks.
    untracked_overflow = False
    untracked_identity = []
    if all_flag:
        max_diff_chars = int(cfg.get("max_diff_chars") or 0)
        seen = set(names)
        untracked = _untracked_files(cwd, deadline)
        untracked_overflow = len(untracked) > MAX_UNTRACKED
        for name in untracked[:MAX_UNTRACKED]:
            if name in seen:
                continue
            seen.add(name)
            # Always track the name so its mtime counts for critic coverage, even
            # once the diff text is full.
            names.append(name)
            file_path = Path(toplevel) / name if toplevel else Path(cwd) / name
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
        for name in names:
            path = Path(toplevel) / name if toplevel else Path(name)
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

    def log(decision, choice=None, confidence=None, needs_review_p=None):
        if not log_fn:
            return
        log_fn({"ts": timestamp(), "feature": "risk_gate", "op": op, "decision": decision,
                "risk": choice, "confidence": confidence, "needs_review": needs_review_p,
                "files": len(names), "diff_chars": len(diff), "latency_ms": elapsed_ms(start)})

    if covered:
        log("covered")
        return None

    denied = state.get("denied") or []
    if diff_hash in denied:
        log("override")
        return None

    ask_state = {"operation": op, "files": names[:200]}
    if cfg.get("send_diff"):
        ask_state["diff"] = diff[: cfg["max_diff_chars"]]
    questions = {
        "risk": {"type": "choice", "instructions": RISK_INSTRUCTIONS, "criteria": RISK_CRITERIA},
        "needs_review": {"type": "noul", "instructions": NEEDS_REVIEW_INSTRUCTIONS},
    }
    answers = ask(cfg, "risk_gate", ask_state, questions, classify_fn)
    if answers is None:
        log("allow")
        return None
    try:
        choice = answers["risk"]["choice"]
        confidence = float(answers["risk"].get("confidence"))
    except Exception:
        log("allow")
        return None
    needs_review_p = noul(answers, "needs_review")

    risky = (choice != "none" and needs_review_p is not None
             and needs_review_p >= cfg["risk_min_probability"])
    if not risky:
        log("allow", choice, confidence, needs_review_p)
        return None

    def add_denied(s):
        s["denied"] = ((s.get("denied") or []) + [diff_hash])[-20:]

    update_state(session_state_path(session_id, state_dir), add_denied)
    log("deny", choice, confidence, needs_review_p)
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


def _analyze_report(text, cfg, classify_fn=None, confidence_heuristic=True):
    """Parse RESULT/EVIDENCE/CONFIDENCE/UNVERIFIED sections and ask Jev whether the
    report is weak. Returns (reasons, reason_codes, supported, material_gap).

    `confidence_heuristic` gates the self-reported low/medium CONFIDENCE check: it
    applies to the PostToolUse agent-done nudge (a foreground report can be told to
    verify or escalate more), but not to the SubagentHandback deny path (a read-only
    critic can't escalate, so low/medium confidence alone must not block it).

    Sections are parsed from the FULL text (a long report's RESULT/EVIDENCE/CONFIDENCE/
    UNVERIFIED block may come after `max_report_chars` of findings), so truncation is
    only applied to what's actually sent to the classifier below."""
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
            "result": sections["RESULT"][:quarter],
            "evidence": sections["EVIDENCE"][:quarter],
            "unverified": sections["UNVERIFIED"][:quarter],
            "confidence": sections["CONFIDENCE"][:quarter],
        }
        questions = {
            "supported": {"type": "noul", "instructions": SUPPORTED_INSTRUCTIONS},
            "material_gap": {"type": "noul", "instructions": MATERIAL_GAP_INSTRUCTIONS},
        }
        answers = ask(cfg, "report_check", ask_state, questions, classify_fn)
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
    reasons, reason_codes, supported, material_gap = _analyze_report(
        message, cfg, classify_fn, confidence_heuristic=False)

    def log(decision):
        if not log_fn:
            return
        log_fn({"ts": timestamp(), "feature": "report_check", "event": "handback",
                "decision": decision, "subagent_type": role, "weak": bool(reasons),
                "reasons": reason_codes, "supported": supported, "material_gap": material_gap,
                "latency_ms": elapsed_ms(start)})

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

    update_state(session_state_path(session_id, state_dir), add_handback_denied)
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

            update_state(session_state_path(session_id, state_dir), set_critic_ts)
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

                update_state(session_state_path(session_id, state_dir), add_critic_started)
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

        update_state(session_state_path(session_id, state_dir), set_critic_ts_completed)

    if not feature_enabled(cfg, "report_check"):
        return None
    if subagent_type not in cfg["report_roles"]:
        return None

    text = _extract_report_text(completed)
    start = time.monotonic()
    reasons, reason_codes, supported, material_gap = _analyze_report(text, cfg, classify_fn)

    if log_fn:
        entry = {"ts": timestamp(), "feature": "report_check", "event": "agent_done",
                 "decision": "weak" if reasons else "ok", "subagent_type": subagent_type,
                 "model": tool_input.get("model"), "weak": bool(reasons), "reasons": reason_codes,
                 "supported": supported, "material_gap": material_gap, "latency_ms": elapsed_ms(start)}
        desc_hash = _desc_hash(tool_input.get("description"))
        if desc_hash:
            entry["desc_hash"] = desc_hash
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
        payload = json.loads(sys.stdin.read() or "{}")
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

#!/usr/bin/env python3
"""Pre-push gate: refuse an unreviewed risky push at git's own pre-push hook (ADR 0001).

Git runs it as `pre-push-gate.py <remote-name> <url>` with one stdin line per ref
being pushed: `<local ref> <local sha> <remote ref> <remote sha>`. A deleted ref is
skipped and a local sha that is not a commit is refused. Each line has one base: the
remote sha for an existing ref, or for a new ref the single newest commit where the
pushed history leaves that remote's tracking refs (none when it leaves at two
independent commits or shares no history with them). The files that differ between
the base (or every boundary commit, or the empty tree, without one) and the pushed
commit are the change.

The critic ledger (`<git-common-dir>/coding-orchestrator/critic-ledger.json`, written
by jev-guard.py when an approving async critic run hands back) holds one approval per critic
run, each an exact change: base commit X -> {file: `<mode>:<blob>` or deleted}.
Approvals never combine. A line is covered only when its base is exactly X and its
changed files are exactly that one approval's files, each at the approved entry. A
push changing a subset or a superset of a run's files, or files approved by two
separate runs, needs review, as does anything else (another base, a moved remote, a
merge or rebase after the review, a new ref without a single base). An uncovered line
needs review of all its files. A remote sha that is not a commit here is refused
(fetch first).

No file needing review: exit 0. Otherwise Jev is asked for the risk category exactly
as `risk_gate` does; a risky answer prints the refusal to stderr and exits 1, and the
refusal holds until the ledger covers the files. Jev off or unavailable: a stderr
warning and exit 0 (advisory, as the commit gate is today). A git failure while reading
the pushed range, or a crash, refuses the push. `git push --no-verify` skips the hook.
Installed per repository by `claude/install.py --pre-push-hook <repo>`.
Nothing here logs diff text or file names.
"""
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

GUARD_PATH = Path(__file__).resolve().parent / "jev-guard.py"
ZERO_SHA_RE = re.compile(r"0+")
GIT_TIMEOUT_SECONDS = 20.0
MAX_LISTED_FILES = 20


def load_guard():
    """jev-guard.py, for its risk questions, redaction and ledger (one source of truth)."""
    spec = importlib.util.spec_from_file_location("jev_guard", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(jev, args, cwd):
    """git's stdout (surrogate-escaped UTF-8), or None on failure. Resolved from PATH on
    every call, as in jev-guard, so a git.exe planted in the repository never runs."""
    git = jev.resolve_executable("git")
    if git is None:
        return None
    try:
        # GIT_NO_REPLACE_OBJECTS: a `git replace` ref must not show the gate other content.
        result = subprocess.run([git, *args], cwd=cwd, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS,
                                env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"})
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "surrogateescape")


def _tree_hashes(jev, sha, cwd):
    """{path: "<mode>:<blob>"} for every entry of `sha`'s tree, as the ledger records
    them; a gitlink is "commit:<sha>", which never matches. None on failure."""
    out = _git(jev, ["ls-tree", "-r", "-z", "--full-tree", sha], cwd)
    if out is None:
        return None
    hashes = {}
    for entry in out.split("\0"):
        meta, sep, name = entry.partition("\t")
        fields = meta.split()
        if sep and len(fields) == 3:
            hashes[name] = f"{fields[0] if fields[1] == 'blob' else fields[1]}:{fields[2]}"
    return hashes


class NotACommit(Exception):
    """A pushed local sha that is a tree, a blob or a tag of one, not a commit."""


class RemoteTipMissing(Exception):
    """A non-zero remote sha that is not a commit of this repository (not fetched)."""


def _names(out):
    return [n for n in out.split("\0") if n]


def _base(jev, local_sha, remote_sha, remote, cwd):
    """(base commit or None, the commits or tree to diff the pushed commit against) for
    one ref, () when a new ref pushes nothing the remote lacks, or None on a git failure.
    An existing ref's base is its remote sha. A new ref's is the single newest commit
    where `local_sha`'s history leaves the remote's tracking refs; with two independent
    ones every one is compared and there is no base, and with no shared history the
    empty tree is compared."""
    if remote_sha is not None:
        return remote_sha, [remote_sha]
    remotes = f"--remotes={remote}" if remote else "--remotes"
    out = _git(jev, ["rev-list", "--boundary", local_sha, "--not", remotes], cwd)
    if out is None:
        return None
    words = out.split()
    if not words:
        return ()  # every pushed commit is already on the remote
    boundaries = [word[1:] for word in words if word.startswith("-")]
    if not boundaries:
        empty = _git(jev, ["hash-object", "-t", "tree", "--stdin"], cwd)
        if empty is None or not empty.strip():
            return None
        return None, [empty.strip()]
    newest = _git(jev, ["merge-base", "--independent", *boundaries], cwd)
    if newest is None:
        return None
    newest = [sha.lower() for sha in newest.split()]
    if len(newest) == 1:
        return newest[0], newest
    return None, newest


def pushed_changes(jev, lines, cwd, remote=None):
    """([(base, {name: pushed "<mode>:<blob>" or deleted})] needing a ledger check, one
    entry per ref that changes files, diff text) for the stdin `lines`, or None when a
    range could not be read. `base` is the ref's single base commit (see _base) or None.
    Raises NotACommit for a pushed local sha that is not a commit, RemoteTipMissing for a
    remote sha that is not a commit here. Each ref's files are checked against its own
    pushed tree: two refs pushing one path at different content both count."""
    current = []
    diff = ""
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 4:
            return None
        _, local_sha, _, remote_sha = parts
        if ZERO_SHA_RE.fullmatch(local_sha):
            continue  # a deleted remote ref pushes no content
        if _git(jev, ["cat-file", "-t", local_sha], cwd) is None:
            return None
        commit = _git(jev, ["rev-parse", "--verify", "--quiet", f"{local_sha}^{{commit}}"], cwd)
        if commit is None or not commit.strip():
            raise NotACommit(local_sha)
        local_sha = commit.strip()
        remote_commit = None
        if not ZERO_SHA_RE.fullmatch(remote_sha):
            if _git(jev, ["cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd) is None:
                raise RemoteTipMissing(remote_sha)
            remote_commit = _git(jev, ["rev-parse", "--verify", "--quiet",
                                       f"{remote_sha}^{{commit}}"], cwd)
            if remote_commit is None or not remote_commit.strip():
                raise RemoteTipMissing(remote_sha)
            remote_commit = remote_commit.strip().lower()
        found = _base(jev, local_sha, remote_commit, remote, cwd)
        if found is None:
            return None
        if not found:
            continue
        base, compare = found
        names = []
        for each in compare:
            out = _git(jev, ["diff", *jev.DIFF_FORMAT_ARGS, "--name-only", "-z", "--no-renames",
                             each, local_sha, "--"], cwd)
            if out is None:
                return None
            names += _names(out)
        names = list(dict.fromkeys(names))
        if not names:
            continue
        tree = _tree_hashes(jev, local_sha, cwd)
        text = _git(jev, ["diff", *jev.DIFF_FORMAT_ARGS, "--no-renames", compare[0], local_sha,
                          "--"], cwd)
        if tree is None or text is None:
            return None
        current.append((base, {name: tree.get(name, jev.LEDGER_DELETED) for name in names}))
        diff += text
    return current, diff


def _needs_review(current, ledger):
    """The files of `current` ([(base, {name: pushed entry})], one per ref) no approval
    covers. A ref is covered only when it has a base and one single approval of the
    ledger (a list of {"base", "files"}, one per critic run) has exactly that base and
    exactly the ref's changed files, each at the same "<mode>:<blob>" (or deleted);
    approvals never combine. An uncovered ref needs review of all its files."""
    needs = []
    for base, files in current:
        covered = base is not None and any(
            isinstance(approval, dict) and approval.get("base") == base
            and approval.get("files") == files for approval in ledger)
        if not covered:
            needs += list(files)
    return list(dict.fromkeys(needs))


def run(argv, stdin_text, cfg, jev, classify_fn=None, log_fn=None, cwd=None, err=None):
    """The hook's exit code (0 allow, 1 refuse); messages go to `err` (stderr)."""
    err = err or sys.stderr
    cwd = cwd or str(Path.cwd())
    start = time.monotonic()

    def log(decision, **extra):
        if log_fn:
            log_fn({"ts": jev.timestamp(), "feature": "risk_gate", "hook": "pre-push",
                    "decision": decision, "latency_ms": jev.elapsed_ms(start), **extra})

    try:
        changes = pushed_changes(jev, stdin_text.splitlines(), cwd,
                                 remote=argv[0] if argv else None)
    except RemoteTipMissing as exc:
        log("deny", reason="remote_tip_missing")
        print(f"[pre-push gate] The remote tip {exc.args[0]} is not in this repository: "
              "fetch first, then push again.", file=err)
        return 1
    except NotACommit:
        log("deny", reason="not_a_commit")
        print("[pre-push gate] A pushed ref points at a tree, blob or tag that is not a "
              "commit, which the gate cannot check, so the push is refused. Push a commit "
              "instead.", file=err)
        return 1
    if changes is None:
        log("deny", reason="unjudged_push")
        print("[pre-push gate] Could not read the commits being pushed, so the push was "
              "not checked and is refused. Check the repository state and push again.",
              file=err)
        return 1
    current, diff = changes
    if not current:
        return 0
    path = jev.ledger_path(cwd)
    ledger = jev.load_ledger(path) if path is not None else []
    needs = _needs_review(current, ledger)
    if not needs:
        log("covered", files=len({name for _, files in current for name in files}))
        return 0

    def advisory(reason, problem):
        log("allow", reason=reason, files=len(needs))
        print(f"[pre-push gate] warning: {len(needs)} pushed file(s) have no critic review "
              f"of their current content, and the Jev risk check {problem}; the push "
              "proceeds unchecked.", file=err)
        return 0

    if not jev.feature_enabled(cfg, "risk_gate"):
        return advisory("disabled", "is off")
    ask_state = {"operation": "push", "files": needs[:200]}
    if cfg.get("send_diff"):
        max_diff_chars = jev._char_limit(cfg, "max_diff_chars")
        # Hunks of secret-looking files and common token shapes never leave the machine.
        ask_state["diff"] = jev._scrub(jev._redact_diff(jev._diff_prefix(diff, max_diff_chars)))[
            : max_diff_chars]
    questions = {
        "risk": {"type": "choice", "instructions": jev.RISK_INSTRUCTIONS,
                 "criteria": jev.RISK_CRITERIA},
        "needs_review": {"type": "noul", "instructions": jev.NEEDS_REVIEW_INSTRUCTIONS},
    }
    errs = []
    answers = jev.ask(cfg, "risk_gate", ask_state, questions, classify_fn, errors=errs,
                      deadline=time.monotonic() + jev.GATE_BUDGET_SECONDS)
    if answers is None:
        return advisory("unavailable", "is unavailable")
    try:
        choice = answers["risk"]["choice"]
    except Exception:
        choice = None
    needs_review_p = jev.noul(answers, "needs_review")
    if not isinstance(choice, str) or (choice != "none" and needs_review_p is None):
        return advisory("malformed", "returned an unusable answer")
    # As in the risk_gate hook, an invalid confidence is logged as None and never lowers
    # the bar (see needs_critic).
    risk = answers["risk"]
    confidence = jev.coerce_confidence(risk.get("confidence") if isinstance(risk, dict) else None)
    if not jev.needs_critic(cfg, choice, confidence, needs_review_p):
        log("allow", risk=choice, confidence=confidence, needs_review=needs_review_p,
            files=len(needs))
        return 0
    log("deny", risk=choice, confidence=confidence, needs_review=needs_review_p,
        files=len(needs))
    listed = "\n".join(f"  {name}" for name in needs[:MAX_LISTED_FILES])
    more = len(needs) - MAX_LISTED_FILES
    if more > 0:
        listed += f"\n  ... and {more} more"
    print(f"[pre-push gate] This push looks risky ({choice}, p={needs_review_p:.2f}) and no "
          "critic has reviewed the current content of these files:\n"
          f"{listed}\n"
          "Run the critic subagent on this change in the background, with a REVIEW-SCOPE "
          "line, resolve its findings, then push again.",
          file=err)
    return 1


def main():
    try:
        jev = load_guard()
        stdin_text = sys.stdin.buffer.read().decode("utf-8", "replace")
        cfg = jev.load_config()
        return run(sys.argv[1:], stdin_text, cfg, jev,
                   log_fn=lambda entry: jev.write_log(cfg, entry))
    except Exception as exc:
        # A crashed gate must not wave the push through.
        print(f"[pre-push gate] The gate failed ({type(exc).__name__}), so the push is "
              "refused.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

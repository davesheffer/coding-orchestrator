#!/usr/bin/env python3
"""Pre-push gate: refuse an unreviewed risky push at git's own pre-push hook (ADR 0001).

Git runs it as `pre-push-gate.py <remote-name> <url>` with one stdin line per ref
being pushed: `<local ref> <local sha> <remote ref> <remote sha>`. For each line it
takes the pushed commits (remote sha..local sha, or for a new remote ref the commits
on none of that remote's tracking refs; a deleted ref is skipped), the files they touch
and the files whose content differs between the endpoints. A local sha that is not a
commit is refused. A file needs review when its blob at that ref's pushed commit
differs from the critic ledger entry
(`<git-common-dir>/coding-orchestrator/critic-ledger.json`, written by jev-guard.py
when a critic run finishes) or has none.

No file needing review: exit 0. Otherwise Jev is asked for the risk category exactly
as `risk_gate` does; a risky answer prints the refusal to stderr and exits 1, and the
refusal holds until the ledger covers the files. Jev off or unavailable: a stderr
warning and exit 0 (advisory, as the commit gate is today). A git failure while reading
the pushed range, or a crash, refuses the push. `git push --no-verify` skips the hook.
Installed per repository by `claude/install.py --pre-push-hook <repo>`.
Nothing here logs diff text or file names.
"""
import importlib.util
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
        result = subprocess.run([git, *args], cwd=cwd, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "surrogateescape")


def _tree_hashes(jev, sha, cwd):
    """{path: object id} for every entry of `sha`'s tree; a gitlink is prefixed so it
    never matches a ledger blob hash. None on failure."""
    out = _git(jev, ["ls-tree", "-r", "-z", "--full-tree", sha], cwd)
    if out is None:
        return None
    hashes = {}
    for entry in out.split("\0"):
        meta, sep, name = entry.partition("\t")
        fields = meta.split()
        if sep and len(fields) == 3:
            hashes[name] = fields[2] if fields[1] == "blob" else f"{fields[1]}:{fields[2]}"
    return hashes


class NotACommit(Exception):
    """A pushed local sha that is a tree, a blob or a tag of one, not a commit."""


def _names(out):
    return [n for n in out.split("\0") if n]


def _endpoint_names(jev, local_sha, remote_sha, remote, cwd):
    """Files whose content at `local_sha` differs from the remote side, or None on a git
    failure. Per-commit `--cc` listings miss a merge that equals one parent (an `-s ours`
    merge can revert upstream content unseen), so the endpoints are compared too: against
    `remote_sha`, or for a new ref against every commit the pushed range branches off
    the remote's refs (a file equal to one of them is already published there), or the
    empty tree when it shares no history with them."""
    if remote_sha is not None:
        bases = [remote_sha]
    else:
        remotes = f"--remotes={remote}" if remote else "--remotes"
        out = _git(jev, ["rev-list", "--boundary", local_sha, "--not", remotes], cwd)
        if out is None:
            return None
        words = out.split()
        if not words:
            return []  # every pushed commit is already on the remote
        bases = [word[1:] for word in words if word.startswith("-")]
        if not bases:
            empty = _git(jev, ["hash-object", "-t", "tree", "--stdin"], cwd)
            if empty is None or not empty.strip():
                return None
            bases = [empty.strip()]
    common = None
    for base in bases:
        out = _git(jev, ["diff", *jev.DIFF_FORMAT_ARGS, "--name-only", "-z", "--no-renames",
                         base, local_sha, "--"], cwd)
        if out is None:
            return None
        names = set(_names(out))
        common = names if common is None else common & names
    return sorted(common or ())


def pushed_changes(jev, lines, cwd, remote=None):
    """([(name, pushed hash)] needing a ledger check, one entry per ref and file, diff
    text) for the stdin `lines`, or None when a range could not be read. Raises
    NotACommit for a pushed local sha that is not a commit. Each ref's files are checked
    against its own pushed tree: two refs pushing one path at different content both count."""
    current = []
    diff = ""
    remotes = f"--remotes={remote}" if remote else "--remotes"
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
        known = (not ZERO_SHA_RE.fullmatch(remote_sha)
                 and _git(jev, ["cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd) is not None)
        rev = [f"{remote_sha}..{local_sha}"] if known else [local_sha, "--not", remotes]
        # --cc lists a merge's files only where it differs from every parent (its own
        # conflict resolutions); the endpoint comparison adds what a merge took or dropped.
        names = _git(jev, ["-c", "log.showRoot=true", "log", "--format=", "--name-only", "-z",
                           "--no-renames", "--cc", *rev, "--"], cwd)
        # Merge diffs are left out of the text: their `diff --cc` headers would escape
        # the redaction, which matches `diff --git` headers only.
        text = _git(jev, ["-c", "log.showRoot=true", "log", "--format=", "-p",
                          *jev.DIFF_FORMAT_ARGS, "--no-renames", *rev, "--"], cwd)
        if names is None or text is None:
            return None
        endpoint = _endpoint_names(jev, local_sha, remote_sha if known else None, remote, cwd)
        if endpoint is None:
            return None
        names = list(dict.fromkeys(_names(names) + endpoint))
        if not names:
            continue
        tree = _tree_hashes(jev, local_sha, cwd)
        if tree is None:
            return None
        current += [(name, tree.get(name, jev.LEDGER_DELETED)) for name in names]
        diff += text
    return current, diff


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
    ledger = jev.load_ledger(path) if path is not None else {}
    needs = list(dict.fromkeys(name for name, blob in current if ledger.get(name) != blob))
    if not needs:
        log("covered", files=len({name for name, _ in current}))
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
    if choice == "none" or needs_review_p < cfg["risk_min_probability"]:
        log("allow", risk=choice, needs_review=needs_review_p, files=len(needs))
        return 0
    log("deny", risk=choice, needs_review=needs_review_p, files=len(needs))
    listed = "\n".join(f"  {name}" for name in needs[:MAX_LISTED_FILES])
    more = len(needs) - MAX_LISTED_FILES
    if more > 0:
        listed += f"\n  ... and {more} more"
    print(f"[pre-push gate] This push looks risky ({choice}, p={needs_review_p:.2f}) and no "
          "critic has reviewed the current content of these files:\n"
          f"{listed}\n"
          "Run the critic subagent on this change, resolve its findings, then push again.",
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

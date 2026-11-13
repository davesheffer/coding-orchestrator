import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "pre-push-gate.py"
INSTALL = ROOT / "claude" / "install.py"
spec = importlib.util.spec_from_file_location("pre_push_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
jev = gate.load_guard()
ZERO = "0" * 40
APPROVE = "RESULT: SHIP\nno findings\nEVIDENCE: read the diff\nCONFIDENCE: high\n"


def _unpushed_names(top, deadline):
    """(files changed by commits on no remote-tracking ref, plus those whose content at
    HEAD differs from any commit those commits branch off (a merge equal to one parent
    lists nothing under --cc), the commit that contains every such branch-off point or
    None when there is no single one), or None on a git failure."""
    names = jev._run_git(["-c", "log.showRoot=true", "log", "--format=", "--name-only", "-z",
                          "--no-renames", "--ignore-submodules=dirty", "--cc", "HEAD",
                          "--not", "--remotes", "--"], top, deadline)
    commits = jev._run_git(["rev-list", "--boundary", "HEAD", "--not", "--remotes"], top,
                           deadline)
    if names is None or commits is None:
        return None
    names = [n for n in names.split("\0") if n]
    words = commits.split()
    if not words:
        head = jev._run_git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], top, deadline)
        return names, (head.strip() or None) if head else None
    bases = [word[1:] for word in words if word.startswith("-")]
    base = None
    if bases:
        newest = jev._run_git(["merge-base", "--independent", *bases], top, deadline)
        newest = (newest or "").split()
        base = newest[0] if len(newest) == 1 else None
    else:
        empty = jev._run_git(["hash-object", "-t", "tree", "--stdin"], top, deadline)
        if not empty or not empty.strip():
            return None
        bases = [empty.strip()]
    for each in bases:
        out = jev._run_git(["diff", *jev.DIFF_FORMAT_ARGS, "--name-only", "-z", "--no-renames",
                            each, "HEAD", "--"], top, deadline)
        if out is None:
            return None
        names += [n for n in out.split("\0") if n]
    return names, base


def critic_ledger_files(cwd, deadline=None):
    """(ledger path, {file: current "<mode>:<blob>" or LEDGER_DELETED}, top level, base)
    for every tracked file this repository changed but has not pushed, or None on a
    failure. `base` is the published commit that contains every point the unpushed
    commits branch off (HEAD when nothing is unpushed), or None when there is no single
    one; with a base the files are those `git diff <base>` lists, else those the unpushed
    commits and the work tree against HEAD change. Deletions are git's; untracked files
    are left out; symlinks, gitlinks and files missing on disk are LEDGER_UNHASHABLE."""
    deadline = min(deadline or float("inf"), time.monotonic() + jev.LEDGER_GIT_SECONDS)
    top = jev._run_git(["rev-parse", "--show-toplevel"], cwd, deadline)
    path = jev.ledger_path(cwd, deadline)
    if not top or not top.strip() or path is None:
        return None
    top = top.strip()
    found = _unpushed_names(top, deadline)
    if found is None:
        return None
    names, base = found
    changed = jev._name_status(top, base or "HEAD", deadline)
    if changed is None:
        return None
    names = changed[0] if base else names + changed[0]
    files = jev._hash_worktree_files(top, names, deadline, deleted=changed[1])
    return None if files is None else (path, files, top, base)


def record_unscoped(cwd):
    """Seed the critic ledger with every file the repository at `cwd` changed but has not
    pushed (critic_ledger_files), as one approval; nothing without a single base or when
    a file is unhashable. A test fixture: the hooks record only an async critic's
    REVIEW-SCOPE, after its approving reply, against its launch snapshot."""
    found = critic_ledger_files(cwd)
    if found is None:
        return
    path, files, _top, base = found
    jev._merge_ledger(path, files, base.lower() if isinstance(base, str) else None, None)


def entries(ledger):
    """{file: "<mode>:<blob>" or deleted} across a ledger's [{"base", "ts", "files"}]
    approvals, the newest approval winning."""
    merged = {}
    for approval in sorted(ledger, key=lambda value: value.get("ts", 0)):
        merged.update(approval["files"])
    return merged


def risk_response(choice="security", needs_review=0.9, confidence=0.9):
    return {"model": "jev-1.13.0", "answers": {
        "risk": {"type": "choice", "choice": choice, "confidence": confidence,
                 "probabilities": {choice: confidence}},
        "needs_review": {"type": "noul", "noul": needs_review},
    }, "usage": {}}


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def init_repo(path):
    path.mkdir(parents=True)
    git(["init", "-b", "main"], path)
    git(["config", "user.email", "t@example.com"], path)
    git(["config", "user.name", "Test"], path)
    git(["config", "commit.gpgsign", "false"], path)


class GateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Recording is time-budgeted; a slow or loaded host must not turn these tests
        # into "nothing recorded" (the budget expiring records nothing, by design).
        for name in ("SHORT_HOOK_BUDGET_SECONDS", "LEDGER_GIT_SECONDS"):
            budget = mock.patch.object(jev, name, 120.0)
            budget.start()
            self.addCleanup(budget.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.remote = Path(temp.name) / "remote.git"
        git(["init", "--bare", "-b", "main", str(self.remote)], temp.name)
        self.repo = Path(temp.name) / "repo"
        init_repo(self.repo)
        git(["remote", "add", "origin", str(self.remote)], self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(["add", "a.txt"], self.repo)
        git(["commit", "-m", "init"], self.repo)
        # Publish the base commit so refs/remotes/origin/main exists, without the hook.
        git(["push", "--no-verify", "origin", "main"], self.repo)
        self.base = git(["rev-parse", "HEAD"], self.repo)
        self.calls = []

    def commit(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")
        git(["add", name], self.repo)
        git(["commit", "-m", f"change {name}"], self.repo)
        return git(["rev-parse", "HEAD"], self.repo)

    def classify(self, response=None, error=None):
        def classify_fn(body, key):
            self.calls.append(body)
            if error:
                raise error
            return response or risk_response()
        return classify_fn

    def run_gate(self, lines, classify_fn=None, cfg=None):
        err = io.StringIO()
        code = gate.run(["origin", str(self.remote)], "".join(f"{line}\n" for line in lines),
                        cfg or self.cfg, jev, classify_fn or self.classify(), cwd=str(self.repo),
                        err=err)
        return code, err.getvalue()

    def test_no_change_push_allowed(self):
        code, err = self.run_gate([f"refs/heads/main {self.base} refs/heads/main {self.base}"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.calls, [])

    def test_covered_by_ledger_allowed(self):
        head = self.commit("a.txt", "two\n")
        record_unscoped(str(self.repo))
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.calls, [])
        ledger = jev.load_ledger(jev.ledger_path(str(self.repo)))
        self.assertEqual([approval["base"] for approval in ledger], [self.base])
        self.assertEqual(ledger[0]["files"],
                         {"a.txt": "100644:" + git(["rev-parse", "HEAD:a.txt"], self.repo)})
        data = json.loads(jev.ledger_path(str(self.repo)).read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 3)
        self.assertNotIn("files", data)

    def test_uncovered_risky_push_refused(self):
        head = self.commit("a.txt", "two\n")
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)
        self.assertIn("Run the critic subagent", err)
        self.assertEqual(len(self.calls), 1)
        # No deny-once: the identical retry is refused again.
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])[0], 1)

    def test_edit_after_review_needs_review_again(self):
        self.commit("a.txt", "two\n")
        record_unscoped(str(self.repo))
        head = self.commit("a.txt", "three\n")
        code, _ = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)

    def test_not_risky_allowed(self):
        head = self.commit("a.txt", "two\n")
        code, _ = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"],
                                self.classify(risk_response("none", 0.1)))
        self.assertEqual(code, 0)

    def test_confident_class_needs_less_review_probability(self):
        # Seen live: security at 0.98 with "needs review" 0.56 was pushed unreviewed.
        head = self.commit("a.txt", "two\n")
        line = f"refs/heads/main {head} refs/heads/main {self.base}"
        for confidence, needs_review, code in ((0.98, 0.56, 1), (0.9, 0.5, 1), (0.98, 0.49, 0),
                                               (0.89, 0.56, 0), (0.89, 0.6, 1),
                                               (None, 0.59, 0), ("high", 0.59, 0), (None, 0.6, 1)):
            with self.subTest(confidence=confidence, needs_review=needs_review):
                response = risk_response("security", needs_review, confidence)
                self.assertEqual(self.run_gate([line], self.classify(response))[0], code)

    def test_log_carries_the_confidence_the_rule_used(self):
        head = self.commit("a.txt", "two\n")
        line = f"refs/heads/main {head} refs/heads/main {self.base}"
        for confidence, logged, decision in ((0.98, 0.98, "deny"), ("high", None, "allow")):
            with self.subTest(confidence=confidence):
                logs, err = [], io.StringIO()
                gate.run(["origin", str(self.remote)], f"{line}\n", self.cfg, jev,
                         self.classify(risk_response("security", 0.56, confidence)),
                         log_fn=logs.append, cwd=str(self.repo), err=err)
                self.assertEqual([(e["decision"], e["risk"], e["confidence"], e["needs_review"])
                                  for e in logs], [(decision, "security", logged, 0.56)])

    def test_jev_unavailable_allows_with_warning(self):
        head = self.commit("a.txt", "two\n")
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"],
                                  self.classify(error=TimeoutError()))
        self.assertEqual(code, 0)
        self.assertIn("warning", err)
        self.assertIn("unavailable", err)

    def test_jev_off_allows_with_warning(self):
        head = self.commit("a.txt", "two\n")
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"],
                                  cfg={**self.cfg, "enabled": False})
        self.assertEqual(code, 0)
        self.assertIn("is off", err)
        self.assertEqual(self.calls, [])

    def test_delete_ref_skipped(self):
        code, err = self.run_gate([f"(delete) {ZERO} refs/heads/old {self.base}"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.calls, [])

    def test_new_branch_range_is_commits_on_no_remote(self):
        git(["checkout", "-b", "feature"], self.repo)
        head = self.commit("b.txt", "new\n")
        code, err = self.run_gate([f"refs/heads/feature {head} refs/heads/feature {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("b.txt", err)
        sent = json.dumps(self.calls[0])
        self.assertIn("b.txt", sent)
        self.assertNotIn("a.txt", sent)

    def test_deleted_file_covered_once_reviewed(self):
        (self.repo / "a.txt").unlink()
        git(["commit", "-am", "drop a"], self.repo)
        head = git(["rev-parse", "HEAD"], self.repo)
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])[0], 1)
        record_unscoped(str(self.repo))
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])[0], 0)

    def test_unreadable_range_refused(self):
        code, err = self.run_gate([f"refs/heads/main {'1' * 40} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)
        self.assertIn("Could not read", err)

    def test_secret_file_hunks_redacted(self):
        head = self.commit("prod.env", "TOKEN=hunter2\n")
        self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"],
                      cfg={**self.cfg, "send_diff": True})
        sent = json.dumps(self.calls[0])
        self.assertIn("prod.env", sent)
        self.assertNotIn("hunter2", sent)

    def test_each_ref_checked_against_its_own_tree(self):
        git(["checkout", "-b", "x"], self.repo)
        head_x = self.commit("a.txt", "two\n")
        git(["checkout", "main"], self.repo)
        head_main = self.commit("a.txt", "three\n")
        record_unscoped(str(self.repo))  # reviews a.txt at "three" only
        code, err = self.run_gate([f"refs/heads/x {head_x} refs/heads/x {ZERO}",
                                   f"refs/heads/main {head_main} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def ours_merge(self):
        """main gains an upstream change to a.txt on the remote; a local branch then merges
        it with `-s ours`, silently reverting a.txt. Returns (upstream sha, merge sha)."""
        upstream = self.commit("a.txt", "upstream\n")
        git(["push", "--no-verify", "origin", "main"], self.repo)
        git(["checkout", "-b", "local", self.base], self.repo)
        self.commit("b.txt", "local\n")
        record_unscoped(str(self.repo))  # reviews b.txt only
        git(["merge", "-s", "ours", "-m", "merge main", "main"], self.repo)
        return upstream, git(["rev-parse", "HEAD"], self.repo)

    def test_ours_merge_revert_needs_review(self):
        upstream, merge = self.ours_merge()
        code, err = self.run_gate([f"refs/heads/local {merge} refs/heads/main {upstream}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_recorder_covers_ours_merge_revert(self):
        upstream, merge = self.ours_merge()
        record_unscoped(str(self.repo))
        ledger = entries(jev.load_ledger(jev.ledger_path(str(self.repo))))
        self.assertEqual(ledger.get("a.txt"), "100644:" + git(["rev-parse", "HEAD:a.txt"], self.repo))
        code, err = self.run_gate([f"refs/heads/local {merge} refs/heads/main {upstream}"])
        self.assertEqual((code, err), (0, ""))

    def reviewed_then_upstream_edit(self, delete=False):
        """A local commit changing (or deleting) a.txt is reviewed at base M; origin/main
        then moves to `up`, which edits a.txt differently. Returns (local sha, up sha)."""
        if delete:
            (self.repo / "a.txt").unlink()
            git(["commit", "-am", "drop a"], self.repo)
            local = git(["rev-parse", "HEAD"], self.repo)
        else:
            local = self.commit("a.txt", "v2\n")
        record_unscoped(str(self.repo))
        git(["checkout", "-b", "upstream", self.base], self.repo)
        up = self.commit("a.txt", "v3\n")
        git(["push", "--no-verify", "origin", "upstream:main"], self.repo)
        git(["checkout", "main"], self.repo)
        return local, up

    def assert_refused(self, merge, up):
        code, err = self.run_gate([f"refs/heads/main {merge} refs/heads/main {up}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)
        self.assertEqual(len(self.calls), 1)

    def test_reviewed_diff_not_spanning_upstream_edit_refused(self):
        for delete in (False, True):
            with self.subTest(delete=delete):
                self.setUp()
                local, up = self.reviewed_then_upstream_edit(delete)
                git(["merge", "-s", "ours", "-m", "merge up", "upstream"], self.repo)
                self.assert_refused(git(["rev-parse", "HEAD"], self.repo), up)

    def test_conflict_resolved_with_ours_refused(self):
        local, up = self.reviewed_then_upstream_edit()
        subprocess.run(["git", "merge", "upstream"], cwd=self.repo, capture_output=True)
        git(["checkout", "--ours", "a.txt"], self.repo)
        git(["add", "a.txt"], self.repo)
        git(["commit", "--no-edit"], self.repo)
        self.assert_refused(git(["rev-parse", "HEAD"], self.repo), up)

    def test_forced_update_to_reviewed_commit_refused(self):
        local, up = self.reviewed_then_upstream_edit()
        self.assert_refused(local, up)

    def test_v1_ledger_reads_as_empty(self):
        head = self.commit("a.txt", "two\n")
        entry = "100644:" + git(["rev-parse", "HEAD:a.txt"], self.repo)
        path = jev.ledger_path(str(self.repo))
        path.parent.mkdir(parents=True, exist_ok=True)
        v1 = {"files": {"a.txt": {"entry": entry, "base": self.base,
                                  "from": "100644:" + git(["rev-parse", "HEAD~1:a.txt"],
                                                          self.repo)}}}
        path.write_text(json.dumps(v1), encoding="utf-8")
        self.assertEqual(jev.load_ledger(path), [])
        self.assert_refused(head, self.base)
        # A ledger holding both shapes is still the older format.
        path.write_text(json.dumps({**v1, "approvals": {self.base: {"files": {"a.txt": entry}}}}),
                        encoding="utf-8")
        self.assertEqual(jev.load_ledger(path), [])
        # A v2 ledger (approvals merged per base) reads as [] too, in either shape.
        v2 = {"version": 2, "approvals": {self.base: {"ts": 1.0, "files": {"a.txt": entry}}}}
        for data in (v2, {**v2, "approvals": [{"base": self.base, "files": {"a.txt": entry}}]}):
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(jev.load_ledger(path), [])
            self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main "
                                            f"{self.base}"])[0], 1)
        # Recording over it upgrades it to v3, dropping the old entries.
        path.write_text(json.dumps({**v1, **v2}), encoding="utf-8")
        record_unscoped(str(self.repo))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 3)
        self.assertEqual(len(data["approvals"]), 1)
        self.assertNotIn("files", data)
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"]),
                         (0, ""))

    def test_unknown_remote_tip_refused(self):
        head = self.commit("a.txt", "two\n")
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {'1' * 40}"])
        self.assertEqual(code, 1)
        self.assertIn("is not in this repository: fetch first, then push again", err)
        self.assertEqual(self.calls, [])

    def critic_cfg(self):
        return {**self.cfg, "features": {**self.cfg["features"], "report_check": False}}

    def scope(self, repo=None, base=None, paths=None):
        line = f"REVIEW-SCOPE: {repo or self.repo} base={base or self.base}"
        return f"Review this change.\n{line}" + (f" paths={paths}" if paths else "") + "\n"

    def foreground(self, prompt, report=APPROVE, duration=0, state=None):
        response = {"status": "completed", "agentId": "fg1",
                    "content": [{"type": "text", "text": report}]}
        if duration is not None:
            response["totalDurationMs"] = duration
        payload = {"tool_name": "Agent", "session_id": "s1", "cwd": str(self.repo),
                   "tool_input": {"subagent_type": "critic", "prompt": prompt},
                   "tool_response": response}
        logs = []
        with tempfile.TemporaryDirectory() as fresh:
            jev.agent_done(payload, self.critic_cfg(), log_fn=logs.append,
                           state_dir=Path(state or fresh))
        self.assertNotIn("REVIEW-SCOPE", json.dumps(logs))
        return [entry for entry in logs if entry.get("event") == "critic_ledger"]

    def ledger(self, repo=None):
        return entries(jev.load_ledger(jev.ledger_path(str(repo or self.repo))))

    def test_scoped_critic_records_what_it_was_handed(self):
        self.commit("a.txt", "two\n")
        (self.repo / "c.txt").write_text("untracked\n", encoding="utf-8")
        logs = self.critic(self.scope())
        ledger = self.ledger()
        self.assertEqual(ledger["a.txt"], "100644:" + git(["rev-parse", "HEAD:a.txt"], self.repo))
        self.assertNotIn("c.txt", ledger)  # untracked: not in the critic's `git diff <base>`
        self.assertEqual([e["decision"] for e in logs], ["recorded"])
        head = git(["rev-parse", "HEAD"], self.repo)
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])[0], 0)

    def test_scope_in_another_repo_records_that_repo_only(self):
        self.commit("a.txt", "two\n")
        other = self.repo.parent / "other"
        init_repo(other)
        (other / "o.txt").write_text("one\n", encoding="utf-8")
        git(["add", "o.txt"], other)
        git(["commit", "-m", "init"], other)
        git(["update-ref", "refs/remotes/origin/main", "HEAD"], other)  # the base is published
        (other / "o.txt").write_text("two\n", encoding="utf-8")
        self.critic(self.scope(repo=other, base=git(["rev-parse", "HEAD"], other)))
        self.assertEqual(self.ledger(other), {"o.txt": "100644:" + git(["hash-object", "o.txt"], other)})
        self.assertEqual(self.ledger(), {})

    def test_no_or_bad_scope_records_nothing(self):
        self.commit("a.txt", "two\n")
        cases = [("Review this change.\n", "no_scope"),
                 ("REVIEW-SCOPE: relative/path base=HEAD\n", "bad_scope"),
                 (f"REVIEW-SCOPE: {self.repo}\n", "bad_scope"),
                 (self.scope() + self.scope(), "bad_scope"),
                 (self.scope(repo=self.repo.parent / "missing"), "not_a_repo"),
                 (self.scope(base="no-such-ref"), "bad_scope"),
                 (self.scope(base="-" + "1" * 39), "bad_scope"),
                 (self.scope(base="1" * 40), "bad_base"),
                 (self.scope(base=git(["rev-parse", "HEAD^{tree}"], self.repo)), "bad_base")]
        for prompt, reason in cases:
            with self.subTest(reason=reason):
                logs = self.critic(prompt)
                # The launch skips on the scope; the hand-back then has no snapshot either.
                self.assertEqual([(e["decision"], e["reason"]) for e in logs],
                                 [("skipped", reason), ("skipped", "no_launch_snapshot")])
        self.assertEqual(self.ledger(), {})

    def test_blocking_or_missing_verdict_records_nothing(self):
        self.commit("a.txt", "two\n")
        for report in ("RESULT: BLOCKING, two findings\n",
                       "Fix first: the tag check\nRESULT: see above\n",
                       "RESULT: fix first\n", "RESULT: blocked\n",
                       "Looks fine to me.\n"):
            with self.subTest(report=report):
                logs = self.critic(self.scope(), report=report)
                self.assertEqual([e.get("reason") for e in logs], ["blocking_or_no_result"])
        self.assertEqual(self.ledger(), {})

    def test_paths_narrow_the_scope(self):
        self.commit("a.txt", "two\n")
        (self.repo / "sub").mkdir()
        self.commit("sub/b.txt", "new\n")
        self.commit("d.txt", "new\n")
        self.critic(self.scope(paths="sub,d.txt"))
        self.assertEqual(sorted(self.ledger()), ["d.txt", "sub/b.txt"])

    def test_scope_with_no_changed_files_logs_no_files(self):
        self.commit("a.txt", "two\n")
        logs = self.critic(self.scope(paths="unchanged.txt"))
        self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                         [("skipped", "no_files")])
        self.assertEqual(self.ledger(), {})

    def run_async_critic(self, during=None, prompt=None, report=APPROVE):
        """Launch an async critic, run `during()` while it runs, hand back `report`;
        returns (critic_ledger log entries of both events, saved session state)."""
        launch = {"tool_name": "Agent", "session_id": "s1", "cwd": str(self.repo),
                  "tool_input": {"subagent_type": "critic", "prompt": prompt or self.scope()},
                  "tool_response": {"status": "async_launched", "agentId": "ag1"}}
        handback = {"tool_name": "SubagentHandback", "session_id": "s1", "cwd": str(self.repo),
                    "agent_type": "critic", "agent_id": "ag1",
                    "tool_input": {"message": report}}
        logs = []
        with tempfile.TemporaryDirectory() as state:
            jev.agent_done(launch, self.critic_cfg(), log_fn=logs.append, state_dir=Path(state))
            if during:
                during()
            jev.agent_done(handback, self.critic_cfg(), log_fn=logs.append,
                           state_dir=Path(state))
            saved = jev.load_session_state("s1", Path(state))
        self.assertNotIn("REVIEW-SCOPE", json.dumps(logs))
        return [e for e in logs if e.get("event") == "critic_ledger"], saved

    def critic(self, prompt, report=APPROVE, during=None):
        """An async critic run handed `prompt`; returns its critic_ledger log entries."""
        return self.run_async_critic(during, prompt, report)[0]

    def test_async_critic_unchanged_since_launch_records_all(self):
        self.commit("a.txt", "two\n")
        self.stage("b.txt", "staged\n")
        logs, saved = self.run_async_critic()
        self.assertEqual([e["decision"] for e in logs], ["recorded"])
        self.assertEqual(sorted(self.ledger()), ["a.txt", "b.txt"])
        self.assertEqual(saved.get("critic_snapshots"), {})

    def test_async_critic_records_nothing_when_a_file_changed_during_the_run(self):
        def edit():
            (self.repo / "a.txt").write_text("edited while the critic ran\n", encoding="utf-8")

        def revert():  # a.txt back at its base content: it leaves `git diff <base>`
            (self.repo / "a.txt").write_text("one\n", encoding="utf-8")

        def add():  # a new tracked change appears in `git diff <base>`
            self.stage("e.txt", "added while the critic ran\n")

        for during in (edit, revert, add):
            with self.subTest(during=during.__name__):
                git(["reset", "-q", "--hard", self.base], self.repo)
                git(["clean", "-qfd"], self.repo)
                self.stage("a.txt", "two\n")
                self.stage("b.txt", "staged\n")
                ledger = jev.ledger_path(str(self.repo))
                if ledger.exists():
                    ledger.unlink()
                logs, saved = self.run_async_critic(during)
                self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                                 [("skipped", "changed_during_run")])
                self.assertEqual(self.ledger(), {})
                self.assertEqual(saved.get("critic_snapshots"), {})
                # The untouched remainder alone is not covered either.
                git(["reset", "-q"], self.repo)
                git(["add", "b.txt"], self.repo)
                git(["commit", "-q", "-m", "b only"], self.repo)
                self.assertEqual(self.run_gate(self.push_line(self.head(), self.base))[0], 1)

    def test_async_critic_blocking_handback_records_nothing(self):
        self.commit("a.txt", "two\n")
        launch = {"tool_name": "Agent", "session_id": "s1", "cwd": str(self.repo),
                  "tool_input": {"subagent_type": "critic", "prompt": self.scope()},
                  "tool_response": {"status": "async_launched", "agentId": "ag1"}}
        handback = {"tool_name": "SubagentHandback", "session_id": "s1", "cwd": str(self.repo),
                    "agent_type": "critic", "agent_id": "ag1",
                    "tool_input": {"message": "RESULT: FIX FIRST, the merge check\n"}}
        with tempfile.TemporaryDirectory() as state:
            jev.agent_done(launch, self.critic_cfg(), state_dir=Path(state))
            jev.agent_done(handback, self.critic_cfg(), state_dir=Path(state))
        self.assertEqual(self.ledger(), {})

    def test_foreground_critic_never_records(self):
        # No launch event, so no snapshot: what it reviewed cannot be told from what
        # changed during its run (a file restored, staged or unstaged meanwhile).
        head = self.commit("a.txt", "two\n")
        stub = ('This agent\'s report was delivered to you as a message from "fg1" (its '
                'SubagentHandback call). Read it there; it is not repeated here.')
        for case in ("approve", "approve_no_duration", "handback_approve", "blocking"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as state:
                report = {"handback_approve": stub, "blocking": "RESULT: block\n"}.get(case,
                                                                                    APPROVE)
                if case == "handback_approve":
                    jev.agent_done({"tool_name": "SubagentHandback", "session_id": "s1",
                                    "cwd": str(self.repo), "agent_type": "critic",
                                    "agent_id": "fg1", "tool_input": {"message": APPROVE}},
                                   self.critic_cfg(), state_dir=Path(state))
                logs = self.foreground(self.scope(), report=report, state=state,
                                       duration=None if case == "approve_no_duration" else 0)
                self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                                 [("skipped", "no_launch_snapshot")])
                self.assertEqual(self.ledger(), {})
                self.assertEqual(self.run_gate(self.push_line(head, self.base))[0], 1)
        # Skipped before any scope work: even a missing scope logs only this reason.
        self.assertEqual([e.get("reason") for e in self.foreground("no scope\n")],
                         ["no_launch_snapshot"])

    def test_async_critic_records_nothing_when_the_diff_set_changed_during_the_run(self):
        # The foreground round-C gaps, for an async critic: a file restored to HEAD,
        # an old untracked file staged, or a staged file unstaged while it ran.
        def restore():
            git(["checkout", "HEAD", "--", "b.txt"], self.repo)

        def stage_old():
            git(["add", "old.txt"], self.repo)

        def unstage():
            git(["rm", "-q", "--cached", "n.txt"], self.repo)

        for during in (restore, stage_old, unstage):
            with self.subTest(during=during.__name__):
                git(["reset", "-q", "--hard", self.base], self.repo)
                git(["clean", "-qfd"], self.repo)
                ledger = jev.ledger_path(str(self.repo))
                if ledger.exists():
                    ledger.unlink()
                head = self.commit("a.txt", "two\n")
                self.commit("b.txt", "committed\n")
                (self.repo / "b.txt").write_text("edited\n", encoding="utf-8")
                (self.repo / "old.txt").write_text("untracked\n", encoding="utf-8")
                self.stage("n.txt", "new\n")
                logs = self.critic(self.scope(), during=during)
                self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                                 [("skipped", "changed_during_run")])
                self.assertEqual(self.ledger(), {})
                self.assertEqual(self.run_gate(self.push_line(head, self.base))[0], 1)

    def test_verdict_rule_table(self):
        body = "\n[minor] a.txt:1 - nit - scenario - fix\nEVIDENCE: read it\nCONFIDENCE: high\n"
        cases = [
            ("RESULT: SHIP" + body, True),
            ("result: ship" + body, True),
            ("RESULT: approve-with-nits" + body, True),
            ("RESULT:SHIP." + body, True),
            ("\n\n  RESULT: SHIP  \nThe change is fine as it is.\n", True),
            ("RESULT: SHIP - no blockers" + body, False),
            ("SHIP" + body, False),
            ("findings first\nRESULT: SHIP" + body, False),
            ("RESULT: block" + body, False),
            ("RESULT: RETHINK" + body, False),
            ("RESULT: approve" + body, False),
            ("SHIP provided the leak is fixed" + body, False),
            ("", False),
            ("\n  \n", False),
        ]
        for line in ("1. RESULT: block", "| RESULT: block |", "> RESULT: SHIP",
                     "Final RESULT: block", "RESULT - block", " RESULT: block",
                     "**RESULT: SHIP**", "`RESULT: block`", "_RESULT:_ block", "* RESULT: SHIP"):
            cases.append((f"RESULT: SHIP{body}{line}\n", False))
            cases.append((f"{line}{body}", False))
        cases.append((f"RESULT: SHIP{body}RESULT: SHIP\n", False))  # a second header
        for line in ("Result: block", "1. result: block", "> Result - block",
                     "VERDICT: block", "Verdict: block"):
            cases.append((f"RESULT: SHIP{body}{line}\n", False))  # lower case, line start
        for line in ("final verdict: block", "Our Verdict: block", "the verdict - block",
                     "so the vErDiCt is:  block"):
            cases.append((f"RESULT: SHIP{body}{line}\n", False))  # verdict, any case, anywhere
        cases.append(("RESULT: SHIP\nfinal verdict: block", False))
        for line in ("Ran 245 tests, result: OK", "The result of the probe: fine",
                     "The overall verdict is fine.", "verdicts are in", "oververdict: x"):
            cases.append((f"RESULT: SHIP{body}{line}\n", True))  # prose, not a header
        for report, approves in cases:
            with self.subTest(report=report):
                self.assertIs(jev.critic_verdict_approves(report), approves)
        for report in (None, 1, ["RESULT: SHIP"], b"RESULT: SHIP"):
            with self.subTest(report=report):
                self.assertIs(jev.critic_verdict_approves(report), False)

    def test_report_content_items_do_not_fuse(self):
        response = {"content": [{"type": "text", "text": "RESULT: SHIP"},
                                {"type": "text", "text": "EVIDENCE: x"}]}
        self.assertEqual(jev._extract_report_text(response), "RESULT: SHIP\nEVIDENCE: x")

    def test_published_base_check_failure_is_git_failed(self):
        real = jev._run_git

        def failing(args, *rest, **kw):
            if args[:1] == ["for-each-ref"] or args[:2] == ["merge-base", "--is-ancestor"]:
                return None
            return real(args, *rest, **kw)

        scope = {"path": str(self.repo), "base": self.base, "paths": None}
        with mock.patch.object(jev, "_run_git", side_effect=failing):
            self.assertEqual(jev.resolve_review_scope(scope), "git_failed")
        calls = []

        def counting(args, *rest, **kw):
            calls.append(args[:1])
            return real(args, *rest, **kw)

        git(["update-ref", "refs/remotes/origin/other", self.base], self.repo)
        with mock.patch.object(jev, "_run_git", side_effect=counting):
            resolved = jev.resolve_review_scope(scope)
        self.assertEqual(resolved["base"], self.base)
        self.assertEqual(calls.count(["for-each-ref"]), 1)
        self.assertEqual(calls.count(["merge-base"]), 0)

    def test_net_zero_edit_in_range_allowed(self):
        self.commit("a.txt", "two\n")
        head = self.commit("a.txt", "one\n")  # back to the published content
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.calls, [])
        self.assertNotIn("a.txt", self.ledger())

    def test_added_then_deleted_file_allowed(self):
        git(["checkout", "-b", "feature"], self.repo)
        self.commit("tmp.txt", "scratch\n")
        git(["rm", "-q", "tmp.txt"], self.repo)
        git(["commit", "-m", "drop tmp"], self.repo)
        head = git(["rev-parse", "HEAD"], self.repo)
        for remote_sha in (self.base, ZERO):
            with self.subTest(remote_sha=remote_sha):
                code, err = self.run_gate([f"refs/heads/feature {head} refs/heads/feature "
                                           f"{remote_sha}"])
                self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.calls, [])

    def test_async_critic_records_deletion(self):
        git(["rm", "-q", "a.txt"], self.repo)
        git(["commit", "-m", "drop a"], self.repo)
        head = git(["rev-parse", "HEAD"], self.repo)
        logs = self.critic(self.scope())
        self.assertEqual([e["decision"] for e in logs], ["recorded"])
        self.assertEqual(self.ledger(), {"a.txt": jev.LEDGER_DELETED})
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"]),
                         (0, ""))

    def test_async_critic_records_nothing_on_deletion_during_run(self):
        (self.repo / "sub").mkdir()
        self.commit("sub/b.txt", "new\n")
        git(["push", "--no-verify", "origin", "main"], self.repo)
        published = git(["rev-parse", "HEAD"], self.repo)
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")

        def delete():
            (self.repo / "sub" / "b.txt").unlink()
            os.rmdir(self.repo / "sub")

        logs = self.critic(self.scope(base=published), during=delete)
        self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                         [("skipped", "changed_during_run")])
        self.assertEqual(self.ledger(), {})

    def test_dirty_submodule_not_in_commit_names(self):
        sub = self.repo / "sub"
        init_repo(sub)
        (sub / "s.txt").write_text("s\n", encoding="utf-8")
        git(["add", "s.txt"], sub)
        git(["commit", "-m", "sub init"], sub)
        git(["add", "sub"], self.repo)
        git(["commit", "-m", "add sub"], self.repo)
        (sub / "untracked.txt").write_text("only untracked\n", encoding="utf-8")
        (self.repo / "a.txt").write_text("staged\n", encoding="utf-8")
        git(["add", "a.txt"], self.repo)
        for all_flag in (True, False):
            with self.subTest(all_flag=all_flag):
                names = jev._diff_names("commit", all_flag, str(self.repo),
                                        time.monotonic() + 10)
                self.assertEqual([n for n in names.split("\0") if n], ["a.txt"])

    def test_index_symlink_never_recorded(self):
        (self.repo / "link").write_text("a.txt", encoding="utf-8")  # core.symlinks=false
        blob = git(["hash-object", "-w", "link"], self.repo)
        git(["update-index", "--add", "--cacheinfo", f"120000,{blob},link"], self.repo)
        files = jev._hash_worktree_files(str(self.repo), ["link", "a.txt"],
                                         time.monotonic() + 10)
        self.assertEqual(files["link"], jev.LEDGER_UNHASHABLE)
        self.assertNotEqual(files["a.txt"], jev.LEDGER_UNHASHABLE)
        # A run holding the symlink records nothing, so a push without it is not covered.
        self.stage("a.txt", "two\n")
        logs = self.critic(self.scope())
        self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                         [("skipped", "unhashable")])
        self.assertEqual(self.ledger(), {})
        # Not `git rm --cached`: with core.symlinks=true (POSIX) the plain file `link`
        # differs from its 120000 index entry, so git refuses to drop it.
        git(["update-index", "--force-remove", "link"], self.repo)
        git(["commit", "-q", "-m", "a only"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), self.base))[0], 1)

    def test_quoted_name_never_hashed_via_stdin_paths(self):
        (self.repo / "x.py").write_text("other\n", encoding="utf-8")
        files = jev._hash_worktree_files(str(self.repo), ['"x.py"'], time.monotonic() + 10)
        self.assertEqual(files, {'"x.py"': jev.LEDGER_UNHASHABLE})
        self.assertEqual(jev._merge_ledger(jev.ledger_path(str(self.repo)), files, self.base,
                                           time.monotonic() + 10), 0)

    def test_file_mode_false_takes_index_mode(self):
        (self.repo / "run.bat").write_text("echo\n", encoding="utf-8")
        git(["add", "run.bat"], self.repo)
        os.chmod(self.repo / "run.bat", 0o755)
        # Executable on disk (Windows: by its .bat extension), 100644 in the index.
        self.assertTrue((self.repo / "run.bat").stat().st_mode & 0o111)
        for file_mode, expected in (("true", "100755"), ("false", "100644")):
            with self.subTest(file_mode=file_mode), \
                    mock.patch.object(jev, "FILE_SYSTEM_EXEC_BIT", True):
                git(["config", "core.fileMode", file_mode], self.repo)
                files = jev._hash_worktree_files(str(self.repo), ["run.bat"],
                                                 time.monotonic() + 10)
                self.assertEqual(files["run.bat"].split(":")[0], expected)

    def test_rethink_verdict_records_nothing(self):
        self.commit("a.txt", "bad design\n")
        logs = self.critic(self.scope(), report="[major] a.txt:1 - wrong approach\n\n"
                               "RESULT: RETHINK - the design is wrong\nEVIDENCE: x\n")
        self.assertEqual([e.get("reason") for e in logs], ["blocking_or_no_result"])
        self.assertEqual(self.ledger(), {})

    def test_unpublished_base_records_nothing(self):
        mid = self.commit("a.txt", "unreviewed big change\nline2\n")
        self.commit("a.txt", "unreviewed big change\nline2 tweaked\n")
        logs = self.critic(self.scope(base=mid))
        self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                         [("skipped", "unpublished_base"), ("skipped", "no_launch_snapshot")])
        self.assertEqual(self.ledger(), {})

    def test_gitlink_bump_seen_despite_ignore_submodules(self):
        git(["update-index", "--add", "--cacheinfo", f"160000,{self.base},sub"], self.repo)
        git(["commit", "-m", "add gitlink"], self.repo)
        git(["push", "--no-verify", "origin", "main"], self.repo)
        published = git(["rev-parse", "HEAD"], self.repo)
        git(["update-index", "--cacheinfo", f"160000,{'1' * 40},sub"], self.repo)
        git(["commit", "-m", "bump gitlink"], self.repo)
        line = [f"refs/heads/main {git(['rev-parse', 'HEAD'], self.repo)} refs/heads/main "
                f"{published}"]
        git(["config", "diff.ignoreSubmodules", "all"], self.repo)
        code, err = self.run_gate(line)
        self.assertEqual(code, 1)
        self.assertIn("sub", err)
        git(["config", "--unset", "diff.ignoreSubmodules"], self.repo)
        (self.repo / ".gitmodules").write_text(
            '[submodule "sub"]\n\tpath = sub\n\turl = ./x\n\tignore = all\n', encoding="utf-8")
        code, err = self.run_gate(line)
        self.assertEqual(code, 1)
        self.assertIn("sub", err)

    def test_mode_change_needs_review(self):
        self.commit("x.sh", "../../outside/target")
        record_unscoped(str(self.repo))
        git(["push", "--no-verify", "origin", "main"], self.repo)
        published = git(["rev-parse", "HEAD"], self.repo)
        blob = git(["rev-parse", "HEAD:x.sh"], self.repo)
        for mode in ("120000", "100755"):
            with self.subTest(mode=mode):
                git(["update-index", "--cacheinfo", f"{mode},{blob},x.sh"], self.repo)
                git(["commit", "-m", f"mode {mode}"], self.repo)
                head = git(["rev-parse", "HEAD"], self.repo)
                code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {published}"])
                self.assertEqual(code, 1)
                self.assertIn("x.sh", err)

    def test_ledger_entry_without_base_not_honoured(self):
        head = self.commit("a.txt", "two\n")
        blob = git(["rev-parse", "HEAD:a.txt"], self.repo)
        path = jev.ledger_path(str(self.repo))
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = f"100644:{blob}"
        for approvals in ([{"files": {"a.txt": entry}}], [{"base": self.base, "files": [entry]}],
                          [{"base": self.base[:12], "files": {"a.txt": entry}}],
                          [{"base": "origin/main", "files": {"a.txt": entry}}],
                          [{"base": self.base, "files": {"a.txt": blob}}],
                          [{"base": self.base, "files": {"a.txt": {"entry": entry}}}],
                          {self.base: {"files": {"a.txt": entry}}}, [self.base]):
            with self.subTest(approvals=approvals):
                path.write_text(json.dumps({"version": 3, "approvals": approvals}),
                                encoding="utf-8")
                code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
                self.assertEqual(code, 1)
                self.assertIn("a.txt", err)

    def test_review_base_on_another_ref_not_honoured(self):
        # origin/main is at the base; an unreviewed big change sits on origin/wip only,
        # and the critic was handed just the tweak after it.
        big = self.commit("a.txt", "unreviewed big change\nline2\n")
        git(["update-ref", "refs/remotes/origin/wip", big], self.repo)
        head = self.commit("a.txt", "unreviewed big change\nline2 tweaked\n")
        self.assertEqual([e["decision"] for e in self.critic(self.scope(base=big))],
                         ["recorded"])
        self.assertIn("a.txt", self.ledger())
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)
        # Once big is on the pushed ref's remote side, the same entry covers the push.
        git(["push", "--no-verify", "origin", f"{big}:refs/heads/main"], self.repo)
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {big}"]),
                         (0, ""))

    def test_new_ref_review_base_on_that_remote_only(self):
        git(["checkout", "-b", "feature"], self.repo)
        head = self.commit("b.txt", "new\n")
        self.critic(self.scope())
        line = [f"refs/heads/feature {head} refs/heads/feature {ZERO}"]
        self.assertEqual(self.run_gate(line), (0, ""))
        mid = self.commit("a.txt", "unreviewed big change\n")
        git(["update-ref", "refs/remotes/other/feature", mid], self.repo)
        head = self.commit("a.txt", "unreviewed big change, tweaked\n")
        self.critic(self.scope(base=mid))
        self.assertIn("a.txt", self.ledger())
        code, err = self.run_gate([f"refs/heads/feature {head} refs/heads/feature {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)
        self.assertIn("b.txt", err)  # an uncovered ref needs review of all its files

    # The exact rule: an approval covers only "remote commit X -> these entries".

    def head(self):
        return git(["rev-parse", "HEAD"], self.repo)

    def push_line(self, local, remote_sha, ref="main"):
        return [f"refs/heads/{ref} {local} refs/heads/{ref} {remote_sha}"]

    def approve(self, base):
        """An approving async critic run with REVIEW-SCOPE base=`base`."""
        self.assertEqual([e["decision"] for e in self.critic(self.scope(base=base))],
                         ["recorded"])

    def publish_upstream(self, edit, start=None):
        """Move origin/main to a commit on `start` (default the base) made by `edit(repo)`;
        returns its sha and leaves main checked out."""
        git(["checkout", "-q", "-B", "upstream", start or self.base], self.repo)
        edit(self.repo)
        git(["add", "-A"], self.repo)
        git(["commit", "-m", "upstream"], self.repo)
        up = self.head()
        git(["push", "--no-verify", "-f", "origin", "upstream:main"], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        return up

    def test_exact_normal_push_covered(self):
        head = self.commit("a.txt", "v2\n")
        self.approve(self.base)
        self.assertEqual(self.run_gate(self.push_line(head, self.base)), (0, ""))

    def test_exact_review_before_commit_covered(self):
        (self.repo / "a.txt").write_text("v2\n", encoding="utf-8")
        self.approve(self.base)
        head = self.commit("a.txt", "v2\n")
        self.assertEqual(self.run_gate(self.push_line(head, self.base)), (0, ""))

    def test_exact_new_branch_covered(self):
        git(["checkout", "-b", "feature"], self.repo)
        head = self.commit("b.txt", "new\n")
        self.approve(self.base)
        self.assertEqual(self.run_gate(self.push_line(head, ZERO, "feature")), (0, ""))
        self.assertEqual(self.calls, [])

    def test_incremental_push_needs_approval_at_new_remote_tip(self):
        c1 = self.commit("a.txt", "v2\n")
        self.approve(self.base)
        git(["push", "--no-verify", "origin", "main"], self.repo)
        head = self.commit("b.txt", "new\n")
        self.approve(self.base)  # the whole series, against the old base
        self.assertEqual(self.run_gate(self.push_line(head, c1))[0], 1)
        self.approve(c1)
        self.assertEqual(self.run_gate(self.push_line(head, c1)), (0, ""))

    def test_merge_or_rebase_onto_moved_main_needs_review(self):
        for how in ("merge", "rebase"):
            with self.subTest(how=how):
                self.setUp()
                self.commit("b.txt", "local\n")
                self.approve(self.base)
                up = self.publish_upstream(
                    lambda repo: (repo / "c.txt").write_text("up\n", encoding="utf-8"))
                git([how, "-q", "upstream"] if how == "rebase" else
                    ["merge", "-q", "-m", "merge", "upstream"], self.repo)
                head = self.head()
                code, err = self.run_gate(self.push_line(head, up))
                self.assertEqual(code, 1)
                self.assertIn("b.txt", err)
                self.approve(up)
                self.assertEqual(self.run_gate(self.push_line(head, up)), (0, ""))

    def test_s1a_upstream_edit_after_review(self):
        local = self.commit("a.txt", "v2\n")
        self.approve(self.base)
        up = self.publish_upstream(
            lambda repo: (repo / "a.txt").write_text("v3\n", encoding="utf-8"))
        git(["merge", "-q", "-s", "ours", "-m", "merge up", "upstream"], self.repo)
        merge = self.head()
        self.assertEqual(self.run_gate(self.push_line(merge, up))[0], 1)
        self.assertEqual(self.run_gate(self.push_line(local, up))[0], 1)  # forced update
        self.approve(self.base)  # re-reviewed, but against the old base
        self.assertEqual(self.run_gate(self.push_line(merge, up))[0], 1)
        self.approve(up)
        self.assertEqual(self.run_gate(self.push_line(merge, up)), (0, ""))

    def test_s1b_upstream_delete_then_forced_update(self):
        local = self.commit("a.txt", "v2\n")
        self.approve(self.base)
        up = self.publish_upstream(lambda repo: (repo / "a.txt").unlink())
        code, err = self.run_gate(self.push_line(local, up))
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_s1c_upstream_adds_same_path_then_forced_update(self):
        local = self.commit("n.txt", "mine\n")
        self.approve(self.base)
        up = self.publish_upstream(
            lambda repo: (repo / "n.txt").write_text("theirs\n", encoding="utf-8"))
        code, err = self.run_gate(self.push_line(local, up))
        self.assertEqual(code, 1)
        self.assertIn("n.txt", err)

    def test_s1d_reviewed_deletion(self):
        git(["rm", "-q", "a.txt"], self.repo)
        git(["commit", "-m", "drop a"], self.repo)
        local = self.head()
        old = time.time() - 1000
        os.utime(self.repo, (old, old))
        self.approve(self.base)
        self.assertEqual(self.run_gate(self.push_line(local, self.base)), (0, ""))
        up = self.publish_upstream(
            lambda repo: (repo / "a.txt").write_text("v3\n", encoding="utf-8"))
        self.assertEqual(self.run_gate(self.push_line(local, up))[0], 1)
        git(["merge", "-q", "-s", "ours", "-m", "merge up", "upstream"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), up))[0], 1)

    def s3(self):
        """a.txt v1 -> v2 reviewed at M and published as M1; upstream reverts a.txt to v1
        (M2); locally c.txt is added on M1 and reviewed with base M1. Returns (M2, tip)."""
        m1 = self.commit("a.txt", "v2\n")
        self.approve(self.base)
        git(["push", "--no-verify", "origin", "main"], self.repo)
        m2 = self.publish_upstream(
            lambda repo: (repo / "a.txt").write_text("one\n", encoding="utf-8"), start=m1)
        tip = self.commit("c.txt", "local\n")
        self.approve(m1)
        return m2, tip

    def test_s3_old_approval_not_replayed_by_forced_update(self):
        m2, tip = self.s3()
        code, err = self.run_gate(self.push_line(tip, m2))
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_s3b_old_approval_not_replayed_by_ours_merge(self):
        m2, _ = self.s3()
        git(["merge", "-q", "-s", "ours", "-m", "merge up", "upstream"], self.repo)
        code, err = self.run_gate(self.push_line(self.head(), m2))
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_s4_symbolic_or_short_base_records_nothing(self):
        self.commit("a.txt", "v2\n")
        for base in ("origin/main", self.base[:12], "HEAD", self.base.upper()[:39]):
            with self.subTest(base=base):
                logs = self.critic(self.scope(base=base))
                self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                                 [("skipped", "bad_scope"), ("skipped", "no_launch_snapshot")])
        self.assertEqual(jev.load_ledger(jev.ledger_path(str(self.repo))), [])

    def test_upper_case_full_base_stored_lower_case(self):
        head = self.commit("a.txt", "v2\n")
        self.approve(self.base.upper())
        self.assertEqual([approval["base"] for approval in
                          jev.load_ledger(jev.ledger_path(str(self.repo)))], [self.base])
        self.assertEqual(self.run_gate(self.push_line(head, self.base)), (0, ""))

    def test_remote_tip_tree_or_tree_tag_refused(self):
        head = self.commit("a.txt", "v2\n")
        tree = git(["rev-parse", "HEAD^{tree}"], self.repo)
        git(["tag", "-a", "-m", "tree tag", "treetag", tree], self.repo)
        for remote_sha in (tree, git(["rev-parse", "treetag"], self.repo)):
            with self.subTest(remote_sha=remote_sha):
                code, err = self.run_gate(self.push_line(head, remote_sha))
                self.assertEqual(code, 1)
                self.assertIn("fetch first", err)
        self.assertEqual(self.calls, [])

    def test_new_ref_off_two_independent_remote_commits_uncovered(self):
        for name in ("x", "y"):
            git(["checkout", "-q", "-b", name, self.base], self.repo)
            self.commit(f"{name}.txt", f"{name}\n")
            git(["push", "--no-verify", "origin", name], self.repo)
        x = git(["rev-parse", "x"], self.repo)
        git(["merge", "-q", "-m", "merge x", "x"], self.repo)
        head = self.commit("z.txt", "z\n")
        self.approve(x)
        code, err = self.run_gate(self.push_line(head, ZERO, "both"))
        self.assertEqual(code, 1)
        self.assertIn("z.txt", err)

    def test_orphan_new_ref_never_covered(self):
        git(["checkout", "-q", "--orphan", "solo"], self.repo)
        git(["rm", "-q", "-f", "a.txt"], self.repo)
        head = self.commit("r.txt", "root\n")
        record_unscoped(str(self.repo))  # no single base: records nothing
        self.assertEqual(jev.load_ledger(jev.ledger_path(str(self.repo))), [])
        self.assertEqual(self.run_gate(self.push_line(head, ZERO, "solo"))[0], 1)

    def test_new_ref_already_on_remote_allowed_without_jev(self):
        git(["checkout", "-q", "-b", "feature"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.base, ZERO, "feature")), (0, ""))
        self.assertEqual(self.calls, [])

    def test_ledger_approvals_append_prune_and_reject_bad_bases(self):
        path = jev.ledger_path(str(self.repo))
        deadline = time.monotonic() + 10
        self.assertEqual(jev._merge_ledger(path, {"a.txt": "100644:1"}, self.base, deadline), 1)
        self.assertEqual(jev._merge_ledger(path, {"b.txt": "deleted"}, self.base, deadline), 1)
        # Two runs at one base stay two approvals, never merged.
        self.assertEqual([(a["base"], a["files"]) for a in jev.load_ledger(path)],
                         [(self.base, {"a.txt": "100644:1"}), (self.base, {"b.txt": "deleted"})])
        for base in (None, "", "HEAD", "origin/main", self.base[:12], self.base.upper(),
                     "-" + self.base[1:], "g" * 40):
            with self.subTest(base=base):
                self.assertEqual(jev._merge_ledger(path, {"c.txt": "100644:2"}, base,
                                                   deadline), 0)
        self.assertEqual(len(jev.load_ledger(path)), 2)
        self.assertEqual(jev._merge_ledger(path, {}, self.base, deadline), 0)
        # Prune: a full ledger drops its oldest approval when a new run is recorded.
        full = [{"base": f"{n:040x}", "ts": float(n), "files": {"a.txt": "100644:1"}}
                for n in range(1, jev.LEDGER_MAX_APPROVALS + 1)]
        path.write_text(json.dumps({"version": 3, "approvals": full[::-1]}), encoding="utf-8")
        self.assertEqual(jev._merge_ledger(path, {"a.txt": "100644:1"}, self.base, deadline), 1)
        bases = [approval["base"] for approval in jev.load_ledger(path)]
        self.assertEqual(len(bases), jev.LEDGER_MAX_APPROVALS)
        self.assertNotIn(f"{1:040x}", bases)
        self.assertIn(f"{2:040x}", bases)
        self.assertEqual(bases[-1], self.base)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 3)

    # One approval is one critic run's whole change set: runs never combine.

    def stage(self, name, text):
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / name).write_text(text, encoding="utf-8")
        git(["add", name], self.repo)

    def test_partial_commit_of_reviewed_change_needs_review(self):
        self.commit("gateway.py", "def handler():\n    return serve()\n")
        self.commit("auth.py", "def check():\n    return verify()\n")
        git(["push", "--no-verify", "origin", "main"], self.repo)
        base = self.head()
        # The critic approves moving the check from auth.py into gateway.py.
        self.stage("auth.py", "def check():\n    return True\n")
        self.stage("gateway.py", "def handler():\n    verify()\n    return serve()\n")
        self.approve(base)
        git(["commit", "-q", "-m", "auth only", "--", "auth.py"], self.repo)
        partial = self.head()
        code, err = self.run_gate(self.push_line(partial, base))
        self.assertEqual(code, 1)
        self.assertIn("auth.py", err)
        git(["commit", "-q", "-m", "gateway", "--", "gateway.py"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), base)), (0, ""))

    def test_scoped_runs_never_combine(self):
        self.stage("a.txt", "two\n")
        self.stage("b.txt", "new\n")
        self.approve_paths("a.txt")
        self.approve_paths("b.txt")
        git(["commit", "-q", "-m", "both"], self.repo)
        both = self.head()
        self.assertEqual(self.run_gate(self.push_line(both, self.base))[0], 1)
        git(["reset", "-q", "--soft", self.base], self.repo)
        git(["commit", "-q", "-m", "a only", "--", "a.txt"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), self.base)), (0, ""))

    def approve_paths(self, paths):
        self.assertEqual([e["decision"] for e in self.critic(self.scope(paths=paths))],
                         ["recorded"])

    def test_push_must_change_exactly_the_runs_files(self):
        self.stage("a.txt", "two\n")
        self.stage("b.txt", "new\n")
        self.approve(self.base)  # one run: {a.txt, b.txt}
        git(["commit", "-q", "-m", "a only", "--", "a.txt"], self.repo)
        subset = self.head()
        self.assertEqual(self.run_gate(self.push_line(subset, self.base))[0], 1)
        git(["commit", "-q", "-m", "b", "--", "b.txt"], self.repo)
        exact = self.head()
        self.assertEqual(self.run_gate(self.push_line(exact, self.base)), (0, ""))
        superset = self.commit("c.txt", "extra\n")
        code, err = self.run_gate(self.push_line(superset, self.base))
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)
        self.assertIn("c.txt", err)

    def test_untracked_file_not_recorded_until_intent_to_add(self):
        (self.repo / "n.txt").write_text("new\n", encoding="utf-8")
        self.stage("a.txt", "two\n")
        self.approve(self.base)
        self.assertNotIn("n.txt", self.ledger())
        git(["add", "-N", "n.txt"], self.repo)
        self.approve(self.base)
        self.assertEqual(self.ledger()["n.txt"],
                         "100644:" + git(["hash-object", "n.txt"], self.repo))
        git(["add", "n.txt"], self.repo)
        git(["commit", "-q", "-m", "a and n"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), self.base)), (0, ""))

    def test_skip_worktree_file_absent_on_disk_not_recorded_deleted(self):
        head = self.commit("a.txt", "two\n")
        git(["update-index", "--skip-worktree", "a.txt"], self.repo)
        (self.repo / "a.txt").unlink()
        self.assertIn("a.txt", git(["diff", "--name-status", self.base], self.repo))
        self.stage("b.txt", "reviewed\n")
        logs = self.critic(self.scope())
        self.assertEqual([(e["decision"], e.get("reason")) for e in logs],
                         [("skipped", "unhashable")])
        self.assertEqual(self.ledger(), {})  # not {b.txt}: the run's set is all or nothing
        self.assertEqual(self.run_gate(self.push_line(head, self.base))[0], 1)
        git(["update-index", "--no-skip-worktree", "a.txt"], self.repo)
        git(["rm", "-q", "--cached", "a.txt"], self.repo)
        git(["commit", "-q", "-m", "really drop a"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), self.base))[0], 1)

    def test_reviewed_real_deletion_covered(self):
        git(["rm", "-q", "a.txt"], self.repo)
        self.approve(self.base)
        self.assertEqual(self.ledger(), {"a.txt": jev.LEDGER_DELETED})
        git(["commit", "-q", "-m", "drop a"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), self.base)), (0, ""))

    def test_case_only_rename_covered(self):
        self.commit("Auth.py", "x = 1\n")
        git(["push", "--no-verify", "origin", "main"], self.repo)
        base = self.head()
        git(["mv", "Auth.py", "auth.py"], self.repo)
        if git(["ls-files"], self.repo).split("\n").count("auth.py") != 1:
            self.skipTest("git mv did not produce a case-only rename here")
        self.approve(base)
        self.assertEqual(self.ledger()["Auth.py"], jev.LEDGER_DELETED)
        self.assertIn("auth.py", self.ledger())
        git(["commit", "-q", "-m", "rename"], self.repo)
        self.assertEqual(self.run_gate(self.push_line(self.head(), base)), (0, ""))

    def test_crlf_blob_under_autocrlf_matches_index(self):
        git(["config", "core.autocrlf", "false"], self.repo)
        (self.repo / "w.txt").write_bytes(b"one\r\ntwo\r\n")
        git(["add", "w.txt"], self.repo)
        git(["commit", "-m", "crlf"], self.repo)
        head = git(["rev-parse", "HEAD"], self.repo)
        git(["config", "core.autocrlf", "true"], self.repo)
        self.critic(self.scope())
        self.assertEqual(self.ledger().get("w.txt"),
                         "100644:" + git(["rev-parse", ":w.txt"], self.repo))
        self.assertEqual(self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"]),
                         (0, ""))

    def test_replace_refs_do_not_hide_content(self):
        good = self.commit("a.txt", "reviewed\n")
        record_unscoped(str(self.repo))
        git(["checkout", "-q", "-b", "evil", self.base], self.repo)
        bad = self.commit("a.txt", "evil\n")
        git(["replace", bad, good], self.repo)
        code, err = self.run_gate([f"refs/heads/evil {bad} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_non_commit_local_sha_refused(self):
        tree = git(["rev-parse", "HEAD^{tree}"], self.repo)
        code, err = self.run_gate([f"refs/heads/t {tree} refs/heads/t {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("not a commit", err)
        self.assertEqual(self.calls, [])
        git(["tag", "-a", "-m", "tree tag", "treetag", tree], self.repo)
        tag = git(["rev-parse", "treetag"], self.repo)
        code, err = self.run_gate([f"refs/tags/treetag {tag} refs/tags/treetag {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("not a commit", err)

    def test_tag_of_commit_checked_as_its_commit(self):
        self.commit("a.txt", "two\n")
        git(["tag", "-a", "-m", "release", "v1"], self.repo)
        tag = git(["rev-parse", "v1"], self.repo)
        code, err = self.run_gate([f"refs/tags/v1 {tag} refs/tags/v1 {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_other_remotes_refs_do_not_count_as_pushed(self):
        git(["checkout", "-b", "feature"], self.repo)
        head = self.commit("b.txt", "new\n")
        git(["update-ref", "refs/remotes/other/feature", head], self.repo)
        code, err = self.run_gate([f"refs/heads/feature {head} refs/heads/feature {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("b.txt", err)

    def test_root_commit_files_listed_despite_log_show_root_off(self):
        git(["config", "log.showRoot", "false"], self.repo)
        git(["checkout", "--orphan", "solo"], self.repo)
        git(["rm", "-q", "-f", "a.txt"], self.repo)
        head = self.commit("r.txt", "root\n")
        code, err = self.run_gate([f"refs/heads/solo {head} refs/heads/solo {ZERO}"])
        self.assertEqual(code, 1)
        self.assertIn("r.txt", err)


class InstallHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.home = self.tmp / "claude home"
        self.remote = self.tmp / "remote.git"
        git(["init", "--bare", "-b", "main", str(self.remote)], temp.name)
        self.repo = self.tmp / "repo"
        init_repo(self.repo)
        # A repository-level hooks path, so the installer is seen to honour it (and a
        # global core.hooksPath on the test machine is never written to).
        git(["config", "core.hooksPath", (self.tmp / "myhooks").as_posix()], self.repo)
        git(["remote", "add", "origin", str(self.remote)], self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(["add", "a.txt"], self.repo)
        git(["commit", "-m", "init"], self.repo)
        self.hooks = self.tmp / "myhooks"
        self.hooks.mkdir()

    def install(self, *args, env=None):
        env = os.environ | {"CLAUDE_HOME": str(self.home)} | (env or {})
        return subprocess.run([sys.executable, str(INSTALL), *args], env=env, text=True,
                              capture_output=True)

    def write_local_hook(self, code):
        hook = self.hooks / "pre-push"
        seen = (self.tmp / "seen.txt").as_posix()
        name = (self.tmp / "name.txt").as_posix()
        hook.write_bytes((f'#!/bin/sh\necho "$1" > "{seen}"\ncat >> "{seen}"\n'
                          f'basename "$0" > "{name}"\nexit {code}\n').encode())
        hook.chmod(0o755)

    def push(self):
        return subprocess.run(["git", "push", "origin", "main"], cwd=self.repo,
                              capture_output=True, text=True)

    def test_chains_existing_hook_and_is_idempotent(self):
        self.write_local_hook(0)
        self.assertEqual(self.install().returncode, 0)  # the bundle, so the gate exists
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        shim = (self.hooks / "pre-push").read_bytes()
        self.assertIn(b"coding-orchestrator pre-push gate", shim)
        self.assertNotIn(b"\r\n", shim)
        self.assertIn(b"exit 0", (self.hooks / "pre-push.d" / "pre-push").read_bytes())
        again = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("up to date", again.stdout)
        self.assertEqual((self.hooks / "pre-push").read_bytes(), shim)
        self.assertEqual(sorted(p.name for p in (self.hooks / "pre-push.d").iterdir()),
                         ["pre-push"])
        self.assertEqual(git(["config", "--get", "core.hooksPath"], self.repo),
                         self.hooks.as_posix())
        # Jev is off in a fresh install, so the gate warns and allows; the chained hook
        # saw git's arguments and stdin.
        pushed = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        seen = (self.tmp / "seen.txt").read_text()
        self.assertTrue(seen.startswith("origin\n"))
        self.assertIn("refs/heads/main", seen)
        # The chained hook keeps its own name, for hooks that dispatch on it.
        self.assertEqual((self.tmp / "name.txt").read_text().strip(), "pre-push")

    def test_failing_chained_hook_blocks_push(self):
        self.write_local_hook(1)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install("--pre-push-hook", str(self.repo)).returncode, 0)
        pushed = self.push()
        self.assertNotEqual(pushed.returncode, 0)
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "--quiet", "main"],
                                        cwd=self.remote, capture_output=True).returncode, 1)

    def test_missing_gate_blocks_push(self):
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not installed", result.stderr)
        pushed = self.push()
        self.assertNotEqual(pushed.returncode, 0)
        self.assertIn("is missing", pushed.stderr)

    def test_refuses_when_chained_hook_name_taken(self):
        self.write_local_hook(0)
        (self.hooks / "pre-push.d").mkdir()
        (self.hooks / "pre-push.d" / "pre-push").write_text("#!/bin/sh\n")
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(result.returncode, 1)
        self.assertIn("merge them by hand", result.stderr)

    def test_migrates_pre_push_local_from_earlier_install(self):
        self.assertEqual(self.install("--pre-push-hook", str(self.repo)).returncode, 0)
        shim = (self.hooks / "pre-push").read_bytes()
        self.write_local_hook(0)
        os.replace(self.hooks / "pre-push", self.hooks / "pre-push.local")
        (self.hooks / "pre-push").write_bytes(shim)
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.hooks / "pre-push.local").exists())
        self.assertIn(b"exit 0", (self.hooks / "pre-push.d" / "pre-push").read_bytes())
        self.assertEqual((self.hooks / "pre-push").read_bytes(), shim)

    def test_refuses_global_hooks_path(self):
        git(["config", "--unset", "core.hooksPath"], self.repo)
        global_hooks = self.tmp / "global hooks"
        global_config = self.tmp / "gitconfig"
        global_config.write_text(f"[core]\n\thooksPath = {global_hooks.as_posix()}\n",
                                 encoding="utf-8")
        result = self.install("--pre-push-hook", str(self.repo),
                              env={"GIT_CONFIG_GLOBAL": str(global_config)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("global", result.stderr)
        self.assertFalse(global_hooks.exists())

    def assert_refuses_in_work_tree(self, hooks):
        git(["config", "core.hooksPath", hooks], self.repo)
        target = self.repo / hooks
        before = sorted(p.name for p in target.iterdir())
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside the work tree", result.stderr)
        self.assertIn("pre-push-gate.py", result.stderr)
        self.assertEqual(sorted(p.name for p in target.iterdir()), before)

    def test_refuses_tracked_hooks_dir(self):
        (self.repo / "inrepo").mkdir()
        (self.repo / "inrepo" / "pre-push").write_text("#!/bin/sh\n", encoding="utf-8")
        git(["add", "inrepo/pre-push"], self.repo)
        git(["commit", "-m", "tracked hooks"], self.repo)
        self.assert_refuses_in_work_tree("inrepo")

    def test_refuses_ignored_hooks_dir_in_work_tree(self):
        (self.repo / ".gitignore").write_text(".husky/_\n", encoding="utf-8")
        (self.repo / ".husky" / "_").mkdir(parents=True)
        self.assert_refuses_in_work_tree(".husky/_")

    def link_dir(self, link, target):
        """`link` as a directory symlink (or on Windows a junction) to `target`, or skip."""
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True)
            if made.returncode == 0:
                return
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory links are unavailable")

    def test_refuses_linked_hooks_dir_in_work_tree(self):
        outside = self.tmp / "outside hooks"
        outside.mkdir()
        self.link_dir(self.repo / "jhooks", outside)
        self.assert_refuses_in_work_tree("jhooks")
        self.assertEqual(list(outside.iterdir()), [])

    def test_default_hooks_dir_accepted(self):
        git(["config", "--unset", "core.hooksPath"], self.repo)
        result = self.install_isolated(self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".git" / "hooks" / "pre-push").exists())

    def install_isolated(self, path):
        """--pre-push-hook `path` with an empty global git config (no global hooksPath)."""
        empty = self.tmp / "empty gitconfig"
        empty.write_text("", encoding="utf-8")
        return self.install("--pre-push-hook", str(path), env={"GIT_CONFIG_GLOBAL": str(empty)})

    def test_subdirectory_argument_accepted(self):
        git(["config", "--unset", "core.hooksPath"], self.repo)
        (self.repo / "pkg").mkdir()
        result = self.install_isolated(self.repo / "pkg")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".git" / "hooks" / "pre-push").exists())

    def test_linked_worktree_accepted(self):
        git(["config", "--unset", "core.hooksPath"], self.repo)
        tree = self.tmp / "wt"
        git(["worktree", "add", "-q", "-b", "wt", str(tree)], self.repo)
        result = self.install_isolated(tree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / ".git" / "hooks" / "pre-push").exists())


if __name__ == "__main__":
    unittest.main()

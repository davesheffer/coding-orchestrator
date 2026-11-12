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
APPROVE = "RESULT: approve, no findings\nEVIDENCE: read the diff\nCONFIDENCE: high\n"


def risk_response(choice="security", needs_review=0.9):
    return {"model": "jev-1.13.0", "answers": {
        "risk": {"type": "choice", "choice": choice, "confidence": 0.9,
                 "probabilities": {choice: 0.9}},
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
        jev.record_critic_ledger(str(self.repo))
        code, err = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.calls, [])
        ledger = jev.load_ledger(jev.ledger_path(str(self.repo)))
        self.assertEqual(ledger["a.txt"], git(["rev-parse", "HEAD:a.txt"], self.repo))

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
        jev.record_critic_ledger(str(self.repo))
        head = self.commit("a.txt", "three\n")
        code, _ = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"])
        self.assertEqual(code, 1)

    def test_not_risky_allowed(self):
        head = self.commit("a.txt", "two\n")
        code, _ = self.run_gate([f"refs/heads/main {head} refs/heads/main {self.base}"],
                                self.classify(risk_response("none", 0.1)))
        self.assertEqual(code, 0)

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
        jev.record_critic_ledger(str(self.repo))
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
        jev.record_critic_ledger(str(self.repo))  # reviews a.txt at "three" only
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
        jev.record_critic_ledger(str(self.repo))  # reviews b.txt only
        git(["merge", "-s", "ours", "-m", "merge main", "main"], self.repo)
        return upstream, git(["rev-parse", "HEAD"], self.repo)

    def test_ours_merge_revert_needs_review(self):
        upstream, merge = self.ours_merge()
        code, err = self.run_gate([f"refs/heads/local {merge} refs/heads/main {upstream}"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", err)

    def test_recorder_covers_ours_merge_revert(self):
        upstream, merge = self.ours_merge()
        jev.record_critic_ledger(str(self.repo))
        ledger = jev.load_ledger(jev.ledger_path(str(self.repo)))
        self.assertEqual(ledger.get("a.txt"), git(["rev-parse", "HEAD:a.txt"], self.repo))
        code, err = self.run_gate([f"refs/heads/local {merge} refs/heads/main {upstream}"])
        self.assertEqual((code, err), (0, ""))

    def critic_cfg(self):
        return {**self.cfg, "features": {**self.cfg["features"], "report_check": False}}

    def scope(self, repo=None, base=None, paths=None):
        line = f"REVIEW-SCOPE: {repo or self.repo} base={base or self.base}"
        return f"Review this change.\n{line}" + (f" paths={paths}" if paths else "") + "\n"

    def foreground(self, prompt, report=APPROVE, duration=None, state=None):
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
        return jev.load_ledger(jev.ledger_path(str(repo or self.repo)))

    def test_scoped_critic_records_what_it_was_handed(self):
        self.commit("a.txt", "two\n")
        (self.repo / "c.txt").write_text("untracked\n", encoding="utf-8")
        logs = self.foreground(self.scope())
        ledger = self.ledger()
        self.assertEqual(ledger["a.txt"], git(["rev-parse", "HEAD:a.txt"], self.repo))
        self.assertEqual(ledger["c.txt"], git(["hash-object", "c.txt"], self.repo))
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
        (other / "o.txt").write_text("two\n", encoding="utf-8")
        self.foreground(self.scope(repo=other, base="HEAD"))
        self.assertEqual(self.ledger(other), {"o.txt": git(["hash-object", "o.txt"], other)})
        self.assertEqual(self.ledger(), {})

    def test_no_or_bad_scope_records_nothing(self):
        self.commit("a.txt", "two\n")
        cases = [("Review this change.\n", "no_scope"),
                 ("REVIEW-SCOPE: relative/path base=HEAD\n", "bad_scope"),
                 (f"REVIEW-SCOPE: {self.repo}\n", "bad_scope"),
                 (self.scope() + self.scope(), "bad_scope"),
                 (self.scope(repo=self.repo.parent / "missing"), "not_a_repo"),
                 (self.scope(base="no-such-ref"), "bad_base")]
        for prompt, reason in cases:
            with self.subTest(reason=reason):
                logs = self.foreground(prompt)
                self.assertEqual([(e["decision"], e["reason"]) for e in logs],
                                 [("skipped", reason)])
        self.assertEqual(self.ledger(), {})

    def test_blocking_or_missing_verdict_records_nothing(self):
        self.commit("a.txt", "two\n")
        for report in ("RESULT: BLOCKING, two findings\n",
                       "Fix first: the tag check\nRESULT: see above\n",
                       "RESULT: fix first\n", "RESULT: blocked\n",
                       "Looks fine to me.\n"):
            with self.subTest(report=report):
                logs = self.foreground(self.scope(), report=report)
                self.assertEqual([e.get("reason") for e in logs], ["blocking_or_no_result"])
        self.assertEqual(self.ledger(), {})

    def test_paths_narrow_the_scope(self):
        self.commit("a.txt", "two\n")
        (self.repo / "sub").mkdir()
        self.commit("sub/b.txt", "new\n")
        self.commit("d.txt", "new\n")
        self.foreground(self.scope(paths="sub,d.txt"))
        self.assertEqual(sorted(self.ledger()), ["d.txt", "sub/b.txt"])

    def test_async_critic_records_only_files_unchanged_since_launch(self):
        self.commit("a.txt", "two\n")
        (self.repo / "b.txt").write_text("untracked\n", encoding="utf-8")
        launch = {"tool_name": "Agent", "session_id": "s1", "cwd": str(self.repo),
                  "tool_input": {"subagent_type": "critic", "prompt": self.scope()},
                  "tool_response": {"status": "async_launched", "agentId": "ag1"}}
        handback = {"tool_name": "SubagentHandback", "session_id": "s1", "cwd": str(self.repo),
                    "agent_type": "critic", "agent_id": "ag1",
                    "tool_input": {"message": APPROVE}}
        with tempfile.TemporaryDirectory() as state:
            jev.agent_done(launch, self.critic_cfg(), state_dir=Path(state))
            (self.repo / "a.txt").write_text("edited while the critic ran\n", encoding="utf-8")
            (self.repo / "e.txt").write_text("created while the critic ran\n", encoding="utf-8")
            jev.agent_done(handback, self.critic_cfg(), state_dir=Path(state))
            saved = jev.load_session_state("s1", Path(state))
        self.assertEqual(self.ledger(), {"b.txt": git(["hash-object", "b.txt"], self.repo)})
        self.assertEqual(saved.get("critic_snapshots"), {})

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

    def test_foreground_critic_verdict_from_its_handback(self):
        self.commit("a.txt", "two\n")
        stub = ('This agent\'s report was delivered to you as a message from "fg1" (its '
                'SubagentHandback call). Read it there; it is not repeated here.')
        for message, recorded in (("RESULT: blocking\n", False), (APPROVE, True)):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as state:
                jev.agent_done({"tool_name": "SubagentHandback", "session_id": "s1",
                                "cwd": str(self.repo), "agent_type": "critic",
                                "agent_id": "fg1", "tool_input": {"message": message}},
                               self.critic_cfg(), state_dir=Path(state))
                self.foreground(self.scope(), report=stub, state=state)
                self.assertEqual("a.txt" in self.ledger(), recorded)

    def test_foreground_critic_skips_files_modified_after_start(self):
        self.commit("a.txt", "two\n")
        old = time.time() - 1000
        os.utime(self.repo / "a.txt", (old, old))
        (self.repo / "c.txt").write_text("written during the run\n", encoding="utf-8")
        self.foreground(self.scope(), duration=60000)
        ledger = self.ledger()
        self.assertIn("a.txt", ledger)
        self.assertNotIn("c.txt", ledger)

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
        git(["config", "core.hooksPath", "myhooks"], self.repo)
        git(["remote", "add", "origin", str(self.remote)], self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        git(["add", "a.txt"], self.repo)
        git(["commit", "-m", "init"], self.repo)
        self.hooks = self.repo / "myhooks"
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
        self.assertEqual(git(["config", "--get", "core.hooksPath"], self.repo), "myhooks")
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

    def test_refuses_tracked_hooks_dir(self):
        self.write_local_hook(0)
        git(["add", "myhooks/pre-push"], self.repo)
        git(["commit", "-m", "tracked hooks"], self.repo)
        before = (self.hooks / "pre-push").read_bytes()
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked by git", result.stderr)
        self.assertIn("pre-push-gate.py", result.stderr)
        self.assertEqual((self.hooks / "pre-push").read_bytes(), before)
        self.assertFalse((self.hooks / "pre-push.d").exists())


if __name__ == "__main__":
    unittest.main()

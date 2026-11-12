import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
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

    def test_agent_done_critic_records_ledger(self):
        self.commit("a.txt", "two\n")
        (self.repo / "c.txt").write_text("untracked\n", encoding="utf-8")
        payload = {"tool_name": "Agent", "session_id": "s1", "cwd": str(self.repo),
                   "tool_input": {"subagent_type": "critic"},
                   "tool_response": {"status": "completed",
                                     "content": [{"type": "text", "text": "ok"}]}}
        cfg = {**self.cfg, "features": {**self.cfg["features"], "report_check": False}}
        with tempfile.TemporaryDirectory() as state:
            jev.agent_done(payload, cfg, state_dir=Path(state))
        ledger = jev.load_ledger(jev.ledger_path(str(self.repo)))
        self.assertEqual(ledger["a.txt"], git(["rev-parse", "HEAD:a.txt"], self.repo))
        self.assertEqual(ledger["c.txt"], git(["hash-object", "c.txt"], self.repo))


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

    def install(self, *args):
        env = os.environ | {"CLAUDE_HOME": str(self.home)}
        return subprocess.run([sys.executable, str(INSTALL), *args], env=env, text=True,
                              capture_output=True)

    def write_local_hook(self, code):
        hook = self.hooks / "pre-push"
        seen = (self.tmp / "seen.txt").as_posix()
        hook.write_bytes((f'#!/bin/sh\necho "$1" > "{seen}"\ncat >> "{seen}"\nexit {code}\n')
                         .encode())
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
        self.assertIn(b"exit 0", (self.hooks / "pre-push.local").read_bytes())
        again = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("up to date", again.stdout)
        self.assertEqual((self.hooks / "pre-push").read_bytes(), shim)
        self.assertFalse((self.hooks / "pre-push.local.local").exists())
        self.assertEqual(git(["config", "--get", "core.hooksPath"], self.repo), "myhooks")
        # Jev is off in a fresh install, so the gate warns and allows; the chained hook
        # saw git's arguments and stdin.
        pushed = self.push()
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        seen = (self.tmp / "seen.txt").read_text()
        self.assertTrue(seen.startswith("origin\n"))
        self.assertIn("refs/heads/main", seen)

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

    def test_refuses_when_local_hook_name_taken(self):
        self.write_local_hook(0)
        (self.hooks / "pre-push.local").write_text("#!/bin/sh\n")
        result = self.install("--pre-push-hook", str(self.repo))
        self.assertEqual(result.returncode, 1)
        self.assertIn("merge them by hand", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Where a push goes, and what the risk gate compares it against (bin/jev-guard.py).

The repository under test is a clone that is up to date with `origin` and has a second
configured remote, `mirror`, that was never fetched. No test runs a push.
"""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "jev-guard.py"
spec = importlib.util.spec_from_file_location("jev_guard_push_dest", SCRIPT)
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)


def run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def risk_response(choice="security", confidence=0.9, needs_review=0.9):
    return {"model": "jev-1.13.0", "answers": {
        "risk": {"type": "choice", "choice": choice, "confidence": confidence,
                 "probabilities": {choice: confidence}},
        "needs_review": {"type": "noul", "noul": needs_review},
    }, "usage": {}}


class PushDestinationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.state_dir = self.tmp / "state"
        origin = self.tmp / "origin"
        origin.mkdir()
        run_git(["init"], origin)
        run_git(["symbolic-ref", "HEAD", "refs/heads/main"], origin)
        self.identify(origin)
        (origin / "a.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "a.txt"], origin)
        run_git(["commit", "-m", "init"], origin)
        run_git(["clone", str(origin), str(self.tmp / "repo")], self.tmp)
        self.repo = self.tmp / "repo"
        self.identify(self.repo)
        run_git(["init", "--bare", str(self.tmp / "mirror.git")], self.tmp)
        run_git(["remote", "add", "mirror", str(self.tmp / "mirror.git")], self.repo)
        self.calls = []
        self.logs = []

    @staticmethod
    def identify(repo):
        run_git(["config", "user.email", "t@example.com"], repo)
        run_git(["config", "user.name", "Test"], repo)

    def classify(self, choice="security"):
        def fn(body, key):
            self.calls.append((body, key))
            return risk_response(choice)
        return fn

    def boom(self, body, key):
        self.calls.append((body, key))
        raise TimeoutError("slow")

    def gate(self, command, session_id="s", classify_fn=None, cfg=None):
        payload = {"tool_name": "Bash", "session_id": session_id,
                   "tool_input": {"command": command}, "cwd": str(self.repo)}
        return jev.gate(payload, cfg or self.cfg, classify_fn or self.classify(),
                        self.logs.append, now=time.time, state_dir=self.state_dir)

    def reason(self, out):
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def commit(self, name="b.txt", text="two\n"):
        (self.repo / name).write_text(text, encoding="utf-8")
        run_git(["add", name], self.repo)
        run_git(["commit", "-m", name], self.repo)

    def assert_passes_unasked(self, *commands):
        for i, command in enumerate(commands):
            with self.subTest(command=command):
                self.calls.clear()
                self.assertIsNone(self.gate(command, f"pass{i}", self.boom))
                self.assertEqual(self.calls, [])

    def assert_asked(self, command, flags, session_id):
        """The classifier is asked about `command` (once, whatever the diff holds), with
        `flags` as its noted kinds; returns the state it was sent."""
        self.calls.clear()
        out = self.gate(command, session_id)
        self.assertIn("This push looks risky", self.reason(out))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.logs[-1]["push_flags"], flags)
        state = self.calls[0][0]["state"]
        # Jev down: denied, never a silent allow.
        self.assertIn("fail-closed", self.reason(self.gate(command, session_id + "-down", self.boom)))
        # Jev answers "none": allowed, having been asked.
        self.calls.clear()
        self.assertIsNone(self.gate(command, session_id + "-none", self.classify("none")))
        self.assertEqual(len(self.calls), 1)
        return state

    def test_push_to_its_upstream_with_nothing_unpushed_passes_unasked(self):
        self.assert_passes_unasked(
            "git push", "git push origin", "git push origin main", "git push origin HEAD",
            "git push origin HEAD:main", "git push origin main:main", "git push -u origin main",
            "git push origin HEAD:refs/heads/main", "git push origin refs/heads/main",
            "git push --repo=origin", "git push --repo origin")

    def test_destination_with_no_tracking_ref_reaches_the_classifier(self):
        # Critic 3: HEAD == origin/main, so each of these was diffed against HEAD's own
        # upstream and passed on the empty diff, whatever remote or branch it named.
        for i, command in enumerate((
                "git push origin main:production", "git push origin HEAD:refs/heads/release",
                "git push mirror main", "git push mirror", "git push mirror HEAD:main",
                "git push --repo=mirror", "git push --repo mirror", "git push -u mirror HEAD",
                "git push origin HEAD:refs/for/main", "git push origin main:refs/tags/v1",
                "git push origin main main:production")):
            with self.subTest(command=command):
                state = self.assert_asked(command, ["destination"], f"d{i}")
                self.assertEqual(state["push"], jev._PUSH_DESTINATION_NOTE)
                self.assertEqual(state["files"], [])
        # An earlier critic run covers files, not an unknown destination.
        saved = jev.load_session_state("crit", self.state_dir)
        saved["critic_ts"] = time.time() + 10
        jev.save_session_state("crit", saved, self.state_dir)
        self.calls.clear()
        self.assertIsNotNone(self.gate("git push mirror main", "crit"))
        self.assertEqual(len(self.calls), 1)
        # The identical retry of a judged denial overrides that push, not another one.
        self.assertIsNone(self.gate("git push mirror main", "crit"))
        self.assertEqual(self.logs[-1]["decision"], "override")
        self.assertIsNotNone(self.gate("git push mirror main:production", "crit"))
        self.assertEqual(len(self.calls), 2)

    def test_unpushed_commits_to_an_unknown_destination_are_still_diffed(self):
        self.commit()
        for i, command in enumerate(("git push mirror main", "git push mirror",
                                     "git push origin main:production")):
            with self.subTest(command=command):
                state = self.assert_asked(command, ["destination"], f"u{i}")
                self.assertIn("+two", state["diff"])
                self.assertEqual(state["files"], ["b.txt"])
        # To its own upstream the same commit is asked about with no note.
        self.calls.clear()
        self.assertIsNotNone(self.gate("git push origin main", "plain"))
        self.assertNotIn("push", self.calls[0][0]["state"])
        self.assertNotIn("push_flags", self.logs[-1])

    def test_new_branch_is_diffed_from_the_default_branch_and_noted(self):
        run_git(["checkout", "-b", "feature"], self.repo)
        self.commit("feature.txt", "feature-only\n")
        for i, command in enumerate(("git push -u origin feature", "git push origin HEAD",
                                     "git push --set-upstream origin HEAD:refs/heads/feature",
                                     "git push origin")):
            with self.subTest(command=command):
                state = self.assert_asked(command, ["destination"], f"n{i}")
                self.assertIn("+feature-only", state["diff"])
        # No remote named: git sends it to origin too, where the branch is as new.
        state = self.assert_asked("git push", ["destination"], "bare")
        self.assertIn("+feature-only", state["diff"])
        self.assertEqual(state["push"], jev._PUSH_DESTINATION_NOTE)

    def test_named_destination_is_compared_with_its_own_tracking_ref(self):
        # mirror/main as a fetch would leave it: one commit behind what origin holds.
        run_git(["update-ref", "refs/remotes/mirror/main", "HEAD"], self.repo)
        self.commit()
        run_git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        self.assert_passes_unasked("git push", "git push origin", "git push origin main")
        for i, command in enumerate(("git push mirror main", "git push mirror",
                                     "git push mirror HEAD:main", "git push --repo=mirror")):
            with self.subTest(command=command):
                self.calls.clear()
                self.assertIn("This push looks risky", self.reason(self.gate(command, f"t{i}")))
                state = self.calls[0][0]["state"]
                self.assertIn("+two", state["diff"])
                self.assertNotIn("push", state)
        # Once mirror holds it too, nothing is left to ask about.
        run_git(["update-ref", "refs/remotes/mirror/main", "HEAD"], self.repo)
        self.assert_passes_unasked("git push mirror main", "git push mirror",
                                   "git push mirror HEAD:main", "git push --repo=mirror")

    def test_upstream_is_the_base_only_on_its_own_remote(self):
        run_git(["update-ref", "refs/remotes/mirror/main", "HEAD"], self.repo)
        run_git(["branch", "--set-upstream-to=mirror/main"], self.repo)
        self.assert_passes_unasked("git push", "git push mirror", "git push origin",
                                   "git push origin main")
        # One commit that mirror, the upstream, holds and origin does not.
        self.commit()
        run_git(["update-ref", "refs/remotes/mirror/main", "HEAD"], self.repo)
        self.assert_passes_unasked("git push", "git push mirror", "git push mirror main")
        for i, command in enumerate(("git push origin", "git push origin main",
                                     "git push --repo=origin")):
            with self.subTest(command=command):
                self.calls.clear()
                self.assertIn("This push looks risky", self.reason(self.gate(command, f"o{i}")))
                state = self.calls[0][0]["state"]
                self.assertIn("+two", state["diff"])
                self.assertNotIn("push", state)

    def test_push_with_no_remote_goes_where_git_sends_it(self):
        # Round 5, 4: remote.pushDefault names the remote of a bare `git push`. Never
        # fetched, it was diffed against the upstream on origin and passed unasked.
        run_git(["remote", "add", "backup", str(self.tmp / "mirror.git")], self.repo)
        run_git(["config", "remote.pushDefault", "backup"], self.repo)
        for i, command in enumerate(("git push", "git push --no-verify", "git push -u")):
            with self.subTest(command=command):
                state = self.assert_asked(command, ["destination"], f"pd{i}")
                self.assertEqual(state["push"], jev._PUSH_DESTINATION_NOTE)
                self.assertEqual(state["files"], [])
        # Naming origin is still a push to the upstream.
        self.assert_passes_unasked("git push origin", "git push origin main")
        # Once backup's branch is known here, the push is compared with it.
        run_git(["update-ref", "refs/remotes/backup/main", "HEAD"], self.repo)
        self.assert_passes_unasked("git push")
        self.commit()
        run_git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        self.calls.clear()
        self.assertIn("This push looks risky", self.reason(self.gate("git push", "behind")))
        state = self.calls[0][0]["state"]
        self.assertIn("+two", state["diff"])
        self.assertNotIn("push", state)
        # branch.<name>.pushRemote comes before remote.pushDefault.
        run_git(["config", "branch.main.pushRemote", "origin"], self.repo)
        self.assert_passes_unasked("git push")
        run_git(["config", "branch.main.pushRemote", "mirror"], self.repo)
        self.assert_asked("git push", ["destination"], "pr")

    def test_remote_added_first_does_not_pass_as_a_known_one(self):
        # In the same command the remote is not configured yet when the gate runs.
        self.calls.clear()
        out = self.gate("git remote add fresh ../mirror.git && git push fresh main", "same")
        self.assertIn("This push looks risky", self.reason(out))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("remote", self.logs[-1]["push_flags"])
        # Added by an earlier command it is configured, but nothing of it was fetched.
        run_git(["remote", "add", "fresh", "../mirror.git"], self.repo)
        for i, command in enumerate(("git push fresh main", "git push fresh",
                                     "git push fresh HEAD:main", "git push --repo=fresh")):
            with self.subTest(command=command):
                self.assert_asked(command, ["destination"], f"f{i}")

    def test_remote_program_options_reach_the_classifier(self):
        # Critic 7: --receive-pack/--exec name what runs on the remote end.
        for args, expected in (
                (["--receive-pack=x", "origin"], True), (["--receive-pack", "x", "origin"], True),
                (["--exec=x"], True), (["--exec", "x"], True), (["--rece=x"], True),
                (["--exe", "x"], True), (["--e=x"], True),
                (["--rec=x"], True),  # ambiguous with --recurse-submodules: read as each
                (["--recurse-submodules=check"], False), (["--recu=check"], False),
                (["--repo=origin"], False), (["--push-option", "--exec"], False),
                (["origin", "--", "--exec=x"], False)):
            self.assertEqual(jev._push_request(args)["program"], expected, args)
        self.assertEqual(jev._push_request(["--exec", "x", "origin", "main"])["remote"], "origin")
        for i, command in enumerate((
                "git push --receive-pack=evil origin main", "git push --receive-pack evil origin main",
                "git push --exec=evil origin main", "git push --exec evil", "git push --rece=evil",
                "git push origin main --exe=evil")):
            with self.subTest(command=command):
                state = self.assert_asked(command, ["program"], f"p{i}")
                self.assertIn("--receive-pack or --exec", state["push"])
        self.assert_passes_unasked("git push --recurse-submodules=check origin main",
                                   "git push --no-verify --repo=origin")
        # An override stands for that program only.
        self.assertIsNotNone(self.gate("git push --exec=one origin main", "p0"))
        self.assertEqual(self.logs[-1]["decision"], "deny")
        self.assertIsNone(self.gate("git push --exec=one origin main", "p0"))
        self.assertEqual(self.logs[-1]["decision"], "override")
        self.assertIsNotNone(self.gate("git push --exec=two origin main", "p0"))
        self.assertEqual(self.logs[-1]["decision"], "deny")

    def test_inline_include_config_reaches_the_classifier(self):
        # Critic 7: an included file may set the remote's pushurl or its refspecs.
        for git_words, expected in (
                (["-c", "include.path=/tmp/x"], True), (["-c", "Include.Path=/tmp/x"], True),
                (["-c", "includeIf.gitdir:/a/.path=/tmp/x"], True),
                (["-c", "INCLUDEIF.onbranch:main.path=x"], True),
                (["--config-env=include.path=X"], True), (["--config-env", "includeif.x.path=X"], True),
                (["-c", "included.path=x"], False), (["-c", "core.pager=cat"], False)):
            self.assertEqual(jev._push_request([], git_words)["config"], expected, git_words)
        for i, command in enumerate((
                "git -c include.path=/tmp/x push", "git -c INCLUDE.PATH=/tmp/x push origin main",
                "git -c includeIf.gitdir:/a/.path=/tmp/x push origin",
                "git --config-env=include.path=X push")):
            with self.subTest(command=command):
                state = self.assert_asked(command, ["config"], f"c{i}")
                self.assertEqual(state["push"], jev._PUSH_CONFIG_NOTE)
        self.assert_passes_unasked("git -c core.pager=cat push", "git -c included.path=x push")

    def test_fail_closed_reason_quotes_the_key_file_setting_only_when_it_is_a_path(self):
        # Critic 5 and 6: a setting that is not a path fails closed, and a key pasted
        # where the path belongs never shows up in the reason.
        self.commit()
        missing = self.tmp / "no-such-key-file"
        long_path = "/no-such-dir/" + "p" * 300
        cases = (
            (str(missing), "the file jev.api_key_file names does not exist: " + str(missing)),
            ("~/no-such-jev-dir-for-this-test/key",
             "the file jev.api_key_file names does not exist: ~/no-such-jev-dir-for-this-test/key"),
            ("tok-secret-pasted-0123456789", "the configured jev.api_key_file does not exist;"),
            ("key.txt", "the configured jev.api_key_file does not exist;"),
            (long_path, "the configured jev.api_key_file "),
            (123, "the configured jev.api_key_file is not a file path;"),
            (["tok-secret-in-a-list"], "the configured jev.api_key_file is not a file path;"),
            ({"tok-secret": 1}, "the configured jev.api_key_file is not a file path;"),
            (False, "the configured jev.api_key_file is not a file path;"))
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            for i, (value, text) in enumerate(cases):
                for classify_fn in (self.boom, self.classify()):  # Jev down or up
                    with self.subTest(value=value):
                        out = self.gate("git push", f"k{i}", classify_fn,
                                        dict(self.cfg, api_key_file=value))
                        reason = self.reason(out)
                        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                        for part in ("fail-closed", text, "fix it or set TYPESAFE_API_KEY"):
                            self.assertIn(part, reason)
                        self.assertNotIn("tok-secret", reason)
                        self.assertNotIn("p" * 50, reason)
                        entry = self.logs[-1]
                        self.assertEqual(
                            (entry["decision"], entry["reason"], entry["error"], entry["fail_closed"]),
                            ("deny", "unavailable", "BadApiKeyFile", True))
            # Not set at all is still the setup gap that fails open, with its warning.
            for value in (None, ""):
                with mock.patch.object(jev.sys, "stderr", io.StringIO()) as stderr:
                    self.assertIsNone(self.gate("git push", "open", self.boom,
                                                dict(self.cfg, api_key_file=value)))
                self.assertEqual(self.logs[-1]["error"], "NoApiKey")
                self.assertEqual(stderr.getvalue(), jev.NO_KEY_WARNING + "\n")
        self.assertEqual(self.calls, [])
        self.assertNotIn("tok-secret", json.dumps(self.logs))


if __name__ == "__main__":
    unittest.main()

import errno
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "jev-guard.py"
spec = importlib.util.spec_from_file_location("jev_guard", SCRIPT)
jev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jev)


def risk_response(choice="security", confidence=0.9, needs_review=0.9):
    return {"model": "jev-1.13.0", "answers": {
        "risk": {"type": "choice", "choice": choice, "confidence": confidence,
                 "probabilities": {choice: confidence}},
        "needs_review": {"type": "noul", "noul": needs_review},
    }, "usage": {}}


def report_response(supported=0.9, material_gap=0.1):
    return {"model": "jev-1.13.0", "answers": {
        "supported": {"type": "noul", "noul": supported},
        "material_gap": {"type": "noul", "noul": material_gap},
    }, "usage": {}}


def run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class GateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "repo"
        self.repo.mkdir()
        self.state_dir = Path(temp.name) / "state"
        run_git(["init"], self.repo)
        run_git(["config", "user.email", "d@ylm.co.il"], self.repo)
        run_git(["config", "user.name", "Test"], self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "a.txt"], self.repo)
        run_git(["commit", "-m", "init"], self.repo)
        self.calls = []
        self.logs = []

    def classify(self, choice="security", confidence=0.9, needs_review=0.9):
        def fn(body, key):
            self.calls.append((body, key))
            return risk_response(choice, confidence, needs_review)
        return fn

    def gate(self, payload, cfg=None, classify_fn=None, now=None):
        return jev.gate(payload, cfg or self.cfg, classify_fn, self.logs.append,
                         now=now or time.time, state_dir=self.state_dir)

    def payload(self, command, session_id="sess1", cwd=None):
        return {"tool_name": "Bash", "session_id": session_id,
                "tool_input": {"command": command}, "cwd": cwd or str(self.repo)}

    def stage_change(self, name="a.txt", content="two\n"):
        (self.repo / name).write_text(content, encoding="utf-8")
        run_git(["add", name], self.repo)

    def test_non_git_bash_is_noop(self):
        out = self.gate(self.payload("ls -la"), classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

    def test_git_status_is_noop(self):
        out = self.gate(self.payload("git status"), classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

    def test_commit_risky_staged_diff_denies(self):
        self.stage_change()
        out = self.gate(self.payload("git commit -m 'x'"), classify_fn=self.classify())
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertIn("security", hook["permissionDecisionReason"])
        self.assertIn("p=0.90", hook["permissionDecisionReason"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.logs[-1]["decision"], "deny")
        self.assertNotIn("two", json.dumps(self.logs[-1]))
        self.assertNotIn("a.txt", json.dumps(self.logs[-1]))

    def test_identical_retry_overrides(self):
        self.stage_change()
        payload = self.payload("git commit -m 'x'")
        out1 = self.gate(payload, classify_fn=self.classify())
        self.assertIsNotNone(out1)
        out2 = self.gate(payload, classify_fn=self.classify())
        self.assertIsNone(out2)
        self.assertEqual(self.logs[-1]["decision"], "override")
        self.assertEqual(len(self.calls), 1)

    def test_critic_after_edit_is_covered(self):
        self.stage_change()
        state = jev.load_session_state("sess1", self.state_dir)
        state["critic_ts"] = time.time() + 10
        jev.save_session_state("sess1", state, self.state_dir)
        out = self.gate(self.payload("git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.logs[-1]["decision"], "covered")

    def test_critic_before_later_edit_still_gated(self):
        state = jev.load_session_state("sess1", self.state_dir)
        state["critic_ts"] = time.time()
        jev.save_session_state("sess1", state, self.state_dir)
        time.sleep(0.05)
        self.stage_change()
        out = self.gate(self.payload("git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(self.logs[-1]["decision"], "deny")

    def test_non_risky_choice_allows(self):
        self.stage_change()
        out = self.gate(self.payload("git commit -m 'x'"),
                         classify_fn=self.classify(choice="none"))
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["decision"], "allow")

    def test_needs_review_below_threshold_allows(self):
        self.stage_change()
        out = self.gate(self.payload("git commit -m 'x'"),
                         classify_fn=self.classify(needs_review=0.1))
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["decision"], "allow")

    def test_junk_max_diff_chars_falls_back_to_default(self):
        self.stage_change()
        for n, bad in enumerate(("abc", [1], {"a": 1}, None, True, -5, float("inf"), 1.5)):
            with self.subTest(bad=repr(bad)):
                self.calls.clear()
                cfg = {**self.cfg, "max_diff_chars": bad}
                payload = self.payload("git commit -m 'x'", session_id=f"junk{n}")
                self.assertIsNotNone(self.gate(payload, cfg=cfg, classify_fn=self.classify()))
                self.assertIn("diff", self.calls[0][0]["state"])
                self.assertLessEqual(len(self.calls[0][0]["state"]["diff"]), 12000)

    def test_junk_max_report_chars_falls_back_to_default(self):
        cfg = {**jev.load_config(Path("/nonexistent/config.json")), "max_report_chars": "abc"}
        text = "RESULT: ok\nEVIDENCE: ran tests, exit 0\nCONFIDENCE: high\nUNVERIFIED: none"
        reasons, codes, _, _ = jev._analyze_report(
            text, cfg, lambda body, key: None, confidence_heuristic=False)
        self.assertNotIn("missing", codes)

    def test_send_diff_false_omits_diff(self):
        self.stage_change()
        cfg = {**self.cfg, "send_diff": False}
        self.gate(self.payload("git commit -m 'x'"), cfg=cfg, classify_fn=self.classify())
        body = self.calls[0][0]
        self.assertNotIn("diff", body["state"])
        self.assertIn("files", body["state"])

    def test_dash_c_dir_option(self):
        sub = self.repo / "sub"
        sub.mkdir()
        run_git(["init"], sub)
        run_git(["config", "user.email", "d@ylm.co.il"], sub)
        run_git(["config", "user.name", "Test"], sub)
        (sub / "b.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        run_git(["commit", "-m", "init"], sub)
        (sub / "b.txt").write_text("two\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        payload = self.payload("git -C sub commit -m 'x'", cwd=str(self.repo))
        out = self.gate(payload, classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_dash_c_dir_expanded_with_spaces(self):
        sub = self.repo / "dir with space"
        sub.mkdir()
        run_git(["init"], sub)
        run_git(["config", "user.email", "d@ylm.co.il"], sub)
        run_git(["config", "user.name", "Test"], sub)
        (sub / "b.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        run_git(["commit", "-m", "init"], sub)
        (sub / "b.txt").write_text("two\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        payload = self.payload('git -C "dir with space" commit -m x', cwd=str(self.repo))
        out = self.gate(payload, classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_lowercase_c_global_option_matched_but_not_used_as_cwd(self):
        # `git -c user.name=x commit` used to be parsed as -C (a dir) -> wrong cwd / bypass.
        self.stage_change()
        out = self.gate(self.payload("git -c user.name=x commit -m m"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_long_global_option_before_subcommand(self):
        self.stage_change()
        out = self.gate(self.payload("git --no-pager -c a=b commit -m m"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_untracked_new_file_included_in_add_commit(self):
        (self.repo / "new.py").write_text("os.system('rm -rf /')\n", encoding="utf-8")
        out = self.gate(self.payload("git add -A && git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        body = self.calls[0][0]
        self.assertIn("new.py", body["state"]["files"])
        self.assertIn("new.py", body["state"]["diff"])
        self.assertNotIn("rm -rf", body["state"]["diff"])

    def test_untracked_new_file_included_in_commit_all_flag(self):
        (self.repo / "a.txt").write_text("three\n", encoding="utf-8")
        (self.repo / "new.py").write_text("secret = 1\n", encoding="utf-8")
        out = self.gate(self.payload("git commit -am 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        body = self.calls[0][0]
        self.assertIn("new.py", body["state"]["files"])

    def test_untracked_file_found_from_subdirectory(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "new.py").write_text("token = 'x'\n", encoding="utf-8")
        out = self.gate(self.payload("git add -A && git commit -m x", cwd=str(self.repo / "sub")),
                        classify_fn=self.classify())
        self.assertIsNotNone(out)
        body = self.calls[0][0]
        self.assertIn("sub/new.py", body["state"]["files"])
        self.assertNotIn("token", body["state"]["diff"])
        self.assertIn("untracked content omitted", body["state"]["diff"])

    def test_untracked_symlink_does_not_send_target_contents(self):
        outside = Path(self.repo.parent) / "outside-secret.txt"
        outside.write_text("PRIVATE_OUTSIDE_CONTENT", encoding="utf-8")
        try:
            (self.repo / "new-link").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        os.utime(outside, (time.time() - 100, time.time() - 100))
        jev.update_state(jev.session_state_path("sess1", self.state_dir),
                         lambda s: s.update(critic_ts=time.time() - 50))
        self.gate(self.payload("git add -A && git commit -m x"), classify_fn=self.classify())
        self.assertEqual(len(self.calls), 1)
        diff = self.calls[0][0]["state"]["diff"]
        self.assertIn("new-link", diff)
        self.assertNotIn("PRIVATE_OUTSIDE_CONTENT", diff)

    def test_untracked_edit_requires_fresh_risk_check(self):
        new_file = self.repo / "new.py"
        new_file.write_text("first", encoding="utf-8")
        payload = self.payload("git add -A && git commit -m x")
        self.assertIsNotNone(self.gate(payload, classify_fn=self.classify()))
        new_file.write_text("second, longer content", encoding="utf-8")
        self.assertIsNotNone(self.gate(payload, classify_fn=self.classify()))
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("second, longer content", self.calls[1][0]["state"]["diff"])

    def test_unicode_untracked_edit_requires_fresh_risk_check(self):
        new_file = self.repo / "é.py"
        new_file.write_text("first", encoding="utf-8")
        payload = self.payload("git add -A && git commit -m x")
        self.assertIsNotNone(self.gate(payload, classify_fn=self.classify()))
        new_file.write_text("second, longer content", encoding="utf-8")
        self.assertIsNotNone(self.gate(payload, classify_fn=self.classify()))
        self.assertEqual(len(self.calls), 2)
        self.assertIn("é.py", self.calls[1][0]["state"]["files"])

    def test_staged_unicode_filename_is_not_git_quoted(self):
        self.stage_change(name="é.py", content="safe change\n")
        self.assertIsNotNone(self.gate(self.payload("git commit -m x"),
                                       classify_fn=self.classify()))
        self.assertIn("é.py", self.calls[0][0]["state"]["files"])

    def test_git_output_preserves_carriage_returns(self):
        output = b"carriage\rname.py\0"
        completed = subprocess.CompletedProcess(["git"], 0, stdout=output, stderr=b"")
        with mock.patch.object(jev.subprocess, "run", return_value=completed):
            self.assertEqual(jev._run_git(["ls-files"], self.repo), "carriage\rname.py\0")

    @unittest.skipIf(os.name == "nt", "Windows filenames cannot contain carriage returns")
    def test_carriage_return_untracked_edit_requires_fresh_risk_check(self):
        new_file = self.repo / "carriage\rname.py"
        new_file.write_text("first", encoding="utf-8")
        payload = self.payload("git add -A && git commit -m x")
        self.assertIsNotNone(self.gate(payload, classify_fn=self.classify()))
        new_file.write_text("second, longer content", encoding="utf-8")
        self.assertIsNotNone(self.gate(payload, classify_fn=self.classify()))
        self.assertEqual(len(self.calls), 2)
        self.assertIn("carriage\rname.py", self.calls[1][0]["state"]["files"])

    def test_untracked_name_kept_when_diff_is_full(self):
        cfg = {**self.cfg, "max_diff_chars": 10}
        (self.repo / "a.txt").write_text("a long enough change\n", encoding="utf-8")
        (self.repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        out = self.gate(self.payload("git commit -am x"), cfg=cfg, classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertIn("new.py", self.calls[0][0]["state"]["files"])

    def nested_repo_change(self):
        sub = self.repo / "sub"
        sub.mkdir()
        run_git(["init"], sub)
        run_git(["config", "user.email", "d@ylm.co.il"], sub)
        run_git(["config", "user.name", "Test"], sub)
        (sub / "b.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        run_git(["commit", "-m", "init"], sub)
        (sub / "b.txt").write_text("two\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        return sub

    def test_cd_anywhere_before_git_sets_cwd(self):
        sub = self.nested_repo_change()
        for command in ("cd sub && git add -A && git commit -m x",
                        "cd sub; git add b.txt; git commit -m x",
                        'cd "sub" && git commit -m x',
                        f'cd "{sub.as_posix()}" && ls && git commit -m x'):
            self.calls.clear()
            # A fresh session each time: an identical diff in one session is a retry.
            self.gate(self.payload(command, session_id=f"cd{len(command)}"), classify_fn=self.classify())
            self.assertEqual(len(self.calls), 1, command)
            self.assertIn("b.txt", self.calls[0][0]["state"]["files"], command)

    def test_shift_operator_and_here_string_do_not_hide_commit(self):
        self.stage_change()
        for command in ('python3 -c "print(1<<3)" && git commit -m x',
                        'grep foo <<< "bar"; git commit -m x'):
            self.calls.clear()
            self.assertIsNotNone(self.gate(self.payload(command, session_id=f"hs{len(command)}"),
                                           classify_fn=self.classify()), command)

    def test_untracked_overflow_is_never_covered(self):
        with mock.patch.object(jev, "MAX_UNTRACKED", 2):
            for i in range(3):
                (self.repo / f"n{i}.py").write_text("x\n", encoding="utf-8")
            jev.update_state(jev.session_state_path("sess1", self.state_dir),
                             lambda s: s.update(critic_ts=time.time() + 100))
            out = self.gate(self.payload("git add -A && git commit -m x"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls[0][0]["state"]["files"]), 2)

    def test_echoed_git_commit_text_not_matched(self):
        out = self.gate(self.payload('echo "git commit -m fake"'), classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

    def test_heredoc_body_with_git_commit_not_matched(self):
        command = "cat <<'EOF'\nsome text with git commit inside\nEOF\n"
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

    def test_quoted_commit_message_still_matches(self):
        self.stage_change()
        out = self.gate(self.payload('git commit -m "msg with git commit words inside"'),
                         classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_cd_prefix_sets_cwd_without_dash_c(self):
        sub = self.repo / "sub"
        sub.mkdir()
        run_git(["init"], sub)
        run_git(["config", "user.email", "d@ylm.co.il"], sub)
        run_git(["config", "user.name", "Test"], sub)
        (sub / "b.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        run_git(["commit", "-m", "init"], sub)
        (sub / "b.txt").write_text("two\n", encoding="utf-8")
        run_git(["add", "b.txt"], sub)
        payload = self.payload("cd sub && git commit -m 'x'", cwd=str(self.repo))
        out = self.gate(payload, classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_deleted_file_is_not_covered(self):
        (self.repo / "a.txt").unlink()
        run_git(["add", "a.txt"], self.repo)
        state = jev.load_session_state("sess1", self.state_dir)
        state["critic_ts"] = time.time() + 10
        jev.save_session_state("sess1", state, self.state_dir)
        out = self.gate(self.payload("git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(self.logs[-1]["decision"], "deny")

    def test_git_budget_exhaustion_allows(self):
        self.stage_change()
        deadline = time.monotonic() - 1  # already spent
        self.assertIsNone(jev._run_git(["status"], self.repo, deadline=deadline))

    def test_commit_am_uses_head_diff(self):
        (self.repo / "a.txt").write_text("three\n", encoding="utf-8")
        out = self.gate(self.payload("git commit -am 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_add_then_commit_uses_head_diff(self):
        (self.repo / "a.txt").write_text("three\n", encoding="utf-8")
        out = self.gate(self.payload("  git add a.txt && git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_flag_outside_commit_segment_is_ignored(self):
        (self.repo / "a.txt").write_text("three\n", encoding="utf-8")
        out = self.gate(self.payload("ls -al; git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNone(out)  # nothing staged: the unstaged edit is not what gets committed
        self.assertEqual(self.calls, [])

    def test_push_without_upstream_falls_back_or_fails_open(self):
        out = self.gate(self.payload("git push"), classify_fn=self.classify())
        # No origin/main and no upstream configured -> both diff attempts fail -> the
        # unreviewable push is denied once, without a classifier call.
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertEqual(hook["permissionDecisionReason"], jev.UNJUDGED_PUSH_REASON)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.logs[-1]["decision"], self.logs[-1]["reason"]),
                         ("deny", "unjudged_push"))
        # The identical retry proceeds as before: None, no call.
        self.assertIsNone(self.gate(self.payload("git push"), classify_fn=self.classify()))
        self.assertEqual(self.calls, [])
        self.assertEqual((self.logs[-1]["decision"], self.logs[-1]["reason"]),
                         ("override", "unjudged_push"))
        # A different command (or session) is denied again.
        self.assertIsNotNone(self.gate(self.payload("git push origin"), classify_fn=self.classify()))
        self.assertIsNotNone(self.gate(self.payload("git push", session_id="sess2"),
                                       classify_fn=self.classify()))

    def test_unjudged_push_retry_still_gates_its_commits(self):
        self.stage_change()
        command = "git commit -m x && git push"
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNJUDGED_PUSH_REASON)
        self.assertEqual(self.calls, [])
        # The retry reviews the commit's diff as today (as a push operation).
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self.calls), 1)

    def test_unjudged_push_when_git_budget_is_spent(self):
        out = jev.gate(self.payload("git push"), self.cfg, self.classify(), self.logs.append,
                       state_dir=self.state_dir, deadline=time.monotonic() - 1)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNJUDGED_PUSH_REASON)

    def test_powershell_payload_is_gated(self):
        self.stage_change()
        payload = dict(self.payload("git commit -m x"), tool_name="PowerShell")
        out = self.gate(payload, classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(self.calls), 1)
        unparsed = dict(self.payload("& git.exe 'push'"), tool_name="PowerShell")
        self.assertEqual(self.gate(unparsed, classify_fn=self.classify())["hookSpecificOutput"][
            "permissionDecisionReason"], jev.UNPARSED_PUSH_REASON)
        for tool in ("Read", "Edit", None):
            self.assertIsNone(self.gate(dict(payload, tool_name=tool), classify_fn=self.classify()))
        self.assertEqual(len(self.calls), 1)

    def test_looks_like_push(self):
        for command in ('git "push" origin main', 'gi"t" push', "git pu\\sh", 'bash -c "git push"',
                        "sh -c 'git push origin main'", 'eval "git push"', "echo git push | sh",
                        "xargs git push",
                        "python -c \"import subprocess; subprocess.run(['git','push'])\"",
                        "git -c alias.p=push p", "git -c alias.p='!git push' p",
                        "GIT=git; $GIT push", "C:\\Git\\cmd\\git.exe push", "& git.exe push",
                        "git -C repo push", "/usr/libexec/git-core/git-push origin",
                        "git --git-dir x --no-pager push",
                        # A subcommand built at run time, or hidden past escapes/options.
                        "p=push; git $p", "git \\\npush", "git pus$(echo h)",
                        "git $(printf 'pu%s' sh)", "git `echo push`", "git ${_:-push}",
                        "git @('push')", "git -c alias.p=pu\\sh p",
                        "git" + " -c k=v" * 6 + ' "push"', "git" + " -x" * 70 + " push",
                        # Quoted multi-word option values stay one word.
                        "git -C 'a b' push",
                        'git -c "http.extraHeader=Authorization: Bearer t" push',
                        'bash -c "git -C \\"a b\\" push"',
                        '& "C:\\Program Files\\Git\\cmd\\git.exe" push',
                        # A dynamic command word.
                        "g=git; $g push origin main", "x=mygit; $x push",
                        # Aliases set at run time, through the environment or git config.
                        "git -c alias.p=${x:-push} p", "cmd=push; git -c alias.p=$cmd p",
                        "GIT_CONFIG_PARAMETERS=\"'alias.p=push'\" git p",
                        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0=push git p",
                        "git config alias.p push && git p",
                        'git config --global alias.p "!git push" && git p',
                        # Pushes under another first subcommand.
                        "git subtree push --prefix=lib origin main",
                        "git subtree -P lib push origin main", "git send-pack origin main",
                        # Brace expansion, and a brace group.
                        "git pus{h..h} origin main", "git {p..p}ush", "{ git push; }",
                        # A dynamic command word with options before the subcommand.
                        "g=git; $g -C . push origin main", "g=git; $g --no-pager push",
                        '"$GIT_BIN" -c k=v push',
                        # send-pack aliases.
                        "git -c alias.p=send-pack p", "git config alias.p send-pack && git p",
                        # A message is skipped, but a wrapper or substitution is not.
                        'git commit -m "$(git push)"', 'git commit -m x && bash -c -m "git push"',
                        'sh -cm "git push"'):
            self.assertTrue(jev._looks_like_push(command), command)
        for command in ("git stash push", 'git commit -m "fix push bug"', "git status", "ls -la",
                        "npm run build", "git log --oneline", "", "git", "git -C push status",
                        "git log $(git rev-parse HEAD)", "man git-push",
                        "git -c alias.co=checkout co pushfix", "git log --format=%H",
                        "git --git-dir=$D status", "git -c user.email=me@x.org commit -m x",
                        'git -C "a b" status', "{ git status; }", "git subtree split -P lib",
                        "git config alias.co checkout", "git config user.name 'push bot'",
                        "cp -r $SRC/git $DST", "$EDITOR -n file", "$EDITOR -n $f",
                        'git commit -m "docs: explain git push"', "git commit -am'git push'",
                        'git commit --message="git push"', 'git tag -a v1 -m "git push"',
                        'git commit -F "git push"',
                        # Text that names a push without running one.
                        'grep -rn "git push" docs/', "echo git push", "git config --get alias.push"):
            self.assertFalse(jev._looks_like_push(command), command)
        # Every push counts, so one hidden next to a parsed one is still seen.
        for command, count in (("git push", 1), ("cd a && git push", 1),
                               ("git push && git push --tags", 2),
                               ('git push; bash -c "cd ../other && git push --force"', 2),
                               ("git -c alias.p=push p; git push", 2),
                               ("git -C $DIR push", 1), ("git -C $DIR push; git push", 2),
                               ('mkdir -p "a b" && git -C "a b" push origin main; '
                                'bash -c "git push --force origin main"', 2),
                               ('git commit -m "add git push hook" && git push', 1),
                               ('git commit -m "x" && bash -c "git push"', 1)):
            self.assertEqual(jev._push_signals(command), count, command)
        # The hardened scanner misses these, which is what the backstop is for.
        for command in ('git "push" origin main', 'bash -c "git push"',
                        "git -c alias.p=push p"):
            self.assertEqual(jev._scan_targets(command, "/repo"), [], command)

    def test_dequoted_command_word_is_parsed(self):
        for command in ('gi"t" push', "'g'it push"):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"], command)

    def test_unparsed_push_denied_once(self):
        command = 'git "push"'
        out = self.gate(self.payload(command), classify_fn=self.classify())
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertEqual(hook["permissionDecisionReason"], jev.UNPARSED_PUSH_REASON)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.logs[-1]["decision"], self.logs[-1]["reason"]),
                         ("deny", "unparsed_push"))
        # The identical retry overrides that denial only. The push is then gated as a
        # wrapped one in this cwd: with no upstream its range is unknown (denied once
        # more), and after that it is put to the classifier, never passed unasked.
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNJUDGED_PUSH_REASON)
        self.assertEqual([(e["decision"], e["reason"]) for e in self.logs[-2:]],
                         [("override", "unparsed_push"), ("deny", "unjudged_push")])
        self.assertEqual(self.calls, [])
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]["state"]["push"], jev.WRAPPED_PUSH_NOTE)
        self.assertEqual(self.logs[-1]["push_flags"], ["wrapped"])
        self.calls.clear()
        # Another wrapped push is denied on its own.
        self.assertIsNotNone(self.gate(self.payload("eval 'git push'"), classify_fn=self.classify()))
        # Ordinary commands never reach the backstop.
        for command in ("git stash push", "git status", 'git commit -m "fix push bug"'):
            self.assertIsNone(self.gate(self.payload(command), classify_fn=self.classify()), command)
        self.assertEqual(self.calls, [])

    def test_unparsed_push_retry_is_never_a_silent_allow(self):
        # With nothing unpushed in the cwd (or a cwd that is no repository) the retry of
        # a wrapped push had nothing left to judge and passed, whatever it forced.
        self.add_remote()
        branch = self.feature_branch()
        run_git(["reset", "--hard", f"origin/{branch}"], self.repo)
        outside = self.repo.parent / "outside"
        outside.mkdir()

        def reasons(command, session, classify_fn, tool="Bash", cwd=None, attempts=3):
            out = []
            for _ in range(attempts):
                payload = dict(self.payload(command, session_id=session, cwd=cwd), tool_name=tool)
                result = self.gate(payload, classify_fn=classify_fn)
                out.append(result and result["hookSpecificOutput"]["permissionDecisionReason"])
            return out
        forms = (('eval "git push --force origin work"', "Bash"),
                 ('eval "git push origin feature:main"', "Bash"),
                 ("bash -c 'git push --force' x", "Bash"),
                 ("pwsh -Command 'git push --force'", "Bash"),
                 ("git -c alias.pf='push --force' pf", "Bash"),
                 ("git send-pack --force origin work", "Bash"),
                 ("python -c \"import os; os.system('git push --force')\"", "Bash"),
                 # codex argv lists, joined with POSIX quoting (see codex/jev-hook.py)
                 ("bash -lc 'git push --force' x", "Bash"),
                 ("/usr/bin/env bash -lc 'git push --force'", "Bash"),
                 ("bash -c -l 'git push --force'", "Bash"),
                 ("powershell.exe -NoProfile -Command 'git push --force'", "Bash"),
                 ("cmd /c 'git push --force'", "Bash"),
                 ('iex "git push --force"', "PowerShell"),
                 ('Invoke-Expression "git push --force"', "PowerShell"),
                 ('cmd /c "git push --force"', "PowerShell"),
                 ("$a=@('push','--force'); git @a", "PowerShell"))
        for i, (command, tool) in enumerate(forms):
            with self.subTest(command=command):
                # Jev down: denied on every attempt.
                self.calls.clear()
                first, second, third = reasons(command, f"w{i}", self.boom, tool)
                self.assertEqual(first, jev.UNPARSED_PUSH_REASON)
                self.assertIn("fail-closed", second)
                self.assertIn("fail-closed", third)
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.calls[0][0]["state"]["push"], jev.WRAPPED_PUSH_NOTE)
                # Jev up: the retry is judged, and allowed only on its answer.
                self.calls.clear()
                first, second = reasons(command, f"v{i}", self.classify(choice="none"), tool,
                                        attempts=2)
                self.assertEqual(first, jev.UNPARSED_PUSH_REASON)
                self.assertIsNone(second)
                self.assertEqual(len(self.calls), 1)
        # A cwd whose range can't be worked out is no exemption: unjudged, then asked.
        command = f'eval "git -C {self.repo.as_posix()} push --force origin newb:main"'
        self.calls.clear()
        first, second, third, fourth = reasons(command, "o", self.boom, cwd=str(outside),
                                               attempts=4)
        self.assertEqual((first, second), (jev.UNPARSED_PUSH_REASON, jev.UNJUDGED_PUSH_REASON))
        self.assertIn("fail-closed", third)
        self.assertIn("fail-closed", fourth)
        # Next to a parsed push in the same cwd the hidden one is still noted.
        self.calls.clear()
        command = 'git push; bash -c "git push --force"'
        first, second = reasons(command, "p", self.boom, attempts=2)
        self.assertEqual(first, jev.UNPARSED_PUSH_REASON)
        self.assertIn("fail-closed", second)
        self.assertEqual(self.calls[0][0]["state"]["push"], jev.WRAPPED_PUSH_NOTE)

    def test_unparsed_push_retry_still_gates_its_commits(self):
        self.add_remote()
        self.stage_change()
        command = 'git commit -m x; bash -c "git push"'
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)
        self.assertEqual(self.calls, [])
        # The overridden push is still gated, so the operation is a push.
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0]["state"]["operation"], "push")

    def test_push_smuggled_next_to_a_parsed_push_is_denied_once(self):
        command = 'git push; bash -c "cd ../other && git push --force"'
        self.assertEqual(len(jev._scan_targets(command, str(self.repo))), 1)

        def reason():
            out = self.gate(self.payload(command), classify_fn=self.classify())
            return out and out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason(), jev.UNPARSED_PUSH_REASON)
        # The retry reaches the parsed push (no upstream here: denied once as unjudged),
        # and the next identical retry is put to the classifier for the hidden one.
        self.assertEqual(reason(), jev.UNJUDGED_PUSH_REASON)
        self.assertEqual(self.calls, [])
        self.assertIn("This push looks risky", reason())
        self.assertEqual(self.calls[0][0]["state"]["push"], jev.WRAPPED_PUSH_NOTE)
        self.calls.clear()
        # Parsed pushes alone never trigger the backstop.
        for command in ("git push", "cd . && git push", "git push && git push --tags",
                        "git push origin main; git push origin v1"):
            self.assertEqual(reason(), jev.UNJUDGED_PUSH_REASON, command)

    def test_push_hidden_after_a_quoted_cwd_push_is_denied_once(self):
        (self.repo / "a b").mkdir()
        command = ('mkdir -p "a b" && git -C "a b" push origin main; '
                   'bash -c "git push --force origin main"')
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)
        # Each newly caught shape is denied once too, with no parsed push beside it.
        for command in ("g=git; $g push origin main", "git config alias.p push && git p",
                        "git pus{h..h} origin main", "git subtree push --prefix=lib origin main",
                        "cmd=push; git -c alias.p=$cmd p"):
            out = self.gate(self.payload(command), classify_fn=self.classify())
            self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                             jev.UNPARSED_PUSH_REASON, command)
        self.assertEqual(self.calls, [])

    def test_commit_message_mentioning_push_is_not_a_hidden_push(self):
        self.stage_change()
        out = self.gate(self.payload('git commit -m "docs: explain git push"'),
                        classify_fn=self.classify())
        self.assertIn("This commit looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        # Beside a real push only the push's own check applies (no upstream: unjudged).
        out = self.gate(self.payload('git commit -m "add git push hook" && git push'),
                        classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNJUDGED_PUSH_REASON)
        # A dynamic command word with options is still caught.
        out = self.gate(self.payload("g=git; $g -C . push origin main"),
                        classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)

    def test_deny_once_is_per_cwd(self):
        command = 'git "push"'
        other = self.repo.parent / "other"
        other.mkdir()

        def reason(cwd):
            out = self.gate(self.payload(command, cwd=str(cwd)), classify_fn=self.classify())
            return out and out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertEqual(reason(self.repo), jev.UNPARSED_PUSH_REASON)
        # Overridden in this repository only: the same command elsewhere is denied again.
        self.assertEqual(reason(other), jev.UNPARSED_PUSH_REASON)
        # Past that denial each is gated as a push in its own cwd (no upstream: unjudged).
        self.assertEqual(reason(self.repo), jev.UNJUDGED_PUSH_REASON)
        self.assertEqual(reason(other), jev.UNJUDGED_PUSH_REASON)
        self.assertEqual(self.calls, [])

    def test_deny_once_says_when_it_could_not_be_recorded(self):
        command = 'git "push"'
        suffix = (" (This denial could not be recorded, so an identical retry will be "
                  "checked again.)")
        with mock.patch.object(jev, "update_state", side_effect=OSError("locked")):
            for _ in range(2):
                out = self.gate(self.payload(command), classify_fn=self.classify())
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                                 jev.UNPARSED_PUSH_REASON + suffix)
                self.assertEqual(self.logs[-1]["state_error"], "OSError")
        # Recorded normally, the reason has no suffix.
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)
        self.assertNotIn("state_error", self.logs[-1])

    def test_looks_like_push_is_linear(self):
        for command, label in (("git " * 250_000, "git words"),
                               ("git " + "-c x " * 200_000, "value options"),
                               ("git -x " * 150_000, "flag runs"),
                               ("alias." * 150_000, "alias without push"),
                               ('"' * 1_000_000, "quotes"),
                               ("a\\" * 500_000, "backslashes"),
                               ("git " + "$x " * 250_000, "dynamic words"),
                               ("git -x " + "-x " * 300_000, "options past the window"),
                               ("/git-push " * 100_000, "git-push paths"),
                               ("'git' " * 200_000, "quoted git words"),
                               ('"git push" ' * 100_000, "quoted pushes"),
                               ("'" * 1_000_001, "unbalanced quotes"),
                               ('"\\' * 500_000, "escapes in an unbalanced quote"),
                               ("{" * 1_000_000, "braces"),
                               ("git config " * 100_000, "config runs"),
                               ("git subtree " * 100_000, "subtree runs"),
                               ("alias.p=$x " * 100_000, "dynamic aliases"),
                               ("git commit " + '-m "a" ' * 150_000, "messages"),
                               ("git commit -m" + '"a"' * 300_000, "one long message word"),
                               ("$g -x " * 150_000, "dynamic words with options")):
            self._assert_fast(lambda: jev._looks_like_push(command), label)

    def test_classify_raising_is_noop(self):
        self.stage_change()

        def boom(body, key):
            raise TimeoutError("slow")
        out = self.gate(self.payload("git commit -m 'x'"), classify_fn=boom)
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["decision"], "allow")

    def test_feature_disabled_is_noop(self):
        self.stage_change()
        cfg = {**self.cfg, "features": {**self.cfg["features"], "risk_gate": False}}
        out = self.gate(self.payload("git commit -m 'x'"), cfg=cfg, classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

    def test_no_staged_diff_is_noop(self):
        out = self.gate(self.payload("git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])


    # ---- critic re-review fixes ----

    def assert_detected(self, command, cwd=None):
        self.stage_change()
        out = self.gate(self.payload(command, cwd=cwd), classify_fn=self.classify())
        self.assertIsNotNone(out, command)
        self.assertEqual(len(self.calls), 1, command)

    def assert_not_detected(self, command):
        self.stage_change()
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertIsNone(out, command)
        self.assertEqual(self.calls, [], command)

    def test_heredoc_line_tail_commit_detected(self):
        self.assert_detected("python3 - <<EOF && git commit -am x\nprint(1)\nEOF\n")

    def test_heredoc_line_tail_push_detected(self):
        cmd = "cat <<EOF | tee out.txt; git push\nbody\nEOF\n"
        match = jev.GIT_COMMAND_RE.search(jev._strip_heredocs_and_quotes(cmd))
        self.assertIsNotNone(match)
        self.assertEqual(match.group(2), "push")

    def test_heredoc_body_commit_not_detected(self):
        self.assert_not_detected("cat <<EOF > notes.txt\ngit commit -m y\nEOF\n")

    def test_heredoc_terminator_with_dot_and_dash_not_detected(self):
        # `.`/`-` in the terminator word (e.g. `EOF-1`, `END.MARKER`) must still be
        # recognized so the heredoc body (containing a fake `git commit`) is stripped.
        self.assert_not_detected("cat <<EOF-1\ngit commit -m y\nEOF-1\n")
        self.assert_not_detected("cat <<END.MARKER\ngit commit -m y\nEND.MARKER\n")

    def test_heredoc_terminator_with_crlf_stripped(self):
        self.assert_not_detected("cat <<EOF\r\ngit commit -m y\r\nEOF\r\n")

    def test_heredoc_dash_form_allows_indented_terminator(self):
        self.assert_not_detected("cat <<-EOF\ngit commit -m y\n\tEOF\n")

    def test_heredoc_plain_form_requires_column_zero_terminator(self):
        # Without `<<-`, bash requires the terminator at column 0; an indented "EOF"
        # does not end the heredoc, so everything up to the real (column-0)
        # terminator -- including the fake `git commit` in between -- is still body
        # text, not a real command.
        self.assert_not_detected("cat <<EOF\ngit commit -m y\n\tEOF\ngit commit -m x\nEOF\n")

    def test_echo_dash_c_quoted_not_detected(self):
        self.assert_not_detected('echo -c "x; git commit -m y"')

    def test_git_dash_c_quoted_still_detected(self):
        self.assert_detected('git -c user.name="Foo Bar" commit -m x')

    def test_git_dash_C_quoted_dir(self):
        spaced = self.repo / "dir with space"
        spaced.mkdir()
        cmd = jev._strip_heredocs_and_quotes('git -C "dir with space" commit -m x')
        match = jev.GIT_COMMAND_RE.search(cmd)
        self.assertIsNotNone(match)
        self.assertEqual(jev._dash_c_dir(match.group(1)), "dir with space")

    def test_env_prefix_detected(self):
        self.assert_detected("GIT_EDITOR=true git commit -m x")

    def test_multiple_env_prefixes_detected(self):
        cmd = "A=1 B=2 git push"
        match = jev.GIT_COMMAND_RE.search(jev._strip_heredocs_and_quotes(cmd))
        self.assertIsNotNone(match)
        self.assertEqual(match.group(2), "push")

    def test_space_separated_work_tree_detected(self):
        self.assert_detected(f"git --work-tree {self.repo} commit -m x")

    def test_subshell_cd_sets_cwd(self):
        sub = self.repo / "sub"
        sub.mkdir()
        cmd = "(cd sub && git commit -m x)"
        match = jev.GIT_COMMAND_RE.search(jev._strip_heredocs_and_quotes(cmd))
        self.assertIsNotNone(match)
        self.assertEqual(jev._cd_prefix_dir(cmd, match.start()), "sub")

    def test_names_failure_is_not_covered(self):
        self.stage_change()
        state = jev.load_session_state("sess1", self.state_dir)
        state["critic_ts"] = time.time() + 1000
        jev.save_session_state("sess1", state, self.state_dir)
        with mock.patch.object(jev, "_diff_names", return_value=None):
            out = self.gate(self.payload("git commit -m 'x'"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertNotEqual(self.logs[-1]["decision"], "covered")


    def test_repeated_value_options_do_not_backtrack(self):
        cmd = "git" + " --work-tree x" * 40 + " --git-dir y" * 40
        start = time.monotonic()
        self.assertIsNone(jev.GIT_COMMAND_RE.search(cmd))
        self.assertIsNone(jev.GIT_COMMAND_RE.search(jev._strip_heredocs_and_quotes(cmd + ' "a"' * 20)))
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertIsNotNone(jev.GIT_COMMAND_RE.search("git --work-tree x --git-dir=y --no-pager commit"))

    def _assert_fast(self, scan, label):
        # Best of 3 runs, so a CPU-load spike on a shared CI runner doesn't fail one.
        elapsed = []
        for _ in range(3):
            start = time.monotonic()
            scan()
            elapsed.append(time.monotonic() - start)
            if elapsed[-1] < 2.0:
                break
        self.assertLess(min(elapsed), 2.0, f"{label}: {', '.join(f'{e:.2f}s' for e in elapsed)}")

    def _assert_scans_fast(self, command, label):
        self._assert_fast(lambda: list(jev.GIT_COMMAND_RE.finditer(
            jev._strip_heredocs_and_quotes(command))), label)

    def test_many_quoted_args_scan_fast(self):
        # Each quote used to re-join the whole scanned-so-far prefix and rescan it
        # from scratch, making this O(n^2); 20000 quotes previously took ~49s.
        self._assert_scans_fast("'a' ;" * 20000, "single-quoted")

    def test_many_double_quoted_args_scan_fast(self):
        self._assert_scans_fast('"a" ' * 20000, "double-quoted")

    def test_many_separators_scan_fast(self):
        # Exercises _GIT's leading path-segment group against a long run of
        # separator-heavy, non-matching text.
        self._assert_scans_fast(";/" * 20000, "separator-heavy")

    def test_padded_commands_scan_fast(self):
        # Each used to take 5-20s, past the hook timeout (which fails open).
        for command, label in (("\n" * 20000, "newline run"),
                               ("cat <<A\n" * 20000, "unterminated heredocs"),
                               ("a=;" * 20000, "empty env assignments"),
                               ("a=b\n" * 25000 + "git push", "env assignment lines"),
                               ("sudo -n\n" * 12500, "wrapper lines"),
                               ("then\n" * 20000, "keyword lines"),
                               ("".join(f"cat <<W{i}\n" for i in range(10000)),
                                "distinct unterminated heredocs")):
            self._assert_scans_fast(command, label)
            self._assert_fast(lambda: jev._scan_targets(command, "/repo"), label)
        start = time.monotonic()
        targets = jev._scan_targets("git commit -m x;" * 5000 + "git -C other push", "/repo")
        self.assertLess(time.monotonic() - start, 2.0)
        # Every match is resolved: the last push keeps its own directory.
        self.assertIn(("push", os.path.join("/repo", "other"), False, None), targets)
        # Repeated quoted global options once backtracked exponentially (25 s at 26).
        for n in (30, 60):
            for command in ("git " + '-C "a" ' * n + "status; git push --force",
                            "git " + "-c 'x=y' " * n + "status; git push --force",
                            "git -c x=" + "a" * 600 + ' -C "s"' * n + " status; git push --force"):
                start = time.monotonic()
                ops = [t[0] for t in jev._scan_targets(command, "/repo")]
                self.assertLess(time.monotonic() - start, 2.0, command[:40])
                self.assertEqual(ops, ["push"], command[:40])

    def test_substitution_env_value_detected(self):
        for command, op in (("FOO=$(date) git push", "push"),
                            ("X=$((1+2)) git commit -m x", "commit")):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], [op], command)

    def test_quoted_dash_c_survives_long_prefix(self):
        # A long -c/env value trims the start of the git segment out of the scanner's
        # window; the quoted -C value must still be kept so the push stays visible.
        for command in ('git -c x=' + "a" * 600 + ' -C "sub dir" push origin main',
                        'FOO=' + "a" * 600 + ' git -C "sub" push'):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"])

    def test_quoted_long_option_value_keeps_subcommand(self):
        # A dropped quoted value let `--git-dir` take the subcommand as its value.
        for command, op in (('git --git-dir "x" push', "push"),
                            ('git --git-dir="x" push', "push"),
                            ("git --work-tree 'w' commit -m x", "commit"),
                            ('git -c x=' + "a" * 600 + ' --git-dir "d" push', "push"),
                            ('git --git-dir="C:/My Repos/x/.git" push', "push"),
                            ("git --work-tree='a b' commit -m x", "commit"),
                            ('git --exec-path="a b" push', "push")):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], [op], command)

    def test_option_value_with_cd_line_keeps_cwd(self):
        # Only -C and cd values are read back; a newline-and-cd inside another option's
        # quoted value must not move the later push to that directory.
        for opt in ('--git-dir "', '--work-tree="', '-c "'):
            command = "git " + opt + '\ncd sub\n" status; git push'
            self.assertEqual(jev._scan_targets(command, "/repo"), [("push", "/repo", False, None)], command)

    def test_arithmetic_shift_is_not_a_heredoc(self):
        command = "echo $((1<<3))\ngit push --force\n3\n"
        self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"])
        # The `$((` opener far behind the `<<` (a long blank or newline run) is still
        # tracked, so the push after it is not hidden as a heredoc body.
        for pad in (" " * 600, "\n" * 600):
            command = "x=$((1" + pad + "<<3\n)); git push --force\n3\n"
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"])
        # Nested parens inside the arithmetic keep it open until its own `))`.
        command = "echo $(( (1+2) <<3 ))\ngit push\n3\n"
        self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"])
        # The closers of nested `$(`/`(` inside the arithmetic don't end it early, a
        # heredoc inside a `$( )` nested in it is still one, and `$[ ]` is arithmetic.
        for command in ("echo $(( $(echo $(echo 1))<<3 ))\ngit push --force\n3\n",
                        "echo $(( $( (echo 1))<<3 ))\ngit push --force\n3\n",
                        "x=$(( $(cat <<EOF | wc -c\n))\nEOF\n) << 3 )); echo x=$x\ngit push\n3\n",
                        "echo $[1<<3]\ngit push\n3\n"):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"],
                             command)
        # A lone `)` (a `case` pattern), a `$$(`/`\$(` and a subscript `]` inside the
        # arithmetic don't end it early either.
        for command in ("(( $( case 1 in 1) echo 1;; esac ) <<3 ))\ngit push\n3\n",
                        "echo $(( $$(1<<3) ))\ngit push\n3\n",
                        "echo $(( \\$(1<<3) ))\ngit push\n3\n",
                        "a=(1 2); echo $[ a[1]<<3 ]\ngit push\n3\n"):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"],
                             command)
        # A real heredoc after a closed arithmetic still hides its body.
        command = "echo $((1<<3)); cat <<EOF\ngit push\nEOF\n"
        self.assertEqual(jev._scan_targets(command, "/repo"), [])
        # A stray quote in a heredoc body taken for a shift would pair with a quote on
        # a later line and hide the command between them; the other reading catches it.
        for command in ("echo $((1<<3)); cat <<EOF\ndon't\nEOF\ngit push -f 'x'\n",
                        "x=$((1<<3)); cat <<'EOF' > n\nDon't\nEOF\ngit commit -m 'u n'\n",
                        "n=$((1<<3)); cat <<EOF\nsay \"hi\nEOF\ngit push origin \"main\"\n",
                        "echo $[1<<3]; cat <<EOF\ndon't\nEOF\ngit push -f 'x'\n",
                        "((x=1)); cat <<EOF\nit's\nEOF\ngit push -f 'y'\n",
                        "echo $(( ( 1 ))\ncat <<EOF\ndon't\nEOF\ngit push -f 'x'\n"):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")][-1:],
                             ["commit" if "commit" in command else "push"], command)

    def _ops(self, command):
        return [t[0] for t in jev._scan_targets(command, "/repo")]

    def test_issue_39_detections(self):
        for command, op in (
                # A heredoc inside a `$( )` nested in `$(( ))` is still a heredoc.
                ("x=$(( $(cat <<EOF | wc -c\ndon't\nEOF\n) ))\ngit push -f 'x'\n", "push"),
                # A case pattern inside that `$( )` doesn't close it early.
                ("echo $(( $(case x in a) echo 1;; esac) <<3 ))\ngit push\n3\n", "push"),
                # `((1) …)` is nested subshells: the no-arithmetic reading catches it.
                ("((1) ; cat <<EOF\n\"\nEOF\ngit push \"x\")\n", "push"),
                # A backslash-newline splitting an operator.
                ("cat <\\\n<EOF\n\"\nEOF\ngit push \"x\"\n", "push"),
                ("cat <<\\\nEOF\n\"\nEOF\ngit push \"x\"\n", "push"),
                ("cat <\\\n<\\\nEOF\n\"\nEOF\ngit push \"x\"\n", "push"),
                ("echo $\\\n(( $(cat <<EOF | wc -c\ndon't\nEOF\n) ))\ngit push -f 'x'\n", "push"),
                ("(\\\n(1<<3))\ngit push\n3\n", "push"),
                # Quoting and option parsing.
                ('git -C "a\\" b" push', "push"),
                ("git --git-dir -x push", "push"),
                ("GIT.EXE commit -m x", "commit"),
                ("Git push", "push"),
                ("/opt/{x}/git push", "push"),
                ('"/opt/git(1)/bin/git" push', "push"),
                ("'git' push", "push"),
                ('"C:\\Program Files\\Git\\cmd\\git.exe" commit -m x', "commit"),
                ("X=a\\ b git push", "push"),
                ('git -c "x; git push" commit -m y', "commit")):
            self.assertEqual(self._ops(command), [op], command)
        # `X=/usr/bin/git push` runs `push`, not git.
        for command in ("X=/usr/bin/git push", 'echo "git" push'):
            self.assertEqual(self._ops(command), [], command)

    def test_issue_39_cwd(self):
        j = os.path.join
        for command, cwd in (
                ('cd -- "dir" && git push', j("/repo", "dir")),
                ('cd -P "dir" && git push', j("/repo", "dir")),
                ("(cd build && make) && git push", "/repo"),
                ("x=$(cd sub && pwd); git push", "/repo"),
                ("X=`cd sub`; git push", "/repo"),
                ("(cd sub && git push)", j("/repo", "sub")),
                ('git -C "\ncd sub\n" status; git push', "/repo"),
                ('git --foo-C "\ncd sub\n" status; git push', "/repo"),
                ('git -c x=' + "a" * 600 + ' --foo-c "\ncd sub\n" status; git push', "/repo"),
                ('git -c x=' + "a" * 600 + ' --foo-C "\ncd sub\n" status; git push', "/repo"),
                ("git -C $JEV_UNSET_39 push", "/repo"),
                ('cd "$JEV_UNSET_39" && git push', "/repo"),
                ("git -C a -C b push", j("/repo", "a", "b"))):
            targets = jev._scan_targets(command, "/repo")
            self.assertEqual([t[1] for t in targets], [cwd], command)

    def test_issue_39_git_dir_and_work_tree(self):
        j = os.path.join
        for command, target in (
                ('git --git-dir="other/.git" push',
                 ("push", "/repo", False, ("--git-dir", j("/repo", "other/.git")))),
                ("git --git-dir other/.git --work-tree 'o t' push",
                 ("push", j("/repo", "o t"), False,
                  ("--git-dir", j("/repo", "other/.git"), "--work-tree", j("/repo", "o t")))),
                ("git -C sub --git-dir=/opt/x.git commit -m y",
                 ("commit", j("/repo", "sub"), False, ("--git-dir", "/opt/x.git"))),
                # A --work-tree alone keeps the cwd (repository discovery) but is passed on.
                ("git --work-tree w commit -m y",
                 ("commit", "/repo", False, ("--work-tree", j("/repo", "w"))))):
            self.assertEqual(jev._scan_targets(command, "/repo"), [target], command)
        # The diff is taken from the named repository, not the payload cwd's.
        repo = self.repo.as_posix()
        self.assert_detected(f'git --git-dir="{repo}/.git" --work-tree="{repo}" commit -m x',
                             cwd=tempfile.gettempdir())

    def test_issue_39_work_tree_threaded_to_git(self):
        with mock.patch.object(jev.subprocess, "run") as run, \
                mock.patch.object(jev, "resolve_executable", return_value="/usr/bin/git"):
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            jev._run_git(["status"], "/repo", git_opts=("--work-tree", "/w"))
        self.assertEqual(run.call_args[0][0], ["/usr/bin/git", "--work-tree", "/w", "status"])
        # Without a git on PATH (never the cwd's), the call fails like a failed git.
        with mock.patch.object(jev.subprocess, "run") as run, \
                mock.patch.object(jev, "resolve_executable", return_value=None) as resolve:
            self.assertIsNone(jev._run_git(["status"], "/repo"))
        resolve.assert_called_once_with("git")
        run.assert_not_called()
        # `commit -a` with only a --work-tree diffs that work tree (the repository is
        # still discovered from the cwd), not the cwd's own clean checkout.
        work = self.repo.parent / "work"
        work.mkdir()
        (work / "a.txt").write_text("changed\n", encoding="utf-8")
        out = self.gate(self.payload(f'git --work-tree="{work.as_posix()}" commit -am x'),
                        classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.calls), 1)

    def test_issue_39_case_pattern_in_subshell_keeps_cwd(self):
        # A case pattern's `)` inside a subshell doesn't restore the cwd from before it.
        j = os.path.join
        other = j("/repo", "other")
        self.assertEqual([t[:2] for t in jev._scan_targets(
            "(cd other && case $x in y) git push;; esac)", "/repo")], [("push", other)])
        # Nested cases and a subshell after `esac`: checked on the normal reading (the
        # union with the no-restore reading in _scan_targets adds spurious cwds).
        command = ("(cd other && case $a in x) case $b in y) true;; esac; git push;; esac; "
                   "(cd sub && make); git commit -m m); git push")
        cd_index = jev._cd_dirs(command)
        self.assertEqual([jev._git_target(command, m, "/repo", None, cd_index)[:2]
                          for m in jev.GIT_COMMAND_RE.finditer(command)],
                         [("push", other), ("commit", other), ("push", "/repo")])
        targets = [t[:2] for t in jev._scan_targets(command, "/repo")]
        for target in (("push", other), ("commit", other), ("push", "/repo")):
            self.assertIn(target, targets)
        self.assertEqual([t[:2] for t in jev._scan_targets(
            '(cd other && case "$x" in y) git push;; esac)', "/repo")], [("push", other)])

    def _normal_reading(self, command):
        # (op, cwd) per git match with the normal cd reading only (no no-restore union).
        stripped = jev._strip_heredocs_and_quotes(command)
        cd_index = jev._cd_dirs(stripped)
        return [jev._git_target(stripped, m, "/repo", None, cd_index)[:2]
                for m in jev.GIT_COMMAND_RE.finditer(stripped)]

    def test_issue_39_case_word_with_blanks_keeps_cwd(self):
        # A case word with blanks inside a `$( )`/`${ }`/backtick substitution opens a
        # case, so its pattern `)` doesn't close the subshell; with a `case` in the
        # command, the reading where no `)` restores the cwd is added as well.
        j = os.path.join
        other = ("push", j("/repo", "other"))
        for command in ("(cd other && case $(uname -s) in Linux) git push;; esac)",
                        "(cd other && case ${x:-a b} in y) git push;; esac)",
                        "(cd other && case $(f $(g)) in y) git push;; esac)",
                        "(cd other && case `uname -s` in y) git push;; esac)"):
            self.assertEqual(self._normal_reading(command), [other], command)
            self.assertIn(other, [t[:2] for t in jev._scan_targets(command, "/repo")], command)
        command = "(cd a; X=$(cd b); case $(uname -s) in p) git push;; esac)"
        self.assertEqual(self._normal_reading(command), [("push", j("/repo", "a"))])
        self.assertIn(("push", j("/repo", "a")), [t[:2] for t in jev._scan_targets(command, "/repo")])

    def test_issue_39_esac_right_after_pattern(self):
        # `p)esac` closes the case, so the `)` after `cd b` ends the subshell.
        command = "(cd a; case x in p)esac; cd b); git push"
        self.assertEqual(self._normal_reading(command), [("push", "/repo")])
        self.assertIn(("push", "/repo"), [t[:2] for t in jev._scan_targets(command, "/repo")])

    def test_issue_39_case_branch_cd(self):
        # A case pattern's `)` splits nothing, so `pattern) cd dir` moves nothing in the
        # normal reading (as before #39); the cwd before the case is always a target,
        # and the branch's own directory is one too (the branch may run).
        j = os.path.join
        for command, before, branch in (
                ('case "$1" in deploy) cd ../other;; esac\ngit push', "/repo", j("/repo", "../other")),
                ("case $x in a) cd sub;; esac; git push", "/repo", j("/repo", "sub")),
                ("cd a; case $x in a) cd b;; b) cd c;; esac; git push", j("/repo", "a"), None),
                ("case $x in\n  a) cd sub ;;\nesac\ngit push", "/repo", j("/repo", "sub")),
                ("case x in a) cd sub; git push;; esac", "/repo", j("/repo", "sub"))):
            self.assertEqual(self._normal_reading(command), [("push", before)], command)
            targets = [t[:2] for t in jev._scan_targets(command, "/repo")]
            self.assertIn(("push", before), targets, command)
            if branch:
                self.assertIn(("push", branch), targets, command)
        # A cd before the case still counts in every reading.
        self.assertEqual([t[:2] for t in jev._scan_targets(
            "(cd other && case $x in y) git push;; esac)", "/repo")], [("push", j("/repo", "other"))])

    def test_issue_39_unparsed_case_word_branch_cd(self):
        # A case word _CASE_RE rejects opens no case frame, so the branch cd counts in
        # the normal reading; the case_cds=False reading still ignores it (any `case`
        # word up to its `esac`), so the cwd before the case is a target.
        # Inside a subshell, such a case's pattern `)` doesn't close the subshell in that
        # reading, so a cd after `esac` stays inside it.
        for command in ("case $(echo $(echo $(echo x))) in a) cd sub;; esac; git push",
                        "case $(( (1) )) in 2) cd sub;; esac; git push",
                        "( case $(f $(g $(h))) in x) git commit;; esac ; cd sub )\ngit push",
                        "( case $(f $(g $(h))) in x) cd a;; esac ; cd sub )\ngit push",
                        "( case $(a $(b $(c))) in x) true;; esac ; cd sub ); git push",
                        "(cd a; case $(f $(g $(h))) in x) true;; esac; cd sub); git push"):
            self.assertIn(("push", "/repo"), [t[:2] for t in jev._scan_targets(command, "/repo")],
                          command)
        # `$(( ))` is a case word, so the normal reading gets it right too.
        self.assertEqual(self._normal_reading("case $(( (1) )) in 2) cd sub;; esac; git push"),
                         [("push", "/repo")])
        self.assertTrue(jev._CASE_RE.match("case $((1+2)) in"))

    def test_issue_39_many_git_commands_scan_fast(self):
        # Each match used to copy the rest of the command (7 s / 22 s at 64000).
        # (A kept quoted -C value costs more per command, hence fewer of those.)
        for unit, count in (("git push; ", 32000), ('git -C "a" push; ', 16000),
                            ("git commit -am x; ", 16000)):
            start = time.monotonic()
            targets = jev._scan_targets(unit * count, "/repo")
            self.assertLess(time.monotonic() - start, 2.0, unit)
            self.assertEqual(len(targets), 1, unit)
        self.assertTrue(jev._scan_targets("git commit -am x; " * 3, "/repo")[0][2])
        self.assertFalse(jev._scan_targets("git commit -m x; git commit -a", "/repo")[0][2])

    def test_issue_39_keep_precheck_and_option_split(self):
        # _may_keep only skips tails none of the keep regexes match.
        for tail in ("git -C ", "git -c ", "x git --git-dir=", "x git --work-tree  ", "cd ",
                     ";cd -- ", "(cd -P\t", "x" + "a" * 600 + " -C ", "git --foo-C ", "echo ;",
                     "a b", "", "  ", "git -Cx ", "cd\n", "${${--foo-C  ", "x--git-dir="):
            if (jev._GIT_DASH_C_PREFIX_RE.search(tail) or jev._CD_PREFIX_RE.search(tail)
                    or jev._LOOSE_KEEP_RE.search(tail)):
                self.assertTrue(jev._may_keep(tail), repr(tail))
        self.assertFalse(jev._may_keep("echo 'a' ;"))
        # The unquoted fast path splits on shlex's blanks only.
        for segment in (" -C a\x0bb", " -C a\xa0b --git-dir=x\x1cy", " -C a\r\n-C\tb"):
            self.assertEqual(jev._global_opts(segment),
                             jev._global_opts(segment + " -c 'z'"), repr(segment))

    def test_issue_39_esac_before_redirection_or_after_ampersand(self):
        # The `esac` closes the case, so the `)` after it ends the subshell and the
        # later push runs in the payload cwd.
        for command in ("(cd other && case x in x) true;; esac>/dev/null); git push",
                        "(cd other && case x in x) true;; esac<x); git push",
                        "(cd other && case x in x) true;; esac<<EOF\nb\nEOF\n); git push",
                        "(cd other && case x in x) true &esac); git push"):
            stripped = jev._strip_heredocs_and_quotes(command)
            match = list(jev.GIT_COMMAND_RE.finditer(stripped))[-1]
            self.assertIsNone(jev._cd_prefix_dir(stripped, match.start()), command)
            self.assertIn(("push", "/repo"), [t[:2] for t in jev._scan_targets(command, "/repo")],
                          command)

    def test_issue_39_long_dash_c_chain(self):
        # -C accumulates like cd and is capped the same way (quadratic before: 2.5 s).
        j = os.path.join
        command = "git " + "-C a " * 160000 + "push"
        targets = []
        self._assert_fast(lambda: targets.append(jev._scan_targets(command, "/repo")), "long -C chain")
        self.assertEqual([t[:2] for t in targets[-1]], [("push", "/repo")])
        long_chain = "git " + "-C a " * 3000
        for command, cwd in ((long_chain + "-C b push", "/repo"),
                             (long_chain + "-C /opt/x -C y push", j("/repo", j("/opt/x", "y"))),
                             ("cd sub; " + long_chain + "push", j("/repo", "sub"))):
            self.assertEqual([t[1] for t in jev._scan_targets(command, "/repo")], [cwd],
                             command[-30:])

    def test_issue_39_many_distinct_targets_scan_fast(self):
        # The target dedupe was a list scan per match (6.6 s at 20000).
        command = "".join(f"git -C d{i} push;" for i in range(20000))
        start = time.monotonic()
        targets = jev._scan_targets(command, "/repo")
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(len(targets), 20000)
        self.assertEqual(targets[-1][:2], ("push", os.path.join("/repo", "d19999")))

    def test_issue_39_case_needs_word_and_in(self):
        # `case` inside `$( )` without `in` opens no case statement, so its `)` closes
        # the substitution and the `<<` after it is a shift, not a heredoc.
        for command in ("echo $[ $(echo case x) + (1<<3) ]\ngit push\n3\n",
                        "echo $[ $( echo case x ) + (1<<3) ]\ngit push\n3\n"):
            self.assertEqual(self._ops(command), ["push"], command)

    def test_issue_39_long_cd_chain(self):
        # Accumulating `cd a; ` * N was quadratic (19.6 s / 6.4 GB at 80000); past
        # MAX_CD_PATH_CHARS the cds are dropped (payload cwd) until an absolute cd.
        start = time.monotonic()
        targets = jev._scan_targets("cd a; " * 80000 + "git push", "/repo")
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual([t[:2] for t in targets], [("push", "/repo")])
        long_chain = "cd a; " * 3000
        self.assertEqual([t[1] for t in jev._scan_targets(long_chain + "cd b; git push", "/repo")],
                         ["/repo"])
        self.assertEqual([t[1] for t in jev._scan_targets(long_chain + "cd /opt/x; cd y; git push",
                                                          "/repo")],
                         [os.path.join("/repo", os.path.join("/opt/x", "y"))])
        # A subshell restores the capped state it started from.
        self.assertEqual([t[1] for t in jev._scan_targets("cd sub; (" + long_chain + "); git push",
                                                          "/repo")],
                         [os.path.join("/repo", "sub")])

    def test_issue_39_git_path_with_equals(self):
        self.assertEqual(self._ops("/opt/a=b/git push"), ["push"])
        self.assertEqual(self._ops("FOO=bar git push"), ["push"])
        for command in ("X=/usr/bin/git push", "X=a/git push", "x_1=/opt/a=b/git push"):
            self.assertEqual(self._ops(command), [], command)

    def test_issue_39_constructs_scan_fast(self):
        for command in ("case x in a) " * 5000 + "git push", "esac " * 20000,
                        "`" * 20001 + "git push", "(" * 10000 + ")" * 10000,
                        "$(( " + "(" * 10000 + "<<3\n", "<" + "\\\n" * 20000 + "x",
                        "<<" + "\\\n" * 20000 + "E", 'git -C "' + '\\"' * 20000 + '" push',
                        "git" + " --git-dir -x" * 5000 + " push", '"git" ;' * 10000,
                        "x=" + "\\ " * 20000 + " git push", '"' + "'\\\"'" * 5000 + ";cd a" * 2000,
                        "cd -- " * 5000 + '"d"' * 5000, "case " * 20000, 'case "' * 20000,
                        "case x" * 20000, "(case x " * 10000 + "in", "a=" * 20000 + "/git push",
                        "/=" * 20000 + " push", "(cd a && case x in y) " * 5000,
                        "case $(" * 20000, "case ${" * 20000, "case `" * 20000,
                        "(case $(x " * 10000, "{case ${x " * 10000, "case $((x " * 10000,
                        ";case `x " * 10000, "case $(x)$(x)$(x)" * 5000,
                        "case $(( (" * 10000, "case $((x))$((x))" * 5000,
                        "case x in a) cd b;; " * 5000 + "git push"):
            start = time.monotonic()
            jev._scan_targets(command, "/repo")
            self.assertLess(time.monotonic() - start, 2.0, command[:30])

    def _assert_cwds_among(self, command, op, *cwds):
        targets = [t[:2] for t in jev._scan_targets(command, "/repo")]
        for cwd in cwds:
            self.assertIn((op, cwd), targets, command)

    def test_issue_57_cd_after_or(self):
        # bash: `cd q || cd sub` stays in q when q exists, and a `git add .` that
        # succeeds skips the cd after its `||`; the cd's own directory stays a target.
        j = os.path.join
        self._assert_cwds_among("cd q || cd sub && git commit", "commit",
                                j("/repo", "q"), j("/repo", "q", "sub"))
        self._assert_cwds_among('git add . || cd -- "d e" && ( git push )', "push",
                                "/repo", j("/repo", "d e"))
        # A blank segment after the `||` keeps it (bash: /repo/a).
        self._assert_cwds_among("cd a ||\n cd b; git push", "push", j("/repo", "a"))
        # The cd right before a `||` failed (bash, with no `nope` dir).
        for command, op, cwd in (("cd nope || cd sub && git commit", "commit", j("/repo", "sub")),
                                 ("cd a && cd nope || cd b; git push", "push", j("/repo", "a", "b")),
                                 ("cd nope || { cd b; git push; }", "push", j("/repo", "b"))):
            self._assert_cwds_among(command, op, cwd)

    def test_issue_57_case_branches(self):
        # Each branch's end cwd (bash: `case x in x) cd sub;; …` leaves /repo/sub), the cwd
        # before the case, a `;&` fall-through, and two cases in sequence (bash: x/y).
        j = os.path.join
        for command, op, cwds in (
                ("case x in x) cd sub;; y) cd a;; esac; git commit", "commit",
                 ("/repo", j("/repo", "sub"), j("/repo", "a"))),
                ("case a in a) cd x;; esac\ncase b in b) cd y;; esac; git push", "push",
                 ("/repo", j("/repo", "x", "y"), j("/repo", "y"))),
                ("case a in a) cd x;& b) cd y;; esac; git push", "push",
                 ("/repo", j("/repo", "x", "y"), j("/repo", "y"))),
                ("case a in a) cd x;;& b) cd y;; esac; git push", "push",
                 (j("/repo", "x", "y"), j("/repo", "x"))),
                ("case x in x) cd sub\nesac; git push", "push", ("/repo", j("/repo", "sub"))),
                ("cd w; case x in x) case y in y) cd a;; esac; cd b;; esac; git push", "push",
                 (j("/repo", "w"), j("/repo", "w", "a", "b"), j("/repo", "w", "b"))),
                ("(case x in x) cd sub;; esac); git push", "push", ("/repo",))):
            self._assert_cwds_among(command, op, *cwds)
        # Subshells restore while branch cds count (bash: /repo/d e when $y matches).
        command = ('( cd -P "d e" && git add . )\ncase "$y" in a|b) cd "d e";; esac; '
                   "git commit --all")
        targets = jev._scan_targets(command, "/repo")
        for cwd in ("/repo", j("/repo", "d e")):
            self.assertIn(("commit", cwd, True, None), targets)
        # The branch ends kept per case are capped, the earliest first.
        command = ("case x in " + "".join(f"p{i}) cd d{i};; " for i in range(40))
                   + "esac; git push")
        cwds = [t[1] for t in jev._scan_targets(command, "/repo")]
        for cwd in ["/repo"] + [j("/repo", f"d{i}") for i in range(jev.MAX_CASE_BRANCHES - 1)]:
            self.assertIn(cwd, cwds)
        self.assertNotIn(j("/repo", "d39"), cwds)

    def test_issue_57_escaped_blank_in_option_value(self):
        j = os.path.join
        for command, op, cwd in (("git -C a\\ b push", "push", j("/repo", "a b")),
                                 ("git -C a\\ b -C c\\\\d commit -m x", "commit",
                                  j("/repo", "a b", "c\\d")),
                                 ("git -c x=a\\ b --foo=c\\ d push", "push", "/repo"),
                                 ('git -C "a"\\ b push', "push", j("/repo", "a b"))):
            self.assertEqual([t[:2] for t in jev._scan_targets(command, "/repo")], [(op, cwd)],
                             command)
        # `a\\` is a whole value, so `b` is the subcommand; a backslash-newline still
        # ends the value as before.
        self.assertEqual(self._ops("git -C a\\\\ b push"), [])
        self.assertTrue(jev.GIT_COMMAND_RE.search("git -C a\\\npush"))

    def test_issue_57_all_flag_search_is_shared(self):
        # _all_args agrees with each match's own segment search.
        for command in ("git commit -a (git commit (git commit -m x", "git commit-a",
                        "git commit (git push -a", "git commit -m x; git commit --all",
                        "git commit\tgit commit -am x | git push", "(git commit -a(git commit)"):
            matches = list(jev.GIT_COMMAND_RE.finditer(command))
            self.assertTrue(matches, command)
            expected = []
            for m in matches:
                end = jev._SEGMENT_END_RE.search(command, m.end())
                expected.append(bool(jev.ALL_FLAG_RE.search(
                    command[m.end():end.start() if end else len(command)])))
            self.assertEqual(jev._all_args(command, matches), expected, command)

    def test_issue_57_constructs_scan_fast(self):
        # The first was quadratic (6.5 s at 20000).
        for command, label in (("git commit -a (" * 20000, "commits without separators"),
                               ("cd q || cd sub && " * 10000 + "git push", "cds after ||"),
                               ("case x in a) cd a;; " * 10000 + "esac; git push", "case branches"),
                               ("git -C a\\ b " * 10000 + "push", "escaped -C values"),
                               ("case x in " + "".join(f"p{i}) cd d{i};; " for i in range(40))
                                + "esac; " + "cd a; " * 10000 + "git push", "branch cds")):
            self._assert_scans_fast(command, label)
            self._assert_fast(lambda: jev._scan_targets(command, "/repo"), label)
        self.assertTrue(jev._scan_targets("git commit -a (" * 3, "/repo")[0][2])

    def _cwds(self, command, op=None):
        return {t[1] for t in jev._scan_targets(command, "/repo") if op in (None, t[0])}

    def test_issue_63_command_substitution_output(self):
        # bash runs the output of a command-position substitution as a command (here
        # `cd b`), so git runs in /repo/b.
        j = os.path.join
        for command in ("`echo cd b`; git commit", "$(echo cd b); git commit",
                        "$(printf 'cd b'); git commit", "x; $(echo \"cd\" b) && git commit",
                        "$(echo -n cd b)\ngit commit", "$(echo -ne cd b); git commit", "true && `echo cd b` && git commit"):
            self.assertIn(j("/repo", "b"), self._cwds(command, "commit"), command)
        self.assertEqual(self._ops("$(echo git push)"), ["push"])
        # Not read as a command: an operator or expansion in the output, a format, or
        # a substitution that isn't at command position.
        for command in ("$(echo 'cd b; x'); git commit", "$(echo cd $d); git commit",
                        "$(printf 'cd %s' b); git commit", "x=$(echo cd b); git commit",
                        "$(echo cd b | cat); git commit", "$(ls cd b); git commit"):
            self.assertEqual(self._cwds(command, "commit"), {"/repo"}, command)

    def test_issue_63_heredoc_substitutions(self):
        # An unquoted heredoc body is expanded, so its `$( )` and backtick commands run
        # in the cwd of the heredoc's command.
        j = os.path.join
        for command, op, cwd in (
                ("cat <<EOF\n$(git push)\nEOF\n", "push", "/repo"),
                ("cd a; cat <<EOF\n`git push`\nEOF\n", "push", j("/repo", "a")),
                ("cat <<-EOF\n\t$(cd b; git push)\n\tEOF\n", "push", j("/repo", "b")),
                ("cat <<EOF\n$(git push)\n", "push", "/repo"),
                ("cat <<-EOF\n$(git push)", "push", "/repo"),
                ("cat <<EOF\nx $(echo $(git push)) y\nEOF", "push", "/repo"),
                ("cat <<EOF >out\n$(git commit -m x)\nEOF\n", "commit", "/repo"),
                ("cat <<A <<B\n$(true)\nA\n$(git commit -m x)\nB\n", "commit", "/repo"),
                ("cat <<EOF; cd b\n$(git push)\nEOF\n", "push", "/repo"),
                ("echo $(cat <<EOF\n$(git push)\nEOF\n)", "push", "/repo"),
                ("cat <<EOF\n$(cat <<E2\n$(git push)\nE2\n)\nEOF\n", "push", "/repo"),
                ("cat <<EOF\n$(echo \"a)\"; git push)\nEOF\n", "push", "/repo")):
            self.assertIn(cwd, self._cwds(command, op), command)
        # Inert: a quoted or escaped delimiter, an escaped substitution, an arithmetic,
        # and plain text.
        for command in ("cat <<'EOF'\n$(git push)\nEOF\n", "cat <<\"EOF\"\n$(git push)\nEOF\n",
                        "cat <<\\EOF\n$(git push)\nEOF\n", "cat <<EOF\n\\$(git push)\nEOF\n",
                        "cat <<EOF\n\\`git push\\`\nEOF\n", "cat <<EOF\n$((1+2)) git push\nEOF\n",
                        "cat <<EOF\ngit push\nEOF\n"):
            self.assertEqual(self._ops(command), [], command)
        # The commands run before the heredoc command, which keeps its own words (a cd
        # still resolves, and its -a flag stays).
        for command in ("cd a <<EOF; git push\n$(true)\nEOF\n",
                        "cd a <<EOF\n$(true)\nEOF\ngit push",
                        "cd a <<EOF && git push\n$(true)\nEOF\n",
                        "cd \"a b\" <<EOF; git push\n$(true)\nEOF\n",
                        "cd a <<EOF >/dev/null; git push\n$(true)\nEOF\n",
                        "cd a <<EOF <<B; git push\n$(true)\nEOF\n$(true)\nB\n",
                        "cd a >&2 <<EOF; git push\n`true`\nEOF\n"):
            expected = os.path.join("/repo", "a b" if '"a b"' in command else "a")
            self.assertIn(expected, self._cwds(command, "push"), command)
        self.assertIn(os.path.join("/repo", "a"),
                      self._cwds("cd a && cat <<EOF\n$(git push)\nEOF\n", "push"))
        # A `$( )` in the heredoc command's words stays whole: the commands go before the
        # command, so the git is still found, with its -a flag.
        for command, cwd in (
                ("git -C $(pwd) commit -am x <<EOF\n$(true)\nEOF\n", "/repo"),
                ("git commit --date=$(date -R) -a -F - <<EOF\nRelease $(cat VERSION)\nEOF\n",
                 "/repo"),
                ("git commit -m $(date +%s) -a <<EOF\n$(true)\nEOF\n", "/repo"),
                ("cd a; git commit --author=$(whoami) -am x <<EOF\n$(true)\nEOF\n",
                 os.path.join("/repo", "a")),
                ("x=$(cd b; cat <<EOF\n$(git push)\nEOF\n)", os.path.join("/repo", "b"))):
            targets = jev._scan_targets(command, "/repo")
            self.assertTrue(targets, command)
            self.assertIn(cwd, {t[1] for t in targets}, command)
            if "commit" in command:
                self.assertTrue(any(t[0] == "commit" and t[2] for t in targets), command)
        # A pipeline stage's cd runs in a subshell, so the commands go before the
        # pipeline (the last real segment), not before the stage.
        a = os.path.join("/repo", "a")
        for command, cwd in (("true | cd a <<EOF\n$(true)\nEOF\ngit push", "/repo"),
                             ("true |& cd a <<EOF\n$(true)\nEOF\ngit push", "/repo"),
                             ("true | cd a <<EOF; git push\n$(true)\nEOF\n", "/repo"),
                             ("cd a; echo | cd b <<EOF\n$(git push)\nEOF\ngit commit -m x", a),
                             ("cd a; x | cat <<EOF\n$(git push)\nEOF\n", a),
                             ("cd a; x & cat <<EOF\n$(git push)\nEOF\n", a)):
            self.assertIn(cwd, self._cwds(command), command)
        self.assertEqual(self._cwds("true | cd a <<EOF\n$(true)\nEOF\ngit push"), {"/repo"})
        # The inserted commands don't hide that a cd follows an `||`.
        j = os.path.join
        self._assert_cwds_among("cd nope || cd b <<EOF\n$(true)\nEOF\ngit push", "push",
                                j("/repo", "nope"), j("/repo", "b"))
        self._assert_cwds_among("cd a && cd nope || cd b <<EOF\n$(true)\nEOF\ngit push", "push",
                                j("/repo", "a", "b"), j("/repo", "a", "nope"))
        # The heredoc's own line keeps its -a flag.
        command = "git commit <<EOF -am x\n$(true)\nEOF\n"
        self.assertEqual([t[:3] for t in jev._scan_targets(command, "/repo")],
                         [("commit", "/repo", True)])

    def test_issue_63_mixed_or_chains(self):
        j = os.path.join
        # Every success/failure combination (bash: b when `nope` and `a` fail; c when
        # also `b` does; a when it succeeds).
        for command, cwds in (
                ("cd nope || cd b || cd c; git push", ("b", "c", "nope")),
                ("cd a || cd b || cd c; git push", ("a", "b", "c")),
                ("cd w && cd x || cd y && cd z || cd v; git push",
                 (j("w", "y", "z"), j("w", "y", "v"), j("w", "x", "z"))),
                ("cd a || cd b || cd c || cd d || cd e; git commit", ("a", "b", "c", "d", "e"))):
            self._assert_cwds_among(command, command[-3:] == "mit" and "commit" or "push",
                                    *[j("/repo", c) for c in cwds])
        # A longer adjacent chain must retain every possible successful cd.
        command = "cd a || cd b || cd c || cd d || cd e || cd f; git push"
        cwds = self._cwds(command)
        self.assertTrue({j("/repo", c) for c in "abcdef"} <= cwds)

    def test_issue_66_substitutions_and_static_output(self):
        j = os.path.join
        for command in ("git -C $(cd 'b)'; pwd) commit -am x",
                        "git -C $(case x in x) echo .;; esac) commit -am x"):
            self.assertIn(("commit", "/repo", True),
                          [t[:3] for t in jev._scan_targets(command, "/repo")], command)
        for command in ("git -C $(git push; pwd) commit -am x",
                        'git -C "$(git push)" commit -am x'):
            self.assertEqual({"push", "commit"},
                             {t[0] for t in jev._scan_targets(command, "/repo")}, command)
        for command, cwd in (("echo \"$(cd a; git push)\"", j("/repo", "a")),
                             ("cat <<< \"$(git push)\"", "/repo"),
                             ("echo \"$(echo 'x'; git push)\"", "/repo"),
                             ("$(printf 'cd a\\n'); git push", j("/repo", "a")),
                             ("$(echo $(echo cd a)); git push", j("/repo", "a")),
                             ("eval \"$(echo cd a)\"; git push", j("/repo", "a"))):
            self.assertIn(cwd, self._cwds(command, "push"), command)
        self.assertEqual(self._cwds('cd a; echo "$(cd b; git push)"; git push'),
                         {j("/repo", "a", "b"), j("/repo", "a")})
        self.assertIn(j("/repo", "b c"), self._cwds('echo "$(git -C "b c" push)"'))
        for command in ("echo '$(git push)'", "echo \"\\$(git push)\"",
                        "echo \\$(git push)", "echo \"$(echo 'git push')\"",
                        "cat <<'EOF'\n$(git push)\nEOF\n"):
            self.assertEqual(self._ops(command), [], command)

    def test_issue_66_cd_candidates_and_background(self):
        j = os.path.join
        for condition in ("true", "false"):
            cwds = self._cwds(f"if {condition}; then cd a; fi || cd b; git push")
            self.assertTrue({"/repo", j("/repo", "a"), j("/repo", "b")} <= cwds)
        cwds = self._cwds("if false; then cd a; else cd z; fi || cd b; git push")
        self.assertTrue({"/repo", j("/repo", "a"), j("/repo", "z"),
                         j("/repo", "b")} <= cwds)
        for command in ('if false; then\ncd a; fi || cd b; git push',
                        'if false; then cd "a b"; fi || cd b; git push'):
            self.assertIn("/repo", self._cwds(command, "push"), command)
        self.assertIn(j("/repo", "a b"), self._cwds('if true; then cd "a b"; fi; git push'))
        self.assertIn(j("/repo", "a"), self._cwds(
            "true & cd a <<EOF\n$(true)\nEOF\ngit push"))
        self.assertEqual(self._cwds("cd a & git push"), {"/repo"})
        self.assertIn(j("/repo", "a"), self._cwds("cd a && git push"))
        self.assertIn("/repo", self._cwds("cd a |& git push"))
        for command in ("cd a >&2; git push", "cd a &>out; git push"):
            self.assertIn(j("/repo", "a"), self._cwds(command), command)
        self.assertIn(j("/repo", "a&b"), self._cwds("cd 'a&b'; git push"))
        with self.assertRaises(jev.TooManyTargets):
            jev._scan_targets(" || ".join("cd x" for _ in range(10)) + "; git push",
                              "/repo", jev.MAX_TARGETS)
        with self.assertRaises(jev.TooManyTargets):
            jev._scan_targets("$(echo " + " " * (jev.MAX_CD_PATH_CHARS + 1)
                              + "git push)", "/repo", jev.MAX_TARGETS)

    def test_issue_66_review_nested_quotes_and_boundaries(self):
        for command in ('git -C "$(echo "b c")" commit -am x',
                        'git -C "$(git -C "b c" push; pwd)" commit -am x'):
            self.assertIn(("commit", "/repo", True),
                          [t[:3] for t in jev._scan_targets(command, "/repo")], command)
        for command in ('echo "$(echo x # )\n git push)"',
                        'echo "$(cat <<\'EOF\'\n)\nEOF\ngit push)"',
                        '$(echo "$(echo git push)")'):
            self.assertIn("/repo", self._cwds(command, "push"), command)
        for command in ("echo 'git -C $('", "cat <<'EOF'\ngit -C $(\nEOF\n"):
            self.assertEqual(jev._scan_targets(command, "/repo", jev.MAX_TARGETS), [], command)
        self.assertIn(os.path.join("/repo", "a"), self._cwds(
            "$(echo cd a); git -C $(case x in x) echo .;; esac) push", "push"))

    def test_issue_66_background_lists_restore_foreground_cwd(self):
        for command in ("true & cd a && true & git push", "cd a && true & git push",
                        "cd a &&\ntrue & git push", "true & cd a || true & git push"):
            self.assertIn("/repo", self._cwds(command, "push"), command)
        command = "cd a; cd b && git push & git commit"
        self.assertIn(os.path.join("/repo", "a", "b"), self._cwds(command, "push"))
        self.assertEqual(self._cwds(command, "commit"), {os.path.join("/repo", "a")})

    def test_issue_66_deep_substitutions_are_bounded(self):
        command = "$(" * 8500 + "git push" + ")" * 8500
        started = time.monotonic()
        self.assertIn(("push", "/repo", False, None),
                      jev._scan_targets(command, "/repo", jev.MAX_TARGETS))
        self.assertLess(time.monotonic() - started, 2.0)
        command = "$(echo " * 100 + "git push" + ")" * 100
        with self.assertRaises(jev.TooManyTargets):
            jev._scan_targets(command, "/repo", jev.MAX_TARGETS)
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.calls, [])
        for inert in ("echo '" + command + "'", "cat <<'EOF'\n" + command + "\nEOF\n",
                      "# " + command):
            self.assertEqual(jev._scan_targets(inert, "/repo", jev.MAX_TARGETS), [])

    def test_issue_66_branch_work_is_bounded(self):
        chain = " || ".join("cd d" for _ in range(9))
        command = chain + "; " + "git push; " * 3000
        with self.assertRaises(jev.TooManyTargets):
            jev._scan_targets(command, "/repo", jev.MAX_TARGETS)
        self.assertEqual(jev._scan_targets(chain + " || cd end", "/repo", jev.MAX_TARGETS), [])
        text = "if true; then cd a; fi\n" * 9
        for inert in ("cat <<'EOF'\n" + text + "EOF\n", "echo '" + text + "'",
                      "# if true; then cd a; fi\n" * 9):
            self.assertEqual(jev._scan_targets(inert, "/repo", jev.MAX_TARGETS), [])
            self.assertIsNone(self.gate(self.payload(inert), classify_fn=self.classify()))
        option = 'git -C "' + text + '" push'
        self.assertTrue(jev._scan_targets(option, "/repo", jev.MAX_TARGETS))

    def test_issue_63_case_with_or(self):
        j = os.path.join
        for command, cwds in (
                ("case x in x) cd a;; esac || cd b; git push", ("a", "b")),
                ("case x in x) cd a;; esac && cd b || cd c; git push", (j("a", "b"), j("a", "c"))),
                ("case x in x) cd a;; esac || cd b && cd c || cd d; git push", (j("a", "c"),)),
                ("case x in y) cd a;; esac || cd b; git push", (".", "b"))):
            self._assert_cwds_among(command, "push", *[j("/repo", c) if c != "." else "/repo"
                                                       for c in cwds])
        # Without a case the old readings are unchanged.
        self._assert_cwds_among("cd a || cd b; git push", "push", j("/repo", "a"))

    def test_issue_63_target_cap(self):
        command = ("case x in " + "".join(f"p{i}) cd d{i};; " for i in range(16))
                   + "esac; " + "cd a; git push; " * 40)
        with self.assertRaises(jev.TooManyTargets):
            jev._scan_targets(command, "/repo", jev.MAX_TARGETS)
        # Exactly the cap is fine, and a scan without a cap is unchanged.
        at_cap = "".join(f"git -C d{i} push;" for i in range(jev.MAX_TARGETS))
        self.assertEqual(len(jev._scan_targets(at_cap, "/repo", jev.MAX_TARGETS)), jev.MAX_TARGETS)
        with self.assertRaises(jev.TooManyTargets):
            jev._scan_targets(at_cap + "git -C x push", "/repo", jev.MAX_TARGETS)
        self.assertGreater(len(jev._scan_targets(command, "/repo")), jev.MAX_TARGETS)
        # The gate denies instead of reviewing a subset.
        out = self.gate(self.payload(command), classify_fn=self.classify())
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertIn("Too many git targets to review", hook["permissionDecisionReason"])
        self.assertEqual(self.calls, [])
        self.assertEqual((self.logs[-1]["decision"], self.logs[-1]["reason"]),
                         ("deny", "too_many_targets"))
        # Ordinary commands are unaffected: the cap's pushes (to missing directories) are
        # denied once as unjudged, and the identical retry passes as before.
        out = self.gate(self.payload(at_cap), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNJUDGED_PUSH_REASON)
        self.assertIsNone(self.gate(self.payload(at_cap), classify_fn=self.classify()))
        self.stage_change()
        out = self.gate(self.payload("cd . || cd x; git commit -m x"),
                        classify_fn=self.classify())
        self.assertIn("security", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_issue_63_constructs_scan_fast(self):
        for command, label in (("$(echo cd b); " * 10000 + "git push", "echo substitutions"),
                               ("; `echo " * 10000 + "git push", "unclosed backticks"),
                               ("cat <<EOF\n$(git push)\nEOF\n" * 5000, "heredoc commands"),
                               ("cat <<EOF\n" + "$(" * 20000 + "\nEOF\n", "open heredoc commands"),
                               ("cat <<EOF\n" + "`" * 20000 + "\nEOF\n", "heredoc backticks"),
                               ("cat <<EOF\n$(" + "cat <<E2\n$(git push)\nE2\n" * 5000 + ")\nEOF\n",
                                "nested heredocs"),
                               ("cd a || cd b || cd c || cd d; " + "git push; " * 10000,
                                "enumerated ||"),
                               ("case x in a) cd a;; esac || cd b && cd c || cd d; "
                                + "git push; " * 10000, "case with ||"),
                               ("case x in a) cd a;; esac; " + "cd q || cd sub && " * 10000
                                + "git push", "case with many ||")):
            self._assert_fast(lambda: jev._scan_targets(command, "/repo"), label)

    def test_plain_cd_targets_skip_shell_tokenization(self):
        for path in ("q", "sub", "../repo-name", "/tmp/repo", "C:/repo", "~/repo"):
            with self.subTest(path=path), mock.patch.object(jev.shlex, "split") as split:
                self.assertEqual(jev._cd_target("cd " + path), jev._usable_path(path))
                split.assert_not_called()
        for argument in ('"repo name"', "'repo'", "-- repo", "-P repo", "repo 2>/dev/null",
                         "repo\\ name", "$HOME/repo", "repo#suffix", "-", ""):
            with self.subTest(argument=argument), mock.patch.object(
                    jev.shlex, "split", wraps=jev.shlex.split) as split:
                jev._cd_target("cd " + argument)
                if argument:
                    split.assert_called_once()

    # ---- command forms, multiple ops, push base, redaction ----

    def test_command_forms_detected(self):
        self.stage_change()
        for i, command in enumerate((
                "git.exe commit -m x",
                "/usr/bin/git commit -m x",
                "C:\\Git\\cmd\\git.exe commit -m x",
                "{ git commit -m x; }",
                "if true; then git commit -m x; fi",
                "for i in 1; do git commit -m x; done",
                "if false; then :; else git commit -m x; fi",
                "time git commit -m x",
                "exec git commit -m x",
                "command git commit -m x",
                "env GIT_EDITOR=true git commit -m x",
                "nice -n 5 git commit -m x",
                "sudo git commit -m x",
                "echo don\\'t; git commit -m 'msg'",
                'echo "<<EOF"\ngit commit -m x\nEOF\n',
                "# <<EOF\ngit commit -m x\nEOF\n",
                "echo `git commit -m x`",
                "git \\\ncommit -m x",
                "gi\\\nt commit -m x")):
            self.calls.clear()
            out = self.gate(self.payload(command, session_id=f"form{i}"), classify_fn=self.classify())
            self.assertIsNotNone(out, command)
            self.assertEqual(len(self.calls), 1, command)

    def test_quoted_and_escaped_text_not_detected(self):
        self.stage_change()
        for i, command in enumerate((
                'echo "git commit -m x"',
                "echo 'don''t git commit'",
                'echo "a \\" ; git commit -m x"',
                "echo x # ; git commit -m x",
                "cat <<'EOF'\n\"unbalanced\ngit commit -m y\nEOF\n",
                "echo git.exe commit")):
            out = self.gate(self.payload(command, session_id=f"nf{i}"), classify_fn=self.classify())
            self.assertIsNone(out, command)
        self.assertEqual(self.calls, [])

    def test_prefixed_and_quoted_commits_are_seen(self):
        # Each shape is either parsed as a target or counted by the backstop.
        for command, op in (("if git commit -m x; then echo ok; fi", "commit"),
                            ("timeout 60 git commit -am x", "commit"),
                            ('echo "`git commit -m x`"', "commit"),
                            ("echo $'a\\'b' ; git commit -m 'x'", "commit"),
                            ("'g'it commit -m x", "commit"),
                            ("while ! git commit -m x; do :; done", "commit"),
                            ("sudo -u bob git commit -m x", "commit"),
                            ("nohup stdbuf -oL git commit -m x", "commit"),
                            ("flock -w 5 /tmp/l git commit -m x", "commit"),
                            ("watch git commit -m x", "commit"),
                            ("ionice -c2 chrt -f 1 git commit -m x", "commit"),
                            ("setsid unshare -r git commit -m x", "commit"),
                            ("su -c 'git commit -m x'", "commit"),
                            ("script -c 'git commit -m x' /dev/null", "commit"),
                            ('bash -o pipefail -c "git commit -m x"', "commit"),
                            ("fish -c 'git commit -m x'", "commit"),
                            ('su - deploy -c "git commit -m x"', "commit"),
                            ('su --command "git commit -m x"', "commit"),
                            ('flock -w 10 ./lock -c "git commit -m x"', "commit"),
                            ('bash -o pipefail -o errexit -o nounset -c "git commit -m x"',
                             "commit"),
                            ("bash -c $'git co\\x6dmit -m x'", "commit"),
                            ("eval $'git pu\\x73h'", "push"),
                            ("git $'co\\x6dmit' -m x", "commit"),
                            ("git $'\\x70ush'", "push"),
                            ("echo push | xargs git", "push")):
            ops = [t[0] for t in jev._scan_targets(command, "/repo")]
            signals = jev._push_signals(command) if op == "push" else jev._commit_signals(command)
            self.assertTrue(op in ops or signals, command)

    def test_unparsed_commit_denied_once(self):
        self.stage_change()
        command = "git 'commit' -m x"
        self.assertEqual(jev._scan_targets(command, str(self.repo)), [])
        out = self.gate(self.payload(command), classify_fn=self.classify())
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertEqual(hook["permissionDecisionReason"], jev.UNPARSED_COMMIT_REASON)
        self.assertEqual((self.logs[-1]["decision"], self.logs[-1]["reason"]),
                         ("deny", "unparsed_commit"))
        # The identical retry is an override, and the staged diff is then reviewed as a
        # plain commit's would be.
        out = self.gate(self.payload(command), classify_fn=self.classify())
        self.assertEqual((self.logs[-2]["decision"], self.logs[-2]["reason"]),
                         ("override", "unparsed_commit"))
        self.assertIn("This commit looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("+two", self.calls[0][0]["state"]["diff"])

    def test_commit_text_outside_command_position_is_not_a_hidden_commit(self):
        for i, command in enumerate(("echo commit", "echo git.exe commit", "git status",
                                     "grep -n commit file", "ls | xargs git add",
                                     "git log --grep commit", "echo git commit",
                                     'bash -c "echo git commit"',
                                     'bash -o pipefail -c "echo git commit"',
                                     "bash run.sh -c 'git commit -m x'",
                                     "watch -n 1 git status", "flock /tmp/l git status",
                                     "flock /tmp/l echo git commit",
                                     "git log --format=$'%h\\t%s'",
                                     "printf $'git\\tcommit\\n'")):
            self.assertFalse([t for t in jev._scan_targets(command, "/repo") if t[0] == "commit"],
                             command)
            self.assertEqual(jev._commit_signals(command), 0, command)
            out = self.gate(self.payload(command, session_id=f"cp{i}"), classify_fn=self.classify())
            self.assertIsNone(out, command)
        self.assertEqual(self.calls, [])

    def test_cd_verbs_set_cwd(self):
        j = os.path.join
        for command in ("pushd sub && git commit -m x", "chdir sub; git commit -m x",
                        "Set-Location sub; git commit -m x", "sl sub; git commit -m x"):
            self.assertEqual([t[1] for t in jev._scan_targets(command, "/repo")],
                             [j("/repo", "sub")], command)

    def test_powershell_cd_keeps_backslash(self):
        base = str(self.repo)
        targets = jev._scan_targets("cd .\\pkg; git commit -m x", base, powershell=True)
        self.assertEqual([os.path.normpath(t[1]) for t in targets],
                         [os.path.normpath(os.path.join(base, "pkg"))])

    def test_powershell_split_backslash_per_os(self):
        token = jev._POWERSHELL.set(True)
        try:
            with mock.patch.object(jev.os, "sep", "/"):
                self.assertEqual(jev._shell_split("cd .\\pkg\\sub"), ["cd", "./pkg/sub"])
            with mock.patch.object(jev.os, "sep", "\\"):
                self.assertEqual(jev._shell_split("cd '.\\pkg'"), ["cd", ".\\pkg"])
        finally:
            jev._POWERSHELL.reset(token)

    def test_long_prefix_chain_scans_fast(self):
        command = "timeout -k " * 2000 + "git commit"
        self._assert_fast(lambda: (jev._scan_targets(command, "/repo"),
                                   jev._commit_signals(command), jev._push_signals(command)),
                          "timeout -k chain")

    def test_many_quoted_words_count_fast(self):
        command = "echo " + "'x' " * 80000 + "; git commit"
        self._assert_fast(lambda: jev._commit_signals(command), "quoted words")
        command = "echo " + "'x'" * 80000 + "; git commit"
        self._assert_fast(lambda: jev._commit_signals(command), "quoted pieces of one word")
        command = "echo " + "y" * 1_000_000 + " 'x'" * 40000 + "; git commit"
        self._assert_fast(lambda: jev._commit_signals(command), "long word, quoted words")

    def test_ansi_decode_is_silent(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for command in ("echo $'\\e[1m'", "echo $'\\cA'", "echo $'\\777'",
                            "bash -c $'echo \\e[1m'"):
                jev._push_signals(command)
                jev._commit_signals(command)

    def test_native_path_translates_msys_drive_on_windows(self):
        with mock.patch.object(jev.os, "name", "nt"):
            self.assertEqual(jev._native_path("/c/Users/me/repo"), "C:/Users/me/repo")
            self.assertEqual(jev._native_path("/d"), "D:/")
            self.assertEqual(jev._native_path("/cd/x"), "/cd/x")
            self.assertEqual(jev._native_path("rel/c/x"), "rel/c/x")
        with mock.patch.object(jev.os, "name", "posix"):
            self.assertEqual(jev._native_path("/c/Users/me"), "/c/Users/me")

    @unittest.skipUnless(os.name == "nt", "Git Bash drive paths only apply on Windows")
    def test_cd_msys_drive_path_sets_cwd(self):
        self.stage_change()
        resolved = self.repo.resolve()
        msys = "/" + resolved.drive[0].lower() + resolved.as_posix()[2:]
        out = self.gate(self.payload(f"cd {msys} && git commit -m x", cwd=tempfile.gettempdir()),
                        classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(self.calls[0][0]["state"]["files"], ["a.txt"])

    def add_remote(self, branch=None, upstream=True):
        remote = self.repo.parent / "remote.git"
        run_git(["init", "--bare", str(remote)], self.repo.parent)
        run_git(["remote", "add", "origin", str(remote)], self.repo)
        target = f"HEAD:refs/heads/{branch}" if branch else "HEAD"
        run_git(["push", *(["-u"] if upstream else []), "origin", target], self.repo)
        (self.repo / "pushed.txt").write_text("unpushed\n", encoding="utf-8")
        run_git(["add", "pushed.txt"], self.repo)
        run_git(["commit", "-m", "local"], self.repo)

    def test_commit_and_push_sends_staged_and_unpushed_as_push(self):
        self.add_remote()
        self.stage_change()
        out = self.gate(self.payload("git commit --amend --no-edit && git push -f"),
                        classify_fn=self.classify())
        self.assertIsNotNone(out)
        state = self.calls[0][0]["state"]
        self.assertEqual(state["operation"], "push")
        self.assertIn("+two", state["diff"])
        self.assertIn("+unpushed", state["diff"])
        self.assertEqual(sorted(state["files"]), ["a.txt", "pushed.txt"])
        self.assertEqual(self.logs[-1]["op"], "push")

    def test_push_base_uses_remote_head(self):
        self.add_remote(branch="trunk", upstream=False)
        run_git(["fetch", "origin"], self.repo)
        run_git(["remote", "set-head", "origin", "trunk"], self.repo)
        self.assertEqual(jev._push_base(self.repo), "origin/trunk")
        out = self.gate(self.payload("git push origin HEAD:trunk"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertIn("+unpushed", self.calls[0][0]["state"]["diff"])

    def test_push_with_unavailable_classifier_fails_closed(self):
        self.add_remote()

        def boom(body, key):
            raise TimeoutError("slow")
        for session in ("s1", "s1"):  # an identical retry is still denied
            out = self.gate(self.payload("git push", session_id=session), classify_fn=boom)
            hook = out["hookSpecificOutput"]
            self.assertEqual(hook["permissionDecision"], "deny")
            self.assertIn("fail-closed", hook["permissionDecisionReason"])
        entry = self.logs[-1]
        self.assertEqual((entry["decision"], entry["reason"], entry["error"], entry["fail_closed"]),
                         ("deny", "unavailable", "TimeoutError", True))

    def test_push_without_key_fails_open_with_a_warning(self):
        # J6: a key that was never configured must not block every push for good.
        self.add_remote()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
                mock.patch.object(jev.sys, "stderr", stderr):
            for _ in range(2):
                self.assertIsNone(self.gate(self.payload("git push"), classify_fn=self.classify()))
            self.assertIsNone(self.gate(self.payload("git push --force", session_id="f"),
                                        classify_fn=self.classify()))
        self.assertEqual(self.calls, [])
        entry = self.logs[-1]
        self.assertEqual((entry["decision"], entry["reason"], entry["error"], entry["fail_open"]),
                         ("allow", "unavailable", "NoApiKey", True))
        self.assertEqual(stderr.getvalue(), (jev.NO_KEY_WARNING + "\n") * 3)
        self.assertNotIn("\n", jev.NO_KEY_WARNING)
        # A key file that is set but unusable is not this gap: it fails closed
        # (test_push_with_an_unusable_key_file_fails_closed).
        # With a key, an unreachable Jev still denies (test_push_with_unavailable_classifier_
        # fails_closed), and a commit without one stays a silent allow.
        self.stage_change()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
                mock.patch.object(jev.sys, "stderr", stderr):
            self.assertIsNone(self.gate(self.payload("git commit -m x"), classify_fn=self.classify()))
        self.assertEqual(stderr.getvalue(), "")

    def feature_branch(self):
        """A `feature` branch off the pushed commit with one commit of its own; the
        checked-out branch (with its unpushed commit) is restored. Returns its name."""
        branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.repo, check=True,
                                capture_output=True, text=True).stdout.strip()
        run_git(["checkout", "-b", "feature", "HEAD~1"], self.repo)
        (self.repo / "feature.txt").write_text("feature-only\n", encoding="utf-8")
        run_git(["add", "feature.txt"], self.repo)
        run_git(["commit", "-m", "feature"], self.repo)
        run_git(["checkout", branch], self.repo)
        return branch

    def boom(self, body, key):
        self.calls.append((body, key))
        raise TimeoutError("slow")

    def test_push_of_another_ref_is_diffed_from_that_ref(self):
        # J1: only `<upstream>...HEAD` was judged, whatever the refspec named.
        self.add_remote()
        branch = self.feature_branch()
        for i, command in enumerate(("git push origin feature", f"git push origin feature:{branch}",
                                     "git push -u origin refs/heads/feature",
                                     f"git push origin feature:refs/heads/{branch} 2>&1 | tail -3")):
            with self.subTest(command=command):
                self.calls.clear()
                out = self.gate(self.payload(command, session_id=f"r{i}"), classify_fn=self.classify())
                self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
                state = self.calls[0][0]["state"]
                self.assertIn("+feature-only", state["diff"])
                self.assertNotIn("+unpushed", state["diff"])
                self.assertEqual(state["files"], ["feature.txt"])
                # A branch the remote is not known to hold is noted (critic 3); a
                # destination with a remote-tracking ref is not.
                self.assertEqual(state.get("push"),
                                 None if branch in command else jev._PUSH_DESTINATION_NOTE)
        # The checked-out branch, named or as HEAD, is still its unpushed range.
        for i, command in enumerate((f"git push origin {branch}", "git push origin HEAD",
                                     f"git push origin HEAD:{branch}", "git push origin")):
            with self.subTest(command=command):
                self.calls.clear()
                self.gate(self.payload(command, session_id=f"h{i}"), classify_fn=self.classify())
                state = self.calls[0][0]["state"]
                self.assertIn("+unpushed", state["diff"])
                self.assertNotIn("+feature-only", state["diff"])
        # Both ranges of a two-refspec push are sent.
        self.calls.clear()
        self.gate(self.payload(f"git push origin {branch} feature", session_id="b"),
                  classify_fn=self.classify())
        self.assertEqual(sorted(self.calls[0][0]["state"]["files"]), ["feature.txt", "pushed.txt"])
        # Jev down: the other ref's push fails closed on every attempt.
        for _ in range(2):
            out = self.gate(self.payload("git push origin feature", session_id="d"), classify_fn=self.boom)
            self.assertIn("fail-closed", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_force_delete_and_wide_pushes_always_reach_the_classifier(self):
        # J1: with nothing unpushed on HEAD these all passed with no classifier call.
        self.add_remote()
        branch = self.feature_branch()
        run_git(["reset", "--hard", f"origin/{branch}"], self.repo)
        self.assertIsNone(self.gate(self.payload("git push"), classify_fn=self.classify()))
        self.assertEqual(self.calls, [])
        for i, (command, kind, text) in enumerate((
                ("git push origin --delete old", "delete", "deletes remote ref(s): old"),
                ("git push -d origin old v1", "delete", "deletes remote ref(s): old, v1"),
                ("git push origin :old", "delete", "deletes remote ref(s): old"),
                ("git push --force", "force", "force push"),
                ("git push -f", "force", "force push"),
                ("git push -fu origin HEAD", "force", "force push"),
                ("git push --force-with-lease", "force", "force push"),
                ("git push --force-with-lease=main:abc --force-if-includes", "force", "force push"),
                (f"git push origin +HEAD:{branch}", "force", "force push"),
                ("git -P push --force 2>&1", "force", "force push"),
                ("git push --mirror", "wide", "--mirror"),
                ("git push --all origin", "wide", "--all"),
                ("git push --tags", "wide", "--tags"),
                ("git push --follow-tags", "wide", "--follow-tags"),
                ("git push origin 'refs/heads/*:refs/heads/*'", "wide", "a refspec pattern"))):
            with self.subTest(command=command):
                self.calls.clear()
                out = self.gate(self.payload(command, session_id=f"n{i}"), classify_fn=self.classify())
                self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertEqual(len(self.calls), 1)
                self.assertIn(text, self.calls[0][0]["state"]["push"])
                self.assertEqual(self.logs[-1]["push_flags"], [kind])
                self.assertNotIn("old", json.dumps(self.logs[-1]))
                # Jev down: denied on every attempt, never a silent allow.
                for _ in range(2):
                    out = self.gate(self.payload(command, session_id=f"x{i}"), classify_fn=self.boom)
                    self.assertIn("fail-closed", out["hookSpecificOutput"]["permissionDecisionReason"])
                # Jev answers "none": allowed, having been asked.
                self.calls.clear()
                self.assertIsNone(self.gate(self.payload(command, session_id=f"o{i}"),
                                            classify_fn=self.classify(choice="none")))
                self.assertEqual(len(self.calls), 1)
        # An earlier critic run covers files, not a force or a delete.
        state = jev.load_session_state("crit", self.state_dir)
        state["critic_ts"] = time.time() + 10
        jev.save_session_state("crit", state, self.state_dir)
        self.calls.clear()
        self.assertIsNotNone(self.gate(self.payload("git push --force", session_id="crit"),
                                       classify_fn=self.classify()))
        self.assertEqual(len(self.calls), 1)
        # The identical retry of a judged denial still overrides, as for any push.
        self.assertIsNone(self.gate(self.payload("git push --force", session_id="crit"),
                                    classify_fn=self.classify()))
        self.assertEqual(self.logs[-1]["decision"], "override")

    def test_push_override_covers_that_push_only(self):
        # With an empty diff the retry identity was the note alone, so one overridden
        # force push covered every later one.
        self.add_remote()
        branch = self.feature_branch()
        run_git(["reset", "--hard", f"origin/{branch}"], self.repo)
        sub = self.repo / "sub"
        sub.mkdir()

        def decision(command, cwd=None):
            self.calls.clear()
            out = self.gate(self.payload(command, cwd=cwd), classify_fn=self.classify())
            return self.logs[-1]["decision"], len(self.calls), out is None
        command = "git push --force origin HEAD:one"
        self.assertEqual(decision(command), ("deny", 1, False))
        self.assertEqual(decision(command), ("override", 0, True))
        # Another ref, another remote, another cwd or another kind of push: asked again.
        for other, cwd in (("git push --force origin HEAD:two", None),
                           ("git push --force upstream HEAD:one", None),
                           ("git push --force origin HEAD:one :two", None),
                           (command, str(sub)),
                           ("git push --force", None),
                           ("git 'push' --force origin HEAD:one", None)):
            with self.subTest(command=other, cwd=cwd):
                if "'" in other:  # past its own unparsed denial
                    self.gate(self.payload(other, cwd=cwd), classify_fn=self.classify())
                self.assertEqual(decision(other, cwd), ("deny", 1, False))
                self.assertEqual(decision(other, cwd), ("override", 0, True))
        # Each override still stands for its own push.
        self.assertEqual(decision(command), ("override", 0, True))

    def test_unresolved_refspec_is_unjudged_then_asked(self):
        self.add_remote()
        for i, command in enumerate(("git push origin nosuch", "git push origin $BRANCH",
                                     "git push origin -- -x")):
            with self.subTest(command=command):
                self.calls.clear()
                payload = self.payload(command, session_id=f"u{i}")
                out = self.gate(payload, classify_fn=self.classify())
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                                 jev.UNJUDGED_PUSH_REASON)
                self.assertEqual(self.calls, [])
                # The retry is not a silent allow: Jev is asked, and fails closed when down.
                out = self.gate(payload, classify_fn=self.boom)
                self.assertIn("fail-closed", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertEqual(self.logs[-1]["push_flags"], ["refspec"])

    def test_push_request_reads_flags_and_refspecs(self):
        def request(*args):
            r = jev._push_request(list(args))
            return (r["remote"], r["specs"], r["deletes"], r["force"], r["wide"], r["dynamic"])
        for args, expected in (
                ((), (None, [], [], False, [], False)),
                (("origin",), ("origin", [], [], False, [], False)),
                (("origin", "main"), ("origin", [("main", "main")], [], False, [], False)),
                (("-u", "origin", "a:b"), ("origin", [("a", "b")], [], False, [], False)),
                (("origin", "+a:b"), ("origin", [("a", "b")], [], True, [], False)),
                (("origin", ":b", "c"), ("origin", [("c", "c")], ["b"], False, [], False)),
                (("--delete", "origin", "b", "c"), ("origin", [], ["b", "c"], False, [], False)),
                (("-d", "origin", "b"), ("origin", [], ["b"], False, [], False)),
                (("origin", "tag", "v1"), ("origin", [("refs/tags/v1", "refs/tags/v1")], [], False, [], False)),
                (("-o", "ci.skip", "origin", "x"), ("origin", [("x", "x")], [], False, [], False)),
                (("--push-option", "f", "--repo=o", "--receive-pack", "d", "origin"),
                 ("origin", [], [], False, [], False)),
                (("--force-with-lease=x:y",), (None, [], [], True, [], False)),
                (("--no-verify", "--tags", "--prune", "--tags"), (None, [], [], False,
                                                                  ["--tags", "--prune", "--tags"], False)),
                (("origin", "$b", "`x`"), ("origin", [], [], False, [], True)),
                (("origin", "--", "-f"), ("origin", [("-f", "-f")], [], False, [], False))):
            self.assertEqual(request(*args), expected, args)

    def test_push_request_reads_abbreviated_unknown_and_run_time_words(self):
        def request(*args, git_words=()):
            r = jev._push_request(list(args), git_words)
            deletes = ["delete"] if r["deletes"] else []
            return sorted(set(r["wide"])) + deletes + [
                k for k in ("force", "dynamic", "runtime", "unknown", "config") if r[k]]
        for args, expected in (
                # git reads a unique prefix of a long option; an ambiguous one is read as each.
                (("--del", "origin", "b"), ["delete"]), (("--al",), ["--all"]),
                (("--mirr",), ["--mirror"]), (("--ta",), ["--tags"]), (("--pru",), ["--prune"]),
                (("--foll",), ["--follow-tags"]), (("--bra",), ["--branches"]),
                (("--forc",), ["force"]), (("--force-w",), ["force"]), (("--force-i",), ["force"]),
                (("--f",), ["--follow-tags", "force"]), (("--d", "origin", "b"), ["delete"]),
                (("--rep", "o", "--exe", "x", "origin", "b"), []),
                (("--no-force", "--no-tags", "--no-ver", "--dry", "--set-up", "origin"), []),
                (("--frobnicate",), ["unknown"]), (("--FORCE",), ["unknown"]),
                # A word the shell fills in may be any of these.
                (("$F",), ["runtime"]), (("${F:---force}",), ["runtime"]), (("$",), ["runtime"]),
                (("`echo", "--force`"), ["runtime", "unknown"]),
                (("origin", "{main", "--force}"), ["dynamic", "runtime", "unknown"]),
                (("-$x", "origin"), ["runtime"]), (("--force-with-lease=$ref",), ["force", "runtime"]),
                (("origin", "HEAD@{u}:main", "main@{1}"), [])):
            self.assertEqual(request(*args), expected, args)
        self.assertEqual(jev._push_request(["--repo=https://h/x.git"])["remote"], "https://h/x.git")
        self.assertEqual(jev._push_request(["--repo", "../o"])["remote"], "../o")
        for git_words, expected in (
                (["-c", "remote.origin.push=+refs/heads/work:refs/heads/main"], ["config"]),
                (["-c", "push.default=matching"], ["config"]),
                (["-c", "Remote.Origin.Mirror=true"], ["config"]),
                (["-c", "url.https://h/.insteadOf=x"], ["config"]), (["-c", "$CFG"], ["config"]),
                (["--config-env=push.default=X"], ["config"]),
                (["--config-env", "branch.main.remote=X"], ["config"]),
                (["-C", "remote.d", "-c", "core.pager=cat", "--no-pager"], [])):
            self.assertEqual(request(git_words=git_words), expected, git_words)
        self.assertEqual(jev._push_commands("git -c push.default=matching -C x push -f o"),
                         [(["-c", "push.default=matching", "-C", "x"], ["-f", "o"])])

    def test_push_whose_arguments_no_diff_vouches_for_reaches_the_classifier(self):
        # Critic 3-5: with nothing unpushed each of these passed as a plain push.
        self.add_remote()
        run_git(["reset", "--hard", "@{u}"], self.repo)
        for command in ("git push", "git push origin", "git push origin HEAD", "git -c core.pager=cat push"):
            self.assertIsNone(self.gate(self.payload(command), classify_fn=self.boom), command)
        self.assertEqual(self.calls, [])
        cases = (
            # 3: abbreviated long options, and ones git push does not have.
            ("git push --del origin gone", "delete"), ("git push --al", "wide"),
            ("git push --mirr", "wide"), ("git push --ta", "wide"), ("git push --pru origin", "wide"),
            ("git push --foll", "wide"), ("git push --bra", "wide"), ("git push --forc", "force"),
            ("git push --force-w", "force"), ("git push --force-i", "force"),
            ("git push --frobnicate", "option"),
            # 4: words only known at run time, outside a refspec too.
            ("F=--force; git push $F", "runtime"), ("git push ${F:---force}", "runtime"),
            ("git push $(echo --force)", "runtime"), ("git push `echo --force`", "runtime"),
            ("git push origin {HEAD,--force}", "runtime"), ('git push "$F" HEAD', "runtime"),
            # 5: a remote that is a URL, a path or not configured; inline config.
            ("git push https://example.invalid/x.git HEAD:main", "remote"),
            ("git push ../other HEAD:main", "remote"), ("git push elsewhere HEAD:main", "remote"),
            ("git push --repo=https://example.invalid/x.git", "remote"),
            ("git -c remote.origin.push=+refs/heads/work:refs/heads/main push origin", "config"),
            ("git -c push.default=matching push", "config"),
            ("git -c remote.origin.mirror=true push origin", "config"),
            ("git --config-env=push.default=X push", "config"),
            ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=push.default GIT_CONFIG_VALUE_0=matching git push",
             "config"),
            ("GIT_CONFIG_PARAMETERS=\"'push.default=matching'\" git push", "config"),
            ("export GIT_CONFIG_COUNT=1; git push", "config"))
        for i, (command, flag) in enumerate(cases):
            for classify_fn, expected in ((self.boom, "fail-closed"),
                                          (self.classify(), "This push looks risky")):
                with self.subTest(command=command, expected=expected):
                    self.calls.clear()
                    payload = self.payload(command, session_id=f"n{i}{expected[0]}")
                    reason = self.gate(payload, classify_fn=classify_fn)[
                        "hookSpecificOutput"]["permissionDecisionReason"]
                    if reason == jev.UNJUDGED_PUSH_REASON:
                        # A refspec only known at run time is denied once first.
                        self.assertEqual(self.calls, [])
                        reason = self.gate(payload, classify_fn=classify_fn)[
                            "hookSpecificOutput"]["permissionDecisionReason"]
                    self.assertIn(expected, reason)
                    self.assertEqual(len(self.calls), 1)
                    self.assertTrue(self.calls[0][0]["state"]["push"])
                    self.assertIn(flag, self.logs[-1]["push_flags"])

    def test_push_with_an_unusable_key_file_fails_closed(self):
        # Critic 6: only a key that was never configured fails open; a key file that is
        # set but cannot be used is a misconfiguration, blocked like an unreachable Jev.
        self.add_remote()
        states = (("missing", None, "does not exist"), ("empty", b"", "is empty"),
                  ("blank", b" \r\n\t\n", "is empty"), ("long", b"x" * 600, "is too long"),
                  ("binary", b"\xff\xfe\x00tok-secret", "is not UTF-8 text"),
                  ("words", b"tok-secret-1 tok-secret-2", "holds more than one word"),
                  ("dir", None, "is a directory"))
        stderr = io.StringIO()
        for i, (name, content, problem) in enumerate(states):
            path = self.repo.parent / f"key-{name}"
            if name == "dir":
                path.mkdir()
            elif content is not None:
                path.write_bytes(content)
            cfg = dict(self.cfg, api_key_file=str(path))
            for classify_fn in (self.boom, self.classify()):  # Jev down or up: nobody can ask it
                with self.subTest(name=name), mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
                        mock.patch.object(jev.sys, "stderr", stderr):
                    out = self.gate(self.payload("git push", session_id=f"k{i}"), cfg=cfg,
                                    classify_fn=classify_fn)
                    hook = out["hookSpecificOutput"]
                    self.assertEqual(hook["permissionDecision"], "deny")
                    for text in ("fail-closed", "jev.api_key_file", problem, str(path)):
                        self.assertIn(text, hook["permissionDecisionReason"])
                    self.assertNotIn("tok-secret", hook["permissionDecisionReason"])
                    entry = self.logs[-1]
                    self.assertEqual(
                        (entry["decision"], entry["reason"], entry["error"], entry["fail_closed"]),
                        ("deny", "unavailable", "BadApiKeyFile", True))
        self.assertEqual(self.calls, [])
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("tok-secret", json.dumps(self.logs))
        # A usable key file: the classifier is asked, with that key.
        good = self.repo.parent / "key-good"
        good.write_text(" tok-good \n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            out = self.gate(self.payload("git push", session_id="kg"),
                            cfg=dict(self.cfg, api_key_file=str(good)), classify_fn=self.classify())
        self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual([key for _, key in self.calls], ["tok-good"])

    def test_unreadable_config_or_key_source_fails_a_push_closed(self):
        # Round 6, item 9: an unreadable config file, or a key source set on purpose that
        # gives no key, read as "no key configured" and let the push through.
        stderr = io.StringIO()
        cfg = dict(jev.load_config(Path("/nonexistent/config.json")),
                   config_problem="/x/config.json cannot be read as JSON")  # gate off, as loaded
        for i, command in enumerate(("git push", "bash -c 'git push origin main'",
                                     "gh pr create --fill")):
            with self.subTest(command=command), mock.patch.object(jev.sys, "stderr", stderr):
                out = self.gate(self.payload(command, session_id=f"cp{i}"), cfg=cfg,
                                classify_fn=self.boom)
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("fail-closed", reason)
                self.assertIn("/x/config.json cannot be read as JSON", reason)
                self.assertEqual(self.logs[-1]["reason"], "config_problem")
        for command in ("git status", "echo git push", "ls"):
            self.assertIsNone(self.gate(self.payload(command, session_id="cpn"), cfg=cfg))
        self.add_remote()
        home = self.repo.parent / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), \
                mock.patch.object(Path, "home", return_value=home), \
                mock.patch.object(jev.sys, "stderr", stderr):
            for i, source in enumerate(("claude_settings", "tok-secret-pasted")):
                out = self.gate(self.payload("git push", session_id=f"ks{i}"),
                                cfg=dict(self.cfg, api_key_source=source), classify_fn=self.boom)
                reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("fail-closed", reason)
                self.assertIn("jev.api_key_source", reason)
                self.assertNotIn("tok-secret", reason)
                self.assertEqual(self.logs[-1]["error"], "BadApiKeyFile")
        self.assertEqual(self.calls, [])
        self.assertEqual(stderr.getvalue(), "")

    def test_push_arg_lists_reads_plain_pushes_only(self):
        for command, expected in (
                ("git push", [[]]),
                ("git push origin main 2>&1 | tail -5", [["origin", "main"]]),
                ("git push origin main > out.log 2> err.log", [["origin", "main"]]),
                ("git push origin main >> log; git -C x --no-pager push -f", [["origin", "main"], ["-f"]]),
                ("git 'push' \"--force\" origin", [["--force", "origin"]]),
                ("cd r && sudo -u bob timeout 60 git -c k=v push --tags", [["--tags"]]),
                ("git commit -m 'a > b; git push --force' && git push origin x", [["origin", "x"]]),
                ("C:\\Git\\cmd\\git.exe push origin main", [["origin", "main"]]),
                ("echo git push --force", []), ("git stash push -f", []), ("git status", []),
                ("cat > f <<'EOF'\ngit push --force\nEOF", []),
                ("bash <<EOF\ngit push --force\nEOF", [["--force"]]),
                ('bash -c "git push --force" && ls', [])):
            self.assertEqual(jev._push_arg_lists(command), expected, command)

    def test_wrapped_whole_command_is_gated_as_its_script(self):
        # J2: these were unparsed, so denied once and then passed with no review at all.
        self.add_remote()
        for command, script in (("bash -lc 'git push'", "git push"),
                                ('sh -c "git push origin HEAD"', "git push origin HEAD"),
                                ("zsh -ic 'cd . && git push'", "cd . && git push"),
                                ("bash --login -c 'git push'", "git push"),
                                ("winpty bash -lc 'git push'", "git push"),
                                ("/usr/bin/bash -l -c 'git push'", "git push"),
                                ("bash -c \"bash -c 'git push'\"", "git push")):
            self.assertEqual(jev._unwrap_shell(command), script, command)
        for command in ("bash -c 'git push' && ls", "bash -o pipefail -c 'git push'", "bash run.sh",
                        "bash -c 'git push' arg0", "bash -lc 'git push", "echo bash -c 'git push'",
                        "bash -lc 'git push' 2>&1", "git push", "", "bash -c " + "'x' " * 60_000):
            self.assertEqual(jev._unwrap_shell(command), command, command[:60])
        for command in ("git -P push", "git -p push origin HEAD", "git -P -C . push",
                        "git --no-replace-objects --literal-pathspecs --no-optional-locks push",
                        "winpty git push", "cd . && winpty -Xallow-non-tty git push"):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"], command)
        self.assertEqual([t[0] for t in jev._scan_targets("git -P commit -m x", "/repo")], ["commit"])
        self.assertEqual(jev._scan_targets("git -Px push", "/repo"), [])
        for i, command in enumerate(("git -P push", "bash -lc 'git push'", "winpty git push",
                                     "winpty bash -lc 'git -P push'", 'sh -c "git push"')):
            with self.subTest(command=command):
                self.calls.clear()
                for _ in range(2):  # reviewed at once, and fail-closed on the identical retry
                    out = self.gate(self.payload(command, session_id=f"w{i}"), classify_fn=self.boom)
                    self.assertIn("fail-closed", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertEqual(len(self.calls), 2)
                self.assertIn("+unpushed", self.calls[0][0]["state"]["diff"])
        # A wrapped commit is reviewed on its first attempt too.
        self.stage_change()
        self.calls.clear()
        out = self.gate(self.payload("bash -lc 'git commit -m x'"), classify_fn=self.classify())
        self.assertIn("This commit looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self.calls), 1)

    def test_overridden_unparsed_push_is_still_reviewed_and_fails_closed(self):
        # J2: the retry of an unparsed push returned "allow" before any diff or classifier.
        self.add_remote()
        for i, command in enumerate(("git 'push'", 'true; bash -c "git push"', "g=git; $g push",
                                     "eval 'git push'")):
            with self.subTest(command=command):
                self.calls.clear()
                payload = self.payload(command, session_id=f"p{i}")
                out = self.gate(payload, classify_fn=self.boom)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                                 jev.UNPARSED_PUSH_REASON)
                self.assertEqual(self.calls, [])
                for _ in range(2):  # Jev down: denied on every further attempt
                    out = self.gate(payload, classify_fn=self.boom)
                    self.assertIn("fail-closed", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertEqual(self.logs[-1]["fail_closed"], True)
                # Jev back: the cwd repository's unpushed range is what gets judged.
                self.calls.clear()
                out = self.gate(payload, classify_fn=self.classify())
                self.assertIn("This push looks risky", out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertIn("+unpushed", self.calls[0][0]["state"]["diff"])
                self.assertIsNone(self.gate(payload, classify_fn=self.classify()))
        # Its flags are read when they are in plain sight.
        self.calls.clear()
        payload = self.payload("git 'push' --force", session_id="pf")
        self.gate(payload, classify_fn=self.classify())
        self.gate(payload, classify_fn=self.classify())
        self.assertIn("force push", self.calls[0][0]["state"]["push"])

    def test_commands_that_only_mention_a_push_are_not_pushes(self):
        # J5: read commands were denied once as "looks like it pushes".
        self.add_remote()
        commands = ('grep -rn "git push" docs/', "rg 'git push'", "echo git push",
                    'git log --grep="git push"', "git grep 'git push'", "git config --get alias.push",
                    "git config get alias.push", "git config --get-regexp alias.push",
                    "git config alias.push", "git config --unset alias.push",
                    "cat > f <<'EOF'\nthen run git push\ngit push --force\nEOF",
                    "cat <<EOF > notes.md\ngit push origin main\nEOF",
                    "Select-String -Pattern 'git push' -Path docs/*.md",
                    "ls # git push", 'grep -c "git push" notes.txt | head -3',
                    'python -m pytest -k "git push"', "rg 'git push' scripts/deploy.sh | head -3",
                    "printf '%s\\n' 'git push'", 'echo "run: git push origin main"')
        for i, command in enumerate(commands):
            with self.subTest(command=command):
                self.assertFalse(jev._looks_like_push(command))
                # PowerShell has no heredoc: the lines after `<<EOF` are commands there.
                for tool in ("Bash",) if "<<" in command else ("Bash", "PowerShell"):
                    payload = dict(self.payload(command, session_id=f"m{i}"), tool_name=tool)
                    self.assertIsNone(self.gate(payload, classify_fn=self.classify()))
        self.assertEqual((self.calls, self.logs), ([], []))
        # What runs is still seen: an interpreter's script, text fed to a shell, a
        # substitution inside double quotes, and an alias that is set rather than read.
        for command in ("echo git push | sh", "sh <<< 'git push'", "sh <(echo git push)",
                        "cat <<EOF | bash\ngit push\nEOF", "bash <<EOF\ngit push\nEOF",
                        "python - <<EOF\nsubprocess.run(['git','push'])\nEOF",
                        "python3 -c \"import os; os.system('git push')\"",
                        "node -e \"require('child_process').execSync('git push')\"",
                        'iex "git push"', 'echo "$(git push)"', 'echo "`git push`"',
                        "git config alias.push 'push --force'", "git config --add alias.p push",
                        "git config --get alias.x; git -c alias.p=push p",
                        "find . -name x -exec git push \\;", "time git 'push'"):
            self.assertTrue(jev._looks_like_push(command), command)

    def test_text_a_command_may_run_is_read_as_a_push(self):
        # Reading only what runs dropped pushes run some other way: a command word or
        # script built at run time, trap/alias/coproc/source, text fed to a shell or an
        # interpreter, a script written and then run.
        self.add_remote()
        branch = self.feature_branch()
        run_git(["reset", "--hard", f"origin/{branch}"], self.repo)
        commands = ("eval $(echo git push --force)", 'c="git push --force"; $c',
                    "c='git push --force'; eval $c", 'c=\'git push --force\'; eval "$c"',
                    "c='git push --force'; bash -c \"$c\"",
                    "alias gp='git push --force'; gp", "bash +x -c 'git push'",
                    "trap 'git push' EXIT", "coproc git push",
                    "echo 'git push --force' > p.sh; sh p.sh",
                    "echo 'git push --force' > p.sh; bash -e p.sh",
                    "echo 'git push --force' > p.sh; chmod +x p.sh; ./p.sh",
                    "echo git push --force | $SHELL", "echo git push --force | fish",
                    'echo "git push --force" | tee p.sh; ./p.sh',
                    ". <(echo git push --force)", "source <(echo git push --force)",
                    'python - <<EOF\nimport os\nos.system("git push --force")\nEOF',
                    'python <<EOF\nimport os\nos.system("git push --force")\nEOF',
                    "bash -s <<EOF\ngit push --force\nEOF",
                    "cat > p.sh <<EOF\ngit push --force\nEOF\nbash p.sh",
                    "cat > p.sh <<'EOF'\ngit push --force\nEOF\n./p.sh",
                    "Start-Process git -ArgumentList 'push','--force'",
                    "node -p \"require('child_process').execSync('git push')\"")
        for i, command in enumerate(commands):
            with self.subTest(command=command):
                self.assertTrue(jev._looks_like_push(command))
                # Jev down: denied on every attempt.
                self.calls.clear()
                for attempt in range(3):
                    out = self.gate(self.payload(command, session_id=f"t{i}"), classify_fn=self.boom)
                    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
                    if attempt:
                        self.assertIn("fail-closed", reason)
                    else:
                        self.assertEqual(reason, jev.UNPARSED_PUSH_REASON)
                self.assertEqual(len(self.calls), 2)
        # Text that is only written, printed or read is still not a push.
        for command in ('grep "git push" notes.md > out.txt; cat out.txt',
                        'echo "git push" 2>&1 | tail -2', 'echo "git push" >> NOTES.md',
                        'grep -rn "git push" . 2>/dev/null | wc -l',
                        'git log --oneline | grep "git push" || echo none',
                        "cat <<EOF\ngit push origin main\nEOF"):
            self.assertFalse(jev._looks_like_push(command), command)

    def test_push_with_malformed_answer_fails_closed(self):
        self.add_remote()
        out = self.gate(self.payload("git push"),
                        classify_fn=lambda body, key: {"answers": {"risk": {"choice": None}}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("fail-closed", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_push_with_disallowed_endpoint_fails_closed(self):
        self.add_remote()
        cfg = dict(self.cfg, endpoint="http://jev.internal.example/v1")
        out = self.gate(self.payload("git push"), cfg=cfg, classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.logs[-1]["error"], "EndpointDisallowed")

    def test_push_with_missing_or_bad_needs_review_fails_closed(self):
        self.add_remote()
        for i, needs_review in enumerate(({"noul": "oops"}, None)):
            answers = {"risk": {"choice": "data_loss", "confidence": 0.9}}
            if needs_review is not None:
                answers["needs_review"] = needs_review
            out = self.gate(self.payload("git push", session_id=f"nr{i}"),
                            classify_fn=lambda body, key, a=answers: {"answers": a})
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", needs_review)
            self.assertEqual(self.logs[-1]["reason"], "malformed")

    def test_commit_with_missing_needs_review_stays_open(self):
        self.stage_change()
        out = self.gate(self.payload("git commit -m x"), classify_fn=lambda body, key: {
            "answers": {"risk": {"choice": "data_loss", "confidence": 0.9}}})
        self.assertIsNone(out)

    def test_push_with_disabled_feature_stays_open(self):
        self.add_remote()
        cfg = dict(self.cfg, enabled=False)
        self.assertIsNone(self.gate(self.payload("git push"), cfg=cfg, classify_fn=self.classify()))


    def test_push_base_falls_back_to_origin_master(self):
        self.add_remote(branch="master", upstream=False)
        run_git(["fetch", "origin"], self.repo)
        self.assertEqual(jev._push_base(self.repo), "origin/master")
        out = self.gate(self.payload("git push origin HEAD:master"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertEqual(self.calls[0][0]["state"]["files"], ["pushed.txt"])

    def test_missing_null_or_nan_confidence_still_gates(self):
        self.stage_change()
        for i, confidence in enumerate(("missing", None, float("nan"), float("inf"))):
            def fn(body, key, confidence=confidence):
                response = risk_response()
                if confidence == "missing":
                    del response["answers"]["risk"]["confidence"]
                else:
                    response["answers"]["risk"]["confidence"] = confidence
                return response
            out = self.gate(self.payload("git commit -m x", session_id=f"conf{i}"), classify_fn=fn)
            self.assertIsNotNone(out, confidence)
            self.assertEqual(self.logs[-1]["decision"], "deny")
            self.assertIsNone(self.logs[-1]["confidence"])

    def test_deny_returned_when_state_persistence_fails(self):
        self.stage_change()
        with mock.patch.object(jev, "update_state", side_effect=PermissionError("in use")):
            out = self.gate(self.payload("git commit -m x"), classify_fn=self.classify())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.logs[-1]["decision"], "deny")
        self.assertEqual(self.logs[-1]["state_error"], "PermissionError")

    def test_git_and_classifier_share_the_hook_deadline(self):
        self.stage_change()
        seen = []
        real_run_git = jev._run_git

        def spy_run_git(args, cwd, deadline=None, git_opts=None):
            seen.append(deadline)
            return real_run_git(args, cwd, deadline, git_opts)
        ask_deadlines = []
        real_ask = jev.ask

        def spy_ask(*args, **kwargs):
            ask_deadlines.append(kwargs.get("deadline"))
            return real_ask(*args, **kwargs)
        deadline = time.monotonic() + 3.0
        with mock.patch.object(jev, "_run_git", spy_run_git), mock.patch.object(jev, "ask", spy_ask):
            jev.gate(self.payload("git commit -m x"), self.cfg, self.classify(), self.logs.append,
                     state_dir=self.state_dir, deadline=deadline)
        self.assertTrue(seen)
        self.assertTrue(all(d is not None and d <= deadline for d in seen), seen)
        self.assertEqual(ask_deadlines, [deadline])

    def test_slow_classifier_is_cut_off_at_the_hook_deadline(self):
        self.stage_change()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(body, key):
            release.wait(5)
            return risk_response()
        start = time.monotonic()
        with mock.patch.object(jev, "GATE_BUDGET_SECONDS", 0.6):
            out = self.gate(self.payload("git commit -m x"), classify_fn=slow)
        # Far below the 5 s the classifier would take, with room for a loaded machine.
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertIsNone(out)
        if self.logs:  # git may have used the whole budget under load, which also allows
            self.assertEqual(self.logs[-1]["error"], "TimeoutError")

    def test_spent_deadline_fails_open_without_classifier(self):
        self.stage_change()
        out = jev.gate(self.payload("git commit -m x"), self.cfg, self.classify(), self.logs.append,
                       state_dir=self.state_dir, deadline=time.monotonic() - 1)
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

    def test_string_or_bool_confidence_is_logged_as_none(self):
        self.stage_change()
        for i, confidence in enumerate(("0.9", True)):
            out = self.gate(self.payload("git commit -m x", session_id=f"coerce{i}"),
                            classify_fn=self.classify(confidence=confidence))
            self.assertIsNotNone(out, confidence)
            self.assertEqual(self.logs[-1]["decision"], "deny")
            self.assertIsNone(self.logs[-1]["confidence"], confidence)

    def test_unavailable_classifier_is_logged_without_details(self):
        self.stage_change()

        def boom(body, key):
            raise TimeoutError("secret-body test-key")
        out = self.gate(self.payload("git commit -m x"), classify_fn=boom)
        self.assertIsNone(out)
        entry = self.logs[-1]
        self.assertEqual((entry["decision"], entry["reason"], entry["error"]),
                         ("allow", "unavailable", "TimeoutError"))
        self.assertIn("latency_ms", entry)
        self.assertNotIn("secret-body", json.dumps(self.logs))
        self.assertNotIn("test-key", json.dumps(self.logs))

    def test_no_key_is_logged_unavailable(self):
        self.stage_change()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            self.assertIsNone(self.gate(self.payload("git commit -m x"), classify_fn=self.classify()))
        self.assertEqual(self.logs[-1]["error"], "NoApiKey")

    def test_secret_files_and_tokens_redacted_before_sending(self):
        token = "ghp_" + "A" * 30
        self.stage_change(".env", "DB_PASSWORD=hunter2\n")
        self.stage_change("server.PEM", "certificate body\n")
        self.stage_change("prod.env", "PROD_SECRET=hunter3\n")
        self.stage_change("credentials.json", "{\"key\": \"hunter4\"}\n")
        self.stage_change("app.jks", "keystore body\n")
        self.stage_change("app.keystore", "keystore body2\n")
        self.stage_change("a.txt", f"key = sk-{'b' * 20}\ntoken = {token}\nplain change\n")
        out = self.gate(self.payload("git commit -m x"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        state = self.calls[0][0]["state"]
        diff = state["diff"]
        self.assertIn(".env", state["files"])
        self.assertIn("diff --git a/.env b/.env", diff)
        self.assertIn("diff --git a/server.PEM b/server.PEM", diff)
        self.assertNotIn("hunter2", diff)
        self.assertNotIn("certificate body", diff)
        self.assertNotIn("hunter3", diff)
        self.assertNotIn("hunter4", diff)
        self.assertNotIn("keystore body", diff)
        self.assertNotIn("keystore body2", diff)
        self.assertNotIn("sk-bbbb", diff)
        self.assertNotIn(token, diff)
        self.assertIn("+plain change", diff)
        self.assertEqual(diff.count("[redacted]"), 8)

    def test_redaction_survives_hostile_diff_config(self):
        # diff.noprefix/mnemonicPrefix drop the a/ b/ header prefixes DIFF_HEADER_RE
        # expects, and color.diff=always would inject ANSI codes into the header line;
        # without forcing --no-color/--src-prefix/--dst-prefix on every diff, _redact_diff
        # never enters header mode and the secret file's hunk is sent unredacted.
        run_git(["config", "diff.noprefix", "true"], self.repo)
        run_git(["config", "diff.mnemonicPrefix", "true"], self.repo)
        run_git(["config", "color.diff", "always"], self.repo)
        self.stage_change(".env", "DB_PASSWORD=hunter2\n")
        out = self.gate(self.payload("git commit -m x"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        state = self.calls[0][0]["state"]
        self.assertIn(".env", state["files"])
        self.assertNotIn("hunter2", state["diff"])
        self.assertIn("[redacted]", state["diff"])

    def test_textconv_output_is_not_sent(self):
        # A textconv driver's output would go out as hunk text under a name that
        # isn't redacted (e.g. `gpg -d` for *.gpg).
        (Path(self.repo) / ".gitattributes").write_text("*.txt diff=leak\n")
        run_git(["config", "diff.leak.textconv", "echo LEAKED-BY-TEXTCONV; cat"], self.repo)
        self.stage_change("a.txt", "plain\n")
        out = self.gate(self.payload("git commit -m x"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        self.assertNotIn("LEAKED-BY-TEXTCONV", self.calls[0][0]["state"]["diff"])

    def test_scrub_token_shapes(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        for secret in (key, "sk-" + "x" * 16, "gho_" + "1" * 20, "github_pat_" + "a" * 22,
                       "AKIA" + "ABCDEFGHIJKLMNOP", "xoxb-123456789-abc"):
            self.assertEqual(jev._scrub(f"before {secret} after"), "before [redacted] after", secret)
        self.assertEqual(jev._scrub("sk-short and AKIAlower"), "sk-short and AKIAlower")

    def test_scrub_npm_jwt_and_url_credentials(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLWJ5dGVz"
        self.assertEqual(jev._scrub(f"before npm_{'a1B2' * 9} after"), "before [redacted] after")
        self.assertEqual(jev._scrub(f"token: {jwt}"), "token: [redacted]")
        self.assertEqual(jev._scrub("url = https://deploy:hunter2@example.com/repo.git"),
                         "url = https://[redacted]@example.com/repo.git")
        self.assertEqual(jev._scrub("+git clone https://x-token:ghx123@github.com/o/r\n"),
                         "+git clone https://[redacted]@github.com/o/r\n")
        # An empty user, and a token as the user with no password.
        for text, want in (("redis://:hunter2@host:6379/0", "redis://[redacted]@host:6379/0"),
                           ("https://:ghx_tok@github.com/o/r", "https://[redacted]@github.com/o/r"),
                           ("https://abcdefghij0123456789KL_-@host/x", "https://[redacted]@host/x")):
            self.assertEqual(jev._scrub(text), want, text)
        for text in ("ssh://git@github.com/o/r", "https://user@host/x",
                     "https://" + "a" * 19 + "@host/x"):
            self.assertEqual(jev._scrub(text), text, text)
        # Near misses are kept: short npm tokens, one-part `eyJ` words, plain URLs and
        # ports, and an `@` past the host.
        for text in ("npm_short", "npm_" + "a" * 37, "eyJhbGciOiJIUzI1NiJ9 only",
                     "https://example.com:8080/path", "ssh://git@github.com/o/r",
                     "https://example.com/a:b/@c"):
            self.assertEqual(jev._scrub(text), text, text)

    def test_redact_diff_new_secret_file_patterns(self):
        def file_diff(name, body="+hunter2\n"):
            return (f"diff --git a/{name} b/{name}\nindex 1..2 100644\n--- a/{name}\n"
                    f"+++ b/{name}\n@@ -0,0 +1 @@\n{body}")
        for name in (".netrc", "home/_netrc", ".npmrc", ".pypirc", ".git-credentials",
                     ".pgpass", "web/.htpasswd", "keys/server.ppk", "infra/prod.tfvars",
                     "vault.kdbx", "id_dsa", "ssh/id_ecdsa.pub", "id_ed25519", "ID_ED25519_SK"):
            out = jev._redact_diff(file_diff(name))
            self.assertNotIn("hunter2", out, name)
            self.assertIn(f"diff --git a/{name} b/{name}", out, name)
            self.assertIn("[redacted]", out, name)
        for name in ("src/netrc.py", "docs/npmrc.md", "main.tf", "src/tfvars.md"):
            self.assertIn("hunter2", jev._redact_diff(file_diff(name)), name)

    def test_secret_directories_redacted_before_sending(self):
        (self.repo / "secrets").mkdir()
        (self.repo / "config" / "Credentials").mkdir(parents=True)
        self.stage_change("secrets/db.yaml", "password: hunter2\n")
        self.stage_change("config/Credentials/prod.json", "{\"key\": \"hunter3\"}\n")
        self.stage_change("a.txt", "plain change\n")
        out = self.gate(self.payload("git commit -m x"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        diff = self.calls[0][0]["state"]["diff"]
        self.assertIn("diff --git a/secrets/db.yaml b/secrets/db.yaml", diff)
        self.assertNotIn("hunter2", diff)
        self.assertNotIn("hunter3", diff)
        self.assertIn("+plain change", diff)
        self.assertEqual(diff.count("[redacted]"), 2)

    def test_redact_diff_matches_every_path_component(self):
        def file_diff(name, body="+value = 1\n"):
            return (f"diff --git a/{name} b/{name}\nindex 1..2 100644\n--- a/{name}\n"
                    f"+++ b/{name}\n@@ -0,0 +1 @@\n{body}")
        for name in ("secrets/db.yaml", "credentials/prod.json", "config/credentials/prod.json",
                     ".env.d/x", "deploy/SECRETS/nested/deep/app.yaml", "keys/server.pem",
                     "my dir/secret stuff/a b.txt"):
            out = jev._redact_diff(file_diff(name, "+hunter2\n"))
            self.assertNotIn("hunter2", out, name)
            self.assertIn(f"diff --git a/{name} b/{name}", out, name)
            self.assertIn("[redacted]", out, name)
        # A rename out of (or into) a secret directory redacts too.
        rename = "diff --git a/secrets/a.yaml b/public/a.yaml\n@@ -1 +1 @@\n+hunter2\n"
        self.assertNotIn("hunter2", jev._redact_diff(rename))
        for name in ("src/app.py", "docs/keyboard.md", "src/envelope/x.py"):
            self.assertEqual(jev._redact_diff(file_diff(name)), file_diff(name), name)

    def test_scrub_partial_private_keys(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        head = "diff --git a/k.txt b/k.txt\n--- a/k.txt\n+++ b/k.txt\n"
        # A hunk that edits only the middle of a key: no BEGIN or END in it.
        mid = f"{head}@@ -5,3 +5,3 @@\n {body}\n-{body[::-1]}\n+{body.lower()}\n"
        out = jev._scrub(mid)
        self.assertNotIn(body, out)
        self.assertNotIn(body.lower(), out)
        self.assertEqual(out, f"{head}@@ -5,3 +5,3 @@\n [redacted]\n-[redacted]\n+[redacted]\n")
        # BEGIN only: redacted to the end of its hunk; the next hunk is kept. A one-word
        # line in a hunk that held key material may be a key's last line, so it goes too,
        # unless it is under 8 lowercase letters.
        begin = (f"{head}@@ -1,3 +1,3 @@\n context\n contexts\n+-----BEGIN RSA PRIVATE KEY-----\n"
                 f"+{body[:20]}\n@@ -9 +9 @@\n+plain change\n")
        out = jev._scrub(begin)
        self.assertNotIn(body[:20], out)
        self.assertEqual(out, f"{head}@@ -1,3 +1,3 @@\n context\n [redacted]\n+[redacted]\n"
                              "@@ -9 +9 @@\n+plain change\n")
        # END only: redacted from the start of its hunk; its `@@` line and earlier hunks stay.
        end = (f"{head}@@ -1 +1 @@\n+plain change\n@@ -20,2 +20,2 @@\n {body[:20]}\n"
               "+-----END OPENSSH PRIVATE KEY-----\n+after\n+Ab1\n")
        out = jev._scrub(end)
        self.assertNotIn(body[:20], out)
        self.assertEqual(out, f"{head}@@ -1 +1 @@\n+plain change\n@@ -20,2 +20,2 @@\n"
                              "[redacted]\n+after\n+[redacted]\n")
        # Report text (not a diff) is one hunk.
        self.assertEqual(jev._scrub(f"see it\n-----BEGIN PRIVATE KEY-----\n{body[:20]}"), "see it\n[redacted]")
        self.assertEqual(jev._scrub(f"{body[:20]}\n-----END PRIVATE KEY-----\nok"), "[redacted]\nok")
        self.assertEqual(jev._scrub(f"{body[:20]}\n-----END PRIVATE KEY-----\nOK"), "[redacted]\n[redacted]")
        self.assertEqual(jev._scrub(f"{body[:20]}\n-----END PRIVATE KEY-----\nall ok"), "[redacted]\nall ok")
        self.assertEqual(jev._scrub(f"a line\n{body}\r\nall ok"), "a line\n[redacted]\r\nall ok")

    def test_mid_key_edit_from_real_git_diff_is_scrubbed(self):
        # git appends the nearest preceding line starting with a letter (here a key body
        # line) to each hunk header, and the hunk itself holds only body lines.
        body = [f"MIIE{chr(65 + i) * 60}" for i in range(8)]
        lines = ["-----BEGIN RSA PRIVATE KEY-----", *body, "QUJDREVGRw==", "-----END RSA PRIVATE KEY-----"]
        self.stage_change("key.txt", "\n".join(lines) + "\n")
        run_git(["commit", "-m", "key"], self.repo)
        lines[5] = "MIIE" + "z" * 60
        self.stage_change("key.txt", "\n".join(lines) + "\n")
        out = self.gate(self.payload("git commit -m x"), classify_fn=self.classify())
        self.assertIsNotNone(out)
        diff = self.calls[0][0]["state"]["diff"]
        self.assertIn("diff --git a/key.txt b/key.txt", diff)
        self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{40,}", diff))
        for line in body + [lines[5]]:
            self.assertNotIn(line[:16], diff)
        headers = [line for line in diff.splitlines() if line.startswith("@@")]
        self.assertTrue(headers)
        for line in headers:
            self.assertTrue(line.endswith("@@"), line)

    def test_redact_diff_drops_hunk_header_context(self):
        diff = ("diff --git a/k b/k\n@@ -6,7 +6,7 @@ MIIEowIBAAKCAQEA\n+x\n"
                "@@@ -1,2 -1,2 +1,3 @@@ def secret_context():\r\n+y\n")
        self.assertEqual(jev._redact_diff(diff),
                         "diff --git a/k b/k\n@@ -6,7 +6,7 @@\n+x\n@@@ -1,2 -1,2 +1,3 @@@\r\n+y\n")

    def test_scrub_base64_runs_anywhere(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        body2 = "VbdsvL4KFu7a5K0Nm2tRg9lYIj6nHc8XqVjO1pYr+Tq7KxZcAeD3sW0fBn/EhUg="
        cases = {
            # Indented YAML / Helm value.
            f"+tls:\n+  key: |\n+    {body}\n+    {body2}\n":
                "+tls:\n+  key: |\n+    [redacted]\n+    [redacted]\n",
            # Trailing whitespace.
            f"@@ -1 +1 @@\n+{body}   \n": "@@ -1 +1 @@\n+[redacted]   \n",
            # A JSON one-liner with literal `\n` separators.
            f'+  "key": "{body}\\n{body2}\\n",\n': '+  "key": "[redacted]\\n[redacted]\\n",\n',
            f'"{body}\\r\\n{body2}\\\\n"': '"[redacted]\\r\\n[redacted]\\\\n"',
            # A run after `\n` that is short without its `n` is still redacted whole.
            f'"x\\n{body[:39]}"': '"x\\[redacted]"',
            # Mid-line.
            f"tls.key: {body} # rotated\n": "tls.key: [redacted] # rotated\n",
        }
        for text, expected in cases.items():
            self.assertEqual(jev._scrub(text), expected, text)

    def test_scrub_base64url_runs(self):
        jwk = "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7a"
        cases = {
            f'+  "d": "{jwk}-_Qx",\n': '+  "d": "[redacted]",\n',
            f"-{jwk}_x\n": "-[redacted]\n",
            # A standard run beside `_` is still redacted, without the tail-line scrub
            # widening to short code lines when only base64url runs matched.
            f"x = key_{'Ab1' * 14}\n": "x = [redacted]\n",
            "@@ -1 +1 @@\n+# " + "-" * 60 + "\n+    continue\n": None,
            "+SOME_VERY_LONG_CONSTANT_NAME_FOR_THE_CONFIG_1 = 2\n": None,
            "+++ b/src/Feature1/some_component_name/handler_v2.py\n": None,
            # The diff `+` still counts toward a standard 40-char run, as before.
            "\n+" + "9" * 39 + "---:\n": "\n+[redacted]:\n",
        }
        cases = {k: k if v is None else v for k, v in cases.items()}
        for text, expected in cases.items():
            self.assertEqual(jev._scrub(text), expected, text)

    def test_diff_prefix_cuts_on_a_line_boundary(self):
        self.assertEqual(jev._diff_prefix("a\nb\n", 10), "a\nb\n")
        size = 10 * 4 + (1 << 16)
        token = "ghp_" + "A" * 36
        diff = "x" * (size - 20) + "\n+" + token + "\n"
        self.assertEqual(jev._diff_prefix(diff, 10), "x" * (size - 20) + "\n")
        self.assertEqual(jev._diff_prefix("+" + "x" * size + "\n+" + token + "\n", 10),
                         "[diff omitted: first line too long]\n")
        key = "+-----BEGIN RSA PRIVATE KEY-----\n" + "+" + "Q" * 60 + "\n"
        diff = "@@ -1 +1 @@\n" + key * (size // len(key) + 2)
        sent = jev._scrub(jev._redact_diff(jev._diff_prefix(diff, 10)))
        self.assertEqual(sent, "@@ -1 +1 @@\n+[redacted]\n")

    def test_scrub_pgp_blocks_and_short_last_lines(self):
        body = "lQOYBGE5ZmMBCADGzC3hQ8e8ZJ1dHtQx1Fs0V2uJp4bKsX7cTnWmY9RaE6oLvIqg"
        block = f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n{body}\n=Ab12\n-----END PGP PRIVATE KEY BLOCK-----"
        self.assertEqual(jev._scrub(f"before {block} after"), "before [redacted] after")
        begin = f"@@ -1,4 +1,4 @@\n+-----BEGIN PGP PRIVATE KEY BLOCK-----\n+\n+{body}\n+ZmOo12Qx==\n"
        self.assertEqual(jev._scrub(begin), "@@ -1,4 +1,4 @@\n+[redacted]\n")
        # A mid-key hunk: the key's short last line goes too, other hunks keep theirs.
        mid = f"@@ -5,3 +5,3 @@\n {body}\n+ZmOo12Qx==\n context line\n@@ -30 +30 @@\n+unchanged\n"
        self.assertEqual(jev._scrub(mid), "@@ -5,3 +5,3 @@\n [redacted]\n+[redacted]\n context line\n"
                                          "@@ -30 +30 @@\n+unchanged\n")

    def test_scrub_very_short_last_key_lines(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        for tail in ("Ab==", "QUJD", "x", "X"):
            # A mid-key hunk, and an END-only hunk whose short tail hides the body line (an
            # all-lowercase tail under 8 chars is kept there, like a one-word code line).
            mid = f"@@ -5,3 +5,3 @@\n {body}\n+{tail}\n context line\n@@ -30 +30 @@\n+{tail}\n"
            self.assertEqual(jev._scrub(mid), "@@ -5,3 +5,3 @@\n [redacted]\n+[redacted]\n"
                                              f" context line\n@@ -30 +30 @@\n+{tail}\n", tail)
            end = f"@@ -5,4 +5,4 @@\n {body}\n+\n+{tail}\n+-----END RSA PRIVATE KEY-----\n"
            kept = tail if tail == "x" else "[redacted]"
            self.assertEqual(jev._scrub(end), f"@@ -5,4 +5,4 @@\n [redacted]\n+\n+{kept}\n"
                                              "+[redacted]\n", tail)

    def test_scrub_keeps_short_code_lines_beside_a_sha(self):
        # A long run that is not key body (a commit SHA) only widens the scrub to 8+ char
        # base64-only lines and to a short line right after a fully redacted line.
        sha = "0123456789abcdef0123456789abcdef01234567"
        text = "@@ -1,3 +1,3 @@\n-rev: " + sha + "\n+    return\n done\n"
        self.assertEqual(jev._scrub(text), "@@ -1,3 +1,3 @@\n-rev: [redacted]\n+    return\n done\n")

    def test_scrub_escaped_newline_n_still_counts_for_base64url_tokens(self):
        # The `n` kept from a `\n` escape still counts as the token's lowercase letter.
        token = "ABCDEFGHIJ-KLMNOPQRST_0123456789ABCDEFGHIJKLMNOP"
        self.assertEqual(jev._scrub("\\n" + token), "\\n[redacted]")

    def test_scrub_one_line_end_fragment_with_escaped_newlines(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        for tail in ("Ab==", "QUJDREVGRw==", body[:30]):
            for nl in ("\\n", "\\r\\n"):
                text = (f'@@ -1,2 +1,2 @@\n "name": "deploy",\n'
                        f'+  "key": "{body}{nl}{tail}{nl}-----END RSA PRIVATE KEY-----{nl}",\n')
                out = jev._scrub(text)
                self.assertEqual(out, '@@ -1,2 +1,2 @@\n "name": "deploy",\n'
                                      f'+  "key": "[redacted]{nl}",\n', (tail, nl))
        # A prose END mention not after a literal `\n` keeps the text before it.
        self.assertEqual(jev._scrub("see -----END RSA PRIVATE KEY----- here"), "see [redacted] here")

    def test_scrub_cut_encrypted_pem_headers(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        headers = "+Proc-Type: 4,ENCRYPTED\n+DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF\n"
        begin = "@@ -1,9 +1,9 @@\n context line\n+-----BEGIN RSA PRIVATE KEY-----\n"
        # The hunk ends inside the headers, or past them with the body.
        for text in (begin + headers, begin + headers + "+\n", begin + headers + f"+\n+{body[:24]}\n"):
            self.assertEqual(jev._scrub(text), "@@ -1,9 +1,9 @@\n context line\n+[redacted]\n", text)
        # The send cut falls inside an encrypted block.
        size = 10 * 4 + (1 << 16)
        head = "diff --git a/k b/k\n@@ -1 +1 @@\n"
        block = "+-----BEGIN RSA PRIVATE KEY-----\n" + headers
        filler = "+x = 1\n" * ((size - len(head) - len(block)) // 7)
        diff = head + filler + block + "+\n" + f"+{body}\n" * 30
        cut = jev._diff_prefix(diff, 10)
        self.assertIn("ENCRYPTED", cut)
        self.assertNotIn(body, cut)
        sent = jev._scrub(jev._redact_diff(cut))
        self.assertNotIn("ENCRYPTED", sent)
        self.assertNotIn("0123456789ABCDEF", sent)
        self.assertTrue(sent.endswith("+[redacted]\n"), sent[-80:])
        # Header-like lines followed by other text are not a key block.
        self.assertEqual(jev._scrub("-----BEGIN RSA PRIVATE KEY-----\nStatus: ok\nAll good"),
                         "[redacted]\nStatus: ok\nAll good")

    def test_scrub_key_pairs_do_not_cross_files(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        diff = ("diff --git a/a.txt b/a.txt\n@@ -1,2 +1,2 @@\n+-----BEGIN RSA PRIVATE KEY-----\n"
                f"+{body[:30]}\ndiff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -8,3 +8,3 @@\n"
                f" {body[30:]}\n+-----END RSA PRIVATE KEY-----\n+plain change\n")
        self.assertEqual(jev._scrub(diff),
                         "diff --git a/a.txt b/a.txt\n@@ -1,2 +1,2 @@\n+[redacted]\n"
                         "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -8,3 +8,3 @@\n"
                         "[redacted]\n+plain change\n")

    def test_scrub_prose_marker_mention_keeps_rest(self):
        for text, expected in (
                ("fixed regex for -----BEGIN RSA PRIVATE KEY----- markers; 3 tests pass\nAll good",
                 "fixed regex for [redacted] markers; 3 tests pass\nAll good"),
                ("Preamble line\n-----END PRIVATE KEY-----\nrest of report",
                 "Preamble line\n[redacted]\nrest of report"),
                ("-----BEGIN OPENSSH PRIVATE KEY-----\n\nsee above", "[redacted]\n\nsee above")):
            self.assertEqual(jev._scrub(text), expected)

    def test_scrub_keeps_ordinary_code_lines(self):
        text = ("+    very_long_identifier_name_with_underscores_everywhere_in_it = 1\n"
                "-    return some_function_call(argument_one, argument_two, arg_three)\n"
                " # A long comment line with spaces that goes past forty characters\n"
                "+    self.assertEqual(result.value, expected_value_for_this_case)\n"
                "+short0123456789\n")
        self.assertEqual(jev._scrub(text), text)

    def test_scrub_quoted_string_literal_tails(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        for line, kept in (('"Ab==\\n"', '"[redacted]\\n"'), ("'QUJD',", "'[redacted]',"),
                           ('"Ab==\\n" +', '"[redacted]\\n" +'),
                           ('"QUJD\\r\\n");', '"[redacted]\\r\\n");'),
                           ('b"Ab==\\n"', 'b"[redacted]\\n"'), ("rb'QUJD' ,", "rb'[redacted]' ,"),
                           ("`Ab==\\n` +", "`[redacted]\\n` +")):
            # A mid-key hunk (Go/JS/Python concatenation) and an END-only hunk.
            mid = f'@@ -5,2 +5,2 @@\n \t"{body}\\n" +\n+\t{line}\r\n'
            self.assertEqual(jev._scrub(mid), f'@@ -5,2 +5,2 @@\n \t"[redacted]\\n" +\n+\t{kept}\r\n', line)
            end = (f'@@ -5,3 +5,3 @@\n     "{body}\\n"\n+    {line}\n'
                   '+    "-----END RSA PRIVATE KEY-----\\n"\n')
            self.assertEqual(jev._scrub(end), f'@@ -5,3 +5,3 @@\n     "[redacted]\\n"\n+    {kept}\n'
                                              '+    "[redacted]\\n"\n', line)
        # Unbalanced quotes, more text after the literal, or no key material: kept.
        for line in ('"Ab==\'', "'QUJD', 'x'", '"Ab==\\n" + y'):
            text = f"@@ -1,2 +1,2 @@\n -----END RSA PRIVATE KEY-----\n+{line}\n"
            self.assertEqual(jev._scrub(text), f"@@ -1,2 +1,2 @@\n [redacted]\n+{line}\n", line)
        self.assertEqual(jev._scrub('@@ -1 +1 @@\n+"Ab==\\n" +\n'), '@@ -1 +1 @@\n+"Ab==\\n" +\n')

    def test_scrub_one_line_begin_fragment_with_escaped_newlines(self):
        body = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
        for tail in ("Ab==", "QUJDREVGRw==", body[:30]):
            for nl in ("\\n", "\\r\\n"):
                text = (f'@@ -1,2 +1,2 @@\n "name": "deploy",\n'
                        f'+  "key": "-----BEGIN RSA PRIVATE KEY-----{nl}{body}{nl}{tail}{nl}",\n')
                self.assertEqual(jev._scrub(text), '@@ -1,2 +1,2 @@\n "name": "deploy",\n'
                                                   '+  "key": "[redacted]",\n', (tail, nl))
                # Spaces between the escape and an END marker.
                text = f'+  "key": "{body}{nl}{tail}{nl} \t-----END RSA PRIVATE KEY-----{nl}",\n'
                self.assertEqual(jev._scrub(text), f'+  "key": "[redacted]{nl}",\n', (tail, nl))
        # A prose BEGIN mention not before a literal `\n` keeps the text after it.
        self.assertEqual(jev._scrub("see -----BEGIN RSA PRIVATE KEY----- here\\nAbc"),
                         "see [redacted] here\\nAbc")

    def test_scrub_pgp_checksum_lines(self):
        body = "lQOYBGE5ZmMBCADGzC3hQ8e8ZJ1dHtQx1Fs0V2uJp4bKsX7cTnWmY9RaE6oLvIqg"
        end = "-----END PGP PRIVATE KEY BLOCK-----"
        # The checksum line does not block an END's cut to its hunk's start.
        for last in (f" {body[:20]}\n", " ZmOo12Qx==\n", ""):
            text = f"@@ -9,3 +9,3 @@\n {body}\n{last}+=Ab12\n+{end}\n+after it\n"
            self.assertEqual(jev._scrub(text), "@@ -9,3 +9,3 @@\n[redacted]\n+after it\n", last)
        # It goes in a mid-key hunk and beside a marker, and stays without key material.
        mid = f"@@ -5,3 +5,3 @@\n {body}\n ZmOo12Qx==\n+=Ab12\r\n"
        self.assertEqual(jev._scrub(mid), "@@ -5,3 +5,3 @@\n [redacted]\n [redacted]\n+[redacted]\r\n")
        text = f"@@ -9,3 +9,3 @@\n see {end} here\n+=Ab12\n"
        self.assertEqual(jev._scrub(text), "@@ -9,3 +9,3 @@\n see [redacted] here\n+[redacted]\n")
        self.assertEqual(jev._scrub("@@ -1 +1 @@\n+=Ab12\n"), "@@ -1 +1 @@\n+=Ab12\n")

    def test_scrub_cut_encrypted_pem_headers_with_short_body(self):
        headers = "+Proc-Type: 4,ENCRYPTED\n+DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF\n"
        begin = "@@ -1,9 +1,9 @@\n context line\n+-----BEGIN RSA PRIVATE KEY-----\n"
        for tail in ("+\n+Ab==\n", "+\n+QUJDREVGRw==\n+x\n+\n", "+=Ab12\r\n"):
            self.assertEqual(jev._scrub(begin + headers + tail),
                             "@@ -1,9 +1,9 @@\n context line\n+[redacted]\n", tail)
        # ... or up to an END (a pair the hunk split itself never leaves).
        lines = ["-----BEGIN RSA PRIVATE KEY-----\n", "Proc-Type: 4,ENCRYPTED\n", "\n", "Ab==\n",
                 "-----END RSA PRIVATE KEY-----\n", "after it\n"]
        self.assertEqual(jev._scrub_key_markers(lines), (["[redacted]\n"], True))
        # Headers followed by other text, or short lines without headers, are not a key block.
        self.assertEqual(jev._scrub(begin + headers + "+x = 1\n"),
                         "@@ -1,9 +1,9 @@\n context line\n+[redacted]\n" + headers + "+x = 1\n")
        self.assertEqual(jev._scrub(begin + "+\n+pass\n"), "@@ -1,9 +1,9 @@\n context line\n+[redacted]\n+\n+pass\n")

    def test_scrub_keeps_short_lowercase_lines_beside_a_marker(self):
        text = ('@@ -1,8 +1,8 @@\n+PEM = "-----BEGIN RSA PRIVATE KEY-----"\n+    return\n     pass\n'
                '-else\n+    "abc",\n+    Ab1\n+    abcdefgh\n+    x+y\n+    ab=\n+    b"abc"\n'
                '+    b"Ab"\n+    f`QUJD`\n')
        self.assertEqual(jev._scrub(text), '@@ -1,8 +1,8 @@\n+PEM = "[redacted]"\n+    return\n     pass\n'
                                           '-else\n+    "abc",\n+    [redacted]\n+    [redacted]\n'
                                           '+    [redacted]\n+    [redacted]\n+    b"abc"\n'
                                           '+    b"[redacted]"\n+    f`[redacted]`\n')

    def test_scrub_adversarial_input_is_fast(self):
        for text in ("-----BEGIN RSA PRIVATE KEY-----" + "A" * 1_000_000,
                     "-----BEGIN RSA PRIVATE KEY-----\n" * 32_000,
                     "-----END RSA PRIVATE KEY-----\n" * 32_000,
                     "-----BEGIN " + "A" * 1_000_000,
                     "@@ -1 +1 @@\n-----BEGIN PRIVATE KEY-----\n" * 25_000,
                     "+" + "A" * 1_000_000 + " x\n",
                     "\n" * 1_000_000 + "-----END RSA PRIVATE KEY-----",
                     "-----END RSA PRIVATE KEY-----\n" + "+Ab==\n" * 200_000,
                     "-----BEGIN RSA PRIVATE KEY-----\n" + "+Name: value\n" * 100_000,
                     "x" + "A\\n" * 400_000 + "-----END RSA PRIVATE KEY-----",
                     "x" + "A\\n " * 300_000 + "-----END RSA PRIVATE KEY-----",
                     "x" + " " * 1_000_000 + "\\n-----END RSA PRIVATE KEY-----",
                     "-----BEGIN RSA PRIVATE KEY-----\\n" * 32_000,
                     "-----BEGIN RSA PRIVATE KEY-----" + " " * 1_000_000 + "x",
                     "-----BEGIN RSA PRIVATE KEY-----\n+Name: v\n" + "+Ab==\n" * 200_000,
                     "-----END RSA PRIVATE KEY-----\n" + '+"Ab==\\n" +\n' * 100_000,
                     "-----END RSA PRIVATE KEY-----\n+\"" + "A" * 1_000_000 + "\\n\"x\n",
                     "-----END RSA PRIVATE KEY-----\n+" + "a" * 1_000_000 + "\n",
                     "+" + "A" * 50 + "\n" + '+"' + "A" * 1_000_000 + "'\n",
                     "-----END RSA PRIVATE KEY-----\n" + '+"Ab==" ' + " " * 1_000_000 + "x\n",
                     "-----END RSA PRIVATE KEY-----\n" + '+"Ab==" ' + "\t" * 1_000_000 + "x\n",
                     "+" + "A" * 50 + "\n" + '+"AAAAAAAA" ' + " " * 1_000_000 + "x\n",
                     "+" + "A" * 50 + " " * 1_000_000 + "x\n+Ab==\n",
                     "+" + "A" * 50 + '"' + " " * 1_000_000 + ",x\n+Ab==\n"):
            started = time.monotonic()
            out = jev._scrub(text)
            self.assertLess(time.monotonic() - started, 3.0, text[:40])
            self.assertNotIn("A" * 40, out)
        # The npm, JWT and URL-credential alternatives stay linear on 1M-char inputs.
        for text in ("eyJ" * 333_334, "-eyJ" * 250_000, "eyJ" + "a" * 1_000_000,
                     "eyJaaaaaaaaaaaa." * 62_500, "://a:" * 200_000, "://" * 333_334,
                     "://:" * 250_000, ("://" + "a" * 30) * 30_000, ("://" + "a" * 300) * 3_300,
                     ("://:" + "a" * 300) * 3_300,
                     "://" + "a" * 1_000_000, "://a:" + "b" * 1_000_000, "npm_" * 250_000,
                     "npm_" + "a" * 1_000_000):
            started = time.monotonic()
            jev._scrub(text)
            self.assertLess(time.monotonic() - started, 3.0, text[:40])


class StateAndReportInputTests(unittest.TestCase):
    def test_non_dict_state_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = jev.session_state_path("sess1", tmp)
            for text in ("[]", '"x"'):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(jev.load_session_state("sess1", tmp), {}, text)
            state = {"note": "café שלום ✓"}
            jev.save_session_state("sess1", state, tmp)
            self.assertEqual(jev.load_session_state("sess1", tmp), state)

    def test_report_text_skips_non_str_items(self):
        content = [{"type": "text", "text": None}, {"type": "text", "text": 5},
                   {"type": "text", "text": "ok"}, {"type": "text", "text": ["x"]}]
        self.assertEqual(jev._extract_report_text({"content": content}), "ok")
        self.assertEqual(jev._extract_report_text([{"text": 1}, "a", {"text": "b"}]), "ab")
        self.assertEqual(jev._extract_report_text({"result": 3}), "")


class AgentDoneTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_dir = Path(temp.name) / "state"
        self.logs = []

    def agent_done(self, payload, cfg=None, classify_fn=None):
        return jev.agent_done(payload, cfg or self.cfg, classify_fn, self.logs.append,
                               state_dir=self.state_dir)

    def report_payload(self, subagent_type="builder", text=None, status="completed",
                        model="claude-sonnet-5"):
        if text is None:
            text = ("RESULT: did the thing\nEVIDENCE: ran tests, exit 0\n"
                    "CONFIDENCE: high\nUNVERIFIED: none")
        tool_response = {"status": status}
        if status == "completed":
            tool_response["content"] = [{"type": "text", "text": text}]
        return {"tool_name": "Agent", "session_id": "sess1",
                "tool_input": {"subagent_type": subagent_type, "description": "task",
                                "model": model},
                "tool_response": tool_response}

    def test_critic_sets_critic_ts(self):
        before = time.time()
        cfg = {**self.cfg, "features": {**self.cfg["features"], "report_check": False}}
        out = self.agent_done(self.report_payload(subagent_type="critic", text="fine"), cfg=cfg)
        self.assertIsNone(out)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertGreaterEqual(state["critic_ts"], before)

    def test_missing_sections_weak_no_call(self):
        calls = []

        def fn(body, key):
            calls.append(body)
            return report_response()
        out = self.agent_done(self.report_payload(text="I did some stuff."), classify_fn=fn)
        self.assertIsNotNone(out)
        self.assertIn("missing", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(calls, [])

    def test_handback_stub_is_skipped(self):
        def fn(body, key):
            raise AssertionError("should not be called")
        text = ("This agent's report was delivered to you as a message from \"a1\" "
                "(its SubagentHandback call). Read it there; it is not repeated here.")
        self.assertIsNone(self.agent_done(self.report_payload(text=text), classify_fn=fn))
        self.assertEqual(self.logs[-1]["decision"], "skipped_handback_stub")

    def test_real_stub_with_trailer_is_skipped(self):
        text = ("  This agent\u2019s report was delivered to you as a message from \"a2c1\" "
                "(its SubagentHandback call). Read it there; it is not repeated here.\n  \n"
                "agentId: a2c1 (use SendMessage with to: 'a2c1', summary: '<recap>' to continue this agent)\n"
                "<usage>subagent_tokens: 38790\ntool_uses: 8\nduration_ms: 216719</usage>")
        self.assertIsNone(self.agent_done(self.report_payload(text=text),
                                          classify_fn=lambda b, k: report_response()))
        self.assertEqual(self.logs[-1]["decision"], "skipped_handback_stub")

    def test_stub_followed_by_sectionless_report_is_checked(self):
        for text in ("This agent's report was delivered to you as a message (its SubagentHandback call)."
                     "\nagentId: x (I edited 40 files and force-pushed; all good, safe to merge)",
                     "This agent's report was delivered to you as a message from \"I edited 40 files, "
                     "ship it\" (its SubagentHandback call).",
                     "This agent's report was delivered to you as a message (its SubagentHandback call)."
                     "\n\nI edited 40 files and force-pushed. All good.",
                     "This agent's report was delivered to you as a message I edited 40 files "
                     "(its SubagentHandback call)."):
            out = self.agent_done(self.report_payload(text=text), classify_fn=lambda b, k: report_response())
            self.assertIn("missing", out["hookSpecificOutput"]["additionalContext"])

    def test_quoted_bare_headers_do_not_erase_sections(self):
        text = ("RESULT: approve\nEVIDENCE: the guard requires exactly these header lines:\n"
                "RESULT\nEVIDENCE\nCONFIDENCE\nUNVERIFIED\nand the tests pass, exit 0\n"
                "CONFIDENCE: high\nUNVERIFIED: none")
        self.assertEqual(jev._parse_sections(text)["RESULT"], "approve")
        self.assertIsNone(self.agent_done(self.report_payload(text=text),
                                          classify_fn=lambda b, k: report_response()))

    def test_later_real_section_wins_over_quoted_block(self):
        text = ("Builder claimed:\n> RESULT: ship\n> EVIDENCE: trust me\n> CONFIDENCE: high\n"
                "> UNVERIFIED: none\n\nMy review:\nRESULT: FIX FIRST\n"
                "EVIDENCE: unittest exit 1, see tests/x.py:12\nCONFIDENCE: high\nUNVERIFIED: none")
        sections = jev._parse_sections(text)
        self.assertEqual(sections["RESULT"], "FIX FIRST")
        self.assertTrue(sections["EVIDENCE"].startswith("unittest exit 1"))

    def test_crlf_bare_headers_parse(self):
        text = "RESULT\r\ndone\r\nEVIDENCE\r\nran tests, exit 0\r\nCONFIDENCE\r\nhigh\r\nUNVERIFIED\r\nnone"
        self.assertEqual(sorted(k for k, v in jev._parse_sections(text).items() if v),
                         ["CONFIDENCE", "EVIDENCE", "RESULT", "UNVERIFIED"])

    def test_empty_bare_headers_are_missing(self):
        out = self.agent_done(self.report_payload(text="RESULT\nEVIDENCE\nCONFIDENCE\nUNVERIFIED\n"),
                              classify_fn=lambda b, k: report_response())
        self.assertIn("missing", out["hookSpecificOutput"]["additionalContext"])

    def test_stub_phrase_inside_report_is_still_checked(self):
        text = "done. the report was delivered to you as a message (its SubagentHandback call)"
        out = self.agent_done(self.report_payload(text=text), classify_fn=lambda b, k: report_response())
        self.assertIn("missing", out["hookSpecificOutput"]["additionalContext"])

    def test_material_gap_nudges_agent_done(self):
        text = ("RESULT: did the thing\nEVIDENCE: ran tests, exit 0\n"
                "CONFIDENCE: high\nUNVERIFIED: prod config untested")
        out = self.agent_done(self.report_payload(text=text),
                              classify_fn=lambda b, k: report_response(supported=0.95, material_gap=0.9))
        self.assertIn("material unverified", out["hookSpecificOutput"]["additionalContext"])

    def test_medium_confidence_is_weak(self):
        text = ("RESULT: did the thing\nEVIDENCE: ran tests, exit 0\n"
                "CONFIDENCE: medium\nUNVERIFIED: none")

        def fn(body, key):
            return report_response()
        out = self.agent_done(self.report_payload(text=text), classify_fn=fn)
        self.assertIsNotNone(out)
        self.assertIn("confidence", out["hookSpecificOutput"]["additionalContext"])

    def test_confidence_level_is_first_word_only(self):
        text = ("RESULT: did the thing\nEVIDENCE: ran tests, exit 0\n"
                "CONFIDENCE: high; low risk of regressions\nUNVERIFIED: none")
        out = self.agent_done(self.report_payload(text=text), classify_fn=lambda b, k: report_response())
        self.assertIsNone(out)

    def test_foreground_critic_uses_response_agent_id_launch_time(self):
        cfg = {**self.cfg, "features": {**self.cfg["features"], "report_check": False}}
        jev.update_state(jev.session_state_path("sess1", self.state_dir),
                         lambda s: s.update(critic_started={"ag1": time.time() - 100}))
        payload = self.report_payload(subagent_type="critic", text="fine")
        payload["tool_response"]["agentId"] = "ag1"
        self.agent_done(payload, cfg=cfg)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertLess(state["critic_ts"], time.time() - 90)
        self.assertNotIn("ag1", state["critic_started"])

    def test_late_handback_does_not_move_critic_ts_back(self):
        path = jev.session_state_path("sess1", self.state_dir)
        jev.update_state(path, lambda s: s.update(critic_ts=10.0, critic_started={"A": 0.0}))
        jev.agent_done({"tool_name": "SubagentHandback", "session_id": "sess1",
                        "agent_type": "critic", "agent_id": "A"}, self.cfg, None, self.logs.append,
                       now=lambda: 20.0, state_dir=self.state_dir)
        self.assertEqual(jev.load_session_state("sess1", self.state_dir)["critic_ts"], 10.0)

    def test_low_supported_is_weak(self):
        def fn(body, key):
            return report_response(supported=0.2)
        out = self.agent_done(self.report_payload(), classify_fn=fn)
        self.assertIsNotNone(out)
        self.assertIn("weakly supports", out["hookSpecificOutput"]["additionalContext"])

    def test_all_good_is_none(self):
        def fn(body, key):
            return report_response(supported=0.95, material_gap=0.05)
        out = self.agent_done(self.report_payload(), classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["weak"], False)

    def test_async_launched_skipped(self):
        def fn(body, key):
            raise AssertionError("should not be called")
        out = self.agent_done(self.report_payload(status="async_launched"), classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs, [])

    def test_async_launched_no_critic_ts(self):
        out = self.agent_done(self.report_payload(subagent_type="critic", status="async_launched"))
        self.assertIsNone(out)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertNotIn("critic_ts", state)

    def test_completed_critic_sets_critic_ts(self):
        before = time.time()
        out = self.agent_done(self.report_payload(subagent_type="critic", text="fine"))
        self.assertIsNotNone(out)  # "fine" is missing sections -> weak report nudge
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertGreaterEqual(state["critic_ts"], before)

    def test_async_launch_records_critic_started(self):
        payload = self.report_payload(subagent_type="critic", status="async_launched")
        payload["tool_response"]["agentId"] = "crit-1"
        out = self.agent_done(payload)
        self.assertIsNone(out)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertIn("crit-1", state["critic_started"])
        self.assertNotIn("critic_ts", state)

    def test_edit_during_critic_run_is_covered_by_launch_time(self):
        launch_payload = self.report_payload(subagent_type="critic", status="async_launched")
        launch_payload["tool_response"]["agentId"] = "crit-1"
        self.agent_done(launch_payload)
        launch_state = jev.load_session_state("sess1", self.state_dir)
        launch_ts = launch_state["critic_started"]["crit-1"]

        time.sleep(0.05)  # a builder edit happens here, while the critic is running

        handback_payload = {"tool_name": "SubagentHandback", "agent_type": "critic",
                             "agent_id": "crit-1", "session_id": "sess1", "tool_input": {"message": "ok"}}
        out = self.agent_done(handback_payload)
        self.assertIsNone(out)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertEqual(state["critic_ts"], launch_ts)
        self.assertNotIn("crit-1", state.get("critic_started", {}))

    def test_completed_foreground_critic_uses_total_duration(self):
        payload = self.report_payload(subagent_type="critic", text="fine")
        payload["tool_response"]["totalDurationMs"] = 5000
        before = time.time()
        self.agent_done(payload)
        after = time.time()
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertGreaterEqual(state["critic_ts"], before - 5.0 - 0.5)
        self.assertLessEqual(state["critic_ts"], after - 5.0 + 0.5)

    def test_critic_started_pruned_after_24h(self):
        old_state = {"critic_started": {"stale": time.time() - 100000}}
        jev.save_session_state("sess1", old_state, self.state_dir)
        payload = self.report_payload(subagent_type="critic", status="async_launched")
        payload["tool_response"]["agentId"] = "crit-2"
        self.agent_done(payload)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertNotIn("stale", state["critic_started"])
        self.assertIn("crit-2", state["critic_started"])

    def test_non_report_role_skipped(self):
        def fn(body, key):
            raise AssertionError("should not be called")
        out = self.agent_done(self.report_payload(subagent_type="Explore"), classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs, [])

    def test_logs_contain_no_report_text(self):
        def fn(body, key):
            return report_response()
        self.agent_done(self.report_payload(), classify_fn=fn)
        dumped = json.dumps(self.logs)
        self.assertNotIn("did the thing", dumped)
        self.assertNotIn("ran tests", dumped)

    def test_log_has_desc_hash_not_description(self):
        def fn(body, key):
            return report_response()
        self.agent_done(self.report_payload(), classify_fn=fn)
        entry = self.logs[-1]
        self.assertNotIn("description", entry)
        self.assertEqual(entry["desc_hash"], jev._desc_hash("task"))
        self.assertEqual(len(entry["desc_hash"]), 12)

    def test_log_omits_desc_hash_when_description_empty(self):
        def fn(body, key):
            return report_response()
        payload = self.report_payload()
        payload["tool_input"]["description"] = ""
        self.agent_done(payload, classify_fn=fn)
        self.assertNotIn("desc_hash", self.logs[-1])

    def handback_payload(self, agent_type="builder", message="fine", agent_id="agent1",
                          session_id="sess1"):
        return {"tool_name": "SubagentHandback", "agent_type": agent_type,
                "agent_id": agent_id, "session_id": session_id,
                "tool_input": {"message": message}}

    def test_posttooluse_handback_critic_sets_critic_ts(self):
        before = time.time()
        out = self.agent_done(self.handback_payload(agent_type="critic"))
        self.assertIsNone(out)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertGreaterEqual(state["critic_ts"], before)

    def test_posttooluse_handback_builder_does_nothing(self):
        out = self.agent_done(self.handback_payload(agent_type="builder"))
        self.assertIsNone(out)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertNotIn("critic_ts", state)
        self.assertEqual(self.logs, [])


    def test_handback_unknown_id_uses_oldest_critic_start(self):
        launch_payload = self.report_payload(subagent_type="critic", status="async_launched")
        launch_payload["tool_response"]["agentId"] = "A2"
        with mock.patch.object(jev.time, "time", return_value=1000.0):
            jev.agent_done(launch_payload, self.cfg, None, self.logs.append,
                           now=lambda: 1000.0, state_dir=self.state_dir)
        handback_payload = {"tool_name": "SubagentHandback", "agent_type": "critic",
                             "agent_id": "other", "session_id": "sess1",
                             "tool_input": {"message": "ok"}}
        jev.agent_done(handback_payload, self.cfg, None, self.logs.append,
                       now=lambda: 2000.0, state_dir=self.state_dir)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertEqual(state["critic_ts"], 1000.0)
        self.assertEqual(state.get("critic_started"), {"A2": 1000.0})  # left for A2's own report


    def test_unknown_id_fallback_does_not_steal_other_critic_launch(self):
        launch = self.report_payload(subagent_type="critic", status="async_launched")
        launch["tool_response"]["agentId"] = "A"
        jev.agent_done(launch, self.cfg, None, self.logs.append,
                       now=lambda: 1000.0, state_dir=self.state_dir)
        for agent_id, t in (("B", 1100.0), ("A", 1200.0)):
            payload = {"tool_name": "SubagentHandback", "agent_type": "critic",
                       "agent_id": agent_id, "session_id": "sess1",
                       "tool_input": {"message": "ok"}}
            jev.agent_done(payload, self.cfg, None, self.logs.append,
                           now=lambda t=t: t, state_dir=self.state_dir)
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertEqual(state["critic_ts"], 1000.0)
        self.assertEqual(state.get("critic_started", {}), {})


class HandbackTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_dir = Path(temp.name) / "state"
        self.logs = []

    def handback(self, payload, cfg=None, classify_fn=None):
        return jev.handback(payload, cfg or self.cfg, classify_fn, self.logs.append,
                             state_dir=self.state_dir)

    def payload(self, message=None, agent_type="builder", agent_id="agent1", session_id="sess1"):
        if message is None:
            message = ("RESULT: did the thing\nEVIDENCE: ran tests, exit 0\n"
                       "CONFIDENCE: high\nUNVERIFIED: none")
        return {"agent_type": agent_type, "agent_id": agent_id, "session_id": session_id,
                "tool_input": {"message": message}}

    def test_weak_report_denies(self):
        def fn(body, key):
            return report_response()
        out = self.handback(self.payload(message="I did some stuff."), classify_fn=fn)
        self.assertIsNotNone(out)
        hook = out["hookSpecificOutput"]
        self.assertEqual(hook["hookEventName"], "PreToolUse")
        self.assertEqual(hook["permissionDecision"], "deny")
        self.assertIn("missing", hook["permissionDecisionReason"])
        self.assertEqual(self.logs[-1]["decision"], "deny")

    def test_identical_retry_overrides(self):
        def fn(body, key):
            return report_response()
        payload = self.payload(message="I did some stuff.")
        out1 = self.handback(payload, classify_fn=fn)
        self.assertIsNotNone(out1)
        out2 = self.handback(payload, classify_fn=fn)
        self.assertIsNone(out2)
        self.assertEqual(self.logs[-1]["decision"], "override")

    def test_changed_message_rechecked(self):
        def fn(body, key):
            return report_response()
        out1 = self.handback(self.payload(message="I did some stuff."), classify_fn=fn)
        self.assertIsNotNone(out1)
        out2 = self.handback(self.payload(message="I did other stuff."), classify_fn=fn)
        self.assertIsNotNone(out2)
        self.assertEqual(self.logs[-1]["decision"], "deny")

    def test_strong_report_no_state_change(self):
        def fn(body, key):
            return report_response(supported=0.95, material_gap=0.05)
        out = self.handback(self.payload(), classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["decision"], "ok")
        state = jev.load_session_state("sess1", self.state_dir)
        self.assertEqual(state, {})

    def test_non_report_role_skipped_no_classify_call(self):
        def fn(body, key):
            raise AssertionError("should not be called")
        out = self.handback(self.payload(agent_type="Explore"), classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs, [])

    def test_feature_disabled_is_noop(self):
        cfg = {**self.cfg, "features": {**self.cfg["features"], "report_check": False}}
        def fn(body, key):
            raise AssertionError("should not be called")
        out = self.handback(self.payload(message="I did some stuff."), cfg=cfg, classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs, [])

    def test_low_confidence_alone_does_not_deny(self):
        # A read-only critic can't escalate one tier, so the self-reported low/medium
        # confidence heuristic must only nudge the PostToolUse agent-done path, not
        # deny here.
        message = ("RESULT: reviewed the diff\nEVIDENCE: read file:12-40, ran tests, exit 0\n"
                   "CONFIDENCE: low\nUNVERIFIED: none")

        def fn(body, key):
            return report_response(supported=0.95, material_gap=0.05)
        out = self.handback(self.payload(message=message), classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["decision"], "ok")

    def test_material_gap_alone_does_not_deny(self):
        def fn(body, key):
            return report_response(supported=0.95, material_gap=0.9)
        out = self.handback(self.payload(message=("RESULT: approve\nEVIDENCE: ran tests, exit 0\n"
                                                  "CONFIDENCE: high\nUNVERIFIED: full suite not run")),
                            classify_fn=fn)
        self.assertIsNone(out)
        self.assertEqual(self.logs[-1]["decision"], "ok")
        self.assertEqual(self.logs[-1]["material_gap"], 0.9)

    def test_missing_sections_still_deny(self):
        def fn(body, key):
            raise AssertionError("should not be called")
        out = self.handback(self.payload(message="looks fine, low confidence though"), classify_fn=fn)
        self.assertIsNotNone(out)
        self.assertIn("missing", out["hookSpecificOutput"]["permissionDecisionReason"])


class SectionParsingTests(unittest.TestCase):
    def test_lowercase_header_not_matched(self):
        text = "Result of git diff analysis: looks fine\nresult: x\nevidence: y"
        self.assertEqual(jev._parse_sections(text), {})

    def test_header_without_colon_not_matched(self):
        text = "RESULT here is what happened"
        self.assertEqual(jev._parse_sections(text), {})

    def test_header_alone_on_its_line_matched(self):
        text = "RESULT: ship\n\nEVIDENCE\n- ran tests, exit 0\n\n**CONFIDENCE**\nhigh\nUNVERIFIED: none"
        sections = jev._parse_sections(text)
        self.assertEqual(sections["EVIDENCE"], "- ran tests, exit 0")
        self.assertEqual(sections["CONFIDENCE"], "high")

    def test_uppercase_header_with_colon_matched(self):
        text = "RESULT: did it\nEVIDENCE: ran it\nCONFIDENCE: high\nUNVERIFIED: none"
        sections = jev._parse_sections(text)
        self.assertEqual(sections["RESULT"], "did it")
        self.assertEqual(sections["CONFIDENCE"], "high")

    def test_leading_markdown_still_matched(self):
        text = "**RESULT:** did it\n# EVIDENCE:\nran it\n- CONFIDENCE: high\n> UNVERIFIED: none"
        sections = jev._parse_sections(text)
        self.assertEqual(sections["RESULT"], "did it")
        self.assertEqual(sections["UNVERIFIED"], "none")


    def test_long_report_sections_parsed_beyond_truncation(self):
        cfg = jev.load_config(Path("/nonexistent/config.json"))
        findings = "[minor] finding text that goes on for a while.\n" * 300
        tail = ("RESULT: FIX FIRST\nEVIDENCE: ran tests, exit 0\n"
                "CONFIDENCE: high\nUNVERIFIED: none")
        text = findings + tail
        self.assertGreater(len(text), cfg["max_report_chars"])
        reasons, codes, _, _ = jev._analyze_report(
            text, cfg, lambda body, key: None, confidence_heuristic=False)
        self.assertNotIn("missing", codes)


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "state" / "jev-sess1.json"

    def test_concurrent_updates_of_different_keys_both_survive(self):
        # Simulate: process A loads (empty state), process B updates key "b" and
        # saves, then process A's stale update of key "a" must merge, not clobber.
        jev.update_state(self.path, lambda s: s.update(a=None) or s.__setitem__("a", 1))
        stale_load = json.loads(self.path.read_text(encoding="utf-8"))
        jev.update_state(self.path, lambda s: s.__setitem__("b", 2))
        # A "stale" writer that only knows about `stale_load` still merges via update_state
        # because update_state reloads from disk under the lock before mutating.
        def add_a_from_stale(s):
            s["a"] = stale_load.get("a", 1)
        jev.update_state(self.path, add_a_from_stale)
        final = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(final, {"a": 1, "b": 2})

    def test_tmp_file_name_contains_pid(self):
        seen_tmp_names = []
        real_write_text = Path.write_text

        def spy_write_text(self_path, *args, **kwargs):
            if self_path.parent == Path(self.path).parent and ".tmp." in self_path.name:
                seen_tmp_names.append(self_path.name)
            return real_write_text(self_path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", spy_write_text):
            jev.update_state(self.path, lambda s: s.__setitem__("x", 1))
        self.assertTrue(seen_tmp_names, "expected a .tmp.<pid> file to be written")
        self.assertIn(f".tmp.{os.getpid()}", seen_tmp_names[0])

    def test_no_lock_file_still_works_without_fcntl(self):
        with mock.patch.object(jev, "fcntl", None):
            jev.update_state(self.path, lambda s: s.__setitem__("x", 1))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"x": 1})

    def test_replace_retried_on_permission_error(self):
        real_replace = Path.replace
        attempts = []

        def flaky(self_path, target):
            attempts.append(self_path)
            if len(attempts) < 3:
                raise PermissionError("in use")
            return real_replace(self_path, target)

        with mock.patch.object(Path, "replace", flaky), \
                mock.patch.object(jev, "REPLACE_RETRY_SECONDS", 0):
            jev.update_state(self.path, lambda s: s.__setitem__("x", 1))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"x": 1})

    def test_replace_gives_up_and_removes_tmp(self):
        with mock.patch.object(Path, "replace", side_effect=PermissionError("in use")) as replace, \
                mock.patch.object(jev, "REPLACE_RETRY_SECONDS", 0):
            with self.assertRaises(PermissionError):
                jev.update_state(self.path, lambda s: s.__setitem__("x", 1))
        self.assertEqual(replace.call_count, jev.REPLACE_ATTEMPTS)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["jev-sess1.json.lock"])

    @unittest.skipUnless(os.name == "nt", "msvcrt locking is Windows-only")
    def test_windows_lock_excludes_concurrent_update(self):
        self.path.parent.mkdir(parents=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a+") as held:
            jev._msvcrt_lock(held)
            with mock.patch.object(jev, "MSVCRT_LOCK_SECONDS", 0.05):
                with self.assertRaises(OSError):
                    jev.update_state(self.path, lambda s: s.__setitem__("x", 1))
            held.seek(0)
            jev.msvcrt.locking(held.fileno(), jev.msvcrt.LK_UNLCK, 1)
        jev.update_state(self.path, lambda s: s.__setitem__("x", 1))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"x": 1})

    def test_held_lock_gives_up_at_deadline(self):
        self.path.parent.mkdir(parents=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a+") as held:
            if jev.fcntl is not None:
                jev.fcntl.flock(held.fileno(), jev.fcntl.LOCK_EX)
            else:
                jev._msvcrt_lock(held)
            try:
                start = time.monotonic()
                with mock.patch.object(jev, "MSVCRT_LOCK_SECONDS", 30):
                    with self.assertRaises(OSError):
                        jev.update_state(self.path, lambda s: s.__setitem__("x", 1),
                                         deadline=time.monotonic() + 0.1)
                self.assertLess(time.monotonic() - start, 0.8)
            finally:
                if jev.fcntl is not None:
                    jev.fcntl.flock(held.fileno(), jev.fcntl.LOCK_UN)
                else:
                    held.seek(0)
                    jev.msvcrt.locking(held.fileno(), jev.msvcrt.LK_UNLCK, 1)
        self.assertFalse(self.path.exists())
        jev.update_state(self.path, lambda s: s.__setitem__("x", 1), deadline=time.monotonic() + 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"x": 1})

    def assert_lock_error_not_retried(self, target, name):
        error = OSError(errno.EBADF, "bad file descriptor")
        start = time.monotonic()
        with mock.patch.object(target, name, side_effect=error) as lock, \
                mock.patch.object(jev, "MSVCRT_LOCK_SECONDS", 30):
            with self.assertRaises(OSError) as raised:
                jev.update_state(self.path, lambda s: s.__setitem__("x", 1),
                                 deadline=time.monotonic() + 30)
        self.assertEqual(raised.exception.errno, errno.EBADF)
        self.assertEqual(lock.call_count, 1)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(jev.fcntl is not None, "fcntl locking is POSIX-only")
    def test_flock_non_contention_error_raises_at_once(self):
        self.assert_lock_error_not_retried(jev.fcntl, "flock")

    @unittest.skipUnless(jev.fcntl is None and jev.msvcrt is not None, "msvcrt locking is Windows-only")
    def test_msvcrt_non_contention_error_raises_at_once(self):
        self.assert_lock_error_not_retried(jev.msvcrt, "locking")


class PersistenceFailureTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_dir = Path(temp.name) / "state"
        self.logs = []
        patcher = mock.patch.object(jev, "update_state", side_effect=PermissionError("in use"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agent_done_critic_persistence_is_best_effort(self):
        text = "RESULT: r\nEVIDENCE: e\nCONFIDENCE: high\nUNVERIFIED: none"
        for tool_response in ({"status": "async_launched", "agentId": "c1"},
                              {"status": "completed", "content": [{"type": "text", "text": text}]}):
            payload = {"tool_name": "Agent", "session_id": "sess1",
                       "tool_input": {"subagent_type": "critic"}, "tool_response": tool_response}
            jev.agent_done(payload, self.cfg, lambda body, key: report_response(), self.logs.append,
                           state_dir=self.state_dir)
        jev.agent_done({"tool_name": "SubagentHandback", "agent_type": "critic", "session_id": "s"},
                       self.cfg, None, self.logs.append, state_dir=self.state_dir)
        self.assertEqual(self.logs[-1]["decision"], "ok")  # the report check still ran

    def test_handback_deny_returned_when_persistence_fails(self):
        payload = {"agent_type": "builder", "agent_id": "a", "session_id": "sess1",
                   "tool_input": {"message": "no sections"}}
        out = jev.handback(payload, self.cfg, None, self.logs.append, state_dir=self.state_dir)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class ReportUnavailableTests(unittest.TestCase):
    REPORT = "RESULT: r\nEVIDENCE: e\nCONFIDENCE: high\nUNVERIFIED: none"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state_dir = Path(temp.name) / "state"
        self.logs = []

    @staticmethod
    def boom(body, key):
        raise OSError("secret-body")

    def test_handback_logs_unavailable(self):
        payload = {"agent_type": "builder", "agent_id": "a", "session_id": "sess1",
                   "tool_input": {"message": self.REPORT}}
        self.assertIsNone(jev.handback(payload, self.cfg, self.boom, self.logs.append,
                                       state_dir=self.state_dir))
        entry = self.logs[-1]
        self.assertEqual((entry["decision"], entry["reason"], entry["error"]),
                         ("ok", "unavailable", "OSError"))
        self.assertNotIn("secret-body", json.dumps(self.logs))

    def test_agent_done_logs_unavailable(self):
        payload = {"tool_name": "Agent", "session_id": "sess1",
                   "tool_input": {"subagent_type": "builder"},
                   "tool_response": {"status": "completed",
                                     "content": [{"type": "text", "text": self.REPORT}]}}
        self.assertIsNone(jev.agent_done(payload, self.cfg, self.boom, self.logs.append,
                                         state_dir=self.state_dir))
        self.assertEqual((self.logs[-1]["reason"], self.logs[-1]["error"]), ("unavailable", "OSError"))

    def test_report_text_scrubbed_before_sending(self):
        sent = []

        def fn(body, key):
            sent.append(body["state"])
            return report_response()
        token = "ghp_" + "Z" * 30
        text = f"RESULT: set key sk-{'q' * 20}\nEVIDENCE: used {token}\nCONFIDENCE: high\nUNVERIFIED: none"
        jev._analyze_report(text, self.cfg, fn)
        self.assertEqual(sent[0]["result"], "set key [redacted]")
        self.assertEqual(sent[0]["evidence"], "used [redacted]")


class SubprocessTests(unittest.TestCase):
    def test_garbage_stdin_exits_0_empty_stdout(self):
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        result = subprocess.run([sys.executable, str(SCRIPT), "gate"], input="not json{{{",
                                 env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_handback_garbage_stdin_exits_0_empty_stdout(self):
        env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        result = subprocess.run([sys.executable, str(SCRIPT), "handback"], input="not json{{{",
                                 env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_utf8_stdin_decoded_regardless_of_locale(self):
        # Hebrew, an emoji and curly quotes: several are undefined in cp1255/cp1252.
        message = "RESULT: שלום אךם \U0001F600 “quoted”"
        data = json.dumps({"tool_input": {"message": message}}, ensure_ascii=False).encode("utf-8")
        env = {k: v for k, v in os.environ.items()
               if k not in ("TYPESAFE_API_KEY", "PYTHONUTF8", "PYTHONIOENCODING")}
        # Unpatched: must not crash.
        result = subprocess.run([sys.executable, str(SCRIPT), "handback"], input=data,
                                env=env, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        # Echo the decoded payload through main()'s output path to see what reached the
        # classifier path.
        probe = ("import importlib.util, sys\n"
                 f"spec = importlib.util.spec_from_file_location('jev_guard', {str(SCRIPT)!r})\n"
                 "jev = importlib.util.module_from_spec(spec)\n"
                 "spec.loader.exec_module(jev)\n"
                 "jev.handback = lambda payload, cfg, log_fn: {'echo': payload['tool_input']['message']}\n"
                 "sys.argv = ['jev-guard.py', 'handback']\n"
                 "sys.exit(jev.main())\n")
        result = subprocess.run([sys.executable, "-c", probe], input=data, env=env,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["echo"], message)


class MainFailClosedTests(unittest.TestCase):
    def run_main(self, payload_text):
        with mock.patch.object(sys, "argv", ["jev-guard.py", "gate"]), \
                mock.patch.object(sys, "stdin", mock.Mock(buffer=mock.Mock(
                    read=lambda: payload_text.encode("utf-8")))), \
                mock.patch.object(jev, "gate", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdout", new_callable=mock.Mock) as out:
            self.assertEqual(jev.main(), 0)
        return "".join(call.args[0] for call in out.write.call_args_list)

    def test_crash_denies_push_and_deploy(self):
        for command in ("git push origin main", "git -C x push -f", "npx heroku releases",
                        "npm run deploy"):
            written = self.run_main(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": command}}))
            self.assertEqual(json.loads(written)["hookSpecificOutput"]["permissionDecision"],
                             "deny", command)

    def test_crash_denies_pushes_spelled_through_quoting(self):
        # The forms the scanner reads through, not only a bare `git ... push`.
        for command in ("g\\it push", "git 'pu'sh origin main", 'git "push" --force',
                        "gi't' pu\\sh", 'bash -c "g\\it pu\\sh"', "git \\\npush",
                        "git send-pack origin main", "git -C x 'send-pack' origin main",
                        "C:\\Git\\cmd\\git.exe push"):
            written = self.run_main(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": command}}))
            self.assertEqual(json.loads(written)["hookSpecificOutput"]["permissionDecision"],
                             "deny", command)
        written = self.run_main('{"tool_input": {"command": "g\\\\it \'pu\'sh"')
        self.assertEqual(json.loads(written)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_push_let_through_without_a_key_warns_the_user(self):
        # stderr at exit 0 is not shown; a systemMessage is, and decides nothing.
        def run(entry):
            def gate(payload, cfg, log_fn=None):
                log_fn(entry)
            with mock.patch.object(sys, "argv", ["jev-guard.py", "gate"]), \
                    mock.patch.object(sys, "stdin", mock.Mock(buffer=mock.Mock(read=lambda: b"{}"))), \
                    mock.patch.object(jev, "load_config", return_value={}), \
                    mock.patch.object(jev, "write_log") as write_log, \
                    mock.patch.object(jev, "gate", gate), \
                    mock.patch.object(sys, "stdout", new_callable=mock.Mock) as out:
                self.assertEqual(jev.main(), 0)
            write_log.assert_called_once_with({}, entry)
            return "".join(call.args[0] for call in out.write.call_args_list)
        written = run({"decision": "allow", "error": "NoApiKey", "fail_open": True})
        self.assertEqual(json.loads(written), {"systemMessage": jev.NO_KEY_WARNING})
        self.assertEqual(run({"decision": "allow", "error": "NoApiKey"}), "")

    def test_crash_on_unparseable_push_payload_denies(self):
        written = self.run_main('{"tool_input": {"command": "git push"')
        self.assertEqual(json.loads(written)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_crash_on_other_commands_stays_open(self):
        for command in ("git commit -m x", "ls -la", "git status"):
            self.assertEqual(self.run_main(json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": command}})), "", command)

    def test_crash_ignores_deploy_words_outside_the_command(self):
        written = self.run_main(json.dumps({
            "tool_name": "Bash", "tool_input": {"command": "ls"},
            "cwd": "C:/work/deploy-tools", "transcript_path": "/x/heroku-notes/t.jsonl"}))
        self.assertEqual(written, "")


class HiddenPushTests(unittest.TestCase):
    """A push the scanner can't parse is counted unless its text is known to be only
    text: behind a wrapper it doesn't know, as another command's argument or quoted
    script, after a redirection, or spelled so that unwrapping would lose it."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = jev.load_config(Path("/nonexistent/config.json"))
        self.cfg["enabled"] = True
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "repo"
        self.repo.mkdir()
        self.state_dir = Path(temp.name) / "state"
        run_git(["init"], self.repo)
        run_git(["config", "user.email", "d@ylm.co.il"], self.repo)
        run_git(["config", "user.name", "Test"], self.repo)
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        run_git(["add", "a.txt"], self.repo)
        run_git(["commit", "-m", "init"], self.repo)
        remote = Path(temp.name) / "remote.git"
        run_git(["init", "--bare", str(remote)], temp.name)
        run_git(["remote", "add", "origin", str(remote)], self.repo)
        run_git(["push", "-u", "origin", "HEAD"], self.repo)
        (self.repo / "pushed.txt").write_text("unpushed\n", encoding="utf-8")
        run_git(["add", "pushed.txt"], self.repo)
        run_git(["commit", "-m", "local"], self.repo)
        self.branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.repo,
                                     check=True, capture_output=True, text=True).stdout.strip()
        self.calls = []

    def classify(self, body, key):
        self.calls.append(body)
        return risk_response()

    def gate(self, command, session_id):
        payload = {"tool_name": "Bash", "session_id": session_id,
                   "tool_input": {"command": command}, "cwd": str(self.repo)}
        return jev.gate(payload, self.cfg, self.classify, None, state_dir=self.state_dir)

    def review(self, command, session_id):
        """(reasons of the denials before the classifier was asked, the state it was
        asked about); fails if the command passes unreviewed."""
        self.calls.clear()
        denials = []
        for _ in range(3):
            out = self.gate(command, session_id)
            self.assertIsNotNone(out, f"passed unreviewed: {command}")
            if self.calls:
                self.assertEqual(len(self.calls), 1)
                self.assertIn("This push looks risky",
                              out["hookSpecificOutput"]["permissionDecisionReason"])
                return denials, self.calls[0]["state"]
            denials.append(out["hookSpecificOutput"]["permissionDecisionReason"])
        self.fail(f"never put to the classifier: {command}")

    def test_push_behind_an_unknown_wrapper_is_denied_once_then_reviewed(self):
        # Reading only command words dropped a push behind any wrapper the scanner has
        # no name for, or run as another program's argument or quoted script.
        commands = ("ssh-agent git push", "ssh-agent git push --force",
                    "uv run git push --force", "op run -- git push --force",
                    "rtk git push --force", "proxychains git push --force",
                    "noglob git push --force", "gtimeout 5 git push --force",
                    "direnv exec . git push --force", "wsl git push --force",
                    "docker exec c git push --force", "nohup ssh-agent git push --force",
                    "parallel git push ::: origin",
                    "git bisect run git push --force",
                    "git submodule foreach git push --force",
                    "git submodule foreach 'git push --force'",
                    "git rebase -x 'git push --force' HEAD",
                    "watch -n1 'git push --force'", "nix-shell --run 'git push --force'",
                    "tee d.sh <<'EOF'\ngit push -f\nEOF\n./d.sh")
        for i, command in enumerate(commands):
            with self.subTest(command=command):
                self.assertTrue(jev._looks_like_push(command))
                denials, state = self.review(command, f"w{i}")
                self.assertEqual(denials, [jev.UNPARSED_PUSH_REASON])
                self.assertEqual(state["operation"], "push")
                self.assertIn("+unpushed", state["diff"])
                self.assertIn(jev.WRAPPED_PUSH_NOTE, state["push"])

    def test_push_after_a_leading_redirection_is_reviewed(self):
        for i, (command, args) in enumerate(((">/dev/null git push", [[]]),
                                             ("2>&1 git push -f", [["-f"]]),
                                             ("&>/dev/null git push", [[]]),
                                             ("2>/dev/null >out.log git push --force", [["--force"]]),
                                             ("sudo >/dev/null git push", [[]]))):
            with self.subTest(command=command):
                self.assertEqual(jev._push_signals(command), 1)
                self.assertEqual(jev._push_arg_lists(command), args)
                denials, state = self.review(command, f"r{i}")
                # Asked at once or after one denial, never passed.
                self.assertIn(denials, ([], [jev.UNPARSED_PUSH_REASON]))
                self.assertIn("+unpushed", state["diff"])
                if args != [[]]:
                    self.assertIn("force push", state["push"])
        # A redirection's target is no command word, wherever it is.
        for command in ("echo hi > git push", "echo hi 2>&1 git push", "ls &> git push"):
            self.assertFalse(jev._looks_like_push(command), command)
        self.assertEqual(jev._commit_signals("2>&1 git commit -m x"), 1)

    def test_dollar_quoted_script_is_not_unwrapped(self):
        # shlex doesn't decode `$'...'`: the unwrapped script read `$git pu\x73h -f`.
        for i, command in enumerate(("bash -c $'git pu\\x73h -f'", "bash -lc $'git push -f'",
                                     'sh -c $"git push -f"',
                                     "winpty bash -c $'git pu\\163h'")):
            with self.subTest(command=command):
                self.assertEqual(jev._unwrap_shell(command), command)
                self.assertTrue(jev._looks_like_push(command))
                denials, state = self.review(command, f"a{i}")
                self.assertEqual(denials, [jev.UNPARSED_PUSH_REASON])
                self.assertIn("+unpushed", state["diff"])
        # A `$'` or `$"` inside the script's own quotes is its text: still unwrapped.
        for command, script in (("bash -c 'grep \"push$\" f && git push'",
                                 'grep "push$" f && git push'),
                                ("bash -c \"echo 'a$' && git push\"", "echo 'a$' && git push")):
            self.assertEqual(jev._unwrap_shell(command), script, command)

    def test_unwrapping_never_lowers_the_push_count(self):
        # Whatever the unwrapped script reads as, the command as given is counted too.
        command = "bash -c 'git push'"
        with mock.patch.object(jev, "_unwrap_shell", return_value="git status"):
            out = self.gate(command, "u1")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)
        self.assertEqual(self.calls, [])
        # Unwrapped as usual, it is asked about at once.
        denials, state = self.review(command, "u2")
        self.assertEqual(denials, [])
        self.assertIn("+unpushed", state["diff"])

    def test_redirection_operators_do_not_split_a_push(self):
        # `&>`, `&>>` and `>|` were read as separators, so `--force` after them was lost.
        for i, tail in enumerate(("&>/dev/null --force", "&> /dev/null --force", "&>> l --force",
                                  ">| l --force", ">>| l --force", "2>&1 --force",
                                  "> log.txt --force", ">&2 --force")):
            command = f"git push origin {self.branch} {tail}"
            with self.subTest(command=command):
                self.assertEqual(jev._push_arg_lists(command),
                                 [["origin", self.branch, "--force"]])
                denials, state = self.review(command, f"s{i}")
                self.assertEqual(denials, [])
                self.assertIn("force push", state["push"])
                self.assertIn("+unpushed", state["diff"])
        # Still separators when they are not part of a redirection.
        for command, expected in (("git push origin a & git push origin b", [["origin", "a"],
                                                                             ["origin", "b"]]),
                                  ("git push origin a |& tee log", [["origin", "a"]]),
                                  ("true & >out.log git push -f", [["-f"]]),
                                  ("git push origin a && >l git push --tags", [["origin", "a"],
                                                                               ["--tags"]])):
            self.assertEqual(jev._push_arg_lists(command), expected, command)
        # _may_run_text reads them the same way: a file is written, nothing is piped.
        for command in ('echo "git push" >| NOTES.md', 'echo "git push" &> NOTES.md',
                        'grep "git push" notes.md &>> out.txt; cat out.txt'):
            self.assertFalse(jev._looks_like_push(command), command)
        self.assertTrue(jev._looks_like_push("echo 'git push --force' &> p.sh; ./p.sh"))

    def test_text_that_only_mentions_a_push_still_passes(self):
        commands = ('grep "git push" docs/', "grep -rn 'git push' . | head -3", "rg 'git push'",
                    'git log --grep="git push"', 'git log --grep "git push"',
                    'git log --author="git push"', "git log -S'git push' --oneline",
                    "git grep 'git push'", "git grep git push", "echo git push",
                    'echo "run: git push origin main"', "sudo echo git push",
                    "time grep 'git push' notes.md", 'python -m pytest -k "git push"',
                    "find . -name '*.md' -exec grep 'git push' {} \\;",
                    "cat <<EOF\ngit push origin main\nEOF", "ls # git push",
                    "git config --get alias.push", "git stash push")
        for i, command in enumerate(commands):
            with self.subTest(command=command):
                self.assertFalse(jev._looks_like_push(command))
                self.assertIsNone(self.gate(command, f"t{i}"))
        self.assertEqual(self.calls, [])
        # A message is prose, also when a heredoc spells it; the commit itself is
        # still what gets reviewed.
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        run_git(["add", "a.txt"], self.repo)
        for i, command in enumerate(('git commit -m "git push"', "git commit -am 'explain git push'",
                                     'git commit -m "$(cat <<\'EOF\'\nfix: handle git push\n\n'
                                     'git push now works\nEOF\n)"')):
            with self.subTest(command=command):
                self.assertFalse(jev._looks_like_push(command))
                self.calls.clear()
                out = self.gate(command, f"c{i}")
                self.assertIn("This commit looks risky",
                              out["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertEqual([call["state"]["operation"] for call in self.calls], ["commit"])
        # Not text: what an unknown command, or git itself, may run.
        for command in ("gh pr create --title 'git push'", "PAGER='git push --force' git log",
                        "git commit -m x && watch 'git push'", "git log --exec='git push'",
                        "python -c 'import sys, subprocess; subprocess.run(sys.argv[1:])' git push",
                        'git commit -m "$(tee d.sh <<EOF\ngit push\nEOF\n)"; ./d.sh'):
            self.assertTrue(jev._looks_like_push(command), command)

    def test_message_word_of_many_pieces_is_text(self):
        # Every piece of a message word is the message, not only the first.
        for command in ('git commit -m"a""git push"', "git commit -m \"a\"'git push'",
                        'git commit --message="a"\'git push\''):
            self.assertFalse(jev._looks_like_push(command), command)
        # A substitution in any piece still runs.
        for command in ('git commit -m "a""$(git push)"', 'git commit -m"a"$(git push)',
                        "git commit -m\"a\" 'git push'"):
            self.assertTrue(jev._looks_like_push(command), command)
        # `alias` the builtin still makes the command a full read; a config key doesn't.
        self.assertTrue(jev._looks_like_push("alias p='git push'\np"))
        self.assertFalse(jev._looks_like_push("git -c alias.p=status p"))

    def assert_pushes(self, *commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertGreater(jev._push_signals(command), 0)

    def assert_text(self, *commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(jev._push_signals(command), 0)

    def test_hash_inside_a_word_is_no_comment(self):
        # Round 5, 1a: a `#` after a backslash escape hid the rest of the line.
        self.assert_pushes("git commit -m fix\\ #123; git push", "git commit -m a#b; git push",
                           "echo a\\#b; git push")
        # The scanner parses the push too, so it is reviewed at once.
        ops = [t[0] for t in jev._scan_targets("git commit -m fix\\ #123; git push", "/repo")]
        self.assertEqual(sorted(ops), ["commit", "push"])
        self.assert_text("ls # git push", "ls; # git push", "git commit -m 'fix #123'")

    def test_heredoc_delimiter_is_read_as_bash_reads_it(self):
        # Round 5, 1b: `<<E\OF` ends at `EOF`, not at `E`.
        self.assert_pushes("cat <<E\\OF\nEOF\ngit push\nE", "cat <<'E'OF\nx\nEOF\ngit push\nE",
                           'cat <<"E"OF\nx\nEOF\ngit push\nE', "cat <<E''OF\nx\nEOF\ngit push\nE",
                           # A word this can't read for sure keeps what follows.
                           "cat <<$X\ncat <<EOF\n$X\ngit push\nEOF",
                           "cat <<EOF!\ngit push\nEOF")
        self.assert_text("cat <<EOF\ngit push\nEOF", "cat <<'EOF'\ngit push\nEOF",
                         "cat <<\\EOF\ngit push\nEOF", 'cat <<"EOF"\ngit push\nEOF',
                         "cat <<E\\OF\ngit push\nEOF", "cat <<-EOF\n\tgit push\n\tEOF",
                         "cat <<'E'OF\nE\ngit push\nEOF",
                         "cat <<A <<B\ngit push\nA\ngit push\nB")

    def test_fed_wrappers_and_eval_words_run_their_input(self):
        # Round 5, 1c and 1d: a fed command of wrappers only, or an eval word, is live.
        self.assert_pushes("echo git push origin main | xargs env",
                           "echo git push origin main | xargs nice",
                           "echo git push origin main | xargs timeout 60",
                           "echo git push origin main | xargs", "echo git push | xargs sudo",
                           'echo "git push" | iex', 'echo "git push" | Invoke-Expression',
                           "'git push' | iex")
        payload = {"tool_name": "PowerShell", "session_id": "ps",
                   "tool_input": {"command": 'echo "git push" | iex'}, "cwd": str(self.repo)}
        out = jev.gate(payload, self.cfg, self.classify, None, state_dir=self.state_dir)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)
        self.assert_text("echo git push | cat", "echo git push | wc -l")

    def test_git_grep_pager_option_runs_its_value(self):
        # Round 5, 1e: -O<pager> and --open-files-in-pager run a command.
        self.assert_pushes("git grep -O'git push;:' foo",
                           "git grep --open-files-in-pager='git push;:' foo",
                           "git grep --open='git push;:' foo", "git grep -nO'git push;:' foo",
                           "git -C x grep -e foo -O'git push;:'",
                           "git grep -Osh git push")
        self.assert_text("git grep 'git push'", "git grep -n 'git push' -- docs",
                         "git grep -o 'git push'", "git grep --only-matching 'git push'",
                         "git grep -e 'git push' --or -e 'x'")

    def test_inert_text_is_dropped_in_linear_time(self):
        # Round 5, 2: each pending heredoc rescanned the rest (17 s and 9 s at 24 KB).
        for command, label in ((": <<a " * 4000 + "\ngit status", "heredocs on one line"),
                               ("x <<E\n" * 4000, "unterminated heredoc lines"),
                               ("".join(f": <<a{k} " for k in range(3000)) + "\ngit status",
                                "distinct heredocs on one line"),
                               ("cat <<E\nx\nE\n" * 3000, "terminated heredocs"),
                               ('echo "$(echo \'"\')"; ' * 1200, "quotes in substitutions"),
                               ('"$(' * 8000, "unbalanced substitutions")):
            with self.subTest(label=label):
                elapsed = []
                for _ in range(3):  # best of 3, as in _assert_fast
                    start = time.monotonic()
                    jev._drop_inert_text(command)
                    self.assertEqual(jev._push_signals(command), 0)
                    elapsed.append(time.monotonic() - start)
                    if elapsed[-1] < 2.0:
                        break
                self.assertLess(min(elapsed), 2.0, label)
        # Past the cap on one line nothing more is dropped: what follows is still read.
        many = ": <<a " * (jev.MAX_PENDING_HEREDOCS + 1)
        self.assertEqual(jev._drop_inert_text(many + "\ngit push\na"), many + "\ngit push\na")
        self.assert_pushes(many + "\ngit push\na")

    def test_words_between_git_and_its_subcommand(self):
        # Round 5, 3a, 3b and 3f.
        self.assert_pushes("git --attr-source HEAD push origin main",
                           "git --namespace x push", "git --config-env k=v push",
                           "git >/dev/null push origin main", "git 2>&1 push", "git <<<x push",
                           "git > out push", "git 2> err push origin", "git &>/dev/null push",
                           "git config -f x alias.p push", "git config --file x alias.p push",
                           "git config --blob x alias.p push", "git config --global alias.p push",
                           "git config --file x --add alias.p '!git push'")
        self.assertEqual([t[0] for t in jev._scan_targets(
            "git --attr-source HEAD push origin main", "/repo")], ["push"])
        denials, state = self.review("git >/dev/null push origin HEAD", "redir")
        self.assertEqual(denials, [jev.UNPARSED_PUSH_REASON])
        self.assertIn("+unpushed", state["diff"])
        self.assert_text("git log >/dev/null", "git status 2>&1", "git config -f x user.name push",
                         "git config --get alias.push", "git config -f x --get alias.push")

    def test_push_supplied_by_xargs_a_glob_or_ifs(self):
        # Round 5, 3c, 3d and 3e.
        self.assert_pushes("echo push | xargs -I{} git {} origin main",
                           "echo push | xargs -I {} git {} origin main",
                           "echo push | xargs -IX git X origin", "echo push | xargs -I X git X origin",
                           "echo push | parallel git", "parallel git ::: push",
                           "/usr/bin/g?t push origin main", "git pus? origin", "git p*sh",
                           "g[i]t push", "git [p]ush origin", "git pu[rs]h",
                           "git${IFS}push origin", "git$IFS'push'", "git${IFS:0:1}push origin main",
                           "git$IFS$9push origin")
        self.assert_text("find . -name x -exec git add {} \\;", "ls | xargs -I{} git add {}",
                         "git add *.py", "git add src/?.py", "git checkout -- p*",
                         "git branch -d feature/*", "ls *", "echo ${IFS}", "git log -- 'p*'",
                         "git add a[0-9].txt")

    def test_quotes_inside_a_substitution_do_not_end_the_string(self):
        # Round 5, 3g: the `"` inside `$(…)` ended the span, so the rest read as quoted.
        self.assert_pushes("echo \"$(echo '\"')\"; bash -c 'git push'; echo '\"'",
                           "echo \"$(echo '\"')\"; git push; echo '\"'",
                           # An end that can't be found: any push word after it counts.
                           "echo \"$(echo '\"'; bash -c 'git push'; echo '\"'")
        self.assert_text("echo \"$(echo '\"')\"; echo 'git push'",
                         'git commit -m "$(cat <<\'EOF\'\nsay "git push" twice\nEOF\n)"')

    def test_message_options_belong_to_git_only(self):
        # Round 5, 3h: `tag` here is a user name, and -m is a shell's flag.
        self.assert_pushes("su tag -c -m 'git push'", "su commit -c 'git push'",
                           "sudo -u merge bash -c 'git push'")
        # Round 5, 5: forms that are only text.
        self.assert_text('git commit -sm "git push"', 'git commit -nm "git push"',
                         'git commit -asm "git push"', "git -C x tag -m 'git push' v1",
                         "sudo git commit -m 'git push'",
                         'git commit -m "mention \\`git push\\` here"',
                         'git commit -m "explain \\$(git push)"',
                         'git log --pretty=format:"git push %h"', 'git log --format="git push %h"',
                         "git stash push -m 'git push'", "git notes add -m 'git push'")
        # A substitution that is not escaped still runs, in any of them.
        self.assert_pushes('git commit -sm "$(git push)"', 'git commit -m "\\\\$(git push)"',
                           'git log --pretty=format:"$(git push)"', 'git commit -m "`git push`"')
        # PowerShell's backslash escapes nothing.
        payload = {"tool_name": "PowerShell", "session_id": "ps-msg", "cwd": str(self.repo),
                   "tool_input": {"command": 'git commit -m "explain \\$(git push)"'}}
        out = jev.gate(payload, self.cfg, self.classify, None, state_dir=self.state_dir)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"],
                         jev.UNPARSED_PUSH_REASON)

    def test_nested_shells_past_the_depth_read_still_count(self):
        # Round 5, 3i: below the last level read, any push word counts.
        for levels in (1, 2, 3, 4, 5, 6, 8):
            command = "git push origin main"
            for _ in range(levels):
                command = "bash -c " + shlex.quote(command)
            with self.subTest(levels=levels):
                self.assertGreater(max(jev._push_signals(command),
                                       jev._push_signals(jev._unwrap_shell(command))), 0)
        command = "git push --force"
        for _ in range(6):
            command = "bash -c " + shlex.quote(command)
        denials, state = self.review(command, "deep")
        self.assertEqual(denials, [jev.UNPARSED_PUSH_REASON])
        self.assertIn("+unpushed", state["diff"])

    def test_other_push_commands_are_counted(self):
        # Round 5, 3j: git's other push programs, and a bare git-push command word.
        self.assert_pushes("git http-push origin", "git send-pack origin main",
                           "git-push origin main", "git-http-push origin", "sudo git-send-pack x",
                           "/usr/lib/git-core/git-http-push x")
        self.assert_text("man git-push", "git help git-push", "apt show git-http-push")

    def test_config_alias_that_pushes_is_denied_once_then_reviewed(self):
        # Round 5, 3j: `git p` with alias.p = push in the repository it runs in.
        for name, value in (("p", "push"), ("pf", "push --force"), ("sp", "send-pack"),
                            ("sh1", "!echo hi"), ("chain", "p origin"), ("st", "status"),
                            ("lg", "log --oneline")):
            run_git(["config", f"alias.{name}", value], self.repo)
        for i, command in enumerate(("git p", "git pf origin", "git sp origin", "git sh1",
                                     "git chain", "sudo git p", "git -C . p",
                                     f"cd {self.repo.as_posix()} && git p")):
            with self.subTest(command=command):
                self.assertEqual(jev._alias_signals(command, str(self.repo)), 1)
                denials, state = self.review(command, f"al{i}")
                self.assertEqual(denials, [jev.UNPARSED_PUSH_REASON])
                self.assertIn("+unpushed", state["diff"])
                self.assertIn(jev.WRAPPED_PUSH_NOTE, state["push"])
        # Looked up where the command runs: the alias is this repository's only.
        other = self.repo.parent / "other"
        other.mkdir()
        run_git(["init"], other)
        run_git(["config", "alias.zzpush", "push"], other)
        self.assertEqual(jev._alias_signals("git zzpush", str(self.repo)), 0)
        self.assertEqual(jev._alias_signals("git -C ../other zzpush", str(self.repo)), 1)
        self.assertEqual(jev._alias_signals("cd ../other && git zzpush", str(self.repo)), 1)
        # No push behind it, one of git's own commands, or no alias at all.
        for i, command in enumerate(("git st", "git lg -3", "git status", "git zz-no-such-alias",
                                     "echo git p", "git log --grep p")):
            with self.subTest(command=command):
                self.assertEqual(jev._alias_signals(command, str(self.repo)), 0)
                self.assertIsNone(self.gate(command, f"an{i}"))
        # Inline config that may define the alias: counted without a lookup.
        with mock.patch.object(jev, "_run_git", side_effect=AssertionError("looked up")):
            for command in ("git -c include.path=/x p", "git --config-env=alias.q=X q",
                            "git -c includeIf.gitdir:/a/.path=/x zz"):
                self.assertEqual(jev._alias_signals(command, str(self.repo)), 1, command)
            self.assertEqual(jev._alias_signals("git status && git log", str(self.repo)), 0)
            self.assertEqual(jev._alias_signals("ls -la", str(self.repo)), 0)
        # One lookup per name and place, and a bounded number per command.
        with mock.patch.object(jev, "_run_git", return_value=None) as git:
            self.assertEqual(jev._alias_signals("git zza; git zza; git zzb", str(self.repo)), 0)
            self.assertEqual(git.call_count, 2)
        many = "; ".join(f"git zz{k}" for k in range(jev.MAX_ALIAS_LOOKUPS + 2))
        with mock.patch.object(jev, "_run_git", return_value="status\n") as git:
            self.assertEqual(jev._alias_signals(many, str(self.repo)), 2)
            self.assertEqual(git.call_count, jev.MAX_ALIAS_LOOKUPS)
        # Round 6, item 6: a lookup that runs out of time counts (fail closed).
        self.assertEqual(jev._alias_signals("git p", str(self.repo), time.monotonic() - 1), 1)


class Round6PushFormTests(unittest.TestCase):
    """Round 6: push forms the lexical backstop counts (a missed push is worse than text
    flagged as one), read as _gate reads them."""

    def detected(self, command, powershell=False, aliases=None):
        def fake_git(args, cwd, deadline=None, git_opts=None):
            if args[:2] == ["config", "--get"]:
                return (aliases or {}).get(args[2])
            return None
        token = jev._POWERSHELL.set(powershell)
        try:
            with mock.patch.object(jev, "_run_git", side_effect=fake_git):
                raw = jev._unwrap_shell(command)
                targets = jev._scan_targets(raw, "/repo", jev.MAX_TARGETS, powershell)
                unknown = []
                signals = max(jev._push_signals(raw, unknown=unknown),
                              jev._push_signals(command, unknown=unknown))
                signals += jev._alias_signals(raw, "/repo", None, powershell, unknown)
            return bool(signals or any(t[0] == "push" for t in targets))
        finally:
            jev._POWERSHELL.reset(token)

    def assert_detected(self, *commands, powershell=False, aliases=None):
        for command in commands:
            with self.subTest(command=command, powershell=powershell):
                self.assertTrue(self.detected(command, powershell, aliases))

    def assert_not_detected(self, *commands, powershell=False, aliases=None):
        for command in commands:
            with self.subTest(command=command, powershell=powershell):
                self.assertFalse(self.detected(command, powershell, aliases))

    def test_powershell_double_quotes_end_at_a_backslash_quote(self):
        # Item 1: a backslash escapes nothing in PowerShell.
        self.assert_detected('echo "C:\\a\\"; git push; echo "done"',
                             'dir "C:\\a\\"; git push origin main; dir "C:\\b\\"',
                             'Write-Host "C:\\"; git push; Write-Host "C:\\"',
                             'Write-Host "a""b"; git push', "Write-Host 'it''s'; git push",
                             'Write-Host "a`"b"; git push', "git pu`sh",
                             "cat <<EOF > notes.md\ngit push origin main\nEOF", powershell=True)
        self.assert_not_detected('Write-Host "git push"', "Write-Host 'git push'",
                                 'Write-Host "C:\\a\\" "git push"', powershell=True)

    def test_powershell_block_comment_drops_only_the_block(self):
        # Item 2: the `#` of `#>` cut the rest of the line as a comment.
        self.assert_detected("git status <# c #>; git push", "echo hi <# x #> ; git push",
                             "<# note #> git push", "git status <# a #> <# b #>; git push",
                             "echo x # c <# d\ngit push", "echo ${x/ #/y}; git push",
                             "echo ${x:- #}; git push", powershell=True)
        self.assert_not_detected("git status <# git push #>", "git status # git push",
                                 "echo hi <# x #> # git push", powershell=True)
        self.assert_detected("<# note #> git push")

    def test_hash_in_backticks_braces_or_arithmetic_is_no_comment(self):
        # Item 3: bash reads these `#`s as text, so what follows still runs.
        self.assert_detected("echo `date #`; git push", "x=`echo a #` && git push",
                             "echo ${x/ #/y}; git push", "echo ${x:- #}; git push",
                             "echo ${x%% #*}; git push", "echo ${v:+ # }; git push origin main",
                             "(( x = 1 #2 )); git push", "echo x@( #a|b); git push",
                             "echo $'a\\'b # c'; git push")
        self.assert_not_detected("echo a # git push", "git status # then git push",
                                 "git status\n# git push\ngit log")

    def test_pipe_at_a_line_end_goes_on_with_the_next_line(self):
        # Item 4: the newline after `|` reset what fed the next command.
        self.assert_detected("echo git push |\nparallel", "echo git push |\nxargs -L1 env",
                             "cat <<EOF |\ngit push\nEOF\nparallel",
                             "cat <<EOF |\ngit push\nEOF\nxargs -I{} sh -c {}",
                             "cat <<EOF |\ngit push\nEOF\n\n\nsh", "cat <<EOF &&\ngit push\nEOF\nbash")
        self.assert_not_detected("cat <<EOF |\ngit push\nEOF\nwc -l", "echo git push |\ncat")

    def test_alias_that_pushes_behind_a_wrapper_or_escape(self):
        # Item 5: the alias lookup read only plain-sight git commands.
        aliases = {"alias.p": "push"}
        self.assert_detected("git p", "true && bash -c 'git p'", "bash -c 'git p' && echo done",
                             "eval 'git p'", "echo 'git p' | sh", 'echo "$(git p)"',
                             "ssh-agent git p", "git submodule foreach git p", "g\\it p",
                             "git \\p", "git P", "watch 'git p'", aliases=aliases)
        self.assert_detected("git p", 'pwsh -Command "git p"', "& git p", powershell=True,
                             aliases=aliases)
        self.assert_not_detected("git st", "echo git p", 'grep "git p" docs/',
                                 'git commit -m "git p"', "bash -c 'git status'", aliases=aliases)

    def test_alias_lookup_that_fails_counts_as_a_push(self):
        # Item 6: only "no such key" (exit 1, no output) is no alias.
        def failing(args, cwd, deadline=None, git_opts=None):
            jev._GIT_UNASKED.set(True)
            return None
        with mock.patch.object(jev, "_run_git", side_effect=failing):
            self.assertEqual(jev._alias_signals("git zz", "/repo"), 1)
        with mock.patch.object(jev, "_run_git", return_value=None):
            self.assertEqual(jev._alias_signals("git zz", "/repo"), 0)
        # A real lookup in a directory that does not exist, and one past its deadline.
        missing = str(Path(tempfile.gettempdir()) / "jev-no-such-dir-r6")
        self.assertEqual(jev._alias_signals("git zz", missing), 1)
        with tempfile.TemporaryDirectory() as repo:
            run_git(["init"], repo)
            self.assertEqual(jev._alias_signals("git zz", repo), 0)
            self.assertEqual(jev._alias_signals("git zz", repo, time.monotonic() - 1), 1)

    def test_config_from_the_environment_makes_any_alias_count(self):
        # Item 7: config files the environment names are not what a lookup here reads.
        with mock.patch.object(jev, "_run_git", side_effect=AssertionError("looked up")):
            for command in ("GIT_CONFIG_GLOBAL=/tmp/c git zz", "HOME=/tmp/h git zz",
                            "env GIT_CONFIG_GLOBAL=/tmp/c git zz", "GIT_CONFIG_SYSTEM=/c git zz",
                            "GIT_CONFIG_NOSYSTEM=1 git zz", "GIT_CONFIG=/c git zz",
                            "XDG_CONFIG_HOME=/c git zz", "GIT_DIR=/c git zz",
                            "GIT_COMMON_DIR=/c git zz", "export HOME=/h; git zz",
                            "HOME=/tmp/h bash -c 'git zz'"):
                with self.subTest(command=command):
                    self.assertEqual(jev._alias_signals(command, "/repo"), 1)
            for command in ("$env:GIT_CONFIG_GLOBAL='x'; git zz", "$env:HOME = 'x'; git zz"):
                with self.subTest(command=command):
                    self.assertEqual(jev._alias_signals(command, "/repo", None, True), 1)
            for command in ("HOME=/tmp/h git status", "GIT_DIR=/tmp/x git log"):
                self.assertEqual(jev._alias_signals(command, "/repo"), 0)

    def test_gh_pr_create_without_a_head_may_push(self):
        # Item 8: gh offers to push the current branch; `gh repo sync` is out of scope.
        self.assert_detected("gh pr create --fill", "gh pr create", "gh pr new --fill",
                             "sudo gh pr create --fill", "bash -c 'gh pr create --fill'",
                             "gh pr -R o/r create --fill", 'echo "$(gh pr create --fill)"')
        self.assert_detected("gh pr create --fill", 'pwsh -Command "gh pr create --fill"',
                             powershell=True)
        self.assert_not_detected("gh pr create --head x", "gh pr create -H x --fill",
                                 "gh pr create --head=x", "gh pr view", "gh repo sync",
                                 "echo gh pr create", 'git commit -m "gh pr create"')

    def test_encoded_scripts_carets_and_escaped_values_count(self):
        # Item 10: text this can't read that a shell runs, and backslash-escaped values.
        self.assert_detected("echo Z2l0IHB1c2g= | base64 -d | sh",
                             "echo Z2l0IHB1c2g= | base64 --decode | bash",
                             "echo 67697420 | xxd -r -p | sh",
                             "echo Z2l0IHB1c2g= | openssl base64 -d | zsh",
                             "echo Z2l0IHB1c2g= | base64 -d |\nsh",
                             "powershell -enc ZwBpAHQAIABwAHUAcwBoAA==",
                             "pwsh -NoProfile -EncodedCommand ZwBpAHQAIABwAHUAcwBoAA==",
                             'cmd /c "g^it push"', "cmd /c g^it pu^sh",
                             "git -c core.pager=git\\ push -p log",
                             "GIT_PAGER=git\\ push git -p log",
                             "env GIT_PAGER=git\\ push git -p log",
                             "git -c core.editor=git\\\tpush commit",
                             "pwsh -e ZwBpAHQAIABwAHUAcwBoAA==",
                             "echo Z2l0IHB1c2g= | base64 -d | pwsh",
                             "echo Z2l0IHB1c2g= | base64 -d | powershell",
                             "echo Z2l0 | certutil -decode | sh")
        self.assert_detected("[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('Z2l0'))"
                             " | iex", "powershell -enc ZwBpAHQAIABwAHUAcwBoAA==",
                             'cmd /c "g^it push"', "echo Z2l0IHB1c2g= | base64 -d | iex",
                             "pwsh -EncodedCommand ZwBpAHQAIABwAHUAcwBoAA==",
                             powershell=True)
        self.assert_not_detected("base64 -d x > y", "echo abc | base64", "base64 -d x | wc -c",
                                 "powershell -ExecutionPolicy Bypass -File x.ps1",
                                 "git commit -m fix\\ git\\ push", "echo git\\ push",
                                 "git log --grep=git\\ push", "git diff HEAD^ HEAD")


class Round7CriticTests(unittest.TestCase):
    """Round 7: push forms the critic found missed, read as _gate reads them."""

    detected = Round6PushFormTests.detected
    assert_detected = Round6PushFormTests.assert_detected
    assert_not_detected = Round6PushFormTests.assert_not_detected

    def test_hash_in_a_regex_or_a_subscript_is_no_comment(self):
        # Item 1: bash keeps these `#`s as text, so the rest of the line runs.
        self.assert_detected("[[ a =~ ( #) ]]; git push", "[[ a =~ ^(x|#) ]]; git push",
                             "[[ a =~ a|#b ]]; git push", "declare -A a; a[k #]=1; git push",
                             "declare -A a; a[x|#]=1; git push")

    def test_heredoc_on_a_compound_command_feeds_what_is_inside(self):
        # Item 2: the `}` or `)` owning the heredoc has no words of its own.
        self.assert_detected("{ xargs -L1 env; } <<EOF\ngit push\nEOF",
                             "{\nbash -c 'sh'\n} <<EOF\ngit push\nEOF",
                             "(xargs -L1 env) <<EOF\ngit push\nEOF",
                             "{ parallel; } <<EOF\ngit push\nEOF",
                             "{ xargs -I{} env {}; } <<EOF\ngit push\nEOF",
                             "{ awk '{system($0)}'; } <<EOF\ngit push\nEOF")

    def test_pipe_into_a_subshell_or_output_substitution_feeds_it(self):
        # Item 3: `fed` was cleared at `(`, and `>(` fed nothing.
        self.assert_detected("echo git push | (xargs -L1 env)",
                             "cat <<EOF | (xargs -L1 env)\ngit push\nEOF",
                             "echo git push > >(xargs -L1 env)",
                             "echo git push | ( xargs -L1 env )",
                             "(echo git push) | (xargs -L1 env)",
                             'echo "git push" | (xargs -L1 env)', "echo git push | (parallel)",
                             "echo git push > >(xargs -I{} sh -c {})",
                             "cat <<EOF > >(xargs -L1 env)\ngit push\nEOF")

    def test_shift_inside_a_parameter_expansion_is_no_heredoc(self):
        # Item 4: the lines after `${a[1 <<true ]}` were dropped as a heredoc body.
        self.assert_detected("echo ${a[1 <<true ]}\ngit push\ntrue",
                             "echo ${x:1 <<true }\ngit push\ntrue")

    def test_subcommand_in_a_span_the_runner_splits(self):
        # Item 5: eval and Start-Process split one quoted span into git's arguments.
        self.assert_detected("eval git 'push origin'")
        self.assert_detected("Start-Process git 'push origin'",
                             "Start-Process git -ArgumentList 'push origin main' -Wait",
                             'Start-Process -FilePath git -ArgumentList "push origin main"',
                             "saps git 'push origin'", "start git 'push origin'",
                             powershell=True)

    def test_gh_pr_create_head_is_only_its_own_option(self):
        # Item 6: another option's value is not a head.
        self.assert_detected("gh pr create -tHotfix -b done", "gh pr create -t --head -b x",
                             'gh pr create --title "-H fix" --fill',
                             "gh pr create --title --head")
        self.assert_not_detected("gh pr create --head x", "gh pr create -H x",
                                 "gh pr create -Hx", "gh pr create --head=x")

    def test_gh_repo_create_with_push(self):
        # Item 7: it pushes the local commits to the new repository.
        self.assert_detected("gh repo create foo --public --source=. --push",
                             "gh repo create --push foo --source=.")
        self.assert_not_detected("gh repo create foo --public")

    def test_alias_in_a_wrapped_span_that_changes_place_counts(self):
        # Item 8: the lookup ran in the hook's cwd, not where the wrapped git runs.
        with tempfile.TemporaryDirectory() as parent:
            for name, value in (("rb", "push"), ("repo", "push"), ("plain", "status")):
                repo = Path(parent) / name
                repo.mkdir()
                run_git(["init"], repo)
                run_git(["config", "alias.zz", value], repo)
            for command in ("true && bash -c 'git -C rb zz'",
                            "true && bash -c 'cd rb && git zz'", "eval 'git -C rb zz'",
                            "ssh-agent git -C rb zz", "env -C repo git zz", "git -C rb zz"):
                with self.subTest(command=command):
                    self.assertEqual(jev._alias_signals(command, parent), 1)
            self.assertEqual(jev._alias_signals("git zz", parent), 0)
            plain = str(Path(parent) / "plain")
            self.assertEqual(jev._alias_signals("git zz", plain), 0)
            # Round 8 item 8: a wrapped one counts with no lookup, wherever it runs.
            self.assertEqual(jev._alias_signals("bash -c 'git zz'", plain), 1)

    def test_file_written_then_run_by_a_later_command(self):
        # Item 9: a body written to a file stays text only when nothing but an inert
        # reader comes after it.
        self.assert_detected("cat <<EOF > Makefile\nall:\n\tgit push\nEOF\nmake",
                             "cat <<EOF > .git/hooks/pre-commit\ngit push\nEOF\ngit commit -m x",
                             "cat <<EOF > justfile\nx:\n\tgit push\nEOF\njust x",
                             'cat > package.json <<EOF\n{"scripts":{"t":"git push"}}\nEOF\n'
                             "npm run t")
        self.assert_not_detected("cat <<EOF > notes.md\ngit push\nEOF",
                                 "cat <<EOF > notes.md\ngit push\nEOF\necho done",
                                 "cat <<EOF > notes.md\ngit push\nEOF\ngit status",
                                 "cat <<EOF > notes.md\ngit push\nEOF\ncat notes.md")


class Round8CriticTests(unittest.TestCase):
    """Round 8: push forms the critic found missed, read as _gate reads them."""

    detected = Round6PushFormTests.detected
    assert_detected = Round6PushFormTests.assert_detected
    assert_not_detected = Round6PushFormTests.assert_not_detected

    def test_runner_split_scan_is_linear(self):
        # Item 1: an option run holding `:git` restarted the span search (quadratic).
        for powershell in (False, True):
            for command in ("eval " + "git -a:" * (50000 // 7),
                            "start git" + " -a:git" * (50000 // 7)):
                start = time.perf_counter()
                self.detected(command, powershell)
                self.assertLess(time.perf_counter() - start, 3.0)

    def test_every_command_of_a_fed_group_is_fed(self):
        # Item 2: only the first command of a piped group was fed.
        self.assert_detected("echo git push | (true; xargs -L1 env)",
                             "echo git push | { true; xargs -L1 env; }",
                             "echo git push | while true; do xargs -L1 env; break; done",
                             "echo git push | if true; then xargs -L1 env; fi",
                             "cat <<EOF | { true; xargs -L1 env; }\ngit push\nEOF",
                             "echo git push > >(true; xargs -L1 env)",
                             "echo git push | (echo hi; parallel)",
                             "echo git push | { echo x; awk '{system($0)}'; }")
        self.assert_not_detected("echo git push | cat", "echo git push | (cat)")

    def test_written_file_holes(self):
        # Item 3: a group's redirection, a digit file name, a background command, a
        # reader that writes a file itself, a write under .git/.
        self.assert_detected('{ echo all:; echo "\tgit push"; } > Makefile; make',
                             '(echo all:; echo "\tgit push") > Makefile; make',
                             "cat <<EOF > 1\nall:\n\tgit push\nEOF\nmake -f 1",
                             "(sleep 1; make) & cat <<EOF > Makefile\nall:\n\tgit push\nEOF",
                             'sort -o package.json <<EOF\n{"scripts":{"t":"git push"}}\nEOF\n'
                             "npm run t",
                             "echo 'x: ; git push' | sort -o Makefile; make",
                             "uniq - Makefile <<EOF\nx: ; git push\nEOF\nmake",
                             "cat <<EOF > .git/hooks/post-index-change\n#!/bin/sh\ngit push\nEOF\n"
                             "git add -A",
                             "cat <<EOF > .git/hooks/post-index-change\n#!/bin/sh\ngit push\nEOF\n"
                             "git status")
        self.assert_not_detected("echo git push > notes.md",
                                 "cat <<EOF > notes.md\ngit push\nEOF\necho done")

    def test_no_comment_test_reads_joined_lines(self):
        # Item 4: a backslash-newline or `$[` hid the operator that makes `#` text.
        self.assert_detected("(echo $[ 1 # ]); git push", "echo $\\\n{x/ #/y}; git push",
                             "declare -A a; a\\\n[k #]=1; git push",
                             "echo $\\\n'a\\'b # c'; git push")

    def test_heredoc_body_starts_outside_open_expansions(self):
        # Item 5: a newline inside `$(`, `${`, `$((` or `((` starts no heredoc body.
        self.assert_detected("cat <<EOF $(true\ngit push)\nEOF",
                             "cat <<EOF ${x:-a\n}; git push\nEOF",
                             "cat <<EOF $((1+\n1)); git push\nEOF",
                             "cat <<EOF; (( 1 +\n1 )); git push\nEOF",
                             "echo ${x:-$(echo })cat <<true }\ngit push\ntrue",
                             "echo $\\\n{a[1 <<true ]}\ngit push\ntrue")

    def test_resplitting_runner_with_push_anywhere_after_git(self):
        # Item 6: options, values or quotes between git and the push word.
        self.assert_detected("eval git -C . 'push origin'", "eval git -c x=y 'push origin'",
                             "eval git push' origin'", "eval git pu'sh origin'",
                             "eval git $'push origin'", "eval git '\"push\" origin'",
                             "eval git 'pu\\sh origin'", "eval git '-C . push origin'",
                             "eval 'git -C .' push origin", "eval git \\\n'push origin'",
                             "watch git 'push origin'", "parallel git 'push origin' ::: 1",
                             "pwsh -c git 'push origin'")
        self.assert_detected("Start-Process git -WorkingDirectory . 'push origin'",
                             "Start-Process git -ArgumentList '-C . push origin'",
                             "Start-Process git -ArgumentList ('push origin')",
                             "Start-Process git -Wait -ArgumentList `\n'push origin'",
                             powershell=True)

    def test_gh_pr_create_empty_or_redirected_head_is_no_head(self):
        # Item 7: gh falls back to the current branch.
        self.assert_detected('gh pr create --head "" --fill', "gh pr create --head '' --fill",
                             "gh pr create --head= --fill", 'gh pr create -H "" --fill',
                             "gh pr create --fill <<< '-H x'")
        self.assert_not_detected("gh pr create --head x", "gh pr create -H x",
                                 "gh pr create -Hx", "gh pr create --head=x")

    def test_wrapped_unknown_subcommand_counts_without_lookup(self):
        # Item 8: zz pushes only in rb and repo; the hook's cwd is their parent.
        with tempfile.TemporaryDirectory() as parent:
            for name in ("rb", "repo"):
                repo = Path(parent) / name
                repo.mkdir()
                run_git(["init"], repo)
                run_git(["config", "alias.zz", "push"], repo)
            for command, powershell in (
                    ("true && bash -c 'c\\d rb && git zz'", False),
                    ("true && bash -c 'c\"\"d rb && git zz'", False),
                    ('env "-C" repo git zz', False), ("e'n'v -C repo git zz", False),
                    ("env --chd=repo git zz", False), ("sudo -D rb git zz", False),
                    ("find rb/.git -maxdepth 0 -execdir git zz ';'", False),
                    ("bash -c 'export GIT_DIR; read GIT_DIR <<< rb/.git; git zz'", False),
                    ("bash -c 'git zz'", False),
                    ("Start-Process git zz -WorkingDirectory rb", True),
                    ("Start-Process git -ArgumentList zz -WorkingDirectory rb", True),
                    ("powershell -c 'S`et-Location rb; git zz'", True)):
                with self.subTest(command=command):
                    self.assertGreaterEqual(
                        jev._alias_signals(command, parent, None, powershell), 1)
            # In plain sight it is still looked up (none in the parent), also when the
            # gate reads the same command twice.
            self.assertEqual(jev._alias_signals("git zz", parent), 0)
            unknown = []
            jev._push_signals("git zz", unknown=unknown)
            jev._push_signals("git zz", unknown=unknown)
            self.assertEqual(jev._alias_signals("git zz", parent, None, False, unknown), 0)


class Round9CriticTests(unittest.TestCase):
    """Round 9: push forms the base detected and round 9 missed, read as _gate reads them."""

    detected = Round6PushFormTests.detected
    assert_detected = Round6PushFormTests.assert_detected
    assert_not_detected = Round6PushFormTests.assert_not_detected

    def test_hash_after_a_substitution_is_no_comment(self):
        # Item 1: `#` right after the `)` of `$(...)`, `<(...)`, `>(...)` is word text.
        self.assert_detected("echo $(true)# b; git push", ": $(:)# ; git push",
                             "x=$(true)# c; git push", "echo $(echo a)# b && git push",
                             "echo <(true)# b; git push", "echo a>(true)# b; git push")

    def test_heredoc_body_starts_after_every_opener_closes(self):
        # Item 2: `$[`, a process substitution, an array subscript or a `case` in a `$(`
        # still open at the newline.
        self.assert_detected("cat <<EOF $[1+\n1]; git push\nEOF",
                             "cat <<EOF $[\n1]; git push\nEOF",
                             "cat <<EOF b=$[1+\n1]; git push\nEOF",
                             "cat <<EOF <(true\ngit push)\nEOF",
                             "cat <<EOF >(true\ngit push)\nEOF",
                             "cat <<EOF > >(true\ngit push)\nEOF",
                             "cat <<EOF; a[1 +\n1]=2; git push\nEOF",
                             "cat <<EOF $(case x in x) true\ngit push;; esac)\nEOF")
        self.assert_not_detected("cat <<EOF\ngit push\nEOF")

    def test_reader_writing_a_file_past_wrappers(self):
        # Item 3: the command word past assignments and wrappers; any case of `-o`.
        self.assert_detected("LC_ALL=C uniq - Makefile <<EOF\nx: ; git push\nEOF\nmake",
                             "env uniq - Makefile <<EOF\nx: ; git push\nEOF\nmake",
                             "command uniq - Makefile <<EOF\nx: ; git push\nEOF\nmake",
                             "echo 'x: ; git push' | LC_ALL=C uniq - Makefile; make",
                             "sort -Vo Makefile <<EOF\nx: ; git push\nEOF\nmake",
                             "sort -Ro Makefile <<EOF\nx: ; git push\nEOF\nmake",
                             "sort -\\o Makefile <<EOF\nx: ; git push\nEOF\nmake")

    def test_loop_with_a_write_is_live(self):
        # Item 4: a command before the write runs it on the loop's next pass.
        for write in ("echo 'x: ; git push' > Makefile;",
                      "cat <<EOF > Makefile\nx: ; git push\nEOF\n"):
            self.assert_detected("for i in 1 2; { make; " + write + " }",
                                 "for ((i=0;i<2;i++)) { make; " + write + " }",
                                 "for i in 1 2\n{ make; " + write + " }")

    def test_dynamic_or_git_dir_write_targets(self):
        # Item 5: a target built at run time before a git command; a `cd` into `.git`.
        self.assert_detected(
            "d=.git; cat <<EOF > $d/hooks/post-index-change\ngit push\nEOF\ngit add -A",
            "cat <<EOF > .g\\it/hooks/post-index-change\ngit push\nEOF\ngit add -A",
            "cd .git/hooks && cat <<EOF > post-index-change\ngit push\nEOF")
        self.assert_not_detected("cat <<EOF > notes.md\ngit push\nEOF\ncat notes.md")

    def test_powershell_built_command_text(self):
        # Item 6: an eval word, `cmd /c` or `::Create(` given a `+`-built expression.
        self.assert_detected("iex ('git ' + 'push')", 'iex ("git " + "push")',
                             "& ([scriptblock]::Create('git ' + 'push'))",
                             "cmd /c ('git ' + 'push')", powershell=True)

    def test_fed_group_ignores_paren_in_parameter_expansion(self):
        # Item 7: a `)` inside `${...}` closes no group.
        self.assert_detected("echo git push | (echo ${x:-)}; xargs -L1 env)",
                             "echo git push | (echo ${x#)}; xargs -L1 env)")

    def test_nul_file_and_reader_preprocessor(self):
        # Item 8: `nul` is a file off Windows; `rg --pre` runs a program.
        self.assert_detected("echo 'x: ; git push' > nul; make -f nul",
                             "echo 'git push' > p.sh; rg --pre sh x p.sh",
                             "echo 'git push' > p.sh; rg --pre=sh x p.sh")
        self.assert_not_detected("echo 'git push' > notes.md", "echo a # git push")


if __name__ == "__main__":
    unittest.main()

import errno
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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
        # No origin/main and no upstream configured -> both diff attempts fail -> None, no call
        self.assertIsNone(out)
        self.assertEqual(self.calls, [])

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
        with mock.patch.object(jev.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            jev._run_git(["status"], "/repo", git_opts=("--work-tree", "/w"))
        self.assertEqual(run.call_args[0][0], ["git", "--work-tree", "/w", "status"])
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
        start = time.monotonic()
        targets = jev._scan_targets("git " + "-C a " * 160000 + "push", "/repo")
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual([t[:2] for t in targets], [("push", "/repo")])
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


if __name__ == "__main__":
    unittest.main()

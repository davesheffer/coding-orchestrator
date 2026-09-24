import importlib.util
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

    def _assert_scans_fast(self, command, label):
        start = time.monotonic()
        stripped = jev._strip_heredocs_and_quotes(command)
        list(jev.GIT_COMMAND_RE.finditer(stripped))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, f"{label}: {elapsed:.2f}s")

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
                               ("a=;" * 20000, "empty env assignments")):
            self._assert_scans_fast(command, label)
        start = time.monotonic()
        targets = jev._scan_targets("git commit -m x;" * 5000 + "git push", "/repo")
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertIn("push", [t[0] for t in targets])

    def test_quoted_dash_c_survives_long_prefix(self):
        # A long -c/env value trims the start of the git segment out of the scanner's
        # window; the quoted -C value must still be kept so the push stays visible.
        for command in ('git -c x=' + "a" * 600 + ' -C "sub dir" push origin main',
                        'FOO=' + "a" * 600 + ' git -C "sub" push'):
            self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"])

    def test_arithmetic_shift_is_not_a_heredoc(self):
        command = "echo $((1<<3))\ngit push --force\n3\n"
        self.assertEqual([t[0] for t in jev._scan_targets(command, "/repo")], ["push"])

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

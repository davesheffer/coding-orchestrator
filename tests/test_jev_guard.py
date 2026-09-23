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
        self.assertIn("rm -rf", body["state"]["diff"])

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
        self.assertIn("token", body["state"]["diff"])

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
                        f"cd {sub} && ls && git commit -m x"):
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


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "codex" / "jev-hook.py"
spec = importlib.util.spec_from_file_location("codex_jev_hook", HOOK)
module = importlib.util.module_from_spec(spec)
# The hook binds CONFIG/STATE/LOG from CODEX_HOME at import; point it at an empty home so
# a developer's real ~/.codex/jev config (enabled, with a key) never routes, calls or logs here.
ISOLATED_HOME = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"CODEX_HOME": ISOLATED_HOME.name}):
    spec.loader.exec_module(module)

UNICODE_TEXT = "\u05e9\u05dc\u05d5\u05dd \u05d0\u05da \U0001f600 \u201cquoted\u201d"
DRIVER = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("codex_jev_hook", sys.argv[1])
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
hook.CONFIG = Path(sys.argv[2])
cfg = hook.settings()
cfg["enabled"] = True
bodies, logs = [], []
hook.settings = lambda: cfg
hook.log = lambda cfg, entry: logs.append(entry)
hook.client.http_classify = lambda body, cfg, key: (
    bodies.append(body), {"answers": {"model": {"choice": "luna", "confidence": 0.9}}})[1]
sys.argv = sys.argv[:1]
hook.main()
sys.stderr.write(json.dumps({"bodies": bodies, "logs": logs}))
"""


class CodexJevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex"
        self.env = os.environ | {"CODEX_HOME": str(self.home)}

    def tearDown(self):
        self.temp.cleanup()

    def install(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "codex/install.py"), *args],
                              env=self.env, capture_output=True, text=True)

    def test_opt_in_preserves_user_hooks_and_disables_owned_hooks(self):
        self.home.mkdir()
        user_group = {"matcher": "Bash", "hooks": [{"type": "command", "command": "user-hook"}]}
        (self.home / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [user_group]}}))
        on = self.install("--jev")
        self.assertEqual(on.returncode, 0, on.stderr)
        hooks = json.loads((self.home / "hooks.json").read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"][0], user_group)
        self.assertTrue((self.home / "bin/jev-hook.py").exists())
        self.assertTrue(json.loads((self.home / "jev/config.json").read_text())["jev"]["enabled"])
        self.assertEqual(self.install("--jev").returncode, 0)
        self.assertEqual(json.loads((self.home / "hooks.json").read_text())["hooks"], hooks)
        off = self.install()
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(json.loads((self.home / "hooks.json").read_text())["hooks"],
                         {"PreToolUse": [user_group]})
        self.assertFalse(json.loads((self.home / "jev/config.json").read_text())["jev"]["enabled"])

    def test_route_pins_native_roles_and_rewrites_default(self):
        cfg = module.settings() | {"enabled": True, "min_confidence": 0.5}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "critic", "message": "review"}}
        with patch.object(module.client, "ask") as ask:
            self.assertIsNone(module.route(payload, cfg))
            ask.assert_not_called()
        payload["tool_input"] = {"agent_type": "default", "message": "read a file"}
        with patch.object(module.client, "ask", return_value={"model": {"choice": "luna", "confidence": 0.9}}), \
             patch.object(module, "log"):
            out = module.route(payload, cfg)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6-luna")

    def route_with(self, answer):
        cfg = module.settings() | {"enabled": True}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "fix it"}}
        with patch.object(module.client, "ask", return_value={"model": answer}), \
             patch.object(module, "log") as log:
            out = module.route(payload, cfg)
        self.logged = log.call_args and log.call_args[0][1]
        return out and out["hookSpecificOutput"]["updatedInput"]["model"]

    def test_route_weakest_model_needs_high_confidence(self):
        self.assertIsNone(self.route_with({"choice": "luna", "confidence": 0.7}))
        self.assertEqual(self.route_with({"choice": "sol", "confidence": 0.6}), "gpt-6.1-sol")

    def test_route_escalates_when_stronger_models_are_likely(self):
        answer = {"choice": "luna", "confidence": 0.55,
                  "probabilities": {"luna": 0.55, "sol": 0.35, "astra": 0.1}}
        self.assertEqual(self.route_with(answer), "gpt-6.1-sol")
        self.assertEqual((self.logged["choice"], self.logged["escalated_to"], self.logged["escalated_mass"]),
                         ("luna", "sol", 0.45))
        answer = {"choice": "sol", "confidence": 0.8, "probabilities": {"sol": 0.8, "astra": 0.2}}
        self.assertEqual(self.route_with(answer), "gpt-6.1-sol")
        answer = {"choice": "luna", "confidence": 0.9, "probabilities": {"luna": 0.9, "sol": "nan"}}
        self.assertEqual(self.route_with(answer), "gpt-6-luna")

    def test_route_never_escalates_to_removed_model(self):
        cfg = module.settings()
        cfg |= {"enabled": True, "labels": {k: v for k, v in cfg["labels"].items() if k != "astra"}}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "fix it"}}
        answer = {"choice": "sol", "confidence": 0.9, "probabilities": {"sol": 0.5, "astra": 0.5}}
        with patch.object(module.client, "ask", return_value={"model": answer}), \
             patch.object(module, "log") as log:
            out = module.route(payload, cfg)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6.1-sol")
        self.assertNotIn("escalated_to", log.call_args[0][1])

    def test_route_respects_send_prompt_false(self):
        cfg = module.settings() | {"enabled": True, "send_prompt": False}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "PRIVATE-TASK-SENTINEL"}}
        def classify(body, key):
            self.assertNotIn("PRIVATE-TASK-SENTINEL", json.dumps(body))
            return {"answers": {"model": {"choice": "sol", "confidence": 0.9}}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), patch.object(module, "log"):
            out = module.route(payload, cfg, classify)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6.1-sol")

    def route_default(self, answer, classify=None):
        cfg = module.settings() | {"enabled": True, "min_confidence": 0.5}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "read a file"}}
        logs = []
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
             patch.object(module, "log", lambda cfg, entry: logs.append(entry)):
            if classify is None:
                with patch.object(module.client, "ask", return_value=answer):
                    return module.route(payload, cfg), logs
            return module.route(payload, cfg, classify), logs

    def test_route_rejects_invalid_confidence(self):
        for value in (float("nan"), float("inf"), -1, 2, None, 10 ** 400, True, "0.9"):
            with self.subTest(value=value):
                out, logs = self.route_default({"model": {"choice": "luna", "confidence": value}})
                self.assertIsNone(out)
                self.assertEqual((logs[0]["applied"], logs[0]["reason"]), (False, "invalid confidence"))
                json.dumps(logs[0], allow_nan=False)

    def test_route_ignores_huge_probabilities(self):
        answer = {"model": {"choice": "luna", "confidence": 0.9, "probabilities": {"luna": 0.9, "sol": 10 ** 400}}}
        out, logs = self.route_default(answer)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6-luna")
        json.dumps(logs[0], allow_nan=False)

    def test_route_logs_every_decision_like_jev_route(self):
        out, logs = self.route_default({"model": {"choice": "sol", "confidence": 0.9}})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6.1-sol")
        entry = logs[0]
        self.assertEqual((entry["applied"], entry["reason"], entry["subagent_type"]), (True, "applied", "default"))
        self.assertIsInstance(entry["latency_ms"], int)
        self.assertEqual(entry["desc_hash"], module.hashlib.sha256(b"read a file").hexdigest()[:12])
        self.assertNotIn("read a file", json.dumps(entry))
        out, logs = self.route_default({"model": {"choice": "sol", "confidence": 0.2}})
        self.assertIsNone(out)
        self.assertEqual((logs[0]["applied"], logs[0]["reason"]), (False, "below confidence threshold"))

    def test_route_missing_or_non_string_choice_is_malformed_like_jev_route(self):
        for answer in ({"confidence": 0.9}, {"choice": ["luna"], "confidence": 0.9},
                       {"choice": None, "confidence": 0.9}):
            with self.subTest(answer=answer):
                out, logs = self.route_default({"model": answer})
                self.assertIsNone(out)
                self.assertEqual((logs[0]["applied"], logs[0]["reason"], logs[0]["error"]),
                                 (False, "unavailable", "MalformedResponse"))
                self.assertNotIn("choice", logs[0])
                json.dumps(logs[0])
        # An unknown string label stays "invalid label".
        out, logs = self.route_default({"model": {"choice": "gpt", "confidence": 0.9}})
        self.assertIsNone(out)
        self.assertEqual((logs[0]["applied"], logs[0]["reason"], logs[0]["choice"]),
                         (False, "invalid label", repr("gpt")))

    def test_route_weakest_configured_model_needs_high_confidence(self):
        cfg = module.settings()
        cfg |= {"enabled": True, "min_confidence": 0.5, "downgrade_min_confidence": 0.8,
                "labels": {k: v for k, v in cfg["labels"].items() if k != "luna"}}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "fix it"}}
        for confidence, expected in ((0.6, None), (0.85, "gpt-6.1-sol")):
            with self.subTest(confidence=confidence), \
                 patch.object(module.client, "ask", return_value={"model": {"choice": "sol", "confidence": confidence}}), \
                 patch.object(module, "log"):
                out = module.route(payload, cfg)
            self.assertEqual(out and out["hookSpecificOutput"]["updatedInput"]["model"], expected)
        # With a single kept model nothing is a downgrade: min_confidence applies.
        cfg["labels"] = {"sol": cfg["labels"]["sol"]}
        with patch.object(module.client, "ask", return_value={"model": {"choice": "sol", "confidence": 0.6}}), \
             patch.object(module, "log"):
            out = module.route(payload, cfg)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6.1-sol")

    def test_junk_max_prompt_chars_still_routes(self):
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"jev": {"enabled": True, "max_prompt_chars": "6000x"}}), encoding="utf-8")
        with patch.object(module, "CONFIG", config):
            cfg = module.settings()
        self.assertEqual(cfg["max_prompt_chars"], 6000)
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "fix it"}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), patch.object(module, "log"):
            out = module.route(payload, cfg, lambda body, key: {"answers": {"model": {"choice": "sol",
                                                                                      "confidence": 0.9}}})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6.1-sol")

    def test_route_rejects_bool_or_string_confidence(self):
        for value in (True, "0.9"):
            with self.subTest(value=value):
                out, logs = self.route_default({"model": {"choice": "luna", "confidence": value}})
                self.assertIsNone(out)
                self.assertEqual((logs[0]["applied"], logs[0]["reason"]), (False, "invalid confidence"))
                self.assertIsNone(logs[0]["confidence"])

    def test_route_malformed_answer_shape_is_logged(self):
        out, logs = self.route_default({"model": "luna"})
        self.assertIsNone(out)
        self.assertEqual((logs[0]["applied"], logs[0]["reason"], logs[0]["error"]),
                         (False, "unavailable", "MalformedResponse"))

    def test_string_min_confidence_is_coerced_to_float(self):
        cfg = module.settings() | {"enabled": True, "min_confidence": "0.5"}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "read a file"}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), patch.object(module, "log"), \
             patch.object(module.client, "ask", return_value={"model": {"choice": "luna", "confidence": 0.9}}):
            out = module.route(payload, cfg)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6-luna")

    def test_route_logs_unavailable(self):
        def boom(body, key):
            raise TimeoutError("slow test-key")
        out, logs = self.route_default(None, boom)
        self.assertIsNone(out)
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0]["applied"], logs[0]["reason"], logs[0]["error"]),
                         (False, "unavailable", "TimeoutError"))
        self.assertIn("latency_ms", logs[0])
        self.assertNotIn("test-key", json.dumps(logs[0]))

    def test_user_labels_are_not_overwritten(self):
        config = Path(self.temp.name) / "config.json"
        with patch.object(module, "CONFIG", config):
            self.assertEqual(module.settings()["labels"], module.LABELS)
            config.write_text(json.dumps({"jev": {"labels": {"sol": "custom", "astra": None, "opus": "x"}}}))
            self.assertEqual(module.settings()["labels"], {"luna": module.LABELS["luna"], "sol": "custom"})

    def test_utf8_stdin_is_decoded_whatever_the_locale(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                   "tool_input": {"agent_type": "default", "message": UNICODE_TEXT}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for encoding in (None, "cp1252"):
            with self.subTest(encoding=encoding):
                env = {k: v for k, v in self.env.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
                env["TYPESAFE_API_KEY"] = "test-key"
                if encoding:
                    env["PYTHONIOENCODING"] = encoding
                result = subprocess.run([sys.executable, "-c", DRIVER, str(HOOK), str(Path(self.temp.name) / "none.json")],
                                        input=data, env=env, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                seen = json.loads(result.stderr.decode("ascii"))
                self.assertEqual(seen["bodies"][0]["state"]["task"], UNICODE_TEXT)
                self.assertEqual(seen["logs"][0]["desc_hash"],
                                 module.hashlib.sha256(UNICODE_TEXT.encode("utf-8")).hexdigest()[:12])
                out = json.loads(result.stdout.decode("ascii"))
                self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["message"], UNICODE_TEXT)

    def test_report_check_continues_once(self):
        cfg = module.settings() | {"enabled": True}
        payload = {"agent_type": "scout", "session_id": "session", "agent_id": "agent",
                   "last_assistant_message": "RESULT: done"}
        with patch.object(module, "STATE", Path(self.temp.name) / "state"), patch.object(module, "log"):
            first = module.subagent_stop(payload, cfg)
            self.assertEqual(first["decision"], "block")
            payload["stop_hook_active"] = True
            self.assertIsNone(module.subagent_stop(payload, cfg))

    def test_report_check_does_not_block_on_material_gap(self):
        cfg = module.settings() | {"enabled": True}
        payload = {"agent_type": "scout", "session_id": "session", "agent_id": "agent",
                   "last_assistant_message": "RESULT: done\nEVIDENCE: ran tests, exit 0\n"
                                             "CONFIDENCE: high\nUNVERIFIED: prod config untested"}
        response = {"answers": {"supported": {"type": "noul", "noul": 0.95},
                                "material_gap": {"type": "noul", "noul": 0.95}}}
        with patch.object(module, "STATE", Path(self.temp.name) / "state"), patch.object(module, "log"):
            self.assertIsNone(module.subagent_stop(payload, cfg, classify_fn=lambda b, k: response))

    def test_shift_context_preserves_latest_user_instruction(self):
        cfg = module.settings() | {"enabled": True}
        with patch.object(module, "STATE", Path(self.temp.name) / "state"), patch.object(module, "log"), \
             patch.object(module.client, "ask", return_value={"continues": {"noul": 0.1}}):
            self.assertIsNone(module.shift({"session_id": "s", "prompt": "build a widget"}, cfg))
            out = module.shift({"session_id": "s", "prompt": "plan a holiday"}, cfg)
        self.assertIn("user's latest instruction", out["hookSpecificOutput"]["additionalContext"])

    def test_busy_state_lock_does_not_skip_later_work(self):
        # A state lock still held at the hook's deadline must not skip the report
        # check (critic stop) or drop the new-task context (shift).
        guard = module.guard_module()
        calls = []

        def busy(path, mutate_fn, deadline=None):
            calls.append(deadline)
            raise BlockingIOError(11, "lock busy")

        guard.update_state = busy
        cfg = module.settings() | {"enabled": True, "report_roles": ["critic"]}
        payload = {"agent_type": "critic", "session_id": "session", "agent_id": "agent",
                   "last_assistant_message": "RESULT: done"}
        with patch.object(module, "guard_module", return_value=guard), \
             patch.object(module, "STATE", Path(self.temp.name) / "state"), patch.object(module, "log"), \
             patch.object(module.client, "ask", return_value={"continues": {"noul": 0.1}}):
            self.assertEqual(module.subagent_stop(payload, cfg)["decision"], "block")
            module.shift({"session_id": "s", "prompt": "build a widget"}, cfg)
            guard.load_session_state = lambda sid, state: {"recent_prompts": ["build a widget"]}
            out = module.shift({"session_id": "s", "prompt": "plan a holiday"}, cfg)
        self.assertIn("user's latest instruction", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(isinstance(d, float) for d in calls), calls)

    def run_main(self, payload, raw=None):
        import io
        from contextlib import redirect_stdout
        data = (raw if raw is not None else json.dumps(payload)).encode("utf-8")
        stdin = type("S", (), {"buffer": io.BytesIO(data)})()
        out = io.StringIO()
        with patch.object(module.sys, "stdin", stdin), redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    def test_crash_fails_closed_for_push_only(self):
        def bash(command):
            return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}

        with patch.object(module, "guard_module", side_effect=RuntimeError("guard missing")):
            code, out = self.run_main(bash("git push origin main"))
            self.assertEqual(code, 0)
            decision = json.loads(out)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("fail-closed", decision["permissionDecisionReason"])
            self.assertEqual(self.run_main(bash("ls -la")), (0, ""))
            # unparsable payload: match the raw text
            code, out = self.run_main(None, raw="{not json git push")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(self.run_main(None, raw="{not json"), (0, ""))
            # non-Bash tools are not gated
            self.assertEqual(self.run_main({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                                            "tool_input": {"command": "git push"}}), (0, ""))

    def test_handoff_grade_is_optional_and_blocks_weak_handoff(self):
        cfg = module.settings() | {"enabled": True}
        with patch.object(module.client, "ask", return_value={"actionable": {"score": 1}}), \
             patch.object(module, "log"):
            result = module.grade_handoff("GOAL: test\nNEXT STEP: run tests", cfg)
        self.assertEqual(result, {"score": 1.0, "weak": True})

    def test_explicit_claude_key_source_does_not_copy_key(self):
        fake_home = Path(self.temp.name)
        claude = fake_home / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"env": {"TYPESAFE_API_KEY": "private-test-key"}}))
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}), patch.object(Path, "home", return_value=fake_home):
            self.assertIsNone(module.client.api_key({}))
            self.assertEqual(module.client.api_key({"api_key_source": "claude_settings"}), "private-test-key")


if __name__ == "__main__":
    unittest.main()

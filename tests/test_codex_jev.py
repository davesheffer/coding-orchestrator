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

    def test_route_respects_send_prompt_false(self):
        cfg = module.settings() | {"enabled": True, "send_prompt": False}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "PRIVATE-TASK-SENTINEL"}}
        def classify(body, key):
            self.assertNotIn("PRIVATE-TASK-SENTINEL", json.dumps(body))
            return {"answers": {"model": {"choice": "sol", "confidence": 0.9}}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), patch.object(module, "log"):
            out = module.route(payload, cfg, classify)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6-sol")

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
        for value in (float("nan"), float("inf"), -1, 2, None):
            with self.subTest(value=value):
                out, logs = self.route_default({"model": {"choice": "luna", "confidence": value}})
                self.assertIsNone(out)
                self.assertEqual((logs[0]["applied"], logs[0]["reason"]), (False, "invalid confidence"))
                json.dumps(logs[0], allow_nan=False)

    def test_route_logs_every_decision_like_jev_route(self):
        out, logs = self.route_default({"model": {"choice": "sol", "confidence": 0.9}})
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6-sol")
        entry = logs[0]
        self.assertEqual((entry["applied"], entry["reason"], entry["subagent_type"]), (True, "applied", "default"))
        self.assertIsInstance(entry["latency_ms"], int)
        self.assertEqual(entry["desc_hash"], module.hashlib.sha256(b"read a file").hexdigest()[:12])
        self.assertNotIn("read a file", json.dumps(entry))
        out, logs = self.route_default({"model": {"choice": "sol", "confidence": 0.2}})
        self.assertIsNone(out)
        self.assertEqual((logs[0]["applied"], logs[0]["reason"]), (False, "below min_confidence"))

    def test_route_rejects_non_string_choice_without_raising(self):
        out, logs = self.route_default({"model": {"choice": ["luna"], "confidence": 0.9}})
        self.assertIsNone(out)
        self.assertEqual((logs[0]["applied"], logs[0]["reason"]), (False, "invalid label"))
        self.assertEqual(logs[0]["choice"], repr(["luna"]))
        json.dumps(logs[0])

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

    def test_shift_context_preserves_latest_user_instruction(self):
        cfg = module.settings() | {"enabled": True}
        with patch.object(module, "STATE", Path(self.temp.name) / "state"), patch.object(module, "log"), \
             patch.object(module.client, "ask", return_value={"continues": {"noul": 0.1}}):
            self.assertIsNone(module.shift({"session_id": "s", "prompt": "build a widget"}, cfg))
            out = module.shift({"session_id": "s", "prompt": "plan a holiday"}, cfg)
        self.assertIn("user's latest instruction", out["hookSpecificOutput"]["additionalContext"])

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

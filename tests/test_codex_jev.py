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
        self.assertEqual(self.route_with({"choice": "sol", "confidence": 0.6}), "gpt-6-sol")

    def test_route_escalates_when_stronger_models_are_likely(self):
        answer = {"choice": "luna", "confidence": 0.55,
                  "probabilities": {"luna": 0.55, "sol": 0.35, "astra": 0.1}}
        self.assertEqual(self.route_with(answer), "gpt-6-sol")
        self.assertEqual((self.logged["choice"], self.logged["escalated_to"], self.logged["escalated_mass"]),
                         ("luna", "sol", 0.45))
        answer = {"choice": "sol", "confidence": 0.8, "probabilities": {"sol": 0.8, "astra": 0.2}}
        self.assertEqual(self.route_with(answer), "gpt-6-sol")
        answer = {"choice": "luna", "confidence": 0.9, "probabilities": {"luna": 0.9, "sol": "nan"}}
        self.assertEqual(self.route_with(answer), "gpt-6-luna")

    def test_route_respects_send_prompt_false(self):
        cfg = module.settings() | {"enabled": True, "send_prompt": False}
        payload = {"tool_name": "Agent", "tool_input": {"agent_type": "default", "message": "PRIVATE-TASK-SENTINEL"}}
        def classify(body, key):
            self.assertNotIn("PRIVATE-TASK-SENTINEL", json.dumps(body))
            return {"answers": {"model": {"choice": "sol", "confidence": 0.9}}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), patch.object(module, "log"):
            out = module.route(payload, cfg, classify)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["model"], "gpt-6-sol")

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

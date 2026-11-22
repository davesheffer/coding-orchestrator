import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "bin" / "spawn-guard.py"
spec = importlib.util.spec_from_file_location("spawn_guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def spawn(prompt="do it", tool="Agent", **fields):
    return {"tool_name": tool, "tool_input": {"prompt": prompt, **fields}}


class SpawnGuardTests(unittest.TestCase):
    def home_with_critic(self, model):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "agents").mkdir()
        (home / "agents" / "critic.md").write_text(f"---\nname: critic\nmodel: {model}\n---\n", encoding="utf-8")
        return home

    def test_general_purpose_denied(self):
        reason = guard.decide(spawn(subagent_type="general-purpose"))
        self.assertIn("ROLE-EXCEPTION", reason)

    def test_missing_subagent_type_denied(self):
        self.assertIn("ROLE-EXCEPTION", guard.decide(spawn()))

    def test_role_exception_allows_general_purpose(self):
        prompt = "work\nROLE-EXCEPTION: needs a tool no role has\n"
        self.assertIsNone(guard.decide(spawn(prompt, subagent_type="general-purpose")))

    def test_critic_frontmatter_fable_denied(self):
        home = self.home_with_critic("fable")
        self.assertIn("RISK", guard.decide(spawn(subagent_type="critic"), home=home))

    def test_critic_model_fable_denied(self):
        self.assertIn("RISK", guard.decide(spawn(subagent_type="critic", model="fable")))

    def test_critic_fable_with_risk_allowed(self):
        prompt = "review\nRISK: security\n"
        self.assertIsNone(guard.decide(spawn(prompt, subagent_type="critic", model="fable")))

    def test_critic_opus_allowed(self):
        self.assertIsNone(guard.decide(spawn(subagent_type="critic", model="opus")))

    def test_builder_and_fork_allowed(self):
        for kind in ("builder", "fork"):
            with self.subTest(kind=kind):
                self.assertIsNone(guard.decide(spawn(subagent_type=kind)))

    def test_other_tool_allowed(self):
        self.assertIsNone(guard.decide({"tool_name": "Bash", "tool_input": {"command": "ls"}}))

    def test_invalid_json_prints_nothing(self):
        result = subprocess.run([sys.executable, str(GUARD)], input=b"not json", capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_deny_output_shape(self):
        payload = json.dumps(spawn(subagent_type="claude")).encode()
        result = subprocess.run([sys.executable, str(GUARD)], input=payload, capture_output=True)
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()

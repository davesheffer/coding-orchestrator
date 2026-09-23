import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("scout", "runner", "builder", "critic")


def frontmatter(path):
    text = path.read_text()
    first, body = text.split("\n---\n", 1)
    fields = {}
    for line in first.splitlines()[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields, body


class BundleContractTests(unittest.TestCase):
    def test_role_names_models_boundaries_and_reports_match(self):
        expected = {
            "scout": ("sonnet", "gpt-6-luna", "read-only"),
            "runner": ("sonnet", "gpt-6-luna", "workspace-write"),
            "builder": ("sonnet", "gpt-6-sol", "workspace-write"),
            "critic": ("fable", "gpt-6-astra", "read-only"),
        }
        for name in ROLES:
            with self.subTest(role=name):
                claude, claude_body = frontmatter(ROOT / "agents" / f"{name}.md")
                codex = tomllib.loads((ROOT / "codex/agents" / f"{name}.toml").read_text())
                self.assertEqual(claude["name"], name)
                self.assertEqual(codex["name"], name)
                self.assertEqual(claude["model"], expected[name][0])
                self.assertEqual(codex["model"], expected[name][1])
                self.assertEqual(codex["sandbox_mode"], expected[name][2])
                self.assertEqual(codex["web_search"], "disabled")
                self.assertFalse(codex["sandbox_workspace_write"]["network_access"])
                self.assertTrue(all(value is False for value in codex["features"].values()))
                self.assertIn("skill_mcp_dependency_install", codex["features"])
                self.assertFalse(codex["agents"]["enabled"])
                self.assertEqual(claude["disallowedTools"], "mcp__*")
                for field in ("RESULT:", "EVIDENCE:", "CONFIDENCE:", "UNVERIFIED:"):
                    self.assertIn(field, claude_body)
                    self.assertIn(field, codex["developer_instructions"])

    def test_instruction_markers_and_hook_template(self):
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        codex = (ROOT / "codex/AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(claude.count("CLAUDE-ORCHESTRATOR:START"), 1)
        self.assertEqual(claude.count("CLAUDE-ORCHESTRATOR:END"), 1)
        self.assertEqual(codex.count("CODEX-ORCHESTRATOR:START"), 1)
        self.assertEqual(codex.count("CODEX-ORCHESTRATOR:END"), 1)
        hooks = json.loads((ROOT / "hooks.json").read_text())
        commands = [h["command"] for groups in hooks["hooks"].values()
                    for group in groups for h in group["hooks"]]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all("__RELAY__" in command for command in commands))

    def test_json_and_toml_sources_parse(self):
        json.loads((ROOT / "hooks.json").read_text())
        json.loads((ROOT / "relay/config.json").read_text())
        tomllib.loads((ROOT / "codex/config.example.toml").read_text())
        for name in ROLES:
            tomllib.loads((ROOT / "codex/agents" / f"{name}.toml").read_text())


if __name__ == "__main__":
    unittest.main()

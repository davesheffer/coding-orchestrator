import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "claude" / "install.py"


class ClaudeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.home = Path(self.temp.name) / "custom claude home"

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self, *args):
        env = os.environ | {"CLAUDE_HOME": str(self.home)}
        return subprocess.run(["python3", str(INSTALL), *args], env=env,
                              text=True, capture_output=True)

    def snapshot(self):
        if not self.home.exists():
            return {}
        return {str(p.relative_to(self.home)): (
            ("link", os.readlink(p)) if p.is_symlink()
            else ("file", p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        ) for p in self.home.rglob("*") if p.is_file() or p.is_symlink()}

    def test_fresh_install_uses_custom_home_and_is_idempotent(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads((self.home / "settings.json").read_text())
        commands = [h["command"] for groups in settings["hooks"].values()
                    for group in groups for h in group["hooks"]]
        self.assertTrue(all(str(self.home / "relay/relay.py") in command for command in commands))
        self.assertTrue(all("$HOME/.claude" not in command for command in commands))
        for role in ("scout", "runner", "builder", "critic"):
            self.assertTrue((self.home / "agents" / f"{role}.md").exists())
        helper = self.home / "bin/pr-status"
        self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        before = self.snapshot()
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_preserves_private_instructions_settings_and_relay_config(self):
        self.home.mkdir(parents=True)
        self.home.joinpath("CLAUDE.md").write_text("private instructions\n")
        self.home.joinpath("settings.json").write_text(json.dumps({
            "theme": "dark", "hooks": {"Stop": [{"hooks": [{
                "type": "command", "command": "printf private"
            }]}]}
        }))
        self.home.joinpath("relay").mkdir()
        config = {"soft_tokens": 10, "hard_tokens": 20}
        self.home.joinpath("relay/config.json").write_text(json.dumps(config))
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        instructions = self.home.joinpath("CLAUDE.md").read_text()
        self.assertTrue(instructions.startswith("private instructions\n"))
        self.assertEqual(instructions.count("CLAUDE-ORCHESTRATOR:START"), 1)
        settings = json.loads(self.home.joinpath("settings.json").read_text())
        self.assertEqual(settings["theme"], "dark")
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("printf private", stop_commands)
        self.assertEqual(json.loads(self.home.joinpath("relay/config.json").read_text()), config)

    def test_local_managed_edit_requires_force_and_keeps_unique_backup(self):
        self.assertEqual(self.run_install().returncode, 0)
        role = self.home / "agents/scout.md"
        changed = role.read_bytes() + b"\nlocal change\n"
        role.write_bytes(changed)
        before = self.snapshot()
        refused = self.run_install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.snapshot(), before)
        forced = self.run_install("--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(role.with_name("scout.md.bak").read_bytes(), changed)
        role.write_bytes(changed + b"again\n")
        self.assertEqual(self.run_install("--force").returncode, 0)
        self.assertTrue(role.with_name("scout.md.bak.1").exists())

    def test_dry_run_validation_and_symlink_refusal_make_no_changes(self):
        dry = self.run_install("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(self.home.exists())
        self.home.mkdir(parents=True)
        self.home.joinpath("settings.json").write_text("{broken")
        before = self.snapshot()
        self.assertNotEqual(self.run_install("--force").returncode, 0)
        self.assertEqual(self.snapshot(), before)
        self.home.joinpath("settings.json").unlink()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.home.joinpath("agents").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.run_install("--force").returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unknown_argument_is_rejected(self):
        result = self.run_install("--unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()

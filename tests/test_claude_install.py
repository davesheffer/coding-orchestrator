import hashlib
import itertools
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
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
        return subprocess.run([sys.executable, str(INSTALL), *args], env=env,
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
        settings = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "claude-opus-5-5")
        commands = [h["command"] for groups in settings["hooks"].values()
                    for group in groups for h in group["hooks"]]
        self.assertTrue(all(str(self.home / "relay/relay.py") in command for command in commands))
        self.assertTrue(all("$HOME/.claude" not in command for command in commands))
        for role in ("scout", "runner", "builder", "critic"):
            self.assertTrue((self.home / "agents" / f"{role}.md").exists())
        helper = self.home / "bin/pr-status"
        instructions = (self.home / "CLAUDE.md").read_text(encoding="utf-8")
        expected_command = ("python " if os.name == "nt" else "") + shlex.quote(str(helper))
        self.assertIn(f"using `{expected_command}`", instructions)
        if os.name == "posix":
            self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        before = self.snapshot()
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_preserves_private_instructions_settings_and_relay_config(self):
        self.home.mkdir(parents=True)
        self.home.joinpath("CLAUDE.md").write_text("private instructions\n")
        self.home.joinpath("settings.json").write_text(json.dumps({
            "theme": "dark", "model": "custom-model", "hooks": {"Stop": [{"hooks": [{
                "type": "command", "command": "printf private"
            }]}]}
        }))
        self.home.joinpath("relay").mkdir()
        config = {"soft_tokens": 10, "hard_tokens": 20}
        self.home.joinpath("relay/config.json").write_text(json.dumps(config))
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        instructions = self.home.joinpath("CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(instructions.startswith("private instructions\n"))
        self.assertEqual(instructions.count("CLAUDE-ORCHESTRATOR:START"), 1)
        settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["model"], "custom-model")
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("printf private", stop_commands)
        self.assertEqual(json.loads(self.home.joinpath("relay/config.json").read_text(encoding="utf-8")), config)

    @unittest.skipUnless(os.name == "posix", "shell entry points require POSIX process execution")
    def test_actual_shell_entry_point_runs(self):
        result = subprocess.run([str(ROOT / "install.sh"), "--dry-run"],
                                env=os.environ | {"CLAUDE_HOME": str(self.home)},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.home.exists())

    def test_previous_release_migrates_without_duplicate_instructions(self):
        fixture = ROOT / "tests/fixtures/previous-release/claude"
        original_home = self.home
        for newline, extra_newline in itertools.product((b"\n", b"\r\n"), (False, True)):
            with self.subTest(newline=newline, extra_newline=extra_newline):
                self.home = original_home / f"{len(newline)}-{extra_newline}"
                shutil.copytree(fixture, self.home)
                for path in self.home.rglob("*"):
                    if path.is_file():
                        data = path.read_bytes().replace(b"\r\n", b"\n")
                        if extra_newline:
                            data += b"\n"
                        path.write_bytes(data.replace(b"\n", newline))
                previous = self.home.joinpath("CLAUDE.md").read_bytes()
                result = self.run_install()
                self.assertEqual(result.returncode, 0, result.stderr)
                instructions = self.home.joinpath("CLAUDE.md").read_text(encoding="utf-8")
                self.assertTrue(instructions.startswith("<!-- CLAUDE-ORCHESTRATOR:START -->"))
                self.assertNotIn("cheap hands, expensive eyes", instructions)
                self.assertEqual(self.home.joinpath("CLAUDE.md.bak").read_bytes(), previous)
                upgraded_settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
                self.assertEqual(upgraded_settings["model"], "claude-opus-5-5")
                for name in ("scout", "runner", "builder", "critic"):
                    path = self.home / "agents" / f"{name}.md"
                    self.assertEqual(path.read_bytes(), (ROOT / "agents" / path.name).read_bytes())
                    self.assertTrue(path.with_name(path.name + ".bak").exists())
                before = self.snapshot()
                self.assertEqual(self.run_install().returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_backslash_manifest_upgrade_preserves_exact_hash_checks(self):
        self.assertEqual(self.run_install().returncode, 0)
        role = self.home / "agents/scout.md"
        old_release = role.read_bytes() + b"\nprevious bundle version\n"
        role.write_bytes(old_release)
        manifest_path = self.home / ".coding-orchestrator-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["agents/scout.md"] = hashlib.sha256(old_release).hexdigest()
        manifest["files"] = {key.replace("/", "\\"): value for key, value in manifest["files"].items()}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(role.with_name("scout.md.bak").read_bytes(), old_release)
        self.assertEqual(role.read_bytes(), (ROOT / "agents/scout.md").read_bytes())
        files = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
        self.assertIn("agents/scout.md", files)
        self.assertTrue(all("\\" not in key for key in files))
        # A local newline edit must not be mistaken for a known release.
        data = role.read_bytes()
        role.write_bytes(data.replace(b"\r\n", b"\n") if b"\r\n" in data
                         else data.replace(b"\n", b"\r\n"))
        before = self.snapshot()
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_edited_legacy_files_are_not_recognized_as_unmodified(self):
        fixture = ROOT / "tests/fixtures/previous-release/claude"
        shutil.copytree(fixture, self.home)
        role = self.home / "agents/scout.md"
        role.write_bytes(role.read_bytes() + b"\nprivate edit\n")
        instructions = self.home / "CLAUDE.md"
        customized = instructions.read_bytes() + b"\nprivate instructions\n"
        instructions.write_bytes(customized)
        before = self.snapshot()
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)
        result = self.run_install("--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(instructions.read_bytes().startswith(customized))

    def test_installed_relay_uses_own_home_without_install_environment(self):
        self.assertEqual(self.run_install().returncode, 0)
        env = dict(os.environ)
        env.pop("CLAUDE_HOME", None)
        env["HOME"] = str(Path(self.temp.name) / "unrelated-home")
        relay = self.home / "relay/relay.py"
        body = "GOAL: preserve Unicode \u05e9\u05dc\u05d5\u05dd \U0001f680\nNEXT STEP: verify installed handoff"
        handoff = subprocess.run([sys.executable, str(relay), "handoff", "--no-open"],
                                 env=env, input=body, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        saved = list(self.home.joinpath("relay/handoffs").glob("*.md"))
        self.assertEqual(len(saved), 1)
        self.assertIn(body, saved[0].read_text(encoding="utf-8"))
        recovered = subprocess.run([sys.executable, str(relay), "prompt"], env=env,
                                   input=json.dumps({"prompt": f"relay:{saved[0].stem}"}),
                                   text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn(body, json.loads(recovered.stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(Path(env["HOME"]).exists())

    def test_migrates_only_owned_hooks_and_preserves_user_commands(self):
        self.home.mkdir(parents=True)
        preserved = [
            'python3 /other-project/relay/relay.py stop',
            'python3 /other-project/relay/relay.py stop && audit-command',
            'python3 "$HOME/.claude/relay/relay.py" stop && audit-command',
            'echo "relay/relay.py stop"',
            "python3 'unterminated",
        ]
        legacy = 'python3 "$HOME/.claude/relay/relay.py" stop 2>/dev/null || true'
        group = {"matcher": "*", "timeout": 5, "hooks": [
            {"type": "command", "command": command} for command in [legacy, *preserved]
        ]}
        self.home.joinpath("settings.json").write_text(json.dumps({"hooks": {"Stop": [group]}}))
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
        groups = settings["hooks"]["Stop"]
        self.assertEqual(groups[0], {**group, "hooks": group["hooks"][1:]})
        self.assertEqual(len(groups), 2)
        commands = [h["command"] for g in groups for h in g["hooks"]]
        self.assertNotIn(legacy, commands)
        self.assertEqual(len(commands), len(preserved) + 1)
        before = self.snapshot()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(os.name == "posix", "generated hook commands require a POSIX shell")
    def test_installed_relay_and_hook_work_without_install_environment(self):
        # Spaces, a quote and shell syntax must remain literal in generated paths.
        self.home = self.home / "literal ' $(false)"
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(os.environ)
        env.pop("CLAUDE_HOME", None)
        env["HOME"] = str(Path(self.temp.name) / "unrelated-home")
        relay = self.home / "relay/relay.py"
        instructions = self.home.joinpath("CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(f"python3 {shlex.quote(str(relay))} handoff", instructions)
        self.assertNotIn("__RELAY__", instructions)
        self.assertNotIn("__PR_STATUS__", instructions)
        self.home.joinpath("relay/config.json").write_text('{"auto_open": false}')
        handoff = subprocess.run([sys.executable, str(relay), "handoff", "--no-open"],
                                 env=env, input="GOAL: recover custom installation\nNEXT STEP: run checks",
                                 text=True, capture_output=True)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        saved = list(self.home.joinpath("relay/handoffs").glob("*.md"))
        self.assertEqual(len(saved), 1)
        settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
        command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        recovered = subprocess.run(["sh", "-c", command], env=env,
                                   input=json.dumps({"prompt": f"relay:{saved[0].stem}"}),
                                   text=True, capture_output=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn("GOAL: recover custom installation", recovered.stdout)
        status = subprocess.run([sys.executable, str(relay), "status"], env=env,
                                text=True, capture_output=True)
        self.assertFalse(json.loads(status.stdout)["config"]["auto_open"])
        transcript = Path(self.temp.name) / "red.jsonl"
        transcript.write_text('{"type":"assistant","message":{"usage":{"input_tokens":300000}}}\n')
        payload = json.dumps({"session_id": "custom-state", "transcript_path": str(transcript)})
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        stop = subprocess.run(["sh", "-c", command], env=env, input=payload,
                              text=True, capture_output=True)
        self.assertEqual(json.loads(stop.stdout)["decision"], "block")
        self.assertTrue(self.home.joinpath("relay/state/custom-state.json").exists())
        again = subprocess.run(["sh", "-c", command], env=env, input=payload,
                               text=True, capture_output=True)
        self.assertEqual(again.stdout, "")
        self.assertFalse(Path(env["HOME"]).exists())

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

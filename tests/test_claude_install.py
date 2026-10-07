import hashlib
import importlib.util
import itertools
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "claude" / "install.py"
_SPEC = importlib.util.spec_from_file_location("claude_install_module", INSTALL)
install_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install_module)


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

    def rendered_agent(self, name):
        text = (ROOT / "agents" / f"{name}.md").read_bytes().decode("utf-8")
        return install_module.fill_placeholders(text, {
            "__ROLE_GUARD__": json.dumps(
                f"{install_module.hook_python()} "
                f"{shlex.quote((self.home / 'bin/role-guard.py').as_posix())} || exit 2"),
            "__RO__": f"{install_module.hook_python()} "
                      f"{shlex.quote((self.home / 'bin/ro.py').as_posix())}",
        }).encode("utf-8")

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
        self.assertEqual((self.home / "bin/rollover-open.py").read_bytes(),
                         (ROOT / "bin/rollover-open.py").read_bytes())
        self.assertEqual((self.home / "bin/session-focus.py").read_bytes(),
                         (ROOT / "bin/session-focus.py").read_bytes())
        instructions = (self.home / "CLAUDE.md").read_text(encoding="utf-8")
        expected_command = f"{install_module.hook_python()} {shlex.quote(str(helper))}"
        self.assertIn(f"using `{expected_command}`", instructions)
        self.assertTrue(instructions.startswith("<!-- CLAUDE-ORCHESTRATOR:START -->"))
        self.assertTrue(instructions.endswith("<!-- CLAUDE-ORCHESTRATOR:END -->\n"))
        if os.name == "posix":
            self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        before = self.snapshot()
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_scout_guard_command_is_valid_yaml_with_apostrophe_home(self):
        self.home = Path(self.temp.name) / "o'brien"
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.home / "agents/scout.md").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if l.strip().startswith("command:"))
        value = json.loads(line.split("command:", 1)[1].strip())
        self.assertIn("role-guard.py", value)
        self.assertTrue(value.endswith("|| exit 2"))
        self.assertEqual(shlex.split(value)[-4], (self.home / "bin/role-guard.py").as_posix())
        self.assertEqual((self.home / "agents/scout.md").read_bytes(), self.rendered_agent("scout"))

    def test_role_frontmatter_rejects_unquoted_yaml_colon(self):
        source = (ROOT / "agents/scout.md").read_bytes()
        install_module.validate_role(Path("scout.md"), source, "scout")
        broken = source.replace(b"enforced; a role", b"enforced: a role")
        self.assertNotEqual(broken, source)
        with self.assertRaisesRegex(ValueError, "quote this YAML value"):
            install_module.validate_role(Path("scout.md"), broken, "scout")
        for name in install_module.ROLE_NAMES:
            data = (ROOT / "agents" / f"{name}.md").read_bytes()
            install_module.validate_role(Path(f"{name}.md"), data, name)

    def test_scout_role_is_rendered_with_guard_and_helpers(self):
        self.assertEqual(self.run_install().returncode, 0)
        text = (self.home / "agents/scout.md").read_text(encoding="utf-8")
        self.assertNotIn("__ROLE_GUARD__", text)
        self.assertNotIn("__RO__", text)
        self.assertIn("role-guard.py' || exit 2", text)
        line = next(l for l in text.splitlines() if l.strip().startswith("command:"))
        value = line.split("command:", 1)[1].strip()
        self.assertTrue(value.startswith('"') and value.endswith('"'))
        self.assertNotIn("\\", value)
        self.assertIn("role-guard.py", value)
        self.assertTrue(value.endswith(" || exit 2\""))
        self.assertEqual(json.loads(value), value[1:-1])
        self.assertIn((self.home / "bin/ro.py").as_posix(), text)
        for name in ("role-guard.py", "ro.py"):
            self.assertEqual((self.home / "bin" / name).read_bytes(), (ROOT / "bin" / name).read_bytes())
        before = self.snapshot()
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("backup", again.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_upgrade_removes_hunch_block_from_polluted_home(self):
        self.assertEqual(self.run_install().returncode, 0)
        path = self.home / "CLAUDE.md"
        clean = path.read_bytes()
        newline = b"\r\n" if clean.endswith(b"\r\n") else b"\n"
        hunch = "<!-- HUNCH:START — auto-generated, do not edit by hand -->\nrepo-only\n<!-- HUNCH:END -->\n"
        polluted = clean + newline + hunch.encode().replace(b"\n", newline)
        path.write_bytes(polluted)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), clean)
        self.assertEqual(path.with_name("CLAUDE.md.bak").read_bytes(), polluted)
        before = self.snapshot()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_instructions_omit_text_outside_managed_block(self):
        source = (b"<!-- CLAUDE-ORCHESTRATOR:START -->\nrules\n<!-- CLAUDE-ORCHESTRATOR:END -->\n\n"
                  b"<!-- HUNCH:START -->\nrepo-only\n<!-- HUNCH:END -->\n")
        merged = install_module.merge_instructions(None, source, Path("CLAUDE.md"))
        self.assertEqual(merged, b"<!-- CLAUDE-ORCHESTRATOR:START -->\nrules\n"
                                 b"<!-- CLAUDE-ORCHESTRATOR:END -->\n")

    def test_upgrade_drops_hunch_block_copied_by_earlier_fresh_install(self):
        block = b"<!-- CLAUDE-ORCHESTRATOR:START -->\nrules\n<!-- CLAUDE-ORCHESTRATOR:END -->"
        hunch = "<!-- HUNCH:START — auto-generated, do not edit by hand -->\nrepo-only\n<!-- HUNCH:END -->".encode()
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                source = (block + b"\n").replace(b"\n", newline)
                polluted = (block + b"\n\n" + hunch + b"\n").replace(b"\n", newline)
                merged = install_module.merge_instructions(polluted, source, Path("CLAUDE.md"))
                self.assertEqual(merged, source)
                self.assertEqual(install_module.merge_instructions(merged, source, Path("CLAUDE.md")), source)
                private = b"mine\n" + polluted + b"also mine\n"
                self.assertEqual(install_module.merge_instructions(private, source, Path("CLAUDE.md")),
                                 b"mine\n" + source.rstrip() + newline + b"also mine\n")

    def test_upgrade_keeps_hunch_text_the_user_placed_elsewhere(self):
        source = b"<!-- CLAUDE-ORCHESTRATOR:START -->\nrules\n<!-- CLAUDE-ORCHESTRATOR:END -->\n"
        hunch = "<!-- HUNCH:START — auto-generated, do not edit by hand -->\nx\n<!-- HUNCH:END -->\n".encode()
        for existing in (hunch + source, source + b"notes\n" + hunch):
            with self.subTest(existing=existing):
                self.assertEqual(install_module.merge_instructions(existing, source, Path("CLAUDE.md")), existing)

    def test_fresh_instructions_keep_source_line_endings(self):
        source = b"<!-- CLAUDE-ORCHESTRATOR:START -->\r\nrules\r\n<!-- CLAUDE-ORCHESTRATOR:END -->\r\n"
        self.assertEqual(install_module.merge_instructions(None, source, Path("CLAUDE.md")), source)
        self.assertEqual(install_module.merge_instructions(b"mine\r\n", source, Path("CLAUDE.md")),
                         b"mine\r\n" + source)

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

    def test_installed_instructions_substitute_platform_interpreter(self):
        # A placeholder token inside the install path must survive substitution literally.
        self.home = self.home / "__PR_STATUS__ __PYTHON__ __RELAY__"
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        python = install_module.hook_python()
        relay = shlex.quote(str(self.home / "relay/relay.py"))
        helper = shlex.quote(str(self.home / "bin/pr-status"))
        ask_jev = shlex.quote(str(self.home / "bin/ask-jev.py"))
        instructions = self.home.joinpath("CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(f"{python} {relay} handoff --title", instructions)
        self.assertIn(f"using `{python} {helper}`", instructions)
        self.assertIn(f"`{python} {ask_jev} -q", instructions)
        leftover = instructions.replace(relay, "").replace(helper, "").replace(ask_jev, "")
        for placeholder in ("__PYTHON__", "__RELAY__", "__PR_STATUS__", "__ASK_JEV__"):
            self.assertNotIn(placeholder, leftover)
        settings = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        commands = [h["command"] for groups in settings["hooks"].values()
                    for group in groups for h in group["hooks"]]
        self.assertEqual(sorted(commands), sorted(f"{python} {relay} {action} 2>/dev/null || true"
                                                  for action in ("prompt", "stop")))

    def test_fill_placeholders_is_single_pass(self):
        values = {"__PYTHON__": "__RELAY__", "__RELAY__": "'/x/__PR_STATUS__'",
                  "__PR_STATUS__": "__PYTHON__"}
        self.assertEqual(install_module.fill_placeholders("__PYTHON__ __RELAY__ __PR_STATUS__", values),
                         "__RELAY__ '/x/__PR_STATUS__' __PYTHON__")

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
                self.assertTrue(instructions.endswith("<!-- CLAUDE-ORCHESTRATOR:END -->\n"))
                self.assertEqual(self.home.joinpath("CLAUDE.md.bak").read_bytes(), previous)
                upgraded_settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
                self.assertEqual(upgraded_settings["model"], "claude-opus-5-5")
                for name in ("scout", "runner", "builder", "critic"):
                    path = self.home / "agents" / f"{name}.md"
                    self.assertEqual(path.read_bytes(), self.rendered_agent(name))
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
        self.assertEqual(role.read_bytes(), self.rendered_agent("scout"))
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
        windows = 'python "$HOME/.claude/relay/relay.py" stop 2>/dev/null || true'
        group = {"matcher": "*", "timeout": 5, "hooks": [
            {"type": "command", "command": command} for command in [legacy, windows, *preserved]
        ]}
        self.home.joinpath("settings.json").write_text(json.dumps({"hooks": {"Stop": [group]}}))
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
        groups = settings["hooks"]["Stop"]
        self.assertEqual(groups[0], {**group, "hooks": group["hooks"][2:]})
        self.assertEqual(len(groups), 2)
        commands = [h["command"] for g in groups for h in g["hooks"]]
        self.assertNotIn(legacy, commands)
        self.assertNotIn(windows, commands)
        self.assertEqual(len(commands), len(preserved) + 1)
        before = self.snapshot()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_jev_hook_is_opt_in_owned_and_preserves_user_hooks(self):
        self.home.mkdir(parents=True)
        user = {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit-bash"}]}
        self.home.joinpath("settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [user]}}))
        bin_dir = self.home / "bin"

        def group(matcher, script, sub=""):
            command = (f"{install_module.hook_python()} {shlex.quote(str(bin_dir / script))}{sub}"
                       " 2>/dev/null || true")
            timeout = 10 if sub == " gate" else 5
            return {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}

        def hooks():
            settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
            return settings["hooks"].get("PreToolUse"), settings["hooks"].get("PostToolUse")

        config = self.home / "relay/config.json"
        result = self.run_install("--jev")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["jev"], {"enabled": True})
        self.assertEqual(hooks(), (
            [user, group("Agent|Task", "jev-route.py"),
             group("Bash|PowerShell", "jev-guard.py", " gate"),
             group("SubagentHandback", "jev-guard.py", " handback")],
            [group("Agent|Task|SubagentHandback", "jev-guard.py", " agent-done")]))
        manifest = json.loads(self.home.joinpath(".coding-orchestrator-manifest.json").read_text())
        for name in ("jev_client.py", "jev-route.py", "jev-guard.py", "jev-report.py", "ask-jev.py"):
            self.assertEqual((bin_dir / name).read_bytes(), (ROOT / "bin" / name).read_bytes())
            self.assertIn(f"bin/{name}", manifest["files"])
        before = self.snapshot()
        self.assertEqual(self.run_install("--jev").returncode, 0)
        self.assertEqual(self.snapshot(), before)
        message = "jev: disabled (was enabled). Re-run with --jev to keep it."
        self.assertNotIn(message, result.stdout)
        dry = self.run_install("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(message, dry.stdout)
        self.assertEqual(self.snapshot(), before)
        off = self.run_install()
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertIn(message, off.stdout)
        self.assertEqual(hooks(), ([user], None))
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["jev"], {"enabled": False})
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn(message, again.stdout)
        self.assertTrue((bin_dir / "jev-route.py").exists())

    def test_rollover_flag_sets_only_that_config_key(self):
        config = self.home / "relay/config.json"
        self.assertEqual(self.run_install("--rollover", "copy").returncode, 0)
        fresh = json.loads(config.read_text(encoding="utf-8"))
        template = json.loads((ROOT / "relay/config.json").read_text(encoding="utf-8"))
        self.assertEqual(fresh, {**template, "rollover": "copy"})
        tuned = {"soft_tokens": 1, "hard_tokens": 2, "auto_open": False, "jev": {"log": False}}
        config.write_text(json.dumps(tuned), encoding="utf-8")
        before = self.snapshot()
        dry = self.run_install("--dry-run", "--rollover", "open")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"write {config}", dry.stdout)
        self.assertEqual(self.snapshot(), before)
        result = self.run_install("--rollover", "copy")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Installing without --jev also pins jev.enabled off, keeping other jev keys.
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")),
                         {**tuned, "rollover": "copy", "jev": {"log": False, "enabled": False}})
        self.assertEqual(json.loads(config.with_name("config.json.bak").read_text()), tuned)
        after = self.snapshot()
        self.assertEqual(self.run_install("--rollover", "copy").returncode, 0)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), after)

    def test_default_install_ignores_malformed_user_pre_tool_use(self):
        self.home.mkdir(parents=True)
        malformed = ["not-a-group", {"matcher": "Bash", "hooks": "not-a-list"}]
        self.home.joinpath("settings.json").write_text(json.dumps({"hooks": {"PreToolUse": malformed}}))
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["hooks"]["PreToolUse"], malformed)

    def test_is_jev_hook_rejects_near_misses(self):
        bin_dir = self.home / "bin"
        route = shlex.quote(str(bin_dir / "jev-route.py"))
        guard = shlex.quote(str(bin_dir / "jev-guard.py"))
        for command in (f"python3 {route} 2>/dev/null || true", f"python3 {route}",
                        'python3 "$HOME/.claude/bin/jev-route.py" 2>/dev/null || true',
                        f"python3 {guard} gate 2>/dev/null || true",
                        f"python3 {guard} agent-done",
                        f"python3 {guard} handback 2>/dev/null || true",
                        'python3 "$HOME/.claude/bin/jev-guard.py" agent-done 2>/dev/null || true',
                        f"python {route} 2>/dev/null || true", f"python {guard} handback",
                        'python "$HOME/.claude/bin/jev-guard.py" gate 2>/dev/null || true'):
            self.assertTrue(install_module.is_jev_hook(command, bin_dir), command)
        for command in (f"python3 {route} --dry 2>/dev/null || true",
                        f"python3 {route} | tee /tmp/log",
                        f"pythonw {route} 2>/dev/null || true",
                        f"python3.12 {route} 2>/dev/null || true",
                        "python3 /other-project/bin/jev-route.py 2>/dev/null || true",
                        f"python3 {route} 2>/dev/null || true && audit",
                        f"python3 {route} gate 2>/dev/null || true",
                        f"python3 {guard} 2>/dev/null || true",
                        f"python3 {guard} rm-rf 2>/dev/null || true",
                        f"python3 {guard} gate agent-done",
                        "python3 'unterminated", None):
            self.assertFalse(install_module.is_jev_hook(command, bin_dir), command)

    def test_hook_interpreter_follows_platform_and_upgrades_strip_either_form(self):
        with mock.patch.object(install_module.os, "name", "nt"):
            self.assertEqual(install_module.hook_python(), "python")
        with mock.patch.object(install_module.os, "name", "posix"):
            self.assertEqual(install_module.hook_python(), "python3")
        bin_dir = self.home / "bin"
        relay = self.home / "relay/relay.py"
        template = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
        built = {}
        for python in ("python", "python3"):
            with mock.patch.object(install_module, "hook_python", return_value=python):
                jev = install_module.jev_template(bin_dir)
                settings = install_module.merge_jev_hook(
                    install_module.merge_hooks({}, template, relay), bin_dir, True)
            commands = [h["command"] for groups in settings["hooks"].values()
                        for group in groups for h in group["hooks"]]
            self.assertEqual(len(commands), 6)
            self.assertTrue(all(command.startswith(f"{python} ") for command in commands), commands)
            self.assertEqual(settings["hooks"]["PreToolUse"], jev["hooks"]["PreToolUse"])
            self.assertIn(f"{python} {shlex.quote(str(relay))} stop 2>/dev/null || true", commands)
            built[python] = settings
        for old, new in (("python", "python3"), ("python3", "python")):
            with mock.patch.object(install_module, "hook_python", return_value=new):
                upgraded = install_module.merge_jev_hook(
                    install_module.merge_hooks(built[old], template, relay), bin_dir, True)
            self.assertEqual(upgraded, built[new])

    def test_missing_hook_interpreter_is_warned(self):
        env = {"CLAUDE_HOME": str(self.home)}
        for found, warned in ((None, True), ("/usr/bin/python3", False)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (mock.patch.dict(os.environ, env),
                  mock.patch.object(install_module.shutil, "which", return_value=found),
                  mock.patch.object(install_module.subprocess, "run",
                                    return_value=mock.Mock(returncode=0)),
                  contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
                self.assertEqual(install_module.main(["--dry-run"]), 0)
            self.assertNotIn("did not run a real Python interpreter", stderr.getvalue())
            self.assertEqual(f"`{install_module.hook_python()}` is not on PATH" in stderr.getvalue(),
                             warned, stderr.getvalue())
        self.assertFalse(self.home.exists())

    # os.name drives pathlib's PosixPath/WindowsPath selection, so it cannot be mocked
    # away from the real platform without breaking Path() calls inside main(); each
    # case below only runs on the platform it actually exercises.
    @unittest.skipUnless(os.name == "nt", "Store-stub probe only runs on Windows")
    def test_store_stub_interpreter_is_warned_on_windows(self):
        env = {"CLAUDE_HOME": str(self.home)}
        cases = (
            (mock.Mock(returncode=0), False),  # probe succeeds
            (mock.Mock(returncode=9009), True),  # Store alias prints a hint and exits 9009
            (OSError("no such file"), True),
            (subprocess.TimeoutExpired(cmd="python", timeout=10), True),
        )
        for run_effect, warned in cases:
            stdout, stderr = io.StringIO(), io.StringIO()
            run_kwargs = {"side_effect": run_effect} if isinstance(run_effect, Exception) \
                else {"return_value": run_effect}
            with (mock.patch.dict(os.environ, env),
                  mock.patch.object(install_module.shutil, "which", return_value="python"),
                  mock.patch.object(install_module.subprocess, "run", **run_kwargs) as run,
                  contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
                self.assertEqual(install_module.main(["--dry-run"]), 0)
            self.assertTrue(run.called)
            self.assertEqual("did not run a real Python interpreter" in stderr.getvalue(),
                             warned, stderr.getvalue())
        self.assertFalse(self.home.exists())

    @unittest.skipUnless(os.name == "posix", "the found-interpreter probe only runs on Windows")
    def test_store_stub_probe_skipped_off_windows(self):
        env = {"CLAUDE_HOME": str(self.home)}
        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch.dict(os.environ, env),
              mock.patch.object(install_module.shutil, "which", return_value="/usr/bin/python3"),
              mock.patch.object(install_module.subprocess, "run") as run,
              contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            self.assertEqual(install_module.main(["--dry-run"]), 0)
        self.assertFalse(run.called)
        self.assertNotIn("did not run a real Python interpreter", stderr.getvalue())
        self.assertFalse(self.home.exists())

    def test_merge_jev_hook_strips_owned_hooks_from_user_and_duplicate_groups(self):
        bin_dir = self.home / "bin"
        template = install_module.jev_template(bin_dir)["hooks"]
        owned = template["PreToolUse"][0]
        owned_hook = owned["hooks"][0]
        user_hook = {"type": "command", "command": "audit-agent"}
        settings = {"hooks": {"PreToolUse": [
            {"matcher": "Agent", "hooks": [user_hook, owned_hook]}, owned, owned],
            "PostToolUse": template["PostToolUse"] * 2}}
        removed = install_module.merge_jev_hook(settings, bin_dir, False)
        self.assertEqual(removed["hooks"]["PreToolUse"], [{"matcher": "Agent", "hooks": [user_hook]}])
        self.assertNotIn("PostToolUse", removed["hooks"])
        added = install_module.merge_jev_hook(settings, bin_dir, True)
        self.assertEqual(added["hooks"]["PreToolUse"],
                         [{"matcher": "Agent", "hooks": [user_hook]}] + template["PreToolUse"])
        self.assertEqual(added["hooks"]["PostToolUse"], template["PostToolUse"])
        only_ours = install_module.merge_jev_hook({"hooks": {"PreToolUse": [owned, owned]}}, bin_dir, False)
        self.assertNotIn("PreToolUse", only_ours["hooks"])

    def test_merge_jev_hook_replaces_gate_installed_with_old_bash_matcher(self):
        bin_dir = self.home / "bin"
        template = install_module.jev_template(bin_dir)["hooks"]
        gate = next(g for g in template["PreToolUse"] if g["hooks"][0]["command"].endswith(
            "gate 2>/dev/null || true"))
        self.assertEqual(gate["matcher"], "Bash|PowerShell")
        old_gate = {**gate, "matcher": "Bash"}
        user = {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit-bash"}]}
        settings = {"hooks": {"PreToolUse": [user, old_gate]}}
        upgraded = install_module.merge_jev_hook(settings, bin_dir, True)
        self.assertEqual(upgraded["hooks"]["PreToolUse"], [user] + template["PreToolUse"])
        self.assertNotIn(old_gate, upgraded["hooks"]["PreToolUse"])
        removed = install_module.merge_jev_hook(settings, bin_dir, False)
        self.assertEqual(removed["hooks"]["PreToolUse"], [user])

    def test_default_install_adds_no_pre_tool_use_hook(self):
        self.assertEqual(self.run_install().returncode, 0)
        settings = json.loads(self.home.joinpath("settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("PreToolUse", settings["hooks"])
        self.assertNotIn("PostToolUse", settings["hooks"])

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
        self.assertIn(f"{install_module.hook_python()} {shlex.quote(str(relay))} handoff", instructions)
        self.assertNotIn("__RELAY__", instructions)
        self.assertNotIn("__PR_STATUS__", instructions)
        self.assertNotIn("__PYTHON__", instructions)
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

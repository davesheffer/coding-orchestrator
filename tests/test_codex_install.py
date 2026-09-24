import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "codex" / "install.py"
SOURCE_AGENTS = (ROOT / "codex" / "AGENTS.md").read_bytes()


class CodexInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.home = Path(self.temp.name) / "home"

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self, *args):
        env = os.environ | {"CODEX_HOME": str(self.home)}
        return subprocess.run([sys.executable, str(INSTALL), *args], env=env, text=True, capture_output=True)

    def test_fresh_install_parses_and_installs_helper(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "bin/agent-report.py").is_file())
        self.assertEqual((self.home / "bin/rollover-open.py").read_bytes(),
                         (ROOT / "bin/rollover-open.py").read_bytes())
        import tomllib
        config = tomllib.loads((self.home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "gpt-6-sol")
        self.assertEqual(config["model_reasoning_effort"], "medium")
        self.assertTrue(config["agents"]["enabled"])
        self.assertEqual(config["agents"]["max_concurrent_threads_per_session"], 6)
        self.assertEqual(config["agents"]["default_subagent_model"], "gpt-6-luna")
        self.assertEqual(config["agents"]["default_subagent_reasoning_effort"], "low")
        for role in ("scout", "runner", "builder", "critic"):
            self.assertEqual(tomllib.loads((self.home / "agents" / f"{role}.toml").read_text(encoding="utf-8"))["name"], role)
        builder = tomllib.loads((self.home / "agents" / "builder.toml").read_text(encoding="utf-8"))
        boundary = builder["sandbox_workspace_write"]
        self.assertEqual(builder["sandbox_mode"], "workspace-write")
        self.assertEqual(boundary["writable_roots"], [])
        self.assertFalse(boundary["network_access"])
        self.assertTrue(boundary["exclude_slash_tmp"])
        self.assertTrue(boundary["exclude_tmpdir_env_var"])
        runner = tomllib.loads((self.home / "agents" / "runner.toml").read_text(encoding="utf-8"))
        self.assertEqual(runner["sandbox_mode"], "workspace-write")
        self.assertFalse(runner["sandbox_workspace_write"]["network_access"])
        self.assertEqual(set(runner["features"]), {
            "apps", "plugins", "remote_plugin", "browser_use", "browser_use_external",
            "browser_use_full_cdp_access", "in_app_browser", "computer_use",
            "image_generation", "multi_agent", "multi_agent_v2",
            "skill_mcp_dependency_install", "hooks",
        })
        self.assertFalse(runner["agents"]["enabled"])
        critic = tomllib.loads((self.home / "agents" / "critic.toml").read_text(encoding="utf-8"))
        self.assertEqual(critic["sandbox_mode"], "read-only")
        self.assertFalse(critic["sandbox_workspace_write"]["network_access"])
        self.assertEqual(critic["approval_policy"], "never")
        self.assertEqual(critic["web_search"], "disabled")
        self.assertFalse(critic["features"]["apps"])
        self.assertFalse(critic["agents"]["enabled"])
        self.assertTrue(all(server["enabled"] is False for server in critic["mcp_servers"].values()))
        self.assertNotIn("default_permissions", config)
        self.assertEqual(list(self.home.glob("*.config.toml")), [])
        installed_instructions = (self.home / "AGENTS.md").read_bytes()
        self.assertIn(b'python "$env:USERPROFILE/.codex/bin/pr-status"', installed_instructions)
        self.assertNotIn(b"<!-- CODEX-CRITIC-NETWORK-FALLBACK:START -->", installed_instructions)
        helper = self.home / "bin" / "pr-status"
        self.assertEqual(helper.read_bytes(), (ROOT / "bin" / "pr-status").read_bytes())
        self.assertEqual((self.home / "bin" / "agent-run.py").read_bytes(),
                         (ROOT / "bin" / "agent-run.py").read_bytes())
        self.assertEqual((self.home / "agent-routing.json").read_bytes(),
                         b'{"network_fallback_roles": []}\n')
        if os.name == "posix":
            self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        self.assertFalse((self.home.parent / ".claude").exists())

    def test_previous_release_upgrade_requires_force_then_preserves_backups(self):
        fixture = ROOT / "tests/fixtures/previous-release/codex/agents"
        shutil.copytree(fixture, self.home / "agents")
        originals = {p.name: p.read_bytes() for p in fixture.glob("*.toml")}
        before = self.snapshot()
        refused = self.run_install()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("use --force", refused.stderr)
        self.assertEqual(self.snapshot(), before)
        dry = self.run_install("--force", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.snapshot(), before)
        upgraded = self.run_install("--force")
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        for name, data in originals.items():
            self.assertEqual((self.home / "agents" / (name + ".bak")).read_bytes(), data)
            self.assertEqual((self.home / "agents" / name).read_bytes(),
                             (ROOT / "codex/agents" / name).read_bytes())
        before = self.snapshot()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_upgrade_preserves_local_critic_choice_without_enabling_fallback(self):
        import tomllib
        base_home = self.home
        for decision in ("allow-after-isolation-failure", "deny"):
            with self.subTest(decision=decision):
                self.home = base_home / decision
                shutil.copytree(ROOT / "tests/fixtures/previous-release/codex/agents",
                                self.home / "agents")
                preference = ("\r\n<!-- CODEX-CRITIC-NETWORK-FALLBACK:START -->\r\n"
                              f"decision: {decision}\r\n"
                              "<!-- CODEX-CRITIC-NETWORK-FALLBACK:END -->\r\n").encode()
                original = (b"private instructions\r\n<!-- CODEX-ORCHESTRATOR:START -->\n"
                            b"old rules\n<!-- CODEX-ORCHESTRATOR:END -->" + preference)
                (self.home / "AGENTS.md").write_bytes(original)
                config = b'model = "local-model"\n'
                (self.home / "config.toml").write_bytes(config)
                profile = self.home / "critic-network-fallback.config.toml"
                profile_bytes = (b'default_permissions = "local-critic"\n'
                                 b'[permissions.local-critic]\nextends = ":read-only"\n'
                                 b'[permissions.local-critic.network]\nenabled = true\n')
                profile.write_bytes(profile_bytes)
                before = self.snapshot()
                dry = self.run_install("--force", "--dry-run")
                self.assertEqual(dry.returncode, 0, dry.stderr)
                self.assertEqual(self.snapshot(), before)
                installed = self.run_install("--force")
                self.assertEqual(installed.returncode, 0, installed.stderr)
                actual = (self.home / "AGENTS.md").read_bytes()
                self.assertTrue(actual.startswith(b"private instructions\r\n"))
                self.assertTrue(actual.endswith(preference))
                self.assertEqual(actual.count(b"<!-- CODEX-CRITIC-NETWORK-FALLBACK:START -->"), 1)
                self.assertEqual((self.home / "AGENTS.md.bak").read_bytes(), original)
                self.assertEqual((self.home / "config.toml").read_bytes(), config)
                self.assertEqual(profile.read_bytes(), profile_bytes)
                critic = tomllib.loads((self.home / "agents/critic.toml").read_text(encoding="utf-8"))
                self.assertEqual(critic["sandbox_mode"], "read-only")
                self.assertFalse(critic["sandbox_workspace_write"]["network_access"])
                before = self.snapshot()
                repeat = self.run_install()
                self.assertEqual(repeat.returncode, 0, repeat.stderr)
                self.assertEqual(self.snapshot(), before)

    @unittest.skipUnless(os.name == "posix", "shell entry points require POSIX process execution")
    def test_actual_shell_entry_point_runs(self):
        result = subprocess.run([str(ROOT / "codex/install.sh"), "--dry-run"],
                                env=os.environ | {"CODEX_HOME": str(self.home)},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.home.exists())

    def test_force_still_rejects_unsafe_incoming_role(self):
        bundle = Path(self.temp.name) / "bundle"
        shutil.copytree(ROOT / "codex", bundle / "codex")
        shutil.copytree(ROOT / "bin", bundle / "bin")
        source = bundle / "codex/agents/scout.toml"
        source.write_text(source.read_text(encoding="utf-8").replace("apps = false", "apps = true"), encoding="utf-8")
        result = subprocess.run([sys.executable, str(bundle / "codex/install.py"), "--force"],
                                env=os.environ | {"CODEX_HOME": str(self.home)},
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("role features", result.stderr)
        self.assertFalse(self.home.exists())

    def test_repeat_is_stable_and_merges_existing_instructions(self):
        self.home.mkdir(parents=True)
        prefix, suffix = b"before\n", b"after\n"
        self.home.joinpath("AGENTS.md").write_bytes(prefix + SOURCE_AGENTS + suffix)
        self.home.joinpath("config.toml").write_text("model = 'local'\n")
        self.assertEqual(self.run_install().returncode, 0)
        first = self.home.joinpath("AGENTS.md").read_bytes()
        self.assertEqual(first[:len(prefix)], prefix)
        self.assertEqual(first[-len(suffix):], suffix)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.home.joinpath("AGENTS.md").read_bytes(), first)
        self.assertEqual(list(self.home.glob("*.bak*")), [])
        self.assertEqual(self.home.joinpath("config.toml").read_text(encoding="utf-8"), "model = 'local'\n")

    def test_append_conflicts_backups_and_dry_run(self):
        self.home.mkdir(parents=True)
        self.home.joinpath("AGENTS.md").write_text("local\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertIn(b"<!-- CODEX-ORCHESTRATOR:START -->", self.home.joinpath("AGENTS.md").read_bytes())
        self.assertIn(b"<!-- CODEX-ORCHESTRATOR:END -->", self.home.joinpath("AGENTS.md").read_bytes())
        role = self.home / "agents" / "scout.toml"
        role.write_bytes((ROOT / "codex" / "agents" / "scout.toml").read_bytes() + b"# local change\n")
        before = role.read_bytes()
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(role.read_bytes(), before)
        dry = self.run_install("--force", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(role.read_bytes(), before)
        forced = self.run_install("--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(role.with_name("scout.toml.bak").read_bytes(), before)

    def test_refuses_malformed_markers_toml_and_symlinks_before_writes(self):
        self.home.mkdir(parents=True)
        self.home.joinpath("AGENTS.md").write_text("<!-- CODEX-ORCHESTRATOR:START -->\n")
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertFalse((self.home / "agents").exists())
        self.home.joinpath("AGENTS.md").unlink()
        self.home.joinpath("config.toml").write_text("[broken\n")
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertFalse((self.home / "agents").exists())
        self.home.joinpath("config.toml").unlink()
        self.home.rmdir()
        target = Path(self.temp.name) / "target"
        target.mkdir()
        self.home.symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_override_is_untouched_and_warned(self):
        self.home.mkdir(parents=True)
        override = self.home / "AGENTS.override.md"
        override.write_text("local override\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shadows global AGENTS", result.stderr)
        self.assertEqual(override.read_text(encoding="utf-8"), "local override\n")

    def snapshot(self):
        return {
            str(p.relative_to(self.home)): (
                ("link", os.readlink(p)) if p.is_symlink()
                else ("file", p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            )
            for p in self.home.rglob("*") if p.is_file() or p.is_symlink()
        }

    def test_fresh_dry_run_creates_nothing(self):
        result = self.run_install("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("write", result.stdout)
        self.assertFalse(self.home.exists())

    def test_replaces_owned_block_with_backup_and_preserves_surroundings(self):
        self.home.mkdir()
        original = (b"private prefix\r\n<!-- CODEX-ORCHESTRATOR:START -->\nold\n"
                    b"<!-- CODEX-ORCHESTRATOR:END -->\r\nprivate suffix")
        self.home.joinpath("AGENTS.md").write_bytes(original)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = self.home.joinpath("AGENTS.md").read_bytes()
        self.assertTrue(actual.startswith(b"private prefix\r\n"))
        self.assertTrue(actual.endswith(b"\r\nprivate suffix"))
        self.assertIn(SOURCE_AGENTS.rstrip(b"\n"), actual)
        self.assertEqual(self.home.joinpath("AGENTS.md.bak").read_bytes(), original)
        before = self.snapshot()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_marker_errors_make_no_changes_even_with_force(self):
        self.home.mkdir()
        start = b"<!-- CODEX-ORCHESTRATOR:START -->"
        end = b"<!-- CODEX-ORCHESTRATOR:END -->"
        for malformed in (start, end, end + start, start + start + end, start + end + end):
            with self.subTest(markers=malformed):
                self.home.joinpath("AGENTS.md").write_bytes(malformed)
                before = self.snapshot()
                self.assertNotEqual(self.run_install("--force").returncode, 0)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.home.joinpath("agents").exists())

    def test_invalid_role_toml_is_not_overwritten_by_force(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.home.joinpath("agents/scout.toml").write_text("[broken\n")
        before = self.snapshot()
        self.assertNotEqual(self.run_install("--force").returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_each_managed_file_symlink_is_refused(self):
        self.home.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.write_text("do not touch")
        for relative in ("AGENTS.md", "config.toml", "agents/scout.toml", "bin/pr-status", "bin/agent-run.py", "agent-routing.json"):
            with self.subTest(path=relative):
                target = self.home / relative
                target.parent.mkdir(exist_ok=True)
                target.symlink_to(outside)
                before = self.snapshot()
                result = self.run_install("--force")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(outside.read_text(encoding="utf-8"), "do not touch")
                target.unlink()

    def test_configure_routing_adds_only_missing_defaults(self):
        self.home.mkdir()
        original = (b'model = "local"\n[agents]\nenabled = false\n'
                    b'default_subagent_model = "custom"\n[agents.custom]\nname = "kept"\n')
        self.home.joinpath("config.toml").write_bytes(original)
        dry = self.run_install("--configure-routing", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.home.joinpath("config.toml").read_bytes(), original)
        result = self.run_install("--configure-routing")
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = self.home.joinpath("config.toml").read_bytes()
        self.assertIn(b'default_subagent_model = "custom"', updated)
        self.assertIn(b'default_subagent_reasoning_effort = "low"', updated)
        self.assertIn(b'[agents.custom]\nname = "kept"', updated)
        self.assertEqual(self.home.joinpath("config.toml.bak").read_bytes(), original)
        before = self.snapshot()
        self.assertEqual(self.run_install("--configure-routing").returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_configure_routing_handles_implicit_agents_parent(self):
        self.home.mkdir()
        self.home.joinpath("config.toml").write_bytes(b'[agents.custom]\nmodel = "custom"\n')
        result = self.run_install("--configure-routing")
        self.assertEqual(result.returncode, 0, result.stderr)
        import tomllib
        config = tomllib.loads(self.home.joinpath("config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["agents"]["custom"]["model"], "custom")
        self.assertEqual(config["agents"]["default_subagent_model"], "gpt-6-luna")

    def test_configure_routing_preserves_eof_and_line_endings(self):
        import tomllib
        base = self.home
        for index, original in enumerate((b'model="local"', b'[agents]',
                                          b'model="local"\r\n[agents]\r\nenabled=false\r\n',
                                          b'[agents.reviewer]\nmodel="custom"\n')):
            with self.subTest(original=original):
                self.home = base / str(index)
                self.home.mkdir(parents=True)
                (self.home / 'config.toml').write_bytes(original)
                result = self.run_install('--configure-routing')
                self.assertEqual(result.returncode, 0, result.stderr)
                updated = (self.home / 'config.toml').read_bytes()
                before = tomllib.loads(original.decode())
                expected = dict(before)
                expected['agents'] = dict(before.get('agents', {}))
                expected['agents'].update(default_subagent_model='gpt-6-luna', default_subagent_reasoning_effort='low')
                self.assertEqual(tomllib.loads(updated.decode()), expected)
                if b'\r\n' in original:
                    self.assertNotIn(b'\n', updated.replace(b'\r\n', b''))

    def test_configure_routing_refuses_unsafe_syntax_without_changes(self):
        self.home.mkdir()
        config = self.home / 'config.toml'
        for original in (b'agents = {enabled = true}\n', b'["agents"]\nenabled=true\n',
                         b'agents.enabled=true\n'):
            with self.subTest(original=original):
                config.write_bytes(original)
                before = self.snapshot()
                result = self.run_install('--configure-routing', '--force')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_configure_routing_preserves_complete_inline_choices(self):
        self.home.mkdir()
        original = b'agents = {default_subagent_model="custom", default_subagent_reasoning_effort="high"}\n'
        (self.home / 'config.toml').write_bytes(original)
        result = self.run_install('--configure-routing')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'config.toml').read_bytes(), original)

    def test_invalid_routing_policy_is_never_mutated(self):
        self.home.mkdir()
        policy = self.home / "agent-routing.json"
        for invalid in (b"not json", b"[]", b"{}", b'{"network_fallback_roles": "scout"}',
                        b'{"network_fallback_roles": ["scout", 1]}',
                        b'{"network_fallback_roles": ["unknown"]}',
                        b'{"network_fallback_roles": ["scout", "scout"]}'):
            with self.subTest(policy=invalid):
                policy.write_bytes(invalid)
                before = self.snapshot()
                result = self.run_install("--force")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_existing_routing_approval_is_preserved(self):
        self.home.mkdir()
        policy = self.home / "agent-routing.json"
        approved = b'{"network_fallback_roles": ["scout"]}\n'
        policy.write_bytes(approved)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(policy.read_bytes(), approved)

    def test_forced_upgrade_preserves_policy_approval_and_refusal(self):
        self.assertEqual(self.run_install().returncode, 0)
        policy = self.home / 'agent-routing.json'
        for original in (b'{"network_fallback_roles": [], "note":"refused"}\n',
                         b'{"network_fallback_roles": ["scout", "critic"], "note":"approved"}\n'):
            with self.subTest(policy=original):
                policy.write_bytes(original)
                helper = self.home / 'bin/agent-run.py'
                helper.write_bytes(helper.read_bytes() + b'\n# local edit\n')
                result = self.run_install('--force')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(policy.read_bytes(), original)
                before = self.snapshot()
                self.assertEqual(self.run_install().returncode, 0)
                self.assertEqual(self.snapshot(), before)

    def test_internal_directory_and_broken_symlinks_are_refused(self):
        self.home.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        for name in ("agents", "bin"):
            with self.subTest(path=name):
                target = self.home / name
                target.symlink_to(outside, target_is_directory=True)
                self.assertNotEqual(self.run_install("--force").returncode, 0)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse((self.home / "AGENTS.md").exists())
                target.unlink()
        self.home.joinpath("AGENTS.md").symlink_to(outside / "missing")
        self.assertNotEqual(self.run_install("--force").returncode, 0)
        self.assertFalse(self.home.joinpath("agents").exists())

    def test_force_never_overwrites_an_earlier_backup(self):
        self.assertEqual(self.run_install().returncode, 0)
        role = self.home / "agents/scout.toml"
        source = role.read_bytes()
        for index in range(2):
            changed = source + f"# customization {index}\n".encode()
            role.write_bytes(changed)
            self.assertEqual(self.run_install("--force").returncode, 0)
            name = "scout.toml.bak" + (f".{index}" if index else "")
            self.assertEqual(role.with_name(name).read_bytes(), changed)
        self.assertEqual(role.with_name("scout.toml.bak").read_bytes(), source + b"# customization 0\n")

    def test_nested_custom_home_and_unknown_argument(self):
        self.home = self.home / "nested" / ".codex"
        self.assertNotEqual(self.run_install("--unknown").returncode, 0)
        self.assertFalse(self.home.exists())
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.home.joinpath("AGENTS.md").exists())
        self.assertFalse(self.home.joinpath("hooks.json").exists())

    def test_install_without_jev_disables_it_and_says_so(self):
        message = "jev: disabled (was enabled). Re-run with --jev to keep it."
        config = self.home / "jev" / "config.json"
        on = self.run_install("--jev")
        self.assertEqual(on.returncode, 0, on.stderr)
        self.assertNotIn(message, on.stdout)
        self.assertIs(json.loads(config.read_text(encoding="utf-8"))["jev"]["enabled"], True)
        dry = self.run_install("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(message, dry.stdout)
        self.assertIs(json.loads(config.read_text(encoding="utf-8"))["jev"]["enabled"], True)
        off = self.run_install()
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertIn(message, off.stdout)
        self.assertIs(json.loads(config.read_text(encoding="utf-8"))["jev"]["enabled"], False)
        self.assertEqual(json.loads(self.home.joinpath("hooks.json").read_text(encoding="utf-8"))["hooks"], {})
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn(message, again.stdout)


if __name__ == "__main__":
    unittest.main()

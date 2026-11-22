import importlib.util
import json
import os
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

    def test_blank_line_after_marker_does_not_count(self):
        self.assertIn("RISK", guard.decide(spawn("RISK:\n\nx", subagent_type="critic", model="fable")))
        self.assertIn("ROLE-EXCEPTION",
                      guard.decide(spawn("ROLE-EXCEPTION:\n\nx", subagent_type="general-purpose")))

    def test_non_ascii_prompt_under_cp1252_still_denied(self):
        prompt = "שלום “quote”"
        payload = json.dumps(spawn(prompt, subagent_type="claude"), ensure_ascii=False).encode("utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        result = subprocess.run([sys.executable, str(GUARD)], input=payload, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_task_tool_name_denied(self):
        self.assertIn("ROLE-EXCEPTION", guard.decide(spawn(tool="Task", subagent_type="claude")))

    def test_non_dict_payload_silent(self):
        result = subprocess.run([sys.executable, str(GUARD)], input=b"[1, 2]", capture_output=True)
        self.assertEqual((result.returncode, result.stdout), (0, b""))

    def test_project_critic_takes_precedence(self):
        home_fable = self.home_with_critic("fable")
        home_opus = self.home_with_critic("opus")
        for project_model, home, denied in (("fable", home_opus, True), ("opus", home_fable, False)):
            with self.subTest(project=project_model):
                proj = tempfile.TemporaryDirectory()
                self.addCleanup(proj.cleanup)
                agents = Path(proj.name) / ".claude" / "agents"
                agents.mkdir(parents=True)
                (agents / "critic.md").write_text(f"---\nname: critic\nmodel: {project_model}\n---\n", encoding="utf-8")
                payload = {**spawn(subagent_type="critic"), "cwd": proj.name}
                self.assertEqual(guard.decide(payload, home=home) is not None, denied)

    def test_model_line_in_body_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "agents").mkdir()
        (Path(tmp.name) / "agents" / "critic.md").write_text(
            "---\nname: critic\nmodel:\n---\nmodel: fable\n", encoding="utf-8")
        self.assertIsNone(guard.decide(spawn(subagent_type="critic"), home=Path(tmp.name)))

    def write_agent(self, base, text, raw=None):
        agents = Path(base) / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "critic.md").write_bytes(raw if raw is not None else text.encode("utf-8"))

    def test_bom_frontmatter_parsed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.write_agent(tmp.name, "", raw=b"\xef\xbb\xbf---\nmodel: fable\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=Path(tmp.name)), "fable")

    def test_empty_frontmatter_ignores_body_model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.write_agent(tmp.name, "---\n---\nmodel: fable\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=Path(tmp.name)), "")

    def test_unclosed_block_alone_is_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.write_agent(tmp.name, "---\nmodel: fable\n")
        self.assertEqual(guard.frontmatter_model("critic", home=Path(tmp.name)), "")

    def test_project_file_without_frontmatter_falls_through(self):
        home = self.home_with_critic("fable")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "just text")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")

    def test_project_file_unclosed_block_falls_through(self):
        home = self.home_with_critic("fable")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "---\nname: critic\nmodel: opus\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")

    def test_walk_up_stops_at_git_root(self):
        home = self.home_with_critic("opus")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.write_agent(Path(tmp.name) / ".claude", "---\nmodel: fable\n---\n")
        repo = Path(tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(repo / "sub")), "opus")

    def test_undecodable_parent_file_falls_through(self):
        home = self.home_with_critic("fable")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        self.write_agent(repo / ".claude", "", raw=b"---\nname: caf\xe9\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(repo / "sub")), "fable")

    def test_undecodable_parent_file_still_denies_fable(self):
        home = self.home_with_critic("fable")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        self.write_agent(repo / ".claude", "", raw=b"\xff\xfe-\x00-\x00")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(repo)), "fable")

    def test_undecodable_project_fable_denies_over_opus_home(self):
        home = self.home_with_critic("opus")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "",
                         raw=b"---\nname: critic\ndescription: caf\xe9\nmodel: fable\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")
        reason = guard.decide({"tool_name": "Agent", "cwd": proj.name, "tool_input": {
            "subagent_type": "critic", "prompt": "review"}}, home=home)
        self.assertEqual(reason, guard.FABLE_REASON)

    def test_bad_header_project_fable_denies_over_opus_home(self):
        home = self.home_with_critic("opus")
        for text in ("\n---\nname: critic\nmodel: fable\n---\n", "---\nname: critic\nmodel: fable\n"):
            with self.subTest(text=text):
                proj = tempfile.TemporaryDirectory()
                self.addCleanup(proj.cleanup)
                self.write_agent(Path(proj.name) / ".claude", text)
                self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")

    def test_bad_header_project_without_fable_falls_through(self):
        home = self.home_with_critic("opus")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "\n---\nname: critic\nmodel: sonnet\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "opus")

    def test_directory_named_like_file_falls_through(self):
        home = self.home_with_critic("fable")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        (Path(proj.name) / ".claude" / "agents" / "critic.md").mkdir(parents=True)
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")

    def test_git_in_cwd_stops_walk(self):
        home = self.home_with_critic("opus")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.write_agent(Path(tmp.name) / ".claude", "---\nmodel: fable\n---\n")
        repo = Path(tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(repo)), "opus")

    def test_project_file_without_model_shadows_home(self):
        home = self.home_with_critic("fable")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "---\nname: critic\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "")

    def test_project_file_without_name_does_not_shadow_home(self):
        home = self.home_with_critic("fable")
        for text in ("---\ndescription: no name here\n---\nbody", "---\nname: other\nmodel: opus\n---\n"):
            with self.subTest(text=text):
                proj = tempfile.TemporaryDirectory()
                self.addCleanup(proj.cleanup)
                self.write_agent(Path(proj.name) / ".claude", text)
                self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")
                payload = {**spawn(subagent_type="critic"), "cwd": proj.name}
                self.assertIn("RISK", guard.decide(payload, home=home))

    def test_nameless_project_fable_still_denies(self):
        home = self.home_with_critic("opus")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "---\nmodel: fable\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")

    def test_quoted_matching_name_shadows_home(self):
        home = self.home_with_critic("fable")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", '---\nname: "critic"\nmodel: opus\n---\n')
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "opus")

    def test_subdirectory_cwd_walks_up_to_project_file(self):
        home = self.home_with_critic("opus")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "---\nmodel: fable\n---\n")
        (Path(proj.name) / ".git").mkdir()
        sub = Path(proj.name) / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(sub)), "fable")

    def test_home_claude_dir_is_not_a_project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "h" / ".claude"
        self.write_agent(home, "---\nmodel: opus\n---\n")
        self.write_agent(Path(tmp.name) / ".claude", "---\nmodel: fable\n---\n")
        (Path(tmp.name) / ".git").mkdir()
        sub = Path(tmp.name) / "h" / "work"
        sub.mkdir()
        # with the skip, the home dir is not a project, so the file above it decides
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(sub)), "fable")

    def test_multiline_model_fable_denies(self):
        home = self.home_with_critic("opus")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "---\nname: critic\nmodel:\n  fable\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")

    def test_quoted_model_key_fable_denies(self):
        home = self.home_with_critic("opus")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", '---\nname: critic\n"model": fable\n---\n')
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "fable")
        reason = guard.decide({"tool_name": "Agent", "cwd": proj.name, "tool_input": {
            "subagent_type": "critic", "prompt": "review"}}, home=home)
        self.assertEqual(reason, guard.FABLE_REASON)

    def test_home_multiline_fable_denies(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.write_agent(home, "---\nname: critic\nmodel:\n  Fable\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home), "fable")

    def test_no_git_ancestor_does_not_shadow_home(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "home"
        self.write_agent(home, "---\nname: critic\nmodel: fable\n---\n")
        self.write_agent(Path(tmp.name) / "outer" / ".claude", "---\nname: critic\nmodel: opus\n---\n")
        sub = Path(tmp.name) / "outer" / "work"
        sub.mkdir(parents=True)
        # no .git anywhere: only the cwd counts, so the ancestor opus file is ignored
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=str(sub)), "fable")

    def test_no_git_cwd_file_still_applies(self):
        home = self.home_with_critic("fable")
        proj = tempfile.TemporaryDirectory()
        self.addCleanup(proj.cleanup)
        self.write_agent(Path(proj.name) / ".claude", "---\nname: critic\nmodel: opus\n---\n")
        self.assertEqual(guard.frontmatter_model("critic", home=home, cwd=proj.name), "opus")


if __name__ == "__main__":
    unittest.main()

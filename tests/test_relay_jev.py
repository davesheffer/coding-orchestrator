import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay/relay.py"
SPEC = importlib.util.spec_from_file_location("relay_jev_module", RELAY)
relay_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_module)


def noul_response(name, value):
    return {"answers": {name: {"type": "noul", "noul": value}}}


def score_response(score, confidence=0.8, next_step=0.9, verified=0.9):
    return {"answers": {
        "actionable": {"type": "score", "score": score, "confidence": confidence},
        "next_step_concrete": {"type": "noul", "noul": next_step},
        "verified_backed": {"type": "noul", "noul": verified},
    }}


class RelayJevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.home = Path(self.temp.name) / "claude"
        self.root = self.home / "relay"
        self.handoffs = self.root / "handoffs"
        self.state = self.root / "state"
        self.log_path = self.root / "jev-log.jsonl"
        env_patch = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(self._reset_classify)
        patches = [
            mock.patch.object(relay_module, "ROOT", self.root),
            mock.patch.object(relay_module, "HANDOFFS", self.handoffs),
            mock.patch.object(relay_module, "STATE", self.state),
            mock.patch.object(relay_module.jev_client, "CONFIG_PATH", self.root / "config.json"),
            mock.patch.object(relay_module.jev_client, "LOG_PATH", self.log_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "config.json").write_text(json.dumps({"jev": {"enabled": True}}), encoding="utf-8")

    def _reset_classify(self):
        relay_module.JEV_CLASSIFY = None

    def run_prompt(self, prompt, session_id="s1", transcript_tokens=None, transcript_path=None):
        if transcript_path is None and transcript_tokens is not None:
            transcript_path = self.transcript(transcript_tokens)
        payload = {"prompt": prompt, "session_id": session_id,
                   "transcript_path": str(transcript_path) if transcript_path else ""}
        out = io.StringIO()
        with mock.patch.object(relay_module.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(out):
            relay_module.cmd_prompt()
        return out.getvalue()

    def transcript(self, tokens):
        path = Path(self.temp.name) / f"transcript-{tokens}.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "isSidechain": False,
            "message": {"usage": {"input_tokens": tokens,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}
        }) + "\n")
        return path

    def seed_state(self, session_id, **kv):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"{session_id}.json").write_text(json.dumps(kv), encoding="utf-8")

    def read_state(self, session_id):
        return json.loads((self.state / f"{session_id}.json").read_text(encoding="utf-8"))

    def log_entries(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    # ---- shift detection ----

    def test_low_p_produces_strong_shift_text(self):
        self.seed_state("s1", recent_prompts=["do task A"])
        relay_module.JEV_CLASSIFY = lambda body, key: noul_response("continues", 0.1)
        context = self.run_prompt("unrelated new task", session_id="s1", transcript_tokens=40000)
        additional = json.loads(context)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TASK SHIFT DETECTED", additional)
        self.assertIn("jev p=0.10", additional)
        self.assertIn("Do not do it here", additional)
        entries = self.log_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["feature"], "shift")
        self.assertEqual(entries[0]["decision"], "shift")
        self.assertNotIn("prompt", json.dumps(entries[0]))
        self.assertNotIn("unrelated new task", json.dumps(entries[0]))

    def test_high_p_drops_shift_sentence(self):
        self.seed_state("s1", recent_prompts=["do task A"])
        relay_module.JEV_CLASSIFY = lambda body, key: noul_response("continues", 0.9)
        context = self.run_prompt("continue task A please", session_id="s1", transcript_tokens=40000)
        additional = json.loads(context)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("TASK-SHIFT RULE", additional)
        self.assertNotIn("TASK SHIFT DETECTED", additional)
        entries = self.log_entries()
        self.assertEqual(entries[0]["decision"], "continue")

    def test_mid_confidence_leaves_default_sentence(self):
        self.seed_state("s1", recent_prompts=["do task A"])
        relay_module.JEV_CLASSIFY = lambda body, key: noul_response("continues", 0.5)
        context = self.run_prompt("maybe related", session_id="s1", transcript_tokens=40000)
        additional = json.loads(context)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TASK-SHIFT RULE", additional)
        entries = self.log_entries()
        self.assertEqual(entries[0]["decision"], "unsure")

    def test_none_answer_leaves_default_sentence(self):
        self.seed_state("s1", recent_prompts=["do task A"])
        relay_module.JEV_CLASSIFY = lambda body, key: {"answers": {}}
        context = self.run_prompt("something", session_id="s1", transcript_tokens=40000)
        additional = json.loads(context)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TASK-SHIFT RULE", additional)
        entries = self.log_entries()
        self.assertEqual(entries[0]["decision"], "unsure")
        self.assertIsNone(entries[0]["p"])

    def test_below_threshold_skips_jev_but_records_prompt(self):
        calls = []
        relay_module.JEV_CLASSIFY = lambda body, key: calls.append(1) or noul_response("continues", 0.1)
        result = self.run_prompt("small prompt", session_id="s1", transcript_tokens=1000)
        self.assertEqual(result, "")
        self.assertEqual(calls, [])
        self.assertEqual(self.read_state("s1")["recent_prompts"], ["small prompt"])
        self.assertEqual(self.log_entries(), [])

    def test_red_zone_skips_jev_and_leaves_output_unchanged(self):
        calls = []
        relay_module.JEV_CLASSIFY = lambda body, key: calls.append(1) or noul_response("continues", 0.1)
        context = self.run_prompt("do something", session_id="s1", transcript_tokens=300000)
        additional = json.loads(context)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ROLL OVER NOW", additional)
        self.assertEqual(calls, [])
        self.assertEqual(self.log_entries(), [])

    def test_relay_prompt_stores_handoff_goal(self):
        self.handoffs.mkdir(parents=True)
        body = "GOAL: continue the exact task\nSTATE: ready\nNEXT STEP: run checks"
        (self.handoffs / "1234abcd.md").write_text(body, encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(relay_module.sys, "stdin",
                                io.StringIO(json.dumps({"prompt": "relay:1234abcd", "session_id": "s2"}))), \
                contextlib.redirect_stdout(out):
            relay_module.cmd_prompt()
        self.assertEqual(self.read_state("s2")["handoff_goal"], "GOAL: continue the exact task")

    def test_no_prompt_text_in_shift_log(self):
        self.seed_state("s1", recent_prompts=["do task A"])
        relay_module.JEV_CLASSIFY = lambda body, key: noul_response("continues", 0.1)
        secret_prompt = "super secret unrelated prompt text xyz123"
        self.run_prompt(secret_prompt, session_id="s1", transcript_tokens=40000)
        raw_log = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn(secret_prompt, raw_log)

    def test_feature_disabled_skips_jev_call(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "config.json").write_text(json.dumps({"jev": {"enabled": True, "features": {"shift": False}}}),
                                                encoding="utf-8")
        self.seed_state("s1", recent_prompts=["do task A"])
        calls = []
        relay_module.JEV_CLASSIFY = lambda body, key: calls.append(1) or noul_response("continues", 0.1)
        context = self.run_prompt("unrelated work", session_id="s1", transcript_tokens=40000)
        additional = json.loads(context)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TASK-SHIFT RULE", additional)
        self.assertEqual(calls, [])
        self.assertEqual(self.log_entries(), [])

    def test_jev_disabled_records_no_prompt_text(self):
        (self.root / "config.json").write_text(json.dumps({"jev": {"enabled": False}}), encoding="utf-8")
        self.run_prompt("MY SECRET PROMPT about payroll", session_id="s9", transcript_tokens=1000)
        path = self.state / "s9.json"
        if path.exists():
            self.assertNotIn("payroll", path.read_text(encoding="utf-8"))
            self.assertNotIn("recent_prompts", self.read_state("s9"))

    def test_shift_disabled_skips_handoff_goal(self):
        (self.root / "config.json").write_text(
            json.dumps({"jev": {"enabled": True, "features": {"shift": False}}}), encoding="utf-8")
        self.handoffs.mkdir(parents=True)
        (self.handoffs / "1234abcd.md").write_text("GOAL: secret goal\nSTATE: x", encoding="utf-8")
        with mock.patch.object(relay_module.sys, "stdin",
                                io.StringIO(json.dumps({"prompt": "relay:1234abcd", "session_id": "s8"}))), \
                contextlib.redirect_stdout(io.StringIO()):
            relay_module.cmd_prompt()
        path = self.state / "s8.json"
        if path.exists():
            self.assertNotIn("handoff_goal", self.read_state("s8"))

    def test_shift_enabled_state_file_is_private(self):
        self.run_prompt("small prompt", session_id="s7", transcript_tokens=1000)
        path = self.state / "s7.json"
        self.assertEqual(self.read_state("s7")["recent_prompts"], ["small prompt"])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        else:
            acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True)
            self.assertNotIn("(I)", acl.stdout)

    def test_stale_tmp_does_not_leak_mode(self):
        self.state.mkdir(parents=True, exist_ok=True)
        stale = self.state / "s6.tmp"
        stale.write_text("{}", encoding="utf-8")
        os.chmod(stale, 0o644)
        relay_module.save_state("s6", {"recent_prompts": ["x"]})
        self.assertEqual(self.read_state("s6"), {"recent_prompts": ["x"]})
        if os.name != "nt":
            self.assertEqual((self.state / "s6.json").stat().st_mode & 0o777, 0o600)
        else:
            acl = subprocess.run(["icacls", str(self.state / "s6.json")],
                                 capture_output=True, text=True, check=True)
            self.assertNotIn("(I)", acl.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows ACL behavior")
    def test_state_is_not_written_when_acl_restriction_fails(self):
        with mock.patch.object(relay_module, "_restrict_windows_state", side_effect=OSError("ACL failed")):
            with self.assertRaises(OSError):
                relay_module.save_state("s10", {"recent_prompts": ["private prompt"]})
        self.assertFalse((self.state / "s10.json").exists())
        self.assertFalse(any(self.state.glob("s10.json.*.tmp")))

    # ---- handoff grading ----

    def run_handoff(self, body, *extra_args):
        self.handoffs.mkdir(parents=True, exist_ok=True)
        out = io.StringIO()
        code = 0
        with mock.patch.object(relay_module.sys, "stdin", io.StringIO(body)), \
                contextlib.redirect_stdout(out):
            try:
                relay_module.cmd_handoff(["--no-open", "--title", "test", *extra_args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue()

    def good_body(self):
        return ("GOAL: continue the exact task\n"
                "STATE: ready, files updated\n"
                "NEXT STEP: run `python3 -m unittest` and check exit 0\n"
                "VERIFIED: ran tests, exit 0")

    def test_weak_handoff_exits_3_and_does_not_save(self):
        relay_module.JEV_CLASSIFY = lambda body, key: score_response(1.0, next_step=0.9, verified=0.9)
        code, out = self.run_handoff("GOAL: vague\nSTATE: unclear\nNEXT STEP: figure it out\n" + "x" * 40)
        self.assertEqual(code, 3)
        self.assertIn("handoff looks weak", out)
        self.assertIn("jev score 1.0/4", out)
        self.assertEqual(list(self.handoffs.glob("*.md")), [])
        entries = self.log_entries()
        self.assertEqual(entries[0]["feature"], "handoff_grade")
        self.assertTrue(entries[0]["weak"])
        self.assertNotIn("accepted_weak", entries[0])

    def test_accept_weak_saves_anyway(self):
        relay_module.JEV_CLASSIFY = lambda body, key: score_response(1.0)
        code, out = self.run_handoff(self.good_body(), "--accept-weak")
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.handoffs.glob("*.md"))), 1)
        entries = self.log_entries()
        self.assertTrue(entries[0]["accepted_weak"])
        self.assertTrue(entries[0]["weak"])

    def test_strong_score_saves(self):
        relay_module.JEV_CLASSIFY = lambda body, key: score_response(3.5)
        code, out = self.run_handoff(self.good_body())
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.handoffs.glob("*.md"))), 1)
        entries = self.log_entries()
        self.assertFalse(entries[0]["weak"])
        self.assertEqual(entries[0]["score"], 3.5)

    def test_jev_none_saves(self):
        relay_module.JEV_CLASSIFY = lambda body, key: (_ for _ in ()).throw(RuntimeError("boom"))
        code, out = self.run_handoff(self.good_body())
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.handoffs.glob("*.md"))), 1)
        self.assertEqual(self.log_entries(), [])

    def test_gaps_report_missing_sections(self):
        relay_module.JEV_CLASSIFY = lambda body, key: score_response(1.0, next_step=0.1, verified=0.1)
        code, out = self.run_handoff("no sections here just prose " + "x" * 40)
        self.assertEqual(code, 3)
        self.assertIn("missing GOAL", out)
        self.assertIn("missing NEXT STEP", out)
        self.assertIn("NEXT STEP is not a concrete action", out)
        self.assertIn("VERIFIED claims do not cite commands/exit codes", out)

    def test_no_body_text_in_handoff_log(self):
        relay_module.JEV_CLASSIFY = lambda body, key: score_response(3.5)
        secret = "GOAL: secret sauce xyz\nSTATE: ok\nNEXT STEP: run tests\nVERIFIED: exit 0"
        self.run_handoff(secret)
        raw_log = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("secret sauce xyz", raw_log)


if __name__ == "__main__":
    unittest.main()

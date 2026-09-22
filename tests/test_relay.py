import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay/relay.py"
SPEC = importlib.util.spec_from_file_location("relay_module", RELAY)
relay_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_module)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.home = Path(self.temp.name) / "claude"
        self.env = os.environ | {"CLAUDE_HOME": str(self.home)}

    def tearDown(self):
        self.temp.cleanup()

    def run_relay(self, *args, input_text=None, cwd=None):
        return subprocess.run([sys.executable, str(RELAY), *args], env=self.env,
                              cwd=cwd, input=input_text, text=True, encoding="utf-8", capture_output=True)

    def transcript(self, tokens):
        path = Path(self.temp.name) / "transcript.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "isSidechain": False,
            "message": {"usage": {"input_tokens": tokens,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 0}}
        }) + "\n")
        return path

    def test_status_reads_latest_main_thread_usage(self):
        result = self.run_relay("status", str(self.transcript(175000)))
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["tokens"], 175000)
        self.assertEqual(status["zone"], "amber")

    def test_prompt_hook_emits_context_and_never_fails_on_bad_input(self):
        payload = json.dumps({"session_id": "abc", "prompt": "continue",
                              "transcript_path": str(self.transcript(175000))})
        result = self.run_relay("prompt", input_text=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("AMBER", output["hookSpecificOutput"]["additionalContext"])
        broken = self.run_relay("prompt", input_text="not-json")
        self.assertEqual(broken.returncode, 0)

    def test_handoff_uses_custom_home(self):
        body = "GOAL: continue the exact task\nSTATE: ready\nNEXT STEP: run the checks"
        result = self.run_relay("handoff", "--no-open", "--title", "test", input_text=body,
                                cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list((self.home / "relay/handoffs").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("GOAL: continue", files[0].read_text(encoding="utf-8"))

    def test_unicode_handoff_round_trips_with_non_utf8_defaults(self):
        self.env.update({"PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii",
                         "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0"})
        body = "GOAL: preserve \u05e9\u05dc\u05d5\u05dd \u4e2d\u6587 \U0001f680\nSTATE: ready\nNEXT STEP: continue"
        result = self.run_relay("handoff", "--no-open", input_text=body, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list((self.home / "relay/handoffs").glob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn(body, files[0].read_text(encoding="utf-8"))
        prompt = self.run_relay("prompt", input_text=json.dumps({"prompt": f"relay:{files[0].stem}"}))
        self.assertEqual(prompt.returncode, 0, prompt.stderr)
        context = json.loads(prompt.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(body, context)

    def test_legacy_locale_handoff_remains_readable_after_upgrade(self):
        handoffs = self.home / "relay/handoffs"
        handoffs.mkdir(parents=True)
        body = "GOAL: continue caf\u00e9 work\nNEXT STEP: preserve the legacy handoff"
        path = handoffs / "1234abcd.md"
        path.write_bytes(body.encode("cp1252"))
        output = io.StringIO()
        with patch.object(relay_module, "HANDOFFS", handoffs), \
                patch.object(relay_module.locale, "getencoding", return_value="cp1252", create=True), \
                patch.object(relay_module.sys, "stdin", io.StringIO('{"prompt":"relay:1234abcd"}')), \
                contextlib.redirect_stdout(output):
            relay_module.cmd_prompt()
        context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(body, context)
        self.assertEqual(path.read_bytes(), body.encode("cp1252"))

    def test_undecodable_handoff_emits_recovery_guidance(self):
        handoffs = self.home / "relay/handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "1234abcd.md").write_bytes(b"invalid \x81 encoding")
        output = io.StringIO()
        with patch.object(relay_module, "HANDOFFS", handoffs), \
                patch.object(relay_module.locale, "getencoding", return_value="cp1252", create=True), \
                patch.object(relay_module.sys, "stdin", io.StringIO('{"prompt":"relay:1234abcd"}')), \
                contextlib.redirect_stdout(output):
            relay_module.cmd_prompt()
        context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could not be decoded", context)
        self.assertIn("original encoding", context)
        self.assertNotIn("CONTINUES", context)

    def test_scan_is_bounded_and_old_usage_does_not_trigger_rollover(self):
        transcript = self.transcript(999999)
        with transcript.open("ab") as handle:
            handle.truncate(2 * relay_module.MAX_TRANSCRIPT_BYTES)
        with transcript.open("rb") as handle:
            tracked = MagicMock(wraps=handle)
            tracked.__enter__.return_value = tracked
            with patch.object(relay_module, "open", return_value=tracked, create=True):
                self.assertIsNone(relay_module.context_tokens(transcript))
            amounts = [call.args[0] for call in tracked.read.call_args_list]
            self.assertTrue(all(0 < amount <= 1 << 20 for amount in amounts))
            self.assertEqual(sum(amounts), relay_module.MAX_TRANSCRIPT_BYTES)
        payload = json.dumps({"prompt": "continue", "session_id": "bounded",
                              "transcript_path": str(transcript)})
        result = self.run_relay("prompt", input_text=payload)
        self.assertIn("unknown", result.stdout)
        self.assertNotIn("RED", result.stdout)
        stopped = self.run_relay("stop", input_text=payload)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(stopped.stdout, "")

    def test_recent_main_usage_wins_over_sidechains_and_malformed_lines(self):
        transcript = self.transcript(999999)
        with transcript.open("a") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 1000, "cache_read_input_tokens": 40000,
                "cache_creation_input_tokens": 10000}}}) + "\n")
            handle.write(json.dumps({"type": "assistant", "isSidechain": True,
                                     "message": {"usage": {"input_tokens": 999999}}}) + "\n")
            handle.write('["usage"]\n{"usage": broken\n')
        self.assertEqual(relay_module.context_tokens(transcript), 51000)

    def test_compaction_does_not_reuse_old_context_measurement(self):
        transcript = self.transcript(999999)
        with transcript.open("a") as handle:
            handle.write('{"type":"system","subtype":"compact_boundary"}\n')
        self.assertIsNone(relay_module.context_tokens(transcript))
        with transcript.open("a") as handle:
            handle.write('{"type":"assistant","message":{"usage":{"input_tokens":100}}}\n')
        self.assertEqual(relay_module.context_tokens(transcript), 100)

    def test_partial_first_record_is_not_accepted_as_usage(self):
        transcript = self.transcript(1)
        suffix = b'{"type":"assistant","message":{"usage":{"input_tokens":999999}}}\n'
        transcript.write_bytes(b"not-a-json-record " + suffix)
        with patch.object(relay_module, "MAX_TRANSCRIPT_BYTES", len(suffix)):
            self.assertIsNone(relay_module.context_tokens(transcript))

    def test_large_record_crossing_chunks_is_reassembled(self):
        transcript = self.transcript(1)
        transcript.write_text(json.dumps({"type": "assistant", "message": {
            "content": "x" * (1 << 20), "usage": {"input_tokens": 12345}
        }}) + "\n")
        self.assertEqual(relay_module.context_tokens(transcript), 12345)

    def test_recent_usage_stops_after_one_chunk(self):
        transcript = self.transcript(1)
        transcript.write_bytes(b"x" * (2 << 20) + b'\n{"type":"assistant",'
                               b'"message":{"usage":{"input_tokens":321}}}\n')
        with transcript.open("rb") as handle:
            tracked = MagicMock(wraps=handle)
            tracked.__enter__.return_value = tracked
            with patch.object(relay_module, "open", return_value=tracked, create=True):
                self.assertEqual(relay_module.context_tokens(transcript), 321)
            tracked.read.assert_called_once_with(1 << 20)


if __name__ == "__main__":
    unittest.main()

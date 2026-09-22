import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay/relay.py"


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.home = Path(self.temp.name) / "claude"
        self.env = os.environ | {"CLAUDE_HOME": str(self.home)}

    def tearDown(self):
        self.temp.cleanup()

    def run_relay(self, *args, input_text=None, cwd=None):
        return subprocess.run(["python3", str(RELAY), *args], env=self.env,
                              cwd=cwd, input=input_text, text=True, capture_output=True)

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
        self.assertIn("GOAL: continue", files[0].read_text())


if __name__ == "__main__":
    unittest.main()

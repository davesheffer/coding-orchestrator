import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay/relay.py"
SID = "abcdef1234567890"
MARKER = "SECRET-MARKER-7f3a"


ISOLATE = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_RELAY_INPLACE_MOD", "CLAUDE_RELAY_ROLLOVER")


def load_relay(name, home):
    """Load relay.py in-process with its module-level paths under the test's temp CLAUDE_HOME."""
    with patch.dict(os.environ, {"CLAUDE_HOME": str(home)}):
        for key in ISOLATE:
            os.environ.pop(key, None)
        spec = importlib.util.spec_from_file_location(name, RELAY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class HookLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR"))
        self.tmp = Path(self.temp.name)
        self.home = self.tmp / "claude"
        self.log = self.home / "relay" / "state" / "hook-log.jsonl"
        self.env = os.environ | {"CLAUDE_HOME": str(self.home), "CLAUDE_RELAY_NO_CLIPBOARD": "1",
                                 "CLAUDE_RELAY_ROLLOVER": "inplace"}
        for name in ISOLATE[:2]:
            self.env.pop(name, None)

    def tearDown(self):
        self.temp.cleanup()

    def hook(self, name, payload, env=None):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(RELAY), name], env=env or self.env, input=text,
                              text=True, encoding="utf-8", capture_output=True)

    def lines(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def transcript(self):
        path = self.tmp / f"{MARKER}.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {"content": MARKER}}) + "\n",
                        encoding="utf-8")
        return str(path)

    def test_one_line_per_hook_with_the_fields(self):
        transcript = self.transcript()
        base = {"session_id": SID, "transcript_path": transcript, "cwd": str(self.tmp)}
        runs = [("prompt", {**base, "prompt": f"please {MARKER} now"}),
                ("stop", base), ("precompact", {**base, "trigger": "auto"}),
                ("session-start", {**base, "source": "compact"})]
        outputs = {}
        for name, payload in runs:
            result = self.hook(name, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            outputs[name] = result.stdout
        lines = self.lines()
        self.assertEqual([line["hook"] for line in lines], [name for name, _ in runs])
        for line in lines:
            self.assertEqual(set(line), {"ts", "hook", "session", "outcome", "chars"})
            self.assertEqual(line["session"], SID[:8])
            self.assertRegex(line["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
            self.assertEqual(line["chars"], len(outputs[line["hook"]]))
            self.assertEqual(line["outcome"] == "none", line["chars"] == 0, line)
        by_hook = {line["hook"]: line for line in lines}
        self.assertEqual(by_hook["precompact"]["outcome"], "pending")
        self.assertEqual(by_hook["session-start"]["outcome"], "inject")
        self.assertGreater(by_hook["session-start"]["chars"], 0)

    def test_printing_prompt_and_stop_hooks_log_inject_and_block(self):
        # A red-zone transcript in rollover "open": the prompt hook injects a gauge, the stop hook blocks.
        path = self.tmp / "red.jsonl"
        path.write_text(json.dumps({"type": "assistant", "isSidechain": False, "message": {
            "model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 400_000, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}}) + "\n", encoding="utf-8")
        env = self.env | {"CLAUDE_RELAY_ROLLOVER": "open"}
        payload = {"session_id": SID, "transcript_path": str(path), "cwd": str(self.tmp)}
        prompt = self.hook("prompt", {**payload, "prompt": "next"}, env)
        stop = self.hook("stop", payload, env)
        self.assertIn("hookSpecificOutput", prompt.stdout)
        self.assertIn('"decision": "block"', stop.stdout)
        by_hook = {line["hook"]: line for line in self.lines()}
        self.assertEqual((by_hook["prompt"]["outcome"], by_hook["prompt"]["chars"]),
                         ("inject", len(prompt.stdout)))
        self.assertEqual((by_hook["stop"]["outcome"], by_hook["stop"]["chars"]), ("block", len(stop.stdout)))

    def test_prompt_text_and_payload_paths_never_reach_the_log(self):
        payload = {"session_id": SID, "prompt": f"{MARKER} " * 50, "transcript_path": self.transcript(),
                   "cwd": str(self.tmp / MARKER)}
        for name in ("prompt", "stop", "precompact", "session-start"):
            self.assertEqual(self.hook(name, payload).returncode, 0)
        text = self.log.read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 4)
        self.assertNotIn(MARKER, text)
        self.assertNotIn(str(self.tmp), text)

    def test_unsafe_session_id_is_logged_as_null(self):
        self.assertEqual(self.hook("stop", {"session_id": "../x" + MARKER}).returncode, 0)
        self.assertIsNone(self.lines()[0]["session"])

    def test_log_rotates_past_the_cap(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_bytes(b"x" * 1_000_001)
        rotated = self.log.with_name("hook-log.jsonl.1")
        rotated.write_text("old\n", encoding="utf-8")
        self.assertEqual(self.hook("stop", {"session_id": SID}).returncode, 0)
        self.assertEqual(rotated.stat().st_size, 1_000_001)
        self.assertEqual([line["hook"] for line in self.lines()], ["stop"])
        self.assertEqual(self.hook("stop", {"session_id": SID}).returncode, 0)
        self.assertEqual(len(self.lines()), 2)  # under the cap: appended, not rotated

    def test_unwritable_state_dir_keeps_the_hook_output(self):
        transcript = self.transcript()
        payload = {"session_id": SID, "source": "compact", "transcript_path": transcript}
        normal = self.hook("session-start", payload)
        self.assertTrue(normal.stdout)
        self.log.unlink()
        # A file where the state folder should be makes every log write fail.
        blocked = dict(self.env, CLAUDE_HOME=str(self.tmp / "blocked"))
        state = self.tmp / "blocked" / "relay" / "state"
        state.parent.mkdir(parents=True)
        state.write_text("not a folder", encoding="utf-8")
        result = self.hook("session-start", payload, blocked)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertTrue(state.is_file())

    def test_exception_in_a_hook_is_logged_by_class_and_exits_zero_silently(self):
        result = self.hook("prompt", "{not json")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        [line] = self.lines()
        self.assertEqual((line["hook"], line["outcome"], line["chars"], line["session"]),
                         ("prompt", "error:JSONDecodeError", 0, None))

    def test_exception_raised_inside_the_hook_function(self):
        relay = load_relay("relay_hooklog_raise", self.home)
        state = self.tmp / "state"

        def boom():
            print("partial")
            raise KeyError("x")

        with patch.object(relay, "STATE", state), patch.object(sys, "stdin", _Stdin('{"session_id": "s1"}')), \
                patch.object(sys, "stdout", _Out()) as out, patch.object(sys, "stderr", _Out()) as err, \
                patch.object(relay.os, "_exit", side_effect=SystemExit) as hard_exit:
            with self.assertRaises(SystemExit):
                relay.run_hook("stop", boom)
        hard_exit.assert_called_once_with(0)
        self.assertEqual(out.getvalue(), "partial\n")
        self.assertEqual(err.getvalue(), "")
        [line] = [json.loads(x) for x in (state / "hook-log.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((line["outcome"], line["chars"], line["session"]), ("error:KeyError", 8, "s1"))

    def test_closed_stdout_still_exits_zero_silently(self):
        payload = {"session_id": SID, "source": "compact", "transcript_path": self.transcript()}
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            result = subprocess.run([sys.executable, str(RELAY), "session-start"], env=self.env,
                                    input=json.dumps(payload).encode(), stdout=write_end,
                                    stderr=subprocess.PIPE)
        finally:
            os.close(write_end)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        [line] = self.lines()
        self.assertEqual((line["hook"], line["chars"]), ("session-start", 0))

    def test_sweep_keeps_the_hook_logs(self):
        relay = load_relay("relay_hooklog_sweep", self.home)
        state = self.tmp / "sweep-state"
        state.mkdir()
        old = time.time() - 30 * 24 * 3600
        for name in ("hook-log.jsonl", "hook-log.jsonl.1", "oldsession.json"):
            (state / name).write_text("x", encoding="utf-8")
            os.utime(state / name, (old, old))
        empty = self.tmp / "empty"
        with patch.object(relay, "STATE", state), patch.object(relay, "HANDOFFS", empty), \
                patch.object(relay, "CHECKPOINTS", empty), patch.object(relay, "INPLACE", empty):
            relay.sweep({"handoff_ttl_hours": 72})
        self.assertEqual(sorted(p.name for p in state.iterdir()), ["hook-log.jsonl", "hook-log.jsonl.1"])


class _Out:
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self.parts)


class _Stdin:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text


if __name__ == "__main__":
    unittest.main()

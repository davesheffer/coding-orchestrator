import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timing  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "bin" / "turn-guard.py"
CLOSE_STDOUT = "import os, sys; os.close(1); os.execv(sys.executable, [sys.executable] + sys.argv[1:])"

REPORT_LINES = [
    "## RESULT", "### RESULT: ok", "- RESULT: ok", "- **RESULT:** ok", "1. RESULT: ok", "> RESULT: ok",
    "`RESULT`: ok", "__RESULT__", "_RESULT:_", "#### VERDICT: FAIL", "| RESULT |", "Result: ok",
    "Verdict: PASS", "**VERDICT: PASS**", "RESULT: ok", "  **RESULT**: ok", "RESULT",
]


class TurnGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.install = Path(self.temp.name)
        (self.install / "bin").mkdir()
        self.script = self.install / "bin" / "turn-guard.py"
        shutil.copyfile(GUARD, self.script)
        self.dir = self.install / "state" / "turn-guard"

    def tearDown(self):
        self.temp.cleanup()

    def run_guard(self, *args, data=None, raw=None):
        if raw is None:
            raw = json.dumps(data).encode("utf-8")
        return subprocess.run([sys.executable, str(self.script), *args], input=raw, capture_output=True)

    def tick(self, limit, agent="a1", **extra):
        return self.run_guard("tick", str(limit), data={"agent_id": agent, "tool_name": "Read", **extra})

    def stop(self, agent="a1", **extra):
        return self.run_guard("stop", data={"agent_id": agent, "agent_type": "builder", **extra})

    def handback(self, message, agent="a1"):
        return self.run_guard("handback", data={"agent_id": agent, "tool_name": "SubagentHandback",
                                                "tool_input": {"message": message}})

    def count(self, agent="a1"):
        return len(list(self.dir.glob(f"{agent}.n*")))

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def context(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        return out["additionalContext"]

    def assertAllowed(self, result, note=None):
        self.assertEqual((result.returncode, result.stdout), (0, b""), note)

    def test_count_increments_per_agent(self):
        for _ in range(3):
            self.tick(60)
        self.tick(60, agent="a2")
        self.assertEqual((self.count("a1"), self.count("a2")), (3, 1))

    def test_nudges_each_fire_once(self):
        messages = [self.context(self.tick(20)) for _ in range(20)]
        found = [(i + 1, m) for i, m in enumerate(messages) if m]
        self.assertEqual([i for i, _ in found], [10, 15])
        self.assertIn("10 of ~20", found[0][1])
        self.assertIn("Save progress to your report file", found[0][1])
        self.assertIn("RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED", found[1][1])
        self.assertIn("report file", found[1][1])

    def test_nudge_on_a_failed_call_names_the_failure_event(self):
        for _ in range(14):
            self.tick(20)
        result = self.tick(20, hook_event_name="PostToolUseFailure")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUseFailure")
        self.assertIn("nearly spent", out["additionalContext"])

    def test_report_regex_is_fast_on_whitespace_runs(self):
        agents = iter(range(100))  # a fresh agent per run, so no run sees an earlier one's state
        result = timing.assert_scales(
            self, lambda n: "\n" * (n // 4) + " " * n + "\n \t \n" * (n // 10) + "done",
            lambda message: self.stop(f"a{next(agents)}", last_assistant_message=message),
            5000, 20000, label="whitespace runs", abs_limit=5)
        self.assertIn(b'"block"', result.stdout)

    def test_resumed_agent_is_nudged_again_each_cycle(self):
        messages = [self.context(self.tick(20)) for _ in range(40)]
        self.assertEqual([i + 1 for i, m in enumerate(messages) if m], [10, 15, 30, 35])
        self.assertEqual(self.count(), 40)

    def test_allowed_stop_clears_flags_but_keeps_ticks(self):
        for _ in range(12):
            self.tick(20)
        self.handback("RESULT: ok")
        self.assertIn("a1.reported", self.names())
        self.assertAllowed(self.stop(last_assistant_message="done"))
        self.assertNotIn("a1.reported", self.names())
        self.assertEqual(self.count(), 12)
        self.assertIn("a1.half0", self.names())

    def test_small_cap_skips_half_nudge(self):
        messages = [self.context(self.tick(10)) for _ in range(10)]
        self.assertEqual([i + 1 for i, m in enumerate(messages) if m], [5])
        self.assertIn("nearly spent", messages[4])

    def test_critic_and_scout_are_not_told_to_write_a_file(self):
        for role in ("critic", "scout"):
            messages = [self.context(self.tick(20, agent=role, agent_type=role)) for _ in range(15)]
            half, last = messages[9], messages[14]
            self.assertIn("findings so far in your final message", half)
            self.assertIn("findings so far in your final message", last)
            self.assertNotIn("report file", half + last)

    def test_bad_limit_counts_without_nudging(self):
        for limit in ("", "abc", "0"):
            for _ in range(30):
                self.assertIsNone(self.context(self.tick(limit)))
        self.assertEqual(self.count(), 90)

    def test_parallel_ticks_are_exact(self):
        with ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(lambda _: self.tick(40), range(40)))
        messages = [m for m in (self.context(r) for r in results) if m]
        self.assertEqual(self.count(), 40)
        self.assertEqual(sum("Save progress" in m for m in messages), 1)
        self.assertEqual(sum("nearly spent" in m for m in messages), 1)
        self.assertEqual(len(messages), 2)
        self.assertFalse([n for n in self.names() if n.endswith(".tmp")])

    def test_prune_removes_old_files_when_a_new_agent_starts(self):
        self.tick(60, agent="old")
        (self.dir / "stale.json.123.tmp").write_text("x")
        for name in ("old.n1", "stale.json.123.tmp"):
            os.utime(self.dir / name, (time.time() - 90000,) * 2)
        self.tick(60, agent="new")
        self.assertEqual(self.names(), ["new.n1"])

    def test_handback_with_result_sets_reported(self):
        self.tick(60, tool_name="SubagentHandback", tool_input={"message": "**RESULT:** done\nEVIDENCE: x"})
        self.assertIn("a1.reported", self.names())
        self.tick(60, agent="a2", tool_name="SubagentHandback", tool_input={"message": "no report here"})
        self.assertNotIn("a2.reported", self.names())

    def test_handback_denies_once_then_allows(self):
        first = self.handback("all done, no report")
        self.assertEqual(first.returncode, 0)
        out = json.loads(first.stdout)["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("no RESULT line", out["permissionDecisionReason"])
        self.assertAllowed(self.handback("all done, no report"))
        self.assertIn("a1.reported", self.names())
        self.assertAllowed(self.stop(last_assistant_message="Report delivered."))

    def test_handback_with_report_is_allowed_and_recorded(self):
        self.assertAllowed(self.handback("RESULT: ok"))
        self.assertIn("a1.reported", self.names())
        self.assertNotIn("a1.denied", self.names())

    def test_every_report_form_is_accepted(self):
        for line in REPORT_LINES:
            for text in (line, f"notes\n{line}\nmore", f"notes\r\n{line}\r\nmore"):
                self.assertAllowed(self.stop(agent="f", last_assistant_message=text), repr(text))

    def test_prose_mentioning_result_is_not_a_report(self):
        for text in ("RESULT of grep was empty; still working", "I will report the result later",
                     "the VERDICT is pending", "RESULTS: none", "no report"):
            out = json.loads(self.stop(last_assistant_message=text).stdout)
            self.assertEqual(out["decision"], "block", text)

    def test_stop_blocks_without_result(self):
        result = self.stop(last_assistant_message="I am done")
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("RESULT", out["reason"])
        self.assertIn("SubagentHandback", out["reason"])

    def test_stop_allows_when_report_present_or_hook_active(self):
        self.assertAllowed(self.stop(last_assistant_message="RESULT: ok"))
        self.assertAllowed(self.stop(last_assistant_message="x", stop_hook_active=True))

    def test_stop_allows_after_reported_handback(self):
        self.tick(60, tool_name="SubagentHandback", tool_input={"message": "RESULT: ok"})
        self.assertAllowed(self.stop(last_assistant_message="handed back"))

    def test_stop_allows_when_fields_are_missing(self):
        base = {"agent_id": "a1", "agent_type": "builder", "last_assistant_message": "x"}
        for drop in ("agent_id", "agent_type", "last_assistant_message"):
            self.assertAllowed(self.run_guard("stop", data={k: v for k, v in base.items() if k != drop}), drop)
        for key, value in (("agent_id", ""), ("agent_type", ""), ("agent_type", 5), ("last_assistant_message", None)):
            self.assertAllowed(self.run_guard("stop", data={**base, key: value}), (key, value))

    def test_malformed_input_fails_open(self):
        for args, raw in ((("tick", "20"), b"not json"), (("stop",), b""), (("tick", "20"), b"[]"),
                          (("tick", "20"), b"{}"), (("bogus",), b"{}"), ((), b"{}"), (("handback",), b"{}"),
                          (("tick", "20"), b"\xff\xfe"), (("tick", "20"), json.dumps({"agent_id": ["x"]}).encode())):
            self.assertAllowed(self.run_guard(*args, raw=raw), (args, raw))

    def test_handback_without_string_message_allows(self):
        for agent, data in (("h1", {"session_id": "s1"}), ("h2", {"agent_id": "h2", "tool_input": None}),
                            ("h3", {"agent_id": "h3", "tool_input": {"message": 5}})):
            self.assertAllowed(self.run_guard("handback", data=data), agent)

    def test_create_error_fails_open_without_spinning(self):
        import builtins
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("turn_guard_under_test", self.script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []

        def denied(path, mode="r", *args, **kwargs):
            if mode == "x":
                calls.append(path)
                raise PermissionError(13, "denied", path)
            return builtins.open(path, mode, *args, **kwargs)

        with mock.patch.object(module, "open", denied, create=True):
            with self.assertRaises(PermissionError):
                module.tick({"agent_id": "a1"}, module.STATE / "a1", "20")
        self.assertEqual(len(calls), 1)

    def test_closed_stdout_still_exits_zero(self):
        data = json.dumps({"agent_id": "a1", "agent_type": "builder", "last_assistant_message": "x"}).encode()
        result = subprocess.run([sys.executable, "-c", CLOSE_STDOUT, str(self.script), "stop"], input=data,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_ascii_utf8_input(self):
        result = self.run_guard("stop", data={"agent_id": "a1", "agent_type": "builder",
                                              "last_assistant_message": "שלום ✓\nRESULT: ok"})
        self.assertAllowed(result)
        self.assertIsNone(self.context(self.run_guard("tick", "20", data={
            "agent_id": "אב", "tool_name": "Read", "agent_type": "builder", "tool_input": {"x": "✓"}})))
        self.assertEqual(self.count("__"), 1)

    def test_session_id_is_the_fallback_and_ids_are_sanitized(self):
        self.run_guard("tick", "60", data={"session_id": "s/../1", "tool_name": "Read"})
        self.assertEqual(self.count("s____1"), 1)
        self.assertEqual([p.name for p in (self.install / "state").iterdir()], ["turn-guard"])


if __name__ == "__main__":
    unittest.main()

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks" / "effort"


def load(name):
    spec = importlib.util.spec_from_file_location(f"effort_{name}", BENCH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EffortBenchTests(unittest.TestCase):
    def test_offline_check_passes(self):
        result = subprocess.run([sys.executable, str(BENCH / "bench.py"), "--check"], cwd=ROOT,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_money_is_the_only_clean_critic_case(self):
        cases = load("critic_tasks").CASES
        self.assertEqual([name for name, case in cases.items() if case["seed"] is None], ["money"])

    def test_missing_output_fails_at_argument_parsing(self):
        result = subprocess.run([sys.executable, str(BENCH / "bench.py")], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--output is required", result.stderr)
        self.assertNotIn("jobs", result.stdout)

    def test_score_critic_accept_range_and_line_parsing(self):
        bench = load("bench")
        case = bench.CASES["lockfile"]  # seeded line 12, accept (9, 12)
        score = bench.score_critic
        self.assertTrue(score(case, [{"line": 9}])["correct"])
        self.assertFalse(score(case, [{"line": 20}])["correct"])
        self.assertEqual(score(case, [{"line": 9}, {"line": 20}])["extra_findings"], 1)
        self.assertTrue(score(case, [{"line": "10"}])["correct"])
        self.assertIsNone(bench.parse_line("²"))
        tags = bench.CASES["tags"]  # seeded line 1, so True == 1 would land without the bool guard
        self.assertEqual(bench.case_diff(tags)[0], 1)
        self.assertFalse(score(tags, [{"line": True}])["correct"])
        self.assertTrue(score(tags, [{"line": 1}])["correct"])
        clean = bench.CASES["money"]
        self.assertTrue(score(clean, [])["correct"])
        self.assertFalse(score(clean, [{"line": 3}])["correct"])
        self.assertEqual(score(clean, [{"line": 3}])["false_pos"], 1)

    def test_check_fails_when_a_reference_is_broken(self):
        bench = load("bench")
        tasks = {name: dict(t) for name, t in bench.TASKS.items()}
        name = next(iter(tasks))
        tasks[name]["ref"] = tasks[name]["stub"]
        bench.TASKS = tasks
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.check(), 1)

    def test_model_mismatch_detection(self):
        bench = load("bench")
        self.assertIn("claude-opus-4-8", bench.model_mismatch("critic", ["claude-opus-4-8", "claude-fable-5-2"]))
        self.assertIsNone(bench.model_mismatch("critic", ["claude-fable-5-2", "claude-haiku-4-5"]))
        self.assertIsNone(bench.model_mismatch("builder", ["claude-sonnet-5-5"]))
        self.assertIsNotNone(bench.model_mismatch("builder", ["claude-sonnet-5"]))
        self.assertIn("missing", bench.model_mismatch("critic", []))
        self.assertIn("missing", bench.model_mismatch("critic", ["claude-haiku-4-5"]))

    def test_run_flags_model_mismatch_and_missing_model(self):
        bench = load("bench")

        def fake(usage):
            out = json.dumps({"result": "ok", "total_cost_usd": 0.2, "num_turns": 1, "modelUsage": usage})
            return mock.patch.object(bench.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], 0, out, ""))

        with fake({"claude-fable-5-1": {"outputTokens": 0}, "claude-opus-4-8": {"outputTokens": 9}}):
            r = bench.run("p", "m", "low", ".", "Read", "critic")
        self.assertIn("model mismatch", r["error"])
        self.assertEqual(r["cost"], 0.2)
        with fake({}):
            r = bench.run("p", "m", "low", ".", "Read", "critic")
        self.assertIn("missing", r["error"])
        with fake({"claude-fable-5-2": {"outputTokens": 5}}):
            self.assertNotIn("error", bench.run("p", "m", "low", ".", "Read", "critic"))

    def test_run_keeps_cli_error_and_appends_mismatch(self):
        bench = load("bench")
        for usage in ({}, {"claude-opus-4-8": {"outputTokens": 3}}, None, ["x"]):
            out = json.dumps({"is_error": True, "subtype": "error_max_turns", "total_cost_usd": 0.1,
                              "modelUsage": usage})
            with mock.patch.object(bench.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 1, out, "")):
                r = bench.run("p", "m", "low", ".", "Read", "critic")
            self.assertTrue(r["error"].startswith("cli error: error_max_turns"), r["error"])
            self.assertIn("model mismatch", r["error"])
            self.assertEqual(r["cost"], 0.1)
        out = json.dumps({"is_error": True, "subtype": "error_max_turns",
                          "modelUsage": {"claude-fable-5-2": {"outputTokens": 1}}})
        with mock.patch.object(bench.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, out, "")):
            self.assertEqual(bench.run("p", "m", "low", ".", "Read", "critic")["error"],
                             "cli error: error_max_turns")

    def test_run_rejects_non_object_json(self):
        bench = load("bench")
        for out in ("[1, 2]", "5", '"x"', "null"):
            with mock.patch.object(bench.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, out, "")):
                r = bench.run("p", "m", "low", ".", "Read", "critic")
            self.assertTrue(r["error"].startswith("non-object json"), r["error"])

    def test_parse_line_rejects_huge_digit_strings(self):
        bench = load("bench")
        self.assertIsNone(bench.parse_line("9" * 5000))
        self.assertIsNone(bench.parse_line("9" * 19))
        self.assertEqual(bench.parse_line(" 12 "), 12)
        self.assertEqual(bench.parse_line(12), 12)

    def test_builder_scoring_crash_keeps_cost_and_prior_error(self):
        bench = load("bench")
        run = {"cost": 0.3, "result": "DONE", "error": "cli error: x"}
        with mock.patch.object(bench, "run", return_value=dict(run)), \
                mock.patch.object(bench, "score_builder", side_effect=RuntimeError("boom")):
            r = bench.builder(next(iter(bench.TASKS)), "low")
        self.assertFalse(r["correct"])
        self.assertTrue(r["error"].startswith("scoring: RuntimeError: boom"), r["error"])
        self.assertIn("cli error: x", r["error"])
        self.assertEqual(r["cost"], 0.3)

    def test_builder_with_hanging_solution_is_scored_not_raised(self):
        bench = load("bench")
        bench.UNITTEST_TIMEOUT = 2

        def fake_run(prompt, model, effort, cwd, tools, role, extra=()):
            (Path(cwd) / "solution.py").write_text("while True:\n    pass\n", encoding="utf-8")
            return {"cost": 0.3, "result": "DONE"}

        with mock.patch.object(bench, "run", fake_run):
            r = bench.builder(next(iter(bench.TASKS)), "low")
        self.assertFalse(r["hidden_pass"])
        self.assertFalse(r["correct"])
        self.assertEqual(r["cost"], 0.3)

    def test_critic_non_list_findings_is_a_parse_failure(self):
        bench = load("bench")
        for result in ('{"findings": 5}', '{"findings": null}'):
            with mock.patch.object(bench, "run", return_value={"cost": 0.4, "result": result}):
                r = bench.critic("tags", "low")
            self.assertFalse(r["correct"])
            self.assertIsNone(r["findings"])
            self.assertEqual(r["cost"], 0.4)

    def test_critic_scoring_crash_keeps_cost(self):
        bench = load("bench")
        with mock.patch.object(bench, "run", return_value={"cost": 0.4, "result": '{"findings": []}'}), \
                mock.patch.object(bench, "score_critic", side_effect=RuntimeError("boom")):
            r = bench.critic("tags", "low")
        self.assertFalse(r["correct"])
        self.assertIn("scoring: RuntimeError: boom", r["error"])
        self.assertEqual(r["cost"], 0.4)

    def test_work_turns_a_raising_builder_into_a_counted_error_row(self):
        bench = load("bench")
        with tempfile.TemporaryDirectory() as tmp:
            bench.ARGS = types.SimpleNamespace(cap=10.0)
            bench.OUT = Path(tmp) / "rows.json"
            with mock.patch.object(bench, "builder", side_effect=RuntimeError("boom")), \
                    contextlib.redirect_stdout(io.StringIO()):
                r = bench.work(("builder", next(iter(bench.TASKS)), "low", 0))
            self.assertFalse(r["correct"])
            self.assertIn("exception: RuntimeError: boom", r["error"])
            self.assertEqual(json.loads(bench.OUT.read_text(encoding="utf-8"))[0]["error"], r["error"])


if __name__ == "__main__":
    unittest.main()

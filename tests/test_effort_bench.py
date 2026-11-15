import contextlib
import importlib.util
import io
import json
import math
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
        self.assertEqual(score(case, [{"line": 9}, {"line": "x"}, "junk", {}])["extra_findings"], 3)
        self.assertTrue(score(case, [{"line": 9}, {"line": "x"}])["correct"])
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
        def run_with(usage):
            out = json.dumps({"is_error": True, "subtype": "error_max_turns", "total_cost_usd": 0.1,
                              "modelUsage": usage})
            with mock.patch.object(bench.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 1, out, "")):
                return bench.run("p", "m", "low", ".", "Read", "critic")

        r = run_with({"claude-opus-4-8": {"outputTokens": 3}})
        self.assertTrue(r["error"].startswith("cli error: error_max_turns; model mismatch"), r["error"])
        self.assertEqual(r["cost"], 0.1)
        # No model was called (empty or malformed usage) or the right one was: no mismatch label.
        for usage in ({}, None, ["x"], {"claude-fable-5-2": {"outputTokens": 1}}):
            r = run_with(usage)
            self.assertEqual(r["error"], "cli error: error_max_turns", usage)
            self.assertEqual(r["cost"], 0.1)

    def test_run_survives_malformed_fields_and_keeps_cost(self):
        bench = load("bench")
        out = json.dumps({"result": ["x"], "total_cost_usd": 0.3,
                          "modelUsage": {"claude-fable-5-2": 5, "claude-opus-5-5": {"outputTokens": "7"},
                                         "claude-haiku-5-5": {"outputTokens": 2}}})
        with mock.patch.object(bench.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, out, "")):
            r = bench.run("p", "m", "low", ".", "Read", "critic")
        self.assertEqual((r["result"], r["out_tokens"], r["cost"]), ("", 0, 0.3))
        self.assertIn("model mismatch", r["error"])  # opus served a critic run
        out = json.dumps({"result": "ok", "total_cost_usd": "0.3",
                          "modelUsage": {"claude-fable-5-2": {"outputTokens": 4}}})
        with mock.patch.object(bench.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, out, "")):
            r = bench.run("p", "m", "low", ".", "Read", "critic")
        self.assertEqual((r["cost"], r["out_tokens"]), (bench.RUN_BUDGET, 4))
        self.assertEqual(r["error"], f"bad cost: 0.3 (counted as ${bench.RUN_BUDGET})")
        out = json.dumps({"is_error": True, "subtype": "x", "total_cost_usd": "1",
                          "modelUsage": {"claude-opus-4-8": {"outputTokens": 1}}})
        with mock.patch.object(bench.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, out, "")):
            self.assertTrue(bench.run("p", "m", "low", ".", "Read", "critic")["error"]
                            .startswith(f"cli error: x; bad cost: 1 (counted as ${bench.RUN_BUDGET}); "
                                        "model mismatch"))
        for cost, label in ((float("nan"), True), (float("inf"), True), (1e999, True), (10 ** 400, True),
                            (-0.5, True), (True, True), (False, True), (0, False), (0.25, False)):
            out = json.dumps({"result": "ok", "total_cost_usd": cost,
                              "modelUsage": {"claude-fable-5-2": {"outputTokens": 1}}})
            with mock.patch.object(bench.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, out, "")):
                r = bench.run("p", "m", "low", ".", "Read", "critic")
            self.assertEqual("error" in r, label, cost)
            self.assertEqual(r["cost"], bench.RUN_BUDGET if label else float(cost))
        out = json.dumps({"result": "ok", "total_cost_usd": 0.1,
                          "modelUsage": {"claude-fable-5-2": {"outputTokens": float("nan")},
                                         "claude-opus-4-8": {"outputTokens": -3}}})
        with mock.patch.object(bench.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, out, "")):
            self.assertEqual(bench.run("p", "m", "low", ".", "Read", "critic")["out_tokens"], 0)

    def test_unreadable_costs_still_reach_the_cap(self):
        bench = load("bench")
        with tempfile.TemporaryDirectory() as tmp:
            bench.ARGS = types.SimpleNamespace(cap=2.0)
            bench.OUT = Path(tmp) / "rows.json"
            calls = []

            out = json.dumps({"result": "DONE", "total_cost_usd": "x",
                              "modelUsage": {"claude-sonnet-5-5": {"outputTokens": 1}}})

            def fake_builder(task, effort):
                calls.append(task)
                return bench.run("p", "sonnet", effort, ".", "Read", "builder")

            with mock.patch.object(bench, "builder", fake_builder), \
                    mock.patch.object(bench.subprocess, "run",
                                      return_value=subprocess.CompletedProcess([], 0, out, "")), \
                    contextlib.redirect_stdout(io.StringIO()):
                for t in list(bench.TASKS):
                    bench.work(("builder", t, "low", 0))
            self.assertEqual(len(calls), 2)  # $1.5 + $1.5 reaches the $2 cap; the rest are skipped

    def test_cli_error_with_only_haiku_usage_has_no_mismatch(self):
        bench = load("bench")
        out = json.dumps({"is_error": True, "subtype": "x", "modelUsage": {"claude-haiku-5-5": {}}})
        with mock.patch.object(bench.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, out, "")):
            self.assertEqual(bench.run("p", "m", "low", ".", "Read", "critic")["error"], "cli error: x")

    def test_model_and_effort_flags(self):
        bench = load("bench")
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "rows.json")
            for bad in (["--builder-model", "fable"], ["--critic-model", "opus"], ["--efforts", "low,huge"],
                        ["--efforts", ","], ["--efforts", "low,,medium"], ["--efforts", ""],
                        ["--builder-model", "claude-opus-5"],
                        ["--builder-model", "claude-"], ["--builder-model", "claude-fable"],
                        ["--critic-model", "claude-opus-latest"], ["--builder-model", "claude-opus-5-5x"],
                        ["--builder-model", "claude-opus-٥-٥"]):
                # If validation ever lets a bad flag through, fail here instead of starting paid runs.
                with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()), \
                        mock.patch.object(bench, "work", side_effect=AssertionError("validation fell through")), \
                        mock.patch.object(bench, "CLAUDE", "claude-must-not-run"):
                    bench.main(["--output", out, *bad])
                self.assertEqual(cm.exception.code, 2, bad)
            seen = []
            with mock.patch.object(bench, "work", side_effect=seen.append), \
                    contextlib.redirect_stdout(io.StringIO()):
                bench.main(["--output", out, "--only", "builder", "--builder-reps", "1",
                            "--builder-model", "claude-opus-5-5", "--efforts", "low,medium,low"])
            self.assertEqual(len(seen), 2 * len(bench.TASKS))
            self.assertEqual({job[2] for job in seen}, {"low", "medium"})
            self.assertEqual((bench.BUILDER_MODEL, bench.EXPECTED["builder"]),
                             ("claude-opus-5-5", "claude-opus-5-5"))
            self.assertIsNone(bench.model_mismatch("builder", ["claude-opus-5-5"]))
            with mock.patch.object(bench, "work", side_effect=seen.append), \
                    contextlib.redirect_stdout(io.StringIO()):
                bench.main(["--output", out, "--only", "critic", "--critic-reps", "1",
                            "--critic-model", "claude-opus-5-5", "--efforts", "xhigh,max"])
            self.assertEqual((bench.BUILDER_MODEL, bench.EXPECTED["builder"]), ("sonnet", "claude-sonnet-5-5"))
            self.assertEqual((bench.CRITIC_MODEL, bench.EXPECTED["critic"]), ("claude-opus-5-5", "claude-opus-5-5"))
            self.assertEqual({job[2] for job in seen[-2 * len(bench.CASES):]}, {"xhigh", "max"})
            with mock.patch.object(bench, "work", side_effect=seen.append), \
                    contextlib.redirect_stdout(io.StringIO()):
                bench.main(["--output", out, "--only", "critic", "--critic-reps", "1"])
            self.assertEqual((bench.CRITIC_MODEL, bench.EXPECTED["critic"]), ("claude-fable-5-2", "claude-fable-5-2"))
            default = []
            with mock.patch.object(bench, "work", side_effect=default.append), \
                    contextlib.redirect_stdout(io.StringIO()):
                bench.main(["--output", out])
            self.assertEqual(len(default), 3 * 3 * len(bench.TASKS) + 2 * 3 * len(bench.CASES))
            self.assertEqual({job[2] for job in default}, {"low", "medium", "high"})

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
        for prior in ({}, {"error": "cli error: x"}):
            run = {"cost": 0.4, "result": '{"findings": []}', **prior}
            with mock.patch.object(bench, "run", return_value=run), \
                    mock.patch.object(bench, "score_critic", side_effect=RuntimeError("boom")):
                r = bench.critic("tags", "low")
            self.assertFalse(r["correct"])
            self.assertEqual(r["error"], "scoring: RuntimeError: boom" + ("; cli error: x" if prior else ""))
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
            self.assertTrue(r["cost_unknown"])
            self.assertEqual(bench.spent[0], bench.RUN_BUDGET)

    def test_rows_without_cost_still_reach_the_cap(self):
        bench = load("bench")
        with tempfile.TemporaryDirectory() as tmp:
            bench.ARGS = types.SimpleNamespace(cap=2.0)
            bench.OUT = Path(tmp) / "rows.json"
            calls = []

            def fake_builder(task, effort):
                calls.append(task)
                return bench.run("p", "sonnet", effort, ".", "Read", "builder")

            for result in (subprocess.TimeoutExpired("claude", 900),
                           subprocess.CompletedProcess([], 1, "not json", "boom"),
                           subprocess.CompletedProcess([], 0, "[1]", "")):
                calls.clear()
                bench.spent[0] = 0.0
                kw = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with mock.patch.object(bench, "builder", fake_builder), \
                        mock.patch.object(bench.subprocess, "run", **kw), \
                        contextlib.redirect_stdout(io.StringIO()):
                    for t in list(bench.TASKS):
                        bench.work(("builder", t, "low", 0))
                self.assertEqual(len(calls), 2, result)
            ok = {"cost": 0.25, "result": "DONE"}
            bench.spent[0] = 0.0
            with mock.patch.object(bench, "builder", return_value=dict(ok)), \
                    contextlib.redirect_stdout(io.StringIO()):
                r = bench.work(("builder", next(iter(bench.TASKS)), "low", 0))
            self.assertNotIn("cost_unknown", r)
            self.assertEqual(bench.spent[0], 0.25)


if __name__ == "__main__":
    unittest.main()

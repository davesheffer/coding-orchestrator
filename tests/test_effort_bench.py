import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path


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
        self.assertFalse(score(case, [{"line": True}])["correct"])
        self.assertTrue(score(case, [{"line": "10"}])["correct"])
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


if __name__ == "__main__":
    unittest.main()

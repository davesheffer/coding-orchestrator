import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "bin" / "jev-report.py"
EVAL = ROOT / "bin" / "eval-jev-routing.py"
BENCHMARK = ROOT / "benchmarks" / "jev-routing.json"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load("jev_report", REPORT)
evaluator = load("eval_jev_routing", EVAL)


def clean_env():
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    return env


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                          env=clean_env(), timeout=30)


def route(desc, choice, conf, applied=True, agent="builder", latency=100, feature=True, legacy=False):
    entry = {"ts": "2026-09-24T10:00:00+0000", "subagent_type": agent,
             "current": "inherit", "choice": choice, "confidence": conf, "applied": applied}
    if legacy:
        entry["description"] = desc
    else:
        entry["desc_hash"] = desc
    if latency is not None:
        entry["latency_ms"] = latency
    if feature:
        entry["feature"] = "route"
    return entry


def check(desc, weak, model=None, legacy=False):
    entry = {"ts": "2026-09-24T10:05:00+0000", "feature": "report_check", "subagent_type": "builder",
             "model": model, "weak": weak, "reasons": [], "latency_ms": 50}
    entry["description" if legacy else "desc_hash"] = desc
    return entry


class ReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.log = self.dir / "jev-log.jsonl"

    def write(self, path, entries, extra_lines=()):
        lines = [json.dumps(e) for e in entries] + list(extra_lines)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_summarize_mixed_features_legacy_and_malformed(self):
        self.write(self.log.with_name("jev-log.jsonl.1"),
                   [route("old", "opus", 0.4, applied=False, latency=None, feature=False)],
                   ["{not json", "[1, 2]", ""])
        self.write(self.log, [
            route("a", "sonnet", 0.6, agent="scout", latency=200),
            route("b", "fable", 0.95, latency=300),
            {"feature": "shift", "zone": "AMBER", "p": 0.9, "decision": "shift", "latency_ms": 40},
            {"feature": "shift", "decision": "continue"},
            {"feature": "risk_gate", "op": "push", "decision": "deny", "latency_ms": 70},
            {"feature": "handoff_grade", "score": 3, "weak": False, "latency_ms": 10},
            {"feature": "handoff_grade", "score": 1, "weak": True, "accepted_weak": True},
            {"feature": "mystery"},
        ], ["garbage"])
        entries = report.load_entries(report.log_paths(self.log))
        summary = report.summarize(entries)
        r = summary["route"]
        self.assertEqual(summary["entries"], 8)
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["applied"], 2)
        self.assertAlmostEqual(r["applied_rate"], 0.6667)
        self.assertEqual(r["choices"], {"builder": {"fable": 1, "opus": 1}, "scout": {"sonnet": 1}})
        self.assertEqual(r["confidence_histogram"], {"<0.5": 1, "0.5-0.7": 1, "0.7-0.9": 0, ">=0.9": 1})
        self.assertEqual(r["latency_ms"], {"count": 2, "p50": 200.0, "p95": 300.0})
        self.assertIsNone(r["warning"])
        self.assertEqual(summary["shift"]["decisions"], {"continue": 1, "shift": 1})
        self.assertEqual(summary["risk_gate"]["decisions"], {"deny": 1})
        grade = summary["handoff_grade"]
        self.assertEqual((grade["total"], grade["score_mean"], grade["weak"], grade["accepted_weak"]),
                         (2, 2.0, 1, 1))
        text = report.render(summary)
        self.assertIn("route: 3 total", text)
        self.assertIn("handoff_grade: 2 total", text)

    def test_report_check_tier_join(self):
        entries = [
            check("early", True),                           # before any route -> unknown
            route("task", "opus", 0.9, applied=False),      # not applied: ignored
            route("task", "sonnet", 0.9),
            check("task", True),                            # joins sonnet (desc_hash)
            route("task", "fable", 0.9),
            check("task", False),                           # joins latest applied: fable
            check("task", True, model="opus"),              # explicit model wins
            check("other", False),                          # no matching route
        ]
        summary = report.summarize(entries)["report_check"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["weak"], 3)
        self.assertEqual(summary["per_tier"], {
            "fable": {"total": 1, "weak": 0, "weak_rate": 0.0},
            "inherit/unknown": {"total": 2, "weak": 1, "weak_rate": 0.5},
            "opus": {"total": 1, "weak": 1, "weak_rate": 1.0},
            "sonnet": {"total": 1, "weak": 1, "weak_rate": 1.0},
        })

    def test_report_check_tier_join_uses_escalated_tier(self):
        escalated = {**route("task", "sonnet", 0.6), "escalated_to": "fable"}
        summary = report.summarize([escalated, check("task", True)])["report_check"]
        self.assertEqual(list(summary["per_tier"]), ["fable"])

    def test_report_check_tier_join_legacy_description(self):
        entries = [
            route("legacy-task", "sonnet", 0.9, legacy=True),
            check("legacy-task", True, legacy=True),        # both legacy: joins sonnet
            check("legacy-task", False),                    # desc_hash side, route is legacy: no match
            route("legacy-task", "opus", 0.9),               # desc_hash route
            check("legacy-task", False, legacy=True),       # legacy side still joins the legacy route: sonnet
        ]
        summary = report.summarize(entries)["report_check"]
        self.assertEqual(summary["per_tier"], {
            "inherit/unknown": {"total": 1, "weak": 0, "weak_rate": 0.0},
            "sonnet": {"total": 2, "weak": 1, "weak_rate": 0.5},
        })

    def test_saturation_warning(self):
        entries = [route(str(i), "opus", 0.995) for i in range(9)] + [route("x", "opus", 0.6)]
        summary = report.summarize(entries)
        self.assertEqual(summary["route"]["warning"], report.SATURATION_WARNING)
        self.assertIn("confidence looks saturated; run bin/eval-jev-routing.py", report.render(summary))
        entries.append(route("y", "opus", 0.6))
        self.assertIsNone(report.summarize(entries)["route"]["warning"])

    def test_empty_log_exits_zero(self):
        result = run(REPORT, "--log", str(self.log))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"no jev log entries at {self.log}", result.stdout)

    def test_json_output_parses(self):
        self.write(self.log, [route("a", "sonnet", 0.8), check("a", True)])
        result = run(REPORT, "--log", str(self.log), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["route"]["total"], 1)
        self.assertEqual(data["report_check"]["per_tier"]["sonnet"]["weak"], 1)


def answers(choice, conf):
    return {"model": {"type": "choice", "choice": choice, "confidence": conf,
                      "probabilities": {choice: conf}}}


class EvalTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = evaluator.eval_config(evaluator.load_config(Path("/nonexistent/config.json")))

    def test_benchmark_file_valid(self):
        data = json.loads(BENCHMARK.read_text(encoding="utf-8"))
        self.assertEqual(evaluator.validate_benchmark(data), [])
        tasks = data["tasks"]
        self.assertEqual(len(tasks), 30)
        self.assertEqual(Counter(t["expected"] for t in tasks), {"sonnet": 10, "opus": 10, "fable": 10})
        self.assertEqual(len({t["id"] for t in tasks}), 30)

    def test_validate_rejects_bad_benchmark(self):
        bad = {"version": 1, "tasks": [{"id": "a", "subagent_type": "x", "description": "d",
                                        "prompt": "p", "expected": "haiku"},
                                       {"id": "a"}]}
        problems = evaluator.validate_benchmark(bad)
        self.assertTrue(any("expected" in p for p in problems))
        self.assertTrue(any("duplicate" in p for p in problems))

    def test_dry_run_exit_zero(self):
        result = run(EVAL, "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["expected"], {"fable": 10, "opus": 10, "sonnet": 10})

    def test_no_key_exits_two(self):
        result = run(EVAL, "--config", "/nonexistent/config.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no API key", result.stderr)

    def test_evaluate_with_fake_classifier(self):
        tasks = [
            {"id": "t1", "subagent_type": "scout", "description": "d1", "prompt": "p1", "expected": "sonnet"},
            {"id": "t2", "subagent_type": "builder", "description": "d2", "prompt": "p2", "expected": "opus"},
            {"id": "t3", "subagent_type": "critic", "description": "d3", "prompt": "p3", "expected": "fable"},
            {"id": "t4", "subagent_type": "builder", "description": "d4", "prompt": "p4", "expected": "opus"},
            {"id": "t5", "subagent_type": "builder", "description": "d5", "prompt": "p5", "expected": "fable"},
        ]
        replies = {"d1": answers("sonnet", 0.9), "d2": answers("opus", 0.7), "d3": answers("opus", 0.8),
                   "d4": None, "d5": RuntimeError("boom")}
        seen = []

        def classify(state, questions):
            seen.append(state)
            self.assertIn("model", questions)
            reply = replies[state["description"]]
            if isinstance(reply, Exception):
                raise reply
            return reply

        result = evaluator.evaluate(tasks, self.cfg, classify)
        self.assertEqual([s["prompt"] for s in seen], ["p1", "p2", "p3", "p4", "p5"])
        self.assertEqual((result["total"], result["correct"], result["errors"]), (5, 2, 2))
        self.assertEqual(result["accuracy"], 0.4)
        self.assertEqual(result["confusion"]["sonnet"], {"sonnet": 1, "opus": 0, "fable": 0, "error": 0})
        self.assertEqual(result["confusion"]["opus"], {"sonnet": 0, "opus": 1, "fable": 0, "error": 1})
        self.assertEqual(result["confusion"]["fable"], {"sonnet": 0, "opus": 1, "fable": 0, "error": 1})
        self.assertEqual(result["recall"], {"sonnet": 1.0, "opus": 0.5, "fable": 0.0})
        self.assertEqual(result["mean_confidence"], {"overall": 0.8, "correct": 0.8, "wrong": 0.8})
        self.assertEqual([m["id"] for m in result["misses"]], ["t3", "t4", "t5"])
        self.assertEqual(result["misses"][0], {"id": "t3", "expected": "fable", "got": "opus", "conf": 0.8})
        text = evaluator.render(result)
        self.assertIn("accuracy: 40.0%", text)
        self.assertIn("t4: expected opus, got error", text)

    def test_eval_config_forces_route_and_overrides_labels(self):
        cfg = dict(self.cfg, enabled=False, features={"route": False, "shift": True})
        forced = evaluator.eval_config(cfg, {"sonnet": "cheap", "fable": "hard"})
        self.assertTrue(forced["enabled"])
        self.assertTrue(forced["features"]["route"])
        self.assertEqual(list(forced["labels"]), ["sonnet", "fable"])


if __name__ == "__main__":
    unittest.main()

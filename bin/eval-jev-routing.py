#!/usr/bin/env python3
"""Evaluate Jev model routing against a labelled benchmark.

Loads benchmarks/jev-routing.json (tasks with an expected tier), asks the Jev
classifier the same question bin/jev-route.py asks, sequentially, and reports
accuracy, a confusion matrix, per-tier recall, confidence by outcome and the
list of misses. "Applied accuracy" scores what the router would actually run:
jev-route's own rule (pinned_agents, min_confidence, same-model skip) decides
whether the choice is applied, otherwise the agent keeps its frontmatter model
(or "inherit"). The route feature is forced on so the result does not depend
on the user's feature toggles. `--labels-file` swaps in candidate rubrics
({tier: rubric}) for tuning. `--dry-run` only validates the benchmark.

Usage: eval-jev-routing.py [--benchmark PATH] [--labels-file PATH] [--config PATH]
                           [--dry-run] [--json]
Exit codes: 0 ok, 1 invalid benchmark or labels, 2 no API key.
"""
import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
from jev_client import AGENT_MODELS, CONFIG_PATH, ROOT, api_key, ask, load_config  # noqa: E402

BENCHMARK_PATH = ROOT / "benchmarks" / "jev-routing.json"
TASK_FIELDS = ("id", "subagent_type", "description", "prompt", "expected")
ERROR = "error"


def load_router():
    spec = importlib.util.spec_from_file_location("jev_route", BIN / "jev-route.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_benchmark(data, tiers=("sonnet", "opus", "fable")):
    """Return a list of problems; empty means valid."""
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tasks"), list):
        return ["benchmark must be an object with version 1 and a tasks list"]
    problems, seen = [], set()
    for index, task in enumerate(data["tasks"]):
        if not isinstance(task, dict):
            problems.append(f"task {index}: not an object")
            continue
        for field in TASK_FIELDS:
            if not isinstance(task.get(field), str) or not task[field].strip():
                problems.append(f"task {index}: missing or empty {field}")
        if task.get("expected") not in tiers:
            problems.append(f"task {index}: expected must be one of {', '.join(tiers)}")
        if task.get("id") in seen:
            problems.append(f"task {index}: duplicate id {task.get('id')}")
        seen.add(task.get("id"))
    if not data["tasks"]:
        problems.append("benchmark has no tasks")
    return problems


def load_labels(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or not data
            or not all(k in AGENT_MODELS and isinstance(v, str) and v.strip() for k, v in data.items())):
        raise ValueError(f"labels must map tiers ({', '.join(AGENT_MODELS)}) to non-empty rubric strings")
    return data


def eval_config(cfg, labels=None):
    """Copy of cfg with the route feature forced on and optional label overrides."""
    cfg = dict(cfg)
    cfg["enabled"] = True
    cfg["features"] = {**cfg.get("features", {}), "route": True}
    if labels is not None:
        cfg["labels"] = dict(labels)
    return cfg


def mean(values):
    return round(sum(values) / len(values), 4) if values else None


def evaluate(tasks, cfg, classify_fn=None, router=None, home=None):
    """Classify each task and score it against its expected tier.

    classify_fn(state, questions) returns the Jev `answers` dict (or None on
    failure); it defaults to jev_client.ask for the route feature. Any failure
    or unusable answer is recorded as "error". Agent frontmatter models are read
    from `home` (default: this install), as jev-route does.
    """
    router = router or load_router()
    home = router.ROOT if home is None else home
    if classify_fn is None:
        classify_fn = lambda state, questions: ask(cfg, "route", state, questions)  # noqa: E731
    questions = router.build_questions(cfg)
    tiers = list(cfg["labels"])
    for task in tasks:
        if task["expected"] not in tiers:
            tiers.append(task["expected"])
    columns = tiers + [ERROR]
    confusion = {row: {col: 0 for col in columns} for row in tiers}
    results, misses = [], []
    for task in tasks:
        state = router.build_state(task, cfg)
        current = router.current_model({}, task, home)
        choice, confidence, probabilities = ERROR, None, None
        try:
            answer = (classify_fn(state, questions) or {})["model"]
            if answer.get("choice") in cfg["labels"]:
                choice = answer["choice"]
                confidence = float(answer.get("confidence"))
                if not math.isfinite(confidence):
                    confidence = None  # keep --json output strict JSON
                probabilities = answer.get("probabilities")
                if isinstance(probabilities, dict):
                    probabilities = {k: (v if isinstance(v, (int, float)) and math.isfinite(v) else None)
                                     for k, v in probabilities.items()}
        except Exception:
            choice, confidence, probabilities = ERROR, None, None
        predicted = choice if choice in columns else ERROR
        confusion[task["expected"]][predicted] += 1
        # Pinned agents are still classified for raw accuracy, but production never
        # routes them, so they keep their own model.
        why = "pinned" if router.pinned(task, cfg) else router.verdict(choice, confidence, current, cfg)
        effective = choice if why in ("applied", "same model") else (
            router.model_tier(current, cfg["labels"]) or "inherit")
        result = {"id": task["id"], "expected": task["expected"], "got": predicted,
                  "confidence": confidence, "probabilities": probabilities,
                  "correct": predicted == task["expected"], "applied": why == "applied",
                  "effective": effective, "applied_correct": effective == task["expected"]}
        results.append(result)
        if not result["correct"]:
            misses.append({"id": task["id"], "expected": task["expected"], "got": predicted,
                           "conf": confidence})
    correct = sum(1 for r in results if r["correct"])
    applied_correct = sum(1 for r in results if r["applied_correct"])
    recall = {}
    for tier in tiers:
        total = sum(confusion[tier].values())
        recall[tier] = round(confusion[tier][tier] / total, 4) if total else None
    confidences = [r["confidence"] for r in results if r["confidence"] is not None]
    return {
        "total": len(results), "correct": correct, "errors": sum(1 for r in results if r["got"] == ERROR),
        "accuracy": round(correct / len(results), 4) if results else None,
        "applied": sum(1 for r in results if r["applied"]), "applied_correct": applied_correct,
        "applied_accuracy": round(applied_correct / len(results), 4) if results else None,
        "labels": columns, "confusion": confusion, "recall": recall,
        "mean_confidence": {
            "overall": mean(confidences),
            "correct": mean([r["confidence"] for r in results if r["correct"] and r["confidence"] is not None]),
            "wrong": mean([r["confidence"] for r in results if not r["correct"] and r["confidence"] is not None]),
        },
        "misses": misses, "results": results,
    }


def fmt(value, pct=False):
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%" if pct else f"{value:.3f}"


def render(report):
    cols = report["labels"]
    width = max(len(c) for c in cols + ["expected"]) + 2
    lines = [f"accuracy: {fmt(report['accuracy'], True)} ({report['correct']}/{report['total']}, "
             f"{report['errors']} errors)",
             f"applied accuracy: {fmt(report['applied_accuracy'], True)} "
             f"({report['applied_correct']}/{report['total']}, {report['applied']} applied)",
             "confusion (rows expected, cols predicted):",
             "  " + "expected".ljust(width) + "".join(c.rjust(width) for c in cols)]
    for row, counts in report["confusion"].items():
        lines.append("  " + row.ljust(width) + "".join(str(counts[c]).rjust(width) for c in cols))
    lines.append("recall: " + ", ".join(f"{k}={fmt(v, True)}" for k, v in report["recall"].items()))
    conf = report["mean_confidence"]
    lines.append(f"mean confidence: overall {fmt(conf['overall'])}, correct {fmt(conf['correct'])}, "
                 f"wrong {fmt(conf['wrong'])}")
    if report["misses"]:
        lines.append("misses:")
        for miss in report["misses"]:
            lines.append(f"  {miss['id']}: expected {miss['expected']}, got {miss['got']} "
                         f"(conf {fmt(miss['conf'])})")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate Jev routing against a labelled benchmark.")
    parser.add_argument("--benchmark", default=str(BENCHMARK_PATH))
    parser.add_argument("--labels-file", help="JSON {tier: rubric} overriding the configured labels")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="relay config.json with the jev key")
    parser.add_argument("--dry-run", action="store_true", help="validate the benchmark only; no network")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.benchmark).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"cannot read benchmark {args.benchmark}: {exc}", file=sys.stderr)
        return 1
    problems = validate_benchmark(data)
    if problems:
        print("invalid benchmark:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    tasks = data["tasks"]
    labels = None
    if args.labels_file:
        try:
            labels = load_labels(args.labels_file)
        except Exception as exc:
            print(f"invalid labels file {args.labels_file}: {exc}", file=sys.stderr)
            return 1
    if args.dry_run:
        by_tier = {}
        for task in tasks:
            by_tier[task["expected"]] = by_tier.get(task["expected"], 0) + 1
        summary = {"tasks": len(tasks), "expected": dict(sorted(by_tier.items())), "valid": True}
        print(json.dumps(summary) if args.json else
              f"benchmark ok: {len(tasks)} tasks ("
              + ", ".join(f"{k}={v}" for k, v in summary["expected"].items()) + ")")
        return 0
    cfg = eval_config(load_config(args.config), labels)
    if not api_key(cfg):
        print("no API key: set TYPESAFE_API_KEY or jev.api_key_file", file=sys.stderr)
        return 2
    report = evaluate(tasks, cfg)
    print(json.dumps(report, sort_keys=True, allow_nan=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

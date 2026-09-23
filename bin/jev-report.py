#!/usr/bin/env python3
"""Summarize the Jev decision log (relay/jev-log.jsonl).

Reads the rotated copy (`jev-log.jsonl.1`) first, then the current file, and
prints a compact per-feature report: routing choices and confidence spread,
report-check weak rates per effective model tier, and decision counts and
latency for shift, risk_gate and handoff_grade. Entries without a "feature"
key are legacy route entries. Malformed lines and missing fields are skipped.

Usage: jev-report.py [--log PATH] [--json]
"""
import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "relay" / "jev-log.jsonl"
FEATURES = ("route", "shift", "risk_gate", "report_check", "handoff_grade")
CONF_BUCKETS = (("<0.5", 0.0, 0.5), ("0.5-0.7", 0.5, 0.7), ("0.7-0.9", 0.7, 0.9), (">=0.9", 0.9, None))
SATURATION_SHARE = 0.9
SATURATION_LEVEL = 0.99
SATURATION_WARNING = "confidence looks saturated; run bin/eval-jev-routing.py"
UNKNOWN_TIER = "inherit/unknown"


def log_paths(path):
    path = Path(path)
    return [path.with_name(path.name + ".1"), path]


def load_entries(paths):
    """Parse JSON-object lines from each existing path, in order; skip anything malformed."""
    entries = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def feature_of(entry):
    feature = entry.get("feature", "route")
    return feature if feature in FEATURES else None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def percentile(values, pct):
    """Nearest-rank percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def latency(entries):
    values = [v for v in (number(e.get("latency_ms")) for e in entries) if v is not None]
    return {"count": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def rate(part, whole):
    return round(part / whole, 4) if whole else None


def counts(values):
    result = {}
    for value in values:
        key = str(value) if value is not None else "unknown"
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items()))


def summarize_route(entries):
    applied = sum(1 for e in entries if e.get("applied") is True)
    choices = {}
    for entry in entries:
        agent = str(entry.get("subagent_type") or "unknown")
        choice = str(entry.get("choice") or "unknown")
        per_agent = choices.setdefault(agent, {})
        per_agent[choice] = per_agent.get(choice, 0) + 1
    confidences = [c for c in (number(e.get("confidence")) for e in entries) if c is not None]
    histogram = {name: 0 for name, _, _ in CONF_BUCKETS}
    for value in confidences:
        for name, low, high in CONF_BUCKETS:
            if value >= low and (high is None or value < high):
                histogram[name] += 1
                break
        else:
            histogram[CONF_BUCKETS[0][0]] += 1  # negative confidences land in the lowest bucket
    saturated = sum(1 for c in confidences if c >= SATURATION_LEVEL)
    warning = (SATURATION_WARNING
               if confidences and saturated >= SATURATION_SHARE * len(confidences) else None)
    return {"total": len(entries), "applied": applied, "applied_rate": rate(applied, len(entries)),
            "choices": {k: dict(sorted(v.items())) for k, v in sorted(choices.items())},
            "confidence_histogram": histogram, "latency_ms": latency(entries), "warning": warning}


def join_key(entry):
    """Prefer desc_hash; fall back to legacy description text for older log lines."""
    desc_hash = entry.get("desc_hash")
    if isinstance(desc_hash, str) and desc_hash:
        return ("hash", desc_hash)
    description = entry.get("description")
    if isinstance(description, str) and description:
        return ("desc", description)
    return None


def effective_tiers(entries):
    """Map id(report_check entry) -> tier, joining on the latest earlier applied route.

    Entries are processed in log order (rotation first, then current), which is
    append order, so "latest seen so far" is the most recent earlier entry. The
    join key is desc_hash, falling back to the legacy "description" text when
    either side is an older log line without a hash.
    """
    last_applied = {}
    tiers = {}
    for entry in entries:
        feature = feature_of(entry)
        if feature == "route" and entry.get("applied") is True and entry.get("choice"):
            key = join_key(entry)
            if key is not None:
                last_applied[key] = str(entry["choice"])
        elif feature == "report_check":
            model = entry.get("model")
            if isinstance(model, str) and model:
                tiers[id(entry)] = model
            else:
                key = join_key(entry)
                tiers[id(entry)] = last_applied.get(key, UNKNOWN_TIER) if key is not None else UNKNOWN_TIER
    return tiers


def summarize_report_check(entries, tiers):
    weak = sum(1 for e in entries if e.get("weak") is True)
    per_tier = {}
    for entry in entries:
        bucket = per_tier.setdefault(tiers.get(id(entry), UNKNOWN_TIER), {"total": 0, "weak": 0})
        bucket["total"] += 1
        bucket["weak"] += 1 if entry.get("weak") is True else 0
    for bucket in per_tier.values():
        bucket["weak_rate"] = rate(bucket["weak"], bucket["total"])
    return {"total": len(entries), "weak": weak, "weak_rate": rate(weak, len(entries)),
            "per_tier": dict(sorted(per_tier.items())), "latency_ms": latency(entries)}


def summarize(entries):
    by_feature = {name: [] for name in FEATURES}
    for entry in entries:
        feature = feature_of(entry)
        if feature:
            by_feature[feature].append(entry)
    summary = {"entries": sum(len(v) for v in by_feature.values()),
               "route": summarize_route(by_feature["route"]),
               "report_check": summarize_report_check(by_feature["report_check"], effective_tiers(entries))}
    for name in ("shift", "risk_gate"):
        group = by_feature[name]
        summary[name] = {"total": len(group), "decisions": counts(e.get("decision") for e in group),
                         "latency_ms": latency(group)}
    grades = by_feature["handoff_grade"]
    scores = [s for s in (number(e.get("score")) for e in grades) if s is not None]
    summary["handoff_grade"] = {
        "total": len(grades), "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
        "weak": sum(1 for e in grades if e.get("weak") is True),
        "accepted_weak": sum(1 for e in grades if e.get("accepted_weak") is True),
        "latency_ms": latency(grades)}
    return summary


def fmt_rate(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fmt_latency(lat):
    if not lat or not lat.get("count"):
        return "latency n/a"
    return f"latency p50 {lat['p50']:.0f}ms p95 {lat['p95']:.0f}ms (n={lat['count']})"


def fmt_counts(mapping):
    return ", ".join(f"{k}={v}" for k, v in mapping.items()) or "none"


def render(summary):
    lines = [f"jev log: {summary['entries']} entries"]
    route = summary["route"]
    lines.append(f"route: {route['total']} total, applied {fmt_rate(route['applied_rate'])}, "
                 f"{fmt_latency(route['latency_ms'])}")
    for agent, choices in route["choices"].items():
        lines.append(f"  {agent}: {fmt_counts(choices)}")
    lines.append(f"  confidence: {fmt_counts(route['confidence_histogram'])}")
    if route["warning"]:
        lines.append(f"  WARNING: {route['warning']}")
    check = summary["report_check"]
    lines.append(f"report_check: {check['total']} total, weak {fmt_rate(check['weak_rate'])}, "
                 f"{fmt_latency(check['latency_ms'])}")
    for tier, bucket in check["per_tier"].items():
        lines.append(f"  {tier}: weak {bucket['weak']}/{bucket['total']} ({fmt_rate(bucket['weak_rate'])})")
    for name in ("shift", "risk_gate"):
        part = summary[name]
        lines.append(f"{name}: {part['total']} total, {fmt_counts(part['decisions'])}, "
                     f"{fmt_latency(part['latency_ms'])}")
    grade = summary["handoff_grade"]
    mean = "n/a" if grade["score_mean"] is None else f"{grade['score_mean']:.2f}"
    lines.append(f"handoff_grade: {grade['total']} total, score mean {mean}, weak {grade['weak']} "
                 f"(accepted {grade['accepted_weak']}), {fmt_latency(grade['latency_ms'])}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarize the Jev decision log.")
    parser.add_argument("--log", default=str(LOG_PATH), help="log path (default: relay/jev-log.jsonl)")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    args = parser.parse_args(argv)
    entries = load_entries(log_paths(args.log))
    summary = summarize(entries)
    if not summary["entries"]:
        message = f"no jev log entries at {args.log}"
        print(json.dumps({"entries": 0, "message": message}) if args.json else message)
        return 0
    print(json.dumps(summary, sort_keys=True) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

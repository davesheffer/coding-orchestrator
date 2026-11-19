#!/usr/bin/env python3
"""Token usage and API-equivalent cost of local Claude Code transcripts, per role and model.

Reads `<projects-root>/<project>/*.jsonl` (main sessions) and
`<project>/<session>/subagents/**/agent-*.jsonl` (subagent and workflow-agent runs, role and
launched-with model from the sibling `agent-*.meta.json`). Each assistant message counts once per message id (streamed chunks
repeat their usage). Prints tokens and an API-equivalent dollar figure per role and model, each
role's share, runs per session, and each subagent's launched-with vs actual model.

The dollar figure weights tokens by list prices (PRICES below); it is not a subscription bill.
A cache write is priced by its TTL from `usage.cache_creation` (5-minute writes at
CACHE_WRITE_5M x input, 1-hour writes at CACHE_WRITE_1H x input); writes without that breakdown
count as 1-hour. A model whose family is not in PRICES counts as $0 and is listed under
"unpriced". Nothing here prints prompt or message text.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time

# USD per million tokens: (input, output, cache read). Cache writes use CACHE_WRITE_MULTIPLIER.
PRICES = {
    "opus": (4.00, 20.00, 0.20),
    "fable": (8.00, 40.00, 0.20),
    "sonnet": (2.00, 10.00, 0.10),
    "haiku": (0.10, 0.50, 0.01),
}
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0
FIELDS = ("input", "cache_write", "cache_read", "output")
USAGE_KEYS = {"input": "input_tokens", "cache_write": "cache_creation_input_tokens",
              "cache_read": "cache_read_input_tokens", "output": "output_tokens"}


def family(model):
    """The PRICES family named in a model id (claude-opus-5-5 -> opus), else None."""
    lowered = str(model).lower()
    return next((name for name in PRICES if name in lowered), None)


def cost(model, usage):
    """API-equivalent USD for one message's usage dict, or None when the model family is unpriced.

    Cache writes use the `cache_creation` TTL split when present; the rest of
    cache_creation_input_tokens is priced as 1-hour writes.
    """
    name = family(model)
    if name is None:
        return None
    price_in, price_out, price_read = PRICES[name]
    write = count(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    short = min(count(split.get("ephemeral_5m_input_tokens")), write)
    return (count(usage.get("input_tokens")) * price_in
            + short * price_in * CACHE_WRITE_5M + (write - short) * price_in * CACHE_WRITE_1H
            + count(usage.get("cache_read_input_tokens")) * price_read
            + count(usage.get("output_tokens")) * price_out) / 1e6


def count(value):
    """A usage field as a non-negative int; anything else counts as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return max(int(value), 0)


def record_time(record):
    """The record's timestamp as epoch seconds, or None when absent or unparseable."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def read_meta(path):
    """(agentType, requested model) from an agent-*.meta.json, ("unknown", None) when unreadable."""
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown", None
    if not isinstance(meta, dict):
        return "unknown", None
    role = meta.get("agentType")
    model = meta.get("model")
    return (role if isinstance(role, str) and role else "unknown",
            model if isinstance(model, str) and model else None)


class Usage:
    def __init__(self, cutoff):
        self.cutoff = cutoff
        self.seen = set()
        self.tokens = defaultdict(lambda: {**dict.fromkeys(FIELDS + ("turns",), 0), "usd": 0.0})  # (role, model)
        self.unpriced = set()
        self.runs = defaultdict(int)  # role -> transcripts with at least one counted message
        self.per_session = defaultdict(lambda: defaultdict(int))  # role -> session -> runs
        self.requested = defaultdict(int)  # (role, requested, actual) -> runs

    def scan(self, path, role, session, requested=None):
        models = set()
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                model = message.get("model")
                if not isinstance(model, str) or not model or model == "<synthetic>":
                    continue
                when = record_time(record)
                if when is not None and when < self.cutoff:
                    continue
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id:
                    if message_id in self.seen:
                        continue
                    self.seen.add(message_id)
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                row = self.tokens[(role, model)]
                for field in FIELDS:
                    row[field] += count(usage.get(USAGE_KEYS[field]))
                row["turns"] += 1
                usd = cost(model, usage)
                if usd is None:
                    self.unpriced.add(model)
                else:
                    row["usd"] += usd
                models.add(model)
        if models:
            self.runs[role] += 1
            self.per_session[role][session] += 1
            if role != "main":
                for model in models:
                    self.requested[(role, requested or "default", model)] += 1

    def scan_project(self, project):
        for path in sorted(project.glob("*.jsonl")):
            if self.fresh(path):
                self.scan(path, "main", path.stem)
        # Workflow agents sit deeper (<session>/subagents/workflows/<run>/agent-*.jsonl).
        for path in sorted(project.glob("*/subagents/**/agent-*.jsonl")):
            if self.fresh(path):
                role, requested = read_meta(path.with_name(path.stem + ".meta.json"))
                self.scan(path, role, path.relative_to(project).parts[0], requested)

    def fresh(self, path):
        try:
            return path.stat().st_mtime >= self.cutoff
        except OSError:
            return False


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)) - 1, 0)] if ordered else None


def summarize(usage, days):
    rows, unpriced = [], usage.unpriced
    for (role, model), tokens in usage.tokens.items():
        rows.append({"role": role, "model": model, **tokens, "usd": round(tokens["usd"], 4)})
    rows.sort(key=lambda r: (-r["usd"], r["role"], r["model"]))
    total = sum(r["usd"] for r in rows)
    roles = defaultdict(float)
    for row in rows:
        roles[row["role"]] += row["usd"]
    sessions = {}
    for role, counts in usage.per_session.items():
        values = list(counts.values())
        sessions[role] = {"sessions": len(values), "median": percentile(values, 0.5),
                          "p90": percentile(values, 0.9), "max": max(values)}
    return {
        "days": days, "total_usd": round(total, 2),
        "assumptions": {"prices_usd_per_million": {k: dict(zip(("input", "output", "cache_read"), v))
                                                   for k, v in PRICES.items()},
                        "cache_write_multiplier": {"5m": CACHE_WRITE_5M, "1h": CACHE_WRITE_1H},
                        "note": "API list-price weights, not a subscription bill"},
        "rows": rows,
        "roles": [{"role": role, "usd": round(usd, 2), "share": round(usd / total, 4) if total else 0.0,
                   "runs": usage.runs.get(role, 0)}
                  for role, usd in sorted(roles.items(), key=lambda kv: (-kv[1], kv[0]))],
        "runs_per_session": dict(sorted(sessions.items())),
        "launched_models": [{"role": r, "launched_with": q, "actual": a, "runs": n}
                             for (r, q, a), n in sorted(usage.requested.items(),
                                                        key=lambda kv: (-kv[1], kv[0]))],
        "unpriced": sorted(unpriced),
    }


def render(summary):
    lines = [f"window {summary['days']}d, API-equivalent total ${summary['total_usd']:,.2f} "
             "(list prices, cache writes 1.25x input for 5m and 2x for 1h; not a subscription bill)", ""]
    lines.append(f"{'role':18}{'share':>7}{'usd':>11}{'runs':>7}")
    for role in summary["roles"]:
        lines.append(f"{role['role'][:17]:18}{role['share']:>7.1%}{role['usd']:>11,.2f}{role['runs']:>7}")
    lines += ["", f"{'role':18}{'model':28}{'turns':>7}{'input':>11}{'cache_w':>13}{'cache_r':>15}"
                  f"{'output':>11}{'usd':>11}"]
    for row in summary["rows"]:
        lines.append(f"{row['role'][:17]:18}{row['model'][:27]:28}{row['turns']:>7}{row['input']:>11}"
                     f"{row['cache_write']:>13}{row['cache_read']:>15}{row['output']:>11}{row['usd']:>11,.2f}")
    lines += ["", "runs per session (sessions, median, p90, max):"]
    for role, stats in summary["runs_per_session"].items():
        lines.append(f"  {role[:17]:18}{stats['sessions']:>5}{stats['median']:>5}{stats['p90']:>5}{stats['max']:>5}")
    if summary["launched_models"]:
        lines += ["", "subagent launched-with model (meta.json, after hooks) -> actual model (runs):"]
        for item in summary["launched_models"]:
            lines.append(f"  {item['role'][:17]:18}{item['launched_with'][:12]:13}-> {item['actual'][:27]:28}{item['runs']:>5}")
    if summary["unpriced"]:
        lines += ["", "unpriced models (counted as $0): " + ", ".join(summary["unpriced"])]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--projects-root", type=Path, default=Path.home() / ".claude" / "projects",
                        help="Claude Code projects directory (default: ~/.claude/projects)")
    parser.add_argument("--project", action="append", default=[],
                        help="project directory name under the root; repeat for several (default: all)")
    parser.add_argument("--days", type=float, default=30.0, help="window in days (default: 30)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    if not math.isfinite(args.days) or args.days <= 0:
        parser.error("--days must be a positive number")
    root = args.projects_root.expanduser()
    if args.project:
        projects = [root / name for name in args.project]
        missing = [str(p) for p in projects if not p.is_dir()]
        if missing:
            parser.error("no such project directory: " + ", ".join(missing))
    elif root.is_dir():
        projects = sorted(p for p in root.iterdir() if p.is_dir())
    else:
        parser.error(f"no such projects root: {root}")
    usage = Usage(time.time() - args.days * 86400)
    for project in projects:
        usage.scan_project(project)
    summary = summarize(usage, args.days if not float(args.days).is_integer() else int(args.days))
    sys.stdout.write((json.dumps(summary, indent=2) if args.json else render(summary)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

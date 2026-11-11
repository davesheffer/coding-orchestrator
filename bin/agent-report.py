#!/usr/bin/env python3
"""Summarize local agent-run reports without exposing task text or event logs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import statistics
import sys

TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens')
MAX_NUMBER = 1e15


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def event_usage(path):
    """A CLI turn's last completion carries its cumulative usage."""
    if not path.is_file():
        return None
    usage = None
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if (isinstance(event, dict) and event.get('type') == 'turn.completed'
                    and isinstance(event.get('usage'), dict)):
                usage = event['usage']
    return {key: usage.get(key, 0) for key in TOKEN_FIELDS} if usage is not None else None


def resolved(value):
    """An over-long or invalid path resolves to None instead of raising."""
    try:
        return Path(value).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def collect(root, workspace=None):
    reports = []
    unreadable = 0
    target = resolved(workspace) if workspace else None
    for path in sorted(root.glob('*/report.json')):
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            unreadable += 1
            continue
        if not isinstance(report, dict):
            unreadable += 1
            continue
        # A report without an absolute workspace must not resolve to the current
        # directory; a bare "" or "." would otherwise match any --workspace filter.
        value = report.get('workspace')
        if workspace and (not isinstance(value, str) or not Path(value).is_absolute()
                          or target is None or resolved(value) != target):
            continue
        # "attempts": null, or an attempt whose "observed" is a string, is a damaged
        # report: count it as unreadable instead of failing the whole summary.
        attempts = report.get('attempts', [])
        if not isinstance(attempts, list) or any(
                isinstance(attempt, dict) and not isinstance(attempt.get('observed'), (dict, type(None)))
                for attempt in attempts):
            unreadable += 1
            continue
        reports.append((path.parent, report))
    return reports, unreadable


def measured(value):
    """A finite, non-negative, non-bool number no larger than 1e15. A huge integer, NaN,
    Infinity or true would break the median or make --json emit non-strict JSON."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= MAX_NUMBER)


def attempt_rows(directory, report):
    """(model, duration_ms, usage) per attempt, or None when the report carries a number
    that is present but not measured; absent (null) numbers stay unknown."""
    if report.get('preflight_ms') is not None and not measured(report['preflight_ms']):
        return None
    rows = []
    for index, attempt in enumerate(report.get('attempts', [])):
        if not isinstance(attempt, dict):
            continue
        model = attempt.get('observed') or {}
        duration = attempt.get('duration_ms')
        usage = attempt.get('usage') if isinstance(attempt.get('usage'), dict) else None
        if usage is None:
            usage = event_usage(directory / f'{index}-events.jsonl')
        if usage is not None:
            usage = {key: usage.get(key) for key in TOKEN_FIELDS}
        if any(value is not None and not measured(value)
               for value in (duration, *(usage or {}).values())):
            return None
        if usage is not None:
            usage = {key: value or 0 for key, value in usage.items()}
        rows.append((str(model.get('model') or attempt.get('requested_model') or 'unknown'),
                     duration, usage))
    return rows


def summarize(root, workspace=None):
    reports, unreadable = collect(root, workspace)
    statuses = Counter()
    roles = Counter()
    models = Counter()
    preflights = []
    attempts_ms = []
    tokens = Counter()
    attempts = 0
    usage_known = 0
    runs = 0
    network_exceptions = 0
    per_role = defaultdict(lambda: {'runs': 0, 'completed': 0, 'attempts': 0,
                                    'input_tokens': 0, 'output_tokens': 0})
    for directory, report in reports:
        rows = attempt_rows(directory, report)
        if rows is None:
            unreadable += 1
            continue
        runs += 1
        role = str(report.get('role', 'unknown'))
        status = str(report.get('status', 'unknown'))
        statuses[status] += 1
        roles[role] += 1
        per_role[role]['runs'] += 1
        per_role[role]['completed'] += status == 'completed'
        network_exceptions += report.get('network_exception') is True
        if report.get('preflight_ms') is not None:
            preflights.append(report['preflight_ms'])
        for model, duration, usage in rows:
            attempts += 1
            per_role[role]['attempts'] += 1
            models[model] += 1
            if duration is not None:
                attempts_ms.append(duration)
            if usage is not None:
                usage_known += 1
                tokens.update(usage)
                per_role[role]['input_tokens'] += usage['input_tokens']
                per_role[role]['output_tokens'] += usage['output_tokens']
    return {
        'runs': runs, 'unreadable_reports': unreadable,
        'statuses': dict(sorted(statuses.items())), 'roles': dict(sorted(roles.items())),
        'models': dict(sorted(models.items())), 'network_exception_runs': network_exceptions,
        'attempts': attempts, 'attempts_with_usage': usage_known,
        'token_totals': {key: tokens[key] for key in TOKEN_FIELDS},
        'preflight_ms': {'samples': len(preflights), 'median': statistics.median(preflights) if preflights else None,
                         'p95': percentile(preflights, .95)},
        'attempt_ms': {'samples': len(attempts_ms), 'median': statistics.median(attempts_ms) if attempts_ms else None,
                       'p95': percentile(attempts_ms, .95)},
        'per_role': dict(sorted(per_role.items())),
    }


def main(argv=None):
    # A piped stdout uses the console code page, which cannot encode every status or model.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path,
                        default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'agent-runs')
    parser.add_argument('--workspace', type=Path, help='Only reports for this resolved workspace')
    parser.add_argument('--json', action='store_true', help='Print machine-readable aggregate')
    args = parser.parse_args(argv)
    if not args.runs.is_dir():
        parser.error(f'run directory does not exist: {args.runs}')
    workspace = None
    if args.workspace:
        workspace = resolved(args.workspace)
        if workspace is None:
            parser.error('workspace path cannot be resolved')
    result = summarize(args.runs, workspace)
    if args.json:
        print(json.dumps(result, indent=2, allow_nan=False))
    else:
        print(f"Runs: {result['runs']} ({result['attempts']} attempts; {result['attempts_with_usage']} with usage)")
        print(f"Status: {result['statuses']}")
        print(f"Models: {result['models']}")
        print(f"Network exception runs: {result['network_exception_runs']}")
        print(f"Preflight ms: {result['preflight_ms']}; attempt ms: {result['attempt_ms']}")
        print(f"Tokens: {result['token_totals']}")
        print('CLI completion is not task correctness; absent duration samples stay unknown.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

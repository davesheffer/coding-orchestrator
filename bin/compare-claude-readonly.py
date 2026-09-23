#!/usr/bin/env python3
"""Run bounded read-only Claude lookup comparisons and retain reviewable results."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


def executable():
    wrapper = shutil.which('claude')
    if not wrapper:
        raise RuntimeError('Claude Code is not installed on PATH')
    if sys.platform == 'win32':
        native = Path(wrapper).parent / 'node_modules/@anthropic-ai/claude-code/bin/claude.exe'
        if native.is_file():
            return str(native)
    return wrapper


def run_one(repo, task, model, budget):
    command = [executable(), '-p', task['prompt'], '--model', model, '--output-format', 'json',
               '--max-budget-usd', str(budget), '--no-session-persistence', '--restricted',
               '--strict-mcp-config', '--setting-sources', '', '--tools', 'Read,Grep,Glob']
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=180)
    except subprocess.TimeoutExpired:
        return {'task': task['id'], 'model': model, 'exit': 124, 'error': 'timeout'}
    record = {'task': task['id'], 'model': model, 'exit': result.returncode,
              'wall_ms': round((time.monotonic() - started) * 1000)}
    if result.returncode:
        record['error'] = result.stderr[-1000:]
        return record
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        record['error'] = 'non-JSON CLI output'
        return record
    answer = output.get('result', '')
    record.update(actual_model=next(iter(output.get('modelUsage', {})), None),
                  cost_usd=output.get('total_cost_usd'),
                  duration_ms=output.get('duration_ms'),
                  turns=output.get('num_turns'),
                  required_hits={term: term.casefold() in answer.casefold()
                                 for term in task['required']},
                  answer=answer)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--tasks', type=Path, default=Path(__file__).resolve().parents[1] /
                        'benchmarks/claude-readonly.json')
    parser.add_argument('--models', nargs='+', default=['haiku', 'sonnet'])
    parser.add_argument('--budget', type=float, default=.20, help='USD maximum per task/model run')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True, help='Private JSON results file')
    args = parser.parse_args(argv)
    tasks = json.loads(args.tasks.read_text(encoding='utf-8'))
    if args.budget <= 0 or not 1 <= args.jobs <= 4:
        parser.error('budget must be positive and jobs must be 1-4')
    if args.output.exists():
        parser.error('output already exists; use a new file to preserve prior evidence')
    work = [(task, model) for task in tasks for model in args.models]
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_one, args.repo, task, model, args.budget)
                   for task, model in work]
        for future in as_completed(futures):
            record = future.result()
            results.append(record)
            print(f"{record['task']} {record['model']}: exit={record['exit']} "
                  f"cost={record.get('cost_usd')} hits={sum(record.get('required_hits', {}).values())}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
    return 0 if all(r['exit'] == 0 for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())

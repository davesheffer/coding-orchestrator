#!/usr/bin/env python3
"""Compare configured Codex scout models under identical launcher restrictions."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys


def run_one(repo, task, model):
    launcher = Path(__file__).with_name('agent-run.py')
    result = subprocess.run([sys.executable, str(launcher), 'scout', '--cd', str(repo),
                             '--trial-model', model], input=task['prompt'],
                            cwd=repo, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=180)
    record = {'task': task['id'], 'model': model, 'exit': result.returncode}
    log_line = next((line for line in result.stdout.splitlines()
                     if line.startswith('Agent run: ')), None)
    if not log_line:
        record['error'] = (result.stderr or result.stdout)[-1000:]
        return record
    directory = Path(log_line.removeprefix('Agent run: '))
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    record.update(status=report.get('status'), preflight_ms=report.get('preflight_ms'),
                  network_exception=report.get('network_exception'))
    attempts = report.get('attempts', [])
    if attempts:
        attempt = attempts[-1]
        record.update(actual_model=(attempt.get('observed') or {}).get('model'),
                      duration_ms=attempt.get('duration_ms'), usage=attempt.get('usage'))
        answer_path = directory / f'{len(attempts) - 1}-final.txt'
        if answer_path.is_file():
            answer = answer_path.read_text(encoding='utf-8')
            record['answer'] = answer
            record['required_hits'] = {term: term.casefold() in answer.casefold()
                                       for term in task['required']}
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parents[1]
    parser.add_argument('--repo', type=Path, default=repo)
    parser.add_argument('--tasks', type=Path, default=repo / 'benchmarks/claude-readonly.json')
    parser.add_argument('--models', nargs='+', default=['gpt-6-luna', 'gpt-6-sol'])
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--output', required=True, type=Path, help='Private JSON results file')
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 4:
        parser.error('jobs must be 1-4')
    if args.output.exists():
        parser.error('output already exists; use a new file')
    tasks = json.loads(args.tasks.read_text(encoding='utf-8'))
    work = [(task, model) for task in tasks for model in args.models]
    records = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_one, args.repo.resolve(), task, model)
                   for task, model in work]
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            print(f"{record['task']} {record['model']}: exit={record['exit']} "
                  f"status={record.get('status')} hits={sum(record.get('required_hits', {}).values())}",
                  flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, indent=2), encoding='utf-8')
    return 0 if all(r['exit'] == 0 for r in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())

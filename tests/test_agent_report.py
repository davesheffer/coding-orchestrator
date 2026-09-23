"""Offline tests for aggregate-only launcher telemetry."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).resolve().parents[1] / 'bin' / 'agent-report.py'
spec = importlib.util.spec_from_file_location('agent_report', path)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


class AgentReportTests(unittest.TestCase):
    def test_aggregate_uses_last_turn_usage_and_omits_task_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / 'run-one'
            run.mkdir()
            (run / 'report.json').write_text(json.dumps({
                'role': 'scout', 'workspace': 'C:/repo', 'status': 'completed',
                'network_exception': True, 'preflight_ms': 12,
                'attempts': [{'requested_model': 'gpt-6-luna', 'observed': {'model': 'gpt-6-luna'},
                              'duration_ms': 34}]}), encoding='utf-8')
            events = [{'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 1}},
                      {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}},
                      {'type': 'item.completed', 'item': {'text': 'private task content'}}]
            (run / '0-events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
            result = reporter.summarize(root, Path('C:/repo'))
            self.assertEqual(result['runs'], 1)
            self.assertEqual(result['token_totals']['input_tokens'], 5)
            self.assertEqual(result['preflight_ms']['median'], 12)
            self.assertEqual(result['attempt_ms']['p95'], 34)
            self.assertEqual(result['network_exception_runs'], 1)
            self.assertNotIn('private task content', json.dumps(result))

    def test_missing_usage_and_bad_report_remain_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bad').mkdir()
            (root / 'bad/report.json').write_text('{', encoding='utf-8')
            (root / 'good').mkdir()
            (root / 'good/report.json').write_text(json.dumps({
                'role': 'runner', 'status': 'failed-no-retry', 'attempts': [{}]}), encoding='utf-8')
            result = reporter.summarize(root)
            self.assertEqual(result['unreadable_reports'], 1)
            self.assertEqual(result['attempts_with_usage'], 0)
            self.assertIsNone(result['attempt_ms']['median'])


if __name__ == '__main__':
    unittest.main()

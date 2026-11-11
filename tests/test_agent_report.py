"""Offline tests for aggregate-only launcher telemetry."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

REPO = os.path.abspath(os.sep + 'repo')

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
                'role': 'scout', 'workspace': REPO, 'status': 'completed',
                'network_exception': True, 'preflight_ms': 12,
                'attempts': [{'requested_model': 'gpt-6-luna', 'observed': {'model': 'gpt-6-luna'},
                              'duration_ms': 34}]}), encoding='utf-8')
            events = [{'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 1}},
                      {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2}},
                      {'type': 'item.completed', 'item': {'text': 'private task content'}}]
            (run / '0-events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
            result = reporter.summarize(root, Path(REPO))
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

    def test_workspace_filter_skips_reports_without_a_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'run').mkdir()
            (root / 'run/report.json').write_text(json.dumps({'role': 'scout', 'status': 'preflight'}),
                                                  encoding='utf-8')
            self.assertEqual(reporter.summarize(root, Path('.'))['runs'], 0)
            self.assertEqual(reporter.summarize(root)['runs'], 1)

    def test_workspace_filter_skips_non_absolute_workspace_values(self):
        # "" and "." resolve to the current directory; they must not masquerade as a match.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in (('blank', ''), ('dot', '.')):
                (root / name).mkdir()
                (root / name / 'report.json').write_text(
                    json.dumps({'role': 'scout', 'status': 'preflight', 'workspace': value}), encoding='utf-8')
            self.assertEqual(reporter.summarize(root, Path.cwd())['runs'], 0)
            self.assertEqual(reporter.summarize(root)['runs'], 2)

    def test_an_event_line_with_a_huge_integer_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            events = Path(temp) / 'events.jsonl'
            good = {'type': 'turn.completed', 'usage': {'input_tokens': 3}}
            events.write_text('{"n": ' + '9' * 5000 + '}\n' + json.dumps(good) + '\n', encoding='utf-8')
            self.assertEqual(reporter.event_usage(events)['input_tokens'], 3)

    def test_invalid_utf8_event_log_still_reports_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / 'run'
            run.mkdir()
            (run / 'report.json').write_text(json.dumps({'role': 'scout', 'status': 'completed',
                                                         'attempts': [{}]}), encoding='utf-8')
            (run / '0-events.jsonl').write_bytes(b'\xff\xfe\n' + json.dumps(
                {'type': 'turn.completed', 'usage': {'input_tokens': 7}}).encode() + b'\n')
            self.assertEqual(reporter.summarize(Path(temp))['token_totals']['input_tokens'], 7)

    def test_malformed_attempt_fields_count_as_an_unreadable_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            damaged = {'null-attempts': {'attempts': None}, 'text-attempts': {'attempts': 'two'},
                       'text-observed': {'attempts': [{'observed': 'gpt-6-luna'}]}}
            for name, fields in damaged.items():
                (root / name).mkdir()
                (root / name / 'report.json').write_text(
                    json.dumps({'role': 'scout', 'status': 'completed', **fields}), encoding='utf-8')
            (root / 'good').mkdir()
            (root / 'good/report.json').write_text(json.dumps({
                'role': 'scout', 'status': 'completed',
                'attempts': [{'observed': None, 'requested_model': 'gpt-6-luna'}, 'not an attempt']}),
                encoding='utf-8')
            result = reporter.summarize(root)
            self.assertEqual(result['unreadable_reports'], 3)
            self.assertEqual(result['runs'], 1)
            self.assertEqual(result['models'], {'gpt-6-luna': 1})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reporter.main(['--runs', str(root)]), 0)


    def test_deeply_nested_json_is_treated_as_malformed(self):
        nested = '[' * 100000 + ']' * 100000
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'deep').mkdir()
            (root / 'deep/report.json').write_text(nested, encoding='utf-8')
            run = root / 'run'
            run.mkdir()
            (run / 'report.json').write_text(json.dumps({'role': 'scout', 'status': 'completed',
                                                         'attempts': [{}]}), encoding='utf-8')
            (run / '0-events.jsonl').write_text(nested + '\n' + json.dumps(
                {'type': 'turn.completed', 'usage': {'input_tokens': 7}}) + '\n', encoding='utf-8')
            result = reporter.summarize(root)
            self.assertEqual(result['unreadable_reports'], 1)
            self.assertEqual(result['token_totals']['input_tokens'], 7)

    def test_unresolvable_workspace_paths_do_not_raise(self):
        long_path = Path(os.path.abspath(os.sep + 'a' * 40000))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'run').mkdir()
            (root / 'run/report.json').write_text(json.dumps(
                {'role': 'scout', 'status': 'preflight', 'workspace': str(long_path)}), encoding='utf-8')
            self.assertEqual(reporter.summarize(root, Path(REPO))['runs'], 0)
            self.assertEqual(reporter.summarize(root, long_path)['runs'], 0)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    reporter.main(['--runs', str(root), '--workspace', str(long_path)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('workspace path cannot be resolved', stderr.getvalue())

    def test_unmeasurable_numbers_count_as_an_unreadable_report(self):
        bad_values = {'huge': 10 ** 400, 'nan': float('nan'), 'inf': float('inf'), 'true': True,
                      'negative': -1}
        placements = {
            'preflight': lambda value: {'preflight_ms': value, 'attempts': []},
            'duration': lambda value: {'attempts': [{'duration_ms': value}]},
            'usage': lambda value: {'attempts': [{'usage': {'input_tokens': value}}]},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for value_name, value in bad_values.items():
                for field, fields in placements.items():
                    (root / f'{field}-{value_name}').mkdir()
                    # json.dumps writes NaN and Infinity, which json.loads reads back.
                    (root / f'{field}-{value_name}' / 'report.json').write_text(json.dumps(
                        {'role': 'scout', 'status': 'completed', **fields(value)}), encoding='utf-8')
            # A NaN in a CLI event log damages the report the same way.
            (root / 'events-nan').mkdir()
            (root / 'events-nan/report.json').write_text(json.dumps(
                {'role': 'scout', 'status': 'completed', 'attempts': [{}]}), encoding='utf-8')
            (root / 'events-nan/0-events.jsonl').write_text(
                '{"type": "turn.completed", "usage": {"output_tokens": NaN}}\n', encoding='utf-8')
            (root / 'good').mkdir()
            (root / 'good/report.json').write_text(json.dumps({
                'role': 'scout', 'status': 'completed', 'preflight_ms': 2.5,
                'attempts': [{'duration_ms': 2.5, 'usage': {'input_tokens': 4, 'output_tokens': None}}]}),
                encoding='utf-8')
            result = reporter.summarize(root)
            self.assertEqual(result['unreadable_reports'], len(bad_values) * len(placements) + 1)
            self.assertEqual(result['runs'], 1)
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(result['preflight_ms']['median'], 2.5)
            self.assertEqual(result['attempt_ms']['median'], 2.5)
            self.assertEqual(result['token_totals']['input_tokens'], 4)
            self.assertEqual(result['token_totals']['output_tokens'], 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(reporter.main(['--runs', str(root), '--json']), 0)

            def strict(constant):
                raise ValueError(f'non-strict JSON constant {constant}')
            self.assertEqual(json.loads(output.getvalue(), parse_constant=strict)['runs'], 1)

    def test_text_output_outside_the_console_code_page_prints_as_utf8(self):
        # A piped stdout on Windows is a code-page wrapper; "\u2192" is not in cp1252.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'run').mkdir()
            (root / 'run/report.json').write_text(json.dumps({
                'role': 'scout', 'status': 'done \u2192 ok',
                'attempts': [{'observed': {'model': 'm\u00f6del'}}]}), encoding='utf-8')
            raw = io.BytesIO()
            stream = io.TextIOWrapper(raw, encoding='cp1252', newline='\n')
            with mock.patch.object(reporter.sys, 'stdout', stream):
                self.assertEqual(reporter.main(['--runs', str(root)]), 0)
                stream.flush()
            printed = raw.getvalue().decode('utf-8')
            self.assertIn('done \u2192 ok', printed)
            self.assertIn('m\u00f6del', printed)


if __name__ == '__main__':
    unittest.main()

"""Exercise launcher control flow with fake Codex responses; no network/model calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'bin' / 'agent-run.py'
spec = importlib.util.spec_from_file_location('agent_run', path)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

GOOD = {'exit': 0, 'filesystem_ok': True, 'network': 'CONNECTED'}
ISOLATED = dict(GOOD, network='DENIED')
STARTUP_FAILURE = {'exit': 1, 'filesystem_ok': False, 'network': 'UNKNOWN', 'error': 'sandbox setup unavailable'}
LEAK = dict(GOOD, filesystem_ok=False)


class FlowTests(unittest.TestCase):
    def run_flow(self, responses, probes, approved=True, extra_args=()):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEST_TMPDIR')) as temp:
            root = Path(temp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (root / 'agents').mkdir()
            (root / 'agents/scout.toml').write_text('model="gpt-6-luna"\nmodel_reasoning_effort="low"\ndeveloper_instructions="Read-only scout"\n')
            (root / 'agent-routing.json').write_text(json.dumps({'network_fallback_roles': ['scout'] if approved else []}))
            brief = root / 'brief.txt'
            brief.write_text('Bounded local test task')
            calls = []

            def fake_exec(command, **kwargs):
                chosen = next(toml.split('=', 1)[1].strip('"') for toml in command if toml.startswith('model='))
                calls.append(chosen)
                code, stream = responses[len(calls) - 1]
                kwargs['stdout'].write(stream)
                Path(command[command.index('--output-last-message') + 1]).write_text('RESULT: test response')
                return subprocess.CompletedProcess(command, code)

            def observed(_):
                return {'model': calls[-1], 'effort': 'low'}

            with patch.object(agent, 'ROOT', root), \
                 patch.object(agent, 'executable', return_value='codex.exe'), \
                 patch.object(agent, 'config_paths', return_value=[]), \
                 patch.object(agent, 'probe', side_effect=probes) as probe, \
                 patch.object(agent, 'verify_tools', return_value={}), \
                 patch.object(agent, 'actual_model', side_effect=observed), \
                 patch.object(agent.subprocess, 'run', side_effect=fake_exec), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = agent.main(['scout', '--cd', str(workspace), '--brief', str(brief), *extra_args])
            report = json.loads(next((root / 'agent-runs').glob('*/report.json')).read_text())
            return code, calls, report, probe.call_count

    def test_model_unavailable_falls_back_once_to_sol(self):
        first = '{"type":"thread.started","thread_id":"first"}\n{"type":"error","message":"model_not_found"}\n'
        second = '{"type":"thread.started","thread_id":"second"}\n{"type":"turn.completed"}\n'
        code, calls, report, _ = self.run_flow([(1, first), (0, second)], [GOOD, GOOD, GOOD])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['gpt-6-luna', 'gpt-6-sol'])
        self.assertEqual(report['attempts'][1]['observed']['model'], 'gpt-6-sol')
        self.assertGreaterEqual(report['preflight_ms'], 0)
        self.assertGreaterEqual(report['attempts'][1]['duration_ms'], 0)

    def test_corrupt_partial_work_stops_without_retry(self):
        bad = '{"type":"item.started",\n{"type":"error","message":"model_not_found"}\n'
        code, calls, report, _ = self.run_flow([(1, bad)], [GOOD, GOOD, GOOD])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report['status'], 'failed-no-retry')

    def test_trial_model_uses_one_configured_candidate(self):
        stream = '{"type":"thread.started","thread_id":"trial"}\n{"type":"turn.completed"}\n'
        code, calls, report, _ = self.run_flow([(0, stream)], [ISOLATED],
                                                extra_args=('--trial-model', 'gpt-6-sol'))
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['gpt-6-sol'])
        self.assertEqual(report['trial_model'], 'gpt-6-sol')

    def test_empty_trial_model_is_rejected_before_launch(self):
        with self.assertRaisesRegex(RuntimeError, 'Trial model is not configured'):
            self.run_flow([], [], extra_args=('--trial-model', ''))

    def test_started_work_stops_without_retry(self):
        stream = '{"type":"item.started"}\n{"type":"error","message":"model_not_found"}\n'
        code, calls, _, _ = self.run_flow([(1, stream)], [ISOLATED])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)

    def test_startup_failure_tries_second_restricted_backend(self):
        stream = '{"type":"thread.started","thread_id":"second"}\n'
        code, calls, report, count = self.run_flow([(0, stream)], [STARTUP_FAILURE, ISOLATED])
        self.assertEqual(code, 0)
        self.assertEqual(count, 2)
        self.assertFalse(report['network_exception'])
        self.assertEqual(len(calls), 1)

    def test_permission_leak_does_not_retry_or_launch(self):
        code, calls, report, count = self.run_flow([], [LEAK])
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(count, 1)
        self.assertEqual(report['status'], 'blocked-filesystem')

    def test_no_backend_means_no_agent_even_with_approval(self):
        code, calls, report, _ = self.run_flow([], [STARTUP_FAILURE, STARTUP_FAILURE])
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'blocked-sandbox-startup')

    def test_revoked_approval_blocks_network_fallback(self):
        code, calls, report, count = self.run_flow([], [GOOD, GOOD], approved=False)
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(count, 2)
        self.assertEqual(report['status'], 'blocked-network')


if __name__ == '__main__':
    unittest.main()

"""Exercise launcher control flow with fake Codex responses; no network/model calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
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
JEV_ONLY = dict(ISOLATED, off_list='URLError:Tunnel connection failed: 403', allow_host='CONNECTED')
JEV_LEAKY = dict(ISOLATED, off_list='CONNECTED', allow_host='CONNECTED')


class FlowTests(unittest.TestCase):
    def run_flow(self, responses, probes, approved=True, extra_args=(), jev=False, launched=None,
                 stdin=None, tools=None, raises=None, final=b'RESULT: test response',
                 role='model="gpt-6-luna"\nmodel_reasoning_effort="low"\ndeveloper_instructions="Read-only scout"\n'):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TEST_TMPDIR')) as temp:
            root = Path(temp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (root / 'agents').mkdir()
            (root / 'agents/scout.toml').write_text(role)
            (root / 'agent-routing.json').write_text(json.dumps({'network_fallback_roles': ['scout'] if approved else []}))
            if jev:
                (root / 'jev').mkdir()
                (root / 'jev/config.json').write_text(json.dumps({'jev': {'enabled': True}}))
            brief = root / 'brief.txt'
            brief.write_text('Bounded local test task')
            calls = []
            self.inputs = []

            def fake_exec(command, **kwargs):
                chosen = next(toml.split('=', 1)[1].strip('"') for toml in command if toml.startswith('model='))
                calls.append(chosen)
                self.inputs.append(kwargs.get('input'))
                if launched is not None:
                    launched.append(command)
                response = responses[len(calls) - 1]
                if isinstance(response, BaseException):
                    raise response
                code, stream = response
                if isinstance(stream, bytes):
                    kwargs['stdout'].flush()
                    kwargs['stdout'].buffer.write(stream)
                else:
                    kwargs['stdout'].write(stream)
                Path(command[command.index('--output-last-message') + 1]).write_bytes(final)
                return subprocess.CompletedProcess(command, code)

            def observed(_):
                return {'model': calls[-1], 'effort': 'low'}

            source = ['--brief', str(brief)] if stdin is None else []
            # Mirrors default Windows stdin: the ANSI code page with surrogateescape.
            stdin = io.TextIOWrapper(io.BytesIO(stdin or b''), encoding='cp1255', errors='surrogateescape')
            output = io.StringIO()
            code = None
            with patch.object(agent, 'ROOT', root), \
                 patch.object(agent, 'executable', return_value='codex.exe'), \
                 patch.object(agent, 'config_paths', return_value=[]), \
                 patch.object(agent, 'probe', side_effect=probes) as probe, \
                 patch.object(agent, 'verify_tools', return_value={}, side_effect=tools), \
                 patch.object(agent, 'actual_model', side_effect=observed), \
                 patch.object(agent.subprocess, 'run', side_effect=fake_exec), \
                 patch.object(agent.sys, 'stdin', stdin), \
                 contextlib.redirect_stdout(output):
                args = ['scout', '--cd', str(workspace), *source, *extra_args]
                if raises is None:
                    code = agent.main(args)
                else:
                    with self.assertRaises(raises):
                        agent.main(args)
            stdin.close()
            self.output = output.getvalue()
            reports = list((root / 'agent-runs').glob('*/report.json'))
            report = json.loads(reports[0].read_text()) if reports else None
            self.probe_hosts = [call.args[5] if len(call.args) > 5 else None for call in probe.call_args_list]
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

    @staticmethod
    def permissions(command):
        part = next(p for p in command if p.startswith('permissions.local-agent-'))
        return tomllib.loads('v = ' + part.split('=', 1)[1])['v']['network']

    def test_jev_disabled_never_probes_allowlist(self):
        stream = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.completed"}\n'
        launched = []
        code, _, report, count = self.run_flow([(0, stream)], [ISOLATED], launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 1)
        self.assertEqual(self.probe_hosts, [None])
        self.assertIsNone(report['jev_allowlist'])
        self.assertNotIn('domains', self.permissions(launched[0]))

    STREAM = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.completed"}\n'

    def developer(self, command):
        return next(p for p in command if p.startswith('developer_instructions='))

    def test_enforced_jev_allowlist_launches_with_one_domain(self):
        launched = []
        code, _, report, count = self.run_flow([(0, self.STREAM)], [ISOLATED, JEV_ONLY], jev=True, launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 2)
        self.assertEqual(self.probe_hosts, [None, 'api.typesafe.ai'])
        self.assertEqual(report['jev_allowlist'], 'api.typesafe.ai')
        self.assertFalse(report['network_exception'])
        self.assertEqual(self.permissions(launched[0]),
                         {'enabled': True, 'domains': {'api.typesafe.ai': 'allow'}})
        self.assertIn('Jev network allowlist', self.developer(launched[0]))

    def test_unenforced_allowlist_keeps_verified_offline_backend(self):
        launched = []
        code, _, report, count = self.run_flow([(0, self.STREAM)], [ISOLATED, JEV_LEAKY], jev=True, launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 2)
        self.assertEqual(self.probe_hosts, [None, 'api.typesafe.ai'])
        self.assertEqual([p['mode'] for p in report['probes']], ['elevated', 'elevated'])
        self.assertIsNone(report['jev_allowlist'])
        self.assertFalse(report['network_exception'])
        self.assertEqual(self.permissions(launched[0]), {'enabled': False})
        self.assertNotIn('Jev network allowlist', self.developer(launched[0]))

    def test_allowlist_probe_without_off_list_evidence_is_not_trusted(self):
        launched = []
        code, _, report, count = self.run_flow([(0, self.STREAM)], [ISOLATED, ISOLATED], approved=False,
                                               jev=True, launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 2)
        self.assertIsNone(report['jev_allowlist'])
        self.assertEqual(self.permissions(launched[0]), {'enabled': False})

    def test_allowlist_probe_startup_failure_keeps_offline(self):
        launched = []
        code, _, report, count = self.run_flow([(0, self.STREAM)], [ISOLATED, STARTUP_FAILURE],
                                               jev=True, launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 2)
        self.assertEqual(self.probe_hosts, [None, 'api.typesafe.ai'])
        self.assertEqual([p['mode'] for p in report['probes']], ['elevated', 'elevated'])
        self.assertIsNone(report['jev_allowlist'])
        self.assertFalse(report['network_exception'])
        self.assertEqual(self.permissions(launched[0]), {'enabled': False})

    def test_allowlist_probe_filesystem_leak_blocks_launch(self):
        code, calls, report, count = self.run_flow([], [ISOLATED, LEAK], jev=True)
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(count, 2)
        self.assertEqual(report['status'], 'blocked-filesystem')

    def test_allowlist_follows_offline_on_second_backend(self):
        launched = []
        code, _, report, count = self.run_flow([(0, self.STREAM)], [STARTUP_FAILURE, ISOLATED, JEV_ONLY],
                                               jev=True, launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 3)
        self.assertEqual(self.probe_hosts, [None, None, 'api.typesafe.ai'])
        self.assertEqual([p['mode'] for p in report['probes']], ['elevated', 'unelevated', 'unelevated'])
        self.assertEqual(report['jev_allowlist'], 'api.typesafe.ai')

    def test_leaky_offline_skips_allowlist_probes(self):
        launched = []
        code, _, report, count = self.run_flow([(0, self.STREAM)], [GOOD, GOOD, GOOD], jev=True, launched=launched)
        self.assertEqual(code, 0)
        self.assertEqual(count, 3)  # two offline probes plus the open-fallback probe
        self.assertEqual(self.probe_hosts, [None, None, None])
        self.assertTrue(report['network_exception'])
        self.assertIsNone(report['jev_allowlist'])
        self.assertEqual(self.permissions(launched[0]), {'enabled': True})
        self.assertNotIn('Jev network allowlist', self.developer(launched[0]))

    def test_leaky_offline_without_approval_blocks_after_two_probes(self):
        code, calls, report, count = self.run_flow([], [GOOD, GOOD], approved=False, jev=True)
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(count, 2)
        self.assertEqual(report['status'], 'blocked-network')

    def test_fallback_probe_startup_failure_is_not_a_filesystem_leak(self):
        code, calls, report, count = self.run_flow([], [GOOD, GOOD, STARTUP_FAILURE])
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(count, 3)
        self.assertEqual(report['status'], 'blocked-sandbox-startup')
        self.assertIn('sandbox setup unavailable', self.output)

    def test_fallback_probe_leak_prints_evidence(self):
        code, calls, report, _ = self.run_flow([], [GOOD, GOOD, LEAK])
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'blocked-filesystem')
        self.assertIn('"filesystem_ok": false', self.output)

    def test_every_probe_entry_records_its_allowlist(self):
        code, _, report, _ = self.run_flow([(0, self.STREAM)], [GOOD, GOOD, GOOD])
        self.assertEqual(code, 0)
        self.assertTrue(report['probes'][-1]['fallback'])
        self.assertTrue(all('jev_allowlist' in p for p in report['probes']))

    def test_tool_check_failure_saves_blocked_status(self):
        code, calls, report, _ = self.run_flow([], [ISOLATED], tools=RuntimeError('remains enabled'),
                                               raises=RuntimeError)
        self.assertIsNone(code)
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'blocked-tools')

    def test_launch_failure_saves_error_status(self):
        code, calls, report, _ = self.run_flow([OSError('cannot start codex')], [ISOLATED], raises=OSError)
        self.assertIsNone(code)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report['status'], 'error')
        self.assertEqual(report['attempts'], [])

    def test_utf8_stdin_brief_survives_a_legacy_console_code_page(self):
        text = 'Brief \u201c\u05e9\u05dc\u05d5\u05dd \u05d0\u05da\u05dc\u05dd\u05de\u05df\u201d \U0001f600'
        code, _, report, _ = self.run_flow([(0, self.STREAM)], [ISOLATED], stdin=text.encode('utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'completed')
        self.assertTrue(self.inputs[0].endswith(text))

    def test_invalid_utf8_in_codex_output_is_recorded(self):
        stream = self.STREAM.encode() + b'\xff\xfe\n'
        code, _, report, _ = self.run_flow([(0, stream)], [ISOLATED], final=b'RESULT: \xff done')
        self.assertEqual(code, 4)
        self.assertEqual(report['status'], 'model-unverified')
        self.assertEqual(len(report['attempts']), 1)
        self.assertIn('RESULT: \ufffd done', self.output)

    def test_role_without_instructions_is_refused_before_launch(self):
        code, calls, report, _ = self.run_flow([], [], role='model="gpt-6-luna"\nmodel_reasoning_effort="low"\n',
                                               raises=RuntimeError)
        self.assertEqual(calls, [])
        self.assertIsNone(report)

if __name__ == '__main__':
    unittest.main()

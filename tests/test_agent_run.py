"""Offline regression tests for the local agent launcher; no model/network calls."""
import importlib.util
import json
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


class RoutingTests(unittest.TestCase):
    def test_only_model_unavailability_before_work_retries(self):
        error = {'type': 'error', 'message': "The 'gpt-6-luna' model is not supported for this account"}
        self.assertTrue(agent.retryable_model_error([{'type': 'thread.started'}, error], 1))
        self.assertFalse(agent.retryable_model_error([error], 0))
        for kind in ('item.started', 'item.completed', 'item.updated'):
            self.assertFalse(agent.retryable_model_error([{'type': kind}, error], 1))

    def test_permission_test_auth_and_unknown_failures_do_not_retry(self):
        for message in ('permission denied', 'tests failed', '401 unauthorized', 'rate limit exceeded', 'unknown error'):
            self.assertFalse(agent.retryable_model_error([{'type': 'error', 'message': message}], 1))
        self.assertFalse(agent.retryable_model_error([], 1))

    def test_misleading_command_output_cannot_trigger_model_fallback(self):
        events = [{'type': 'item.completed', 'item': {'type': 'command_execution',
                   'aggregated_output': 'model_not_found', 'exit_code': 1}}]
        self.assertFalse(agent.retryable_model_error(events, 1))

    def test_toml_roundtrip_quoted_paths_and_instruction_text(self):
        value = {'C:\\repo name': 'write', 'a.b': {'quote': '"\n`$()'}, 'enabled': False}
        self.assertEqual(tomllib.loads('value = ' + agent.toml(value))['value'], value)

    def test_toml_roundtrip_unicode_workspace_and_instructions(self):
        value = {'C:\\repos\\\U0001f680': 'write', 'instructions': 'Read \u05ea\u05d9\u05e7\u05d9\u05d9\u05d4 \U0001f680'}
        self.assertEqual(tomllib.loads('value = ' + agent.toml(value))['value'], value)

    def test_toml_roundtrip_ascii_control_characters(self):
        value = {''.join(map(chr, range(128))): ''.join(map(chr, range(128)))}
        self.assertEqual(tomllib.loads('value = ' + agent.toml(value))['value'], value)

    def test_role_model_chains_do_not_silently_upgrade_to_astra(self):
        for role in ('scout', 'runner', 'builder'):
            self.assertNotIn('gpt-6-astra', [m for m, _ in agent.MODELS[role]])
        self.assertEqual(agent.MODELS['critic'], [('gpt-6-astra', 'high')])

    def test_layered_servers_disabled_without_corrupting_transports(self):
        root = Path('C:/fixture')
        user = root / 'user.toml'
        project = root / 'project.toml'
        with patch.object(agent, 'config_paths', return_value=[user, project]), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'read_text', side_effect=['[mcp_servers.docs]\nurl="https://example.invalid"\n',
                                                        '[mcp_servers.new_server]\ncommand="node"\n']):
            settings = agent.settings_for(root, False, False, 'elevated', 'test-policy', 'brief')
        self.assertEqual(settings['mcp_servers']['docs'], {'enabled': False, 'url': 'https://example.invalid'})
        self.assertEqual(settings['mcp_servers']['new_server'], {'enabled': False, 'command': 'node'})
        self.assertEqual(settings['permissions.test-policy']['filesystem'], {str(root): 'read'})
        self.assertFalse(settings['permissions.test-policy']['network']['enabled'])

    def test_corrupt_partial_work_events_prevent_fallback(self):
        error = json.dumps({'type': 'error', 'message': 'model_not_found'})
        for broken in ('{"type":"item.started",', 'null', '[]', '{}', '{"type":null}', 'unexpected output'):
            events, complete = agent.parse_events(broken + '\n' + error)
            self.assertFalse(complete)
            self.assertFalse(complete and agent.retryable_model_error(events, 1))

    def test_valid_unavailable_model_events_can_fallback(self):
        events, complete = agent.parse_events('{"type":"thread.started"}\n{"type":"error","message":"model_not_found"}\n')
        self.assertTrue(complete)
        self.assertTrue(agent.retryable_model_error(events, 1))

    def test_probe_timeout_becomes_failed_startup_evidence(self):
        with patch.object(Path, 'write_text'), patch.object(Path, 'is_file', return_value=False), \
             patch.object(agent.tempfile, 'gettempdir', return_value='C:/fake-temp'), \
             patch.object(agent.subprocess, 'run', side_effect=subprocess.TimeoutExpired('codex', 45)):
            evidence = agent.probe('codex', Path.cwd(), {}, 'test-policy', False)
        self.assertEqual(evidence['exit'], 124)
        self.assertFalse(evidence['filesystem_ok'])
        self.assertEqual(evidence['network'], 'UNKNOWN')

    def test_writable_scope_only_exact_workspace(self):
        with patch.object(agent, 'config_paths', return_value=[]):
            settings = agent.settings_for(Path('C:/project'), True, True, 'elevated', 'test-policy', 'brief')
        permissions = settings['permissions.test-policy']
        self.assertEqual(permissions['extends'], ':read-only')
        self.assertEqual(permissions['filesystem'], {str(Path('C:/project')): 'write'})
        self.assertNotIn('workspace_roots', permissions)
        self.assertEqual(settings['sandbox_workspace_write.writable_roots'], [])
        self.assertTrue(all(settings['features.' + name] is False for name in agent.DISABLED))

    def test_effective_tool_check_refuses_enabled_mcp(self):
        output = subprocess.CompletedProcess([], 0, json.dumps([{'name': 'extra', 'enabled': True}]), '')
        with patch.object(agent.subprocess, 'run', return_value=output):
            with self.assertRaisesRegex(RuntimeError, 'remains enabled'):
                agent.verify_tools('codex', Path.cwd(), {})

    def test_effective_tool_check_refuses_missing_disabled_feature(self):
        outputs = [subprocess.CompletedProcess([], 0, '[]', ''),
                   subprocess.CompletedProcess([], 0, 'apps stable false\n', '')]
        with patch.object(agent.subprocess, 'run', side_effect=outputs):
            with self.assertRaisesRegex(RuntimeError, 'not effective'):
                agent.verify_tools('codex', Path.cwd(), {})

    def test_missing_policy_grants_no_exception(self):
        with patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'exists', return_value=False):
            self.assertEqual(agent.approved_roles(), [])

    def test_policy_rejects_ambiguous_or_malformed_role_lists(self):
        invalid = [[], {}, {'network_fallback_roles': 'scout'},
                   {'network_fallback_roles': ['scout', 'scout']},
                   {'network_fallback_roles': ['unknown']}, {'network_fallback_roles': [True]}]
        for value in invalid:
            with self.subTest(value=value), patch.object(Path, 'is_symlink', return_value=False), \
                 patch.object(Path, 'exists', return_value=True), patch.object(Path, 'read_text', return_value=json.dumps(value)):
                with self.assertRaises(RuntimeError):
                    agent.approved_roles()

    def test_policy_only_authorizes_named_roles(self):
        for roles in ([], ['critic'], ['scout', 'runner', 'builder', 'critic']):
            with patch.object(Path, 'is_symlink', return_value=False), patch.object(Path, 'exists', return_value=True), \
                 patch.object(Path, 'read_text', return_value=json.dumps({'network_fallback_roles': roles})):
                self.assertEqual(agent.approved_roles(), roles)

    def test_symlink_policy_never_grants_consent(self):
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                agent.approved_roles()

    def test_non_windows_launcher_refuses_before_launch(self):
        with patch.object(agent.sys, 'platform', 'linux'), patch.object(agent.shutil, 'which') as lookup:
            with self.assertRaisesRegex(RuntimeError, 'Windows only'):
                agent.executable()
            lookup.assert_not_called()

    def test_workspace_cannot_contain_user_consent_home(self):
        with patch.object(agent, 'ROOT', Path.cwd() / '.codex'):
            with self.assertRaisesRegex(RuntimeError, 'outside the delegated workspace'):
                agent.main(['scout', '--cd', str(Path.cwd()), '--probe-only'])


class JevAllowlistTests(unittest.TestCase):
    def host_for(self, config):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            if config is not None:
                (root / 'jev').mkdir()
                (root / 'jev/config.json').write_text(
                    config if isinstance(config, str) else json.dumps(config), encoding='utf-8')
            with patch.object(agent, 'ROOT', root):
                return agent.jev_host()

    def test_enabled_jev_allows_only_default_endpoint_host(self):
        self.assertEqual(self.host_for({'jev': {'enabled': True}}), 'api.typesafe.ai')
        self.assertEqual(self.host_for({'jev': {'enabled': True, 'endpoint': 'https://jev.example.org:443/v1'}}),
                         'jev.example.org')

    def test_disabled_missing_or_malformed_config_means_no_allowlist(self):
        for config in (None, '', 'not json', '[]', {'jev': []}, {'jev': {}}, {'jev': {'enabled': False}},
                       {'jev': {'enabled': 'true'}}, {'jev': {'enabled': 1}}):
            self.assertIsNone(self.host_for(config), config)

    def test_bad_endpoint_means_no_allowlist(self):
        for endpoint in ('http://api.typesafe.ai/v1', 'http://localhost:8080', 'https://127.0.0.1/v1',
                         'https://[::1]/v1', 'https://api.typesafe.ai:8443/v1', 'https://user:pw@api.typesafe.ai/',
                         'https://*.typesafe.ai/', 'https://localhost/', 'https://example.com/', 'https:///v1',
                         'https://api.typesafe.ai:bad/', 'https://-bad.typesafe.ai/', 'https://ex\u00e4mple.com/',
                         'https://api.typesafe.ai./', None, 7, ['https://api.typesafe.ai']):
            self.assertIsNone(self.host_for({'jev': {'enabled': True, 'endpoint': endpoint}}), endpoint)

    def test_allowlist_profile_has_exactly_one_domain(self):
        with patch.object(agent, 'config_paths', return_value=[]):
            settings = agent.settings_for(Path('C:/project'), False, False, 'elevated', 'p', 'brief',
                                          'api.typesafe.ai')
        self.assertEqual(settings['permissions.p']['network'],
                         {'enabled': True, 'domains': {'api.typesafe.ai': 'allow'}})
        self.assertFalse(settings['sandbox_workspace_write.network_access'])
        self.assertIs(settings['features.hooks'], False)

    def test_without_allowlist_profile_is_unchanged(self):
        with patch.object(agent, 'config_paths', return_value=[]):
            offline = agent.settings_for(Path('C:/project'), False, False, 'elevated', 'p', 'brief')
            fallback = agent.settings_for(Path('C:/project'), False, True, 'elevated', 'p', 'brief', 'api.typesafe.ai')
        self.assertEqual(offline['permissions.p']['network'], {'enabled': False})
        self.assertEqual(fallback['permissions.p']['network'], {'enabled': True})

    def test_isolation_requires_denied_socket_and_refused_unlisted_host(self):
        refused = 'URLError:<urlopen error Tunnel connection failed: 403 Forbidden>'
        good = {'network': 'DENIED', 'off_list': refused, 'allow_host': 'CONNECTED'}
        self.assertTrue(agent.isolated({'network': 'DENIED'}, None))
        self.assertFalse(agent.isolated({'network': 'CONNECTED'}, None))
        self.assertTrue(agent.isolated(good, 'h.example'))
        self.assertFalse(agent.isolated(dict(good, network='CONNECTED'), 'h.example'))
        dns = 'URLError:<urlopen error [Errno 11001] getaddrinfo failed>'
        for off_list in ('CONNECTED', None, 0, '', dns, 'SSLCertVerificationError:bad cert', 'TimeoutError:'):
            self.assertFalse(agent.isolated(dict(good, off_list=off_list), 'h.example'), off_list)
        for allow_host in (None, dns, 'TimeoutError:'):
            self.assertFalse(agent.isolated(dict(good, allow_host=allow_host), 'h.example'), allow_host)

    def test_hex_or_numeric_tld_is_not_a_domain(self):
        for endpoint in ('https://0x7f.0x0.0x0.0x1/v1', 'https://127.0.0.0x1/', 'https://jev.example.c0m/'):
            self.assertIsNone(self.host_for({'jev': {'enabled': True, 'endpoint': endpoint}}), endpoint)

    def test_critic_never_gets_allowlist(self):
        self.assertEqual(agent.JEV_ROLES, ('scout', 'runner', 'builder'))

    def test_probe_python_runs_isolated_from_workspace_modules(self):
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(command, 1, '', 'stop')
        with patch.object(Path, 'write_text'), patch.object(Path, 'is_file', return_value=False),              patch.object(agent.tempfile, 'gettempdir', return_value='C:/fake-temp'),              patch.object(agent.subprocess, 'run', side_effect=run):
            agent.probe('codex', Path.cwd(), {}, 'test-policy', False, 'api.typesafe.ai')
        command = seen[0]
        python = command.index(agent.sys.executable, command.index('--'))
        self.assertEqual(command[python + 1:python + 4], ['-I', '-B', '-c'])
        self.assertEqual(json.loads(command[-1])['allow_host'], 'api.typesafe.ai')

if __name__ == '__main__':
    unittest.main()

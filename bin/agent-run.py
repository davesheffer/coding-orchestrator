#!/usr/bin/env python3
"""Audited Windows Codex delegation with permission probes and bounded model fallback."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid

ROOT = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve()
MODELS = {
    'scout': [('gpt-5.6-luna', 'low'), ('gpt-5.6-terra', 'low'), ('gpt-5.6-sol', 'low')],
    'runner': [('gpt-5.6-luna', 'low'), ('gpt-5.6-terra', 'low'), ('gpt-5.6-sol', 'low')],
    'builder': [('gpt-5.6-terra', 'medium'), ('gpt-5.6-sol', 'medium')],
    'critic': [('gpt-6-astra', 'high')],
}
DISABLED = ('apps', 'plugins', 'remote_plugin', 'browser_use', 'browser_use_external',
            'browser_use_full_cdp_access', 'in_app_browser', 'computer_use',
            'image_generation', 'multi_agent', 'multi_agent_v2',
            'skill_mcp_dependency_install')


def toml(value):
    if isinstance(value, dict):
        return '{ ' + ', '.join(toml(k) + ' = ' + toml(v) for k, v in value.items()) + ' }'
    # TOML forbids raw DEL; JSON's Unicode mode leaves it literal.
    return json.dumps(value, ensure_ascii=False).replace('\x7f', '\\u007f')


def overrides(settings):
    return [part for key, value in settings.items() for part in ('-c', key + '=' + toml(value))]


def executable():
    if sys.platform != 'win32':
        raise RuntimeError('This restricted launcher currently supports Windows only; use verified native roles or keep the task in the main session')
    command = shutil.which('codex')
    if not command:
        raise RuntimeError('codex is not installed on PATH')
    path = Path(command)
    if path.suffix.lower() == '.exe':
        return str(path)
    vendor = path.parent / 'node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe'
    if vendor.is_file():
        return str(vendor)
    raise RuntimeError('Cannot resolve native codex.exe; refusing a shell-interpolated launch')


def approved_roles():
    """User-owned consent only; absent, malformed or linked policies grant nothing."""
    path = ROOT / 'agent-routing.json'
    if path.is_symlink():
        raise RuntimeError('Network fallback policy must be a regular user-local file, not a symlink')
    if not path.exists():
        return []
    config = json.loads(path.read_text(encoding='utf-8'))
    roles = config.get('network_fallback_roles') if isinstance(config, dict) else None
    if not isinstance(roles, list) or any(not isinstance(role, str) or role not in MODELS for role in roles):
        raise RuntimeError('Invalid network_fallback_roles; expected a list of known role names')
    if len(roles) != len(set(roles)):
        raise RuntimeError('Duplicate network fallback role in user-local policy')
    return roles


def config_paths(cwd):
    return list(dict.fromkeys([ROOT / 'config.toml'] +
                             [p / '.codex/config.toml' for p in reversed([cwd, *cwd.parents])]))


def settings_for(cwd, writable, network, mode, policy, instructions):
    servers = set()
    for path in config_paths(cwd):
        if path.exists():
            config = tomllib.loads(path.read_text(encoding='utf-8-sig'))
            servers.update(config.get('mcp_servers', {}))
    permissions = {'extends': ':read-only', 'network': {'enabled': network},
                   'filesystem': {str(cwd): 'write' if writable else 'read'}}
    settings = {
        'default_permissions': policy,
        'permissions.' + policy: permissions,
        'approval_policy': 'never', 'web_search': 'disabled', 'windows.sandbox': mode,
        'agents.enabled': False, 'developer_instructions': instructions,
        'sandbox_workspace_write.network_access': False,
        'sandbox_workspace_write.writable_roots': [],
        'sandbox_workspace_write.exclude_slash_tmp': True,
        'sandbox_workspace_write.exclude_tmpdir_env_var': True,
    }
    settings.update({'features.' + name: False for name in DISABLED})
    # CLI tables merge recursively. Preserve each existing transport, disable it,
    # and supply a valid dummy transport only for absent built-in server entries.
    settings['mcp_servers'] = {name: {'enabled': False} for name in servers}
    for name in ('node_repl', 'computer-use'):
        if name not in servers:
            settings['mcp_servers'][name] = {'enabled': False, 'command': 'cmd.exe'}
    return settings


def verify_tools(exe, cwd, settings):
    result = subprocess.run([exe, *overrides(settings), 'mcp', 'list', '--json'],
                            cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=30)
    if result.returncode:
        raise RuntimeError('Cannot inspect effective MCP settings: ' + result.stderr[-2000:])
    servers = json.loads(result.stdout)
    if not isinstance(servers, list) or any(s.get('enabled', True) for s in servers):
        raise RuntimeError('Effective MCP server remains enabled; refusing delegation')
    result = subprocess.run([exe, *overrides(settings), 'features', 'list'],
                            cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=30)
    if result.returncode:
        raise RuntimeError('Cannot inspect effective feature settings: ' + result.stderr[-2000:])
    features = {parts[0]: parts[-1] for line in result.stdout.splitlines()
                if len(parts := line.split()) >= 3}
    if any(features.get(name) != 'false' for name in DISABLED):
        raise RuntimeError('A required tool restriction is not effective; refusing delegation')
    return {'mcp_servers': {s['name']: s['enabled'] for s in servers},
            'disabled_features': list(DISABLED)}


PROBE = r'''
import json,socket,sys
from pathlib import Path
spec=json.loads(sys.argv[1]); r={'read':False,'writes':{}}
try:r['read']=Path(spec['read']).read_text()=='agent-probe'
except OSError as e:r['read_error']=type(e).__name__+':'+str(e)
for p in spec['writes']:
 try:
  with open(p,'xb') as f:f.write(b'agent-probe')
  r['writes'][p]='ALLOWED'
 except PermissionError:r['writes'][p]='DENIED'
 except OSError as e:r['writes'][p]=type(e).__name__
try:
 with socket.create_connection(('1.1.1.1',443),timeout=3):r['network']='CONNECTED'
except PermissionError:r['network']='DENIED'
except OSError as e:r['network']=type(e).__name__
print(json.dumps(r))
'''


def probe(exe, cwd, settings, policy, writable):
    marker = '.agent-probe-' + uuid.uuid4().hex
    read = cwd / (marker + '.read')
    targets = list(dict.fromkeys([cwd / marker, cwd.parent / marker,
                                  Path(tempfile.gettempdir()) / marker, ROOT / marker]))
    if any(p.parent == cwd for p in targets[1:]):
        raise RuntimeError('Workspace overlaps protected probe roots')
    read.write_text('agent-probe', encoding='utf-8')
    try:
        command = [exe, 'sandbox', '--permission-profile', policy, '--cd', str(cwd),
                   *overrides(settings), '--', sys.executable, '-B', '-c', PROBE,
                   json.dumps({'read': str(read), 'writes': [str(p) for p in targets]})]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=45)
        except subprocess.TimeoutExpired:
            return {'filesystem_ok': False, 'exit': 124,
                    'error': 'Permission probe startup timed out after 45 seconds', 'network': 'UNKNOWN'}
        if result.returncode:
            return {'filesystem_ok': False, 'exit': result.returncode,
                    'error': result.stderr[-4000:], 'network': 'UNKNOWN'}
        data = json.loads(result.stdout)
        expected = {str(p): ('ALLOWED' if i == 0 and writable else 'DENIED')
                    for i, p in enumerate(targets)}
        data['filesystem_ok'] = data['read'] and data['writes'] == expected
        data['exit'] = result.returncode
        return data
    finally:
        # Only these fresh, unique probe files can be removed; never recursive.
        for path in [read, *targets]:
            if path.is_file() and path.read_bytes() == b'agent-probe':
                path.unlink()


def retryable_model_error(events, returncode):
    """Only an explicit service/model error BEFORE any work can change models."""
    if returncode == 0 or not events:
        return False
    if any(e.get('type', '').startswith('item.') for e in events):
        return False
    errors = []
    for event in events:
        if event.get('type') in ('error', 'turn.failed'):
            errors.append(str(event.get('message', event.get('error', ''))).lower())
    text = ' '.join(errors)
    unavailable = ('model_not_found', 'model_not_available', 'model is not supported',
                   'model is unavailable', 'model is not available', 'model is not enabled',
                   'does not support the requested model')
    return any(term in text for term in unavailable)


def parse_events(text):
    events = []
    complete = True
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                complete = False
            else:
                events.append(event)
        except json.JSONDecodeError:
            complete = False
    return events, complete


def actual_model(thread_id):
    if not thread_id:
        return None
    uri = (ROOT / 'state_5.sqlite').as_uri() + '?mode=ro'
    try:
        with sqlite3.connect(uri, uri=True) as db:
            row = db.execute('select model,reasoning_effort from threads where id=?',
                             (thread_id,)).fetchone()
            return {'model': row[0], 'effort': row[1]} if row else None
    except sqlite3.Error:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=MODELS)
    parser.add_argument('--cd', required=True, type=Path)
    parser.add_argument('--brief', type=Path, help='UTF-8 brief file; otherwise read stdin')
    parser.add_argument('--probe-only', action='store_true')
    parser.add_argument('--no-network-fallback', action='store_true')
    args = parser.parse_args(argv)
    cwd = args.cd.resolve(strict=True)
    if not cwd.is_dir() or cwd == cwd.parent or cwd in (ROOT, Path.home().resolve()):
        raise RuntimeError('Choose a specific repository/workspace directory')
    if ROOT == cwd or ROOT.is_relative_to(cwd):
        raise RuntimeError('CODEX_HOME must be outside the delegated workspace; project files cannot grant fallback consent')
    brief = '' if args.probe_only else (args.brief.read_text(encoding='utf-8') if args.brief else sys.stdin.read())
    if not args.probe_only and not brief.strip():
        raise RuntimeError('A bounded task brief is required')
    role = tomllib.loads((ROOT / 'agents' / (args.role + '.toml')).read_text(encoding='utf-8-sig'))
    if (role.get('model'), role.get('model_reasoning_effort')) != MODELS[args.role][0]:
        raise RuntimeError('Installed role model differs from launcher policy; reconcile settings before launch')
    approved = args.role in approved_roles() and not args.no_network_fallback
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + args.role + '-' + uuid.uuid4().hex[:8]
    logs = ROOT / 'agent-runs' / run_id
    logs.mkdir(parents=True)
    report = {'role': args.role, 'workspace': str(cwd), 'probes': [], 'attempts': [], 'status': 'preflight'}

    def save():
        target = logs / 'report.json'
        staged = logs / 'report.tmp'
        staged.write_text(json.dumps(report, indent=2), encoding='utf-8')
        os.replace(staged, target)

    save()
    print('Agent run:', logs, flush=True)
    exe = executable()
    writable = args.role in ('builder', 'runner')
    # Each invocation starts from strict isolation; never cache a failed network probe.
    policy = 'local-agent-' + uuid.uuid4().hex
    isolated = None
    fallback_mode = None
    for mode in ('elevated', 'unelevated'):
        settings = settings_for(cwd, writable, False, mode, policy, role['developer_instructions'])
        evidence = probe(exe, cwd, settings, policy, writable)
        report['probes'].append({'mode': mode, 'fallback': False, **evidence})
        save()
        if evidence['exit'] != 0:
            # The probe never ran. Try the other restricted backend, never a
            # permissive backend. No verified filesystem result means no launch.
            continue
        if not evidence['filesystem_ok']:
            report['status'] = 'blocked-filesystem'
            save()
            print(json.dumps(evidence), flush=True)
            return 3
        fallback_mode = mode
        if evidence['network'] == 'DENIED':
            isolated = settings
            break
    network_exception = isolated is None
    if network_exception:
        if fallback_mode is None:
            report['status'] = 'blocked-sandbox-startup'
            save()
            print('Neither restricted backend could run its permission probe. Return work to main session.')
            return 3
        if not approved:
            report['status'] = 'blocked-network'
            save()
            print('Network isolation unavailable; fallback is not authorized. Return work to main session.')
            return 3
        instructions = role['developer_instructions'] + '''\n\nUser-local network fallback authorized by agent-routing.json: this launch has freshly
verified filesystem limits but cannot enforce network isolation. This explicit
authorization overrides the role's network-only stop rule. Do not stop solely
because network access is available. Make NO external requests. Web, MCP, apps,
plugins, browser/computer tools, images and nested agents are disabled. Report
the network isolation exception in UNVERIFIED. File limits remain mandatory.
'''
        settings = settings_for(cwd, writable, True, fallback_mode, policy, instructions)
        evidence = probe(exe, cwd, settings, policy, writable)
        report['probes'].append({'mode': fallback_mode, 'fallback': True, **evidence})
        if not evidence['filesystem_ok']:
            report['status'] = 'blocked-filesystem'
            save()
            return 3
        print('Fallback: network isolation unavailable; filesystem restrictions verified. No external requests allowed.', flush=True)
    else:
        settings = isolated
    report['network_exception'] = network_exception
    report['authorization_source'] = str(ROOT / 'agent-routing.json') if network_exception else None
    report['tools'] = verify_tools(exe, cwd, settings)
    report['status'] = 'preflight-passed'
    save()
    if args.probe_only:
        return 0
    launch_brief = ('Assigned role: ' + args.role + '. You are not alone in the workspace; preserve others\' edits.\n'
                    'Do not delegate or run Hunch bookkeeping. Follow the bounded brief below.\n'
                    'Fresh permission probe evidence:\n' + json.dumps(report['probes']) + '\n\n' + brief)
    for i, (model, effort) in enumerate(MODELS[args.role]):
        settings['model'] = model
        settings['model_reasoning_effort'] = effort
        message_path = logs / f'{i}-final.txt'
        command = [exe, 'exec', '--strict-config', '--cd', str(cwd), '--json',
                   *overrides(settings), '--output-last-message', str(message_path), '-']
        print(f'Launching {args.role}: {model} / {effort}', flush=True)
        with (logs / f'{i}-events.jsonl').open('w', encoding='utf-8') as output, (logs / f'{i}-stderr.log').open('w', encoding='utf-8') as error:
            result = subprocess.run(command, input=launch_brief, text=True, encoding='utf-8',
                                    stdout=output, stderr=error)
        events, events_complete = parse_events((logs / f'{i}-events.jsonl').read_text(encoding='utf-8'))
        thread = next((e.get('thread_id') for e in events if e.get('type') == 'thread.started'), None)
        observed = actual_model(thread)
        attempt = {'requested_model': model, 'requested_effort': effort, 'observed': observed,
                   'thread_id': thread, 'exit': result.returncode, 'events_complete': events_complete}
        report['attempts'].append(attempt)
        if result.returncode == 0:
            report['status'] = 'completed' if events_complete and observed == {'model': model, 'effort': effort} else 'model-unverified'
            save()
            print(json.dumps(attempt), flush=True)
            if message_path.exists():
                print(message_path.read_text(encoding='utf-8'), flush=True)
            return 0 if report['status'] == 'completed' else 4
        can_retry = events_complete and retryable_model_error(events, result.returncode)
        attempt['model_fallback_allowed'] = can_retry
        report['status'] = 'model-unavailable' if can_retry else 'failed-no-retry'
        save()
        if not can_retry or i == len(MODELS[args.role]) - 1:
            print(f'Agent stopped (exit {result.returncode}); inspect {logs}. Main session must assess partial work before continuing.')
            return result.returncode or 1
        print('Model unavailable before work started; trying the next configured model.', flush=True)
    return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print('Agent launcher refused: ' + str(exc), file=sys.stderr)
        raise SystemExit(3)

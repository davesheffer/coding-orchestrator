import { describe, expect, mock, test } from 'claude-code/testing'

import { hunchCli, hunchInvocation, hunchName, hunchOutcome, reapStale } from '../hooks/parse'

const TASK = 'htask_a0a4770f5592438f23dad47a'

const CONSTRAINTS = [
  'Constraints affecting "lib/a.dart":',
  '',
  '• con_13cfa1c5ef [warning/advisory_v1] Everything must work exactly like the web.',
  '    rationale: user correction',
  '• con_0b3a37149d [advisory/advisory_v1] Guard preview home shows connected.',
].join('\n')

const scroll = { offset: 0, bodyRows: 40 }

describe('hunch parsing', () => {
  test('MCP names and their targets', async () => {
    expect(hunchName('mcp__hunch__hunch_context')).toBe('context')
    expect(hunchName('mcp__hunch__hunch_check_constraints')).toBe('check_constraints')
    expect(hunchName('mcp__github__get_issue')).toBeUndefined()

    expect(hunchInvocation('context', { target: 'lib/a.dart', task_id: TASK })).toEqual({
      name: 'context',
      target: 'lib/a.dart',
      taskId: TASK,
    })
    expect(hunchInvocation('task', { action: 'finish', task_id: TASK }).target).toBe('finish')
    expect(hunchInvocation('merge_verdict', { working: true }).target).toBe('working tree')
    expect(hunchInvocation('merge_verdict', {}).target).toBe('staged')
    expect(hunchInvocation('compare', { candidates: ['a', 'b'] }).target).toBe('a vs b')
  })

  test('the CLI through Bash: task verify, other subcommands, and look-alikes', async () => {
    const verify = hunchCli(
      `'/c/Program Files/nodejs/node.exe' 'C:\\x\\node_modules\\@davesheffer\\hunch\\dist\\cli\\index.js' task verify ${TASK} -- flutter test test/a_test.dart 2>&1 | tail -5`,
    )
    expect(verify).toEqual({ name: 'verify', target: 'flutter test test/a_test.dart', taskId: TASK })
    expect(hunchCli('hunch update')).toEqual({ name: 'cli update', target: '', taskId: undefined })
    expect(hunchCli('npx -y @davesheffer/hunch now')?.name).toBe('cli now')
    expect(hunchCli('grep -rn hunch lib/')).toBeUndefined()
    expect(hunchCli('cat .hunch/decisions/x.json')).toBeUndefined()
    expect(hunchCli('git commit -m "feat: band; hunch update now runs"')).toBeUndefined()
    expect(hunchCli("echo '(hunch status)'")).toBeUndefined()
    expect(hunchCli('git add . && hunch update')?.name).toBe('cli update')
    // Heredoc bodies are data, not commands; a newline or `$(` starts a command.
    expect(hunchCli("git commit -F- <<'EOF'\nfeat: band\nhunch update\nEOF")).toBeUndefined()
    expect(hunchCli('cat <<EOF > notes.md\n(hunch status)\nEOF')).toBeUndefined()
    expect(hunchCli('echo foo (hunch status)')).toBeUndefined()
    expect(hunchCli('cd repo\nhunch update')?.name).toBe('cli update')
    expect(hunchCli('id=$(hunch now)')?.name).toBe('cli now')
    // Not heredocs: a here-string and a shift; and a real call after a heredoc's terminator.
    expect(hunchCli('cat <<< "x" && hunch update')?.name).toBe('cli update')
    expect(hunchCli('echo $((1 << 2)); hunch update')?.name).toBe('cli update')
    expect(hunchCli('cat <<EOF > f\nbody\nEOF\nhunch update')?.name).toBe('cli update')
    expect(hunchCli('cat <<EOF > f\r\nbody\r\nEOF\r\nhunch update')?.name).toBe('cli update')
    // A launcher path quoted in a heredoc body is data; one before the heredoc is the call.
    const launcher = `node C:/x/@davesheffer/hunch/dist/cli/index.js task verify ${TASK} --`
    expect(hunchCli(`python relay.py handoff <<'H'\nVERIFIED: ${launcher} flutter test exit 0\nH`)).toBeUndefined()
    expect(hunchCli(`${launcher} python - <<'PY'\nprint(1)\nPY`)?.name).toBe('verify')
  })

  test('stale running calls are reaped, fresh ones kept', async () => {
    const base = { target: '', role: 'main', summary: '', level: 'info' as const }
    const now = Date.parse('2026-10-06T14:00:00')
    const calls = [
      { ...base, id: 'old', name: 'context', startedAt: now - 16 * 60_000, status: 'running' as const },
      { ...base, id: 'long', name: 'verify', startedAt: now - 11 * 60_000, status: 'running' as const },
      { ...base, id: 'new', name: 'why', startedAt: now - 60_000, status: 'running' as const },
      { ...base, id: 'done', name: 'why', startedAt: now - 60 * 60_000, status: 'ok' as const },
    ]
    const reaped = reapStale(calls, now)
    expect(reaped.map(call => [call.id, call.status])).toEqual([
      ['old', 'error'],
      ['long', 'running'],
      ['new', 'running'],
      ['done', 'ok'],
    ])
    expect(reaped[0]?.summary).toBe('no result recorded')
    expect(reaped[0]?.level).toBe('warn')
  })

  test('constraints: warning is warn, advisory is listed but quiet', async () => {
    const outcome = hunchOutcome('check_constraints', CONSTRAINTS, false)
    expect(outcome.constraints.map(one => [one.id, one.severity])).toEqual([
      ['con_13cfa1c5ef', 'warning'],
      ['con_0b3a37149d', 'advisory'],
    ])
    expect(outcome.level).toBe('warn')
    expect(outcome.summary).toBe('2 constraints (1 warning)')
    expect(hunchOutcome('check_constraints', 'No constraints match.', false).summary).toBe('no constraints in scope')
    expect(hunchOutcome('check_constraints', '• con_x1 [blocking/hard] Never X.', false).level).toBe('alert')
  })

  test('merge verdict, escalations, verify exit codes, errors', async () => {
    const warn = hunchOutcome('merge_verdict', 'VERDICT: ⚠ WARN — touches memory\n(scope: commit HEAD, 14 file(s))', false)
    expect(warn.verdict).toBe('WARN')
    expect(warn.level).toBe('warn')
    expect(warn.summary).toBe('verdict WARN · commit HEAD, 14 file(s)')
    expect(hunchOutcome('merge_verdict', 'VERDICT: ⛔ BLOCK — x', false).level).toBe('alert')
    expect(hunchOutcome('merge_verdict', 'VERDICT: ✅ PASS', false).level).toBe('info')

    expect(hunchOutcome('escalations', '✓ Nothing needs a human decision', false).summary).toBe('none')
    const open = hunchOutcome('escalations', '2 decisions need the human\'s call\n⚖ auth: keep A or B?\n· cache premise stale', false)
    expect(open.level).toBe('alert')
    expect(open.summary).toBe('2 need your decision')

    expect(hunchOutcome('verify', '{\n  "exit_code": 0,\n  "timed_out": false\n}', false).summary).toBe('exit 0')
    const failed = hunchOutcome('verify', '{ "exit_code": 1, "timed_out": false }', true)
    expect(failed.level).toBe('alert')
    expect(hunchOutcome('verify', '{ "exit_code": 2, "timed_out": false }', false).exitCode).toBe(2)

    // The checked command's own JSON comes first; the launcher's result JSON is last.
    const streamed = '{"exit_code": 0, "suite": "x"}\n3 tests failed\n{\n  "exit_code": 1,\n  "output_hash": "sha256:a",\n  "timed_out": false\n}'
    expect(hunchOutcome('verify', streamed, true).summary).toBe('exit 1')
    // `| tail -5` cut the JSON's head off: the Bash call's own exit decides.
    expect(hunchOutcome('verify', 'Exit code 1\n  "timed_out": false,\n  "source": "x"\n}', true).summary).toBe('exit 1')
    expect(hunchOutcome('verify', '  "timed_out": false,\n  "source": "x"\n}', false).level).toBe('warn')
    // A failed timeout alerts even when no exit code survived; the command's own `timed_out` does not.
    expect(hunchOutcome('verify', '  "timed_out": true,\n  "source": "x"\n}', true).summary).toBe('timed out')
    expect(hunchOutcome('verify', '{"results":{"timed_out": true}}', false).level).toBe('warn')
    // `Exit code N` in a passing command's stdout is not core's marker.
    const tolerated = hunchOutcome('verify', 'step A\nExit code 1 (tolerated)\n{"exit_code": 0, "timed_out": false}', false)
    expect([tolerated.summary, tolerated.level]).toEqual(['exit 0', 'info'])
    // Launcher JSON cut off, the checked command's own JSON said 0: the Bash exit wins.
    const cut = hunchOutcome('verify', 'Exit code 1\n{"exit_code": 0, "suite": "x"}\n3 tests failed', true)
    expect(cut.summary).toBe('exit 1')
    expect(cut.level).toBe('alert')

    const broken = hunchOutcome('why', 'hunch: task not found in this repository', true)
    expect(broken.level).toBe('alert')
    expect(broken.summary).toBe('hunch: task not found in this repository')

    expect(hunchOutcome('task', `Task started: ${TASK}`, false).taskId).toBe(TASK)
    expect(hunchOutcome('context', '## 🧠 Brief\n\n---\nInvariants: 2', false).summary).toBe('🧠 Brief')
  })
})

test('a live MCP call lands in the pane and a BLOCK verdict toasts', async ($, on) => {
  mock.clock(on, { now: Date.parse('2026-10-06T14:00:00') })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('tool.call', { tool: 'mcp__hunch__hunch_merge_verdict' }, () => ({
    result: 'VERDICT: ⛔ BLOCK — breaks con_x\n(scope: staged, 2 file(s))',
  }))
  on('tool.call', { tool: 'mcp__hunch__hunch_context' }, () => ({ result: '## Brief for lib/a.dart' }))

  await $.tool.call({ tool: 'mcp__hunch__hunch_context', target: 'lib/a.dart', task_id: TASK })
  await $.tool.call({ tool: 'mcp__hunch__hunch_merge_verdict', task_id: TASK })

  expect(toasts.filter(text => /merge verdict BLOCK/.test(text)).length).toBe(1)

  for (const surface of ['terminal', 'desktop', 'vscode'] as const) {
    const pane = await $.ui.mount({
      plugin: 'mission-control',
      surface,
      component: 'Pane',
      requestId: 'mission-control',
      props: { title: 'Mission Control', isFocused: false, bodyColumns: 100, placement: 'dock', scroll, view: {} },
    })
    expect(await pane.find({ text: new RegExp(`${TASK} · 2 calls`) })).toBeDefined()
    expect(await pane.find({ text: /context 1 · merge_verdict 1/ })).toBeDefined()
    expect(await pane.find({ text: /verdict BLOCK · staged/ })).toBeDefined()
    expect(await pane.find({ text: /Brief for lib\/a\.dart/ })).toBeDefined()
    await pane.unmount()

    const band = await $.ui.mount({
      plugin: 'mission-control',
      surface,
      component: 'AbovePrompt',
      props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 120, scroll, view: {} },
    })
    expect(await band.find({ text: /hunch 2 ⚠1/ })).toBeDefined()
    await band.unmount()
  }
})

test('a warning invariant toasts once per session, however many checks hit it', async ($, on) => {
  mock.clock(on, { now: Date.parse('2026-10-06T14:00:00') })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('tool.call', { tool: 'mcp__hunch__hunch_check_constraints' }, () => ({ result: CONSTRAINTS }))

  await $.tool.call({ tool: 'mcp__hunch__hunch_check_constraints', scope: 'lib/a.dart' })
  await $.tool.call({ tool: 'mcp__hunch__hunch_check_constraints', scope: 'lib/b.dart' })

  const invariants = toasts.filter(text => /Hunch invariant/.test(text))
  expect(invariants.length).toBe(1)
  expect(invariants[0]).toMatch(/\[warning\] \(lib\/a\.dart\): Everything must work exactly like the web/)
})

test('task verify through Bash records its exit code and toasts a failure', async ($, on) => {
  mock.clock(on, { now: Date.parse('2026-10-06T14:00:00') })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('tool.call', { tool: 'Bash' }, (_$, e) => {
    const code = e.tool === 'Bash' && e.command.includes('failing') ? 1 : 0
    const out = `{ "exit_code": ${code}, "timed_out": false }`

    return { result: { stdout: out, stderr: '', interrupted: false }, text: out }
  })

  const cli = `node C:/x/@davesheffer/hunch/dist/cli/index.js task verify ${TASK} --`
  await $.tool.call({ tool: 'Bash', command: `${cli} flutter test passing`, description: 'verify' })
  await $.tool.call({ tool: 'Bash', command: `${cli} flutter test failing`, description: 'verify' })
  await $.tool.call({ tool: 'Bash', command: 'git status', description: 'not hunch' })

  expect(toasts.filter(text => /Hunch verify/.test(text))).toEqual(['✖ Hunch verify exit 1 (flutter test failing)'])

  const pane = await $.ui.mount({
    plugin: 'mission-control',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'mission-control',
    props: { title: 'Mission Control', isFocused: false, bodyColumns: 100, placement: 'dock', scroll, view: {} },
  })
  expect(await pane.find({ text: /verify 2/ })).toBeDefined()
  expect(await pane.find({ text: /exit 0/ })).toBeDefined()
  await pane.unmount()
})

test('an interrupted call is marked interrupted, without a failure toast', async ($, on) => {
  mock.clock(on, { now: Date.parse('2026-10-06T14:00:00') })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: '', stderr: '', interrupted: true }, text: '' }))
  on('tool.call', { tool: 'mcp__hunch__hunch_why' }, () => ({
    isError: true as const,
    result: undefined,
    text: '[Request interrupted by user for tool use]',
  }))

  const cli = `node C:/x/@davesheffer/hunch/dist/cli/index.js task verify ${TASK} --`
  await $.tool.call({ tool: 'Bash', command: `${cli} flutter test slow`, description: 'verify' })
  await $.tool.call({ tool: 'mcp__hunch__hunch_why', target: 'lib/a.dart' })

  expect(toasts.filter(text => /Hunch/.test(text))).toEqual([])

  const pane = await $.ui.mount({
    plugin: 'mission-control',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'mission-control',
    props: { title: 'Mission Control', isFocused: false, bodyColumns: 100, placement: 'dock', scroll, view: {} },
  })
  expect(await pane.find({ text: /interrupted/ })).toBeDefined()
  await pane.unmount()

  // Interrupted calls count but do not raise the band's warning mark.
  const band = await $.ui.mount({
    plugin: 'mission-control',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 120, scroll, view: {} },
  })
  expect(await band.find({ text: /hunch 2/ })).toBeDefined()
  expect(await band.find({ text: /hunch 2 ⚠/ })).toBeUndefined()
  await band.unmount()
})

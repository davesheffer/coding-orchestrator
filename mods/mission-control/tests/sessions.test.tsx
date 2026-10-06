import { describe, expect, mock, test } from 'claude-code/testing'

import { ago, repoName, sessionRow, sessionRows } from '../hooks/parse'

const NOW = Date.parse('2026-10-06T14:00:00')

const SELF = '366f2731-1b7b-4099-9607-d8e06526a63e'
const OTHER = 'aaaaaaaa-1111-4222-8333-444444444444'
const TERMINAL = 'bbbbbbbb-1111-4222-8333-444444444444'

const entry = (fields: Record<string, unknown>) => JSON.stringify({ kind: 'interactive', version: '2.1.289', ...fields })

const REGISTRY: Record<string, string> = {
  '23332.json': entry({
    pid: 23332,
    sessionId: SELF,
    cwd: 'c:\\Users\\davids\\github\\soldi-flutter',
    name: 'soldi-flutter-44',
    entrypoint: 'claude-vscode',
    status: 'idle',
    updatedAt: NOW - 30_000,
  }),
  '25676.json': entry({
    pid: 25676,
    sessionId: OTHER.toUpperCase(),
    cwd: 'c:\\Users\\davids\\github\\soldi',
    name: 'soldi-12',
    entrypoint: 'claude-vscode',
    status: 'busy',
    updatedAt: NOW - 5_000,
  }),
  '31276.json': entry({
    pid: 31276,
    sessionId: TERMINAL,
    cwd: '/home/d/coding-orchestrator',
    entrypoint: 'cli',
    status: 'idle',
    updatedAt: NOW - 3 * 3600_000,
  }),
  '31276.abc.key': '{"peerToken":"x"}',
  'broken.json': '{ not json',
}

const scroll = { offset: 0, bodyRows: 40 }

const RUN = { command: 'sessions', args: '', origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 100 } }

describe('session registry parsing', () => {
  test('a registry file becomes a row; anything else is skipped', async () => {
    const row = sessionRow(REGISTRY['25676.json'] ?? '')
    expect(row).toEqual({
      pid: 25676,
      sessionId: OTHER,
      name: 'soldi-12',
      cwd: 'c:\\Users\\davids\\github\\soldi',
      entrypoint: 'claude-vscode',
      status: 'busy',
      updatedAt: NOW - 5_000,
    })
    // No name: the repo folder names it.
    expect(sessionRow(REGISTRY['31276.json'] ?? '')?.name).toBe('coding-orchestrator')
    expect(sessionRow(entry({ pid: 2, sessionId: SELF, status: 'weird\nvalue' }))?.status).toBe('unknown')
    expect(sessionRow(entry({ pid: 2, sessionId: SELF }))?.status).toBe('unknown')
    expect(sessionRow('{ not json')).toBeUndefined()
    expect(sessionRow('{"peerToken":"x"}')).toBeUndefined()
    expect(sessionRow(entry({ pid: 1, sessionId: '../../etc/passwd' }))).toBeUndefined()
    expect(sessionRow(entry({ pid: '1', sessionId: SELF }))).toBeUndefined()
  })

  test('rows sort newest first, one per session', async () => {
    const base = { cwd: '', entrypoint: 'cli', status: 'idle' as const }
    const rows = sessionRows([
      { ...base, pid: 1, sessionId: SELF, name: 'stale', updatedAt: 10 },
      { ...base, pid: 2, sessionId: OTHER, name: 'b', updatedAt: 30 },
      { ...base, pid: 3, sessionId: SELF, name: 'live', updatedAt: 20 },
    ])
    expect(rows.map(row => [row.pid, row.name])).toEqual([
      [2, 'b'],
      [3, 'live'],
    ])
  })

  test('repo names and ages', async () => {
    expect(repoName('c:\\Users\\davids\\github\\soldi\\')).toBe('soldi')
    expect(repoName('/home/d/repo')).toBe('repo')
    expect(repoName('')).toBe('')
    expect(ago(45_000)).toBe('45s')
    expect(ago(12 * 60_000)).toBe('12m')
    expect(ago(3 * 3600_000)).toBe('3h')
    expect(ago(2 * 86400_000)).toBe('2d')
  })
})

test('/sessions lists the registry and switch runs the focus helper', async ($, on) => {
  mock.clock(on, { now: NOW })
  mock.env(on, { USERPROFILE: 'C:\\Users\\d' })
  const toasts: string[] = []
  const runs: (readonly string[])[] = []
  const opened: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', (_$, e) => {
    opened.push(e.id)

    return { value: { isPlaced: true as const } }
  })
  on('session.id', () => ({ value: SELF }))
  // The engine hands hooks a resolved path, with backslashes on Windows.
  const slashed = (path: string) => path.replace(/\\/g, '/')
  on('fs.read', (_$, e) => {
    const path = slashed(e.path)
    if (path.startsWith('C:/Users/d/.claude/sessions/')) {
      const raw = REGISTRY[path.slice('C:/Users/d/.claude/sessions/'.length)]
      if (raw !== undefined) return { value: raw }
    }
    throw new Error(`ENOENT: ${path}`)
  })
  on('fs.list', (_$, e) => {
    if (slashed(e.path) !== 'C:/Users/d/.claude/sessions') throw new Error(`ENOENT: ${e.path}`)

    return {
      value: Object.keys(REGISTRY).map(name => ({ name, kind: 'file' as const, size: 1, mtimeMs: NOW, isLink: false })),
    }
  })
  on('process.run', (_$, e) => {
    runs.push(e.argv)

    return { value: { exitCode: 0, stdout: 'focused soldi-12\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })

  const { text } = await $.command.run(RUN)
  expect(text).toBe('Sessions opened: 3 registered.')
  expect(opened).toEqual(['mission-control-sessions'])

  for (const surface of ['terminal', 'desktop', 'vscode'] as const) {
    const pane = await $.ui.mount({
      plugin: 'mission-control',
      surface,
      component: 'Pane',
      requestId: 'mission-control-sessions',
      props: { title: 'Sessions', isFocused: false, bodyColumns: 100, placement: 'dock', scroll, view: {} },
    })
    expect(await pane.find({ text: /Open sessions \(3\)/ })).toBeDefined()
    expect(await pane.find({ text: /soldi-12\s+soldi\s+busy\s+5s/ })).toBeDefined()
    expect(await pane.find({ text: /this session/ })).toBeDefined()
    expect(await pane.find({ text: /terminal/ })).toBeDefined()
    // Only another VS Code session gets a switch button: not this one, not a terminal.
    expect(await pane.find({ key: `focus-${OTHER}` })).toBeDefined()
    expect(await pane.find({ key: `focus-${SELF}` })).toBeUndefined()
    expect(await pane.find({ key: `focus-${TERMINAL}` })).toBeUndefined()

    if (surface === 'terminal') {
      await pane.press({ key: `focus-${OTHER}` })
      expect(runs).toEqual([['python', 'C:/Users/d/.claude/bin/session-focus.py', '--session', OTHER]])
      expect(toasts.at(-1)).toBe('🗂 focused soldi-12')
    }
    await pane.unmount()
  }
})

test('a failed switch toasts the helper line', async ($, on) => {
  mock.clock(on, { now: NOW })
  mock.env(on, { USERPROFILE: 'C:\\Users\\d' })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true as const } }))
  on('session.id', () => ({ value: SELF }))
  on('fs.list', () => ({ value: [{ name: '25676.json', kind: 'file' as const, size: 1, mtimeMs: NOW, isLink: false }] }))
  on('fs.read', () => ({ value: REGISTRY['25676.json'] ?? '' }))
  on('process.run', () => ({
    value: { exitCode: 1, stdout: 'session process 25676 is gone (stale registry file)\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))

  await $.command.run(RUN)
  const pane = await $.ui.mount({
    plugin: 'mission-control',
    surface: 'terminal',
    component: 'Pane',
    requestId: 'mission-control-sessions',
    props: { title: 'Sessions', isFocused: false, bodyColumns: 100, placement: 'dock', scroll, view: {} },
  })
  await pane.press({ key: `focus-${OTHER}` })
  expect(toasts.at(-1)).toBe('✖ session process 25676 is gone (stale registry file)')
  await pane.unmount()
})

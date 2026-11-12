import { expect, mock, test } from 'claude-code/testing'

const LOG = 'C:/Users/d/.claude/relay/jev-log.jsonl'

const deny = (op: string) => JSON.stringify({ ts: '2026-10-06T13:00:00+0300', feature: 'risk_gate', op, decision: 'deny' })

test('a rotated or recreated Jev log alerts on the new file from its start', async ($, on) => {
  const clock = mock.clock(on, { now: Date.parse('2026-10-06T14:00:00') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\d' })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: 'C:/repo' }))
  on('session.usage', () => ({ value: { context: { tokens: 1000, window: 200_000 } } }))
  on('fs.list', () => ({ value: [] }))
  // The engine hands hooks a resolved path, with backslashes on Windows.
  const slashed = (path: string) => path.replace(/\\/g, '/')
  let log: string | undefined = `${deny('tag')}\n${deny('merge')}\n`
  on('fs.stat', (_$, e) => {
    if (slashed(e.path) !== LOG || log === undefined) throw new Error(`ENOENT: ${e.path}`)

    return { value: { kind: 'file' as const, size: log.length, mtimeMs: 0, isLink: false } }
  })
  on('fs.read', (_$, e) => {
    if (slashed(e.path) !== LOG || log === undefined) throw new Error(`ENOENT: ${e.path}`)

    return { value: log }
  })
  const denied = () => toasts.filter(text => /risk gate denied/.test(text))

  await $.session.start({ cwd: 'C:/repo', source: 'startup' })
  // Lines already in the log when this load first read it do not alert.
  expect(denied()).toEqual([])

  // Rotated: a shorter file in its place.
  log = `${deny('deploy')}\n`
  await clock.advance(5000)
  expect(denied().length).toBe(1)
  expect(denied()[0]).toMatch(/denied deploy/)

  // Gone, then back with a line of its own.
  log = undefined
  await clock.advance(5000)
  log = `${deny('release')}\n`
  await clock.advance(5000)
  expect(denied().length).toBe(2)
  expect(denied()[1]).toMatch(/denied release/)
})

test('a Jev toast masks a secret the log line carries', async ($, on) => {
  const clock = mock.clock(on, { now: Date.parse('2026-10-06T14:00:00') })
  mock.env(on, { USERPROFILE: 'C:\\Users\\d' })
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('ui.status', () => ({ value: undefined }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: 'C:/repo' }))
  on('session.usage', () => ({ value: { context: { tokens: 1000, window: 200_000 } } }))
  on('fs.list', () => ({ value: [] }))
  const slashed = (path: string) => path.replace(/\\/g, '/')
  let log = `${deny('tag')}\n`
  on('fs.stat', (_$, e) => {
    if (slashed(e.path) !== LOG) throw new Error(`ENOENT: ${e.path}`)

    return { value: { kind: 'file' as const, size: log.length, mtimeMs: 0, isLink: false } }
  })
  on('fs.read', (_$, e) => {
    if (slashed(e.path) !== LOG) throw new Error(`ENOENT: ${e.path}`)

    return { value: log }
  })

  await $.session.start({ cwd: 'C:/repo', source: 'startup' })
  log += `${deny('mysql --password hunter2 -e drop')}\n`
  await clock.advance(5000)

  const denied = toasts.filter(text => /risk gate denied/.test(text))
  expect(denied.length).toBe(1)
  expect(denied[0]).toMatch(/--password \*\*\*/)
  expect(denied[0]).not.toMatch(/hunter2/)
})

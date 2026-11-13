import { expect, mock, test } from 'claude-code/testing'

const ROOT = 'C:/home/relay'
const SID = 'sess-1'
const PATH = `${ROOT}/inplace/${SID}.json`
const NOW = 1_800_000_000_000

const slashed = (path: string) => path.replace(/\\/g, '/')

// The engine beneath the mod: an in-memory file store and a record of what the mod did, in order.
const world = (on: any, files: Record<string, string>, opts: { failClear?: boolean; failRead?: boolean; env?: Record<string, string> } = {}) => {
  const calls: string[] = []
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, opts.env ?? { CLAUDE_HOME: 'C:/home' })
  on('turn.complete', (_$: any, e: any) => ({ text: e.answer }))
  on('session.start', () => ({}))
  on('session.id', () => ({ value: SID }))
  on('ui.log', () => ({ value: undefined }))
  on('fs.exists', (_$: any, e: any) => ({ value: slashed(e.path) in files }))
  on('fs.read', (_$: any, e: any) => {
    if (opts.failRead) throw new Error('unreadable')
    return { value: files[slashed(e.path)] }
  })
  on('fs.write', (_$: any, e: any) => {
    files[slashed(e.path)] = e.text
    calls.push('claim')
    return { value: undefined }
  })
  on('command.run', (_$: any, e: any) => {
    calls.push(`command:${e.command}`)
    if (opts.failClear) throw new Error('boom')
    return { value: { text: '' } }
  })
  on('prompt.submit', (_$: any, e: any) => {
    calls.push(`submit:${e.text}`)
    return { text: e.text }
  })
  return { calls, clock }
}

const req = (over: Record<string, unknown> = {}) =>
  JSON.stringify({ v: 1, hid: 'abcdef01', title: 'Fix "the" thing', ts: NOW / 1000 - 10, ...over })

const run = async (
  $: any,
  on: any,
  files: Record<string, string>,
  event: Record<string, unknown> = {},
  opts: { failClear?: boolean; failRead?: boolean } = {},
) => {
  const { calls, clock } = world(on, files, opts)
  await $.turn.complete({ answer: 'ok', reason: 'answer', ...event } as any)
  await clock.advance(1000)
  return calls
}

test('no request file: nothing happens', async ($, on) => {
  expect(await run($, on, {})).toEqual([])
})

test('a valid request is claimed first, then clear, then submit', async ($, on) => {
  const files = { [PATH]: req() }
  const calls = await run($, on, files)
  expect(calls).toEqual(['claim', 'command:clear', 'submit:relay:abcdef01 continue "Fix the thing" from the handoff.'])
  expect(JSON.parse(files[PATH]).claimed).toBe(true)
})

test('a stale request is claimed and nothing else', async ($, on) => {
  const files = { [PATH]: req({ ts: NOW / 1000 - 300 }) }
  expect(await run($, on, files)).toEqual(['claim'])
  expect(JSON.parse(files[PATH]).claimed).toBe(true)
})

test('a request from the future is claimed and nothing else', async ($, on) => {
  expect(await run($, on, { [PATH]: req({ ts: NOW / 1000 + 60 }) })).toEqual(['claim'])
})

test('a malformed request leads to no clear and no submit', async ($, on) => {
  const calls = await run($, on, { [PATH]: req({ hid: 'XYZ' }) })
  expect(calls.filter((c) => c !== 'claim')).toEqual([])
})

test('an unparsable request leads to no clear and no submit', async ($, on) => {
  const calls = await run($, on, { [PATH]: 'not json' })
  expect(calls.filter((c) => c !== 'claim')).toEqual([])
})

test('a subagent turn.complete is ignored', async ($, on) => {
  const files = { [PATH]: req() }
  expect(await run($, on, files, { agentId: 'agent-1' })).toEqual([])
  expect(JSON.parse(files[PATH]).claimed).toBeUndefined()
})

test('a failing API call does not propagate and skips the submit', async ($, on) => {
  expect(await run($, on, { [PATH]: req() }, {}, { failClear: true })).toEqual(['claim', 'command:clear'])
})

test('an aborted turn cancels the request: claimed, no clear, no submit', async ($, on) => {
  const files = { [PATH]: req() }
  expect(await run($, on, files, { reason: 'aborted' })).toEqual(['claim'])
  const left = JSON.parse(files[PATH])
  expect(left.claimed).toBe(true)
  expect(left.cancelled).toBe(true)
  expect(left.hid).toBe('abcdef01')
})

test('an aborted turn with no request file does nothing', async ($, on) => {
  expect(await run($, on, {}, { reason: 'aborted' })).toEqual([])
})

test('an aborted subagent turn is ignored', async ($, on) => {
  const files = { [PATH]: req() }
  expect(await run($, on, files, { reason: 'aborted', agentId: 'agent-1' })).toEqual([])
  expect(JSON.parse(files[PATH]).claimed).toBeUndefined()
})

test('an aborted turn cancels a request it cannot read', async ($, on) => {
  const files = { [PATH]: req() }
  expect(await run($, on, files, { reason: 'aborted' }, { failRead: true })).toEqual(['claim'])
  const left = JSON.parse(files[PATH])
  expect(left.claimed).toBe(true)
  expect(left.cancelled).toBe(true)
})

test('the completed turn after an aborted one does not act on the cancelled request', async ($, on) => {
  const files = { [PATH]: req() }
  const { calls, clock } = world(on, files)
  await $.turn.complete({ answer: 'stopped', reason: 'aborted' } as any)
  await clock.advance(1000)
  await $.turn.complete({ answer: 'ok', reason: 'answer' } as any)
  await clock.advance(1000)
  expect(calls).toEqual(['claim'])
})

test('an abort during the settle wait stops the clear and marks the request cancelled', async ($, on) => {
  const files = { [PATH]: req() }
  const { calls, clock } = world(on, files)
  await $.turn.complete({ answer: 'ok', reason: 'answer' } as any)
  await clock.advance(100)
  expect(calls).toEqual(['claim'])
  await $.turn.complete({ answer: 'stopped', reason: 'aborted' } as any)
  await clock.advance(1000)
  expect(calls).toEqual(['claim', 'claim'])
  const left = JSON.parse(files[PATH])
  expect(left.claimed).toBe(true)
  expect(left.cancelled).toBe(true)
  expect(left.hid).toBe('abcdef01')
})

test('a request written just after an abort is cancelled', async ($, on) => {
  const files: Record<string, string> = {}
  const { calls, clock } = world(on, files)
  await $.turn.complete({ answer: 'stopped', reason: 'aborted' } as any)
  await clock.advance(3000)
  files[PATH] = req({ ts: NOW / 1000 + 2 })
  await $.turn.complete({ answer: 'ok', reason: 'answer' } as any)
  await clock.advance(1000)
  expect(calls).toEqual(['claim'])
  expect(JSON.parse(files[PATH]).cancelled).toBe(true)
})

test('a request written well after an abort is acted on', async ($, on) => {
  const files: Record<string, string> = {}
  const { calls, clock } = world(on, files)
  await $.turn.complete({ answer: 'stopped', reason: 'aborted' } as any)
  await clock.advance(30_000)
  files[PATH] = req({ ts: NOW / 1000 + 20 })
  await $.turn.complete({ answer: 'ok', reason: 'answer' } as any)
  await clock.advance(1000)
  expect(calls).toEqual(['claim', 'command:clear', 'submit:relay:abcdef01 continue "Fix the thing" from the handoff.'])
  expect(JSON.parse(files[PATH]).cancelled).toBeUndefined()
})

test('a read failure leaves the request unclaimed: no claim, no clear, no submit', async ($, on) => {
  const files = { [PATH]: req() }
  expect(await run($, on, files, {}, { failRead: true })).toEqual([])
  expect(JSON.parse(files[PATH]).claimed).toBeUndefined()
})

test('an already-claimed request is skipped', async ($, on) => {
  const files = { [PATH]: JSON.stringify({ v: 1, claimed: true, hid: 'abcdef01', at: NOW }) }
  expect(await run($, on, files)).toEqual([])
})

const sessionStart = async ($: any, on: any, env: Record<string, string>) => {
  const sets: Record<string, string>[] = []
  mock.env(on, env)
  on('session.start', () => ({ cwd: 'C:/work' }))
  on('session.id', () => ({ value: SID }))
  on('ui.log', () => ({ value: undefined }))
  on('env.set', (_$: any, e: any) => {
    sets.push({ name: e.name, value: e.value })
    return { value: {} }
  })
  await $.session.start({ cwd: 'C:/work' } as any)
  return sets
}

test('session.start ignores CLAUDE_RELAY_ROOT, as relay.py does', async ($, on) => {
  expect(await sessionStart($, on, { CLAUDE_RELAY_ROOT: 'C:/elsewhere', CLAUDE_HOME: 'C:/home' })).toEqual([
    { name: 'CLAUDE_RELAY_INPLACE_MOD', value: ROOT },
  ])
})

test('session.start ignores CLAUDE_RELAY_ROOT without CLAUDE_HOME too', async ($, on) => {
  const sets = await sessionStart($, on, { CLAUDE_RELAY_ROOT: 'C:/elsewhere' })
  expect(sets.length).toBe(1)
  expect(sets[0].value.endsWith('/../../relay')).toBe(true)
  expect(sets[0].value.startsWith('C:/elsewhere')).toBe(false)
})

test('session.start: an empty CLAUDE_HOME falls back to the plugin root', async ($, on) => {
  const sets = await sessionStart($, on, { CLAUDE_HOME: '' })
  expect(sets.length).toBe(1)
  expect(sets[0].value.endsWith('/../../relay')).toBe(true)
})

test('session.start: default root from CLAUDE_HOME', async ($, on) => {
  expect(await sessionStart($, on, { CLAUDE_HOME: 'C:/home' })).toEqual([
    { name: 'CLAUDE_RELAY_INPLACE_MOD', value: 'C:/home/relay' },
  ])
})

test('session.start: default root from the plugin root', async ($, on) => {
  const sets = await sessionStart($, on, {})
  expect(sets.length).toBe(1)
  expect(sets[0].value.endsWith('/../../relay')).toBe(true)
  expect(sets[0].value.startsWith('C:/home')).toBe(false)
})

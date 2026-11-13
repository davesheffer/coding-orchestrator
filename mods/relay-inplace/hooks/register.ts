import type { Register } from 'claude-code'

// Relay in-place rollover. relay.py (`handoff`, rollover "inplace") leaves <relay root>/inplace/<session>.json;
// when the main loop's turn completes, this mod claims that request and, if it is fresh, clears the tab
// and submits the relay:<id> prompt. Without a request file it does nothing (one fs.exists per turn).
// An aborted turn cancels the request: the user took over, so the tab is not cleared behind them.

const SESSION_RE = /^[A-Za-z0-9_-]{1,128}$/
const HID_RE = /^[a-f0-9]{8}$/
const MAX_AGE_MS = 180_000
const SETTLE_MS = 500
const TITLE_LIMIT = 120
// A handoff command still running when the turn was aborted can write its request just after the abort.
const ABORT_GRACE_MS = 5_000

let acting = false
let aborts = 0
let abortedAt = Number.NEGATIVE_INFINITY

const debug = ($: any, text: string) => {
  try {
    $.ui.log(`relay-inplace: ${text}`, { to: 'debug' })
  } catch {
    // logging must never break the session
  }
}

const cleanTitle = (value: string) => value.replace(/[\u0000-\u001f\u007f-\u009f"]/g, '').slice(0, TITLE_LIMIT)

// Same rule as relay.py's ROOT: $CLAUDE_HOME/relay, else the relay folder beside the mods folder. CLAUDE_HOME is
// used as written (relay.py expands a leading ~), so it must be absolute. CLAUDE_RELAY_ROOT is not read.
// relay.py warns when CLAUDE_RELAY_INPLACE_MOD differs from its ROOT.
const relayRoot = async ($: any) => {
  const home = await $.env.get('CLAUDE_HOME')
  if (typeof home === 'string' && home !== '') return `${home}/relay`
  return `${$.plugin.root}/../../relay`
}

// Recorded for every aborted main-loop turn, also while a request is being acted on.
const noteAbort = async ($: any) => {
  aborts += 1
  try {
    abortedAt = await $.clock.now()
  } catch (error) {
    debug($, `clock failed on abort: ${String(error)}`)
  }
}

const act = async ($: any, aborted: boolean) => {
  try {
    const seen = aborts
    const sid = await $.session.id()
    if (typeof sid !== 'string' || !SESSION_RE.test(sid)) return
    const path = `${await relayRoot($)}/inplace/${sid}.json`
    if (!(await $.fs.exists(path))) return

    let text = ''
    try {
      text = await $.fs.read(path)
    } catch (error) {
      if (!aborted) {
        debug($, `read failed, request left unclaimed: ${String(error)}`)
        return
      }
      // Aborted: cancel the request even though it cannot be read.
    }
    let request: any
    try {
      request = JSON.parse(text)
    } catch {
      request = undefined
    }
    if (request && typeof request === 'object' && request.claimed === true) return

    const now = await $.clock.now()
    const valid =
      request &&
      typeof request === 'object' &&
      request.v === 1 &&
      typeof request.hid === 'string' &&
      HID_RE.test(request.hid) &&
      typeof request.ts === 'number' &&
      Number.isFinite(request.ts) &&
      typeof request.title === 'string'
    // Cancelled by this aborted turn, or written by a turn that was aborted (up to the grace after it).
    const cancelled = aborted || (valid && request.ts * 1000 <= abortedAt + ABORT_GRACE_MS)

    // Claim before anything else: a request is used at most once, valid or not.
    await $.fs.write(
      path,
      JSON.stringify({
        v: 1,
        claimed: true,
        ...(valid ? { hid: request.hid } : {}),
        at: now,
        ...(cancelled ? { cancelled: true } : {}),
      }),
    )
    if (cancelled) {
      debug($, `request${valid ? ` ${request.hid}` : ''} cancelled: the turn was aborted`)
      return
    }
    if (!valid) {
      debug($, 'malformed request ignored')
      return
    }

    const age = now - request.ts * 1000
    if (age < 0 || age > MAX_AGE_MS) {
      debug($, `request ${request.hid} not fresh (age ${Math.round(age / 1000)} s); ignored`)
      return
    }

    const title = cleanTitle(request.title)
    await $.clock.sleep(SETTLE_MS)
    if (aborts !== seen) {
      // A turn was aborted during the wait: stand down, and say so in the file for relay.py.
      await $.fs.write(path, JSON.stringify({ v: 1, claimed: true, hid: request.hid, at: now, cancelled: true }))
      debug($, `request ${request.hid} cancelled: a turn was aborted before the clear`)
      return
    }
    await $.command.run({ command: 'clear' })
    await $.prompt.submit({ text: `relay:${request.hid} continue "${title}" from the handoff.` })
    debug($, `request ${request.hid} done`)
  } catch (error) {
    debug($, `error: ${String(error)}`)
  }
}

const start = ($: any, aborted: boolean) => {
  if (acting) return
  acting = true
  void act($, aborted).finally(() => {
    acting = false
  })
}

export const register: Register = (on) => {
  on('session.start', async ($: any, e: any, next: any) => {
    const result = await next(e)
    try {
      const result = await $.env.set('CLAUDE_RELAY_INPLACE_MOD', await relayRoot($))
      if (result && typeof result.deny === 'string') debug($, `env set refused: ${result.deny}`)
    } catch (error) {
      debug($, `env set failed: ${String(error)}`)
    }
    return result
  })

  on('turn.complete', async ($: any, e: any, next: any) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      const aborted = e.reason === 'aborted'
      if (aborted) await noteAbort($)
      start($, aborted)
    }
    return result
  })
}

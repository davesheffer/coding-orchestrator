import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, ToolCallResult } from 'claude-code'

import type { GuardLedger, Touched } from '../types'
import {
  cardReminder,
  clip,
  EMPTY_LEDGER,
  isCheck,
  isOutward,
  isRisky,
  mainOnlyLabel,
  needsCheck,
  outwardBlockers,
  parseCard,
  roleFor,
  routeVerdict,
  settle,
  shellWrites,
  summary,
  touch,
} from './policy'

const ledger = atom({ plugin: 'orch-guard', key: 'ledger' } as const, EMPTY_LEDGER)
const spawned = atom({ plugin: 'orch-guard', key: 'spawned' } as const, {})

type Mode = 'enforce' | 'warn' | 'off'

/** Origins that are the person's own gesture: a waiver or reset from anywhere else is refused. */
const PERSON = new Set(['composer', 'bridge', 'sdk'])

/** Entries an earlier version stored as bare paths read as touched at 0. */
function normalise(raw: GuardLedger): GuardLedger {
  const fix = (list: readonly (Touched | string)[]): Touched[] => list.map(one => (typeof one === 'string' ? { path: one, at: 0 } : one))

  return { ...raw, uncheckedEdits: fix(raw.uncheckedEdits), riskyPending: fix(raw.riskyPending) }
}

async function current($: EngineInterface): Promise<GuardLedger> {
  return normalise(await read($, ledger))
}

/** Applies `fn` with compare-and-set, so parallel tool calls never drop each other's entries. */
async function change($: EngineInterface, fn: (one: GuardLedger) => GuardLedger) {
  await update($, ledger, raw => fn(normalise(raw)))
  const text = summary(await current($))
  $.ui.status(text === '' ? undefined : text)
}

/** Adds the reminder the model reads after the tool's answer; a deny is left as it is. */
function remind(result: ToolCallResult, text: string): ToolCallResult {
  return result.deny !== undefined ? result : ({ ...result, context: [...(result.context ?? []), text] } as ToolCallResult)
}

/** Refuses the call (enforce), or lets it run with a reminder (warn). */
async function refuse($: EngineInterface, mode: Mode, run: () => Promise<ToolCallResult>, reason: string): Promise<ToolCallResult> {
  await change($, one => ({ ...one, blocked: one.blocked + 1 }))
  if (mode === 'enforce') return { deny: `orch-guard: ${reason}` }

  $.ui.toast(`orch-guard ⚠ ${clip(reason, 110)}`, { timeoutMs: 8000 })

  return remind(await run(), `orch-guard (warn mode) flagged this call: ${reason}`)
}

export const register: Register = (on, options) => {
  const mode = (options.mode as Mode | undefined) ?? 'enforce'
  const riskyPattern = typeof options.riskyPattern === 'string' ? options.riskyPattern : ''
  const checkPattern = typeof options.checkPattern === 'string' ? options.checkPattern : ''
  let lastToast = ''

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'orch-guard',
      description: 'orch-guard: show what blocks push/publish (`waive <reason>` opens the gate until the next edit, `reset` clears)',
    })
    await change($, one => one)

    return next(e)
  })

  on('command.run', { command: 'orch-guard' }, async ($, e) => {
    const [verb = '', ...rest] = e.args.trim().split(/\s+/)

    if ((verb === 'waive' || verb === 'reset') && !PERSON.has(e.origin.kind)) {
      return { text: `orch-guard: only the person can ${verb} the gate.` }
    }
    if (verb === 'waive') {
      const reason = rest.join(' ').trim() || 'waived by the user'
      const at = await $.clock.now()
      await change($, one => ({ ...one, waiver: { at, reason } }))

      return { text: `orch-guard: the push/publish gate is open until the next edit (${reason}).` }
    }
    if (verb === 'reset') {
      await change($, () => EMPTY_LEDGER)

      return { text: 'orch-guard: ledger cleared.' }
    }

    const now = await current($)
    const blockers = outwardBlockers(now)
    const lines = [
      `orch-guard (${mode}) — ${summary(now) || 'nothing pending'}`,
      `last check: ${now.lastCheck === null ? 'none' : `${now.lastCheck.isPassing ? 'passed' : 'FAILED'} — ${clip(now.lastCheck.command, 70)}`}`,
      `critic: ${now.critic === null ? 'none this session' : now.critic.verdict}`,
      `calls refused or flagged: ${now.blocked}`,
      blockers.length === 0 ? 'push/publish: clear' : `push/publish blocked:\n- ${blockers.join('\n- ')}`,
      now.waiver === null ? '' : `waiver active until the next edit: ${now.waiver.reason}`,
    ]

    return { text: lines.filter(Boolean).join('\n') }
  })

  if (mode !== 'off') {
    // Routing: named roles on their models, no inherited main model, no recursive delegation.
    on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
      if (e.tool !== 'Agent') return next(e)

      const reason = routeVerdict({ subagent_type: e.subagent_type, model: e.model }, e.agentId !== undefined)
      if (reason !== undefined) return refuse($, mode, () => next(e), reason)

      const result = await next(e)
      const role = roleFor(e.subagent_type, e.model)
      // A background agent's card arrives at its turn end, not here; a foreground ask can still launch in the background.
      const status = (result.result as { status?: string } | undefined)?.status
      if (role === undefined || e.run_in_background !== false || status !== 'completed' || result.text === undefined) return result

      const reminder = cardReminder(role, parseCard(result.text))

      return reminder === undefined ? result : remind(result, reminder)
    }).catch(($, e, next) => next(e))
  }

  // One hook for every tool call: the main-session-only and ship-boundary gates, then tracking.
  on('tool.call', async ($, e, next) => {
    const command = e.tool === 'Bash' ? e.command : undefined

    if (mode !== 'off') {
      const label = e.agentId === undefined ? undefined : mainOnlyLabel(e.tool, command)
      if (label !== undefined) {
        return refuse($, mode, () => next(e), `${label} stays in the main session. Stop and report what should be done; the orchestrator runs it.`)
      }

      if (e.agentId === undefined && isOutward(e.tool, command)) {
        const now = await current($)
        const blockers = outwardBlockers(now)
        if (now.waiver !== null) {
          $.ui.toast(`orch-guard: gate waived (${clip(now.waiver.reason, 60)})`)
        } else if (blockers.length > 0) {
          return refuse($, mode, () => next(e), `not ready to ship. ${blockers.join(' ')} If the user accepts the risk, they can run \`/orch-guard waive <reason>\`.`)
        }
      }
    }

    const startedAt = await $.clock.now()
    const result = await next(e)
    if (result.deny !== undefined || next.signal.aborted) return result

    const path = e.tool === 'Edit' || e.tool === 'Write' ? e.file_path : e.tool === 'NotebookEdit' ? e.notebook_path : undefined
    if (path !== undefined && result.isError === undefined) {
      const at = await $.clock.now()
      await change($, one => ({
        ...one,
        uncheckedEdits: needsCheck(path) ? touch(one.uncheckedEdits, path, at) : one.uncheckedEdits,
        riskyPending: isRisky(path, riskyPattern) ? touch(one.riskyPending, path, at) : one.riskyPending,
        waiver: null,
      }))
    } else if (e.tool === 'Bash' && command !== undefined) {
      const record = result.result as { backgroundTaskId?: string; interrupted?: boolean } | undefined
      const isBackground = e.run_in_background === true || record?.backgroundTaskId !== undefined

      if (isCheck(command, checkPattern) && !isBackground && record?.interrupted !== true) {
        // Only changes made before this check started are covered by it.
        const isPassing = result.isError !== true
        await change($, one => ({
          ...one,
          uncheckedEdits: isPassing ? settle(one.uncheckedEdits, startedAt) : one.uncheckedEdits,
          lastCheck: { at: startedAt, command, isPassing },
        }))
      } else if (result.isReadOnly !== true) {
        const written = shellWrites(command)
        if (written.length > 0) {
          const at = await $.clock.now()
          const label = `bash: ${clip(command, 50)}`
          const risky = written.filter(one => one !== '?' && isRisky(one, riskyPattern))
          // An inferred shell write keeps the person's waiver: only the Edit tools close it.
          await change($, one => ({
            ...one,
            uncheckedEdits: written.some(path => path === '?' || needsCheck(path)) ? touch(one.uncheckedEdits, label, at) : one.uncheckedEdits,
            riskyPending: risky.reduce((list, path) => touch(list, path, at), one.riskyPending),
          }))
        }
      }
    }

    return result
    // A guard failure never blocks ordinary work; an outward ship it could not check is refused instead.
  }).catch(($, e, next) => {
    let isShip = false
    try {
      isShip = mode === 'enforce' && !next.called && e.agentId === undefined && isOutward(e.tool, e.tool === 'Bash' ? e.command : undefined)
    } catch {
      isShip = false
    }

    return isShip ? { deny: 'orch-guard: its ship check failed, so this push/publish is held. The person can run `/orch-guard reset` and retry.' } : next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    const agentId = 'agentId' in result ? result.agentId : undefined
    if (agentId !== undefined) {
      const model = e.model ?? ('model' in result ? result.model : undefined)
      const at = await $.clock.now()
      // A critic's SHIP covers only what was pending when it started.
      const reviews = roleFor(e.subagentType, model) === 'critic' ? (await current($)).riskyPending.map(one => one.path) : undefined
      await update($, spawned, map => ({ ...map, [agentId]: { type: e.subagentType, model, isBackground: e.background, at, reviews } }))
    }

    return result
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const agentId = e.agentId

    if (agentId === undefined) {
      const now = await current($)
      const text = outwardBlockers(now).length > 0 && mode !== 'off' ? summary(now) : ''
      // Once per change of what is pending, not on every chat turn.
      if (text !== '' && text !== lastToast) $.ui.toast(`orch-guard: ${text} — verify before calling it done`, { timeoutMs: 6000 })
      lastToast = text

      return next(e)
    }

    const agent = (await read($, spawned))[agentId]
    await update($, spawned, map => Object.fromEntries(Object.entries(map).filter(([id]) => id !== agentId)))
    const role = roleFor(agent?.type, agent?.model)
    if (agent === undefined || role === undefined || e.reason !== 'answer') return next(e)

    const card = parseCard(e.answer ?? '')
    if (role === 'critic') {
      const at = await $.clock.now()
      await change($, one => ({
        ...one,
        critic: { at, verdict: card.verdict },
        riskyPending: card.verdict === 'SHIP' ? settle(one.riskyPending, agent.at, agent.reviews ?? []) : one.riskyPending,
      }))
    }

    const reminder = cardReminder(role, card)
    if (agent.isBackground && reminder !== undefined && mode !== 'off') {
      await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: reminder }] } }).catch(() => undefined)
    }

    return next(e)
  }).catch(($, e, next) => next(e))
}

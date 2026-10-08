import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, ToolCallResult } from 'claude-code'

import {
  addUnique,
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
  summary,
} from './policy'

const ledger = atom({ plugin: 'orch-guard', key: 'ledger' } as const, EMPTY_LEDGER)
const spawned = atom({ plugin: 'orch-guard', key: 'spawned' } as const, {})

type Mode = 'enforce' | 'warn' | 'off'

/** Origins that are the person's own gesture: a waiver from anywhere else is refused. */
const PERSON = new Set(['composer', 'bridge', 'sdk'])

async function refresh($: EngineInterface) {
  const text = summary(await read($, ledger))
  $.ui.status(text === '' ? undefined : text)
}

/** Appends the reminder the model reads after the tool's result; an error or a deny is left as it is. */
function remind(result: ToolCallResult, text: string): ToolCallResult {
  return 'result' in result && result.result !== undefined && result.isError === undefined
    ? { ...result, context: [...(result.context ?? []), text] }
    : result
}

/** Refuses the call (enforce), or lets it run with a reminder (warn). */
async function refuse($: EngineInterface, mode: Mode, run: () => Promise<ToolCallResult>, reason: string): Promise<ToolCallResult> {
  await update($, ledger, one => ({ ...one, blocked: one.blocked + 1 }))
  if (mode === 'enforce') return { deny: `orch-guard: ${reason}` }

  $.ui.toast(`orch-guard ⚠ ${clip(reason, 110)}`, { timeoutMs: 8000 })

  return remind(await run(), `orch-guard (warn mode) flagged this call: ${reason}`)
}

export const register: Register = (on, options) => {
  const mode = (options.mode as Mode | undefined) ?? 'enforce'
  const riskyPattern = typeof options.riskyPattern === 'string' ? options.riskyPattern : ''

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'orch-guard',
      description: 'orch-guard: show what blocks push/publish (`waive <reason>` lets the next one through, `reset` clears)',
    })
    await refresh($)

    return next(e)
  })

  on('command.run', { command: 'orch-guard' }, async ($, e) => {
    const [verb = '', ...rest] = e.args.trim().split(/\s+/)

    if (verb === 'waive') {
      if (!PERSON.has(e.origin.kind)) return { text: 'orch-guard: only the person can waive the gate.' }
      const reason = rest.join(' ').trim() || 'waived by the user'
      const at = await $.clock.now()
      await update($, ledger, one => ({ ...one, waiver: { at, reason } }))
      await refresh($)

      return { text: `orch-guard: the push/publish gate is waived until the next edit (${reason}).` }
    }
    if (verb === 'reset') {
      await update($, ledger, () => EMPTY_LEDGER)
      await refresh($)

      return { text: 'orch-guard: ledger cleared.' }
    }

    const now = await read($, ledger)
    const blockers = outwardBlockers(now)
    const lines = [
      `orch-guard (${mode}) — ${summary(now) || 'nothing pending'}`,
      `last check: ${now.lastCheck === null ? 'none' : `${now.lastCheck.isPassing ? 'passed' : 'FAILED'} — ${clip(now.lastCheck.command, 70)}`}`,
      `critic: ${now.critic === null ? 'none this session' : now.critic.verdict}`,
      `calls refused or flagged: ${now.blocked}`,
      blockers.length === 0 ? 'push/publish: clear' : `push/publish blocked:\n- ${blockers.join('\n- ')}`,
      now.waiver === null ? '' : `waiver active: ${now.waiver.reason}`,
    ]

    return { text: lines.filter(Boolean).join('\n') }
  })

  if (mode !== 'off') {
    // Routing: named roles on their models, no inherited Opus, no recursive delegation.
    on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
      if (e.tool !== 'Agent') return next(e)

      const reason = routeVerdict({ subagent_type: e.subagent_type, model: e.model }, e.agentId !== undefined)
      if (reason !== undefined) return refuse($, mode, () => next(e), reason)

      const result = await next(e)
      const role = roleFor(e.subagent_type, e.model)
      // A background agent's card arrives at its turn end, not here.
      if (role === undefined || e.run_in_background !== false || result.text === undefined) return result

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
        const now = await read($, ledger)
        const blockers = outwardBlockers(now)
        if (now.waiver !== null) {
          $.ui.toast(`orch-guard: gate waived (${clip(now.waiver.reason, 60)})`)
        } else if (blockers.length > 0) {
          return refuse($, mode, () => next(e), `not ready to ship. ${blockers.join(' ')} If the user accepts the risk, they can run \`/orch-guard waive <reason>\`.`)
        }
      }
    }

    const result = await next(e)
    if (result.deny !== undefined || next.signal.aborted) return result

    const path = e.tool === 'Edit' || e.tool === 'Write' ? e.file_path : e.tool === 'NotebookEdit' ? e.notebook_path : undefined
    if (path !== undefined && result.isError === undefined) {
      const isCode = needsCheck(path)
      const isRiskyEdit = isRisky(path, riskyPattern)
      await update($, ledger, one => ({
        ...one,
        uncheckedEdits: isCode ? addUnique(one.uncheckedEdits, path) : one.uncheckedEdits,
        riskyPending: isRiskyEdit ? addUnique(one.riskyPending, path) : one.riskyPending,
        waiver: null,
      }))
      await refresh($)
    } else if (command !== undefined && isCheck(command)) {
      const at = await $.clock.now()
      const isPassing = result.isError !== true
      await update($, ledger, one => ({
        ...one,
        uncheckedEdits: isPassing ? [] : one.uncheckedEdits,
        lastCheck: { at, command, isPassing },
      }))
      await refresh($)
    }

    return result
    // A guard failure never blocks work: the call goes on as if the guard were absent.
  }).catch(($, e, next) => next(e))

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    const agentId = 'agentId' in result ? result.agentId : undefined
    if (agentId !== undefined) {
      await update($, spawned, map => ({ ...map, [agentId]: { type: e.subagentType, model: e.model ?? ('model' in result ? result.model : undefined), isBackground: e.background } }))
    }

    return result
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const agentId = e.agentId

    if (agentId === undefined) {
      const now = await read($, ledger)
      const open = outwardBlockers(now)
      if (open.length > 0 && mode !== 'off') $.ui.toast(`orch-guard: ${summary(now)} — verify before calling it done`, { timeoutMs: 6000 })

      return next(e)
    }

    const agent = (await read($, spawned))[agentId]
    const role = roleFor(agent?.type, agent?.model)
    if (role === undefined || e.reason !== 'answer') return next(e)

    const card = parseCard(e.answer ?? '')
    if (role === 'critic') {
      const at = await $.clock.now()
      await update($, ledger, one => ({
        ...one,
        critic: { at, verdict: card.verdict },
        riskyPending: card.verdict === 'SHIP' ? [] : one.riskyPending,
      }))
      await refresh($)
    }

    const reminder = cardReminder(role, card)
    if (agent?.isBackground === true && reminder !== undefined && mode !== 'off') {
      await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: reminder }] } }).catch(() => undefined)
    }

    return next(e)
  }).catch(($, e, next) => next(e))
}


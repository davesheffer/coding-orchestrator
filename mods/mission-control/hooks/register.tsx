import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, ToolCallResult } from 'claude-code'

import type {
  ActivityEntry,
  AgentRun,
  Gauge,
  HunchCall,
  HunchConstraint,
  HunchLevel,
  HunchLog,
  JevStatus,
  ReportCard,
  SessionRow,
  Zone,
} from '../types'
import {
  ago,
  bar,
  capDetail,
  clip,
  clockSeconds,
  clockTime,
  describeTool,
  hunchCli,
  hunchInvocation,
  hunchName,
  hunchOutcome,
  isRole,
  jevStatus,
  kTokens,
  localDay,
  maxLevel,
  parseCard,
  parseLines,
  reapStale,
  repoName,
  roleOf,
  seconds,
  sessionRow,
  sessionRows,
  shortModel,
  STALE_MS,
  toolDetail,
  wasInterrupted,
  zoneOf,
} from './parse'

const PANE = 'mission-control'
const SESSIONS_PANE = 'mission-control-sessions'
const POLL_MS = 5000

const agents = atom({ plugin: 'mission-control', key: 'agents' } as const, [])
const reports = atom({ plugin: 'mission-control', key: 'reports' } as const, [])
const gauge = atom({ plugin: 'mission-control', key: 'gauge' } as const, null)
const jev = atom({ plugin: 'mission-control', key: 'jev' } as const, null)
const hunch = atom({ plugin: 'mission-control', key: 'hunch' } as const, { calls: [], total: 0, seenConstraints: [] })
const isBandHidden = atom({ plugin: 'mission-control', key: 'isBandHidden' } as const, false)
const tick = atom({ plugin: 'mission-control', key: 'tick' } as const, 0)
const sessions = atom({ plugin: 'mission-control', key: 'sessions' } as const, [])
const sessionsCheckedAt = atom({ plugin: 'mission-control', key: 'sessionsCheckedAt' } as const, 0)
const activity = atom({ plugin: 'mission-control', key: 'activity' } as const, [])
const showDetails = atom({ plugin: 'mission-control', key: 'showDetails' } as const, false)
const openEntries = atom({ plugin: 'mission-control', key: 'openEntries' } as const, [])
// Set by a prompt, cleared when the main loop's turn ends: the timeline's prompt line can scroll out mid-turn.
const turnOpen = atom({ plugin: 'mission-control', key: 'turnOpen' } as const, false)

const DRAWER_LINES = 20

const HUNCH_KEEP = 40
const ACTIVITY_KEEP = 80
const TIMELINE_ROWS = 15

const ACTIVITY_MARK: Record<ActivityEntry['status'], string> = { running: '▶', ok: '✔', error: '✖', stopped: '■', note: '·' }

const ACTIVITY_COLOR: Record<ActivityEntry['status'], string | undefined> = {
  running: 'yellow',
  ok: 'green',
  error: 'red',
  stopped: 'gray',
  note: undefined,
}

const ZONE_PLAIN: Record<Zone, string> = {
  green: 'keep going',
  amber: 'getting full, hand off soon',
  red: 'full, hand off now',
  unknown: 'not measured yet',
}

const HUNCH_COLOR: Record<HunchLevel, string | undefined> = { info: undefined, warn: 'yellow', alert: 'red' }

const HUNCH_MARK: Record<HunchCall['status'], string> = { running: '…', ok: '✔', error: '✖', denied: '⛔' }

const ZONE_COLOR: Record<Zone, string> = { green: 'green', amber: 'yellow', red: 'red', unknown: 'gray' }

const ZONE_HINT: Record<Zone, string> = {
  green: 'continue',
  amber: 'delegate reads, hand off at the next boundary',
  red: 'write a relay handoff before new work',
  unknown: '',
}

const CONFIDENCE_COLOR: Record<ReportCard['confidence'], string> = {
  high: 'green',
  medium: 'yellow',
  low: 'red',
  missing: 'red',
}

// A hot reload evaluates this module afresh, so these start over; everything drawn lives in $.state.
let homeDir = ''
let relayDir = ''
let soft = 150_000
let hard = 250_000
let isJevEnabled = true
let logSize = -1
let alertCursor = -1

async function loadRelay($: EngineInterface) {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
  homeDir = home.replace(/\\/g, '/')
  relayDir = `${homeDir}/.claude/relay`

  try {
    const config = JSON.parse(await $.fs.read(`${relayDir}/config.json`)) as {
      soft_tokens?: number
      hard_tokens?: number
      jev?: { enabled?: boolean }
    }
    soft = config.soft_tokens ?? soft
    hard = config.hard_tokens ?? hard
    isJevEnabled = config.jev?.enabled !== false
  } catch {
    // no relay installed: default thresholds, the gauge still draws
  }
}

async function measure($: EngineInterface, tokens: number | undefined, window: number, percent?: number, usd?: number) {
  const zone = zoneOf(tokens, soft, hard)
  const before = await read($, gauge)
  const next: Gauge = { tokens, window, percent, zone, soft, hard, usd }
  await update($, gauge, () => next)

  if (before !== null && before.zone !== zone && zone !== 'unknown' && before.zone !== 'unknown') {
    $.ui.toast(`Relay ${zone.toUpperCase()} at ${kTokens(tokens)}: ${ZONE_HINT[zone]}`, { timeoutMs: 8000 })
  }
  await refreshStatus($)
}

async function pollJev($: EngineInterface) {
  const path = `${relayDir}/jev-log.jsonl`
  const stat = await $.fs.stat(path).catch(() => undefined)

  if (stat === undefined) {
    await update($, jev, () => ({ isOnline: null, spendToday: 0, deniesToday: 0, weakToday: 0, recent: [], note: 'no jev-log.jsonl' }))
    return
  }
  if (stat.size === logSize) return
  logSize = stat.size

  let raw: string
  try {
    raw = await $.fs.read(path)
  } catch {
    await update($, jev, current => ({
      ...(current ?? { isOnline: null, spendToday: 0, deniesToday: 0, weakToday: 0, recent: [] }),
      note: 'jev-log.jsonl unreadable (over 4 MiB?)',
    }))
    return
  }

  const lines = parseLines(raw)
  const before = await read($, jev)
  const status: JevStatus = jevStatus(lines, localDay(await $.clock.now()), isJevEnabled)
  await update($, jev, () => status)

  // Alert only on lines written after this load first read the log.
  if (alertCursor >= 0) {
    for (const line of lines.slice(alertCursor)) {
      if (line.feature === 'risk_gate' && line.decision === 'deny') {
        const why = line.fail_closed === true ? `fail-closed, Jev ${String(line.error ?? 'unavailable')}` : 'risky'
        $.ui.toast(`⛔ Jev risk gate denied ${String(line.op ?? 'the action')} (${why})`, { timeoutMs: 10000 })
      } else if (line.feature === 'report_check' && (line.decision === 'deny' || line.decision === 'weak')) {
        $.ui.toast(`⚠ Jev: ${String(line.subagent_type ?? 'subagent')} report ${String(line.decision)}`, { timeoutMs: 8000 })
      }
    }
  }
  alertCursor = lines.length

  if (before !== null && before.isOnline === true && status.isOnline === false) {
    $.ui.toast(`Jev went offline: ${status.lastError ?? 'unavailable'}; risk gate now fails closed`, { timeoutMs: 10000 })
  }
  await refreshStatus($)
}

/** The calls that belong to the current task, or every call while no task id has been seen. */
function taskCalls(log: HunchLog): HunchCall[] {
  return log.taskId === undefined ? log.calls : log.calls.filter(call => call.taskId === undefined || call.taskId === log.taskId)
}

function loudest(calls: HunchCall[]): HunchLevel {
  return calls.reduce<HunchLevel>((level, call) => maxLevel(level, call.level), 'info')
}

function resultText(result: ToolCallResult): string {
  if (result.deny !== undefined) return result.deny
  if (result.text !== undefined) return result.text
  if (typeof result.result === 'string') return result.result

  return result.result === undefined ? '' : JSON.stringify(result.result)
}

async function startHunchCall(
  $: EngineInterface,
  id: string,
  agentId: string | undefined,
  invocation: { name: string; target: string; taskId?: string },
) {
  const caller = agentId === undefined ? undefined : (await read($, agents)).find(agent => agent.id === agentId)
  const call: HunchCall = {
    id,
    name: invocation.name,
    target: invocation.target,
    taskId: invocation.taskId,
    role: agentId === undefined ? 'main' : roleOf(caller?.type ?? 'subagent'),
    startedAt: await $.clock.now(),
    status: 'running',
    summary: '',
    level: 'info',
  }
  await update($, hunch, log => ({
    ...log,
    taskId: agentId === undefined ? (invocation.taskId ?? log.taskId) : log.taskId,
    total: log.total + 1,
    calls: [...reapStale(log.calls, call.startedAt).filter(one => one.id !== id), call].slice(-HUNCH_KEEP),
  }))
  await refreshStatus($)
}

async function finishHunchCall($: EngineInterface, id: string, result: ToolCallResult) {
  const log = await read($, hunch)
  const call = log.calls.find(one => one.id === id)
  if (call === undefined) return

  const now = await $.clock.now()
  const isDenied = result.deny !== undefined

  // The person stopped it: no verdict on Hunch, so no failure toast and no finish check.
  if (!isDenied && wasInterrupted(result.result, resultText(result))) {
    const stopped: HunchCall = { ...call, status: 'error', durationMs: now - call.startedAt, summary: 'interrupted', level: 'info' }
    await update($, hunch, current => ({ ...current, calls: current.calls.map(one => (one.id === id ? stopped : one)) }))
    await refreshStatus($)

    return
  }

  const isError = isDenied || result.isError === true
  const outcome = hunchOutcome(call.name, resultText(result), isError)
  const taskId = call.taskId ?? outcome.taskId
  let { level, summary } = outcome

  // A task closed through the tool without the brief Hunch says to read first.
  const isFinish = call.name === 'task' && call.target.startsWith('finish')
  const hadContext = log.calls.some(
    one => one.name === 'context' && (taskId === undefined || one.taskId === undefined || one.taskId === taskId),
  )
  if (isFinish && !isError && !hadContext) {
    level = maxLevel(level, 'warn')
    summary = `no hunch_context this task · ${summary}`
  }

  let fresh: HunchConstraint[] = []
  const done: HunchCall = {
    ...call,
    taskId,
    status: isDenied ? 'denied' : isError ? 'error' : 'ok',
    durationMs: now - call.startedAt,
    summary,
    level,
  }
  // Read seenConstraints inside the write, so parallel checks of one invariant toast it once.
  // 5: a task id in an output only moves the current task when the main loop started it.
  const isMainStart = call.role === 'main' && call.name === 'task' && call.target.startsWith('start')
  await update($, hunch, current => {
    fresh = outcome.constraints.filter(one => one.severity !== 'advisory' && !current.seenConstraints.includes(one.id))

    return {
      ...current,
      taskId: isMainStart ? (outcome.taskId ?? current.taskId) : current.taskId,
      seenConstraints: [...current.seenConstraints, ...fresh.map(one => one.id)].slice(-200),
      calls: current.calls.map(one => (one.id === id ? done : one)),
    }
  })

  const where = call.target === '' ? '' : ` (${call.target})`
  const first = fresh[0]
  if (first !== undefined) {
    const more = fresh.length > 1 ? ` +${fresh.length - 1} more` : ''
    $.ui.toast(`🧠 Hunch invariant [${first.severity}]${where}: ${first.statement}${more}`, { timeoutMs: 10000 })
  }
  if (outcome.verdict === 'BLOCK') {
    $.ui.toast(`⛔ Hunch merge verdict BLOCK${where}: fix the cited invariant before merging`, { timeoutMs: 12000 })
  }
  if (call.name === 'escalations' && level === 'alert' && !isError) {
    $.ui.toast(`🧠 Hunch escalations: ${summary}; ask the user, silence is never approval`, { timeoutMs: 10000 })
  }
  if (call.name === 'verify' && level === 'alert') {
    $.ui.toast(`✖ Hunch verify ${summary}${where}`, { timeoutMs: 10000 })
  }
  if (isError && call.name !== 'verify') {
    $.ui.toast(`✖ Hunch ${call.name} ${isDenied ? 'denied' : 'failed'}: ${summary}`, { timeoutMs: 8000 })
  }
  if (isFinish && !hadContext && !isError) {
    $.ui.toast('⚠ Hunch task finished without a hunch_context brief', { timeoutMs: 8000 })
  }
  await refreshStatus($)
}

/** Closes a call that got no result: `interrupted` (the dispatch was abandoned) is quiet, `lost` alerts. */
async function settleHunchCall($: EngineInterface, id: string, why: 'interrupted' | 'lost') {
  const now = await $.clock.now()
  const [level, summary] = why === 'interrupted' ? (['info', 'interrupted'] as const) : (['alert', 'aborted or not recorded'] as const)
  await update($, hunch, log => ({
    ...log,
    calls: log.calls.map(one =>
      one.id === id && one.status === 'running' ? { ...one, status: 'error' as const, level, durationMs: now - one.startedAt, summary } : one,
    ),
  }))
  await refreshStatus($)
}

async function whoOf($: EngineInterface, agentId: string | undefined): Promise<string> {
  if (agentId === undefined) return 'main'
  const run = (await read($, agents)).find(agent => agent.id === agentId)

  return roleOf(run?.type ?? 'agent')
}

/** Adds or replaces one timeline line; running lines older than STALE_MS are closed as stopped. */
async function logActivity($: EngineInterface, entry: ActivityEntry) {
  await update($, activity, list =>
    [
      ...list
        .filter(one => one.id !== entry.id)
        .map(one => (one.status === 'running' && entry.at - one.at > STALE_MS ? { ...one, status: 'stopped' as const } : one)),
      entry,
    ].slice(-ACTIVITY_KEEP),
  )
}

/** Closes one line; a line already closed as stale (stopped, no duration) still takes its real outcome. */
async function endActivity($: EngineInterface, id: string, status: ActivityEntry['status']) {
  const now = await $.clock.now()
  const isOpen = (one: ActivityEntry) => one.status === 'running' || (one.status === 'stopped' && one.durationMs === undefined)
  await update($, activity, list => list.map(one => (one.id === id && isOpen(one) ? { ...one, status, durationMs: now - one.at } : one)))
}

/** Closes running lines older than STALE_MS as stopped; writes only when one changed, so idle polls draw nothing. */
async function reapActivity($: EngineInterface, now: number) {
  const isStale = (one: ActivityEntry) => one.status === 'running' && now - one.at > STALE_MS
  if (!(await read($, activity)).some(isStale)) return
  await update($, activity, list => list.map(one => (isStale(one) ? { ...one, status: 'stopped' as const } : one)))
}

/** The time a drawing measures ages against, from state alone: the poll's tick advances it while work runs. */
function drawnNow(tick: number, list: readonly ActivityEntry[], runs: readonly AgentRun[]): number {
  return Math.max(tick, ...list.map(one => one.at), ...runs.map(run => run.startedAt))
}

/** What the session is doing right now, in a few words. */
function nowText(list: readonly ActivityEntry[], running: readonly AgentRun[], now: number, isTurnOpen: boolean): string {
  const main = list.filter(one => one.who === 'main' && one.status === 'running').at(-1)
  if (main !== undefined) return `${main.text} (${seconds(now - main.at)})`
  if (running.length > 0) return `waiting on ${running.map(agent => roleOf(agent.type)).join(', ')}`

  return isTurnOpen ? 'thinking' : 'idle, waiting for you'
}

async function refreshStatus($: EngineInterface) {
  if (!(await read($, isBandHidden))) {
    $.ui.status(undefined)
    return
  }
  const g = await read($, gauge)
  const list = await read($, agents)
  const running = list.filter(agent => agent.status === 'running')
  const zone = g === null ? '?' : g.zone.toUpperCase()
  const lines = await read($, activity)
  const doing = nowText(lines, running, drawnNow(await read($, tick), lines, running), await read($, turnOpen))
  $.ui.status(clip(`${zone} ${kTokens(g?.tokens)} · now: ${doing} · ${running.length} agents`, 100))
}

function openPane($: EngineInterface) {
return $.ui.open({ id: PANE, title: 'Mission Control' })
}

/** Rereads the session registry; writes the atom only when a row changed, so idle polls draw nothing. */
async function pollSessions($: EngineInterface) {
  if (homeDir === '') await loadRelay($)
  const dir = `${homeDir}/.claude/sessions`
  const entries = await $.fs.list(dir).catch(() => [])
  const files = entries.filter(entry => entry.kind === 'file' && entry.name.endsWith('.json'))
  const raws = await Promise.all(files.map(entry => $.fs.read(`${dir}/${entry.name}`).catch(() => '')))
  const rows = sessionRows(raws.flatMap(raw => sessionRow(typeof raw === 'string' ? raw : '') ?? []))

  if (JSON.stringify(rows) !== JSON.stringify(await read($, sessions))) await update($, sessions, () => rows)

  // Ages are drawn from the clock: bump this at most every 30 s so the pane redraws without a row change.
  const checkedAt = Math.floor((await $.clock.now()) / 30_000) * 30_000
  if (checkedAt !== (await read($, sessionsCheckedAt))) await update($, sessionsCheckedAt, () => checkedAt)
}

/** Asks the handoff bridge in the session's own VS Code window to bring its tab forward. */
async function focusSession($: EngineInterface, row: SessionRow) {
  $.ui.toast(`Switching to ${row.name}…`, { timeoutMs: 3000 })
  let message: string
  try {
    const helper = `${homeDir}/.claude/bin/session-focus.py`
    const { exitCode, stdout, stderr } = await $.process.run(['python', helper, '--session', row.sessionId], {
      timeoutMs: 20_000,
    })
    const last = (text: string) => text.trim().split(/\r?\n/).at(-1) ?? ''
    const line = last(stdout) || last(stderr) || `session-focus exited ${exitCode}`
    message = exitCode === 0 ? `🗂 ${line}` : `✖ ${line}`
  } catch (error) {
    message = `✖ session-focus could not run: ${error instanceof Error ? error.message : String(error)}`
  }
  $.ui.toast(message, { timeoutMs: 8000 })
}

async function openSessions($: EngineInterface) {
  await pollSessions($)

  return $.ui.open({ id: SESSIONS_PANE, title: 'Sessions' })
}

async function setBandHidden($: EngineInterface, isHidden: boolean) {
  await update($, isBandHidden, () => isHidden)
  await refreshStatus($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await loadRelay($)
    await $.command.register({
      name: 'orch',
      description: 'Mission Control: open the orchestrator pane (`/orch band` toggles the band)',
    })
    await $.command.register({
      name: 'sessions',
      description: 'Mission Control: list open Claude Code sessions and switch VS Code to one',
    })

    const usage = await $.session.usage()
    await measure($, usage.context.tokens, usage.context.window, usage.context.percent, usage.cost?.usd)
    await pollJev($)
    await pollSessions($)

    $.clock.every(POLL_MS, () => {
      void (async () => {
        await pollJev($)
        await pollSessions($)
        const list = await read($, agents)
        await reapActivity($, await $.clock.now())
        const isBusy = (await read($, activity)).some(one => one.status === 'running')
        if (isBusy || list.some(agent => agent.status === 'running')) {
          const now = await $.clock.now()
          await update($, tick, () => now)
        }
      })()
    })

    return next(e)
  })

  on('command.run', { command: 'orch' }, async ($, e) => {
    if (e.args.trim() === 'band') {
      const isHidden = !(await read($, isBandHidden))
      await setBandHidden($, isHidden)

      return { text: isHidden ? 'Mission Control band hidden (summary moved to the status line).' : 'Mission Control band shown.' }
    }
    await openPane($)

    return { text: 'Mission Control opened.' }
  })

  on('command.run', { command: 'sessions' }, async ($) => {
    await openSessions($)
    const count = (await read($, sessions)).length

    return { text: `Sessions opened: ${count} registered.` }
  })

  on('prompt.submit', async ($, e, next) => {
    try {
      const at = await $.clock.now()
      const detail = e.text.length > 70 ? capDetail(e.text) : undefined
      await update($, turnOpen, () => true)
      await logActivity($, { id: `prompt-${at}`, at, who: 'you', text: `asked: "${clip(e.text, 70)}"`, status: 'note', detail })
    } catch {
      // the timeline is a view; a prompt never waits on it
    }

    return next(e)
  }).catch(($, e, next) => next(e))

  // Every tool call, from the main loop or a subagent, becomes one plain timeline line.
  on('tool.call', async ($, e, next) => {
    const id = e.tool_use_id
    try {
      const at = await $.clock.now()
      const args = e as unknown as Record<string, unknown>
      const text = describeTool(e.tool, args)
      const detail = toolDetail(e.tool, args) || undefined
      await logActivity($, { id, at, who: await whoOf($, e.agentId), text, status: 'running', detail })
      await refreshStatus($)
    } catch {
      // never block a tool on the timeline
    }

    let status: ActivityEntry['status'] = 'stopped'
    try {
      const result = await next(e)
      if (!next.signal.aborted) {
        const isStopped = result.deny === undefined && wasInterrupted(result.result, resultText(result))
        status = isStopped ? 'stopped' : result.deny !== undefined || result.isError === true ? 'error' : 'ok'
      }

      return result
    } finally {
      await endActivity($, id, status).catch(() => undefined)
      await refreshStatus($).catch(() => undefined)
    }
    // An observer: whatever went wrong here, the call itself goes on (or replays what it settled to).
  }).catch(($, e, next) => next(e))

  on('session.measure', async ($, e, next) => {
    await measure($, e.context.tokens, e.context.window, e.context.percent, e.cost?.usd)

    return next(e)
  })

  on('agent.spawn', async ($, e, next) => {
    const spawned = await next(e)
    const agentId = 'agentId' in spawned ? spawned.agentId : undefined
    const model = 'model' in spawned ? spawned.model : undefined

    if (agentId !== undefined && model !== undefined) {
      const run: AgentRun = {
        id: agentId,
        type: e.subagentType,
        model: shortModel(model),
        description: e.description,
        isBackground: e.background,
        startedAt: await $.clock.now(),
        status: 'running',
      }
      await update($, agents, list => [...list.filter(one => one.id !== agentId), run].slice(-30))
      await refreshStatus($)
    }

    return spawned
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    const agentId = e.agentId
    if (agentId === undefined) {
      const at = await $.clock.now()
      const how = e.reason === 'answer' ? 'answered' : e.reason === 'aborted' ? 'stopped' : 'ended with an error'
      await logActivity($, {
        id: `turn-${at}`,
        at,
        who: 'main',
        text: `${how} (turn took ${seconds(e.durationMs)})`,
        status: e.reason === 'answer' ? 'note' : e.reason === 'aborted' ? 'stopped' : 'error',
      }).catch(() => undefined)
      await update($, turnOpen, () => false).catch(() => undefined)
      await refreshStatus($).catch(() => undefined)

      return next(e)
    }

    const now = await $.clock.now()
    const status: AgentRun['status'] = e.reason === 'answer' ? 'done' : e.reason === 'aborted' ? 'aborted' : 'error'
    const run = (await read($, agents)).find(one => one.id === agentId)
    await update($, agents, list => list.map(one => (one.id === agentId ? { ...one, status, endedAt: now } : one)))

    const type = run?.type ?? 'subagent'
    const card = parseCard(e.answer)

    // Only role agents owe a card; an Explore or general-purpose answer is not graded.
    if (status === 'done' && (isRole(type) || card.hasAnyField)) {
      const report: ReportCard = {
        agentId,
        type,
        model: run?.model ?? shortModel(e.usage?.model ?? ''),
        at: now,
        durationMs: e.durationMs,
        confidence: card.confidence,
        missing: card.missing,
        unverified: card.unverified,
        exitCodes: card.exitCodes,
        isWeak: card.isWeak,
        result: card.result,
      }
      await update($, reports, list => [...list, report].slice(-20))

      if (card.isWeak) {
        const why = [
          card.missing.length > 0 ? `missing ${card.missing.join('/')}` : '',
          card.confidence !== 'high' && card.confidence !== 'missing' ? `confidence ${card.confidence}` : '',
          card.unverified > 0 ? `${card.unverified} unverified` : '',
        ]
          .filter(Boolean)
          .join(', ')
        $.ui.toast(`⚠ ${roleOf(type)} report weak: ${why}; verify before relying on it`, { timeoutMs: 8000 })
      }
    }
    const verdict = card.result === '' ? '' : `: ${card.result}`
    const graded = card.hasAnyField ? ` (${card.confidence} confidence)` : ''
    await logActivity($, {
      id: `agent-end-${agentId}`,
      at: now,
      who: roleOf(type),
      text: clip(status === 'done' ? `finished${graded}${verdict}` : `${status}`, 90),
      status: status === 'done' ? 'ok' : status === 'aborted' ? 'stopped' : 'error',
      durationMs: run === undefined ? undefined : now - run.startedAt,
    }).catch(() => undefined)
    await refreshStatus($)

    return next(e)
  })

  on('tool.call', { tool: /^mcp__hunch__/ }, async ($, e, next) => {
    const name = hunchName(e.tool)
    if (name === undefined) return next(e)

    await startHunchCall($, e.tool_use_id, e.agentId, hunchInvocation(name, e as unknown as Record<string, unknown>))
    const onAbort = () => void settleHunchCall($, e.tool_use_id, 'interrupted').catch(() => undefined)
    next.signal.addEventListener('abort', onAbort, { once: true })
    if (next.signal.aborted) onAbort()
    const result = await next(e)
    next.signal.removeEventListener('abort', onAbort)
    // Abandoned, then resolved late (e.g. backgrounded by a turn abort): keep it interrupted.
    if (next.signal.aborted) return result
    await finishHunchCall($, e.tool_use_id, result)

    return result
  }).catch(async ($, e, next) => {
    try {
      return await next(e)
    } finally {
      await settleHunchCall($, e.tool_use_id, next.signal.aborted ? 'interrupted' : 'lost').catch(() => undefined)
    }
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const invocation = e.tool === 'Bash' ? hunchCli(e.command) : undefined
    if (invocation === undefined) return next(e)

    await startHunchCall($, e.tool_use_id, e.agentId, invocation)
    const onAbort = () => void settleHunchCall($, e.tool_use_id, 'interrupted').catch(() => undefined)
    next.signal.addEventListener('abort', onAbort, { once: true })
    if (next.signal.aborted) onAbort()
    const result = await next(e)
    next.signal.removeEventListener('abort', onAbort)
    // Abandoned, then resolved late (e.g. backgrounded by a turn abort): keep it interrupted.
    if (next.signal.aborted) return result
    await finishHunchCall($, e.tool_use_id, result)

    return result
  }).catch(async ($, e, next) => {
    try {
      return await next(e)
    } finally {
      await settleHunchCall($, e.tool_use_id, next.signal.aborted ? 'interrupted' : 'lost').catch(() => undefined)
    }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isBandHidden))) return next(e)

    const { Box, Text, Button } = $.ui.resolve(e)
    const g = await read($, gauge)
    const j = await read($, jev)
    const running = (await read($, agents)).filter(agent => agent.status === 'running')
    const hunchNow = taskCalls(await read($, hunch))
    const hunchLevel = loudest(hunchNow)
    const sessionCount = (await read($, sessions)).length
    const isWide = e.props.bodyColumns >= 100
    const list = await read($, activity)
    const now = drawnNow(await read($, tick), list, running)

    const zone: Zone = g?.zone ?? 'unknown'
    const fill = g?.tokens === undefined ? 0 : g.tokens / g.hard
    const jevColor = j?.isOnline === true ? 'green' : j?.isOnline === false ? 'red' : 'gray'
    const jevText = j?.isOnline === true ? 'Jev ✔' : j?.isOnline === false ? `Jev ✖ ${j.lastError ?? ''}`.trim() : 'Jev ?'
    const doing = nowText(list, running, now, await read($, turnOpen))
    const crew = running.map(agent => `${roleOf(agent.type)} ${seconds(now - agent.startedAt)}`).join(', ')

    return (
      <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
        <Text color={ZONE_COLOR[zone]} bold>
          {'●'} {zone.toUpperCase()} {kTokens(g?.tokens)}/{kTokens(g?.hard)}
        </Text>
        {isWide && <Text color={ZONE_COLOR[zone]}>{bar(fill, 10)}</Text>}
        <Text color={jevColor}>{jevText}</Text>
        <Text color={HUNCH_COLOR[hunchLevel] ?? (hunchNow.length === 0 ? 'gray' : 'cyan')}>
          {'🧠'} hunch {hunchNow.length}
          {hunchNow.some(call => call.status === 'running') ? '…' : ''}
          {hunchLevel !== 'info' ? ` ⚠${hunchNow.filter(call => call.level !== 'info').length}` : ''}
        </Text>
        <Text wrap="truncate" color={doing.startsWith('idle') ? 'gray' : 'yellow'}>
          now: {doing}
        </Text>
        {crew !== '' && <Text wrap="truncate">agents: {crew}</Text>}
        <Button key="open" label="orch" onPress={() => openPane($)} />
        <Button key="sessions" label={`🗂 ${sessionCount}`} onPress={() => openSessions($)} />
        <Button key="hide" label="hide" onPress={() => setBandHidden($, true)} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const g = await read($, gauge)
    const j = await read($, jev)
    const list = await read($, agents)
    const cards = await read($, reports)
    const hunchLog = await read($, hunch)
    const hunchNow = taskCalls(hunchLog)
    const counts = new Map<string, number>()
    for (const call of hunchNow) counts.set(call.name, (counts.get(call.name) ?? 0) + 1)
    const now = drawnNow(await read($, tick), await read($, activity), list)
    const width = Math.max(20, e.props.bodyColumns)
    const zone: Zone = g?.zone ?? 'unknown'
    const running = list.filter(agent => agent.status === 'running')
    const lines = await read($, activity)
    const isDetailed = await read($, showDetails)
    const opened = await read($, openEntries)
    const mainNow = lines.filter(one => one.who === 'main' && one.status === 'running').at(-1)
    const isThinking = await read($, turnOpen)
    const lastCard = cards.at(-1)
    const hunchWarnings = hunchNow.filter(call => call.level !== 'info').length

    return (
      <Box flexDirection="column" width={width}>
        <Text bold>Now</Text>
        <Text wrap="truncate" color={mainNow === undefined && running.length === 0 ? 'gray' : 'yellow'}>
          {'  '}main {mainNow === undefined ? (isThinking ? 'thinking' : 'idle, waiting for you') : `${mainNow.text} · ${seconds(now - mainNow.at)}`}
        </Text>
        {running.map(agent => (
          <Text wrap="truncate" color="yellow">
            {'  '}
            {roleOf(agent.type)} ({agent.model}) {agent.description} · {seconds(now - agent.startedAt)}
            {agent.isBackground ? ' · background' : ''}
          </Text>
        ))}

        <Text bold>Timeline</Text>
        {lines.length === 0 && <Text dimColor>{'  '}Nothing yet.</Text>}
        {lines
          .slice(-TIMELINE_ROWS)
          .reverse()
          .map(one => {
            const isOpen = one.detail !== undefined && opened.includes(one.id)
            const drawer = isOpen ? (one.detail ?? '').split(/\r?\n/) : []

            return (
              <Box key={`line-${one.id}`} flexDirection="column">
                <Box flexDirection="row" columnGap={1}>
                  {one.detail !== undefined ? (
                    <Button
                      key={`drawer-${one.id}`}
                      label={isOpen ? '▾' : '▸'}
                      onPress={() => update($, openEntries, ids => (ids.includes(one.id) ? ids.filter(id => id !== one.id) : [...ids, one.id].slice(-20)))}
                    />
                  ) : (
                    <Text> </Text>
                  )}
                  <Text wrap="truncate" color={one.status === 'note' ? undefined : ACTIVITY_COLOR[one.status]} dimColor={one.status === 'note'}>
                    {clockSeconds(one.at)} {clip(one.who, 8).padEnd(8)} {ACTIVITY_MARK[one.status]} {one.text}
                    {one.status === 'running'
                      ? ` · ${seconds(now - one.at)}`
                      : one.durationMs !== undefined && one.durationMs >= 1000
                        ? ` · ${seconds(one.durationMs)}`
                        : ''}
                  </Text>
                </Box>
                {drawer.slice(0, DRAWER_LINES).map(line => (
                  <Text dimColor wrap="truncate">
                    {'      │ '}
                    {line}
                  </Text>
                ))}
                {drawer.length > DRAWER_LINES && <Text dimColor>{`      │ … ${drawer.length - DRAWER_LINES} more lines`}</Text>}
              </Box>
            )
          })}

        <Text bold>Summary</Text>
        <Text wrap="truncate" color={ZONE_COLOR[zone]}>
          {'  '}context {kTokens(g?.tokens)} of {kTokens(g?.hard)} · {zone.toUpperCase()} · {ZONE_PLAIN[zone]}
          {g?.usd !== undefined ? ` · $${g.usd.toFixed(2)}` : ''}
        </Text>
        <Text wrap="truncate">
          {'  '}agents {running.length} running
          {lastCard !== undefined
            ? ` · last report: ${roleOf(lastCard.type)} ${lastCard.confidence}${lastCard.unverified > 0 ? `, ${lastCard.unverified} unverified` : ''}`
            : ''}
        </Text>
        <Text wrap="truncate" color={HUNCH_COLOR[loudest(hunchNow)]}>
          {'  '}hunch {hunchNow.length} call{hunchNow.length === 1 ? '' : 's'} · {hunchWarnings === 0 ? 'no warnings' : `${hunchWarnings} warning${hunchWarnings === 1 ? '' : 's'}`}
        </Text>
        <Text wrap="truncate" color={j?.isOnline === false ? 'red' : undefined}>
          {'  '}jev {j?.isOnline === true ? 'online' : j?.isOnline === false ? `offline (${j.lastError ?? 'unavailable'})` : 'unknown'}
        </Text>

        <Box flexDirection="row" columnGap={1}>
          <Button key="details" label={isDetailed ? 'hide details' : 'details'} onPress={() => update($, showDetails, shown => !shown)} />
          <Button
            key="band"
            label="toggle band"
            onPress={async () => setBandHidden($, !(await read($, isBandHidden)))}
          />
          <Button
            key="clear"
            label="clear done"
            onPress={async () => {
              await update($, agents, all => all.filter(agent => agent.status === 'running'))
              await update($, activity, all => all.filter(one => one.status === 'running'))
              const now = await $.clock.now()
              await update($, hunch, log => ({ ...log, calls: reapStale(log.calls, now).filter(call => call.status === 'running') }))
            }}
          />
        </Box>

        {isDetailed && (
        <Box flexDirection="column">
        <Text bold>Relay</Text>
        <Text color={ZONE_COLOR[zone]}>
          {zone.toUpperCase()} {bar(g?.tokens === undefined ? 0 : g.tokens / g.hard, Math.min(24, width - 22))}{' '}
          {kTokens(g?.tokens)}/{kTokens(g?.hard)}
        </Text>
        <Text dimColor wrap="truncate">
          amber {kTokens(g?.soft)} · red {kTokens(g?.hard)}
          {g?.usd !== undefined ? ` · session $${g.usd.toFixed(2)}` : ''}
          {ZONE_HINT[zone] !== '' ? ` · ${ZONE_HINT[zone]}` : ''}
        </Text>

        <Text bold>In flight ({running.length})</Text>
        {running.length === 0 && <Text dimColor>No subagents running.</Text>}
        {running.map(agent => (
          <Text wrap="truncate">
            {roleOf(agent.type).padEnd(8)} {agent.model.padEnd(7)} {seconds(now - agent.startedAt).padStart(4)}{' '}
            {agent.isBackground ? 'bg ' : ''}
            {agent.description}
          </Text>
        ))}

        <Text bold>Report cards</Text>
        {cards.length === 0 && <Text dimColor>No role reports yet.</Text>}
        {cards
          .slice(-6)
          .reverse()
          .map(card => (
            <Box flexDirection="column">
              <Text wrap="truncate">
                <Text color={CONFIDENCE_COLOR[card.confidence]}>{card.confidence.toUpperCase().padEnd(7)}</Text>{' '}
                {roleOf(card.type).padEnd(8)} {seconds(card.durationMs).padStart(4)}
                {card.exitCodes.length > 0 ? ` exit ${card.exitCodes.join(',')}` : ''}
                {card.unverified > 0 ? ` ⚠ ${card.unverified} unverified` : ''}
                {card.missing.length > 0 ? ` missing ${card.missing.join('/')}` : ''}
              </Text>
              {card.result !== '' && (
                <Text dimColor wrap="truncate">
                  {'  '}
                  {card.result}
                </Text>
              )}
            </Box>
          ))}

        <Text bold>
          Hunch{' '}
          <Text color={HUNCH_COLOR[loudest(hunchNow)]}>
            {hunchLog.taskId ?? 'no task id yet'} · {hunchNow.length} call{hunchNow.length === 1 ? '' : 's'}
          </Text>
        </Text>
        {hunchNow.length === 0 && <Text dimColor>No Hunch calls yet this task.</Text>}
        {counts.size > 0 && (
          <Text dimColor wrap="truncate">
            {[...counts].map(([name, count]) => `${name} ${count}`).join(' · ')}
          </Text>
        )}
        {hunchNow
          .slice(-8)
          .reverse()
          .map(call => (
            <Box flexDirection="column">
              <Text wrap="truncate" color={HUNCH_COLOR[call.level]}>
                {clockTime(call.startedAt)} {HUNCH_MARK[call.status]} {call.role.padEnd(7)} {call.name.padEnd(17)}{' '}
                {call.durationMs === undefined ? '' : `${seconds(call.durationMs).padStart(4)} `}
                {call.target}
              </Text>
              {call.summary !== '' && (
                <Text dimColor wrap="truncate">
                  {'      '}
                  {call.summary}
                </Text>
              )}
            </Box>
          ))}

        <Text bold>
          Jev{' '}
          <Text color={j?.isOnline === true ? 'green' : j?.isOnline === false ? 'red' : 'gray'}>
            {j?.isOnline === true ? 'online' : j?.isOnline === false ? `offline (${j.lastError ?? 'unavailable'})` : 'unknown'}
          </Text>
        </Text>
        {j !== null && (
          <Text dimColor>
            today ${j.spendToday.toFixed(4)} · {j.deniesToday} gate denies · {j.weakToday} weak reports
          </Text>
        )}
        {j?.note !== undefined && <Text color="yellow">{j.note}</Text>}
        {(j?.recent ?? []).map(entry => (
          <Text wrap="truncate" color={entry.isAlert ? 'red' : undefined}>
            {entry.ts.slice(11, 16)} {entry.feature.padEnd(13)} {entry.summary}
          </Text>
        ))}
        </Box>
        )}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: SESSIONS_PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const rows = await read($, sessions)
    await read($, sessionsCheckedAt) // read so the pane redraws as ages advance
    const self = await $.session.id()
    const now = await $.clock.now()
    const width = Math.max(20, e.props.bodyColumns)
    const nameWidth = Math.min(22, Math.max(8, ...rows.map(row => row.name.length)))

    return (
      <Box flexDirection="column" width={width}>
        <Text bold>Open sessions ({rows.length})</Text>
        {rows.length === 0 && <Text dimColor>No Claude Code sessions registered.</Text>}
        {rows.map(row => {
          const isSelf = row.sessionId === self
          const isEditor = row.entrypoint === 'claude-vscode'

          return (
            <Box key={`row-${row.sessionId}`} flexDirection="row" columnGap={1}>
              <Text wrap="truncate" color={isSelf ? 'cyan' : undefined}>
                <Text color={row.status === 'busy' ? 'yellow' : row.status === 'idle' ? 'green' : 'gray'}>
                  {row.status === 'busy' ? '◐' : '●'}
                </Text>{' '}
                {row.name.slice(0, nameWidth).padEnd(nameWidth)} {repoName(row.cwd).slice(0, 18).padEnd(18)}{' '}
                {row.status.padEnd(4)} {ago(now - row.updatedAt).padStart(4)}
              </Text>
              {isSelf ? (
                <Text dimColor>this session</Text>
              ) : isEditor ? (
                <Button key={`focus-${row.sessionId}`} label="switch" onPress={() => focusSession($, row)} />
              ) : (
                <Text dimColor>terminal</Text>
              )}
            </Box>
          )
        })}
        <Text dimColor wrap="truncate">
          switch asks the handoff bridge in the session's VS Code window to show its tab
        </Text>
        <Box flexDirection="row" columnGap={1}>
          <Button key="refresh" label="refresh" onPress={() => pollSessions($)} />
        </Box>
      </Box>
    )
  })
}

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, ToolCallResult } from 'claude-code'

import type { AgentRun, Gauge, HunchCall, HunchConstraint, HunchLevel, HunchLog, JevStatus, ReportCard, Zone } from '../types'
import {
  bar,
  clockTime,
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
  roleOf,
  seconds,
  shortModel,
  wasInterrupted,
  zoneOf,
} from './parse'

const PANE = 'mission-control'
const POLL_MS = 5000

const agents = atom({ plugin: 'mission-control', key: 'agents' } as const, [])
const reports = atom({ plugin: 'mission-control', key: 'reports' } as const, [])
const gauge = atom({ plugin: 'mission-control', key: 'gauge' } as const, null)
const jev = atom({ plugin: 'mission-control', key: 'jev' } as const, null)
const hunch = atom({ plugin: 'mission-control', key: 'hunch' } as const, { calls: [], total: 0, seenConstraints: [] })
const isBandHidden = atom({ plugin: 'mission-control', key: 'isBandHidden' } as const, false)
const tick = atom({ plugin: 'mission-control', key: 'tick' } as const, 0)

const HUNCH_KEEP = 40

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
let relayDir = ''
let soft = 150_000
let hard = 250_000
let isJevEnabled = true
let logSize = -1
let alertCursor = -1

async function loadRelay($: EngineInterface) {
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
  relayDir = `${home.replace(/\\/g, '/')}/.claude/relay`

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

async function refreshStatus($: EngineInterface) {
  if (!(await read($, isBandHidden))) {
    $.ui.status(undefined)
    return
  }
  const g = await read($, gauge)
  const j = await read($, jev)
  const running = (await read($, agents)).filter(agent => agent.status === 'running').length
  const zone = g === null ? '?' : g.zone.toUpperCase()
  const jevMark = j?.isOnline === false ? 'Jev off' : j?.isOnline === true ? 'Jev on' : 'Jev ?'
  const hunchCount = taskCalls(await read($, hunch)).length
  $.ui.status(`${zone} ${kTokens(g?.tokens)} · ${running} agents · ${jevMark} · hunch ${hunchCount}`)
}

function openPane($: EngineInterface) {
return $.ui.open({ id: PANE, title: 'Mission Control' })
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

    const usage = await $.session.usage()
    await measure($, usage.context.tokens, usage.context.window, usage.context.percent, usage.cost?.usd)
    await pollJev($)

    $.clock.every(POLL_MS, () => {
      void (async () => {
        await pollJev($)
        const list = await read($, agents)
        if (list.some(agent => agent.status === 'running')) {
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
    if (agentId === undefined) return next(e)

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
    const last = (await read($, reports)).at(-1)
    const hunchNow = taskCalls(await read($, hunch))
    const hunchLevel = loudest(hunchNow)
    const isWide = e.props.bodyColumns >= 100

    const zone: Zone = g?.zone ?? 'unknown'
    const fill = g?.tokens === undefined ? 0 : g.tokens / g.hard
    const jevColor = j?.isOnline === true ? 'green' : j?.isOnline === false ? 'red' : 'gray'
    const jevText = j?.isOnline === true ? 'Jev ✔' : j?.isOnline === false ? `Jev ✖ ${j.lastError ?? ''}`.trim() : 'Jev ?'
    const crew =
      running.length === 0
        ? 'idle'
        : running.map(agent => `${roleOf(agent.type)}·${agent.model}`).join(', ')

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
        <Text wrap="truncate">
          {'⚙'} {crew}
        </Text>
        {last !== undefined && (
          <Text color={CONFIDENCE_COLOR[last.confidence]}>
            last {roleOf(last.type)} {last.confidence.toUpperCase()}
            {last.unverified > 0 ? ` ⚠${last.unverified}` : ''}
          </Text>
        )}
        <Button key="open" label="orch" onPress={() => openPane($)} />
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
    const now = Math.max(await read($, tick), ...list.map(agent => agent.startedAt))
    const width = Math.max(20, e.props.bodyColumns)
    const zone: Zone = g?.zone ?? 'unknown'
    const running = list.filter(agent => agent.status === 'running')

    return (
      <Box flexDirection="column" width={width}>
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

        <Box flexDirection="row" columnGap={1}>
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
              const now = await $.clock.now()
              await update($, hunch, log => ({ ...log, calls: reapStale(log.calls, now).filter(call => call.status === 'running') }))
            }}
          />
        </Box>
      </Box>
    )
  })
}

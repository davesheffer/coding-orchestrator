import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentRun, Gauge, JevStatus, ReportCard, Zone } from '../types'
import {
  bar,
  isRole,
  jevStatus,
  kTokens,
  localDay,
  parseCard,
  parseLines,
  roleOf,
  seconds,
  shortModel,
  zoneOf,
} from './parse'

const PANE = 'mission-control'
const POLL_MS = 5000

const agents = atom({ plugin: 'mission-control', key: 'agents' } as const, [])
const reports = atom({ plugin: 'mission-control', key: 'reports' } as const, [])
const gauge = atom({ plugin: 'mission-control', key: 'gauge' } as const, null)
const jev = atom({ plugin: 'mission-control', key: 'jev' } as const, null)
const isBandHidden = atom({ plugin: 'mission-control', key: 'isBandHidden' } as const, false)
const tick = atom({ plugin: 'mission-control', key: 'tick' } as const, 0)

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
  $.ui.status(`${zone} ${kTokens(g?.tokens)} · ${running} agents · ${jevMark}`)
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
  })

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

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isBandHidden))) return next(e)

    const { Box, Text, Button } = $.ui.resolve(e)
    const g = await read($, gauge)
    const j = await read($, jev)
    const running = (await read($, agents)).filter(agent => agent.status === 'running')
    const last = (await read($, reports)).at(-1)
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
            onPress={() => update($, agents, all => all.filter(agent => agent.status === 'running'))}
          />
        </Box>
      </Box>
    )
  })
}

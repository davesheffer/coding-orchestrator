import type { Confidence, JevEntry, JevStatus, Zone } from '../types'

// Pure helpers: no `$`, so the tests exercise them directly.

export const ROLES = ['scout', 'runner', 'builder', 'critic'] as const

const FIELDS = ['RESULT', 'EVIDENCE', 'CONFIDENCE', 'UNVERIFIED'] as const

const HEADER = /^[ \t>#*_\-]*(RESULT|EVIDENCE|CONFIDENCE|UNVERIFIED)\b[ \t*_]*[:\-–—]?[ \t]*(.*)$/i

const NOTHING = /^(none|n\/?a|nothing|-+|—|\(none\)|no(ne)? unverified.*)\.?$/i

const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/

export type ParsedCard = {
  hasAnyField: boolean
  confidence: Confidence
  missing: string[]
  unverified: number
  exitCodes: number[]
  isWeak: boolean
  result: string
}

/** The role an agent type names: `scout` for `scout` or `some-plugin:scout`. */
export function roleOf(type: string): string {
  return type.slice(type.lastIndexOf(':') + 1)
}

export function isRole(type: string): boolean {
  return (ROLES as readonly string[]).includes(roleOf(type).toLowerCase())
}

/** `claude-sonnet-5-5` -> `sonnet`; anything else as given. */
export function shortModel(model: string): string {
  const family = /(opus|sonnet|haiku|fable)/i.exec(model)
  return (family?.[1] ?? model).toLowerCase()
}

/** Reads the orchestrator's report card out of a subagent's final answer. */
export function parseCard(answer: string): ParsedCard {
  const sections = new Map<string, string[]>()
  let current: string | undefined

  for (const raw of answer.split(/\r?\n/)) {
    const line = raw.replace(/\*\*/g, '')
    const header = HEADER.exec(line)

    if (header !== null) {
      const [, name = '', rest = ''] = header
      current = name.toUpperCase()
      sections.set(current, rest.trim() === '' ? [] : [rest.trim()])
    } else if (current !== undefined) {
      sections.get(current)?.push(line)
    }
  }

  const missing = FIELDS.filter(field => !sections.has(field))
  const confidenceText = (sections.get('CONFIDENCE') ?? []).join(' ').toLowerCase()
  const level = /\b(high|medium|med|low)\b/.exec(confidenceText)?.[1]
  const confidence: Confidence =
    level === undefined ? 'missing' : level === 'med' ? 'medium' : (level as Confidence)

  const unverifiedLines = (sections.get('UNVERIFIED') ?? [])
    .map(line => line.trim())
    .filter(line => line !== '')
  const realItems = unverifiedLines.filter(line => !NOTHING.test(line.replace(BULLET, '').trim()))
  const bullets = realItems.filter(line => BULLET.test(line)).length
  const unverified = realItems.length === 0 ? 0 : Math.max(1, bullets)

  const exitCodes = [...answer.matchAll(/\bexit(?:\s+code)?\s*[:=]?\s*(-?\d+)\b/gi)].map(match =>
    Number(match[1]),
  )

  const result = (sections.get('RESULT') ?? [])
    .map(line => line.replace(BULLET, '').trim())
    .find(line => line !== '') ?? ''

  return {
    hasAnyField: missing.length < FIELDS.length,
    confidence,
    missing,
    unverified,
    exitCodes,
    isWeak: missing.length > 0 || confidence !== 'high' || unverified > 0,
    result: result.slice(0, 120),
  }
}

export function zoneOf(tokens: number | undefined, soft: number, hard: number): Zone {
  if (tokens === undefined) return 'unknown'
  if (tokens >= hard) return 'red'
  if (tokens >= soft) return 'amber'

  return 'green'
}

type JevLine = Record<string, unknown>

const text = (value: unknown): string => (typeof value === 'string' ? value : '')

/** One display line for a Jev decision, or undefined for noise (usage rows, routine shifts). */
export function summarize(line: JevLine): JevEntry | undefined {
  if (line.kind === 'usage') return undefined

  const ts = text(line.ts)
  const feature = text(line.feature)
  const decision = text(line.decision)
  const offline = text(line.error) !== '' ? ' (offline)' : ''

  switch (feature) {
    case 'risk_gate': {
      const fail = line.fail_closed === true ? ' fail-closed' : ''
      const risk = typeof line.risk === 'number' || typeof line.risk === 'string' ? ` risk ${line.risk}` : ''

      return {
        ts,
        feature,
        summary: `${text(line.op)} ${decision.toUpperCase()}${fail}${risk}`,
        isAlert: decision === 'deny',
      }
    }
    case 'report_check': {
      if (decision === '' || decision.startsWith('skipped')) return undefined
      const reasons = Array.isArray(line.reasons) ? line.reasons.map(text).filter(Boolean) : []

      return {
        ts,
        feature,
        summary: `${text(line.subagent_type)} ${decision}${reasons.length > 0 ? `: ${reasons[0]}` : ''}${offline}`,
        isAlert: decision === 'deny' || decision === 'weak',
      }
    }
    case 'route': {
      const choice = text(line.choice) || text(line.current)
      const applied = line.applied === true ? '' : ' (kept)'

      return { ts, feature, summary: `${text(line.subagent_type)} -> ${choice}${applied}${offline}`, isAlert: false }
    }
    case 'shift':
      if (decision === '' || decision === 'continue' || decision === 'unsure') return undefined

      return { ts, feature, summary: `${text(line.zone)} ${decision}`, isAlert: false }
    case 'handoff_grade': {
      const score = typeof line.score === 'number' ? line.score.toFixed(2) : '?'

      return { ts, feature, summary: `score ${score}${line.weak === true ? ' WEAK' : ''}`, isAlert: line.weak === true }
    }
    default:
      return feature === '' ? undefined : { ts, feature, summary: decision || '-', isAlert: false }
  }
}

export function parseLines(raw: string): JevLine[] {
  const lines: JevLine[] = []

  for (const one of raw.split(/\r?\n/)) {
    if (one.trim() === '') continue
    try {
      const value: unknown = JSON.parse(one)
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) lines.push(value as JevLine)
    } catch {
      // a torn last line while Jev writes: skipped, read again next poll
    }
  }

  return lines
}

/** Jev's health and today's figures from its log, newest line last. */
export function jevStatus(lines: JevLine[], today: string, isEnabled: boolean): JevStatus {
  let isOnline: boolean | null = null
  let lastError: string | undefined

  const newestFirst = [...lines].reverse()

  for (const line of newestFirst.slice(0, 400)) {
    if (isOnline !== null) break
    if (line.kind === 'usage') {
      isOnline = true
    } else if (text(line.error) !== '') {
      isOnline = false
      lastError = text(line.error)
    }
  }

  let spendToday = 0
  let deniesToday = 0
  let weakToday = 0

  for (const line of newestFirst) {
    if (!text(line.ts).startsWith(today)) break
    if (line.kind === 'usage' && typeof line.usd === 'number') spendToday += line.usd
    if (line.feature === 'risk_gate' && line.decision === 'deny') deniesToday += 1
    if (line.feature === 'report_check' && (line.decision === 'deny' || line.decision === 'weak')) weakToday += 1
  }

  const recent: JevEntry[] = []
  for (const line of newestFirst) {
    if (recent.length >= 8) break
    const entry = summarize(line)
    if (entry !== undefined) recent.push(entry)
  }

  return {
    isOnline: isEnabled ? isOnline : false,
    lastError: isEnabled ? lastError : 'disabled',
    spendToday,
    deniesToday,
    weakToday,
    recent,
  }
}

/** `2026-10-06` for a clock reading, in local time as Jev stamps it. */
export function localDay(ms: number): string {
  const date = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')

  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function kTokens(n: number | undefined): string {
  if (n === undefined) return '?'

  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n)
}

export function bar(fraction: number, cells: number): string {
  const filled = Math.max(0, Math.min(cells, Math.round(fraction * cells)))

  return '█'.repeat(filled) + '░'.repeat(cells - filled)
}

export function seconds(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))

  return s < 90 ? `${s}s` : `${Math.round(s / 60)}m`
}

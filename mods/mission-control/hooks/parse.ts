import type { Confidence, HunchConstraint, HunchLevel, JevEntry, JevStatus, Zone } from '../types'

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

/** `14:05` for a clock reading, local time. */
export function clockTime(ms: number): string {
  const date = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')

  return `${pad(date.getHours())}:${pad(date.getMinutes())}`
}

// Hunch: calls through its MCP server (`mcp__hunch__hunch_context`) or its CLI through Bash.

const HUNCH_MCP = /^mcp__hunch__(?:hunch_)?(.+)$/

const TASK_ID = /\bhtask_[a-f0-9]{24}\b/

const TARGET_KEYS = ['target', 'scope', 'topic', 'symbol', 'symptom_or_symbol', 'query', 'id', 'title'] as const

// The CLI as the prompt hook prints it (`node .../@davesheffer/hunch/dist/cli/index.js task verify ...`),
// or `hunch <sub>` / `npx @davesheffer/hunch <sub>` starting a command (matched with quoted text blanked,
// so `git commit -m "hunch update"` is not a call).
const HUNCH_LAUNCHER = /@davesheffer[\\/]+hunch[\\/]+dist[\\/]+cli[\\/]+index\.js['"]?\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?/i

const HUNCH_BARE = /(?:^|[;&|(]\s*)(?:npx\s+(?:-y\s+)?)?(?:@davesheffer\/)?hunch\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?/i

const LEVEL_RANK: Record<HunchLevel, number> = { info: 0, warn: 1, alert: 2 }

const BLOCKING = /^(blocking|block|error|critical|must)$/i

export type HunchInvocation = {
  name: string
  target: string
  taskId?: string
}

export type HunchOutcome = {
  summary: string
  level: HunchLevel
  constraints: HunchConstraint[]
  verdict?: 'BLOCK' | 'WARN' | 'PASS'
  exitCode?: number
  taskId?: string
}

export function maxLevel(a: HunchLevel, b: HunchLevel): HunchLevel {
  return LEVEL_RANK[a] >= LEVEL_RANK[b] ? a : b
}

/** `context` for `mcp__hunch__hunch_context`; undefined for any other tool. */
export function hunchName(tool: string): string | undefined {
  return HUNCH_MCP.exec(tool)?.[1]
}

/** What an MCP Hunch call is about, read from its arguments. */
export function hunchInvocation(name: string, args: Record<string, unknown>): HunchInvocation {
  const taskId = text(args.task_id) || undefined
  let target: string

  if (name === 'task') {
    target = [text(args.action), text(args.title)].filter(Boolean).join(' ')
  } else if (name === 'merge_verdict' || name === 'pr_impact') {
    target = text(args.base) || text(args.commit) || (args.working === true ? 'working tree' : 'staged')
  } else if (name === 'compare' && Array.isArray(args.candidates)) {
    target = args.candidates.map(text).filter(Boolean).join(' vs ')
  } else {
    target = TARGET_KEYS.map(key => text(args[key])).find(Boolean) ?? ''
  }

  return { name, target: oneLine(target), taskId }
}

/** The hunch CLI call a Bash command makes, or undefined when it makes none. */
export function hunchCli(command: string): HunchInvocation | undefined {
  const match = HUNCH_LAUNCHER.exec(command) ?? HUNCH_BARE.exec(command.replace(/"[^"]*"|'[^']*'/g, '""'))
  if (match === null) return undefined

  const [, sub = '', action = ''] = match
  const taskId = TASK_ID.exec(command)?.[0]

  if (sub === 'task' && action === 'verify') {
    const separator = command.indexOf(' -- ')
    const checked = separator >= 0 ? command.slice(separator + 4) : ''

    return { name: 'verify', target: oneLine(checked.replace(/\s*2>&1.*$/, '')), taskId }
  }

  return { name: `cli ${sub}`, target: action, taskId }
}

/** Grades what a Hunch call returned: a summary line, how loud, and what to toast about. */
export function hunchOutcome(name: string, output: string, isError: boolean): HunchOutcome {
  const taskId = TASK_ID.exec(output)?.[0]

  if (name === 'verify') return verifyOutcome(output, isError)
  if (isError) {
    return { summary: firstLine(output) || 'failed', level: 'alert', constraints: [], taskId }
  }

  switch (name) {
    case 'check_constraints': {
      const constraints = [...output.matchAll(/^\s*[•*-]\s*(con_\w+)\s*\[([\w-]+)[^\]]*\]\s*(.*)$/gm)].map(match => ({
        id: match[1] ?? '',
        severity: (match[2] ?? '').toLowerCase(),
        statement: oneLine(match[3] ?? ''),
      }))
      const blocking = constraints.filter(one => BLOCKING.test(one.severity)).length
      const warnings = constraints.filter(one => one.severity === 'warning').length
      const parts = [blocking > 0 ? `${blocking} blocking` : '', warnings > 0 ? `${warnings} warning` : ''].filter(Boolean)

      return {
        summary:
          constraints.length === 0
            ? 'no constraints in scope'
            : `${constraints.length} constraint${constraints.length === 1 ? '' : 's'}${parts.length > 0 ? ` (${parts.join(', ')})` : ''}`,
        level: blocking > 0 ? 'alert' : warnings > 0 ? 'warn' : 'info',
        constraints,
        taskId,
      }
    }
    case 'merge_verdict': {
      const verdict = /VERDICT:\W*(BLOCK|WARN|PASS)/i.exec(output)?.[1]?.toUpperCase() as HunchOutcome['verdict']
      const scope = /\(scope:\s*(.+?)\)\s*$/m.exec(output)?.[1]

      return {
        summary: `verdict ${verdict ?? '?'}${scope !== undefined ? ` · ${scope}` : ''}`,
        level: verdict === 'BLOCK' ? 'alert' : verdict === 'WARN' ? 'warn' : 'info',
        constraints: [],
        verdict,
        taskId,
      }
    }
    case 'escalations': {
      if (/nothing needs/i.test(output) || output.trim() === '') {
        return { summary: 'none', level: 'info', constraints: [], taskId }
      }
      const headline = /(\d+)\s+decisions?\s+needs?\b/i.exec(output)?.[1]
      const items = output.split(/\r?\n/).filter(line => /^\s*(?:[-*•⚖·]|\d+[.)])\s+/.test(line)).length
      const count = headline !== undefined ? Number(headline) : Math.max(1, items)

      return { summary: `${count} need your decision`, level: 'alert', constraints: [], taskId }
    }
    default:
      return { summary: firstLine(output), level: 'info', constraints: [], taskId }
  }
}

/**
 * `task verify` streams the checked command's output, then its result JSON last: the last
 * `"exit_code"` is the launcher's. Piped through `tail` or cut by the Bash output cap the JSON
 * can be gone; then the Bash call's own exit (`Exit code N`, isError) is the evidence.
 */
function verifyOutcome(output: string, isError: boolean): HunchOutcome {
  const taskId = TASK_ID.exec(output)?.[0]
  const codes = [...output.matchAll(/"exit_code"\s*:\s*(-?\d+)/g)]
  const jsonCode = codes.at(-1)?.[1]
  const bashCode = /^Exit code (-?\d+)/m.exec(output)?.[1]
  const code = jsonCode ?? bashCode
  const isTimedOut = /"timed_out"\s*:\s*true/.test(output)

  if (code === undefined) {
    return isError
      ? { summary: `failed: ${firstLine(output) || 'no exit code'}`, level: 'alert', constraints: [], taskId }
      : { summary: 'exit code not in output (piped or truncated?)', level: 'warn', constraints: [], taskId }
  }
  const exitCode = Number(code)

  return {
    summary: `exit ${exitCode}${isTimedOut ? ' (timed out)' : ''}`,
    level: exitCode === 0 && !isTimedOut && !isError ? 'info' : 'alert',
    constraints: [],
    exitCode,
    taskId,
  }
}

function oneLine(value: string): string {
  return value.replace(/\s+/g, ' ').trim().slice(0, 120)
}

/** The first line that says something: no rule, bare heading mark or footer. */
function firstLine(output: string): string {
  for (const raw of output.split(/\r?\n/)) {
    const line = raw
      .replace(/<[^>]+>/g, '')
      .replace(/^[\s#>*_`•-]+/, '')
      .replace(/\*\*/g, '')
      .trim()
    if (line !== '' && !/^[-=_─]{3,}$/.test(line) && line !== '{') return oneLine(line)
  }

  return ''
}

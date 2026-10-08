import type { CriticVerdict, GuardLedger, Role } from '../types'

/** CLAUDE.md's routing table: the model each named role runs on. */
export const ROLE_MODELS: Record<Role, string> = {
  scout: 'sonnet',
  runner: 'sonnet',
  builder: 'sonnet',
  critic: 'fable',
}

/** Agent types that pin their own model (or, for `fork`, cannot take one). */
const SELF_PINNED = new Set(['statusline-setup', 'claude-code-guide', 'fork'])

export const EMPTY_LEDGER: GuardLedger = {
  uncheckedEdits: [],
  riskyPending: [],
  lastCheck: null,
  critic: null,
  waiver: null,
  blocked: 0,
}

/** `scout`, `orchestrator:scout` → `scout`; anything else → undefined. */
export function roleOf(type: string | undefined): Role | undefined {
  const name = (type ?? '').split(':').pop() ?? ''

  return name in ROLE_MODELS ? (name as Role) : undefined
}

/** The role an agent plays: its named role, else critic for a built-in agent sent on fable (CLAUDE.md reserves fable for the critic). */
export function roleFor(type: string | undefined, model: string | undefined): Role | undefined {
  return roleOf(type) ?? (/fable/i.test(model ?? '') ? 'critic' : undefined)
}

export type AgentArgs = { subagent_type?: string; model?: string }

/** Why an Agent call breaks the routing rules, or undefined when it does not. */
export function routeVerdict(args: AgentArgs, isSubagent: boolean): string | undefined {
  if (isSubagent) {
    return 'subagents follow their role and brief without recursive delegation. Hand what is left back to the main session in your report.'
  }

  const type = args.subagent_type ?? 'general-purpose'
  const role = roleOf(type)

  if (role !== undefined) {
    const expected = ROLE_MODELS[role]

    return args.model === undefined || args.model === expected
      ? undefined
      : `the ${role} role runs on ${expected}. Drop \`model\` or set model: '${expected}'.`
  }

  if (args.model === undefined && !SELF_PINNED.has(type)) {
    return `'${type}' would inherit the main session's model. Use a named role (scout, runner, builder, critic), or pass model: 'sonnet' for bounded reading, exact checks or a specified change ('fable' only for critic-style review).`
  }

  return undefined
}

type Rule = { label: string; pattern: RegExp }

/** Actions CLAUDE.md keeps in the main session: push, publish, deploy, delete, send, networked PR polling. */
const MAIN_ONLY: Rule[] = [
  { label: 'git push', pattern: /\bgit\s+push\b/ },
  {
    label: 'a GitHub write',
    pattern: /\bgh\s+(pr\s+(create|merge|close|comment|review|edit)|release\s+create|issue\s+(create|comment|close|edit)|api\b)/,
  },
  { label: 'a publish', pattern: /\b((npm|pnpm|yarn)\s+publish|twine\s+upload|cargo\s+publish|docker\s+push|gem\s+push|vsce\s+publish)\b/ },
  {
    label: 'a deploy',
    pattern: /\b(kubectl\s+(apply|delete)|terraform\s+(apply|destroy)|helm\s+(install|upgrade|uninstall)|vercel\s+(deploy|--prod)|netlify\s+deploy|fly\s+deploy|firebase\s+deploy)\b/,
  },
  {
    label: 'a destructive delete',
    pattern: /\brm\s+-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)\b|\bgit\s+(branch\s+-D|reset\s+--hard|clean\s+-[a-z]*f|push\s+--delete)\b/,
  },
  { label: 'a send', pattern: /\b(curl|wget)\b[^|;&]*\s-X\s*(POST|PUT|PATCH|DELETE)\b|\bsendmail\b/ },
  {
    label: 'networked PR/CI polling',
    pattern: /\bpr-status\b|__PR_STATUS__|\bgh\s+(pr\s+(checks|view|status)|run\s+(watch|view|list))\b/,
  },
]

/** MCP tools that write to the outside world. */
const MCP_WRITE = /^mcp__.+__(create|merge|send|delete|push|update|publish|trash|forward|reply|share|add_.*comment|enable_pr_auto_merge|request_)/i

/** The rule a subagent's call breaks by being outside the main session, if any. */
export function mainOnlyLabel(tool: string, command: string | undefined): string | undefined {
  if (tool === 'Bash' && command !== undefined) {
    return MAIN_ONLY.find(rule => rule.pattern.test(command))?.label
  }

  return MCP_WRITE.test(tool) ? 'an outward MCP write' : undefined
}

/** Calls that ship work out of the session: the boundary where checks and critic review are due. */
const OUTWARD = /\bgit\s+push\b|\bgh\s+(pr\s+(create|merge)|release\s+create)\b|\b((npm|pnpm|yarn)\s+publish|twine\s+upload|cargo\s+publish|docker\s+push|vsce\s+publish)\b|\b(kubectl\s+apply|terraform\s+apply|helm\s+(install|upgrade)|netlify\s+deploy|fly\s+deploy|firebase\s+deploy)\b/
const MCP_OUTWARD = /^mcp__github__(create_pull_request|merge_pull_request|push_files|create_or_update_file|enable_pr_auto_merge)$/

export function isOutward(tool: string, command: string | undefined): boolean {
  return tool === 'Bash' ? OUTWARD.test(command ?? '') : MCP_OUTWARD.test(tool)
}

/** Commands that count as a check: tests, builds, linters, type-checkers, `task verify`. */
const CHECK =
  /\b(pytest|unittest|compileall|jest|vitest|mocha|tsc|mypy|pyright|ruff|eslint|flake8|clippy|gradle|mvn|playwright\s+test)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|typecheck|check)\b|\bcargo\s+(test|build|check)\b|\bgo\s+(test|build|vet)\b|\bdotnet\s+(test|build)\b|\bmake\b|\bbash\s+-n\b|\bclaude\s+plugin\s+(test|validate)\b|\btask\s+verify\b|\bnode\s+\S*test\S*\.m?js\b/

export function isCheck(command: string): boolean {
  return CHECK.test(command)
}

const DOCS = /\.(md|mdx|txt|rst|adoc)$|(^|\/)docs\//i

/** Edits that need a check after them: anything but prose. */
export function needsCheck(path: string): boolean {
  return !DOCS.test(path)
}

const RISKY =
  /(auth|security|secret|token|credential|crypt|password|permission|sandbox|migrat|schema|payment|billing|deploy|install|guard|concurren|mutex|\.github\/workflows\/|Dockerfile|\.sql$|(^|\/)hooks?\.json$|settings(\.local)?\.json$)/i

/** Edits that need a critic SHIP before they ship (CLAUDE.md: risky work needs critic review). */
export function isRisky(path: string, extra: string): boolean {
  if (RISKY.test(path)) return true
  if (extra.trim() === '') return false

  try {
    return new RegExp(extra, 'i').test(path)
  } catch {
    return false
  }
}

/** Why an outward call is premature, from the ledger; empty when it may go. */
export function outwardBlockers(ledger: GuardLedger): string[] {
  const reasons: string[] = []
  const unchecked = ledger.uncheckedEdits

  if (unchecked.length > 0) {
    const failing = ledger.lastCheck !== null && !ledger.lastCheck.isPassing ? ` (last check failed: ${clip(ledger.lastCheck.command, 60)})` : ''
    reasons.push(`${unchecked.length} edited file(s) have no passing check after the last edit${failing}: ${list(unchecked)}. Run the covering tests/build first.`)
  }
  if (ledger.riskyPending.length > 0) {
    const verdict = ledger.critic === null ? 'no critic has reviewed them' : `the last critic verdict was ${ledger.critic.verdict}`
    reasons.push(`risky files changed and ${verdict}: ${list(ledger.riskyPending)}. Send the diff to the critic role (fable) in fresh context and resolve its findings.`)
  }

  return reasons
}

export type Card = {
  hasAnyField: boolean
  missing: string[]
  confidence: 'high' | 'medium' | 'low' | 'missing'
  unverified: number
  verdict: CriticVerdict
  isWeak: boolean
}

const FIELDS = ['RESULT', 'EVIDENCE', 'CONFIDENCE', 'UNVERIFIED'] as const
const HEADER = /^\s*[#>*\-\s]*\b(RESULT|EVIDENCE|CONFIDENCE|UNVERIFIED)\b\s*:?(.*)$/i
const NOTHING = /^(none|n\/a|nothing|-|—|no(ne)?\.?)$/i
const BULLET = /^\s*([-*•]|\d+[.)])\s+/

/** Reads the RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED card a role hands back. */
export function parseCard(answer: string): Card {
  const sections = new Map<string, string[]>()
  let current: string | undefined

  for (const raw of answer.split(/\r?\n/)) {
    const header = HEADER.exec(raw.replace(/\*\*/g, ''))
    if (header !== null) {
      current = (header[1] ?? '').toUpperCase()
      const rest = (header[2] ?? '').trim()
      sections.set(current, rest === '' ? [] : [rest])
    } else if (current !== undefined) {
      sections.get(current)?.push(raw)
    }
  }

  const missing = FIELDS.filter(field => !sections.has(field))
  const level = /\b(high|medium|med|low)\b/i.exec((sections.get('CONFIDENCE') ?? []).join(' '))?.[1]?.toLowerCase()
  const confidence = level === undefined ? 'missing' : level === 'med' ? 'medium' : (level as Card['confidence'])

  const items = (sections.get('UNVERIFIED') ?? [])
    .map(line => line.trim())
    .filter(line => line !== '' && !NOTHING.test(line.replace(BULLET, '').trim()))
  const unverified = items.length === 0 ? 0 : Math.max(1, items.filter(line => BULLET.test(line)).length)

  const result = (sections.get('RESULT') ?? []).join(' ').toUpperCase()
  const verdict: CriticVerdict = /\bFIX FIRST\b/.test(result) ? 'FIX FIRST' : /\bRETHINK\b/.test(result) ? 'RETHINK' : /\bSHIP\b/.test(result) ? 'SHIP' : 'unknown'

  return {
    hasAnyField: missing.length < FIELDS.length,
    missing,
    confidence,
    unverified,
    verdict,
    isWeak: missing.length > 0 || confidence !== 'high' || unverified > 0,
  }
}

/** The CLAUDE.md follow-up a role's card calls for, or undefined when none does. */
export function cardReminder(role: Role, card: Card): string | undefined {
  const notes: string[] = []

  if (!card.hasAnyField) {
    notes.push(`the ${role} returned no RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED card: treat its answer as unverified`)
  } else if (card.isWeak) {
    const why = [
      card.missing.length > 0 ? `missing ${card.missing.join('/')}` : '',
      card.confidence !== 'high' ? `confidence ${card.confidence}` : '',
      card.unverified > 0 ? `${card.unverified} unverified item(s)` : '',
    ].filter(Boolean)
    notes.push(`the ${role} card is weak (${why.join(', ')})`)
  }
  if (notes.length > 0) {
    const escalate = role === 'builder' ? 'escalate to the main session' : role === 'critic' ? 'verify the open points directly' : 'escalate to builder or the main session'
    notes.push(`verify directly or ${escalate}; do not retry the same role with the same brief`)
  }
  if (role === 'builder') {
    notes.push('read the builder diff yourself and confirm checks ran after its last edit, with real exit codes')
  }
  if (role === 'critic' && (card.verdict === 'FIX FIRST' || card.verdict === 'RETHINK')) {
    notes.push(`critic verdict ${card.verdict}: resolve each finding, then report the actual verdict`)
  }

  return notes.length === 0 ? undefined : `orch-guard: ${notes.join('; ')}.`
}

export function summary(ledger: GuardLedger): string {
  const parts = [
    ledger.uncheckedEdits.length > 0 ? `${ledger.uncheckedEdits.length} unchecked` : ledger.lastCheck?.isPassing === true ? 'checked ✓' : '',
    ledger.riskyPending.length > 0 ? `critic due (${ledger.riskyPending.length} risky)` : '',
    ledger.critic !== null ? `critic ${ledger.critic.verdict}` : '',
    ledger.waiver !== null ? 'waived' : '',
  ].filter(Boolean)

  return parts.length === 0 ? '' : `orch: ${parts.join(' · ')}`
}

export function addUnique(list: readonly string[], item: string, cap = 50): string[] {
  return [...list.filter(one => one !== item), item].slice(-cap)
}

export function clip(value: string, max: number): string {
  const flat = value.replace(/\s+/g, ' ').trim()

  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

function list(paths: readonly string[]): string {
  const names = paths.slice(-5).map(path => path.split('/').pop() ?? path)

  return paths.length > 5 ? `${names.join(', ')} and ${paths.length - 5} more` : names.join(', ')
}

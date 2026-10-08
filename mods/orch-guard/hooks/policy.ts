import type { CriticVerdict, GuardLedger, Role, Touched } from '../types'

/** CLAUDE.md's routing table: the model each named role runs on. */
export const ROLE_MODELS: Record<Role, string> = {
  scout: 'sonnet',
  runner: 'sonnet',
  builder: 'sonnet',
  critic: 'fable',
}

/** Built-in agent types that inherit the main session's model when no `model` is given. */
const INHERITING = new Set(['general-purpose', 'Explore', 'Plan', 'claude'])

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

  // Other agent types (plugins', the user's) may pin a model in their own definition.
  if (args.model === undefined && INHERITING.has(type)) {
    return `'${type}' would inherit the main session's model. Use a named role (scout, runner, builder, critic), or pass model: 'sonnet' for bounded reading, exact checks or a specified change ('fable' only for critic-style review).`
  }

  return undefined
}

/**
 * The simple commands a shell line runs, each with quoted text, heredoc bodies and comments removed,
 * and leading `VAR=value`, `sudo`, `time`, `env`, `command` and `exec` stripped: so `grep "git push"`
 * or a commit message naming a command never reads as running it.
 */
export function commandsOf(line: string): string[] {
  const noHeredocs = line.replace(/<<-?\s*(['"]?)(\w+)\1[^\n]*\n[\s\S]*?\n\s*\2[ \t]*(?=\n|$)/g, ' ')
  const noQuotes = noHeredocs.replace(/'[^']*'|"(?:[^"\\]|\\.)*"/g, ' Q ')
  const noComments = noQuotes.replace(/(^|\s)#[^\n]*/g, ' ')

  return noComments
    .split(/\n|;|&&|\|\||\||&|\$\(|`|\(|\)/)
    .map(part => part.trim().replace(/^((\w+=\S*|sudo|time|env|command|exec|nohup)\s+)+/, '').trim())
    .filter(part => part !== '')
}

type Rule = { label: string; pattern: RegExp }

const GIT = String.raw`git(\s+(-[cC]\s+\S+|--\S+))*\s+`
const GH_PR = String.raw`gh\s+pr(\s+(-R|--repo)\s+\S+)*\s+`

/** Actions CLAUDE.md keeps in the main session, matched at the head of a simple command. */
const MAIN_ONLY: Rule[] = [
  { label: 'git push', pattern: new RegExp(`^${GIT}push\\b`) },
  {
    label: 'a GitHub write',
    pattern: new RegExp(`^(${GH_PR}(create|merge|close|comment|review|edit|ready)\\b|gh\\s+(release\\s+(create|delete|upload)|issue\\s+(create|comment|close|edit)|repo\\s+(create|delete|edit)|api)\\b)`),
  },
  { label: 'a publish', pattern: /^((npm|pnpm|yarn|bun)\s+publish|twine\s+upload|cargo\s+publish|docker\s+push|gem\s+push|vsce\s+publish|ovsx\s+publish)\b/ },
  {
    label: 'a deploy',
    pattern: /^(kubectl\s+(apply|delete|rollout)|terraform\s+(apply|destroy)|helm\s+(install|upgrade|uninstall)|vercel(\s+deploy)?\s+--prod|netlify\s+deploy|fly\s+deploy|firebase\s+deploy)\b/,
  },
  {
    label: 'a destructive delete',
    pattern: /^rm\s+(-\S*\s+)*(\/|~|\$HOME|\.\.|\.git)(\s|\/?$)/,
  },
  {
    label: 'a send',
    pattern: /^(curl|wget)\b.*(\s(-X|--request)\s*(POST|PUT|PATCH|DELETE)\b|\s(-d|--data\S*|-F|--form|-T|--upload-file|--post-data)\b)|^(sendmail|mail)\b/,
  },
  {
    label: 'networked PR/CI polling',
    pattern: new RegExp(`^(\\S*/)?pr-status(\\s|$)|^__PR_STATUS__|^(${GH_PR}(checks|view|status)|gh\\s+run\\s+(watch|view|list))\\b`),
  },
]

/** MCP tools that write to the outside world. */
const MCP_WRITE = /^mcp__.+__(create|merge|send|delete|push|update|publish|trash|forward|reply|share|add_.*comment|enable_pr_auto_merge|request_)/i

/** The rule a subagent's call breaks by being outside the main session, if any. */
export function mainOnlyLabel(tool: string, command: string | undefined): string | undefined {
  if (tool === 'Bash' && command !== undefined) {
    for (const simple of commandsOf(command)) {
      const rule = MAIN_ONLY.find(one => one.pattern.test(simple))
      if (rule !== undefined) return rule.label
    }

    return undefined
  }

  return MCP_WRITE.test(tool) ? 'an outward MCP write' : undefined
}

/** Calls that ship work out of the session: the boundary where checks and critic review are due. */
const OUTWARD = new RegExp(
  `^(${GIT}push\\b(?!.*\\s(--dry-run|-n)\\b)|${GH_PR}(create|merge)\\b|gh\\s+release\\s+create\\b|(npm|pnpm|yarn|bun)\\s+publish\\b|twine\\s+upload\\b|cargo\\s+publish\\b|docker\\s+push\\b|vsce\\s+publish\\b|kubectl\\s+apply\\b|terraform\\s+apply\\b|helm\\s+(install|upgrade)\\b|netlify\\s+deploy\\b|fly\\s+deploy\\b|firebase\\s+deploy\\b)`,
)
const MCP_OUTWARD = /^mcp__github__(create_pull_request|merge_pull_request|push_files|create_or_update_file|enable_pr_auto_merge)$/

export function isOutward(tool: string, command: string | undefined): boolean {
  return tool === 'Bash' ? commandsOf(command ?? '').some(simple => OUTWARD.test(simple)) : MCP_OUTWARD.test(tool)
}

/** Commands that count as a check when they head a simple command: tests, builds, linters, type-checkers, `task verify`. */
const CHECK = new RegExp(
  [
    String.raw`(python3?\s+-m\s+)?(pytest|unittest|compileall|mypy|tox|nox)\b`,
    String.raw`(npx\s+)?(jest|vitest|mocha|tsc|pyright|eslint|playwright\s+test)\b`,
    String.raw`(ruff|flake8)\b`,
    String.raw`(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|typecheck|check)\b`,
    String.raw`cargo\s+(test|build|check|clippy)\b`,
    String.raw`go\s+(test|build|vet)\b`,
    String.raw`dotnet\s+(test|build)\b`,
    String.raw`(gradle|\./gradlew|mvn)\s+\S*(test|build|check|verify)`,
    String.raw`make\s+(\S+\s+)*(test|check|lint|build|ci)\b`,
    String.raw`(ctest|rspec|phpunit)\b`,
    String.raw`(bundle\s+exec\s+)?rake\s+(test|spec)\b`,
    String.raw`(deno|swift)\s+test\b`,
    String.raw`node\s+(--test\b|\S*test\S*\.m?[jt]s\b)`,
    String.raw`bash\s+-n\b`,
    String.raw`claude\s+plugin\s+(test|validate)\b`,
    String.raw`\S*node\s+\S*hunch\S*\s+task\s+verify\b`,
    String.raw`(\S*/)?hunch\s+task\s+verify\b`,
  ]
    .map(alt => `^${alt}`)
    .join('|'),
)

/**
 * Whether a Bash line is a check whose exit code speaks for itself: a check heads one of its commands,
 * no `--help`, and no pipe or `|| ...` that could hide its exit code (unless `set -o pipefail`).
 */
export function isCheck(command: string, extra = ''): boolean {
  const simple = commandsOf(command)
  let custom: RegExp | undefined
  try {
    custom = extra.trim() === '' ? undefined : new RegExp(`^(${extra})`)
  } catch {
    custom = undefined
  }
  const hasCheck = simple.some(one => !/\s--help\b/.test(one) && (CHECK.test(one) || custom?.test(one) === true))
  const stripped = command.replace(/'[^']*'|"(?:[^"\\]|\\.)*"/g, ' ')
  const masks = /\|\|/.test(stripped) || (/(^|[^|])\|([^|]|$)/.test(stripped) && !/pipefail/.test(stripped))

  return hasCheck && !masks
}

/** Bash lines that may change tracked files (not read-only by the tool's own check). */
const WRITES =
  /^(sed\s+(-\S*\s+)*-i|perl\s+(-\S*\s+)*-i|tee\b|patch\b|mv\b|cp\b|truncate\b|git\s+(apply|am|checkout\s+(\S+\s+)*--|restore|merge|pull|cherry-pick|rebase|revert|stash\s+(pop|apply))\b)|\s>{1,2}\s*(?!&|\/dev\/null)\S/

/** Whether a Bash line may have edited files the Edit tools never saw. */
export function mayWrite(command: string): boolean {
  return commandsOf(command).some(simple => WRITES.test(simple))
}

const DOCS = /\.(md|mdx|txt|rst|adoc)$|(^|\/)docs\/|(^|\/)(LICENSE|CHANGELOG|AUTHORS|NOTICE)[^/]*$|(^|\/)\.(gitignore|gitattributes|editorconfig)$/i

/** Edits that need a check after them: anything but prose. */
export function needsCheck(path: string): boolean {
  return !DOCS.test(path)
}

const RISKY_WORDS =
  'auth|security|secrets?|credentials?|crypto|passwords?|permissions?|sandbox|migrations?|schema|payments?|billing|deploy|install|guard|concurrency|mutex|tokens?'
const RISKY = new RegExp(
  `(^|[/_.-])(${RISKY_WORDS})([/_.-]|$)|\\.github/workflows/|(^|/)Dockerfile|\\.sql$|(^|/)hooks?\\.json$|(^|/)settings(\\.local)?\\.json$`,
  'i',
)

/** Code edits that need a critic SHIP before they ship (CLAUDE.md: risky work needs critic review). Prose never does. */
export function isRisky(path: string, extra: string): boolean {
  if (DOCS.test(path)) return false
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
    reasons.push(`${unchecked.length} edit(s) have no passing check after them${failing}: ${list(unchecked)}. Run the covering tests/build in the foreground, without a pipe that hides the exit code.`)
  }
  if (ledger.riskyPending.length > 0) {
    const verdict = ledger.critic === null ? 'no critic has reviewed them' : `the last critic verdict was ${ledger.critic.verdict}`
    reasons.push(`risky files changed and ${verdict}: ${list(ledger.riskyPending)}. Send the diff to the critic role (fable) in fresh context and resolve its findings.`)
  }

  return reasons
}

/** Adds or refreshes an entry, keeping the newest `cap`. */
export function touch(entries: readonly Touched[], path: string, at: number, cap = 50): Touched[] {
  return [...entries.filter(one => one.path !== path), { path, at }].slice(-cap)
}

/** Drops the entries a check or review that started at `since` covered. */
export function settle(entries: readonly Touched[], since: number, only?: readonly string[]): Touched[] {
  return entries.filter(one => one.at > since || (only !== undefined && !only.includes(one.path)))
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
const HEADER = /^\s*[#>\-\s]*(RESULT|EVIDENCE|CONFIDENCE|UNVERIFIED)\s*:(.*)$/
const NOTHING = /^(none|n\/a|nothing|-|—|no(ne)?\.?)$/i
const BULLET = /^\s*([-*•]|\d+[.)])\s+/

/** Reads the RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED card a role hands back. */
export function parseCard(answer: string): Card {
  const sections = new Map<string, string[]>()
  let current: string | undefined

  for (const raw of answer.split(/\r?\n/)) {
    const header = HEADER.exec(raw.replace(/\*\*/g, ''))
    if (header !== null) {
      current = header[1] ?? ''
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

export function clip(value: string, max: number): string {
  const flat = value.replace(/\s+/g, ' ').trim()

  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

function list(entries: readonly Touched[]): string {
  const names = entries.slice(-5).map(one => one.path.split('/').pop() ?? one.path)

  return entries.length > 5 ? `${names.join(', ')} and ${entries.length - 5} more` : names.join(', ')
}

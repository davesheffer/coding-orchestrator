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

/** One simple command of a shell line and the operator that follows it (`''` at the end). */
export type Simple = { cmd: string; after: string }

/** A parsed line; `isTruncated` when it was too long or too nested to read in full. */
export type Parsed = { simple: Simple[]; isTruncated: boolean }

const MAX_LINE = 64_000
const MAX_SIMPLE = 400
const MAX_HEREDOCS = 40
const MAX_DEPTH = 3

/**
 * Prefixes that run the command after them: stripped until the real command heads the line. Each
 * alternative is written so a token can match one way only (no exponential backtracking).
 */
const WRAPPERS: RegExp[] = [
  /^\w+=\S*\s+/,
  /^\\(?=\w)/,
  /^(time|command|exec|nohup|builtin|then|do|else|elif|if|while|until|!|\{)\s+/,
  /^(sudo|doas)(\s+(-[ugCDhpr]\s+(?!-)\S+|-\S+))*\s+/,
  /^env(\s+(-[uSC]\s+(?!-)\S+|-\S+|\w+=\S*))*\s+/,
  /^nice(\s+-n\s+-?\d+|\s+-\S+)?\s+/,
  /^(timeout|gtimeout)(\s+(-[sk]\s+(?!-)\S+|-\S+))*\s+\d\S*\s+/,
  /^xargs(\s+(-[IdEsnLPa]\s+(?!-)\S+|-\S+))*\s+/,
  /^(uv|poetry|pipenv|pdm|hatch|rye)\s+run(\s+(--(with|python|extra|group|package|directory|project)\s+(?!-)\S+|-\S+))*\s+/,
  /^coverage\s+run(\s+-\S+)*\s+/,
  /^python[\d.]*(\s+(-[WXQ]\s+(?!-)\S+|-(?!m\s)\S+))*\s+-m\s+/,
  /^(npx|bunx|pnpx)(\s+-\S+)*\s+(--\s+)?/,
  /^(npm|pnpm|yarn)\s+(exec|dlx)(\s+-\S+)*\s+(--\s+)?/,
  /^bundle\s+exec\s+/,
  /^(\S*\/)(?=[\w.-]+(\s|$))/,
]

/** Shells: a heredoc or pipe they read is code. */
const SHELLS = /^(bash|sh|zsh|dash|ksh)(\s|$)/

/** Heads whose arguments are code: their own quoted text and remainder are read as commands too. */
const CODE_HEADS = /^((bash|sh|zsh|dash|ksh)(\s+-(?!-)\S+)*\s+-[a-zA-Z]*c|eval|ssh(\s+(-[ilpoFJ]\s+(?!-)\S+|-\S+))*\s+(?!-)\S+|su\s+(\S+\s+)*-c|script\s+(\S+\s+)*-c)(\s|$)/

function unwrap(cmd: string): string {
  let current = cmd.trim()
  for (let round = 0; round < 12; round++) {
    const rule = WRAPPERS.find(one => one.test(current))
    if (rule === undefined) break
    current = current.replace(rule, '').trim()
  }

  return current
}

/** Quoted text that is one plain word reads as that word (`"git" push`, `'/opt/node'`); anything else becomes a placeholder. */
const PLAIN = /^[\w./@:+=,%~$-]+$/
const QUOTE_REF = /__Q(\d+)__/g
const HEREDOC_REF = /__H(\d+)__/g

type State = { count: number; isTruncated: boolean }

/**
 * Reads a shell line as the simple commands it runs. Quoted text, heredoc bodies and comments are
 * data, and wrappers (`VAR=x`, `sudo`, `env`, `timeout 60`, `xargs`, `npx`, `uv run`, a path before
 * the binary) are stripped, so `grep "git push"` or a commit message never reads as a push. Code a
 * shell runs (`bash -c "…"`, `eval`, `ssh host …`, a heredoc fed to a shell, `"$(…)"`) is read in place,
 * its last command taking the operator after the shell, so `bash -c "npm test" || true` still hides
 * the exit code. Long or deeply nested lines come back `isTruncated`, never slowly.
 */
export function parseLine(line: string): Parsed {
  const heredocs = (line.match(/<</g) ?? []).length
  if (line.length > MAX_LINE || heredocs > MAX_HEREDOCS) return { simple: [], isTruncated: true }

  const state: State = { count: 0, isTruncated: false }
  const simple = parse(line, 0, state)

  return { simple, isTruncated: state.isTruncated }
}

function parse(line: string, depth: number, state: State): Simple[] {
  const joined = line.replace(/\\\r?\n/g, ' ').replace(/\$\{(\w+)\}/g, '$$$1')

  // Heredoc bodies are data unless the line feeds them to a shell; the marker stays where the body was read.
  const bodies: string[] = []
  const noHeredocs = joined.replace(
    /^([^\n]*?)<<-?\s*(['"\\]?)(\w+)\2([^\n]*)\n([\s\S]*?)\n\s*\3[ \t]*(?=\n|$)/gm,
    (_all, before: string, _q: string, _word: string, rest: string, body: string) => {
      bodies.push(body)

      return `${before} ${rest} __H${bodies.length - 1}__`
    },
  )

  const quoted: string[] = []
  const noQuotes = noHeredocs.replace(/'([^']*)'|"((?:[^"\\]|\\.)*)"/g, (_all, single: string | undefined, double: string | undefined) => {
    const text = single ?? double ?? ''
    if (text !== '' && PLAIN.test(text)) return text
    quoted.push(double !== undefined ? `\u0000${text}` : text)

    return `__Q${quoted.length - 1}__`
  })
  const noComments = noQuotes.replace(/(^|\s)#[^\n]*/g, ' ')

  const pieces = noComments.split(/(\n|;|&&|\|\||\||(?<![<>&])&(?![>&])|\$\(|`|\(|\)|\}|\bfi\b|\bdone\b)/)
  const simple: Simple[] = []

  for (let index = 0; index < pieces.length && !state.isTruncated; index += 2) {
    const after = (pieces[index + 1] ?? '').trim()
    const cmd = unwrap(pieces[index] ?? '')
    if (cmd === '') continue
    if (++state.count > MAX_SIMPLE) {
      state.isTruncated = true
      break
    }

    const isCode = CODE_HEADS.test(cmd)
    const isShell = isCode || SHELLS.test(cmd)
    const code: string[] = []
    // The remainder of `eval git push` / `ssh host git push` runs too.
    if (isCode) code.push(cmd.replace(CODE_HEADS, ' ').replace(QUOTE_REF, ' ').replace(HEREDOC_REF, ' '))
    for (const [, ref] of cmd.matchAll(QUOTE_REF)) {
      const text = quoted[Number(ref)] ?? ''
      const isDouble = text.startsWith('\u0000')
      const plain = isDouble ? text.slice(1) : text
      if (isCode) code.push(plain)
      // Command substitution runs inside double quotes whatever the command.
      else if (isDouble) for (const match of plain.matchAll(/\$\(([^)]*)\)|`([^`]*)`/g)) code.push(match[1] ?? match[2] ?? '')
    }
    // A heredoc's marker sits on the last piece of its line, so `cat <<EOF | bash` lands on bash.
    for (const [, ref] of cmd.matchAll(HEREDOC_REF)) {
      if (isShell) code.push(bodies[Number(ref)] ?? '')
    }

    const shown = cmd.replace(HEREDOC_REF, ' ').trim()
    const inner: Simple[] = []
    if (depth >= MAX_DEPTH && code.length > 0) {
      state.isTruncated = true
    } else {
      for (const text of code) inner.push(...parse(text, depth + 1, state))
    }

    // The shell's own operator applies to every command that ends one of its code chunks.
    simple.push({ cmd: shown, after }, ...inner.map(one => (isShell && one.after === '' ? { cmd: one.cmd, after } : one)))
  }

  return simple
}

export function simpleCommands(line: string): Simple[] {
  return parseLine(line).simple
}

export function commandsOf(line: string): string[] {
  return parseLine(line).simple.map(one => one.cmd)
}

/** A bounded raw-text look, for when the parser cannot be trusted (too long, or it failed). */
const RAW_SHIP = /\b(push|publish|deploy|release|merge|apply|upload)\b/
const RAW_MAIN_ONLY = /\b(push|publish|deploy|release|upload|reset|clean|rm|curl|wget|gh|kubectl|terraform|helm|pr-status|__PR_STATUS__)\b/

export function mightShip(tool: string, command: string | undefined): boolean {
  return tool === 'Bash' ? RAW_SHIP.test(command ?? '') : MCP_OUTWARD.test(tool)
}

export function mightBeMainOnly(tool: string, command: string | undefined): boolean {
  return tool === 'Bash' ? RAW_MAIN_ONLY.test(command ?? '') : MCP_WRITE.test(tool)
}

type Rule = { label: string; pattern: RegExp }

const GIT = String.raw`git(\s+(-[cC]\s*\S+|--\S+))*\s+`
const GH_OPTS = String.raw`(\s+(-R|--repo)(=|\s+)\S+)*`
const GH_PR = String.raw`gh${GH_OPTS}\s+pr${GH_OPTS}\s+`
const GH = String.raw`gh${GH_OPTS}\s+`

/** Actions CLAUDE.md keeps in the main session, matched at the head of a simple command. */
const MAIN_ONLY: Rule[] = [
  { label: 'git push', pattern: new RegExp(`^${GIT}push\\b`) },
  {
    label: 'a GitHub write',
    pattern: new RegExp(`^(${GH_PR}(create|merge|close|comment|review|edit|ready)\\b|${GH}(release\\s+(create|delete|upload)|issue\\s+(create|comment|close|edit)|repo\\s+(create|delete|edit)|api)\\b)`),
  },
  { label: 'a publish', pattern: /^((npm|pnpm|yarn|bun)\s+publish|twine\s+upload|cargo\s+publish|docker(\s+compose)?\s+push|gem\s+push|vsce\s+publish|ovsx\s+publish)\b/ },
  {
    label: 'a deploy',
    pattern: /^(kubectl\s+(apply|delete|rollout)|terraform\s+(apply|destroy)|helm\s+(install|upgrade|uninstall)|vercel(\s+deploy)?(\s+\S+)*\s+--prod|netlify\s+deploy|fly\s+deploy|firebase\s+deploy)\b/,
  },
  {
    label: 'a destructive delete',
    pattern: new RegExp(
      String.raw`^rm\s+(-\S*\s+)*((\/|~|\$HOME|\$\{HOME\}|\$PWD|\.\.?|\.git)\/?\*?|\*)(\s|$)` +
        String.raw`|^${GIT}(reset\s+(\S+\s+)*--hard|clean\s+(\S+\s+)*-\S*f|branch\s+(\S+\s+)*-D|checkout\s+(\S+\s+)*(--\s+)?\.(\s|$)|stash\s+(drop|clear))`,
    ),
  },
  {
    label: 'a send',
    pattern: /^(curl|wget)\b.*(\s(-[a-zA-Z]*X|--request)(=|\s*)(POST|PUT|PATCH|DELETE)\b|\s(-[a-zA-Z]*[dFT]|--data\S*|--form\S*|--json|--upload-file|--post-data|--post-file|--method=\S+)\b)|^(sendmail|mail)\b/,
  },
  {
    label: 'networked PR/CI polling',
    pattern: new RegExp(`^pr-status(\\s|$)|^__PR_STATUS__|^(${GH_PR}(checks|view|status)|${GH}run\\s+(watch|view|list))\\b`),
  },
]

/** MCP tools that write to the outside world. */
const MCP_WRITE = /^mcp__.+__(create|merge|send|delete|push|update|publish|trash|forward|reply|share|add_.*comment|enable_pr_auto_merge|request_)/i

/** The rule a subagent's call breaks by being outside the main session, if any. */
export function mainOnlyLabel(tool: string, command: string | undefined): string | undefined {
  if (tool === 'Bash' && command !== undefined) {
    const parsed = parseLine(command)
    for (const { cmd } of parsed.simple) {
      const rule = MAIN_ONLY.find(one => one.pattern.test(cmd))
      if (rule !== undefined) return rule.label
    }

    return parsed.isTruncated && mightBeMainOnly(tool, command) ? 'a command too long or nested to read' : undefined
  }

  return MCP_WRITE.test(tool) ? 'an outward MCP write' : undefined
}

/** Calls that ship work out of the session: the boundary where checks and critic review are due. */
const OUTWARD = new RegExp(
  `^(${GIT}push\\b(?!.*\\s(--dry-run|-n)\\b)|${GH_PR}(create|merge)\\b|${GH}release\\s+create\\b|(npm|pnpm|yarn|bun)\\s+publish\\b|twine\\s+upload\\b|cargo\\s+publish\\b|docker(\\s+compose)?\\s+push\\b|vsce\\s+publish\\b|kubectl\\s+apply\\b|terraform\\s+apply\\b|helm\\s+(install|upgrade)\\b|netlify\\s+deploy\\b|fly\\s+deploy\\b|firebase\\s+deploy\\b)`,
)
const MCP_OUTWARD = /^mcp__github__(create_pull_request|merge_pull_request|push_files|create_or_update_file|enable_pr_auto_merge)$/

export function isOutward(tool: string, command: string | undefined): boolean {
  if (tool !== 'Bash') return MCP_OUTWARD.test(tool)

  const parsed = parseLine(command ?? '')

  return parsed.simple.some(one => OUTWARD.test(one.cmd)) || (parsed.isTruncated && mightShip(tool, command))
}

/** Commands that count as a check when they head a simple command (after wrappers): tests, builds, linters, type-checkers, `task verify`. */
const CHECK = new RegExp(
  [
    String.raw`(pytest|py\.test|unittest|compileall|mypy|tox|nox)\b`,
    String.raw`(jest|vitest|mocha|tsc|pyright|eslint|playwright\s+test)\b`,
    String.raw`(yarn|pnpm)\s+(jest|vitest|mocha|tsc|eslint)\b`,
    String.raw`(ruff|flake8)\b`,
    String.raw`(npm|pnpm|yarn|bun)\s+(t|test|tests)\b`,
    String.raw`(npm|pnpm|yarn|bun)\s+run\s+(test|tests|build|lint|typecheck|check|ci|verify|validate)\S*`,
    String.raw`(npm|pnpm|yarn|bun)\s+(build|lint|typecheck|check)\b`,
    String.raw`cargo\s+(test|build|check|clippy|nextest)\b`,
    String.raw`go\s+(test|build|vet)\b`,
    String.raw`dotnet\s+(test|build)\b`,
    String.raw`(gradle|gradlew|mvn|mvnw)\s+\S*(test|build|check|verify)`,
    String.raw`make\s+(\S+\s+)*(test|tests|check|lint|build|ci|verify)\b`,
    String.raw`(ctest|rspec|phpunit)\b`,
    String.raw`rake\s+(test|spec)\b`,
    String.raw`(deno|swift)\s+test\b`,
    String.raw`node\s+(--test\b|\S*test\S*\.m?[jt]s\b)`,
    String.raw`bash\s+-n\b`,
    String.raw`claude\s+plugin\s+(test|validate)\b`,
    // The hunch launcher as its prompt hook prints it (quoted node path and script), or the hunch CLI.
    String.raw`(\S*node\s+)?\S*hunch\S*\s+task\s+verify\b`,
  ]
    .map(alt => `^${alt}`)
    .join('|'),
)

/**
 * Whether a Bash line is a check whose exit code speaks for itself: a check heads one of its commands,
 * no `--help`, and nothing after it can hide its exit code (a pipe without `pipefail`/`PIPESTATUS`,
 * `||`, or a later command after `;` without `set -e`).
 */
export function isCheck(command: string, extra = ''): boolean {
  let custom: RegExp | undefined
  try {
    custom = extra.trim() === '' ? undefined : new RegExp(`^(${extra})`)
  } catch {
    custom = undefined
  }
  const parsed = parseLine(command)
  if (parsed.isTruncated) return false
  const simple = parsed.simple
  const hasPipefail = /pipefail|PIPESTATUS/.test(command)
  const hasErrexit = /\bset\s+-\w*e/.test(command)

  return simple.some((one, index) => {
    if (/\s--help\b/.test(one.cmd) || !(CHECK.test(one.cmd) || custom?.test(one.cmd) === true)) return false
    const isLast = index === simple.length - 1
    if (one.after === '|') return hasPipefail
    if (one.after === '||') return false
    if (one.after === '&&' || one.after === '') return true

    return isLast || hasErrexit
  })
}

/** Paths a shell may write to without touching the work: temp, devices, caches. */
const SCRATCH = /^(&|\/tmp\/|\/dev\/|\/var\/tmp\/|\/proc\/|\$TMPDIR|\$\{TMPDIR|~\/\.cache\/|\$HOME\/\.cache\/)/

const PLACEHOLDER = /^__Q\d+__$/
const SCRIPT = /^[sy]([^\w\s]).*\1.*\1\w*$/

/** Shell commands that edit files in place, whatever their targets. */
const EDITS =
  /^(sed\s+(\S+\s+)*(-[a-zA-Z]*i\S*|--in-place\S*)|perl\s+(\S+\s+)*-[a-zA-Z]*i|patch\b|truncate\b|git\s+((apply|am|restore|merge|pull|cherry-pick|rebase|revert)\b|checkout\s+(\S+\s+)*--(\s|$)|stash\s+(pop|apply)\b)|prettier\s+(\S+\s+)*--write|black\b|isort\b|ruff\s+(format|(\S+\s+)*--fix)|eslint\s+(\S+\s+)*--fix|gofmt\s+(\S+\s+)*-w|cargo\s+fmt|rustfmt\b|clang-format\s+(\S+\s+)*-i)/

/** The files a Bash line may have changed that the Edit tools never saw: `['?']` when it cannot tell which. */
export function shellWrites(command: string): string[] {
  const written: string[] = []

  const parsed = parseLine(command)
  if (parsed.isTruncated) return ['?']

  for (const { cmd } of parsed.simple) {
    const words = cmd.split(/\s+/)
    for (const match of cmd.matchAll(/(^|[^<>])[&\d]?>{1,2}\s*(\S+)/g)) {
      const target = match[2] ?? ''
      if (!SCRATCH.test(target)) written.push(PLACEHOLDER.test(target) ? '?' : target)
    }
    if (/^(cp|mv|install|rsync|ln)\b/.test(cmd)) {
      const target = words[words.length - 1] ?? ''
      if (!SCRATCH.test(target)) written.push(PLACEHOLDER.test(target) ? '?' : target)
    } else if (/^tee\b/.test(cmd)) {
      written.push(...words.slice(1).filter(word => !word.startsWith('-') && !SCRATCH.test(word)))
    } else if (EDITS.test(cmd)) {
      // A sed/perl script (`s/a/b/`) is not a path.
      const paths = words.slice(1).filter(word => /[./]/.test(word) && !word.startsWith('-') && !SCRATCH.test(word) && !SCRIPT.test(word) && !PLACEHOLDER.test(word))
      written.push(...(paths.length > 0 ? paths : ['?']))
    }
  }

  return [...new Set(written)]
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
  // A change in the same millisecond as the start stays pending: fail closed.
  return entries.filter(one => one.at >= since || (only !== undefined && !only.includes(one.path)))
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

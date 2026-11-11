import type { Confidence, HunchCall, HunchConstraint, HunchLevel, HunchLog, JevEntry, JevStatus, SessionRow, Zone } from '../types'

// Pure helpers: no `$`, so the tests exercise them directly.

export const ROLES = ['scout', 'runner', 'builder', 'critic'] as const

const FIELDS = ['RESULT', 'EVIDENCE', 'CONFIDENCE', 'UNVERIFIED'] as const

// Uppercase and a colon, so prose like "Result was fine" is not taken for a header.
const HEADER = /^[ \t#*_\-]*(RESULT|EVIDENCE|CONFIDENCE|UNVERIFIED)[ \t*_]*:[ \t]*(.*)$/

// Quoted (`> ...`) and fenced text is someone else's card or sample output, not this report's
// headers. A fence opens with three or more backticks or tildes and closes with at least as many
// of the same; a backtick fence's info string holds no backtick, so inline code that fills a line
// (three backticks, a command, three backticks) opens nothing. So is an indented code block: lines
// set in by four spaces or a tab, from the first one below a blank line (or a closed fence, or the
// top) to the next line that is not set in.
const QUOTED = /^\s*>\s?/
const FENCE_OPEN = /^\s*(`{3,}(?=[^`]*$)|~{3,})/
const INDENTED = /^(?: {4}|\t)/

function closesFence(line: string, fence: string): boolean {
  const marks = line.trim()

  return marks.length >= fence.length && marks === fence.charAt(0).repeat(marks.length)
}

// Nothing is unverified: `none`, `none.`, `_none_`, `n/a`, `-`, `(none)`, `no unverified items`. A
// remark after the mark is read closed: it stays nothing only when it says everything ran (`none -
// all ran`, `None. All 12 tests passed.`) or adds nothing (`none at this time`). Any other remark is
// an item: `none of the paths ran`, `none (the migration is assumed to be idempotent)`. Only what
// happened or holds counts as ran (`ran`, `pass`, `passed`, `were run`), never a bare `run`, and
// only straight after its subject (`all`, `every path`, `all the 12 e2e tests`): in `all tests
// failed to pass` or `all the e2e tests were disabled so the rest passed` nothing ran. A `none` that
// is struck through (`~~none~~`) is taken back, so `~` is not emphasis here.
const NONE = /^\(?(?:none|nothing|n\/?a|nil)\)?(?![\w/])/i
const NO_ITEMS = /^(?:[-—–]+|no(?:ne)? unverified(?: (?:items?|claims?|parts?))?\.?)$/i
const ALL_RAN = new RegExp(
  '^(?:all|every|everything|each)(?: of)?(?: the)?(?: \\d+)?(?: (?:unit|e2e|integration|smoke|regression))?' +
    '(?: (?:tests?|checks?|paths?|cases?|suites?|specs?))?(?: (?:all|now))? ' +
    '(?:(?:have |has )?(?:ran|pass(?:ed|es)?|succeeded)|(?:(?:have |has )?been |were |was |got )run|' +
    '(?:(?:have |has )?been |were |was |are |is |got )?(?:passing|checked|verified|tested|covered|exercised|confirmed|green))' +
    '(?: (?:locally|here|cleanly|successfully|in ci))?$',
  'i',
)
const NO_MORE = /^(?:at this time|at present|at the moment|for now|so far|currently|remaining|outstanding|left|known|to report|read-only task)$/i
const EMPHASIS = /[*`]|(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])/g

// Every line of an answer goes through these, so a line with nothing to take out is not copied: a
// long run of lines below a card stays cheap.
function unemphasized(text: string): string {
  return /[*`_]/.test(text) ? text.replace(EMPHASIS, '') : text
}

function unstarred(text: string): string {
  return text.includes('**') ? text.replace(/\*\*/g, '') : text
}

// A remark that takes the `none` back is an item: `none, except the migration`, `N/A: could not run`.
const CAVEAT =
  /\b(?:except\w*|but|however|apart|aside|besides|other than|though|although|not|cannot|never|could|did|unable|skip\w*|untested|unverified|unchecked|pending|unknown|unclear|unsure)\b|n['’]t\b/i

// So is a remark about what has not happened yet: `none - all tests should pass`, `N/A - all checks
// will be run in CI`, `none - everything still needs to be verified`.
const NOT_YET =
  /\b(?:will|would|should|need|needs|needed|yet|still|to be|able|expect|assum\w*|might|may|could|pending|later|todo|hope)\b/i

const BULLET = /^\s*(?:[-*+•]|\d+[.)])\s+/

function unbulleted(line: string): string {
  const bullet = BULLET.exec(line)

  return bullet === null ? line : line.slice(bullet[0].length)
}

function isNothing(line: string): boolean {
  const item = unemphasized(unbulleted(line)).trim()
  if (NO_ITEMS.test(item)) return true
  const mark = NONE.exec(item)
  if (mark === null) return false
  const remark = item.slice(mark[0].length)
  if (remark === '') return true

  return (
    !CAVEAT.test(remark) &&
    !NOT_YET.test(remark) &&
    remark
      .split(/[.,;:!()]|\s[-—–]\s|^\s*[-—–]+/)
      .map(clause => clause.trim())
      .every(clause => clause === '' || ALL_RAN.test(clause) || NO_MORE.test(clause))
  )
}

// What may stand below a card without being an open item: a rule, a sign-off (`Done.`, `That is
// all.`), or a line that only says everything ran. Any other line there is read closed, as an
// unverified item: `Caveat: ...`, `Two things remain open: ...`, `Note: two tests fail on main.`.
const RULE = /^(?:[-*_=][ \t]*){3,}$/
const SIGN_OFF = /^(?:done|all done|that is all|that['’]s all|that is it|that['’]s it|thanks|thank you|end of report)$/i

function isClosing(raw: string): boolean {
  if (raw.length > 200) return false
  if (RULE.test(raw.trim())) return true
  const plain = unemphasized(raw).trim()
  const said = plain.slice(0, tailStart(plain, '.!'))
  if (said === '' || SIGN_OFF.test(said)) return true
  if (CAVEAT.test(said) || NOT_YET.test(said)) return false
  // Checked before the line is split, which is costly in a run of thousands of lines: only marks
  // and blanks is no clause at all; a line with more that names no `all`, `every` or `each` has a
  // clause that does not say everything ran (a dash between blanks is read as a word here).
  if (!/[^.,;:!()\s]/.test(said)) return true
  if (!/\b(?:all|every|each)/i.test(said)) return false
  if (!/[.,;:!()]|\s[-—–]\s/.test(said)) return ALL_RAN.test(said.trim())

  return said
    .split(/[.,;:!()]|\s[-—–]\s/)
    .map(clause => clause.trim())
    .every(clause => clause === '' || ALL_RAN.test(clause))
}

/**
 * `isBlank`: nothing stands under the header yet, not even quoted, fenced or indented text.
 * `items`: lines of an UNVERIFIED section that are items whatever they say (quoted, fenced, indented).
 */
type Section = { name: string; lines: string[]; isAllNothing: boolean; isBlank: boolean; items: number }

/** Where a scan stands between two lines: the fence left open, an indented block that runs on. */
type BlockState = { fence: string; isIndented: boolean; wasBlank: boolean; hasOutside: boolean }

// Lines kept for a card whose UNVERIFIED section is still to come; one is enough to grade it.
const HELD_MAX = 20

/** The block state below the lines, read the way `scanCards` reads them. */
function blockState(lines: string[]): BlockState {
  let fence = ''
  let isIndented = false
  let wasBlank = true
  let hasOutside = false

  for (const raw of lines) {
    const isBlank = raw.trim() === ''
    const isBelowBlank = wasBlank
    wasBlank = isBlank

    if (fence !== '') {
      if (closesFence(raw, fence)) {
        fence = ''
        wasBlank = true
      }
      continue
    }
    const opened = FENCE_OPEN.exec(raw)
    if (opened !== null) {
      fence = opened[1] ?? ''
      isIndented = false
      continue
    }
    if (isIndented) {
      if (!isBlank && !INDENTED.test(raw)) isIndented = false
    } else if (!isBlank && isBelowBlank && INDENTED.test(raw)) {
      isIndented = true
    }
    if (!isIndented && !isBlank) hasOutside = true
  }

  return { fence, isIndented, wasBlank, hasOutside }
}

/**
 * Adds a line, keeping track of whether an UNVERIFIED section still says nothing but `none`.
 * `isItem`: the line is an open item even when it reads `none` (`> n/a`, a fenced line).
 */
function addLine(section: Section, line: string, isItem = false): void {
  section.lines.push(line)
  section.isBlank = false
  if (section.name !== 'UNVERIFIED') return
  if (isItem) {
    section.items++
    section.isAllNothing = false
  } else if (section.isAllNothing) {
    section.isAllNothing = isNothing(line)
  }
}

/**
 * Every card in the lines, in order, the text of each fenced or indented block, and whether
 * anything but blank lines stands outside those blocks. Fields may come in any
 * order. A card starts at a RESULT header that repeats one or stands apart from the field above
 * it (a blank line, closing prose), and where CONFIDENCE or UNVERIFIED repeats: the new card
 * keeps only the fields below the earlier one, so it never borrows a grade from the card above;
 * a repeated CONFIDENCE keeps the earlier one beside it, and the weaker of the two grades the card.
 * A repeated EVIDENCE only adds to the card. `start` is the state the first line is read in, for
 * the tail of a longer answer. `isCutOpen`: the lines end inside a fence that holds a header, so
 * what looks like a quoted card may be the report's own below a fence the cut left unmatched.
 */
function scanCards(
  lines: string[],
  start?: BlockState,
): { cards: Section[][]; blocks: string[][]; hasOutside: boolean; isCutOpen: boolean } {
  const cards: Section[][] = []
  const blocks: string[][] = []
  let hasOutside = start?.hasOutside ?? false
  let current: Section | undefined
  let fence = start?.fence ?? ''
  let isIndented = start?.isIndented ?? false
  let wasBlank = start?.wasBlank ?? true
  let isAfterBlank = false
  let hadBlank = false
  let hasFencedHeader = false
  let held: [string, boolean][] = []
  if (fence !== '' || isIndented) blocks.push([])

  // Prose outside the fields is read closed. Below a card that has its UNVERIFIED section, every
  // line that is not a closing is an item of it, whichever field came last (`However, I could not
  // run the e2e suite.`, `Remaining risks:` and its bullets). Before that section exists the same
  // holds for a line between the fields (`Two tests fail on main.`, `Known issue: ...`): it is held
  // and becomes an item when the section comes. A quoted, fenced or indented line (`isItem`) is
  // one whatever it says, a closing or `none` too.
  const takeBack = (raw: string, isItem = false): void => {
    const card = cards[cards.length - 1]
    const line = unstarred(raw).trim()
    if (card === undefined || line === '') return
    const open = card.find(section => section.name === 'UNVERIFIED')
    if (open === undefined) {
      if (held.length < HELD_MAX && (isItem || !isClosing(raw))) held.push([line, isItem])
    } else if (isItem || !isClosing(raw)) {
      addLine(open, line, isItem)
    }
  }

  for (const raw of lines) {
    let foreign: string | undefined
    const isBlank = raw.trim() === ''
    const isBelowBlank = wasBlank
    wasBlank = isBlank

    if (fence !== '') {
      if (closesFence(raw, fence)) {
        fence = ''
        wasBlank = true
        continue
      }
      blocks[blocks.length - 1]?.push(raw)
      if (!hasFencedHeader && HEADER.test(unstarred(raw))) hasFencedHeader = true
      foreign = raw
    } else {
      const opened = FENCE_OPEN.exec(raw)
      if (opened !== null) {
        fence = opened[1] ?? ''
        hasFencedHeader = false
        isIndented = false
        blocks.push([])
        continue
      }
      if (isIndented) {
        // A blank line does not end the block; the next line that is not set in does.
        if (!isBlank && !INDENTED.test(raw)) isIndented = false
      } else if (!isBlank && isBelowBlank && INDENTED.test(raw)) {
        isIndented = true
        blocks.push([])
      }
      if (isIndented) blocks[blocks.length - 1]?.push(raw.replace(INDENTED, ''))
      if (isIndented && !isBlank) foreign = raw
      else if (!isBlank) hasOutside = true
      if (foreign === undefined && QUOTED.test(raw)) foreign = raw.replace(QUOTED, '')
    }

    if (foreign !== undefined) {
      // Never a header of this report; inside its UNVERIFIED section it is an item whatever it says,
      // a fenced or indented block below a plain `none` too (`the Windows path is broken`, `> n/a`).
      if (current !== undefined && foreign.trim() !== '') current.isBlank = false
      if (current?.name === 'UNVERIFIED' && foreign.trim() !== '') {
        addLine(current, foreign.trim(), true)
        isAfterBlank = false
      } else if (current?.name !== 'UNVERIFIED' && foreign.trim() !== '') {
        // Anywhere else it is prose that may take the card back: held, or added, as an unverified item.
        // Except a fenced or indented block straight under EVIDENCE: that is its output, unless the line
        // says what did not happen (`Caveat: e2e not run`).
        const isOutput = (fence !== '' || isIndented) && current?.name === 'EVIDENCE' && !hadBlank
        if (!isOutput || CAVEAT.test(foreign) || NOT_YET.test(foreign)) takeBack(foreign, true)
      }
      continue
    }

    const line = unstarred(raw)
    const header = HEADER.exec(line)

    if (header !== null) {
      const [, name = '', rest = ''] = header
      const card = cards[cards.length - 1]
      const seen = card === undefined ? -1 : card.map(section => section.name).lastIndexOf(name)
      const isApart = current === undefined || hadBlank
      current = { name, lines: [], isAllNothing: true, isBlank: true, items: 0 }
      if (rest.trim() !== '') addLine(current, rest.trim())
      isAfterBlank = false
      hadBlank = false
      if (card === undefined || (name === 'RESULT' && (seen >= 0 || isApart))) {
        cards.push([current])
        held = []
      } else if (seen < 0 || name === 'EVIDENCE') {
        card.push(current)
      } else {
        const stated = name === 'CONFIDENCE' ? card.filter(section => section.name === 'CONFIDENCE') : []
        cards.push([...stated, ...card.splice(seen + 1), current])
      }
      if (name === 'UNVERIFIED') {
        for (const [caveat, isItem] of held) addLine(current, caveat, isItem)
        held = []
      }
    } else if (current !== undefined) {
      // A section runs to the next header. Below a blank line only list items (and quoted or
      // fenced text) still belong to it, so closing prose is not read as an unverified item;
      // prose there that takes the card back (`However, I could not run ...`, `Note: the Windows
      // path was assumed to work.`) is one, here and anywhere below the card.
      if (line.trim() === '') {
        isAfterBlank = current.lines.length > 0
        hadBlank = true
      } else if (isAfterBlank && !BULLET.test(line) && !(current.name === 'UNVERIFIED' && (CAVEAT.test(line) || NOT_YET.test(line)))) {
        current = undefined
        isAfterBlank = false
        takeBack(raw)
      } else {
        addLine(current, line.trim())
        isAfterBlank = false
        hadBlank = false
      }
    } else {
      takeBack(raw)
    }
  }

  return { cards, blocks, hasOutside, isCutOpen: fence !== '' && hasFencedHeader }
}

export type ParsedCard = {
  hasAnyField: boolean
  confidence: Confidence
  /** What the CONFIDENCE section said, for a card whose wording names no level. */
  confidenceText: string
  missing: string[]
  unverified: number
  exitCodes: number[]
  isWeak: boolean
  result: string
}

// Secrets: every string that reaches the timeline, a drawer, the band, the status line or plugin
// state passes through `redact`. It reads at most REDACT_MAX characters, and every pattern starts
// on a literal with bounded gaps, so the cost grows with the length of the text, not its square.

const MASK = '***'
const REDACT_MAX = 20_000

// A word on a command line, to the next blank, whatever it holds: bare text with the quoted parts
// glued to it (`ab'c d'"e"`, `$'a\'b'`, `"a\"b"`, `admin:'x'`). A quote left open runs to the end
// of its line. It always ends a pattern, so its alternatives are never tried a second time. A
// backtick before a newline is PowerShell's continuation, not part of the word (`NAME` + backtick).
const ARGUMENT = `(?:\\$'(?:[^'\\\\\\n]|\\\\.)*'?|"(?:[^"\\\\\\n]|\\\\.)*"?|'[^'\\n]*'?|\\\\.|\`(?!\\r?\\n)|[^\\s"'\\\\\`])+`

// A newline the command runs on over: the one after a `\` (or PowerShell's backtick), CR or not.
const CONTINUED = '(?<=[\\\\`]\\r?)\\n'
// The blank before a flag, or the start of a continuation line.
const BLANK = '(?:[ \\t]|(?<=\\n))'
// The blank between two words of a command: spaces, a few continued lines (a `\` or backtick, then
// the newline) and the indent after each, however deep the indent is set (within 200).
const STORE_GAP = '(?:[ \\t]{1,200}(?:[\\\\`]\\r?\\n[ \\t]{0,200}){0,8}|(?:[\\\\`]\\r?\\n[ \\t]{0,200}){1,8})'
// The rest of a word a continuation cuts in two (`hunter\` newline `2xyz`), the mark and the newline
// with it: a value is masked whole.
const SPLIT_TAIL = '(?:[\\\\`]\\r?\\n(?:' + ARGUMENT + '))*'
// What joins a flag to its value when no blank does: `=`, and a continuation right after it.
const EQUALS = '=(?:[\\\\`]\\r?\\n[ \\t]{0,200})?'

// The rest of one command, up to a separator (`&&`, `||`, `;`, `|`) or the end of the line, a
// continued line being one line; and the same past one or two quoted arguments, inside which a
// separator ends nothing (`-e "select 1; select 2"`).
const UNQUOTED = `(?:[^\\n&;|"']|${CONTINUED}){0,300}`
const QUOTED_ARGUMENT = `(?:"[^"\\n]{0,300}"|'[^'\\n]{0,300}')`
const SAME_COMMAND = [
  `(?:[^\\n&;|]|${CONTINUED}){0,1000}?`,
  `${UNQUOTED}${QUOTED_ARGUMENT}(?:${UNQUOTED}${QUOTED_ARGUMENT})?(?:[^\\n&;|]|${CONTINUED}){0,300}?`,
] as const

/** A bearer token rather than a word of prose (`Bearer authentication is required`). */
function looksSecret(token: string): boolean {
  return /\d/.test(token) || (/[a-z]/.test(token) && /[A-Z]/.test(token.slice(1))) || token.length >= 24
}

type Rule = readonly [RegExp, string | ((...groups: string[]) => string)]

// A later URL glued into the same word: `A=http://localhost:8080/x,B=postgres://u:p@h/db`.
const NEXT_URL = /:\/\/[^\s/?#:"'<>]{0,100}:/

/**
 * Where the last `@` is in the word after `scheme://` (`user:pass@host/path`), or -1: whatever
 * follows it is taken for the host, so nothing a password may hold (`/`, `?`, `#`, `@`, a quote) and
 * nothing after the host (`"`, `**`, `\share`, `[::1]`) leaves it shown. Where the colon may lead a
 * port (digits followed by `/` or the end of the word: `host:8080/a?to=a@b.com`,
 * `host:5173/@vite/client`), an `@` in the query or starting a path segment is not userinfo;
 * `user:12345/abc@host` still is, and so is `user:12345?x@host`.
 */
function hostAt(rest: string): number {
  const port = /^[^:]*:\d{1,5}(?=\/|$)/.exec(rest)?.[0]
  const query = port === undefined ? -1 : rest.slice(port.length).search(/[?#]/)
  const last = port === undefined || query < 0 ? rest.length : port.length + query
  for (let at = rest.lastIndexOf('@', last); at >= 0; at = at === 0 ? -1 : rest.lastIndexOf('@', at - 1)) {
    if (!(port !== undefined && rest.charAt(at - 1) === '/')) return at
  }

  return -1
}

/**
 * The word after `scheme://` with its userinfo masked. The whole word is read, however long, so a
 * password is never cut before its `@`. When the first URL has no userinfo (`localhost:8080/x?y=1`)
 * a later `://user:` in the same word is read the same way; past a few of those the rest of the
 * word is masked rather than read on.
 */
function maskUserinfo(lead: string, rest: string): string {
  let shown = lead
  let word = rest
  for (let turn = 0; turn < 8 && word.includes('@'); turn++) {
    const at = hostAt(word)
    if (at >= 0) return `${shown}${MASK}${word.slice(at)}`
    const next = NEXT_URL.exec(word)
    if (next === null) return shown + word
    shown += word.slice(0, next.index + 3)
    word = word.slice(next.index + 3)
  }

  return word.includes('@') ? `${shown}${MASK}` : shown + word
}

// SQL sets a password with no `=`: `ALTER USER app WITH PASSWORD 'x'`, `CREATE USER x IDENTIFIED BY
// "x"`, `IDENTIFIED WITH plugin BY 'x'`. The literal after the word is masked to its closing quote (a
// doubled quote is part of it), or to the end of the line when it has none; the quotes stay.
const SQL_PASSWORD =
  /((?:password|\bidentified(?:[ \t]{1,4}with[ \t]{1,4}[\w'"`-]{1,60})?[ \t]{1,4}by)[ \t]{1,4})(\\"[^"\n]*"?|'(?:[^'\n]|'')*'?|"(?:[^"\n]|"")*"?)/gi

function maskSql(source: string): string {
  return source.replace(SQL_PASSWORD, (all: string, lead: string, literal: string, offset: number) => {
    const mark = literal.startsWith('\\') ? '\\"' : literal.charAt(0)
    // In `grep -i "password " src/` the quote closes the one the word stands in; it opens no literal.
    const isCloser = mark.length === 1 && /^(?:[\s;&|),}\]]|$)/.test(source.charAt(offset + lead.length + 1))
    if (isCloser && openOn(source, offset).quote === mark) return all

    return `${lead}${mark}${MASK}${mark}`
  })
}

// A name holds a credential when it ENDS with one of these words: `DB_PASSWORD`, `--client-secret`,
// `X-Api-Key`, `accessToken`, `MYSQL_PWD`. `sort_key`, `primary_key`, `max-tokens` and `token-file`
// do not. The short words also need a boundary before them, so `compass` and `OAuth` are not names.
const KEY_KIND = 'api|access|private|secret|auth|signing|encryption|master|license|ssh|client|account|storage|session|deploy|webhook'
// The `kind-key` names come first: in `secret_key` the name is all of it, not `secret` with a tail.
const SECRET_END = `(?:${KEY_KIND})[_-]?key|passphrase|password|passwd|pwd|pass|secret|token|credentials?|authorization|auth|apikey`
const SHORT_END = /^(?:pwd|pass|auth)$/i
// `pass` and `pwd` glued to what the password is for: `DBPASS`, `--dbpass`, `userpass`, `rootpwd`.
const GLUED_PASS = /(?:^|[^A-Za-z])(?:db|user|usr|root|admin|sql|mysql|pg|ftp|smtp|ssh|sudo|http|proxy|rpc|keystore|store|key)$/i

/** Whether a short word is only the end of another one (`compass`, `OAuth`); `userPass` and `dbpass` are names. */
function isWordEnd(prefix: string, word: string): boolean {
  if (!SHORT_END.test(word)) return false
  const before = prefix.slice(-1)
  const isCamel = /[a-z0-9]/.test(before) && /^[A-Z][a-z]/.test(word)
  const isGlued = /^p/i.test(word) && GLUED_PASS.test(prefix)

  return /[A-Za-z0-9]/.test(before) && !isCamel && !isGlued
}

// An environment name may carry the word in the middle: `SECRET_KEY_BASE`, `DB_PASSWORD_PROD`. The
// whole name is UPPER_SNAKE and the word one of its segments; a name that ends in one of these
// says where the credential is or how big, not what it is (`PASSWORD_FILE=/run/secrets/x`).
const UPPER_SNAKE = /^[A-Z][A-Z0-9_]*$/
const NOT_SECRET_TAIL = /_(?:FILE|PATH|ID|URL|NAME|DIR|ENV|LENGTH|LEN|SIZE)$/
// In any case, a tail that only says which one it is keeps the name a credential: `secret_key_base`,
// `api_key_prod`, `password_old`, `token_2`.
const WHICH_TAIL =
  /^(?:_(?:base|prod|production|dev|development|staging|stage|test|live|old|new|current|previous|primary|secondary|backup|value|raw|plain|plaintext|b64|base64|v?\d{1,4}))+$/i
// A name given apart from its value (`- name: DB_PASSWORD`): the word is a segment of it or ends it.
const SECRET_NAME = new RegExp(
  `(?:^|[^A-Za-z0-9])(?:${SECRET_END})(?![A-Za-z0-9])|(?:passphrase|password|passwd|secret|token|credentials?|apikey)$`,
  'i',
)
// An environment name whose last segment is `KEY`, whatever kind of key: `APP_KEY`, `HMAC_KEY`,
// `COSMOS_KEY`. These say which row or which entry, or are public: no credential.
const KEY_WORD = /^key$/i
const NOT_SECRET_KEY = /^(?:(?:PRIMARY|SORT|PARTITION|FOREIGN|CACHE|IDEMPOTENCY)_KEY|(?:[A-Z0-9_]*_)?(?:PUBLIC|PUBLISHABLE)_KEY)$/

function isKeyName(name: string): boolean {
  return UPPER_SNAKE.test(name) && name.endsWith('_KEY') && !NOT_SECRET_KEY.test(name)
}

/** Whether a name given apart from its value holds a credential: `DB_PASSWORD`, `Db:Password`, `COSMOS_KEY`. */
function namesSecret(name: string): boolean {
  return (SECRET_NAME.test(name) && !NOT_SECRET_TAIL.test(name)) || isKeyName(name)
}

// After `-p` or `-P`: the rest of `-password` or `-Password`, which is a flag of its own.
const NOT_SPELLED = '(?![aA][sS][sS][wW][oO][rR][dD](?![\\w-]))'

// A curl flag that takes a credential or a cookie, alone or last in a cluster (`-sSu`), and its value.
const CURL_SECRET = new RegExp(`(\\s(?:-[A-Za-z]{0,10}[uUb](?!-)|--(?:(?:proxy-)?user|cookie)(?![\\w-]))[ \\t]{0,4})(?:${ARGUMENT})`, 'g')

// A string literal in code, to its closing quote or the end of its line; never an empty one.
const LITERAL = `"(?:[^"\\\\\\n]|\\\\.)+"?|'(?:[^'\\\\\\n]|\\\\.)+'?`

// Run before the named values below. Only values are masked: the command, the flag and the host stay.
const SHAPES_BEFORE: readonly Rule[] = [
  // A PEM or PGP private key, whole; one cut short (no END line) is masked to the end.
  [
    /-----BEGIN ([A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?)-----[\s\S]*?(?:-----END \1-----|$)/g,
    `-----BEGIN $1-----${MASK}-----END $1-----`,
  ],
  // URL userinfo with a password, which may itself hold `/`, `?`, `#`, `@` or a quote: after
  // `scheme://user:` it runs to the last `@` in the word, so `user:p@ss/word@host` leaves no tail,
  // and neither does a user that is an email (`user@example.com:pw@host`). An email in the query of
  // `user:pw@host/a?to=a@b.com` is masked with it rather than risk one.
  [/(:\/\/)([^\s/?#:"'<>]{0,100}:\S*)/g, (_all, lead = '', rest = '') => maskUserinfo(lead, rest)],
  // Any other userinfo ends before the first `/`, `?` or `#`: `https://user@host/path` ->
  // `https://***@host/path`. An `@` in the query (`?to=a@b.com`) is not userinfo.
  [/(:\/\/)[^\s/?#@"'<>]+@/g, `$1${MASK}@`],
  // curl's credentials and cookies, every one on its line (a continued line being one line): `curl
  // -u user:pass`, `curl -sSu user:pass`, `--user "user:pass"`, `--proxy-user`, `-U`, and `-b
  // 'sid=x'`, `--cookie "sid=x"`.
  [new RegExp(`\\bcurl\\b(?:[^\\n]|${CONTINUED})*`, 'g'), line => line.replace(CURL_SECRET, `$1${MASK}`)],
  // A password glued to `-p` (`mysql -pX`, `docker login -pX`, `sshpass -pX`) or after it at a login
  // or a mongo tool (`docker login -p X`, `mongosh -p X`, `mongodump -p X`, `sshpass -p X`), and
  // after SQL Server's `-P` (`sqlcmd -U sa -P X`, `bcp ... -P X`), within that command only: in
  // `docker login registry && mkdir -p out` the `-p` is mkdir's. A flag spelled out (`Invoke-Sqlcmd
  // -Password X`, `login -password X`) is no `-p` with `assword` glued to it: the named values
  // below mask what follows it. So is PsExec's `-p X`.
  ...SAME_COMMAND.flatMap((gap): Rule[] => [
    [new RegExp(`(\\b(?:mysql\\w{0,12}|mariadb\\w{0,12}|mycli)\\b${gap}${BLANK}-p${NOT_SPELLED})(?:${ARGUMENT})`, 'g'), `$1${MASK}`],
    [
      new RegExp(`(\\b(?:login|mongo\\w{0,12}|[pP][sS][eE][xX][eE][cC](?:64)?)\\b${gap}${BLANK}-p${NOT_SPELLED}[ \\t]{0,4})(?:${ARGUMENT})`, 'g'),
      `$1${MASK}`,
    ],
    // The command in any case (`Sqlcmd`, `BCP`); the flag is `-P` only.
    [
      new RegExp(`(\\b(?:[sS][qQ][lL][cC][mM][dD]|[bB][cC][pP])\\b${gap}${BLANK}-P${NOT_SPELLED}[ \\t]{0,4})(?:${ARGUMENT})`, 'g'),
      `$1${MASK}`,
    ],
  ]),
  [new RegExp(`(\\bsshpass[ \\t]{1,4}-p[ \\t]{0,4})(?:${ARGUMENT})`, 'g'), `$1${MASK}`],
  // Windows: `schtasks ... /rp X` (and `/p X`), `xfreerdp /p:X`, and the password that follows the
  // share or the user: `net use \\srv\share X /user:me`, `net user admin X /add`.
  ...SAME_COMMAND.flatMap((gap): Rule[] => [
    [new RegExp(`(\\bschtasks(?:\\.exe)?${gap}${BLANK}/r?p[ \\t]{1,4})(?:${ARGUMENT})`, 'gi'), `$1${MASK}`],
    [new RegExp(`(\\b[a-z]{0,3}freerdp\\d?(?:\\.exe)?${gap}${BLANK}/p:)(?:${ARGUMENT})`, 'gi'), `$1${MASK}`],
  ]),
  [
    new RegExp(
      `(\\bnet(?:\\.exe)?[ \\t]{1,4}use[ \\t]{1,4}(?:[a-z*]:?[ \\t]{1,4})?(?:"\\\\\\\\[^"\\n]{1,300}"|\\\\\\\\\\S{1,300})[ \\t]{1,4})(?![/-])(?:${ARGUMENT})`,
      'gi',
    ),
    `$1${MASK}`,
  ],
  [new RegExp(`(\\bnet(?:\\.exe)?[ \\t]{1,4}user[ \\t]{1,4}(?![/-])[^\\s/]{1,100}[ \\t]{1,4})(?![/-])(?:${ARGUMENT})`, 'gi'), `$1${MASK}`],
  // A secret given whole to its store: `aws secretsmanager ... --secret-string X`, `--secret-binary X`,
  // the value on the line it is continued to or not.
  [new RegExp(`((?<![\\w-])--secret-(?:string|binary)(?:${STORE_GAP}|${EQUALS}))(?:${ARGUMENT})${SPLIT_TAIL}`, 'g'), `$1${MASK}`],
  // openssl's pass phrase source: `-passin pass:X`, `-passout pass:X`, `-pass pass:X`, `-password
  // pass:X`, the phrase quoted or not (`pass:'two words'`, `"pass:two words"`).
  [
    new RegExp(`((?<![\\w-])-{1,2}pass(?:in|out|word)?[ \\t=]{1,4})(?:"pass:[^"\\n]*"?|'pass:[^'\\n]*'?|pass:(?:${ARGUMENT})?)`, 'g'),
    `$1pass:${MASK}`,
  ],
  // Redis' password: `redis-cli -h host -a X ping`, `redis-cli --pass X`.
  ...SAME_COMMAND.map(
    (gap): Rule => [new RegExp(`(\\bredis-cli\\b${gap}${BLANK}(?:-a|--pass)[ \\t]{1,4})(?:${ARGUMENT})`, 'g'), `$1${MASK}`],
  ),
  // A netrc entry, on one line or several: `machine github.com login me password X`.
  [
    /(\b(?:machine|login)[ \t]{1,4}[^\s"']{1,200}\s{1,4}(?:[^\n"']{0,200}?\s{1,4})?password[ \t]{1,4})([^\s"'&;|)]{1,500})/gi,
    (all, lead = '', value = '') => (isNotValue(value, false, false) ? all : `${lead}${MASK}`),
  ],
  // A credential set by name: `aws configure set aws_secret_access_key X`.
  [
    new RegExp(`(\\bconfigure[ \\t]{1,4}set[ \\t]{1,4}[\\w.]{0,60}(?:secret_access_key|session_token)[ \\t]{1,4})(?:${ARGUMENT})`, 'gi'),
    `$1${MASK}`,
  ],
  // PowerShell's plain-text conversion: `ConvertTo-SecureString "abc" -AsPlainText`.
  [new RegExp(`(\\bConvertTo-SecureString(?:[ \\t]{1,4}-\\w{1,20}){0,4}[ \\t]{1,4})(?!-)(?:${ARGUMENT})`, 'gi'), `$1${MASK}`],
  // A Go DSN: `root:x@tcp(127.0.0.1:3306)/db`.
  [/(\b[\w.-]{1,64}:)(?!\/\/)[^\s"'`]{1,200}?(?=@(?:tcp|unix|udp)[46]?\()/g, `$1${MASK}`],
  // A setter called with a literal: `conn.setPassword("x")`, `.password('x')`, `set_api_key("x")`.
  [
    new RegExp(`((?:\\.|\\b(?:set|with|use|add|update|change|reset)_?)(?:${SECRET_END})\\([ \\t]{0,4})(${LITERAL})`, 'gi'),
    (_all, lead = '', literal = '') => `${lead}${literal.charAt(0)}${MASK}${literal.charAt(0)}`,
  ],
  // A credential passed by its name: `req.Header.Set("Authorization", "Basic x")`, `setHeader('X-Api-Key', 'x')`.
  [
    new RegExp(`((["'])([\\w.-]{0,40}?)(${SECRET_END})\\2[ \\t]{0,4},[ \\t]{0,4})(${LITERAL})`, 'gi'),
    (all, lead = '', _quote = '', prefix = '', word = '', literal = '') =>
      isWordEnd(prefix, word) ? all : `${lead}${literal.charAt(0)}${MASK}${literal.charAt(0)}`,
  ],
  // An XML element named like a credential: `<password>x</password>`, `<db.apiKey enc="no">x</db.apiKey>`.
  [
    new RegExp(`(<([\\w.:-]{0,40}?)(${SECRET_END})(?:\\s[^<>\\n]{0,200})?>)([^<\\n]{1,500})(?=</)`, 'gi'),
    (all, open = '', prefix = '', word = '', value = '') => (isWordEnd(prefix, word) || value.trim() === '' ? all : `${open}${MASK}`),
  ],
  // A Kubernetes env entry: `- name: DB_PASSWORD` with `value: x` on the next line.
  [
    /(\bname:[ \t]{0,4}["']?([\w.-]{1,100})["']?[ \t]{0,4}\r?\n[ \t]{0,40}value:[ \t]{0,4})([^\r\n]+)/g,
    (all, lead = '', name = '') => (namesSecret(name) ? `${lead}${MASK}` : all),
  ],
]

// A cookie header, whole: to the quote that closes the one it stands in (`-H "Cookie: a=1; b=2"`),
// else to the end of the line, whatever its values hold (`theme="dark"; sessionid=abc`).
const COOKIE = /(\bcookie[ \t]{0,4}:[ \t]{0,4})([\w.%-]{1,100}=[^\r\n]*)/gi

function maskCookies(source: string): string {
  return source.replace(COOKIE, (_all: string, lead: string, value: string, offset: number) => {
    const { quote } = openOn(source, offset)
    const close = quote === '' ? -1 : value.indexOf(quote)
    // An escaped closer (`\"`) keeps its backslash.
    const end = close > 0 && value.charAt(close - 1) === '\\' ? close - 1 : close

    return `${lead}${MASK}${end < 0 ? '' : value.slice(end)}`
  })
}

// A secret store's command and the words after it, on its line: `gh secret set NAME --body x`,
// `dotnet user-secrets set "Db:Password" "x"`, `fly secrets set NAME=x`, `heroku config:set NAME=x`,
// and the settings that take a name and a value: `npm config set //host/:_authToken x`, `pulumi
// config set --secret NAME x`, `rclone config create r s3 secret_access_key x`, `setx NAME x`.
const STORE_VERB =
  '\\b(?:config(?:[ \\t]{1,4}|:)(?:set|create|update|password)|user-secrets[ \\t]{1,4}set|secrets?(?:[ \\t]{1,4}|:)(?:set|put|create|add)|setx(?:\\.exe)?)'
const STORE = new RegExp(
  `(${STORE_VERB})` +
    `((?:${STORE_GAP}(?![&;|])(?:${ARGUMENT})){1,64})`,
  'gi',
)
const STORE_WORD = new RegExp(`(${STORE_GAP})(${ARGUMENT})`, 'g')
const LINE = new RegExp(`(?:[^\\n]|${CONTINUED})+`, 'g')
const VALUE_FLAG = new RegExp(`(${BLANK}--value(?:${STORE_GAP}|${EQUALS}))(?:${ARGUMENT})${SPLIT_TAIL}`, 'g')
const NAME_FLAG = new RegExp(`${BLANK}--name(?:[ \\t]{1,4}|=)(${ARGUMENT})`, 'g')
// What makes a `--value` a secret: `aws ssm put-parameter`, `az keyvault secret set`, `--type SecureString`.
const VALUE_STORE = /secret|\bssm\b|keyvault|securestring/i
// A line that may hold one of them, checked before the line is read word by word.
const STORE_HINT = /config|secret|setx|\bssm\b|keyvault|securestring|--name/i

/**
 * The words after a store's command with the values masked. Where the command stores secrets
 * (`secret set`, `config:set`, `--secret`) the first word is the name and every other one a value,
 * and so is what follows a `=`; elsewhere a value is the word after a name that holds a credential.
 * Flags stay, and so does a redirection.
 */
function maskStoreWords(verb: string, words: string, rest = ''): string {
  const isSecret = /secret|:/i.test(verb) || /(?:^|[ \t\n])--secret(?![\w-])/.test(`${words} ${rest}`)
  let seen = 0
  let isValue = false

  return words.replace(STORE_WORD, (word: string, blank: string, text: string) => {
    const pair = /^(-{0,2}[\w.:/-]{1,100}=)./.exec(text)
    // A value is masked whole, `=` and all (`--body aGk==`, `put K X==`); only a name's own `NAME=value` keeps its name.
    if (isSecret && pair !== null && (isValue || (seen > 0 && !text.startsWith('-')))) {
      seen++
      isValue = false

      return `${blank}${MASK}`
    }
    if (isSecret && pair !== null) return `${blank}${pair[1]}${MASK}`
    // The word after a value flag is a value, whatever came before it (`gh secret set --body x NAME`).
    if (isSecret && /^(?:-b|--body|--value|--secret-string|--secret-binary)=?$/.test(text)) {
      seen++
      isValue = true

      return word
    }
    // The word after a name or a value flag is masked whatever it starts with (`--body -x`).
    if (!isValue && /^[-<>]/.test(text)) return word
    const isMasked = isValue || (isSecret && seen > 0)
    seen++
    isValue = !isMasked && namesSecret(text.replace(/["']/g, ''))

    return isMasked ? `${blank}${MASK}` : word
  })
}

// The blanks and continuation marks after the words a store's command was read to.
const STORE_LEFT = /[ \t\r\n\\`]*/y

/**
 * A store's commands on one line with their values masked. Where the words were not read to the
 * end of the command (an indent past the gap, more words than are read) the rest of the line is
 * masked, unless a separator ends the command there.
 */
function maskStoreLine(line: string): string {
  let out = ''
  let at = 0
  STORE.lastIndex = 0
  for (let store = STORE.exec(line); store !== null; store = STORE.exec(line)) {
    const [all, verb = '', words = ''] = store
    const end = store.index + all.length
    STORE_LEFT.lastIndex = end
    STORE_LEFT.exec(line)
    const left = STORE_LEFT.lastIndex
    const isUnread = left < line.length && !/[&;|]/.test(line.charAt(left))
    out += `${line.slice(at, store.index)}${verb}${maskStoreWords(verb, words, isUnread ? line.slice(left) : '')}`
    at = end
    if (isUnread) return `${out}${line.slice(end, left)}${MASK}`
  }

  return out + line.slice(at)
}

// A store's verb or a value flag, and the blanks and continuation marks after it.
const UNREAD_GAP = new RegExp(
  `(?:${STORE_VERB}|(?<![\\w-])(?:--secret-(?:string|binary)|--value|--body|-b))([ \\t\\\\\`\\r\\n]+)`,
  'gi',
)

/**
 * The line with what follows a verb or a value flag masked when the blanks between them are more
 * than STORE_GAP reads (over 8 continued lines, an indent over 200): the value is then somewhere
 * the words are not read, so the rest of the line goes.
 */
function maskUnreadGap(line: string, isStore: boolean): string {
  UNREAD_GAP.lastIndex = 0
  const isSecretLine = isStore || /secret/i.test(line)
  for (let match = UNREAD_GAP.exec(line); match !== null; match = UNREAD_GAP.exec(line)) {
    const run = match[1] ?? ''
    const flag = match[0].slice(0, match[0].length - run.length).trim()
    if (/^-(?:-value|b|-body)$/.test(flag) && !isSecretLine) continue
    const pieces = run.split(/[\\`]\r?\n/)
    const end = match.index + match[0].length
    if (end >= line.length || (pieces.length <= 9 && pieces.every(piece => piece.length <= 200))) continue

    return `${line.slice(0, end)}${MASK}`
  }

  return line
}

/** Masks what a command hands to a secret store, line by line (a continued line being one line). */
function maskStores(source: string): string {
  if (!STORE_HINT.test(source)) return source

  return source.replace(LINE, (line: string) => {
    if (!STORE_HINT.test(line)) return line
    const names = [...line.matchAll(NAME_FLAG)].map(name => (name[1] ?? '').replace(/["']/g, ''))
    const isStore = VALUE_STORE.test(line) || names.some(namesSecret)
    const flagged = isStore ? line.replace(VALUE_FLAG, `$1${MASK}`) : line

    return maskStoreLine(maskUnreadGap(flagged, isStore))
  })
}

// Run after them.
const SHAPES_AFTER: readonly Rule[] = [
  [/\b(bearer[ \t]{1,4})([\w.~+/=-]{8,})/gi, (all, lead = '', token = '') => (looksSecret(token) ? `${lead}${MASK}` : all)],
  // Token shapes: GitHub (`ghp_`, `github_pat_`), GitLab (`glpat-`), npm, PyPI, `sk-...`, Stripe
  // (`sk_live_`), Slack (`xoxb-`), AWS key ids (`AKIA...`), Google (`AIza...`), Hugging Face
  // (`hf_`), SendGrid (`SG.x.y`).
  [
    /(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|glpat-[\w-]{20,}|npm_[A-Za-z0-9]{20,}|pypi-[\w-]{20,}|sk-[\w-]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{10,}|xox[a-z]-[\w-]{10,}|A[KS]IA[0-9A-Z]{16}|AIza[\w-]{35}|hf_[A-Za-z0-9]{30,}|SG\.[\w-]{16,}\.[\w-]{16,})/g,
    MASK,
  ],
  // A JSON web token: `eyJ<header>.<payload>.<signature>`.
  [/(?<![\w-])eyJ[\w-]{10,}\.[\w-]{10,}(?:\.[\w-]+)?/g, MASK],
  // A mask a continuation runs on from, no blank between (`hunter\` newline `2xyz`): the continued
  // text is the rest of the same word, and is masked with it, every piece.
  [/\*\*\*(?:[\\`]\r?\n(?:[^\s\\`]|\\(?!\r?\n)|`(?!\r?\n))+)+/g, MASK],
]

// The name's end, the segments after it, its closing quote (and the bracket of a subscript:
// `config['SECRET_KEY'] = x`), and what joins it to a value: `=`, `:` or `:=` (never `==`, `!=`, nor
// `=~` after a blank: `[[ $x =~ re ]]`), `=>` for a quoted or `:`-led name, or just a space when
// the name is a flag (`--password abc`, `-Token abc`). A bare `KEY` segment is read as a name too,
// for `maskedValue` to say whether it ends an environment name (`COSMOS_KEY=x`).
const KEY = new RegExp(
  `(${SECRET_END}|(?<=_)key)((?:_[A-Za-z0-9]{1,40}){0,8})((?:\\\\?["']\\]?)?)` +
    `([ \\t]{0,8}(?:=>|:=|=(?![=>])(?!(?<=[ \\t]=)~)|:(?![:=/]))[ \\t]{0,8}|[ \\t]{1,8}(?=[^\\s:=]))`,
  'gi',
)

// The value: `\\"escaped\\"`, "double" or 'single' quoted (to the closing quote, across lines, or to
// the end when it is cut short), or the start of a bare word, with its scheme for `Authorization:
// Bearer abc`. Where a bare value ends is for `bareEnd` to say.
const VALUE =
  /(\\"[^"]*(?:"|$))|("(?:[^"\\]|\\[\s\S]?)*(?:"|$))|('[^']*(?:'|$))|((?:(?:bearer|basic|token|digest|negotiate)[ \t]{1,4})?[^\s"'&;|)]+)/iy

const NAME_CHAR = /[\w.$-]/

// Bare words after a name that are code or prose, not a credential: `token = await getToken()`,
// `Authorization: required by the API`, `password: string`.
const NOT_A_VALUE = new Set(
  (
    'null none nil undefined true false nan await new function async yield typeof void this self string number boolean ' +
    'int integer bool str object any required optional is are was were be been the a an it its that these those to of ' +
    'for in on at by with from and or not no yes if when then than as via per must should shall will would can could ' +
    'may might has have had do does did see use used using set unset get given provided missing invalid expired empty ' +
    'blank unknown redacted hidden masked removed changed rotated revoked needed expected error failed here there which ' +
    'what who how why field header flag option parameter param argument variable env name value never always keep we ' +
    'you i they all each every some only also just so but because after before while once now todo tbd n/a ' +
    'authentication auth token tokens scheme bearer basic digest oauth oauth2 jwt'
  ).split(' '),
)
// Those of them that lead a credential in an `Authorization` header.
const SCHEME = /^(?:auth|tokens?|bearer|basic|digest|negotiate|oauth2?|jwt)$/i

/** Where a run of the given characters at the end of the value starts. */
function tailStart(value: string, chars: string): number {
  let end = value.length
  while (end > 0 && chars.includes(value.charAt(end - 1))) end--

  return end
}

function isNotValue(bare: string, isFlag: boolean, isTight: boolean): boolean {
  // A command substitution (`$(cat f)`) or a lone bracket (`auth={"user": ...}`) is code; after a
  // flag, so is another flag or a redirection.
  if (/^\$\(|^[{\[(]+$/.test(bare) || (isFlag && /^[-<>]/.test(bare))) return true
  // A call, an index, an object or an interpolation is code (`getToken()`, `os.environ["X"]`,
  // `{"user": "x"}`, `${token}`), but not in an assignment with no space around its `=`: that is a
  // value whatever it looks like (`PASSWORD=Summer(2024)!`, `DB_PASSWORD=hunter[2]x`).
  if (!isTight && /^(?:[A-Za-z_$][\w$.]*)?[(\[]|^\$?\{/.test(bare)) return true
  if (bare.length > 40) return false
  const word = bare.slice(0, tailStart(bare, '.,;:!?')).toLowerCase()

  return NOT_A_VALUE.has(word.split(/[ \t]+/).at(-1) ?? word)
}

const OPENER: Record<string, string> = { ')': '(', ']': '[', '}': '{' }

/**
 * The quote left open on the line before `end`, and the brackets left open there (outside any
 * quote that has closed since); both '' when the line is too long to say.
 */
function openOn(source: string, end: number): { quote: string; brackets: string } {
  const lineStart = source.lastIndexOf('\n', end - 1) + 1
  if (end - lineStart > 500) return { quote: '', brackets: '' }
  let quote = ''
  let brackets = ''
  let outside = 0
  for (let at = lineStart; at < end; at++) {
    const char = source.charAt(at)
    if (char === quote) {
      quote = ''
      brackets = brackets.slice(0, outside)
    } else if (quote === '' && (char === '"' || (char === "'" && !(/\w/.test(source.charAt(at - 1)) && /\w/.test(source.charAt(at + 1)))))) {
      // An apostrophe inside a word (`don't`) opens nothing.
      quote = char
      outside = brackets.length
    } else if ('([{'.includes(char)) {
      brackets += char
    } else if (brackets !== '' && brackets.endsWith(OPENER[char] ?? ' ') && brackets.length > (quote === '' ? 0 : outside)) {
      brackets = brackets.slice(0, -1)
    }
  }

  return { quote, brackets }
}

/**
 * Where a bare value ends: at the next whitespace, whatever it holds (`aB3&xY9;kL`, `aB3|xY9)kL`),
 * with the quoted parts glued to it (`abc"def"`, `$'abc'`). Inside a quote opened before the name
 * it ends at the quote that closes it (`echo "TOKEN=abc"`), unless a word is glued to that quote
 * (`"TOKEN=abc"def`); `endsAtClose` rules that out for a quoted name (`{"password":123,"user":1}`).
 */
function bareEnd(source: string, from: number, open: string, endsAtClose: boolean): number {
  let closer = open
  let at = from
  while (at < source.length) {
    const char = source.charAt(at)
    if (/\s/.test(char)) break
    if (char === closer && closer !== '') {
      const next = source.charAt(at + 1)
      // An escaped closer (`\"`) keeps its backslash.
      if (endsAtClose || next === '' || /[\s;&|),}\]:]/.test(next)) return at > from && source.charAt(at - 1) === '\\' ? at - 1 : at
      closer = ''
    } else if (closer === '' && (char === '"' || char === "'")) {
      const close = source.indexOf(char, at + 1)
      if (close < 0) return source.length
      at = close
    }
    at++
  }

  return at
}

/**
 * Where the quote `open` closes in the text: the first one outside a command substitution, whose
 * own quotes close nothing (`"Basic $(echo -n "me:x" | base64)"`). -1 when there is none, or when
 * a substitution is left open.
 */
function closeOf(text: string, open: string): number {
  let depth = 0
  for (let at = 0; at < text.length; at++) {
    const char = text.charAt(at)
    if (depth === 0) {
      if (char === open) return at
      if (char === '$' && text.charAt(at + 1) === '(') depth = 1
      if (depth === 1) at++
    } else if (char === '(') {
      depth++
    } else if (char === ')') {
      depth--
    } else if (char === '"' || char === "'") {
      at = text.indexOf(char, at + 1)
      if (at < 0) return -1
    }
  }

  return -1
}

/** Where a value ends that runs to the end of its line, or to the quote that closes `open` on it. */
function lineEnd(source: string, from: number, open: string): number {
  const newline = source.indexOf('\n', from)
  let end = newline < 0 ? source.length : newline
  const text = open === '' ? '' : source.slice(from, end)
  const close = text.includes('$(') ? closeOf(text, open) : text.indexOf(open === '' ? '\n' : open)
  if (close >= 0) end = from + close
  while (end > from && /\s/.test(source.charAt(end - 1))) end--
  if (close >= 0 && end > from && source.charAt(end - 1) === '\\') end--

  return end
}

/**
 * Where the brackets at the end of a bare value start that close ones opened before its name
 * (`(password=x)`, `[{"password": 1}]`), a few deep at most. A bracket the value opened itself is
 * part of it (`Summer(2024)`).
 */
function closersStart(value: string, brackets: string): number {
  const count = (text: string, char: string): number => text.split(char).length - 1

  for (let from = Math.max(tailStart(value, ')]}'), value.length - Math.min(brackets.length, 8)); from < value.length; from++) {
    const head = value.slice(0, from)
    const isOuter = [...value.slice(from)].every((char, at) => OPENER[char] === brackets.charAt(brackets.length - 1 - at))
    if (isOuter && ['()', '[]', '{}'].every(pair => count(head, pair.charAt(0)) <= count(head, pair.charAt(1)))) return from
  }

  return value.length
}

// A name at the end of a value, with or without what joins it to a value of its own.
const ENDS_AT_NAME = new RegExp(`(?:${SECRET_END})(?:\\\\?["'])?([:=]?)$`, 'i')
const NAME_JOINT = /[ \t]{0,8}(?::=|[:=])[ \t]{0,8}/y
const NAME_GAP = /[ \t]{1,8}/y

/**
 * The length of a value that ran up to another name: in `sed "s|password: .*|password: x|"` the
 * first value ends at the second `password:`, and in `s#token = .+#token = x#` at the second
 * `token`. What that name is joined to is masked with it, a few names deep at most.
 */
function pastNames(source: string, from: number, length: number, open: string, endsAtClose: boolean): number {
  let end = from + length
  for (let turn = 0; turn < 4; turn++) {
    const name = ENDS_AT_NAME.exec(source.slice(Math.max(from, end - 80), end))
    if (name === null) break
    const gap = name[1] === '' ? NAME_JOINT : NAME_GAP
    gap.lastIndex = end
    const start = end + (gap.exec(source)?.[0].length ?? 0)
    const next = start === end ? end : bareEnd(source, start, open, endsAtClose)
    if (next === start) break
    end = next
  }

  return end - from
}

/** The mask for the value that follows a name at `end`, and how much text it replaces. */
function maskedValue(source: string, key: RegExpExecArray): { mask: string; length: number } | undefined {
  const [, word = '', tail = '', quote = '', joint = ''] = key
  const sign = joint.trim()

  let start = key.index
  while (start > 0 && key.index - start < 64 && NAME_CHAR.test(source.charAt(start - 1))) start--
  const prefix = source.slice(start, key.index)
  const before = prefix.slice(-1)
  const isFlag = sign === ''
  const isRocket = sign === '=>'

  if (SHORT_END.test(word)) {
    if (isWordEnd(prefix, word)) return undefined
    // `pass: 59` is a test count; `SMTP_PASS: x`, `pass=x` and cmdkey's `/pass:x` are not.
    if (word.toLowerCase() === 'pass' && sign === ':' && joint !== ':' && prefix.replace(/^-+/, '') === '') return undefined
  }
  if (KEY_WORD.test(word)) {
    // `COSMOS_KEY=x`, `APP_KEY: x`: an environment name, joined by `=` or `:`; `sort_key: x` is not one.
    const name = `${prefix.slice(prefix.lastIndexOf('.') + 1)}${word}`
    if (!isKeyName(name) || (sign !== '=' && sign !== ':' && sign !== ':=') || (tail !== '' && !WHICH_TAIL.test(tail))) return undefined
  }
  if (tail !== '') {
    // `SECRET_KEY_BASE=x`, `DB_PASSWORD_PROD: x`, `process.env.SECRET_KEY_BASE = x`: the word is a
    // segment of an UPPER_SNAKE name, the last one of a dotted path. `secret_key_base: x`: or the
    // tail only says which credential it is.
    const name = prefix.slice(prefix.lastIndexOf('.') + 1)
    const isSegment = name === '' || name.endsWith('_')
    const isNamed =
      (UPPER_SNAKE.test(`${name}${word}${tail}`) && !NOT_SECRET_TAIL.test(tail)) || (!SHORT_END.test(word) && WHICH_TAIL.test(tail))
    if (!isSegment || isFlag || isRocket || !isNamed) return undefined
  }
  // A subscript is only assigned to: `os.environ["DB_PASSWORD"] = "x"`.
  if (quote.endsWith(']') && sign !== '=') return undefined
  if (isFlag && (quote !== '' || !prefix.startsWith('-'))) return undefined
  // A hash rocket joins only a quoted or `:`-led name to its value: `'password' => 'x'`, `:password => "x"`.
  if (isRocket && quote === '' && source.charAt(start - 1) !== ':') return undefined
  // `grep -rn password= src/`, `rg token= -g '*.py'`: a blank straight after the `=` leaves the
  // value empty, and the next word is not it.
  if (/^=[ \t]/.test(joint)) return undefined

  const from = key.index + key[0].length
  const open = openOn(source, start)
  VALUE.lastIndex = from
  const value = VALUE.exec(source)
  if (value === null) {
    // A value that starts with a separator, straight after the `=`: `DB_PASSWORD=&x`, `TOKEN=;x`. A
    // separator alone leaves the value empty (`TOKEN=; next`, `f(password=)`), and so does the `&`
    // that starts the next parameter of a query (`?token=&page=2`).
    if (joint !== '=' || /[?&]/.test(source.charAt(start - 1))) return undefined
    const end = bareEnd(source, from, open.quote, quote !== '')

    return /^[&;|)]*$/.test(source.slice(from, end)) ? undefined : { mask: MASK, length: end - from }
  }
  const [all, escaped, double, , found] = value

  if (found !== undefined) {
    // After a hash rocket only a quoted literal is a value: `token => getToken()` is code.
    if (isRocket) return undefined
    // Already masked (a line is masked again on its way to the screen): what follows the mask was
    // not part of the value the first time, and is not now (`TOKEN = ***; gh pr list`).
    if (found.startsWith(MASK) && /^(?:[\s,;)}\]&|"']|$)/.test(source.charAt(from + MASK.length))) return undefined
    const isTight = joint === '='
    const next = source.charAt(from + found.length)
    // An f-string or a template that interpolates is code: `f"Bearer {token}"`, `Bearer ${token}`
    // in backticks.
    if (/^(?:f|rf|fr)$/i.test(found) && (next === '"' || next === "'")) return undefined
    if (found.startsWith('`') && /^[^`]*\$\{/.test(source.slice(from + 1, from + 200))) return undefined
    // `PWD=/home/user` is the shell's working directory.
    if (word === 'PWD' && prefix === '' && /^[/~.]/.test(found)) return undefined
    // In `"password": 12345, ...` the comma or bracket closes the member, not the value.
    const headEnd = sign === ':' ? tailStart(found, ',}]') : found.length
    const isHeader = sign === ':' && word.toLowerCase() === 'authorization'
    // A header's value is a scheme and a credential, whatever the scheme (`ApiKey x`, `Bot x`, `OAuth
    // x`), to the end of the line or the quote the header stands in; prose after it (`required by the
    // API`) starts with a plain word that is no scheme.
    const lead = isHeader ? /^(\S+)[ \t]+\S/.exec(source.slice(from, lineEnd(source, from, open.quote)))?.[1] : undefined
    if (lead !== undefined) {
      if (NOT_A_VALUE.has(lead.toLowerCase()) && !SCHEME.test(lead)) return undefined
    } else if (headEnd === 0 || isNotValue(found.slice(0, headEnd), isFlag, isTight)) {
      return undefined
    }

    // A value joined by `:` to a name that starts its line or follows a blank is YAML or a properties
    // file, where it runs to the end of the line: `passphrase: correct horse battery staple`. So does
    // one whose name starts a quoted header, to the closing quote: `-H "X-Api-Key: abc def"`.
    const isQuotedHeader = quote === '' && open.quote !== '' && source.charAt(start - 1) === open.quote
    const isLine = sign === ':' && (start === 0 || /\s/.test(source.charAt(start - 1)) || isQuotedHeader)
    let length = (isHeader || isLine ? lineEnd(source, from, open.quote) : bareEnd(source, from + found.length, open.quote, quote !== '')) - from
    if (!isHeader && !isLine) length = pastNames(source, from, length, open.quote, quote !== '')
    // In a query string the next `&` starts another parameter: `?api-key=abc&x=1`.
    const amp = isTight && /[?&]/.test(source.charAt(start - 1)) ? source.slice(from, from + length).indexOf('&') : -1
    if (amp >= 0) length = amp
    const bare = source.slice(from, from + length)
    const closers = isHeader || isLine ? bare.length : closersStart(bare, open.brackets)
    const end = sign === ':' ? tailStart(bare.slice(0, closers), ',}]') : closers
    if (end === 0) return undefined

    return { mask: `${MASK}${bare.slice(end)}`, length }
  }
  // In `grep "password:" src/` or `rg 'token=' -g '*.py'` the quote after the name closes the one
  // opened before it; it opens no value. A word glued to that quote is one, up to the next quote of
  // its kind or the next blank: `'PGPASSWORD='hunter2'`, `echo "token="abc123`.
  if ((sign === ':' || sign === '=') && quote === '' && escaped === undefined && open.quote === all.charAt(0)) {
    const next = source.charAt(from + 1)
    if (next === '' || /[\s;&|),}\]]/.test(next)) return undefined
    const end = bareEnd(source, from + 1, open.quote, true)

    return end === from + 1 ? undefined : { mask: `${open.quote}${MASK}`, length: end - from }
  }
  // A quoted value with more glued to its closing quote is one word: `TOKEN="abc"def`.
  const after = source.charAt(from + all.length)
  const isGlued = after !== '' && after !== open.quote && !/[\s,;)}\]&|]/.test(after)
  const glued = isGlued ? bareEnd(source, from + all.length, open.quote, quote !== '') - from : all.length
  const length = isGlued ? pastNames(source, from, glued, open.quote, quote !== '') : glued
  if (sign !== ':' && !isRocket) return { mask: MASK, length }
  const mark = escaped !== undefined ? '\\"' : double !== undefined ? '"' : "'"

  return { mask: `${mark}${MASK}${mark}`, length }
}

const CONTINUED_PIECE = /[ \t]*\S*/y
const MARK_NL = /[\\`]\r?\n/y

/**
 * How far a masked value goes on: one that ends in a continuation mark is cut by it, and the lines
 * it continues on (any number, with an indent when the mark was all the value) are the rest of it.
 */
function continuedEnd(source: string, start: number, end: number): number {
  const isMarkOnly = /^[\\`]\r?$/.test(source.slice(start, end))
  let at = end
  while (at > 0) {
    MARK_NL.lastIndex = at - 1
    if (!MARK_NL.test(source)) break
    const mark = MARK_NL.lastIndex
    CONTINUED_PIECE.lastIndex = mark
    CONTINUED_PIECE.exec(source)
    const piece = source.slice(mark, CONTINUED_PIECE.lastIndex)
    if (piece.trim() === '' || (!isMarkOnly && /^[ \t]/.test(piece))) break
    at = CONTINUED_PIECE.lastIndex
  }

  return at - end
}

/** Masks the value of every named credential: `TOKEN=abc`, `"api_key": "abc"`, `password: abc`, `--secret abc`. */
function maskNamed(source: string): string {
  let masked = ''
  let from = 0

  KEY.lastIndex = 0
  for (let key = KEY.exec(source); key !== null; key = KEY.exec(source)) {
    const value = maskedValue(source, key)
    if (value === undefined) {
      // Not a credential: read on from just after the word, so nothing behind it is skipped.
      KEY.lastIndex = key.index + (key[1]?.length ?? 1)
      continue
    }
    const end = key.index + key[0].length
    const stop = end + value.length
    // A name inside the continued text a mask already took is read too: its value is masked as well.
    if (stop <= from) {
      KEY.lastIndex = stop
      continue
    }
    masked += source.slice(from, end) + value.mask
    from = stop + (value.mask === MASK ? continuedEnd(source, end, stop) : 0)
    // Read on from where the value stopped, not from the end of its continued pieces.
    KEY.lastIndex = stop
  }

  return masked + source.slice(from)
}

const shaped = (source: string, rules: readonly Rule[]): string =>
  // `replace` takes a string or a function; the union needs the cast.
  rules.reduce((masked, [pattern, replacement]) => masked.replace(pattern, replacement as string), source)

// A file whose text is itself the secret: dotenv files, keys, key stores and credential files.
const SECRET_FILE =
  /^(?:\.env.*|.*\.env|.*\.(?:pem|key|pfx|p12|jks|keystore)|id_(?:rsa|dsa|ecdsa|ed25519).*|[._]netrc|\.pgpass|\.pypirc|\.git-credentials|\.htpasswd|credentials(?:\.(?:json|ya?ml|toml|ini|txt|xml|csv))?|secrets?\.(?:ya?ml|json|toml|ini|txt))$/i

export const HIDDEN = '[content hidden]'

/**
 * Masks credentials in a string before it is shown or stored; text past REDACT_MAX is dropped.
 * Callers cut the result, never the input: a secret cut before it is masked may no longer look like one.
 */
export function redact(value: string): string {
  let source = value
  if (value.length > REDACT_MAX) {
    // Cut at a blank, however far back, so no credential is left half shown at the end: the word
    // the limit falls in is dropped whole.
    let end = REDACT_MAX
    while (end > 0 && !/\s/.test(value.charAt(end - 1))) end--
    source = `${value.slice(0, end)}…`
  }

  return shaped(maskNamed(maskSql(maskStores(maskCookies(shaped(source, SHAPES_BEFORE))))), SHAPES_AFTER)
}

/**
 * Whether a path names a file whose content is never shown: `.env*`, `*.env`, `*.pem`, `*.key`,
 * `*.pfx`, `*.p12`, `id_rsa*`, `.netrc`, `.pgpass`, `credentials*`, `secrets.yaml`.
 */
export function isSecretFile(path: string): boolean {
  return SECRET_FILE.test(fileName(path))
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
  return gradeCard(answer)
}

/** The longest answer read whole for its card. */
export const CARD_TAIL = 50_000

/**
 * The card of an answer of any length. The card closes the answer, so past `max` characters only
 * the tail is graded, from the start of a line, and as what it is in the whole answer: a fence or
 * an indented block the cut falls in is still open at the top of the tail, so a card quoted in it
 * is not taken for the report's own, and the report's own is not hidden behind its closing fence.
 */
export function parseAnswer(answer: string, max = CARD_TAIL): ParsedCard {
  if (answer.length <= max) return gradeCard(answer)
  const newline = answer.indexOf('\n', answer.length - max)
  const cut = newline < 0 ? answer.length : newline + 1
  const head = answer.slice(0, cut).replace(/\r?\n$/, '')

  return gradeCard(answer.slice(cut), blockState(head.split(/\r?\n/)))
}

// The weaker of two stated levels grades the card; a level that is unclear is weaker than `high`.
const CONFIDENCE_RANK: Record<Confidence, number> = { low: 0, medium: 1, missing: 2, high: 3 }

function gradeCard(answer: string, start?: BlockState): ParsedCard {
  const lines = answer.split(/\r?\n/)
  let scan = scanCards(lines, start)

  // A report quoted whole (every line behind `>`) is this report, not someone else's card. Of a
  // cut answer only the tail is known, so it is not read this way.
  if (start === undefined && scan.cards.length === 0 && lines.every(line => line.trim() === '' || QUOTED.test(line))) {
    scan = scanCards(lines.map(line => line.replace(QUOTED, '')))
  }
  let cards = scan.cards

  // A report fenced or indented whole has nothing but blank lines outside its blocks: the last card
  // in one is graded instead. With prose around it (`The expected format was:`) such a card is a sample.
  // Any line in a block below that one is an open item (`Caveat: e2e suite was not run`).
  let isBelowBlock = false
  if (cards.length === 0 && !scan.hasOutside) {
    for (let at = scan.blocks.length - 1; at >= 0; at--) {
      const inner = scanCards(scan.blocks[at] ?? []).cards
      if (inner.length === 0) continue
      cards = inner
      isBelowBlock = scan.blocks.slice(at + 1).some(block => block.some(line => line.trim() !== ''))
      break
    }
  }

  // Only the last card is graded: an earlier card or a stray header above it does not stand in
  // for this report's own.
  const graded = cards[cards.length - 1] ?? []
  const sections = new Map(graded.map(section => [section.name, section.lines]))

  // A RESULT, EVIDENCE or UNVERIFIED header with nothing under it is as good as absent: an empty
  // `UNVERIFIED:` does not say `none`. An empty CONFIDENCE is reported as not stated instead.
  const blank = new Set(graded.filter(section => section.isBlank && section.name !== 'CONFIDENCE').map(section => section.name))
  const missing = FIELDS.filter(field => !sections.has(field) || blank.has(field))
  // Every CONFIDENCE the card states is read, and the weakest level stands (the later one of equals).
  let confidence: Confidence = 'missing'
  let said = ''
  let isStated = false
  for (const section of graded) {
    if (section.name !== 'CONFIDENCE') continue
    const stated = section.lines.map(unbulleted)
    const level = confidenceOf(stated.join('\n').toLowerCase())
    if (isStated && CONFIDENCE_RANK[level] > CONFIDENCE_RANK[confidence]) continue
    confidence = level
    said = stated.join(' ').trim()
    isStated = true
  }

  // A section already known to say nothing is not read a second time.
  const open = graded.find(section => section.name === 'UNVERIFIED')
  const realItems = open === undefined || open.isAllNothing ? [] : open.lines.filter(line => !isNothing(line))
  const bullets = realItems.filter(line => BULLET.test(line)).length
  const isOpen = realItems.length > 0 || (open?.items ?? 0) > 0
  // Text that ends inside a fence holding a header cannot be told from a cut one: one open item.
  const unverified = Math.max(isOpen ? Math.max(1, bullets) : 0, scan.isCutOpen || isBelowBlock ? 1 : 0)

  // `exit 0`, `exit code: 1`, and JSON's `"exit_code": 2`. The gaps are bounded, so a long run of
  // spaces after `exit` costs nothing.
  const exitCodes = [...answer.matchAll(/\bexit(?:[\s_]{1,4}code)?"?\s{0,4}[:=]?\s{0,4}(-?\d+)\b/gi)].map(match =>
    Number(match[1]),
  )

  const result = (sections.get('RESULT') ?? [])
    .map(line => unbulleted(line).trim())
    .find(line => line !== '') ?? ''

  return {
    hasAnyField: graded.length > 0,
    confidence,
    // Masked whole before the cut, here and below, so a secret the cut would split is not half shown.
    confidenceText: redact(said).slice(0, 40),
    missing,
    unverified,
    exitCodes,
    isWeak: missing.length > 0 || confidence !== 'high' || unverified > 0,
    result: redact(result).slice(0, 120),
  }
}

// `low risk`, `low-level`: the word describes something other than the confidence. `low-ish` and
// `medium-high` are still levels.
const NOT_A_LEVEL =
  '(?!-(?!(?:ish|to|high|med|medium|low)\\b)\\w|\\s+(?:risk|level|impact|priority|chance|likelihood|probability|severity|stakes|cost|overhead))'
const LOW = new RegExp(`\\blow\\b${NOT_A_LEVEL}`)
const MEDIUM = new RegExp(`\\bmed(?:ium)?\\b${NOT_A_LEVEL}`)
// A later clause states a level only when it leads with one: `high, but low on Windows`. The later
// clauses are read as one text, a clause to a line, so a section of any length is read once.
const THEN = '^[^a-z\\n]*(?:(?:but|and|though|although|however|yet|only)\\s+)?'
const THEN_LOW = new RegExp(`${THEN}low\\b${NOT_A_LEVEL}`, 'm')
const THEN_MEDIUM = new RegExp(`${THEN}med(?:ium)?\\b${NOT_A_LEVEL}`, 'm')
const CLAUSE = /[(),;:\n]|\.\s|\s[-—–]\s/
// The whole first clause; in anything longer the word says how much of something else (`high risk
// of breaking Windows`, `highly unsure`, `high-level review only`) or is hedged (`high but shaky`).
const HIGH = /^(?:high|very high|very high confidence|highly confident|high confidence)$/
// What may follow a `high` without taking it back; the rest of the section is read closed, so any
// other clause leaves the level unclear (`high (ish)`, `high, I think`, `high; e2e suite fails`).
// A figure of 90% or more: `95%`, `0.95`, `9/10`, `95 percent`.
const FIGURE =
  /^(?:(\d{1,3}(?:\.\d{1,4})?)\s?(?:%|percent)|(\d{1,4})\s?\/\s?(\d{1,4})|(0\.\d{1,4}|1\.0{1,4}))(?: (?:confident|confidence|sure|certain))?$/
// A check that passed, said plainly: `tests pass`, `the build is green`, `ci passed`.
const PASSED =
  /^(?:the )?(?:(?:unit|e2e|integration|smoke|regression) )?(?:tests?|checks?|suites?|specs?|build|ci|lint|typecheck)(?: (?:all|now))? (?:pass(?:ed|es)?|succeeded|(?:is |are )?(?:passing|green))(?: (?:locally|here|cleanly|successfully|in ci))?$/
// More clauses than this after the level are not read: the level is unclear.
const BACKING_MAX = 8

/** Whether a clause after `high` only backs it: a figure of 90% or more, or checks that ran or passed. */
function backsHigh(clause: string): boolean {
  const said = clause.replace(EMPHASIS, '').trim().replace(/[.!]+$/, '')
  if (said === '' || ALL_RAN.test(said) || PASSED.test(said)) return true
  const figure = FIGURE.exec(said)
  if (figure === null) return false
  const [, percent, top, bottom, share] = figure
  const value = percent !== undefined ? Number(percent) / 100 : top !== undefined ? Number(top) / Number(bottom) : Number(share)

  return value >= 0.9 && value <= 1
}

/**
 * A clause without the emphasis around it (`_high_`, `**high**`, `high.`). Anything else in front
 * stays, so `~high` and `50% high` name no level; nor does a struck-through `~~high~~`, nor a level
 * with a mark only behind it (`high*` points at a footnote).
 */
function unmarked(clause: string): string {
  let from = 0
  while (from < clause.length && ' \t*_`'.includes(clause.charAt(from))) from++
  const end = Math.max(from, tailStart(clause, ' \t.!*_`'))
  const marks = (text: string): number => text.replace(/[^*_`]/g, '').length

  return marks(clause.slice(end)) > marks(clause.slice(0, from)) ? clause : clause.slice(from, end)
}

/**
 * The level a CONFIDENCE section claims, read from its first clause: any mention of low or medium
 * as a level there decides it, and `high` counts only when the clause is exactly `high`, `very
 * high`, `very high confidence`, `highly confident` or `high confidence`, so a hedge like `fairly high`, `not high`, `high
 * risk of ...`, `~high`, `50% high` or `highly unsure` names no level and reads as `missing`; so
 * does a high followed by anything, on its line or below it, that does not just back it (`high,
 * but shaky`, `high, assuming the migration is idempotent`, `high (60%)`, `high, in theory`): what
 * may follow is listed, not what may not. A later clause that leads with a level names it (`high
 * for the parser, low for the installer`).
 */
function confidenceOf(said: string): Confidence {
  const [first = '', ...later] = said
    .split(CLAUSE)
    .map(clause => clause.trim())
    .filter(clause => clause !== '')
  const rest = later.join('\n')
  if (LOW.test(first) || THEN_LOW.test(rest)) return 'low'
  if (MEDIUM.test(first) || THEN_MEDIUM.test(rest)) return 'medium'

  // A level that trails off (`high...`, `high…`) is not stated.
  if (!HIGH.test(unmarked(first)) || /\.{2,}|…/.test(said)) return 'missing'
  const isBacked =
    later.length <= BACKING_MAX && !rest.includes('?') && !CAVEAT.test(rest) && !NOT_YET.test(rest) && later.every(backsHigh)

  return isBacked ? 'high' : 'missing'
}

/** Why a card is weak, for its toast: never empty for a weak card. */
export function weakReason(card: ParsedCard): string {
  let confidence = ''
  if (card.confidence === 'low' || card.confidence === 'medium') {
    confidence = `confidence ${card.confidence}`
  } else if (card.confidence === 'missing' && !card.missing.includes('CONFIDENCE')) {
    confidence = card.confidenceText === '' ? 'confidence not stated' : `confidence unclear ("${card.confidenceText}")`
  }

  return [
    card.missing.length > 0 ? `missing ${card.missing.join('/')}` : '',
    confidence,
    card.unverified > 0 ? `${card.unverified} unverified` : '',
  ]
    .filter(Boolean)
    .join(', ')
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
      lastError = redact(text(line.error)).slice(0, 400)
    }
  }

  let spendToday = 0
  let deniesToday = 0
  let weakToday = 0

  for (const line of newestFirst) {
    const ts = text(line.ts)
    // A line without a stamp says nothing about the day: skip it, stop at the first older one.
    if (ts === '') continue
    if (!ts.startsWith(today)) break
    if (line.kind === 'usage' && typeof line.usd === 'number') spendToday += line.usd
    if (line.feature === 'risk_gate' && line.decision === 'deny') deniesToday += 1
    if (line.feature === 'report_check' && (line.decision === 'deny' || line.decision === 'weak')) weakToday += 1
  }

  const recent: JevEntry[] = []
  for (const line of newestFirst) {
    if (recent.length >= 8) break
    const entry = summarize(line)
    if (entry !== undefined) recent.push({ ...entry, summary: redact(entry.summary).slice(0, 400) })
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
  // A narrow pane can ask for no cells or fewer: draw nothing rather than throw.
  const width = Math.max(0, cells)
  const filled = Math.max(0, Math.min(width, Math.round(fraction * width)))

  return '█'.repeat(filled) + '░'.repeat(width - filled)
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

// Hunch: calls through its MCP server (`mcp__hunch__hunch_context`, or `mcp__plugin_hunch_hunch__hunch_context`
// when the Hunch plugin provides it) or its CLI through Bash.

const HUNCH_MCP = /^mcp__(?:plugin_hunch_)?hunch__(?:hunch_)?(.+)$/

const TASK_ID = /\bhtask_[a-f0-9]{24}\b/

const TARGET_KEYS = ['target', 'scope', 'topic', 'symbol', 'symptom_or_symbol', 'query', 'id', 'title'] as const

// The CLI as the prompt hook prints it (`node .../@davesheffer/hunch/dist/cli/index.js task verify ...`),
// or `hunch <sub>` / `npx @davesheffer/hunch <sub>` starting a command (matched with quoted text blanked
// and heredoc bodies dropped, so `git commit -m "hunch update"` is not a call). Windows shims count
// the same: `hunch.cmd`, `hunch.exe`, `hunch.ps1`, `npx.cmd hunch`, behind a path
// (`C:\Users\x\npm\hunch.cmd`, `.\node_modules\.bin\hunch.cmd`), quoted after PowerShell's call
// operator (`& "C:\...\hunch.ps1"`), after `cmd /c`, and after npx flags (`npx --yes hunch`).

// A heredoc opener (`<<EOF`, `<<-'EOF'`; not `<<<` or `1 << 2`), the rest of its line, then its body
// up to the terminator line, or to the end when none follows.
const HEREDOC = /(?<!<)<<-?[ \t]*(['"]?)([A-Za-z_]\w*)\1([^\n]*)(?:\n[\s\S]*?(?:\n[ \t]*\2[ \t]*(?=\r?\n|$)|$)|$)/g
const HUNCH_LAUNCHER = /@davesheffer[\\/]+hunch[\\/]+dist[\\/]+cli[\\/]+index\.js['"]?\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?/i

const SHIM = '(?:\\.cmd|\\.exe|\\.ps1)?'
// A path before the launcher, to its last `\` or `/`; and up to eight npx flags (`-y`, `--yes`,
// `--package=@davesheffer/hunch`). Each piece ends on a literal, so a long word is read once.
const SHIM_PATH = `(?:[^\\s;&|()'"]*[\\\\/])?`
const NPX_FLAGS = '(?:--?[\\w-]+(?:=\\S*)?\\s+){0,8}'
const HUNCH_BARE = new RegExp(
  `(?:^|[;&|\\n]|\\$\\()\\s*(?:\\(\\s*)?(?:cmd(?:\\.exe)?\\s+\\/[ck]\\s+)?${SHIM_PATH}(?:npx${SHIM}\\s+${NPX_FLAGS})?(?:@davesheffer\\/)?hunch${SHIM}\\s+([a-z][\\w-]*)(?:\\s+([a-z][\\w-]*))?`,
  'i',
)
// A quoted launcher (`"C:\Program Files\nodejs\hunch.ps1"`), read as its bare name.
const QUOTED_SHIM = /(?:^|[\\/])((?:hunch|npx)(?:\.cmd|\.exe|\.ps1)?)$/i

// Only the head of a command is searched for a hunch call: a generated script of megabytes is
// not parsed on every Bash call.
const HUNCH_SCAN_MAX = 20_000

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

/** An invariant severity worth a toast: `warning` or a blocking one; advisory and info stay in the pane. */
export function isToastSeverity(severity: string): boolean {
  return /^warn(ing)?$/i.test(severity) || BLOCKING.test(severity)
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
export function hunchCli(full: string): HunchInvocation | undefined {
  // Runs of blanks become one space and runs of line breaks one `\n` first: the patterns below
  // retry `\s*` from every break, which is quadratic on a long run of them.
  const command = full
    .slice(0, HUNCH_SCAN_MAX)
    .replace(/[^\S\n]+/g, ' ')
    .replace(/(?: ?\n)+ ?/g, '\n')
  const lines = command.replace(HEREDOC, (_all, _quote: string, tag: string, rest: string) => `<<${tag}${rest}`)
  const match =
    HUNCH_LAUNCHER.exec(lines) ??
    HUNCH_BARE.exec(lines.replace(/"[^"]*"|'[^']*'/g, quoted => QUOTED_SHIM.exec(quoted.slice(1, -1))?.[1] ?? '""'))
  if (match === null) return undefined

  const [, sub = '', action = ''] = match
  const taskId = TASK_ID.exec(command)?.[0]

  if (sub === 'task' && action === 'verify') {
    const separator = command.indexOf(' -- ')
    const checked = separator >= 0 ? command.slice(separator + 4) : ''

    // Each line up to its first `2>&1`, found by search: nothing here retries.
    const shown = checked.split('\n').map(line => {
      const merged = line.indexOf('2>&1')

      return merged < 0 ? line : line.slice(0, merged)
    })

    return { name: 'verify', target: oneLine(shown.join('\n')), taskId }
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
      // Each gap stays on its line, so a long run of blank lines or an unclosed `[` is read once.
      const constraints = [
        ...output.matchAll(/^[ \t]*[•*-][ \t]*(con_\w+)[ \t]*\[([\w-]+)(?![\w-])[^\]\n]*\][ \t]*(.*)$/gm),
      ].map(match => ({
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
      // The scope stays on its line and is bounded, so a long run of `(scope: ` costs its length.
      const scope = /\(scope:[ \t]*([^\n]{1,300}?)\)[ \t\r]{0,20}$/m.exec(output)?.[1]

      return {
        summary: `verdict ${verdict ?? '?'}${scope !== undefined ? ` · ${oneLine(scope)}` : ''}`,
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
      const headline = /(?<!\d)(\d{1,9})\s{1,10}decisions?\s{1,10}needs?\b/i.exec(output)?.[1]
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
  // On an errored call core's `Exit code N` beats a 0 that may be the checked command's own JSON
  // (the launcher's cut off); otherwise an `Exit code` line is the command's stdout, not evidence.
  const code = isError && bashCode !== undefined && bashCode !== '0' ? bashCode : (jsonCode ?? bashCode)
  const isTimedOut = /"timed_out"\s*:\s*true/.test(output)

  if (code === undefined) {
    if (isTimedOut && isError) return { summary: 'timed out', level: 'alert', constraints: [], taskId }

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

/** A call still running this long never got its result recorded (a lost abort, a reload); past Bash's 10 min cap. */
export const STALE_MS = 15 * 60_000

/** Marks running calls older than STALE_MS as errored, so the pane and band stop waiting on them. */
export function reapStale(calls: readonly HunchCall[], now: number): HunchCall[] {
  return calls.map(call =>
    call.status === 'running' && now - call.startedAt > STALE_MS
      ? { ...call, status: 'error', level: 'warn', durationMs: now - call.startedAt, summary: 'no result recorded' }
      : call,
  )
}

/** A `hunch_context` brief, through MCP or the CLI. */
export function isContextCall(name: string): boolean {
  return name === 'context' || name === 'cli context'
}

/**
 * Whether a task had its brief. `contextTasks` is kept per task, not read from `calls`: that ring drops
 * old calls and `clear done` empties it. State saved before `contextTasks` existed falls back to scanning
 * `calls`. An id-less brief ('') never vouches for a task with a real id.
 */
export function hadBrief(log: Pick<HunchLog, 'calls' | 'contextTasks'>, taskId: string | undefined): boolean {
  const briefed = log.contextTasks
  if (briefed === undefined) {
    return log.calls.some(
      one => isContextCall(one.name) && (taskId === undefined || one.taskId === undefined || one.taskId === taskId),
    )
  }

  return taskId === undefined ? briefed.length > 0 : briefed.includes(taskId)
}

/** Whether an abort ended the call: Bash's own `interrupted`, or the marker text core gives the model. */
export function wasInterrupted(result: unknown, text: string): boolean {
  if (typeof result === 'object' && result !== null && (result as { interrupted?: unknown }).interrupted === true) return true

  return /^\[?Request interrupted by user|^Interrupted by user/i.test(text.trim())
}

function oneLine(value: string): string {
  // Masked whole before the cut, so a secret the cut would split is not left half shown.
  return redact(value.replace(/\s+/g, ' ').trim()).slice(0, 120)
}

/** The first line that says something: no rule, bare heading mark or footer. */
function firstLine(output: string): string {
  // Only the head is read, and a tag holds no `<`, so a long run of `<` is passed over once.
  for (const raw of output.slice(0, REDACT_MAX).split(/\r?\n/)) {
    const line = raw
      .replace(/<[^<>]+>/g, '')
      .replace(/^[\s#>*_`•-]+/, '')
      .replace(/\*\*/g, '')
      .trim()
    if (line !== '' && !/^[-=_─]{3,}$/.test(line) && line !== '{') return oneLine(line)
  }

  return ''
}

// Sessions: the registry Claude Code keeps at `~/.claude/sessions/<pid>.json`, one file per live process.

const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** The last path segment of a Windows or POSIX path. */
export function repoName(cwd: string): string {
  return cwd.replace(/[\\/]+$/, '').split(/[\\/]/).at(-1) ?? ''
}

/** One registry file as a row; undefined for anything that is not a session entry. */
export function sessionRow(raw: string): SessionRow | undefined {
  let entry: unknown
  try {
    entry = JSON.parse(raw)
  } catch {
    return undefined
  }
  if (typeof entry !== 'object' || entry === null) return undefined

  const { pid, sessionId, cwd, name, entrypoint, status, updatedAt, startedAt } = entry as Record<string, unknown>
  if (typeof pid !== 'number' || typeof sessionId !== 'string' || !SESSION_ID.test(sessionId)) return undefined

  const folder = typeof cwd === 'string' ? cwd : ''
  const id = sessionId.toLowerCase()

  return {
    pid,
    sessionId: id,
    name: typeof name === 'string' && name.trim() !== '' ? oneLine(name) : repoName(folder) || id.slice(0, 8),
    cwd: folder,
    entrypoint: typeof entrypoint === 'string' ? entrypoint : 'unknown',
    status: status === 'idle' || status === 'busy' ? status : 'unknown',
    updatedAt: typeof updatedAt === 'number' ? updatedAt : typeof startedAt === 'number' ? startedAt : 0,
  }
}

/** Newest first, one row per session: a crashed process can leave an older file for the same id. */
export function sessionRows(rows: readonly SessionRow[]): SessionRow[] {
  const newest = new Map<string, SessionRow>()
  for (const row of rows) {
    const seen = newest.get(row.sessionId)
    if (seen === undefined || row.updatedAt > seen.updatedAt) newest.set(row.sessionId, row)
  }

  return [...newest.values()].sort((a, b) => b.updatedAt - a.updatedAt || a.name.localeCompare(b.name))
}

/** `45s`, `12m`, `3h`, `2d`: how long ago, coarsely. */
export function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 90) return `${s}s`
  if (s < 90 * 60) return `${Math.round(s / 60)}m`
  if (s < 36 * 3600) return `${Math.round(s / 3600)}h`

  return `${Math.round(s / 86400)}d`
}

// Activity: one short plain-language line per thing the session does.

const HUNCH_VERB: Record<string, string> = {
  context: 'brief on',
  check_constraints: 'check rules for',
  why: 'why',
  bug_lineage: 'past bugs for',
  blast_radius: 'blast radius of',
  get_dependents: 'dependents of',
  current_decision: 'current decision on',
  record_decision: 'record decision',
  record_correction: 'record correction',
  record_finding: 'record finding',
  capture_decision: 'capture decision',
  merge_verdict: 'merge verdict for',
  pr_impact: 'PR impact of',
  conformance: 'conformance check',
  escalations: 'open escalations',
  structure: 'repo map',
  query: 'search',
  verify: 'check',
}

/** The last segment of a path, for a short label. */
export function fileName(path: string): string {
  return path.split(/[\\/]+/).filter(Boolean).at(-1) ?? path
}

export function clip(value: string, max: number): string {
  const line = value.replace(/\s+/g, ' ').trim()

  return line.length > max ? `${line.slice(0, max - 1)}…` : line
}

/** `clip` for text that can carry a credential (a command, a prompt): masked whole first, then cut. */
export function label(value: string, max: number): string {
  // Masked as written first: a value on the line below its name (`- name: DB_PASSWORD`, `value: x`)
  // is one only while the lines stand apart. Then again as the one line it is shown as.
  return clip(redact(redact(value).replace(/\s+/g, ' ').trim()), max)
}

/** `hh:mm:ss`, local time. */
export function clockSeconds(ms: number): string {
  const date = new Date(ms)

  return `${clockTime(ms)}:${String(date.getSeconds()).padStart(2, '0')}`
}

const DETAIL_MAX = 4000

/** The full text behind a timeline line (a command, a prompt, an edit), for its drawer; '' when the line says it all. */
export function toolDetail(tool: string, args: Record<string, unknown>): string {
  let detail: string
  switch (tool) {
    case 'Bash':
    case 'PowerShell':
      detail = text(args.command)
      break
    case 'Agent':
      detail = text(args.prompt)
      break
    case 'Edit':
      detail = isSecretFile(text(args.file_path))
        ? `${text(args.file_path)}\n${HIDDEN}`
        : `${text(args.file_path)}\n--- old\n${text(args.old_string)}\n+++ new\n${text(args.new_string)}`
      break
    case 'Write':
      detail = `${text(args.file_path)}\n${isSecretFile(text(args.file_path)) ? HIDDEN : text(args.content)}`
      break
    case 'Read':
    case 'Glob':
    case 'Grep':
    case 'ToolSearch':
    case 'Skill':
      detail = ''
      break
    default: {
      const reserved = ['tool', 'tool_use_id', 'agentId', 'consent']
      const rest = Object.fromEntries(Object.entries(args).filter(([key]) => !reserved.includes(key)))
      detail = tool.startsWith('mcp__') && Object.keys(rest).length > 0 ? JSON.stringify(redactLeaves(rest, 0), null, 2) : ''
    }
  }

  return capDetail(detail)
}

/**
 * The arguments with every string redacted as written. Serialised, a continued line is the two
 * characters `\` `n` and no longer one, so the text is masked before that, and again after it.
 */
function redactLeaves(value: unknown, depth: number, budget = { left: REDACT_MAX }): unknown {
  // Past the depth limit or the budget (what `capDetail` can show anyway) a leaf is never handed on raw.
  budget.left -= 1 + (typeof value === 'string' ? value.length : 0)
  if (budget.left < 0 || (depth > 20 && typeof value === 'object' && value !== null)) return '[…]'
  if (typeof value === 'string') return redact(value)
  if (typeof value !== 'object' || value === null) return value
  if (Array.isArray(value)) return value.map(item => redactLeaves(item, depth + 1, budget))

  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactLeaves(item, depth + 1, budget)]))
}

/** Masks secrets in drawer text and cuts it to DETAIL_MAX characters, saying how much was left out. */
export function capDetail(detail: string): string {
  // Masked whole before the cut: a secret the cut would split (a long URL password, say) is matched
  // in full, and what is cut is already masked.
  const shown = redact(detail).slice(0, DETAIL_MAX)

  return detail.length > DETAIL_MAX ? `${shown}\n… (${detail.length - DETAIL_MAX} more characters)` : shown
}

/** What a tool call does, in a few plain words: `read parse.ts`, `start critic (fable): Review diff`. */
export function describeTool(tool: string, args: Record<string, unknown>): string {
  // `redact` bounds what is masked: a file name or a tool name can be as long as the caller likes.
  return label(describe(tool, args), 80)
}

function describe(tool: string, args: Record<string, unknown>): string {
  const file = fileName(text(args.file_path) || text(args.notebook_path) || text(args.path))
  const hunch = hunchName(tool)
  if (hunch !== undefined) {
    const { target } = hunchInvocation(hunch, args)
    const verb = HUNCH_VERB[hunch] ?? hunch.replace(/_/g, ' ')

    return label(`Hunch: ${verb}${target === '' ? '' : ` ${target}`}`, 80)
  }

  switch (tool) {
    case 'Bash':
    case 'PowerShell': {
      const command = text(args.command)
      const cli = hunchCli(command)
      const what = text(args.description) || command
      if (cli?.name === 'verify') return label(`check: ${what}`, 80)

      return label(`run: ${what}`, 80)
    }
    case 'Read':
      return `read ${file}`
    case 'Edit':
    case 'NotebookEdit':
      return `edit ${file}`
    case 'Write':
      return `write ${file}`
    case 'Grep':
      return label(`search for "${text(args.pattern)}"${file === '' ? '' : ` in ${file}`}`, 80)
    case 'Glob':
      return label(`find files ${text(args.pattern)}`, 80)
    case 'Agent': {
      const role = roleOf(text(args.subagent_type) || 'general-purpose')
      const model = text(args.model)

      return label(`start ${role}${model === '' ? '' : ` (${model})`}: ${text(args.description)}`, 80)
    }
    case 'SendMessage':
      return 'message an agent'
    case 'Skill':
      return `load skill ${text(args.skill)}`
    case 'ToolSearch':
      return 'load tool definitions'
    case 'WebSearch':
      return label(`web search "${text(args.query)}"`, 80)
    case 'WebFetch':
      return label(`fetch ${redact(text(args.url)).replace(/^https?:\/\//, '').split('/')[0] ?? ''}`, 80)
    case 'AskUserQuestion':
      return 'ask you a question'
    case 'Artifact':
      return `artifact ${text(args.action) || 'publish'}`
  }

  const mcp = /^mcp__(.+?)__(.+)$/.exec(tool)
  if (mcp !== null) return label(`${mcp[1]?.replace(/^claude_ai_/, '')}: ${mcp[2]?.replace(/_/g, ' ')}`, 80)

  return tool
}

import { describe, expect, mock, test } from 'claude-code/testing'

import {
  cardReminder,
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
} from '../hooks/policy'

describe('routing', () => {
  test('named roles run on their own models', async () => {
    expect(routeVerdict({ subagent_type: 'scout' }, false)).toBe(undefined)
    expect(routeVerdict({ subagent_type: 'critic', model: 'fable' }, false)).toBe(undefined)
    expect(routeVerdict({ subagent_type: 'orchestrator:runner', model: 'sonnet' }, false)).toBe(undefined)
    expect(routeVerdict({ subagent_type: 'critic', model: 'sonnet' }, false)).toContain("model: 'fable'")
    expect(routeVerdict({ subagent_type: 'builder', model: 'opus' }, false)).toContain("model: 'sonnet'")
  })

  test('built-in agents must not inherit the main model', async () => {
    expect(routeVerdict({}, false)).toContain('inherit')
    expect(routeVerdict({ subagent_type: 'Explore' }, false)).toContain('inherit')
    expect(routeVerdict({ subagent_type: 'Explore', model: 'sonnet' }, false)).toBe(undefined)
    expect(routeVerdict({ subagent_type: 'statusline-setup' }, false)).toBe(undefined)
  })

  test('a built-in agent on fable reviews as the critic', async () => {
    expect(roleFor('general-purpose', 'fable')).toBe('critic')
    expect(roleFor('general-purpose', 'claude-fable-5-1')).toBe('critic')
    expect(roleFor('general-purpose', 'sonnet')).toBe(undefined)
    expect(roleFor('scout', 'fable')).toBe('scout')
  })

  test('subagents do not delegate', async () => {
    expect(routeVerdict({ subagent_type: 'scout' }, true)).toContain('recursive delegation')
  })
})

describe('main-session-only actions', () => {
  test('push, publish, deploy, delete, send and PR polling are named', async () => {
    expect(mainOnlyLabel('Bash', 'git push -u origin main')).toBe('git push')
    expect(mainOnlyLabel('Bash', 'gh pr create --fill')).toBe('a GitHub write')
    expect(mainOnlyLabel('Bash', 'npm publish')).toBe('a publish')
    expect(mainOnlyLabel('Bash', 'rm -rf build')).toBe('a destructive delete')
    expect(mainOnlyLabel('Bash', 'git reset --hard HEAD~1')).toBe('a destructive delete')
    expect(mainOnlyLabel('Bash', 'curl -X POST https://example.com')).toBe('a send')
    expect(mainOnlyLabel('Bash', '~/.claude/bin/pr-status 42')).toBe('networked PR/CI polling')
    expect(mainOnlyLabel('mcp__github__create_pull_request', undefined)).toBe('an outward MCP write')
  })

  test('ordinary work is not', async () => {
    expect(mainOnlyLabel('Bash', 'git status && git diff')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'rm build/out.txt')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'python -m unittest discover -s tests')).toBe(undefined)
    expect(mainOnlyLabel('Read', undefined)).toBe(undefined)
    expect(mainOnlyLabel('mcp__github__pull_request_read', undefined)).toBe(undefined)
  })
})

describe('classification', () => {
  test('checks, outward calls, docs and risky paths', async () => {
    expect(isCheck('python -m unittest discover -s tests -v')).toBe(true)
    expect(isCheck('claude plugin test mods/orch-guard')).toBe(true)
    expect(isCheck('node hunch/dist/cli/index.js task verify htask_1 -- pytest')).toBe(true)
    expect(isCheck('git status')).toBe(false)
    expect(isOutward('Bash', 'git push -u origin feature')).toBe(true)
    expect(isOutward('Bash', 'git pull origin main')).toBe(false)
    expect(isOutward('mcp__github__create_pull_request', undefined)).toBe(true)
    expect(needsCheck('README.md')).toBe(false)
    expect(needsCheck('relay/relay.py')).toBe(true)
    expect(isRisky('claude/install.py', '')).toBe(true)
    expect(isRisky('src/util/format.ts', '')).toBe(false)
    expect(isRisky('src/billing.ts', '')).toBe(true)
    expect(isRisky('relay/relay.py', '^relay/')).toBe(true)
    expect(isRisky('relay/relay.py', '([')).toBe(false)
  })

  test('the ship boundary names what is missing', async () => {
    expect(outwardBlockers(EMPTY_LEDGER)).toEqual([])
    const blockers = outwardBlockers({ ...EMPTY_LEDGER, uncheckedEdits: ['a.py'], riskyPending: ['install.py'] })
    expect(blockers.length).toBe(2)
    expect(blockers[0]).toContain('no passing check')
    expect(blockers[1]).toContain('no critic')
  })
})

describe('report cards', () => {
  test('a strong card needs no reminder; a weak one escalates', async () => {
    const strong = parseCard('RESULT: found it\nEVIDENCE: a.py:3\nCONFIDENCE: high\nUNVERIFIED: none')
    expect(strong.isWeak).toBe(false)
    expect(cardReminder('scout', strong)).toBe(undefined)

    const weak = parseCard('RESULT: probably\nEVIDENCE: a.py\nCONFIDENCE: medium\nUNVERIFIED:\n- Windows path')
    expect(weak.unverified).toBe(1)
    expect(cardReminder('scout', weak)).toContain('escalate to builder or the main session')
    expect(cardReminder('runner', parseCard('All tests passed.'))).toContain('no RESULT')
  })

  test('critic verdicts and builder diffs', async () => {
    const fix = parseCard('RESULT: FIX FIRST — race in relay\nEVIDENCE: x\nCONFIDENCE: high\nUNVERIFIED: none')
    expect(fix.verdict).toBe('FIX FIRST')
    expect(cardReminder('critic', fix)).toContain('resolve each finding')
    expect(parseCard('RESULT: SHIP — clean').verdict).toBe('SHIP')
    expect(cardReminder('builder', parseCard('RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED: none'))).toContain('read the builder diff')
  })
})

/** A refused call reads `{ deny }` here, or an errored result where a settings hook stands beneath. */
const refusal = (answer: { deny?: string; isError?: true; text?: string }) => answer.deny ?? (answer.isError === true ? answer.text : undefined)

const typed = (args: string) =>
  ({ command: 'orch-guard', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } }) as const

describe('in the engine', () => {
  test('an Explore agent with no model is refused', async ($, on) => {
    mock.clock(on)
    let ran = false
    on('tool.call', { tool: 'Agent' }, () => {
      ran = true
      return { result: 'ok' } as never
    })
    const answer = await $.tool.call({ tool: 'Agent', description: 'find x', prompt: 'find x', subagent_type: 'Explore' })
    expect(ran).toBe(false)
    expect(refusal(answer)).toContain('inherit')
  })

  test('a push after an unchecked edit is refused, and allowed once a check passes', async ($, on) => {
    mock.clock(on)
    const pushes: string[] = []
    on('tool.call', ($, e) => {
      if (e.tool === 'Bash' && /git push/.test(e.command)) pushes.push(e.command)
      return { result: 'ok', text: 'ok' } as never
    })

    await $.tool.call({ tool: 'Edit', file_path: '/repo/src/format.ts', old_string: 'a', new_string: 'b' })
    const refused = await $.tool.call({ tool: 'Bash', command: 'git push -u origin feature' })
    expect(refusal(refused)).toContain('no passing check')
    expect(pushes).toEqual([])

    await $.tool.call({ tool: 'Bash', command: 'npm test' })
    await $.tool.call({ tool: 'Bash', command: 'git push -u origin feature' })
    expect(pushes).toEqual(['git push -u origin feature'])
  })

  test('a risky edit needs a critic, and /orch-guard waive lets the next push through', async ($, on) => {
    mock.clock(on)
    const pushes: string[] = []
    on('tool.call', ($, e) => {
      if (e.tool === 'Bash' && /git push/.test(e.command)) pushes.push(e.command)
      return { result: 'ok', text: 'ok' } as never
    })

    await $.tool.call({ tool: 'Edit', file_path: '/repo/claude/install.py', old_string: 'a', new_string: 'b' })
    await $.tool.call({ tool: 'Bash', command: 'python -m unittest discover -s tests' })
    const refused = await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(refusal(refused)).toContain('critic')

    const status = await $.command.run(typed(''))
    expect(JSON.stringify(status)).toContain('push/publish blocked')

    const fromPlugin = await $.command.run({ ...typed('waive model says so'), origin: { kind: 'plugin', name: 'x' } } as never)
    expect(JSON.stringify(fromPlugin)).toContain('only the person')
    const stillRefused = await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(refusal(stillRefused)).toContain('critic')

    await $.command.run(typed('waive hotfix approved by the user'))
    await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(pushes).toEqual(['git push'])
  })

  test('warn mode lets the call run', { options: { mode: 'warn' } }, async ($, on) => {
    mock.clock(on)
    let ran = false
    on('tool.call', { tool: 'Agent' }, () => {
      ran = true
      return { result: 'ok', text: 'ok' } as never
    })
    await $.tool.call({ tool: 'Agent', description: 'find x', prompt: 'find x' })
    expect(ran).toBe(true)
  })
})

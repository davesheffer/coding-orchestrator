import { describe, expect, mock, test } from 'claude-code/testing'

import {
  cardReminder,
  EMPTY_LEDGER,
  isCheck,
  isOutward,
  isRisky,
  mainOnlyLabel,
  mayWrite,
  needsCheck,
  outwardBlockers,
  parseCard,
  roleFor,
  routeVerdict,
  settle,
  touch,
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
    expect(routeVerdict({ subagent_type: 'code-reviewer' }, false)).toBe(undefined)
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
    expect(mainOnlyLabel('Bash', 'curl -X POST https://example.com')).toBe('a send')
    expect(mainOnlyLabel('Bash', '~/.claude/bin/pr-status 42')).toBe('networked PR/CI polling')
    expect(mainOnlyLabel('mcp__github__create_pull_request', undefined)).toBe('an outward MCP write')
  })

  test('quoted text, searches and commit messages are not commands', async () => {
    expect(mainOnlyLabel('Bash', 'grep -rn "git push" relay/')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'rg "gh pr create" mods/')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'cat docs/pr-status.md')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'echo "do not run gh api here"')).toBe(undefined)
    expect(isOutward('Bash', 'git commit -m "refuse git push until checks pass"')).toBe(false)
    expect(isOutward('Bash', 'git log --grep "git push"')).toBe(false)
    expect(isOutward('Bash', "git commit -F - <<'EOF'\nfix: git push gate\nEOF")).toBe(false)
    expect(isOutward('Bash', 'git push --dry-run origin main')).toBe(false)
  })

  test('option-led spellings still count', async () => {
    expect(mainOnlyLabel('Bash', 'git -C /home/user/repo push origin main')).toBe('git push')
    expect(mainOnlyLabel('Bash', 'cd repo && GIT_TRACE=1 git -c core.x=y push')).toBe('git push')
    expect(mainOnlyLabel('Bash', 'gh pr --repo o/r create --fill')).toBe('a GitHub write')
    expect(mainOnlyLabel('Bash', 'curl --request POST https://x')).toBe('a send')
    expect(mainOnlyLabel('Bash', 'curl -d @body.json https://x')).toBe('a send')
    expect(isOutward('Bash', 'git -C /x push')).toBe(true)
    expect(isOutward('Bash', 'npm test && git push -u origin b')).toBe(true)
  })

  test('ordinary work is not', async () => {
    expect(mainOnlyLabel('Bash', 'rm -rf dist && npm run build')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'rm -rf node_modules/.cache')).toBe(undefined)
    expect(mainOnlyLabel('Bash', 'rm -rf /')).toBe('a destructive delete')
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
    expect(isCheck('echo "npm test passed"')).toBe(false)
    expect(isCheck('true # npm test')).toBe(false)
    expect(isCheck('ls jest.config.js')).toBe(false)
    expect(isCheck('git grep pytest')).toBe(false)
    expect(isCheck('make clean')).toBe(false)
    expect(isCheck('npm test --help')).toBe(false)
    expect(isCheck('npm test 2>&1 | tail -20')).toBe(false)
    expect(isCheck('pytest || true')).toBe(false)
    expect(isCheck('set -o pipefail; npm test | tail -5')).toBe(true)
    expect(isCheck('cd mods && npm test')).toBe(true)
    expect(isCheck('node --test')).toBe(true)
    expect(isCheck('just test', 'just\\s+test')).toBe(true)
    expect(mayWrite("sed -i 's/a/b/' src/auth.ts")).toBe(true)
    expect(mayWrite('cat > src/x.py <<EOF\nprint(1)\nEOF')).toBe(true)
    expect(mayWrite('git apply fix.diff')).toBe(true)
    expect(mayWrite('npm test 2>/dev/null')).toBe(false)
    expect(mayWrite('git diff > /dev/null')).toBe(false)
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
    expect(isRisky('docs/deployment.md', '')).toBe(false)
    expect(isRisky('README-security.md', '')).toBe(false)
    expect(isRisky('src/auth-ui-labels.json', '')).toBe(true)
    expect(needsCheck('LICENSE')).toBe(false)
    expect(needsCheck('.gitignore')).toBe(false)
  })

  test('the ship boundary names what is missing', async () => {
    expect(outwardBlockers(EMPTY_LEDGER)).toEqual([])
    const blockers = outwardBlockers({ ...EMPTY_LEDGER, uncheckedEdits: [{ path: 'a.py', at: 1 }], riskyPending: [{ path: 'install.py', at: 1 }] })
    expect(blockers.length).toBe(2)
    expect(blockers[0]).toContain('no passing check')
    expect(blockers[1]).toContain('no critic')
  })

  test('a check or review covers only what came before it', async () => {
    const entries = [{ path: 'a', at: 1 }, { path: 'b', at: 5 }]
    expect(settle(entries, 3)).toEqual([{ path: 'b', at: 5 }])
    expect(settle(entries, 9, ['b'])).toEqual([{ path: 'a', at: 1 }])
    expect(touch(entries, 'a', 7)).toEqual([{ path: 'b', at: 5 }, { path: 'a', at: 7 }])
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

  test('a body line that starts like a field does not reopen it', async () => {
    const card = parseCard('RESULT: SHIP\nEVIDENCE:\nResult of pytest: exit 0\nCONFIDENCE: high\nUNVERIFIED: none')
    expect(card.verdict).toBe('SHIP')
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

    const resetFromPlugin = await $.command.run({ ...typed('reset'), origin: { kind: 'plugin', name: 'x' } } as never)
    expect(JSON.stringify(resetFromPlugin)).toContain('only the person')
    const fromPlugin = await $.command.run({ ...typed('waive model says so'), origin: { kind: 'plugin', name: 'x' } } as never)
    expect(JSON.stringify(fromPlugin)).toContain('only the person')
    const stillRefused = await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(refusal(stillRefused)).toContain('critic')

    await $.command.run(typed('waive hotfix approved by the user'))
    await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(pushes).toEqual(['git push'])
  })

  test('a check that started before an edit does not cover it', async ($, on) => {
    const clock = mock.clock(on)
    let release = () => {}
    const held = new Promise<void>(resolve => {
      release = resolve
    })
    on('tool.call', async ($, e) => {
      if (e.tool === 'Bash' && e.command === 'npm test') await held
      return { result: 'ok', text: 'ok' } as never
    })
    const check = $.tool.call({ tool: 'Bash', command: 'npm test' })
    await clock.advance(1000)
    // An edit lands while the check runs.
    await $.tool.call({ tool: 'Edit', file_path: '/repo/src/x.ts', old_string: 'a', new_string: 'b' })
    release()
    await check
    const refused = await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(refusal(refused)).toContain('x.ts')
  })

  test('a critic SHIP clears only what was pending when it started', async ($, on) => {
    const clock = mock.clock(on)
    on('tool.call', () => ({ result: 'ok', text: 'ok' }) as never)
    on('agent.spawn', () => ({ model: 'claude-fable-5-1', agentId: 'crit1' }) as never)
    on('turn.complete', () => ({ text: '' }) as never)

    await $.tool.call({ tool: 'Edit', file_path: '/repo/src/auth.ts', old_string: 'a', new_string: 'b' })
    await clock.advance(10)
    await $.agent.spawn({ subagentType: 'general-purpose', model: 'fable', background: true, prompt: 'review', description: 'review' } as never)
    await clock.advance(10)
    await $.tool.call({ tool: 'Edit', file_path: '/repo/src/billing.ts', old_string: 'a', new_string: 'b' })
    await clock.advance(10)
    await $.tool.call({ tool: 'Bash', command: 'npm test' })
    await $.turn.complete({ agentId: 'crit1', reason: 'answer', answer: 'RESULT: SHIP\nEVIDENCE: read\nCONFIDENCE: high\nUNVERIFIED: none', durationMs: 1 } as never)

    const refused = await $.tool.call({ tool: 'Bash', command: 'git push' })
    expect(refusal(refused)).toContain('billing.ts')
    expect(refusal(refused)).not.toContain('auth.ts')
  })

  test('a foreground role report gets a reminder; a background launch does not', async ($, on) => {
    mock.clock(on)
    let status = 'completed'
    on('tool.call', { tool: 'Agent' }, () => ({ result: { status }, text: 'RESULT: maybe\nCONFIDENCE: low' }) as never)

    const weak = await $.tool.call({ tool: 'Agent', description: 'd', prompt: 'p', subagent_type: 'scout', run_in_background: false })
    expect(JSON.stringify(weak.context ?? [])).toContain('escalate to builder')

    status = 'async_launched'
    const launched = await $.tool.call({ tool: 'Agent', description: 'd', prompt: 'p', subagent_type: 'scout', run_in_background: false })
    expect(launched.context ?? []).toEqual([])
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

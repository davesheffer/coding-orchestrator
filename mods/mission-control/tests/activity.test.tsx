import { describe, expect, mock, test } from 'claude-code/testing'

import { describeTool, fileName, toolDetail } from '../hooks/parse'

const scroll = { offset: 0, bodyRows: 60 }

describe('plain activity lines', () => {
  test('each tool reads as a few words', async () => {
    expect(fileName('C:\\repo\\hooks\\parse.ts')).toBe('parse.ts')
    expect(describeTool('Read', { file_path: 'C:\\repo\\hooks\\parse.ts' })).toBe('read parse.ts')
    expect(describeTool('Edit', { file_path: '/repo/a.ts' })).toBe('edit a.ts')
    expect(describeTool('Bash', { command: 'npm test -- --watch=false', description: 'Run unit tests' })).toBe('run: Run unit tests')
    expect(describeTool('Bash', { command: 'git status' })).toBe('run: git status')
    expect(describeTool('Bash', { command: 'node C:/x/@davesheffer/hunch/dist/cli/index.js task verify htask_0 -- npm test', description: 'Verify' })).toBe(
      'check: Verify',
    )
    expect(describeTool('Agent', { subagent_type: 'critic', model: 'fable', description: 'Review diff', prompt: 'long' })).toBe(
      'start critic (fable): Review diff',
    )
    expect(describeTool('mcp__hunch__hunch_check_constraints', { scope: 'lib/a.dart' })).toBe('Hunch: check rules for lib/a.dart')
    expect(describeTool('mcp__claude_ai_Gmail__search_threads', {})).toBe('Gmail: search threads')
    expect(describeTool('Grep', { pattern: 'TODO', path: 'src' })).toBe('search for "TODO" in src')
  })

  test('the drawer holds the full command, prompt or edit; short lookups have none', async () => {
    expect(toolDetail('Bash', { command: 'a && b' })).toBe('a && b')
    expect(toolDetail('Agent', { prompt: 'do the thing' })).toBe('do the thing')
    expect(toolDetail('Read', { file_path: 'x' })).toBe('')
    expect(toolDetail('Bash', { command: 'x'.repeat(5000) })).toMatch(/1000 more characters\)$/)
  })
})

test('a tool call shows in the timeline and its drawer opens on demand', async ($, on) => {
  mock.clock(on, { now: Date.parse('2026-10-07T10:00:00') })
  on('ui.status', () => ({ value: undefined }))
  on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: 'ok', stderr: '', interrupted: false }, text: 'ok' }))

  const command = 'python -m unittest tests.test_relay tests.test_rollover_open'
  await $.tool.call({ tool: 'Bash', command, description: 'Run relay tests' })

  for (const surface of ['terminal', 'desktop'] as const) {
    const pane = await $.ui.mount({
      plugin: 'mission-control',
      surface,
      component: 'Pane',
      requestId: 'mission-control',
      props: { title: 'Mission Control', isFocused: false, bodyColumns: 100, placement: 'dock', scroll, view: {} },
    })
    expect(await pane.find({ text: /main +✔ run: Run relay tests/ })).toBeDefined()
    expect(await pane.find({ text: /tests\.test_rollover_open/ })).toBeUndefined()

    const drawer = await pane.find({ type: 'Button', text: '▸' })
    expect(drawer).toBeDefined()
    await pane.press({ key: drawer?.key ?? '' })
    expect(await pane.find({ text: /│ python -m unittest tests\.test_relay tests\.test_rollover_open/ })).toBeDefined()
    await pane.press({ key: drawer?.key ?? '' })
    expect(await pane.find({ text: /tests\.test_rollover_open/ })).toBeUndefined()
    await pane.unmount()
  }

  const band = await $.ui.mount({
    plugin: 'mission-control',
    surface: 'terminal',
    component: 'AbovePrompt',
    props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 120, scroll, view: {} },
  })
  expect(await band.find({ text: /now: idle, waiting for you/ })).toBeDefined()
  await band.unmount()
})

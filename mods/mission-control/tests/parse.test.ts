import { describe, expect, test } from 'claude-code/testing'

import { isRole, jevStatus, parseCard, parseLines, shortModel, summarize, zoneOf } from '../hooks/parse'

describe('parseCard', () => {
  test('a full high-confidence card is not weak', async () => {
    const card = parseCard(
      [
        'RESULT: parser fixed, 12 tests pass',
        'EVIDENCE: `npm test` exit 0',
        'CONFIDENCE: high',
        'UNVERIFIED: none',
      ].join('\n'),
    )
    expect(card.missing).toEqual([])
    expect(card.confidence).toBe('high')
    expect(card.unverified).toBe(0)
    expect(card.exitCodes).toEqual([0])
    expect(card.isWeak).toBe(false)
    expect(card.result).toBe('parser fixed, 12 tests pass')
  })

  test('markdown headers, medium confidence and bullet unverified items make it weak', async () => {
    const card = parseCard(
      [
        '**RESULT**: found 3 call sites',
        '**EVIDENCE**:',
        '- src/a.ts:10',
        '**CONFIDENCE**: Medium',
        '**UNVERIFIED**:',
        '- whether b.ts is dead code',
        '- runtime path on Windows',
      ].join('\n'),
    )
    expect(card.missing).toEqual([])
    expect(card.confidence).toBe('medium')
    expect(card.unverified).toBe(2)
    expect(card.isWeak).toBe(true)
  })

  test('a card missing EVIDENCE is weak and says so', async () => {
    const card = parseCard('RESULT: done\nCONFIDENCE: high\nUNVERIFIED: -')
    expect(card.missing).toEqual(['EVIDENCE'])
    expect(card.isWeak).toBe(true)
    expect(card.hasAnyField).toBe(true)
  })

  test('a free-form answer has no card fields', async () => {
    const card = parseCard('Here is a summary of the repo layout.')
    expect(card.hasAnyField).toBe(false)
    expect(card.confidence).toBe('missing')
  })
})

describe('helpers', () => {
  test('roles, models and zones', async () => {
    expect(isRole('scout')).toBe(true)
    expect(isRole('orchestrator:critic')).toBe(true)
    expect(isRole('Explore')).toBe(false)
    expect(shortModel('claude-sonnet-5-5')).toBe('sonnet')
    expect(shortModel('inherit')).toBe('inherit')
    expect(zoneOf(undefined, 150_000, 250_000)).toBe('unknown')
    expect(zoneOf(149_999, 150_000, 250_000)).toBe('green')
    expect(zoneOf(150_000, 150_000, 250_000)).toBe('amber')
    expect(zoneOf(260_000, 150_000, 250_000)).toBe('red')
  })
})

describe('jev log', () => {
  const log = [
    '{"ts": "2026-10-05T11:00:00+0300", "kind": "usage", "feature": "route", "usd": 0.5}',
    '{"ts": "2026-10-06T09:00:00+0300", "kind": "usage", "feature": "route", "usd": 0.25}',
    '{"ts": "2026-10-06T10:00:00+0300", "feature": "report_check", "decision": "weak", "subagent_type": "scout", "reasons": ["no exit code"]}',
    '{"ts": "2026-10-06T13:07:01+0300", "feature": "risk_gate", "op": "push", "decision": "deny", "fail_closed": true, "error": "NoApiKey"}',
    '{"ts": "2026-10-06T13:08:35+0300", "feature": "shift", "zone": "amber", "decision": "unsure"}',
    '{"ts": "2026-10-06T13:09', // torn line while Jev writes
  ].join('\n')

  test('health, today figures and the feed', async () => {
    const lines = parseLines(log)
    expect(lines.length).toBe(5)

    const status = jevStatus(lines, '2026-10-06', true)
    expect(status.isOnline).toBe(false)
    expect(status.lastError).toBe('NoApiKey')
    expect(status.spendToday).toBe(0.25)
    expect(status.deniesToday).toBe(1)
    expect(status.weakToday).toBe(1)
    expect(status.recent.map(entry => entry.feature)).toEqual(['risk_gate', 'report_check'])
    expect(status.recent[0]?.summary).toBe('push DENY fail-closed')
    expect(status.recent[0]?.isAlert).toBe(true)
  })

  test('a disabled Jev reads offline', async () => {
    expect(jevStatus(parseLines(log), '2026-10-06', false).isOnline).toBe(false)
  })

  test('routine shifts and usage rows are not feed entries', async () => {
    expect(summarize({ feature: 'shift', decision: 'continue' })).toBeUndefined()
    expect(summarize({ kind: 'usage', feature: 'route' })).toBeUndefined()
  })
})

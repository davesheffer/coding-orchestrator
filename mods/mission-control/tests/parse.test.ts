import { describe, expect, test } from 'claude-code/testing'

import {
  bar,
  capDetail,
  describeTool,
  hunchCli,
  hunchOutcome,
  isRole,
  isSecretFile,
  jevStatus,
  label,
  parseAnswer,
  parseCard,
  parseLines,
  redact,
  shortModel,
  summarize,
  toolDetail,
  weakReason,
  zoneOf,
} from '../hooks/parse'

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

  test('prose that starts with a field name is not a header', async () => {
    const card = parseCard(
      [
        'RESULT: parser fixed',
        'Result was fine on the second run.',
        'evidence: lowercase is prose too',
        'EVIDENCE: `bun test` exit 0',
        'CONFIDENCE: high',
        'Confidence in the build is solid, though.',
        'UNVERIFIED: none',
      ].join('\n'),
    )
    expect(card.missing).toEqual([])
    expect(card.result).toBe('parser fixed')
    // The prose line is no header, so it belongs to the CONFIDENCE section, and its `though` takes the high back.
    expect(card.confidence).toBe('missing')
    // So does any line below the level that does not just back it.
    expect(parseCard('RESULT: x\nCONFIDENCE: high\nConfidence in the build is solid.\nUNVERIFIED: none').confidence).toBe('missing')
    expect(parseCard('Result - done\nConfidence high').hasAnyField).toBe(false)
  })

  test('confidence is high only when it leads and nothing lower is mentioned', async () => {
    expect(parseCard('CONFIDENCE: high').confidence).toBe('high')
    expect(parseCard('CONFIDENCE: Very high (all 12 tests pass)').confidence).toBe('high')
    expect(parseCard('CONFIDENCE: not high, low').confidence).toBe('low')
    expect(parseCard('CONFIDENCE: high\nlow for the installer change').confidence).toBe('low')
    expect(parseCard('CONFIDENCE: high for the parser, medium for the rest').confidence).toBe('medium')
    expect(parseCard('CONFIDENCE: Med, the Windows path is untested').confidence).toBe('medium')
    expect(parseCard('CONFIDENCE:\n- low').confidence).toBe('low')
    expect(parseCard('CONFIDENCE: not high').confidence).toBe('missing')
    expect(parseCard('CONFIDENCE: very high confidence').confidence).toBe('high')
    // Prose below the card, after a blank line, is not part of the section.
    expect(parseCard('CONFIDENCE: high\n\nThe diff is low risk.').confidence).toBe('high')
  })

  test('unrecognised confidence wording is weak and the toast says why', async () => {
    const card = parseCard('RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: fairly high\nUNVERIFIED: none')
    expect(card.missing).toEqual([])
    expect(card.confidence).toBe('missing')
    expect(card.isWeak).toBe(true)
    expect(weakReason(card)).toBe('confidence unclear ("fairly high")')
    expect(weakReason(parseCard('RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE:\nUNVERIFIED: none'))).toBe('confidence not stated')
    expect(weakReason(parseCard('RESULT: done\nCONFIDENCE: medium\nUNVERIFIED:\n- a\n- b'))).toBe(
      'missing EVIDENCE, confidence medium, 2 unverified',
    )
    expect(weakReason(parseCard('RESULT: done'))).toBe('missing EVIDENCE/CONFIDENCE/UNVERIFIED')
  })

  test('only the last card is graded; quoted and fenced cards are not', async () => {
    const weak = ['RESULT: first pass', 'EVIDENCE: exit 1', 'CONFIDENCE: low', 'UNVERIFIED:', '- the Windows path']
    const strong = ['RESULT: second pass', 'EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none']

    const later = parseCard([...weak, '', 'After the retry:', '', ...strong].join('\n'))
    expect([later.result, later.confidence, later.unverified, later.isWeak]).toEqual(['second pass', 'high', 0, false])
    const earlier = parseCard([...strong, '', 'Then it regressed:', '', ...weak].join('\n'))
    expect([earlier.result, earlier.confidence, earlier.unverified, earlier.isWeak]).toEqual(['first pass', 'low', 1, true])

    // A stray header in the prose above does not split the card below it.
    const stray = parseCard(['CONFIDENCE: low on the first try', '', ...strong].join('\n'))
    expect([stray.missing, stray.confidence]).toEqual([[], 'high'])

    // A quoted complete card cannot stand in for a report that has none of its own, and a fenced
    // one does not lend its fields to the report around it.
    const quoted = parseCard(['The builder said:', ...strong.map(line => `> ${line}`), 'I disagree.'].join('\n'))
    expect(quoted.hasAnyField).toBe(false)
    const fenced = parseCard(['RESULT: reviewed', 'Their card:', '```', ...strong, '```', 'CONFIDENCE: medium'].join('\n'))
    expect(fenced.missing).toEqual(['EVIDENCE', 'UNVERIFIED'])
    expect(fenced.confidence).toBe('medium')
  })

  test('the last card borrows no field from a card pasted above it', async () => {
    const pasted = ['RESULT: their pass', 'EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none']

    // A new card starts at each RESULT header.
    const own = parseCard([...pasted, 'RESULT: my pass', 'CONFIDENCE: high'].join('\n'))
    expect([own.result, own.missing, own.isWeak]).toEqual(['my pass', ['EVIDENCE', 'UNVERIFIED'], true])
    const bare = parseCard([...pasted, '', 'RESULT: looked at it'].join('\n'))
    expect([bare.result, bare.missing]).toEqual(['looked at it', ['EVIDENCE', 'CONFIDENCE', 'UNVERIFIED']])

    // And where CONFIDENCE or UNVERIFIED repeats, with no RESULT of its own.
    const tail = parseCard([...pasted, 'CONFIDENCE: low', 'UNVERIFIED:', '- the deploy step'].join('\n'))
    expect([tail.missing, tail.confidence, tail.unverified]).toEqual([['RESULT', 'EVIDENCE'], 'low', 1])
    const repeated = parseCard([...pasted, 'UNVERIFIED: the Windows path'].join('\n'))
    expect([repeated.missing, repeated.unverified, repeated.isWeak]).toEqual([['RESULT', 'EVIDENCE', 'CONFIDENCE'], 1, true])
    const headless = parseCard(['EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none', ...pasted.slice(2)].join('\n'))
    expect(headless.missing).toEqual(['RESULT', 'EVIDENCE'])
  })

  test('unverified: a leading none is nothing, an exception is an item, prose below is one unless it is a closing', async () => {
    const count = (section: string) => parseCard(`UNVERIFIED: ${section}`).unverified

    for (const nothing of ['none', 'None.', 'none - every path ran', 'none — all checked', 'N/A', '-', '(none)', 'no unverified items', 'nothing.']) {
      expect([nothing, count(nothing)]).toEqual([nothing, 0])
    }
    expect(count('no unverified items except the Windows path')).toBe(1)
    expect(count('none of the Windows paths ran')).toBe(1)
    expect(count('nonetheless untested on macOS')).toBe(1)
    // Prose below a blank line is read closed: only a closing is not an unverified item.
    expect(count('none\n\nFiles changed: src/a.ts')).toBe(1)
    expect(count('none\n\nDone.')).toBe(0)
    // A blank line straight after the header does not hide the items below it.
    expect(count('\n\n- the Windows path')).toBe(1)
  })

  test('unverified: a none that is taken back is an item', async () => {
    const full = (section: string) => parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED: ${section}`)

    for (const caveat of [
      'none, except the migration was only run on SQLite',
      'None. However the deploy script was not exercised.',
      'none (did not run on Windows)',
      'none - except the Windows path',
      'None; e2e tests could not run here',
      'N/A: tests could not run',
      'nothing - but CI was not run',
      'none: apart from the publish step',
      "none - the installer wasn't run",
      'none, though the e2e suite was skipped',
    ]) {
      expect([caveat, full(caveat).unverified, full(caveat).isWeak]).toEqual([caveat, 1, true])
    }
    for (const remark of ['none - every path ran', 'None. All 12 tests passed.', 'none (all checks ran locally)', 'N/A: read-only task']) {
      expect([remark, full(remark).unverified, full(remark).isWeak]).toEqual([remark, 0, false])
    }
  })

  test('unverified: quoted, fenced and later-paragraph items still count', async () => {
    const full = (section: string) => parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED:${section}`)
    const weak = (section: string) => [full(section).unverified > 0, full(section).isWeak]

    expect(weak('\n> did not run the migration')).toEqual([true, true])
    expect(weak('\n```\ndid not run the migration\n```')).toEqual([true, true])
    expect(weak('\n~~~text\ndeploy step not exercised\n~~~')).toEqual([true, true])
    // Items below a blank line belong to the section; only prose ends it.
    expect(weak('\nnone.\n\n- except the deploy step')).toEqual([true, true])
    expect(weak('\n- none\n\n- deploy step not run')).toEqual([true, true])
    expect(full('\n- a\n- b\n\n- c, after a blank line').unverified).toBe(3)
    expect(full(' none\n\n> except the deploy step').unverified).toBe(1)
    expect(weak(' none\n\nFiles changed: src/a.ts\n- src/b.ts')).toEqual([true, true])
    // A fence left open still shows its items.
    expect(weak('\n```\nthe Windows path')).toEqual([true, true])
  })

  test('fences: inline code is no fence, a closer matches its opener, a fenced-whole card is graded', async () => {
    const strong = ['RESULT: second pass', 'EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none']

    // A line of inline code in triple backticks does not open a fence that hides the rest.
    const inline = parseCard(['RESULT: done', 'EVIDENCE:', '```mix precommit``` exit 0', 'CONFIDENCE: low', 'UNVERIFIED:', '- the deploy step'].join('\n'))
    expect([inline.missing, inline.confidence, inline.unverified]).toEqual([[], 'low', 1])
    const sameLine = parseCard(['RESULT: done', 'EVIDENCE: ```mix precommit```', 'CONFIDENCE: high', 'UNVERIFIED:', '```mix test```', '- was not run on Windows'].join('\n'))
    expect([sameLine.missing, sameLine.unverified, sameLine.isWeak]).toEqual([[], 1, true])

    // A three-backtick fence inside a four-backtick one does not close it.
    const nested = parseCard(
      ['RESULT: reviewed', 'EVIDENCE: exit 0', '````', '```', 'CONFIDENCE: high', '```', 'UNVERIFIED: none', '````', 'CONFIDENCE: low', 'UNVERIFIED:', '- the publish step'].join('\n'),
    )
    expect([nested.missing, nested.confidence, nested.unverified]).toEqual([[], 'low', 1])
    const tilde = parseCard(['RESULT: reviewed', '~~~', '```', 'CONFIDENCE: high', 'UNVERIFIED: none', '~~~', 'CONFIDENCE: medium'].join('\n'))
    expect([tilde.missing, tilde.confidence]).toEqual([['EVIDENCE', 'UNVERIFIED'], 'medium'])

    // A report fenced whole is graded as written, not reported as missing every field.
    for (const fence of ['```', '```text', '````', '~~~']) {
      const whole = parseCard(['', fence, ...strong, fence.replace(/[a-z]+$/, ''), ''].join('\n'))
      expect([fence, whole.missing, whole.confidence, whole.isWeak, whole.result]).toEqual([fence, [], 'high', false, 'second pass'])
    }
    const weakWhole = parseCard(['```', 'RESULT: x', 'EVIDENCE: exit 1', 'CONFIDENCE: low', 'UNVERIFIED:', '- a', '```'].join('\n'))
    expect([weakWhole.confidence, weakWhole.unverified, weakWhole.isWeak]).toEqual(['low', 1, true])
    // The last fenced card, when there are several.
    const two = parseCard(['```', ...strong, '```', '', '```', 'RESULT: third', 'CONFIDENCE: medium', '```'].join('\n'))
    expect([two.result, two.missing, two.confidence]).toEqual(['third', ['EVIDENCE', 'UNVERIFIED'], 'medium'])
  })

  test('confidence: low and medium name the level only when they lead; after a high they hedge it', async () => {
    // No list decides whether the word describes the confidence or something else: the high is taken back.
    for (const hedged of ['High — low risk of regression', 'high (no low-level changes)', 'High; a low-risk, medium-sized diff', 'high, low impact']) {
      expect([hedged, parseCard(`CONFIDENCE: ${hedged}`).confidence]).toEqual([hedged, 'missing'])
    }
    for (const low of ['low', 'Low — the risk is high', 'high for the parser, low for the installer', 'high, but low on Windows']) {
      expect([low, parseCard(`CONFIDENCE: ${low}`).confidence]).toEqual([low, 'low'])
    }
    // Without a leading high the section still names no level, so the card stays weak.
    expect(parseCard('CONFIDENCE: low risk').confidence).toBe('missing')
  })

  test('confidence is read from its first clause; a later one names a level when it leads with one, and hedges otherwise', async () => {
    for (const hedged of ['high (memory use stays low)', 'high\nThe failure rate was low in 100 runs.']) {
      expect([hedged, parseCard(`CONFIDENCE: ${hedged}`).confidence]).toEqual([hedged, 'missing'])
    }
    expect(parseCard('CONFIDENCE: highly confident').confidence).toBe('high')
    expect(parseCard('CONFIDENCE: low-ish, could not run it').confidence).toBe('low')
    expect(parseCard('CONFIDENCE: low-ish').confidence).toBe('low')
    expect(parseCard('CONFIDENCE: medium-high').confidence).toBe('medium')
    expect(parseCard('CONFIDENCE:\n- parser: high\n- installer: low').confidence).toBe('low')
  })

  test('unverified: a remark after none is read closed', async () => {
    const full = (section: string) => parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED: ${section}`)

    for (const item of [
      'none - relied on reading the code only; tests were left for CI',
      'none (the migration is assumed to be idempotent)',
      'None - see EVIDENCE; the prod path was only simulated',
      // A paragraph below a blank line that takes the none back.
      'none\n\nHowever, I could not run the e2e tests.',
    ]) {
      expect([item, full(item).unverified, full(item).isWeak]).toEqual([item, 1, true])
    }
    for (const nothing of ['_none_', '*none*', '**none**', '_None._', 'none at this time', 'none - all ran', 'None, everything verified.']) {
      expect([nothing, full(nothing).unverified, full(nothing).isWeak]).toEqual([nothing, 0, false])
    }
  })

  test('unverified: a none followed by what has not happened yet is an item', async () => {
    const full = (section: string) => parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED: ${section}`)

    for (const item of [
      'none - all tests should pass',
      'N/A - all checks will be run in CI',
      'none - everything still needs to be verified',
      'none - all tests I was able to run passed',
      // Only what already happened counts as ran.
      'none - all tests run',
    ]) {
      expect([item, full(item).unverified >= 1, full(item).isWeak]).toEqual([item, true, true])
    }
    for (const nothing of ['none - all tests ran', 'none - all tests passed', 'None. All 12 tests pass.', 'none - all checks were run', 'none', 'none.', '_none_']) {
      expect([nothing, full(nothing).unverified, full(nothing).isWeak]).toEqual([nothing, 0, false])
    }
  })

  test('unverified: a fenced block below a plain none is an item, sample output too', async () => {
    const full = (section: string) => parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED: ${section}`)

    // Round nine: read closed, it cannot be told from an open item (`the Windows path is broken`).
    const sample = full('none\n\n```\n$ npm test\n59 pass, 0 fail\n```')
    expect([sample.unverified, sample.isWeak]).toEqual([1, true])
    expect(full('none\n```\nnot run on Windows\n```').unverified).toBe(1)
    expect(full('none.\n~~~text\nok\n~~~\n').unverified).toBe(1)
    // Without the none, or below a none that was taken back, fenced text is still an item.
    expect(full('\n```\ndid not run the migration\n```').unverified).toBe(1)
    expect(full('none, except:\n```\nthe deploy step\n```').unverified).toBe(1)
  })

  test('confidence: high that describes a risk or a doubt is not a level', async () => {
    const full = (said: string) => parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: ${said}\nUNVERIFIED: none`)

    for (const hedge of [
      'high risk of breaking Windows',
      'highly uncertain',
      'High chance this misses macOS',
      'highly doubtful',
      'highly unlikely to hold',
      'High (the risk is in the installer)',
    ]) {
      expect([hedge, full(hedge).confidence, full(hedge).isWeak]).toEqual([hedge, 'missing', true])
    }
    for (const level of ['high', 'high - all checks passed', 'highly confident']) {
      expect([level, full(level).confidence, full(level).isWeak]).toEqual([level, 'high', false])
    }
  })

  test('fields out of order or a stray header do not split a complete card', async () => {
    const strong = ['RESULT: second pass', 'EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none']

    const swapped = parseCard(['RESULT: done', 'CONFIDENCE: high', 'EVIDENCE: exit 0', 'UNVERIFIED: none'].join('\n'))
    expect([swapped.missing, swapped.confidence, swapped.isWeak]).toEqual([[], 'high', false])
    const late = parseCard('EVIDENCE: ran pytest\nRESULT: 12 passed')
    expect([late.missing, late.result]).toEqual([['CONFIDENCE', 'UNVERIFIED'], '12 passed'])
    const extra = parseCard([...strong, 'EVIDENCE: also see logs'].join('\n'))
    expect([extra.missing, extra.confidence, extra.result, extra.isWeak]).toEqual([[], 'high', 'second pass', false])

    // A grade is still never borrowed: a repeated CONFIDENCE or UNVERIFIED keeps only what is below the earlier one.
    const regraded = parseCard([...strong, 'CONFIDENCE: high'].join('\n'))
    expect([regraded.missing, regraded.isWeak]).toEqual([['RESULT', 'EVIDENCE'], true])
    const own = parseCard([...strong, 'EVIDENCE: exit 1', 'CONFIDENCE: low', 'UNVERIFIED:', '- the deploy step'].join('\n'))
    expect([own.missing, own.confidence, own.unverified]).toEqual([['RESULT'], 'low', 1])
    // A RESULT that stands apart from the header above it starts its own card.
    const apart = parseCard(['CONFIDENCE: high so far', '', 'RESULT: done', 'EVIDENCE: exit 0', 'UNVERIFIED: none'].join('\n'))
    expect([apart.missing, apart.isWeak]).toEqual([['CONFIDENCE'], true])
  })

  test('a report quoted whole is graded as written', async () => {
    const strong = ['RESULT: second pass', 'EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none']
    const whole = parseCard(strong.map(line => `> ${line}`).join('\n'))
    expect([whole.missing, whole.confidence, whole.result, whole.isWeak]).toEqual([[], 'high', 'second pass', false])
    const weak = parseCard(['> RESULT: x', '>', '> CONFIDENCE: low', '> UNVERIFIED: the deploy step'].join('\n'))
    expect([weak.missing, weak.confidence, weak.unverified]).toEqual([['EVIDENCE'], 'low', 1])
  })

  test('exit codes in prose and JSON', async () => {
    expect(parseCard('EVIDENCE: `npm test` exit 0; {"exit_code": 2}; exit code: 1').exitCodes).toEqual([0, 2, 1])
    expect(parseCard('exit  code = 3, exit:4').exitCodes).toEqual([3, 4])

    // A long run of spaces after `exit` is not rescanned from every position.
    const started = Date.now()
    expect(parseCard(`exit${' '.repeat(50_000)}`).exitCodes).toEqual([])
    expect(parseCard(`${'exit '.repeat(20_000)}`).exitCodes).toEqual([])
    expect(Date.now() - started).toBeLessThan(500)
  })
})

describe('redaction', () => {
  const TOKEN = `ghp_${'a1B2'.repeat(9)}`

  test('credentials are masked, ordinary text is left alone', async () => {
    expect(redact('curl -H "Authorization: Bearer abc.def-123" https://api.x.io')).toBe('curl -H "Authorization: ***" https://api.x.io')
    expect(redact('{"authorization": "Basic dXNlcjpwYXNz"}')).toBe('{"authorization": "***"}')
    expect(redact('X-Api-Key: 12345')).toBe('X-Api-Key: ***')
    expect(redact('send Bearer abcdefgh12345678 along')).toBe('send Bearer *** along')
    expect(redact('GITHUB_TOKEN=abc123 npm publish')).toBe('GITHUB_TOKEN=*** npm publish')
    expect(redact('export OPENAI_API_KEY="sk-short" && run')).toBe('export OPENAI_API_KEY=*** && run')
    expect(redact("$env:DB_PASSWORD = 'hunter 2'; ./go.ps1")).toBe('$env:DB_PASSWORD = ***; ./go.ps1')
    expect(redact('CLIENT_SECRET=s3 PASSWORD=p4 x=1')).toBe('CLIENT_SECRET=*** PASSWORD=*** x=1')
    expect(redact('{"api_key": "abc", "query": "q"}')).toBe('{"api_key": "***", "query": "q"}')
    expect(redact(`gh auth login --with-token ${TOKEN}`)).toBe('gh auth login --with-token ***')
    expect(redact(`github_pat_${'x'.repeat(30)} gho_${'y'.repeat(24)} ghs_${'z'.repeat(24)}`)).toBe('*** *** ***')
    expect(redact(`sk-${'k'.repeat(32)} and xoxb-1234567890-abcdef`)).toBe('*** and ***')
    expect(redact('git clone https://user:p4ss@github.com/o/r.git')).toBe('git clone https://***@github.com/o/r.git')
    expect(redact('postgres://admin:pw@db:5432/app')).toBe('postgres://***@db:5432/app')

    for (const plain of ['git status', 'npm test -- --max-tokens=5', 'monkey=3 task-runner key: value', 'see https://example.com/a@b', 'bearer of news']) {
      expect(redact(plain)).toBe(plain)
    }
    // A port-like password followed by `?` is still userinfo.
    expect(redact('git clone https://user:12345?x@host/repo')).not.toContain('12345?x')
    expect(redact('https://host:5173/@vite/client')).toBe('https://host:5173/@vite/client')
    expect(redact('https://host:8080/a?to=a@b.com')).toBe('https://host:8080/a?to=a@b.com')
    // A comma or bracket after the closing quote ends the name; it opens no value.
    expect(redact('f("password=", pw)')).toBe('f("password=", pw)')
    expect(redact('{"pattern":"password="}')).toBe('{"pattern":"password="}')
    expect(redact('echo "token="abc123')).not.toContain('abc123')
    // Masking twice changes nothing more.
    const once =redact(`Authorization: Bearer ${TOKEN} API_KEY=abc https://u:p@h/x`)
    expect(redact(once)).toBe(once)
  })

  test('secret flags, curl credentials and PEM private keys are masked', async () => {
    expect(redact('mysql --password hunter2 -h db')).toBe('mysql --password *** -h db')
    expect(redact('tool --token=abc123 --secret "s 3" --api-key \'k1\' go')).toBe('tool --token=*** --secret *** --api-key *** go')
    expect(redact('curl -u admin:pw https://x.io')).toBe('curl -u *** https://x.io')
    expect(redact('curl -s --user "admin:p w" https://x.io')).toBe('curl -s --user *** https://x.io')
    expect(redact('curl -uadmin:pw https://x.io')).toBe('curl -u*** https://x.io')
    // Look-alikes keep their value: another flag, a curl option that only starts with `--user`.
    for (const plain of ['curl --user-agent bot https://x.io', 'mycli --token-file t.txt', 'ssh -u root host']) {
      expect(redact(plain)).toBe(plain)
    }

    const pem = '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n-----END RSA PRIVATE KEY-----'
    expect(redact(`key:\n${pem}\ndone`)).toBe('key:\n-----BEGIN RSA PRIVATE KEY-----***-----END RSA PRIVATE KEY-----\ndone')
    expect(redact('-----BEGIN PRIVATE KEY-----\nMIIEvQ cut short')).toBe('-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----')
    expect(redact('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----')).toBe(
      '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----',
    )
    const once = redact(`${pem} --password p`)
    expect(redact(once)).toBe(once)
  })

  test('timeline lines and drawers are masked before they are cut', async () => {
    const command = `curl -H "Authorization: Bearer ${TOKEN}" https://api.github.com/user`
    expect(describeTool('Bash', { command })).toBe('run: curl -H "Authorization: ***" https://api.github.com/user')
    expect(describeTool('PowerShell', { command: `$env:GITHUB_TOKEN = "${TOKEN}"; gh pr list` })).toBe('run: $env:GITHUB_TOKEN = ***; gh pr list')
    expect(describeTool('WebFetch', { url: 'https://user:pw@example.com/path' })).toBe('fetch ***@example.com')
    expect(describeTool('Agent', { subagent_type: 'runner', description: `push with ${TOKEN}` })).toBe('start runner: push with ***')
    expect(toolDetail('Bash', { command })).not.toContain(TOKEN)
    expect(toolDetail('Agent', { prompt: `use API_KEY=${TOKEN} to deploy` })).toBe('use API_KEY=*** to deploy')
    expect(toolDetail('mcp__x__call', { password: 'hunter2', query: 'q' })).toContain('"password": "***"')
    expect(label(`asked to use ${TOKEN} here`, 70)).toBe('asked to use *** here')

    // A token the 80-character cut would split is masked whole, not left half shown.
    const long = describeTool('Bash', { command: `${'echo '.repeat(12)}${TOKEN}` })
    expect(long).not.toContain('ghp_')
    const split = capDetail(`${'x'.repeat(3990)} ${TOKEN} tail`)
    expect(split).not.toContain('ghp_')
    expect(split).toMatch(/more characters\)$/)
  })

  test('the content of dotenv and key files is never kept', async () => {
    for (const path of ['.env', 'C:\\repo\\.env.local', '/srv/app/.env.production', 'certs/server.pem', 'tls/site.KEY', '/home/u/.ssh/id_rsa', 'id_rsa.pub', 'id_ed25519']) {
      expect([path, isSecretFile(path)]).toEqual([path, true])
    }
    for (const path of ['src/env.ts', 'docs/environment.md', 'keys.json', 'README.md', 'src/id_rsa_test/a.ts']) {
      expect([path, isSecretFile(path)]).toEqual([path, false])
    }

    expect(toolDetail('Write', { file_path: '/repo/.env', content: 'DB_URL=postgres://u:p@h/db\nPLAIN=value' })).toBe('/repo/.env\n[content hidden]')
    expect(toolDetail('Edit', { file_path: 'certs/server.pem', old_string: 'MIIB-old', new_string: 'MIIB-new' })).toBe(
      'certs/server.pem\n[content hidden]',
    )
    expect(toolDetail('Write', { file_path: '/repo/a.ts', content: 'const a = 1' })).toBe('/repo/a.ts\nconst a = 1')
    expect(toolDetail('Edit', { file_path: 'a.ts', old_string: 'TOKEN=abc', new_string: 'b' })).toBe('a.ts\n--- old\nTOKEN=***\n+++ new\nb')
  })

  test('credential files beyond dotenv and keys are hidden too', async () => {
    for (const path of ['prod.env', 'config/staging.ENV', '/home/u/.aws/credentials', '.netrc', 'C:\\Users\\u\\_netrc', '.pgpass', 'gcp/credentials.json', 'k8s/secrets.yaml', 'secret.yml', 'certs/site.pfx', 'store.p12']) {
      expect([path, isSecretFile(path)]).toEqual([path, true])
    }
    for (const path of ['src/credentials.ts', 'docs/secrets.md', 'environment.yaml', 'src/netrc.py', 'a.p12.md']) {
      expect([path, isSecretFile(path)]).toEqual([path, false])
    }
    expect(toolDetail('Write', { file_path: 'deploy/prod.env', content: 'PLAIN=value' })).toBe('deploy/prod.env\n[content hidden]')
  })

  test('a flag, an option or a variable whose name ends in a secret word has its value masked', async () => {
    expect(redact('tool --client-secret X1 --apikey X2 --access-token X3 --http-password X4 --secret-access-key X5 go')).toBe(
      'tool --client-secret *** --apikey *** --access-token *** --http-password *** --secret-access-key *** go',
    )
    expect(redact('mysql -pS3cret -h db app')).toBe('mysql -p*** -h db app')
    expect(redact('docker login -u me -p S3cret registry.io')).toBe('docker login -u me -p *** registry.io')
    expect(redact('Connect-Thing -Password S3cret -Token Y2 -Verbose')).toBe('Connect-Thing -Password *** -Token *** -Verbose')
    expect(redact('ConvertTo-SecureString "S3cret" -AsPlainText -Force')).toBe('ConvertTo-SecureString *** -AsPlainText -Force')
    expect(redact('curl -sSu user:pass https://x.io')).toBe('curl -sSu *** https://x.io')
    expect(redact('MYSQL_PWD=a1 SMTP_PASS=b2 PASSPHRASE=c3 run')).toBe('MYSQL_PWD=*** SMTP_PASS=*** PASSPHRASE=*** run')
    expect(redact('SECRET_KEY=abc AWS_SECRET_ACCESS_KEY=zz')).toBe('SECRET_KEY=*** AWS_SECRET_ACCESS_KEY=***')
    // Look-alikes: a port, a directory, a prompt for the password, a flag that takes no value here.
    for (const plain of ['mysql -P 3306 -p', 'mkdir -p a/b', 'ssh -p 22 host', 'docker run -p 80:80 img', 'gh auth login --with-token < token.txt', 'tool --password --verbose']) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('YAML, object, JSON, escaped JSON, cut-short and query values are masked', async () => {
    expect(redact('password: S3cret')).toBe('password: ***')
    expect(redact("{password: 'S3cret'}")).toBe("{password: '***'}")
    expect(redact("{'password': 'S3cret'}")).toBe("{'password': '***'}")
    expect(redact('{"password": 12345678, "user": "u"}')).toBe('{"password": ***, "user": "u"}')
    expect(redact('{\\"api_key\\": \\"S3cret\\"}')).toBe('{\\"api_key\\": \\"***\\"}')
    // A quoted value is masked to its closing quote, across lines, or to the end when it has none.
    expect(redact('TOKEN="abc\ndef" tail')).toBe('TOKEN=*** tail')
    expect(redact('TOKEN="abc\ndef')).toBe('TOKEN=***')
    expect(redact('{"token": "abc')).toBe('{"token": "***"')
    expect(redact('https://x.io/a?api-key=S3cret&x=1')).toBe('https://x.io/a?api-key=***&x=1')
    expect(redact('db_password = hunter2')).toBe('db_password = ***')
  })

  test('token shapes and PGP private keys are masked', async () => {
    expect(redact('AKIAIOSFODNN7EXAMPLE sk_live_abcdefghij1234567890 glpat-abcdefghij1234567890')).toBe('*** *** ***')
    expect(redact(`npm_${'a1'.repeat(18)}`)).toBe('***')
    expect(redact('jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N here')).toBe('jwt *** here')
    expect(redact('-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n-----END PGP PRIVATE KEY BLOCK-----\nafter')).toBe(
      '-----BEGIN PGP PRIVATE KEY BLOCK-----***-----END PGP PRIVATE KEY BLOCK-----\nafter',
    )
    expect(redact('-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQEN\n-----END PGP PUBLIC KEY BLOCK-----')).toContain('mQEN')
  })

  test('code and prose that only mention a secret word are left alone', async () => {
    for (const plain of [
      'if (token == null) return',
      'if (token === undefined || token != other) return',
      'const token = await getToken()',
      'token = getToken(x)',
      'sort_key=name',
      'primary_key = id',
      'Bearer authentication is required',
      'Authorization: required by the API',
      'https://example.com?to=a@b.com',
      'pass: 59, fail: 1',
      'compass=3 bypass: yes',
      'function f(token: string, password?: string) {}',
      'pass --token to the CLI',
      'mycli --max-tokens 5 --token-file t.txt',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
    // Userinfo is masked in the authority only: the host and the path stay.
    expect(redact('https://x-access-token:T0ken@github.com/o/r')).toBe('https://***@github.com/o/r')
    // After `user:` the password may hold an `@`, so the mask runs to the last one a host follows.
    expect(redact('https://user:pw@example.com/a?to=a@b.com')).toBe('https://***@b.com')
  })

  test('a URL password that holds / ? # or @ is masked whole', async () => {
    expect(redact('psql postgres://app:Zk3/9xQ+abc@db.internal/prod')).toBe('psql postgres://***@db.internal/prod')
    expect(redact('psql postgres://app:p@ss@db.internal/prod')).toBe('psql postgres://***@db.internal/prod')
    expect(redact('redis://:s3?cr#t@cache:6379/0')).toBe('redis://***@cache:6379/0')
    // A port is not a password, and an `@` in the path or the query is not userinfo.
    for (const plain of ['https://example.com?to=a@b.com', 'https://example.com:8080/a?to=a@b.com', 'see http://localhost:3000/@me/repo']) {
      expect(redact(plain)).toBe(plain)
    }
    const once = redact('postgres://app:p@ss@db.internal/prod and postgres://app:Zk3/9xQ@db/prod')
    expect(redact(once)).toBe(once)
  })

  test('values that look like code, glued pass names and glued -p passwords are masked', async () => {
    expect(redact('PASSWORD=Summer(2024)! ./run.sh')).toBe('PASSWORD=*** ./run.sh')
    expect(redact('DB_PASSWORD=hunter[2]x')).toBe('DB_PASSWORD=***')
    expect(redact('DBPASS=hunter2')).toBe('DBPASS=***')
    expect(redact('wp config create --dbpass=hunter2')).toBe('wp config create --dbpass=***')
    expect(redact('docker login -u me -phunter2 reg')).toBe('docker login -u me -p*** reg')
    expect(redact('sshpass -phunter2 ssh h')).toBe('sshpass -p*** ssh h')
    expect(redact('mongosh -u root -p hunter2')).toBe('mongosh -u root -p ***')
    expect(redact('aws configure set aws_secret_access_key wJalrXUtnFEMI/K7MDENG')).toBe('aws configure set aws_secret_access_key ***')
    expect(redact('curl -H "Cookie: session=abc123; theme=dark" https://x.io')).toBe('curl -H "Cookie: ***" https://x.io')
    expect(redact(`hf_${'aB3'.repeat(12)} SG.${'a1B2'.repeat(5)}.${'c3D4'.repeat(8)}`)).toBe('*** ***')
    // With spaces around the `=` a call is still code.
    expect(redact('token = getToken(x)')).toBe('token = getToken(x)')
    expect(redact('surpass=3 encompass: all')).toBe('surpass=3 encompass: all')
  })

  test('ordinary commands and code that mention a secret word are not mangled', async () => {
    for (const plain of [
      'grep -rn "password:" src/ | head -20\nsrc/a.ts:3: ask again',
      'echo "Enter password: "; read x; echo "hello"',
      'docker login registry && mkdir -p /tmp/out',
      'echo "login failed"; mkdir -p build/dir',
      "mysqldump db > out.sql && find . -name '*.sql' -print",
      '{"Authorization": f"Bearer {token}"}',
      'Authorization: `Bearer ${token}`',
      '"auth": {"user": "x"}',
      'PWD=/home/user make',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
    // A quoted value after a name outside any quote is still masked.
    expect(redact('echo "ok" && cfg password: "S3cret"')).toBe('echo "ok" && cfg password: "***"')
    expect(redact('docker login registry -p S3cret && mkdir -p out')).toBe('docker login registry -p *** && mkdir -p out')
    // A separator inside a quoted argument does not end the command.
    expect(redact('mysql -e "select 1; select 2" -pS3cret')).toBe('mysql -e "select 1; select 2" -p***')
  })

  test('a URL password that holds an @ before / ? or # leaves no tail', async () => {
    expect(redact('psql postgres://admin:p@ss/word@db.internal:5432/app')).toBe('psql postgres://***@db.internal:5432/app')
    expect(redact('git clone https://user:P@ss#1@github.com/o/r.git')).toBe('git clone https://***@github.com/o/r.git')
    expect(redact('curl https://user:12345/abc@host.com/x')).toBe('curl https://***@host.com/x')
    expect(redact('https://user:Pa/ss?w#rd@github.com/o/r.git')).toBe('https://***@github.com/o/r.git')
    expect(redact('(see https://user:p@ss@host.com)')).toBe('(see https://***@host.com)')
    for (const plain of ['https://github.com/o/r.git', 'https://host:8080/path', 'http://localhost:5173/@vite/client']) {
      expect(redact(plain)).toBe(plain)
    }
    // One word is read once, whatever it holds.
    const started = performance.now()
    for (const word of [`https://u:${'@a/'.repeat(6000)}`, `://a:${'@'.repeat(19_000)}`, '://a:'.repeat(4000)]) redact(word)
    expect(performance.now() - started).toBeLessThan(500)
  })

  test('a quoted name with = on a command line opens no value', async () => {
    for (const plain of ['grep -rn "password=" src/ && npm test', "rg 'token=' -g '*.py'"]) {
      expect(redact(plain)).toBe(plain)
    }
    expect(redact('password=hunter2')).toBe('password=***')
    expect(redact('TOKEN="abc123"')).toBe('TOKEN=***')
    expect(redact('echo "x" && TOKEN="abc123" npm test')).toBe('echo "x" && TOKEN=*** npm test')
  })

  test('netrc entries and redis-cli passwords are masked', async () => {
    expect(redact('echo "machine github.com login me password hunter2"')).toBe('echo "machine github.com login me password ***"')
    expect(redact('machine github.com\nlogin me\npassword hunter2')).toBe('machine github.com\nlogin me\npassword ***')
    expect(redact('redis-cli -a hunter2 ping')).toBe('redis-cli -a *** ping')
    expect(redact('redis-cli -h cache -p 6379 --pass hunter2 ping')).toBe('redis-cli -h cache -p 6379 --pass *** ping')
    for (const plain of ['the login page password field is required', 'redis-cli ping && ls -a src', 'ls -a src && redis-cli ping']) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('masking is idempotent', async () => {
    for (const text of [
      'tool --client-secret X1 -Password "a b"',
      "{password: 'S3cret', \"token\": \"abc\", key: 1}",
      '{\\"api_key\\": \\"S3cret\\"}',
      'TOKEN="abc\ndef',
      'mysql -pS3cret; docker login -p S3cret; curl -sSu a:b https://u:p@h/x?api-key=k',
      'Authorization: Bearer abc.def-123 and Bearer abcdefgh12345678',
    ]) {
      const once = redact(text)
      expect(redact(once)).toBe(once)
    }
  })

  test('adversarial input is masked in linear time, and long input is cut', async () => {
    const adversarial = [
      'x-'.repeat(50_000),
      'x-token-'.repeat(12_500),
      '"token'.repeat(16_000),
      'curl '.repeat(20_000),
      'token="'.repeat(14_000),
      'eyJ-'.repeat(25_000),
      '://a:b@'.repeat(14_000),
      '://a:'.repeat(20_000),
      'token:"'.repeat(14_000),
      'login '.repeat(17_000),
      `mysql "${'mysql '.repeat(50)}"`.repeat(330),
      'cookie:a='.repeat(11_000),
      'PASSWORD=a('.repeat(9_000),
    ]
    for (const text of adversarial) {
      const started = performance.now()
      const masked = redact(text)
      expect([text.slice(0, 8), performance.now() - started < 200]).toEqual([text.slice(0, 8), true])
      expect(masked.length).toBeLessThanOrEqual(20_001)
    }

    // Past the limit nothing is shown, masked or not; a secret within it still is.
    const long = redact(`PASSWORD=hunter2 ${'word '.repeat(5000)}TOKEN=late-secret`)
    expect(long.startsWith('PASSWORD=*** word')).toBe(true)
    expect(long).not.toContain('late-secret')
    expect(long.endsWith('…')).toBe(true)

    // A path or a tool name of any length reaches the timeline cut and masked.
    for (const tool of ['Read', 'Edit', 'Write', 'NotebookEdit']) {
      const started = performance.now()
      const line = describeTool(tool, { file_path: `/repo/${'x-'.repeat(50_000)}` })
      expect([tool, performance.now() - started < 200, line.length <= 80]).toEqual([tool, true, true])
    }
    const started = performance.now()
    expect(describeTool('x-token-'.repeat(12_500), {}).length).toBeLessThanOrEqual(80)
    expect(describeTool('Skill', { skill: '"token'.repeat(16_000) }).length).toBeLessThanOrEqual(80)
    expect(performance.now() - started).toBeLessThan(200)
    expect(describeTool('Read', { file_path: '/repo/src/parse.ts' })).toBe('read parse.ts')
  })
})

describe('hunch output scan', () => {
  test('100KB of adversarial Hunch output is graded in linear time, and ordinary output still reads', async () => {
    const cases: [string, string, boolean][] = [
      ['context', '<'.repeat(100_000), true],
      ['check_constraints', '\n'.repeat(100_000), false],
      ['check_constraints', `- con_a [${'a'.repeat(100_000)}`, false],
      ['escalations', '7'.repeat(100_000), false],
    ]
    for (const [name, output, isError] of cases) {
      const started = performance.now()
      hunchOutcome(name, output, isError)
      expect([name, output.slice(0, 8), performance.now() - started < 200]).toEqual([name, output.slice(0, 8), true])
    }

    // A merge verdict's scope: 100k characters of openers in under 50 ms, and a real one still reads.
    hunchOutcome('merge_verdict', '(scope: a)', false)
    const started = performance.now()
    hunchOutcome('merge_verdict', '(scope: '.repeat(12_500), false)
    expect(performance.now() - started).toBeLessThan(50)
    expect(hunchOutcome('merge_verdict', 'VERDICT: WARN\r\n(scope: staged, 2 file(s))  \r\nmore', false).summary).toBe(
      'verdict WARN · staged, 2 file(s)',
    )

    expect(hunchOutcome('context', '<b>lookup failed</b> for src/a.ts', true).summary).toBe('lookup failed for src/a.ts')
    expect(hunchOutcome('check_constraints', `${'\n'.repeat(1000)}  • con_x1 [warning] Never X.`, false).summary).toBe('1 constraint (1 warning)')
    expect(hunchOutcome('escalations', "2 decisions need the human's call\n- a\n- b\n- c", false).summary).toBe('2 need your decision')
  })
})

describe('hunch CLI scan', () => {
  test('long whitespace runs and huge commands stay fast, and are still read', async () => {
    const started = Date.now()
    expect(hunchCli(`echo start${'\n \t\n'.repeat(200_000)}hunch update`)).toBeUndefined()
    expect(hunchCli(`echo a;${' '.repeat(400_000)}`)).toBeUndefined()
    expect(hunchCli(`cat <<EOF\n${'x\n'.repeat(500_000)}EOF\nhunch update`)).toBeUndefined()
    expect(Date.now() - started).toBeLessThan(2000)

    // Within the scanned head, padded commands read as before.
    expect(hunchCli(`cd repo${'\n\n   \n'.repeat(1000)}hunch   update`)?.name).toBe('cli update')
    expect(hunchCli(`hunch update ${'#'.repeat(50_000)}`)?.name).toBe('cli update')
    expect(hunchCli('hunch  task   verify htask_a0a4770f5592438f23dad47a --   npm  test  2>&1 | tail -3')).toEqual({
      name: 'verify',
      target: 'npm test',
      taskId: 'htask_a0a4770f5592438f23dad47a',
    })
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

  test('a bar with no room draws nothing instead of throwing', async () => {
    expect(bar(0.5, 4)).toBe('██░░')
    expect(bar(0.5, 0)).toBe('')
    expect(bar(0.5, -2)).toBe('')
    expect(bar(2, -2)).toBe('')
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

  test('a line without a stamp does not end today', async () => {
    const lines = parseLines(
      [
        '{"ts": "2026-10-06T09:00:00+0300", "kind": "usage", "feature": "route", "usd": 0.25}',
        '{"kind": "usage", "feature": "route", "usd": 1}',
        '{"ts": "2026-10-06T13:07:01+0300", "feature": "risk_gate", "op": "push", "decision": "deny"}',
        '{"feature": "report_check", "decision": "weak", "subagent_type": "scout"}',
      ].join('\n'),
    )
    const status = jevStatus(lines, '2026-10-06', true)
    expect(status.spendToday).toBe(0.25)
    expect(status.deniesToday).toBe(1)
    expect(status.weakToday).toBe(0)
  })

  test('a disabled Jev reads offline', async () => {
    expect(jevStatus(parseLines(log), '2026-10-06', false).isOnline).toBe(false)
  })

  test('routine shifts and usage rows are not feed entries', async () => {
    expect(summarize({ feature: 'shift', decision: 'continue' })).toBeUndefined()
    expect(summarize({ kind: 'usage', feature: 'route' })).toBeUndefined()
  })
})

describe('report cards are read closed', () => {
  const full = (confidence: string, unverified: string) =>
    parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: ${confidence}\nUNVERIFIED: ${unverified}`)

  test('confidence: high is the whole first clause, not just its leading word', async () => {
    for (const hedge of [
      'highly unsure',
      'highly speculative',
      'highly skeptical',
      'high confidence is not warranted',
      'High-level review only',
      'high? not really',
      'high but shaky',
      'High in theory',
      'high-ish, honestly closer to low',
      // A later clause that takes the level back.
      'high, but shaky',
      'high - not really',
      // Held from earlier rounds.
      'high risk',
      'highly uncertain',
    ]) {
      expect([hedge, full(hedge, 'none').confidence, full(hedge, 'none').isWeak]).toEqual([hedge, 'missing', true])
    }
    for (const level of ['high', 'High.', 'high!', 'Very high', 'Highly confident', 'High confidence', '_high_', 'high (95%)', 'High confidence. All 12 tests pass.']) {
      expect([level, full(level, 'none').confidence, full(level, 'none').isWeak]).toEqual([level, 'high', false])
    }
  })

  test('unverified: all ran only straight after its subject', async () => {
    for (const item of [
      'none - all the e2e tests were disabled so the rest passed',
      'none - all flaky tests ignored and the rest passed',
      'none - all tests failed to pass',
      'none - all tests timed out before one passed',
      'none - every test in the unrun suite passed',
      'none - all checks were faked so they passed',
    ]) {
      expect([item, full('high', item).unverified, full('high', item).isWeak]).toEqual([item, 1, true])
    }
    for (const nothing of [
      'none',
      '_none_',
      'None. All 12 tests pass.',
      'none - all tests ran',
      'none - all tests passed',
      'none - all tests pass',
      'none - all the 12 e2e tests passed',
      'none - all of the unit tests have passed',
      'none - all tests now pass',
      'none - every path ran',
    ]) {
      expect([nothing, full('high', nothing).unverified, full('high', nothing).isWeak]).toEqual([nothing, 0, false])
    }
    // A modal is still an item.
    expect(full('high', 'none - all tests should pass').isWeak).toBe(true)
  })

  test('unverified: a none does not hide fenced items or a closing note that takes it back', async () => {
    const fenced = full('high', 'none\n```\n- e2e suite was NOT run\n- Windows untested\n```')
    expect([fenced.unverified, fenced.isWeak]).toEqual([2, true])
    expect(full('high', 'none\n```\n+ the deploy step\n```').unverified).toBe(1)
    expect(full('high', 'none\n```\nthe migration should be idempotent\n```').unverified).toBe(1)
    const note = full('high', 'none\n\nNote: the Windows path was assumed to work.')
    expect([note.unverified, note.isWeak]).toEqual([1, true])
    // A closing is still not an item; sample output (round nine) and any other prose below the card are.
    expect(full('high', 'none\n\n```\n$ npm test\n59 pass, 0 fail\n```').unverified).toBe(1)
    expect(full('high', 'none\n\nThat is all.').unverified).toBe(0)
    expect(full('high', 'none\n\nFiles changed: src/a.ts').unverified).toBe(1)
  })

  test('a fenced card with prose around it is a sample, not the report', async () => {
    const template = parseCard(
      ['I could not finish the task.', 'The expected format was:', '```', 'RESULT: SHIP', 'EVIDENCE: tests', 'CONFIDENCE: high', 'UNVERIFIED: none', '```', 'Sorry.'].join('\n'),
    )
    expect([template.hasAnyField, template.confidence, template.isWeak]).toEqual([false, 'missing', true])
    // Fenced whole, with only blank lines around it, it is the report.
    const whole = parseCard(['', '```', 'RESULT: SHIP', 'EVIDENCE: exit 0', 'CONFIDENCE: high', 'UNVERIFIED: none', '```', ''].join('\n'))
    expect([whole.missing, whole.confidence, whole.isWeak]).toEqual([[], 'high', false])
  })

  test('a long none list above a long fence is read in linear time', async () => {
    const card = (rows: number) => `RESULT: x\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED:\n${'none\n'.repeat(rows)}\`\`\`\n${'ok\n'.repeat(rows)}\`\`\`\n`
    const started = performance.now()
    // The fenced `ok` lines below the none are items (round nine).
    expect(parseCard(card(4000)).unverified).toBe(1)
    expect(parseCard(card(9000)).unverified).toBe(1)
    expect(performance.now() - started).toBeLessThan(500)
  })
})

describe('redaction fails closed', () => {
  test('URL userinfo is masked to the last @ of the word, whatever follows the host', async () => {
    // A user that is an email.
    expect(redact('git clone https://user@example.com:hunter2@dev.azure.com/org/repo')).toBe('git clone https://***@dev.azure.com/org/repo')
    expect(redact('smtp://alice@gmail.com:S3cretPw@smtp.gmail.com:587')).toBe('smtp://***@smtp.gmail.com:587')
    // A host followed by anything.
    expect(redact('{"cmd":"git clone \\"https://user:p/ss@host\\""}')).toBe('{"cmd":"git clone \\"https://***@host\\""}')
    expect(redact('**https://user:p/ss@host**')).toBe('**https://***@host**')
    expect(redact('https://user:p/ss@host:')).toBe('https://***@host:')
    expect(redact('https://user:p/ss@host!')).toBe('https://***@host!')
    expect(redact('https://user:p/ss@host*')).toBe('https://***@host*')
    expect(redact('https://user:p/ss@host\\share')).toBe('https://***@host\\share')
    expect(redact('https://user:p/ss@[::1]:8080/')).toBe('https://***@[::1]:8080/')
    expect(redact('https://user:pa"ss/w@host/x')).toBe('https://***@host/x')
    // Held: digit-leading passwords, an `@` in the password, a user alone, plain URLs.
    expect(redact('https://user:12345@host/x')).toBe('https://***@host/x')
    expect(redact('https://user:12345/abc@host')).toBe('https://***@host')
    expect(redact('https://user:p@ss/word@host/x')).toBe('https://***@host/x')
    expect(redact('ssh://git@github.com:22/o/r.git')).toBe('ssh://***@github.com:22/o/r.git')
    for (const plain of ['https://[::1]:8080/path', 'https://github.com/@me/repo', 'http://host:8080/a?to=a@b.com', 'http://host:5173/@vite/client']) {
      expect(redact(plain)).toBe(plain)
    }
    for (const word of ['smtp://alice@gmail.com:S3cretPw@smtp.gmail.com:587', '**https://user:p/ss@host**', 'https://user:p/ss@[::1]:8080/']) {
      expect(redact(redact(word))).toBe(redact(word))
    }
  })

  test('a value glued to the quote that closes the name is masked', async () => {
    expect(redact("ssh host 'PGPASSWORD='hunter2' psql'")).toBe("ssh host 'PGPASSWORD='***' psql'")
    expect(redact('echo "password="hunter2')).toBe('echo "password="***')
    expect(redact('echo "token="abc123secretvalue')).toBe('echo "token="***')
    expect(redact("psql 'host=db user=me password='hunter2''")).toBe("psql 'host=db user=me password='***''")
    expect(redact('say "hi; export API_KEY="abc123def456"')).toBe('say "hi; export API_KEY="***"')
    expect(redact("the users' password='hunter2'")).toBe("the users' password='***'")
    expect(redact(redact('echo "password="hunter2'))).toBe('echo "password="***')
    // Held: a quoted pattern stays, and a value outside the quote is still masked.
    for (const plain of ['grep -rn "password=" src/', "rg 'token=' -g '*.py'", 'grep -rn "password:" src/ | head', 'echo "Enter password: "; read x', '(grep -c "token=")']) {
      expect(redact(plain)).toBe(plain)
    }
    expect(redact('grep -rn "password:" src/ && export TOKEN="abc123"')).toBe('grep -rn "password:" src/ && export TOKEN=***')
    expect(redact('PASSWORD="hunter2" ./run; echo "password=" > f')).toBe('PASSWORD=*** ./run; echo "password=" > f')
  })

  test('an empty value after = does not take the next word', async () => {
    for (const plain of ['grep -rn password= src/', "rg token= -g '*.py'", 'export TOKEN=']) {
      expect(redact(plain)).toBe(plain)
    }
    // Held: named values, spaced or not.
    expect(redact('password=hunter2')).toBe('password=***')
    expect(redact('PASSWORD = hunter2')).toBe('PASSWORD = ***')
    expect(redact('password: hunter2')).toBe('password: ***')
    expect(redact('TOKEN="abc123"')).toBe('TOKEN=***')
    expect(redact('docker login -u me -p hunter2 reg')).toBe('docker login -u me -p *** reg')
    expect(redact('curl -u user:hunter2 https://x.io')).toBe('curl -u *** https://x.io')
    expect(redact('redis-cli -a hunter2 ping')).toBe('redis-cli -a *** ping')
    expect(redact('machine github.com login me password hunter2')).toBe('machine github.com login me password ***')
  })
})

describe('hunch CLI shims', () => {
  const TASK = 'htask_a0a4770f5592438f23dad47a'
  const verify = { name: 'verify', target: 'npm test', taskId: TASK }

  test('a path, a quoted launcher, cmd /c and npx flags still read as the CLI', async () => {
    for (const command of [
      `C:\\Users\\x\\npm\\hunch.cmd task verify ${TASK} -- npm test`,
      `.\\node_modules\\.bin\\hunch.cmd task verify ${TASK} -- npm test`,
      `./node_modules/.bin/hunch task verify ${TASK} -- npm test`,
      `& "C:\\Program Files\\nodejs\\hunch.ps1" task verify ${TASK} -- npm test`,
      `& 'C:\\x\\hunch.ps1' task verify ${TASK} -- npm test`,
      `cmd /c hunch.cmd task verify ${TASK} -- npm test`,
      `npx --yes @davesheffer/hunch task verify ${TASK} -- npm test`,
      `npx --yes --package=@davesheffer/hunch hunch task verify ${TASK} -- npm test`,
      `cd repo && C:\\tools\\npx.cmd -y hunch task verify ${TASK} -- npm test`,
    ]) {
      expect([command, hunchCli(command)]).toEqual([command, verify])
    }
    for (const other of ['cat docs/hunch update', 'git commit -m "hunch" update', 'echo "C:\\x\\hunch.cmd" task verify', 'cat hunch.cmd', 'hunch.command update']) {
      expect([other, hunchCli(other)]).toEqual([other, undefined])
    }
  })

  test('2>&1 is cut on its own line, in linear time', async () => {
    expect(hunchCli(`hunch task verify ${TASK} -- npm test 2>&1\necho done`)).toEqual({ ...verify, target: 'npm test echo done' })
    expect(hunchCli(`hunch task verify ${TASK} -- npm test 2>&1 | tail -3`)).toEqual(verify)
    const started = performance.now()
    expect(hunchCli(`hunch task verify ${TASK} -- ${'2>&1 '.repeat(4000)}`)?.target).toBe('')
    expect(hunchCli(`hunch task verify ${TASK} -- npm test ${'2>&1 '.repeat(3990)}`)?.target).toBe('npm test')
    expect(performance.now() - started).toBeLessThan(50)
  })
})

describe('redaction: headers, SQL, joints and value tails', () => {
  /** The masked text, after checking that none of the secrets is left in it and that masking again changes nothing. */
  const hidden = (text: string, ...secrets: string[]): string => {
    const masked = redact(text)
    for (const secret of secrets) expect([text, masked.includes(secret)]).toEqual([text, false])
    expect([text, redact(masked)]).toEqual([text, masked])

    return masked
  }

  test('an Authorization header is masked whatever its scheme', async () => {
    expect(hidden('curl -H "Authorization: ApiKey VnVhQ2ZHY0JDZGJrUQ" https://es:9200', 'VnVhQ2ZHY0JDZGJrUQ')).toBe(
      'curl -H "Authorization: ***" https://es:9200',
    )
    expect(hidden('Authorization: Bot MTk4abc.Cl2FMQ.ZnCjxyz', 'MTk4abc', 'Cl2FMQ', 'ZnCjxyz')).toBe('Authorization: ***')
    for (const scheme of ['SSWS', 'OAuth', 'Key', 'JWT', 'Token', 'AWS4-HMAC-SHA256']) {
      hidden(`Authorization: ${scheme} s3cretValue99`, 's3cretValue99')
      hidden(`curl -H 'Proxy-Authorization: ${scheme} s3cretValue99' http://x`, 's3cretValue99')
    }
    // To the closing quote of the header, or the end of its line; the next line stays.
    expect(hidden('Authorization: ApiKey abc def\nHost: es', 'abc', 'def')).toBe('Authorization: ***\nHost: es')
    expect(hidden('curl -H "X-Api-Key: abc def" https://x.io', 'abc', 'def')).toBe('curl -H "X-Api-Key: ***" https://x.io')
    // Held: prose and code after the name.
    for (const plain of ['Authorization: required by the API', '{"Authorization": f"Bearer {token}"}', 'Authorization: `Bearer ${token}`']) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('SQL passwords are masked inside their quotes', async () => {
    expect(hidden('psql -c "ALTER USER app WITH PASSWORD \'hunter2\'"', 'hunter2')).toBe('psql -c "ALTER USER app WITH PASSWORD \'***\'"')
    expect(hidden("CREATE USER x IDENTIFIED BY 'hunter2'", 'hunter2')).toBe("CREATE USER x IDENTIFIED BY '***'")
    expect(hidden('IDENTIFIED BY "hunter2"', 'hunter2')).toBe('IDENTIFIED BY "***"')
    expect(hidden("alter role app with encrypted password 'hunter2';", 'hunter2')).toBe("alter role app with encrypted password '***';")
    hidden('mysql -e "CREATE USER x IDENTIFIED BY \\"hunter2\\""', 'hunter2')
    hidden("ALTER USER x IDENTIFIED WITH mysql_native_password BY 'hunter2'", 'hunter2')
    // A doubled quote is part of the literal, and one cut short is masked to the end of its line.
    hidden("ALTER USER a PASSWORD 'it''s hunter2'", 'hunter2', 'it')
    expect(hidden("ALTER USER a PASSWORD 'hunter2\nnext", 'hunter2')).toBe("ALTER USER a PASSWORD '***'\nnext")
    // Held: a quote that closes the one the word stands in opens no literal.
    for (const plain of ['grep -i "password " src/', 'the login page password field is required']) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('a hash rocket joins a quoted or colon-led name to a quoted value', async () => {
    expect(hidden("'password' => 'hunter2',", 'hunter2')).toBe("'password' => '***',")
    expect(hidden('"api_key" => "abc123"', 'abc123')).toBe('"api_key" => "***"')
    expect(hidden(':password => "hunter2"', 'hunter2')).toBe(':password => "***"')
    expect(hidden('password: "hunter2"', 'hunter2')).toBe('password: "***"')
    for (const plain of ['token => getToken()', '(password) => "x"', "const f = token => 'x'"]) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('an UPPER_SNAKE name with the secret word in any segment is masked, unless it names a place or a size', async () => {
    expect(hidden('SECRET_KEY_BASE=abc123def456ghi789', 'abc123def456ghi789')).toBe('SECRET_KEY_BASE=***')
    expect(hidden('AWS_SECRET_ACCESS_KEY=hunter2 DB_PASSWORD_PROD=hunter3', 'hunter')).toBe('AWS_SECRET_ACCESS_KEY=*** DB_PASSWORD_PROD=***')
    expect(hidden('export API_TOKEN_STAGING="hunter2"', 'hunter2')).toBe('export API_TOKEN_STAGING=***')
    expect(hidden('DB_PASSWORD_PROD: hunter2', 'hunter2')).toBe('DB_PASSWORD_PROD: ***')
    for (const plain of [
      'PASSWORD_FILE=/run/secrets/x',
      'SECRET_KEY_PATH=/etc/key TOKEN_ID=5 AUTH_URL=http://x SECRET_NAME=n',
      'CREDENTIALS_DIR=/c TOKEN_ENV=prod PASSWORD_LENGTH=8 PASSWORD_MIN_LEN=8 TOKEN_SIZE=32',
      // Not UPPER_SNAKE, or the word is not a segment of its own.
      'password_hash=abc',
      'MAX_TOKENS_OUT=5',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
    // Held: lower-case names that end in the word.
    expect(redact('secret_key=abc auth_token=def aws_secret_access_key=ghi')).toBe('secret_key=*** auth_token=*** aws_secret_access_key=***')
  })

  test('a bare value runs to the next blank, with the quoted parts glued to it', async () => {
    expect(hidden('spring.datasource.password=aB3&xY9;kL', 'aB3', 'xY9', 'kL')).toBe('spring.datasource.password=***')
    expect(hidden('password: aB3|xY9)kL', 'aB3', 'xY9', 'kL')).toBe('password: ***')
    expect(hidden("export PASSWORD=$'hunter2'", 'hunter2')).toBe('export PASSWORD=***')
    expect(hidden('TOKEN=abc"def"', 'abc', 'def')).toBe('TOKEN=***')
    expect(hidden('TOKEN="abc"def run', 'abc', 'def')).toBe('TOKEN=*** run')
    expect(hidden('echo "TOKEN=abc"def"ghi"', 'abc', 'def', 'ghi')).toBe('echo "TOKEN=***')
    expect(hidden('echo "password="aB3&xY9', 'aB3', 'xY9')).toBe('echo "password="***')
    expect(hidden('PASSWORD=~hunter2x', 'hunter2x')).toBe('PASSWORD=***')
    // YAML and properties values run to the end of their line.
    expect(hidden('passphrase: correct horse battery staple', 'correct', 'horse', 'battery', 'staple')).toBe('passphrase: ***')
    expect(hidden('env:\n  DB_PASSWORD: correct horse\r\n  USER: me', 'correct', 'horse')).toBe('env:\n  DB_PASSWORD: ***\r\n  USER: me')
    expect(hidden('{user: me, password: correct horse}', 'correct', 'horse')).toBe('{user: me, password: ***}')
    // A closer that matches what was opened before the name stays; so does the next query parameter.
    expect(hidden('(password=hunter2)', 'hunter2')).toBe('(password=***)')
    expect(hidden('echo "TOKEN=hunter2" > f', 'hunter2')).toBe('echo "TOKEN=***" > f')
    expect(hidden('{"password":12345678,"user":"u"}', '12345678')).toBe('{"password":***,"user":"u"}')
    expect(hidden('[{"a":{"token":hunter2}}]', 'hunter2')).toBe('[{"a":{"token":***}}]')
    expect(hidden('curl -d "user=me&password=hunter2&x=1" http://x', 'hunter2')).toBe('curl -d "user=me&password=***&x=1" http://x')
    // A bracket the value opened itself is part of it.
    expect(hidden('(PASSWORD=Summer(2024)', 'Summer', '2024')).toBe('(PASSWORD=***')
    // Held.
    for (const plain of [
      'grep -rn password= src/',
      "rg token= -g '*.py'",
      'grep "password:" src/',
      'f("password=", pw)',
      '{"pattern":"password="}',
      'PWD=/home/user',
      '[[ $password =~ ^hunter ]]',
      'db.password= hunter2',
      'pass: hunter2',
      'mypass=hunter2 REDISPASS=hunter3',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
    // A line masked twice on its way to the screen keeps what followed the value.
    expect(redact(redact('$env:GITHUB_TOKEN = "hunter2"; gh pr list'))).toBe('$env:GITHUB_TOKEN = ***; gh pr list')
  })

  test('openssl, mongo tools and SQL Server take their password from a flag', async () => {
    expect(hidden('openssl rsa -in k.pem -passin pass:hunter2', 'hunter2')).toBe('openssl rsa -in k.pem -passin pass:***')
    hidden('openssl pkcs12 -export -passout pass:hunter2 -out f', 'hunter2')
    hidden('openssl enc -aes256 -pass pass:hunter2 -in f', 'hunter2')
    hidden('openssl pkcs12 -password pass:hunter2 -in f', 'hunter2')
    hidden('openssl rsa -passin "pass:hunter 2" -in f', 'hunter')
    expect(hidden('mongodump -u admin -p hunter2 --db x', 'hunter2')).toBe('mongodump -u admin -p *** --db x')
    for (const tool of ['mongorestore', 'mongoexport', 'mongoimport', 'mongosh']) hidden(`${tool} -u admin -p hunter2 --db x`, 'hunter2')
    expect(hidden('sqlcmd -S srv -U sa -P hunter2', 'hunter2')).toBe('sqlcmd -S srv -U sa -P ***')
    hidden('bcp db.t out f -S srv -U sa -P hunter2', 'hunter2')
    for (const plain of ['mysql -P 3306 -p', 'ssh -p 22 host', 'sqlcmd -S srv -E && mkdir -P out']) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('URL userinfo: a later URL in the same word, and a password of any length', async () => {
    expect(hidden('A=http://localhost:8080/x?y=1,B=postgres://u:p@ss@h/db', 'p@ss')).toBe('A=http://localhost:8080/x?y=1,B=postgres://***@h/db')
    expect(hidden(`https://user:${'Ab/'.repeat(300)}@host/x`, 'Ab/')).toBe('https://***@host/x')
    expect(hidden(`https://user:${'Ab@'.repeat(300)}x@host`, 'Ab@')).toBe('https://***@host')
    // Held: ambiguous with a port, a path and a fragment.
    for (const plain of ['https://user:12345/@host/path', 'http://localhost:8080/x?y=1,B=http://localhost:9090/z']) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('a secret is masked before the text is cut, at every cut', async () => {
    const url = `https://user:${'Ab/'.repeat(300)}@host`
    // The drawer cut falls inside the password.
    const drawer = capDetail(`${'x '.repeat(1950)}${url}`)
    expect(drawer.includes('Ab/')).toBe(false)
    expect(drawer).toMatch(/more characters\)$/)
    // So does the limit of `redact` itself: the word it falls in is dropped whole.
    const limit = redact(`${'x '.repeat(9900)}${url}`)
    expect(limit.includes('Ab/')).toBe(false)
    expect(limit.endsWith('x …')).toBe(true)
    // And the cuts of a timeline line and of a card's fields.
    expect(label(`${'x'.repeat(20)} ${url}`, 80).includes('Ab/')).toBe(false)
    expect(describeTool('Bash', { command: `${'x '.repeat(990)}${url}` }).includes('Ab/')).toBe(false)
    const card = parseCard(`RESULT: ${'x '.repeat(480)}${url}\nCONFIDENCE: ${'y '.repeat(190)}${url}`)
    expect([card.result.includes('Ab/'), card.confidenceText.includes('Ab/')]).toEqual([false, false])
  })

  test('the new rules read 50k characters in linear time', async () => {
    const units = ['=', ':', '"', "'", '://a:', 'Authorization: ', "PASSWORD '", 'password: ', 'TOKEN=a"', 'SECRET_A_', '(password=x)', '://a:1/@', "'token'=>'", '-passin pass:']
    const slow: string[] = []
    for (const unit of units) {
      // Glued (just under the limit, so it is read, not dropped), spaced, and one to a line.
      const texts = { glued: unit.repeat(Math.ceil(19_000 / unit.length)), spaced: `${unit} `.repeat(Math.ceil(50_000 / (unit.length + 1))), lines: `${unit}\n`.repeat(Math.ceil(50_000 / (unit.length + 1))) }
      for (const [kind, text] of Object.entries(texts)) {
        // The better of two runs: the first 25,000-line card of a cold process pays for the JIT, not for the input.
        const timed = (name: string, run: () => unknown): void => {
          const took = [0, 1].map(() => {
            const started = performance.now()
            run()

            return performance.now() - started
          })
          if (Math.min(...took) >= 200) slow.push(`${JSON.stringify(unit)} ${kind} ${name}: ${took.map(Math.round).join(' and ')} ms`)
        }
        timed('redact', () => redact(text))
        timed('confidence', () => parseCard(`RESULT: x\nEVIDENCE: y\nCONFIDENCE: high, ${text}\nUNVERIFIED: none`))
        timed('below the card', () => parseCard(`RESULT: x\nEVIDENCE: y\nCONFIDENCE: high\nUNVERIFIED: none\n\n${text}`))
      }
    }
    expect(slow).toEqual([])
  })
})

describe('report cards: round five', () => {
  const full = (confidence: string, unverified = 'none') => parseCard(`RESULT: done\nEVIDENCE: ran tests, exit 0\nCONFIDENCE: ${confidence}\nUNVERIFIED: ${unverified}`)

  test('a high that any later clause or line hedges is no level', async () => {
    for (const hedged of [
      'high (all tests pass), except the Windows path',
      'high (parser), but the installer is untested',
      'high, assuming the migration is idempotent',
      'high, unless CI differs',
      'high, lower for the installer',
      'high, moderate on Windows',
      'high; uncertain about the Windows path',
      'high, with caveats',
      'high, mostly guesswork',
      'high. E2E was left out.',
      'high\nThe Windows path is a guess',
      'high (60%)',
      'high (89.9% sure)',
      'high (all tests pass) if the cache is warm',
      'high - the e2e suite was skipped',
      'high (partly verified)',
      'high (100% of the 12 tests pass)',
    ]) {
      expect([hedged, full(hedged).confidence, full(hedged).isWeak]).toEqual([hedged, 'missing', true])
    }
    for (const level of ['high', 'high (95%)', 'Very high (all 12 tests pass)', 'High confidence. All 12 tests pass.']) {
      expect([level, full(level).confidence, full(level).isWeak]).toEqual([level, 'high', false])
    }
  })

  test('only emphasis is stripped from the level', async () => {
    for (const hedged of ['~high', '50% high', '~~high~~', '≈ high', '>high', '(high)?']) {
      expect([hedged, full(hedged).confidence, full(hedged).isWeak]).toEqual([hedged, 'missing', true])
    }
    for (const level of ['_high_', '**high**', '*high*', '`high`', ' high. ']) {
      expect([level, full(level).confidence, full(level).isWeak]).toEqual([level, 'high', false])
    }
  })

  test('prose below the card that takes it back is an unverified item, whichever field is last', async () => {
    const last = parseCard('RESULT: done\nEVIDENCE: tests\nUNVERIFIED: none\nCONFIDENCE: high\n\nHowever, I could not run the e2e suite.')
    expect([last.unverified, last.isWeak, weakReason(last)]).toEqual([1, true, '1 unverified'])
    const evidence = parseCard('RESULT: done\nCONFIDENCE: high\nUNVERIFIED: none\nEVIDENCE: tests\n\nNote: the Windows path was assumed to work.')
    expect([evidence.unverified, evidence.isWeak]).toEqual([1, true])
    // Also below closing prose that ended the last section.
    const later = parseCard('RESULT: done\nEVIDENCE: tests\nCONFIDENCE: high\nUNVERIFIED: none\n\nFiles changed: a.ts\n\nHowever, I could not run the e2e suite.')
    expect([later.unverified, later.isWeak]).toEqual([1, true])
    // Any other prose there is read closed; only a closing is no item.
    const plain = parseCard('RESULT: done\nEVIDENCE: tests\nUNVERIFIED: none\nCONFIDENCE: high\n\nFiles changed: a.ts')
    expect([plain.unverified, plain.isWeak]).toEqual([1, true])
    const closed = parseCard('RESULT: done\nEVIDENCE: tests\nUNVERIFIED: none\nCONFIDENCE: high\n\nDone.')
    expect([closed.unverified, closed.isWeak]).toEqual([0, false])
  })

  test('a field with nothing under it is missing', async () => {
    const empty = parseCard('RESULT:\nEVIDENCE:\nCONFIDENCE: high\nUNVERIFIED:')
    expect([empty.missing, empty.isWeak, empty.hasAnyField]).toEqual([['RESULT', 'EVIDENCE', 'UNVERIFIED'], true, true])
    const evidence = parseCard('RESULT: done\nEVIDENCE:   \n\nCONFIDENCE: high\nUNVERIFIED: none')
    expect([evidence.missing, weakReason(evidence)]).toEqual([['EVIDENCE'], 'missing EVIDENCE'])
    // An empty UNVERIFIED does not say none.
    const unverified = parseCard('RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED:\n')
    expect([unverified.missing, unverified.isWeak, weakReason(unverified)]).toEqual([['UNVERIFIED'], true, 'missing UNVERIFIED'])
    // Held: evidence in a fenced block or a list is evidence.
    const fenced = parseCard('RESULT: done\nEVIDENCE:\n```\n$ npm test\nexit 0\n```\nCONFIDENCE: high\nUNVERIFIED: none')
    expect([fenced.missing, fenced.isWeak]).toEqual([[], false])
    const listed = parseCard('RESULT: done\nEVIDENCE:\n- exit 0\nCONFIDENCE: high\nUNVERIFIED: none')
    expect([listed.missing, listed.isWeak]).toEqual([[], false])
  })

  test('an indented block below a blank line is a sample; a report indented whole is the report', async () => {
    for (const indent of ['    ', '\t', '      ']) {
      const sample = parseCard(['Expected format:', '', 'RESULT: x', 'EVIDENCE: y', 'CONFIDENCE: high', 'UNVERIFIED: none'].map((line, at) => (at < 2 ? line : indent + line)).join('\n'))
      expect([indent, sample.hasAnyField, sample.confidence, sample.isWeak]).toEqual([indent, false, 'missing', true])
    }
    // It lends no field to the report around it either.
    const around = parseCard(['RESULT: reviewed', 'EVIDENCE: exit 0', 'Their card:', '', '    CONFIDENCE: high', '    UNVERIFIED: none', '', 'CONFIDENCE: medium'].join('\n'))
    expect([around.missing, around.confidence]).toEqual([['UNVERIFIED'], 'medium'])
    const whole = parseCard(['', '    RESULT: x', '    EVIDENCE: exit 0', '', '    CONFIDENCE: high', '    UNVERIFIED: none', ''].join('\n'))
    expect([whole.missing, whole.confidence, whole.result, whole.isWeak]).toEqual([[], 'high', 'x', false])
    // Held: a line set in straight below its header is that section's text, and an item below a blank line still counts.
    const continued = parseCard('RESULT: x\nEVIDENCE:\n    exit 0\nCONFIDENCE: high\nUNVERIFIED: none')
    expect([continued.missing, continued.isWeak]).toEqual([[], false])
    expect(parseCard('RESULT: x\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED:\n- a\n\n    - the Windows path').unverified).toBe(2)
  })

  test('a struck-through none is not none', async () => {
    for (const struck of ['~~none~~', '~~none~~ the Windows path', '- ~~none~~', '~none']) {
      expect([struck, full('high', struck).unverified, full('high', struck).isWeak]).toEqual([struck, 1, true])
    }
  })
})

describe('report cards and redaction: round six', () => {
  const STRONG = 'RESULT: done\nEVIDENCE: mix test exit 0\nCONFIDENCE: high\nUNVERIFIED: none'
  const full = (confidence: string, below = '') =>
    parseCard(`RESULT: done\nEVIDENCE: mix test exit 0\nCONFIDENCE: ${confidence}\nUNVERIFIED: none${below}`)
  const hidden = (shown: string, secret = 'hunter2') => {
    const masked = redact(shown)
    expect([shown, masked.includes(secret)]).toEqual([shown, false])

    return masked
  }

  test('after high only what backs it may follow', async () => {
    for (const hedged of [
      'high (ish)',
      'high, hopefully',
      'high, I think',
      'high, in theory',
      'high, tentatively',
      'high (relatively)',
      'high, more or less',
      'high, with reservations',
      'high, barring flaky CI',
      'high, without running the e2e suite',
      'high - parser; installer unreviewed',
      'high — for the parser alone',
      'high; e2e suite fails',
      'high\nTwo tests fail on Windows.',
      'high (0.6)',
      'high, 6/10',
      'high: 55/100',
      'high, 60 percent',
      'high (50-95%)',
      'high (memory use stays level)',
      'high*',
      `high${', all tests pass'.repeat(9)}`,
    ]) {
      expect([hedged, full(hedged).confidence, full(hedged).isWeak]).toEqual([hedged, 'missing', true])
    }
    for (const level of [
      'high',
      'very high',
      'high confidence',
      '**high**',
      'high.',
      'high (95%)',
      'high - all tests pass',
      'high (9/10)',
      'high, 0.95',
      'high (tests pass)',
      'high (all 59 tests pass, the build is green)',
    ]) {
      expect([level, full(level).confidence, full(level).isWeak]).toEqual([level, 'high', false])
    }
  })

  test('a card that states two confidences keeps the weaker', async () => {
    const spliced = parseCard('CONFIDENCE: low\nRESULT: x\nEVIDENCE: y\nUNVERIFIED: none\nCONFIDENCE: high')
    expect([spliced.missing, spliced.confidence, spliced.isWeak]).toEqual([[], 'low', true])
    const twice = parseCard('RESULT: x\nCONFIDENCE: medium\nCONFIDENCE: high\nEVIDENCE: y\nUNVERIFIED: none')
    expect([twice.confidence, twice.isWeak]).toEqual(['medium', true])
    const unclear = parseCard('CONFIDENCE: high, I think\nRESULT: x\nEVIDENCE: y\nUNVERIFIED: none\nCONFIDENCE: high')
    expect([unclear.confidence, unclear.isWeak, weakReason(unclear)]).toEqual(['missing', true, 'confidence unclear ("high, I think")'])
    // Held: the same level twice is that level, and a new RESULT still starts a card of its own.
    const same = parseCard('CONFIDENCE: high\nRESULT: x\nEVIDENCE: y\nUNVERIFIED: none\nCONFIDENCE: high')
    expect([same.missing, same.confidence, same.isWeak]).toEqual([[], 'high', false])
    const own = parseCard(`RESULT: a\nEVIDENCE: b\nCONFIDENCE: low\nUNVERIFIED: c\n\n${STRONG}`)
    expect([own.confidence, own.isWeak]).toEqual(['high', false])
  })

  test('a caveat between the fields is an item once UNVERIFIED comes', async () => {
    const held = parseCard('RESULT: done\nEVIDENCE: mix test exit 0\nCONFIDENCE: high\n\nHowever, I could not run the e2e suite.\n\nUNVERIFIED: none')
    expect([held.confidence, held.unverified, held.isWeak, weakReason(held)]).toEqual(['high', 1, true, '1 unverified'])
    const apart = parseCard('RESULT: done\n\nEVIDENCE: exit 0\n\nCONFIDENCE: high\n\nNote: the e2e suite was not run.\n\nUNVERIFIED: none')
    expect([apart.unverified, apart.isWeak]).toEqual([1, true])
    const early = parseCard('RESULT: done\n\nThe Windows path is still untested.\n\nEVIDENCE: exit 0\nCONFIDENCE: high\nUNVERIFIED: none')
    expect([early.unverified, early.isWeak]).toEqual([1, true])
    // A caveat above another card is not this card's.
    const other = parseCard(`RESULT: first try\n\nHowever, that could not run.\n\n${STRONG}`)
    expect([other.unverified, other.isWeak]).toEqual([0, false])
    // Held: fields set apart by blank lines alone are a strong card.
    const spaced = parseCard('RESULT: done\n\nEVIDENCE: exit 0\n\nCONFIDENCE: high\n\nUNVERIFIED: none')
    expect([spaced.missing, spaced.unverified, spaced.isWeak]).toEqual([[], 0, false])
  })

  test('prose below the card is an item unless it is a closing', async () => {
    for (const below of [
      'Caveat: the Windows path is a guess.',
      'Two things remain open: the Windows path and the migration.',
      'Note: two tests fail on main.',
      'Files changed: a.ts',
      '## Notes',
      `${'x'.repeat(300)}`,
    ]) {
      expect([below, full('high', `\n\n${below}`).unverified, full('high', `\n\n${below}`).isWeak]).toEqual([below, 1, true])
    }
    for (const list of ['Remaining risks:\n- Windows path\n- migration', 'Open items:\n- Windows path never exercised\n- migration']) {
      expect([list, full('high', `\n\n${list}`).unverified, full('high', `\n\n${list}`).isWeak]).toEqual([list, 2, true])
    }
    for (const closing of ['Done.', 'That is all.', "That's all!", 'Thanks.', '**Done.**', '---', '***', '___', 'All 12 tests passed.', '', '  \n\n']) {
      expect([closing, full('high', `\n\n${closing}`).unverified, full('high', `\n\n${closing}`).isWeak]).toEqual([closing, 0, false])
    }
    // A closing that takes something back is no closing.
    expect(full('high', '\n\nDone, but all tests were not run.').isWeak).toBe(true)
  })

  test('the tail of a long answer is graded as what it is in the whole answer', async () => {
    const fence = '```'
    const own = 'My own card:\n\nRESULT: failed\nEVIDENCE: y\nCONFIDENCE: low\nUNVERIFIED: everything\n'
    const filler = 'filler line\n'.repeat(4200)
    // The scout's strong card is quoted in a fence that opens before the cut and closes after it.
    const answer = `intro\n\nThe scout sent this:\n\n${fence}\n${filler}${STRONG}\n\nThat is all.\n${fence}\n\n${own}`
    expect(answer.length).toBe(50_605)
    const whole = parseCard(answer)
    expect([whole.confidence, whole.unverified, whole.isWeak]).toEqual(['low', 1, true])
    const tail = parseAnswer(answer)
    expect([tail.confidence, tail.unverified, tail.isWeak]).toEqual(['low', 1, true])
    // The bare cut, graded alone, ends inside a fence that holds a header: weak whatever it quotes.
    expect(parseCard(answer.slice(-50_000)).isWeak).toBe(true)
    expect(parseCard(`${STRONG}\n\nThat is all.\n${fence}\n\n${own}`).isWeak).toBe(true)

    // The other way round: a strong card below a long fenced log stays strong, and a short answer is read whole.
    const logged = parseAnswer(`Log:\n\n${fence}\n${filler}${fence}\n\n${STRONG}`)
    expect([logged.missing, logged.confidence, logged.isWeak]).toEqual([[], 'high', false])
    expect(parseAnswer(STRONG).isWeak).toBe(false)
    // A sample fenced below prose is a sample, however long the fence: the prose is in the part cut off.
    const sample = parseAnswer(`The expected format was:\n${fence}\n${filler}${STRONG}\n${fence}`)
    expect([sample.hasAnyField, sample.isWeak]).toEqual([false, true])
    // So is one in an indented block that the cut falls in.
    const indented = parseAnswer(`Expected format:\n\n${'    filler line\n'.repeat(3200)}${STRONG.replace(/^/gm, '    ')}\n`)
    expect([indented.hasAnyField, indented.isWeak]).toEqual([false, true])
    // A nested shorter fence in the part cut off does not close the longer one.
    const nested = parseAnswer(`Quoted:\n\n${fence}${fence}\n${fence}\n${filler}${STRONG}\n${fence}${fence}\n\n${own}`)
    expect([nested.confidence, nested.isWeak]).toEqual(['low', true])
    // The cut is made at the start of a line, and a huge answer is still read in linear time.
    expect(parseAnswer(`${'x'.repeat(60)}RESULT: a\nEVIDENCE: b\nCONFIDENCE: high\nUNVERIFIED: none`, 70).missing).toEqual(['RESULT'])
    const started = performance.now()
    expect(parseAnswer(`${`${fence}\nfiller\n`.repeat(200_001)}${STRONG}`).hasAnyField).toBe(false)
    expect(performance.now() - started).toBeLessThan(1500)
  })

  test('a flag value ends at the next blank, quoted parts and all', async () => {
    expect(redact("mysql -uroot -p$'hunter2' db")).toBe('mysql -uroot -p*** db')
    expect(redact("docker login -u me -p $'hunter2'")).toBe('docker login -u me -p ***')
    expect(redact("redis-cli -a $'hunter2' ping")).toBe('redis-cli -a *** ping')
    expect(redact("mongosh -u a -p ab'hunter2'")).toBe('mongosh -u a -p ***')
    expect(redact('sshpass -p \'ab\'"hunter2" ssh h')).toBe('sshpass -p *** ssh h')
    expect(redact('sqlcmd -S s -U sa -P "ab"\'hunter2\'')).toBe('sqlcmd -S s -U sa -P ***')
    expect(redact('mysql -uroot -p"ab\\"hunter2" db')).toBe('mysql -uroot -p*** db')
    expect(redact("curl -u admin:'hunter2' https://x")).toBe('curl -u *** https://x')
    expect(redact('curl -u "admin":"hunter2" https://x')).toBe('curl -u *** https://x')
    hidden("mysql -uroot -p'it'\\x27'hunter2' db")
    hidden("mysql -uroot -p$'it\\'s hunter2' db")
    // A quote left open runs to the end of its line, not past it.
    expect(redact('mysql -uroot -p"hunter2 db\nnext line')).toBe('mysql -uroot -p***\nnext line')
  })

  test('a continued line is one command', async () => {
    expect(redact('curl https://api.example.com \\\n  -H "Accept: json" \\\n  -u admin:hunter2')).toBe(
      'curl https://api.example.com \\\n  -H "Accept: json" \\\n  -u ***',
    )
    expect(redact('mysql -u root \\\n  -phunter2 mydb')).toBe('mysql -u root \\\n  -p*** mydb')
    expect(redact('docker login r \\\n  -u me \\\n  -p hunter2')).toBe('docker login r \\\n  -u me \\\n  -p ***')
    expect(redact('redis-cli -h host \\\n  -a hunter2 ping')).toBe('redis-cli -h host \\\n  -a *** ping')
    expect(redact('sqlcmd -S srv -U sa \\\n  -P hunter2')).toBe('sqlcmd -S srv -U sa \\\n  -P ***')
    // CRLF, PowerShell's backtick, a flag at the start of the line, a quoted argument on the way.
    hidden('docker login r \\\r\n  -u me \\\r\n  -p hunter2')
    hidden('docker login r `\r\n  -u me `\r\n  -p hunter2')
    hidden('mysql -u root \\\n-phunter2 mydb')
    hidden('mysql -e "select 1" \\\n  -u root \\\n  -phunter2')
    // Held: a line that is not continued ends the command.
    expect(redact('docker login registry\nmkdir -p out')).toBe('docker login registry\nmkdir -p out')
    expect(redact('docker login registry && \\\n  mkdir -p out')).toBe('docker login registry && \\\n  mkdir -p out')
  })

  test('an openssl pass phrase and a cookie header are masked whole', async () => {
    expect(redact("openssl rsa -passin pass:'ab hunter2'")).toBe('openssl rsa -passin pass:***')
    expect(redact('openssl enc -aes-256-cbc -pass pass:"correct hunter2" -in f')).toBe('openssl enc -aes-256-cbc -pass *** -in f')
    expect(redact('openssl rsa -passin pass:hunter2 -in key.pem')).toBe('openssl rsa -passin pass:*** -in key.pem')
    expect(redact('Cookie: theme="dark"; sessionid=hunter2')).toBe('Cookie: ***')
    expect(redact(`curl -H "Cookie: a=${'x'.repeat(2100)}; sid=hunter2" http://x`)).toBe('curl -H "Cookie: ***" http://x')
    expect(redact("curl -H 'Cookie: a=\"b\"; sid=hunter2' http://x")).toBe("curl -H 'Cookie: ***' http://x")
    expect(redact('Set-Cookie: sid=hunter2; Path=/\nnext line')).toBe('Set-Cookie: ***\nnext line')
    // Too far into its line to tell which quote it stands in: masked to the end of the line.
    hidden(`curl ${'-v '.repeat(200)}-H "Cookie: a="b"; sid=hunter2" http://x`)
  })

  test('credentials in code, XML, Kubernetes and DSN forms are masked', async () => {
    expect(redact("app.config['SECRET_KEY'] = 'hunter2'")).toBe("app.config['SECRET_KEY'] = ***")
    expect(redact('os.environ["DB_PASSWORD"] = "hunter2"')).toBe('os.environ["DB_PASSWORD"] = ***')
    expect(redact('process.env.SECRET_KEY_BASE = "hunter2"')).toBe('process.env.SECRET_KEY_BASE = ***')
    expect(redact('secret_key_base: hunter2abc')).toBe('secret_key_base: ***')
    expect(redact('conn.setPassword("hunter2")')).toBe('conn.setPassword("***")')
    expect(redact("builder.password('hunter2').build()")).toBe("builder.password('***').build()")
    expect(redact('client.set_api_key("hunter2")')).toBe('client.set_api_key("***")')
    expect(redact('<password>hunter2</password>')).toBe('<password>***</password>')
    expect(redact('<db.apiKey enc="no">hunter2</db.apiKey>')).toBe('<db.apiKey enc="no">***</db.apiKey>')
    expect(redact('- name: DB_PASSWORD\n  value: hunter2')).toBe('- name: DB_PASSWORD\n  value: ***')
    expect(redact('- name: "SECRET_KEY_BASE"\r\n  value: "hunter2"')).toBe('- name: "SECRET_KEY_BASE"\r\n  value: ***')
    expect(redact('req.Header.Set("Authorization", "Basic dXNlcjpodW50ZXIy")')).toBe('req.Header.Set("Authorization", "***")')
    expect(redact("xhr.setRequestHeader('X-Api-Key', 'hunter2')")).toBe("xhr.setRequestHeader('X-Api-Key', '***')")
    expect(redact('headers["Authorization"] = "ApiKey hunter2"')).toBe('headers["Authorization"] = ***')
    expect(redact('root:hunter2@tcp(127.0.0.1:3306)/db')).toBe('root:***@tcp(127.0.0.1:3306)/db')
    expect(redact('Sqlcmd -S srv -U sa -P hunter2')).toBe('Sqlcmd -S srv -U sa -P ***')
    expect(redact('BCP db.t out f -U sa -P hunter2')).toBe('BCP db.t out f -U sa -P ***')

    // Held: names that only look alike, reads and comparisons stay as they are.
    for (const plain of [
      '- name: LOG_LEVEL\n  value: debug',
      '- name: PASSWORD_FILE\n  value: /run/secrets/x',
      '<compass>north</compass>',
      '<title>password</title>',
      'if (d["password"] == other) return',
      'x = os.environ["DB_PASSWORD"]',
      'token_count: 12',
      'os.getenv("HOME", "x")',
      'conn.getPassword("x")',
      'host:8080@tcp',
      'sqlcmd -S srv -p 5',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('the new shapes are read in linear time', async () => {
    const started = performance.now()
    for (const long of [
      'a:'.repeat(10_000),
      'a.b:c@'.repeat(3300),
      '"a", '.repeat(4000),
      `"${'password'.repeat(4)}", `.repeat(500),
      '<a '.repeat(6600),
      '<password '.repeat(2000),
      'name: x\n'.repeat(2500),
      'cookie: a=b; '.repeat(1500),
      '.password('.repeat(2000),
      "-p$'a".repeat(4000),
      'mysql \\\n'.repeat(2500),
      'login "a" \\\n'.repeat(1600),
      `CONFIDENCE: high${', all tests pass'.repeat(3000)}`,
    ]) {
      const once = performance.now()
      redact(long)
      parseCard(long)
      expect([long.slice(0, 20), performance.now() - once < 400]).toEqual([long.slice(0, 20), true])
    }
    expect(performance.now() - started).toBeLessThan(1500)
  })
})

describe('report cards and redaction: round seven', () => {
  const S = 'hunter2xyz'
  const hidden = (shown: string) => {
    const masked = redact(shown)
    const labelled = label(shown, 400)
    expect([shown, masked.includes('hunter2'), labelled.includes('hunter2')]).toEqual([shown, false, false])

    return masked
  }

  test('a -Password flag is not a glued -P value', async () => {
    expect(hidden(`Invoke-Sqlcmd -ServerInstance s -Username sa -Password ${S} -Query "select 1"`)).toBe(
      'Invoke-Sqlcmd -ServerInstance s -Username sa -Password *** -Query "select 1"',
    )
    hidden(`Invoke-Sqlcmd -ServerInstance s -Username sa -password ${S}`)
    hidden(`docker login r -u me -password ${S}`)
    expect(hidden(`sqlcmd -S s -U sa -P ${S}`)).toBe('sqlcmd -S s -U sa -P ***')
    expect(hidden(`sqlcmd -S s -U sa -P${S}`)).toBe('sqlcmd -S s -U sa -P***')
  })

  test('every line between the fields that is not a closing is an unverified item once UNVERIFIED comes', async () => {
    const head = 'RESULT: done\nEVIDENCE: mix test, exit 0\nCONFIDENCE: high\n\n'
    for (const answer of [
      `${head}Two tests fail on main.\n\nUNVERIFIED: none`,
      `${head}Caveat: two tests fail on main.\n\nUNVERIFIED: none`,
      `${head}Risk: the migration is a guess.\n\nUNVERIFIED: none`,
      `${head}(e2e failing on Windows)\n\nUNVERIFIED: none`,
      `${head}Known issue: flaky on CI.\n\nUNVERIFIED: none`,
      `${head}Only the Linux path was exercised.\n\nUNVERIFIED: none`,
      'RESULT: done\n\nEVIDENCE: mix test exit 0\n\nThe integration suite is failing on main.\n\nCONFIDENCE: high\nUNVERIFIED: none',
      'RESULT: partial\n\nCONFIDENCE: high\nEVIDENCE: x\n\ne2e suite broken, 3 failures.\n\nUNVERIFIED: none',
    ]) {
      const card = parseCard(answer)
      expect([answer, card.isWeak, card.unverified >= 1]).toEqual([answer, true, true])
    }
    // A closing between the fields stays nothing.
    const closed = parseCard(`${head}---\n\nUNVERIFIED: none`)
    expect([closed.isWeak, closed.unverified]).toEqual([false, 0])
  })

  test('a level that trails off is not stated', async () => {
    for (const level of ['high...', 'high…']) {
      const card = parseCard(`RESULT: done\nEVIDENCE: exit 0\nCONFIDENCE: ${level}\nUNVERIFIED: none`)
      expect([level, card.confidence, card.isWeak]).toEqual([level, 'missing', true])
    }
  })

  test('a sed replacement masks the value it writes', async () => {
    for (const command of [
      `sed -i "s|password: .*|password: ${S}|" config.yml`,
      `sed -i s/password: .*/password: ${S}/ config.yml`,
      `sed -i 's#password: .*#password: ${S}#' config.yml`,
      `sed -i "s|password = .*|password = ${S}|" config.yml`,
      `sed -i 's/"password": ".*"/"password": "${S}"/' f.json`,
      `sed -i "s|PASSWORD=.*|PASSWORD=${S}|" .cfg`,
    ]) {
      hidden(command)
    }
  })

  test('what a command hands to a secret store is masked', async () => {
    for (const command of [
      `gh secret set NAME --body "${S}"`,
      `gh secret set NAME -b ${S}`,
      `gh secret set NAME --body=${S} --repo o/r`,
      `aws secretsmanager create-secret --name n --secret-string ${S}`,
      `aws secretsmanager put-secret-value --secret-id n --secret-string '{"k":"${S}"}'`,
      `aws ssm put-parameter --name /prod/db --value ${S} --type SecureString`,
      `aws ssm put-parameter --name /prod/db --type SecureString --value "${S}"`,
      `az keyvault secret set --vault-name v --name n --value ${S}`,
      `az appconfig kv set --name db-password --value ${S}`,
      `setx DB_PASSWORD ${S}`,
      `setx GITHUB_TOKEN "${S}" /M`,
      `dotnet user-secrets set "Db:Password" "${S}"`,
      `dotnet user-secrets set "Db:Password" "${S}" --project p`,
      `npm config set //registry.npmjs.org/:_authToken ${S}`,
      `npm config set _auth ${S}`,
      `npm config set //r.example.com/:_password ${S}`,
      `pulumi config set --secret NAME ${S}`,
      `pulumi config set NAME ${S} --secret`,
      `rclone config create r s3 access_key_id AK secret_access_key ${S}`,
      `rclone config create r sftp host h pass ${S}`,
      `rclone config create r sftp host h password ${S}`,
      `heroku config:set FOO=${S}`,
      `fly secrets set FOO=${S}`,
    ]) {
      hidden(command)
    }
    expect(hidden(`pulumi config set --secret NAME ${S}`)).toBe('pulumi config set --secret NAME ***')
    for (const plain of [
      'git config set user.name David',
      'git config --global core.autocrlf false',
      'npm config set registry https://registry.npmjs.org/',
      'docker run --name web -p 8080:80 nginx',
      'aws s3 ls --name x --value y',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('a cookie handed to curl is masked', async () => {
    for (const command of [`curl --cookie "sessionid=${S}" http://x`, `curl -b 'connect.sid=${S}' http://x`, `curl -sSb sid=${S} http://x`]) {
      hidden(command)
    }
  })

  test('an UPPER_SNAKE name ending in KEY holds a credential, past the exemptions', async () => {
    for (const text of [
      `APP_KEY=base64:${S}`,
      `DD_APP_KEY=${S}`,
      `export HMAC_KEY=${S}`,
      `heroku config:set JWT_SIGNING_KEY=${S} COSMOS_KEY=${S}`,
      `COSMOS_KEY: ${S}`,
    ]) {
      hidden(text)
    }
    for (const plain of [
      'export PUBLIC_KEY=abc PRIMARY_KEY=id SORT_KEY=ts PARTITION_KEY=pk CACHE_KEY=c IDEMPOTENCY_KEY=i',
      'STRIPE_PUBLISHABLE_KEY=pk_x APP_PUBLIC_KEY=x',
    ]) {
      expect(redact(plain)).toBe(plain)
    }
  })

  test('a header value runs to the end of its command substitution', async () => {
    hidden(`curl -H "Authorization: Basic $(echo -n "me:${S}" | base64)" https://x`)
  })

  test('every credential flag on one curl is masked', async () => {
    for (const command of [
      `curl -u user:pw1 --proxy-user puser:${S} https://x`,
      `curl --proxy-user puser:pw1 -u user:${S} https://x`,
      `curl -U puser:pw1 -u user:${S} https://x`,
      `curl -u user:pw1 -U puser:${S} https://x`,
    ]) {
      expect(hidden(command).includes('pw1')).toBe(false)
    }
  })

  test('a value that starts with a shell separator is still masked', async () => {
    for (const lead of ['&', ';', '|', ')']) {
      expect(hidden(`DB_PASSWORD=${lead}${S}`)).toBe('DB_PASSWORD=***')
    }
  })

  test('a label masks a value on the line below its name before it flattens the lines', async () => {
    expect(label(`- name: DB_PASSWORD\n  value: ${S}`, 400).includes('hunter2')).toBe(false)
  })

  // The 50k timing test above, for this round's shapes: a test of its own, as the harness gives each test 5 s.
  test('the round seven shapes read 50k characters in linear time', async () => {
    const units = ['secret set a ', '--value ', 'DD_KEY=', '--cookie ', '/pass:', 's|password: ', '"Authorization: $(', 'PASSWORD=&', '-Password ', 'secret_access_key ']
    const slow: string[] = []
    for (const unit of units) {
      // Glued (just under the limit, so it is read, not dropped), spaced, and one to a line.
      const texts = { glued: unit.repeat(Math.ceil(19_000 / unit.length)), spaced: `${unit} `.repeat(Math.ceil(50_000 / (unit.length + 1))), lines: `${unit}\n`.repeat(Math.ceil(50_000 / (unit.length + 1))) }
      for (const [kind, text] of Object.entries(texts)) {
        // The better of two runs, as above.
        const timed = (name: string, run: () => unknown): void => {
          const took = [0, 1].map(() => {
            const started = performance.now()
            run()

            return performance.now() - started
          })
          if (Math.min(...took) >= 200) slow.push(`${JSON.stringify(unit)} ${kind} ${name}: ${took.map(Math.round).join(' and ')} ms`)
        }
        timed('redact', () => redact(text))
        timed('confidence', () => parseCard(`RESULT: x\nEVIDENCE: y\nCONFIDENCE: high, ${text}\nUNVERIFIED: none`))
        timed('below the card', () => parseCard(`RESULT: x\nEVIDENCE: y\nCONFIDENCE: high\nUNVERIFIED: none\n\n${text}`))
      }
    }
    expect(slow).toEqual([])
  })

  test('Windows credential shapes', async () => {
    for (const command of [
      `cmdkey /add:srv /user:me /pass:${S}`,
      `psexec \\\\srv -u me -p ${S} cmd`,
      `schtasks /create /tn t /tr c /ru me /rp ${S}`,
      `xfreerdp /u:me /p:${S} /v:host`,
      `net use \\\\srv\\share ${S} /user:me`,
      `net use Z: \\\\srv\\share ${S} /user:me`,
      `net user admin ${S} /add`,
    ]) {
      hidden(command)
    }
  })
})

describe('report cards and redaction: round eight', () => {
  const S = 'hunter2xyz'
  const hidden = (shown: string) => {
    const masked = redact(shown)
    const labelled = label(shown, 400)
    expect([shown, masked.includes('hunter2'), labelled.includes('hunter2')]).toEqual([shown, false, false])

    return masked
  }

  test('a secret store value on a continued line is masked', async () => {
    const BS = '\\'
    for (const mark of [BS, '`']) {
      hidden(`gh secret set NAME ${mark}\n  --body ${S}`)
      hidden(`gh secret set NAME ${mark}\r\n  --body ${S}`)
    }
    hidden(`gh secret set NAME ${BS}\n  -b ${S}`)
    hidden(`gh secret set NAME --repo o/r ${BS}\n  --body ${S}`)
    hidden(`pulumi config set --secret ${BS}\n  apiThing ${S}`)
    hidden(`dotnet user-secrets set ${BS}\n  Api:Thing ${S}`)
    hidden(`setx API_TOKEN ${BS}\n  ${S}`)
    expect(hidden(`gh secret set NAME ${BS}\n  --body ${S}`)).toBe(`gh secret set NAME ${BS}\n  --body ***`)
  })

  test('a quoted, indented or fenced line among the fields is never graded strong', async () => {
    const head = 'RESULT: done\nEVIDENCE: mix test, exit 0\nCONFIDENCE: high\n'
    for (const answer of [
      `${head}\n> Two tests fail on main.\n\nUNVERIFIED: none`,
      `${head}\n    Two tests fail on main.\n\nUNVERIFIED: none`,
      `${head}\n\tNote: the e2e suite was not run.\n\nUNVERIFIED: none`,
      `${head}> but the e2e suite is failing\nUNVERIFIED: none`,
      `${head}> (for the parser only)\nUNVERIFIED: none`,
      `${head}\n\`\`\`\nCaveat: e2e not run\n\`\`\`\n\nUNVERIFIED: none`,
      'RESULT: done\nEVIDENCE: x\nUNVERIFIED: none\nCONFIDENCE: high\n\n> However the e2e suite was not run.',
      'RESULT: done\nEVIDENCE: x\nUNVERIFIED: none\nCONFIDENCE: high\n\n    However the e2e suite was not run.',
      'RESULT: done\n\n> Two tests fail on main.\n\nEVIDENCE: x\nCONFIDENCE: high\nUNVERIFIED: none',
      'RESULT: done\nEVIDENCE: x\n\n    The integration suite is failing on main.\n\nCONFIDENCE: high\nUNVERIFIED: none',
      'RESULT: done\nEVIDENCE: mix test, exit 0\n```\nCaveat: e2e not run\n```\nCONFIDENCE: high\nUNVERIFIED: none',
    ]) {
      const card = parseCard(answer)
      expect([answer, card.isWeak]).toEqual([answer, true])
    }
  })
})

describe('report cards and redaction: round nine', () => {
  const S = 'hunter2xyz'
  const BS = '\\'
  const pad = (n: number) => ' '.repeat(n)
  const hidden = (shown: string) => {
    const masked = redact(shown)
    const outs = [masked, label(shown, 400), toolDetail('Bash', { command: shown }), describeTool('Bash', { command: shown })]
    expect([shown, outs.some(out => out.includes('hunter2'))]).toEqual([shown, false])

    return masked
  }

  test('a store value on a deeply indented continuation line is masked', async () => {
    for (const mark of [BS, '`']) {
      for (const eol of ['\n', '\r\n']) {
        hidden(`gh secret set NAME ${mark}${eol}${pad(23)}--body ${S}`)
        hidden(`gh secret set NAME ${mark}${eol}${pad(30)}--body ${S}`)
        hidden(`gh secret set NAME --body ${mark}${eol}${pad(30)}${S}`)
        hidden(`        setx API_TOKEN ${mark}${eol}${pad(30)}${S}`)
        hidden(`        pulumi config set --secret ${mark}${eol}${pad(26)}key ${S}`)
      }
    }
    expect(hidden(`gh secret set NAME ${BS}\n${pad(30)}--body ${S}`)).toBe(`gh secret set NAME ${BS}\n${pad(30)}--body ***`)
  })

  test('words a store command was not read to are masked to the end of the line', async () => {
    const pairs = Array.from({ length: 70 }, (_, i) => `K${i}=${i === 69 ? S : `v${i}`}`)
    hidden(`fly secrets set ${pairs.join(' ')}`)
    hidden(`fly secrets set${pairs.map(pair => ` ${BS}\n  ${pair}`).join('')}`)
    hidden(`gh secret set NAME${pad(300)}--body ${S}`)
    hidden(`gh secret set NAME ${`${BS}\n  `.repeat(12)}--body ${S}`)
    hidden(`gh secret set NAME ${BS}\n  --body ${S}`)
    // A separator still ends the command: what follows it is another one.
    expect(redact(`gh secret set NAME --body ${S} && echo done`)).toBe('gh secret set NAME --body *** && echo done')
    expect(redact(`gh secret set NAME --body ${S}${pad(300)}| tee log`)).toBe(`gh secret set NAME --body ***${pad(300)}| tee log`)
  })

  test('a --value or --secret-string value on the continued line is masked', async () => {
    for (const eol of ['\n', '\r\n']) {
      hidden(`aws ssm put-parameter --name /prod/db --type SecureString --value ${BS}${eol}  ${S}`)
      hidden(`aws ssm put-parameter ${BS}${eol}--name /prod/db ${BS}${eol}--value ${BS}${eol}${S} ${BS}${eol}--type SecureString`)
      hidden(`aws secretsmanager create-secret --name n --secret-string ${BS}${eol}  ${S}`)
      hidden(`aws secretsmanager put-secret-value --secret-id n --secret-string ${BS}${eol}  ${S}`)
      hidden(`aws secretsmanager create-secret --name n --secret-string ${pad(30)}${S}`)
    }
  })

  test('--secret at the start of a continuation line makes the command a store', async () => {
    for (const eol of ['\n', '\r\n']) hidden(`pulumi config set ${BS}${eol}--secret region ${S}`)
  })

  test('a backtick glued to the word before the newline is a continuation', async () => {
    for (const eol of ['\n', '\r\n']) {
      expect(hidden(`gh secret set NAME\`${eol} --body ${S}`)).toBe(`gh secret set NAME\`${eol} --body ***`)
    }
  })

  const F = '```'
  const R = 'RESULT: done'
  const E = 'EVIDENCE: mix test, exit 0'
  const C = 'CONFIDENCE: high'
  const U = 'UNVERIFIED: none'
  const fence = (...lines: string[]) => [F, ...lines, F]
  const weak = (lines: string[]) => {
    for (const eol of ['\n', '\r\n']) {
      const answer = lines.join(eol)
      expect([answer, parseCard(answer).isWeak]).toEqual([answer, true])
    }
  }

  test('a card read out of a block is weak with a later block below it', async () => {
    const caveat = 'Caveat: e2e suite was not run'
    weak([...fence(R, E, C, U), '', ...fence(caveat)])
    weak([...fence(R, E, C, U), '', `    ${caveat}`])
    weak([`    ${R}`, `    ${E}`, `    ${C}`, `    ${U}`, ...fence(caveat)])
    weak([...fence(R, E, C, U), F, caveat])
    // Alone, a card fenced whole is still the report.
    expect(parseCard(fence(R, E, C, U).join('\n')).isWeak).toBe(false)
  })

  test('a fenced or indented block under UNVERIFIED: none is an item, whatever it says', async () => {
    weak([R, E, C, U, ...fence('the Windows path is broken')])
    weak([R, E, C, U, '', ...fence('the Windows path is broken')])
    weak([R, E, C, U, '', '    2 tests fail on main'])
    weak([R, E, C, U, ...fence('FAILED tests/test_a.py::test_x', '1 failed, 10 passed')])
    weak([R, E, U, ...fence('the Windows path is broken'), C])
    weak([R, E, U, '', '    the Windows path is broken', C])
    weak([R, E, C, 'UNVERIFIED: n/a', '~~~', 'Open issue: prod migration fails', '~~~'])
    // EVIDENCE output stays exempt (round eight).
    expect(parseCard([R, E, ...fence('12 passed'), C, U].join('\n')).isWeak).toBe(false)
  })

  test('a held quoted line is an item even when it reads none', async () => {
    weak([R, E, '> n/a', C, U])
    weak([R, E, C, U, '> none'])
    weak([R, '> nothing remaining', E, C, U])
    weak([R, E, C, U, '> Done.'])
  })
})

describe('report cards and redaction: round ten', () => {
  const BS = '\\'
  const BT = '`'
  const V = 'hunter2xyz'
  const viaMcp = (command: string): string => toolDetail('mcp__x__run', { command })
  const shapes = (command: string, ...eols: string[]): string[] => eols.map(eol => command.replace(/\n/g, eol))
  const hidden = (command: string): void => {
    for (const text of shapes(command, '\n', '\r\n')) {
      expect(redact(text)).not.toContain(V)
      expect(viaMcp(text)).not.toContain(V)
      expect(toolDetail('Bash', { command: text })).not.toContain(V)
      expect(label(text, 400)).not.toContain(V)
    }
  }

  test('a value continued to the next line is masked through an MCP tool call', async () => {
    hidden(`aws secretsmanager create-secret --name n --secret-string ${BS}\n  ${V}`)
    hidden(`aws ssm put-parameter --name n --type SecureString --value ${BS}\n  ${V}`)
    hidden(`aws secretsmanager create-secret --name n --secret-string ${BT}\n  ${V}`)
    hidden(`setx API_TOKEN ${BT}\n  ${V}`)
    hidden(`pulumi config set region ${V} ${BS}\n--secret`)
    hidden(`heroku config:set ${BT}\nNAME=${V}`)
    expect(viaMcp(`aws secretsmanager create-secret --name n --secret-string ${BS}\n  ${V}`)).toContain('***')
  })

  test('the words of an MCP call are masked in nested objects and arrays', async () => {
    const detail = toolDetail('mcp__x__run', { steps: [{ command: `setx API_TOKEN ${BT}\n  ${V}` }], note: 'kept' })
    expect(detail).not.toContain(V)
    expect(detail).toContain('kept')
  })

  test('the word after a value flag is a value, before or after the name', async () => {
    hidden(`gh secret set --body ${V} NAME`)
    hidden(`gh secret set -b ${V} NAME`)
    hidden(`gh secret set --value ${V} NAME`)
    expect(redact(`gh secret set NAME --body ${V}`)).not.toContain(V)
  })

  test('a gap the reader cannot cross masks the rest of the logical line', async () => {
    const many = `${BS}\n`.repeat(9)
    const wide = `${BS}\n${' '.repeat(201)}`
    hidden(`gh secret set ${many}NAME --body ${V}`)
    hidden(`gh secret set ${wide}NAME --body ${V}`)
    hidden(`aws secretsmanager create-secret --name n --secret-string ${many}${V}`)
    hidden(`aws secretsmanager create-secret --name n --secret-string ${wide}${V}`)
    hidden(`aws ssm put-parameter --name n --type SecureString --value ${many}${V}`)
    hidden(`aws ssm put-parameter --name n --type SecureString --value ${wide}${V}`)
    hidden(`pulumi config set region ${V} ${many}--secret`)
  })

  test('a value cut in two by a continuation is masked whole', async () => {
    hidden(`aws secretsmanager create-secret --name n --secret-string hunter${BS}\n2xyz`)
    hidden(`aws ssm put-parameter --name n --type SecureString --value hunter${BS}\n2xyz`)
  })

  test('a gap it can cross costs nothing and the masking stays linear', async () => {
    expect(redact('aws s3 ls --value x')).toContain('--value x')
    const long = `gh secret set NAME --body ${V} ${'a'.repeat(50_000)}`
    const start = performance.now()
    redact(long)
    redact(`aws secretsmanager create-secret --secret-string ${`${BS}\n`.repeat(2000)}${V}`)
    expect(performance.now() - start).toBeLessThan(2000)
  })
})

describe('report cards and redaction: round eleven', () => {
  const BS = String.fromCharCode(92)
  const BT = '`'
  const V = 'hunter2xyz'
  const eols = ['\n', '\r\n']
  const viaMcp = (command: string): string => toolDetail('mcp__x__run', { command })
  const hidden = (command: string): void => {
    for (const eol of eols) {
      const text = command.replace(/\n/g, eol)
      expect([text, redact(text).includes(V)]).toEqual([text, false])
      expect([text, viaMcp(text).includes(V)]).toEqual([text, false])
      expect([text, label(text, 400).includes(V)]).toEqual([text, false])
    }
  }
  // A value cut by a continuation never holds V whole: each half is checked on its own.
  const halves = (command: string): void => {
    for (const eol of eols) {
      const text = command.replace(/\n/g, eol)
      const shown = [redact(text), viaMcp(text), label(text, 400), toolDetail('Bash', { command: text })]
      for (const out of shown) {
        expect([text, out.includes('hunter')]).toEqual([text, false])
        expect([text, out.includes('2xyz')]).toEqual([text, false])
      }
    }
  }

  test('an MCP argument nested past the depth limit is never shown raw', async () => {
    const command = `aws secretsmanager create-secret --name n --secret-string ${BS}\n  ${V}`
    for (const depth of [20, 21, 22, 30]) {
      let objects: unknown = { command }
      let arrays: unknown = [command]
      for (let level = 0; level < depth; level++) {
        objects = { next: objects }
        arrays = [arrays]
      }
      expect(toolDetail('mcp__x__run', { value: objects })).not.toContain(V)
      expect(toolDetail('mcp__x__run', { value: arrays })).not.toContain(V)
    }
  })

  test('an MCP call past the budget is cut, not scanned whole', async () => {
    const leaves = Array.from({ length: 100_000 }, () => 'ab')
    const started = performance.now()
    const detail = toolDetail('mcp__x__run', { leaves })
    expect(performance.now() - started).toBeLessThan(1500)
    expect(detail.length).toBeLessThan(5000)
  })

  test('a value that starts with a dash after a value flag is masked', async () => {
    hidden(`gh secret set NAME --body -${V}`)
    hidden(`gh secret set NAME -b -${V}`)
    hidden(`gh secret set NAME --value -${V}`)
    hidden(`gh secret set --body -${V} NAME`)
    hidden(`gh secret set NAME --app actions --body -${V}`)
  })

  test('a value continued over ten or more lines is masked, however it is cut', async () => {
    const pieces = `a${BS}\n`.repeat(12)
    hidden(`aws secretsmanager create-secret --name n --secret-string ${pieces}${V}`)
    hidden(`aws ssm put-parameter --name n --type SecureString --value ${pieces}${V}`)
    hidden(`aws secretsmanager create-secret --name n --secret-string ${`a${BT}\n`.repeat(12)}${V}`)
  })

  test('a word cut by a continuation with no indent is masked in both halves', async () => {
    halves(`npm config set //r/:_authTOKEN hunter${BS}\n2xyz`)
    halves(`pulumi config set dbpassword hunter${BS}\n2xyz`)
    halves(`rclone config create r s3 secret_access_key hunter${BS}\n2xyz`)
    halves(`setx API_TOKEN hunter${BT}\n2xyz`)
    halves(`mysql -phunter${BS}\n2xyz`)
    halves(`docker login -u me -p hunter${BS}\n2xyz`)
    halves(`sshpass -p hunter${BS}\n2xyz ssh h`)
    halves(`net user admin hunter${BS}\n2xyz /add`)
    halves(`redis-cli -a hunter${BS}\n2xyz ping`)
    halves(`aws configure set aws_secret_access_key hunter${BS}\n2xyz`)
    halves(`curl -u me:hunter${BS}\n2xyz https://h`)
    halves(`tool --password hunter${BS}\n2xyz`)
    halves(`export DB_PASSWORD=hunter${BS}\n2xyz`)
    halves(`PGPASSWORD=hunter${BS}\n2xyz psql`)
    halves(`curl -H "Authorization: Bearer hunter${BS}\n2xyz" https://h`)
    halves(`curl -H "X-Api-Key: hunter${BS}\n2xyz" https://h`)
  })

  test('a continuation right after = leads to the value', async () => {
    hidden(`aws secretsmanager create-secret --name n --secret-string=${BS}\n${V}`)
    hidden(`aws ssm put-parameter --name n --type SecureString --value=${BS}\n${V}`)
    hidden(`gh secret set --body=${BS}\n${V} NAME`)
    hidden(`gh secret set NAME --body=${BS}\n${V}`)
  })

  test('ordinary commands are left alone by the continued-word pass', async () => {
    expect(redact(`npm install ${BS}\n  left-pad`)).toContain('left-pad')
    expect(redact(`gh secret set NAME --body x`)).toContain('NAME')
  })
})

describe('report cards and redaction: round thirteen', () => {
  const BS = String.fromCharCode(92)
  const BT = '`'
  const eols = ['\n', '\r\n']
  const viaMcp = (command: string): string => toolDetail('mcp__x__run', { command })
  const outputs = (text: string): string[] => [
    redact(text),
    label(text, 400),
    toolDetail('Bash', { command: text }),
    viaMcp(text),
    describeTool('Bash', { command: text }),
  ]
  const hideAll = (command: string, ...parts: string[]): void => {
    for (const eol of eols) {
      const text = command.replace(/\n/g, eol)
      for (const out of outputs(text)) {
        for (const part of parts) expect([text, out.includes(part)]).toEqual([text, false])
      }
    }
  }
  const split = ['hunter', '2xyz']

  test('a bare value cut by a continuation is masked with its continued pieces', async () => {
    for (const mark of [BS, BT]) {
      for (const tail of ['', ' psql']) {
        hideAll(`tool --password hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`export DB_PASSWORD=hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`PGPASSWORD=hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`Authorization: Bearer hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`X-Api-Key: hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`tool -TOKEN hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`heroku config:set API_TOKEN=hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`openssl enc -passin pass:hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`tool --api-key hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`tool --client-secret hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`tool --token hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`$env:API_TOKEN=hunter${mark}\n2xyz${tail}`, ...split)
        hideAll(`password: hunter${mark}\n2xyz${tail}`, ...split)
      }
    }
  })

  test('a value cut by two or three continuations is masked in every piece', async () => {
    for (const mark of [BS, BT]) {
      hideAll(`export DB_PASSWORD=hun${mark}\nter${mark}\n2xyz`, ...split)
      hideAll(`tool --password hun${mark}\nter${mark}\n2xyz${mark}\nrest`, ...split, 'rest')
      hideAll(`PGPASSWORD=hunter${mark}\n${mark}\n2xyz psql`, ...split)
    }
  })

  test('a continuation right after the joint hides the value on the next line', async () => {
    for (const mark of [BS, BT]) {
      hideAll(`export DB_PASSWORD=${mark}\nhunter2xyz`, ...split)
      hideAll(`export DB_PASSWORD=${mark}\n  hunter2xyz`, ...split)
      hideAll(`tool --password=${mark}\nhunter2xyz`, ...split)
      hideAll(`PGPASSWORD=${mark}\nhunter2xyz psql`, ...split)
      hideAll(`$env:API_TOKEN=${mark}\nhunter2xyz`, ...split)
      hideAll(`export DB_PASSWORD=${mark}\n${mark}\nhunter2xyz`, ...split)
    }
  })

  test('a store value holding = is masked whole', async () => {
    const b64 = 'aGVsbG8gd29ybGQhIQ=='
    hideAll(`gh secret set NAME --body ${b64}`, b64, 'aGVsbG8')
    hideAll(`gh secret set NAME -b ${b64}`, b64, 'aGVsbG8')
    hideAll(`gh secret set --body ${b64} NAME`, b64, 'aGVsbG8')
    hideAll(`dotnet user-secrets set Db:password ${b64}`, b64, 'aGVsbG8')
    hideAll(`pulumi config set --secret k ${b64}`, b64, 'aGVsbG8')
    hideAll(`vercel secrets add k ${b64}`, b64, 'aGVsbG8')
    hideAll(`wrangler secret put K ${b64}`, b64, 'aGVsbG8')
    hideAll(`doppler secrets set K ${b64}`, b64, 'aGVsbG8')
    hideAll('gh secret set NAME --body hunter=2xyz', ...split)
    hideAll('gh secret set NAME --body X==', 'X==')
    hideAll('gh secret set --body X== NAME', 'X==')
  })

  test('a credential name in the continued text of a masked value is masked too', async () => {
    const W = 'Qq7wZk9v'
    for (const mark of [BS, BT]) {
      hideAll(`mysql -u root --password ${mark}\n  --token ${W}`, W)
      hideAll(`tool login --with-token ${mark}\n  --password ${W}`, W)
      hideAll(`Invoke-Thing -Token ${mark}\n  -Password ${W}`, W)
      hideAll(`Set it with ${BT}export API_TOKEN=abc123${BT} ${mark}\npassword: ${W}`, W)
      hideAll(`Use ${BT}--password=abc123${BT} ${mark}\n"api_key": "${W}"`, W)
      hideAll(`curl https://h --password abc123${mark}\n--token ${W}`, W)
      hideAll(`db.password=abc123${mark}\napi.token = ${W}`, W)
      hideAll(`secret: C:\\vault${mark}\npassword: ${W}`, W)
      hideAll(`export DB_PASSWORD=${mark}\n  --token ${W}`, W)
      hideAll(`tool --password abc123${mark}\nmid${mark}\n--token ${W}`, W)
      hideAll(`X-Api-Key: abc123${mark}\nAuthorization: Bearer ${W}`, W)
    }
  })

  test('names and ordinary commands stay readable', async () => {
    expect(redact('gh secret set NAME --body x')).toContain('NAME')
    expect(redact('fly secrets set NAME=abc')).toContain('NAME=')
    expect(redact(`npm install ${BS}\n  left-pad`)).toContain('left-pad')
  })
})

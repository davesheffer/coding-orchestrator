const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');

const calls = [];
const tabs = [];
const mockVscode = {
  commands: { executeCommand: async (...args) => {
    calls.push(args);
    if (args[0] === 'chatgpt.newCodexPanel') {
      setTimeout(() => tabs.push({ input: { uri: { scheme: 'openai-codex' } } }), 20);
    }
  } },
  env: { clipboard: { writeText: async text => calls.push(['clipboard', text]) } },
  window: {
    tabGroups: { all: [{ tabs }] },
    registerUriHandler: handler => { calls.push(['registered', handler]); return {}; },
    terminals: [],
    createTerminal: options => {
      calls.push(['createTerminal', options]);
      return {
        show: () => calls.push(['show']),
        sendText: (text, addNewLine) => calls.push(['sendText', text, addNewLine]),
      };
    },
  },
};
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === 'vscode') return mockVscode;
  return originalLoad.call(this, request, parent, isMain);
};
const bridge = require('./extension');
Module._load = originalLoad;

// An await that never settles lets Node drain the loop and exit 0 without running the rest;
// fail the run unless every test got to the end.
let finished = false;
process.on('exit', code => {
  if (!finished && code === 0) {
    console.error('handoff bridge tests did not complete');
    process.exitCode = 1;
  }
});

(async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'coding-orchestrator-bridge-test-'));
  process.env.ORCHESTRATOR_HANDOFF_HOME = root;
  try {
    const context = { subscriptions: [] };
    bridge.activate(context);
    assert.equal(calls[0][0], 'registered');
    // Stop the background scan; the tests drive scanPending directly.
    for (const subscription of context.subscriptions) subscription.dispose?.();
    fs.mkdirSync(path.join(root, 'handoffs'));
    const handoff = path.join(root, 'handoffs', 'handoff.md');
    fs.writeFileSync(handoff, 'GOAL: continue\n');
    fs.mkdirSync(path.join(root, 'launches'));
    const id = 'a'.repeat(32);
    fs.writeFileSync(path.join(root, 'launches', `${id}.json`), JSON.stringify({
      client: 'codex', handoff, prompt: 'Continue from handoff', created_at: Date.now() / 1000,
    }));
    await bridge.handleUri({ path: '/open', query: `id=${id}` });
    assert.deepEqual(calls.slice(1).map(call => call[0]),
                     ['chatgpt.newCodexPanel', 'clipboard']);
    assert.match(calls[2][1], /GOAL: continue/);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${id}.json`))).status, 'opened');
    assert.equal(fs.existsSync(path.join(root, 'launches', `${id}.json`)), false);
    assert.deepEqual(fs.readdirSync(path.join(root, 'acks')), [`${id}.json`]);
    calls.length = 0;
    // A replayed or already-claimed request is a no-op and must not overwrite the real ack.
    await bridge.handleUri({ path: '/open', query: `id=${id}` });
    assert.equal(calls.length, 0);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${id}.json`))).status, 'opened');
    const outside = path.join(root, 'outside.md');
    fs.writeFileSync(outside, 'SECRET\n');
    for (const [index, target] of [outside, path.join(root, 'handoffs', '..', 'outside.md'),
                                   path.join(root, 'handoffs')].entries()) {
      const escapeId = String(index + 1).repeat(32);
      fs.writeFileSync(path.join(root, 'launches', `${escapeId}.json`), JSON.stringify({
        client: 'codex', handoff: target, prompt: 'Continue', created_at: Date.now() / 1000,
      }));
      await bridge.handleUri({ path: '/open', query: `id=${escapeId}` });
      assert.equal(calls.length, 0);
      const ack = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${escapeId}.json`)));
      assert.equal(ack.status, 'error');
      assert.match(ack.error, /invalid or expired/);
      assert.equal(fs.existsSync(path.join(root, 'launches', `${escapeId}.json`)), false);
    }
    const escapeOutsideDir = fs.mkdtempSync(path.join(os.tmpdir(), 'coding-orchestrator-bridge-outside-'));
    const linkPath = path.join(root, 'handoffs', 'escape-link');
    try {
      fs.writeFileSync(path.join(escapeOutsideDir, 'secret.md'), 'SECRET\n');
      fs.symlinkSync(escapeOutsideDir, linkPath, process.platform === 'win32' ? 'junction' : 'dir');
      const linkId = '9'.repeat(32);
      fs.writeFileSync(path.join(root, 'launches', `${linkId}.json`), JSON.stringify({
        client: 'codex', handoff: path.join(linkPath, 'secret.md'),
        prompt: 'Continue', created_at: Date.now() / 1000,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${linkId}` });
      assert.equal(calls.length, 0);
      const linkAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${linkId}.json`)));
      assert.equal(linkAck.status, 'error');
      assert.match(linkAck.error, /invalid or expired/);
      assert.equal(fs.existsSync(path.join(root, 'launches', `${linkId}.json`)), false);
    } finally {
      fs.rmSync(linkPath, { recursive: true, force: true });
      fs.rmSync(escapeOutsideDir, { recursive: true, force: true });
    }
    // realpath cannot see a hard link inside handoffs to a file outside, so a handoff file
    // with more than one link is refused; the helper always creates a new file.
    const hardLink = path.join(root, 'handoffs', 'hard-link.md');
    let linked = false;
    try { fs.linkSync(outside, hardLink); linked = true; } catch { /* no hard links here */ }
    if (linked) {
      try {
        const hardId = '4'.repeat(32);
        fs.writeFileSync(path.join(root, 'launches', `${hardId}.json`), JSON.stringify({
          client: 'codex', handoff: hardLink, prompt: 'Continue', created_at: Date.now() / 1000,
        }));
        calls.length = 0;
        await bridge.handleUri({ path: '/open', query: `id=${hardId}` });
        assert.equal(calls.length, 0);
        const hardAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${hardId}.json`)));
        assert.equal(hardAck.status, 'error');
        assert.match(hardAck.error, /invalid or expired/);
      } finally {
        fs.rmSync(hardLink, { force: true });
      }
    }
    // A file swapped in between validation and open is refused: the handle must be the
    // same device and inode that the validated path names.
    const originalOpen = fs.openSync;
    fs.openSync = (file, ...rest) =>
      originalOpen(path.basename(String(file)) === 'handoff.md' ? outside : file, ...rest);
    try {
      const swapId = '5'.repeat(32);
      fs.writeFileSync(path.join(root, 'launches', `${swapId}.json`), JSON.stringify({
        client: 'codex', handoff, prompt: 'Continue', created_at: Date.now() / 1000,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${swapId}` });
      assert.equal(calls.length, 0);
      const swapAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${swapId}.json`)));
      assert.equal(swapAck.status, 'error');
      assert.match(swapAck.error, /invalid or expired/);
    } finally {
      fs.openSync = originalOpen;
    }
    // A final-component symlink inside handoffs to a file outside is refused.
    const fileLink = path.join(root, 'handoffs', 'file-link.md');
    let symlinked = false;
    try { fs.symlinkSync(outside, fileLink, 'file'); symlinked = true; } catch (error) {
      if (!['EPERM', 'EACCES'].includes(error.code)) throw error;  // no symlink privilege
    }
    if (symlinked) {
      try {
        const fileLinkId = 'ab'.repeat(16);
        fs.writeFileSync(path.join(root, 'launches', `${fileLinkId}.json`), JSON.stringify({
          client: 'codex', handoff: fileLink, prompt: 'Continue', created_at: Date.now() / 1000,
        }));
        calls.length = 0;
        await bridge.handleUri({ path: '/open', query: `id=${fileLinkId}` });
        assert.equal(calls.length, 0);
        const fileLinkAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${fileLinkId}.json`)));
        assert.equal(fileLinkAck.status, 'error');
        assert.match(fileLinkAck.error, /invalid or expired/);
      } finally {
        fs.unlinkSync(fileLink);
      }
    }
    // The handoffs folder swapped for a link between validation and open is refused while
    // the swap is still in place: the path now resolves outside the validated folder.
    const handoffsDir = path.join(root, 'handoffs');
    const movedDir = path.join(root, 'handoffs-moved');
    const decoyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'coding-orchestrator-bridge-outside-'));
    fs.writeFileSync(path.join(decoyDir, 'handoff.md'), 'SECRET\n');
    let swapped = false;
    fs.openSync = (file, ...rest) => {
      if (!swapped && path.basename(String(file)) === 'handoff.md') {
        fs.renameSync(handoffsDir, movedDir);
        swapped = true;
        fs.symlinkSync(decoyDir, handoffsDir, process.platform === 'win32' ? 'junction' : 'dir');
      }
      return originalOpen(file, ...rest);
    };
    try {
      const dirSwapId = 'cd'.repeat(16);
      fs.writeFileSync(path.join(root, 'launches', `${dirSwapId}.json`), JSON.stringify({
        client: 'codex', handoff, prompt: 'Continue', created_at: Date.now() / 1000,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${dirSwapId}` });
      assert.equal(swapped, true);
      assert.equal(calls.length, 0);
      const dirSwapAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${dirSwapId}.json`)));
      assert.equal(dirSwapAck.status, 'error');
      assert.match(dirSwapAck.error, /invalid or expired/);
    } finally {
      fs.openSync = originalOpen;
      if (swapped) {
        try { fs.unlinkSync(handoffsDir); } catch { /* the link was never created */ }
        fs.renameSync(movedDir, handoffsDir);
      }
      fs.rmSync(decoyDir, { recursive: true, force: true });
    }
    if (process.platform === 'win32') {
      // A home given by its 8.3 short name (C:\Users\DAVIDS~1) must accept both the long
      // handoff path the helper saves (Python's resolve() expands it) and the short one.
      let shortRoot = root;
      try {
        shortRoot = execFileSync('cmd.exe', ['/d', '/s', '/c', `"for %I in ("${root}") do @echo %~sI"`],
                                 { windowsVerbatimArguments: true, encoding: 'utf8' }).trim();
      } catch { /* treated as no short name */ }
      if (!shortRoot || shortRoot.toLowerCase() === root.toLowerCase()) {
        console.log(`skipped 8.3 short-name test: no short name for ${root}`);
      } else {
        const savedEnv = process.env.ORCHESTRATOR_HANDOFF_HOME;
        process.env.ORCHESTRATOR_HANDOFF_HOME = shortRoot;
        try {
          for (const [shortId, target] of [['6'.repeat(32), handoff],
                                           ['7'.repeat(32), path.join(shortRoot, 'handoffs', 'handoff.md')]]) {
            fs.writeFileSync(path.join(root, 'launches', `${shortId}.json`), JSON.stringify({
              client: 'codex', handoff: target, prompt: 'Continue via short home', created_at: Date.now() / 1000,
            }));
            calls.length = 0;
            await bridge.handleUri({ path: '/open', query: `id=${shortId}` });
            assert.deepEqual(calls.map(call => call[0]), ['chatgpt.newCodexPanel', 'clipboard']);
            assert.match(calls[1][1], /GOAL: continue/);
            const shortAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${shortId}.json`)));
            assert.equal(shortAck.status, 'opened');
          }
        } finally {
          process.env.ORCHESTRATOR_HANDOFF_HOME = savedEnv;
        }
      }
    }
    if (process.platform === 'win32' && /^[a-zA-Z]:/.test(root)) {
      // A junction/symlink escape must be blocked, but a legit launch whose
      // ORCHESTRATOR_HANDOFF_HOME only differs by drive-letter case must still work.
      const flippedDrive = root[0] === root[0].toLowerCase() ? root[0].toUpperCase() : root[0].toLowerCase();
      const flippedRoot = flippedDrive + root.slice(1);
      const caseId = 'c'.repeat(32);
      const savedEnv = process.env.ORCHESTRATOR_HANDOFF_HOME;
      process.env.ORCHESTRATOR_HANDOFF_HOME = flippedRoot;
      try {
        fs.writeFileSync(path.join(root, 'launches', `${caseId}.json`), JSON.stringify({
          client: 'codex', handoff, prompt: 'Continue via case-flipped home', created_at: Date.now() / 1000,
        }));
        calls.length = 0;
        await bridge.handleUri({ path: '/open', query: `id=${caseId}` });
        assert.deepEqual(calls.map(call => call[0]), ['chatgpt.newCodexPanel', 'clipboard']);
        const caseAck = JSON.parse(fs.readFileSync(path.join(flippedRoot, 'acks', `${caseId}.json`)));
        assert.equal(caseAck.status, 'opened');
      } finally {
        process.env.ORCHESTRATOR_HANDOFF_HOME = savedEnv;
      }
    }
    calls.length = 0;
    const claudeId = 'b'.repeat(32);
    // Claude relay handoffs live under ~/.claude/relay/handoffs, outside the bridge home.
    fs.writeFileSync(path.join(root, 'launches', `${claudeId}.json`), JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:1234abcd continue', created_at: Date.now() / 1000,
    }));
    await bridge.handleUri({ path: '/open', query: `id=${claudeId}` });
    assert.equal(calls[0][0], 'claude-vscode.primaryEditor.open');
    assert.equal(calls[0][2], 'relay:1234abcd continue');
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${claudeId}.json`))).status, 'opened');

    // A request from a session in another window's extension host is left for that window.
    const otherPid = process.pid === 1 ? 2 : process.pid - 1;
    const elsewhereId = 'e'.repeat(32);
    const elsewhere = path.join(root, 'launches', `${elsewhereId}.json`);
    fs.writeFileSync(elsewhere, JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:elsewhere continue',
      created_at: Date.now() / 1000, hosts: [otherPid],
    }));
    calls.length = 0;
    await bridge.handleUri({ path: '/open', query: `id=${elsewhereId}` });
    await bridge.scanPending();
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(elsewhere), true);
    assert.equal(fs.existsSync(path.join(root, 'acks', `${elsewhereId}.json`)), false);
    fs.unlinkSync(elsewhere);

    // An untargeted request is opened only through the URL, never by the scan.
    const untargetedId = 'f'.repeat(32);
    const untargeted = path.join(root, 'launches', `${untargetedId}.json`);
    fs.writeFileSync(untargeted, JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:untargeted continue',
      created_at: Date.now() / 1000, hosts: [],
    }));
    await bridge.scanPending();
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(untargeted), true);
    fs.unlinkSync(untargeted);

    // The scan opens a request whose ancestors include this extension host.
    const hereId = 'd'.repeat(32);
    fs.writeFileSync(path.join(root, 'launches', `${hereId}.json`), JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:here continue',
      created_at: Date.now() / 1000, hosts: [otherPid, process.pid],
    }));
    await bridge.scanPending();
    assert.deepEqual(calls.map(call => [call[0], call[2]]),
                     [['claude-vscode.primaryEditor.open', 'relay:here continue']]);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${hereId}.json`))).status, 'opened');
    assert.equal(fs.existsSync(path.join(root, 'launches', `${hereId}.json`)), false);

    // Dead requests are swept without being opened.
    const deadId = '0'.repeat(32);
    const dead = path.join(root, 'launches', `${deadId}.json`);
    fs.writeFileSync(dead, JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:dead continue',
      created_at: Date.now() / 1000 - 11 * 60, hosts: [process.pid],
    }));
    const old = new Date(Date.now() - 11 * 60 * 1000);
    fs.utimesSync(dead, old, old);
    calls.length = 0;
    await bridge.scanPending();
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(dead), false);

    // An old mtime with a fresh created_at is not dead and is not swept.
    const staleId = '9'.repeat(32);
    const stale = path.join(root, 'launches', `${staleId}.json`);
    fs.writeFileSync(stale, JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:stale continue',
      created_at: Date.now() / 1000, hosts: [otherPid],
    }));
    fs.utimesSync(stale, old, old);
    await bridge.scanPending();
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(stale), true);
    fs.unlinkSync(stale);

    // The URL handler opens a request whose ancestors include this extension host.
    const viaId = '8'.repeat(32);
    fs.writeFileSync(path.join(root, 'launches', `${viaId}.json`), JSON.stringify({
      client: 'claude', handoff: outside, prompt: 'relay:via continue',
      created_at: Date.now() / 1000, hosts: [process.pid],
    }));
    await bridge.handleUri({ path: '/open', query: `id=${viaId}` });
    assert.deepEqual(calls.map(call => [call[0], call[2]]),
                     [['claude-vscode.primaryEditor.open', 'relay:via continue']]);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${viaId}.json`))).status, 'opened');
    assert.equal(fs.existsSync(path.join(root, 'launches', `${viaId}.json`)), false);

    // The scan focuses a Claude session open in this window.
    const session = '366f2731-1b7b-4099-9607-d8e06526a63e';
    const focusId = '1a'.repeat(16);
    fs.writeFileSync(path.join(root, 'launches', `${focusId}.json`), JSON.stringify({
      action: 'focus', session, hosts: [otherPid, process.pid], created_at: Date.now() / 1000,
    }));
    calls.length = 0;
    await bridge.scanPending();
    assert.deepEqual(calls, [['claude-vscode.primaryEditor.open', session]]);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${focusId}.json`))),
                     { status: 'focused', session });
    assert.equal(fs.existsSync(path.join(root, 'launches', `${focusId}.json`)), false);

    // A focus request without a target window is never run, not even by the URL's window.
    for (const [index, hosts] of [[], undefined].entries()) {
      const noHostId = String(index + 2).repeat(16) + 'f'.repeat(16);
      fs.writeFileSync(path.join(root, 'launches', `${noHostId}.json`), JSON.stringify({
        action: 'focus', session, hosts, created_at: Date.now() / 1000,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${noHostId}` });
      assert.equal(calls.length, 0);
      const noHostAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${noHostId}.json`)));
      assert.equal(noHostAck.status, 'error');
      assert.match(noHostAck.error, /focus requests must target a window/);
    }

    // The URL's window leaves a focus request for another window alone.
    const otherFocusId = '3c'.repeat(16);
    const otherFocus = path.join(root, 'launches', `${otherFocusId}.json`);
    fs.writeFileSync(otherFocus, JSON.stringify({
      action: 'focus', session, hosts: [otherPid], created_at: Date.now() / 1000,
    }));
    calls.length = 0;
    await bridge.handleUri({ path: '/open', query: `id=${otherFocusId}` });
    await bridge.scanPending();
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(otherFocus), true);
    assert.equal(fs.existsSync(path.join(root, 'acks', `${otherFocusId}.json`)), false);
    fs.unlinkSync(otherFocus);

    // Invalid session IDs and expired focus requests are refused.
    for (const [badId, bad] of [
      ['4d'.repeat(16), { session: 'not-a-session', created_at: Date.now() / 1000 }],
      ['5e'.repeat(16), { session: `${session}x`, created_at: Date.now() / 1000 }],
      ['6f'.repeat(16), { session, created_at: Date.now() / 1000 - 301 }],
    ]) {
      fs.writeFileSync(path.join(root, 'launches', `${badId}.json`), JSON.stringify({
        action: 'focus', hosts: [process.pid], ...bad,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${badId}` });
      assert.equal(calls.length, 0);
      const badAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${badId}.json`)));
      assert.equal(badAck.status, 'error');
      assert.match(badAck.error, /invalid or expired focus request/);
      assert.equal(fs.existsSync(path.join(root, 'launches', `${badId}.json`)), false);
    }
    // A Claude CLI request opens a new integrated terminal. The typed text is built from the
    // validated token only; an injected prompt is ignored.
    const terminalText = 'claude "relay:abcd1234 continue from the saved handoff."';
    const terminalId = '7a'.repeat(16);
    fs.writeFileSync(path.join(root, 'launches', `${terminalId}.json`), JSON.stringify({
      client: 'claude-terminal', handoff: outside, token: 'relay:abcd1234',
      prompt: 'relay:abcd1234 continue."; rm -rf / #', cwd: root,
      created_at: Date.now() / 1000, hosts: [], shells: [],
    }));
    calls.length = 0;
    await bridge.handleUri({ path: '/open', query: `id=${terminalId}` });
    assert.deepEqual(calls, [['createTerminal', { name: 'Claude relay', cwd: root }], ['show'],
                             ['sendText', terminalText, true]]);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${terminalId}.json`))),
                     { status: 'opened', client: 'claude-terminal' });
    assert.equal(fs.existsSync(path.join(root, 'launches', `${terminalId}.json`)), false);

    // Malformed tokens and expired terminal requests are refused without a terminal.
    for (const [badId, bad] of [
      ['7b'.repeat(16), { token: 'relay:abcd1234; rm -rf /' }],
      ['7c'.repeat(16), { token: 'relay:ABCD1234' }],
      ['7d'.repeat(16), { token: undefined }],
      ['7e'.repeat(16), { token: 'relay:abcd1234', created_at: Date.now() / 1000 - 301 }],
    ]) {
      fs.writeFileSync(path.join(root, 'launches', `${badId}.json`), JSON.stringify({
        client: 'claude-terminal', handoff: outside, prompt: 'relay:abcd1234 continue',
        cwd: root, created_at: Date.now() / 1000, ...bad,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${badId}` });
      assert.equal(calls.length, 0);
      const badAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${badId}.json`)));
      assert.equal(badAck.status, 'error');
      assert.match(badAck.error, /invalid or expired handoff request/);
    }

    // A cwd that is not an absolute directory is omitted.
    for (const [cwdId, cwd] of [['7f'.repeat(16), outside], ['8a'.repeat(16), 'relative'],
                                ['8b'.repeat(16), 42]]) {
      fs.writeFileSync(path.join(root, 'launches', `${cwdId}.json`), JSON.stringify({
        client: 'claude-terminal', handoff: outside, token: 'relay:abcd1234', cwd,
        created_at: Date.now() / 1000,
      }));
      calls.length = 0;
      await bridge.handleUri({ path: '/open', query: `id=${cwdId}` });
      assert.deepEqual(calls, [['createTerminal', { name: 'Claude relay', cwd: undefined }], ['show'],
                               ['sendText', terminalText, true]]);
    }

    // A request whose shell ancestors include one of this window's terminals is opened by
    // the scan; one naming another window's terminal is ignored by the URL and the scan.
    mockVscode.window.terminals.push({ processId: Promise.resolve(4242) },
                                     { processId: Promise.reject(new Error('closed')) });
    mockVscode.window.terminals[1].processId.catch(() => {});
    const shellId = '8c'.repeat(16);
    fs.writeFileSync(path.join(root, 'launches', `${shellId}.json`), JSON.stringify({
      client: 'claude-terminal', handoff: outside, token: 'relay:abcd1234', cwd: root,
      created_at: Date.now() / 1000, hosts: [], shells: [99999, 4242],
    }));
    calls.length = 0;
    await bridge.scanPending();
    assert.deepEqual(calls.map(call => call[0]), ['createTerminal', 'show', 'sendText']);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${shellId}.json`))).status, 'opened');
    assert.equal(fs.existsSync(path.join(root, 'launches', `${shellId}.json`)), false);
    const otherShellId = '8d'.repeat(16);
    const otherShell = path.join(root, 'launches', `${otherShellId}.json`);
    fs.writeFileSync(otherShell, JSON.stringify({
      client: 'claude-terminal', handoff: outside, token: 'relay:abcd1234', cwd: root,
      created_at: Date.now() / 1000, hosts: [], shells: [5151],
    }));
    calls.length = 0;
    await bridge.handleUri({ path: '/open', query: `id=${otherShellId}` });
    await bridge.scanPending();
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(otherShell), true);
    assert.equal(fs.existsSync(path.join(root, 'acks', `${otherShellId}.json`)), false);
    fs.unlinkSync(otherShell);
    mockVscode.window.terminals.length = 0;

    // A terminal whose shell never reports a process ID must not stall the scan or the URL:
    // the lookup times out and the next terminal still matches.
    mockVscode.window.terminals.push({ processId: new Promise(() => {}) },
                                     { processId: Promise.resolve(6161) });
    const stuckId = '8e'.repeat(16);
    fs.writeFileSync(path.join(root, 'launches', `${stuckId}.json`), JSON.stringify({
      client: 'claude-terminal', handoff: outside, token: 'relay:abcd1234', cwd: root,
      created_at: Date.now() / 1000, hosts: [], shells: [6161],
    }));
    calls.length = 0;
    await bridge.scanPending();
    assert.deepEqual(calls.map(call => call[0]), ['createTerminal', 'show', 'sendText']);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${stuckId}.json`))).status, 'opened');
    const stuckUriId = '8f'.repeat(16);
    const stuckUri = path.join(root, 'launches', `${stuckUriId}.json`);
    fs.writeFileSync(stuckUri, JSON.stringify({
      client: 'claude-terminal', handoff: outside, token: 'relay:abcd1234', cwd: root,
      created_at: Date.now() / 1000, hosts: [], shells: [7171],
    }));
    calls.length = 0;
    await bridge.handleUri({ path: '/open', query: `id=${stuckUriId}` });
    assert.equal(calls.length, 0);
    assert.equal(fs.existsSync(stuckUri), true);
    fs.unlinkSync(stuckUri);
    mockVscode.window.terminals.length = 0;

    console.log('handoff bridge tests passed');
    finished = true;
  } finally {
    if (path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep) &&
        path.basename(root).startsWith('coding-orchestrator-bridge-test-')) {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

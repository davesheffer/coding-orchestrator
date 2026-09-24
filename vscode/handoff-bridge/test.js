const assert = require('node:assert/strict');
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
  },
};
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === 'vscode') return mockVscode;
  return originalLoad.call(this, request, parent, isMain);
};
const bridge = require('./extension');
Module._load = originalLoad;

(async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'coding-orchestrator-bridge-test-'));
  process.env.ORCHESTRATOR_HANDOFF_HOME = root;
  try {
    bridge.activate({ subscriptions: [] });
    assert.equal(calls[0][0], 'registered');
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
    await bridge.handleUri({ path: '/open', query: `id=${id}` });
    assert.equal(calls.length, 0);
    assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'acks', `${id}.json`))).status, 'error');
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
    console.log('handoff bridge tests passed');
  } finally {
    if (path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep) &&
        path.basename(root).startsWith('coding-orchestrator-bridge-test-')) {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

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
    const handoff = path.join(root, 'handoff.md');
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
    calls.length = 0;
    const claudeId = 'b'.repeat(32);
    fs.writeFileSync(path.join(root, 'launches', `${claudeId}.json`), JSON.stringify({
      client: 'claude', handoff, prompt: 'relay:1234abcd continue', created_at: Date.now() / 1000,
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

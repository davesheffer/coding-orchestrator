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
    if (args[0] === 'vscode.openWith') {
      setTimeout(() => tabs.push({ input: { uri: {
        scheme: 'openai-codex', fragment: args[1].fragment,
        toString: () => args[1].toString(),
      } } }), 20);
    } else if (args[0] === 'claude-vscode.primaryEditor.open') {
      setTimeout(() => tabs.push({ input: { viewType: 'mainThreadWebview-claudeVSCodePanel' } }), 20);
    }
  } },
  env: { clipboard: { writeText: async text => calls.push(['clipboard', text]) } },
  Uri: { parse: value => ({
    with: ({ fragment }) => ({ fragment, toString: () => `${value}#${fragment}` }),
  }) },
  workspace: { workspaceFolders: [] },
  window: {
    state: { focused: true },
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
  const subscriptions = [];
  try {
    const workspace = path.join(root, 'project');
    fs.mkdirSync(workspace);
    const handoff = path.join(root, 'handoff.md');
    fs.writeFileSync(handoff, 'GOAL: continue\n');
    fs.mkdirSync(path.join(root, 'launches'));
    tabs.push({ input: { uri: {
      scheme: 'openai-codex', toString: () => 'openai-codex://route/extension/panel/new',
    } } });
    bridge.activate({ subscriptions });
    assert.equal(calls[0][0], 'registered');

    const id = 'a'.repeat(32);
    fs.writeFileSync(path.join(root, 'launches', `${id}.json`), JSON.stringify({
      client: 'codex', handoff, workspace, prompt: 'Continue from handoff',
      created_at: Date.now() / 1000,
    }));
    mockVscode.workspace.workspaceFolders = [{ uri: { scheme: 'file', fsPath: root } }];
    await bridge.handleUri({ path: '/open', query: `id=${id}` });
    assert.equal(fs.existsSync(path.join(root, 'acks', `${id}.json`)), false);
    assert.equal(fs.existsSync(path.join(root, 'launches', `${id}.json`)), true);
    mockVscode.workspace.workspaceFolders = [{ uri: { scheme: 'file', fsPath: workspace } }];
    await bridge.scanPending();
    assert.deepEqual(calls.slice(1).map(call => call[0]),
                     ['vscode.openWith', 'clipboard']);
    assert.equal(calls[1][2], 'chatgpt.conversationEditor');
    assert.match(calls[2][1], /GOAL: continue/);
    const ack = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${id}.json`)));
    assert.equal(ack.status, 'opened');
    assert.equal(ack.editorWorkspace, workspace);
    assert.equal(ack.tab, `openai-codex://route/extension/panel/new#${id}`);
    assert.equal(tabs.length, 2);
    assert.equal(fs.existsSync(path.join(root, 'launches', `${id}.json`)), false);

    calls.length = 0;
    const claudeId = 'b'.repeat(32);
    fs.writeFileSync(path.join(root, 'launches', `${claudeId}.json`), JSON.stringify({
      client: 'claude', handoff, workspace, prompt: 'relay:1234abcd continue',
      created_at: Date.now() / 1000,
    }));
    await bridge.scanPending();
    assert.equal(calls[0][0], 'claude-vscode.primaryEditor.open');
    assert.equal(calls[0][2], 'relay:1234abcd continue');
    const claudeAck = JSON.parse(fs.readFileSync(path.join(root, 'acks', `${claudeId}.json`)));
    assert.equal(claudeAck.status, 'opened');
    assert.equal(claudeAck.tab, 'mainThreadWebview-claudeVSCodePanel');
    console.log('handoff bridge tests passed');
  } finally {
    for (const item of subscriptions) item.dispose?.();
    if (path.resolve(root).startsWith(path.resolve(os.tmpdir()) + path.sep) &&
        path.basename(root).startsWith('coding-orchestrator-bridge-test-')) {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

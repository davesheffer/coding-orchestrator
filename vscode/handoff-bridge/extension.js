const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');

function handoffHome() {
  return process.env.ORCHESTRATOR_HANDOFF_HOME || path.join(os.homedir(), '.coding-orchestrator');
}

function writeAck(root, id, result) {
  const folder = path.join(root, 'acks');
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, `${id}.json`);
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(result), 'utf8');
  fs.renameSync(temporary, file);
}

async function waitForNewCodexTab(previous, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tabs = vscode.window.tabGroups.all.flatMap(group => group.tabs);
    if (tabs.some(tab => !previous.has(tab) && tab.input?.uri?.scheme === 'openai-codex')) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('new Codex tab was not observed');
}

async function handleUri(uri) {
  if (uri.path !== '/open') return;
  const id = new URLSearchParams(uri.query).get('id');
  if (!/^[0-9a-f]{32}$/.test(id || '')) return;
  const root = handoffHome();
  try {
    const request = JSON.parse(fs.readFileSync(path.join(root, 'launches', `${id}.json`), 'utf8'));
    if (!['codex', 'claude'].includes(request.client) ||
        typeof request.handoff !== 'string' || typeof request.prompt !== 'string' ||
        !Number.isFinite(request.created_at) ||
        Math.abs(Date.now() / 1000 - request.created_at) > 300 ||
        !fs.statSync(request.handoff).isFile()) {
      throw new Error('invalid or expired handoff request');
    }
    if (request.client === 'claude') {
      await vscode.commands.executeCommand('claude-vscode.primaryEditor.open', undefined, request.prompt);
    } else {
      const previous = new Set(vscode.window.tabGroups.all.flatMap(group => group.tabs));
      await vscode.commands.executeCommand('chatgpt.newCodexPanel');
      await waitForNewCodexTab(previous);
      const handoff = fs.readFileSync(request.handoff, 'utf8');
      await vscode.env.clipboard.writeText(`${request.prompt}\n\nSaved handoff:\n${handoff}`);
    }
    writeAck(root, id, { status: 'opened', client: request.client });
  } catch (error) {
    writeAck(root, id, { status: 'error', error: String(error.message || error) });
  }
}

function activate(context) {
  context.subscriptions.push(vscode.window.registerUriHandler({ handleUri }));
}

function deactivate() {}

module.exports = { activate, deactivate, handleUri };

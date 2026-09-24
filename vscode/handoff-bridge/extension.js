const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');

function handoffHome() {
  return process.env.ORCHESTRATOR_HANDOFF_HOME || path.join(os.homedir(), '.coding-orchestrator');
}

// Codex requests copy the handoff text, so it must be a file the helper saved under
// <home>/handoffs. Claude requests only pre-fill the relay prompt from ~/.claude/relay.
// Resolve symlinks/junctions on both sides so a link inside handoffs cannot point
// outside it, while still accepting a legit path that only differs by case on Windows.
// On Windows the native realpath also expands 8.3 short names, as Python's resolve() does.
const realpath = process.platform === 'win32' ? fs.realpathSync.native : fs.realpathSync;

function realHandoffPath(root, handoff) {
  let folder;
  try { folder = realpath(path.resolve(root, 'handoffs')); } catch { return null; }
  let resolved;
  try { resolved = realpath(path.resolve(handoff)); } catch { return null; }
  const prefix = folder + path.sep;
  const matches = process.platform === 'win32'
    ? resolved.toLowerCase().startsWith(prefix.toLowerCase())
    : resolved.startsWith(prefix);
  return matches ? resolved : null;
}

function writeAck(root, id, result) {
  const folder = path.join(root, 'acks');
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, `${id}.json`);
  const temporary = `${file}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
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
    const launch = path.join(root, 'launches', `${id}.json`);
    const request = JSON.parse(fs.readFileSync(launch, 'utf8'));
    fs.unlinkSync(launch);  // consume the request so its URI cannot be replayed
    const isCodex = request.client === 'codex';
    const resolvedHandoff = isCodex && typeof request.handoff === 'string'
      ? realHandoffPath(root, request.handoff) : request.handoff;
    if (!['codex', 'claude'].includes(request.client) ||
        typeof request.handoff !== 'string' || typeof request.prompt !== 'string' ||
        !Number.isFinite(request.created_at) ||
        Math.abs(Date.now() / 1000 - request.created_at) > 300 ||
        (isCodex && !resolvedHandoff) ||
        !fs.statSync(resolvedHandoff).isFile()) {
      throw new Error('invalid or expired handoff request');
    }
    let handoff;
    if (isCodex) {
      // Read through one handle now, before any await, so the checked file is the one copied.
      const fd = fs.openSync(resolvedHandoff, 'r');
      try {
        if (!fs.fstatSync(fd).isFile()) throw new Error('invalid or expired handoff request');
        handoff = fs.readFileSync(fd, 'utf8');
      } finally {
        fs.closeSync(fd);
      }
    }
    if (request.client === 'claude') {
      await vscode.commands.executeCommand('claude-vscode.primaryEditor.open', undefined, request.prompt);
    } else {
      const previous = new Set(vscode.window.tabGroups.all.flatMap(group => group.tabs));
      await vscode.commands.executeCommand('chatgpt.newCodexPanel');
      await waitForNewCodexTab(previous);
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

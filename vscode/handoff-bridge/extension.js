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

const REQUEST_FILE = /^([0-9a-f]{32})\.json$/;
const DEAD_REQUEST_MS = 10 * 60 * 1000;  // well past the 300s expiry; safe for any window to delete

// A request from a session inside a VS Code extension host lists that session's ancestor
// PIDs in `hosts`. Only the bridge running in that same extension host opens it, so the tab
// lands in the session's own window, not whichever window was active last. The URI still
// wakes the last active window, which ignores requests aimed elsewhere.
function targetsThisWindow(request) {
  return Array.isArray(request.hosts) && request.hosts.includes(process.pid);
}

function targetsOtherWindow(request) {
  return Array.isArray(request.hosts) && request.hosts.length > 0 && !targetsThisWindow(request);
}

function readRequest(root, id) {
  try { return JSON.parse(fs.readFileSync(path.join(root, 'launches', `${id}.json`), 'utf8')); }
  catch { return null; }
}

async function handleUri(uri) {
  if (uri.path !== '/open') return;
  const id = new URLSearchParams(uri.query).get('id');
  if (!/^[0-9a-f]{32}$/.test(id || '')) return;
  const root = handoffHome();
  await openRequest(root, id, true);
}

async function scanPending() {
  const root = handoffHome();
  const folder = path.join(root, 'launches');
  let files;
  try { files = fs.readdirSync(folder); } catch { return; }
  for (const file of files) {
    const id = REQUEST_FILE.exec(file)?.[1];
    if (!id) continue;
    try {
      if (Date.now() - fs.statSync(path.join(folder, file)).mtimeMs > DEAD_REQUEST_MS) {
        // Dead only if the request's own timestamp agrees; a stale mtime alone (clock skew,
        // restored file) must not drop a live request.
        const created = Number(readRequest(root, id)?.created_at);
        if (!Number.isFinite(created) || Date.now() / 1000 - created > DEAD_REQUEST_MS / 1000) {
          fs.unlinkSync(path.join(folder, file));
          continue;
        }
      }
    } catch { continue; }
    const request = readRequest(root, id);
    if (request && targetsThisWindow(request)) await openRequest(root, id, false);
  }
}

async function openRequest(root, id, viaUri) {
  const launch = path.join(root, 'launches', `${id}.json`);
  let request;
  try {
    request = JSON.parse(fs.readFileSync(launch, 'utf8'));
    if (viaUri && request && targetsOtherWindow(request)) return;  // checked before claiming
    fs.unlinkSync(launch);  // consume the request so its URI cannot be replayed
  } catch (error) {
    // Already claimed through this window's other path, or withdrawn by the helper.
    if (error.code === 'ENOENT') return;
    writeAck(root, id, { status: 'error', error: String(error.message || error) });
    return;
  }
  try {
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
  let scanning = false;
  const poll = async () => {
    if (scanning) return;
    scanning = true;
    try { await scanPending(); } catch { /* the next tick retries */ } finally { scanning = false; }
  };
  const timer = setInterval(poll, 500);
  timer.unref?.();
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  void poll();
}

function deactivate() {}

module.exports = { activate, deactivate, handleUri, scanPending };

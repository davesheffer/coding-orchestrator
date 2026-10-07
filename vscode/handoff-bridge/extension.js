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

function handoffsFolder(root) {
  try { return realpath(path.resolve(root, 'handoffs')); } catch { return null; }
}

function inside(folder, resolved) {
  const prefix = folder + path.sep;
  return process.platform === 'win32'
    ? resolved.toLowerCase().startsWith(prefix.toLowerCase())
    : resolved.startsWith(prefix);
}

function realHandoffPath(folder, handoff) {
  if (!folder) return null;
  let resolved;
  try { resolved = realpath(path.resolve(handoff)); } catch { return null; }
  return inside(folder, resolved) ? resolved : null;
}

// O_NOFOLLOW refuses a symlink swapped in for the resolved file; Windows has no such flag.
const OPEN_FLAGS = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0);

// Read the validated handoff through one handle, before any await, so the checked file is
// the one copied. Validation and open are separate path lookups, so after opening, the path
// is resolved again against the handoffs folder validated before, and the handle must be the
// regular file it names (same device and inode). On Linux the handle's own path must also
// lie in that folder. This catches a replaced final component, and a handoffs folder swapped
// for a link if the swap is still in place after the open. A swap undone between the open
// and these checks cannot be ruled out without openat2(RESOLVE_BENEATH), which Node lacks.
// realpath cannot see hard links: a link inside handoffs to a file elsewhere resolves inside.
// The helper creates each handoff as a new file with one link, so more links are refused.
// All of this is defense in depth against same-user tampering, not a security boundary.
function readHandoff(folder, resolved) {
  let fd;
  try { fd = fs.openSync(resolved, OPEN_FLAGS); } catch (error) {
    if (error.code === 'ENOENT' || error.code === 'ELOOP') return null;
    throw error;
  }
  try {
    const opened = fs.fstatSync(fd, { bigint: true });
    const after = realHandoffPath(folder, resolved);
    let named;
    try { named = after && fs.lstatSync(after, { bigint: true }); } catch { return null; }
    if (!named || !opened.isFile() || opened.nlink !== 1n ||
        opened.dev !== named.dev || opened.ino !== named.ino) return null;
    let opener;
    try { opener = fs.readlinkSync(`/proc/self/fd/${fd}`); } catch { /* no procfs */ }
    if (opener !== undefined && !inside(folder, opener)) return null;
    return fs.readFileSync(fd, 'utf8');
  } finally {
    fs.closeSync(fd);
  }
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

// A Claude CLI session in an integrated terminal is not under the extension host, so its
// `hosts` is empty. It lists its ancestor PIDs in `shells` instead, and the window whose own
// terminal shell is among them owns the request.
function nonEmpty(list) {
  return Array.isArray(list) && list.length > 0;
}

// A terminal whose shell never started leaves processId pending forever; without a bound
// the scan would never finish and the poll latch would stop every later request.
const PROCESS_ID_TIMEOUT_MS = 1000;

function terminalPid(terminal) {
  let timer;
  const timeout = new Promise(resolve => { timer = setTimeout(resolve, PROCESS_ID_TIMEOUT_MS); });
  return Promise.race([Promise.resolve(terminal.processId), timeout]).finally(() => clearTimeout(timer));
}

async function ownsRequest(request) {
  if (targetsThisWindow(request)) return true;
  if (!nonEmpty(request.shells)) return false;
  for (const terminal of vscode.window.terminals) {
    let pid;
    try { pid = await terminalPid(terminal); } catch { continue; }
    if (Number.isInteger(pid) && request.shells.includes(pid)) return true;
  }
  return false;
}

async function targetedElsewhere(request) {
  return (nonEmpty(request.hosts) || nonEmpty(request.shells)) && !(await ownsRequest(request));
}

function readRequest(root, id) {
  try { return JSON.parse(fs.readFileSync(path.join(root, 'launches', `${id}.json`), 'utf8')); }
  catch { return null; }
}

const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Focus reveals a Claude session already open in this window. The Claude command resumes
// the session in a new tab when it is not open here, so a focus request must name this
// window's extension host; an untargeted one never falls through to the last active window.
async function focusSession(request) {
  if (!targetsThisWindow(request)) throw new Error('focus requests must target a window');
  if (typeof request.session !== 'string' || !SESSION_ID.test(request.session) ||
      !Number.isFinite(request.created_at) ||
      Math.abs(Date.now() / 1000 - request.created_at) > 300) {
    throw new Error('invalid or expired focus request');
  }
  await vscode.commands.executeCommand('claude-vscode.primaryEditor.open', request.session);
}

const RELAY_TOKEN = /^relay:[0-9a-f]{8}$/;

// A Claude CLI session continues in a new integrated terminal. The text is typed into a
// shell, so it is built only from the validated token, never from request.prompt or any
// other free text in the request, which could carry shell syntax.
function openTerminal(request) {
  if (typeof request.token !== 'string' || !RELAY_TOKEN.test(request.token) ||
      typeof request.handoff !== 'string' || !Number.isFinite(request.created_at) ||
      Math.abs(Date.now() / 1000 - request.created_at) > 300 ||
      !fs.statSync(request.handoff).isFile()) {
    throw new Error('invalid or expired handoff request');
  }
  let cwd;
  try {
    if (typeof request.cwd === 'string' && path.isAbsolute(request.cwd) &&
        fs.statSync(request.cwd).isDirectory()) cwd = request.cwd;
  } catch { /* a missing folder falls back to the terminal default */ }
  const text = `claude "${request.token} continue from the saved handoff."`;
  const terminal = vscode.window.createTerminal({ name: 'Claude relay', cwd });
  terminal.show();
  terminal.sendText(text, true);
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
    if (request && await ownsRequest(request)) await openRequest(root, id, false);
  }
}

async function openRequest(root, id, viaUri) {
  const launch = path.join(root, 'launches', `${id}.json`);
  let request;
  try {
    request = JSON.parse(fs.readFileSync(launch, 'utf8'));
    if (viaUri && request && await targetedElsewhere(request)) return;  // checked before claiming
    fs.unlinkSync(launch);  // consume the request so its URI cannot be replayed
  } catch (error) {
    // Already claimed through this window's other path, or withdrawn by the helper.
    if (error.code === 'ENOENT') return;
    writeAck(root, id, { status: 'error', error: String(error.message || error) });
    return;
  }
  try {
    if (request?.action === 'focus') {
      await focusSession(request);
      writeAck(root, id, { status: 'focused', session: request.session });
      return;
    }
    if (request?.client === 'claude-terminal') {
      openTerminal(request);
      writeAck(root, id, { status: 'opened', client: 'claude-terminal' });
      return;
    }
    const isCodex = request.client === 'codex';
    const folder = isCodex ? handoffsFolder(root) : null;
    const resolvedHandoff = isCodex && typeof request.handoff === 'string'
      ? realHandoffPath(folder, request.handoff) : request.handoff;
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
      handoff = readHandoff(folder, resolvedHandoff);
      if (handoff === null) throw new Error('invalid or expired handoff request');
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

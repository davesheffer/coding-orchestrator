const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');

function handoffHome() {
  return process.env.ORCHESTRATOR_HANDOFF_HOME || path.join(os.homedir(), '.coding-orchestrator');
}

function writeAck(root, id, result) {
  const folder = path.join(root, 'acks-v2');
  fs.mkdirSync(folder, { recursive: true });
  const file = path.join(folder, `${id}.json`);
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(result), 'utf8');
  fs.renameSync(temporary, file);
}

function matchingWorkspace(requested) {
  if (typeof requested !== 'string' || !path.isAbsolute(requested)) return null;
  let target;
  try { target = fs.realpathSync.native(requested); } catch { return null; }
  for (const folder of vscode.workspace.workspaceFolders || []) {
    if (folder.uri.scheme !== 'file') continue;
    let root;
    try { root = fs.realpathSync.native(folder.uri.fsPath); } catch { continue; }
    if (path.relative(root, target) === '') {
      return folder.uri.fsPath;
    }
  }
  return null;
}

async function waitForNewTab(previous, client, id, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tabs = vscode.window.tabGroups.all.flatMap(group => group.tabs);
    const opened = tabs.find(tab => !previous.has(tab) &&
      (client === 'codex' ? tab.input?.uri?.scheme === 'openai-codex' &&
                            tab.input.uri.fragment === id
                          : ['claudeVSCodePanel', 'mainThreadWebview-claudeVSCodePanel']
                              .includes(tab.input?.viewType)));
    if (opened) return opened;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const observed = vscode.window.tabGroups.all.flatMap(group => group.tabs)
    .filter(tab => tab.input?.uri?.scheme === 'openai-codex' ||
                   tab.input?.viewType === 'claudeVSCodePanel')
    .slice(-6)
    .map(tab => `${previous.has(tab) ? 'old' : 'new'}:${tab.input?.uri?.toString() || tab.input?.viewType}`);
  throw new Error(`new ${client} tab was not observed; agent tabs: ${observed.join(', ')}`);
}

async function tryHandleId(id) {
  if (!/^[0-9a-f]{32}$/.test(id || '')) return;
  const root = handoffHome();
  const source = path.join(root, 'launches-v2', `${id}.json`);
  let request;
  try { request = JSON.parse(fs.readFileSync(source, 'utf8')); } catch { return; }
  if (Math.abs(Date.now() / 1000 - request.created_at) > 30) return;
  const editorWorkspace = matchingWorkspace(request.workspace);
  if (!editorWorkspace) return;
  if (vscode.window.state && !vscode.window.state.focused &&
      Date.now() / 1000 - request.created_at < 2) return;
  const claims = path.join(root, 'claims-v2');
  fs.mkdirSync(claims, { recursive: true });
  const claimed = path.join(claims, `${id}.json`);
  try { fs.renameSync(source, claimed); } catch { return; }
  try {
    if (!['codex', 'claude'].includes(request.client) ||
        typeof request.handoff !== 'string' || typeof request.prompt !== 'string' ||
        !Number.isFinite(request.created_at) || !fs.statSync(request.handoff).isFile()) {
      throw new Error('invalid handoff request');
    }
    let tab;
    if (request.client === 'claude') {
      const previous = new Set(vscode.window.tabGroups.all.flatMap(group => group.tabs));
      await vscode.commands.executeCommand('claude-vscode.primaryEditor.open', undefined, request.prompt);
      tab = await waitForNewTab(previous, 'claude', id);
    } else {
      const previous = new Set(vscode.window.tabGroups.all.flatMap(group => group.tabs));
      const panel = vscode.Uri.parse('openai-codex://route/extension/panel/new')
        .with({ fragment: id });
      await vscode.commands.executeCommand('vscode.openWith', panel,
                                           'chatgpt.conversationEditor',
                                           { preview: false, preserveFocus: false });
      tab = await waitForNewTab(previous, 'codex', id);
      const handoff = fs.readFileSync(request.handoff, 'utf8');
      await vscode.env.clipboard.writeText(`${request.prompt}\n\nSaved handoff:\n${handoff}`);
    }
    writeAck(root, id, { status: 'opened', client: request.client,
                         workspace: request.workspace, editorWorkspace,
                         tab: tab?.input?.uri?.toString() || tab?.input?.viewType });
  } catch (error) {
    writeAck(root, id, { status: 'error', error: String(error.message || error) });
  } finally {
    try { fs.unlinkSync(claimed); } catch { /* already processed */ }
  }
}

async function scanPending() {
  let files;
  try { files = fs.readdirSync(path.join(handoffHome(), 'launches-v2')); } catch { return; }
  for (const file of files) {
    if (/^[0-9a-f]{32}\.json$/.test(file)) await tryHandleId(file.slice(0, -5));
  }
}

async function handleUri(uri) {
  if (uri.path !== '/open') return;
  await tryHandleId(new URLSearchParams(uri.query).get('id'));
}

function activate(context) {
  context.subscriptions.push(vscode.window.registerUriHandler({ handleUri }));
  let scanning = false;
  const poll = async () => {
    if (scanning) return;
    scanning = true;
    try { await scanPending(); } finally { scanning = false; }
  };
  const timer = setInterval(poll, 300);
  timer.unref?.();
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  void poll();
}

function deactivate() {}

module.exports = { activate, deactivate, handleUri, scanPending, tryHandleId };

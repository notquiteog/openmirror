'use strict';

/**
 * The extension: a webview panel, a bridge, and nothing else.
 *
 * Everything that decides anything lives in `src/host.js`. This file is the
 * plumbing around it, and it is deliberately dumb: read the settings, build a
 * host, hand it a webview and a log, register the commands, and get out of the
 * way. The reasoning that matters is not in here.
 *
 * Four decisions are, and they are the ones a reader should be able to find.
 *
 * 1. `vscode` is required lazily, and `activate()` accepts it as an argument.
 *    The module only exists inside the extension host, so a top-level
 *    `require('vscode')` would make this file unreadable by `node --test`, by
 *    `node --check`'s parser in a plain process, and by anything that reads a
 *    JavaScript file without running it. The second argument is the seam: VS
 *    Code calls `activate(context)` and never passes one, a test passes a fake,
 *    and neither path has to know about the other.
 *
 * 2. `localResourceRoots` is `media/` and nothing else. A webview is a browser
 *    context VS Code renders; if it can load `file:` URLs from the workspace it
 *    can be made to load one, and the extension's whole safety argument -- one
 *    root, confined, nothing else -- is worth exactly as much as the weakest
 *    thing that can reach the repository. The panel gets its own directory.
 *
 * 3. The daemon's token never crosses into the page, and `src/host.js` is where
 *    that is enforced: it refuses a frame carrying the token on its way to
 *    `webview.postMessage`. This file holds the token only inside a `Host`.
 *
 * 4. Nothing is started on anybody's behalf. The extension is a client, in the
 *    same sense `openmirror/tui.py` is: it starts no daemon, spawns no agent,
 *    and keeps no state that outlives the panel. There is a command that runs
 *    `openmirror serve` in a terminal, and it exists because the alternative --
 *    a panel that cannot tell you how to start the thing it is a client of --
 *    leaves you reading a log file.
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const Host = require('./host');
const { resolveDaemon, DEFAULT_HOST, DEFAULT_PORT } = require('./host');

/** The panel's view type. Fixed, because `retainContextWhenHidden` and any
 *  future `registerWebviewViewProvider` both key off it. */
const VIEW_TYPE = 'openmirror.panel';

/** The webview's own files. `panel.html` is the one that is required, and it
 *  links its own stylesheet and script; these two are the fallback for a page
 *  that does not, so a differently shaped `media/` is an unstyled panel rather
 *  than an error. */
const MEDIA_ROOT = path.join(__dirname, '..', 'media');
const MEDIA_PAGE = 'panel.html';
const MEDIA_SCRIPT = 'panel.js';
const MEDIA_STYLE = 'panel.css';

const SETTINGS_SECTION = 'openmirror';

/** Changing one of these means the open panel is talking to the wrong daemon. */
const LINK_SETTINGS = ['host', 'port', 'token'];

/** Changing one of these means the *next* conversation starts differently. The
 *  daemon reads them when a session is created, and a session that already
 *  exists cannot be re-created by editing a setting. */
const CONVERSATION_SETTINGS = ['model', 'provider', 'mode', 'effort', 'root'];

/** The six modes `openmirror/agent/approval.py` defines, with the same one-line
 *  descriptions its own docstring gives, so the picker reads the way the modes
 *  are documented rather than inventing new prose for them. */
const MODES = [
  { mode: 'read_only', detail: 'nothing that changes anything, ever' },
  { mode: 'plan', detail: 'reads run; nothing else does until a plan is approved' },
  { mode: 'ask', detail: 'reads run; everything else is asked (the default)' },
  { mode: 'auto_edit', detail: 'reads and file writes run; commands are asked' },
  { mode: 'trusted', detail: 'commands run too; destructive things are asked' },
  { mode: 'unrestricted', detail: 'nothing is asked' },
];

/** `openmirror chat`'s levels, from the `/think` row in docs/CLI.md. */
const EFFORT_LEVELS = ['off', 'low', 'medium', 'high', 'xhigh', 'max', 'default'];

/** What the "start the daemon" command runs. A command a person can read in a
 *  terminal rather than a spawn of a resolved binary: it should be obvious what
 *  this button did. */
const START_COMMAND = 'openmirror serve';

/** One probe a second for this many, and then stop. A bounded wait rather than a
 *  poller that lives as long as the panel: somebody who pressed the button and
 *  walked away should not leave a timer running all afternoon. */
const PROBE_FIRST_MS = 500;
const PROBE_EVERY_MS = 1000;
const PROBE_ATTEMPTS = 10;

let api = null;
let context = null;
let deps = {};
let clock = { setTimeout, clearTimeout };
let output = null;
let panel = null;
let host = null;
let lastHook = '';
let rootOverride = '';

// -- the vscode module -------------------------------------------------------

/**
 * `require('vscode')` at the moment it is wanted rather than at the top of the
 * file, for the reason in the header: this module has to load in a plain Node
 * process, and one that cannot is one that cannot be tested.
 */
function vscode() {
  if (!api) {
    // eslint-disable-next-line global-require
    api = require('vscode');
  }
  return api;
}

// -- logging ----------------------------------------------------------------

/** Everything the host says, and everything this file decides to say, in one
 *  channel the person debugging the panel will already have open. */
function log(message) {
  if (!output) {
    return;
  }
  output.appendLine(`openmirror: ${String(message === undefined ? '' : message)}`);
}

/**
 * A failure somebody has to be told about: into the log, into the panel if it
 * is open, and as a notification.
 *
 * All three, because they answer different questions. The log is what is
 * happening, the panel is what the panel should say, and the notification is
 * the only one of the three somebody sees if they never open either of the
 * others. Notification rather than modal, because a modal would make a typo in
 * a token block the editor until it was dismissed.
 */
function fail(where, error) {
  const detail = error && error.message ? String(error.message) : String(error);
  log(`${where}: ${detail}`);
  if (error && error.stack) {
    log(String(error.stack).split('\n').slice(1, 4).join('\n'));
  }
  if (host) {
    host.say(detail, 'error');
  }
  const vsc = vscode();
  if (vsc.window && typeof vsc.window.showErrorMessage === 'function') {
    vsc.window.showErrorMessage(`openmirror: ${detail}`);
  }
}

/** Every command runs inside this. A throw that escapes into VS Code becomes a
 *  notification with no stack and no context, and a palette entry that fails
 *  that way is worse than a command that was never there. */
async function guard(id, run, args) {
  try {
    return await run(...(args || []));
  } catch (error) {
    fail(id, error);
    return undefined;
  }
}

// -- the host ----------------------------------------------------------------

function settings() {
  const workspace = vscode().workspace || {};
  if (typeof workspace.getConfiguration !== 'function') {
    return {};
  }
  return workspace.getConfiguration(SETTINGS_SECTION) || {};
}

function read(key) {
  const config = settings();
  if (typeof config.get !== 'function') {
    return '';
  }
  const value = config.get(key);
  return value === undefined || value === null ? '' : String(value);
}

/** The one root this panel works in: the folder the command was invoked on, the
 *  setting, or the first folder in the workspace, in that order. */
function workingRoot() {
  if (rootOverride) {
    return rootOverride;
  }
  const configured = read('root');
  if (configured) {
    return configured;
  }
  const folders = (vscode().workspace && vscode().workspace.workspaceFolders) || [];
  const first = folders.length ? folders[0] : null;
  return first && first.uri && first.uri.fsPath ? String(first.uri.fsPath) : '';
}

/**
 * Build a `Host` for this panel.
 *
 * The settings object is handed over as a `WorkspaceConfiguration`, unwrapped:
 * `resolveDaemon` accepts both shapes, and reading six keys into a plain object
 * here would be a second place for the resolution order to be wrong. `given` is
 * the injection seam for the three things that touch the outside world --
 * `fetch`, `createSocket` and `timers` -- so a test can open a panel with no
 * daemon and no real clock behind it.
 */
function buildHost(given) {
  const options = given || {};
  const name = (vscode().workspace && vscode().workspace.name) || 'VS Code';
  return new Host({
    settings: options.settings || settings(),
    env: options.env || process.env,
    root: options.root === undefined ? workingRoot() : options.root,
    model: options.model === undefined ? read('model') : options.model,
    provider: options.provider === undefined ? read('provider') : options.provider,
    mode: options.mode === undefined ? read('mode') : options.mode,
    effort: options.effort === undefined ? read('effort') : options.effort,
    title: options.title === undefined ? `${name} (VS Code)` : options.title,
    send: (frame) => {
      // Watching is not the same as reshaping: the frame goes out byte for byte
      // whatever is noticed on the way past. This one line is how the extension
      // knows a project hook wants running, which is the one daemon event that
      // has no frame to answer it with.
      noteHook(frame);
      if (panel) {
        panel.webview.postMessage(frame);
      }
    },
    log,
    fetch: options.fetch,
    // Left to `Host`'s own default when a test has not supplied one, so
    // there is exactly one place in the extension that knows the export name
    // `src/ws.js` publishes. It used to be two, and the second was wrong.
    createSocket: options.createSocket,
    timers: options.timers,
  });
}

/** A `hook.approval` event, remembered so the commands that answer one can
 *  offer the command as a default.
 *
 *  `PROTOCOL.md` has no frame for agreeing to a hook, and `Host.agreeHook()` is
 *  reachable only from here: the panel renders the banner, and the answer is a
 *  palette command. Reading it off the event rather than off the page keeps the
 *  page from needing a frame the protocol does not have. */
function noteHook(frame) {
  if (!frame || frame.t !== 'event' || !frame.event || frame.event.type !== 'hook.approval') {
    return;
  }
  const hook = frame.event.hook || {};
  lastHook = String(hook.command || hook.name || '');
  log(`a project hook wants to run: ${lastHook} (from ${hook.source || 'an unknown file'})`);
}

/** Open a conversation when the panel does not have one.
 *
 *  This is what opening the panel does, and it is what `openmirror chat` does
 *  too: the client asks the daemon for a session rather than inventing one. A
 *  daemon that is not there has already said so through `Host`, so the
 *  rejection here is only logged -- the panel has been told once already. */
function ensureSession(reason) {
  if (!host || host.sessionId || host.disposed) {
    return Promise.resolve(null);
  }
  return host.open({}).catch((error) => {
    log(`no conversation could be opened (${reason}): ${error && error.message}`);
    return null;
  });
}

// -- the webview -------------------------------------------------------------

/** The page's own CSP. `connect-src 'none'` is the load-bearing clause: the
 *  panel reaches the daemon through the extension host or not at all, and a
 *  webview that could open a socket itself would be a second implementation of
 *  the one thing that deliberately has exactly one. */
function policy(webview) {
  const source = webview.cspSource || '';
  return [
    "default-src 'none'",
    `img-src ${source} data:`,
    `style-src ${source} 'unsafe-inline'`,
    `script-src ${source}`,
    `font-src ${source}`,
    "connect-src 'none'",
  ].join('; ');
}

/** Insert `text` before the first `marker`, or append it. */
function before(html, marker, text) {
  const at = html.indexOf(marker);
  if (at < 0) {
    return `${html}\n${text}\n`;
  }
  return `${html.slice(0, at)}${text}\n${html.slice(at)}`;
}

/** A `media/` file, as a URI the webview may load. */
function mediaUri(webview, name) {
  return webview.asWebviewUri(vscode().Uri.file(path.join(MEDIA_ROOT, name))).toString();
}

/**
 * Turn the page's relative `src` and `href` into webview resource URIs.
 *
 * A webview's own origin is not the extension's directory, so `href="panel.css"`
 * in `media/panel.html` is a URL that resolves to nothing. Anything absolute,
 * a URL, a fragment, or a path that would climb out of `media/` is left exactly
 * as it was: this walks markup, and markup is allowed to reference things that
 * are not the page's own stylesheet.
 */
function absolutise(html, webview) {
  return html.replace(/(\s(?:src|href)\s*=\s*")([^"]*)(")/gi, (whole, open, value, close) => {
    if (!value || /^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(value)) {
      return whole;
    }
    if (value.startsWith('/') || value.split('/').includes('..')) {
      return whole;
    }
    return `${open}${mediaUri(webview, value)}${close}`;
  });
}

/**
 * `media/panel.html`, ready to hand to the webview.
 *
 * Two substitutions, which `media/panel.html` documents in a comment at the top
 * of itself: `{{cspSource}}`, the webview's origin, and `{{nonce}}`, a fresh
 * one per load. The nonce goes in the policy and on the script element, and a
 * nonce in a policy that is not on the element authorises nothing, which is why
 * it is generated here and not in the page.
 *
 * A page that carries its own policy is left to it. Two policies in one document
 * are the stricter of the two, so adding one to a page that already has a
 * stricter one could only break something; the policy below is here for a page
 * that has none, along with the asset links, so a differently shaped `media/`
 * is still a working panel rather than an unstyled one.
 */
function pageHtml(webview) {
  const page = path.join(MEDIA_ROOT, MEDIA_PAGE);
  let html;
  try {
    html = fs.readFileSync(page, 'utf8');
  } catch (error) {
    log(`media/${MEDIA_PAGE} could not be read: ${error && error.message}`);
    return missingPage(webview);
  }
  const nonce = crypto.randomBytes(16).toString('base64');
  html = html.split('{{cspSource}}').join(webview.cspSource || '').split('{{nonce}}').join(nonce);
  html = absolutise(html, webview);
  if (/Content-Security-Policy/i.test(html)) {
    return html;
  }
  const head = [`<meta http-equiv="Content-Security-Policy" content="${policy(webview)}">`];
  if (fs.existsSync(path.join(MEDIA_ROOT, MEDIA_STYLE))) {
    head.push(`<link rel="stylesheet" href="${mediaUri(webview, MEDIA_STYLE)}">`);
  }
  if (fs.existsSync(path.join(MEDIA_ROOT, MEDIA_SCRIPT))) {
    head.push(`<script src="${mediaUri(webview, MEDIA_SCRIPT)}"></script>`);
  }
  return before(html, '</head>', head.join('\n'));
}

/** The page when `media/panel.html` is not there. A missing asset is a broken
 *  install, and saying so in the panel beats a blank window with an exception
 *  behind it that nobody is ever going to open. */
function missingPage(webview) {
  return [
    '<!DOCTYPE html>',
    '<html lang="en">',
    '<head>',
    '<meta charset="utf-8">',
    `<meta http-equiv="Content-Security-Policy" content="${policy(webview)}">`,
    '<title>openmirror</title>',
    '</head>',
    '<body>',
    `<p>openmirror: media/${MEDIA_PAGE} is missing from this install, so the panel has nothing to render.</p>`,
    '<p>The exact error is in the Output panel, under "openmirror".</p>',
    '</body>',
    '</html>',
    '',
  ].join('\n');
}

/**
 * The page reloaded: send the state again.
 *
 *  `ready` is the only frame that means "the page can hear me", and `Host`
 *  answers it with `config` and the `/` menu. Routing a reload through the same
 *  frame the first load uses is the point: there is one answer to a page that
 *  has just loaded, not two that can drift apart.
 */
function reloaded() {
  log('the webview reloaded; sending the session state again');
  return host.handle({ t: 'ready' });
}

/** Every frame from the webview goes to the host and nowhere else.
 *
 *  `Host.handle()` already catches what its own handlers throw; the `catch`
 *  here is for a frame that is not an object, a host that has been disposed,
 *  and the version skew where `media/` and this file disagree about the shape of
 *  a frame. None of those may reach VS Code. */
async function onFrame(frame) {
  if (!frame || typeof frame !== 'object' || Array.isArray(frame)) {
    log('ignoring a frame from the webview that is not an object');
    return;
  }
  if (!host) {
    log('ignoring a frame from the webview: there is no daemon link open');
    return;
  }
  // The reload signal is `type`, not `t`; the protocol's own frames are `t`.
  // Accept either so a page that sends the documented frame and a page that
  // sends the documented signal both work.
  if (String(frame.t || '') === 'replay' || String(frame.type || '') === 'replay') {
    await reloaded();
    return;
  }
  try {
    await host.handle(frame);
  } catch (error) {
    fail('a frame from the webview', error);
  }
}

/**
 * Open the panel, or bring the one that is open back to the front.
 *
 * `resource` is whatever the editor title button or the explorer's context menu
 * passed: a `Uri`, for a folder, and its `fsPath` becomes this panel's working
 * root. A panel beside the editor rather than replacing it, because this is a
 * conversation about the file you are looking at and not instead of it.
 */
function openPanel(resource) {
  const vsc = vscode();
  rootOverride = folderOf(resource);
  if (panel) {
    panel.reveal(vsc.ViewColumn.Beside, true);
    return Promise.resolve(panel);
  }

  panel = vsc.window.createWebviewPanel(VIEW_TYPE, 'openmirror', {
    viewColumn: vsc.ViewColumn.Beside,
    preserveFocus: true,
  }, {
    enableScripts: true,
    // The page must not be able to invoke commands. It has no business running
    // one in this extension, and `true` is the only other answer.
    enableCommandUris: false,
    // media/ and nothing else. Not the extension root, not the workspace.
    localResourceRoots: [vsc.Uri.file(MEDIA_ROOT)],
    retainContextWhenHidden: true,
  });

  const webview = panel.webview;
  // Before the HTML is assigned: the page starts posting the moment it loads.
  webview.onDidReceiveMessage((frame) => onFrame(frame), undefined, context.subscriptions);
  webview.html = pageHtml(webview);

  if (!host || host.disposed) {
    host = buildHost(deps);
  }

  // A host that outlives the tab is a live socket and a reconnect timer that
  // keep trying for a panel nobody is looking at.
  panel.onDidDispose(() => {
    log('the panel was closed');
    if (host) {
      host.dispose();
      host = null;
    }
    panel = null;
  }, undefined, context.subscriptions);

  ensureSession('the panel was opened');
  return Promise.resolve(panel);
}

function folderOf(resource) {
  if (typeof resource === 'string' && resource) {
    return resource;
  }
  if (resource && typeof resource.fsPath === 'string' && resource.fsPath) {
    return String(resource.fsPath);
  }
  // The explorer's context menu hands over `(uri, uris[])`.
  if (Array.isArray(resource) && resource.length) {
    return folderOf(resource[0]);
  }
  return '';
}

// -- commands ----------------------------------------------------------------

async function newConversation() {
  await openPanel();
  await host.handle({ t: 'new' });
}

/** The stored conversations, as a picker.
 *
 *  `Host.storedSessions()` reads the daemon's own store, so the list is the
 *  daemon's and not this file's guess at it. The folder is shown with each one
 *  because it matters: resuming does not move a conversation into the workspace
 *  you are in now, and a list of bare titles would hide that. */
async function resumeConversation() {
  await openPanel();
  let sessions;
  try {
    sessions = await host.storedSessions(100);
  } catch (error) {
    fail('the stored conversations could not be listed', error);
    return;
  }
  if (!Array.isArray(sessions) || !sessions.length) {
    host.say('this daemon has no stored conversations yet', 'warn');
    return;
  }
  const picked = await pick(sessions.map((session) => ({
    label: String((session && session.title) || '(untitled)'),
    description: String((session && session.id) || ''),
    detail: [String((session && session.root) || ''), `${Number((session && session.turns) || 0)} turns`]
      .filter(Boolean)
      .join(' - '),
    id: String((session && session.id) || ''),
  })), 'Resume which conversation?');
  if (!picked || !picked.id) {
    return;
  }
  await host.handle({ t: 'open', id: picked.id });
}

/** A copy of this conversation, sharing the files with the original.
 *  `Host.forkTo()` leaves the original completely alone, which is the whole
 *  reason it exists: a "clear" would throw away the context that explains what
 *  changed your mind. */
async function forkConversation() {
  await openPanel();
  await host.handle({ t: 'fork' });
}

async function showContextReport() {
  await openPanel();
  await host.handle({ t: 'context' });
}

async function setApprovalMode() {
  await openPanel();
  const current = host && host.info ? String(host.info.mode || '') : '';
  const picked = await pick(MODES.map((entry) => ({
    label: entry.mode,
    description: current === entry.mode ? '(now)' : '',
    detail: entry.detail,
    mode: entry.mode,
  })), 'How much may the agent do without asking?');
  if (!picked || !picked.mode) {
    return;
  }
  await host.handle({ t: 'policy', mode: picked.mode });
}

async function setThinkingLevel() {
  await openPanel();
  const picked = await pick(EFFORT_LEVELS.map((level) => ({ label: level })), 'How hard should the model think?');
  if (!picked || !picked.label) {
    return;
  }
  await host.handle({ t: 'effort', level: picked.label });
}

/**
 * A model name, as a box rather than a list.
 *
 * A picker would mean shipping a list, and the only honest list is the
 * daemon's, which this file has no way to read. `/model` is a turn command on
 * the agent socket rather than a socket message of its own, so this goes
 * through the protocol's `model` frame and the daemon answers it the way the
 * terminal does.
 */
async function setModel() {
  const current = host && host.info ? String(host.info.model || '') : '';
  const answer = await ask({
    prompt: 'Which model? A provider name such as anthropic, or whatever your provider expects.',
    placeHolder: 'openmirror models lists what this daemon can reach',
    value: current,
  });
  if (!answer || !answer.trim()) {
    return;
  }
  await openPanel();
  await host.handle({ t: 'model', name: answer.trim() });
}

/** Stop the turn that is running. Queued rather than dropped when the socket is
 *  down, which is `Host`'s rule: an interrupt that is thrown away is a turn that
 *  runs to its end. */
async function stopTurn() {
  await openPanel();
  await host.handle({ t: 'interrupt' });
}

/**
 * Agree to, or refuse, a project hook.
 *
 * A hook is somebody else's code in somebody else's repository asking to run on
 * yours. It can only stop a call, never allow one, so agreeing is not as large
 * a decision as it looks -- and a banner in the panel with a button that does
 * nothing would be worse than no banner, so the answer is a command and the
 * panel names it.
 *
 * The command is asked for rather than assumed: the answer is remembered from
 * the last `hook.approval` and offered as the default, which is right most of
 * the time and editable when it is not.
 */
async function decideHook(allow) {
  await openPanel();
  if (!host.sessionId) {
    host.say('there is no conversation for a hook to belong to', 'warn');
    return;
  }
  const answer = await ask({
    prompt: allow ? 'Which hook command are you agreeing to run?' : 'Which hook command are you refusing?',
    placeHolder: 'the command, exactly as the banner shows it',
    value: lastHook,
  });
  if (!answer || !answer.trim()) {
    return;
  }
  try {
    await host.agreeHook(answer.trim(), allow);
  } catch (error) {
    fail('the hook decision could not be sent', error);
    return;
  }
  log(`${allow ? 'agreed to' : 'refused'} the hook ${answer.trim()}`);
}

/**
 * The command line for "start the daemon", with the configured host and port
 * spelled out when they are not the daemon's own defaults.
 *
 * Anything that could break out of the double quotes is dropped rather than
 * escaped: a host name with a backtick or a `$` in it is not a host name, and
 * the alternative is a shell that can be talked into running something else.
 */
function startLine() {
  const where = resolveDaemon(settings(), process.env);
  const bits = [START_COMMAND];
  if (where.host && where.host !== DEFAULT_HOST && !/["`$\\]/.test(where.host)) {
    bits.push(`--host "${where.host}"`);
  }
  if (where.port !== DEFAULT_PORT) {
    bits.push(`--port ${where.port}`);
  }
  return bits.join(' ');
}

/**
 * Open a terminal and run `openmirror serve` in it.
 *
 * Offered, never done automatically, and it will stay that way: a process
 * started behind somebody's back is the behaviour this project refuses in the
 * CLI, in the TUI and in the browser. What this does instead is open a terminal
 * they can see, watch and Ctrl-C, and then wait a bounded ten seconds for the
 * daemon to answer so an open panel picks it up on its own.
 */
function startDaemon() {
  const line = startLine();
  const terminal = vscode().window.createTerminal({ name: 'openmirror serve' });
  terminal.show();
  terminal.sendText(line);
  log(`started a terminal running: ${line}`);
  if (host) {
    host.say(`\`${line}\` is running in a terminal.`, 'info');
  }
  watchForDaemon();
  return terminal;
}

/**
 * Notice a daemon that has just been started.
 *
 * One probe a second, ten of them, then give up and say so. This is here so that
 * pressing the button and watching the panel come alive is one action rather
 * than two; it is not a supervisor, it restarts nothing, and the timer is on the
 * extension's subscription list so closing the panel or the window stops it.
 */
function watchForDaemon() {
  const state = { timer: null, left: PROBE_ATTEMPTS, done: false };
  const stop = () => {
    state.done = true;
    if (state.timer !== null) {
      clock.clearTimeout(state.timer);
      state.timer = null;
    }
  };
  const probe = async () => {
    state.timer = null;
    if (state.done || !host || host.disposed) {
      stop();
      return;
    }
    try {
      // `storedSessions` rather than anything that sounds like a health check:
      // it is a real endpoint the daemon answers, and an empty list from a
      // brand new daemon is an answer rather than a failure.
      await host.storedSessions(1);
    } catch (error) {
      state.left -= 1;
      if (state.left > 0) {
        state.timer = clock.setTimeout(probe, PROBE_EVERY_MS);
        return;
      }
      log(`the daemon did not answer within ${PROBE_ATTEMPTS}s of being started`);
      host.say('the daemon has not answered yet. It prints its own address when it starts.', 'warn');
      stop();
      return;
    }
    stop();
    if (host.sessionId) {
      host.say('the daemon is up; this panel is attached to it.', 'info');
    } else {
      await ensureSession('the daemon had just been started');
    }
  };
  state.timer = clock.setTimeout(probe, PROBE_FIRST_MS);
  context.subscriptions.push({ dispose: stop });
  return stop;
}

function showOutput() {
  if (output) {
    output.show(true);
  }
}

/**
 * Every command in `contributes.commands`, and only those.
 *
 * One table rather than a `registerCommand` at each site: the test walks this
 * object against `package.json`, which is the assertion that matters, because it
 * fails the moment a command is added to one and not the other. A command in the
 * manifest with no handler is a command somebody presses and nothing happens.
 */
const COMMANDS = {
  'openmirror.openPanel': (resource) => openPanel(resource),
  'openmirror.newConversation': () => newConversation(),
  'openmirror.resumeConversation': () => resumeConversation(),
  'openmirror.forkConversation': () => forkConversation(),
  'openmirror.showContextReport': () => showContextReport(),
  'openmirror.setApprovalMode': () => setApprovalMode(),
  'openmirror.setThinkingLevel': () => setThinkingLevel(),
  'openmirror.setModel': () => setModel(),
  'openmirror.stopTurn': () => stopTurn(),
  'openmirror.agreeHook': () => decideHook(true),
  'openmirror.refuseHook': () => decideHook(false),
  'openmirror.startDaemon': () => startDaemon(),
  'openmirror.showOutput': () => showOutput(),
};

// -- lifecycle ---------------------------------------------------------------

/**
 * @param {object} context   the `ExtensionContext` VS Code passes.
 * @param {object} [api]     an injected `vscode`. VS Code never passes a second
 *   argument; this is how a test drives `activate` in a plain Node process,
 *   where `require('vscode')` cannot resolve.
 * @param {object} [deps]    `fetch`, `createSocket` and `timers` for the `Host`
 *   this builds, so a test can open a panel with nothing behind it.
 */
function activate(extensionContext, injected, injectedDeps) {
  api = injected || vscode();
  context = extensionContext || { subscriptions: [] };
  if (!Array.isArray(context.subscriptions)) {
    context.subscriptions = [];
  }
  deps = injectedDeps || {};
  // The post-start wait runs on the same clock as the host's retry loop, so
  // there is one timer seam rather than one here and one there.
  clock = deps.timers || { setTimeout, clearTimeout };

  output = api.window.createOutputChannel('openmirror');
  context.subscriptions.push(output);

  for (const [id, run] of Object.entries(COMMANDS)) {
    context.subscriptions.push(api.commands.registerCommand(id, (...args) => guard(id, run, args)));
  }
  context.subscriptions.push(api.workspace.onDidChangeConfiguration(onConfiguration));

  const where = resolveDaemon(settings(), process.env);
  log(`activated; the daemon would be at ${where.host}:${where.port}`);
  return {
    openPanel,
    showOutput,
    get host() {
      return host;
    },
    get panel() {
      return panel;
    },
  };
}

/**
 * A setting changed.
 *
 * Two cases, different enough to be worth two branches.
 *
 * `host`, `port` and `token` decide which daemon this panel is talking to, and
 * `Host` reads them once, when it is built. There is no setter, and inventing
 * one would mean rewriting the credentials of a live socket; so the panel says
 * plainly that it is still attached to the daemon it opened and that reopening
 * it picks up the change. Silently rebuilding the host under an open
 * conversation would throw that conversation away without saying so.
 *
 * The rest are defaults for the *next* conversation, so they are logged and
 * mentioned and nothing more. Moving a running conversation onto a new model or
 * a new root is not a thing anybody asked for.
 */
function onConfiguration(event) {
  if (!event || typeof event.affectsConfiguration !== 'function') {
    return;
  }
  if (!event.affectsConfiguration(SETTINGS_SECTION)) {
    return;
  }
  const link = LINK_SETTINGS.filter((key) => event.affectsConfiguration(`${SETTINGS_SECTION}.${key}`));
  if (link.length) {
    const names = link.map((key) => `${SETTINGS_SECTION}.${key}`).join(', ');
    log(`${names} changed`);
    if (host) {
      host.say(
        `${names} changed. This panel is still attached to the daemon it opened; `
        + 'close it and open the panel again to use the new one.',
        'warn',
      );
    }
    return;
  }
  const defaults = CONVERSATION_SETTINGS.filter((key) => event.affectsConfiguration(`${SETTINGS_SECTION}.${key}`));
  if (defaults.length) {
    const names = defaults.map((key) => `${SETTINGS_SECTION}.${key}`).join(', ');
    log(`${names} changed; it applies to the next new conversation`);
    if (host) {
      host.say(`${names} changed. New conversations start that way; this one is unchanged.`, 'info');
    }
  }
}

function deactivate() {
  if (host) {
    host.dispose();
    host = null;
  }
  if (panel) {
    const closing = panel;
    panel = null;
    closing.dispose();
  }
  if (output) {
    output.dispose();
    output = null;
  }
  rootOverride = '';
  clock = { setTimeout, clearTimeout };
  api = null;
}

// -- the two dialogs ---------------------------------------------------------

/** `showQuickPick` answers `undefined` when the person pressed Escape, and
 *  `undefined` is also what an empty answer means everywhere below it. */
async function pick(items, placeHolder) {
  const answer = await vscode().window.showQuickPick(items, { placeHolder });
  return Array.isArray(answer) ? answer[0] : answer;
}

async function ask(options) {
  return vscode().window.showInputBox(options);
}

module.exports = {
  activate,
  deactivate,
  COMMANDS,
  VIEW_TYPE,
  MEDIA_ROOT,
  buildHost,
  openPanel,
};
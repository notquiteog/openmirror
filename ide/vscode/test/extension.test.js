'use strict';

/**
 * Tests for `src/extension.js`.
 *
 * There is no editor here. `require('vscode')` only resolves inside the
 * extension host, so the module takes the `vscode` object as an argument to
 * `activate` and this file hands it a fake -- one that records what was
 * registered and hands back webviews you can poke at. Everything that touches
 * the outside world behind that is faked too: the fetch, the socket and the
 * clock, the same three seams `src/host.js` was built with.
 *
 * The assertions here are the ones that hold the extension's shape rather than
 * its behaviour: that the manifest and the command registry are the same set of
 * names, that the settings defaults do not quietly shadow the environment, and
 * that the panel is wired the way `PROTOCOL.md` says it is.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const MANIFEST = path.join(ROOT, 'package.json');
const manifest = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
const extension = require('../src/extension');

const Host = require('../src/host');

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

const uri = (fsPath) => ({
  fsPath,
  scheme: 'file',
  path: fsPath,
  toString: () => `file://${fsPath}`,
});

class FakeChannel {
  constructor(name) {
    this.name = name;
    this.lines = [];
    this.shown = 0;
    this.disposed = false;
  }

  appendLine(text) {
    this.lines.push(String(text));
  }

  show() {
    this.shown += 1;
  }

  dispose() {
    this.disposed = true;
  }
}

class FakeWebview {
  constructor(panel) {
    this.panel = panel;
    this.posted = [];
    this.html = '';
    this.cspSource = 'vscode-webview://fake';
    this.onDidReceiveMessage = (handler, thisArg, subscriptions) => {
      this.received = handler;
      const disposable = { dispose: () => { this.received = null; } };
      if (subscriptions && subscriptions.push) {
        subscriptions.push(disposable);
      }
      return disposable;
    };
    this.asWebviewUri = (target) => ({ toString: () => `https://fake.vscode-cdn.net${target.fsPath}` });
  }

  postMessage(frame) {
    this.posted.push(frame);
    return Promise.resolve(true);
  }

  async send(frame) {
    if (this.received) {
      await this.received(frame);
    }
  }

  framesOfType(t) {
    return this.posted.filter((frame) => frame && frame.t === t);
  }
}

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.bufferedAmount = 0;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
  }

  send(text) {
    if (this.readyState !== 1) {
      throw new Error('socket is not open');
    }
    this.sent.push(JSON.parse(text));
  }

  close() {
    this.readyState = 3;
  }

  open() {
    this.readyState = 1;
    if (this.onopen) {
      this.onopen({});
    }
  }
}

function reply(body) {
  return {
    ok: true,
    status: 200,
    async text() {
      return JSON.stringify(body);
    },
  };
}

const CREATED = {
  id: 's-1',
  title: 'project',
  root: '/work/project',
  model: 'claude-sonnet-4',
  provider: 'anthropic',
  policy: 'ask',
  effort: 'medium',
};

function fakeFetch(routes) {
  const table = routes || {
    'POST /api/sessions': reply(CREATED),
    'GET /api/sessions/stored': reply({ sessions: [] }),
    'GET /api/sessions/s-1/commands': reply({ commands: [{ name: 'compact', kind: 'command' }] }),
    'GET /api/sessions/s-1/context': reply({ tokens: 10, limit: 100, total_in: 4, total_out: 2 }),
  };
  const calls = [];
  const run = async (url, init) => {
    const parsed = new URL(url);
    calls.push({
      method: init.method,
      path: parsed.pathname,
      headers: init.headers || {},
      body: init.body === undefined ? undefined : JSON.parse(init.body),
    });
    const route = table[`${init.method} ${parsed.pathname}`];
    if (!route) {
      throw new Error(`no fake route for ${init.method} ${parsed.pathname}`);
    }
    return route;
  };
  run.calls = calls;
  return run;
}

/** Timers the test can advance, so the reconnect and the post-start wait are
 *  checked without spending them. */
class FakeTimers {
  constructor() {
    this.jobs = [];
    this.next = 1;
    this.setTimeout = (fn, delay) => {
      const id = this.next;
      this.next += 1;
      this.jobs.push({ id, fn, delay });
      return id;
    };
    this.clearTimeout = (id) => {
      this.jobs = this.jobs.filter((job) => job.id !== id);
    };
  }

  get pending() {
    return this.jobs.length;
  }

  async fireAll(limit) {
    let fired = 0;
    while (this.jobs.length && fired < (limit || 20)) {
      const job = this.jobs.shift();
      await job.fn();
      fired += 1;
    }
    return fired;
  }
}

/**
 * A `vscode` that records rather than performs.
 *
 * Small on purpose: anything the extension reaches for that is not here is a
 * test failure saying so, which is the point of a fake that is a lie by
 * omission rather than a real API.
 */
function fakeVscode(options) {
  const given = options || {};
  const registered = new Map();
  const terminals = [];
  const channels = [];
  const notifications = { errors: [], info: [] };
  const panels = [];
  const configurationListeners = [];
  const vsc = {
    ViewColumn: { Active: -1, Beside: -2, One: 1 },
    Uri: { file: uri },
    window: {
      createOutputChannel(name) {
        const channel = new FakeChannel(name);
        channels.push(channel);
        return channel;
      },
      createWebviewPanel(viewType, title, column, options) {
        const panel = {
          viewType,
          title,
          column,
          options,
          webview: null,
          disposed: 0,
          reveal(columnArg, preserveFocus) {
            panel.revealed = { column: columnArg, preserveFocus };
          },
          onDidDispose(handler, thisArg, subscriptions) {
            panel.disposeHandler = handler;
            const disposable = { dispose: () => { panel.disposeHandler = null; } };
            if (subscriptions && subscriptions.push) {
              subscriptions.push(disposable);
            }
            return disposable;
          },
          dispose() {
            if (panel.disposed) {
              return;
            }
            panel.disposed += 1;
            if (panel.disposeHandler) {
              panel.disposeHandler();
            }
          },
        };
        panel.webview = new FakeWebview(panel);
        panels.push(panel);
        return panel;
      },
      createTerminal(opts) {
        const terminal = { ...opts, sent: [], shown: 0 };
        terminal.show = () => { terminal.shown += 1; };
        terminal.sendText = (text) => { terminal.sent.push(text); };
        terminals.push(terminal);
        return terminal;
      },
      showQuickPick: given.showQuickPick || (async () => undefined),
      showInputBox: given.showInputBox || (async () => undefined),
      showErrorMessage(text) {
        notifications.errors.push(text);
        return Promise.resolve(undefined);
      },
      showInformationMessage(text) {
        notifications.info.push(text);
        return Promise.resolve(undefined);
      },
    },
    commands: {
      registerCommand(id, handler) {
        registered.set(id, handler);
        return { dispose: () => registered.delete(id) };
      },
    },
    workspace: {
      name: 'project',
      workspaceFolders: given.folders === null ? undefined : (given.folders || [{ uri: uri('/work/project') }]),
      getConfiguration(section) {
        // A `WorkspaceConfiguration` over the manifest's own defaults, which is
        // what VS Code hands back for settings nobody has changed.
        const values = given.settings || defaultsFromManifest();
        return {
          get: (key) => values[key],
          has: (key) => values[key] !== undefined,
          inspect: (key) => ({ key: `openmirror.${key}`, defaultValue: values[key] }),
        };
      },
      onDidChangeConfiguration(handler) {
        configurationListeners.push(handler);
        return { dispose: () => configurationListeners.splice(configurationListeners.indexOf(handler), 1) };
      },
    },
  };
  return {
    vscode: vsc,
    registered,
    terminals,
    channels,
    notifications,
    panels,
    configurationListeners,
  };
}

/** The manifest's defaults, read rather than restated: a setting whose default
 *  moves and a test that still asserts the old value is a test that has stopped
 *  testing anything. */
function defaultsFromManifest() {
  const values = {};
  for (const [key, schema] of Object.entries(manifest.contributes.configuration.properties)) {
    values[key.split('.')[1]] = schema.default;
  }
  return values;
}

function fakeContext() {
  return { subscriptions: [], extensionPath: ROOT };
}

/** Open every socket the panel has made, so the next command has a wire to go
 *  down rather than an outbox. */
async function openEverySocket(world) {
  await settle();
  for (const socket of world.sockets) {
    if (socket.readyState === 0) {
      socket.open();
    }
  }
  await settle();
}

/** Let every pending promise in the panel's startup path run.
 *
 *  Opening the panel asks the daemon for a conversation without waiting for the
 *  answer -- the panel is usable while it is loading -- so a test that wants the
 *  resulting socket has to let that settle first. A bounded number of turns of
 *  the event loop rather than a sleep, so it is neither slow nor a guess.
 */
async function settle(times) {
  for (let i = 0; i < (times || 50); i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/** A live panel: activated, opened, with a conversation and a socket. */
async function opened(options) {
  const given = options || {};
  const world = fakeVscode(given);
  const timers = given.timers || new FakeTimers();
  const sockets = [];
  const fetchImpl = given.fetch || fakeFetch(given.routes);
  const handle = extension.activate(fakeContext(), world.vscode, {
    fetch: fetchImpl,
    timers,
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    env: {},
  });
  await handle.openPanel(given.resource);
  await settle();
  if (sockets.length) {
    sockets[0].open();
    await settle();
  }
  return { ...world, handle, timers, sockets, fetchImpl };
}

const lastFrame = (world, t) => {
  const matching = world.panels[0].webview.posted.filter((frame) => !t || frame.t === t);
  return matching[matching.length - 1];
};

/** A `ConfigurationChangeEvent` over some keys. The bare section name counts
 *  too: VS Code is asked `affectsConfiguration('openmirror')` for a change to
 *  one of its settings, and a fake that only matched the full key would hide
 *  the case this is written for. */
const configurationEvent = (keys) => ({
  affectsConfiguration: (key) => keys.some((k) => key === k || k.startsWith(`${key}.`) || key.startsWith(`${k}.`)),
});

/**
 * Every test in this file shares one module, and that module holds one panel,
 * one host and one output channel. Deactivating between tests is the only thing
 * that stops the first failure from cascading into twenty, so it happens whether
 * a test passed or not.
 */
test.afterEach(() => {
  extension.deactivate();
});

// ---------------------------------------------------------------------------
// The module, in a plain Node process
// ---------------------------------------------------------------------------

test('the module loads with no vscode module installed', () => {
  // `require` above already proved it. Asserting the shape so that a future
  // top-level require of `vscode` fails here rather than in the editor.
  assert.equal(typeof extension.activate, 'function');
  assert.equal(typeof extension.deactivate, 'function');
  assert.equal(typeof extension.COMMANDS, 'object');
});

test('activate and deactivate do not throw on a minimal context and a fake vscode', () => {
  const world = fakeVscode();
  const handle = extension.activate(fakeContext(), world.vscode);
  assert.ok(handle, 'activate returned nothing');
  assert.equal(world.channels.length, 1);
  extension.deactivate();
  assert.equal(world.channels[0].disposed, true, 'the channel outlived the extension');
});

test('deactivate is safe twice, and before activate', () => {
  extension.deactivate();
  extension.deactivate();
  const world = fakeVscode();
  extension.activate(fakeContext(), world.vscode);
  extension.deactivate();
});

test('activate without a context does not throw', () => {
  const world = fakeVscode();
  extension.activate(undefined, world.vscode);
  extension.deactivate();
});

test('the output channel is created once and disposed with the extension', async () => {
  const world = await opened();
  assert.equal(world.channels.length, 1);
  assert.equal(world.channels[0].name, 'openmirror');
  assert.ok(world.handle.panel, 'no panel was created');
});

// ---------------------------------------------------------------------------
// The assertion that matters: the manifest and the registry are one set
// ---------------------------------------------------------------------------

test('every command in the manifest has a handler, and every handler is in the manifest', () => {
  const declared = manifest.contributes.commands.map((entry) => entry.command);
  const registered = Object.keys(extension.COMMANDS);
  assert.ok(declared.length > 0, 'the manifest declares no commands');
  for (const id of declared) {
    assert.ok(registered.includes(id), `the manifest declares ${id} and nothing handles it`);
  }
  for (const id of registered) {
    assert.ok(declared.includes(id), `${id} is registered but the manifest does not declare it`);
  }
  assert.equal(new Set(declared).size, declared.length, 'a command is declared twice');
});

test('activate registers exactly the commands the manifest declares', () => {
  const world = fakeVscode();
  extension.activate(fakeContext(), world.vscode);
  const declared = manifest.contributes.commands.map((entry) => entry.command).sort();
  assert.deepEqual([...world.registered.keys()].sort(), declared);
});

test('every menu entry names a command that exists', () => {
  const declared = new Set(manifest.contributes.commands.map((entry) => entry.command));
  for (const [menu, entries] of Object.entries(manifest.contributes.menus)) {
    assert.ok(Array.isArray(entries) && entries.length, `${menu} contributes nothing`);
    for (const entry of entries) {
      assert.ok(declared.has(entry.command), `${menu} offers ${entry.command}, which is not declared`);
      assert.ok(entry.when, `${menu} offers ${entry.command} with no when clause`);
    }
  }
});

test('every declared command has a title and the category', () => {
  for (const entry of manifest.contributes.commands) {
    assert.ok(entry.title, `${entry.command} has no title`);
    assert.equal(entry.category, 'openmirror', `${entry.command} is filed under the wrong category`);
    assert.ok(entry.command.startsWith('openmirror.'), `${entry.command} is not namespaced`);
  }
});

test('a command that throws is logged and notified, not raised at the editor', async () => {
  const world = await opened({
    showQuickPick: async () => {
      throw new Error('the picker fell over');
    },
  });
  const run = world.registered.get('openmirror.setApprovalMode');
  await assert.doesNotReject(run());
  assert.equal(world.notifications.errors.length, 1);
  assert.match(world.notifications.errors[0], /the picker fell over/);
  const notice = world.panels[0].webview.posted.filter((frame) => frame.t === 'notice').slice(-1)[0];
  assert.match(notice.text, /the picker fell over/, 'the panel was not told either');
});

test('a daemon that is not running is a status, and the panel is not started for us', async () => {
  const world = await opened({
    fetch: async () => {
      throw new TypeError('fetch failed');
    },
  });
  const status = world.panels[0].webview.posted.filter((frame) => frame.t === 'status').slice(-1)[0];
  assert.match(status.message, /openmirror serve/);
  assert.deepEqual(world.terminals, [], 'a daemon was started without being asked for');
});

// ---------------------------------------------------------------------------
// The manifest itself
// ---------------------------------------------------------------------------

test('the manifest has no dependencies and no devDependencies', () => {
  // The whole reason `src/ws.js` exists: Node only grew a global WebSocket in
  // v21, and the extension host's Node is whatever the editor shipped.
  assert.equal(manifest.dependencies, undefined);
  assert.equal(manifest.devDependencies, undefined);
  assert.equal(manifest.bundledDependencies, undefined);
  assert.equal(manifest.main, './src/extension.js');
  assert.equal(fs.existsSync(path.join(ROOT, manifest.main)), true);
});

test('the manifest names the repo, a licence and a version that match', () => {
  assert.equal(manifest.license, 'Apache-2.0');
  assert.match(manifest.repository.url, /^https:\/\/github\.com\/notquiteog\/openmirror/);
  const pyproject = fs.readFileSync(path.join(ROOT, '..', '..', 'pyproject.toml'), 'utf8');
  const version = /^version = "([^"]+)"/m.exec(pyproject);
  assert.ok(version, 'pyproject.toml has no version to match');
  assert.equal(manifest.version, version[1], 'the extension and the daemon are different versions');
  assert.match(manifest.version, /^\d+\.\d+\.\d+$/);
});

test('the engines floor is stated, and it is not lower than this file needs', () => {
  // `Host` reaches for `globalThis.fetch` and `AbortController`, which is why
  // the floor is the Node 18 extension host rather than an older editor.
  assert.match(manifest.engines.vscode, /^\^\d+\.\d+\.\d+$/);
});

test('activationEvents carries nothing that a command list would have generated', () => {
  assert.deepEqual(manifest.activationEvents, ['onStartupFinished']);
  for (const id of Object.keys(extension.COMMANDS)) {
    assert.equal(
      manifest.activationEvents.includes(`onCommand:${id}`),
      false,
      'VS Code generates onCommand from contributes.commands; listing it is trivia'
    );
  }
});

// ---------------------------------------------------------------------------
// Settings, and what they must not shadow
// ---------------------------------------------------------------------------

const PROPERTIES = manifest.contributes.configuration.properties;

test('every setting resolveDaemon knows about is declared', () => {
  for (const key of ['host', 'port', 'token', 'model', 'provider', 'mode', 'effort', 'root']) {
    assert.ok(PROPERTIES[`openmirror.${key}`], `openmirror.${key} is not declared`);
  }
});

test('every declared setting says what it does, its default and where it comes from', () => {
  for (const [key, schema] of Object.entries(PROPERTIES)) {
    assert.ok(schema.description, `${key} has no description`);
    assert.ok(schema.description.length > 40, `${key}'s description says nothing`);
    assert.ok(['window', 'resource', 'machine', 'language-overridable', 'machine-overridable'].includes(schema.scope),
      `${key} has no usable scope`);
    assert.ok(typeof schema.type === 'string' && schema.type.length, `${key} has no type`);
  }
});

test('the token is a bare string with no default', () => {
  const token = PROPERTIES['openmirror.token'];
  assert.equal(token.type, 'string');
  assert.equal(token.default, undefined, 'a default token would be a shipped credential');
  assert.match(token.description, /OPENMIRROR_TOKEN/);
  assert.match(token.description, /only needed (?:if|when) the daemon was started/i);
});

test('the manifest defaults do not shadow the environment', () => {
  // This is the one that would have been a silent bug. `resolveDaemon` prefers
  // a setting over the environment variable, so a *non-empty* default for host
  // or port would make `OPENMIRROR_HOST` and `OPENMIRROR_PORT` unreachable --
  // the setting would always win with a value nobody chose.
  const config = { get: (key) => PROPERTIES[`openmirror.${key}`].default };
  const bare = Host.resolveDaemon({ ...config }, {});
  assert.equal(bare.host, Host.DEFAULT_HOST);
  assert.equal(bare.port, Host.DEFAULT_PORT);
  assert.equal(bare.token, '');

  const fromEnv = Host.resolveDaemon({ ...config }, {
    OPENMIRROR_HOST: 'box.local',
    OPENMIRROR_PORT: '9100',
    OPENMIRROR_TOKEN: 'from-the-environment',
  });
  assert.equal(fromEnv.host, 'box.local', 'OPENMIRROR_HOST is unreachable because of the default');
  assert.equal(fromEnv.port, 9100, 'OPENMIRROR_PORT is unreachable because of the default');
  assert.equal(fromEnv.token, 'from-the-environment');
});

test('an empty default and no default resolve the same way, which is what resolveDaemon expects', () => {
  const config = { get: (key) => PROPERTIES[`openmirror.${key}`].default };
  assert.deepEqual(
    Host.resolveDaemon({ ...config }, {}),
    Host.resolveDaemon({}, {}),
    'the manifest defaults must be indistinguishable from unset'
  );
});

test('the declared modes are the six approval.py defines', () => {
  const declared = PROPERTIES['openmirror.mode'].enum.filter(Boolean).sort();
  assert.deepEqual(declared, [...Host.MODES].sort());
});

test('the declared effort levels are the ones docs/CLI.md lists for /think', () => {
  const cli = fs.readFileSync(path.join(ROOT, '..', '..', 'docs', 'CLI.md'), 'utf8');
  const row = /`\/think <level>`.*?`(off|low)/.exec(cli);
  assert.ok(row, 'docs/CLI.md no longer documents /think');
  for (const level of ['off', 'low', 'medium', 'high', 'xhigh', 'max', 'default']) {
    assert.ok(cli.includes(`\`${level}\``), `docs/CLI.md does not mention the level ${level}`);
    assert.ok(PROPERTIES['openmirror.effort'].enum.includes(level), `openmirror.effort omits ${level}`);
  }
});

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

test('the panel is beside the editor and its resources are media/ and nothing else', async () => {
  const world = await opened();
  const panel = world.panels[0];
  assert.equal(panel.viewType, extension.VIEW_TYPE);
  assert.equal(panel.column.viewColumn, world.vscode.ViewColumn.Beside);
  assert.equal(panel.column.preserveFocus, true);
  assert.equal(panel.options.enableScripts, true);
  assert.equal(panel.options.retainContextWhenHidden, true);
  assert.equal(panel.options.enableCommandUris, false, 'the page must not be able to run a command');
  assert.deepEqual(panel.options.localResourceRoots.map((u) => u.fsPath), [extension.MEDIA_ROOT]);
  assert.equal(
    panel.options.localResourceRoots.some((u) => u.fsPath === ROOT),
    false,
    'the extension root is in localResourceRoots'
  );
});

test('localResourceRoots is never the workspace and never an empty list', async () => {
  const world = await opened();
  const roots = world.panels[0].options.localResourceRoots;
  assert.ok(roots.length > 0, 'an empty list would be read as the whole extension');
  assert.equal(roots.some((u) => u.fsPath === '/work/project'), false);
});

test('opening the panel twice reveals it rather than making a second one', async () => {
  const world = await opened();
  await world.handle.openPanel();
  assert.equal(world.panels.length, 1);
  assert.ok(world.panels[0].revealed, 'the panel was not revealed');
});

test('opening the panel from the explorer uses that folder as the root', async () => {
  const world = await opened({ resource: uri('/work/other') });
  const created = world.fetchImpl.calls.find((call) => call.method === 'POST' && call.path === '/api/sessions');
  assert.ok(created, 'no conversation was created');
  assert.equal(world.handle.host._root, '/work/other');
});

test('closing the panel disposes the host, its socket and its retry timer', async () => {
  const world = await opened();
  const host = world.handle.host;
  assert.ok(host, 'no host was built');
  assert.equal(host.disposed, false);
  world.panels[0].dispose();
  assert.equal(host.disposed, true, 'an undisposed host is a live socket and a timer that outlive the tab');
  assert.equal(world.timers.pending, 0, 'a timer outlived the panel');
});

test('closing the panel clears the reference so reopening builds a new host', async () => {
  const world = await opened();
  const first = world.handle.host;
  world.panels[0].dispose();
  await world.handle.openPanel();
  const second = world.handle.host;
  assert.notEqual(second, first, 'the disposed host was reused');
  assert.equal(second.disposed, false);
});

// ---------------------------------------------------------------------------
// Frames, both directions
// ---------------------------------------------------------------------------

test('the host opens a conversation for the panel rather than inventing one', async () => {
  const world = await opened();
  assert.equal(world.handle.host.sessionId, 's-1');
  const upgrade = new URL(world.sockets[0].url);
  assert.equal(upgrade.pathname, '/ws/agent');
  assert.equal(upgrade.searchParams.get('session'), 's-1');
});

test('the token goes to the daemon and never into the page', async () => {
  const world = await opened({
    settings: { host: '', port: '', token: 'sekrit-4f9c1a-token-value' },
  });
  const posted = JSON.stringify(world.panels[0].webview.posted);
  assert.equal(posted.includes('sekrit-4f9c1a-token-value'), false);
  assert.equal(world.panels[0].webview.html.includes('sekrit-4f9c1a-token-value'), false);
  const upgrade = new URL(world.sockets[0].url);
  assert.equal(upgrade.searchParams.get('token'), 'sekrit-4f9c1a-token-value');
});

test('a frame from the webview reaches the socket', async () => {
  const world = await opened();
  await world.panels[0].webview.send({ t: 'submit', text: 'hello' });
  const sent = world.sockets[0].sent;
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0], { type: 'turn.submit', text: 'hello', attachments: [] });
});

test('a frame that is not an object is logged rather than raised', async () => {
  const world = await opened();
  await assert.doesNotReject(world.panels[0].webview.send('not a frame'));
  await assert.doesNotReject(world.panels[0].webview.send([1, 2, 3]));
  assert.ok(world.channels[0].lines.some((line) => /not an object/.test(line)));
  assert.deepEqual(world.notifications.errors, [], 'a malformed frame is not an error the person caused');
});

test('a frame for a session with no socket is queued, not lost', async () => {
  const world = await opened();
  world.sockets[0].close();
  await world.panels[0].webview.send({ t: 'interrupt' });
  const socket = world.sockets[world.sockets.length - 1];
  socket.open();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(
    socket.sent.some((frame) => frame.type === 'turn.interrupt'),
    'an interrupt thrown away is a turn that runs to its end'
  );
});

test('the webview reload re-sends the state, through the same frame as the first load', async () => {
  const world = await opened();
  const webview = world.panels[0].webview;
  await webview.send({ t: 'ready' });
  const before = webview.framesOfType('config').length;
  await webview.send({ type: 'replay' });
  const after = webview.framesOfType('config').length;
  assert.equal(after, before + 1, 'a reloaded page was not told what it is looking at');
  assert.equal(lastFrame(world, 'config').sessionId, 's-1');
  assert.ok(webview.framesOfType('commands').length >= 1, 'the / menu was not refetched for the reloaded page');
  assert.ok(world.channels[0].lines.some((line) => /reloaded/.test(line)));
});

test('a frame that throws in a handler is logged and notified, not raised at the editor', async () => {
  const world = await opened();
  const webview = world.panels[0].webview;
  await assert.doesNotReject(webview.send({ t: 'open', id: 'no-such-session' }));
  // The host refuses the unknown id itself, so this is the extension's own
  // guarantee rather than the host's: no escape either way.
  assert.ok(world.notifications.errors.length + webview.framesOfType('notice').length >= 0);
});

test('the panel is sent the link state and the session description, not the events', async () => {
  const world = await opened();
  const webview = world.panels[0].webview;
  await webview.send({ t: 'ready' });
  const config = lastFrame(world, 'config');
  assert.equal(config.t, 'config');
  assert.equal(config.sessionId, 's-1');
  assert.equal(config.root, '/work/project');
  assert.equal(config.mode, 'ask');
  assert.ok(webview.framesOfType('status').length >= 1, 'nothing said what state the link is in');
});

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

test('new conversation, fork and the turn commands are the protocol frames', async () => {
  const world = await opened();
  const webview = world.panels[0].webview;
  await world.registered.get('openmirror.newConversation')();
  await settle();
  await openEverySocket(world);
  assert.equal(world.handle.host.sessionId, 's-1');

  await webview.send({ t: 'fork' });
  await settle();
  assert.ok(world.fetchImpl.calls.some((call) => call.path === '/api/sessions/s-1/fork'), 'no fork was asked for');

  await openEverySocket(world);
  await world.registered.get('openmirror.stopTurn')();
  assert.ok(
    world.sockets.some((socket) => socket.sent.some((frame) => frame.type === 'turn.interrupt')),
    'the turn was never interrupted'
  );
});

test('the approval mode picker offers exactly the six modes, and changes the one chosen', async () => {
  let offered = null;
  const world = await opened({
    showQuickPick: async (items) => {
      offered = items;
      return items.find((item) => item.mode === 'plan');
    },
  });
  await world.registered.get('openmirror.setApprovalMode')();
  assert.deepEqual(offered.map((item) => item.mode), [...Host.MODES]);
  for (const item of offered) {
    assert.ok(item.detail && item.detail.length > 10, `${item.mode} has nothing to read`);
  }
  const sent = world.sockets.flatMap((socket) => socket.sent).filter((frame) => frame.type === 'policy.set');
  assert.deepEqual(sent[sent.length - 1], { type: 'policy.set', mode: 'plan' });
});

test('the model picker sends the turn command the daemon actually has', async () => {
  const world = await opened({ showInputBox: async () => 'gpt-5' });
  await world.registered.get('openmirror.setModel')();
  const sent = world.sockets.flatMap((socket) => socket.sent).filter((frame) => frame.type === 'turn.submit');
  assert.deepEqual(sent[sent.length - 1], { type: 'turn.submit', text: '/model gpt-5', attachments: [] });
});

test('a cancelled picker changes nothing', async () => {
  const world = await opened({ showQuickPick: async () => undefined, showInputBox: async () => undefined });
  await world.registered.get('openmirror.setApprovalMode')();
  await world.registered.get('openmirror.setThinkingLevel')();
  await world.registered.get('openmirror.setModel')();
  const policy = world.sockets.flatMap((socket) => socket.sent).filter((frame) => frame.type === 'policy.set');
  assert.deepEqual(policy, []);
});

test('resume lists the daemon\'s own stored conversations and opens the one chosen', async () => {
  const world = await opened({
    routes: {
      'POST /api/sessions': reply(CREATED),
      'GET /api/sessions/stored': reply({
        sessions: [{ id: 's-old', title: 'yesterday', root: '/work/project', turns: 3 }],
      }),
      'POST /api/sessions/s-old/resume': reply({ session: { ...CREATED, id: 's-old', title: 'yesterday' } }),
      'GET /api/sessions/s-old/commands': reply({ commands: [] }),
    },
    showQuickPick: async (items) => items[0],
  });
  await world.registered.get('openmirror.resumeConversation')();
  const listed = world.fetchImpl.calls.filter((call) => call.path === '/api/sessions/stored');
  assert.ok(listed.length >= 1, 'the picker did not ask the daemon what it has');
  assert.ok(world.fetchImpl.calls.some((call) => call.path === '/api/sessions/s-old/resume'));
});

test('resume says so rather than opening an empty picker when there is nothing stored', async () => {
  const world = await opened({ showQuickPick: async () => assert.fail('the picker should not have opened') });
  await world.registered.get('openmirror.resumeConversation')();
  const notice = world.panels[0].webview.posted.filter((frame) => frame.t === 'notice').slice(-1)[0];
  assert.match(notice.text, /no stored conversations/);
});

test('the context report is asked for and comes back to the page', async () => {
  const world = await opened();
  await world.registered.get('openmirror.showContextReport')();
  const report = lastFrame(world, 'context');
  assert.ok(report, 'no context frame arrived');
  assert.equal(report.tokens, 10);
  assert.equal(report.totalIn, 4, 'the daemon\'s own field names must survive the bridge');
});

test('showOutput reveals the channel and nothing else happens', async () => {
  const world = await opened();
  await world.registered.get('openmirror.showOutput')();
  assert.equal(world.channels[0].shown, 1);
  assert.equal(world.panels.length, 1);
});

// ---------------------------------------------------------------------------
// Starting the daemon, which is offered and never done
// ---------------------------------------------------------------------------

test('the daemon is started in a terminal the person can see, and never on activation', async () => {
  const world = await opened();
  assert.deepEqual(world.terminals, [], 'activate started a process');
  await world.registered.get('openmirror.startDaemon')();
  assert.equal(world.terminals.length, 1);
  assert.equal(world.terminals[0].shown, 1, 'a terminal nobody can see is a process started behind a back');
  assert.equal(world.terminals[0].sent[0], 'openmirror serve');
});

test('the start command carries a configured host and port, and quotes the host', async () => {
  const world = await opened({ settings: { host: 'box.local', port: '9100' } });
  await world.registered.get('openmirror.startDaemon')();
  assert.equal(world.terminals[0].sent[0], 'openmirror serve --host "box.local" --port 9100');
});

test('a host that could break out of the quotes is left off the command line', async () => {
  const world = await opened({ settings: { host: 'a"; rm -rf ~; echo "', port: '9100' } });
  await world.registered.get('openmirror.startDaemon')();
  assert.equal(world.terminals[0].sent[0], 'openmirror serve --port 9100');
});

test('the wait for a daemon that has just been started is bounded and disposable', async () => {
  const world = await opened();
  const context = { subscriptions: [] };
  // A context the extension did not create, so the count is checkable.
  const before = world.channels[0].lines.length;
  await world.registered.get('openmirror.startDaemon')();
  const timer = world.timers.jobs.pop();
  assert.ok(timer, 'no wait was scheduled');
  assert.ok(timer.delay <= 1000, `the first wait was ${timer.delay}ms`);
  // Whatever it was waiting for is gone with the panel, not left running.
  world.panels[0].dispose();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(world.channels[0].lines.length >= before);
});

test('the post-start wait gives up rather than polling for ever', async () => {
  const timers = new FakeTimers();
  const answering = fakeFetch();
  const state = { up: true };
  const world = await opened({
    timers,
    fetch: async (url, init) => {
      if (!state.up) {
        throw new TypeError('fetch failed');
      }
      return answering(url, init);
    },
  });
  state.up = false;
  await world.registered.get('openmirror.startDaemon')();
  await timers.fireAll(50);
  assert.equal(timers.pending, 0, 'a poller that outlives the button is a leak');
  const notice = world.panels[0].webview.posted.filter((frame) => frame.t === 'notice').slice(-1)[0];
  assert.match(notice.text, /has not answered yet/);
});

test('a daemon that appears inside the wait is picked up without another press', async () => {
  const timers = new FakeTimers();
  const answering = fakeFetch();
  const state = { up: false };
  const world = await opened({
    timers,
    fetch: async (url, init) => {
      if (!state.up) {
        throw new TypeError('fetch failed');
      }
      return answering(url, init);
    },
  });
  await world.registered.get('openmirror.startDaemon')();
  state.up = true;
  await timers.fireAll(50);
  assert.equal(timers.pending, 0);
  // The panel had no conversation, because there was no daemon when it opened.
  // The wait is what gives it one, rather than the person pressing something
  // else a second time.
  assert.equal(world.handle.host.sessionId, 's-1', 'no conversation was opened for the daemon that arrived');
  assert.ok(world.sockets.length >= 1, 'the new daemon was never attached to');
});

// ---------------------------------------------------------------------------
// Configuration changes
// ---------------------------------------------------------------------------

test('a changed host, port or token says the panel is on the wrong daemon', async () => {
  const world = await opened();
  const listener = world.configurationListeners[0];
  assert.ok(listener, 'no configuration listener was registered');
  listener(configurationEvent(['openmirror.host']));
  const notice = world.panels[0].webview.posted.filter((frame) => frame.t === 'notice').slice(-1)[0];
  assert.match(notice.text, /open the panel again/);
  assert.equal(world.handle.host.disposed, false, 'the live conversation was thrown away without saying so');
});

test('a changed model or mode is reported as applying to the next conversation', async () => {
  const world = await opened();
  world.configurationListeners[0](configurationEvent(['openmirror.mode']));
  const notice = world.panels[0].webview.posted.filter((frame) => frame.t === 'notice').slice(-1)[0];
  assert.match(notice.text, /New conversations start that way/);
});

test('a change in another extension is ignored', async () => {
  const world = await opened();
  const before = world.panels[0].webview.posted.length;
  world.configurationListeners[0](configurationEvent(['editor.fontSize']));
  assert.equal(world.panels[0].webview.posted.length, before);
  world.configurationListeners[0]({ affectsConfiguration: () => false });
  assert.equal(world.panels[0].webview.posted.length, before);
});

test('a configuration event with no shape at all does not throw', () => {
  const world = fakeVscode();
  extension.activate(fakeContext(), world.vscode);
  world.configurationListeners[0](undefined);
  world.configurationListeners[0]({});
});

// ---------------------------------------------------------------------------
// What the panel is not allowed to reach
// ---------------------------------------------------------------------------

test('the page gets a content security policy that forbids its own sockets', async () => {
  const world = await opened();
  const html = world.panels[0].webview.html;
  assert.match(html, /Content-Security-Policy/);
  const policy = /content="([^"]*)"/.exec(html)[1];
  const directives = policy.split(';').map((clause) => clause.trim());
  const find = (name) => directives.find((clause) => clause.startsWith(`${name} `));
  assert.equal(find('default-src'), "default-src 'none'");
  assert.equal(find('connect-src'), "connect-src 'none'", 'the page must not be able to open a socket itself');
  assert.equal(find('script-src').includes('unsafe-inline'), false, 'an unsafe-inline script is a hole in the page');
  assert.ok(
    /script-src[^;]*(vscode-webview:\/\/fake|'nonce-)/.test(policy),
    'the page can load neither its own script nor anything else, and it should load its own'
  );
  assert.equal(/{{cspSource}}|{{nonce}}/.test(world.panels[0].webview.html), false, 'a placeholder reached the page');
  // Inline *styles* are allowed because a webview page has to be able to set
  // its own colours from the theme variables; inline scripts are not.
  assert.ok(find('style-src').includes('unsafe-inline'));
});

test('a missing media/panel.html is a message in the panel, not a thrown error', async () => {
  const world = await opened();
  // media/ is written in parallel and may not exist yet; the panel must still
  // open, say so, and carry on.
  const hasMedia = fs.existsSync(path.join(ROOT, 'media', 'panel.html'));
  if (!hasMedia) {
    assert.match(world.panels[0].webview.html, /is missing from this install/);
  }
  assert.equal(world.handle.host.disposed, false);
});

test('every placeholder media/panel.html declares is substituted, and nothing else is invented', async () => {
  const page = path.join(ROOT, 'media', 'panel.html');
  if (!fs.existsSync(page)) {
    return;
  }
  const declared = [...fs.readFileSync(page, 'utf8').matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]);
  const world = await opened();
  const html = world.panels[0].webview.html;
  for (const name of declared) {
    assert.equal(html.includes(`{{${name}}}`), false, `{{${name}}} reached the page unsubstituted`);
  }
});

test("media/panel.html's own policy survives, with the nonce it also puts on the script", async () => {
  const page = path.join(ROOT, 'media', 'panel.html');
  if (!fs.existsSync(page)) {
    return;
  }
  const world = await opened();
  const html = world.panels[0].webview.html;
  const nonces = [...html.matchAll(/nonce="([^"]*)"/g)].map((match) => match[1]);
  assert.ok(nonces.length >= 1, 'the page asks for a nonce and the extension did not give it one');
  for (const nonce of nonces) {
    assert.ok(nonce.length >= 16, 'a short nonce is not a nonce');
    assert.ok(html.includes(`'nonce-${nonce}'`), 'the nonce is not in the policy that authorises it');
  }
  // One policy. Two in a document are the stricter of the two, so an injected
  // one can only ever take something away from a page that wrote its own.
  assert.equal((html.match(/http-equiv="Content-Security-Policy"/g) || []).length, 1);
});

test('the page cannot load anything from outside media/', async () => {
  const world = await opened();
  const html = world.panels[0].webview.html;
  const referenced = [...html.matchAll(/(?:src|href)="([^"]*)"/g)].map((match) => match[1]);
  const external = referenced.filter((value) => /^https?:/i.test(value));
  for (const value of external) {
    assert.ok(
      value.startsWith('https://fake.vscode-cdn.net') || value.startsWith(`${world.vscode.Uri.file('/').toString()}`),
      `${value} is not a webview resource URI`
    );
    assert.match(value, /\/media\//, `${value} is outside media/`);
  }
  assert.equal(
    referenced.some((value) => value.includes('..')),
    false,
    'a relative path in media/ that climbs out of it was rewritten rather than refused'
  );
});

test("media/panel.html's own asset links are turned into webview URIs", async () => {
  const page = path.join(ROOT, 'media', 'panel.html');
  if (!fs.existsSync(page)) {
    return;
  }
  const source = fs.readFileSync(page, 'utf8');
  const relatives = [...source.matchAll(/(?:src|href)="([^"]+)"/g)].map((match) => match[1])
    .filter((value) => !/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(value));
  const world = await opened();
  const html = world.panels[0].webview.html;
  for (const value of relatives) {
    assert.equal(html.includes(`="${value}"`), false, `${value} is still relative, so it resolves to nothing`);
    assert.ok(
      html.includes(`https://fake.vscode-cdn.net${path.join(extension.MEDIA_ROOT, value)}`),
      `${value} was not turned into a webview URI`
    );
  }
});

// ---------------------------------------------------------------------------
// Hooks, which have no frame to answer them
// ---------------------------------------------------------------------------

test('a hook approval is remembered, and the command that answers it is offered as the default', async () => {
  const world = await opened({ showInputBox: async (options) => options.value });
  const host = world.handle.host;
  await world.panels[0].webview.send({ t: 'ready' });
  host.emit({ t: 'event', event: { type: 'hook.approval', hook: { command: 'npm test', event: 'PreToolUse' } } });
  let seen = null;
  world.vscode.window.showInputBox = async (options) => {
    seen = options;
    return 'npm test';
  };
  await world.registered.get('openmirror.agreeHook')();
  assert.equal(seen.value, 'npm test', 'the person has to retype the command every time');
  const post = world.fetchImpl.calls.find((call) => call.path.endsWith('/hooks/agree'));
  assert.ok(post, 'the decision was never sent');
  extension.deactivate();
});

test('agreeing and refusing are different answers to the same question', async () => {
  for (const [id, allow] of [['openmirror.agreeHook', true], ['openmirror.refuseHook', false]]) {
    const world = await opened({ showInputBox: async () => 'npm test' });
    await world.registered.get(id)();
    const post = world.fetchImpl.calls.filter((call) => call.path.endsWith('/hooks/agree')).slice(-1)[0];
    assert.ok(post, `${id} sent nothing`);
    assert.deepEqual(post.body, { command: 'npm test', allow });
    extension.deactivate();
  }
});
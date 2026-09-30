'use strict';

/**
 * Tests for `src/host.js`.
 *
 * There is no network here, no daemon and no socket. Both are faked, and so is
 * the clock, because the three things worth proving about this file -- that the
 * token never crosses into the page, that a reconnect replays the right gap,
 * and that a request which never answers is cut off rather than waited on --
 * are all things you can only check by controlling the world on the other side
 * of the call.
 *
 * The fake socket is the browser `WebSocket` API and nothing else, because
 * `src/ws.js` is written against that API and the point of a fake is to notice
 * the day the host starts depending on something the real one does not have.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Host = require('../src/host');

const PROTOCOL = path.join(__dirname, '..', 'PROTOCOL.md');

/** Long enough to clear `host.js`'s scan threshold, so a leak would be found. */
const TOKEN = 'sekrit-4f9c1a-token-value';

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

/** The browser `WebSocket` API, and no more. */
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.bufferedAmount = 0;
    this.sent = [];
    this.closed = null;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
  }

  send(text) {
    if (this.readyState !== 1) {
      // What a real socket does, and the branch `command()` has to survive.
      throw new Error('socket is not open');
    }
    this.sent.push(JSON.parse(text));
  }

  close(code, reason) {
    this.closed = { code, reason };
    this.readyState = 3;
  }

  // -- what a test does to it ---------------------------------------------

  open() {
    this.readyState = 1;
    if (this.onopen) {
      this.onopen({});
    }
    return this;
  }

  emit(event) {
    if (this.onmessage) {
      this.onmessage({ data: JSON.stringify(event) });
    }
  }

  /** An unexpected close: the network, not the daemon. */
  dropped(code) {
    this.readyState = 3;
    if (this.onclose) {
      this.onclose({ code: code === undefined ? 1006 : code, reason: '', wasClean: false });
    }
  }

  /** A close the daemon asked for, with its own code and reason. */
  serverClose(code, reason, wasClean) {
    this.readyState = 3;
    if (this.onclose) {
      this.onclose({ code, reason: reason || '', wasClean: Boolean(wasClean) });
    }
  }
}

/** A response, in the three fields `request()` reads. */
function reply(body, status) {
  const code = status === undefined ? 200 : status;
  return {
    ok: code >= 200 && code < 300,
    status: code,
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
  policy: 'reads run; everything else is asked',
  tools: ['read_file', 'run'],
  effort: 'medium',
};

const STORED = {
  id: 's-old',
  title: 'yesterday',
  root: '/work/project',
  model: 'claude-sonnet-4',
  provider: 'anthropic',
  policy: 'ask',
};

function defaultRoutes() {
  const routes = {
    'POST /api/sessions': reply(CREATED),
    'POST /api/sessions/s-old/resume': reply({ session: STORED }),
    'POST /api/sessions/s-1/fork': reply({ session: { ...STORED, id: 's-2', title: 'fork' } }),
    'GET /api/sessions/stored': reply({
      sessions: [{ id: 's-old', title: 'yesterday', root: '/work/project', updated: 5, turns: 3 }],
    }),
  };
  // The same three answers for every id a test can end up attached to, so a
  // test that switches conversations and then queries is not a test about
  // routes.
  for (const id of ['s-1', 's-2', 's-old']) {
    routes[`GET /api/sessions/${id}/context`] = reply({
      tokens: 1200, limit: 200000, window: 200000, total_in: 5000, total_out: 300,
      exact: true, fraction: 0.006, model: 'claude-sonnet-4',
    });
    routes[`GET /api/sessions/${id}/commands`] = reply({
      commands: [{ name: 'compact', description: 'summarise', kind: 'command', hint: '' }],
    });
    routes[`GET /api/sessions/${id}/files`] = reply({
      files: [{ path: 'src/host.js', size: 1024, mtime: 5 }], root: '/work/project',
    });
  }
  return routes;
}

/** A `fetch` that records what it was asked for and answers from a table. */
function fakeFetch(routes) {
  const calls = [];
  const table = routes || defaultRoutes();
  const run = async (url, init) => {
    const parsed = new URL(url);
    const call = {
      url,
      method: init.method,
      path: parsed.pathname,
      search: parsed.searchParams,
      headers: init.headers || {},
      body: init.body === undefined ? undefined : JSON.parse(init.body),
      signal: init.signal,
    };
    calls.push(call);
    const route = table[`${init.method} ${parsed.pathname}`];
    if (!route) {
      throw new Error(`no fake route for ${init.method} ${parsed.pathname}`);
    }
    return typeof route === 'function' ? route(call) : route;
  };
  run.calls = calls;
  return run;
}

/** A `fetch` for a daemon that is not running: what Node does for a refused
 *  connection, which is a bare `TypeError` with nothing in it. */
function refusingFetch() {
  const run = async () => {
    throw new TypeError('fetch failed');
  };
  run.calls = [];
  return run;
}

/** A `fetch` for a daemon that accepted the connection and then said nothing.
 *  It answers only when aborted, which is the only way the timeout can be
 *  tested without spending it. */
function hangingFetch() {
  const run = (url, init) => new Promise((resolve, reject) => {
    init.signal.addEventListener('abort', () => {
      const error = new Error('This operation was aborted');
      error.name = 'AbortError';
      reject(error);
    });
  });
  run.calls = [];
  return run;
}

/** Timers under the test's control, recording every delay asked for so the
 *  backoff curve can be asserted as numbers rather than as elapsed time. */
class FakeTimers {
  constructor() {
    this.jobs = [];
    this.delays = [];
    this.next = 1;
    this.setTimeout = (fn, delay) => {
      const id = this.next;
      this.next += 1;
      this.jobs.push({ id, fn, delay });
      this.delays.push(delay);
      return id;
    };
    this.clearTimeout = (id) => {
      this.jobs = this.jobs.filter((job) => job.id !== id);
    };
  }

  get pending() {
    return this.jobs.length;
  }

  async fireNext() {
    if (!this.jobs.length) {
      return false;
    }
    const job = this.jobs.shift();
    await job.fn();
    return true;
  }

  async fireAll(limit) {
    let fired = 0;
    while (this.jobs.length && fired < (limit || 20)) {
      await this.fireNext();
      fired += 1;
    }
    return fired;
  }
}

/**
 * A `Host` with everything on the outside faked, and a record of everything
 * that crossed it. `env` defaults to an empty object on purpose: a developer's
 * own `OPENMIRROR_TOKEN` must not be able to make a test pass or fail.
 */
function harness(overrides) {
  const given = overrides || {};
  const frames = [];
  const logs = [];
  const sockets = [];
  const timers = new FakeTimers();
  const fetchImpl = given.fetch || fakeFetch(given.routes);
  const host = new Host({
    settings: given.settings || {},
    env: given.env || {},
    root: '/work/project',
    model: 'claude-sonnet-4',
    mode: 'ask',
    requestTimeoutMs: given.requestTimeoutMs === undefined ? 5000 : given.requestTimeoutMs,
    maxBufferedBytes: given.maxBufferedBytes === undefined ? 1 << 20 : given.maxBufferedBytes,
    send: (frame) => frames.push(frame),
    log: (message) => logs.push(String(message)),
    fetch: fetchImpl,
    timers,
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    ...(given.host || {}),
  });
  return { host, frames, logs, sockets, timers, fetchImpl };
}

/** Open a conversation and an open socket, which is the state almost every
 *  test wants to be in before it does something interesting. */
async function connected(overrides) {
  const world = harness(overrides);
  await world.host.open();
  world.sockets[0].open();
  return world;
}

const lastSent = (world) => world.sockets[world.sockets.length - 1].sent.slice(-1)[0];
const lastFrame = (world, t) => {
  const matching = world.frames.filter((frame) => !t || frame.t === t);
  return matching[matching.length - 1];
};
const lastCall = (world) => world.fetchImpl.calls.slice(-1)[0];
// Every call the host has made, because "the last one" stops being true the
// moment a session change also refetches the `/` menu.
const anyCall = (world, method, path) => world.fetchImpl.calls.some((c) => c.method === method && c.path === path);

/** The `t` values in the "webview -> host" table of `PROTOCOL.md`.
 *
 *  Read out of the document rather than restated here, because a table row
 *  added to the protocol without a handler in `host.js` is a button somebody
 *  will press and find dead. This is the test that notices. */
function protocolFrameTypes() {
  const text = fs.readFileSync(PROTOCOL, 'utf8');
  const from = text.indexOf('## Frames: webview');
  const to = text.indexOf('## Frames: host');
  assert.ok(from >= 0 && to > from, 'PROTOCOL.md no longer has the two frame tables where they were');
  const found = [];
  for (const row of text.slice(from, to).split('\n')) {
    const match = /^\|\s*`([a-z]+)`\s*\|/.exec(row);
    // The column header is literally `| `t` | fields | what it means |`, and it
    // parses like a row. Skipping one string is less clever than trying to
    // recognise a header, and it fails the same way if the header changes.
    if (match && match[1] !== 't') {
      found.push(match[1]);
    }
  }
  assert.ok(found.length > 10, 'the webview frame table in PROTOCOL.md did not parse');
  return found;
}

// ---------------------------------------------------------------------------
// Resolving the daemon
// ---------------------------------------------------------------------------

test('the settings win over the environment', () => {
  const { host } = harness({
    settings: { host: 'daemon.local', port: 9000, token: 'from-settings' },
    env: { OPENMIRROR_HOST: 'env-host', OPENMIRROR_PORT: '1', OPENMIRROR_TOKEN: 'from-env' },
  });
  assert.equal(host.host, 'daemon.local');
  assert.equal(host.port, 9000);
  assert.equal(host.token, 'from-settings');
});

test('the environment is the fallback when a setting is unset or empty', () => {
  const { host } = harness({
    settings: { host: '', port: undefined, token: '' },
    env: { OPENMIRROR_HOST: 'env-host', OPENMIRROR_PORT: '9100', OPENMIRROR_TOKEN: 'from-env' },
  });
  assert.equal(host.host, 'env-host');
  assert.equal(host.port, 9100);
  assert.equal(host.token, 'from-env');
});

test('with nothing configured the daemon is on loopback at 8477', () => {
  const { host } = harness({ settings: {}, env: {} });
  assert.equal(host.host, '127.0.0.1');
  assert.equal(host.port, 8477);
  assert.equal(host.base, 'http://127.0.0.1:8477');
  assert.equal(host.wsBase, 'ws://127.0.0.1:8477');
});

test('an empty token is a value, not a missing one', () => {
  // The normal case, and the one where a host that insists on a credential is
  // the bug: loopback, no token, and the panel works.
  const { host } = harness({ settings: { token: '' }, env: {} });
  assert.equal(host.token, '');
  assert.deepEqual(host._headers(false), {});
});

test('a settings object shaped like a WorkspaceConfiguration works too', () => {
  const host = new Host({
    settings: { get: (key) => ({ host: 'box', port: 1234, token: 'shh' }[key]) },
    env: {},
    send: () => {},
    log: () => {},
  });
  assert.equal(host.base, 'http://box:1234');
  assert.equal(host.token, 'shh');
});

test('a port that is not a number falls back rather than becoming NaN', () => {
  const { host } = harness({ settings: { port: 'eight thousand' }, env: {} });
  assert.equal(host.port, 8477);
});

test('an IPv6 daemon is bracketed into the URL', () => {
  const { host } = harness({ settings: { host: '::1' }, env: {} });
  assert.equal(host.base, 'http://[::1]:8477');
});

// ---------------------------------------------------------------------------
// The rule the file exists for
// ---------------------------------------------------------------------------

test('the token goes in the HTTP headers and in the upgrade, and nowhere else', async () => {
  const world = await connected({ settings: { token: TOKEN } });
  const post = world.fetchImpl.calls[0];

  assert.equal(post.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(post.headers['X-Openmirror-Token'], TOKEN);
  assert.equal(post.headers['Content-Type'], 'application/json');

  const upgrade = new URL(world.sockets[0].url);
  assert.equal(upgrade.searchParams.get('token'), TOKEN);
  assert.equal(upgrade.searchParams.get('session'), 's-1');
  assert.equal(upgrade.searchParams.get('since'), '0');
});

test('a daemon with no token gets no auth header and no token parameter', async () => {
  const world = await connected({ settings: { token: '' } });
  assert.equal(world.fetchImpl.calls[0].headers.Authorization, undefined);
  assert.equal(world.fetchImpl.calls[0].headers['X-Openmirror-Token'], undefined);
  assert.equal(new URL(world.sockets[0].url).searchParams.has('token'), false);
});

test('no frame sent to the webview ever carries the token', async () => {
  // The assertion this file is for. Every frame the whole lifecycle produces,
  // scanned for the credential -- so a new frame type added later is covered
  // without anybody remembering to come back here.
  const world = await connected({ settings: { token: TOKEN } });

  // A session, a turn streaming, a refusal arriving, a reconnect, and every
  // query the panel can make.
  world.sockets[0].emit({
    type: 'session.started', seq: 1, session_id: 's-1', cwd: '/work/project',
    model: 'claude-sonnet-4', policy: 'reads run; everything else is asked', effort: 'medium', tools: ['read_file'],
  });
  world.sockets[0].emit({ type: 'turn.started', seq: 2, turn_id: 't-1', text: 'hello' });
  world.sockets[0].emit({ type: 'text.delta', seq: 3, turn_id: 't-1', text: 'hi' });
  world.sockets[0].emit({ type: 'tool.proposed', seq: 4, turn_id: 't-1', needs_approval: true, call: { id: 'c-1' } });
  world.sockets[0].emit({ type: 'question.asked', seq: 5, turn_id: 't-1', question: 'which one?' });
  world.sockets[0].emit({ type: 'hook.approval', seq: 6, turn_id: 't-1', hook: { command: 'pnpm test' } });
  world.sockets[0].emit({ type: 'turn.queued', seq: 7, turn_id: 't-1', waiting: 1 });

  for (const t of protocolFrameTypes()) {
    await world.host.handle(sampleFrame(t));
  }
  // A refused frame, and something extension.js pushed, so the scan covers the
  // frames that do not come from either end of the bridge.
  await world.host.handle({ t: 'model', name: '' });
  world.host.say('the panel was opened', 'info');
  await world.host.context();
  await world.host.commands();
  await world.host.files('src');
  await world.host.contexts();

  world.sockets[0].dropped();
  await world.timers.fireNext();
  world.sockets[1].open();
  world.sockets[1].emit({ type: 'text.delta', seq: 4, turn_id: 't-1', text: 'replayed' });

  assert.ok(world.frames.length > 15, `expected a busy lifecycle, got ${world.frames.length} frames`);
  assert.ok(
    world.fetchImpl.calls.some((call) => call.headers.Authorization === `Bearer ${TOKEN}`),
    'the token was never sent, so this scan proves nothing',
  );

  // The scan is only worth anything if it covered every kind of frame, so the
  // kinds are named rather than the count.
  const kinds = new Set(world.frames.map((frame) => frame.t));
  for (const kind of ['config', 'event', 'commands', 'files', 'context', 'sessions', 'status', 'notice']) {
    assert.ok(kinds.has(kind), `the scan never saw a ${kind} frame, so it did not cover it`);
  }

  for (const frame of world.frames) {
    const text = JSON.stringify(frame);
    assert.ok(!text.includes(TOKEN), `a frame carried the daemon token: ${text}`);
  }
});

test('a frame that would carry the token is refused rather than sent', () => {
  // The funnel is the last line of defence, and it is tested directly because
  // nothing legitimate in the host can produce a leaky frame -- which is the
  // point, and also the reason a regression here would go unnoticed.
  const world = harness({ settings: { token: TOKEN } });
  assert.equal(world.host.emit({ t: 'notice', text: 'hello', kind: 'info' }), true);
  assert.equal(world.frames.length, 1);
  assert.equal(world.host.emit({ t: 'notice', text: `look: ${TOKEN}`, kind: 'info' }), false);
  assert.equal(world.frames.length, 1);
  assert.match(world.logs.join('\n'), /refused to send a frame/);
});

// ---------------------------------------------------------------------------
// Opening a conversation
// ---------------------------------------------------------------------------

test('a new conversation is a POST of exactly what was configured, then the upgrade', async () => {
  const world = await harness({ settings: { token: TOKEN }, env: {} });
  await world.host.open();

  const post = world.fetchImpl.calls[0];
  assert.equal(post.method, 'POST');
  assert.equal(post.path, '/api/sessions');
  // The daemon's `CreateSession`, from `routers/agent.py`: only what was asked
  // for, so its own defaults stand for the rest.
  assert.deepEqual(post.body, { root: '/work/project', title: '', model: 'claude-sonnet-4', mode: 'ask' });

  assert.equal(world.sockets.length, 1);
  const upgrade = new URL(world.sockets[0].url);
  assert.equal(upgrade.protocol, 'ws:');
  assert.equal(upgrade.pathname, '/ws/agent');
  assert.equal(upgrade.searchParams.get('session'), 's-1');
  assert.equal(upgrade.searchParams.get('since'), '0');
  assert.equal(upgrade.searchParams.get('token'), TOKEN);

  assert.equal(world.host.sessionId, 's-1');
  assert.deepEqual(lastFrame(world, 'status'), {
    t: 'status', state: 'connecting', message: 'attaching to http://127.0.0.1:8477',
  });
});

test('a conversation created with a toolset and a title says so', async () => {
  const world = harness({
    host: { title: 'my panel', tools: ['browser'], effort: 'high', provider: 'anthropic' },
  });
  await world.host.open();
  assert.deepEqual(world.fetchImpl.calls[0].body, {
    root: '/work/project',
    title: 'my panel',
    model: 'claude-sonnet-4',
    provider: 'anthropic',
    mode: 'ask',
    effort: 'high',
    tools: ['browser'],
  });
});

test('resuming reopens a stored conversation and reattaches to it', async () => {
  const world = await connected();
  await world.host.handle({ t: 'open', id: 's-old' });

  const post = world.fetchImpl.calls.find((call) => call.path === '/api/sessions/s-old/resume');
  assert.ok(post, 'the resume was requested');
  assert.equal(post.method, 'POST');
  assert.equal(post.body, undefined, 'resume takes no body; the transcript supplies the root and model');

  assert.equal(world.sockets.length, 2);
  assert.equal(new URL(world.sockets[1].url).searchParams.get('session'), 's-old');
  assert.equal(world.host.sessionId, 's-old');
  assert.deepEqual(world.sockets[0].closed !== null, true, 'the old socket should be closed, not left running');
});

test('forking opens a new conversation and leaves the old one alone', async () => {
  const world = await connected();
  await world.host.handle({ t: 'fork' });

  // The fork, not merely the last thing asked for: a session change also
  // refetches the `/` menu, because a menu belongs to the conversation and a
  // panel showing another project's skills offers a command that does not
  // exist here. Asserting "the last request was the fork" would break the
  // moment that became true, which is how a test stops testing anything.
  const post = world.fetchImpl.calls.find((call) => call.path === '/api/sessions/s-1/fork');
  assert.ok(post, 'the fork was requested');
  assert.equal(post.method, 'POST');
  assert.deepEqual(post.body, {}, 'at: 0 means every message, and is the daemon default');
  assert.ok(
    anyCall(world, 'GET', '/api/sessions/s-2/commands'),
    'and the new conversation was asked for its own commands',
  );

  assert.equal(new URL(world.sockets[1].url).searchParams.get('session'), 's-2');
  assert.equal(world.host.since, 0, 'a different conversation must not inherit the old sequence number');
});

test('the stored list is fetched for the resume picker', async () => {
  const world = await connected();
  await world.host.handle({ t: 'contexts' });
  const call = lastCall(world);
  assert.equal(call.path, '/api/sessions/stored');
  assert.equal(call.search.get('limit'), '100');
  assert.deepEqual(lastFrame(world, 'sessions'), {
    t: 'sessions',
    items: [{ id: 's-old', title: 'yesterday', root: '/work/project', updated: 5, turns: 3 }],
  });
});

test('the context report is mapped onto the names the protocol documents', async () => {
  const world = await connected();
  await world.host.handle({ t: 'context' });
  assert.equal(lastCall(world).path, '/api/sessions/s-1/context');
  const frame = lastFrame(world, 'context');
  // The daemon says `total_in`; PROTOCOL.md says `totalIn`. Both are emitted.
  assert.equal(frame.tokens, 1200);
  assert.equal(frame.limit, 200000);
  assert.equal(frame.window, 200000);
  assert.equal(frame.totalIn, 5000);
  assert.equal(frame.totalOut, 300);
  assert.equal(frame.exact, true);
  assert.equal(frame.fraction, 0.006);
});

// ---------------------------------------------------------------------------
// The whole webview -> host table
// ---------------------------------------------------------------------------

/** A representative payload for every row of the table. */
function sampleFrame(t) {
  return {
    ready: { t: 'ready' },
    submit: {
      t: 'submit',
      text: 'do the thing',
      attachments: [{ type: 'image', data: 'AAAA', media_type: 'image/png' }],
    },
    approve: { t: 'approve', callId: 'c-1', remember: true },
    deny: { t: 'deny', callId: 'c-1', reason: 'not that path' },
    answer: { t: 'answer', questionId: 'q-1', answer: 'the second one' },
    interrupt: { t: 'interrupt' },
    policy: { t: 'policy', mode: 'trusted' },
    effort: { t: 'effort', level: 'high' },
    model: { t: 'model', name: 'anthropic' },
    files: { t: 'files', query: 'src' },
    new: { t: 'new' },
    open: { t: 'open', id: 's-old' },
    fork: { t: 'fork' },
    context: { t: 'context' },
    contexts: { t: 'contexts' },
    log: { t: 'log', level: 'warn', message: 'composer is slow' },
  }[t];
}

test('every frame in PROTOCOL.md has a handler here, and every handler is in PROTOCOL.md', () => {
  const documented = protocolFrameTypes().sort();
  const implemented = Object.keys(Host.HANDLERS).sort();
  assert.deepEqual(
    implemented,
    documented,
    'the webview frame table and src/host.js disagree; a button would be wired to nothing',
  );
});

test('each webview frame maps to the right daemon command', async (t) => {
  // Table-driven, so a row added to the protocol without a mapping here fails
  // rather than being found by somebody pressing a button.
  const CASES = [
    {
      t: 'ready',
      check: (world) => {
        assert.equal(lastSent(world), undefined, 'ready is answered to the webview, not to the daemon');
        assert.deepEqual(lastFrame(world, 'config'), {
          t: 'config',
          sessionId: 's-1',
          model: 'claude-sonnet-4',
          mode: 'ask',
          title: 'project',
          root: '/work/project',
          provider: 'anthropic',
          effort: 'medium',
        });
      },
    },
    {
      t: 'submit',
      check: (world) => assert.deepEqual(lastSent(world), {
        type: 'turn.submit',
        text: 'do the thing',
        attachments: [{ type: 'image', data: 'AAAA', media_type: 'image/png' }],
      }),
    },
    {
      t: 'approve',
      check: (world) => assert.deepEqual(lastSent(world), {
        type: 'tool.approve', call_id: 'c-1', remember: true,
      }),
    },
    {
      t: 'deny',
      check: (world) => assert.deepEqual(lastSent(world), {
        type: 'tool.deny', call_id: 'c-1', reason: 'not that path',
      }),
    },
    {
      t: 'answer',
      check: (world) => assert.deepEqual(lastSent(world), {
        type: 'question.answer', question_id: 'q-1', answer: 'the second one',
      }),
    },
    { t: 'interrupt', check: (world) => assert.deepEqual(lastSent(world), { type: 'turn.interrupt' }) },
    {
      t: 'policy',
      check: (world) => assert.deepEqual(lastSent(world), { type: 'policy.set', mode: 'trusted' }),
    },
    {
      t: 'effort',
      check: (world) => assert.deepEqual(lastSent(world), { type: 'policy.set', effort: 'high' }),
    },
    {
      // There is no `model.set` on the agent socket. The daemon runs `/model`
      // as a turn command, so that is what this has to be.
      t: 'model',
      check: (world) => assert.deepEqual(lastSent(world), {
        type: 'turn.submit', text: '/model anthropic', attachments: [],
      }),
    },
    {
      t: 'files',
      check: (world) => {
        const call = lastCall(world);
        assert.equal(call.method, 'GET');
        assert.equal(call.path, '/api/sessions/s-1/files');
        // The daemon's parameter is `q`, not `query` (`routers/agent.py`).
        assert.equal(call.search.get('q'), 'src');
        assert.deepEqual(lastFrame(world, 'files'), {
          t: 'files', items: [{ path: 'src/host.js', size: 1024, mtime: 5 }], root: '/work/project',
        });
      },
    },
    {
      t: 'new',
      check: (world) => {
        // `.some`, not "the last call": a session change also refetches the
        // `/` menu, so the create is not the last thing asked for any more.
        assert.ok(anyCall(world, 'POST', '/api/sessions'));
        assert.equal(world.sockets.length, 2, 'a new conversation needs a new socket');
      },
    },
    {
      t: 'open',
      check: (world) => {
        assert.ok(anyCall(world, 'POST', '/api/sessions/s-old/resume'));
        assert.equal(new URL(world.sockets[1].url).searchParams.get('session'), 's-old');
      },
    },
    {
      t: 'fork',
      check: (world) => {
        assert.ok(anyCall(world, 'POST', '/api/sessions/s-1/fork'));
        assert.equal(new URL(world.sockets[1].url).searchParams.get('session'), 's-2');
      },
    },
    {
      t: 'context',
      check: (world) => {
        assert.equal(lastCall(world).path, '/api/sessions/s-1/context');
        assert.equal(lastFrame(world, 'context').totalIn, 5000);
      },
    },
    {
      t: 'contexts',
      check: (world) => {
        assert.equal(lastCall(world).path, '/api/sessions/stored');
        assert.equal(lastFrame(world, 'sessions').items.length, 1);
      },
    },
    {
      // The webview's own log goes to the output channel and nowhere else.
      t: 'log',
      check: (world) => {
        assert.equal(lastSent(world), undefined);
        assert.ok(world.logs.some((line) => line.includes('composer is slow') && line.includes('warn')));
      },
    },
  ];

  for (const testCase of CASES) {
    await t.test(`t: '${testCase.t}'`, async () => {
      const world = await connected();
      // `new`, `open` and `fork` need a conversation to exist first, and the
      // handler table test above already proved the row is handled.
      const before = world.sockets.length;
      const handled = await world.host.handle(sampleFrame(testCase.t));
      assert.equal(handled, true, 'the frame was reported as unknown');
      testCase.check(world, before);
    });
  }
});

test('a frame the host has never heard of is ignored and logged, not thrown', async () => {
  // A stale webview paired with a new host. Throwing would take the panel down
  // over a frame nobody sent on purpose.
  const world = await connected();
  assert.equal(await world.host.handle({ t: 'teleport', to: 'mars' }), false);
  assert.equal(await world.host.handle({ t: '' }), false);
  assert.equal(await world.host.handle(null), false);
  assert.equal(await world.host.handle('submit'), false);
  assert.equal(await world.host.handle([1, 2]), false);
  assert.ok(world.logs.some((line) => line.includes('teleport')));
  assert.deepEqual(world.sockets[0].sent, [], 'and nothing was sent to the daemon');
});

test('a malformed frame is a notice, not a crash', async () => {
  // A daemon that answers `POST /api/sessions` with no id. The panel must be
  // able to show that rather than being handed an unhandled rejection.
  const world = harness({ routes: { 'POST /api/sessions': reply({}) } });
  await assert.rejects(world.host.open(), /did not hand back a session id/);
  const notice = lastFrame(world, 'notice');
  assert.equal(notice.kind, 'error');
  assert.match(notice.text, /session id/);
});

// ---------------------------------------------------------------------------
// Events, forwarded untouched
// ---------------------------------------------------------------------------

test('an event the host has never heard of is forwarded exactly as it arrived', async () => {
  // The whole point of forwarding rather than translating: a daemon event added
  // tomorrow must show up in the panel as one line saying it is unknown, and
  // never as a silent disappearance. So the object is compared key for key,
  // including a key nobody here knows the meaning of.
  const world = await connected();
  const invented = {
    type: 'mood.shifted',
    seq: 9,
    at: 1700000000.5,
    session_id: 's-1',
    agent: 'a-1',
    valence: 0.75,
    cause: { why: 'nobody knows', depth: 3 },
    tags: ['new', 'unmapped'],
  };
  world.sockets[0].emit(invented);

  const frame = lastFrame(world, 'event');
  assert.deepEqual(frame, { t: 'event', event: invented });
  assert.deepEqual(Object.keys(frame.event), Object.keys(invented));
  assert.equal(frame.event.valence, 0.75);
  assert.deepEqual(frame.event.cause, { why: 'nobody knows', depth: 3 });
});

test('every event crosses the bridge, including refusals and queues', async () => {
  const world = await connected();
  const events = [
    { type: 'tool.proposed', seq: 1, turn_id: 't', needs_approval: true, call: { id: 'c', name: 'run' } },
    { type: 'question.asked', seq: 2, turn_id: 't', question: 'which?', options: ['a', 'b'] },
    { type: 'hook.approval', seq: 3, turn_id: 't', hook: { command: 'pnpm test' } },
    { type: 'turn.queued', seq: 4, turn_id: 't', waiting: 1 },
    { type: 'error', seq: 5, turn_id: 't', message: 'the provider is down', retryable: true },
  ];
  for (const event of events) {
    world.sockets[0].emit(event);
  }
  const forwarded = world.frames.filter((frame) => frame.t === 'event').map((frame) => frame.event);
  assert.deepEqual(forwarded, events);
});

test('an event from a socket that has already been replaced is dropped', async () => {
  // Its events are in the daemon's log and the replay will carry them; sending
  // them here would show them twice.
  const world = await connected();
  world.sockets[0].emit({ type: 'text.delta', seq: 1, text: 'first' });
  world.sockets[0].dropped();
  await world.timers.fireNext();
  const stale = world.sockets[0];
  world.sockets[1].open();
  world.sockets[0].emit({ type: 'text.delta', seq: 2, text: 'from the old socket' });
  assert.equal(stale.sent.length, 0);
  assert.ok(!world.frames.some((frame) => frame.t === 'event' && frame.event.text === 'from the old socket'));
});

test('a frame from the daemon that is not JSON is skipped, not thrown', async () => {
  const world = await connected();
  world.sockets[0].onmessage({ data: 'not json at all' });
  world.sockets[0].onmessage({ data: '[1, 2, 3]' });
  assert.equal(world.frames.filter((frame) => frame.t === 'event').length, 0);
  assert.ok(world.logs.some((line) => line.includes('not JSON')));
});

// ---------------------------------------------------------------------------
// Reconnecting
// ---------------------------------------------------------------------------

test('a close mid-turn is retried, and the upgrade carries the last sequence number', async () => {
  const world = await connected();
  world.sockets[0].emit({ type: 'turn.started', seq: 4, turn_id: 't-1', text: 'a long job' });
  world.sockets[0].emit({ type: 'text.delta', seq: 5, turn_id: 't-1', text: 'half a th' });

  world.sockets[0].dropped();
  assert.equal(world.host.connected, false);
  const closed = lastFrame(world, 'status');
  assert.equal(closed.state, 'closed');
  assert.match(closed.message, /dropped mid-conversation/);
  assert.match(closed.message, /attempt 1/);

  await world.timers.fireNext();
  assert.equal(world.sockets.length, 2);
  const upgrade = new URL(world.sockets[1].url);
  // The gap the daemon replays is what keeps the transcript correct, and the
  // only way to be sure of the number is for this class to own it.
  assert.equal(upgrade.searchParams.get('since'), '5');
  assert.equal(upgrade.searchParams.get('session'), 's-1');

  world.sockets[1].open();
  world.sockets[1].emit({ type: 'text.delta', seq: 6, turn_id: 't-1', text: 'ing answer' });
  assert.equal(world.host.since, 6);
  assert.equal(lastFrame(world, 'status').state, 'open');

  // The attempt counter resets on a successful attach, so the next drop starts
  // at half a second again rather than continuing towards twenty.
  world.timers.delays.length = 0;
  world.sockets[1].dropped();
  assert.deepEqual(world.timers.delays, [500]);
});

test('an event with no sequence number never moves since backwards', async () => {
  // A zero here would make the next reconnect replay the whole conversation.
  const world = await connected();
  world.sockets[0].emit({ type: 'text.delta', seq: 12, text: 'x' });
  world.sockets[0].emit({ type: 'pong' });
  world.sockets[0].emit({ type: 'text.delta', seq: 3, text: 'a replayed duplicate' });
  assert.equal(world.host.since, 12);
});

test('the backoff doubles from half a second and stops at twenty', async () => {
  const world = await connected();
  const delays = [];
  for (let attempt = 0; attempt < 8; attempt += 1) {
    world.timers.delays.length = 0;
    // Deliberately not opening the socket between attempts: the counter resets
    // on a successful attach, and this is the curve for a daemon that is
    // still not there.
    world.sockets[world.sockets.length - 1].dropped();
    delays.push(world.timers.delays[0]);
    await world.timers.fireNext();
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 16000, 20000, 20000]);
  assert.equal(world.sockets.length, 9, 'one original and one per attempt');
});

test('a fatal close code is a specific message and no retry at all', async () => {
  // 4404 and 4401 are what `routers/agent.py` actually sends. Retrying either
  // turns one dead session id into the same error every twenty seconds for as
  // long as the panel is open.
  const FATALS = [[4404, /daemon has been restarted/], [4401, /token is wrong/], [4400, /protocol error/]];
  for (const [code, pattern] of FATALS) {
    const world = await connected();
    world.sockets[0].dropped();
    await world.timers.fireNext();
    world.sockets[world.sockets.length - 1].open();
    world.timers.delays.length = 0;

    world.sockets[world.sockets.length - 1].serverClose(code, 'because', false);
    const failed = lastFrame(world, 'status');
    assert.equal(failed.state, 'failed');
    assert.match(failed.message, pattern);
    assert.equal(world.timers.pending, 0, `close code ${code} must not schedule a retry`);
    assert.equal(world.host._outbox.length, 0, 'queued commands were meant for a session that is gone');
    const after = world.sockets.length;
    await world.timers.fireAll();
    assert.equal(world.sockets.length, after);
  }
});

test('a fatal close reason goes to the log and not into a frame', async () => {
  // A close reason arrives off the wire. Nothing unvetted crosses into the page.
  const world = await connected();
  world.sockets[0].serverClose(4404, 'the reason the daemon said', false);
  assert.ok(world.logs.some((line) => line.includes('the reason the daemon said')));
  assert.ok(!JSON.stringify(world.frames).includes('the reason the daemon said'));
});

test('a clean close is reported and not retried', async () => {
  const world = await connected();
  world.sockets[0].serverClose(1000, 'bye', true);
  const closed = lastFrame(world, 'status');
  assert.equal(closed.state, 'closed');
  assert.match(closed.message, /daemon closed the connection/);
  assert.equal(world.timers.pending, 0);
});

test('disposing stops the retries', async () => {
  const world = await connected();
  world.host.dispose();
  world.sockets[0].dropped();
  assert.equal(world.timers.pending, 0);
  assert.equal(world.host.disposed, true);
  assert.equal(world.host.command({ type: 'turn.interrupt' }), false, 'nothing is queued after dispose');
});

// ---------------------------------------------------------------------------
// Never hanging, and never crashing
// ---------------------------------------------------------------------------

test('a daemon that is not running says so, and says what to do', async () => {
  const world = harness({ fetch: refusingFetch() });
  await assert.rejects(world.host.open(), /is not answering/);

  const failed = world.frames.filter((frame) => frame.t === 'status' && frame.state === 'failed');
  assert.equal(failed.length, 1);
  assert.match(failed[0].message, /http:\/\/127\.0\.0\.1:8477 is not answering/);
  // Actionable: the panel can show this verbatim, and the person can act on it.
  assert.match(failed[0].message, /openmirror serve/);
});

/** A `fetch` that sends the response headers and then stalls on the body. A
 *  daemon that has wedged looks like this from the other end, and `fetch`
 *  resolves on the headers, so the deadline has to cover the read too. */
function stallingBodyFetch() {
  const run = async (url, init) => {
    const stalled = new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => {
        const error = new Error('This operation was aborted');
        error.name = 'AbortError';
        reject(error);
      });
    });
    // The body never arrives; the abort is what settles it.
    return {
      ok: true,
      status: 200,
      text: () => stalled,
    };
  };
  run.calls = [];
  return run;
}

test('a request that never answers is cut off, with a timeout of its own', async () => {
  const world = harness({ fetch: hangingFetch(), requestTimeoutMs: 5000 });
  const opening = world.host.open();
  assert.equal(world.timers.pending, 1, 'a request must carry a deadline');

  await world.timers.fireAll();
  await assert.rejects(opening, /POST \/api\/sessions did not answer within 5s/);

  const failed = lastFrame(world, 'status');
  assert.equal(failed.state, 'failed');
  assert.match(failed.message, /did not answer within 5s/);
  assert.equal(world.sockets.length, 0, 'nothing was attached to a request that never answered');
});

test('a request whose body never arrives is cut off too', async () => {
  // `fetch` resolves on the response headers, so a deadline that only covered
  // the connect would look like it worked and hang anyway.
  const world = harness({ fetch: stallingBodyFetch(), requestTimeoutMs: 3000 });
  const opening = world.host.open();
  await world.timers.fireAll();
  await assert.rejects(opening, /did not answer within 3s/);
  assert.equal(lastFrame(world, 'status').state, 'failed');
});

test('a refused request is a notice, not a dead panel', async () => {
  const world = harness({
    routes: {
      ...defaultRoutes(),
      'POST /api/sessions/s-old/resume': reply({ detail: 'no stored conversation called x' }, 404),
    },
  });
  await world.host.open();
  await world.host.handle({ t: 'open', id: 's-old' });
  const notice = lastFrame(world, 'notice');
  assert.equal(notice.kind, 'error');
  assert.match(notice.text, /no stored conversation/);
  assert.equal(world.sockets.length, 1, 'a refused resume does not leave a second socket running');
});

test('a wrong token on the REST surface is a status, not a line in the transcript', async () => {
  const world = harness({
    routes: { ...defaultRoutes(), 'POST /api/sessions': reply({ detail: 'that is not the token' }, 401) },
  });
  await assert.rejects(world.host.open(), /that is not the token/);
  const failed = lastFrame(world, 'status');
  assert.equal(failed.state, 'failed');
  assert.match(failed.message, /openmirror\.token/);
});

test('a query with no conversation behind it says so instead of asking', async () => {
  const world = harness();
  await world.host.handle({ t: 'context' });
  await world.host.handle({ t: 'files', query: 'src' });
  await world.host.handle({ t: 'fork' });
  assert.equal(world.fetchImpl.calls.length, 0);
  assert.equal(world.frames.filter((frame) => frame.t === 'notice').length, 3);
});

// ---------------------------------------------------------------------------
// The outbox
// ---------------------------------------------------------------------------

test('a command larger than the socket will take is queued, then written', async () => {
  // A five-megabyte screenshot in a `turn.submit` is exactly the case. Dropping
  // it would lose the message; the queue is what makes the high-water mark
  // mean "wait" rather than "lose".
  const world = await connected({ maxBufferedBytes: 512 });
  const socket = world.sockets[0];
  socket.bufferedAmount = 64 * 1024;

  const text = 'x'.repeat(200000);
  assert.equal(world.host.command({ type: 'turn.submit', text, attachments: [] }), true);
  assert.deepEqual(socket.sent, [], 'it must not be written while the buffer is full');
  assert.equal(world.host._outbox.length, 1);

  socket.bufferedAmount = 0;
  await world.timers.fireAll();
  assert.equal(socket.sent.length, 1);
  assert.equal(socket.sent[0].text.length, 200000);
  assert.equal(world.host._outbox.length, 0);
});

test('a command sent before the socket is open is queued and flushed on open', async () => {
  // A frame in the outbox has not been handed to the socket, so writing it
  // after a reconnect cannot duplicate anything.
  const world = await harness();
  await world.host.open();
  world.host.command({ type: 'turn.interrupt' });
  assert.equal(world.sockets[0].sent.length, 0);
  assert.equal(world.host._outbox.length, 1);

  world.sockets[0].open();
  assert.deepEqual(world.sockets[0].sent, [{ type: 'turn.interrupt' }]);
  assert.equal(world.host._outbox.length, 0);
});

test('queued commands are flushed after the replay, not before it', async () => {
  const world = await connected();
  world.sockets[0].dropped();
  await world.timers.fireNext();
  world.sockets[1].open();
  world.sockets[1].emit({ type: 'text.delta', seq: 6, turn_id: 't-1', text: 'the gap' });
  world.host.command({ type: 'turn.submit', text: 'next', attachments: [] });
  assert.deepEqual(world.sockets[1].sent, [{ type: 'turn.submit', text: 'next', attachments: [] }]);
});

test('a socket that stops accepting mid-write keeps the frame for the next one', async () => {
  // A close between the check and the `send`. The frame is put back, and the
  // close that follows is what flushes it.
  const world = await connected();
  const socket = world.sockets[0];
  const send = socket.send.bind(socket);
  socket.send = (text) => {
    socket.readyState = 3;
    send(text);
  };
  world.host.command({ type: 'turn.interrupt' });
  assert.equal(socket.sent.length, 0, 'the frame was rejected, so it never went out');
  assert.equal(world.host._outbox.length, 1, 'a rejected frame is kept, not lost');

  socket.onclose({ code: 1006, reason: '', wasClean: false });
  await world.timers.fireNext();
  world.sockets[1].open();
  assert.deepEqual(world.sockets[1].sent, [{ type: 'turn.interrupt' }]);
});

test('the outbox has a ceiling, and says so when it drops something', async () => {
  const world = await connected({ maxBufferedBytes: 512 });
  world.sockets[0].bufferedAmount = 1 << 20;
  for (let index = 0; index < 70; index += 1) {
    world.host.command({ type: 'turn.submit', text: String(index), attachments: [] });
  }
  assert.equal(world.host._outbox.length, 64);
  assert.ok(world.logs.some((line) => line.includes('outbox is full')));
  // The newest command is the one that matters; the oldest is the one to lose.
  world.sockets[0].bufferedAmount = 0;
  await world.timers.fireAll();
  assert.equal(world.sockets[0].sent[world.sockets[0].sent.length - 1].text, '69');
});

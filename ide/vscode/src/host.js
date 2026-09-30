'use strict';

/**
 * The extension's bridge to a running openmirror daemon.
 *
 * This file exists because of one rule, written down in `PROTOCOL.md`: the
 * extension host owns the socket, and the webview never sees the daemon's
 * token, its URL, or its socket. The webview is a browser context that VS Code
 * renders; anything put in it is something a webview bug, an injected script
 * or a screenshot can read. The daemon's token runs commands on the
 * developer's machine, and passing it into a page to save writing a proxy would
 * be the one thing in this extension that undoes the confinement everything
 * else here is for.
 *
 * So this class is a pure client in the same sense `openmirror/tui.py` is: it
 * starts no daemon, spawns no agent, and keeps no state that outlives the
 * panel. The daemon stays the only thing holding sessions, providers and tools.
 *
 * Two decisions shape everything else here.
 *
 * 1. Every way out of this class is one funnel. Frames to the webview go
 *    through `emit()`, and that is the only place they can leave. It is also
 *    the one place the token invariant can be enforced, so a frame carrying
 *    the token is refused there rather than noticed afterwards in a bug report
 *    with a screenshot attached.
 *
 * 2. Everything that touches the outside world is injectable -- the HTTP
 *    client, the socket factory, the clock and the timers. That is not test
 *    hygiene for its own sake; it is what makes the reconnect policy, the
 *    backoff curve and the "no frame carries the token" claim checkable at all.
 *    A class that reached for the network directly could only be tested by
 *    running a daemon.
 *
 * What it deliberately does *not* do is translate the daemon's events. They
 * cross the bridge verbatim, as one `{t: 'event', event}` frame each. The
 * reasoning is in `PROTOCOL.md` and it holds up: `openmirror/static/app.js`
 * already renders the whole union, and a third renderer in `media/` is the one
 * that stops being updated when an event is added. A new event shows up in the
 * panel as one line saying it is unknown, which is the correct behaviour and
 * not a silent disappearance.
 */

/** The daemon's own defaults, from `openmirror/config.py`. */
const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_PORT = 8477;

/** `readyState` when a socket is open. Spelled out rather than read off the
 *  constructor because a test's fake socket has no static constants, and the
 *  browser API fixes the value at 1. */
const OPEN = 1;

/**
 * Close codes the daemon uses for "this will never work".
 *
 * Read out of `openmirror/routers/agent.py` rather than guessed:
 * 4401 before `accept()` when the token does not match, and 4404 when the
 * named session is not live or cannot be created. 4400 is carried for parity
 * with `tui.py`'s `FATAL_CLOSE`; `/ws/agent` does not send it today (only
 * `/ws/voice` does), and it is mapped to a protocol error so a future sender
 * still lands on a message rather than on an infinite retry.
 *
 * Retrying any of these turns one dead session id into an unbounded stream of
 * the same error, forever, with a status frame appearing every 20 seconds.
 */
const FATAL_CLOSE = {
  4400: 'the daemon closed the agent socket with a protocol error.',
  4401: 'the daemon refused the connection: its token is wrong.',
  4404:
    'this conversation is gone: the daemon has been restarted. Sessions live in memory and do not '
    + 'survive a restart, so start a new one or open a stored conversation.',
};

/** The approval modes `openmirror/agent/approval.py` defines.
 *
 *  The create endpoint answers with a *sentence* describing the mode while
 *  resume answers with the mode's own name, so the value has to be recognised
 *  rather than trusted, or the panel's status bar shows a paragraph where a
 *  two-word label belongs. */
const MODES = new Set(['read_only', 'plan', 'ask', 'auto_edit', 'trusted', 'unrestricted']);

/** Below this length a "token" is a string that would occur in ordinary text.
 *  Scanning frames for it would refuse nearly every one of them, which is a
 *  quieter failure than the leak it prevents. Real tokens are long; this is
 *  the length at which the scan starts meaning something. */
const MIN_SCAN_LENGTH = 8;

/** The daemon answered, and the answer was no. */
class DaemonError extends Error {
  constructor(status, detail, extra) {
    super(detail);
    this.name = 'DaemonError';
    this.status = status || 0;
    this.detail = detail;
    Object.assign(this, extra || {});
  }
}

/**
 * Where the daemon is and what it wants as proof.
 *
 * A pure function of a settings object and an environment, so the resolution
 * order is testable without constructing a `Host`: setting, then environment
 * variable, then the daemon's own default. The token may legitimately be
 * empty, which is the normal case on loopback, so an empty value is a value
 * and not a missing one -- and the default for it is empty too.
 */
function resolveDaemon(settings, env) {
  const config = settings || {};
  const environment = env || {};
  const pick = (key, variable, fallback) => {
    // `get` is there so extension.js may hand over a
    // `WorkspaceConfiguration` unchanged instead of unwrapping six keys into a
    // plain object first. Both shapes are things a caller will try.
    const fromSetting = typeof config.get === 'function' ? config.get(key) : config[key];
    if (fromSetting !== undefined && fromSetting !== null && String(fromSetting) !== '') {
      return String(fromSetting);
    }
    const fromEnv = environment[variable];
    if (fromEnv !== undefined && fromEnv !== null && String(fromEnv) !== '') {
      return String(fromEnv);
    }
    return fallback;
  };
  const port = Number(pick('port', 'OPENMIRROR_PORT', DEFAULT_PORT));
  return {
    host: pick('host', 'OPENMIRROR_HOST', DEFAULT_HOST),
    // A setting of "eight thousand" is not a port. Falling back beats starting
    // a request against `NaN`, which `fetch` rejects with a message nobody can
    // act on.
    port: Number.isFinite(port) && port > 0 ? Math.trunc(port) : DEFAULT_PORT,
    token: pick('token', 'OPENMIRROR_TOKEN', ''),
  };
}

/**
 * `require('./ws')` at the moment a socket is wanted rather than at the top of
 * this file. Two reasons, and the second is the real one: the tests drive this
 * class with a fake socket and must not need a WebSocket implementation, and a
 * module that cannot resolve should fail when somebody opens a panel rather
 * than when the extension activates and refuses to load at all.
 *
 * The export shape is tolerated three ways because the browser `WebSocket` API
 * can reasonably be published as the class, as a named export or as a default.
 */
function defaultCreateSocket(url) {
  // The export name is `WebSocketClient`, not `WebSocket`, because it is not
  // the browser's: it is a dependency-free RFC 6455 client for a Node whose
  // global `WebSocket` may not exist. The aliases below are kept only so a
  // hand-edited `ws.js` exporting a differently-named constructor still works
  // — the primary name is checked first, and the failure names the file.
  const module_ = require('./ws');
  const ctor = module_.WebSocketClient
    || module_.WebSocket
    || module_.default
    || (typeof module_ === 'function' ? module_ : null);
  if (typeof ctor !== 'function') {
    throw new Error('src/ws.js does not export a WebSocketClient constructor');
  }
  return new ctor(url);
}

class Host {
  /**
   * @param {object} options
   * @param {object} [options.settings]     the `openmirror.*` settings, as read
   *   from `workspace.getConfiguration('openmirror')`. Missing keys are unset,
   *   which is what VS Code returns for a setting nobody has changed.
   * @param {object} [options.env]          defaults to `process.env`.
   * @param {function} [options.fetch]      defaults to the global `fetch`.
   * @param {function} [options.createSocket]  `(url) => WebSocket`.
   * @param {function} [options.send]       `(frame) => void`, to the webview.
   * @param {function} [options.log]        `(message) => void`, the output channel.
   * @param {string}  [options.root]        the workspace folder, for a new conversation.
   * @param {string}  [options.model]       default model for a new conversation.
   * @param {string}  [options.provider]    default provider, likewise.
   * @param {string}  [options.mode]        default approval mode, likewise.
   * @param {string}  [options.effort]      default thinking level, likewise.
   * @param {string}  [options.title]       title for a new conversation.
   * @param {string[]} [options.tools]      toolset names; empty means all of them.
   * @param {number}  [options.requestTimeoutMs] a request that does not answer is cut off here.
   * @param {number}  [options.maxBufferedBytes] the socket's high-water mark.
   * @param {number}  [options.maxQueuedCommands] the outbox ceiling.
   * @param {number}  [options.drainIntervalMs] how often a full buffer is looked at again.
   * @param {number}  [options.retryBaseMs] first backoff.
   * @param {number}  [options.retryCapMs]  the ceiling the backoff doubles into.
   * @param {object}  [options.timers]      `{setTimeout, clearTimeout}`.
   */
  constructor(options) {
    const given = options || {};
    this._settings = given.settings || {};
    this._env = given.env || process.env;
    this._fetch = given.fetch || ((...args) => globalThis.fetch(...args));
    this._createSocket = given.createSocket || defaultCreateSocket;
    this._send = given.send || (() => {});
    // `console.error` rather than a no-op: a silent host is how a reconnect
    // bug turns into a panel that quietly stops working. extension.js passes
    // the real output channel; this is the floor.
    this._log = given.log || ((message) => console.error(`openmirror: ${message}`));

    this._root = given.root || '';
    this._defaults = {
      model: given.model || '',
      provider: given.provider || '',
      mode: given.mode || '',
      effort: given.effort || '',
      title: given.title || '',
      tools: Array.isArray(given.tools) ? given.tools.slice() : [],
    };

    this._requestTimeoutMs = given.requestTimeoutMs === undefined ? 10000 : given.requestTimeoutMs;
    this._maxBufferedBytes = given.maxBufferedBytes === undefined ? 1 << 20 : given.maxBufferedBytes;
    this._maxQueued = given.maxQueuedCommands === undefined ? 64 : given.maxQueuedCommands;
    this._drainIntervalMs = given.drainIntervalMs === undefined ? 50 : given.drainIntervalMs;
    // 0.5s doubling to 20s, the curve `tui.py` uses. Short enough that a daemon
    // restart is invisible, long enough that a daemon that is simply not
    // running is not hammered once a second for an hour.
    this._retryBaseMs = given.retryBaseMs === undefined ? 500 : given.retryBaseMs;
    this._retryCapMs = given.retryCapMs === undefined ? 20000 : given.retryCapMs;

    this._timers = given.timers || { setTimeout, clearTimeout };

    const where = resolveDaemon(this._settings, this._env);
    this._host = where.host;
    this._port = where.port;
    this._token = where.token;

    this._socket = null;
    this._sessionId = '';
    this._since = 0;
    this._attempt = 0;
    this._outbox = [];
    this._drainTimer = null;
    this._retryTimer = null;
    this._disposed = false;
    /** The last known description of what the panel is looking at. Seeded from
     *  the create/resume answer and refined by the two events that carry it, so
     *  the `config` frame is right even when the mode was changed by another
     *  client attached to the same session. */
    this.info = {
      model: this._defaults.model,
      mode: this._defaults.mode,
      title: this._defaults.title,
      root: this._root,
      provider: this._defaults.provider,
      effort: this._defaults.effort,
    };
  }

  // -- the daemon ---------------------------------------------------------

  get host() {
    return this._host;
  }

  get port() {
    return this._port;
  }

  get token() {
    return this._token;
  }

  get base() {
    let host = this._host || DEFAULT_HOST;
    // An IPv6 literal is a URL, not a mess. `http://::1:8477` is not a host.
    if (host.includes(':') && !host.startsWith('[')) {
      host = `[${host}]`;
    }
    return `http://${host}:${this._port}`;
  }

  get wsBase() {
    return `ws${this.base.slice('http'.length)}`;
  }

  get sessionId() {
    return this._sessionId;
  }

  get since() {
    return this._since;
  }

  get connected() {
    return Boolean(this._socket) && this._socket.readyState === OPEN;
  }

  get disposed() {
    return this._disposed;
  }

  /** What the panel is looking at, as the `config` frame carries it. */
  configFrame() {
    return {
      t: 'config',
      sessionId: this._sessionId,
      model: this.info.model,
      mode: this.info.mode,
      title: this.info.title,
      root: this.info.root,
      provider: this.info.provider,
      effort: this.info.effort,
    };
  }

  // -- the only way a frame leaves ----------------------------------------

  /**
   * Send one frame to the webview, refusing any that carries the token.
   *
   * Every frame in the class goes through here, which is the point: the
   * credential rule is checkable at one place instead of being a thing to
   * remember in twenty. A frame that would carry it is dropped and logged
   * rather than thrown -- throwing inside the funnel would take the panel down
   * over a bug that should never have been written, and a webview that loses
   * one line is a far smaller failure than a browser context holding a
   * credential that runs commands on this machine.
   */
  emit(frame) {
    let text = '';
    try {
      text = JSON.stringify(frame === undefined ? null : frame);
    } catch (error) {
      this._log(`a frame could not be serialised: ${error && error.message}`);
      return false;
    }
    if (this._token && this._token.length >= MIN_SCAN_LENGTH && text.includes(this._token)) {
      this._log('refused to send a frame to the webview: it carried the daemon token');
      return false;
    }
    this._send(frame);
    return true;
  }

  status(state, message) {
    this.emit({ t: 'status', state, message: String(message || '') });
  }

  notice(text, kind) {
    this.emit({ t: 'notice', text: String(text || ''), kind: String(kind || 'info') });
  }

  // -- HTTP ---------------------------------------------------------------

  _headers(withBody) {
    // Empty when there is no token, because `Bearer ` with nothing after it is
    // a header that says "I tried" rather than one that says nothing.
    const headers = {};
    if (withBody) {
      headers['Content-Type'] = 'application/json';
    }
    if (this._token) {
      headers.Authorization = `Bearer ${this._token}`;
      headers['X-Openmirror-Token'] = this._token;
    }
    return headers;
  }

  /**
   * One HTTP call to the daemon, with a deadline.
   *
   * The deadline is not politeness. A request that never answers is what a
   * daemon that has wedged, not crashed, looks like from here, and without
   * `AbortController` the panel would sit on a spinner for ever with nothing
   * to show. The abort is also the only way a test can prove the timeout path
   * exists without waiting ten seconds for it.
   */
  async request(method, path_, options) {
    const given = options || {};
    const url = new URL(this.base + path_);
    for (const [key, value] of Object.entries(given.params || {})) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value));
      }
    }
    const controller = new AbortController();
    const budget = given.timeoutMs === undefined ? this._requestTimeoutMs : given.timeoutMs;
    let expired = null;
    const guard = this._timers.setTimeout(() => {
      expired = new DaemonError(0, `${method} ${path_} did not answer within ${Math.round(budget / 1000)}s`, {
        timedOut: true,
      });
      controller.abort();
    }, budget);
    let response;
    let body;
    try {
      response = await this._fetch(url.toString(), {
        method,
        headers: this._headers(given.body !== undefined),
        body: given.body === undefined ? undefined : JSON.stringify(given.body),
        signal: controller.signal,
      });
      // Read inside the deadline, not after it. `fetch` resolves on the
      // response headers, so a daemon that sends those and then stalls on the
      // body would otherwise hang with the guard already cleared.
      body = await this._body(response);
    } catch (error) {
      if (expired) {
        throw expired;
      }
      // `fetch` rejects with a TypeError for a refused connection, a DNS
      // failure and a bad port alike. The daemon is on loopback, so nearly
      // always it is the first of those, and the only useful thing to say
      // about it is how to start it.
      throw new DaemonError(0, `${this.base} is not answering: ${error && error.message}`, {
        unreachable: true,
      });
    } finally {
      this._timers.clearTimeout(guard);
    }

    if (!response.ok) {
      const detail = body && typeof body === 'object' && body.detail ? String(body.detail) : `HTTP ${response.status}`;
      throw new DaemonError(response.status, detail);
    }
    return body;
  }

  async _body(response) {
    // A failed *read* is deliberately left to `request()`. An abort in the
    // middle of one is the deadline firing, and swallowing it here would turn a
    // timeout into an empty answer -- the one thing a deadline exists to
    // prevent.
    const text = await response.text();
    // An error page is not JSON and a 204 has no body; neither is worth an
    // exception in a path whose whole job is to turn them into a message.
    try {
      return text ? JSON.parse(text) : {};
    } catch (error) {
      return {};
    }
  }

  // -- conversations ------------------------------------------------------

  /** The `POST /api/sessions` body.
   *
   *  Only what was configured, so the daemon keeps its own defaults for the
   *  rest. Sending `model: null` and sending nothing are the same thing to
   *  pydantic, but a body full of nulls is a body somebody has to read twice
   *  when the daemon starts answering differently. */
  _createBody(overrides) {
    const given = overrides || {};
    const body = {
      root: given.root !== undefined ? given.root : this._root,
      title: given.title !== undefined ? given.title : this._defaults.title,
    };
    for (const key of ['model', 'provider', 'mode', 'effort']) {
      const value = given[key] !== undefined ? given[key] : this._defaults[key];
      if (value) {
        body[key] = value;
      }
    }
    const tools = given.tools !== undefined ? given.tools : this._defaults.tools;
    if (tools && tools.length) {
      body.tools = Array.isArray(tools) ? tools.slice() : [String(tools)];
    }
    return body;
  }

  async createSession(overrides) {
    return this.request('POST', '/api/sessions', { body: this._createBody(overrides) });
  }

  async resume(id) {
    // The root, model and toolset come from the stored transcript, not from
    // this request. A conversation about one project reopened in another is a
    // conversation that will confidently edit the wrong files.
    const answer = await this.request('POST', `/api/sessions/${encodeURIComponent(id)}/resume`);
    return (answer && answer.session) || {};
  }

  async fork(at) {
    // `at: 0` means every message, which is the daemon's own reading of zero
    // (routers/agent.py, `Fork`).
    const body = {};
    if (at) {
      body.at = Number(at);
    }
    const answer = await this.request('POST', `/api/sessions/${encodeURIComponent(this._sessionId)}/fork`, { body });
    return (answer && answer.session) || {};
  }

  async storedSessions(limit) {
    const answer = await this.request('GET', '/api/sessions/stored', {
      params: { limit: limit === undefined ? 100 : limit },
    });
    return (answer && answer.sessions) || [];
  }

  // -- the socket ---------------------------------------------------------

  /** The upgrade URL. Three query parameters, and the third is the token.
   *
   *  `?token=` rather than a header because that is what the daemon accepts
   *  here (`agent.py`, `token: str | None = Query(None)`), and it is the same
   *  one `tui.py` sends. It is built here and nowhere else, and it is never
   *  put in a frame. */
  _upgradeUrl() {
    const url = new URL(`${this.wsBase}/ws/agent`);
    url.searchParams.set('session', this._sessionId);
    url.searchParams.set('since', String(this._since));
    if (this._token) {
      url.searchParams.set('token', this._token);
    }
    return url.toString();
  }

  /**
   * Open the socket, at the last sequence number seen.
   *
   * `since` is owned by this class and moves in exactly one place: the
   * `seq` of an event that actually arrived. The daemon replays the gap, which
   * is what makes a dropped connection cost a moment rather than a transcript.
   */
  attach() {
    if (this._disposed) {
      return null;
    }
    this._closeSocket();
    // Every attach says so, whichever asked for it: the first one, a new
    // conversation, a resume, a fork and a reconnect all arrive as `open` or
    // as `failed`, and a panel with no way to say "trying" is a panel that
    // looks broken while the daemon starts.
    this.status('connecting', `attaching to ${this.base}`);
    const socket = this._createSocket(this._upgradeUrl());
    this._socket = socket;
    socket.onopen = () => {
      if (this._socket !== socket) {
        return;
      }
      this._attempt = 0;
      this.status('open', `attached to ${this.base}`);
      // Flush after `open`, not before: the replay the daemon owes us must
      // land before anything this client was holding, or the transcript would
      // read backwards.
      this._drain();
    };
    socket.onmessage = (message) => this._onMessage(message, socket);
    socket.onerror = () => {
      // Deliberately empty. A socket error carries no detail the browser API
      // chooses to share, and the close that follows carries the code. Logging
      // an empty event here would fill the output channel with nothing.
    };
    socket.onclose = (event) => this._onClose(event, socket);
    return socket;
  }

  _onMessage(message, socket) {
    if (this._socket !== socket) {
      // A frame from a socket that has already been replaced by a reconnect.
      // Its events are in the daemon's log and the replay will carry them.
      return;
    }
    let event;
    try {
      event = JSON.parse(typeof message.data === 'string' ? message.data : String(message.data));
    } catch (error) {
      this._log('ignoring a frame from the daemon that was not JSON');
      return;
    }
    if (!event || typeof event !== 'object' || Array.isArray(event)) {
      return;
    }
    // The one place `since` moves. An event with no `seq` leaves it alone
    // rather than zeroing it, because a zero would make the next reconnect
    // replay the entire conversation.
    const sequence = event.seq;
    if (Number.isInteger(sequence) && sequence > this._since) {
      this._since = sequence;
    }
    this._absorb(event);
    // Verbatim. No renaming, no reshaping, no filtering -- including events
    // this file has never heard of. See the header.
    this.emit({ t: 'event', event });
  }

  /** Keep the `config` frame honest using only the two events that carry the
   *  facts it holds. Every other event passes through untouched. */
  _absorb(event) {
    const type = String(event.type || '');
    if (type === 'session.started') {
      if (event.model) {
        this.info.model = String(event.model);
      }
      if (event.cwd) {
        this.info.root = String(event.cwd);
      }
      if (MODES.has(String(event.policy || ''))) {
        this.info.mode = String(event.policy);
      }
      if (event.effort !== undefined) {
        this.info.effort = event.effort === null ? '' : String(event.effort);
      }
    } else if (type === 'policy.changed') {
      if (event.mode) {
        this.info.mode = String(event.mode);
      }
      if (event.effort !== undefined) {
        this.info.effort = event.effort === null ? '' : String(event.effort);
      }
      if (event.model) {
        this.info.model = String(event.model);
      }
      if (event.provider) {
        this.info.provider = String(event.provider);
      }
    }
  }

  _onClose(event, socket) {
    if (this._socket !== socket) {
      return;
    }
    this._socket = null;
    this._stopDrain();
    if (this._disposed) {
      return;
    }
    const code = event && Number.isInteger(event.code) ? event.code : 0;
    const fatal = FATAL_CLOSE[code];
    if (fatal) {
      // The daemon's reason goes to the log and not into a frame: a close
      // reason arrives off the wire, and this class's promise is that nothing
      // unvetted crosses into the page.
      this._log(`agent socket closed with ${code}${event && event.reason ? `: ${event.reason}` : ''}`);
      // Anything still queued was meant for a session that no longer exists.
      this._outbox.length = 0;
      this.status('failed', fatal);
      return;
    }
    if (event && event.wasClean) {
      this.status('closed', 'the daemon closed the connection');
      return;
    }
    this._attempt += 1;
    this._scheduleRetry();
  }

  /** Back off and try again, saying how long for.
   *
   *  Kept apart from `_onClose` because the socket factory can also fail from
   *  inside the timer callback, and an exception thrown there is an unhandled
   *  exception that takes the whole extension host with it. */
  _scheduleRetry() {
    const delay = this._backoff(this._attempt);
    this.status(
      'closed',
      `the connection dropped mid-conversation; retrying in ${Math.round(delay / 100) / 10}s `
      + `(attempt ${this._attempt})`,
    );
    this._retryTimer = this._timers.setTimeout(() => {
      this._retryTimer = null;
      if (this._disposed) {
        return;
      }
      try {
        this.attach();
      } catch (error) {
        this._log(`the agent socket could not be opened: ${error && error.message}`);
        this._attempt += 1;
        this._scheduleRetry();
      }
    }, delay);
  }

  /** 0.5s, 1s, 2s ... 20s, and then 20s. The same curve as `tui.py`'s
   *  `Socket.events`, because two clients that recover from a daemon restart on
   *  different schedules is one more thing to explain. */
  _backoff(attempt) {
    const raw = this._retryBaseMs * 2 ** Math.max(0, attempt - 1);
    return Math.min(raw, this._retryCapMs);
  }

  _closeSocket() {
    const socket = this._socket;
    this._socket = null;
    if (socket) {
      // Cleared first: a socket whose handlers are still live would report a
      // close into the object that is already replacing it.
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        socket.close();
      } catch (error) {
        // A socket that cannot be closed is a socket that is already gone.
      }
    }
  }

  // -- commands out, and the outbox ---------------------------------------

  /**
   * Hand one command to the daemon, queueing it when the socket cannot take it.
   *
   * Queuing is not a fallback, it is the rule. A frame above the socket's
   * high-water mark has not been sent, and dropping it loses a text delta, an
   * approval or an answer -- and a lost approval is a tool call that hangs
   * until somebody notices. A frame in the outbox has *not* been handed to the
   * socket, so flushing it after a reconnect cannot duplicate anything: the
   * only frames at risk of being lost are the ones already handed over, and
   * those the daemon's replay cannot recover either.
   */
  command(frame) {
    if (this._disposed) {
      return false;
    }
    const socket = this._socket;
    if (!socket || socket.readyState !== OPEN) {
      this._enqueue(frame);
      return true;
    }
    if (Number(socket.bufferedAmount || 0) > this._maxBufferedBytes) {
      this._enqueue(frame);
      return true;
    }
    try {
      socket.send(JSON.stringify(frame));
    } catch (error) {
      this._writeFailed(socket, frame, error, false);
    }
    return true;
  }

  /** What to do about a frame the socket would not take.
   *
   *  Two different failures wearing the same exception. A socket that is no
   *  longer open means a close is on its way, and the frame is worth keeping --
   *  re-queueing it is what the next `open` flushes. A socket that is *still*
   *  open rejected the frame itself, so it is not something the wire can carry:
   *  putting it back would leave the drain polling at `drainIntervalMs` for the
   *  rest of the panel's life, retrying a frame that can never go. */
  _writeFailed(socket, frame, error, fromDrain) {
    this._log(`a command could not be written to the socket: ${error && error.message}`);
    if (socket.readyState !== OPEN) {
      this._enqueue(frame);
    } else if (fromDrain) {
      // The frames behind this one are probably fine, but nothing is going to
      // tell us when this one becomes sendable, so the poll stops here.
      this._stopDrain();
    }
  }

  _enqueue(frame) {
    this._outbox.push(frame);
    if (this._outbox.length > this._maxQueued) {
      // A ceiling, not a policy. Five megabytes of screenshot per submit, from
      // somebody holding a key down, must not be an unbounded allocation in
      // the extension host.
      this._outbox.shift();
      this._log(`outbox is full; dropped the oldest of ${this._maxQueued} queued commands`);
    }
    this._armDrain();
  }

  _armDrain() {
    if (this._drainTimer !== null || !this._outbox.length) {
      return;
    }
    this._drainTimer = this._timers.setTimeout(() => {
      this._drainTimer = null;
      this._drain();
    }, this._drainIntervalMs);
  }

  _stopDrain() {
    if (this._drainTimer !== null) {
      this._timers.clearTimeout(this._drainTimer);
      this._drainTimer = null;
    }
  }

  _drain() {
    const socket = this._socket;
    if (!socket || socket.readyState !== OPEN) {
      // A reconnect flushes on open. The timer is not re-armed here because
      // polling a socket that is not there is a busy loop wearing a coat.
      return;
    }
    while (this._outbox.length && Number(socket.bufferedAmount || 0) <= this._maxBufferedBytes) {
      const frame = this._outbox.shift();
      try {
        socket.send(JSON.stringify(frame));
      } catch (error) {
        this._outbox.unshift(frame);
        this._writeFailed(socket, frame, error, true);
        return;
      }
    }
    if (this._outbox.length) {
      // Still over the mark. Re-arm rather than spin: the buffer drains as the
      // kernel takes it, and a loop here would burn a core doing nothing.
      this._armDrain();
    }
  }

  // -- the webview's frames -----------------------------------------------

  /**
   * Handle one frame from the webview.
   *
   * The whole table in `PROTOCOL.md` is here and nothing else is, and an
   * unknown `t` is ignored and logged rather than thrown. A stale webview
   * paired with a new host is a version skew somebody will hit -- the
   * extension updates, the panel does not, and a thrown error would take the
   * panel down over a frame the person did not send. Returns whether the frame
   * was one this host knows.
   */
  async handle(frame) {
    if (!frame || typeof frame !== 'object' || Array.isArray(frame)) {
      this._log('ignoring a frame from the webview that is not an object');
      return false;
    }
    const t = String(frame.t || '');
    const handler = HANDLERS[t];
    if (!handler) {
      this._log(`ignoring an unknown frame from the webview: ${JSON.stringify(t)}`);
      return false;
    }
    try {
      await handler.call(this, frame);
    } catch (error) {
      this._report(error);
    }
    return true;
  }

  _report(error) {
    const message = error && error.message ? String(error.message) : String(error);
    this._log(message);
    // A fact about the link to the daemon gets a `status`, not a `notice`: the
    // panel shows the link, and "the daemon is not running" or "the token is
    // wrong" is not something to scroll past in the middle of a transcript.
    // Everything else is about one request, and a one-line notice is right.
    if (error && (error.status === 401 || error.status === 403)) {
      // The HTTP guard's own words are 'that is not the token', which does not
      // say where to change it. `tui.py` says the same thing here, and both of
      // them are wrong in the same direction: the token lives in a setting.
      this.status('failed', `${message} Set \`openmirror.token\`, or OPENMIRROR_TOKEN, to the daemon's token.`);
    } else if (error && (error.unreachable || error.timedOut)) {
      this.status('failed', `${message} Start it with \`openmirror serve\`, then reopen the panel.`);
    } else {
      this.notice(message, 'error');
    }
  }

  _needSession(what) {
    if (this._sessionId) {
      return true;
    }
    this.notice(`there is no conversation to ${what} yet`, 'warn');
    return false;
  }

  /** Reattach to a session, replaying from the beginning.
   *
   *  A new conversation has nothing to replay, and a resumed one must replay
   *  everything, so `since` is zeroed rather than kept: carrying the old
   *  session's last sequence number into a new one would make the daemon skip
   *  events that have never been sent. */
  async _reattach(session, verb) {
    this._sessionId = String((session && session.id) || '');
    this._since = 0;
    this._attempt = 0;
    this._outbox.length = 0;
    if (!this._sessionId) {
      throw new DaemonError(0, `the daemon did not hand back a session id for ${verb}`);
    }
    this.info = {
      model: String((session && session.model) || this.info.model || ''),
      mode: this._adoptMode(session),
      title: String((session && session.title) || ''),
      root: String((session && session.root) || this._root || ''),
      provider: String((session && session.provider) || this.info.provider || ''),
      effort: (session && session.effort !== undefined)
        ? String(session.effort || '')
        : this.info.effort,
    };
    // `attach()` says `connecting`; there is nothing to say between here and
    // there, and a duplicate `status` frame is a flicker in the panel.
    this.attach();
    // What `/` offers belongs to the *session*, not the panel: a resumed
    // conversation has the skills that were installed when it was had, and a
    // panel showing the ones from another project would offer a command that
    // does not exist here. Fire and forget — the menu is not worth a failed
    // session change over, and a rejection here is already reported.
    this.commands().catch(() => {});
    return session;
  }
  /** The create endpoint answers with a sentence about the mode; resume answers
   *  with the mode's own name. Only one of those is a value a status bar can
   *  show, so the sentence is dropped and what was asked for is kept. */
  _adoptMode(session) {
    const answer = session && session.policy !== undefined ? String(session.policy) : '';
    if (MODES.has(answer)) {
      return answer;
    }
    return this._defaults.mode || '';
  }

  /**
   * Start a new conversation and attach to it.
   *
   * The two steps, and the reason they are two: a session is created over HTTP
   * and *attached to* over the socket, because a socket is a window onto work
   * that is happening anyway. Creating first means the panel can show the
   * conversation's real id from the moment it exists, rather than inventing one
   * and hoping the daemon agrees.
   */
  async open(overrides) {
    try {
      const session = await this.createSession(overrides);
      return await this._reattach(session, 'a new conversation');
    } catch (error) {
      this._report(error);
      throw error;
    }
  }

  /** Resume a stored conversation and attach to it.
   *
   *  The root, model and toolset come from the stored transcript rather than
   *  from this call, so a conversation about one project cannot be reopened in
   *  another and left to confidently edit the wrong files. */
  async reopen(id) {
    try {
      const session = await this.resume(id);
      return await this._reattach(session, `conversation ${id}`);
    } catch (error) {
      this._report(error);
      throw error;
    }
  }

  /** Fork the current conversation and attach to the copy.
   *
   *  The old conversation is left completely alone and the files are shared; a
   *  fork is a different *conversation*, and `/clear` would have thrown away the
   *  context that explains what changed your mind. */
  async forkTo(at) {
    try {
      const session = await this.fork(at);
      return await this._reattach(session, 'a fork');
    } catch (error) {
      this._report(error);
      throw error;
    }
  }

  /** The context report, as the `context` frame carries it.
   *
   *  The daemon names these `total_in` / `total_out` and the protocol doc
   *  names them `totalIn` / `totalOut`, so both are emitted: the documented
   *  shape is what `media/` is written against, and dropping the daemon's own
   *  names would be this class quietly deciding the doc is right. `fraction`
   *  and `model` come along because the report already has them and a panel
   *  that wants a bar should not have to ask twice. */
  async context() {
    if (!this._needSession('measure the context of')) {
      return null;
    }
    const report = await this.request('GET', `/api/sessions/${encodeURIComponent(this._sessionId)}/context`);
    const frame = {
      t: 'context',
      tokens: report.tokens || 0,
      limit: report.limit || 0,
      window: report.window === undefined ? null : report.window,
      totalIn: report.total_in || 0,
      totalOut: report.total_out || 0,
      exact: Boolean(report.exact),
      fraction: report.fraction,
      model: report.model,
    };
    this.emit(frame);
    return frame;
  }

  async commands() {
    if (!this._needSession('list commands for')) {
      return null;
    }
    const answer = await this.request('GET', `/api/sessions/${encodeURIComponent(this._sessionId)}/commands`);
    const frame = { t: 'commands', items: (answer && answer.commands) || [] };
    this.emit(frame);
    return frame;
  }

  /** The `@`-menu. The endpoint's parameter is `q`, not `query`
   *  (`routers/agent.py`, `find_files`), and the daemon does the matching and
   *  the ranking; re-filtering here would be a second, weaker copy of it. */
  async files(query, limit) {
    if (!this._needSession('list files for')) {
      return null;
    }
    const answer = await this.request('GET', `/api/sessions/${encodeURIComponent(this._sessionId)}/files`, {
      params: { q: query || '', limit: limit === undefined ? 40 : limit },
    });
    const frame = {
      t: 'files',
      items: (answer && answer.files) || [],
      root: (answer && answer.root) || this.info.root,
    };
    this.emit(frame);
    return frame;
  }

  async contexts(limit) {
    const sessions = await this.storedSessions(limit);
    const frame = { t: 'sessions', items: sessions };
    this.emit(frame);
    return frame;
  }

  /** Agree or refuse a hook command for this session.
   *
   *  `POST /api/sessions/{id}/hooks/agree` exists and the protocol doc's
   *  webview table has no frame for it, so this is reachable only from
   *  extension.js. It is here rather than being left out because the daemon
   *  already asks (`hook.approval` crosses the bridge as an ordinary event)
   *  and a host that cannot answer would leave the panel showing a question
   *  with no way to reply to it. */
  async agreeHook(command, allow) {
    if (!this._needSession('agree a hook for')) {
      return null;
    }
    return this.request('POST', `/api/sessions/${encodeURIComponent(this._sessionId)}/hooks/agree`, {
      body: { command: String(command || ''), allow: allow !== false },
    });
  }

  /** Send a frame to the webview that did not come from the daemon: what a
   *  command in `extension.js` wants the person to read. */
  say(text, kind) {
    return this.notice(text, kind);
  }

  dispose() {
    this._disposed = true;
    this._stopDrain();
    if (this._retryTimer !== null) {
      this._timers.clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
    this._outbox.length = 0;
    this._closeSocket();
  }
}

/**
 * The webview -> host table, one method per row of `PROTOCOL.md`.
 *
 * A table rather than a switch so the mapping can be read in one place, and so
 * the test can walk the same rows the document lists: a row added to the
 * protocol without a handler here fails that test rather than being discovered
 * by somebody pressing a button.
 */
const HANDLERS = {
  /** The webview has rendered. The only moment the `config` frame is sent. */
  ready() {
    this.emit(this.configFrame());
    // Also here, not only on a session change: a webview that reloads loses
    // its menu and needs it back, and `ready` is the only frame that means
    // "the page can hear me again".
    this.commands().catch(() => {});
  },

  submit(frame) {
    // `attachments` is `[{type, data, media_type}]` with base64 and no data-URL
    // prefix, which is exactly what `agent.py` hands to `agent.submit`. The
    // webview does the reading of the file; a base64 blob in a webview would
    // have to come from the extension host anyway.
    this.command({
      type: 'turn.submit',
      text: String(frame.text || ''),
      attachments: Array.isArray(frame.attachments) ? frame.attachments : [],
    });
  },

  approve(frame) {
    this.command({ type: 'tool.approve', call_id: String(frame.callId || ''), remember: Boolean(frame.remember) });
  },

  deny(frame) {
    this.command({ type: 'tool.deny', call_id: String(frame.callId || ''), reason: String(frame.reason || '') });
  },

  answer(frame) {
    this.command({
      type: 'question.answer',
      question_id: String(frame.questionId || ''),
      answer: String(frame.answer || ''),
    });
  },

  interrupt() {
    this.command({ type: 'turn.interrupt' });
  },

  /** The approval mode and the thinking level are one daemon command.
   *  `agent.py` reads `mode` and `effort` off the same `policy.set`, and each
   *  may arrive alone, so each is sent alone. */
  policy(frame) {
    this.command({ type: 'policy.set', mode: String(frame.mode || '') });
  },

  effort(frame) {
    this.command({ type: 'policy.set', effort: String(frame.level || '') });
  },

  /** There is no `model.set` on the agent socket.
   *
   *  The daemon's own answer is that `/model <name>` is a *turn command*
   *  (`agent/session.py`, the slash dispatcher, calling `set_model`), and the
   *  tui sends every slash command as ordinary turn text for exactly this
   *  reason. So a model change is a submit, and the `policy.changed` event
   *  that comes back is how the panel learns it worked. Inventing a
   *  `model.set` command here would be a frame the daemon answers with
   *  `unknown command`. */
  model(frame) {
    const name = String(frame.name || '').trim();
    if (!name) {
      this.notice('say which model, e.g. /model anthropic', 'warn');
      return;
    }
    this.command({ type: 'turn.submit', text: `/model ${name}`, attachments: [] });
  },

  files(frame) {
    return this.files(String(frame.query || ''));
  },

  /**
   * Try the daemon again, now.
   *
   * A `failed` status is terminal on the host's side: a bad token or a daemon
   * that has restarted will not fix itself, and retrying forever is a log
   * filling up at twenty seconds a line. But the reason it failed is often
   * something the person just did — they started the daemon, they fixed the
   * token — and the only way back without this frame is closing the tab and
   * opening it again, which throws away a conversation that is still on disk.
   */
  reconnect() {
    if (this._disposed) {
      return undefined;
    }
    // A `failed` link stops the retry loop, so the counter is what it decides
    // the next delay is. Zeroing it here is what makes "now" mean now rather
    // than "in twenty seconds".
    this._attempt = 0;
    this._outbox.length = 0;
    this.attach();
    return this.commands().catch(() => {});
  },

  /**
   * Agree to a hook, or refuse it, from the panel.
   *
   * One click, and a refusal is the default the panel offers first. The
   * banner is the one place the daemon asks permission for something that is
   * not a tool call, and a banner whose only answer lives in the command
   * palette is a banner people learn to read past.
   */
  hook(frame) {
    return this.agreeHook(String(frame.command || ''), frame.allow !== false);
  },

  new() {
    return this.open({});
  },

  open(frame) {
    const id = String(frame.id || '');
    if (!id) {
      this.notice('no conversation was named to open', 'warn');
      return undefined;
    }
    return this.reopen(id);
  },

  fork() {
    if (!this._needSession('fork')) {
      return undefined;
    }
    return this.forkTo(0);
  },

  context() {
    return this.context();
  },

  contexts() {
    return this.contexts();
  },

  /** The webview's own log, into the extension's output channel. Not a frame
   *  and not a daemon command: a console message from the page belongs beside
   *  the extension's own logs, where somebody debugging the panel will look. */
  log(frame) {
    this._log(`webview [${String(frame.level || 'log')}]: ${String(frame.message || '')}`);
  },
};

module.exports = Host;
module.exports.Host = Host;
module.exports.DaemonError = DaemonError;
module.exports.resolveDaemon = resolveDaemon;
module.exports.FATAL_CLOSE = FATAL_CLOSE;
module.exports.HANDLERS = HANDLERS;
module.exports.DEFAULT_HOST = DEFAULT_HOST;
module.exports.DEFAULT_PORT = DEFAULT_PORT;
module.exports.MODES = MODES;

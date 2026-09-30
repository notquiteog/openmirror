/* The four things every part of the panel needs.
 *
 * A port of `openmirror/static/dom.js` and no more, because the browser client
 * is the reference renderer of this protocol and a second set of decisions
 * about how a number is written is a second set to keep in step.
 *
 * What is *not* here, and is the whole point of `PROTOCOL.md`: no `fetch`, no
 * `WebSocket`, no `token()`, no `socket()`. The extension host owns the socket
 * and the credential; this file has exactly one way out and it is
 * `postMessage`. There is no base URL to build a request against and no token
 * to attach, because a webview is a browser context and a credential in one is
 * a credential on a screenshot.
 *
 * Every builder here takes its text as a string and puts it in with
 * `textContent`. There is no `innerHTML` in this directory, and that is not
 * tidiness: a transcript contains whatever the agent read, which is whatever
 * was in the repository, which is attacker-controlled by definition. A panel
 * that renders a tool result as markup is a panel that executes the repo.
 */

/** One node, by selector. Spelled the same way as the browser client's. */
export const $ = (selector, root) => (root || document).querySelector(selector);

/**
 * An element with its text already in it.
 *
 * The `text` argument is the only way text gets into a node this file builds,
 * and it goes in as a text node. Every caller passes agent-supplied strings
 * through here: a model response, a tool result, a file path, a session title,
 * a command description.
 */
export const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) {
    node.className = cls;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
};

/** A button, typed, so it can live inside the composer's form without sending. */
export const button = (cls, label, title) => {
  const node = el('button', cls, label);
  node.type = 'button';
  if (title) {
    node.title = title;
    node.setAttribute('aria-label', title);
  }
  return node;
};

/** Empty a node without going near `innerHTML`. */
export const clear = (node) => {
  node.textContent = '';
  return node;
};

/** Set text on a node, or empty it. `null` is a thing that is not there yet. */
export const say = (node, value) => {
  node.textContent = value === undefined || value === null ? '' : String(value);
  return node;
};

/* ------------------------------------------------------------------ the bus */

/* `acquireVsCodeApi` may only be called once per page, and it throws if it is
 * called twice -- so the handle is kept here rather than handed to each module.
 * A panel that failed to attach should still render: an empty transcript with a
 * working composer is a far better failure than a blank rectangle. */
let api = null;
let failed = false;

function vscode() {
  if (api || failed) {
    return api;
  }
  try {
    api = acquireVsCodeApi();
  } catch (error) {
    failed = true;
    // The output channel is the only place this is visible, since there is no
    // host to tell. A page opened outside VS Code (a plain browser, a test
    // harness) lands here and that is a supported way to look at the markup.
    if (typeof console !== 'undefined') {
      console.error(`openmirror panel: no VS Code API: ${error && error.message}`);
    }
  }
  return api;
}

/**
 * One frame out. Every frame in this directory goes through here.
 *
 * The `t` names are the table in `PROTOCOL.md` and nothing is sent that is not
 * in it. There is no catch-all: a frame the host does not know is logged and
 * ignored on the other side, and inventing one here would be a panel that
 * looks like it works against a host that does not.
 */
export function send(frame) {
  const bridge = vscode();
  if (!bridge) {
    return false;
  }
  bridge.postMessage(frame);
  return true;
}

/** The webview's own log, forwarded to the extension's output channel. */
export function note(level, message) {
  return send({ t: 'log', level: String(level || 'log'), message: String(message) });
}

/** Errors are worth the channel; ordinary chatter is not. */
export function warn(message) {
  return note('warn', message);
}

/**
 * Frames in. A handler that throws is logged rather than allowed to take the
 * listener down: one malformed event must not stop the rest of the stream.
 */
export function receive(handler) {
  window.addEventListener('message', (message) => {
    const frame = message && message.data;
    if (!frame || typeof frame !== 'object' || Array.isArray(frame)) {
      return;
    }
    try {
      handler(frame);
    } catch (error) {
      warn(`a frame could not be handled (${frame.t}): ${error && error.message}`);
    }
  });
}

// ------------------------------------------------------------------- numbers

/**
 * One line, at most `limit` characters, with an ellipsis if it was cut.
 *
 * Ported from `openmirror/tui.py`'s `flat` rather than invented, because the
 * terminal and this panel are the same protocol's two front ends and a tool
 * summary that reads one way in `openmirror chat` and another way here is the
 * kind of drift nobody notices until somebody trusts the wrong one.
 */
export function flat(value, limit) {
  const line = String(value === undefined || value === null ? '' : value).split(/\s+/).join(' ').trim();
  const max = limit || 90;
  if (line.length <= max) {
    return line;
  }
  // The ellipsis is three ASCII characters rather than the one `tui.py` writes,
  // so it is counted out of the budget rather than added on top of it. The
  // promise is "at most `limit` characters" and it is the same promise in both
  // clients, which is the part that matters.
  return `${line.slice(0, Math.max(1, max - 3)).replace(/\s+$/, '')}...`;
}

/** A token count at the size a person reads it at. `tui.py`'s `human`. */
export function human(count) {
  const n = Math.max(0, Math.floor(Number(count) || 0));
  if (n < 1000) {
    return String(n);
  }
  if (n < 1000000) {
    return `${(n / 1000).toFixed(1)}k`;
  }
  return `${(n / 1000000).toFixed(1)}M`;
}

/**
 * A duration, in the units somebody would say out loud.
 *
 * `tui.py` says in its own docstring that this mirrors the browser client's
 * `took` on purpose, and this is the third one. Three clients that disagree
 * about how long something took is a small thing that costs trust.
 */
export function took(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) {
    return '';
  }
  if (value < 1000) {
    return `${Math.round(value)}ms`;
  }
  if (value < 60000) {
    return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)}s`;
  }
  const minutes = Math.floor(value / 60000);
  return `${minutes}m ${Math.round((value % 60000) / 1000)}s`;
}

/** How long ago, in the four bands a person reads. `app.js`'s `ago`. */
export function ago(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  if (value < 60) {
    return 'now';
  }
  if (value < 3600) {
    return `${Math.floor(value / 60)}m`;
  }
  if (value < 86400) {
    return `${Math.floor(value / 3600)}h`;
  }
  return `${Math.floor(value / 86400)}d`;
}

/** `updated` on a stored conversation is a unix timestamp, not a duration. */
export function when(timestamp, now) {
  const at = Number(timestamp);
  if (!Number.isFinite(at) || at <= 0) {
    return '';
  }
  const reference = Number.isFinite(now) ? now : Date.now() / 1000;
  return ago(reference - at);
}

/** A file size, at the size a person reads it at. `attachments.js`'s `kb`. */
export function kb(bytes) {
  const n = Math.max(0, Number(bytes) || 0);
  if (n >= 1024 * 1024) {
    return `${Math.round(n / (1024 * 1024))}MB`;
  }
  return `${Math.max(1, Math.round(n / 1024))}kB`;
}

/** The last segment of a path, for a chip wide enough for one word. */
export function basename(path) {
  const value = String(path || '').replace(/\/+$/, '');
  return value.split('/').pop() || value;
}

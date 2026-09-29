/* The four things every part of this page needs.
 *
 * Extracted when the client grew from one screen to six. They were duplicated
 * for about an hour first, which was long enough to notice that `api` in
 * particular must not be: it is the one place that knows the daemon has gone,
 * and two copies of that knowledge means one of them showing "live" while the
 * other has been failing for a minute.
 */

export const $ = (sel, root = document) => root.querySelector(sel);

export const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* One of the symbols defined at the top of the page. */
export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'ic');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

/* Whoever wants to know when the daemon comes and goes. */
const reachability = new Set();
let reachable = true;

export function onReachable(fn) {
  reachability.add(fn);
}

function setReachable(ok) {
  if (ok === reachable) return;
  reachable = ok;
  for (const fn of reachability) fn(ok);
}

/* A 401 means "you have not proved who you are", which is a different thing
 * from "the daemon is not there" and needs a different response: a person has
 * to type a token, and no amount of retrying will do it for them. Without this
 * the 401 is swallowed by the `catch` below and the page just sits there with
 * every list empty, which reads as a bug rather than as a locked door.
 *
 * One gate, and it only ever runs once: the first 401 puts the sign-in card
 * up, and the exchange itself is the one call allowed through while it waits. */
const GATE_PATH = '/api/auth/session';
let gate = null;
let gated = false;

function showGate() {
  if (gated) return;
  gated = true;
  if (!gate) gate = buildGate();
  gate.hidden = false;
  document.body.classList.add('signed-out');
}

function buildGate() {
  const card = document.createElement('div');
  card.className = 'gate';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');

  const title = document.createElement('h2');
  title.textContent = 'This install is locked';

  const note = document.createElement('p');
  note.className = 'gate-note';
  note.textContent =
    'Paste the OPENMIRROR_TOKEN this daemon was started with. It is stored as a cookie on this machine only.';

  const form = document.createElement('form');
  const field = document.createElement('input');
  field.type = 'password';
  field.name = 'token';
  field.autocomplete = 'off';
  field.spellcheck = false;
  field.placeholder = 'token';
  field.setAttribute('aria-label', 'Access token');

  const button = document.createElement('button');
  button.type = 'submit';
  button.textContent = 'Unlock';

  const problem = document.createElement('p');
  problem.className = 'gate-problem';
  problem.setAttribute('role', 'alert');

  form.append(field, button);
  card.append(title, note, form, problem);
  document.body.append(card);

  const attempt = async (event) => {
    event?.preventDefault();
    problem.textContent = '';
    const res = await fetch(GATE_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: field.value.trim() }),
    });
    if (res.ok) {
      // Reload rather than trying to resume in place: half the page has
      // already decided it is signed out, and a reload is the one path that
      // cannot leave some of it wrong.
      location.reload();
      return;
    }
    field.value = '';
    field.focus();
    problem.textContent = 'That is not the token.';
  };

  form.addEventListener('submit', attempt);
  field.focus();
  return card;
}

/* Every call to the daemon goes through here.
 *
 * The page is expected to outlive the process it talks to: the daemon gets
 * restarted, the laptop sleeps, the tab sits open overnight. So a failed fetch
 * is an ordinary condition rather than an exception, and callers get null
 * instead of a rejection — an unhandled rejection in a five-second poll is
 * particularly bad, because that poll is the thing that notices the daemon
 * came back. */
export async function api(path, options) {
  try {
    const res = await fetch(path, options);
    setReachable(true);
    // A 401 is the daemon answering — it is there and it is saying no — so it
    // must not be confused with the fetch failing below, which means the daemon
    // is gone. The sign-in route is the one thing exempt, since it is the call
    // that resolves this.
    if (res.status === 401 && !path.startsWith(GATE_PATH)) showGate();
    return res;
  } catch {
    setReachable(false);
    return null;
  }
}

/* The common case: a JSON body, or null if anything at all went wrong. The
 * status is deliberately not exposed here — a caller that needs to tell 404
 * from 500 should use `api` and look. */
export async function json(path, options) {
  const res = await api(path, options);
  if (!res || !res.ok) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}

export async function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/* A POST, and both halves of the answer.
 *
 * `post` hands back a `Response` and `json` hands back the body or null on
 * anything that is not a 2xx. Between them there is no way to write the thing
 * most of these calls actually want, which is *why* it did not work: the
 * success case needs the body, and the failure case needs the `detail` the
 * server sent — and a `null` from `json` throws the second away exactly when
 * it is the part worth showing.
 *
 * `timeout` bounds a call that waits on a model. Without one, a button that
 * says "writing…" does so for ever when the provider is slow, and the only
 * way out is reloading the page — which loses whatever else was in the dialog.
 * The abort is reported through the same `detail` channel as a refusal, so
 * every call site handles one thing rather than two.
 *
 * A daemon that has gone is `status: 0` rather than a thrown error, for the
 * same reason `api` catches: this page is expected to outlive the process, and
 * an unhandled rejection here would be how a tab found out it was alone.
 */
export async function send(path, body, { timeout = 0 } = {}) {
  const options = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  // `AbortSignal.timeout` is not in Node and not in older Safari, and a
  // caller that cannot abort still has to be able to say something went
  // wrong — so the signal is optional and its absence is not an error.
  if (timeout > 0 && typeof AbortSignal.timeout === 'function') {
    options.signal = AbortSignal.timeout(timeout);
  }

  const res = await api(path, options);
  if (!res) {
    /* An aborted fetch arrives here as a null exactly like a daemon that has
       gone, and the two want opposite reactions: one says "try again", the
       other says "the thing you asked for took too long". Saying "the daemon
       did not answer" for a slow model is the more confusing of the two
       errors to hand somebody. */
    return {
      status: 0,
      data: null,
      detail: options.signal && options.signal.aborted
        ? `no answer after ${Math.round(timeout / 1000)}s — the model may be slow or busy. Try again.`
        : 'the daemon did not answer',
    };
  }

  let data = null;
  try {
    data = await res.json();
  } catch {
    /* Not JSON. An HTML error page from a proxy, most often — and its text
       is in `detail` below, so this is not a dead end. */
  }
  const detail = (data && typeof data.detail === 'string' && data.detail)
    || (res.ok ? '' : `the server refused it (HTTP ${res.status})`);
  return { status: res.status, data, detail };
}

/* The token, if this page was opened with one. Every socket needs it and
 * every one of them was reading it out of the query string separately. */
export function token() {
  return new URLSearchParams(location.search).get('token');
}

/* A websocket URL on this origin, with the token attached. */
export function socket(path, params = {}) {
  const url = new URL(path, location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') url.searchParams.set(key, String(value));
  }
  const t = token();
  if (t) url.searchParams.set('token', t);
  return url;
}

export function took(ms) {
  // Rounded: a /command that answers at once takes a few microseconds, and
  // the raw figure printed as "0.0000419464111328ms".
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`;
  const mins = Math.floor(ms / 60000);
  return `${mins}m ${Math.round((ms % 60000) / 1000)}s`;
}

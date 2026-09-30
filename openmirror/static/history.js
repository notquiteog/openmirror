/* Finding an old conversation, and taking one with you.
 *
 * Two features, one module, because they answer the same question asked at two
 * different times. "Where was that conversation about the retry loop?" and
 * "send that to someone" both start from a transcript, and both were gaps:
 * the session list shows what is open and what was open, ordered by recency,
 * which is useless past about twenty rows on a machine that has been working
 * all week.
 *
 * Search is over the *stored* transcripts rather than the live list, so it
 * finds a conversation that has been closed for a week. The server answers
 * with the matching text and the character offsets of the match inside it —
 * never markup. A transcript contains whatever the agent read, so a server
 * that returned HTML would make every future client responsible for escaping
 * it, forever. Offsets survive a client that renders the text its own way.
 *
 * The highlighting below therefore builds text nodes and wraps the matched
 * ranges itself. Everything here is set with `textContent` or a text node, so
 * a conversation about `<script>` is displayed as the words and cannot become
 * markup.
 *
 * Share writes a single self-contained HTML file, which is what openCode's
 * `/share` amounts to for a tool that has nowhere to publish. One file, no
 * scripts, no network references, and it opens in any browser with the
 * network off.
 *
 * session-id-exempt: every `/api/sessions/${…}` in this file is an id out of
 * a search result or a resume response, in scope as a parameter — never the
 * session currently on screen. Reading `state.sessionId` here would be the bug
 * the rule exists to prevent: sharing or reopening the conversation you happen
 * to be looking at instead of the one whose row you pressed.
 */

import { $, api, el } from './dom.js';

const DEBOUNCE = 220;
const MAX_RESULTS = 40;

let timer = null;
let ticket = 0;
let onEmpty = null;
let onOpen = null;

/* Wire the box. `onEmpty` puts the ordinary session list back, and `onOpen`
   takes a session id — the app owns what opening one means, and reimplementing
   it here would be a second copy of the reattach dance. */
export function wireHistory({ onEmpty: restore, onOpen: open } = {}) {
  onEmpty = restore;
  onOpen = open;
  const box = $('#session-search');
  if (!box) return;

  box.addEventListener('input', () => {
    clearTimeout(timer);
    const query = box.value.trim();
    if (!query) {
      show();
      return;
    }
    // Debounced because this runs on every keystroke, and because a search
    // that fires per character is a search that feels like it is guessing.
    timer = setTimeout(() => run(query), DEBOUNCE);
  });

  box.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && box.value) {
      event.preventDefault();
      clear();
      return;
    }
    if (event.key !== 'Enter') return;
    // Enter runs it now rather than after the pause, which is what somebody
    // typing a whole phrase and then Enter expects to happen.
    event.preventDefault();
    clearTimeout(timer);
    const query = box.value.trim();
    if (query) run(query);
  });
}

export function clear() {
  const box = $('#session-search');
  if (box) box.value = '';
  clearTimeout(timer);
  show();
}

/* Whether a search is on screen.
 *
 * The session list is polled, and a poll that re-renders while somebody is
 * reading search results replaces them with the live list every few seconds —
 * which looks exactly like the search flickering and losing. The poller asks
 * this and leaves the list alone.
 */
export function active() {
  const box = $('#session-search');
  return Boolean(box && box.value.trim());
}

function show() {
  ticket += 1;                      // any answer still in flight is now stale
  if (onEmpty) onEmpty();
}

/* The search itself. Every request carries a ticket, and a reply whose ticket
 * is stale is dropped: two fetches racing is how a list ends up showing
 * results for a query that has already been edited.
 */
async function run(query) {
  const mine = ++ticket;
  const list = $('#sessions');

  const params = new URLSearchParams({ q: query, limit: String(MAX_RESULTS) });
  const res = await api(`/api/sessions/search?${params}`);
  if (mine !== ticket) return;

  if (!res || !res.ok) {
    list.textContent = '';
    list.appendChild(el('li', 'empty', 'The search could not be reached.'));
    return;
  }
  const { results } = await res.json();
  if (mine !== ticket) return;

  list.textContent = '';
  if (!results.length) {
    const none = el('li', 'empty', `Nothing in any conversation matches “${query}”.`);
    list.appendChild(none);
    return;
  }

  const head = el('li', 'group');
  head.appendChild(el('span', '', `${results.length} conversation${results.length === 1 ? '' : 's'}`));
  list.appendChild(head);
  for (const hit of results) list.appendChild(row(hit));
}

function row(hit) {
  const li = el('li', 'hit');
  li.setAttribute('aria-label', `${hit.title || hit.id} — ${plain(hit)}`);

  li.appendChild(el('span', 'name', hit.title || hit.id));
  li.appendChild(el('span', 'when', since(hit.updated)));
  li.appendChild(marked(hit));
  if (hit.root) li.appendChild(el('span', 'where', lastPart(hit.root)));

  const button = el('button', 'share');
  button.type = 'button';
  button.textContent = 'share';
  button.title = 'Write this conversation out as one HTML file';
  button.onclick = (event) => {
    event.stopPropagation();
    share(hit.id);
  };
  li.appendChild(button);

  li.onclick = () => go(hit);
  return li;
}

/* The excerpt with its matches wrapped, as text nodes plus <mark>. */
function marked(hit) {
  const out = el('div', 'excerpt');
  const text = hit.excerpt || '';
  const spans = (hit.spans || []).slice().sort((a, b) => a[0] - b[0]);

  let at = 0;
  for (const [start, end] of spans) {
    // A span that does not fit the string it arrived with is skipped rather
    // than sliced: a slice past the end throws, and this is a list somebody is
    // in the middle of reading.
    if (!(start >= at && end <= text.length)) continue;
    if (start > at) out.appendChild(document.createTextNode(text.slice(at, start)));
    out.appendChild(el('mark', '', text.slice(start, end)));
    at = end;
  }
  if (at < text.length) out.appendChild(document.createTextNode(text.slice(at)));
  if (!out.childNodes.length) out.textContent = text;
  return out;
}

function plain(hit) {
  return (hit.excerpt || '').replace(/\s+/g, ' ').slice(0, 160);
}

/* Opening a search result. A stored conversation is a file on disk, and a hit
   in the sidebar is somebody who has decided to go back to work in it — so it
   is resumed if it is not already live, and the app's own opener does the
   rest. */
async function go(hit) {
  if (hit.unfinished && !hit.id) return;
  const live = await api('/api/sessions');
  if (live && live.ok) {
    const { sessions } = await live.json();
    if (sessions.some((s) => s.id === hit.id)) {
      clear();
      if (onOpen) onOpen(hit.id);
      return;
    }
  }
  const res = await api(`/api/sessions/${hit.id}/resume`, { method: 'POST' });
  if (!res || !res.ok) {
    flash('That conversation could not be resumed.');
    return;
  }
  const session = await res.json();
  clear();
  if (onOpen) onOpen(session.id);
}

/* The share button. A download rather than a link, because there is nowhere to
   host it: the honest version of "share this conversation" for a tool that
   runs on your own machine is a file you can then do whatever you like with —
   mail it, put it in a repo, paste it into a doc. */
async function share(id) {
  const res = await api(`/api/sessions/${id}/export.html`);
  if (!res || !res.ok) {
    flash('That conversation could not be written out.');
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = el('a');
  link.href = url;
  link.download = filename(res) || `${id}.html`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked a moment later rather than immediately: some browsers cancel a
  // download whose object URL disappears in the same tick.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  flash('Written as one HTML file. No scripts in it, and it needs no network.');
}

/* The name the server asked for, rather than one invented here that would not
   match. Content-Disposition's filename is quoted and may be percent-encoded;
   both are undone, and anything that comes back unusable is dropped so the
   browser falls back to the id. */
function filename(res) {
  const header = res.headers.get('content-disposition') || '';
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
  if (!match) return '';
  try {
    const name = decodeURIComponent(match[1].trim());
    return /^[A-Za-z0-9._ -]{1,80}$/.test(name) ? name : '';
  } catch {
    return '';
  }
}

/* A line under the list, for the two things that can go wrong here. Not the
   app's global toast: that lives in `app.js`, which wires the whole page on
   import, and importing it from this module would run the app twice. */
function flash(text) {
  const list = $('#sessions');
  if (!list) return;
  const line = el('li', 'empty', text);
  list.appendChild(line);
  setTimeout(() => line.remove(), 6000);
}

function lastPart(path) {
  const parts = String(path || '').split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || '';
}

function since(stamp) {
  if (!stamp) return '';
  const seconds = (Date.now() - stamp * 1000) / 1000;
  if (seconds < 60) return 'now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

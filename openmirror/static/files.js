/* Mentioning a file, with `@`.
 *
 * Every other harness has this and it is the single biggest thing missing
 * from a composer that only takes prose: being able to say "this file" without
 * pasting a path and hoping. It is small, and it is also the thing that makes
 * the difference between asking about a file and asking about a file *by
 * name* — which is what a model can actually find.
 *
 * The design constraint is that it runs on every keystroke, so:
 *
 *   - The query is debounced, and the list is replaced rather than appended
 *     to, so a fast typist sees one request in flight at a time.
 *   - A request whose answer arrives after a newer one is dropped. Two fetches
 *     racing is how a list ends up showing `app.js` for a query that has
 *     already become `apple`.
 *   - Enter completes rather than sends while the list is open, because
 *     `@app` followed by Enter nearly always means "I meant that one". It sends
 *     when the text is already a complete path, which is the same rule the
 *     slash menu uses and for the same reason.
 *   - Nothing is read. The server sends names, sizes and times; the excerpt
 *     beside a suggestion is fetched only for the one under the cursor, and
 *     only when the list is at rest.
 */

import { $, el } from './dom.js';

const state = {
  items: [],
  at: 0,
  open: false,
  timer: null,
  ticket: 0,
  lastFetched: '',
  sessionId: () => null,
  request: null,
};

/* The one keydown handler, asked about this list first.
 *
 * A second listener on the same element does not work: `preventDefault()`
 * stops the browser's default, not the *other* listener, and the composer's
 * own Enter handler submits the form regardless. So this is called from the
 * existing handler and returns whether it consumed the key — the same shape
 * the slash menu uses, which is why the slash menu is checked there and not
 * here. */
export function handleFileKey(event) {
  if (!state.open || !state.items.length) return false;

  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const step = event.key === 'ArrowDown' ? 1 : -1;
    state.at = (state.at + step + state.items.length) % state.items.length;
    render();
    return true;
  }

  if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
    // A completed path sends; a partial one completes. Same as the slash
    // menu, for the same reason: `@app` then Enter means the file, and
    // `@src/app.js` then Enter means send it.
    const chosen = state.items[state.at];
    const found = queryAt();
    if (!chosen || !found) return false;
    if (event.key === 'Tab' || found.query !== chosen.path) {
      event.preventDefault();
      pick(chosen.path, found);
      return true;
    }
    return false;
  }

  if (event.key === 'Escape') {
    // Closing the list, not stopping the turn. The document-level Escape is
    // for that and this one is closer to hand.
    event.preventDefault();
    event.stopPropagation();
    hide();
    return true;
  }
  return false;
}

export function wireFiles({ sessionId, request }) {
  state.sessionId = sessionId;
  state.request = request;
  const input = $('#input');

  input.addEventListener('input', () => {
    const found = queryAt();
    if (!found) {
      hide();
      return;
    }
    // Same query, different caret position: the list is already right.
    if (found.query === state.lastFetched && state.open) {
      render();
      return;
    }
    clearTimeout(state.timer);
    const ticket = ++state.ticket;
    // 120ms is about where it stops feeling instant and starts feeling
    // laggy, measured by typing a path at the speed anybody actually does.
    state.timer = setTimeout(() => fetchFiles(found.query, ticket), 120);
  });

  // A click outside dismisses. `mousedown` and not `click`, so the composer
  // does not lose focus as a side effect of the list going away.
  document.addEventListener('mousedown', (event) => {
    if (state.open && !$('#files').contains(event.target) && event.target !== input) hide();
  });
}

/* Where the caret is, and what has been typed since the `@`. Null when there
   is no `@` being typed, which is most of the time.
 *
 * The `@` has to start a word. An email address mid-sentence is not a file
 * mention, and treating it as one turns every message about somebody's
 * address into a suggestion list. */
function queryAt() {
  const input = $('#input');
  const upto = input.value.slice(0, input.selectionStart ?? input.value.length);
  const match = /(?:^|\s)@([\w./-]*)$/.exec(upto);
  if (!match) return null;
  const at = upto.length - match[1].length - 1;
  return { at, query: match[1] };
}

async function fetchFiles(query, ticket) {
  const id = state.sessionId();
  if (!id || !state.request) return;
  try {
    const data = await state.request(`/api/sessions/${encodeURIComponent(id)}/files?q=${encodeURIComponent(query)}`);
    // Dropped if a newer query has been asked for since this one went out.
    // Two racing fetches is how a list shows the answer to a question that
    // has already been retyped.
    if (ticket !== state.ticket) return;
    state.items = (data && data.files) || [];
    state.at = 0;
    state.lastFetched = query;
    render();
  } catch {
    if (ticket === state.ticket) hide();
  }
}

function render() {
  const list = $('#files');
  list.textContent = '';
  if (!state.items.length) {
    hide();
    return;
  }
  state.open = true;
  list.hidden = false;

  state.items.slice(0, 8).forEach((file, index) => {
    const row = el('button', 'file-pick' + (index === state.at ? ' at' : ''));
    row.type = 'button';
    row.appendChild(el('span', 'path', file.path));
    const bits = [];
    if (file.size >= 1024) bits.push(`${Math.round(file.size / 1024)}k`);
    row.appendChild(el('span', 'size', bits.join(' ')));
    row.onmousedown = (event) => {
      // `mousedown`, so the click lands before the textarea loses focus and
      // the caret position the picker needs is still there.
      event.preventDefault();
      const found = queryAt();
      if (found) pick(file.path, found);
    };
    row.onmouseenter = () => {
      state.at = index;
      for (const other of list.children) other.classList.remove('at');
      row.classList.add('at');
    };
    list.appendChild(row);
  });
}

function pick(path, found) {
  const input = $('#input');
  const before = input.value.slice(0, found.at);
  const after = input.value.slice(input.selectionStart ?? input.value.length);
  // A trailing space so the next thing typed is not glued onto the path, and
  // a `/` in the middle of the sentence is not a problem: only the segment
  // after the caret was being typed.
  input.value = `${before}@${path} ${after}`;
  const caret = before.length + path.length + 2;
  input.setSelectionRange(caret, caret);
  hide();
  input.focus();
}

function hide() {
  $('#files').hidden = true;
  state.open = false;
  state.items = [];
  state.at = 0;
}

/* Called when the session changes: the list belongs to the session it was
   asked about, and offering files from the last one is worse than none. */
export function resetFiles() {
  clearTimeout(state.timer);
  state.ticket++;
  hide();
}

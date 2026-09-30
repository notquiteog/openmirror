/* Mentioning a file, with `@`.
 *
 * A port of `openmirror/static/files.js`, and the two constraints that make it
 * a separate file at all are the same there:
 *
 *   - The query is debounced, and the list is replaced rather than appended to,
 *     so a fast typist has one request in flight at a time.
 *   - A reply for a query that has been retyped is dropped. Two answers racing
 *     is how a list ends up showing `app.js` for a query that has already
 *     become `apple`, and it is the half that actually shows up as a wrong
 *     list -- the debounce is the easy half.
 *
 * The answers come from the `files` frame, which is an answer to a question --
 * unlike `commands` and `context`, which the host pushes because something just
 * happened that changed them. The panel asks with `{t: 'files', query}` and gets
 * back `{t: 'files', items: [{path, size, mtime}]}`. The daemon does the
 * matching and the ranking; re-filtering here would be a second, weaker copy of
 * a decision already made.
 *
 * The `@` has to start a word. An email address mid-sentence is not a file
 * mention, and treating it as one turns every message about somebody's address
 * into a suggestion list.
 */

import { $, button, el, kb } from './dom.js';

const LIMIT = 8;
const DEBOUNCE_MS = 120;

/**
 * Where the caret is, and what has been typed since the `@`.
 *
 * Null when there is no `@` being typed, which is most of the time.
 *
 * @param {string} text   the whole box
 * @param {number} caret  `selectionStart`, or the end of the text
 */
export function mentionAt(text, caret) {
  const value = String(text || '');
  const upto = value.slice(0, Number.isInteger(caret) ? caret : value.length);
  const match = /(?:^|\s)@([\w./-]*)$/.exec(upto);
  if (!match) {
    return null;
  }
  return { at: upto.length - match[1].length - 1, query: match[1] };
}

/** The box with the mention completed, and where the caret goes after it. */
export function completion(found, path, value) {
  const before = String(value || '').slice(0, found.at);
  const after = String(value || '').slice(found.at + 1 + String(found.query).length);
  // A trailing space, so the next thing typed is not glued onto the path. A `/`
  // in the middle of the sentence is not a problem: only the segment after the
  // caret was being typed.
  const text = `${before}@${path} ${after}`;
  return { text, caret: before.length + String(path).length + 2 };
}

/**
 * Enter completes rather than sends while the list is open, because `@app`
 * followed by Enter nearly always means "I meant that one". It sends when the
 * text is already a complete path, which is the slash menu's rule for the same
 * reason.
 */
export function mentionSends(found, item) {
  if (!found || !item) {
    return false;
  }
  return found.query === String(item.path || '');
}

/**
 * Whether a reply is still wanted.
 *
 * `asked` is the query the outstanding request went out for and `current` is
 * what is in the box now. A reply is dropped unless the two still agree, which
 * covers both races: a second request having gone out since, and the user
 * having typed on while the first was in the air. The `files` frame carries no
 * query of its own, so the question has to be answered locally or not at all.
 */
export function accepts(asked, current) {
  if (asked === null || asked === undefined) {
    return false;
  }
  return String(asked) === String(current === null || current === undefined ? '' : current);
}

/**
 * The menu, and the debounce that feeds it.
 *
 * @param {object} ctx
 * @param {function} ctx.ask     `(query)`, asks the host for candidates
 * @param {function} ctx.onPick  `(path)`, completes a mention
 */
export function createMentions(ctx) {
  const context = ctx || {};
  const list = $('#mentions');
  const state = {
    items: [], at: 0, open: false, current: null, asked: null, timer: null,
  };

  function hide() {
    list.hidden = true;
    list.textContent = '';
    state.items = [];
    state.at = 0;
    state.open = false;
  }

  function render() {
    list.textContent = '';
    if (!state.items.length) {
      hide();
      return;
    }
    state.open = true;
    list.hidden = false;
    state.items.slice(0, LIMIT).forEach((file, index) => {
      const row = button('', '', file.path);
      row.className = index === state.at ? 'file-pick at' : 'file-pick';
      row.append(el('span', 'path', file.path), el('span', 'size', kb(file.size)));
      row.title = file.path;
      // `mousedown`, so the pick lands before the textarea loses focus and the
      // caret position it needs is still there.
      row.onmousedown = (event) => {
        event.preventDefault();
        pick(file.path);
      };
      row.onmouseenter = () => {
        state.at = index;
        for (const other of list.children) {
          other.classList.remove('at');
        }
        row.classList.add('at');
      };
      list.appendChild(row);
    });
  }

  function pick(path) {
    hide();
    if (context.onPick) {
      context.onPick(path);
    }
  }

  /**
   * The `files` frame.
   *
   * Returns whether it was drawn, so a caller can tell a dropped reply from an
   * empty list -- and so the rule above is checkable from a test rather than
   * only by typing quickly and looking.
   */
  function receive(items) {
    if (!accepts(state.asked, state.current)) {
      return false;
    }
    state.items = Array.isArray(items) ? items : [];
    state.at = 0;
    render();
    return true;
  }

  function update(value, caret) {
    const found = mentionAt(value, caret);
    state.current = found ? found.query : null;
    if (!found) {
      clearTimeout(state.timer);
      state.timer = null;
      state.asked = null;
      hide();
      return;
    }
    // Same query, different caret position: the list is already right.
    if (found.query === state.asked && state.open) {
      render();
      return;
    }
    clearTimeout(state.timer);
    // 120ms is about where it stops feeling instant and starts feeling laggy,
    // measured by typing a path at the speed anybody actually does.
    state.timer = setTimeout(() => {
      state.timer = null;
      state.asked = found.query;
      if (context.ask) {
        context.ask(found.query);
      }
    }, DEBOUNCE_MS);
  }

  /**
   * The one keydown handler, asked about this list first.
   *
   * Called from the composer's own handler rather than added as a second one,
   * for the reason `slash.js` says: `preventDefault()` stops the browser's
   * default, not the other listener, and the composer's Enter would send.
   */
  function handleKey(event, value, caret) {
    if (!state.open || !state.items.length) {
      return false;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      state.at = (state.at + step + state.items.length) % state.items.length;
      render();
      return true;
    }
    if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
      const chosen = state.items[state.at];
      const found = mentionAt(value, caret);
      if (!chosen || !found) {
        return false;
      }
      if (event.key === 'Tab' || !mentionSends(found, chosen)) {
        event.preventDefault();
        pick(chosen.path);
        return true;
      }
      return false;
    }
    if (event.key === 'Escape') {
      // Closing the list, not stopping the turn: the panel's Escape is for
      // that and this one is closer to hand.
      event.preventDefault();
      event.stopPropagation();
      hide();
      return true;
    }
    return false;
  }

  /** Called when the conversation changes: the list belongs to the session it
   *  was asked about, and offering files from the last one is worse than none. */
  function reset() {
    clearTimeout(state.timer);
    state.timer = null;
    state.asked = null;
    state.current = null;
    hide();
  }

  if (list) {
    // A click outside dismisses. `mousedown` and not `click`, so the composer
    // does not lose focus as a side effect of the list going away.
    document.addEventListener('mousedown', (event) => {
      if (state.open && !list.contains(event.target)) {
        hide();
      }
    });
  }

  return { update, receive, handleKey, reset, hide, get open() { return state.open; } };
}

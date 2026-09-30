/* The `/` menu: the session's commands and skills.
 *
 * A port of the slash half of `openmirror/static/app.js`, and the same rules
 * because they are the right ones rather than because they are the ones
 * written down:
 *
 *   - `/` at the start of the box, and only while the *name* is being typed.
 *     Once there is a space what follows is the command's arguments and the
 *     menu has nothing left to offer.
 *   - Tab completes. Enter completes a name that is not finished yet and sends
 *     one that is: typing `/compact` in full and pressing Enter should run it.
 *   - Escape closes the menu, and closes *only* the menu. The panel's Escape
 *     stops the turn, and the one in the composer is closer to hand.
 *   - Names that start with what you typed come first, then the ones that
 *     merely contain it. Eight is enough to scan in a 300px panel and few
 *     enough that the list is not a scrollbar.
 *
 * The items arrive in the `commands` frame, which the host fetches whenever the
 * conversation changes -- on `ready`, and after `new`, `open` and `fork`. They
 * belong to the session, not to this panel: a resumed conversation has the
 * skills that were installed when it was had, and offering another project's is
 * a command that does not exist here.
 */

import { $, el, flat } from './dom.js';

const LIMIT = 8;

/** The name being typed, or null when the menu should be closed. */
export function slashQuery(text) {
  const match = /^\/([\w:-]*)$/.exec(String(text || ''));
  return match ? match[1].toLowerCase() : null;
}

/**
 * What the menu should offer for `query`.
 *
 * Prefix matches first, then substring matches, and a query of `''` keeps the
 * daemon's own order -- which is its ranking, and re-sorting it here would be a
 * second, weaker copy of a decision that was already made.
 */
export function filterCommands(commands, query, limit) {
  const list = Array.isArray(commands) ? commands : [];
  const wanted = String(query === undefined || query === null ? '' : query).toLowerCase();
  const starts = list.filter((item) => String(item.name || '').toLowerCase().startsWith(wanted));
  const inside = list.filter((item) => {
    const name = String(item.name || '').toLowerCase();
    return !name.startsWith(wanted) && name.includes(wanted);
  });
  return [...starts, ...inside].slice(0, limit || LIMIT);
}

/** What the box should say once a name is completed. A trailing space, so the
 *  next thing typed is not glued onto the command. */
export function completionText(item) {
  return `/${String((item && item.name) || '')} `;
}

/**
 * Enter sends rather than completes when the name is already finished.
 *
 * The same rule as the `@` menu and for the same reason: `@src/host.js` then
 * Enter means send it, and `@src/hos` then Enter means the file.
 */
export function slashSends(text, item) {
  if (!item) {
    return false;
  }
  const query = slashQuery(text);
  return query !== null && query === String(item.name || '').toLowerCase();
}

/**
 * The menu, wired to the composer.
 *
 * @param {object} ctx
 * @param {function} ctx.onPick   `(text)`, the completed line to put in the box
 */
export function createSlash(ctx) {
  const context = ctx || {};
  const menu = $('#slash');
  const state = { items: [], at: 0, open: false };
  let commands = [];

  function set(list) {
    commands = Array.isArray(list) ? list : [];
    // The list belongs to the session it was fetched for. If the panel has
    // moved on, the new one arrives with the next `commands` frame and until
    // then the menu is better empty than wrong.
    state.items = [];
    state.at = 0;
    hide();
  }

  function hide() {
    menu.hidden = true;
    menu.textContent = '';
    state.items = [];
    state.at = 0;
    state.open = false;
  }

  function render(query) {
    state.items = filterCommands(commands, query);
    if (!state.items.length) {
      hide();
      return;
    }
    state.at = Math.min(state.at, state.items.length - 1);
    menu.textContent = '';
    state.items.forEach((item, index) => {
      const row = el('li', index === state.at ? 'at' : '');
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', String(index === state.at));
      row.appendChild(el('span', 'name', `/${item.name}`));
      if (item.hint) {
        row.appendChild(el('span', 'hint', flat(item.hint, 24)));
      }
      row.appendChild(el('span', 'desc', flat(item.description, 60)));
      if (item.kind) {
        row.appendChild(el('span', 'kind', item.kind));
      }
      // `mousedown`, not `click`, so the box keeps its focus and its caret.
      row.onmousedown = (event) => {
        event.preventDefault();
        pick(index);
      };
      row.onmouseenter = () => {
        state.at = index;
        for (const other of menu.children) {
          other.classList.remove('at');
        }
        row.classList.add('at');
      };
      menu.appendChild(row);
    });
    menu.hidden = false;
    state.open = true;
  }

  function pick(index) {
    const item = state.items[index];
    if (!item) {
      return;
    }
    const text = completionText(item);
    hide();
    if (context.onPick) {
      context.onPick(text);
    }
  }

  /** Called on every keystroke. The menu is cheap; nothing is fetched. */
  function update(text) {
    const query = slashQuery(text);
    if (query === null || !commands.length) {
      hide();
      return;
    }
    render(query);
  }

  /**
   * The one keydown handler, asked about this menu first.
   *
   * A second listener on the textarea does not work: `preventDefault()` stops
   * the browser's default, not the *other* listener, and the composer's own
   * Enter handler would submit regardless. So this is called from the existing
   * handler and returns whether it consumed the key -- the same shape
   * `mentions.js` uses, and the reason both are called from there.
   */
  function handleKey(event, text) {
    if (!state.open || !state.items.length) {
      return false;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      state.at = (state.at + step + state.items.length) % state.items.length;
      render(slashQuery(text));
      return true;
    }
    if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey && !slashSends(text, state.items[state.at]))) {
      event.preventDefault();
      pick(state.at);
      return true;
    }
    if (event.key === 'Escape') {
      // Closing the menu, not stopping the turn.
      event.preventDefault();
      event.stopPropagation();
      hide();
      return true;
    }
    return false;
  }

  if (menu) {
    // A click outside dismisses. `mousedown` and not `click`, so the composer
    // does not lose focus as a side effect of the list going away.
    document.addEventListener('mousedown', (event) => {
      if (state.open && !menu.contains(event.target)) {
        hide();
      }
    });
  }

  return { set, update, hide, handleKey, get open() { return state.open; } };
}

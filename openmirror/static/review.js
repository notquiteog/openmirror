/* Reviewing a change one hunk at a time.
 *
 * The agent edited five files and the diff is a wall. "Take all of it" and
 * "undo the turn" are the only two things a diff view can offer, and neither
 * is useful, because changes rarely deserve all-or-nothing: of the four things
 * it did, three are right and the fourth is wrong, and undoing the turn to
 * get rid of the fourth throws the three away.
 *
 * So each hunk is a decision. Keep it, or put it back. Nothing is applied
 * until you press it, and the button says which files it is about to touch —
 * because "review" and "apply" are different acts and a panel that conflates
 * them is a panel nobody trusts.
 *
 * **The decisions are held here and sent once.** Flipping a hunk redraws the
 * preview, it does not call the server. Somebody comparing three options
 * should be able to flip between them as fast as they can think, and a
 * network round trip per flip makes the panel feel like a form.
 *
 * **The server owns the hunks.** They are cut from the snapshots rewind
 * already keeps, and the numbers you send back index *its* list, not a count
 * counted here — so a hunk cannot be dropped or duplicated by anything that
 * happens between loading the panel and pressing the button.
 */

import { $, el, json, send } from './dom.js';

const state = {
  enabled: false,
  files: [],
  keep: new Map(),   // path -> Set of hunk indices
  loaded: false,
  // A *function* returning the session id, handed in by `wireReview` — the
  // same shape `wireTalk` and `wireLive` take, because a copy of the value
  // goes stale the moment the session changes, and a stale one is how a panel
  // ends up describing the session you just left.
  sessionId: null,
};

const currentSession = () => (state.sessionId ? state.sessionId() : null);

function everyIndex(path) {
  const file = state.files.find((f) => f.path === path);
  return file ? file.all : [];
}

function keptFor(path) {
  if (!state.keep.has(path)) state.keep.set(path, new Set(everyIndex(path)));
  return state.keep.get(path);
}

function describeSelection() {
  let files = 0;
  let hunks = 0;
  for (const file of state.files) {
    const keep = keptFor(file.path);
    if (keep.size !== file.all.length) files += 1;
    for (const index of file.all) if (!keep.has(index)) hunks += 1;
  }
  if (!hunks) return 'nothing to apply — every hunk is kept';
  const bit = files === 1 ? '1 file' : `${files} files`;
  return `Put back ${hunks} hunk${hunks === 1 ? '' : 's'} in ${bit}`;
}

function render() {
  const body = $('#review-body');
  body.textContent = '';
  state.files.forEach((file) => {
    const keep = keptFor(file.path);
    const card = el('section', 'review-file');

    const head = el('header');
    head.appendChild(el('span', 'path', file.path));
    head.appendChild(el('span', 'tag', file.status));
    if (file.edited_since) {
      // Said, not hidden. A hand edit after the turn means these hunks are
      // not what is in the file, and applying over it would lose the edit.
      head.appendChild(el('span', 'tag warn', 'edited since'));
    }
    card.appendChild(head);

    if (file.edited_since) {
      card.appendChild(el(
        'p', 'meta',
        'Somebody edited this file after the turn finished. Reviewing it will be refused unless you '
        + 'say so, because the hunks below are not what is in the file now.',
      ));
    }

    for (const hunk of file.hunks) {
      const row = el('div', 'review-hunk' + (keep.has(hunk.index) ? ' kept' : ' dropped'));
      const bar = el('div', 'hunk-bar');
      const toggle = el('button', 'hunk-toggle', keep.has(hunk.index) ? 'Keep' : 'Put back');
      toggle.type = 'button';
      toggle.onclick = () => {
        if (keep.has(hunk.index)) keep.delete(hunk.index);
        else keep.add(hunk.index);
        render();
      };
      bar.appendChild(el('span', 'hunk-summary', hunk.summary));
      bar.appendChild(toggle);
      row.appendChild(bar);
      row.appendChild(el('pre', 'hunk-diff', hunk.diff));
      card.appendChild(row);
    }

    if (!file.hunks.length) {
      card.appendChild(el('p', 'meta', 'No change in this file any more.'));
    }
    body.appendChild(card);
  });

  $('#review-apply').textContent = describeSelection();
  $('#review-apply').disabled = !state.files.some((f) => keptFor(f.path).size !== f.all.length);
}

async function load({ draw = true } = {}) {
  const id = currentSession();
  if (!id) return;
  const data = await json(`/api/sessions/${id}/review`);
  if (!data || !data.enabled) {
    state.enabled = false;
    $('#nav-review').hidden = true;
    return;
  }
  state.enabled = true;
  // Un-hidden here, not only in the markup: the element ships `hidden` and
  // `resetReview` hides it, so this is the only place that can show it again.
  $('#nav-review').hidden = false;
  state.files = data.files || [];
  state.loaded = true;
  // Everything kept by default: the agent's work is the starting point, and
  // this is a panel for removing the parts you disagree with.
  state.keep = new Map(state.files.map((f) => [f.path, new Set(f.all)]));
  // Building the panel is only worth doing when the panel is open. Rendering
  // it on every turn end built a DOM node per hunk into a dialog nobody had
  // opened, and it showed up as measured dropped frames: 86 of 2916 over
  // 20ms at an 850-node transcript, where there had been none.
  if (draw) render();
}

async function apply() {
  const changed = state.files.filter((f) => keptFor(f.path).size !== f.all.length);
  if (!changed.length) return;
  const anyHandEdit = changed.some((f) => f.edited_since);
  if (anyHandEdit) {
    const ok = confirm(
      'A file in this review was edited by hand after the turn. Applying will write over that edit, '
      + 'which cannot be undone from here.\n\nApply anyway?',
    );
    if (!ok) return;
  }

  const button = $('#review-apply');
  button.disabled = true;
  button.textContent = 'Applying…';
  const results = [];
  for (const file of changed) {
    const keep = [...keptFor(file.path)];
    // eslint-disable-next-line no-await-in-loop - one file at a time, so a
    // failure halfway leaves the earlier files applied and reported.
    // `send` and not `post`: `post` answers with a `Response`, and reading
    // `.data` off one is `undefined` — which reported every successful apply
    // as a failure while the file behind it was written correctly. The worst
    // of both, and the reason `send` exists.
    const { data, detail } = await send(`/api/sessions/${currentSession()}/review`, {
      path: file.path, keep, force: anyHandEdit,
    });
    results.push({ path: file.path, ok: Boolean(data && data.ok), detail });
  }
  button.textContent = 'Applied';
  await load();
  const failed = results.filter((r) => !r.ok);
  if (failed.length) {
    $('#review-note').textContent = `${failed.length} file(s) could not be applied: ${failed.map((f) => f.path).join(', ')}`;
  } else {
    $('#review-note').textContent = `Put back ${results.length} change(s) across ${results.length} file(s).`;
  }
  // The rewind history and the commit bar both describe what is on disk.
  if (typeof window.refreshSessionsSoon === 'function') window.refreshSessionsSoon();
}

function setAll(keepThem) {
  for (const file of state.files) {
    state.keep.set(file.path, keepThem ? new Set(file.all) : new Set());
  }
  render();
}

export function wireReview({ sessionId }) {
  if (sessionId) state.sessionId = sessionId;
  const open = $('#nav-review');
  if (open) open.onclick = openDialog;
  const close = $('#review-close');
  if (close) close.onclick = () => $('#review-dialog').close();
  const go = $('#review-apply');
  if (go) go.onclick = apply;
  const all = $('#review-keep-all');
  if (all) all.onclick = () => setAll(true);
  const none = $('#review-drop-all');
  if (none) none.onclick = () => setAll(false);
}

function openDialog() {
  $('#review-note').textContent = '';
  $('#review-dialog').showModal();
  load();
}

/* Called when a turn ends, so the button appears without anybody looking for
   it — and only when there is something to look at. Fetches, does not draw:
   see `load`.
 */
export function refreshReview() {
  if (!currentSession()) return;
  load({ draw: false });
}

export function resetReview() {
  state.files = [];
  state.keep = new Map();
  $('#nav-review').hidden = true;
}

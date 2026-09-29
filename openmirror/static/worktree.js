/* Worktrees: a place to put a session so it cannot hurt the rest.
 *
 * The reason this exists rather than a switch is the shape of the switch.
 * `unconfined` opens the whole disk, which means somebody who needs to read
 * one directory next door either turns it on and stays scared, or does without
 * and works more slowly than they need to.
 *
 * A worktree is a second checkout on its own branch. A session opened in one
 * edits that one. This one, with whatever you have open in it, is somewhere
 * the agent cannot reach — and the branch is there afterwards to merge, look
 * at, or throw away.
 *
 * **Removal is a button that can fail.** `git worktree remove` refuses a
 * worktree with changes in it, and that refusal is left in place: a day of
 * work is not a directory to be lost to a tidy-up, and a "force" next to the
 * button would be one click from doing exactly that.
 */

import { $, el, json, send } from './dom.js';

const state = { sessionId: null, info: { worktrees: [], available: false, why: '' } };

/* A *function* returning the id, handed in by `wireWorktrees` — the same shape
   every other module here takes. Interpolating `state.sessionId` instead puts
   the function's own source text in the URL, which is a 404 that looks like a
   missing route. */
const currentSession = () => (state.sessionId ? state.sessionId() : null);

function render() {
  const body = $('#worktree-body');
  body.textContent = '';
  const info = state.info;

  if (!info.available) {
    body.appendChild(el('p', 'meta', info.why || 'Worktrees are not available here.'));
    body.appendChild(el(
      'p', 'meta',
      'A worktree is a second checkout of a git repository, on its own branch, that a session '
      + 'works in — so a turn cannot touch what you have open.',
    ));
    return;
  }

  for (const w of info.worktrees || []) {
    const row = el('div', 'wt-row');
    const head = el('div', 'wt-head');
    head.appendChild(el('span', 'wt-branch', w.branch));
    const bits = [];
    if (w.modified?.length) bits.push(`${w.modified.length} changed`);
    if (w.added?.length) bits.push(`${w.added.length} new`);
    if (w.removed?.length) bits.push(`${w.removed.length} removed`);
    head.appendChild(el('span', 'wt-state', w.dirty ? bits.join(', ') || 'dirty' : 'clean'));
    row.appendChild(head);
    row.appendChild(el('code', 'wt-path', w.path));

    // The main checkout has no remove button, and neither has a worktree
    // that came from somewhere else.
    if (w.branch && w.branch !== 'main' && w.branch !== 'master') {
      const drop = el('button', 'ghost small', 'Remove');
      drop.title = w.dirty
        ? 'This has changes in it, so it will be refused — copy them out first.'
        : 'Take this worktree away.';
      drop.onclick = async () => {
        if (w.dirty) {
          const ok = confirm(
            `This worktree has ${bits.join(', ') || 'changes'}.\n\n`
            + 'Removing it will be refused, which is the point. Copy anything you want to keep first.',
          );
          if (!ok) return;
        }
        drop.disabled = true;
        const { data, detail } = await send(`/api/sessions/${currentSession()}/worktree?path=${encodeURIComponent(w.path)}`,
          {}, { method: 'DELETE' });
        body.appendChild(el('p', data && data.ok ? 'meta' : 'update-error', detail || (data ? 'Removed.' : 'Not removed.')));
        await load();
      };
      row.appendChild(drop);
    }
    body.appendChild(row);
  }
}

async function load() {
  if (!currentSession()) return;
  const info = await json(`/api/sessions/${currentSession()}/worktrees`);
  if (info) state.info = info;
  render();
  return info;
}

/* The button appears only for a session in a git repository.

 * "This is not a git repository, so there is no branch to put anywhere" is an
 * answer to a question nobody asked, and a rail item that opens onto an
 * explanation of why it cannot help is worse than no rail item. */
export async function refreshWorktrees() {
  const button = $('#worktree-open');
  if (!button || !currentSession()) return;
  const info = await load();
  button.hidden = !(info && info.available);
}

async function make() {
  const label = $('#worktree-label').value.trim();
  const button = $('#worktree-make');
  button.disabled = true;
  button.textContent = 'Making…';
  const { data, detail } = await send(`/api/sessions/${currentSession()}/worktree`, { label });
  button.disabled = false;
  button.textContent = 'New worktree';
  if (!data || !data.worktree) {
    $('#worktree-note').textContent = detail || 'it could not be made';
    return;
  }
  $('#worktree-label').value = '';
  $('#worktree-note').textContent = `${data.worktree.branch} is ready.`;
  if (data.session) {
    // Open it as a session rather than telling somebody a path: the whole
    // point is that they do not have to do anything with it.
    location.href = `/?session=${data.session.id}`;
    return;
  }
  await load();
}

export function wireWorktrees({ sessionId }) {
  if (sessionId) state.sessionId = sessionId;
  const open = $('#worktree-open');
  if (open) {
    open.onclick = () => {
      $('#worktree-dialog').showModal();
      load();
    };
  }
  const close = $('#worktree-close');
  if (close) close.onclick = () => $('#worktree-dialog').close();
  const go = $('#worktree-make');
  if (go) go.onclick = make;
}

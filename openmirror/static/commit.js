/* Committing, from the project you are in.
 *
 * The bar appears only when the session's working root is a git repository
 * with something in it. It is not a git client and does not try to be — no
 * rebase, no stash, no branch switcher. It answers the one question that
 * comes up at the end of every piece of work: what changed, and what do we
 * call it.
 *
 * **The two buttons are the whole feature.** "Write it for me" reads the
 * staged diff, sends it to whichever model this install routes chat to, and
 * puts a draft in the message field. It commits nothing. "Commit" sends the
 * message that is in the field at that moment, which may be the draft, an
 * edit of the draft, or something typed from nothing.
 *
 * That split is deliberate and it is the same one the mail pane makes. A
 * single button with an `ai: true` flag would be one click instead of two
 * and nobody would read the messages after the first week — at which point
 * the model is writing history for a person who has stopped looking, and
 * the commit message is the one artefact of a piece of work that outlives
 * it. Two clicks is the price of it being read.
 */

import { $, el, json, send } from './dom.js';

const state = {
  root: '',
  status: null,
};

/* Re-read on open rather than kept live. A status that refreshes itself
   behind a dialog is a status that changes under the message you are
   writing, and a commit that lands a different set than the one shown is
   worse than a stale one. */
async function refresh() {
  if (!state.root) return;
  const data = await json(`/api/git/status?root=${encodeURIComponent(state.root)}`);
  if (!data) return;
  state.status = data;
  renderBar();
  renderFiles();
}

function changed(data) {
  if (!data) return 0;
  return (data.staged || []).length + (data.unstaged || []).length + (data.untracked || []).length;
}

function renderBar() {
  const bar = $('#commit-slot');
  if (!bar) return;
  const count = changed(state.status);
  const inRepo = state.status && state.status.root;
  bar.hidden = !(inRepo && count > 0);
  if (!bar.hidden) $('#commit-count').textContent = `${count} changed`;
}

function renderFiles() {
  const box = $('#commit-files');
  box.textContent = '';
  if (!state.status) return;
  const groups = [
    ['Staged', state.status.staged],
    ['Changed', state.status.unstaged],
    ['Not tracked', state.status.untracked],
  ];
  for (const [title, list] of groups) {
    if (!list || !list.length) continue;
    const section = el('div', 'commit-group');
    section.appendChild(el('span', 'label', `${title} (${list.length})`));
    for (const entry of list) {
      const row = el('label', 'commit-file');
      const box2 = el('input');
      box2.type = 'checkbox';
      box2.checked = (state.status.staged || []).some((s) => s.path === entry.path);
      box2.onchange = () => toggle(entry.path, box2.checked);
      row.appendChild(box2);
      row.appendChild(el('span', 'path', entry.path));
      if (entry.label && entry.label !== 'modified') row.appendChild(el('span', 'tag', entry.label));
      section.appendChild(row);
    }
    box.appendChild(section);
  }
}

async function toggle(path, wanted) {
  const { data, detail } = await send('/api/git/' + (wanted ? 'stage' : 'unstage'), {
    root: state.root, paths: [path],
  });
  if (data) await refresh();
  else if (detail) $('#commit-note').textContent = detail;
}

async function stageEverything() {
  const { data, detail } = await send('/api/git/stage', { root: state.root, paths: [] });
  if (data) {
    $('#commit-note').textContent = 'everything is staged';
    await refresh();
  } else {
    $('#commit-note').textContent = detail;
  }
}

async function propose() {
  const button = $('#commit-ai');
  button.disabled = true;
  button.textContent = 'writing…';
  $('#commit-note').textContent = '';
  try {
    // Bounded, because a button that says "writing…" for ever is a button
    // with no way out but reloading the page.
    const { data, detail } = await send('/api/git/propose', { root: state.root }, { timeout: 120_000 });
    if (data && data.message) {
      $('#commit-message').value = data.message;
      $('#commit-note').textContent = 'a first draft — read it, change what you want, then commit';
    } else {
      $('#commit-note').textContent = detail;
    }
  } finally {
    button.disabled = false;
    button.textContent = 'Write it for me';
  }
}

async function commit() {
  const message = $('#commit-message').value.trim();
  if (!message) {
    $('#commit-note').textContent = 'a commit needs a message — write one, or let it draft one';
    $('#commit-message').focus();
    return;
  }
  const button = $('#commit-go');
  button.disabled = true;
  $('#commit-note').textContent = '';
  try {
    const { data, detail } = await send('/api/git/commit', { root: state.root, message });
    if (data && data.sha) {
      $('#commit-message').value = '';
      $('#commit-note').textContent = `committed ${data.sha} — ${data.subject}`;
      await refresh();
    } else {
      // git's own wording, which names the fix. "Nothing added to commit"
      // here means nothing is staged, and the button two along says so.
      $('#commit-note').textContent = detail;
    }
  } finally {
    button.disabled = false;
  }
}

async function openDialog() {
  await refresh();
  const data = state.status;
  $('#commit-root').textContent = data ? `${data.root} · ${data.branch || 'detached'}` : '';
  $('#commit-note').textContent = '';
  $('#commit-dialog').showModal();
}

export function wireCommit() {
  const open = $('#commit-open');
  if (open) open.onclick = openDialog;
  const ai = $('#commit-ai');
  if (ai) ai.onclick = propose;
  const go = $('#commit-go');
  if (go) go.onclick = commit;
  const all = $('#commit-stage-all');
  if (all) all.onclick = stageEverything;
}

/* The root comes from the session rather than from a picker: the question
   "commit this" is asked about the project you are already working in, and
   asking which one on top of that is a question with one answer. */
export function setCommitRoot(root) {
  const changedRoot = root && root !== state.root;
  state.root = root || '';
  if (!state.root) {
    const bar = $('#commit-slot');
    if (bar) bar.hidden = true;
    return;
  }
  if (changedRoot) {
    // A new project: the old status describes a different tree, and showing
    // it for even one frame is how you stage the wrong thing.
    state.status = null;
    refresh();
  }
}

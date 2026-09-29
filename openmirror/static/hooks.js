/* Hooks: what this project wants to run, and the answer you give it.
 *
 * Shown rather than buried, because the whole consent story is that a
 * repository's hooks are somebody else's code and the first time one would
 * fire you are told what it would run. A question nobody is asked is a policy
 * nobody agreed to, and a hook that is simply skipped is exactly that.
 *
 * The answer is per *command* and lasts for the session. Per file would be
 * wrong: a project that ships one hook today and a different one tomorrow
 * would inherit yesterday's answer for tomorrow's code.
 *
 * The panel appears on its own, once, when something wants to run — not as a
 * dialog over a turn in progress, because a dialog that covers the work is a
 * dialog that gets dismissed without being read.
 */

import { $, el, json, post } from './dom.js';

const state = { sessionId: null, hooks: [], asked: new Map() };

/* A *function* returning the session id, handed in by `wireHooks` — the same
   shape `wireReview` and `wireTalk` take. Storing the value instead is the
   bug this file was written with: `sessionId` is a function, so the URL came
   out as the function's own source text and every request 404'd. */
const currentSession = () => (state.sessionId ? state.sessionId() : null);

function render() {
  const body = $('#hooks-body');
  body.textContent = '';
  if (!state.hooks.length) {
    body.appendChild(el(
      'p', 'meta',
      'No hooks configured. Add a hooks.json to ~/.openmirror/ for your own, or to a project for that '
      + "project's — a project's are asked about before they run, because they are code you did not write.",
    ));
    return;
  }
  for (const hook of state.hooks) {
    const row = el('div', 'hook-row' + (hook.trusted ? ' trusted' : ' project'));
    const head = el('div', 'hook-head');
    head.appendChild(el('span', 'hook-event', hook.event));
    head.appendChild(el('span', 'hook-name', hook.name));
    if (hook.trusted) {
      head.appendChild(el('span', 'hook-tag', 'yours'));
    } else {
      head.appendChild(el('span', 'hook-tag warn', 'this project'));
    }
    row.appendChild(head);
    row.appendChild(el('code', 'hook-command', hook.command_readable || hook.command));
    if (hook.tools && hook.tools.length) {
      row.appendChild(el('p', 'meta', `Only for: ${hook.tools.join(', ')}`));
    }
    if (hook.pattern) {
      row.appendChild(el('p', 'meta', `Only when the call matches /${hook.pattern}/`));
    }
    if (hook.source) row.appendChild(el('p', 'meta', hook.source));
    body.appendChild(row);
  }
}

async function load() {
  const id = currentSession();
  if (!id) return;
  const data = await json(`/api/sessions/${id}/hooks`);
  if (!data) return;
  state.hooks = data.hooks || [];
  $('#hooks-foot').textContent =
    `Enabled: ${data.enabled ? 'yes' : 'no'} · project hooks ask first: ${data.allow_project ? 'no' : 'yes'}`
    + ` · ${data.timeout}s each`;
  render();
}

async function decide(command, allow) {
  await post(`/api/sessions/${currentSession()}/hooks/agree`, { command, allow });
  state.asked.delete(command);
  await load();
  $('#hooks-banner').hidden = true;
}

/* The one-time question. A strip rather than a dialog: it must not cover the
   turn that is running, and it must not be the only thing on screen either. */
function ask(hook) {
  const banner = $('#hooks-banner');
  banner.textContent = '';
  banner.hidden = false;
  banner.appendChild(el(
    'span', 'hook-ask',
    `This project wants to run a ${hook.event} hook before some tool calls:`,
  ));
  banner.appendChild(el('code', 'hook-command', hook.command));
  banner.appendChild(el('span', 'meta', 'It can only stop a call, never allow one.'));

  const no = el('button', 'ghost small', 'Not this time');
  no.onclick = () => decide(hook.command, false);
  const yes = el('button', 'primary small', 'Run it');
  yes.onclick = () => decide(hook.command, true);
  banner.appendChild(no);
  banner.appendChild(yes);
}

export function wireHooks({ sessionId }) {
  if (sessionId) state.sessionId = sessionId;

  const settings = $('#hooks-open');
  if (settings) {
    settings.onclick = () => {
      $('#hooks-dialog').showModal();
      load();
    };
  }
  const close = $('#hooks-close');
  if (close) close.onclick = () => $('#hooks-dialog').close();
}

export function onHookApproval(hook) {
  if (!hook || !hook.command) return;
  if (state.asked.has(hook.command)) return;
  state.asked.set(hook.command, hook);
  ask(hook);
}

export function resetHooks() {
  state.asked.clear();
  $('#hooks-banner').hidden = true;
}

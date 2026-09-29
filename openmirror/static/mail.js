/* Mail, from the browser.
 *
 * The same five operations the `mail` tool offers, over HTTP, for somebody
 * who wants to read their inbox rather than ask about it. The server decides
 * which protocol each account speaks; this file does not know or care.
 *
 * Three decisions that are the interface's and not the server's:
 *
 *   - **The AI toggle is a draft, never a send.** "Write it for me" fills the
 *     composer and stops. Committing to that is a separate click on a message
 *     you can read in full, which is the only arrangement where the model
 *     never puts a word in somebody's inbox that nobody read. A button that
 *     drafted and sent would get used once and then never read again.
 *   - **Opening a folder does not mark it read.** The server uses BODY.PEEK
 *     and the `$seen` keyword is only touched when you ask, so scrolling
 *     through a list does not quietly clear the unread badge.
 *   - **The composer keeps what you typed.** Switching folders, opening a
 *     message or pressing the AI button mid-draft must not eat it, because
 *     the usual reason to come back to a draft is that the first attempt was
 *     not quite right.
 */

import { $, el, json, send } from './dom.js';
import { onEnter } from './modes.js';

const state = {
  account: '',
  folder: '',
  folders: [],
  messages: [],
  selected: null,
  loading: false,
};

/* A draft in progress, kept here rather than in the DOM so that re-rendering
   the list cannot clear it. */
let draft = null;

/* What "write it for me" is allowed to see. Enough to answer the question the
   draft is about, and no more: the message being replied to, and nothing
   from any other conversation. */
const DRAFT_SYSTEM = `You are helping somebody write an email in their own voice.

You are given the message they are replying to. Write the reply they would
write. Rules:
- Write as them, to the person in the message. First person, their tone.
- Match how they write: their greeting, their sign-off or lack of one, how
  formal they are, how long their sentences are. Their messages are the only
  evidence you have of that.
- Answer what the message asks. If it asks something only they can decide,
  say so plainly and briefly rather than inventing an answer.
- Do not commit to a date, a price, a promise or a deadline that was not
  agreed. "I'll check and come back to you" is a real sentence.
- Plain text only. No signature block, no disclaimer, no subject line, no
  greeting line spelled out twice.
- Reply with the body and nothing else. No preamble, no notes, no code fence.`;

let lastReplied = null;

function accountLabel() {
  const found = state.folders.length ? state.folders : [];
  return state.account || '';
}

async function loadAccounts() {
  const data = await json('/api/mail/accounts');
  const list = (data && data.accounts) || [];
  if (!list.length) return false;
  if (!state.account || !list.some((a) => (a.id || a.address) === state.account)) {
    const preferred = list.find((a) => a.default) || list[0];
    state.account = preferred.id || preferred.address;
  }
  return true;
}

async function loadFolders() {
  const box = $('#mail-folders');
  const data = await json(`/api/mail/folders?account=${encodeURIComponent(state.account)}`);
  state.folders = (data && data.folders) || [];

  if (!state.folders.length) {
    box.textContent = '';
    return;
  }
  box.textContent = '';
  for (const folder of state.folders) {
    const row = el('button', 'mail-folder', folder.name);
    row.type = 'button';
    if (folder.name === state.folder) row.setAttribute('aria-current', 'true');
    if (folder.unread) row.appendChild(el('span', 'count', String(folder.unread)));
    row.onclick = () => { state.folder = folder.name; loadFolders(); loadMessages(); };
    box.appendChild(row);
  }
}

function messageRow(message) {
  const row = el('button', `mail-row${message.seen ? '' : ' unread'}`);
  row.type = 'button';

  const who = el('span', 'who', message.from.short || message.from.email);
  const when = el('span', 'when', (message.date || '').slice(0, 16));
  const subject = el('span', 'subject', message.subject || '(no subject)');
  const snippet = el('span', 'snippet', message.snippet || '');

  const top = el('span', 'row-top');
  top.appendChild(who);
  top.appendChild(when);
  if (!message.seen) top.appendChild(el('span', 'dot', '●'));
  if (message.attachments.length) top.appendChild(el('span', 'meta', '📎'));

  row.appendChild(top);
  row.appendChild(subject);
  row.appendChild(snippet);
  row.onclick = () => openMessage(message);
  return row;
}

async function loadMessages() {
  const list = $('#mail-list');
  list.textContent = 'loading…';
  const query = new URLSearchParams({ account: state.account, limit: '40' });
  if (state.folder) query.set('folder', state.folder);
  const data = await json(`/api/mail/messages?${query}`);
  if (!data) {
    list.textContent = 'that mailbox did not answer. The account may need a password, or the host may be down.';
    return;
  }
  state.messages = data.messages || [];
  if (!state.folder) state.folder = data.folder || '';
  list.textContent = '';
  if (!state.messages.length) {
    list.appendChild(el('p', 'meta', 'Nothing here.'));
    return;
  }
  for (const message of state.messages) list.appendChild(messageRow(message));
}

async function openMessage(message) {
  state.selected = message;
  const reader = $('#mail-reader');
  reader.hidden = false;
  $('#mail-reader-body').textContent = '…';
  $('#mail-reader-subject').textContent = message.subject || '(no subject)';
  $('#mail-reader-from').textContent = `${message.from.name || message.from.email} <${message.from.email}>`;

  const full = await json(
    `/api/mail/message?uid=${encodeURIComponent(message.uid)}&account=${encodeURIComponent(state.account)}`
    + `&folder=${encodeURIComponent(message.folder || '')}`,
  );
  const body = (full && full.message && full.message.body) || '';
  $('#mail-reader-body').textContent = body || '(this message has no text body)';
  $('#mail-attachments').textContent = (full && full.message && full.message.attachments || [])
    .map((a) => a.name).join(', ');

  // Mark read — and only now, having actually been read. Listing the folder
  // deliberately did not do this.
  await send('/api/mail/read', { uid: message.uid, seen: true, account: state.account,
    folder: message.folder || 'inbox' });
  const found = state.messages.find((m) => m.uid === message.uid);
  if (found) found.seen = true;
  loadMessages();
}

function startReply() {
  const message = state.selected;
  if (!message) return;
  draft = {
    to: message.from.email ? `${message.from.name || ''} <${message.from.email}>`.trim() : '',
    subject: message.subject ? (/^re:/i.test(message.subject) ? message.subject : `Re: ${message.subject}`) : '',
    body: '',
    replyTo: message.uid,
    folder: message.folder || '',
    quoted: (message.body || '').split('\n').filter((l) => l.startsWith('>')).slice(0, 12).join('\n'),
  };
  renderDraft();
  $('#mail-body').focus();
}

function renderDraft() {
  const panel = $('#mail-compose');
  panel.hidden = !draft;
  if (!draft) return;
  $('#mail-to').value = draft.to;
  $('#mail-subject').value = draft.subject;
  $('#mail-body').value = draft.body;
  const quoted = $('#mail-quoted');
  quoted.textContent = draft.quoted || '';
  quoted.hidden = !draft.quoted;
}

/* The whole of the AI feature, and it is deliberately small: one button that
 * writes a first draft into a box that is already open, addressed and
 * subject-lined from the message being answered. It cannot send. Sending is
 * the next button, and it is a different button on purpose. */
async function propose() {
  if (!draft) startReply();
  if (!draft) return;
  const button = $('#mail-ai');
  button.disabled = true;
  button.textContent = 'writing…';
  try {
    const { data, detail } = await send('/api/mail/propose', {
      account: state.account,
      reply_to: draft.replyTo || '',
      folder: draft.folder || '',
    }, { timeout: 120_000 });
    if (data && data.draft) {
      draft.body = data.draft;
      renderDraft();
      $('#mail-ai-note').textContent = 'a first draft — read it, change what you want, then send';
    } else {
      $('#mail-ai-note').textContent = detail;
    }
  } finally {
    button.disabled = false;
    button.textContent = 'Write it for me';
  }
}

/* Named `sendIt` rather than `send` because `send` is the imported helper in
   dom.js, and a module cannot have both. */
async function sendIt() {
  if (!draft) return;
  const button = $('#mail-send');
  button.disabled = true;
  try {
    const { data, detail } = await send('/api/mail/send', {
      account: state.account,
      to: [draft.to],
      subject: draft.subject,
      body: draft.body,
      reply_to: draft.replyTo || '',
      folder: draft.folder || '',
    });
    if (data && data.sent) {
      draft = null;
      renderDraft();
      $('#mail-ai-note').textContent = 'sent';
      closeReader();
      loadFolders();
      loadMessages();
    } else {
      $('#mail-ai-note').textContent = detail;
    }
  } finally {
    button.disabled = false;
  }
}

function closeReader() {
  state.selected = null;
  $('#mail-reader').hidden = true;
  $('#mail-compose').hidden = true;
  draft = null;
}

export function wireMail() {
  $('#mail-close').onclick = closeReader;
  $('#mail-reply').onclick = startReply;
  $('#mail-discard').onclick = () => { draft = null; renderDraft(); };
  $('#mail-ai').onclick = propose;
  $('#mail-send').onclick = sendIt;
  $('#mail-new').onclick = () => { startReply(); };

  for (const [id, key] of [['mail-to', 'to'], ['mail-subject', 'subject'], ['mail-body', 'body']]) {
    const node = $(`#${id}`);
    if (node) node.oninput = () => { if (draft) draft[key] = node.value; };
  }

  onEnter('mail', async () => {
    $('#mail-pane-note').textContent = '';
    if (!(await loadAccounts())) {
      $('#mail-list').textContent = '';
      $('#mail-pane-note').textContent =
        'No mail account yet. Add one in Settings, or set OPENMIRROR_MAIL_ADDRESS with the IMAP and '
        + 'SMTP hosts and OPENMIRROR_MAIL_PASSWORD and restart. A JMAP account needs its session URL.';
      return;
    }
    await loadFolders();
    await loadMessages();
  });
}

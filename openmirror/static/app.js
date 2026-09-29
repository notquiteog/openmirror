/* openmirror's browser client: the shell, and the mode you type in.
 *
 * No framework and no build step: this is served from the machine it runs on,
 * so a toolchain would be a dependency with nothing to show for it.
 *
 * This file is the session list, the agent socket and the transcript — the
 * Code mode, which is the one everything else is measured against. The other
 * five modes are their own files and know nothing about this one; they share
 * the daemon, the companion and a handful of DOM helpers, and nothing else.
 * That is why adding Talk did not touch the transcript and adding the studio
 * did not touch the socket.
 *
 * The agent socket is attached to rather than owned — closing this page
 * pauses your view of a session, it does not stop it — so on connect we send
 * the last sequence number we saw and the server replays the gap.
 */

import { companion, caption } from './companions/index.js';
import { openConnections, wireConnections } from './connections.js';
import { handleFileKey, resetFiles, wireFiles } from './files.js';
import { setCommitRoot, wireCommit } from './commit.js';
import { $, api, el, icon, json, onReachable, socket, took } from './dom.js';
import { endLive, isLive, wireLive } from './live.js';
import { wireMail } from './mail.js';
import { go, mode, onEnter, onLeave, wireModes } from './modes.js';
import { focusSearch, wireSearch } from './search.js';
import { cancel as cancelStream, flush as flushStream, queue } from './stream.js';
import { openStudio, stopPolling, wireStudio } from './studio.js';
import { startTalking, stopTalking, talking, wireTalk } from './talk.js';
import { renderContext, refreshContext, wireContext } from './context.js';
import { renderUpdate, watchUpdates, wireUpdates } from './updates.js';
import { Voice } from './voice.js';
import { narrate, refreshRuns, wireWatch } from './watch.js';

function ago(seconds) {
  if (seconds < 60) return 'now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const state = {
  sessionId: null,
  seq: 0,
  agent: null,
  voice: null,
  busy: false,
  turnNode: null,
  tools: new Map(),
  // Commands typed while the socket was down, replayed on reconnect.
  outbox: [],
  // Consecutive failed reconnects, for the backoff.
  retries: 0,
  // The locally echoed user bubble, removed when the server confirms the turn.
  echo: null,
  // What the open session is, for the chips under the composer.
  info: null,
  // When the running turn started, so the transcript can say how long it took.
  turnStart: 0,
  // Files this turn has written, and by how much, from the diffs the server
  // already sends. Emptied at the start of every turn.
  turnFiles: new Map(),
  // The same counted across the whole session, for the bar.
  added: 0,
  removed: 0,
  // The latest to-do list the session's own agent wrote.
  todos: [],
  // Background work, by task id, as the server last described it.
  tasks: new Map(),
  // What `/` can be followed by in the open session, for the menu.
  commands: [],
};

/* ---------------------------------------------------------------- transcript */

const transcript = $('#transcript');

/* Whether the reader is at the tail, and the scroll that follows from it.
 *
 * Both were a synchronous layout read on every append, and that is the one
 * thing measured to be expensive here. Measured in a real browser at 880
 * nodes, streaming text alone holds a 16.7ms frame and so does appending one
 * card — but doing both, with a layout read and a scroll write alongside each
 * append, sits at 33.3ms, because a card append invalidates layout and the
 * next read forces it back. Two vsyncs for one frame's work, every frame, for
 * the whole of a turn.
 *
 * The fix is to do it once per frame instead of once per append: the decision
 * is memoised against a frame stamp, and the scroll is written once on the
 * next animation frame. Four appends in one frame then cost one layout.
 *
 * The semantics are unchanged — the same question, the same answer, asked
 * less often. What changes is that the answer is allowed to be up to one
 * frame stale, which is invisible: the thing it gates is whether a scroll
 * follows, and a scroll that follows one frame later is the same scroll.
 */
let followWanted = false;
let followScheduled = false;
let atBottomFrame = -1;
let atBottomValue = true;

function atBottom() {
  const stamp = frameStamp();
  if (stamp === atBottomFrame) return atBottomValue;
  const value = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  atBottomFrame = stamp;
  atBottomValue = value;
  return value;
}

/* A counter that advances once per animation frame.

   Not `performance.now()`, whose resolution is deliberately coarse: a
   millisecond timer cannot tell two layout reads in the same frame from two
   in different ones, which is the whole question here. */
let frameNo = 0;
let frameScheduled = false;
function frameStamp() {
  if (!frameScheduled) {
    frameScheduled = true;
    requestAnimationFrame(() => {
      frameScheduled = false;
      frameNo++;
    });
  }
  return frameNo;
}

/* Keep the tail in view, once, on the next frame.

   The write is `scrollHeight` because that is what clamps to the bottom, and
   a read of it is the cost being coalesced rather than removed — one per
   frame instead of one per append. */
function scrollToTail() {
  followWanted = true;
  if (followScheduled) return;
  followScheduled = true;
  requestAnimationFrame(() => {
    followScheduled = false;
    if (!followWanted) return;
    followWanted = false;
    transcript.scrollTop = transcript.scrollHeight;
  });
}

function append(node) {
  // Only follow the tail if the reader was already at it. Yanking someone back
  // down while they are reading earlier output is the most irritating thing a
  // streaming log can do.
  const follow = atBottom();
  transcript.appendChild(node);
  if (follow) scrollToTail();
  prune();
  settle();
  return node;
}

/* An empty session is a different window: the companion in the middle, the
   question above the box, and a few things you might ask. The moment there is
   anything to read, all of that goes and the transcript takes the room. */
function settle() {
  const empty = !transcript.querySelector('.turn, .tool, .question, .notice, .changed, .worked, .compacted');
  document.body.classList.toggle('blank', empty);
  $('#hero').hidden = !empty;
  $('#starters').hidden = !empty;
}

/* A transcript that has been running all afternoon is a few thousand nodes,
   every one of them laid out on every append and every scroll. Past a certain
   size that stops being a transcript and starts being the reason the page
   feels heavy — and the nodes that go are the ones you will not scroll back
   to, because you are looking at what just happened.

   The oldest completed turns go first, and only ever as a whole turn. Half a
   turn is worse than a full one: a tool card whose output has been dropped but
   whose header and diff are still on screen is a broken-looking card, and the
   receipt at the end of a turn with the turn gone is a receipt for nothing.

   A turn is kept as a unit, the current turn is never touched, and nothing is
   removed while the reader is up in the part that would go — pruning out from
   under a scroll position is what makes a long log feel like it is fighting
   you. The threshold is deliberately generous: this is a safety valve for
   pathological sessions, not an optimisation, and a transcript that fits in
   memory should stay whole.

   Kept as a count of nodes rather than a height, because height changes with
   the window and with how a given turn happens to be sized, and either would
   make the same session prune differently on two machines. */
const KEEP_NODES = 900;

function prune() {
  const kids = [...transcript.children];
  if (kids.length <= KEEP_NODES) return;

  /* Never while the reader is away from the tail — not because removing things
     above them is wrong in itself, but because the content under the cursor
     would shift by an amount they did not cause and could not predict. */
  if (!atBottom()) return;

  let spare = kids.length - KEEP_NODES;
  for (const node of kids) {
    if (spare <= 0) break;
    if (node.id === 'hero') continue;
    /* The turn in progress is the one thing on screen by definition. */
    if (node === state.turnNode || (state.turnNode && node.contains(state.turnNode))) continue;
    /* Walk forward in whole turns: everything from one `.turn` up to the next
       is part of the same answer, and stopping halfway through one leaves
       orphans. `tail` is where the current turn starts. */
    if (node.classList.contains('turn') && !node.classList.contains('user')) {
      const at = kids.indexOf(node);
      const end = kids.findIndex((later, i) => i > at && later.classList.contains('turn'));
      const stop = end === -1 ? kids.length : end;
      for (let i = at; i < stop && spare > 0; i++) {
        cancelStream(kids[i]);
        kids[i].remove();
        spare--;
      }
      continue;
    }
    /* Not a turn of its own: a tool card, a notice, a receipt. They belong to
       the turn above them and go only once that turn has, so they are left
       alone here — an orphan receipt is worse than a long transcript, and the
       walk above will have taken the turn that owns them. */
  }
  settle();
}

function clearTranscript() {
  /* Everything queued belongs to nodes that are about to be detached, and some
     of it belongs to the session being left. Dropped rather than flushed:
     flushing would write one last frame of a session you have already
     navigated away from, straight into detached nodes. */
  for (const node of [...transcript.children]) {
    cancelStream(node);
    if (node.id !== 'hero') node.remove();
  }
  for (const card of state.tools.values()) cancelStream(card);
  state.tools.clear();
  /* The sidebar is not part of the transcript, so tearing one down does not
     tear the other down — but the fingerprint has to go with the session it
     described, or the next `loadSessions` would see an unchanged print and
     leave the list stale. */
  lastPrint = '';
  settle();
}

/* The greeting names the folder the agent is actually pointed at, because
   that is the one fact about a new session worth checking before you type. */
function dressHero() {
  const info = state.info;
  const where = info && info.root ? info.root.replace(/\/+$/, '').split('/').pop() : '';
  $('#hero-ask').textContent = where ? `What should we do in ${where}?` : 'What should we do?';
  $('#hero-sub').textContent = info ? [info.model, info.policy].filter(Boolean).join('  ·  ') : '';
}

const STARTERS = [
  'Have a look around this folder and tell me what it is',
  'Find the tests and run them',
  'Explain what the most recent change does',
  'Look something up on the web for me',
];

function buildStarters() {
  const box = $('#starters');
  box.textContent = '';
  for (const text of STARTERS) {
    const row = el('button', 'starter');
    row.type = 'button';
    row.appendChild(icon('chat'));
    row.appendChild(el('span', '', text));
    // Filled in rather than sent. A suggestion you cannot edit before it runs
    // is a button that does something to your machine on one click.
    row.onclick = () => {
      const input = $('#input');
      input.value = text;
      input.focus();
      input.dispatchEvent(new Event('input'));
    };
    box.appendChild(row);
  }
}

function notice(text, kind = '') {
  // The same message twice in a row is collapsed into a count. Even with the
  // retry loop fixed, anything that can repeat should not be able to bury the
  // transcript under copies of itself.
  const last = transcript.lastElementChild;
  if (last && last.classList.contains('notice') && last.dataset.text === text) {
    const n = Number(last.dataset.count || 1) + 1;
    last.dataset.count = String(n);
    last.textContent = `${text}  (×${n})`;
    return last;
  }
  const node = el('div', `notice ${kind}`, text);
  node.dataset.text = text;
  return append(node);
}

function userTurn(text) {
  const wrap = el('div', 'turn user');
  wrap.appendChild(el('div', 'body', text));
  return append(wrap);
}

function assistantTurn() {
  const wrap = el('div', 'turn assistant');
  wrap.appendChild(el('div', 'body'));
  state.turnNode = wrap;
  return append(wrap);
}

function appendText(text) {
  if (!state.turnNode) assistantTurn();
  const body = state.turnNode.querySelector('.body');
  /* Coalesced to the next frame rather than written per token. A burst of
     deltas in one frame becomes one write, and the scroll is a single pass at
     the end of it instead of a layout read followed by a layout write for
     every token. Nothing is dropped: the text is accumulated, not sampled. */
  queue(body, text, (node, chunk) => {
    node.textContent += chunk;
    // Coalesced like everything else here: a burst of deltas in one frame is
    // one read and one scroll, not one each per token.
    if (atBottom()) scrollToTail();
  });
}

function appendThinking(text) {
  if (!state.turnNode) assistantTurn();
  let box = state.turnNode.querySelector('.thinking');
  if (!box) {
    box = el('details', 'thinking');
    box.appendChild(el('summary', '', 'reasoning'));
    box.appendChild(el('span'));
    state.turnNode.insertBefore(box, state.turnNode.firstChild);
  }
  queue(box.querySelector('span'), text, (node, chunk) => {
    node.textContent += chunk;
  });
}

function renderScreenshot(display) {
  const box = el('div', 'shot');
  const img = document.createElement('img');
  img.src = `data:${display.media_type || 'image/png'};base64,${display.image}`;
  img.alt = 'what the agent saw';
  img.loading = 'lazy';
  box.appendChild(img);

  // Click through to the full-size frame: the thumbnail is for scanning the
  // transcript, and a 2560px screenshot scaled into a card is unreadable.
  img.onclick = () => window.open(img.src, '_blank');

  if (display.width) {
    box.dataset.w = display.width;
    box.dataset.h = display.height;
  }

  const meta = [];
  if (display.width) meta.push(`${display.width}x${display.height}`);
  if (display.url) meta.push(display.url);
  if (meta.length) box.appendChild(el('div', 'shot-meta', meta.join('  ·  ')));
  return box;
}

function markClick(card, x, y, label) {
  /* Draw the click point onto the screenshot above it.
   *
   * The screenshot was taken before the click, so the marker goes on the
   * previous card rather than this one — which is exactly what someone
   * reviewing the run wants to see: the frame, and where it was about to go. */
  const shots = transcript.querySelectorAll('.tool .shot img');
  const last = shots[shots.length - 1];
  if (!last) return;

  const wrap = last.parentElement;
  wrap.style.position = 'relative';
  const dot = el('div', 'click-marker');
  // Percentages, so the marker stays put when the image is scaled to the card.
  const w = Number(wrap.dataset.w || last.naturalWidth || 0);
  const h = Number(wrap.dataset.h || last.naturalHeight || 0);
  if (!w || !h) return;
  dot.style.left = `${(x / w) * 100}%`;
  dot.style.top = `${(y / h) * 100}%`;
  dot.title = label || `${x}, ${y}`;
  wrap.appendChild(dot);
}

function renderDiff(text) {
  const box = el('div', 'diff');
  for (const line of text.split('\n')) {
    let cls = '';
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'del';
    else if (line.startsWith('@@')) cls = 'hunk';
    const span = el('span', cls, line + '\n');
    box.appendChild(span);
  }
  return box;
}

/* --------------------------------------------------------------- tool cards */

/* What the agent did, said as a person would say it. The tool name is exact
   and useless at a glance; "Ran" followed by the command is what someone
   skimming a transcript is actually reading for. Anything not listed falls
   back to its own name in monospace, which is honest about being a tool. */
const VERBS = {
  read_file: 'Read',
  read_files: 'Read',
  outline: 'Sketched',
  list_dir: 'Listed',
  glob: 'Found',
  grep: 'Searched',
  recall: 'Recalled',
  write_file: 'Wrote',
  edit_file: 'Edited',
  multi_edit: 'Edited',
  apply_patch: 'Patched',
  notebook_edit: 'Edited',
  todo: 'Updated the to-dos',
  propose_plan: 'Proposed a plan',
  agent: 'Delegated',
  skill: 'Used the skill',
  lsp: 'Asked the language server',
  tasks: 'Background',
  remember: 'Remembered',
  shell: 'Ran',
  web_search: 'Searched the web for',
  web_fetch: 'Fetched',
  research: 'Looked into',
  mcp_list_resources: 'Listed what the servers have',
  mcp_read_resource: 'Read',
  browser_navigate: 'Opened',
  browser_read: 'Read the page',
  browser_click: 'Clicked',
  browser_type: 'Typed into',
  browser_screenshot: 'Looked at',
  browser_hand_over: 'Handed you',
  desktop_screenshot: 'Looked at the screen',
  desktop_click: 'Clicked',
  desktop_type: 'Typed',
  desktop_key: 'Pressed',
  desktop_scroll: 'Scrolled',
  system_info: 'Checked the machine',
  package_search: 'Looked for',
  package_install: 'Installed',
  package_remove: 'Removed',
  display_info: 'Checked the displays',
  display_hdr: 'Changed HDR for',
  media_params: 'Checked what it can tune',
  generate_image: 'Generated',
  generate_video: 'Started rendering',
  media_job: 'Checked on the render',
  import_workflow: 'Installed the workflow',
  git: 'Checked the repository for',
  mail: 'Mail',
  calendar: 'Checked the calendar for',
  ask_user: 'Asked you',
};

/* A finished, unremarkable call is one grey line; a card is for what wants
   looking at. This decides which, and it is called again when the call ends,
   because "unremarkable" is not knowable until then. */
function setCard(card, on) {
  card.classList.toggle('card', on);
  card.classList.toggle('open', on);
}

/* `host` is where the card goes: the transcript, or — for a subagent's call —
   the log inside the card of the call that started it. */
function toolCard(call, needsApproval, host = null) {
  const card = el('div', 'tool');
  const head = el('div', 'head');

  const verb = VERBS[call.name];
  if (verb) head.appendChild(el('span', 'verb', verb));
  else head.appendChild(el('span', 'name', call.name));
  head.appendChild(el('span', 'summary', call.summary || ''));
  head.appendChild(el('span', 'took'));
  head.appendChild(el('span', `risk ${call.risk}`, call.risk));
  head.appendChild(el('span', 'chev', '›'));

  // The whole head is the disclosure control: a chevron you have to hit
  // exactly is a worse target than the line it sits on.
  head.onclick = () => {
    const open = card.classList.toggle('open');
    if (open) card.classList.add('card');
    else card.classList.toggle('card', card.classList.contains('failed') || Boolean(card.querySelector('.decide')));
  };
  card.appendChild(head);
  if (host) host.appendChild(card);
  else append(card);
  state.tools.set(call.id, card);

  if (needsApproval) {
    setCard(card, true);
    const bar = el('div', 'decide');

    // The companion leans in where the decision is. It is the same creature
    // in the same state as the one on the perch — it just sits next to the
    // thing it is stuck on, so the reason it stopped is where you are looking.
    const who = el('span', 'who');
    bar.appendChild(who);
    const pet = companion.attach(who, { scale: 2 });

    bar.appendChild(el('span', 'why', 'Waiting for you.'));

    const remember = el('label');
    const box = el('input');
    box.type = 'checkbox';
    remember.appendChild(box);
    remember.appendChild(el('span', '', "don't ask for this exact call again"));
    // The server refuses to remember a destructive call. Say so here rather
    // than offering a checkbox that silently does nothing.
    if (call.risk === 'destructive') {
      box.disabled = true;
      remember.lastChild.textContent = 'destructive calls are always confirmed';
    }
    bar.appendChild(remember);

    const deny = el('button', 'ghost small', 'Deny');
    const allow = el('button', 'primary small', 'Allow');
    const decided = () => {
      pet.remove();
      bar.remove();
    };
    deny.onclick = () => {
      send({ type: 'tool.deny', call_id: call.id, reason: 'declined in the UI' });
      decided();
      companion.set('thinking');
    };
    allow.onclick = () => {
      send({ type: 'tool.approve', call_id: call.id, remember: box.checked });
      decided();
      // Approval is the only signal that this call is now running: the server
      // sends no separate "started" for a call it was already told about.
      companion.tool(call.name);
    };
    bar.appendChild(deny);
    bar.appendChild(allow);
    card.appendChild(bar);
    allow.focus();
  }
  return card;
}

function toolOutput(callId, text, stream) {
  const card = state.tools.get(callId);
  if (!card) return;
  let out = card.querySelector('.out.live');
  if (!out) {
    out = el('div', 'out live');
    card.appendChild(out);
    // Output arriving now is worth watching now — a build scrolling past is
    // the one thing in here nobody wants folded away.
    setCard(card, true);
  }
  if (stream === 'stderr') out.classList.add('err');
  /* Trimmed inside the queued write rather than on the way in. Measuring
     `out.textContent.length` per token is itself a read of the whole log, and
     a build emitting 40k lines would be doing that 40k times. The cap is the
     same, and it still keeps the tail. */
  queue(out, text, (node, chunk) => {
    node.textContent += chunk;
    if (node.textContent.length > 40000) node.textContent = node.textContent.slice(-40000);
    node.scrollTop = node.scrollHeight;
  });
}

/* How much of a file changed, counted off the unified diff the server already
   sends. It is the same number `git diff --stat` would print, and it is free:
   the diff is on screen either way. */
function countDiff(text) {
  let add = 0;
  let del = 0;
  for (const line of text.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) add++;
    else if (line.startsWith('-') && !line.startsWith('---')) del++;
  }
  return { add, del };
}

function noteChange(path, diff) {
  const counts = countDiff(diff);
  const at = state.turnFiles.get(path) || { add: 0, del: 0 };
  at.add += counts.add;
  at.del += counts.del;
  state.turnFiles.set(path, at);
  state.added += counts.add;
  state.removed += counts.del;
  showDiffstat();
}

function showDiffstat() {
  const box = $('#diffstat');
  box.textContent = '';
  if (!state.added && !state.removed) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.appendChild(el('span', 'add', `+${state.added}`));
  box.appendChild(document.createTextNode(' '));
  box.appendChild(el('span', 'del', `-${state.removed}`));
  box.title = 'what the agent has changed in this session';
}

function toolDone(result) {
  const card = state.tools.get(result.id);
  if (!card) return;
  /* This card stops being live output and becomes a finished record: the
     output element loses `.live`, the card folds, and the transcript is
     measured to decide. Any text still queued for it has to land first, or
     the last lines of a build would be missing from the card that keeps it. */
  flushStream();
  if (!result.ok) card.classList.add('failed');
  // A finished call must not still be offering Allow and Deny. This shows up
  // on replay, where the proposal is re-rendered long after it was decided.
  card.querySelector('.decide')?.remove();

  const live = card.querySelector('.out.live');
  if (live) live.classList.remove('live');

  // What the agent saw. This is the whole of "watch it work": every frame it
  // acted on, in order, with the point it clicked marked on the frame before
  // the click. A picture of the screen is worth far more here than the
  // sentence the model wrote about it.
  const shot = result.display && result.display.image;
  if (shot) {
    card.appendChild(renderScreenshot(result.display));
  }

  const diff = result.display && result.display.diff;
  const items = result.name === 'todo' && result.ok && result.display ? result.display.items : null;
  if (items) {
    // The list itself, not the text the model was sent: a checklist reads at
    // a glance, and "[x] [~] [ ]" is notation.
    card.appendChild(fillTodos(el('ol', 'out todos'), items));
  } else if (diff) {
    card.appendChild(renderDiff(diff));
    if (result.ok && result.display.path) noteChange(result.display.path, diff);
  } else if (!live && !shot && result.content) {
    const out = el('div', result.ok ? 'out' : 'out err');
    out.textContent = result.content.length > 4000 ? result.content.slice(0, 4000) + '\n…' : result.content;
    card.appendChild(out);
  }

  if (result.name === 'desktop_click' && result.display && result.ok) {
    markClick(card, result.display.x, result.display.y, result.display.label);
  }

  // A delegation folds to one line saying how much work was inside it; the
  // agent's own steps and its report are under the chevron.
  if (result.name === 'agent' && result.display && result.display.tool_calls !== undefined) {
    const n = result.display.tool_calls;
    card.querySelector('.summary').textContent += `  ·  ${n} tool call${n === 1 ? '' : 's'}`;
  }

  // Anything that failed, changed a file, or has a picture in it stays a card
  // and stays open. A successful read collapses to its line: it is in the
  // transcript so you can check, not so you have to.
  setCard(card, !result.ok || Boolean(shot || diff));

  const when = card.querySelector('.took');
  if (when && result.duration_ms > 400) when.textContent = took(result.duration_ms);
}

/* ------------------------------------------------------------ what changed */

/* The receipt at the end of a turn: which files it touched, by how much, and
   one button that puts them back. The list is built from the diffs that came
   past during the turn, so it says exactly what this client watched happen. */
function changedCard() {
  if (!state.turnFiles.size) return;
  const files = [...state.turnFiles.entries()];
  state.turnFiles = new Map();

  const box = el('div', 'changed');
  const top = el('div', 'top');
  top.appendChild(el('span', 'what', `${files.length} file${files.length === 1 ? '' : 's'} changed`));

  const undo = el('button', 'ghost small undo');
  undo.appendChild(icon('undo'));
  undo.appendChild(el('span', '', 'Undo'));
  undo.onclick = () => undoLast(undo);
  top.appendChild(undo);
  box.appendChild(top);

  for (const [path, counts] of files) {
    const row = el('div', 'file');
    const short = path.replace(/^.*\/([^/]+\/[^/]+)$/, '$1');
    const name = el('span', 'path', short);
    name.title = path;
    row.appendChild(name);
    if (counts.add) row.appendChild(el('span', 'add', `+${counts.add}`));
    if (counts.del) row.appendChild(el('span', 'del', `-${counts.del}`));
    box.appendChild(row);
  }
  append(box);
}

/* Undo means the newest checkpoint, which is the one this turn made. Anything
   more selective than that is what the Rewind dialog is for — and rewinding
   to the middle of a session is exactly what the checkpoint code refuses to
   do, because it would leave a state that never existed. */
async function undoLast(button) {
  const res = await api(`/api/sessions/${state.sessionId}/checkpoints`);
  if (!res || !res.ok) return;
  const { enabled, checkpoints } = await res.json();
  if (!enabled || !checkpoints.length) {
    notice('There is no checkpoint to undo to — checkpoints are off for this session.', 'error');
    return;
  }
  const cp = checkpoints[0];
  if (!confirm(`Put files back to before "${cp.label}"?\n\nThis undoes that turn and every one after it.`)) return;
  button.disabled = true;
  await restore(cp.id);
  button.disabled = false;
}

function questionCard(ev, host = null) {
  const card = el('div', 'question');
  card.appendChild(el('div', 'q', ev.question));

  const answer = (text) => {
    send({ type: 'question.answer', question_id: ev.question_id, answer: text });
    companion.set('thinking');
    card.querySelector('.opts')?.remove();
    card.querySelector('form')?.remove();
    card.appendChild(el('div', 'notice', `You answered: ${text}`));
  };

  if (ev.options && ev.options.length) {
    const opts = el('div', 'opts');
    for (const option of ev.options) {
      const button = el('button', 'ghost small', option);
      button.onclick = () => answer(option);
      opts.appendChild(button);
    }
    card.appendChild(opts);
  }

  const form = el('form');
  const input = el('input');
  input.placeholder = 'or write your own answer';
  const go = el('button', 'primary small', 'Answer');
  form.onsubmit = (e) => {
    e.preventDefault();
    if (input.value.trim()) answer(input.value.trim());
  };
  form.appendChild(input);
  form.appendChild(go);
  card.appendChild(form);
  if (host) host.appendChild(card);
  else append(card);
  input.focus();
}

function denied(ev) {
  const card = state.tools.get(ev.call_id);
  if (!card) return;
  card.classList.add('denied');
  card.querySelector('.decide')?.remove();
  card.appendChild(el('div', 'out', `Not run: ${ev.reason}`));
}

/* ------------------------------------------------------------------ agents */

/* A subagent's events arrive on the same socket as everything else, marked
   with the call that started it, and they nest inside that call's card. The
   delegation is one line in the transcript; what the agent did is inside it
   for anyone who wants to look — and it opens by itself when the agent needs
   you, because a question inside a folded card is a question nobody sees. */
function agentLog(callId) {
  const card = state.tools.get(callId);
  if (!card) return null;
  let log = card.querySelector(':scope > .agent-log');
  if (!log) {
    log = el('div', 'agent-log');
    // Above the report, which arrives last and belongs at the bottom.
    card.insertBefore(log, card.querySelector(':scope > .out'));
  }
  return log;
}

function childEvent(ev) {
  const log = agentLog(ev.agent);
  if (!log) return;
  const card = state.tools.get(ev.agent);

  switch (ev.type) {
    case 'text.delta': {
      let said = log.lastElementChild;
      if (!said || !said.classList.contains('said')) said = log.appendChild(el('div', 'said'));
      /* Same batching as the main transcript, and for the same reason: a
         subagent narrating a long search is the fastest text on the page. */
      queue(said, ev.text, (node, chunk) => {
        node.textContent += chunk;
      });
      break;
    }
    case 'tool.proposed':
      toolCard(ev.call, ev.needs_approval, log);
      if (ev.needs_approval) {
        setCard(card, true);
        card.scrollIntoView({ block: 'nearest' });
        companion.set('waiting');
      }
      break;
    case 'tool.output.delta':
      toolOutput(ev.call_id, ev.text, ev.stream);
      break;
    case 'tool.completed':
      /* The card is about to be folded to a line and measured; the log inside
         it is not, but the output being written to it is. */
      flushStream();
      toolDone(ev.result);
      break;
    case 'tool.denied':
      denied(ev);
      break;
    case 'question.asked':
      questionCard(ev, log);
      setCard(card, true);
      companion.set('waiting');
      break;
    case 'error':
      log.appendChild(el('div', 'notice error', ev.message));
      break;
  }
}

/* ------------------------------------------------------------------ to-dos */

/* The to-do list, pinned above the composer: the one thing that stays in view
   while the transcript scrolls, because "which step is it on?" is the question
   someone watching a long turn keeps asking. The latest list wins, and a list
   with nothing left on it folds down to its title. */
const TODO_MARKS = { todo: '○', doing: '◐', done: '●', dropped: '⊘' };

function fillTodos(list, items) {
  list.textContent = '';
  for (const item of items || []) {
    const li = el('li', item.state);
    li.appendChild(el('span', 'mark', TODO_MARKS[item.state] || '○'));
    li.appendChild(el('span', 'text', item.text));
    list.appendChild(li);
  }
  return list;
}

function showTodos(items) {
  state.todos = items || [];
  const panel = $('#todo-panel');
  if (!state.todos.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const done = state.todos.filter((i) => i.state === 'done').length;
  const left = state.todos.filter((i) => i.state === 'todo' || i.state === 'doing').length;
  const doing = state.todos.find((i) => i.state === 'doing');
  $('#todo-title').textContent = doing ? doing.text : left ? 'To-do' : 'All done';
  $('#todo-count').textContent = `${done} of ${state.todos.length} done`;
  fillTodos($('#todo-list'), state.todos);
  if (!left) foldTodos(true);
}

function foldTodos(folded) {
  $('#todo-head').setAttribute('aria-expanded', String(!folded));
  $('#todo-list').hidden = folded;
}

/* ------------------------------------------------------------- background */

/* Work left running — a dev server, a watcher, an agent sent off — as one chip
   each, with the one control that matters. A server you cannot see is a port
   held until somebody goes looking for it. */
function taskEnded(task) {
  const what = task.kind === 'agent' ? 'Background agent' : 'Background command';
  if (task.status === 'stopped') return `${what} ${task.id} was stopped: ${task.label}`;
  if (task.kind === 'shell') return `${what} ${task.id} exited with code ${task.exit_code}: ${task.label}`;
  return `${what} ${task.id} ${task.status === 'done' ? 'finished' : 'failed'}: ${task.label}`;
}

function taskUpdated(task) {
  const known = state.tasks.get(task.id);
  state.tasks.set(task.id, task);
  if (task.status !== 'running' && known && known.status === 'running') {
    notice(taskEnded(task), task.status === 'failed' ? 'error' : '');
  }
  showTasks();
}

function showTasks() {
  const strip = $('#tasks-strip');
  strip.textContent = '';
  const running = [...state.tasks.values()].filter((t) => t.status === 'running');
  strip.hidden = !running.length;
  for (const task of running) {
    const chip = el('span', 'task-chip');
    chip.appendChild(el('span', 'dot'));
    chip.appendChild(el('span', 'id', task.id));
    const label = el('span', 'label', task.label);
    label.title = task.label;
    chip.appendChild(label);
    const stop = el('button', 'ghost tiny', 'Stop');
    stop.type = 'button';
    stop.title = `Stop ${task.id}`;
    stop.onclick = () => {
      stop.disabled = true;
      send({ type: 'task.stop', task_id: task.id });
    };
    chip.appendChild(stop);
    strip.appendChild(chip);
  }
}

/* Where the conversation was summarised to make room, or cleared. Said in the
   transcript because it changes what the agent knows without anything else
   visibly happening — after it, the model has the summary and not the words. */
function compactedNote(ev) {
  const box = el('div', 'compacted');
  if (ev.reason === 'cleared') {
    box.appendChild(el('span', '', 'Conversation cleared — the agent starts afresh from here.'));
    showTodos([]);
  } else {
    const details = el('details');
    const how = ev.reason === 'automatic' ? 'Compacted to make room' : 'Compacted';
    const size = ev.tokens_before ? `, about ${Math.max(1, Math.round(ev.tokens_before / 1000))}k tokens` : '';
    details.appendChild(el('summary', '', `${how}: ${ev.messages_before} messages${size}, now a summary`));
    details.appendChild(el('div', 'body', ev.summary));
    box.appendChild(details);
  }
  append(box);
}

/* ------------------------------------------------------------------ slash */

/* `/` at the start of the box opens the session's commands and skills,
   filtered as you type. A menu rather than a help page, because the only
   moment anybody wants the list is the moment they have typed the slash. */
const slash = { items: [], at: 0 };

async function loadCommands(id) {
  state.commands = [];
  const res = await api(`/api/sessions/${id}/commands`);
  if (!res || !res.ok || state.sessionId !== id) return;
  state.commands = (await res.json()).commands || [];
}

function slashQuery() {
  // Only while the name is being typed: once there is a space, what follows
  // is the command's arguments and the menu has nothing left to offer.
  const match = /^\/([\w:-]*)$/.exec($('#input').value);
  return match ? match[1].toLowerCase() : null;
}

function showSlash() {
  const query = slashQuery();
  if (query === null || !state.commands.length) {
    hideSlash();
    return;
  }
  const starts = state.commands.filter((c) => c.name.startsWith(query));
  const inside = state.commands.filter((c) => !c.name.startsWith(query) && c.name.includes(query));
  slash.items = [...starts, ...inside].slice(0, 8);
  if (!slash.items.length) {
    hideSlash();
    return;
  }
  slash.at = Math.min(slash.at, slash.items.length - 1);

  const menu = $('#slash');
  menu.textContent = '';
  slash.items.forEach((item, i) => {
    const li = el('li');
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', String(i === slash.at));
    li.appendChild(el('span', 'name', `/${item.name}`));
    if (item.hint) li.appendChild(el('span', 'hint', item.hint));
    li.appendChild(el('span', 'desc', item.description));
    li.appendChild(el('span', 'kind', item.kind));
    // mousedown rather than click, so the box keeps its focus and caret.
    li.onmousedown = (e) => {
      e.preventDefault();
      pickSlash(i);
    };
    menu.appendChild(li);
  });
  menu.hidden = false;
}

function hideSlash() {
  $('#slash').hidden = true;
  slash.items = [];
  slash.at = 0;
}

function pickSlash(i) {
  const item = slash.items[i];
  if (!item) return;
  const input = $('#input');
  input.value = `/${item.name} `;
  hideSlash();
  input.focus();
}

/* ------------------------------------------------------------ agent socket */

function send(command) {
  if (state.agent && state.agent.readyState === WebSocket.OPEN) {
    state.agent.send(JSON.stringify(command));
    return;
  }
  // The socket reconnects on its own, so a command sent during the gap is
  // held rather than dropped. Losing what someone typed — silently, because
  // the UI had already echoed it — is the worst failure this client has.
  state.outbox.push(command);
  notice('Not connected — held until the connection is back.');
}

function flushOutbox() {
  if (!state.agent || state.agent.readyState !== WebSocket.OPEN) return;
  const held = state.outbox.splice(0);
  for (const command of held) state.agent.send(JSON.stringify(command));
  if (held.length) notice(`Reconnected — sent ${held.length} held message${held.length === 1 ? '' : 's'}.`);
}

function setBusy(busy) {
  state.busy = busy;
  // The send button becomes the stop button while a turn is running: one
  // place to look, and no way to queue a second turn into a busy session.
  $('#send').disabled = busy || !state.sessionId;
  $('#send').hidden = busy;
  $('#stop').hidden = !busy;
}

function setLink(on) {
  const pill = $('#link');
  pill.textContent = on ? 'live' : 'offline';
  pill.className = `pill ${on ? 'on' : 'off'}`;
}

// Close codes the server uses for "this will never work". Retrying any of
// them is how one dead session id becomes an unbounded stream of identical
// errors — which is exactly what happened when the daemon was restarted under
// a page that still remembered a session from the previous one.
const FATAL_CLOSE = new Set([4400, 4401, 4404]);

function connectAgent(sessionId) {
  if (state.agent) {
    state.agent.onclose = null;
    state.agent.close();
  }
  state.sessionId = sessionId;

  // Resume rather than replay everything: on a reconnect we only want the gap.
  const ws = new WebSocket(socket('/ws/agent', { session: sessionId, since: state.seq }));
  state.agent = ws;

  ws.onopen = () => {
    setLink(true);
    state.retries = 0;
    // Whatever arrives next is the replay of what was missed, not news.
    companion.resumed();
    $('#input').disabled = false;
    $('#send').disabled = false;
    $('#voice-toggle').disabled = false;
    $('#rewind').hidden = false;
    $('#nav-rewind').disabled = false;
    flushOutbox();
  };

  ws.onmessage = (msg) => handleAgentEvent(JSON.parse(msg.data));

  ws.onclose = (event) => {
    setLink(false);
    if (state.sessionId !== sessionId) return;      // we moved on deliberately

    if (FATAL_CLOSE.has(event.code)) {
      // Nothing to retry. The commonest cause by far is the daemon having been
      // restarted while this page kept the old session id, so rather than
      // stopping with an error, forget it and attach to whatever is there now.
      forgetSession();
      state.sessionId = null;
      state.retries = 0;
      loadSessions();
      return;
    }

    // Genuinely transient — a laptop lid, a daemon restarting. Backed off
    // rather than hammered, and capped so it keeps trying all day at a
    // sensible rate.
    state.retries = (state.retries || 0) + 1;
    const wait = Math.min(1000 * 2 ** (state.retries - 1), 20000);
    setTimeout(() => {
      if (state.sessionId === sessionId) connectAgent(sessionId);
    }, wait);
  };
}

function handleAgentEvent(ev) {
  // Autopilot renders the same events as one line each, so it is fed here
  // rather than opening a second socket onto the same session — two readers
  // of one event stream is two places for the sequence number to drift.
  narrate(ev);

  if (ev.agent) {
    // A subagent's, which nests inside the call that started it.
    childEvent(ev);
    if (typeof ev.seq === 'number' && ev.seq > state.seq) state.seq = ev.seq;
    return;
  }

  switch (ev.type) {
    case 'session.started':
      // The promise, spelled out, once: what this session may do without
      // asking. The chips under the composer carry the short forms of the
      // same facts, so repeating the model here would only crowd the title.
      $('#session-meta').textContent = ev.policy;
      $('#mode-wrap').title = ev.policy;
      dressChips({ model: ev.model, root: ev.cwd, effort: ev.effort ?? null });
      break;

    case 'policy.changed': {
      // One event for both live controls, so say only what actually moved —
      // a thinking change announced as "Approval is now: ask first" would be
      // a true sentence about the wrong thing.
      const before = state.info || {};
      $('#mode').value = ev.mode;
      if (before.policy !== ev.mode) notice(`Approval is now: ${ev.policy || ev.mode}.`);
      if ((before.effort ?? null) !== (ev.effort ?? null)) {
        notice(`Thinking is now: ${ev.effort || "the model's own default"}.`);
      }
      dressChips({ policy: ev.mode, effort: ev.effort ?? null });
      break;
    }

    case 'turn.started':
      state.turnNode = null;
      // The server's clock, not this one: a reattached client replays turns
      // that ended hours ago, and timing them against `now` would report
      // every one of them as having taken a millisecond.
      state.turnStart = ev.at || 0;
      state.turnFiles = new Map();
      setBusy(true);
      companion.set('thinking');
      // Rendered from the event rather than on submit, so a live client and a
      // reattached one show the same conversation. The local echo is removed
      // first to avoid showing it twice.
      if (ev.text) {
        const echoed = state.echo && state.echo.querySelector('.body');
        if (echoed && echoed.textContent === ev.text) state.echo.remove();
        state.echo = null;
        userTurn(ev.text);
      }
      break;

    case 'text.delta':
      appendText(ev.text);
      break;

    /* Anything that rebuilds the transcript's children out from under a queued
       write has to hand those writes back first, or they land on a node that is
       about to be detached. Cheap when nothing is pending, which is all but the
       streaming paths. */
    case 'text.flush':
      flushStream();
      break;

    case 'thinking.delta':
      appendThinking(ev.text);
      break;

    case 'tool.proposed': {
      state.turnNode = null;
      const card = toolCard(ev.call, ev.needs_approval);
      // Open while it works, so its steps are visible as they happen; it
      // folds to a line when it reports back.
      if (ev.call.name === 'agent') setCard(card, true);
      // What it is about to do, or the fact that it cannot do it without you.
      if (ev.needs_approval) companion.set('waiting');
      else companion.tool(ev.call.name);
      break;
    }

    case 'tool.output.delta':
      toolOutput(ev.call_id, ev.text, ev.stream);
      break;

    case 'tool.completed':
      toolDone(ev.result);
      if (ev.result.name === 'todo' && ev.result.ok && ev.result.display) showTodos(ev.result.display.items);
      if (ev.result.ok) companion.set('thinking');
      else companion.flash('error');
      break;

    case 'tool.denied':
      denied(ev);
      companion.set('thinking');
      break;

    case 'task.updated':
      taskUpdated(ev.task);
      break;

    case 'context.compacted':
      compactedNote(ev);
      break;

    case 'question.asked':
      state.turnNode = null;
      questionCard(ev);
      companion.set('waiting');
      break;

    case 'turn.completed': {
      /* The turn's last words are almost certainly still queued. This card is
         the end of the turn, so anything buffered has to land before it — an
         answer that is missing its final sentence because the socket closed on
         the same frame is the sort of thing that gets reported as "it cuts off
         at the end". */
      flushStream();
      setBusy(false);
      state.turnNode = null;
      companion.set('idle');
      // How long that took, and what it cost you in files. Both are things
      // you would otherwise have to reconstruct by scrolling. Said only when
      // both ends of it are known — a made-up duration is worse than none.
      if (state.turnStart && ev.at) {
        append(el('div', 'worked', `Worked for ${took((ev.at - state.turnStart) * 1000)}`));
      }
      state.turnStart = 0;
      changedCard();
      // How full it is now. The turn event carries it, so this costs nothing
      // and is right for the moment the turn ended rather than for whenever
      // somebody thought to ask.
      if (ev.context) renderContext(ev.context);
      if (ev.stop_reason === 'interrupted') notice('Interrupted.');
      else if (ev.stop_reason === 'max_steps') notice('Stopped: too many steps.', 'error');
      else if (ev.stop_reason === 'error') companion.flash('error');
      else companion.flash('success');
      break;
    }

    case 'error':
      flushStream();
      notice(ev.message, 'error');
      setBusy(false);
      companion.set('idle');
      companion.flash('error');
      break;

    case 'session.ended':
      notice(`Session ended: ${ev.reason}`);
      setBusy(false);
      companion.set('idle');
      break;

    case 'pong':
      return; // no sequence of its own
  }

  // The server stamps each event with its sequence number; remembering the
  // highest is what lets a reconnect ask for the gap instead of the lot.
  if (typeof ev.seq === 'number' && ev.seq > state.seq) state.seq = ev.seq;
  refreshSessionsSoon();
}

/* ------------------------------------------------------------------ voice */

/* The composer's microphone button opens the same call Talk mode uses; the
   difference is only the surface around it. See voice.js — the call itself
   lives there precisely so that these two cannot drift apart on the delicate
   part, which is barge-in. */

function setVoiceState(text) {
  $('#voice-state').textContent = text;
}

function startStripVoice() {
  const call = new Voice({
    ready: (ev) => {
      $('#voice-strip').hidden = false;
      setVoiceState(`${ev.stt} → ${ev.llm} → ${ev.tts}`);
    },
    listening: () => companion.set('listening'),
    thinking: () => companion.set('thinking'),
    partial: (text) => { $('#voice-partial').textContent = text; },
    heard: (text, submitted) => {
      $('#voice-partial').textContent = '';
      if (submitted) userTurn(text);
      else notice(`Heard "${text}" — too short to answer.`);
    },
    said: (delta) => appendText(delta),
    speaking: () => {
      state.turnNode = null;
      companion.set('speaking');
    },
    cancelled: () => {
      notice('— interrupted');
      companion.set('listening');
    },
    finished: () => companion.set('idle'),
    error: (message) => notice(message, 'error'),
    stopped: () => {
      state.voice = null;
      companion.set('idle');
      $('#voice-strip').hidden = true;
      $('#voice-toggle').classList.remove('on');
      $('#voice-toggle').title = 'Start voice';
    },
  });
  return call;
}

/* --------------------------------------------------------------- requests */

/* `api` itself is in dom.js, because every mode needs it and the knowledge
   that the daemon has gone must exist in exactly one place — two copies means
   one of them showing "live" while the other has been failing for a minute. */
onReachable((ok) => {
  if (ok) return;
  const pill = $('#link');
  pill.textContent = 'no daemon';
  pill.className = 'pill off';
});

/* ------------------------------------------------------------------ rewind */

async function loadRewind() {
  const list = $('#rewind-list');
  list.textContent = '';
  const res = await api(`/api/sessions/${state.sessionId}/checkpoints`);
  if (!res || !res.ok) return;
  const { enabled, checkpoints } = await res.json();
  if (!enabled) return;

  if (!checkpoints.length) {
    const li = el('li', 'empty', 'Nothing to undo — no files have been changed yet.');
    list.appendChild(li);
    return;
  }

  for (const cp of checkpoints) {
    const li = el('li');
    const what = el('div', 'what');
    what.appendChild(el('span', 'label', cp.label || `turn ${cp.turn_id}`));
    const names = cp.paths.map((p) => p.split('/').pop()).join(', ');
    what.appendChild(el('div', 'files', `${cp.files} file${cp.files === 1 ? '' : 's'} · ${names}`));
    li.appendChild(what);

    const go = el('button', 'ghost small', 'Undo to here');
    go.onclick = async () => {
      // Named plainly rather than as "restore": what it does is throw away
      // work, and the button should say so before it is pressed.
      if (!confirm(`Put files back to before "${cp.label}"?\n\nThis undoes that turn and every one after it.`)) return;
      if (await restore(cp.id)) loadRewind();
    };
    li.appendChild(go);
    list.appendChild(li);
  }
}

/* Shared by the Rewind dialog and by the Undo button on a turn's receipt:
   both throw the same work away, so both had better report it the same way. */
async function restore(checkpointId) {
  const r = await api(`/api/sessions/${state.sessionId}/restore`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ checkpoint: checkpointId }),
  });
  if (!r) {
    notice('The daemon is not reachable.', 'error');
    return false;
  }
  if (!r.ok) {
    notice((await r.json()).detail || 'could not rewind', 'error');
    return false;
  }
  const report = await r.json();
  const bits = [];
  if (report.restored.length) bits.push(`${report.restored.length} restored`);
  if (report.deleted.length) bits.push(`${report.deleted.length} deleted`);
  if (report.skipped && Object.keys(report.skipped).length) {
    bits.push(`${Object.keys(report.skipped).length} skipped`);
  }
  notice(`Rewound: ${bits.join(', ') || 'nothing to change'}.`);
  // Worth saying out loud: those files had been edited by hand since, and
  // that work has just been overwritten.
  if (report.changed_since && report.changed_since.length) {
    notice(
      `${report.changed_since.length} file(s) had been changed since the agent wrote them, ` +
      `and were overwritten: ${report.changed_since.join(', ')}`,
      'error',
    );
  }
  return true;
}

function wireRewind() {
  const open = () => { loadRewind(); $('#rewind-dialog').showModal(); };
  $('#rewind').onclick = open;
  $('#nav-rewind').onclick = open;
  $('#rewind-close').onclick = () => $('#rewind-dialog').close();
}

/* ------------------------------------------------------------------ memory */

/* Memory is off on some installs entirely, and off per person on the rest.
   The button is only shown when the endpoints exist, so nothing here implies
   a feature that is not there. */

async function memorySettings() {
  const res = await api('/api/memory/settings');
  if (!res || res.status === 404) return null;   // unreachable, or not enabled here
  return res.ok ? res.json() : null;
}

async function loadMemory() {
  const settings = await memorySettings();
  if (!settings) return;

  $('#mem-enabled').checked = settings.enabled;
  $('#mem-auto').checked = settings.auto_capture;
  // Automatic capture is meaningless without the master switch, and offering
  // it as though it were independent would misrepresent what it does.
  $('#mem-auto').disabled = !settings.enabled;

  const list = $('#mem-list');
  list.textContent = '';

  if (!settings.enabled) {
    // Switching memory off stops it storing and stops it recalling; it does
    // not delete what is already there. Saying only "nothing is being stored"
    // would read as "your data is gone", which would be untrue.
    $('#mem-count').textContent = settings.count
      ? `Nothing new is being stored, and nothing is being recalled. ${settings.count} earlier `
        + `memor${settings.count === 1 ? 'y is' : 'ies are'} still kept — use Forget everything to delete them.`
      : 'Nothing is being stored, and nothing is kept.';
    return;
  }

  const res = await api('/api/memory');
  if (!res || !res.ok) return;
  const { memories } = await res.json();
  $('#mem-count').textContent = memories.length
    ? `${memories.length} remembered.`
    : 'Nothing remembered yet.';

  for (const m of memories) {
    const li = el('li');
    const text = el('div', 'text');
    text.appendChild(el('div', '', m.text));
    const when = new Date(m.created_at * 1000).toLocaleDateString();
    text.appendChild(el('div', 'meta', `${m.kind} · ${when}${m.hits ? ` · used ${m.hits}×` : ''}`));
    li.appendChild(text);

    const forget = el('button', 'ghost forget', 'Forget');
    forget.onclick = async () => {
      await api(`/api/memory/${m.id}`, { method: 'DELETE' });
      loadMemory();
    };
    li.appendChild(forget);
    list.appendChild(li);
  }
}

async function saveMemorySettings() {
  await api('/api/memory/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      enabled: $('#mem-enabled').checked,
      auto_capture: $('#mem-auto').checked,
    }),
  });
  loadMemory();
}

function wireMemory() {
  $('#mem-enabled').onchange = saveMemorySettings;
  $('#mem-auto').onchange = saveMemorySettings;

  $('#open-memory').onclick = () => {
    loadMemory();
    $('#memory-dialog').showModal();
  };
  $('#mem-close').onclick = () => $('#memory-dialog').close();

  $('#mem-wipe').onclick = async () => {
    // Irreversible and it also switches memory back off, so it is worth one
    // question — but only one.
    if (!confirm('Delete everything openmirror remembers about you? This cannot be undone.')) return;
    await api('/api/memory', { method: 'DELETE' });
    loadMemory();
  };

  memorySettings().then((settings) => {
    if (settings) $('#open-memory').hidden = false;
  });
}

/* ----------------------------------------------------------------- browser */

/* Which browser the agent drives. A list rather than a dropdown because the
   interesting part of each option is the sentence explaining when you would
   want it, and a dropdown has nowhere to put that — and because an option that
   is not installed has to be able to say so next to itself rather than
   disappearing, which is how someone concludes openmirror cannot use their Chrome. */

async function loadBrowserChoices() {
  const res = await api('/api/browser');
  if (!res || !res.ok) return null;
  const state = await res.json();
  renderBrowserChoices(state);
  return state;
}

function renderBrowserChoices(state) {
  const list = $('#browser-list');
  list.innerHTML = '';

  for (const choice of state.choices) {
    const row = document.createElement('li');
    row.className = 'conn-row';
    const chosen = choice.id === state.browser;
    row.innerHTML = `
      <label class="switch">
        <input type="radio" name="browser-choice" value="${choice.id}"
               ${chosen ? 'checked' : ''} ${choice.installed ? '' : 'disabled'}>
        <span>${choice.label}
          <em>${choice.note}${choice.installed ? '' : ` — ${choice.why || 'not available here'}`}</em>
        </span>
      </label>`;
    row.querySelector('input').onchange = () => {
      $('#browser-path-field').hidden = choice.id !== 'custom';
      if (choice.id === 'custom') $('#browser-path').focus();
      else saveBrowserChoice(choice.id);
    };
    list.appendChild(row);
  }

  $('#browser-path-field').hidden = state.browser !== 'custom';
  $('#browser-path').value = state.executable || '';

  // Which of the two is in force, said plainly: somebody who set
  // OPENMIRROR_BROWSER_CHANNEL and then picked something here needs to know which
  // one won, and "saved" versus "from the environment" is that answer.
  const where = state.source === 'saved'
    ? 'Saved here, so it overrides OPENMIRROR_BROWSER_ENGINE and friends.'
    : 'From this install\'s environment. Choosing one here overrides it.';
  $('#browser-state').textContent =
    `${state.label} — ${where} Running ${state.headless ? 'headless' : 'headful'}; `
    + `profile at ${state.profile}.`;
  $('#browser-reset').hidden = state.source !== 'saved';
}

async function saveBrowserChoice(id) {
  const body = { browser: id };
  if (id === 'custom') body.executable = $('#browser-path').value.trim();

  $('#browser-status').textContent = 'Checking…';
  const res = await api('/api/browser', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res) {
    $('#browser-status').textContent = 'The daemon is not answering.';
    return;
  }
  const state = await res.json().catch(() => null);

  if (!res.ok) {
    // The refusal is the useful part — "Chrome is not installed", "that path is
    // not executable" — so it is shown rather than replaced with "failed".
    const why = (state && state.detail) || 'Could not use that browser.';
    // Re-read, so the radio goes back to what is actually in force rather than
    // sitting on the option that was just refused — but put back what they
    // typed. A path rejected for one wrong character is a path to fix, and
    // clearing the field makes them type the whole thing again.
    await loadBrowserChoices();
    if (id === 'custom') {
      $('#browser-path-field').hidden = false;
      $('#browser-path').value = body.executable;
      $('#browser-path').focus();
    }
    $('#browser-status').textContent = why;
    return;
  }
  $('#browser-status').textContent = (state && state.note) || 'Saved.';
  loadBrowserChoices();
}

function wireBrowserChoice() {
  // Enter in the path field saves it. A Save button next to a single input that
  // already has Enter is a button nobody presses twice.
  $('#browser-path').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    saveBrowserChoice('custom');
  });

  $('#browser-reset').onclick = async () => {
    await api('/api/browser', { method: 'DELETE' });
    $('#browser-status').textContent = 'Back to the default.';
    loadBrowserChoices();
  };
}

/* ---------------------------------------------------------------- settings */

/* One dialog, two tabs, and a hook per tab for whatever it needs loading.
   Tabs rather than one scrolling page because which browser to drive and which
   model answers a request are unrelated decisions that happen to both be
   settings — somebody opening this has come for exactly one of them.

   The panel that is not showing is `hidden`, not merely invisible: a panel
   hidden with CSS is still in the tab order, and tabbing into a form you
   cannot see is a way to edit a connection by accident. */

const SETTINGS_TABS = {
  'tab-browser': { panel: 'panel-browser', load: () => loadBrowserChoices() },
  'tab-connections': { panel: 'panel-connections', load: () => openConnections() },
  'tab-updates': { panel: 'panel-updates', load: () => wireUpdates() },
};

function showSettingsTab(id) {
  for (const [tab, { panel }] of Object.entries(SETTINGS_TABS)) {
    const on = tab === id;
    $(`#${tab}`).setAttribute('aria-selected', String(on));
    $(`#${panel}`).hidden = !on;
  }
  SETTINGS_TABS[id].load();
}

function wireSettings(onConnectionsChanged) {
  for (const id of Object.keys(SETTINGS_TABS)) {
    $(`#${id}`).onclick = () => showSettingsTab(id);
  }

  $('#open-settings').onclick = () => {
    showSettingsTab('tab-browser');
    $('#settings-dialog').showModal();
    watchUpdates(true);
  };

  // The update poll only runs while this dialog is open. A progress bar
  // updating itself in a window nobody is looking at is work for nobody, and
  // the rule this project already follows for a voice call left running.
  $('#settings-dialog').addEventListener('close', () => watchUpdates(false));

  $('#settings-close').onclick = () => {
    $('#settings-dialog').close();
    // Connections are re-read on close whichever tab was open. Changing a
    // route and coming back to a header still naming the old model is how you
    // conclude the change did not take.
    if (onConnectionsChanged) onConnectionsChanged();
  };

  // The dialog's own close (Escape, the backdrop) has to do the same, or the
  // refresh depends on which control was used to leave.
  $('#settings-dialog').addEventListener('close', () => {
    if (onConnectionsChanged) onConnectionsChanged();
  });
}

/* ------------------------------------------------------------------- shell */

let refreshTimer = null;
function refreshSessionsSoon() {
  clearTimeout(refreshTimer);
  /* Cleared as the timer fires, not left behind: `pollSessions` stands down
     while one of these is pending, and a handle that outlived its own timeout
     would stand it down forever. */
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    loadSessions().catch(() => {});
  }, 700);
}

/* What the sidebar is about to draw, as one string. This is the change test.
   `loadSessions` used to sit on a five-second timer and rebuild the whole list
   on every tick, so an idle tab fetched a JSON summary of every session and
   recreated a hundred DOM nodes twelve times a minute to produce a list
   identical to the one already on screen — the same cost whether anything had
   happened or not, which is the wrong shape for a poll.

   So the fetch happens when something might have changed, and the rebuild only
   when the answer actually differs.

   `idle_for` is deliberately not in the fingerprint. It counts up every
   second, so hashing it would report a change on every poll and defeat the
   whole thing. What the row shows is `ago(idle_for)`, and that only ever
   changes at a minute, an hour or a day — so the fingerprint records which of
   those four bands the session is in, and the text is kept current locally by
   `restateTimes` instead. `state.sessionId` is in the fingerprint because it
   decides `aria-current`: selecting a session has to redraw the list even
   though no session changed. */
function sessionFingerprint(sessions) {
  return JSON.stringify([
    state.sessionId,
    sessions.map((s) => [
      s.id, s.title, s.root, s.model, s.turns,
      s.busy ? 1 : 0, s.waiting_on || '',
      s.idle_for < 60 ? 0 : s.idle_for < 3600 ? 1 : s.idle_for < 86400 ? 2 : 3,
    ]),
  ]);
}

/* The last thing the sidebar was drawn from. Cleared on teardown so a new
   session cannot inherit a stale "already up to date" and skip its first
   render. */
let lastPrint = '';

/* When that draw happened, so `restateTimes` can work out how far the server's
   `idle_for` has aged without asking it again. */
let loadedAt = Date.now();

/* The rows on screen, so the relative times can be re-stated without a fetch. */
const sessionRows = new Map();

async function loadSessions() {
  const res = await api('/api/sessions');
  if (!res || !res.ok) return;
  const { sessions } = await res.json();

  // This knows the truth sooner than the socket does. If the session we
  // are holding is not in the list, it is gone — the daemon was restarted —
  // and waiting for the websocket to work that out means sitting in backoff
  // for up to twenty seconds on an id that will never resolve.
  if (state.sessionId && !sessions.some((s) => s.id === state.sessionId)) {
    forgetSession();
    state.sessionId = null;
    state.retries = 0;
    if (state.agent) {
      state.agent.onclose = null;      // do not let it schedule another retry
      state.agent.close();
      state.agent = null;
    }
  }

  // On a fresh page, reattach to whatever was open. The session outlived the
  // tab; the point of that is undermined if reopening starts from nothing.
  if (!state.sessionId && sessions.length) {
    const wanted = lastSession();
    const resume = sessions.find((s) => s.id === wanted) || sessions[0];
    if (resume) openSession(resume);
  }

  // Nothing that goes on screen has changed, so stop before touching the DOM.
  // The relative times are the one thing that keeps moving, and they are
  // handled locally by `restateTimes` without a fetch.
  const print = sessionFingerprint(sessions);
  if (print === lastPrint) return;
  lastPrint = print;
  loadedAt = Date.now();

  // Grouped by working root. A root is the only thing a session is really
  // *about* — everything else about it is history — so it is what the list
  // sorts itself under, and one machine running three projects reads as
  // three projects rather than as nine chats.
  const list = $('#sessions');
  list.textContent = '';
  sessionRows.clear();

  const roots = new Map();
  for (const s of sessions) {
    if (!roots.has(s.root)) roots.set(s.root, []);
    roots.get(s.root).push(s);
  }

  for (const [root, group] of roots) {
    const head = el('li', 'group');
    head.appendChild(icon('folder'));
    head.appendChild(el('span', '', basename(root)));
    head.title = root;
    list.appendChild(head);

    for (const s of group) {
      const li = el('li');
      li.setAttribute('aria-current', String(s.id === state.sessionId));

      let status = `${s.turns} turn${s.turns === 1 ? '' : 's'}`;
      const dot = el('span', 'state');
      if (s.busy) {
        dot.classList.add('busy');
        status = 'working';
      }
      if (s.waiting_on) {
        dot.classList.add('waiting');
        status = s.waiting_on === 'approval' ? 'needs approval' : 'asked you something';
      }
      // Colour is never the only carrier: the same thing is in the row's
      // label, which is what a screen reader and a hover both get.
      li.appendChild(dot);
      li.appendChild(el('span', 'name', s.title || s.id));
      const when = el('span', 'when', s.busy ? 'now' : ago(s.idle_for));
      li.appendChild(when);
      li.title = `${status} · ${s.model}`;
      li.setAttribute('aria-label', `${s.title || s.id} — ${status}`);

      li.onclick = () => openSession(s);
      sessionRows.set(s.id, { when, summary: s });
      list.appendChild(li);
    }
  }
}

/* "3m" has to become "1h" on its own. A local timer, and only for the text: no
   fetch, no rebuild, and it walks the rows rather than the server's session
   list. It is not a replacement for the poll. The poll watches for sessions
   appearing and disappearing, which this cannot see; this only keeps text that
   is already on screen honest as it ages. */
function restateTimes() {
  // Nothing drawn yet, so there is nothing to age. `loadedAt` is set when the
  // first draw happens, and until then it holds page-load time, which would
  // subtract time the rows have not been up for.
  if (!sessionRows.size) return;
  const elapsed = Math.floor((Date.now() - loadedAt) / 1000);
  for (const { when, summary } of sessionRows.values()) {
    if (summary.busy) continue;              // "now" does not go stale
    when.textContent = ago(Math.max(0, summary.idle_for - elapsed));
  }
}

function basename(path) {
  return String(path || '').replace(/\/+$/, '').split('/').pop() || String(path || '');
}

/* Put a session in the window: its name in the bar, its facts in the chips
   under the composer, and its socket attached. */
function openSession(summary) {
  $('#session-title').textContent = summary.title || summary.id;
  dressChips(summary);
  selectSession(summary.id);
}

/* What the next thing you say will be run under: which folder, which model,
   what it is allowed to do. Kept beside the box you type in, because that is
   what all three of them qualify. */
function dressChips(patch) {
  state.info = { ...(state.info || {}), ...patch };
  const info = state.info;

  $('#mode-wrap').hidden = false;
  if (info.policy) $('#mode').value = info.policy;
  const effortWrap = $('#effort-wrap');
  if (effortWrap) {
    effortWrap.hidden = false;
    if ('effort' in info) $('#effort').value = info.effort || '';
  }

  const root = $('#root-chip');
  if (info.root) {
    root.hidden = false;
    root.querySelector('span').textContent = basename(info.root);
    root.title = info.root;
  }
  // The commit bar follows the project you are in, and hides itself where
  // there is no repository to commit to. Called here rather than from the
  // commit module so it changes on every root the session reports, including
  // the ones that arrive on the socket.
  setCommitRoot(info.root);

  const model = $('#model-chip');
  if (info.model) {
    model.hidden = false;
    model.textContent = info.model;
    model.title = `answering with ${info.model}`;
  }
  dressHero();
}

async function loadProviders() {
  const res = await api('/api/providers');
  if (!res || !res.ok) return;
  const data = await res.json();
  const box = $('#providers');
  box.textContent = '';

  for (const [modality, ids] of Object.entries(data.capabilities || {})) {
    const row = el('div', 'row');
    row.appendChild(el('span', '', modality));
    const provider = ids[0];
    const info = (data.providers || []).find((p) => p.id === provider);
    row.appendChild(el('span', info && info.local ? 'local' : '', provider));
    box.appendChild(row);
  }
  if (!Object.keys(data.capabilities || {}).length) {
    box.appendChild(el('div', 'row', 'no providers configured'));
  }

  // Which provider is answering, next to which model is answering. The pair
  // is one fact, and it belongs under the composer with the rest of them.
  const chat = (data.capabilities || {}).chat || [];
  const chip = $('#provider-chip');
  if (chat.length) {
    const info = (data.providers || []).find((p) => p.id === chat[0]);
    chip.hidden = false;
    chip.textContent = chat[0];
    chip.title = info && info.local ? 'running on this machine' : 'a remote provider';
    chip.classList.toggle('local', Boolean(info && info.local));
  }

  const select = $('#new-dialog select[name=provider]');
  // Rebuilt rather than appended to: this runs again whenever a connection is
  // added, and appending would give three copies of Groq after three edits.
  select.textContent = '';
  const auto = el('option', '', 'first that can do chat');
  auto.value = '';
  select.appendChild(auto);
  for (const p of data.providers || []) {
    if (!p.modalities.includes('chat')) continue;
    const option = el('option', '', `${p.label}${p.local ? ' (local)' : ''}`);
    option.value = p.id;
    select.appendChild(option);
  }

  // The studio's provider list is the subset that can actually make something.
  wireStudio((data.providers || []).filter(
    (p) => p.modalities.includes('image') || p.modalities.includes('video'),
  ));
}

const LAST_SESSION = 'openmirror.session';

function rememberSession(id) {
  // Wrapped because a browser with site data blocked throws on access rather
  // than returning null, and losing the page over a convenience is silly.
  try { localStorage.setItem(LAST_SESSION, id); } catch { /* private window */ }
}

function forgetSession() {
  try { localStorage.removeItem(LAST_SESSION); } catch { /* private window */ }
}

function lastSession() {
  try { return localStorage.getItem(LAST_SESSION); } catch { return null; }
}

function selectSession(id) {
  if (id === state.sessionId) return;
  clearTranscript();
  state.seq = 0;
  state.turnNode = null;
  state.turnStart = 0;
  state.turnFiles = new Map();
  // The diff stat counts what this client watched happen in one session, so
  // moving to another one starts it again rather than carrying a total over.
  state.added = 0;
  state.removed = 0;
  showDiffstat();
  // Held commands belong to the session they were typed for, not the next one.
  state.outbox = [];
  // As do its to-dos, its background work and what `/` offers in it. The
  // first two come back with the replay; the commands are asked for.
  showTodos([]);
  state.tasks = new Map();
  showTasks();
  hideSlash();
  // The file list belongs to the session it was asked about; offering files
  // from the one just left is worse than offering none.
  resetFiles();
  loadCommands(id);
  rememberSession(id);
  // A reattached session has the transcript but never saw the turn that
  // produced it, so the meter is empty until it asks. Deliberately after the
  // id is set, so the request is about the session being shown.
  refreshContext(id);
  connectAgent(id);
  loadSessions();
}

/* The thinking level a new session starts at: the last one chosen, on this
   browser. Per viewer and best-effort — storage can be missing entirely —
   because the session itself is where the level really lives. */
function preferredEffort() {
  try { return localStorage.getItem('openmirror.effort') || null; } catch { return null; }
}
function rememberEffort(value) {
  try {
    if (value) localStorage.setItem('openmirror.effort', value);
    else localStorage.removeItem('openmirror.effort');
  } catch { /* storage unavailable: the session still has its level */ }
}

async function createSession(form) {
  const body = {
    root: form.root.value.trim() || null,
    model: form.model.value.trim() || null,
    provider: form.provider.value || null,
    mode: form.mode.value,
    tools: form.tools.value ? form.tools.value.split(',') : [],
    effort: preferredEffort(),
  };
  const res = await api('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res) {
    notice('The daemon is not reachable.', 'error');
    return;
  }
  if (!res.ok) {
    notice((await res.json()).detail || 'could not create the session', 'error');
    return;
  }
  const session = await res.json();
  openSession({ ...session, policy: body.mode });
}

function wire() {
  const input = $('#input');

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + 'px';
    companion.typing();
    showSlash();
  });

  input.addEventListener('keydown', (e) => {
    // The file list first: while it is open, Enter belongs to it, and the
    // slash menu is not showing at the same time.
    if (handleFileKey(e)) return;
    if (!$('#slash').hidden) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : -1;
        slash.at = (slash.at + step + slash.items.length) % slash.items.length;
        showSlash();
        return;
      }
      // Enter completes a name that is not finished yet, and sends one that
      // is: typing "/compact" in full and pressing Enter should run it.
      const chosen = slash.items[slash.at];
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && chosen && slashQuery() !== chosen.name)) {
        e.preventDefault();
        pickSlash(slash.at);
        return;
      }
      if (e.key === 'Escape') {
        // Closing the menu, not stopping the turn: the document-level
        // Escape is for that, and this one is closer to hand.
        e.preventDefault();
        e.stopPropagation();
        hideSlash();
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('#composer').requestSubmit();
    }
  });

  $('#todo-head').onclick = () => foldTodos($('#todo-head').getAttribute('aria-expanded') === 'true');

  $('#composer').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || state.busy) return;
    // Echoed immediately so typing feels instant; the server's turn.started
    // replaces it, so live and replayed conversations render identically.
    state.echo = userTurn(text);
    send({ type: 'turn.submit', text });
    hideSlash();
    input.value = '';
    input.style.height = 'auto';
  });

  $('#stop').onclick = () => send({ type: 'turn.interrupt' });

  const newSession = () => $('#new-dialog').showModal();
  $('#new-session').onclick = newSession;
  $('#nav-new').onclick = newSession;
  $('#new-dialog').addEventListener('close', function () {
    if (this.returnValue === 'create') createSession(this.querySelector('form'));
  });

  // Approval is a live control, not a label. Changing it takes effect from
  // the next tool call — the one already in flight was decided under the old
  // rule, and pretending otherwise would be a lie about what ran.
  $('#mode').onchange = (e) => {
    if (!state.sessionId) return;
    send({ type: 'policy.set', mode: e.target.value });
  };

  // How hard the model thinks — beside the approval mode because it is the
  // other live control, and for the same reason: whether a problem deserves
  // deliberation is learned while watching the model work on it. Built here
  // rather than in the page's markup; the levels are the six the server
  // takes, plus "default", which hands the choice back to the model. `/think`
  // in the composer does the same from the keyboard.
  const effortWrap = document.createElement('label');
  effortWrap.id = 'effort-wrap';
  effortWrap.className = 'chip';
  effortWrap.hidden = true;
  effortWrap.title = 'How hard the model thinks before answering. xhigh and max only differ on frontier '
    + 'models; a smaller model uses its hardest setting instead.';
  const effortSelect = document.createElement('select');
  effortSelect.id = 'effort';
  for (const [value, label] of [
    ['', 'think: default'], ['off', 'think: off'], ['low', 'think: low'], ['medium', 'think: medium'],
    ['high', 'think: high'], ['xhigh', 'think: xhigh'], ['max', 'think: max'],
  ]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    effortSelect.append(option);
  }
  effortWrap.append(effortSelect);
  $('#mode-wrap').after(effortWrap);
  effortSelect.value = preferredEffort() || '';
  effortSelect.onchange = (e) => {
    rememberEffort(e.target.value);
    if (!state.sessionId) return;
    send({ type: 'policy.set', effort: e.target.value || 'default' });
  };

  // The provider list is long on a well-configured machine and irrelevant
  // most of the time, so it starts folded.
  $('#providers-toggle').onclick = function () {
    const open = this.getAttribute('aria-expanded') !== 'true';
    this.setAttribute('aria-expanded', String(open));
    $('#providers').hidden = !open;
  };

  buildStarters();
  settle();

  $('#voice-toggle').onclick = async () => {
    if (state.voice) {
      state.voice.stop();
      return;
    }
    const voice = startStripVoice();
    try {
      // The strip's call is attached to the open session, so speaking to it
      // here can act on the machine — which is the difference between this
      // and Talk mode's default, where the tick box is off.
      await voice.start({ agentSession: state.sessionId });
      state.voice = voice;
      $('#voice-toggle').classList.add('on');
      $('#voice-toggle').title = 'End voice';
    } catch (err) {
      notice(`Voice could not start: ${err.message}`, 'error');
      voice.stop();
    }
  };

  $('#voice-interrupt').onclick = () => state.voice && state.voice.interrupt();
  $('#voice-mute').onclick = (e) => {
    if (!state.voice) return;
    const muted = !state.voice.muted;
    state.voice.setMuted(muted);
    e.target.textContent = muted ? 'Unmute' : 'Mute';
  };

  // Escape stops whatever is running: the fastest possible way to say "no",
  // and it has to mean that in every mode. Ordered by what is most urgent to
  // stop — something currently talking at you comes before a turn quietly
  // grinding away in the background.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (isLive()) endLive();
    else if (talking()) stopTalking();
    else if (state.voice) state.voice.interrupt();
    else if (state.busy) send({ type: 'turn.interrupt' });
  });

  // The modes worth a shortcut are the two that change what the keyboard is
  // for. Ctrl+Shift rather than a bare letter, because a bare letter would
  // fire while someone is typing into the composer.
  document.addEventListener('keydown', (e) => {
    if (!e.ctrlKey || !e.shiftKey) return;
    if (e.key.toLowerCase() === 't') {
      e.preventDefault();
      $('#talk-button').click();
    }
  });
}

// The `native` class is added by the desktop shell itself, and only when it
// actually made the window transparent. It is not sniffed from the user agent:
// whether transparency worked is a property of the compositor, which the page
// has no way to observe, and dropping the background without it would leave
// the interface floating on nothing.

/* ------------------------------------------------------------------ modes */

/* The one button. It does the whole thing — switches the app to talking and
   opens the microphone — because a "talk mode" you then have to press a
   second control to actually start talking in is two buttons wearing one
   coat. Pressing it again puts you back where you were. */
function wireTheOneButton() {
  $('#talk-button').onclick = () => {
    if (mode() === 'talk' || mode() === 'live') {
      go('code');
      return;
    }
    go('talk');
    startTalking();
  };
}

/* What each mode owns while it is on screen, and what it must give back.
 *
 * Handing things back is the half that matters. A microphone left open
 * because someone clicked away from Talk is a microphone left open; a frame
 * stream left running is a screen being captured for nobody. So every mode
 * that takes a resource releases it here, in one place, rather than each of
 * them remembering to. */
function wireModeLifecycle() {
  onEnter('talk', () => companion.set('idle'));
  onLeave('talk', () => stopTalking());

  onEnter('live', () => companion.set('idle'));
  onLeave('live', () => endLive());

  onEnter('watch', () => refreshRuns());

  onEnter('studio', () => openStudio());
  onLeave('studio', () => stopPolling());

  onEnter('search', () => focusSearch());
}

wire();
wireMemory();
wireBrowserChoice();
wireRewind();
wireTheOneButton();

const sessionId = () => state.sessionId;
wireTalk({ sessionId });
wireLive({ sessionId });
wireWatch({
  root: () => (state.info && state.info.root) || null,
  // Autopilot's narration comes off the ordinary agent socket, so watching a
  // run means attaching to its session — which also puts its approvals and
  // questions in the Code tab, with the full context around them.
  attach: (id) => selectSession(id),
});
wireSearch();
wireMail();
wireCommit();
wireContext();
wireFiles({ sessionId, request: (path) => json(path) });
// Once, at load, and not polled: all this does is put a dot on the Updates tab
// so somebody finds out there is a new release without having gone looking.
// The daemon already asked GitHub; this asks the daemon.
json('/api/update/status').then((info) => {
  if (info) renderUpdate(info);
}).catch(() => {});
// Adding or removing a connection changes what every picker on the page can
// offer, so the whole lot is refreshed rather than the dialog patching them.
wireConnections();
wireSettings(() => loadProviders());
wireModeLifecycle();

// Last, and that ordering is load-bearing: this restores the mode you were
// last in and runs that mode's enter hook as it does. Anything registered
// after it is registered too late, which shows up as reopening the page on
// the studio and getting an empty one.
wireModes();

/* The companion, everywhere it has something to say.
 *
 * One creature, one state, and now eight places: the perch it lives on, the
 * sidebar where it doubles as the app's own mark, the middle of an empty
 * session, the composer where it says in words what it is doing, the voice
 * strip, and the three new modes — talking, live, and watching it work.
 * Approval bars grow another on demand. They are all driven from the same
 * object, so the animal is never in two moods at once, and choosing a
 * different one — or none — changes every one of them together. */
companion.mount($('#perch'), { place: 'above' });
companion.mount($('#brand-pet'), { place: 'below', scale: 2 });
companion.attach($('#hero-pet'), { scale: 6 });
companion.attach($('#buddy-pet'), { scale: 1 });
companion.attach($('#voice-pet'), { scale: 1 });

// The words half of the same signal, for anyone who would rather read it
// than watch a moth — and for a screen reader, which cannot watch anything.
companion.watch((what) => {
  $('#buddy-state').textContent = caption(what);
});

loadProviders();
loadSessions();

/* The session list, and how often it is asked for.
 *
 * This used to be an unconditional five-second `setInterval`, and it is still a
 * poll — because there is nothing else that can see it. The socket is attached
 * to the *open* session, so a session started in another tab, or one that
 * closed, or one that has begun waiting for an approval, is invisible from
 * here. Replacing this with an event would mean a second socket that carries
 * every session's events, which is a real subsystem rather than a client
 * change, and inventing one here would be a bigger change than the problem
 * earns. So the poll stays, and what changed is what it costs.
 *
 * Three things used to happen twelve times a minute regardless: a fetch, a
 * rebuild of every row in the sidebar, and the relative times being
 * recomputed from scratch. The last two no longer happen at all — the
 * fingerprint in `loadSessions` means an unchanged list touches no DOM, and
 * the times are restated locally by `restateTimes`. So an idle tab now pays
 * one small request and nothing else, instead of a request plus a hundred
 * nodes thrown away and rebuilt to the same shape.
 *
 * And it stops entirely when the tab is hidden. A tab nobody is looking at
 * does not need a sidebar up to date, and on a laptop that is a request every
 * five seconds for as long as the window is in the background — the exact
 * shape of cost that is invisible on the machine and real on the battery. The
 * refresh on becoming visible is the same one, coalesced, so returning to a tab
 * shows the truth rather than the state it was left in. */
let sessionTimer = null;
let clockTimer = null;
let sessionRefreshQueued = false;

/* Never throws. The daemon can be down, and this is the thing that finds out,
   so it has to survive every second it is. */
function pollSessions() {
  if (document.hidden) return;
  // An event on the open session's socket has already asked for a refresh and
  // it is still sitting in the debounce. Asking again now would fetch the same
  // answer twice and let the two race to be the last writer, so this one stands
  // down — the pending refresh is the one that is about to run anyway.
  if (refreshTimer !== null) return;
  loadSessions().catch(() => {});
}

function startSessionPoll() {
  if (sessionTimer === null) sessionTimer = setInterval(pollSessions, 5000);
  /* The relative times move on their own, with no request at all. Thirty
     seconds is the finest any of them can read — `ago` says "12m" across a
     span of twelve minutes and thirty seconds — so a faster tick would be
     doing work that cannot change what is on screen. */
  if (clockTimer === null) clockTimer = setInterval(restateTimes, 30000);
}

function stopSessionPoll() {
  if (sessionTimer !== null) {
    clearInterval(sessionTimer);
    sessionTimer = null;
  }
  if (clockTimer !== null) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopSessionPoll();
    return;
  }
  /* Queued rather than immediate: `visibilitychange` fires before the browser
     has finished bringing the tab back, and several can arrive in a row as the
     focus moves between windows. One refresh, once the dust settles. */
  if (sessionRefreshQueued) return;
  sessionRefreshQueued = true;
  setTimeout(() => {
    sessionRefreshQueued = false;
    pollSessions();
    startSessionPoll();
  }, 250);
});

// The same "the daemon is back" signal, arriving on a fetch rather than on a
// timer. `onReachable` in dom.js already reports the transition, and the poll
// used to be the reason that mattered; now it is a bonus, because the poll is
// no longer what discovers it.
onReachable((ok) => { if (ok) pollSessions(); });

startSessionPoll();

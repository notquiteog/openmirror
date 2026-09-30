/* The transcript: a daemon event, rendered.
 *
 * A port of the half of `openmirror/static/app.js` that draws the conversation,
 * with one presentation decision changed and everything else kept. The change
 * is size. A browser tab is 1200px wide and a side panel is 300, and the
 * browser client can afford a card per tool call with its output inside it; a
 * panel that does the same is full of tool output within two turns and is then
 * useless. So:
 *
 *   - One line per tool call: the verb, what it was for, the risk, the time.
 *   - The result is *retained* but not *built*. Nothing goes into the DOM until
 *     somebody opens the card, and even then it is bounded. A 2000-line file
 *     read is a string in a closure until it is clicked.
 *   - Live output never enters the DOM at all. The head line carries the first
 *     line of it and a running count, which is what `tui.py` does, and it is
 *     what makes a four-minute build cost 200 bytes rather than 4MB.
 *
 * The pure half is above the line: `eventLines` turns any event of the
 * `AgentEvent` union into the lines it should say, including the default branch
 * that says `unknown event: <type>` for anything it has not heard of. Every
 * node below is built out of those strings, so the wording exists once and
 * `events.test.js` can assert it without a DOM.
 *
 * Security, and it is the rule this file is written around: every string the
 * agent produced -- a model response, a tool result, a file path, a hook
 * command, a session title -- reaches the page as a text node. There is no
 * `innerHTML` in this directory. A transcript is whatever the agent read, and
 * a transcript rendered as markup is the repository executing itself inside
 * the editor.
 */

import { $, button, clear, el, flat, human, say, took } from './dom.js';
import { cancel as cancelStream, flush as flushStream, queue } from './stream.js';

/* The mapping of a tool's own name to the thing it did, verbatim from
 * `app.js`'s `VERBS`. The tool name is exact and useless at a glance; "Read"
 * followed by the path is what somebody skimming is actually reading for.
 * Anything not listed falls back to its own name in monospace, which is honest
 * about being a tool this client has never heard of. */
export const VERBS = {
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
  hr: 'HR',
  ask_user: 'Asked you',
};

/** How a tool call reads as a phrase, falling back to the tool's own name. */
export function verbFor(name) {
  return VERBS[String(name || '')] || '';
}

/* Which argument is worth putting on the line, most specific first. Printed
 * for whichever is there, which was tried and showed `run_in_background=false`
 * more often than the path. From `tui.py`'s `ARG_KEYS`. */
const ARG_KEYS = ['path', 'file_path', 'notebook', 'command', 'cmd', 'url', 'query', 'pattern', 'glob', 'text'];

/** A shell command arrives as a list, and `['git', 'status']` is a worse line
 *  to read than `git status`. */
function renderArg(value) {
  if (Array.isArray(value)) {
    return value.map((part) => renderArg(part)).filter(Boolean).join(' ');
  }
  if (value && typeof value === 'object') {
    return flat(JSON.stringify(value), 80);
  }
  return flat(value, 80);
}

/**
 * Whether an argument says nothing.
 *
 * `null`, an empty string, an empty list, an empty object, and the three ways a
 * tool says "here" -- `.`, `./` and `/`. `tui.py` compares the same set, and it
 * has to be an explicit test rather than truthiness: a missing key is not the
 * same as `false`, and treating `run_in_background: false` as trivial would
 * silently drop a real argument from every shell call.
 */
export function isTrivialArgument(value) {
  if (value === null || value === undefined) {
    return true;
  }
  if (typeof value === 'string') {
    return ['', '.', './', '/', '~'].includes(value.trim());
  }
  if (Array.isArray(value)) {
    return value.length === 0;
  }
  if (typeof value === 'object') {
    return Object.keys(value).length === 0;
  }
  return false;
}

/**
 * What this call is doing, in a few words.
 *
 * `summary` first, because it is written by the tool that knows which of its
 * arguments matters. Falling back to the arguments is what lets a client that
 * has never heard of a tool still say something true about it.
 */
export function describeCall(call) {
  const said = String((call && call.summary) || '').trim();
  if (said) {
    return flat(said, 90);
  }
  let args = call && call.arguments;
  if (args && typeof args === 'object' && !Array.isArray(args)) {
    for (const key of ARG_KEYS) {
      if (!isTrivialArgument(args[key])) {
        return flat(renderArg(args[key]), 90);
      }
    }
    args = Object.fromEntries(
      Object.entries(args).filter(([, value]) => !isTrivialArgument(value)),
    );
  }
  if (args && typeof args === 'object' && Object.keys(args).length) {
    return flat(JSON.stringify(args), 90);
  }
  return '';
}

/** The whole tool line as one string, for the transcript and the tests. */
export function toolLine(call) {
  const name = String((call && call.name) || 'tool');
  const verb = verbFor(name);
  return [verb || name, describeCall(call)].filter(Boolean).join('  ').trim();
}

/** The pieces of the compact head. The DOM builds a node per piece. */
export function toolHead(call) {
  const name = String((call && call.name) || 'tool');
  return {
    name,
    known: Boolean(VERBS[name]),
    verb: verbFor(name),
    summary: describeCall(call),
    risk: String((call && call.risk) || 'read'),
  };
}

/** The server refuses to remember a destructive call, so do not offer it. */
export function rememberable(risk) {
  return String(risk || '') !== 'destructive';
}

/** Added and removed lines, counted off the diff the daemon already sends. */
export function countDiff(text) {
  let add = 0;
  let del = 0;
  for (const line of String(text || '').split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) {
      add++;
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      del++;
    }
  }
  return { add, del };
}

/** The first line of something, which is what a one-line head can show. */
export function firstLine(text) {
  for (const line of String(text || '').split('\n')) {
    if (line.trim()) {
      return line.trim();
    }
  }
  return '';
}

/** `unknown event: <type>`, and the one line that must never be swallowed. */
export function unknownEvent(type) {
  const name = String(type === undefined || type === null ? '' : type).trim();
  return `unknown event: ${name || '(no type)'}`;
}

/**
 * Background work, as the sentence that says how it ended.
 *
 * `flat` on the way out, which is not tidiness: a task with no id and no label
 * is a malformed event rather than an impossible one, and a status line with
 * two spaces in the middle of it reads as a rendering bug.
 */
export function taskEnded(task) {
  const what = task && task.kind === 'agent' ? 'Background agent' : 'Background command';
  const id = String((task && task.id) || '');
  const label = String((task && task.label) || '');
  if (task && task.status === 'stopped') {
    return flat(`${what} ${id} was stopped: ${label}`, 120);
  }
  if (task && task.kind === 'shell') {
    return flat(`${what} ${id} exited with code ${task.exit_code}: ${label}`, 120);
  }
  return flat(`${what} ${id} ${task && task.status === 'done' ? 'finished' : 'failed'}: ${label}`, 120);
}

/** Where the conversation was summarised to, or cleared. */
export function compactedText(event) {
  if (event && event.reason === 'cleared') {
    return 'context cleared - the agent starts afresh from here';
  }
  const how = event && event.reason === 'automatic' ? 'compacted to make room' : 'compacted';
  const size = event && event.tokens_before ? `, about ${human(event.tokens_before)} tokens` : '';
  return `${how}: ${(event && event.messages_before) || 0} messages${size}, now a summary`;
}

/**
 * Only what actually moved.
 *
 * One `policy.changed` carries the mode, the thinking level and the model, and
 * a sentence about the wrong one is a true sentence that makes somebody think
 * something changed when nothing did. `app.js` compares against what the panel
 * already shows, and so does this.
 */
export function policyNotices(before, event) {
  const was = before || {};
  const ev = event || {};
  const out = [];
  if (was.mode !== ev.mode) {
    out.push(`Approval is now: ${ev.policy || ev.mode}.`);
  }
  if ((was.effort || null) !== (ev.effort || null)) {
    out.push(`Thinking is now: ${ev.effort || "the model's own default"}.`);
  }
  if (ev.model && was.model !== ev.model) {
    out.push(`Answering with ${ev.model}${ev.provider ? ` on ${ev.provider}` : ''} from now on.`);
  }
  return out;
}

/**
 * What one event says, as lines.
 *
 * The whole union in `openmirror/protocol/agent.py`, plus the three the
 * daemon sends that are not in it (`text.flush` is the browser client's own
 * batching hint, `turn.queued` is what a message sent during a turn comes back
 * as, and `pong` is a keepalive). The default branch is the last line of the
 * function and it is the point: a new event the daemon adds shows up in the
 * panel as one line saying so, rather than vanishing.
 */
export function eventLines(event) {
  const ev = event || {};
  switch (ev.type) {
    case 'session.started':
      return ev.policy ? [{ cls: 'meta', text: flat(ev.policy, 160) }] : [];

    case 'turn.started':
      return ev.text ? [{ cls: 'user', text: ev.text }] : [];

    case 'text.delta':
      return [{ cls: 'delta', text: String(ev.text || '') }];

    case 'thinking.delta':
      return [{ cls: 'thinking', text: String(ev.text || '') }];

    case 'text.flush':
    case 'tool.started':
    case 'pong':
      return [];

    case 'tool.proposed': {
      const lines = [{ cls: 'tool', text: toolLine(ev.call || {}) }];
      if (ev.needs_approval) {
        lines.push({ cls: 'warn', text: 'needs your approval' });
      }
      return lines;
    }

    case 'tool.output.delta':
      return firstLine(ev.text) ? [{ cls: 'tool-out', text: firstLine(ev.text) }] : [];

    case 'tool.completed': {
      const result = ev.result || {};
      const bits = [];
      const body = firstLine(result.content) || summariseDisplay(result.display);
      if (!result.ok) {
        bits.push(`failed: ${body || 'no detail'}`);
      } else if (body) {
        bits.push(body);
      } else {
        bits.push('ok');
      }
      if (result.duration_ms) {
        bits.push(took(result.duration_ms));
      }
      if (result.truncated) {
        bits.push('truncated');
      }
      // ` - ` and not two spaces: `flat` collapses runs of whitespace, and a
      // separator the formatter eats is a separator that is not there.
      return [{ cls: result.ok ? 'tool-ok' : 'tool-err', text: flat(bits.join(' - '), 140) }];
    }

    case 'tool.denied': {
      const why = String(ev.reason || '').trim();
      return [{ cls: 'tool-err', text: `not run${why ? `: ${flat(why, 90)}` : ''}` }];
    }

    case 'question.asked': {
      const lines = [{ cls: 'question', text: flat(ev.question, 200) }];
      for (const option of ev.options || []) {
        lines.push({ cls: 'option', text: flat(option, 80) });
      }
      return lines;
    }

    case 'turn.queued':
      return [{ cls: 'meta', text: `held - it runs when this turn finishes (${ev.waiting || 0} waiting)` }];

    case 'turn.completed': {
      if (ev.stop_reason === 'interrupted') {
        return [{ cls: 'meta', text: 'Interrupted.' }];
      }
      if (ev.stop_reason === 'max_steps') {
        return [{ cls: 'error', text: 'Stopped: too many steps.' }];
      }
      if (ev.stop_reason === 'error') {
        return [{ cls: 'error', text: 'The turn ended in an error.' }];
      }
      return [];
    }

    case 'error':
      return [{ cls: 'error', text: flat(ev.message, 200) }];

    case 'policy.changed':
    case 'hook.approval':
      // `hook.approval` is a banner rather than a transcript line, and the
      // mode picker already says the mode. Both are rendered elsewhere, and
      // both are still handled: the default branch is only for events nothing
      // knows about.
      return [];

    case 'task.updated':
      return [{ cls: 'meta', text: taskEnded(ev.task) }];

    case 'context.compacted':
      return [{ cls: 'meta', text: compactedText(ev) }];

    case 'session.ended':
      return [{ cls: 'meta', text: `session ended: ${flat(ev.reason || 'closed', 60)}` }];

    default:
      return [{ cls: 'unknown', text: unknownEvent(ev.type) }];
  }
}

/** A tool that returns structure rather than prose still gets a line. */
function summariseDisplay(display) {
  if (!display || typeof display !== 'object') {
    return '';
  }
  for (const key of ['summary', 'path', 'message']) {
    if (display[key]) {
      return flat(display[key], 80);
    }
  }
  const scalars = [];
  for (const [key, value] of Object.entries(display)) {
    if (['string', 'number', 'boolean'].includes(typeof value) && String(value).slice(0, 40)) {
      scalars.push(`${key}=${value}`);
    }
    if (scalars.length === 3) {
      break;
    }
  }
  return flat(scalars.join(', '), 80);
}

/* The receipt at the end of a turn. Said only when both ends of it are known:
 * a made-up duration is worse than none. */
export function receipt(event, startedAt) {
  const from = Number(startedAt) || 0;
  const to = Number(event && event.at) || 0;
  if (!from || !to || to <= from) {
    return [];
  }
  return [{ cls: 'meta', text: `Worked for ${took((to - from) * 1000)}` }];
}

/* ========================================================================== *
 * The DOM half.
 * ========================================================================== */

/** How much of a tool's output is kept while it runs. */
const MAX_LIVE = 6000;
/** How much of a result is built when somebody opens the card. */
const MAX_DETAIL = 4000;
/** How many lines of a diff are built when somebody opens the card. */
const MAX_DIFF = 400;
/** A transcript that has run all afternoon is a lot of nodes in a small panel. */
const KEEP_NODES = 600;

const TODO_MARKS = { todo: '[ ]', doing: '[~]', done: '[x]', dropped: '[-]' };

/** The whole list, for the inside of a `todo` tool's card. The marks are ASCII
 *  notation rather than glyphs, which is honest about being a checklist. */
export function todoLines(items) {
  return (items || []).map((item) => ({
    state: String(item.state || 'todo'),
    mark: TODO_MARKS[item.state] || TODO_MARKS.todo,
    text: String(item.text || ''),
  }));
}

function fillTodos(list, items) {
  for (const item of todoLines(items)) {
    const li = el('li', item.state);
    li.append(el('span', 'mark', item.mark), el('span', 'text', item.text));
    list.appendChild(li);
  }
  return list;
}

/**
 * The transcript, and everything that hangs off it.
 *
 * @param {object} ctx
 * @param {function} ctx.send      one frame out
 * @param {function} ctx.onInfo    the chips changed
 * @param {function} ctx.onBusy    a turn started or finished
 * @param {function} ctx.onContext the context report
 * @param {function} ctx.onHook    a project hook wants to run
 */
export function createTranscript(ctx) {
  const context = ctx || {};
  const view = $('#transcript');
  const todos = $('#todos');
  const state = {
    tools: new Map(),
    /** callId -> {call, output, first, count, built, open, result, denied}. */
    decided: new Set(),
    turnNode: null,
    turnStart: 0,
    echo: null,
    info: {},
    added: 0,
    removed: 0,
  };

  /* ------------------------------------------------------------ scrolling */

  let followWanted = false;
  let followScheduled = false;

  function atBottom() {
    return view.scrollHeight - view.scrollTop - view.clientHeight < 80;
  }

  /* One scroll per frame rather than one per append: appending a card
   * invalidates layout, and a read straight after it forces a second layout in
   * the same frame. The decision is therefore taken before the append and the
   * write lands on the next animation frame. */
  function scrollToTail() {
    followWanted = true;
    if (followScheduled) {
      return;
    }
    followScheduled = true;
    requestAnimationFrame(() => {
      followScheduled = false;
      if (!followWanted) {
        return;
      }
      followWanted = false;
      view.scrollTop = view.scrollHeight;
    });
  }

  function append(node) {
    // Only follow the tail if the reader was already at it. Yanking somebody
    // back down while they are reading earlier output is the most irritating
    // thing a streaming log can do.
    const follow = atBottom();
    view.appendChild(node);
    if (follow) {
      scrollToTail();
    }
    prune();
    settle();
    return node;
  }

  /* An empty panel is a different window: what it is and how to start it. The
   * moment there is anything to read, that goes and the transcript takes the
   * room. */
  function settle() {
    const empty = !view.querySelector('.turn, .tool, .question, .note, .compacted, .todos-box, .unknown-line');
    document.body.classList.toggle('blank', empty);
    const hero = $('#hero');
    if (hero) {
      hero.hidden = !empty;
    }
  }

  /* Oldest completed turns go, and only ever as a whole turn: half a turn is
   * worse than a full one, and a card whose output was dropped while its head
   * stayed is a broken-looking card. Never while the reader is away from the
   * tail, and never the turn in progress. */
  function prune() {
    const kids = [...view.children];
    if (kids.length <= KEEP_NODES || !atBottom()) {
      return;
    }
    let spare = kids.length - KEEP_NODES;
    for (const node of kids) {
      if (spare <= 0) {
        break;
      }
      if (node.id === 'hero' || node === state.turnNode) {
        continue;
      }
      if (node.classList.contains('turn') && !node.classList.contains('user')) {
        const at = kids.indexOf(node);
        const later = kids.findIndex((other, index) => index > at && other.classList.contains('turn'));
        const stop = later === -1 ? kids.length : later;
        for (let i = at; i < stop && spare > 0; i++) {
          cancelStream(kids[i]);
          kids[i].remove();
          spare--;
        }
      }
    }
    settle();
  }

  /* -------------------------------------------------------------- messages */

  /* The same message twice in a row collapses into a count, because anything
   * that can repeat should not be able to bury the transcript under copies of
   * itself. */
  function note(cls, text) {
    const last = view.lastElementChild;
    if (last && last.classList.contains('note') && last.dataset.text === text) {
      const seen = Number(last.dataset.count || 1) + 1;
      last.dataset.count = String(seen);
      last.textContent = `${text}  (x${seen})`;
      return last;
    }
    const node = el('div', `note ${cls || ''}`.trim(), text);
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
    if (!state.turnNode) {
      assistantTurn();
    }
    const body = state.turnNode.querySelector('.body');
    queue(body, text, (node, chunk) => {
      node.textContent += chunk;
      if (atBottom()) {
        scrollToTail();
      }
    });
  }

  /* Reasoning is separate from the answer so it can be folded away, which in a
   * panel is most of the point: it is the longest text in the transcript and
   * the least often re-read. */
  function appendThinking(text) {
    if (!state.turnNode) {
      assistantTurn();
    }
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

  /* ------------------------------------------------------------ tool cards */

  function cardFor(call, host) {
    const callId = String((call && call.id) || '');
    const existing = state.tools.get(callId);
    if (existing) {
      return existing;
    }
    const head = toolHead(call || {});
    const card = el('div', 'tool');
    const line = el('button', 'head');
    line.type = 'button';
    line.setAttribute('aria-expanded', 'false');

    const sum = el('span', 'sum', head.summary);
    const tail = el('span', 'tail');
    const risk = el('span', 'risk', head.risk);
    const spent = el('span', 'took', '');
    const chev = el('span', 'chev', 'v');
    tail.append(risk, spent, chev);
    line.append(
      el('span', head.known ? 'verb' : 'name', head.known ? head.verb : head.name),
      sum,
      tail,
    );
    if (head.summary) {
      line.title = `${head.name}: ${head.summary}`;
    }
    card.appendChild(line);

    const record = {
      card,
      head,
      sum,
      spent,
      output: '',
      first: '',
      count: 0,
      result: null,
      built: false,
      open: false,
      id: callId,
    };
    // The whole line is the disclosure control: a chevron you have to hit
    // exactly is a worse target than the line it sits on.
    line.onclick = () => expand(record, !record.open);
    state.tools.set(callId, record);
    (host || view).appendChild(card);
    if (!host) {
      scrollToTail();
    }
    return record;
  }

  /* Opening a card is the only thing that builds its output, and even then it
   * is bounded: `MAX_DETAIL` characters of a result, `MAX_DIFF` lines of a
   * diff, the tail of whatever streamed. This is the decision the panel exists
   * to make. */
  function expand(record, on) {
    record.open = on;
    record.card.classList.toggle('open', on);
    const line = record.card.querySelector('.head');
    if (line) {
      line.setAttribute('aria-expanded', String(on));
    }
    if (on && !record.built) {
      record.built = true;
      record.card.appendChild(buildDetail(record));
    }
  }

  function buildDetail(record) {
    const body = el('div', 'body');
    if (record.output) {
      body.appendChild(el('pre', 'out', tailOf(record.output)));
    }
    const result = record.result;
    if (!result) {
      return body;
    }
    const diff = result.display && result.display.diff;
    const items = result.name === 'todo' && result.ok && result.display ? result.display.items : null;
    if (items) {
      body.appendChild(fillTodos(el('ol', 'todos-box'), items));
    } else if (diff) {
      body.appendChild(renderDiff(diff));
    } else if (result.content) {
      body.appendChild(el('pre', result.ok ? 'out' : 'out err', clip(result.content, MAX_DETAIL)));
    }
    if (result.display && result.display.image) {
      body.appendChild(renderShot(result.display));
    }
    if (result.truncated) {
      body.appendChild(el('p', 'note', 'The tool truncated this before the model saw it either.'));
    }
    return body;
  }

  function renderDiff(text) {
    const box = el('div', 'diff');
    const lines = String(text || '').split('\n');
    for (const line of lines.slice(0, MAX_DIFF)) {
      let cls = '';
      if (line.startsWith('+') && !line.startsWith('+++')) {
        cls = 'add';
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        cls = 'del';
      } else if (line.startsWith('@@')) {
        cls = 'hunk';
      }
      box.appendChild(el('span', cls, line));
    }
    if (lines.length > MAX_DIFF) {
      box.appendChild(el('span', 'hunk', `... ${lines.length - MAX_DIFF} more lines`));
    }
    return box;
  }

  function renderShot(display) {
    const box = el('div', 'shot');
    const img = document.createElement('img');
    img.src = `data:${display.media_type || 'image/png'};base64,${display.image}`;
    img.alt = 'what the agent saw';
    img.loading = 'lazy';
    box.appendChild(img);
    return box;
  }

  /* A running tool: the head line carries the first line of the output and a
   * count, and nothing is written into the panel. A build that emits 40k lines
   * costs 200 bytes and one text node. */
  function toolOutput(callId, text, stream) {
    const record = state.tools.get(String(callId || ''));
    if (!record) {
      return;
    }
    const chunk = String(text || '');
    record.count += 1;
    if (!record.first) {
      record.first = firstLine(chunk);
      if (record.first) {
        record.sum.textContent = record.first;
        record.card.querySelector('.head').title = `${record.head.name}: ${record.head.summary}`;
      }
    }
    record.output = (record.output + chunk).slice(-MAX_LIVE);
    record.card.classList.add('live');
    if (stream === 'stderr') {
      record.card.classList.add('stderr');
    }
    const spent = record.spent;
    if (spent) {
      spent.textContent = record.count > 1 ? `${record.count} lines` : 'streaming';
    }
  }

  function toolDone(result) {
    const record = state.tools.get(String((result && result.id) || ''));
    if (!record) {
      return;
    }
    // Anything still queued for this card has to land before the card is
    // measured and folded, or the last lines of a build are missing from the
    // card that keeps them.
    flushStream();
    record.result = result;
    // A finished call must not still be offering Allow and Deny. This shows up
    // on replay, where the proposal is re-rendered long after it was decided.
    record.card.querySelector('.decide')?.remove();
    if (record.open) {
      record.built = false;
      const old = record.card.querySelector('.body');
      if (old) {
        old.remove();
      }
      expand(record, true);
    }
    if (!result.ok) {
      record.card.classList.add('failed');
    }
    const diff = result.display && result.display.diff;
    if (result.ok && diff) {
      const counts = countDiff(diff);
      state.added += counts.add;
      state.removed += counts.del;
    }
    if (result.name === 'todo' && result.ok && result.display) {
      showTodos(result.display.items);
    }
    if (result.name === 'agent' && result.display && result.display.tool_calls !== undefined) {
      const n = Number(result.display.tool_calls) || 0;
      record.sum.textContent = `${record.head.summary}  -  ${n} tool call${n === 1 ? '' : 's'}`;
    } else if (!result.ok && result.content) {
      record.sum.textContent = firstLine(result.content) || record.sum.textContent;
    }
    record.card.classList.remove('live');
    if (record.spent && result.duration_ms > 400) {
      record.spent.textContent = took(result.duration_ms);
    }
    // Anything that failed, or changed a file, or has a picture in it wants
    // looking at. A successful read collapses to its line: it is in the
    // transcript so you can check, not so you have to.
    const interesting = !result.ok || Boolean(diff || (result.display && result.display.image));
    if (interesting && !record.open) {
      expand(record, true);
    }
    if (result.name === 'agent') {
      // A delegation is one line in the transcript; what it did is inside it,
      // and its report arrives last so it is the last thing in the card.
      expand(record, true);
    }
  }

  function denied(event) {
    const record = state.tools.get(String(event.call_id || ''));
    if (!record) {
      return;
    }
    record.card.classList.add('denied');
    record.card.querySelector('.decide')?.remove();
    const why = String(event.reason || '').trim();
    record.sum.textContent = `not run${why ? `: ${flat(why, 80)}` : ''}`;
  }

  /* The approval. Allow and Deny, the tool, and one line of what it would do.
   *
   * Deny comes first in the tab order and Deny is what Escape-adjacent
   * reflexes reach first, deliberately: a mis-click on a panel that is about to
   * run a shell command should cost nothing. The buttons are *not* disabled on
   * the first click -- the host queues a second answer if one is sent, and a
   * disabled button that re-enables on an error is a button that invites the
   * second click. The bar is removed instead, and the call is remembered as
   * decided so a replayed proposal cannot put it back. */
  function decide(record, call) {
    const bar = el('div', 'decide');
    bar.appendChild(el('div', 'why', call.summary || describeCall(call) || call.name));
    bar.appendChild(el('div', 'risk-line', `risk: ${call.risk} - ${call.name}`));

    const actions = el('div', 'acts');
    const deny = button('btn deny', 'Deny', 'Refuse this call (default)');
    const allow = button('btn allow', 'Allow', 'Let it run once');

    const remember = el('label', 'remember');
    const box = el('input');
    box.type = 'checkbox';
    const rememberLabel = el('span', '', "don't ask for this exact call again");
    remember.append(box, rememberLabel);
    if (!rememberable(call.risk)) {
      // The daemon refuses to remember a destructive call. Say so here rather
      // than offering a checkbox that silently does nothing.
      box.disabled = true;
      rememberLabel.textContent = 'destructive calls are always confirmed';
    }

    const done = (frame) => {
      if (state.decided.has(record.id)) {
        return;
      }
      state.decided.add(record.id);
      bar.remove();
      if (record.spent) {
        record.spent.textContent = 'sent';
      }
      context.send(frame);
    };

    deny.onclick = () => done({ t: 'deny', callId: record.id, reason: 'declined in the panel' });
    allow.onclick = () => done({ t: 'approve', callId: record.id, remember: Boolean(box.checked) });

    actions.append(deny, allow);
    bar.append(remember, actions);
    record.card.appendChild(bar);
    expand(record, true);
  }

  /* ------------------------------------------------------------- questions */

  function question(event, host) {
    const card = el('div', 'question');
    const lines = eventLines(event);
    card.appendChild(el('div', 'q', lines[0].text));
    let settled = false;

    const answer = (text) => {
      if (settled) {
        return;
      }
      settled = true;
      context.send({ t: 'answer', questionId: String(event.question_id || ''), answer: String(text) });
      card.querySelector('.opts')?.remove();
      card.querySelector('form')?.remove();
      card.appendChild(el('div', 'note', `You answered: ${text}`));
    };

    if (event.options && event.options.length) {
      const opts = el('div', 'opts');
      for (const option of event.options) {
        const pick = button('btn ghost small', flat(option, 60), flat(option, 200));
        pick.onclick = () => answer(option);
        opts.appendChild(pick);
      }
      card.appendChild(opts);
    }

    const form = el('form', 'answer');
    const input = el('input');
    input.type = 'text';
    input.placeholder = 'or write your own answer';
    input.setAttribute('aria-label', 'your answer');
    const go = button('btn allow small', 'Answer', 'Send this answer');
    form.onsubmit = (submitEvent) => {
      submitEvent.preventDefault();
      if (input.value.trim()) {
        answer(input.value.trim());
      }
    };
    form.append(input, go);
    card.appendChild(form);
    (host || view).appendChild(card);
    if (!host) {
      scrollToTail();
      settle();
    }
    return card;
  }

  /* ----------------------------------------------------------------- to-dos */

  /* Pinned above the composer: which step is it on is the question somebody
   * watching a long turn keeps asking, and in a narrow panel the transcript is
   * rarely showing the part that answers it. The step and the count only --
   * the whole list is one click away in the `todo` tool's own card, and a list
   * of fourteen items above the box is a list that has pushed the conversation
   * off the screen. */
  function showTodos(items) {
    if (!todos) {
      return;
    }
    const list = items || [];
    todos.hidden = !list.length;
    clear(todos);
    if (!list.length) {
      return;
    }
    const done = list.filter((item) => item.state === 'done').length;
    const doing = list.find((item) => item.state === 'doing');
    todos.appendChild(el('div', 'todos-head', doing ? flat(doing.text, 60) : 'To-do'));
    todos.appendChild(el('div', 'todos-count', `${done} of ${list.length} done`));
  }

  /* ------------------------------------------------------------ subagents */

  /* A subagent's events arrive on the same socket as everything else, marked
   * with the call that started it, and they nest inside that call's card. The
   * delegation is one line in the transcript; what the agent did is inside it,
   * and it opens by itself when the subagent needs a person -- a question
   * inside a folded card is a question nobody sees. */
  function agentLog(callId) {
    const record = state.tools.get(String(callId || ''));
    if (!record) {
      return null;
    }
    let log = record.card.querySelector('.agent-log');
    if (!log) {
      log = el('div', 'agent-log');
      record.card.appendChild(log);
    }
    return log;
  }

  function child(event) {
    const log = agentLog(event.agent);
    if (!log) {
      return;
    }
    const record = state.tools.get(String(event.agent));
    switch (event.type) {
      case 'text.delta': {
        let said = log.lastElementChild;
        if (!said || !said.classList.contains('said')) {
          said = log.appendChild(el('div', 'said'));
        }
        queue(said, event.text, (node, chunk) => {
          node.textContent += chunk;
        });
        break;
      }
      case 'tool.proposed': {
        const sub = cardFor(event.call || {}, log);
        if (event.needs_approval) {
          decide(sub, event.call || {});
          expand(record, true);
        }
        break;
      }
      case 'tool.output.delta':
        toolOutput(event.call_id, event.text, event.stream);
        break;
      case 'tool.completed':
        flushStream();
        toolDone(event.result);
        break;
      case 'tool.denied':
        denied(event);
        break;
      case 'question.asked':
        question(event, log);
        expand(record, true);
        break;
      case 'error':
        log.appendChild(el('div', 'note error', flat(event.message, 200)));
        break;
      default:
        for (const line of eventLines(event)) {
          if (line.cls !== 'delta' && line.cls !== 'thinking') {
            log.appendChild(el('div', `note ${line.cls}`, line.text));
          }
        }
    }
  }

  /* ------------------------------------------------------------- the events */

  function handle(event) {
    const ev = event || {};

    if (ev.agent) {
      // A subagent's, which nests inside the call that started it.
      child(ev);
      return;
    }

    switch (ev.type) {
      case 'session.started': {
        // The promise, spelled out, once: what this session may do without
        // asking. The mode picker carries the short form of the same fact.
        say($('#policy'), flat(ev.policy, 160));
        context.onInfo({ model: ev.model, root: ev.cwd, effort: ev.effort === undefined ? undefined : ev.effort });
        // The turn this panel thought was running is not: a `session.started`
        // during a replay means the daemon came back without it, and a stop
        // button for a turn that ended minutes ago is a control that lies.
        if (context.onBusy) {
          context.onBusy(false);
        }
        return;
      }

      case 'policy.changed': {
        for (const text of policyNotices(state.info, ev)) {
          note('meta', text);
        }
        context.onInfo({
          mode: ev.mode,
          effort: ev.effort === undefined ? undefined : ev.effort,
          model: ev.model === undefined ? undefined : ev.model,
          provider: ev.provider === undefined ? undefined : ev.provider,
        });
        if (ev.policy) {
          say($('#policy'), flat(ev.policy, 160));
        }
        return;
      }

      case 'turn.started': {
        state.turnNode = null;
        // The daemon's clock, not this one: a reattached panel replays turns
        // that ended hours ago, and timing them against `now` would report
        // every one of them as having taken a millisecond.
        state.turnStart = ev.at || 0;
        state.added = 0;
        state.removed = 0;
        context.onBusy(true);
        // Rendered from the event rather than on submit, so a live panel and a
        // reattached one show the same conversation. The local echo goes first,
        // or the words are on screen twice.
        if (ev.text) {
          const echoed = state.echo && state.echo.querySelector('.body');
          if (echoed && echoed.textContent === ev.text) {
            state.echo.remove();
          }
          state.echo = null;
          userTurn(ev.text);
        }
        return;
      }

      case 'text.delta':
        appendText(ev.text);
        return;

      case 'thinking.delta':
        appendThinking(ev.text);
        return;

      case 'tool.proposed': {
        state.turnNode = null;
        const call = ev.call || {};
        const record = cardFor(call);
        if (ev.needs_approval && !state.decided.has(record.id)) {
          decide(record, call);
        }
        return;
      }

      case 'tool.started':
        return;

      case 'tool.output.delta':
        toolOutput(ev.call_id, ev.text, ev.stream);
        return;

      case 'tool.completed':
        state.turnNode = null;
        toolDone(ev.result || {});
        return;

      case 'tool.denied':
        state.turnNode = null;
        denied(ev);
        return;

      case 'question.asked':
        state.turnNode = null;
        question(ev);
        return;

      case 'hook.approval':
        if (context.onHook) {
          context.onHook(ev.hook || {});
        }
        return;

      case 'turn.completed': {
        // The turn's last words are almost certainly still queued, and this is
        // the end of the turn, so anything buffered has to land first.
        flushStream();
        state.turnNode = null;
        context.onBusy(false);
        for (const line of receipt(ev, state.turnStart)) {
          note(line.cls, line.text);
        }
        state.turnStart = 0;
        if (state.added || state.removed) {
          note('meta', `Changed +${state.added} -${state.removed} this turn.`);
        }
        if (ev.context && context.onContext) {
          context.onContext(ev.context);
        }
        for (const line of eventLines(ev)) {
          note(line.cls === 'error' ? 'error' : 'meta', line.text);
        }
        return;
      }

      case 'error':
        flushStream();
        note('error', flat(ev.message, 200));
        context.onBusy(false);
        return;

      case 'session.ended':
        context.onBusy(false);
        break;

      case 'text.flush':
        flushStream();
        return;

      default:
        break;
    }

    /* Everything else, including everything this build has never heard of. A
     * new event the daemon adds shows up as one line saying so; that is the
     * correct behaviour and not a silent disappearance. */
    for (const line of eventLines(ev)) {
      if (line.cls === 'delta' || line.cls === 'thinking' || line.cls === 'user') {
        continue;
      }
      const node = note(line.cls === 'unknown' ? 'unknown' : '', line.text);
      if (line.cls === 'unknown') {
        node.classList.add('unknown-line');
      }
    }
  }

  /* ----------------------------------------------------------------- reset */

  function clearAll() {
    for (const node of [...view.children]) {
      cancelStream(node);
      if (node.id !== 'hero') {
        node.remove();
      }
    }
    for (const record of state.tools.values()) {
      cancelStream(record.card);
    }
    state.tools.clear();
    state.decided.clear();
    state.turnNode = null;
    state.turnStart = 0;
    state.echo = null;
    state.added = 0;
    state.removed = 0;
    state.info = {};
    showTodos([]);
    settle();
  }

  function setInfo(patch) {
    state.info = { ...state.info, ...patch };
  }

  function echo(text) {
    state.echo = userTurn(text);
    return state.echo;
  }

  settle();
  return { handle, clear: clearAll, setInfo, echo, note };
}

/* Not named `tail` because a card head has a `<span class="tail">` in it, and a
 * function and a node with the same name in one file is a trap. */
function tailOf(text) {
  const value = String(text || '');
  return value.length > MAX_LIVE ? `...${value.slice(-MAX_LIVE)}` : value;
}

function clip(text, limit) {
  const value = String(text || '');
  return value.length > limit ? `${value.slice(0, limit)}\n...` : value;
}

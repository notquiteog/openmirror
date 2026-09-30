/* Everything that is a fact about the conversation rather than the conversation:
 * the link to the daemon, the chips that say what the next thing you say will be
 * run under, the context meter, the hook banner, the empty state, and the
 * picker for stored conversations.
 *
 * The visual half of this is entirely VS Code's own variables. The panel is
 * rendered inside somebody's editor, and a stylesheet that carries its own
 * palette is a panel that is a slightly different colour from everything around
 * it in every theme -- and a visibly different colour from itself in high
 * contrast, where the whole point is that nothing is a guess. So there are no
 * colours in `panel.css`: only `--vscode-*` variables, and `--vscode-*`
 * variables with a fallback for the ones that are not defined in every theme.
 *
 * `PROTOCOL.md` says the meter is drawn "only when there is a denominator": a
 * bar at 40% on a model that will refuse the request is worse than no bar,
 * because somebody makes a decision about whether to keep working by looking at
 * it. The daemon's own `window_fraction` is the rule -- the compaction limit
 * wins over the model's window, because that is the number this session will
 * actually hit -- and it is ported rather than approximated.
 */

import { $, basename, button, clear, el, flat, human, say, when } from './dom.js';

/* 75% is a bar somebody should act on, 92% is one they should act on now. The
 * same two thresholds as `openmirror/static/context.js`, because a meter that
 * turns amber in one client and not in the other is a meter that cannot be
 * trusted from either. */
export const WARN_PERCENT = 75;
export const FULL_PERCENT = 92;

/**
 * How full it is, as a fraction, or null when the denominator is unknown.
 *
 * `limit` is the point this session summarises the conversation at and it wins
 * over the model's window: showing 30% of a 200k window when the conversation
 * is summarised at 100k is a bar that means nothing to the decision being made.
 */
export function windowFraction(tokens, limit, window) {
  const n = Number(tokens) || 0;
  const ceiling = Number(limit) > 0 ? Number(limit) : (Number(window) || 0);
  if (ceiling <= 0) {
    return null;
  }
  return Math.max(0, Math.min(1, n / ceiling));
}

/** The link indicator. Four states, and they are not the same thing. */
export function linkView(frame) {
  const state = String((frame && frame.state) || '');
  const message = String((frame && frame.message) || '');
  if (state === 'open') {
    return { state, cls: 'on', label: 'live', title: message || 'attached to the daemon', fatal: false };
  }
  if (state === 'failed') {
    // Stop trying, and say the host's own message: it already names the fix.
    return { state, cls: 'failed', label: 'offline', title: message, fatal: true };
  }
  if (state === 'closed') {
    // Transient, and the message already says when it will retry.
    return { state, cls: 'retrying', label: 'retrying', title: message, fatal: false };
  }
  return {
    state: state || 'connecting',
    cls: 'connecting',
    label: 'connecting',
    title: message || 'attaching to the daemon',
    fatal: false,
  };
}

/**
 * The context meter.
 *
 * The daemon names the totals `total_in` / `total_out` and `PROTOCOL.md` names
 * them `totalIn` / `totalOut`, and the host sends both -- so both are read here
 * rather than one of them being quietly assumed.
 */
export function contextView(report) {
  if (!report) {
    return { hidden: true };
  }
  const tokens = Number(report.tokens) || 0;
  const limit = Number(report.limit) || 0;
  const win = Number(report.window) || 0;
  const totalIn = Number(report.totalIn !== undefined ? report.totalIn : report.total_in) || 0;
  const totalOut = Number(report.totalOut !== undefined ? report.totalOut : report.total_out) || 0;
  if (!tokens && !totalIn) {
    return { hidden: true };
  }
  const given = typeof report.fraction === 'number' ? report.fraction : null;
  const fraction = given === null ? windowFraction(tokens, limit, win) : Math.max(0, Math.min(1, given));
  const hasBar = fraction !== null;
  const percent = hasBar ? Math.round(fraction * 100) : 0;

  let text;
  if (hasBar) {
    text = `${percent}%`;
  } else if (limit) {
    text = `${human(tokens)} of ${human(limit)} before it compacts`;
  } else {
    text = `${human(tokens)} in context`;
  }

  const parts = [`${human(tokens)} tokens in context`];
  if (win) {
    parts.push(`model window ${human(win)}`);
  }
  if (limit) {
    parts.push(`summarised at ${human(limit)}`);
  }
  if (totalIn || totalOut) {
    parts.push(`this session: ${human(totalIn)} in, ${human(totalOut)} out`);
  }
  parts.push(report.exact ? 'counted by the model' : 'estimated');

  return {
    hidden: false,
    hasBar,
    percent,
    level: !hasBar ? '' : percent >= FULL_PERCENT ? 'full' : percent >= WARN_PERCENT ? 'warn' : '',
    text,
    title: `${parts.join('  -  ')}\n\nClick to compact the conversation.`,
  };
}

/** One row of the resume picker. */
export function sessionRow(session, now) {
  const item = session || {};
  const turns = Number(item.turns) || 0;
  const ago = when(item.updated, now);
  return {
    id: String(item.id || ''),
    title: String(item.title || item.id || ''),
    where: basename(item.root),
    root: String(item.root || ''),
    when: ago,
    detail: [`${turns} turn${turns === 1 ? '' : 's'}`, ago, basename(item.root)].filter(Boolean).join('  -  '),
    label: `${item.title || item.id || 'conversation'}${ago ? `, ${ago} ago` : ''}`,
  };
}

/**
 * What an empty panel should say.
 *
 * A daemon that is not running gets the command that starts it, because a panel
 * that shows an empty box under a grey dot is a panel somebody has to guess
 * about. The `failed` message already names the fix and is shown verbatim, so
 * this does not paraphrase it -- it says what to do next, which is the thing a
 * message inside a status pill cannot.
 */
export function emptyState(frame, info) {
  const view = linkView(frame);
  const what = info && info.root ? basename(info.root) : '';
  if (view.fatal) {
    return {
      ask: what ? `openmirror is not running for ${what}.` : 'openmirror is not running.',
      sub: 'Start it with `openmirror serve`, then Try again.',
      reason: view.title,
      retry: true,
    };
  }
  if (!info || !info.session) {
    return {
      ask: 'No conversation yet.',
      sub: 'New conversation from the command palette, or pick a stored one.',
      reason: '',
    };
  }
  return {
    ask: what ? `What should we do in ${what}?` : 'What should we do?',
    sub: [info.model, info.mode].filter(Boolean).join('  -  '),
    reason: '',
  };
}

/* ========================================================================== *
 * The DOM half.
 * ========================================================================== */

/**
 * @param {object} ctx
 * @param {function} ctx.send      one frame out
 * @param {function} ctx.onCompact called when the meter is clicked
 */
export function createStatus(ctx) {
  const context = ctx || {};
  const state = { info: {}, frame: null, sessions: [] };
  const pill = $('#link');
  const meter = $('#context-meter');
  const bar = $('#context-bar');
  const text = $('#context-text');

  function paintLink() {
    const view = linkView(state.frame);
    pill.className = `pill ${view.cls}`;
    pill.textContent = view.label;
    pill.title = view.title;
    pill.setAttribute('aria-label', `daemon: ${view.label}`);
    document.body.classList.toggle('link-failed', view.fatal);
    paintEmpty();
  }

  /* The chips say what the next thing you say will be run under, because that
   * is what all three of them qualify. Kept beside the box for that reason. */
  function paintInfo() {
    const info = state.info;
    say($('#title'), info.title || 'openmirror');
    const root = $('#root-chip');
    if (info.root) {
      root.hidden = false;
      say(root.querySelector('span'), basename(info.root));
      root.title = info.root;
    } else {
      root.hidden = true;
    }
    const model = $('#model-chip');
    if (info.model) {
      model.hidden = false;
      // The provider rides along once it is known, because a chip saying
      // "claude-sonnet-5" beside one saying "anthropic" is two facts about one
      // thing, and after a `/model` switch the provider is the one that moved.
      const where = info.provider ? ` on ${info.provider}` : '';
      say(model, info.model + where);
      model.title = `answering with ${info.model}${where}`;
    } else {
      model.hidden = true;
    }
    const mode = $('#mode');
    if (mode && info.mode) {
      mode.value = info.mode;
    }
    const effort = $('#effort');
    if (effort && info.effort !== undefined && info.effort !== null) {
      effort.value = info.effort;
    }
    paintEmpty();
  }

  function paintContext(report) {
    const view = contextView(report);
    meter.hidden = view.hidden;
    if (view.hidden) {
      return;
    }
    if (bar) {
      bar.hidden = !view.hasBar;
      if (view.hasBar) {
        bar.firstElementChild.style.width = `${Math.max(1, view.percent)}%`;
        bar.firstElementChild.className = `fill ${view.level}`.trim();
      }
    }
    say(text, view.text);
    meter.title = view.title;
    meter.setAttribute('aria-label', meter.title);
  }

  function paintEmpty() {
    const view = emptyState(state.frame, state.info);
    say($('#hero-ask'), view.ask);
    say($('#hero-sub'), view.sub);
    const why = $('#hero-why');
    if (why) {
      why.textContent = view.reason;
      why.hidden = !view.reason;
    }
    // The way back from a link the host has given up on. It stopped retrying
    // on purpose — a wrong token or a stopped daemon will not fix itself — so
    // without this the only recovery was closing the tab, which throws away a
    // conversation that is still sitting on disk.
    const retry = $('#hero-retry');
    if (retry) {
      retry.hidden = !view.retry;
      retry.onclick = () => say({ t: 'reconnect' });
    }
  }

  /* A project hook wants to run, and has not been agreed to.
   *
   * Shown rather than buried, because a repository's hooks are somebody else's
   * code and the first time one would fire you are told what it would run. A
   * question nobody is asked is a policy nobody agreed to.
   *
   * Two buttons, because `PROTOCOL.md` has a `hook` frame and a banner whose
   * only answer lives in the command palette is a banner people learn to read
   * past. What a hook *cannot* do is worth keeping in view either way: it can
   * stop a call, never allow one, so agreeing to it only stops a call being
   * refused. */
  const hookBanner = $('#hooks');
  const askedHooks = new Set();

  function onHook(hook) {
    const command = String(hook.command_readable || hook.command || hook.event || 'a hook');
    if (askedHooks.has(command)) {
      return;
    }
    askedHooks.add(command);
    clear(hookBanner);
    hookBanner.appendChild(el('span', 'hook-ask', `This project wants to run a ${flat(hook.event, 30)} hook:`));
    hookBanner.appendChild(el('code', 'hook-command', flat(command, 160)));
    // Refusal first, and first *in* the DOM, so the keyboard's first stop is
    // the answer that does not run anything. The one thing a hook can do is
    // stop a call, and running a command from a repository because a banner
    // said so is the outcome this whole project is arranged against.
    const refuse = button('hook no', 'Refuse', 'Do not let this hook run');
    refuse.onclick = () => {
      askedHooks.delete(command);
      hookBanner.hidden = true;
      clear(hookBanner);
      say({ t: 'hook', command, allow: false });
    };
    const allow = button('hook yes', 'Allow it once', 'Let this hook run this once');
    allow.onclick = () => {
      askedHooks.delete(command);
      hookBanner.hidden = true;
      clear(hookBanner);
      say({ t: 'hook', command, allow: true });
    };
    hookBanner.appendChild(refuse);
    hookBanner.appendChild(allow);
    hookBanner.hidden = false;
  }

  /* The resume picker. A list rather than a dropdown because the interesting
   * part of each row is the sentence saying when it was had and what it was
   * about, and a dropdown has nowhere to put that. */
  const dialog = $('#sessions');
  const list = $('#sessions-list');

  function showSessions(items) {
    clear(list);
    const now = Date.now() / 1000;
    for (const item of items || []) {
      const row = sessionRow(item, now);
      const choice = button('session', row.title, row.detail);
      choice.setAttribute('aria-label', row.label);
      choice.appendChild(el('span', 'name', row.title));
      choice.appendChild(el('span', 'detail', row.detail));
      choice.onclick = () => {
        dialog.close();
        if (row.id) {
          context.send({ t: 'open', id: row.id });
        }
      };
      list.appendChild(choice);
    }
    if (!(items || []).length) {
      list.appendChild(el('p', 'meta', 'No stored conversations.'));
    }
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    }
  }

  const open = $('#open');
  if (open) {
    open.onclick = () => {
      // A question, so a frame is asked for: unlike `commands` and `context`,
      // nothing has just happened that would make the host push it.
      context.send({ t: 'contexts' });
    };
  }
  const close = $('#sessions-close');
  if (close) {
    close.onclick = () => dialog.close();
  }

  if (meter) {
    // The same as typing /compact, because it is the same thing and somebody
    // looking at 92% should not have to remember a command.
    meter.onclick = () => {
      if (context.onCompact) {
        context.onCompact();
      }
    };
  }

  paintLink();
  paintInfo();
  return {
    onFrame(frame) {
      state.frame = frame;
      paintLink();
    },
    onConfig(frame) {
      state.info = { ...state.info, ...frame, session: Boolean(frame && frame.sessionId) };
      paintInfo();
      return state.info;
    },
    onInfo(patch) {
      state.info = { ...state.info, ...patch };
      paintInfo();
      return state.info;
    },
    get info() {
      return state.info;
    },
    onContext: paintContext,
    onHook,
    showSessions,
    onSessions: showSessions,
    /* A new or resumed conversation. The meter, the hook answers and the
     * picker all belong to the conversation just left; the link does not,
     * because it is a fact about the daemon and not about any one of them. */
    onSessionChange() {
      state.info = {};
      askedHooks.clear();
      hookBanner.hidden = true;
      clear(list);
      meter.hidden = true;
      paintInfo();
    },
  };
}

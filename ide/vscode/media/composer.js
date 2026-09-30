/* The composer: the box you type in, and the buttons around it.
 *
 * Two decisions here are the ones the panel is judged on, and both come from
 * `app.js` rather than from taste.
 *
 * 1. **Sending is never blocked while a turn is running.** The daemon holds a
 *    message sent during a turn and starts it when the turn ends, and the
 *    moment you have something to add is usually *while* the agent is working
 *    on the first half of it. A panel that greys the button and refuses Enter
 *    throws away the thing you just typed and makes you watch for a gap to
 *    type in. So the send button stays live while busy, its title says what
 *    will happen, and `turn.queued` comes back as a line in the transcript so
 *    the message is visibly held rather than silently sitting there.
 *
 * 2. **Deny is the default-looking one.** The approval bar's buttons are *not*
 *    disabled after the first click. The host queues a frame when the socket is
 *    full, so a second click would be a second answer, and a button that
 *    re-enables on an error is a button that invites one. The bar is removed
 *    instead, which is what `events.js` does.
 *
 * The frame builders are exported and pure, because they are the seam between
 * this panel and the host and a frame with the wrong field name is a message
 * that silently does nothing.
 */

import { $, el, note } from './dom.js';

/* The levels the daemon takes, plus `default`, which hands the choice back to
 * the model. `app.js` builds this in script for the same reason: the six levels
 * live in the agent and a hard-coded picker that drifted would offer a level
 * that does not exist. */
export const EFFORT_LEVELS = [
  ['', 'think: default'],
  ['off', 'think: off'],
  ['low', 'think: low'],
  ['medium', 'think: medium'],
  ['high', 'think: high'],
  ['xhigh', 'think: xhigh'],
  ['max', 'think: max'],
];

/** The approval modes, in the order the daemon lists them in its own docs. */
export const MODES = [
  ['read_only', 'read only'],
  ['plan', 'plan first'],
  ['ask', 'ask first'],
  ['auto_edit', 'auto edit'],
  ['trusted', 'trusted'],
  ['unrestricted', 'unrestricted'],
];

/**
 * The `submit` frame.
 *
 * `attachments` is omitted when empty rather than sent as `[]`, which is the
 * same thing to the daemon and one less key in every message somebody types
 * into a panel with no picture on it.
 */
export function submitFrame(text, attachments) {
  const body = String(text === undefined || text === null ? '' : text);
  const pictures = Array.isArray(attachments) ? attachments : [];
  if (!pictures.length) {
    return { t: 'submit', text: body };
  }
  return { t: 'submit', text: body, attachments: pictures };
}

/** The `approve` frame. `remember` is scoped by the daemon to this exact call. */
export function approvalFrame(callId, remember) {
  return { t: 'approve', callId: String(callId || ''), remember: Boolean(remember) };
}

/** The `deny` frame. The reason goes back to the model as the tool's result, so
 *  it can try another way instead of proposing the same thing again. */
export function denyFrame(callId, reason) {
  return { t: 'deny', callId: String(callId || ''), reason: String(reason || '') };
}

/** The `answer` frame, for an `ask_user` question. */
export function answerFrame(questionId, answer) {
  return { t: 'answer', questionId: String(questionId || ''), answer: String(answer || '') };
}

/**
 * Whether there is anything to send.
 *
 * A picture is a message on its own. "Have a look at this" with a screenshot
 * under it is a complete thing to say, and refusing to send it because there are
 * no words would make the feature work only where it is least needed -- so the
 * second argument is a count rather than a list, because all it is ever asked
 * is whether there is one.
 */
export function canSend(text, attachmentCount) {
  return Boolean(String(text || '').trim()) || (Number(attachmentCount) || 0) > 0;
}

/** What the echo in the transcript says when a picture was sent on its own. */
export function echoText(text, count) {
  const body = String(text || '').trim();
  if (body) {
    return body;
  }
  const n = Number(count) || 0;
  return `${n} picture${n === 1 ? '' : 's'}`;
}

/** The send button's tooltip, which is where "it will be queued" lives. */
export function sendTitle(busy) {
  return busy ? 'Queue this - it runs when the turn in progress finishes' : 'Send';
}

/** The stop button's tooltip, which has to say what it does to what. A stop
 *  that reads as "cancel the conversation" would be the wrong promise. */
export function stopTitle(busy) {
  return busy ? 'Stop this turn (Esc). Running tools are cancelled; the conversation stays.' : '';
}

/* ========================================================================== *
 * The DOM half.
 * ========================================================================== */

/**
 * The box, the two buttons, the two live controls and the keys.
 *
 * @param {object} ctx
 * @param {function} ctx.onSend      `(text, attachments)`
 * @param {function} ctx.onInterrupt the stop button and the Escape key
 * @param {function} ctx.onPolicy    `(mode)`
 * @param {function} ctx.onEffort    `(level)`
 * @param {function} ctx.onTyping    called on every keystroke, for the menus
 * @param {function} ctx.onKey       the menus' keydown handler, first refusal
 */
export function createComposer(ctx) {
  const context = ctx || {};
  const form = $('#composer');
  const input = $('#input');
  const sendButton = $('#send');
  const stopButton = $('#stop');
  const state = { busy: false, pictures: 0, session: false };

  /* One line, growing to four, then scrolling. A panel has no room for a
   * paragraph box and a composer that is three rows tall by default is a
   * composer nobody uses. */
  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }

  function paint() {
    sendButton.disabled = !state.session || !canSend(input.value, state.pictures);
    sendButton.title = sendTitle(state.busy);
    stopButton.hidden = !state.busy;
    stopButton.title = stopTitle(state.busy);
    document.body.classList.toggle('busy', state.busy);
  }

  function setBusy(busy) {
    state.busy = Boolean(busy);
    paint();
  }

  function setSession(on) {
    state.session = Boolean(on);
    paint();
  }

  function setPictures(count) {
    state.pictures = Number(count) || 0;
    paint();
  }

  function setText(value, caret) {
    input.value = value;
    if (Number.isInteger(caret)) {
      input.setSelectionRange(caret, caret);
    }
    autoGrow();
    if (context.onTyping) {
      context.onTyping(input.value, input.selectionStart);
    }
  }

  input.addEventListener('input', () => {
    autoGrow();
    if (context.onTyping) {
      context.onTyping(input.value, input.selectionStart);
    }
    paint();
  });

  input.addEventListener('keydown', (event) => {
    /* The file list first: while it is open, Enter belongs to it. */
    if (context.onKey && context.onKey(event, input.value, input.selectionStart)) {
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value.trim();
    const attachments = context.attachments ? context.attachments() : [];
    if (!canSend(text, attachments.length)) {
      return;
    }
    // Echoed immediately so typing feels instant; the daemon's `turn.started`
    // replaces it, so a live panel and a reattached one show the same
    // conversation. Sent while busy too, and the daemon holds it.
    if (context.onEcho) {
      context.onEcho(echoText(text, attachments.length));
    }
    if (context.onSend) {
      context.onSend(text, attachments);
    }
    input.value = '';
    autoGrow();
    if (context.onCleared) {
      context.onCleared();
    }
    paint();
  });

  stopButton.onclick = () => {
    if (context.onInterrupt) {
      context.onInterrupt();
    }
  };

  /* Approval is a live control, not a label. It takes effect from the next tool
   * call: the one already in flight was decided under the old rule, and
   * pretending otherwise would be a lie about what ran. */
  const mode = $('#mode');
  if (mode) {
    for (const [value, label] of MODES) {
      const option = el('option', '', label);
      option.value = value;
      mode.appendChild(option);
    }
    mode.onchange = () => {
      if (context.onPolicy) {
        context.onPolicy(mode.value);
      }
    };
  }

  const effort = $('#effort');
  if (effort) {
    for (const [value, label] of EFFORT_LEVELS) {
      const option = el('option', '', label);
      option.value = value;
      effort.appendChild(option);
    }
    effort.onchange = () => {
      if (context.onEffort) {
        context.onEffort(effort.value);
      }
    };
  }

  /* Escape stops whatever is running: the fastest possible way to say "no". On
   * the document rather than the textarea so it works from a menu row, an
   * approval bar or a link as well as from the box. */
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !state.busy) {
      return;
    }
    event.preventDefault();
    if (context.onInterrupt) {
      context.onInterrupt();
    }
  });

  /* An unhandled failure in the page should reach the extension's output
   * channel rather than disappearing into a console nobody has open. */
  window.addEventListener('error', (event) => {
    note('error', `panel: ${event.message} (${event.filename}:${event.lineno})`);
  });

  autoGrow();
  paint();
  return {
    setBusy, setSession, setPictures, setText, focus: () => input.focus(), get busy() { return state.busy; },
  };
}

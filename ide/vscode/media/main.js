/* The panel.
 *
 * This is the entry `ide/vscode/src/extension.js` points the webview at:
 * `media/panel.html`, which loads this file. Everything below is wiring; the
 * decisions live in the modules beside it, and the reasoning lives in
 * `PROTOCOL.md` and in `openmirror/static/`, which is the renderer this one
 * follows.
 *
 * The shape of the whole thing is one relay. The extension host holds the
 * daemon's socket and its token, this page holds neither, and everything that
 * happens here is either a frame in or a frame out. There is no `fetch` and no
 * `WebSocket` in this directory, and there is nothing to reconstruct one from:
 * the page is not given the daemon's URL or its credential, and a panel that
 * needed either would be the one part of the extension that undoes the
 * confinement the rest of it is for.
 *
 * The file list, and why it is split this way:
 *
 *   `dom.js`       the four things every part needs, and the number formatters
 *   `stream.js`    streamed text, batched to the frame
 *   `events.js`    the transcript: a daemon event, rendered
 *   `composer.js`  the box, the buttons, the keys
 *   `slash.js`     the `/` menu
 *   `mentions.js`  the `@` menu
 *   `pictures.js`  paste, drop, thumbnail
 *   `status.js`    the link, the chips, the meter, the picker, the empty state
 *
 * The pure half of each of those is exported and tested by `node --test`
 * without a DOM; the DOM half is what wires it to the markup in `panel.html`.
 */

import { $, receive, send, warn } from './dom.js';
import { createComposer, submitFrame } from './composer.js';
import { createMentions, completion, mentionAt } from './mentions.js';
import { createPictures, wirePictures } from './pictures.js';
import { createSlash } from './slash.js';
import { createStatus } from './status.js';
import { createTranscript } from './events.js';

/* The conversation the panel is looking at. The host owns the truth about this
 * -- it sends the `config` frame whenever it changes -- and this copy exists to
 * notice a *change*, because a new or resumed conversation replays from the
 * beginning and stacking it on the old transcript would show two conversations
 * at once. */
let sessionId = '';

const status = createStatus({
  send: (frame) => send(frame),
  onCompact: () => {
    // The same as typing /compact, because it is the same thing and somebody
    // looking at a full meter should not have to remember a command.
    composer.setText('/compact');
    composer.focus();
  },
});

const transcript = createTranscript({
  send: (frame) => send(frame),
  onInfo: (patch) => status.onInfo(patch),
  onBusy: (busy) => composer.setBusy(busy),
  onContext: (report) => status.onContext(report),
  onHook: (hook) => status.onHook(hook),
});

/* The composer is built before the things it asks about, because the picture
 * strip paints itself once when it is created and the strip's count is what
 * tells the send button whether there is anything to send. */
const composer = createComposer({
  attachments: () => pictures.pending(),
  onEcho: (text) => transcript.echo(text),
  onSend: (text, attachments) => {
    send(submitFrame(text, attachments));
    // The pictures went with the message, so the strip empties here rather than
    // waiting for an answer -- resending the same screenshot on the next turn
    // because the strip still showed it is worse than not clearing it.
    pictures.reset();
  },
  onCleared: () => {
    slash.hide();
    mentions.hide();
  },
  onInterrupt: () => send({ t: 'interrupt' }),
  onPolicy: (mode) => send({ t: 'policy', mode }),
  onEffort: (level) => send({ t: 'effort', level }),
  onTyping: (value, caret) => {
    // The file list is asked about first, exactly as the browser client does it:
    // while it is open, Enter belongs to it, and the slash menu is not showing
    // at the same time because a mention is not a command.
    mentions.update(value, caret);
    slash.update(value);
  },
  onKey: (event, value, caret) => {
    if (mentions.handleKey(event, value, caret)) {
      return true;
    }
    return slash.handleKey(event, value);
  },
});

const pictures = createPictures({
  onChange: (count) => composer.setPictures(count),
});

const mentions = createMentions({
  ask: (query) => send({ t: 'files', query }),
  onPick: (path) => {
    const box = $('#input');
    const found = mentionAt(box.value, box.selectionStart);
    if (!found) {
      return;
    }
    const done = completion(found, path, box.value);
    composer.setText(done.text, done.caret);
    composer.focus();
  },
});

const slash = createSlash({
  onPick: (text) => {
    composer.setText(text);
    composer.focus();
  },
});

wirePictures(pictures);

/* --------------------------------------------------------------- the frames */

/**
 * One frame from the host.
 *
 * Every row of the host-to-webview table in `PROTOCOL.md` is here, and an
 * unknown `t` is logged rather than thrown: a stale webview paired with a new
 * host is a version skew somebody will hit and it must not take the panel down.
 */
function onFrame(frame) {
  switch (frame.t) {
    case 'config': {
      const next = String(frame.sessionId || '');
      if (next !== sessionId) {
        // A new or resumed conversation. The transcript, the mention list and
        // the pictures on the composer all belong to the conversation just
        // left, and the daemon replays the new one from the beginning.
        sessionId = next;
        status.onSessionChange();
        transcript.clear();
        mentions.reset();
        pictures.reset();
        slash.set([]);
        composer.setBusy(false);
      }
      status.onConfig(frame);
      transcript.setInfo(status.info);
      composer.setSession(Boolean(next));
      return;
    }

    case 'event':
      transcript.handle(frame.event);
      return;

    case 'commands':
      slash.set(frame.items);
      return;

    case 'files':
      // A reply for a query that has been retyped since is dropped rather than
      // painted over the newer one.
      if (!mentions.receive(frame.items)) {
        warn(`dropped a files reply for a query that is no longer in the box: ${frame.items && frame.items.length}`);
      }
      return;

    case 'context':
      status.onContext(frame);
      return;

    case 'sessions':
      status.onSessions(frame.items);
      return;

    case 'status':
      status.onFrame(frame);
      return;

    case 'notice':
      transcript.note(frame.kind === 'error' ? 'error' : 'meta', String(frame.text || ''));
      return;

    default:
      warn(`ignoring a frame from the host this panel has no handler for: ${JSON.stringify(frame.t)}`);
  }
}

receive(onFrame);

/* ------------------------------------------------------------------ startup */

/**
 * `ready`, once rendered and again on every reload.
 *
 * It is the only frame that means "the page can hear me again", and the host
 * answers it with `config` plus `commands` -- so it goes out from the bottom of
 * the module, after every listener is attached, and again from `pageshow` for a
 * page restored from the back/forward cache, where the module does not run a
 * second time and the host would otherwise be talking to a page that is not
 * listening. A webview reload re-runs this module, so the reload case is the
 * ordinary one.
 */
function announce() {
  send({ t: 'ready' });
  composer.focus();
}

window.addEventListener('pageshow', (event) => {
  if (event.persisted) {
    announce();
  }
});

announce();

/* Which of the seven things this is being right now.
 *
 * A body class, and one event. Everything else — which pane is visible, which
 * rail item is current, whether the composer exists — falls out of CSS, so
 * there is no imperative show/hide to get out of step with itself.
 *
 * Modes are told when they are left as well as when they are entered, and
 * that is the part that matters: a voice call left running because someone
 * clicked away from it is a microphone left open, and an autopilot frame
 * stream left running is a screen being captured for nobody.
 *
 * `mail` is on the same list as everything else rather than being a dialog,
 * for the reason the others are modes: it is a different kind of attention,
 * and switching to it should not be possible to do halfway through reading a
 * message.
 */

const MODES = ['code', 'talk', 'live', 'watch', 'studio', 'search', 'mail'];
const STORE = 'openmirror.mode';

const entering = new Map();
const leaving = new Map();

let current = 'code';

export function onEnter(mode, fn) {
  entering.set(mode, fn);
}

export function onLeave(mode, fn) {
  leaving.set(mode, fn);
}

export function mode() {
  return current;
}

export function go(next) {
  if (!MODES.includes(next) || next === current) return;

  const previous = current;
  current = next;

  const stop = leaving.get(previous);
  if (stop) {
    try {
      stop();
    } catch (err) {
      // A mode that throws on the way out must not trap you in it.
      console.error(`leaving ${previous}:`, err);
    }
  }

  for (const cls of MODES) document.body.classList.toggle(`mode-${cls}`, cls === next);
  for (const button of document.querySelectorAll('.nav-row.mode')) {
    button.setAttribute('aria-current', String(button.dataset.mode === next));
  }
  for (const cls of MODES) {
    const pane = document.getElementById(`pane-${cls}`);
    if (pane) pane.hidden = cls !== next;
  }

  try { localStorage.setItem(STORE, next); } catch { /* private window */ }

  const start = entering.get(next);
  if (start) start();
}

export function wireModes() {
  for (const button of document.querySelectorAll('.nav-row.mode')) {
    button.onclick = () => go(button.dataset.mode);
  }

  // Reopening the page puts you back where you were, except for the two modes
  // that hold a live microphone: coming back to a tab that starts listening
  // because of something you did yesterday is not a restored state, it is a
  // surprise.
  let remembered = 'code';
  try { remembered = localStorage.getItem(STORE) || 'code'; } catch { /* private window */ }
  if (remembered === 'talk' || remembered === 'live') remembered = 'code';

  document.body.classList.add('mode-code');
  go(remembered);
  // `go` is a no-op when the remembered mode is already current, so the
  // initial class and pane state are set explicitly here.
  if (remembered === 'code') {
    for (const button of document.querySelectorAll('.nav-row.mode')) {
      button.setAttribute('aria-current', String(button.dataset.mode === 'code'));
    }
  }
}

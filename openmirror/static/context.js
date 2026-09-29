/* How full the conversation is.
 *
 * Two numbers, and the difference between them matters more than it looks:
 * the token count is real, and the bar needs a denominator this project might
 * not have. So the bar is drawn only when there is one, and the text is
 * always the count.
 *
 * A bar at 40% on a model that will refuse the request is worse than no bar,
 * because somebody makes a decision about whether to keep working by looking
 * at it. That is the whole reason `window` comes back as null for a model
 * this project has never heard of.
 *
 * The bar fills towards the point the conversation is *summarised*, not
 * towards the model's window, because that is the number the session will
 * actually hit and the only one that changes what somebody should do.
 */

import { $, json } from './dom.js';

const state = { last: null };

/* 12k, 340k, 1.2M — whatever reads. `toLocaleString` gives a separator the
   reader already expects, which is a different one per locale and is the
   point of asking it. */
function count(n) {
  if (!n && n !== 0) return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString();
}

export function renderContext(info) {
  if (!info) return;
  state.last = info;

  const meter = $('#context-meter');
  const fill = $('#context-fill');
  const text = $('#context-text');

  if (!info.tokens && !info.total_in) {
    meter.hidden = true;
    return;
  }
  meter.hidden = false;

  const fraction = info.fraction;
  if (fraction === null || fraction === undefined) {
    // No denominator, so no bar — but the number still matters, and it says
    // the limit when there is one so `/compact` is discoverable.
    fill.parentElement.hidden = true;
    text.textContent = info.limit
      ? `${count(info.tokens)} of ${count(info.limit)} before it compacts`
      : `${count(info.tokens)} in context`;
  } else {
    fill.parentElement.hidden = false;
    fill.style.width = `${Math.max(1, Math.round(fraction * 100))}%`;
    const percent = Math.round(fraction * 100);
    // A bar that is nearly full is a bar somebody should act on, and the
    // colour is the only thing that says so without a tooltip.
    fill.classList.toggle('warn', percent >= 75);
    fill.classList.toggle('full', percent >= 92);
    text.textContent = `${percent}%`;
  }

  // The tooltip carries the numbers the bar does not: the estimate, the
  // session total, and whether this is a real count or the estimate.
  const parts = [`${count(info.tokens)} tokens in context`];
  if (info.window) parts.push(`model window ${count(info.window)}`);
  if (info.limit) parts.push(`summarised at ${count(info.limit)}`);
  if (info.total_in || info.total_out) {
    parts.push(`this session: ${count(info.total_in)} in, ${count(info.total_out)} out`);
  }
  parts.push(info.exact ? 'counted by the model' : 'estimated');
  meter.title = `${parts.join(' · ')}\n\nClick to compact the conversation.`;
}

export function wireContext() {
  const meter = $('#context-meter');
  if (!meter) return;
  meter.onclick = () => {
    /* The same as typing /compact, because it is the same thing and a
       person at 92% should not have to remember a command. */
    const input = $('#input');
    input.value = '/compact';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  };
}

/* Read on demand rather than pushed. The turn event carries it, so this is
   only for a session that was reattached to — where the client has the
   transcript but never saw the turn that produced it.

   Takes the id rather than reading localStorage, because this page has one
   place that knows which session it is on and a second copy of that is how a
   header ends up naming a session the transcript is not from. */
/* A *function* returning the id, handed in rather than read from storage:
   this page has one place that knows which session it is on, and a second copy
   of that is how a panel ends up describing the session you just left.
   Interpolating the function instead of calling it produces a 404 that reads
   as a missing route, which is exactly what happened. */
const currentSession = (fn) => (fn ? fn() : null);

export async function refreshContext(sessionId) {
  const id = currentSession(sessionId);
  if (!id) return;
  const info = await json(`/api/sessions/${encodeURIComponent(id)}/context`);
  if (info) renderContext(info);
}

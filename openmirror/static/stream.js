/* Streamed text, batched to the frame.
 *
 * Every token the model emits arrived as its own event, and every event wrote
 * to the DOM. `body.textContent += text` is not a small edit: it reads the
 * node's text, concatenates, and writes back — so the whole paragraph is
 * re-laid-out for every token, and at the rates a model streams (often 20-40
 * tokens a second, sometimes more) that is a forced synchronous layout per
 * token, on the main thread, competing with the frame the companion and the
 * scroll are trying to get.
 *
 * The obvious fix is to append the token to a string and let the next frame put
 * it on screen. That is what this does, and the fiddly part is not the
 * batching: it is every point at which something *reads* the text back.
 *
 * The rules, each of which exists because breaking it is a visible bug rather
 * than a slow one:
 *
 *   - Anything that renders, scrolls, copies, measures or collapses what is on
 *     screen has to flush first. `flush()` is idempotent and cheap when there
 *     is nothing pending, so it goes at the top of those.
 *   - Flushing must not reorder text. Several events in one frame can target
 *     the same node — a tool card's output and the agent's prose — so each
 *     pending entry keeps its own node, and a single frame applies them in the
 *     order they were queued.
 *   - A pending write is cancelled if the node it was aimed at leaves the
 *     transcript, because a build log torn down mid-stream would otherwise
 *     have a frame applied to a detached node. `cancel()` is called by the
 *     teardown paths.
 *
 * Frame-coalescing, not rate-limiting: nothing here throttles, and no text is
 * ever dropped. A turn that ends in the same frame it started still renders
 * completely, because `flush()` runs on `turn.completed`.
 */

/* One queued write. Kept as a list rather than a map so two updates to the
   same node in one frame are applied in arrival order — a tool card that is
   written to twice before the next frame must not have its lines swapped. */
const pending = [];
let frame = null;
let writes = 0;

function paint() {
  frame = null;
  if (!pending.length) return;
  /* Copied out before the first write, because a write can queue another one —
     `apply` is the only place that happens today, but relying on that would be
     a trap for whoever adds the next caller. */
  const batch = pending.splice(0, pending.length);
  /* Runs of writes aimed at the same node are merged into one. A burst of
     twenty tokens for one paragraph is twenty queue entries but only *one*
     thing to put on screen, and joining them here is what makes the saving the
     whole point: without it the frame does twenty appends where it could do
     one. Merging is by adjacency, not by lookup, which is what keeps arrival
     order intact — two updates to a card interleaved with the agent's prose
     stay in the order they came in rather than being reordered by a map. */
  for (let i = 0; i < batch.length; i++) {
    const item = batch[i];
    let text = item.text;
    let j = i + 1;
    while (j < batch.length && batch[j].node === item.node) {
      text += batch[j].text;
      j++;
    }
    /* A node can be detached between queueing and painting: a tool card torn
       down, a session switched. Writing to it is not a crash, but the text is
       then nowhere, and the next thing that renders will look like the tool
       never produced output. Skipped rather than written. */
    if (item.node.isConnected) {
      writes++;
      item.apply(item.node, text);
    }
    i = j - 1;
  }
  if (pending.length && frame === null) frame = requestAnimationFrame(paint);
}

/* Queue `text` for `node` on the next frame. `apply` receives the node because
   the same batching serves four different targets — the assistant's prose, the
   reasoning disclosure, a tool card's output, a subagent's — and each has its
   own trailing work (scrolling, trimming, wrapping) after the text lands. */
export function queue(node, text, apply) {
  if (!node || !text) return;
  /* Nothing pending and the page is live: still go through the frame. The
     whole point is that a burst of events costs one write, and routing the
     single-event case around the queue would make the two paths differ in
     ways that only show up under load. */
  pending.push({ node, text, apply });
  if (frame === null) frame = requestAnimationFrame(paint);
}

/* Put everything pending on screen now. Called before anything reads the DOM,
   and at the end of a turn so a finished answer is never left half a frame
   behind. Safe to call when there is nothing to do — which is most calls. */
export function flush() {
  if (!pending.length) return;
  if (frame !== null) {
    cancelAnimationFrame(frame);
    frame = null;
  }
  paint();
}

/* Forget a node's pending text, because the node is going away and the text
   belongs to it rather than to the page. Without this a torn-down tool card
   would get one more frame of output written into a detached node. */
export function cancel(node) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].node === node) pending.splice(i, 1);
  }
  if (!pending.length && frame !== null) {
    cancelAnimationFrame(frame);
    frame = null;
  }
}

/* What the tests want to know. `queued` is the number of writes waiting for a
   frame and `writes` how many frames have actually been taken; the ratio is
   the thing being fixed — before this, every queued write was also a layout. */
export function stats() {
  return { queued: pending.length, writes, scheduled: frame !== null };
}

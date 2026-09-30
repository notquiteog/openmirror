/* Streamed text, batched to the frame.
 *
 * A port of `openmirror/static/stream.js`. It exists here for the same reason
 * it exists there: every token the model emits arrives as its own event, and
 * `body.textContent += text` is not a small edit -- it reads the node, joins,
 * and writes back, re-laying-out the whole paragraph for every token. At the
 * rates a model streams, that is a forced synchronous layout per token, on the
 * main thread of a webview that is sharing a process with the editor.
 *
 * Frame-coalescing, not rate-limiting. Nothing is throttled and nothing is
 * dropped: a burst of twenty deltas in one frame is twenty queue entries and
 * one write, so a long answer still appears progressively and completely. A
 * turn that ends in the frame it started still renders in full, because
 * `flush()` runs on `turn.completed`.
 *
 * The rules, each of which exists because breaking it is a visible bug rather
 * than a slow one:
 *
 *   - Anything that renders, scrolls, copies, measures or collapses what is on
 *     screen flushes first. `flush()` is idempotent and cheap when there is
 *     nothing pending, so it goes at the top of those.
 *   - Flushing must not reorder text. Several events in one frame can target
 *     the same node, so pending writes are a list rather than a map and a
 *     single frame applies them in arrival order.
 *   - A pending write is cancelled if the node it was aimed at leaves the
 *     transcript, or a torn-down card would get a frame written into a
 *     detached node and the text would be nowhere.
 */

/** One queued write. A list, not a map, so arrival order survives the batch. */
const pending = [];
let frame = null;

/** Runs of writes aimed at the same node merge into one, by adjacency. */
function paint() {
  frame = null;
  if (!pending.length) {
    return;
  }
  const batch = pending.splice(0, pending.length);
  for (let i = 0; i < batch.length; i++) {
    const item = batch[i];
    let text = item.text;
    let j = i + 1;
    while (j < batch.length && batch[j].node === item.node) {
      text += batch[j].text;
      j++;
    }
    // A node can be detached between queueing and painting. Writing to it is
    // not a crash, but the text is then nowhere, and the next thing that
    // renders looks as though the tool never produced output.
    if (item.node.isConnected) {
      item.apply(item.node, text);
    }
    i = j - 1;
  }
  if (pending.length && frame === null) {
    frame = requestAnimationFrame(paint);
  }
}

/**
 * Queue `text` for `node` on the next frame.
 *
 * `apply` receives the node because the same batching serves the assistant's
 * prose, the reasoning disclosure and a subagent's, and each has its own
 * trailing work after the text lands.
 */
export function queue(node, text, apply) {
  if (!node || !text) {
    return;
  }
  pending.push({ node, text, apply });
  if (frame === null) {
    frame = requestAnimationFrame(paint);
  }
}

/**
 * Everything pending, on screen now.
 *
 * Called before anything reads the DOM and at the end of a turn: an answer
 * missing its final sentence because the socket closed on the same frame is the
 * sort of thing that gets reported as "it cuts off at the end".
 */
export function flush() {
  if (!pending.length) {
    return;
  }
  if (frame !== null) {
    cancelAnimationFrame(frame);
    frame = null;
  }
  paint();
}

/** Forget a node's pending text, because the node is going away. */
export function cancel(node) {
  for (let i = pending.length - 1; i >= 0; i--) {
    if (pending[i].node === node) {
      pending.splice(i, 1);
    }
  }
  if (!pending.length && frame !== null) {
    cancelAnimationFrame(frame);
    frame = null;
  }
}

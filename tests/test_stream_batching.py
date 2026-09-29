"""Streamed text is written to the DOM once per frame, not once per token.

There is no browser here, so this runs the real `stream.js` under node behind a
stubbed `requestAnimationFrame` and counts the writes. What it is checking is
the thing that was wrong: every `text.delta` did `body.textContent += text`,
which reads the node's text, concatenates and writes it back — a forced
synchronous layout for every token, at the rate a model streams. On a long
answer that is thousands of layouts competing with the frame the transcript and
the companion are trying to get.

The module is run as it ships. An earlier version of this test cut the source
down and reimplemented the batching inline, which meant it was measuring the
test's own copy; a mutation of the real module passed. So `stream.js` is
copied byte for byte and imported, and the stubs are the two globals it uses.

The other half of the file reads `app.js` as text. The wiring cannot be
exercised without a DOM, and every check there asserts that it found the code
it was looking for — a check that passes by matching nothing is worse than no
check at all.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STREAM = ROOT / 'openmirror' / 'static' / 'stream.js'
APP = (ROOT / 'openmirror' / 'static' / 'app.js').read_text()

HARNESS = r'''
import { readFileSync, writeFileSync } from 'fs';
import { pathToFileURL } from 'url';

const queue = [];
let cancelled = 0;
/* The ids are the queue positions, and cancelling genuinely removes the
   callback — so a stale frame left behind by a mutation shows up as an extra
   call rather than being silently absorbed. A stub that only nulled the entry
   would have made "the frame was not cancelled" indistinguishable from "the
   callback ran and found nothing to do". */
globalThis.requestAnimationFrame = (fn) => { queue.push(fn); return queue.length; };
globalThis.cancelAnimationFrame = (id) => { if (queue[id - 1]) { queue[id - 1] = null; cancelled++; } };

/* A node with the one property the module reads, and a record of what was
   written to it. `isConnected` is what the real DOM exposes and what the
   detached-node guard checks. */
function node(name) {
  return { name, isConnected: true, textContent: '', writes: [] };
}

const copy = process.argv[2];
writeFileSync(copy, readFileSync(process.argv[3], 'utf8'));
const stream = await import(pathToFileURL(copy).href);

const add = (n, chunk) => { n.textContent += chunk; n.writes.push(chunk); };

/* Run whatever frames are outstanding, as a browser would. */
function frames(n = 1) {
  for (let i = 0; i < n; i++) {
    for (const fn of queue.splice(0)) if (fn) fn();
  }
}

const out = {};

/* A burst of tokens, which is what a model actually produces. */
{
  const body = node('body');
  for (const t of ['The ', 'plan ', 'is ', 'three ', 'files.']) stream.queue(body, t, add);
  out.burst = { ...stream.stats() };
  frames();
  out.burstAfter = { ...stream.stats() };
  out.burstText = body.textContent;
  out.burstWrites = body.writes.length;
}

/* Two different targets in one frame — the agent's prose and a build log, which
   is the common case: the two interleave on the socket. */
{
  stream.flush();
  const body = node('body');
  const out2 = node('out');
  for (let i = 0; i < 20; i++) {
    stream.queue(body, `t${i} `, add);
    stream.queue(out2, `line ${i}\n`, add);
  }
  out.twoTargets = stream.stats().writes;
  frames();
  out.twoTargetsWrites = body.writes.length + out2.writes.length;
  out.twoTargetsIntact = body.textContent === Array.from({ length: 20 }, (_, i) => `t${i} `).join('')
    && out2.textContent === Array.from({ length: 20 }, (_, i) => `line ${i}\n`).join('');
}

/* The same target twice in one frame must keep its arrival order: two writes
   to one card in a frame is ordinary, and reordering them swaps lines. */
{
  stream.flush();
  const body = node('body');
  stream.queue(body, 'first ', add);
  stream.queue(body, 'second ', add);
  frames();
  out.sameTarget = body.textContent;
}

/* A flush has to put the text on screen now: this is what a finished turn
   relies on so its last sentence is not a frame behind. */
{
  stream.flush();
  const body = node('body');
  stream.queue(body, 'tail', add);
  out.beforeFlush = body.textContent;
  stream.flush();
  out.afterFlush = body.textContent;
  out.flushDrainedFrame = stream.stats().scheduled;
}

/* A flush when nothing is pending must not schedule a frame — it is called on
   every tool completion, and most of the time there is nothing queued. */
{
  const before = stream.stats();
  stream.flush();
  stream.flush();
  out.idleFlush = { before, after: stream.stats() };
}

/* A node that leaves the page before the frame arrives gets nothing written to
   it: a tool card torn down mid-build should not get one more line. */
{
  const body = node('body');
  const gone = node('gone');
  stream.queue(body, 'kept', add);
  stream.queue(gone, 'dropped', add);
  gone.isConnected = false;
  frames();
  out.detached = { kept: body.textContent, gone: gone.textContent };
}

/* A flush drains the queue, so the frame it took for itself has to be given
   back rather than left to fire at nothing. Cheap to assert and it is the one
   path where a rAF can be left outstanding, since the burst path releases it
   by simply not asking for another. */
{
  const body = node('body');
  const before = cancelled;
  stream.queue(body, 'x', add);
  stream.flush();
  out.flushCancelled = cancelled - before;
  frames();
  out.afterFlushFrame = stream.stats().scheduled;
}

/* `cancel` is the explicit version of that, for a teardown that knows it is
   about to remove a node. */
{
  const body = node('body');
  stream.queue(body, 'before', add);
  stream.cancel(body);
  frames();
  out.cancelled = { text: body.textContent, queued: stream.stats().queued };
}

/* Cancelling the last thing queued has to release the frame, or the page keeps
   a rAF alive for an empty queue. */
{
  const body = node('body');
  stream.queue(body, 'x', add);
  stream.cancel(body);
  out.cancelFreesFrame = stream.stats().scheduled;
  frames();
}

/* Empty text and a missing node are no-ops: some events carry an empty delta,
   and a card can be gone by the time its output is replayed. */
{
  const body = node('body');
  const before = stream.stats();
  stream.queue(body, '', add);
  stream.queue(null, 'orphan', add);
  out.emptyNoop = { before, after: stream.stats(), text: body.textContent };
  frames();
}

out.totalWrites = stream.stats().writes;
console.log(JSON.stringify(out));
'''


@pytest.fixture(scope='module')
def stream(tmp_path_factory):
    node_bin = _node()
    here = tmp_path_factory.mktemp('stream')
    script = here / 'harness.mjs'
    script.write_text(HARNESS)
    copy = here / 'stream.mjs'

    def run():
        proc = subprocess.run(
            [node_bin, str(script), str(copy), str(STREAM)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    return run


def _node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


# -- the batching itself ----------------------------------------------------


def test_a_burst_of_tokens_is_one_write(stream):
    """The number the whole change is about. Five deltas in one frame used to be
    five reads of the paragraph and five writes of it back."""
    out = stream()
    assert out['burst']['queued'] == 5, 'the tokens were not queued'
    assert out['burstAfter']['writes'] == 1, (
        f"a five-token burst took {out['burstAfter']['writes']} writes"
    )
    assert out['burstWrites'] == 1, (
        f"the node was written {out['burstWrites']} times, not once"
    )


def test_nothing_is_dropped_or_reordered(stream):
    """Batching is coalescing, not sampling. The text has to be exactly what
    arrived, in order — a lost token is a mangled sentence, and no amount of
    speed is worth that."""
    assert stream()['burstText'] == 'The plan is three files.'


def test_two_targets_in_one_frame_stay_separate_and_complete(stream):
    """The agent's prose and a build log interleave on the socket. They are
    different nodes with different trailing work, so coalescing must not merge
    them or drop either."""
    out = stream()
    assert out['twoTargetsWrites'] == 40, f"{out['twoTargetsWrites']} writes for 40 tokens"
    assert out['twoTargetsIntact'], 'interleaved targets lost or reordered text'


def test_two_writes_to_one_node_in_a_frame_keep_their_order(stream):
    """A card written twice before the next frame is ordinary, and swapping the
    two writes swaps its lines."""
    assert stream()['sameTarget'] == 'first second '


def test_a_burst_costs_one_frame_not_one_per_token(stream):
    """The scheduling side of the same claim: one rAF for the burst, and it is
    released once the queue drains rather than left running."""
    out = stream()
    assert out['burst']['scheduled'] is True
    assert out['burstAfter']['scheduled'] is False, 'the frame was not released'


# -- and everything that reads the DOM back ---------------------------------


def test_a_flush_puts_the_text_on_screen_immediately(stream):
    """What a finished turn depends on. If `turn.completed` did not flush, the
    last sentence of an answer could sit unwritten until the next frame — or
    never, if the socket closed on the same frame."""
    out = stream()
    assert out['beforeFlush'] == '', 'the text was written before the flush'
    assert out['afterFlush'] == 'tail'
    assert out['flushDrainedFrame'] is False
    assert out['flushCancelled'] == 1, (
        'a flush leaves its own frame outstanding, to fire at an empty queue'
    )
    assert out['afterFlushFrame'] is False


def test_a_flush_with_nothing_pending_costs_nothing(stream):
    """Called on every tool completion, and almost always with an empty queue.
    Scheduling a frame for it would be a rAF that paints nothing."""
    out = stream()
    assert out['idleFlush']['before']['queued'] == 0
    assert out['idleFlush']['after'] == out['idleFlush']['before'], (
        f"an idle flush changed the state: {out['idleFlush']['after']}"
    )


def test_a_node_that_left_the_page_is_not_written_to(stream):
    """A tool card torn down mid-build, or a session switched. Writing the last
    line into a detached node puts it nowhere, and the card looks like it never
    produced output."""
    out = stream()
    assert out['detached']['kept'] == 'kept'
    assert out['detached']['gone'] == '', 'text was written to a detached node'


def test_cancel_drops_a_nodes_text_and_frees_the_frame(stream):
    out = stream()
    assert out['cancelled']['text'] == ''
    assert out['cancelled']['queued'] == 0
    assert out['cancelFreesFrame'] is False, 'the rAF was left running for an empty queue'


def test_empty_text_and_a_missing_node_are_no_ops(stream):
    """Some events carry an empty delta, and a card can be gone by the time its
    output is replayed after a reconnect."""
    out = stream()
    assert out['emptyNoop']['before'] == out['emptyNoop']['after'], 'an empty delta queued work'
    assert out['emptyNoop']['text'] == ''


# -- and the module is wired into the paths that stream ---------------------


def _body(of: str, name: str) -> str:
    """The text of a named function, so a check cannot pass by matching the same
    words somewhere else in a 2000-line file."""
    at = APP.index(name)
    start = APP.index('{', at)
    depth = 0
    for i in range(start, len(APP)):
        if APP[i] == '{':
            depth += 1
        elif APP[i] == '}':
            depth -= 1
            if depth == 0:
                return APP[at:i + 1]
    raise AssertionError(f'{name} has no closing brace')


@pytest.mark.parametrize('name, why', [
    ('function appendText', 'the assistant\'s prose'),
    ('function appendThinking', 'the reasoning disclosure'),
    ('function toolOutput', 'a tool card\'s build log'),
])
def test_every_streaming_path_is_batched(name, why):
    """Four paths stream, and each one had its own per-token write. A path
    added later has to be batched too, which is what these three plus the
    subagent log are for — and why this is a list rather than one check."""
    body = _body(APP, name)
    assert 'queue(' in body, f'{why} still writes straight to the DOM'
    assert 'textContent +=' in body, f'{why} writes per token, outside the queued write'
    queued = body[body.index('queue('):]
    assert 'textContent +=' in queued, f'{why} appends outside the queued write'


def test_the_subagent_log_is_batched_too():
    """A subagent narrating a search is the fastest text on the page, and it was
    the fourth un-batched `textContent +=`."""
    body = _body(APP, 'function childEvent')
    assert 'queue(said, ev.text' in body, 'the subagent log still writes per token'
    assert not re.search(r'said\.textContent \+=', body), 'a per-token write is left behind'


def test_nothing_left_writes_streamed_text_per_token():
    """The belt to the other braces: whatever path it came in by, the four
    streaming targets are only ever appended to inside a queued write.

    Scoped to those targets on purpose. There are other `+=` writes in the
    file — a tool card's summary line, the run count on a group header — and
    those run once per tool call, not once per token, so batching them would
    be cost without benefit. Catching them here would have meant either
    excluding them by name, which rots, or batching them, which is wrong.
    """
    for target in ('said', 'out', 'body', "box.querySelector('span')"):
        for match in re.finditer(re.escape(f'{target}.textContent +='), APP):
            line = APP[APP.rfind('\n', 0, match.start()) + 1:APP.find('\n', match.start())]
            assert 'node.textContent +=' in line, (
                f'per-token text write to a streaming target: {line.strip()}'
            )
    # The queued apply is the only shape allowed anywhere in the file.
    per_token = [
        line.strip() for line in APP.splitlines()
        if re.search(r'\.textContent \+=', line) and 'node.textContent +=' not in line
    ]
    assert len(per_token) <= 3, (
        'unexpected per-token writes, or new ones added: ' + '; '.join(per_token)
    )


def test_the_turn_flushes_before_it_ends():
    """A finished turn is the one place where a queued write has a hard
    deadline: the socket may not deliver another frame, and the reader is
    watching for the answer to be complete."""
    body = _body(APP, "case 'turn.completed'")
    assert 'flushStream()' in body, 'a completed turn does not flush its text'
    at = body.index('flushStream()')
    for after in ('setBusy(false)', 'companion.flash', 'notice('):
        if after in body:
            assert body.index(after) > at, f'{after} runs before the flush'


def test_a_completed_tool_card_flushes_before_it_folds():
    """`toolDone` takes the output element out of its live state and, when a
    call failed, changed a file or produced a screenshot, folds the card to a
    line. Queued output landing after the fold is output the reader never saw
    scroll past — and on a replayed session, where the fold is the only render
    there is, it is missing entirely.

    The assertion is against the first state change, not against `setCard`
    specifically: `setCard` only toggles two classes and `setCard(card, false)`
    hides nothing, so ordering it either side of the flush is not what makes
    the difference. `.live` and `.card` carry no layout of their own in
    `style.css`, so what matters is that the text is on the node before the
    card is declared finished.
    """
    body = _body(APP, 'function toolDone')
    at = body.index('flushStream()')
    for after in ("classList.add('failed')", "classList.remove('live')", 'setCard('):
        assert body.index(after) > at, f'{after} runs before the flush'


def test_an_error_flushes_before_it_is_reported():
    """An error ends the turn the same way `turn.completed` does, and it is
    often the only thing left: the socket drops straight after, so a queued
    half-answer that never got written is exactly the "it cut off" report. The
    notice is appended to the transcript, so it must come after the flush or it
    renders before the text it is explaining."""
    body = _body(APP, 'function handleAgentEvent')
    case = body.index("case 'error'")
    rest = body[case:]
    assert 'flushStream()' in rest, 'an error ends the turn without flushing its text'
    at = rest.index('flushStream()')
    assert rest.index('notice(') > at, 'the notice is rendered before the flush'
    assert rest.index('setBusy(false)') > at, 'the turn ends before the flush'


def test_teardown_drops_queued_text_rather_than_writing_it():
    """The other side of the flush: switching sessions must not paint one last
    frame of the session you left into nodes that are about to be detached."""
    body = _body(APP, 'function clearTranscript')
    assert 'cancelStream(node)' in body, 'a detached node keeps its queued text'
    assert 'flushStream()' not in body, 'teardown flushes instead of cancelling'
    assert 'cancelStream(card)' in body, 'tool cards keep their queued output'


def test_the_transcript_is_pruned_in_whole_turns():
    """A turn kept in part is worse than no pruning: a card whose output is
    gone but whose header is not reads as broken, and a receipt for a turn that
    is no longer there reads as nothing at all."""
    body = _body(APP, 'function prune')
    assert 'classList.contains(\'turn\')' in body, 'pruning is not keyed on whole turns'
    assert re.search(r'findIndex\(.*classList\.contains', body), (
        'a turn is not walked to its end, so one can be cut in half'
    )
    # ...and never the turn in progress, which is the only thing on screen.
    assert 'state.turnNode' in body, 'the current turn is not protected'
    # ...and never while the reader is up in the part that would go.
    assert 'atBottom()' in body, 'pruning happens under the reader'


def test_pruning_only_starts_above_a_generous_threshold():
    """This is a safety valve for pathological sessions, not an optimisation.
    A transcript that fits in memory should stay whole, and a limit tuned to
    what a slow machine happens to tolerate today would start deleting history
    on someone else's."""
    match = re.search(r'const KEEP_NODES = (\d+);', APP)
    assert match, 'the prune threshold is not a named constant'
    assert int(match.group(1)) >= 500, (
        f'only {match.group(1)} nodes are kept; a normal session would lose history'
    )


def test_pruning_is_reached_from_the_append_path():
    """Batching a token stream makes each write cheap; it does not make the
    transcript stop growing. This is the other half of the cost, and it has to
    actually be called."""
    body = _body(APP, 'function append')
    assert re.search(r'\bprune\(\)', body), 'nothing prunes the transcript'
    # ...after the node is in, and before the empty-state check, which reads
    # the transcript's contents.
    assert body.index('prune()') > body.index('appendChild')
    assert body.index('prune()') < body.index('settle()')

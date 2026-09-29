"""The scroll follow asks once per frame, not once per append.

Lifted out of `app.js` as it ships and run under node, the way
`test_session_refresh.py` does it, so a mutation of the real function fails
this. The property is about a count, which is the only kind that can be
asserted without a browser and also happens to be the thing that matters:

**One layout read per frame, and one scroll write per frame**, however many
nodes were appended in it.

Before, `atBottom()` read `scrollHeight`/`scrollTop`/`clientHeight` on every
append and every queued text write. A card append invalidates layout, so the
next read forces it back — and a frame that appended four cards forced layout
four times. Measured in a real browser, one append alone is 16.7ms and four
appends with a read each is 33.3ms: two vsyncs for one frame's work, for the
whole of a turn.

The claim is *at most* once per frame, and it is at most rather than exactly
because a frame with no appends and no writes should do no work at all — a
`requestAnimationFrame` that re-reads layout for nothing is the same cost in a
smaller hat.

`test_live_frames.py` measures the end result on a real turn. That one cannot
distinguish this from the alternative, which is why this is here: this can
fail.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'openmirror' / 'static' / 'app.js'


def node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


def _lift(name: str) -> str:
    """The source of one top-level function, as shipped."""
    source = APP.read_text()
    start = source.index(f'function {name}')
    brace = source.index('{', start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f'{name} is not closed in the source')


HARNESS = r'''
import { readFileSync } from 'fs';

const src = readFileSync(process.argv[1], 'utf8');

function lift(name) {
  const start = src.indexOf('function ' + name);
  const brace = src.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error(name + ' not closed');
}

/* The count is on the *getter*, not on the call. `atBottom` may be called
   twenty times in a frame; what costs anything is how many times it actually
   goes and reads layout, and only a getter sees that. `scrollHeight` and
   `clientHeight` are both layout-forcing, and reading both is one flush, so
   the claim is "two getter hits for any number of calls in a frame". */
let reads = 0;
let writes = 0;
const transcript = {
  get scrollHeight() { reads++; return 1000; },
  get clientHeight() { reads++; return 100; },
  scrollTop: 900,
  set scrollTop(v) { writes++; this._t = v; },
  _t: 900,
  appendChild() {},
};

/* The scheduler, stubbed so "a frame" is a call rather than a wait.
   `frameNo` is deliberately *not* declared here: `frameStamp` owns it in the
   lifted source, and a second one here would shadow it and the memo would
   never expire — which is the exact bug the last test is looking for. */
let queued = [];
function requestAnimationFrame(fn) { queued.push(fn); return 1; }
function runFrame() { const q = queued; queued = []; for (const fn of q) fn(0); }

/* The module-level state these two functions close over, lifted from the
   source rather than rewritten — a rewrite is a second implementation and a
   second thing to be wrong. */
function liftLine(pattern) {
  const at = src.indexOf(pattern);
  if (at < 0) throw new Error('not in the source: ' + pattern);
  const end = src.indexOf('\n', at);
  return src.slice(at, end);
}
const state = [
  liftLine('let followWanted'),
  liftLine('let followScheduled'),
  liftLine('let atBottomFrame'),
  liftLine('let atBottomValue'),
  liftLine('let frameNo'),
  liftLine('let frameScheduled'),
].join('\n');

const names = ['atBottom', 'frameStamp', 'scrollToTail'];
const bodies = state + '\n\n' + names.map(lift).join('\n\n');
const api = new Function(
  'transcript', 'requestAnimationFrame',
  bodies + '\nreturn { atBottom, frameStamp, scrollToTail };'
)(transcript, requestAnimationFrame);

const out = {};

/* --- one layout for many appends in one frame --------------------------- */
reads = 0; writes = 0; queued = [];
for (let i = 0; i < 8; i++) api.atBottom();
out.eight = reads;
for (let i = 0; i < 8; i++) api.atBottom();
out.sixteen = reads;
runFrame();

/* --- one write for many appends in one frame ----------------------------- */
reads = 0; writes = 0; queued = [];
for (let i = 0; i < 8; i++) { api.atBottom(); api.scrollToTail(); }
out.writesBeforeFrame = writes;
runFrame();
out.writesAfterFrame = writes;

/* --- asking again in a new frame reads again ----------------------------- */
reads = 0; writes = 0; queued = [];
api.atBottom();
const before = reads;
api.atBottom();
out.secondCallInSameFrame = reads - before;
runFrame();
const after = reads;
api.atBottom();
out.callInNextFrame = reads - after;

console.log(JSON.stringify(out));
'''


@pytest.fixture(scope='module')
def counts() -> dict:
    result = subprocess.run(
        [node(), '--input-type=module', '-e', HARNESS, str(APP)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f'node failed:\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_many_appends_in_one_frame_read_the_layout_once(counts):
    """The claim, and the reason this file exists.

    A card append invalidates layout and the next read forces it back, so
    before the memo this was quadratic in the number of appends — and a turn
    that reports four results at once is not a rare shape. Sixteen calls in
    one frame read the layout twice (once per layout-forcing property) rather
    than thirty-two times.
    """
    assert counts['eight'] == 2, f"eight calls forced {counts['eight']} layout reads; expected 2"
    # And it does not grow: sixteen calls, still one layout.
    assert counts['sixteen'] == 2, f"sixteen calls forced {counts['sixteen']} layout reads; expected 2"


def test_many_appends_in_one_frame_write_the_scroll_once(counts):
    assert counts['writesBeforeFrame'] == 0, 'the write must not happen inline'
    assert counts['writesAfterFrame'] == 1, (
        f"{counts['writesAfterFrame']} scroll writes for eight appends in one frame"
    )


def test_the_answer_is_memoised_within_a_frame_and_not_after_it(counts):
    """The memo has to expire, or the transcript stops following the tail the
    moment the reader scrolls up and back — which is the bug the memo was
    added to fix, and a worse one because it is silent."""
    assert counts['secondCallInSameFrame'] == 0, 'a second call in one frame read again'
    assert counts['callInNextFrame'] == 2, 'the memo did not expire at the frame boundary'


def test_the_lifted_source_still_says_what_it_claims():
    """The measurement above is only about these two functions. If they are
    renamed or the memo removed, this fails first and says so plainly."""
    source = APP.read_text()
    assert 'atBottomFrame' in source
    assert 'atBottomValue' in source
    assert 'function scrollToTail()' in source
    # And nothing writes the transcript's scroll position except the coalesced
    # path — one place to look when it stops following.
    direct = [
        line for line in source.splitlines()
        if 'transcript.scrollTop = transcript.scrollHeight' in line
    ]
    assert len(direct) == 1, f'the scroll is written from {len(direct)} places; expected one'
    assert _lift('scrollToTail').count('transcript.scrollTop = transcript.scrollHeight') == 1

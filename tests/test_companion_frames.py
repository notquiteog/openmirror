"""The companion frame loop: one loop, and one that honours the art's own rate.

There is no browser in this environment, so none of this measures a frame
time. What it does instead is load the real `engine.js` — copied byte for byte,
with nothing edited out of it — behind a stubbed canvas and a stubbed clock,
and count how many times it decides to paint. That is the number that was
wrong, and it is countable without a compositor.

The bug this guards against was structural. Each `Perch` owned a
`requestAnimationFrame` loop, so up to eight were running at once; and each one
repainted at display rate regardless of what the state asked for. A companion
whose idle drift is authored at 0.6fps — `moth.js` and `mandrake.js` both do
this — was being redrawn sixty times a second to put back the identical sprite.
That is the largest steady source of wasted work on the page, it is entirely
invisible, and it competes with the transcript for the same frame budget.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'openmirror' / 'static' / 'companions' / 'engine.js'
COMPANIONS = ROOT / 'openmirror' / 'static' / 'companions'

FRAME_MS = 1000 / 60

# The engine is loaded whole. An earlier version of this test cut the class
# down to the scheduling methods and stubbed `draw`, which meant the assertions
# were made against a rewritten copy of the driver: three separate mutations of
# the real `tick`, `set` and `flash` all passed, because none of that code ran.
# The only things the engine needs from a browser are `createElement('canvas')`
# and a 2d context, both of which are one line to fake, so the file is run as
# it ships and a paint is counted the way a browser would notice one: a sprite
# put on the canvas.
HARNESS = r'''
import { readFileSync, writeFileSync } from 'fs';
import { pathToFileURL } from 'url';

let now = 1000;                 // not 0: `add` uses a zero lastPaint as "never painted"
const pending = [];
let rafCalls = 0;
let maxQueued = 0;

globalThis.performance = { now: () => now };
globalThis.requestAnimationFrame = (fn) => { pending.push(fn); rafCalls++; return rafCalls; };

function target(extra) {
  const listeners = new Map();
  return Object.assign({
    addEventListener(name, fn) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(fn);
    },
    removeEventListener(name, fn) {
      const held = listeners.get(name) || [];
      const at = held.indexOf(fn);
      if (at >= 0) held.splice(at, 1);
    },
    fire(name) { for (const fn of (listeners.get(name) || []).slice()) fn(); },
  }, extra);
}

globalThis.window = target({
  devicePixelRatio: 1,
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
});

function canvas() {
  const ctx = {
    imageSmoothingEnabled: true,
    fillStyle: '',
    setTransform() {}, save() {}, restore() {}, translate() {}, scale() {},
    fillRect() {}, clearRect() {},
    drawImage() {},
  };
  return { width: 0, height: 0, style: {}, getContext: () => ctx };
}
globalThis.document = target({ hidden: false, createElement: () => canvas() });

/* Copied verbatim — the copy is the file, not an edit of it. */
const copy = process.argv[2];
writeFileSync(copy, readFileSync(process.argv[3], 'utf8'));
const { Perch, driverState } = await import(pathToFileURL(copy).href);

const plan = JSON.parse(process.argv[4]);

/* A creature with one pixel in it, and states at the rates a caller asks for.
   `null` means no `fps` at all, which several of the real creatures use to say
   "as fast as the display". */
function make(id, rate) {
  const state = rate === null ? { frames: ['body'] } : { frames: ['body'], fps: rate };
  const def = {
    id,
    palette: { '#': '#8899aa' },
    frames: { body: ['#'] },
    states: {
      idle: state,
      running: { frames: ['body'], fps: 22 },
      success: { frames: ['body'], fps: 5 },
    },
    home: { x: 0, y: 0 },
  };
  const perch = new Perch(document.createElement('canvas'), { scale: 1 });
  const ctx = perch.ctx;
  perch.marks = [];
  ctx.drawImage = () => perch.marks.push(Math.round(now));
  const found = perch.use(def);
  if (found.length) throw new Error(`${id}: ${found[0]}`);
  return perch;
}

function advance(ms) {
  for (let i = 0; i < Math.round(ms / (1000 / 60)); i++) {
    now += 1000 / 60;
    maxQueued = Math.max(maxQueued, pending.length);
    for (const fn of pending.splice(0)) fn(now);
  }
}

const made = plan.perches.map((rate, i) => make(`stub-${i}`, rate === undefined ? null : rate));
const snaps = {};
let clock = 0;

function perform(action) {
  const perch = made[action.index];
  if (action.op === 'set') perch.set(action.arg);
  else if (action.op === 'flash') perch.flash(action.arg, action.ms);
  else if (action.op === 'destroy') perch.destroy();
  else if (action.op === 'hide') { document.hidden = true; document.fire('visibilitychange'); }
  else if (action.op === 'show') { document.hidden = false; document.fire('visibilitychange'); }
  else if (action.op === 'snapshot') {
    snaps[action.label] = {
      paints: made.map((p) => p.marks.length),
      live: driverState().live,
      running: driverState().running,
      /* When the observation was taken, on the engine's own clock. Snapshots
       * are asked for in the same time base as the paints, so a plan that moves
       * the clock backwards lands here with a time before the events it is
       * trying to observe — and the windows the assertions use would then be
       * measuring the wrong moment without saying so. */
      at: Math.round(now),
    };
  } else throw new Error(`unknown op ${action.op}`);
}

for (const action of plan.actions || []) {
  const at = action.at === undefined ? clock : action.at;
  if (at < clock) throw new Error(`action at ${at} runs the clock back from ${clock}`);
  advance(at - clock);
  clock = at;
  perform(action);
  if (action.then !== undefined) {
    if (action.then < at) throw new Error(`action at ${at} says then ${action.then}`);
    advance(action.then - clock);
    clock = action.then;
  }
}
advance(plan.ms - clock);

console.log(JSON.stringify({
  marks: made.map((p) => p.marks),
  rAFCalls: rafCalls,
  maxQueued,
  live: driverState().live,
  running: driverState().running,
  snaps,
}));
'''


@pytest.fixture(scope='module')
def driver(tmp_path_factory):
    """Load the real engine under node, behind a stubbed canvas and clock."""
    node = _node()
    here = tmp_path_factory.mktemp('driver')
    script = here / 'harness.mjs'
    script.write_text(HARNESS)
    # A `.js` file with no package.json beside it is CommonJS to node, and the
    # engine is a module. Copy it to `.mjs` rather than rewriting it.
    copy = here / 'engine.mjs'

    def run(perches, ms=1000, actions=()):
        proc = subprocess.run(
            [node, str(script), str(copy), str(ENGINE), json.dumps(
                {'perches': list(perches), 'ms': ms, 'actions': list(actions)},
            )],
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


def paints(out, index=0, after=0, before=None):
    """How many times this perch painted, in a window of simulated time.

    Times are counted from the moment the page was mounted, and the harness
    starts its clock at 1000 rather than 0 because the engine reads a zero
    `lastPaint` as "never painted" — the offset is what every window below is
    measured against.
    """
    marks = out['marks'][index]
    start = 1000 + after
    end = 1000 + before if before is not None else None
    return len([t for t in marks if t >= start and (end is None or t < end)])


# -- one loop --------------------------------------------------------------


def test_a_slow_state_is_not_repainted_at_display_rate(driver):
    """The whole point. 0.6fps over one second is one frame, not sixty."""
    out = driver([0.6])
    assert paints(out) == 1, f'a 0.6fps companion painted {paints(out)} times in 1s'


def test_every_rate_is_honoured(driver):
    """Not "slower than 60" — actually the rate the art asked for.

    Measured one-sidedly, because the driver is quantised to the display: it
    can only sample on a frame boundary, so an interval is always rounded *up*
    to the next 16.7ms. A 14fps state has a 71.4ms interval, which is four
    frames — 83.3ms — so it lands at 12fps. That is the correct behaviour and
    not drift to be corrected: painting a frame early would mean painting one
    nobody can see. So the bound is `rate` to `rate` plus a frame.
    """
    rates = [0.6, 0.7, 0.9, 5, 6, 7, 9, 10, 13, 14, 15, 18, 22]
    out = driver(rates)
    for rate, marks in zip(rates, out['marks'], strict=True):
        got = len(marks)
        frames = (1000 / rate) / FRAME_MS
        # Rounding the interval up to a whole number of frames, twice over:
        # once for the interval and once for where the second ends.
        slack = 3 if rate > 1 else 1
        assert rate - slack <= got <= rate + 1, (
            f'{rate}fps painted {got} times in 1s, outside one frame of it '
            f'(its interval is {frames:.2f} frames)'
        )


def test_a_state_with_no_rate_still_runs_at_display_rate(driver):
    """A missing `fps` means "as fast as it can", which is how several of the
    creatures are authored. Reading it as zero would freeze them."""
    out = driver([None])
    assert paints(out) == 60, f'an unrated state painted {paints(out)} times in 1s'


def test_one_loop_serves_every_perch(driver):
    """Eight perches used to mean eight rAF callbacks per frame. The driver is
    a single loop that walks its set, so the callback count is constant in the
    number of perches — measured against a single-perch run rather than a
    literal, so it stays true if the loop's own bookkeeping changes."""
    rates = [0.6, 0.7, 5, 14, 22]
    out = driver(rates, ms=1000)
    alone = driver([22], ms=1000)
    assert out['live'] == 5
    assert out['running'] is True
    assert out['rAFCalls'] == alone['rAFCalls'], (
        f'5 perches cost {out["rAFCalls"]} rAF callbacks, 1 cost {alone["rAFCalls"]}'
    )
    assert out['maxQueued'] == 1, 'more than one frame callback was ever outstanding'


def test_the_loop_stops_when_nothing_is_mounted(driver):
    """An idle page with no companion must not be running a frame loop at
    all. A self-rescheduling rAF is the classic way to keep a page warm
    forever after the thing that wanted it is gone."""
    out = driver([0.6, 14], ms=1000, actions=[{'op': 'snapshot', 'label': 'before'}])
    assert out['snaps']['before']['running'] is True
    assert out['rAFCalls'] > 1, 'the loop never ran at all'
    gone = driver([], ms=1000)
    assert gone['live'] == 0
    assert gone['running'] is False, 'the rAF kept running with an empty set'
    assert gone['rAFCalls'] == 0, 'a frame was requested with no companion mounted'


def test_a_destroyed_perch_is_dropped_from_the_loop(driver):
    """A creature can be carried by an approval bar or a voice strip, both of
    which are torn down. If `destroy` did not take it out of the set, the loop
    would keep a page alive for a perch that no longer exists."""
    out = driver(
        [0.6], ms=1000,
        actions=[{'op': 'destroy', 'index': 0, 'at': 300},
                 {'op': 'snapshot', 'label': 'after', 'at': 600}],
    )
    at_teardown = paints(out)
    assert at_teardown == 1, f'{at_teardown} paints before teardown; it was not actually slow'
    assert out['snaps']['after']['live'] == 0
    assert out['snaps']['after']['running'] is False
    assert len(out['marks'][0]) == at_teardown, 'a destroyed perch was painted again'


def test_a_hidden_tab_does_not_run_the_driver(driver):
    """A companion animating in a tab nobody is looking at is waste with a
    battery attached."""
    out = driver(
        [22], ms=2000,
        actions=[{'op': 'hide', 'at': 200, 'then': 800},
                 {'op': 'snapshot', 'label': 'hidden'},
                 {'op': 'show', 'at': 1000, 'then': 1000}],
    )
    assert paints(out, before=200) >= 4, 'it never painted while visible'
    assert out['snaps']['hidden']['live'] == 0
    hidden = paints(out, after=200, before=1000)
    assert hidden == 0, f'{hidden} paints in the 800ms the tab was hidden'
    assert out['running'] is True, 'it did not come back when the tab was shown again'
    assert paints(out, after=1000) >= 10, 'it stayed stopped after the tab was shown'


def test_eight_perches_cost_far_less_than_eight_loops(driver):
    """The measured claim, as a bound rather than a vibe.

    Eight perches at display rate is 480 paints a second. Paced to the rates
    the art actually declares, the same page is a fraction of that.
    """
    out = driver([0.6, 0.7, 0.9, 14, 22, 5, 0.7, 22])
    total = sum(len(marks) for marks in out['marks'])
    assert total < 120, f'{total} paints/s for 8 perches; the old loops did 480'


# -- and the rate follows what is being drawn ------------------------------


def test_a_state_change_is_not_waited_for_by_the_old_rate(driver):
    """The gap is the whole point of the creature. Coming out of a 0.6fps idle
    drift, a companion has to repaint on the next frame — not up to 1.6s
    later, which is what it looks like when it is late."""
    out = driver(
        [0.6], ms=1000,
        actions=[{'op': 'set', 'index': 0, 'arg': 'running', 'at': 300}],
    )
    assert paints(out, after=300) >= 10, (
        f'{paints(out, after=300)} paints in the 700ms after the state change'
    )
    assert paints(out, before=300) <= 1, 'it was not actually slow before the change'


def test_a_flash_is_not_waited_for_by_the_old_rate(driver):
    """Success and failure are moments, not conditions. A companion sitting in
    a 0.6fps idle has to show the failure it just had straight away."""
    out = driver(
        [0.6], ms=1000,
        actions=[{'op': 'flash', 'index': 0, 'arg': 'success', 'ms': 300, 'at': 300}],
    )
    assert paints(out, after=300) >= 2, (
        f'{paints(out, after=300)} paints in the 700ms after the flash; '
        f'it was paced by the 0.6fps idle it was sitting in'
    )


def test_a_fast_state_is_slow_again_once_it_is_over(driver):
    """Both directions. Pacing that only ever ratchets faster is not pacing:
    the interval comes from the state being drawn, not from the last fast one
    or from a running maximum."""
    out = driver(
        [0.6], ms=3000,
        actions=[{'op': 'set', 'index': 0, 'arg': 'running', 'at': 1000, 'then': 1000},
                 {'op': 'set', 'index': 0, 'arg': 'idle', 'at': 2000}],
    )
    assert paints(out, after=1000, before=2000) >= 10, 'it never sped up'
    assert paints(out, after=2000) <= 2, f'{paints(out, after=2000)} paints in 1s back at 0.6fps'


def test_the_rate_comes_from_what_is_being_drawn_not_from_the_last_one(driver):
    """A flash hands back to the ambient state on its own, with nothing
    telling the driver the state changed. If the interval is carried over from
    the last frame drawn rather than recomputed from the one now on screen, a
    companion that drifts at 0.6fps keeps painting at the flash's pace after
    the flash is over — the frames are identical, and it does so forever,
    because nothing else resets the deadline.

    The engine recomputes after drawing, in `tick`, for this reason. A flash at
    5fps over a 0.6fps idle is the case that separates the two: the deadline
    has to drop from 200ms back to 1.6s at the moment the flash ends.
    """
    out = driver(
        [0.6], ms=1500,
        actions=[{'op': 'flash', 'index': 0, 'arg': 'success', 'ms': 300, 'at': 100}],
    )
    assert paints(out, after=100, before=400) >= 2, 'the flash never animated'
    after = paints(out, after=500)
    assert after <= 2, (
        f'{after} paints in 1s once the flash was over, but the ambient state is 0.6fps'
    )


# -- and the art is allowed to be slow in the first place ------------------


def test_the_slow_states_are_really_that_slow():
    """The pacing above is only correct if states below display rate exist.
    If someone raised `moth`'s idle to 12fps, the test above would still pass
    while the page got more expensive — so the art's own rates are checked."""
    slow = []
    for path in sorted(COMPANIONS.glob('*.js')):
        if path.name in {'engine.js', 'index.js'}:
            continue
        for rate in re.findall(r'fps:\s*([\d.]+)', path.read_text()):
            if float(rate) < 1:
                slow.append((path.name, float(rate)))
    assert slow, 'no state is slower than 1fps; the pacing test above is then vacuous'
    for name, rate in slow:
        assert rate > 0, f'{name} has an fps of {rate}, which would freeze the companion'

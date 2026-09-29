"""Does the interface hold a frame while a turn is actually running?

Every smoothness claim in this project was made by reading code. This one is
made by driving a real turn against a real provider and timing the frames,
because the thing that was wrong was not visible in the source at all:

    at 880 transcript nodes, streaming text alone      16.7ms   (one vsync)
    at 880 nodes, appending a tool card alone          16.7ms   (one vsync)
    both, with a layout read and scroll write          33.3ms   (two)

The cost was not the transcript and not the card. It was doing a synchronous
layout read *per append* — a card append invalidates layout, the next
`scrollHeight` forces it back, and a frame with four appends forced layout four
times. The fix is to ask the question once per frame rather than once per
append, and this is the test that says the fix is in.

**What is measured** is frame intervals across a real turn: real deltas, real
tool cards, real scroll following, the companion animating. A synthetic loop
that resembles it proved the number was real but not which code caused it, and
this is the one that closes that gap.

**Skipped rather than failed** with no provider configured or no browser —
there is nothing to measure and a number from a stub is not one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MEASURE = r'''
import json, os, subprocess, sys, time, tempfile, pathlib, shutil, urllib.request

HERE = sys.argv[1]
PY = sys.argv[2]
PORT = sys.argv[3]

from playwright.sync_api import sync_playwright

work = pathlib.Path(tempfile.mkdtemp())
for n in range(40):
    (work / f'module{n}.py').write_text(f'def f{n}():\n    return {n}\n')

env = {**os.environ, 'OPENMIRROR_TOKEN': '', 'OPENMIRROR_PORT': PORT}
proc = subprocess.Popen([PY, '-m', 'openmirror.main'], cwd=HERE, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{PORT}/healthz', timeout=1)
        break
    except Exception:
        time.sleep(0.25)
else:
    proc.kill(); raise SystemExit('no daemon')

out = {'ok': False}
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=['--disable-gpu-vsync'])
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.goto(f'http://127.0.0.1:{PORT}/', wait_until='networkidle')

        # The session is created *through the page*, not through the API.
        # Created server-side, the page discovers it on its next poll and
        # calls `selectSession`, which clears the transcript — so anything
        # filled in beforehand is gone by the time the turn runs, and the
        # frames measured describe an empty page.
        #
        # That is not a hypothetical: this version of the test first created
        # the session over HTTP, reported "0 frames over 20ms", and was
        # measuring two nodes. Sampling the count *during* the turn is what
        # showed it, and is why the sample is below.
        page.evaluate("() => document.getElementById('new-dialog').showModal()")
        page.wait_for_timeout(300)
        page.fill("#new-dialog [name='root']", str(work))
        page.click('#create-session')
        page.wait_for_timeout(4000)

        page.evaluate("""() => {
          const t = document.getElementById('transcript');
          for (let i = 0; i < 850; i++) {
            const turn = document.createElement('div');
            turn.className = 'turn';
            const body = document.createElement('div');
            body.className = 'body';
            body.textContent = 'An earlier answer. '.repeat(30);
            turn.appendChild(body);
            t.appendChild(turn);
          }
        }""")
        page.wait_for_timeout(400)
        out['filled'] = page.evaluate("document.getElementById('transcript').children.length")
        if out['filled'] < 500:
            raise SystemExit('the transcript was not full: %s' % out['filled'])

        # Frames, for as long as the turn takes. Started before the turn and
        # stopped after it, so the streaming frames are the ones counted and
        # not the idle ones.
        # The node count is sampled *during* the turn, not only before and
        # after it. The page has its own session lifecycle, and a session
        # arriving from the server between filling the transcript and sending
        # clears it — so a count taken at the end reads 7, and a test that
        # trusted it would be measuring an empty transcript and passing
        # whatever the code did.
        page.evaluate("""() => {
          window.__gaps = [];
          window.__sizes = [];
          window.__watching = true;
          let last = performance.now();
          (function tick(now) {
            window.__gaps.push(now - last);
            last = now;
              if (window.__gaps.length % 30 === 0) {
              window.__sizes.push(document.getElementById('transcript').children.length);
            }
            if (window.__watching) requestAnimationFrame(tick);
          })(performance.now());
        }""")

        page.fill('#input', 'List the files in this directory and read module1.py.')
        page.click('#send')
        # Wait for the turn to actually finish rather than for a fixed time.
        for _ in range(200):
            page.wait_for_timeout(500)
            if page.evaluate("document.getElementById('send').disabled === false "
                             "&& document.getElementById('stop').hidden"):
                break
        page.wait_for_timeout(800)
        page.evaluate("() => { window.__watching = false; }")

        gaps = page.evaluate("() => window.__gaps.slice(1)")
        sizes = page.evaluate("() => window.__sizes")
        out['frames'] = len(gaps)
        out['during_min'] = min(sizes) if sizes else 0
        out['during_max'] = max(sizes) if sizes else 0
        out['nodes'] = page.evaluate("document.getElementById('transcript').children.length")
        if len(gaps) > 20:
            s = sorted(gaps)
            out['median'] = round(s[len(s) // 2], 2)
            out['p95'] = round(s[int(len(s) * 0.95)], 2)
            out['p99'] = round(s[int(len(s) * 0.99)], 2)
            out['over_20ms'] = sum(1 for g in gaps if g > 20)
            out['worst'] = round(s[-1], 2)
        out['ok'] = True
        browser.close()
finally:
    proc.kill()
    shutil.rmtree(work, ignore_errors=True)

print(json.dumps(out))
'''


@pytest.fixture(scope='module')
def streamed() -> dict:
    candidates = [ROOT / '.venv' / 'bin' / 'python', shutil.which('python3') or 'python3']
    python = None
    for candidate in candidates:
        found = str(candidate) if Path(candidate).exists() else shutil.which(str(candidate))
        if found and 'playwright' in subprocess.run(
            [found, '-c', 'import playwright'], capture_output=True, text=True, timeout=60
        ).stdout + '' or (found and subprocess.run(
            [found, '-c', 'import playwright'], capture_output=True, text=True, timeout=60
        ).returncode == 0):
            python = found
            break
    if python is None:
        pytest.skip('no interpreter with playwright on this machine')

    # A model to answer with. Without one there is no turn to time, and a
    # number from a stub would be a number about the stub.
    from openmirror.config import config

    if not config.chat_provider and not (config.perch_host and config.perch_token) and not config.openrouter_key:
        pytest.skip('no provider configured, so there is no turn to measure')

    with tempfile.TemporaryDirectory() as scratch:
        script = Path(scratch) / 'live.py'
        script.write_text(MEASURE)
        port = str(8600 + (os.getpid() % 90))
        result = subprocess.run(
            [python, str(script), str(ROOT), python, port],
            capture_output=True, text=True, timeout=600, env={**os.environ},
        )
    if result.returncode != 0:
        lowered = (result.stdout + result.stderr).lower()
        if any(word in lowered for word in ('executable', 'browserType', 'playwright install')):
            pytest.skip('no usable browser')
        raise AssertionError(f'the measurement failed:\n{result.stdout}\n{result.stderr}')
    got = json.loads(result.stdout.strip().splitlines()[-1])
    if not got.get('ok'):
        pytest.skip(f'the turn did not run: {got}')
    if got.get('frames', 0) < 40:
        pytest.skip(f'too few frames to say anything: {got}')
    return got


def test_a_real_turn_holds_its_frames_in_a_long_transcript(streamed):
    """The claim, measured on the code that actually runs.

    A frame is 16.7ms at 60Hz. A median over 20ms is a dropped frame in the
    middle of ordinary work, and the number to watch is the tail: a good median
    with a bad p99 is a burst of work somewhere, which is the thing worth
    looking at and the thing a screenshot never shows.
    """
    assert streamed['filled'] > 500, f"the transcript was not full: {streamed['filled']}"
    # The claim is about a *full* transcript, so it has to have been full while
    # the turn ran and not merely just before it.
    assert streamed['during_min'] > 400, (
        f"the transcript was down to {streamed['during_min']} nodes during the turn — "
        f"the frames below describe an empty transcript, not a full one"
    )
    assert streamed['median'] < 20, f"a median frame of {streamed['median']}ms is a dropped frame"
    assert streamed['p99'] < 45, f"1% of frames took over {streamed['p99']}ms"
    # Under 3% over 20ms. A streaming answer runs for seconds; a frame budget
    # blown every twentieth frame is visible as a hitch rather than a stall.
    share = streamed['over_20ms'] / streamed['frames']
    assert share < 0.03, f"{share:.1%} of frames were over 20ms ({streamed['over_20ms']}/{streamed['frames']})"


def test_the_transcript_is_pruned_rather_than_grown_without_limit(streamed):
    """Past 900 nodes the oldest whole turns go, so the numbers above are the
    cost of a transcript at its designed ceiling rather than of one that has
    been left running all day."""
    assert streamed['nodes'] <= 1000, f"{streamed['nodes']} nodes survived pruning"

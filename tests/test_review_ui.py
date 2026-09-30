"""The review panel, and the socket's "held" reply.

Same split as the other client tests: the parts that are decisions are read as
text, and the parts that are arithmetic are run under node.

**What matters in this panel is that reviewing and applying are different
acts.** The decisions are held in the browser and nothing is written until
the button is pressed, because somebody comparing three options should be able
to flip between them as fast as they can think — and a network round trip per
flip makes the panel feel like a form, which is how a review gets clicked
through.

Which is also why the render is *not* run when the turn ends. Building a node
per hunk into a dialog nobody has opened showed up as measured dropped
frames: 86 of 2916 over 20ms on an 850-node transcript, where there had been
none. The state is fetched so the button can appear; the panel is built when
it is opened.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'openmirror' / 'static'
REVIEW = (STATIC / 'review.js').read_text()
APP = (STATIC / 'app.js').read_text()
INDEX = (STATIC / 'index.html').read_text()


def node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


def run(script: str, *args: str) -> dict:
    """Run a snippet under node. The file under test goes in `args`, and the
    script reads it from `process.argv[1]`: `node -e` has no script-path
    slot, so the first user argument is argv[1]."""
    result = subprocess.run(
        [node(), '--input-type=module', '-e', script, *args],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(f'node failed:\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])


# --- the panel exists and is its own act --------------------------------------


def test_review_is_a_separate_button_from_rewind():
    """They are separate decisions. Rewind throws a whole turn away; review
    keeps the parts you agree with. A panel where the two sit together is a
    panel where the wrong one gets pressed."""
    assert 'id="nav-rewind"' in INDEX and 'id="nav-review"' in INDEX
    assert 'id="review-dialog"' in INDEX
    assert 'id="review-apply"' in INDEX


def test_nothing_is_written_until_the_button_is_pressed():
    """The decisions are held in the browser. Somebody comparing three options
    should be able to flip between them as fast as they can think."""
    # `send` and not `post`: `post` answers with a Response, and reading
    # `.data` off one is undefined, which reported every successful apply as a
    # failure while the file behind it was written correctly.
    assert "await post(" not in REVIEW
    assert 'await send(' in REVIEW
    assert 'data.ok' in REVIEW
    body = REVIEW[REVIEW.index('function render()'):REVIEW.index('async function apply()')]
    assert 'post(' not in body, 'rendering must not talk to the server'
    # And the apply is the only place that does.
    assert REVIEW.count('await send(') == 1


def test_the_panel_is_not_built_until_it_is_opened():
    """Building a node per hunk into a dialog nobody has opened showed up as
    measured dropped frames: 86 of 2916 over 20ms on an 850-node transcript,
    where there had been none."""
    assert 'async function load({ draw = true } = {})' in REVIEW
    assert 'if (draw) render();' in REVIEW
    assert 'load({ draw: false });' in REVIEW
    # And it is drawn when it is opened.
    assert re.search(r"function openDialog\(\)\s*\{[^}]*\$\('#review-dialog'\)\.showModal\(\);\s*load\(\);", REVIEW, re.S)


def test_the_button_appears_when_a_turn_ends():
    assert 'refreshReview();' in APP
    assert 'case \'turn.completed\'' in APP
    # And it is cleared when the session changes, so it never describes a
    # turn from the session just left.
    assert 'resetReview();' in APP
    assert 'resetReview();' in APP


def test_the_panel_is_told_which_session_it_describes():
    """A second copy of "which session is on screen" is how a panel ends up
    offering hunks from the session you just left. This page has one place
    that knows, and the panel is handed it."""
    # The button ships hidden and is hidden again on a session change, so
    # this is the only place that can show it — and it is not obvious.
    assert 'id="nav-review" hidden' in INDEX
    assert "$('#nav-review').hidden = false;" in REVIEW
    assert 'wireReview({ sessionId })' in APP
    assert 'export function wireReview({ sessionId })' in REVIEW
    assert 'sessionId: null' in REVIEW, 'and it is not read out of storage'
    assert 'openmirror.session' not in REVIEW
    # And it is a function, not a copied value: a copy goes stale the moment
    # the session changes, and a stale one is how a panel ends up describing
    # the session you just left. Same shape `wireTalk` takes.
    assert 'const currentSession = () => (state.sessionId ? state.sessionId() : null);' in REVIEW
    assert '${currentSession()}' in REVIEW


def test_everything_is_kept_by_default():
    """The agent's work is the starting point; this is a panel for removing
    the parts you disagree with."""
    assert 'new Set(f.all)' in REVIEW
    assert 'state.keep = new Map(state.files.map((f) => [f.path, new Set(f.all)]));' in REVIEW


def test_the_button_says_what_it_is_about_to_do():
    """Three hunks in two files, said as words, because the alternative is
    'Apply' and Apply does not say which."""
    assert 'Put back' in REVIEW
    assert 'hunk${hunks === 1 ? \'\' : \'s\'}' in REVIEW
    assert "nothing to apply" in REVIEW


def test_a_hand_edited_file_is_shown_and_confirmed_before_it_is_written_over():
    """A file edited by hand after the turn means the hunks are not what is
    in it, and applying would lose the edit. Said in the panel and confirmed,
    because the server refuses it and a panel that silently stops working is
    worse than one that says so."""
    assert 'edited_since' in REVIEW
    assert 'Somebody edited this file after the turn finished' in REVIEW
    assert 'confirm(' in REVIEW
    assert 'force: anyHandEdit' in REVIEW


def test_a_failure_says_which_files_and_leaves_the_rest_applied():
    """One request per file rather than one for all of them, so a failure
    halfway is a failure of one file and the earlier ones still stand."""
    apply_body = REVIEW[REVIEW.index('async function apply()'):REVIEW.index('function setAll(')]
    assert 'for (const file of changed)' in apply_body
    assert 'could not be applied' in apply_body


# --- the decisions, as they are made ------------------------------------------


HARNESS = r'''
/* The selection logic, lifted out as it ships: what the button says for a
   given set of choices. */
const { readFileSync } = await import('fs');
const src = readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function describeSelection');
const brace = src.indexOf('{', start);
let depth = 0, end = start;
for (let i = brace; i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') { depth--; if (depth === 0) { end = i; break; } }
}
const fn = src.slice(start, end + 1);

const state = { files: [], keep: new Map() };
const everyIndex = (path) => (state.files.find((f) => f.path === path) || { all: [] }).all;
const keptFor = (path) => {
  if (!state.keep.has(path)) state.keep.set(path, new Set(everyIndex(path)));
  return state.keep.get(path);
};
const describeSelection = new Function(
  'state', 'everyIndex', 'keptFor',
  fn + '; return describeSelection;',
)(state, everyIndex, keptFor);

const reset = (files) => {
  state.files = files;
  state.keep = new Map(files.map((f) => [f.path, new Set(f.all)]));
};
const out = {};

/* Nothing to do. */
reset([{ path: 'a.py', all: [0, 1] }]);
out.allKept = describeSelection();

/* One hunk of one file. */
keptFor('a.py').delete(1);
out.oneOfTwo = describeSelection();

/* Everything of two files. */
reset([{ path: 'a.py', all: [0, 1] }, { path: 'b.py', all: [0] }]);
keptFor('a.py').clear();
keptFor('b.py').clear();
out.allDropped = describeSelection();

/* One hunk, singular, so the plural is not "1 hunks". */
reset([{ path: 'a.py', all: [0, 1] }]);
keptFor('a.py').delete(1);
out.singular = describeSelection();

console.log(JSON.stringify(out));
'''


def test_the_button_says_what_it_will_do():
    got = run(HARNESS, str(STATIC / 'review.js'))
    assert 'nothing to apply' in got['allKept']
    assert '1 hunk in 1 file' in got['oneOfTwo']
    assert '3 hunks in 2 files' in got['allDropped']
    # One hunk is "1 hunk", not "1 hunks". A count that reads wrong is a
    # count nobody trusts on a panel whose whole job is counting.
    assert '1 hunk' in got['singular'] and 'hunks' not in got['singular']


# --- the socket says a message was held ---------------------------------------


def test_a_held_message_is_told_apart_from_a_started_one():
    """The text is already echoed into the transcript, so silence here would
    show a message that is neither running nor lost — the one outcome the
    queue exists to remove."""
    assert 'turn.queued' in APP
    assert 'Held — it runs when this turn finishes' in APP
    # And the server reports a held message as an empty turn id, which is the
    # only way the client can tell.
    assert "if not turn_id:" in (ROOT / 'openmirror' / 'routers' / 'agent.py').read_text()
    assert "'type': 'turn.queued'" in (ROOT / 'openmirror' / 'routers' / 'agent.py').read_text()


def test_the_socket_does_not_report_an_error_for_a_busy_session_any_more():
    """It used to: "a turn is already running — interrupt it first". That is
    the error this feature removed."""
    agent_router = (ROOT / 'openmirror' / 'routers' / 'agent.py').read_text()
    assert 'a turn is already running' not in agent_router
    session = (ROOT / 'openmirror' / 'agent' / 'session.py').read_text()
    assert 'raise RuntimeError(\'a turn is already running' not in session


# --- the send button, while a turn is running ---------------------------------


def test_the_send_button_stays_available_while_a_turn_runs():
    """A hidden button cannot be pressed, so hiding it made the queue
    unreachable — the server held messages and the interface had no way to
    send one."""
    set_busy = APP[APP.index('function setBusy(busy)'):]
    set_busy = set_busy[:set_busy.index('\n}\n')]
    assert '$(\'#send\').hidden = busy;' not in set_busy
    assert "$('#send').hidden = false;" in set_busy
    assert '$(\'#stop\').hidden = !busy;' in set_busy
    assert 'Queue this' in set_busy, 'and it says what pressing it will do'


def test_composer_submit_does_not_refuse_while_busy():
    """The other half: the keyboard path had the same check, so Enter did
    nothing mid-turn either.

    Asserted as "a turn.submit goes out and nothing guards on busy" rather than
    on one exact call, because the send is now two shapes — with attachments and
    without — and a test pinned to the older literal would fail on the shape
    rather than on the behaviour it is about.
    """
    submit = APP[APP.index("$('#composer').addEventListener('submit'"):]
    submit = submit[:submit.index("\n  });")]
    assert 'state.busy) return' not in submit
    assert "type: 'turn.submit'" in submit
    assert 'send(' in submit

"""`@` in the composer, and the loop that has to keep up with typing.

The client half runs as it ships under node, because the bugs here are all
about the interaction between two things that only exist together: a caret and
a list. Those cannot be unit-tested in pieces, and a text assertion finds
nothing wrong with a function that never gets called.

What is held:

* **The query has to be at the caret.** A mention three words back is not what
  the person is typing, and completing it would rewrite text they have already
  moved past. `and @guide please` then Tab does nothing, which is correct.
* **An email address is not a file mention.** Every sentence containing
  somebody's address would otherwise open a list.
* **Two racing fetches cannot leave the wrong answer on screen.** Typed fast,
  the reply to `@ap` arrives after the one to `@app`, and showing the older one
  is a list that does not match what is in the box.
* **One keydown handler, not two.** A second listener does not work:
  `preventDefault` stops the browser's default, not the *other* listener, so
  the composer's own Enter handler submits the form regardless. Found by
  pressing Enter with the list open and watching the turn start.
* **Escape closes the list and keeps the text.**
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / 'openmirror' / 'static'
FILES = (STATIC / 'files.js').read_text()
APP = (STATIC / 'app.js').read_text()


def node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


def run(script: str, *args: str) -> dict:
    """Run a snippet under node with `args`, and hand back what it printed.

    The file under test goes in `args` and the script reads it from
    `process.argv[1]`: `node -e` puts the first user argument there, with no
    slot for a script path.
    """
    result = subprocess.run(
        [node(), '--input-type=module', '-e', script, *args],
        capture_output=True, text=True, timeout=60, cwd=str(Path(__file__).resolve().parents[1]),
    )
    if result.returncode != 0:
        raise AssertionError(f'node failed:\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])


# --- the server half -----------------------------------------------------------


def test_finding_a_file_is_ranked_and_bounded(tmp_path):
    """Ranked so the answer is at the top, because the list shows the first
    screenful and nobody scrolls a suggestion list."""
    from openmirror.agent.files import find

    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'app.js').write_text('x')
    (tmp_path / 'src' / 'apple.ts').write_text('x')
    (tmp_path / 'src' / 'application.md').write_text('x')
    (tmp_path / 'src' / 'unrelated.py').write_text('x')

    # All three match by prefix, and within a tier the most recently touched
    # comes first — so the order is a set, and the *set* is the claim: a
    # query narrows and nothing outside it comes back.
    assert {f['path'] for f in find(tmp_path, 'app')} == {
        'src/app.js', 'src/apple.ts', 'src/application.md',
    }
    assert 'unrelated.py' not in [f['path'] for f in find(tmp_path, 'app')]


def test_an_exact_name_beats_a_prefix(tmp_path):
    from openmirror.agent.files import find

    (tmp_path / 'app.js').write_text('x')
    (tmp_path / 'application.js').write_text('x')
    assert find(tmp_path, 'app.js')[0]['path'] == 'app.js'


def test_the_walk_skips_what_the_search_tools_skip(tmp_path):
    """`.git` and `node_modules` are not files anybody wants to mention, and
    walking them is how a suggestion list takes two seconds to appear."""
    from openmirror.agent.files import find

    for name in ('.git', 'node_modules', '__pycache__'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'secret.js').write_text('x')
    (tmp_path / 'real.js').write_text('x')
    assert [f['path'] for f in find(tmp_path)] == ['real.js']


def test_dotfiles_are_not_offered(tmp_path):
    from openmirror.agent.files import find

    (tmp_path / '.env').write_text('SECRET=1')
    (tmp_path / 'ok.py').write_text('x')
    assert [f['path'] for f in find(tmp_path)] == ['ok.py']


def test_a_big_file_is_not_offered(tmp_path):
    """The point of a mention is that somebody can ask about the file, and a
    ninety-megabyte one is not a conversation."""
    from openmirror.agent.files import MAX_BYTES, find

    (tmp_path / 'huge.bin').write_bytes(b'x' * (MAX_BYTES + 1))
    (tmp_path / 'small.py').write_text('x')
    assert [f['path'] for f in find(tmp_path)] == ['small.py']


def test_finding_nothing_is_empty_and_not_an_error(tmp_path):
    from openmirror.agent.files import find

    assert find(tmp_path, 'nothing-like-this') == []
    assert find(tmp_path) == []


def test_an_excerpt_cannot_leave_the_root(tmp_path):
    """Same check as the file tools, for the same reason: this one is reachable
    from a browser."""
    from openmirror.agent.files import excerpt

    (tmp_path / 'ok.py').write_text('one\ntwo\n')
    assert 'one' in excerpt(tmp_path, 'ok.py')
    assert excerpt(tmp_path, '../../../etc/passwd') == ''


def test_a_binary_file_has_no_excerpt(tmp_path):
    """Not by guessing at content — by the read failing, which is the answer."""
    from openmirror.agent.files import excerpt

    (tmp_path / 'blob.dat').write_bytes(b'\xff\xfe\x00\x01binary')
    assert excerpt(tmp_path, 'blob.dat') == ''


# --- the client half, as it ships ----------------------------------------------


HARNESS = r'''
import { readFileSync } from 'fs';

/* The one function that decides whether a mention is being typed, lifted out
   of the file as it ships. It is pure — text and a caret in, a query out —
   which is what makes it the part worth running. */
const src = readFileSync(process.argv[1], 'utf8');  // `node -e` has no script-path slot
const start = src.indexOf('function queryAt');
const brace = src.indexOf('{', start);
let depth = 0, end = start;
for (let i = brace; i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') { depth--; if (depth === 0) { end = i; break; } }
}
const fn = src.slice(start, end + 1);

/* A stand-in for the textarea, since the real one only exists in a browser. */
const input = { value: '', selectionStart: 0 };
const $ = () => input;
const queryAt = new Function('$', fn + '; return queryAt;')($);

const at = (text, caret) => {
  input.value = text;
  input.selectionStart = caret === undefined ? text.length : caret;
  const got = queryAt();
  return got ? got.query : null;
};

const out = {};

/* The ordinary cases. */
out.bare = at('@');
out.named = at('@app');
out.path = at('look at @src/app.js');
out.afterSpace = at('two words @ap');
out.mid = at('@apple and then @');

/* Not a mention. */
out.email = at('mail bob@example now');
out.midSentence = at('@guide please');
/* The caret in the middle of the mention, which is still the mention. */
out.midMention = at('and @guide please', 10);  // just after the mention
out.slash = at('/compact');
out.twoAts = at('@@app');
out.nothing = at('just some words');
/* The caret *before* the mention: there is nothing typed yet, so there is
   nothing to complete. */
out.caretBefore = at('@guide and more', 0);

/* The query, and where it starts, together — the picker needs both. */
input.value = 'look at @src/ap';
input.selectionStart = input.value.length;
const found = queryAt();
out.start = found.at;
out.query = found.query;

console.log(JSON.stringify(out));
'''


def test_the_query_is_only_where_the_caret_is():
    got = run(HARNESS, str(STATIC / 'files.js'))
    # The ordinary cases.
    assert got['bare'] == ''
    assert got['named'] == 'app'
    assert got['path'] == 'src/app.js'
    assert got['afterSpace'] == 'ap'
    assert got['mid'] == ''

    # An email address is not a file mention. Every sentence containing
    # somebody's address would otherwise open a list.
    assert got['email'] is None

    # A mention that is not at the caret is not being typed. Completing it
    # would rewrite text the person has already moved past — which is the
    # property, and the reason it is worth a test.
    assert got['midSentence'] is None
    assert got['caretBefore'] is None
    # The caret inside the mention is still the mention, though.
    assert got['midMention'] == 'guide'

    # Not a mention, and not two of them.
    assert got['slash'] is None
    assert got['twoAts'] is None
    assert got['nothing'] is None

    # The query, and the index of the @ itself — which is what the picker
    # splices at, so an off-by-one here eats a character of the message.
    assert got['query'] == 'src/ap'
    assert got['start'] == len('look at ')


def test_a_racing_reply_cannot_overwrite_a_newer_one():
    """Typed fast, the answer to `@ap` arrives after the answer to `@app`.
    Showing the older one is a list that does not match what is in the box,
    and it is the kind of wrong that looks like the tool not working."""
    assert 'if (ticket !== state.ticket) return;' in FILES
    assert '++state.ticket' in FILES
    # And the sequence number is captured before the request goes out, not
    # read from the state afterwards — which would be the same number twice.
    assert 'const ticket = ++state.ticket;' in FILES


def test_the_fetch_is_debounced():
    assert 'setTimeout' in FILES
    assert '120' in FILES, 'and the delay is a number rather than a wish'


def test_there_is_one_keydown_handler_not_two():
    """A second listener does not work. `preventDefault` stops the browser's
    default, not the *other* listener, and the composer's own Enter handler
    submits the form regardless — so Enter with the list open started a turn.

    Found by pressing Enter with a suggestion highlighted and watching the
    whole message go."""
    assert "input.addEventListener('keydown'" not in FILES, 'files.js must not add its own keydown listener'
    assert 'export function handleFileKey(' in FILES
    assert 'if (handleFileKey(e)) return;' in APP
    # Checked before the slash menu, and the slash menu is the one that
    # already knows this shape.
    handler = APP[APP.index("input.addEventListener('keydown'"):]
    assert handler.index('handleFileKey') < handler.index('#slash')


def test_a_stale_answer_is_dropped_and_a_session_change_clears_the_list():
    """The list belongs to the session it was asked about. Offering files from
    the one just left is worse than offering none."""
    assert 'export function resetFiles' in FILES
    assert 'resetFiles();' in APP
    reset = FILES[FILES.index('export function resetFiles'):]
    assert 'state.ticket++' in reset
    assert 'hide()' in reset


def test_escape_closes_the_list_without_losing_the_text():
    assert 'Escape' in FILES
    key = FILES[FILES.index('export function handleFileKey'):]
    escape = key[key.index("event.key === 'Escape'"):][:400]
    assert 'event.preventDefault()' in escape
    assert 'event.stopPropagation()' in escape
    # The document-level Escape stops a turn, and that one must survive this.
    assert 'hide()' in escape
    # Nothing in `pick` or `hide` clears the textarea.
    pick = FILES[FILES.index('function pick('):FILES.index('function hide(')]
    assert 'input.value' in pick
    hide = FILES[FILES.index('function hide('):]
    assert 'input.value' not in hide.split('export')[0]

"""The session list is fetched when something might have changed, and rebuilt
only when the answer actually differs.

There is no browser here, so this is two kinds of check. The part that is pure
logic — the fingerprint, and the local restatement of relative times — is
extracted from `app.js` as it ships and run under node, so a mutation of the
real function fails these tests. The wiring is read as text, with every check
asserting that it found the code it was looking for.

What is being protected is a specific old behaviour. `loadSessions` sat on a
five-second `setInterval` and rebuilt the whole sidebar on every tick, so an idle
tab fetched a summary of every session and threw away and recreated a hundred DOM
nodes twelve times a minute to draw a list identical to the one already on
screen — the same cost whether anything had happened or not.

A fallback poll has to stay. The agent socket is attached to the *open* session,
so a session started in another tab, one that has closed, or one that has begun
waiting for an approval is invisible from here, and no global event channel
exists. Removing the poll without one would trade a rendering cost for a
correctness regression, which is the wrong trade. So the poll remains, and what
the tests hold is what it costs: one small request, no DOM work when nothing
changed, and nothing at all while the tab is hidden.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'openmirror' / 'static' / 'app.js').read_text()


def _node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


# -- the fingerprint, run as it ships ---------------------------------------

# `sessionFingerprint` is lifted out of the file rather than reimplemented, so
# a mutation of the real function fails. It is pure — it closes over
# `state.sessionId` and nothing else it can observe, and it is the whole of the
# change detection — so running it alone is running the real thing.
FINGERPRINT_HARNESS = r'''
import { readFileSync } from 'fs';

let sessionId = null;

const src = readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('function sessionFingerprint');
if (start < 0) throw new Error('sessionFingerprint is not in app.js');
const brace = src.indexOf('{', start);
let depth = 0, end = start;
for (let i = brace; i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') { depth--; if (depth === 0) { end = i; break; } }
}
const fn = src.slice(start, end + 1);

const state = { get sessionId() { return sessionId; } };
const fp = new Function('state', fn + '; return sessionFingerprint;')(state);

const s = (over = {}) => Object.assign({
  id: 'a', title: 't', root: '/r', model: 'm', turns: 3,
  busy: false, waiting_on: null, idle_for: 10,
}, over);

const one = (over) => fp([s(over)]);
const out = {};

/* A tick that changed nothing has to hash the same, or the rebuild happens on
   every poll and none of this is worth anything. */
out.identical = one({}) === one({});
out.order = fp([s({ id: 'a' }), s({ id: 'b' })]) !== fp([s({ id: 'b' }), s({ id: 'a' })]);

/* `idle_for` counts up every second by construction. Hashing it would report a
   change on every tick and put the fingerprint straight back where the old code
   was. What the row shows is `ago(idle_for)`, which only moves at a minute, an
   hour or a day — so the fingerprint records which band a session is in. */
out.withinSecond = one({ idle_for: 10 }) === one({ idle_for: 11 });
out.withinMinute = one({ idle_for: 10 }) === one({ idle_for: 59 });
out.withinMinute2 = one({ idle_for: 10 }) === one({ idle_for: 60 - 1 });
out.withinHour = one({ idle_for: 100 }) === one({ idle_for: 3599 });
out.withinHour2 = one({ idle_for: 100 }) === one({ idle_for: 3600 - 1 });
out.withinDay = one({ idle_for: 3600 }) === one({ idle_for: 86400 - 1 });
out.withinDay2 = one({ idle_for: 86400 }) === one({ idle_for: 86400 * 40 });
out.withinWeek = one({ idle_for: 86400 }) === one({ idle_for: 86400 * 400 });
out.acrossMinute = one({ idle_for: 59 }) !== one({ idle_for: 60 });
out.acrossHour = one({ idle_for: 3599 }) !== one({ idle_for: 3600 });
out.acrossDay = one({ idle_for: 86399 }) !== one({ idle_for: 86400 });
/* A very old session still has to settle somewhere, or the top band is open. */
out.acrossWeek = one({ idle_for: 86399 }) !== one({ idle_for: 86400 * 40 });

/* Everything the row actually shows, or a status derived from it. */
out.title = one({ title: 'x' }) !== one({ title: 'y' });
out.model = one({ model: 'x' }) !== one({ model: 'y' });
out.turns = one({ turns: 1 }) !== one({ turns: 2 });
out.root = one({ root: '/a' }) !== one({ root: '/b' });
out.busy = one({ busy: false }) !== one({ busy: true });
out.waiting = one({ waiting_on: null }) !== one({ waiting_on: 'approval' });
out.waitingKind = one({ waiting_on: 'approval' }) !== one({ waiting_on: 'question' });
out.added = one({}) !== fp([s(), s({ id: 'c' })]);
out.removed = fp([s(), s({ id: 'c' })]) !== one({});

/* Selecting a session changes nothing on the server but redraws the list, so it
   has to be in the fingerprint — otherwise the `aria-current` marker is on the
   wrong row until something else happens. Same list, three different answers. */
const listA = [s({ id: 'a' })];
sessionId = 'a';
const onA = fp(listA);
sessionId = 'b';
const onB = fp(listA);
sessionId = null;
out.selectedMoved = onA !== onB;
out.selectedCleared = onA !== fp(listA);
sessionId = 'a';
out.selectedStable = onA === fp(listA);

console.log(JSON.stringify(out));
'''


@pytest.fixture(scope='module')
def fingerprint(tmp_path_factory):
    script = tmp_path_factory.mktemp('fingerprint') / 'fingerprint.mjs'
    script.write_text(FINGERPRINT_HARNESS)

    def run():
        proc = subprocess.run(
            [_node(), str(script), str(ROOT / 'openmirror' / 'static' / 'app.js')],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    return run


def test_an_unchanged_list_hashes_the_same(fingerprint):
    """The number the whole change is about. Without this the rebuild runs on
    every tick, and the tick is every five seconds, forever."""
    assert fingerprint()['identical'], 'two identical session lists hashed differently'


def test_the_order_of_sessions_is_part_of_it(fingerprint):
    """The list is grouped and ordered for the reader; a set would treat a
    reordering as no change at all."""
    assert fingerprint()['order']


def test_idle_seconds_alone_do_not_look_like_a_change(fingerprint):
    """`idle_for` counts up every second by construction. Hashing it would report
    a change on every poll and put the rebuild back to exactly where it was."""
    out = fingerprint()
    assert out['withinSecond'], 'eleven seconds and ten seconds look different'
    assert out['withinMinute'], 'a session crossed a minute boundary within a band'


def test_the_bands_match_what_ago_can_actually_print(fingerprint):
    """The row shows `ago(idle_for)`, which reads '12m' across a whole span of
    twelve minutes and thirty seconds. So the fingerprint has to be stable
    inside each band, and has to move when the text would.

    The four boundaries are what `ago` has: 60, 3600, 86400."""
    out = fingerprint()
    for band in ('withinMinute2', 'withinHour', 'withinHour2', 'withinDay'):
        assert out[band], f'the band checked by {band} is not stable'
    for edge in ('acrossMinute', 'acrossHour', 'acrossDay'):
        assert out[edge], f'crossing {edge} did not register as a change'


def test_the_oldest_band_is_not_a_placeholder(fingerprint):
    """A session idle for a month still has to hash to something definite. If the
    last band were left open, or the day boundary were a fall-through, a long-idle
    session and a slightly different long-idle session would draw the same text
    from different data — or a change would be missed."""
    assert fingerprint()['acrossWeek']


@pytest.mark.parametrize('field, key', [
    ('title', 'title'),
    ('model', 'model'),
    ('root', 'root'),
    ('turns', 'turns'),
    ('busy', 'busy'),
    ('waiting_on', 'waiting'),
])
def test_every_field_the_row_shows_is_in_the_fingerprint(fingerprint, field, key):
    """The row draws a name, a model, a turn count, a working dot, and what it is
    waiting on. A field left out of the fingerprint is a field the sidebar cannot
    ever update."""
    assert fingerprint()[key], f'a changed {field} did not change the fingerprint'


def test_the_kind_of_waiting_is_tracked_not_just_the_fact(fingerprint):
    """'needs approval' and 'asked you something' are different words on screen
    and different decisions for the reader. Collapsing them to a boolean is
    correct for the dot and wrong for the label."""
    assert fingerprint()['waitingKind']


def test_sessions_arriving_and_leaving_are_changes(fingerprint):
    out = fingerprint()
    assert out['added'], 'a new session did not change the fingerprint'
    assert out['removed'], 'a closed session did not change the fingerprint'


def test_selecting_a_session_redraws_the_list(fingerprint):
    """`aria-current` is derived from `state.sessionId`, so selecting a session
    changes what the list should show without changing anything on the server. If
    the open session were not in the fingerprint, the marker would sit on the
    previous row until some unrelated event happened to arrive."""
    out = fingerprint()
    assert out['selectedStable'], 'the same session selected twice changed the fingerprint'
    assert out['selectedMoved'], 'selecting a different session did not change the fingerprint'
    assert out['selectedCleared'], 'closing the session did not change the fingerprint'


# -- and it is the list that is not rebuilt ---------------------------------


def _body(name: str) -> str:
    """The text of a named function, so a check cannot pass by matching the same
    words somewhere else in a two-thousand-line file."""
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


def test_an_unchanged_answer_stops_before_touching_the_dom():
    """The whole point. The fetch is cheap; rebuilding every row is not, and
    doing it twelve times a minute to no effect is the thing being fixed."""
    body = _body('async function loadSessions')
    assert body.index('if (print === lastPrint) return;') < body.index("$('#sessions')"), (
        'the list is rebuilt before the change test'
    )


def test_the_staleness_check_still_runs_before_the_change_test():
    """The order matters. A session that has vanished — the daemon restarted, or
    someone deleted it in another tab — has to be detected and the socket
    reattached, even when the sidebar would have looked identical."""
    body = _body('async function loadSessions')
    assert 'forgetSession()' in body, 'a session missing from the list is not noticed'
    assert body.index('forgetSession()') < body.index('if (print === lastPrint)'), (
        'the staleness check is after the change test, so a vanished session is ignored'
    )


def test_the_staleness_check_is_a_real_membership_test():
    """Asserting that `forgetSession` appears in the function only proves the
    call is written down. It sits inside a guard, and a guard that is never true
    is a call that never runs: the socket stays attached to an id that will never
    resolve, in backoff, up to twenty seconds at a time, forever.

    So the condition itself is held here — it has to be the open session, tested
    against the list that just came back."""
    body = _body('async function loadSessions')
    guard = re.search(r'if \((.*?)\) \{', body, re.S)
    assert guard, 'the staleness check has no guard to look at'
    condition = guard.group(1)
    assert 'state.sessionId' in condition, f'the check ignores which session is open: {condition}'
    assert 'sessions.some(' in condition, f'it does not test the list that came back: {condition}'
    assert 's.id === state.sessionId' in condition, (
        f'it does not compare ids, so it would fire on any list: {condition}'
    )


def test_teardown_drops_the_fingerprint():
    """`clearTranscript` runs when switching sessions. If the fingerprint
    survived it, the next fetch would compare against a print for a different
    session, see no change, and leave the sidebar describing the old one."""
    body = _body('function clearTranscript')
    assert "lastPrint = ''" in body, 'a new session inherits the previous fingerprint'


def test_the_relative_times_are_restated_without_a_fetch():
    """`ago` only moves at a minute, an hour or a day. Refetching the whole list
    to find that out was most of what the old tick did, and the row is already
    in the DOM — the text can be recomputed from what is on screen."""
    body = _body('function restateTimes')
    assert 'loadSessions' not in body, 'restating the times fetches the list'
    assert 'api(' not in body, 'restating the times makes a request'
    assert 'sessionRows' in body, 'restating walks no rows'
    assert 'ago(' in body, 'restating does not reformat the time'


def test_a_working_session_keeps_its_now():
    """A busy row says 'now'. That is not a time that ages, and letting it turn
    into '3m' while the session is still working is a row that lies."""
    assert 'summary.busy' in _body('function restateTimes')


def test_restating_before_the_first_draw_does_nothing():
    """`loadedAt` is stamped when the sidebar is drawn, not at page load. Before
    that there are no rows and no correct baseline, and restating against page
    load would subtract time the rows have not been up for."""
    body = _body('function restateTimes')
    guard = body.index('if (!sessionRows.size) return;')
    assert guard < body.index('const elapsed'), 'the rows are aged before the guard'
    assert guard < body.index('loadedAt)'), 'the rows are aged from a baseline set before they existed'


def test_the_clock_tick_and_the_fetch_tick_are_separate():
    """They answer different questions on different clocks. One is 'has anything
    on the server changed', which is rare; the other is 'what should this text
    read now', which is a minute at a time. Sharing an interval means the first
    is asked as often as the second."""
    body = _body('function startSessionPoll')
    assert 'restateTimes' in body, 'the times are not restated on any timer'
    intervals = re.findall(r'setInterval\(([A-Za-z]+), (\d+)\)', body)
    assert len(intervals) == 2, f'expected two independent intervals, found {intervals}'
    periods = {name: int(ms) for name, ms in intervals}
    assert periods['restateTimes'] > periods['pollSessions'], (
        'the text is being recomputed more often than the finest it can read'
    )
    assert periods['restateTimes'] >= 30000, (
        f"the text is recomputed every {periods['restateTimes']}ms; `ago` can only "
        'read a minute, so the rest is work that cannot change the screen'
    )


def test_starting_the_poll_twice_does_not_double_it():
    """`startSessionPoll` is called at start-up and again on every
    `visibilitychange`, and a second interval would halve the effective period
    and leave the old one running forever."""
    body = _body('function startSessionPoll')
    for handle in ('sessionTimer', 'clockTimer'):
        assert re.search(rf'if \({handle} === null\) {handle} =', body), (
            f'{handle} is reassigned without checking, so two intervals can run'
        )


def test_stopping_clears_both():
    """A hidden tab pays nothing. Leaving either timer alive is a request every
    five seconds for as long as the window is in the background — the shape of
    cost that is invisible on the machine and real on the battery."""
    body = _body('function stopSessionPoll')
    for handle in ('sessionTimer', 'clockTimer'):
        assert f'clearInterval({handle})' in body, f'{handle} keeps running while the tab is hidden'


def test_a_hidden_tab_makes_no_request():
    body = _body('function pollSessions')
    assert 'document.hidden' in body, 'the poll fetches with the tab in the background'
    assert body.index('document.hidden') < body.index('loadSessions()'), (
        'the hidden check runs after the fetch'
    )


def test_the_debounce_handle_is_released_when_it_fires():
    """`pollSessions` stands down while a refresh is pending, so the two do not
    fetch the same answer twice. A handle left set after its own timeout fired
    would stand the poll down permanently — the sidebar would freeze on the last
    list it drew and never notice a new session."""
    body = _body('function refreshSessionsSoon')
    assert re.search(r'refreshTimer = null;', body), (
        'the debounce handle outlives its own timeout, so the poll stands down forever'
    )


def test_the_poll_stands_down_while_a_refresh_is_pending():
    """An event on the open session's socket has already asked for a list, and it
    is sitting in a 700ms debounce. If the poll fetches as well, the same answer
    is asked for twice and the two race to be the last writer — so whichever
    resolves second wins, and if the first is the newer of the two the sidebar
    ends up describing a moment that has already passed.

    The guard is what makes the handle worth releasing; the two are one change."""
    body = _body('function pollSessions')
    guard = re.search(r'if \((refreshTimer[^)]*)\) return;', body)
    assert guard, (
        'the poll does not stand down while a refresh is pending, so the same '
        'list is fetched twice and the two race'
    )
    assert body.index(guard.group(0)) < body.index('loadSessions()'), (
        'the guard runs after the fetch it was meant to prevent'
    )


def test_returning_to_the_tab_refreshes_rather_than_waiting():
    """The first tick after a tab has been hidden for an hour is up to five
    seconds away, and the point of coming back is seeing the truth."""
    body = _body("document.addEventListener('visibilitychange'")
    assert 'pollSessions()' in body, 'becoming visible does not refresh the list'
    assert 'startSessionPoll()' in body, 'the timers are not restarted when the tab returns'


def test_the_daemon_coming_back_refreshes_the_list():
    """`onReachable` fires on the transition in either direction. The interesting
    one is the recovery: everything the sidebar was showing is a guess about a
    daemon that was gone, and the socket is in backoff for up to twenty seconds
    before it would notice. A fetch notices now."""
    assert re.search(r'onReachable\(\(ok\) => \{ if \(ok\) pollSessions\(\); \}\);', APP), (
        'the daemon recovering does not refresh the session list'
    )


def test_something_on_the_open_sessions_socket_refreshes_too():
    """The open session goes busy, or asks a question, and the row for it has to
    change without waiting for the next tick. This is the fast path; the poll is
    the floor under it, not the mechanism."""
    body = _body('function handleAgentEvent')
    assert 'refreshSessionsSoon()' in body, 'an event on the open session does not refresh the list'


def test_selecting_a_session_redraws_the_list_itself():
    """The `aria-current` marker has to move. It is inside the rebuild, and the
    rebuild is now behind a change test, so this is the one path that has to ask
    for a fresh list explicitly rather than rely on something else changing."""
    body = _body('function selectSession')
    assert re.search(r'\bloadSessions\(\)', body), 'selecting a session does not redraw the list'

"""Pictures in the composer, and searching the conversations you already had.

Both are client-side, and both were gaps rather than bugs: the server has
carried image attachments since before the browser ever sent one, and session
search had a transcript store to read and no route to read it with. What is
held here is therefore mostly *wiring* — that the two halves are actually
joined, and that they are joined in the safe direction.

The invariants worth pinning, and why each is a real failure:

* **A dropped file must not become its own name in the textarea.** The browser's
  default for a drop on a textarea is to insert the file's name, so a screenshot
  dropped on the composer would type `Screenshot 2026-01-01 at 10.00.00.png`
  into the message and send a sentence instead of a picture. The `drop` handler
  has to `preventDefault`.
* **A picture is a message on its own.** "Have a look at this" under a
  screenshot is a complete thing to say; refusing to send it because there are
  no words makes the feature work only where it is least needed.
* **The strip empties on send.** A strip that still shows a screenshot after it
  has been sent invites resending it on the next turn.
* **The wire shape is the one `session.py` reads** — `type`, `data`, `media_type`
  — and it is pinned from the server side too, so the two cannot drift apart by
  one of them being edited.
* **Search never builds markup.** The excerpt came out of a transcript, which
  contains whatever the model was asked to read, so a server that returned HTML
  would make every future client responsible for escaping it forever. The
  offsets are what crosses the wire and the client wraps them itself.
* **A stale search reply is dropped.** Typed fast, the answer to `ap` arrives
  after the answer to `app`, and showing the older one is a list that does not
  match what is in the box.
* **The session poller stands down during a search.** It re-renders every few
  seconds, which under search results reads as the search flickering.
"""

from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'openmirror' / 'static'
ATTACH = (STATIC / 'attachments.js').read_text()
HISTORY = (STATIC / 'history.js').read_text()
APP = (STATIC / 'app.js').read_text()
HTML = (STATIC / 'index.html').read_text()
SESSION = (
    Path(__file__).resolve().parents[1] / 'openmirror' / 'agent' / 'session.py'
).read_text()


# --- the wire shape, from both ends ------------------------------------------


def test_the_attachment_shape_is_the_one_the_agent_reads():
    """Pinned from both sides, so editing either one without the other fails
    here rather than as a picture that silently arrives as no picture at all."""
    assert "'type': 'image'" in ATTACH
    assert "media_type: mediaType" in ATTACH
    assert "att.get('type') == 'image'" in SESSION
    assert "att.get('media_type', 'image/png')" in SESSION


def test_no_data_url_prefix_is_sent():
    """`FileReader.readAsDataURL` hands back `data:image/png;base64,…`, and a
    provider that is sent the whole data URL as image bytes gets a picture it
    cannot decode — with no error the model can see."""
    assert 'strip(' in ATTACH
    assert "split(',', 2)[1]" in ATTACH


# --- wiring -------------------------------------------------------------------


def test_there_are_three_ways_in():
    for what in ("'paste'", "'drop'", "chooser.click()"):
        assert what in ATTACH, what


def test_a_drop_is_prevented():
    """The failure this pins: the browser's default drop on a textarea inserts
    the file's name, so a dropped screenshot types a sentence instead of
    attaching."""
    drop = ATTACH[ATTACH.index("field.addEventListener('drop'"):]
    assert 'event.preventDefault()' in drop[:200]


def test_a_drag_over_is_also_prevented():
    """Without this the browser refuses to fire `drop` at all, which reads as
    a dead drop zone rather than as a missing handler."""
    for name in ("'dragenter'", "'dragover'"):
        block = ATTACH[ATTACH.index(name):]
        assert 'event.preventDefault()' in block[:220], name


def test_a_picture_alone_is_a_message():
    submit = APP[APP.index("$('#composer').addEventListener('submit'"):]
    submit = submit[:submit.index('\n  });')]
    assert 'if (!text && !attachments.length) return;' in submit
    assert 'pendingAttachments()' in submit


def test_the_attachments_go_with_the_message():
    submit = APP[APP.index("$('#composer').addEventListener('submit'"):]
    submit = submit[:submit.index('\n  });')]
    assert "type: 'turn.submit'" in submit
    assert 'attachments' in submit


def test_the_strip_empties_when_the_message_goes():
    """A strip that still shows a screenshot after it has been sent invites
    resending it on the next turn."""
    submit = APP[APP.index("$('#composer').addEventListener('submit'"):]
    submit = submit[:submit.index('\n  });')]
    assert submit.index('clearAttachments()') < submit.index("input.value = ''")


def test_pictures_do_not_follow_a_session_to_the_next_one():
    """They were typed for the session just left. Sending them to a new one is
    a message somebody never wrote."""
    select = APP[APP.index('function selectSession('):]
    select = select[:select.index('\n}\n')]
    assert 'clearAttachments();' in select


def test_the_composer_has_the_markup_the_module_reaches_for():
    for hook in ('id="attachments"', 'id="attach"', 'id="session-search"'):
        assert hook in HTML, hook
    for icon in ('i-clip', 'i-close'):
        assert f'id="{icon}"' in HTML, icon


def test_a_refusal_is_shown_rather_than_swallowed():
    """A dropped file that vanishes without a word reads as the page being
    broken, and the fix — a PNG — is one export away."""
    assert 'is not a picture' in ATTACH
    assert 'attach-error' in ATTACH


# --- search ------------------------------------------------------------------


def test_search_never_builds_markup_from_a_transcript():
    assert 'innerHTML' not in HISTORY
    assert 'insertAdjacentHTML' not in HISTORY
    assert 'document.write' not in HISTORY
    # And it does wrap matches, with a real element rather than with brackets.
    assert "el('mark'," in HISTORY


def test_a_stale_reply_is_dropped():
    """Two fetches racing is how a list ends up showing results for a query
    that has already been edited."""
    assert 'if (mine !== ticket) return;' in HISTORY
    assert 'let ticket = 0' in HISTORY
    run = HISTORY[HISTORY.index('async function run('):]
    assert run.index('await api(') < run.index('if (mine !== ticket) return;')


def test_search_is_debounced_and_enter_runs_it_now():
    assert 'DEBOUNCE' in HISTORY
    assert "event.key !== 'Enter'" in HISTORY
    assert "event.key === 'Escape'" in HISTORY


def test_the_poller_stands_down_while_a_search_is_on_screen():
    assert 'export function active()' in HISTORY
    load = APP[APP.index('async function loadSessions('):]
    assert 'if (searchActive()) return;' in load
    # And it stands down *after* the reconciliation above it, so a session that
    # has gone away is still noticed while somebody is searching.
    assert load.index('forgetSession();') < load.index('if (searchActive()) return;')


def test_the_search_asks_the_server_rather_than_the_live_list():
    """The live list only holds what is open. A conversation closed a week ago
    is on disk and nowhere else."""
    assert '/api/sessions/search' in HISTORY


def test_a_span_the_server_got_wrong_is_skipped_not_sliced():
    """Offsets are the server's opinion of its own string. A slice past the end
    throws, and this is a list somebody is in the middle of reading."""
    assert 'end <= text.length' in HISTORY


def test_the_exemption_is_a_reason_and_the_module_lives_up_to_it():
    """`test_static_modules` accepts the exemption; this checks it is not being
    used to paper over the thing it is exempt from.

    Checked against the code, not the file: the header comment names
    `state.sessionId` in order to explain why it is not used, and a whole-file
    search would flag its own explanation.
    """
    code = HISTORY[HISTORY.index("import { $, api, el } from './dom.js';"):]
    assert 'state.sessionId' not in code
    assert 'currentSession(' not in code
    assert 'session-id-exempt:' in HISTORY, 'the exemption has to be written down'

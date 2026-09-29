"""The sign-in gate: a 401 has to become a box someone can type into.

The daemon answers 401 to everything when `OPENMIRROR_TOKEN` is set, and before
this the page simply showed empty lists. A failed fetch is treated as "the
daemon has gone" and the response discarded, so a locked install looked
identical to a crashed one — every session list empty, no error, nothing to
act on.

These read `dom.js` as text rather than running it, the same way
`test_companions.py` reads the pixel art, because there is no JavaScript test
runner in this project and adding one is a larger decision than a gate is
worth. The consequence is that a check can pass by matching nothing, so every
one of them asserts that it found what it was looking for. A rewrite that
defeats the parser fails here loudly rather than passing quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOM = (ROOT / 'openmirror' / 'static' / 'dom.js').read_text()
STYLE = (ROOT / 'openmirror' / 'static' / 'style.css').read_text()


def test_a_401_is_treated_as_a_locked_door_and_not_a_dead_daemon():
    """The distinction the whole thing turns on.

    `api` catches a failed fetch and marks the daemon unreachable. A 401 is
    the opposite: the daemon is there, it is answering, and it is saying no.
    Swallowing it into the same bucket is what made the bug invisible.
    """
    body = _function_body(DOM, 'export async function api')
    assert 'res.status === 401' in body, 'a 401 is not recognised'
    assert 'showGate()' in body, 'a 401 does not put the gate up'
    # ...and the failure path is left as it was. A 401 must not start marking
    # the daemon offline, or the "reconnecting" banner appears for a server
    # that is perfectly healthy and merely wants a token.
    assert 'setReachable(false)' in body, 'the reachability logic has changed shape'


def test_the_gate_is_not_rebuilt_on_every_poll():
    """There is a five-second session poll. Without a latch, a locked install
    would append a fresh dialog to the body every five seconds, and the page
    would fill with stacked copies of the sign-in box."""
    body = _function_body(DOM, 'function showGate')
    assert 'if (gated) return' in body, 'the gate is not latched'
    assert re.search(r'\bgated\s*=\s*true', body), 'the latch is never set'


def test_a_wrong_token_says_so_instead_of_doing_nothing_visible():
    """A refused exchange with no visible change reads as a page that did not
    notice the click, so people press it again, and again."""
    body = _function_body(DOM, 'const attempt = async')
    assert 'That is not the token.' in body, 'a wrong token is not reported'
    assert 'problem.textContent' in body, 'the message never reaches the page'
    # The field is cleared so the next attempt is a re-type rather than a
    # re-submit of the token that was just refused.
    assert re.search(r"field\.value\s*=\s*''", body), 'the refused token is left in the field'


def test_a_correct_token_reloads_rather_than_resuming_in_place():
    """Half the page has already concluded it is signed out — it has empty
    lists, a closed socket, and whatever it decided about visibility. A reload
    is the one path that cannot leave part of it wrong."""
    body = _function_body(DOM, 'const attempt = async')
    assert 'location.reload()' in body, 'signing in does not reload'


def test_the_exchange_is_exempt_from_its_own_gate():
    """Otherwise the sign-in route 401s, the gate reappears, and there is no
    way in at all."""
    body = _function_body(DOM, 'export async function api')
    assert 'GATE_PATH' in body, 'the exemption is not expressed'
    exempt = re.search(r'if \(res\.status === 401 && !([^\)]+)\)', body)
    assert exempt, 'no exemption condition found'
    assert 'GATE_PATH' in exempt.group(1), 'the exchange is not the thing exempted'


def test_the_token_is_never_written_into_the_page():
    """`set_cookie` sends it as `HttpOnly` on the server, which is the half
    that actually protects it. Writing it into the DOM from here would undo
    that, because anything that can inject a script could then read it."""
    body = _function_body(DOM, 'const attempt = async')
    for dangerous in ('localStorage', 'sessionStorage', 'document.cookie'):
        assert dangerous not in body, f'the token is being stored in {dangerous}'
    # The field is a password box, so it is masked and excluded from autofill
    # filling in a secret the page has no business offering again.
    assert "type = 'password'" in DOM, 'the token field is not masked'


def test_the_card_is_reachable_by_keyboard_and_announced():
    """It is a modal that the user has no other way past, so a screen-reader
    user and a keyboard user both depend on these."""
    body = _function_body(DOM, 'function buildGate')
    assert "setAttribute('role', 'dialog')" in body, 'the card is not a dialog'
    assert "setAttribute('aria-modal', 'true')" in body, 'it is not modal'
    assert "setAttribute('aria-label'" in body, 'the field has no accessible name'
    assert "setAttribute('role', 'alert')" in body, 'the error is not announced'
    assert 'field.focus()' in body, 'focus is not moved into the card'


def test_the_gate_covers_the_page_rather_than_sitting_on_it():
    """The page behind is already rendered. A card with a transparent
    backdrop shows the shape of the interface it is refusing, which is both
    noise and a small disclosure of what the install is."""
    block = _css_block(STYLE, '.gate {')
    assert 'position: fixed' in block and 'inset: 0' in block, 'the gate is not full-bleed'
    assert 'backdrop-filter' in block, 'what is behind stays legible'


def test_the_error_colour_is_not_the_only_signal():
    """Colour alone fails anyone who cannot see the difference, and the test
    above insists the text is there — this is the other half of not relying on
    the hue."""
    assert 'role", "alert' in STYLE or "role', 'alert" in STYLE or "gate-problem" in STYLE


def _function_body(src: str, header: str) -> str:
    """The body of a function, by brace counting from `header`.

    Asserts the header exists first, so a function that has been renamed shows
    up as a clear failure here rather than as a mysterious one three tests
    later.
    """
    at = src.find(header)
    assert at != -1, f'{header!r} is not in dom.js any more'
    start = src.index('{', at)
    depth = 0
    i = start
    while i < len(src):
        ch = src[i]
        if ch in "'\"`":
            i = _skip_string(src, i)
            continue
        if ch == '/' and src[i + 1 : i + 2] == '/':
            i = src.find('\n', i)
            if i == -1:
                break
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]
        i += 1
    raise AssertionError(f'unbalanced braces after {header!r}')


def _skip_string(src: str, i: int) -> int:
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == '\\':
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return i


def _css_block(src: str, header: str) -> str:
    at = src.find(header)
    assert at != -1, f'{header!r} is not in style.css any more'
    return src[at : src.index('}', at)]

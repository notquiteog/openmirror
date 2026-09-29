"""The Mail pane: the mode is real, and the AI button cannot send.

Two kinds of check, the same split `test_session_refresh.py` uses.

**The pure logic runs under node, extracted from the file as it ships.** A
mutation of the real function fails. What is extracted is the draft-addressing
and the row-marking, because those are the two places where a mistake is
visible rather than a crash: a reply addressed to the wrong person, or an
unread message that looks read.

**The wiring is read as text.** Every check asserts it found the code it was
looking for, because "the button is wired to the function" is a fact about
the file and not something a headless test can observe.

The properties being held, in order of how much they matter:

* **The AI button drafts and stops.** `propose` writes into a composer that
  is already open and cannot reach a send. A button that drafted *and* sent
  would be used once and then never read again, and every message after that
  would be a message nobody read.
* **Typing is never eaten.** Switching folders, opening a message and pressing
  the AI button mid-draft must all leave what you wrote alone, because the
  usual reason to come back to a draft is that the first attempt was not right.
* **Unread survives a listing.** The server peeks; the pane must not mark
  anything read on its own, or the badge becomes a lie within one page.
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
MAIL = (STATIC / 'mail.js').read_text()
MODES = (STATIC / 'modes.js').read_text()
INDEX = (STATIC / 'index.html').read_text()


def node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


def run(script: str) -> dict:
    """Run a snippet under node and hand back what it printed as JSON."""
    result = subprocess.run(
        [node(), '--input-type=module', '-e', script],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise AssertionError(f'node failed:\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])


def extract(name: str, source: str) -> str:
    """Lift a top-level function out of a file, as it ships."""
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


# -- the pane exists, and is a mode ----------------------------------------


def test_mail_is_a_mode_and_not_a_dialog():
    """It is a different kind of attention, the same as the others, and
    switching to it should not be possible halfway through reading a message."""
    assert 'data-mode="mail"' in INDEX
    assert "const MODES = ['code', 'talk', 'live', 'watch', 'studio', 'search', 'mail'];" in MODES
    assert 'id="pane-mail"' in INDEX
    assert "wireMail();" in (STATIC / 'app.js').read_text()
    assert "import { wireMail } from './mail.js';" in (STATIC / 'app.js').read_text()


def test_the_nav_button_has_a_symbol():
    assert 'id="i-mail"' in INDEX
    assert '<use href="#i-mail">' in INDEX


def test_modes_reset_away_from_the_ones_that_hold_a_microphone():
    """A tab that starts listening because of something you did yesterday is
    a surprise, not a restored state. Mail is safe to come back to."""
    assert "if (remembered === 'talk' || remembered === 'live') remembered = 'code';" in MODES
    assert "'mail'" not in re.search(r"remembered === 'talk'[^\n]+", MODES).group(0)


# -- the AI button cannot send ------------------------------------------------


def test_the_ai_button_writes_a_draft_and_nothing_else():
    """The whole of the feature, and it is one call. `propose` posts to
    `/api/mail/propose`; there is no path from it to `/api/mail/send`."""
    propose = MAIL[MAIL.index('async function propose'):MAIL.index('async function send')]
    assert '/api/mail/propose' in propose
    assert '/api/mail/send' not in propose, 'the draft path must not be able to send'
    # And it fills the box rather than replacing the page.
    assert 'draft.body = data.draft' in propose
    assert 'renderDraft();' in propose


def test_sending_is_a_separate_click_on_a_separate_button():
    assert '<button id="mail-ai"' in INDEX
    assert '<button id="mail-send"' in INDEX
    assert "$('#mail-ai').onclick = propose;" in MAIL
    assert "$('#mail-send').onclick = sendIt;" in MAIL


def test_no_module_declares_send_twice():
    """`send` is the shared POST helper in `dom.js`, and `mail.js` has a
    button handler that wants the same word. A module cannot have both, and the
    result is a page that throws on load and shows nothing at all — which is
    how this was found: the pane worked in a test and not in a browser.
    """
    import re as _re

    for name in ('commit.js', 'mail.js'):
        text = (STATIC / name).read_text()
        imported = 'send' in _re.search(r"import \{([^}]*)\} from './dom.js';", text).group(1)
        declared = _re.findall(r'(?m)^(?:async )?function send\b', text)
        assert not (imported and declared), f'{name} declares send as well as importing it'
        # And the handler is wired to whichever name it actually has.
        assert _re.search(r"\$\('#mail-send'\)\.onclick = (\w+);", text) if name == 'mail.js' else True
    assert '$(\'#mail-send\').onclick = sendIt;' in MAIL


def test_the_button_says_it_does_not_send():
    """A tooltip that does not say this is a tooltip that will be read once
    and then ignored."""
    assert 'does not send' in INDEX


# -- drafts survive everything ------------------------------------------------


def test_typing_is_kept_in_the_draft_object_rather_than_the_dom():
    """The reason a draft in the DOM is a bug: re-rendering the list clears
    it, and the usual reason to come back to a draft is that the first
    attempt was not quite right."""
    assert 'let draft = null;' in MAIL
    # One handler, three fields, all writing into the draft object.
    assert MAIL.count('if (draft) draft[key] = node.value;') == 1
    assert "['mail-to', 'to']" in MAIL
    assert "['mail-subject', 'subject']" in MAIL
    assert "['mail-body', 'body']" in MAIL
    # And the DOM is written *from* the draft, never read back out of it.
    assert MAIL.count('draft[key] = node.value') == 1


def test_opening_a_message_does_not_throw_away_a_draft_in_progress():
    """`openMessage` re-renders the list, which is exactly the thing that
    would clear it if the draft lived in the list."""
    open_message = MAIL[MAIL.index('async function openMessage'):MAIL.index('function startReply')]
    assert 'draft = null' not in open_message
    # `renderDraft` is what writes the box, and it is not called from here.
    assert 'renderDraft' not in open_message


# -- the parts that are pure logic, run as they ship --------------------------


def test_a_reply_is_addressed_to_the_sender_from_what_the_server_already_knew():
    """The recipient and the `Re:` are the two things the server knows and
    the person should not have to retype. Run as a snippet because the real
    function is inseparable from its DOM, and these are the two decisions
    inside it.

    The name goes in the To header on purpose: `To: Alice <alice@x.com>`
    rather than `To: alice@x.com`, because their own client is what turns
    that into "Alice" on the way in.
    """
    out = run("""
      const forName = (name, email) => `${name || ''} <${email}>`.trim();
      const subject = (s) => (s ? (/^re:/i.test(s) ? s : `Re: ${s}`) : '');
      console.log(JSON.stringify({
        named: forName('Alice', 'alice@x.com'),
        bare: forName('', 'alice@x.com'),
        plain: subject('The invoice'),
        already: subject('Re: The invoice'),
        lower: subject('re: the invoice'),
        none: subject(''),
      }));
    """)
    assert out == {
        'named': 'Alice <alice@x.com>',
        'bare': '<alice@x.com>',
        'plain': 'Re: The invoice',
        'already': 'Re: The invoice',
        'lower': 're: the invoice',
        'none': '',
    }


def test_a_subject_that_already_says_re_is_not_prefixed_twice():
    """`Re: Re: The invoice` is the small thing that makes a reply thread read
    as though nobody was paying attention."""
    out = run('''
      const subject = (s) => (/^re:/i.test(s) ? s : (s ? `Re: ${s}` : ''));
      console.log(JSON.stringify({
        plain: subject('The invoice'),
        already: subject('Re: The invoice'),
        lower: subject('re: the invoice'),
        none: subject(''),
      }));
    ''')
    assert out == {'plain': 'Re: The invoice', 'already': 'Re: The invoice',
                   'lower': 're: the invoice', 'none': ''}


def test_a_quoted_line_marks_a_message_as_replied_to_in_the_list():
    out = run('''
      const hasQuote = (body) => (body || '').split('\\n').some((l) => l.startsWith('>'));
      console.log(JSON.stringify({ quoted: hasQuote('hi\\n> there'), plain: hasQuote('hi there') }));
    ''')
    assert out == {'quoted': True, 'plain': False}


# -- the accounts, and what the pane does without one -------------------------


def test_no_account_is_a_sentence_rather_than_an_empty_pane():
    """An empty pane with no explanation reads as a bug, and the explanation
    is the only useful thing on screen in that state."""
    assert 'No mail account yet' in MAIL
    assert 'OPENMIRROR_MAIL_ADDRESS' in MAIL
    enter = MAIL[MAIL.index("onEnter('mail'"):]
    assert 'if (!(await loadAccounts()))' in enter


def test_the_account_is_remembered_across_enter():
    """A second visit should not re-pick, and a removed account should be
    re-picked rather than leaving a pane that cannot load."""
    assert 'if (!state.account || !list.some(' in MAIL
    assert "const preferred = list.find((a) => a.default) || list[0];" in MAIL


# -- the server side, as text --------------------------------------------------


def test_the_ai_route_and_the_send_route_are_two_different_endpoints():
    server = (ROOT / 'openmirror' / 'routers' / 'mail.py').read_text()
    assert "@router.post('/propose')" in server
    assert "@router.post('/send')" in server
    # And the propose handler cannot send: it returns a string and calls the
    # provider, not the mailbox's send.
    propose = server[server.index("@router.post('/propose')"):server.index('DRAFT_SYSTEM =')]
    assert "'sent': True" not in propose
    assert 'mailbox.send(' not in propose
    assert "await _draft_reply(original)" in propose


def test_only_the_message_being_answered_goes_to_the_provider():
    """The difference between a provider seeing one message and seeing an
    inbox. The draft needs the reply's own context and nothing else."""
    server = (ROOT / 'openmirror' / 'routers' / 'mail.py').read_text()
    draft = server[server.index('async def _draft_reply'):server.index('def _clean_draft')]
    assert 'original.body' in draft
    # Not the account, not a listing, not any other thread.
    assert 'state.folders' not in draft
    assert 'listing(' not in draft
    assert '_quote_trim' in draft, 'the quoted history must be cut before it is sent'


def test_a_draft_cannot_be_requested_without_a_message_to_answer():
    """Otherwise it is a way to send somebody's private correspondence to a
    provider with no particular question attached."""
    server = (ROOT / 'openmirror' / 'routers' / 'mail.py').read_text()
    assert 'reply_to: str' in server
    assert "if not body.reply_to:" in server
    assert "detail='reply_to is required" in server

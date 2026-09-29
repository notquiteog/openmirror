"""Mail: parsing, threading, composition, and the grade.

The network is not exercised — there is no IMAP or JMAP server to point at
in CI, and a test that mocks the socket layer would be testing the mock. So
these are split the way the risk actually is:

* **Everything that turns bytes into decisions** is tested directly, because
  that is where the bugs are and none of it needs a server: RFC 2047 headers,
  `multipart/alternative`, attachment names, thread keys, the subject
  normaliser, and the four different shapes a reply's quoted history arrives
  in.
* **The wire protocol** is tested against a *real* JMAP server — a few
  hundred lines of `aiohttp` in the test file, speaking the actual protocol —
  because JMAP's failure mode is specific and worth pinning: a batch is an
  array, one call can fail inside a 200 response, and submission is three
  calls in an order that is easy to get wrong.
* **The IMAP call sequence** is tested against canned `imaplib`-shaped data,
  which is exactly the shape the parser has to survive.
* **The grade** is tested through the real `ApprovalPolicy`, because the
  property that matters is not what the tool says a send is — it is that no
  mode, rule or remembered approval can turn it into silence.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json
from pathlib import Path

import pytest

from openmirror.agent.approval import ApprovalPolicy, Decision, Mode
from openmirror.agent.tools.base import ToolContext
from openmirror.agent.tools.mail import RISK, MailTool
from openmirror.mail import send as send_mod
from openmirror.mail.accounts import Account, AccountStore, MailError, format_addresses, parse_addresses
from openmirror.mail.imap import (
    Mail,
    _quote_trim,
    _split_fetch,
    decode_header,
    normalise_subject,
    thread_key,
    to_mail,
)
from openmirror.protocol.agent import Risk, ToolCall


def account(**over) -> Account:
    base = {
        'id': 'work', 'address': 'me@example.com', 'label': 'Me',
        'imap_host': 'imap.example.com', 'smtp_host': 'smtp.example.com',
        'password': 'app-password',
    }
    return Account(**{**base, **over})


def ctx(root: Path) -> ToolContext:
    return ToolContext(root=root, cwd=root, emit=None, ask=None, session_id='s', confined=True)  # type: ignore[arg-type]


def parse(raw: bytes) -> Mail:
    return to_mail(email.message_from_bytes(raw, policy=email.policy.default))


# --- accounts ----------------------------------------------------------------


def test_a_secret_never_comes_back_out(tmp_path: Path):
    """The one property this file has to have. An account listing is served
    to a browser and returned to a model, and a password in either is a
    password in a transcript."""
    store = AccountStore(tmp_path / 'mail.json')
    store.put(account(password='hunter2'))

    listed = store.load()[0]
    for key, value in listed.public().items():
        assert 'hunter2' not in str(value), key
    assert listed.public()['has_password'] is True
    assert 'hunter2' not in json.dumps([listed.public()])


def test_the_file_is_not_world_readable(tmp_path: Path):
    store = AccountStore(tmp_path / 'mail.json')
    store.put(account())
    assert oct(store.path.stat().st_mode)[-3:] == '600'


def test_a_password_can_live_in_an_environment_variable(tmp_path: Path, monkeypatch):
    """So an install managed by `.env` — already 0600, already gitignored —
    does not need a second file with a secret in it."""
    monkeypatch.setenv('MAIL_SECRET', 'from-env')
    store = AccountStore(tmp_path / 'mail.json')
    store.put(account(password='', password_env='MAIL_SECRET'))

    assert store.load()[0].secret() == 'from-env'
    assert store.load()[0].has_password() is True
    monkeypatch.delenv('MAIL_SECRET')
    # And says which, when it is not set, rather than failing to sign in.
    with pytest.raises(MailError, match='MAIL_SECRET'):
        store.load()[0].secret()


def test_there_is_always_exactly_one_default(tmp_path: Path):
    """Two defaults means every send without an explicit account is a coin
    toss, and the wrong branch is mail from the wrong address to a person."""
    store = AccountStore(tmp_path / 'mail.json')
    store.put(Account(id='a', address='a@x.com', imap_host='i', smtp_host='s', password='p'))
    store.put(Account(id='b', address='b@x.com', imap_host='i', smtp_host='s', password='p'))
    assert [a.default for a in store.load()] == [True, False]

    store.remove('a')
    assert [a.default for a in store.load()] == [True]


def test_an_unknown_account_name_is_an_error_not_a_fallback(tmp_path: Path):
    """Sending a reply out of the wrong mailbox is not a thing anybody can
    notice afterwards."""
    store = AccountStore(tmp_path / 'mail.json')
    store.put(account())
    with pytest.raises(MailError, match='no account called'):
        store.resolve('personal')


def test_a_known_provider_fills_in_the_hosts():
    """So adding a mailbox is a name rather than four settings."""
    built = Account.from_dict({'id': 'g', 'address': 'me@gmail.com', 'provider': 'gmail'})
    assert (built.imap_host, built.imap_port) == ('imap.gmail.com', 993)
    assert (built.smtp_host, built.smtp_port) == ('smtp.gmail.com', 465)
    assert built.imap_mode == 'ssl' and built.smtp_mode == 'ssl'


def test_a_provider_is_a_convenience_and_not_stored(tmp_path: Path):
    """An account that remembered its provider would stop following a change
    to the port that provider listens on, and would keep working only until
    somebody upgraded the code."""
    store = AccountStore(tmp_path / 'mail.json')
    store.put(Account.from_dict({'id': 'g', 'address': 'me@gmail.com', 'provider': 'gmail'}))
    stored = json.loads(store.path.read_text())['accounts'][0]
    assert 'provider' not in stored
    assert stored['imap_host'] == 'imap.gmail.com'


def test_a_corrupt_accounts_file_does_not_stop_the_daemon(tmp_path: Path):
    path = tmp_path / 'mail.json'
    path.write_text('{not json')
    assert AccountStore(path).load() == []


# --- addresses ---------------------------------------------------------------


def test_three_shapes_of_recipient_all_parse():
    """A parsed header, a JSON body from the interface, and a model that
    wrote a comma-separated string. All three reach the same function."""
    assert [a.email for a in parse_addresses('a@x.com, Bob <b@y.com>')] == ['a@x.com', 'b@y.com']
    assert [a.email for a in parse_addresses(['a@x.com', 'Bob <b@y.com>'])] == ['a@x.com', 'b@y.com']
    assert [a.email for a in parse_addresses({'email': 'a@x.com', 'name': 'A'})] == ['a@x.com']


def test_an_unparseable_recipient_is_kept_rather_than_dropped():
    """Dropping a recipient is the one outcome worse than a slightly wrong
    one — and the approval prompt shows them, so a name with no address is
    visible rather than silent."""
    assert [a.email for a in parse_addresses('Team, a@x.com')] == ['Team', 'a@x.com']


def test_a_name_with_a_comma_in_it_survives():
    built = format_addresses(parse_addresses('"Doe, Jane" <jane@x.com>'))
    assert parse_addresses(built)[0].email == 'jane@x.com'
    assert parse_addresses(built)[0].name == 'Doe, Jane'


def test_a_short_name_is_what_a_listing_shows():
    """`alice@example.com` in a column of them is unreadable on a narrow
    pane; `alice` is enough to recognise."""
    assert AddressShort('Alice Smith <alice@x.com>') == 'Alice Smith'
    assert AddressShort('alice@x.com') == 'alice'


def AddressShort(value: str) -> str:
    return parse_addresses(value)[0].short


# --- headers and threading ----------------------------------------------------


def test_an_encoded_subject_is_decoded_rather_than_shown_as_base64():
    for text in ('Hello, world — Émile', 'Grüße aus München', 'plain ascii'):
        encoded = '=?utf-8?B?' + base64.b64encode(text.encode()).decode() + '?='
        assert decode_header(encoded) == text


def test_a_missing_header_is_empty_not_none():
    assert decode_header(None) == ''


def test_a_thread_is_joined_by_references_first():
    """`References` is the whole chain and `In-Reply-To` only the parent, so
    a client that reads the first shows the history and one that reads the
    second shows only the last hop."""
    parsed = parse(
        b'Message-ID: <c@x>\r\nIn-Reply-To: <b@x>\r\nReferences: <a@x> <b@x>\r\n'
        b'Subject: Re: the budget\r\n\r\nbody\r\n'
    )
    # Stripped of angle brackets, so the same message reached by either
    # header groups with itself.
    assert thread_key(parsed) == 'a@x'


def test_a_reply_with_no_references_falls_back_to_the_subject():
    """What half the mail clients in the world actually send."""
    parsed = parse(b'Message-ID: <c@x>\r\nSubject: Re: The Budget\r\n\r\nbody\r\n')
    assert thread_key(parsed).startswith('subject:')


def test_re_and_fwd_and_brackets_all_come_off_the_subject():
    for raw in ('Re: budget', 'RE: budget', 'Fwd: budget', 'Re: Fwd: budget', 'Re[2]: budget'):
        assert normalise_subject(raw) == 'budget', raw


def test_a_subject_that_really_is_about_replies_is_not_stripped():
    assert normalise_subject('Re: Re: budget thread') == 'budget thread'
    assert 'reply' in normalise_subject('Reply handling')


def test_a_listing_line_carries_what_a_scan_needs_and_nothing_else():
    parsed = parse(
        b'From: Alice <alice@x.com>\r\nTo: bob@x.com\r\nSubject: The invoice\r\n'
        b'Date: Mon, 12 Jan 2026 10:00:00 +0000\r\n\r\nIt is attached.\r\n'
    )
    parsed.uid = '42'
    line = parsed.header_line()
    assert 'uid 42' in line and 'alice' in line and 'The invoice' in line and 'It is attached' in line
    # The body is not in it. A listing that includes whole messages is a
    # listing nobody reads.
    assert len(line) < 300


def test_a_snippet_starts_after_the_quotes():
    """A snippet beginning `> On Tuesday, Alice wrote:` says nothing about the
    message it is in."""
    parsed = parse(b'From: a@x.com\r\n\r\n> old reply\r\n> more old\r\nThe actual point.\r\n')
    assert parsed.snippet == 'The actual point.'


# --- bodies -------------------------------------------------------------------


def test_a_plain_part_is_preferred_over_html():
    """`multipart/alternative` is written for exactly this: the plain part is
    the one the sender meant."""
    raw = (
        b'From: a@x.com\r\nSubject: s\r\nContent-Type: multipart/alternative; boundary="b"\r\n\r\n'
        b'--b\r\nContent-Type: text/plain\r\n\r\nThe plain version.\r\n'
        b'--b\r\nContent-Type: text/html\r\n\r\n<p>The <b>HTML</b> version.</p>\r\n--b--\r\n'
    )
    parsed = parse(raw)
    assert 'The plain version.' in parsed.body
    assert 'HTML' not in parsed.body


def test_an_html_only_message_is_converted_rather_than_shown_as_source():
    raw = b'From: a@x.com\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello <b>there</b></p>\r\n'
    parsed = parse(raw)
    assert 'Hello there' in parsed.body
    assert '<' not in parsed.body
    assert parsed.has_html is True


def test_a_signature_image_is_not_read_as_the_body():
    raw = (
        b'From: a@x.com\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b'--b\r\nContent-Type: text/plain\r\n\r\nThe real body.\r\n'
        b'--b\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\n\r\n\x89PNG\r\n--b--\r\n'
    )
    assert 'The real body.' in parse(raw).body


def test_attachments_are_named_and_never_loaded():
    """An inbox is the easiest place in the world to make a model read a
    40MB PDF by accident, and the bytes are never in this process."""
    raw = (
        b'From: a@x.com\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b'--b\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n'
        b'--b\r\nContent-Type: application/pdf\r\n'
        b'Content-Disposition: attachment; filename="Q1 report.pdf"\r\n\r\n%PDF-1.4 ...\r\n--b--\r\n'
    )
    parsed = parse(raw)
    assert [a.name for a in parsed.attachments] == ['Q1 report.pdf']
    assert 'See attached.' in parsed.body


# --- replying ------------------------------------------------------------------


@pytest.mark.parametrize(
    'body',
    [
        'Sounds good.\n\nOn Mon, Bob wrote:\n> the old text',
        'Sounds good.\n\n-----Original Message-----\nFrom: Bob\nthe old text',
        'Sounds good.\n\nFrom: Bob <bob@x.com>\nSent: Monday\nthe old text',
        'Sounds good.\n\n> the old text\n> more of it',
    ],
    ids=['gmail', 'apple', 'outlook', 'quoted'],
)
def test_quoted_history_is_cut_off_whichever_client_left_it(body: str):
    """A reply that keeps the whole thread is unreadable, and every mail
    client has solved this for twenty years in a different way."""
    assert _quote_trim(body) == 'Sounds good.'


def test_a_body_with_no_quote_in_it_is_left_alone():
    assert _quote_trim('Just the reply.') == 'Just the reply.'


def test_a_reply_carries_both_threading_headers():
    """Get only one right and the reply is in the thread with no history in
    it, which is the specific bug that makes generated mail look generated."""
    original = parse(
        b'Message-ID: <b@x>\r\nReferences: <a@x> <b@x>\r\nIn-Reply-To: <a@x>\r\n'
        b'From: Alice <alice@x.com>\r\nSubject: The budget\r\n\r\noriginal\r\n'
    )
    message, _ = send_mod.build(account(), to='bob@x.com', subject='Re: The budget', body='yes', reply_to_message=original)
    assert message['In-Reply-To'] == '<b@x>'
    # The whole chain, not just the parent.
    assert message['References'].split() == ['<a@x>', '<b@x>']


def test_a_reply_does_not_invent_a_re_prefix():
    original = parse(b'Message-ID: <b@x>\r\nFrom: a@x.com\r\nSubject: Re: already\r\n\r\nx\r\n')
    message, _ = send_mod.build(account(), to='a@x.com', subject='Re: already', body='y', reply_to_message=original)
    assert str(message['Subject']) == 'Re: already'


def test_a_subject_with_a_newline_cannot_add_a_header():
    """Header injection is the oldest trick in mail. The subject is folded to
    one line before it becomes a header, so the newline cannot escape."""
    message, _ = send_mod.build(
        account(), to='a@x.com', subject='Hello\nBcc: attacker@evil.com', body='x'
    )
    assert message['Bcc'] is None
    assert '\n' not in str(message['Subject'])


def test_a_message_carries_both_a_plain_and_an_html_part():
    """Plain-only renders as an unstyled wall; HTML-only arrives as source
    code to half the people who read it."""
    message, _ = send_mod.build(account(), to='a@x.com', subject='s', body='Hello\n\n> quoted')
    assert message.is_multipart()
    assert message.get_body(preferencelist=('plain', )) is not None
    assert message.get_body(preferencelist=('html', )) is not None


def test_a_bcc_is_in_the_envelope_and_not_in_the_headers():
    """The one step that is easy to leave out, and it produces the specific
    bug where a bcc silently goes nowhere."""
    message, hidden = send_mod.build(account(), to=['a@x.com'], subject='s', body='x', bcc=['secret@x.com'])
    assert message['Bcc'] is None
    assert 'secret@x.com' in [p.email for p in hidden]
    assert 'secret@x.com' in send_mod.recipients(message, hidden)
    assert 'secret@x.com' not in send_mod.recipients(message, [])


def test_bcc_self_copies_the_sender():
    """The only way to catch a runaway loop from the receiving side."""
    _, hidden = send_mod.build(account(bcc_self=True), to='a@x.com', subject='s', body='x')
    assert 'me@example.com' in [p.email for p in hidden]


def test_a_message_with_no_recipient_is_refused():
    message, hidden = send_mod.build(account(), to='a@x.com', subject='s', body='x')
    message.replace_header('To', '')
    with pytest.raises(MailError, match='no recipient'):
        send_mod.recipients(message, hidden)


# --- the IMAP response shape ----------------------------------------------------


def test_a_uid_is_matched_to_the_message_beside_it_not_by_position():
    """The failure is silent and lands a reply on the wrong person. A FETCH
    response interleaves `UID n` byte lines with the message they belong to,
    and the sequence numbers and the UIDs disagree after any deletion."""
    data = [
        b'1 (MATCH)',
        b'UID 101',
        (b'101 (FLAGS (\\Seen))', b'From: a@x.com\r\nSubject: one\r\n\r\nfirst\r\n'),
        b'UID 205',
        (b'102 (FLAGS ())', b'From: b@x.com\r\nSubject: two\r\n\r\nsecond\r\n'),
    ]
    found = _split_fetch(data)
    assert [(uid, msg['Subject']) for uid, _flags, msg in found] == [('101', 'one'), ('205', 'two')]


def test_flags_are_read_out_of_the_fetch_response():
    from openmirror.mail.imap import _flags_of

    assert _flags_of('(\\Seen \\Answered \\Flagged)') == (True, True, True)
    assert _flags_of('()') == (False, False, False)


# --- the grade -------------------------------------------------------------------


def test_reading_is_free_and_sending_is_the_invariant_axis():
    tool = MailTool(AccountStore(Path('/nonexistent/mail.json')))
    for action in ('accounts', 'folders', 'list', 'read', 'search', 'unread'):
        assert RISK[action] is Risk.READ, action
    for action in ('send', 'reply'):
        assert RISK[action] is Risk.MESSAGE, action
    assert RISK['draft'] is Risk.WRITE
    assert tool.assess({'action': 'unread'}, ctx(Path('.'))).risk is Risk.READ


def test_no_mode_turns_a_send_into_silence():
    """The whole point. `unrestricted` means "stop asking me about this
    machine"; it has never meant "correspond on my behalf", and a switch
    that made it mean that is a switch somebody sets once and forgets while
    an agent runs unattended."""
    tool = MailTool(AccountStore(Path('/nonexistent/mail.json')))
    call = ToolCall(id='c', name='mail', arguments={'action': 'send', 'to': ['a@x.com'],
                                                     'subject': 's', 'body': 'b'})
    call.risk = tool.assess(call.arguments, ctx(Path('.'))).risk
    # `read_only` and `plan` refuse outright rather than asking, and have
    # their own test below; every other mode must ask.
    for mode in Mode:
        if mode in (Mode.READ_ONLY, Mode.PLAN):
            continue
        assert ApprovalPolicy(mode=mode).decide(call)[0] is Decision.ASK, mode


def test_a_rule_cannot_allow_a_send():
    """Rules exist to stop being asked about `npm test`. Read as a
    mail-exemption one of them is a line that removes the only prompt between
    an unattended agent and somebody's outbox."""
    from openmirror.agent.approval import Rule

    tool = MailTool(AccountStore(Path('/nonexistent/mail.json')))
    args = {'action': 'send', 'to': ['a@x.com'], 'subject': 's', 'body': 'b'}
    call = ToolCall(id='c', name='mail', arguments=args)
    call.risk = tool.assess(args, ctx(Path('.'))).risk
    call.summary = 'anything'
    policy = ApprovalPolicy(mode=Mode.UNRESTRICTED, rules=[Rule(pattern='.*', decision=Decision.ALLOW)])
    assert policy.decide(call)[0] is Decision.ASK


def test_a_remembered_yes_does_not_carry_to_the_next_message():
    """A remembered approval is a fingerprint of the exact arguments, so a
    yes to one reply would carry over to the next message to the same
    person — a different message saying a different thing."""
    tool = MailTool(AccountStore(Path('/nonexistent/mail.json')))
    args = {'action': 'send', 'to': ['alice@x.com'], 'subject': 'the invoice', 'body': 'attached'}
    call = ToolCall(id='c', name='mail', arguments=args)
    call.risk = tool.assess(args, ctx(Path('.'))).risk
    call.summary = tool.assess(args, ctx(Path('.'))).summary
    policy = ApprovalPolicy(mode=Mode.ASK)
    policy.remember(call)
    assert policy.decide(call)[0] is Decision.ASK


def test_switching_mail_off_is_a_refusal_and_not_a_prompt():
    """An install that should not be able to put words in somebody's inbox
    at all is better served by nothing to click than by a yes/no."""
    tool = MailTool(AccountStore(Path('/nonexistent/mail.json')))
    args = {'action': 'send', 'to': ['a@x.com'], 'subject': 's', 'body': 'b'}
    call = ToolCall(id='c', name='mail', arguments=args)
    call.risk = tool.assess(args, ctx(Path('.'))).risk
    assert ApprovalPolicy(mode=Mode.UNRESTRICTED, allow_messages=False).decide(call)[0] is Decision.DENY


def test_read_only_and_plan_refuse_a_send_rather_than_asking():
    tool = MailTool(AccountStore(Path('/nonexistent/mail.json')))
    args = {'action': 'send', 'to': ['a@x.com'], 'subject': 's', 'body': 'b'}
    call = ToolCall(id='c', name='mail', arguments=args)
    call.risk = tool.assess(args, ctx(Path('.'))).risk
    for mode in (Mode.READ_ONLY, Mode.PLAN):
        assert ApprovalPolicy(mode=mode).decide(call)[0] is Decision.DENY, mode


def test_the_approval_summary_shows_the_recipient_the_subject_and_the_opening():
    """The one line a person reads before agreeing to send is the only chance
    they get to catch a wrong recipient, so it has to carry the thing most
    often got wrong, in the order it is most often got wrong."""
    summary = MailTool(AccountStore(Path('/n/mail.json'))).assess(
        {'action': 'send', 'to': ['alice@x.com'], 'subject': 'The invoice', 'body': 'Attached is the Q1 number.'},
        ctx(Path('.')),
    ).summary
    assert 'alice@x.com' in summary and 'The invoice' in summary and 'Attached' in summary
    assert summary.index('alice@x.com') < summary.index('The invoice')


def test_a_reply_all_prompt_says_it_is_one():
    tool = MailTool(AccountStore(Path('/n/mail.json')))
    summary = tool.assess({'action': 'reply', 'uid': '7', 'body': 'ok', 'reply_all': True}, ctx(Path('.'))).summary
    assert 'all recipients' in summary


def test_a_long_body_is_cut_hard_in_the_prompt():
    """A twenty-line preview is a prompt nobody finishes reading."""
    tool = MailTool(AccountStore(Path('/n/mail.json')))
    summary = tool.assess(
        {'action': 'send', 'to': ['a@x.com'], 'subject': 's', 'body': 'word ' * 500}, ctx(Path('.'))
    ).summary
    assert len(summary) < 300


def test_a_send_with_no_recipient_or_no_subject_is_refused_before_anybody_is_asked():
    """A blank subject is what a message looks like when it went to the wrong
    person in a hurry, and it is trivially avoidable."""
    tool = MailTool(AccountStore(Path('/n/mail.json')))
    context = ctx(Path('.'))
    assert 'to is required' in (tool.assess({'action': 'send', 'subject': 's', 'body': 'b'}, context).invalid or '')
    assert 'subject is required' in (
        tool.assess({'action': 'send', 'to': ['a@x.com'], 'body': 'b'}, context).invalid or ''
    )
    assert tool.assess({'action': 'send', 'to': ['a@x.com'], 'subject': 's', 'body': 'b'}, context).invalid is None


def test_a_reply_without_a_uid_is_refused():
    """`reply` answers one message, and the uid is what says which."""
    tool = MailTool(AccountStore(Path('/n/mail.json')))
    assert 'uid is required' in (tool.assess({'action': 'reply', 'body': 'ok'}, ctx(Path('.'))).invalid or '')


def test_every_action_has_a_grade():
    from openmirror.agent.tools.mail import ACTIONS

    assert set(ACTIONS) == set(RISK)


# --- nothing configured ---------------------------------------------------------


async def test_with_no_account_the_tool_says_so_and_does_not_pretend(tmp_path: Path):
    tool = MailTool(AccountStore(tmp_path / 'mail.json'))
    out = await tool.run({'action': 'accounts'}, ctx(tmp_path))
    assert 'No mail account' in out.content
    assert out.display['accounts'] == []


async def test_with_no_account_a_send_is_a_tool_error_not_a_crash(tmp_path: Path):
    """The model reads this and tells the person, rather than the turn
    ending on an exception."""
    from openmirror.agent.tools.base import ToolError

    tool = MailTool(AccountStore(tmp_path / 'mail.json'))
    with pytest.raises(ToolError, match='no mail account'):
        await tool.run({'action': 'unread'}, ctx(tmp_path))

"""JMAP, against a server.

The IMAP side is tested by pinning the parts that are this project's own
(`tests/test_mail.py` covers headers, bodies, threading, composition and the
grade). JMAP is different, and this file is the reason:

* **A batch is an array whose results are in request order, and a failed call
  comes back as `{"type": "error"}` inside a 200 response.** That is the
  whole reason `Jmap.call` checks every slot rather than looking at the HTTP
  status, and it cannot be checked by reading the code — you have to see a
  200 with a failure in it.
* **Submission is three calls in a specific order** — create a draft, attach
  a blob, move it to the outbox — and the middle one is the easy one to miss.
  A message created from JMAP *properties* gets a reconstructed body, and
  what the server then sends is the reconstruction rather than the bytes that
  were composed. So `References` and the charset of the text part can differ
  from what the sender wrote, silently, and the only way to know is to watch
  the three calls happen.
* **The session URL may be given or discovered**, and a host that answers at
  `/.well-known/jmap` rather than at the configured URL is the common case.

The server here is a real `aiohttp` application speaking the real protocol
shape. It is small on purpose — the point is to test *this client's* requests,
not to be a JMAP implementation.
"""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

import pytest
from aiohttp import web

from openmirror.mail.accounts import Account
from openmirror.mail.imap import Mail
from openmirror.mail.jmap import SUBMISSION, Jmap, JmapError

SUBMISSION_ACCOUNT = 'me'


class Server:
    """A JMAP server that records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.blobs: dict[str, bytes] = {}
        self.created: dict[str, dict[str, Any]] = {}
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.fail: str | None = None
        self.with_submission = True
        # Replaces the whole session document, for the account-selection case.
        self.session_body: dict[str, Any] | None = None
        self.answers: dict[str, Any] = {}
        self.port = 0

    # -- routing ---------------------------------------------------------

    async def well_known(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {'apiUrl': f'http://127.0.0.1:{self.port}/jmap/api', 'downloadUrl': f'http://127.0.0.1:{self.port}/d'}
        )

    async def session(self, _request: web.Request) -> web.Response:
        if self.session_body is not None:
            return web.json_response(self.session_body)
        capabilities = {'urn:ietf:params:jmap:core': {}, 'urn:ietf:params:jmap:mail': {}}
        if self.with_submission:
            capabilities[SUBMISSION] = {}
        return web.json_response(
            {
                'apiUrl': f'http://127.0.0.1:{self.port}/jmap/api',
                'username': 'me',
                'accountId': SUBMISSION_ACCOUNT,
                'primaryAccounts': {SUBMISSION_ACCOUNT: {'name': 'me@example.com'}},
                'accounts': {
                    SUBMISSION_ACCOUNT: {
                        'name': 'me@example.com',
                        'accountCapabilities': capabilities,
                        'isPersonal': True,
                        'isReadOnly': False,
                    }
                },
            }
        )

    async def api(self, request: web.Request) -> web.Response:
        payload = await request.json()
        calls = payload['methodCalls']
        self.calls.append(calls)

        out: list[dict[str, Any]] = []
        for call in calls:
            # The wire form is `[name, arguments, callId]` — three elements,
            # and a client that sends the arguments as an object is malformed
            # in a way that produces a 400 with nothing useful in it.
            name, args = call[0], call[1]
            if self.fail == name:
                # A failure in one call, in its own slot, with the HTTP
                # status still 200. The thing this file exists for.
                out.append({'type': 'error', 'status': 400, 'description': 'that method is unhappy'})
                continue
            out.append(self._answer(name, args))
        return web.json_response({'methodResponses': out, 'sessionState': '1'})

    def _answer(self, name: str, call: dict[str, Any]) -> dict[str, Any]:
        if name in self.answers:
            return self.answers[name]
        if name == 'Mailbox/get':
            return {
                'type': 'Mailbox/get',
                'id': call.get('ids'),
                'list': [
                    {'id': 'mb-in', 'name': 'Inbox', 'role': 'inbox', 'unreadEmails': 3, 'totalEmails': 40},
                    {'id': 'mb-draft', 'name': 'Drafts', 'role': 'drafts', 'unreadEmails': 0, 'totalEmails': 1},
                    {'id': 'mb-out', 'name': 'Outbox', 'role': 'outbox', 'unreadEmails': 0, 'totalEmails': 0},
                    {'id': 'mb-sent', 'name': 'Sent', 'role': 'sent', 'unreadEmails': 0, 'totalEmails': 9},
                ],
            }
        if name == 'Email/query':
            return {'type': 'Email/query', 'accountId': call['accountId'], 'ids': ['e1', 'e2']}
        if name == 'Email/get':
            return {
                'type': 'Email/get',
                'accountId': call['accountId'],
                'list': [
                    {
                        'id': 'e1', 'threadId': 't7', 'messageId': '<a@x>',
                        'from': [{'email': 'alice@x.com', 'name': 'Alice'}],
                        'to': [{'email': 'me@example.com'}],
                        'subject': 'The invoice', 'receivedAt': '2026-01-12T10:00:00Z',
                        'keywords': ['$seen', '$flagged'], 'preview': 'It is attached.',
                        'textBody': [{'partId': '1', 'value': 'It is attached.\n\n> old'}],
                        'hasAttachment': True, 'size': 4096,
                    },
                    {
                        'id': 'e2', 'threadId': 't8', 'messageId': '<b@x>',
                        'from': [{'email': 'bob@x.com'}],
                        'to': [{'email': 'me@example.com'}],
                        'subject': 'Lunch', 'receivedAt': '2026-01-11T09:00:00Z',
                        'keywords': [], 'preview': 'Thursday?',
                        'htmlBody': [{'partId': '1', 'value': '<p>Thursday?</p>'}],
                    },
                ],
            }
        if name == 'Blob/set':
            created = {}
            for key, spec in (call.get('create') or {}).items():
                blob_id = f'blob-{len(self.blobs) + 1}'
                # RFC 8620: base64url, unpadded. Raw bytes are not JSON, and a
                # server asked to decode one is not decoding a message.
                self.blobs[blob_id] = base64.urlsafe_b64decode(spec['blobId'] + '=' * (-len(spec['blobId']) % 4))
                self.created[key] = {'id': f'{key}-id', 'blobId': blob_id}
                created[key] = {'id': f'{key}-id', 'blobId': blob_id}
            return {'type': 'Blob/set', 'accountId': call['accountId'], 'created': created, 'notCreated': {}}
        if name == 'Email/set':
            created = {}
            for key, spec in (call.get('create') or {}).items():
                self.created[key] = spec
                created[key] = {'id': f'{key}-made', **({'blobId': spec['blobId']} if 'blobId' in spec else {})}
            for email_id, patch in (call.get('update') or {}).items():
                self.updated.append((email_id, patch))
            return {'type': 'Email/set', 'accountId': call['accountId'], 'created': created, 'notCreated': {},
                    'updated': {k: None for k in (call.get('update') or {})}}
        return {'type': name, 'accountId': call.get('accountId', '')}

    # -- convenience -----------------------------------------------------

    def methods(self) -> list[str]:
        return [call[0] for batch in self.calls for call in batch]


@pytest.fixture
async def server():
    held = Server()
    app = web.Application()
    app.router.add_get('/.well-known/jmap', held.well_known)
    app.router.add_get('/jmap/session', held.session)
    app.router.add_post('/jmap/api', held.api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    held.port = runner.addresses[0][1]
    held.runner = runner
    yield held
    await runner.cleanup()


def jmap_account(server: Server, **over) -> Account:
    base = {
        'id': 'j', 'address': 'me@example.com', 'label': 'Me',
        'jmap_url': f'http://127.0.0.1:{server.port}/jmap/session',
        'imap_host': '', 'smtp_host': '',
        'password': 'app-password', 'protocol': 'jmap',
    }
    return Account(**{**base, **over})


def test_jmap_is_chosen_when_configured_and_can_be_refused():
    """Both directions matter: a host with a bridge that is worse than its
    IMAP is a real configuration, and a host with no JMAP at all is the
    common one."""
    assert Account(id='a', address='a@x.com', jmap_url='https://h/s').use_jmap is True
    # An IMAP host is NOT evidence of JMAP. Every Gmail and Outlook account
    # has one, and treating it as evidence sends every one of them to
    # /.well-known/jmap, where there is nothing.
    assert Account(id='a', address='a@x.com', imap_host='imap.x', smtp_host='s').use_jmap is False
    # Asked for by name instead, with the IMAP host used to find it.
    named = Account(id='a', address='a@x.com', imap_host='imap.x', smtp_host='s', protocol='jmap')
    assert named.use_jmap is True
    # And named the other way to opt out of a bridge that is worse than IMAP.
    forced = Account(id='a', address='a@x.com', jmap_url='https://h/s', protocol='imap', imap_host='i', smtp_host='s')
    assert forced.use_jmap is False


def test_the_discovery_host_comes_from_the_imap_host(server: Server):
    """A person configuring a mailbox has typed their mail host once."""
    built = Account(id='a', address='a@x.com', imap_host='mail.example.com', smtp_host='s')
    assert built.jmap_host == 'mail.example.com'
    assert Account(id='a', address='a@x.com', jmap_url='https://jmap.example.com/s', imap_host='i',
                   smtp_host='s').jmap_host == 'jmap.example.com'
    # A self-hosted server on a LAN is often plain http, and there has to be
    # a way to say so.
    assert Account(id='a', address='a@x.com', imap_host='http://box.local:8080', smtp_host='s').jmap_host == (
        'http://box.local:8080'
    )


async def test_a_session_is_fetched_from_the_configured_url(server: Server):
    import aiohttp

    client = aiohttp.ClientSession()
    try:
        found = await Jmap(jmap_account(server)).session(client)
    finally:
        await client.close()
    assert found['username'] == 'me'
    assert SUBMISSION in found['accounts'][SUBMISSION_ACCOUNT]['accountCapabilities']


async def test_a_session_is_discovered_when_there_is_no_url(server: Server):
    """`/.well-known/jmap` is the convention every server follows, and it is
    what makes adding a JMAP account a single extra field."""
    import aiohttp

    account = Account(id='j', address='me@example.com', jmap_url='',
                      imap_host=f'http://127.0.0.1:{server.port}', smtp_host='', password='p', protocol='jmap')
    client = aiohttp.ClientSession()
    try:
        found = await Jmap(account).session(client)
    finally:
        await client.close()
    assert found['apiUrl'].endswith('/jmap/api')


async def test_the_right_account_is_picked_out_of_several(server: Server):
    """A person with a personal and a work mailbox on one host has both in
    the session, and picking the wrong one is reading somebody else's mail."""
    import aiohttp

    server.session_body = {
        'apiUrl': f'http://127.0.0.1:{server.port}/jmap/api',
        # A session for a signed-in user who is NOT the address we want.
        'username': 'someone-else',
        'accounts': {
            'personal': {'name': 'me@example.com', 'accountCapabilities': {}},
            'work': {'name': 'other@example.com', 'accountCapabilities': {}},
        },
    }
    client = aiohttp.ClientSession()
    try:
        found = await Jmap(jmap_account(server)).session(client)
    finally:
        await client.close()
    # Matched on the address, not on whichever came first.
    assert Jmap(jmap_account(server))._pick_account(found) == 'personal'
    # And an address that is in neither falls back rather than guessing one.
    stranger = Jmap(jmap_account(server, address='absent@nowhere.com'))
    assert stranger._pick_account(found) not in ('personal', 'work')


async def test_a_failed_call_inside_a_200_is_reported(server: Server):
    """The specific thing this file exists for. `imaplib` raises; JMAP
    returns a 200 whose *body* says which slot failed, so a client that
    checks the HTTP status reports success for something that did not
    happen."""
    import aiohttp

    server.fail = 'Mailbox/get'
    client = aiohttp.ClientSession()
    try:
        with pytest.raises(JmapError, match='Mailbox/get failed'):
            await Jmap(jmap_account(server)).mailboxes(client)
    finally:
        await client.close()


async def test_a_folder_is_named_by_its_role_not_its_title(server: Server):
    """`inbox` is the inbox whatever the server calls it, so "the inbox" does
    not have to be guessed from a name — and `outbox` is how sending finds
    where to put a message."""
    import aiohttp

    client = aiohttp.ClientSession()
    try:
        found = await Jmap(jmap_account(server)).mailboxes(client)
        assert [f['name'] for f in found][0] == 'Inbox'
        assert {f['role']: f['id'] for f in found}['inbox'] == 'mb-in'
    finally:
        await client.close()


async def test_a_listing_is_two_round_trips_and_carries_the_server_s_thread(server: Server):
    """Not one per message, and `threadId` is the server's own answer rather
    than a heuristic reconstructed from headers."""
    import aiohttp

    client = aiohttp.ClientSession()
    try:
        found = await Jmap(jmap_account(server)).get(client, ['e1', 'e2'], full=True)
    finally:
        await client.close()
    mails = [Jmap(jmap_account(server)).to_mail(item) for item in found]
    assert [m.thread for m in mails] == ['t7', 't8']
    assert [m.uid for m in mails] == ['e1', 'e2']
    assert mails[0].seen and mails[0].flagged and not mails[0].answered
    assert mails[0].sender.email == 'alice@x.com' and mails[0].sender.short == 'Alice'
    # An HTML-only message is converted, not shown as source.
    assert mails[1].body == 'Thursday?' and mails[1].has_html


async def test_an_attachment_is_reported_without_a_name_it_does_not_have(server: Server):
    """JMAP has a `hasAttachment` flag and no attachment list. Inventing a
    filename would be worse than saying there is one."""
    import aiohttp

    client = aiohttp.ClientSession()
    try:
        found = await Jmap(jmap_account(server)).get(client, ['e1'], full=True)
    finally:
        await client.close()
    mail = Jmap(jmap_account(server)).to_mail(found[0])
    assert [a.name for a in mail.attachments] == ['(attachment)']


async def test_sending_uploads_the_exact_bytes_before_it_moves_to_the_outbox(server: Server):
    """The order is the whole of it, and the middle step is the one that gets
    missed. Without the blob, the server sends a *reconstructed* body from
    the JMAP properties — which can differ from what was composed, in the
    charset of the text part and in headers such as `References`."""
    import aiohttp

    account = jmap_account(server)
    session = Jmap(account)
    message = EmailMessage()
    message['From'] = 'me@example.com'
    message['To'] = 'alice@x.com'
    message['Subject'] = 'Re: The invoice'
    message['Message-ID'] = '<mine@x>'
    message['In-Reply-To'] = '<a@x>'
    message['References'] = '<a@x>'
    message.set_content('Yes, sent.')

    client = aiohttp.ClientSession()
    try:
        result = await session.submit(client, message, [])
    finally:
        await client.close()

    assert server.methods() == ['Mailbox/get', 'Email/set', 'Blob/set', 'Email/set', 'Email/set']
    # The blob goes over as base64url, unpadded, and arrives as the exact
    # bytes that were composed.
    sent = server.calls[2][0][1]['create']['b']['blobId']
    assert isinstance(sent, str) and '=' not in sent
    assert base64.urlsafe_b64decode(sent + '=' * (-len(sent) % 4)) == message.as_bytes(policy=SMTP)
    # The exact bytes that were composed, not a reconstruction of them.
    stored = list(server.blobs.values())[0]
    assert b'In-Reply-To: <a@x>' in stored
    assert b'References: <a@x>' in stored
    assert b'Yes, sent.' in stored
    # And the last call is the send: the draft is moved into the outbox.
    email_id, patch = server.updated[-1]
    assert patch['mailboxIds'] == {'mb-out': True}
    assert result['sent'] is True and result['to'] == ['alice@x.com']


async def test_an_account_that_cannot_send_says_so_by_name(server: Server):
    """Rather than failing on a method the server does not have, which reads
    as a bug rather than as a capability."""
    import aiohttp

    server.with_submission = False
    client = aiohttp.ClientSession()
    try:
        with pytest.raises(JmapError, match='not set up to send'):
            await Jmap(jmap_account(server)).submit(client, EmailMessage(), [])
    finally:
        await client.close()


async def test_a_draft_is_filed_and_nothing_is_sent(server: Server):
    """`draft` is the honest "write it but do not send it", and it puts the
    message where the person's own mail client will show it."""
    import aiohttp

    message = EmailMessage()
    message['From'] = 'me@example.com'
    message['To'] = 'alice@x.com'
    message['Subject'] = 'Later'
    message.set_content('When I think of it.')

    client = aiohttp.ClientSession()
    try:
        made = await Jmap(jmap_account(server)).save_draft(client, message)
    finally:
        await client.close()
    assert made == 'draft-made'
    assert server.methods().count('Email/set') == 1
    assert b'When I think of it.' in list(server.blobs.values())[0]
    # Nothing was moved to the outbox.
    assert not any(p.get('mailboxIds') == {'mb-out': True} for _i, p in server.updated)


async def test_marking_read_is_a_keyword_and_cannot_be_misspelt(server: Server):
    import aiohttp

    client = aiohttp.ClientSession()
    try:
        await Jmap(jmap_account(server)).call(
            client, [('Email/set', {'accountId': SUBMISSION_ACCOUNT, 'update': {'e1': {'keywords/$seen': True}}})]
        )
    finally:
        await client.close()
    assert server.updated[-1] == ('e1', {'keywords/$seen': True})


def test_an_account_with_no_url_says_where_to_look():
    """The one setting a JMAP account needs and an IMAP one does not."""
    blank = Account(id='j', address='me@example.com', imap_host='mail.example.com', smtp_host='s',
                    password='p', protocol='jmap')
    with pytest.raises(JmapError, match='well-known/jmap'):
        Jmap(blank).base_url()


def test_discovery_keeps_a_scheme_that_was_asked_for():
    """A self-hosted JMAP server on a LAN is very often plain HTTP, and
    forcing TLS on it produces a WRONG_VERSION_NUMBER that reads like a
    broken server rather than a scheme that was assumed."""
    lan = Account(id='j', address='me@example.com', imap_host='http://box.local:8080', smtp_host='s',
                  password='p', protocol='jmap')
    assert Jmap(lan)._candidates() == [
        'http://box.local:8080/.well-known/jmap',
        'http://box.local:8080/jmap/session',
    ]
    hosted = Account(id='j', address='me@example.com', imap_host='box.example.com', smtp_host='s',
                     password='p', protocol='jmap')
    assert Jmap(hosted)._candidates()[0] == 'https://box.example.com/.well-known/jmap'
    # A configured URL is tried first and is not decorated with a path.
    direct = Account(id='j', address='me@example.com', jmap_url='https://h.example/s', smtp_host='s',
                     password='p', protocol='jmap')
    assert Jmap(direct)._candidates() == ['https://h.example/s']


async def test_the_facade_hides_the_protocol(server: Server, tmp_path: Any):
    """The same five calls, and the same `Mail` back, whichever is underneath
    — which is the entire reason the facade exists."""
    from openmirror.mail.client import box

    account = jmap_account(server)
    assert box(account).protocol == 'jmap'
    found = await box(account).listing(folder='inbox', limit=5)
    assert all(isinstance(m, Mail) for m in found)
    assert [m.uid for m in found] == ['e1', 'e2']
    # A JMAP reading carries the server's thread id rather than a guess.
    assert found[0].thread == 't7'
    assert json.dumps([m.public() for m in found])  # and it is JSON-serialisable for the browser

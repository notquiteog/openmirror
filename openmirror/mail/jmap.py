"""JMAP: the same mailbox, over JSON instead of IMAP.

RFC 8620 for the session and the objects, RFC 8621 for `Email` and
`Mailbox`. Every major host either speaks it or has a bridge that does —
Fastmail and Proton Mail natively, Gmail and Microsoft through the same
third-party bridges that make their IMAP unreliable anyway.

**Why it is worth having alongside IMAP rather than instead of it.** JMAP is
strictly the better protocol and IMAP is strictly the better-supported one, so
the honest answer is both, chosen per account:

* **One round trip gets a whole page.** `Email/query` returns a list of ids
  and `Email/get` returns the objects, batched, over HTTPS. The IMAP path
  above issues a SEARCH and then a FETCH per message over a socket, and a
  fifty-message listing on a slow link is visibly slow in a way this is not.
* **Threading is a field, not a guess.** `threadId` is computed by the
  server, correctly, by the same code that draws the thread in the person's
  own client. IMAP has no such field, which is why `imap.py` has to
  reconstruct it from `References` and a normalised subject — a heuristic
  that is right most of the time and visibly wrong some of the time.
* **A keyword is a set.** `$seen`, `$flagged`, `$answered` are booleans in
  JSON, so marking read cannot be spelled wrong and cannot silently fail to
  take effect.
* **Submission is a mailbox.** The outbox is a `Mailbox` with the role
  `outbox`, and putting a message in it sends it. There is no separate
  protocol, no second authentication and no separate failure mode.

What it costs: it is HTTP, so there is one more thing that can be configured
wrong (the session URL), and hosts that only speak IMAP cannot use it. Both
of those are why `Account.protocol` exists and why the default is IMAP.

**Every request is a POST of a JSON array of method calls**, and a response
is an array of results in the same order. A method that fails does not fail
the batch: it comes back as `{"type": "error", ...}` in its own slot, and
that has to be checked per call rather than by looking at the HTTP status.
Getting that wrong is how "it said OK" and "the first thing did not happen"
end up being the same experience.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from typing import Any

import aiohttp

from openmirror.agent.tools.web import html_to_text
from openmirror.mail.accounts import Account, Address, MailError, parse_addresses
from openmirror.mail.imap import BODY_LIMIT, MAX_PAGE, SNIPPET, Attachment, Mail, normalise_subject

log = logging.getLogger(__name__)

# The capability that means this account can send. A JMAP session that lacks
# it is read-only, and the send path says so by name rather than failing on a
# method the server does not have.
SUBMISSION = 'urn:ietf:params:jmap:submission'
# A list this long in one `Email/get` is a large response but one round trip;
# beyond it, paging costs more than it saves.
BATCH = 50


class JmapError(MailError):
    pass


def _words(text: str, count: int = 3) -> str:
    """The first few non-empty, non-quoted lines — a JMAP snippet."""
    out: list[str] = []
    for line in (text or '').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('>'):
            continue
        out.append(stripped)
        if len(out) >= count:
            break
    return ' '.join(out)[:SNIPPET]


class Jmap:
    """One JMAP session. Opened, used, closed."""

    def __init__(self, account: Account, *, timeout: int = 30) -> None:
        self.account = account
        self.timeout = timeout
        self._session: dict[str, Any] | None = None
        self._account_id: str = ''

    # -- the session --------------------------------------------------------

    def base_url(self) -> str:
        url = (self.account.jmap_url or '').strip()
        if not url:
            raise JmapError(
                f'{self.account.name}: this account is set to JMAP but has no URL. It is the one shown '
                f'by the host under "JMAP session URL", or discoverable at https://<host>/.well-known/jmap'
            )
        if not url.startswith(('http://', 'https://')):
            url = f'https://{url}'
        return url.rstrip('/')

    def _candidates(self) -> list[str]:
        """Where a session might be, best first.

        The configured URL is first when there is one. Otherwise discovery,
        at the well-known path and at the one other place every server has
        used — which is the difference between "add a JMAP account" being one
        field and being four.

        An explicit `http://` in the host is kept: a self-hosted JMAP server
        on a LAN is very often plain HTTP, and forcing TLS on it produces a
        `WRONG_VERSION_NUMBER` that reads like a broken server rather than a
        scheme that was assumed.
        """
        configured = (self.account.jmap_url or '').strip()
        if configured:
            return [configured if configured.startswith('http') else f'https://{configured}']
        host = (self.account.jmap_host or '').strip()
        if not host:
            return []
        scheme = 'http' if host.startswith('http://') else 'https'
        bare = re.sub(r'^https?://', '', host).rstrip('/')
        return [f'{scheme}://{bare}/.well-known/jmap', f'{scheme}://{bare}/jmap/session']

    def _auth_header(self) -> dict[str, str]:
        """The `Authorization` header, which every JMAP server accepts.

        Built by hand rather than through `aiohttp.BasicAuth`, which is
        deprecated and going away: the header is one base64 and this way there
        is no deprecation to chase when aiohttp 4 lands, and the token case
        shares the same path as the password one.

        A bearer token is used when there is one. Kept alongside the password
        rather than instead of it: a bridge that hands out a token usually
        still accepts the app password underneath, and a config that can
        express both is one that does not have to be rewritten when the
        operator changes their mind.
        """
        if self.account.jmap_token:
            return {'Authorization': f'Bearer {self.account.jmap_token}'}
        user = self.account.address or self.account.jmap_username or ''
        if not user:
            return {}
        return {'Authorization': aiohttp.encode_basic_auth(user, self.account.secret())}

    async def session(self, client: aiohttp.ClientSession) -> dict[str, Any]:
        """The session resource, cached for the life of this object.

        Fetched from the configured URL, or discovered at `/.well-known/jmap`
        when there is none — which is the case that makes adding a JMAP
        account a single field, because the well-known URL is a convention
        every server follows and the answer names the API endpoint itself.
        """
        if self._session is not None:
            return self._session

        headers = {'Accept': 'application/json', **self._auth_header()}

        urls = self._candidates()
        last = ''
        for url in urls:
            try:
                # transport-exempt: the person's own mail host, not a model server.
                async with client.get(url, headers=headers, timeout=self.timeout) as response:
                    if response.status == 401:
                        raise JmapError(
                            f'{self.account.name}: the server refused these credentials at {url}. '
                            f'For a JMAP account this is usually an app password rather than the '
                            f'account password, or a token that has been revoked.'
                        )
                    if response.status >= 400:
                        last = f'HTTP {response.status}'
                        continue
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
                last = str(exc)
                continue
            if isinstance(payload, dict) and payload.get('apiUrl'):
                self._session = payload
                self._account_id = self._pick_account(payload)
                return payload

        raise JmapError(
            f'{self.account.name}: no JMAP session at {" or ".join(urls) or "(no URL configured)"}'
            + (f' — {last}' if last else '')
        )

    def _pick_account(self, payload: dict[str, Any]) -> str:
        """Which account in the session this one is.

        A session can carry several, and a person with a personal and a work
        mailbox on one host has both. The rule is the documented one: the
        session's `primaryAccounts`/`username` match, then the account whose
        address matches, then `accountId` as the fallback — and when the
        session names exactly one, that one.
        """
        username = (payload.get('username') or '').lower()
        accounts: dict[str, Any] = payload.get('accounts') or {}
        if username in accounts:
            return username
        for account_id, entry in accounts.items():
            if isinstance(entry, dict) and str(entry.get('name', '')).lower() == (self.account.address or '').lower():
                return account_id
        for account_id in (payload.get('primaryAccounts') or {}):
            if account_id in accounts:
                return account_id
        if self.account.jmap_username and self.account.jmap_username in accounts:
            return self.account.jmap_username
        if len(accounts) == 1:
            return next(iter(accounts))
        return payload.get('accountId') or self.account.jmap_username or self.account.address

    # -- calling ------------------------------------------------------------

    async def call(
        self, client: aiohttp.ClientSession, calls: list[tuple[str, dict[str, Any]]], *, using: list[str] | None = None
    ) -> list[Any]:
        """Run a batch of method calls, and check each one.

        Takes `(name, arguments)` pairs and puts them on the wire as
        `[name, arguments, callId]` — which is what the protocol specifies and
        what every server expects. Sending the arguments as an object instead
        is not a variant any server accepts; it arrives as a malformed request
        and the only thing to look at is a 400 with nothing useful in it.

        A JMAP response is an array in the same order as the request, and a
        failed call is a `{"type": "error"}` object in its own slot with the
        HTTP status still 200. So the batch cannot be checked by looking at
        the response code — each slot has to be looked at, and that is the
        whole of the per-result handling below.
        """
        payload = {
            'using': using or ['urn:ietf:params:jmap:core', 'urn:ietf:params:jmap:mail'],
            'methodCalls': [[name, args, f'c{index}'] for index, (name, args) in enumerate(calls)],
        }
        session = await self.session(client)
        try:
            # transport-exempt: the person's own mail host, not a model server.
            async with client.post(
                session['apiUrl'], json=payload, headers=self._auth_header(), timeout=self.timeout
            ) as response:
                if response.status == 401:
                    raise JmapError(f'{self.account.name}: the session was refused mid-call')
                if response.status >= 400:
                    body = (await response.text())[:300]
                    raise JmapError(f'{self.account.name}: the server returned HTTP {response.status} — {body}')
                data = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise JmapError(f'{self.account.name}: could not reach the JMAP server ({exc})') from exc

        results = data.get('methodResponses') if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(calls):
            raise JmapError(f'{self.account.name}: the server returned a response this cannot read')
        for index, result in enumerate(results):
            if isinstance(result, dict) and result.get('type') == 'error':
                # The name of the method is the one that was asked for, which
                # is what makes this readable rather than "error at index 2".
                method = calls[index][0] if index < len(calls) else 'a call'
                detail = result.get('description') or result.get('type', 'unknown')
                raise JmapError(f'{self.account.name}: {method} failed — {detail}')
        return results

    def _basic(self) -> aiohttp.BasicAuth | None:
        if self.account.jmap_token:
            return None
        user = self.account.address or self.account.jmap_username or ''
        # transport-exempt: an auth header, not a connection.
        return aiohttp.BasicAuth(user, self.account.secret()) if user else None

    def _headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.account.jmap_token}'} if self.account.jmap_token else {}

    # -- objects ------------------------------------------------------------

    async def mailboxes(self, client: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """Every mailbox, with counts and the role that names it.

        The role is what makes this worth having over a list of names: a
        server's inbox is `inbox` whatever it is called, so "the inbox" does
        not have to be guessed from a name, and `outbox` is how the send path
        finds where to put a message.
        """
        results = await self.call(
            client,
            [(
                'Mailbox/get',
                {
                    'accountId': self._account_id,
                    'ids': None,
                    'properties': ['id', 'name', 'role', 'parentId', 'unreadEmails', 'totalEmails', 'sortOrder'],
                },
            )],
        )
        boxes = results[0].get('list', []) if isinstance(results[0], dict) else []
        return sorted(
            (
                {
                    'name': box.get('name', ''),
                    'role': box.get('role') or '',
                    'id': box.get('id', ''),
                    'unread': box.get('unreadEmails'),
                    'total': box.get('totalEmails'),
                }
                for box in boxes
            ),
            key=lambda b: (b['role'] != 'inbox', b['name'].lower()),
        )

    def _mailbox_id(self, boxes: list[dict[str, Any]], wanted: str) -> str:
        """Resolve `inbox`, or a name, or an id, to a mailbox id."""
        key = (wanted or 'inbox').strip().lower()
        for box in boxes:
            if box['role'] == key:
                return box['id']
        for box in boxes:
            if box['name'].lower() == key:
                return box['id']
        hits = [box for box in boxes if box['name'].lower().startswith(key)]
        if len(hits) == 1:
            return hits[0]['id']
        raise JmapError(
            f'no mailbox called {wanted!r}. There is: {", ".join(sorted(b["name"] for b in boxes)[:20])}'
        )

    async def query(
        self,
        client: aiohttp.ClientSession,
        *,
        mailbox_id: str,
        limit: int = 25,
        unread_only: bool = False,
        search: str = '',
        since: str = '',
    ) -> list[str]:
        """Ids, newest first, then capped — two round trips, not one per message."""
        filter: dict[str, Any] = {'inMailbox': mailbox_id}
        if unread_only:
            filter['notKeyword'] = '$seen'
        if since:
            filter['after'] = since
        if search:
            # `text` searches the server's index, which is what a person means
            # by "find" and which does not need the message bodies fetched
            # first — the reason this is one call rather than a fetch-then-
            # filter pass over the whole folder.
            filter['text'] = search

        results = await self.call(
            client,
            [(
                'Email/query',
                {
                    'accountId': self._account_id,
                    'filter': filter,
                    'sort': [{'property': 'receivedAt', 'isAscending': False}],
                    'limit': max(1, min(int(limit or 25), MAX_PAGE)),
                },
            )],
        )
        return list(results[0].get('ids', [])) if isinstance(results[0], dict) else []

    async def get(self, client: aiohttp.ClientSession, ids: list[str], *, full: bool = True) -> list[dict[str, Any]]:
        """The objects, in pages, with their bodies.

        Batched at `BATCH` per call: one request for fifty messages is one
        round trip, and a hundred and fifty is three rather than a hundred
        and fifty.
        """
        out: list[dict[str, Any]] = []
        properties = [
            'id', 'blobId', 'threadId', 'mailboxIds', 'from', 'to', 'cc', 'bcc', 'subject', 'receivedAt',
            'sentAt', 'messageId', 'inReplyTo', 'references', 'keywords', 'preview', 'hasAttachment',
        ]
        if full:
            properties += ['textBody', 'htmlBody', 'size', 'receivedAt']
        for start in range(0, len(ids), BATCH):
            page = ids[start:start + BATCH]
            results = await self.call(
                client, [('Email/get', {'accountId': self._account_id, 'ids': page, 'properties': properties})]
            )
            if isinstance(results[0], dict):
                out.extend(results[0].get('list', []))
        return out

    def to_mail(self, raw: dict[str, Any], *, folder: str = 'INBOX') -> Mail:
        """A JMAP `Email` object into the shape the rest of this project uses."""
        sender = (raw.get('from') or [{}])[0] if raw.get('from') else {}
        keywords = set(raw.get('keywords') or [])
        body = '\n'.join(part.get('value', '') for part in (raw.get('textBody') or []))
        saw_html = False
        if not body.strip():
            body = '\n'.join(html_to_text(part.get('value', '')) for part in (raw.get('htmlBody') or []))
            saw_html = True
        body = body[:BODY_LIMIT]

        mail = Mail(
            uid=raw.get('id', ''),
            folder=folder,
            message_id=raw.get('messageId') or '',
            subject=raw.get('subject') or '',
            sender=Address(sender.get('email', ''), sender.get('name') or ''),
            to=[Address(p.get('email', ''), p.get('name') or '') for p in (raw.get('to') or [])],
            cc=[Address(p.get('email', ''), p.get('name') or '') for p in (raw.get('cc') or [])],
            date=str(raw.get('receivedAt') or raw.get('sentAt') or ''),
            body=body,
            snippet=(raw.get('preview') or _words(body))[:SNIPPET],
            seen='$seen' in keywords,
            answered='$answered' in keywords,
            flagged='$flagged' in keywords,
            thread=raw.get('threadId') or f'subject:{normalise_subject(raw.get("subject") or "")}',
            in_reply_to=raw.get('inReplyTo') or '',
            references=[r for r in (raw.get('references') or []) if r],
            has_html=saw_html,
        )
        if raw.get('hasAttachment'):
            # Counted, not listed: JMAP's `Email/get` does not return
            # attachment names at all, and the alternative — fetching each
            # message's raw blob to find out — is a large transfer per message
            # to answer a question nobody asked.
            mail.attachments.append(_AttachmentStub(raw.get('size', 0)))
        return mail

    # -- writing ------------------------------------------------------------

    async def submit(self, client: aiohttp.ClientSession, message: EmailMessage, bcc: list[Address]) -> dict[str, Any]:
        """Send by filing the message in the outbox.

        Three calls, and the middle one is the part that is easy to miss: a
        message created in Drafts from JMAP properties has a *reconstructed*
        body, and the server will send the reconstruction rather than the
        bytes that were actually composed. Setting `blobId` to the exact
        RFC 5322 bytes is what makes what gets sent what was written —
        otherwise headers such as `References` and the charset of the text
        part can differ from what the sender intended.
        """
        session = await self.session(client)
        capabilities = (session.get('accounts') or {}).get(self._account_id, {}).get('accountCapabilities') or {}
        if SUBMISSION not in capabilities:
            raise JmapError(
                f'{self.account.name}: this account is not set up to send over JMAP — the session does not '
                f'offer {SUBMISSION}. Set it to IMAP, or send through a host that does.'
            )

        boxes = await self.mailboxes(client)
        drafts = next((b for b in boxes if b['role'] == 'drafts'), None)
        outbox = next((b for b in boxes if b['role'] == 'outbox'), None)
        if not outbox:
            raise JmapError(f'{self.account.name}: this server has no outbox, so it cannot send')

        raw = message.as_bytes(policy=SMTP_POLICY)
        envelope = _envelope(message, bcc)

        plain = message.get_body(preferencelist=('plain',))
        created = await self.call(
            client,
            [(
                'Email/set',
                {
                    'accountId': self._account_id,
                    'create': {
                        'draft': {
                            'mailboxIds': ({drafts['id']: True} if drafts else {outbox['id']: True}),
                            'keywords': {'$draft': True},
                            # Minimal placeholders, replaced by the blob below.
                            'from': [{'email': self.account.address}],
                            'to': [{'email': a} for a in envelope['to']],
                            'subject': str(message.get('Subject', '')),
                            'textBody': [{'partId': '1', 'value': plain.get_content() if plain else ''}],
                        }
                    },
                },
            )],
        )
        created_id = ''
        if isinstance(created[0], dict):
            entry = (created[0].get('created') or {}).get('draft') or {}
            created_id = entry.get('id', '')
        if not created_id:
            raise JmapError(f'{self.account.name}: the server would not accept the draft, so nothing was sent')

        # The exact bytes, uploaded as a blob, then attached to the draft.
        blob_id = await self._upload_blob(client, raw)
        await self.call(
            client,
            [('Email/set', {
                'accountId': self._account_id,
                'update': {created_id: {'blobId': blob_id, 'keywords': {'$draft': True}}},
            })],
        )
        # Into the outbox. This is the send.
        await self.call(
            client,
            [('Email/set', {
                'accountId': self._account_id,
                'update': {created_id: {'mailboxIds': {outbox['id']: True}}},
            })],
        )
        return {
            'sent': True,
            'at': datetime.now(UTC).isoformat(timespec='seconds'),
            'to': envelope['to'],
            'bcc': [p.email for p in bcc],
            'subject': str(message.get('Subject', '')),
            'message_id': str(message.get('Message-ID', '')),
            'in_reply_to': str(message.get('In-Reply-To', '')),
        }

    async def _upload_blob(self, client: aiohttp.ClientSession, raw: bytes) -> str:
        """Put the composed bytes where `Email/set` can point `blobId` at them.

        The bytes go over as **base64url, unpadded** — RFC 8620 §3.1, and the
        reason it is worth stating is that raw bytes are not JSON at all: a
        server handed `{"blobId": b"From: ..."}` is not decoding a message,
        it is returning a parse error, and the symptom is a send that fails at
        the last step with nothing about the body in the message.
        """
        encoded = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
        results = await self.call(
            client,
            [('Blob/set', {
                'accountId': self._account_id,
                'create': {'b': {'type': 'message/rfc822', 'blobId': encoded}},
                'update': {},
            })],
        )
        created = results[0].get('created', {}) if isinstance(results[0], dict) else {}
        blob_id = created.get('b')
        if isinstance(blob_id, dict):
            blob_id = blob_id.get('blobId')
        if not blob_id:
            raise JmapError(f'{self.account.name}: the server would not store the message body')
        return str(blob_id)

    async def save_draft(self, client: aiohttp.ClientSession, message: EmailMessage) -> str:
        """File a draft, without sending it."""
        boxes = await self.mailboxes(client)
        drafts = next((b for b in boxes if b['role'] == 'drafts'), None)
        if not drafts:
            raise JmapError(f'{self.account.name}: this server has no drafts mailbox')
        blob_id = await self._upload_blob(client, message.as_bytes(policy=SMTP_POLICY))
        results = await self.call(
            client,
            [('Email/set', {
                'accountId': self._account_id,
                'create': {
                    'draft': {
                        'mailboxIds': {drafts['id']: True},
                        'keywords': {'$draft': True},
                        'blobId': blob_id,
                    }
                },
            })],
        )
        created = results[0].get('created', {}) if isinstance(results[0], dict) else {}
        entry = created.get('draft') or {}
        return str(entry.get('id', '')) or drafts['name']


def _envelope(message: EmailMessage, bcc: list[Address]) -> dict[str, list[str]]:
    """Every envelope recipient. Bcc is invisible in the headers and must
    still be here, or it is not delivered to at all."""
    out: list[str] = []
    for header in ('To', 'Cc'):
        for entry in message.get_all(header, []):
            out.extend(p.email for p in parse_addresses(entry) if p.email)
    out.extend(p.email for p in bcc if p.email)
    seen: set[str] = set()
    return {'to': [r for r in out if r and not (r in seen or seen.add(r))]}


class _AttachmentStub(Attachment):
    """Stands in for an attachment JMAP did not name.

    `Email/get` has no attachment list — there is a `hasAttachment` flag and
    nothing else — so a name and a size would have to come from fetching the
    raw blob, which is a full message transfer per message. Reporting that
    there is one, without inventing its name, is the honest version of it.
    """

    def __init__(self, size: int) -> None:
        super().__init__('(attachment)', int(size or 0), 'application/octet-stream')


__all__ = ['Jmap', 'JmapError']

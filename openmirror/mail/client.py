"""One mailbox interface, two protocols behind it.

`box(account)` is the only thing the tool and the routes talk to. It hands
back something with the same five methods whether the account is IMAP or
JMAP, and the branching happens here and nowhere else.

**Why a facade at all**, when IMAP and JMAP could each have their own tool.
Because the *shape of what a person asks for* does not change with the
protocol: "what did I get today", "reply to that", "is there anything from
Acme". A tool per protocol would mean the model picking one, a tool per
capability doubled in the tool list — and the tool list is the single thing
this project has measured to matter most (see the note above `TOOLSETS`) —
and a `mail_read` that behaves differently depending on a config field the
model cannot see.

The one place the protocols genuinely differ is *inside*, and the differences
are handled rather than papered over:

* **IMAP is blocking and JMAP is not.** The IMAP path runs in a thread so a
  five-second SEARCH does not stall the event loop — which matters, because
  the loop is also carrying a websocket with a person watching an agent work
  on it. The JMAP path is `async` all the way down because `aiohttp` already
  is.
* **Sending differs in mechanism**, not in outcome. Over JMAP, submission is
  filing the message in the outbox mailbox; over IMAP it is `SMTP.sendmail`.
  Both produce a `Message-ID` and a confirmed handoff, and both raise
  `MailError` with the server's own words if they do not.
* **Threading is a field on one and a heuristic on the other.** The IMAP
  backfill is in `imap.thread_key`; JMAP's `threadId` is the server's own.
  The interface returns the same `thread` string either way, so a caller
  grouping by it behaves the same, and grouping is better on JMAP.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from openmirror.mail import imap as imap_mod
from openmirror.mail import send as send_mod
from openmirror.mail.accounts import Account, MailError
from openmirror.mail.imap import Mail
from openmirror.mail.jmap import Jmap

log = logging.getLogger(__name__)


class Box:
    """A mailbox, whichever protocol it speaks."""

    def __init__(self, account: Account) -> None:
        self.account = account

    @property
    def protocol(self) -> str:
        return 'jmap' if self.account.use_jmap else 'imap'

    # -- reading ------------------------------------------------------------

    async def folders(self) -> list[dict[str, Any]]:
        if self.protocol == 'jmap':
            return await self._jmap().folders(self._session())
        return await asyncio.to_thread(imap_mod.folders, self.account)

    async def listing(
        self,
        *,
        folder: str = '',
        limit: int = 25,
        unread_only: bool = False,
        since_days: int | None = None,
        search: str = '',
        include_body: bool = False,
    ) -> list[Mail]:
        if self.protocol != 'jmap':
            return await asyncio.to_thread(
                imap_mod.listing,
                self.account,
                folder=folder, limit=limit, unread_only=unread_only,
                since_days=since_days, search=search, include_body=include_body,
            )

        session = self._jmap()
        async with self._session() as client:
            boxes = await session.mailboxes(client)
            mailbox_id = session._mailbox_id(boxes, folder or 'inbox')
            since = ''
            if since_days and since_days > 0:
                # `Email/query` filters on `receivedAt`, which is an ISO
                # timestamp rather than IMAP's `01-Jan` date, so the offset
                # is computed here instead of by the server.
                when = datetime.now(UTC) - timedelta(days=int(since_days))
                since = when.isoformat().replace('+00:00', 'Z')
            ids = await session.query(
                client, mailbox_id=mailbox_id, limit=limit, unread_only=unread_only, search=search, since=since
            )
            if not ids:
                return []
            raw = await session.get(client, ids, full=include_body)
            name = next((b['name'] for b in boxes if b['id'] == mailbox_id), folder or 'INBOX')
            return [session.to_mail(item, folder=name) for item in raw]

    async def read(self, uid: str, *, folder: str = '', mark_seen: bool = False) -> Mail:
        if self.protocol != 'jmap':
            return await asyncio.to_thread(
                imap_mod.read, self.account, uid, folder=folder, mark_seen=mark_seen
            )
        session = self._jmap()
        async with self._session() as client:
            found = await session.get(client, [uid], full=True)
            if not found:
                raise MailError(f'{self.account.name}: no message {uid}')
            return session.to_mail(found[0], folder=folder or 'INBOX')

    async def reply_context(self, uid: str, folder: str = '') -> Mail:
        """The message to reply to, with its quoted history cut off.

        On JMAP the server holds the thread, so there is no quoted history in
        the body to cut — which is one of the quieter benefits of the
        protocol. The trimming still runs, because a message forwarded from
        somewhere else brings its own quotes with it and those are the ones
        that make a reply unreadable.
        """
        original = await self.read(uid, folder=folder)
        if self.protocol == 'jmap' and original.thread:
            original.body = imap_mod._quote_trim(original.body)
            return original
        return await asyncio.to_thread(imap_mod.reply_context, self.account, uid, folder)

    async def flag(self, uid: str, *, seen: bool | None = None, folder: str = 'inbox', keyword: str = '') -> bool:
        if self.protocol != 'jmap':
            return await asyncio.to_thread(
                imap_mod.set_flag, self.account, uid, seen=seen,
                folder=folder or 'INBOX', keyword=keyword,
            )
        session = self._jmap()
        async with self._session() as client:
            patch: dict[str, Any] = {}
            if seen is not None:
                patch['keywords/$seen'] = seen
            if keyword:
                patch[f'keywords/${keyword.lstrip("$")}'] = True
            if not patch:
                return True
            await session.call(
                client, [('Email/set', {'accountId': session._account_id, 'update': {uid: patch}})]
            )
            return True

    # -- writing ------------------------------------------------------------

    async def send(self, message: EmailMessage, bcc: list[Any] | None = None) -> dict[str, Any]:
        """Hand a message to the server.

        JMAP's submission is used when the account has it, because it is one
        authenticated connection and one round trip instead of a second
        protocol with its own login. An account that is JMAP-only — no SMTP
        host — has to go that way; one that has both prefers JMAP and falls
        back to SMTP, because the JMAP path is the one whose auth was already
        proven to work.
        """
        hidden = list(bcc or [])
        if self.protocol == 'jmap':
            session = self._jmap()
            try:
                async with self._session() as client:
                    return await session.submit(client, message, hidden)
            except MailError as exc:
                if not self.account.smtp_host:
                    raise
                # A JMAP account with an SMTP host configured is a person
                # who expects it to work both ways. Saying which leg failed
                # is the difference between a fixable message and a mystery.
                log.warning('JMAP submission on %s failed, falling back to SMTP: %s', self.account.name, exc)
                return await asyncio.to_thread(send_mod.send, self.account, message, hidden)
        return await asyncio.to_thread(send_mod.send, self.account, message, hidden)

    async def draft(self, message: EmailMessage) -> str:
        """File a message as a draft, in the server's own Drafts."""
        if self.protocol == 'jmap':
            session = self._jmap()
            async with self._session() as client:
                return await session.save_draft(client, message)

        def _append() -> str:
            folder = imap_mod.resolve_folder(self.account, 'drafts')
            with imap_mod.Mailbox(self.account) as conn:
                status, _ = conn.select(f'"{folder}"')
                if status != 'OK':
                    raise MailError(
                        f'{self.account.name}: there is no {folder} folder to keep a draft in. '
                        f'Create one, or send the message instead.'
                    )
                # The \Draft flag is what makes a mail client show this in
                # Drafts rather than in the folder as an ordinary message.
                stamp = email.utils.formatdate(localtime=True)
                flags = f'(\\Draft Date "{stamp}")'
                code, _ = conn.append(folder, flags, message.as_bytes(policy=_smtp_policy()))
            if code != 'OK':
                raise MailError(f'{self.account.name}: the server refused the draft ({code})')
            return folder

        return await asyncio.to_thread(_append)

    # -- internals ----------------------------------------------------------

    def _jmap(self) -> Jmap:
        return Jmap(self.account)

    def _session(self) -> Any:
        """An HTTP session for one batch of calls.

        Not cached on the object: a `Jmap` is constructed per operation, so
        caching the session resource here would gain nothing and would risk
        holding a connection open past the end of the call that opened it.
        """
        import aiohttp

        # transport-exempt: not a model server. This reaches the person's own
        # mailbox over the protocol their mail host speaks, and the Tor toggle
        # is a per-connection setting on *model* traffic — routing somebody's
        # correspondence through it would be a decision about where their mail
        # goes, made by a switch meant for prompts. See openmirror/net/tor.py.
        return aiohttp.ClientSession()


def _smtp_policy() -> Any:
    from email.policy import SMTP

    return SMTP


def box(account: Account) -> Box:
    """The mailbox for an account. The one entry point."""
    return Box(account)


__all__ = ['Box', 'box']

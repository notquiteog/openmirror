"""Reading mail, and the parts of a message worth reading.

IMAP through the standard library, because every mailbox this has to talk to
speaks it and the alternative is an OAuth dance per vendor — see the reasoning
in `openmirror.mail.accounts`.

Four decisions that are not obvious:

* **Threading is computed, not trusted.** `In-Reply-To` and `References` are
  how a reply lands in the right conversation, and they are also what most
  mailing lists and half the mail clients in the world get wrong. So the
  thread key here prefers `References`, falls back to `In-Reply-To`, and
  falls back again to a normalised subject when neither is present — which is
  what actually happens with a phone-generated reply half the time. Getting
  this wrong does not lose mail; it makes an inbox unusable, because a
  conversation arrives as thirty unrelated messages.

* **The body prefers `text/plain`.** An HTML-only message is converted with
  the same `html_to_text` the web tools use, so there is one HTML handling
  path in the project rather than two that drift apart. A multipart/alternative
  message is read as its plain part and its HTML part is ignored, which is
  what a reader does.

* **Attachments are named, never loaded.** A list of filenames and sizes comes
  back and the bytes do not. An inbox is the easiest place in the world to
  make a model read a 40 MB PDF by accident, and a tool that fetches
  attachments on demand is one context window away from doing it.

* **Nothing is marked seen.** Listing a folder uses `BODY.PEEK`, so opening
  the interface does not mark a thousand messages read and turn somebody's
  unread count into a lie. Marking read is a separate, explicit call.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from openmirror.agent.tools.web import html_to_text
from openmirror.mail.accounts import Account, Address, MailError, format_addresses, parse_addresses

log = logging.getLogger(__name__)

# Enough of a message for a model to decide whether to read the rest, and
# enough of a listing for a person to scan a week of it.
BODY_LIMIT = 20_000
SNIPPET = 240
MAX_PAGE = 200

# Folders worth offering by name, because these four are universal and the
# alternative is that a model has to ask what the mailbox calls its inbox.
COMMON = {
    'inbox': 'INBOX',
    'sent': 'Sent',
    'drafts': 'Drafts',
    'trash': 'Trash',
    'junk': 'Junk',
    'archive': 'Archive',
}


@dataclass(slots=True)
class Attachment:
    name: str
    size: int
    content_type: str

    def describe(self) -> str:
        size = f'{self.size / 1024:.0f}KB' if self.size < 1024 * 1024 else f'{self.size / 1024 / 1024:.1f}MB'
        return f'{self.name} ({self.content_type}, {size})'


@dataclass(slots=True)
class Mail:
    """One message, in the shape both the model and the interface want."""

    uid: str = ''
    folder: str = 'INBOX'
    message_id: str = ''
    subject: str = ''
    sender: Address = field(default_factory=lambda: Address(''))
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    date: str = ''
    body: str = ''
    snippet: str = ''
    seen: bool = False
    answered: bool = False
    flagged: bool = False
    thread: str = ''
    in_reply_to: str = ''
    references: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    has_html: bool = False

    def header_line(self) -> str:
        """One line per message, for a listing.

        Everything a scan needs and nothing it does not: who, when, what it
        is called, whether it has been answered, and enough of the first line
        to tell two messages with the same subject apart. A listing that
        includes the whole body is a listing nobody reads.
        """
        bits = [
            f'uid {self.uid}',
            self.date[:16],
            f'{self.sender.short} <{self.sender.email}>' if self.sender.email else '(no sender)',
        ]
        if self.to:
            bits.append(f'to {", ".join(p.short for p in self.to[:3])}')
        marks = []
        if not self.seen:
            marks.append('unread')
        if self.answered:
            marks.append('replied')
        if self.flagged:
            marks.append('flagged')
        if self.attachments:
            marks.append(f'{len(self.attachments)} attachment(s)')
        subject = self.subject or '(no subject)'
        return f'{" | ".join(bits)} | {subject}' + (f' | {self.snippet}' if self.snippet else '') + (
            f' [{" ".join(marks)}]' if marks else ''
        )

    def as_text(self) -> str:
        """The whole message, for a model that has decided to read it."""
        lines = [
            f'Subject: {self.subject or "(no subject)"}',
            f'From: {self.sender}',
            f'To: {format_addresses(self.to)}' if self.to else '',
            f'Cc: {format_addresses(self.cc)}' if self.cc else '',
            f'Date: {self.date}',
            f'Folder: {self.folder}   uid: {self.uid}   thread: {self.thread or "(none)"}',
            f'Attachments: {", ".join(a.describe() for a in self.attachments)}' if self.attachments else '',
            '',
            self.body or '(no text body)',
        ]
        if self.has_html and not self.body:
            lines.append('(this message had no plain-text part; the above is the HTML, converted)')
        return '\n'.join(line for line in lines if line != '' or True).strip()

    def public(self) -> dict[str, Any]:
        """For the interface. The same fields, as JSON."""
        return {
            'uid': self.uid,
            'folder': self.folder,
            'message_id': self.message_id,
            'subject': self.subject,
            'from': {'email': self.sender.email, 'name': self.sender.name, 'short': self.sender.short},
            'to': [{'email': p.email, 'name': p.name, 'short': p.short} for p in self.to],
            'cc': [{'email': p.email, 'name': p.name, 'short': p.short} for p in self.cc],
            'date': self.date,
            'body': self.body,
            'snippet': self.snippet,
            'seen': self.seen,
            'answered': self.answered,
            'flagged': self.flagged,
            'thread': self.thread,
            'in_reply_to': self.in_reply_to,
            'references': self.references,
            'attachments': [{'name': a.name, 'size': a.size, 'content_type': a.content_type} for a in self.attachments],
            'has_html': self.has_html,
        }


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def decode_header(raw: Any) -> str:
    """One header value, decoded.

    `email.header.decode_header` returns a list of (bytes, charset) pairs and
    RFC 2047 base64 is the normal case for a subject, so anything that reads
    `msg['Subject']` directly gets `=?utf-8?B?...?=` in a listing. Every real
    implementation of this gets it slightly wrong; the failure mode here is
    that an undecodable chunk is shown as the literal string rather than as a
    replacement character, because `=?utf-8?B?…?=` in a list of subjects is
    unreadable in a way that a slightly mangled accent is not.
    """
    if raw is None:
        return ''
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    parts: list[str] = []
    try:
        chunks = email.header.decode_header(str(raw))
    except (email.errors.HeaderParseError, ValueError):
        return str(raw)
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            for candidate in (charset or 'utf-8', 'utf-8', 'latin-1'):
                try:
                    parts.append(chunk.decode(candidate))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                parts.append(chunk.decode('utf-8', 'replace'))
        else:
            parts.append(chunk)
    return ''.join(parts).strip()


def first_address(raw: Any) -> Address:
    """The first address in a header, or an empty one."""
    found = parse_addresses(raw)
    return found[0] if found else Address('')


def normalise_subject(subject: str) -> str:
    """The thread key for a message with no usable references.

    Re: and Fwd: are stripped, and the rest is casefolded and squeezed, so
    "Re: Re: Budget" and "RE: budget" are one conversation. A subject *with*
    a real `References` header is not matched on this at all — see
    `thread_key`, where it is used only as a last resort.
    """
    text = re.sub(r'^\s*((re|fw|fwd|aw|tr)\s*(\[\d+\])?\s*:\s*)+', '', subject or '', flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return re.sub(r'[^a-z0-9 ]+', '', text)


def thread_key(message: Mail) -> str:
    """What groups messages into a conversation.

    `References` first, then `In-Reply-To`, then the subject. The order is
    the point: the subject is the *last* resort because it is the only one
    that is always present and the only one that is not an identifier.
    """
    for ref in message.references:
        if ref:
            return ref.strip().strip('<>')
    if message.in_reply_to:
        return message.in_reply_to.strip().strip('<>')
    if message.message_id:
        # A reply with no headers at all still has its own id; grouping every
        # such message under one key would be wrong, so it is its own thread
        # unless the subject matches something already seen.
        return f'subject:{normalise_subject(message.subject)}'
    return f'subject:{normalise_subject(message.subject)}'


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


def _walk_parts(message: email.message.Message) -> list[email.message.Message]:
    """Every leaf part, in order, without loading any bytes."""
    if message.is_multipart():
        out: list[email.message.Message] = []
        for part in message.walk():
            if not part.is_multipart():
                out.append(part)
        return out
    return [message]


def _body_of(message: email.message.Message) -> tuple[str, bool]:
    """The readable text of a message, and whether it came from HTML.

    `multipart/alternative` is read as its plain part, because that is the
    part the sender wrote for this purpose. `multipart/mixed` is walked and
    the first readable body wins, so a message with a signature image and a
    real body underneath gives the body.
    """
    plain: list[str] = []
    marked: list[str] = []
    saw_html = False

    for part in _walk_parts(message):
        ctype = part.get_content_type()
        disposition = (part.get('Content-Disposition') or '').lower()
        if 'attachment' in disposition or part.get_filename():
            continue
        if ctype == 'text/plain':
            try:
                plain.append(part.get_content())
            except (LookupError, ValueError, UnicodeDecodeError):
                plain.append(decode_header(part.get_payload(decode=True) or b''))
        elif ctype == 'text/html':
            saw_html = True
            try:
                marked.append(html_to_text(part.get_content()))
            except (LookupError, ValueError, UnicodeDecodeError):
                marked.append(decode_header(part.get_payload(decode=True) or b''))

    if plain:
        return '\n'.join(plain).strip(), saw_html
    if marked:
        return '\n'.join(marked).strip(), True
    return '', saw_html


def _quote_trim(body: str) -> str:
    """Cut a reply's quoted history off the end.

    A reply that keeps the whole thread is unreadable, and every mail client
    has solved this for twenty years. The line that starts the quote varies —
    `On <date>, <someone> wrote:`, `-----Original Message-----`, a line of `>`,
    a "From: … Sent: …" Outlook block — so all four are tried, longest first,
    and the earliest match wins.
    """
    markers = (
        re.compile(r'^-{2,}\s*Original Message\s*-{2,}', re.I | re.M),
        re.compile(r'^On .{4,80}\bwrote:\s*$', re.I | re.M),
        re.compile(r'^From:\s.+$', re.M),
        re.compile(r'^>+ ?.*$', re.M),
    )
    cut = len(body)
    for marker in markers:
        found = marker.search(body)
        if found and found.start() < cut:
            cut = found.start()
    return body[:cut].rstrip()


def _first_lines(body: str, count: int = 3) -> str:
    """The first few non-quoted, non-empty lines — a listing's snippet.

    Quoted lines are skipped rather than kept: a snippet that begins with
    `> On Tuesday, Alice wrote:` tells you nothing about the message it is in.
    """
    out: list[str] = []
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith('>'):
            continue
        out.append(text)
        if len(out) >= count:
            break
    return ' '.join(out)[:SNIPPET]


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------


def _ssl_context() -> ssl.SSLContext:
    """A verifying TLS context.

    `ssl.create_default_context` and nothing else: certificate verification
    on, hostname checking on, no way to turn either off from a config file.
    A mail client that can be talked into an unverified connection is a mail
    client that can be talked into reading somebody else's inbox.
    """
    return ssl.create_default_context()


class Mailbox:
    """One IMAP session. Not reusable across calls — open, do, close.

    A context manager rather than a long-lived object on purpose: IMAP
    connections have server-side idle timeouts that differ per host, and a
    cached connection that has been dropped produces an error on a call that
    has nothing to do with connecting. Opening costs one TLS handshake.
    """

    def __init__(self, account: Account, *, timeout: int = 30) -> None:
        self.account = account
        self.timeout = timeout
        self._conn: imaplib.IMAP4 | None = None

    def __enter__(self) -> imaplib.IMAP4:
        account = self.account
        if not account.imap_host:
            raise MailError(f'{account.name}: no IMAP host')
        try:
            if account.imap_mode == 'ssl':
                conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                    account.imap_host, account.imap_port, ssl_context=_ssl_context(), timeout=self.timeout
                )
            else:
                conn = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=self.timeout)
                conn.starttls(ssl_context=_ssl_context())
        except (OSError, ssl.SSLError) as exc:
            raise MailError(
                f'{account.name}: could not reach IMAP at {account.imap_host}:{account.imap_port} ({exc})'
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise MailError(f'{account.name}: {exc}') from exc

        try:
            # `login` rather than `authenticate`: it is the one path every
            # server implements, and an app password is a plain password to
            # the server. LOGIN over the TLS connection above is encrypted.
            conn.login(account.address, account.secret())
        except imaplib.IMAP4.error as exc:
            # The most common failure by a wide margin, and worth naming
            # precisely: the account password will not work on either of
            # these hosts, and the thing that does is called an app password.
            hint = ''
            if any(word in str(exc).upper() for word in ('AUTHENTICATIONFAILED', 'AUTHENTICATE', 'LOGIN')):
                hint = (
                    f' {account.address} was refused. Gmail and Microsoft both want an app password here, '
                    f'not the account password — see the README for how to make one.'
                )
            try:
                conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            raise MailError(f'{account.name}: could not sign in.{hint} ({exc})') from exc
        self._conn = conn
        return conn

    def __exit__(self, *_exc: object) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except (imaplib.IMAP4.error, OSError):
                pass
            try:
                self._conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
            self._conn = None


def _check(ok: Any, what: str, account: Account) -> None:
    """Turn IMAP's `(NO, ...)` into a sentence.

    `imaplib` returns a status rather than raising, which is why every call
    site in the stdlib's own examples checks the first element. Forgetting to
    is how a folder that does not exist becomes an empty listing that reads
    like an empty mailbox.
    """
    if isinstance(ok, tuple) and ok and ok[0] != 'OK':
        detail = ' '.join(str(part) for part in ok[1:]).strip()
        raise MailError(f'{account.name}: {what} was refused — {detail or "the server said no"}')


def folders(account: Account) -> list[dict[str, Any]]:
    """Every folder, with its unread and total counts.

    The counts are the reason this is not just a list of names: "23 unread in
    Sub-contracts/Acme" is the answer to "what needs me", and getting it means
    one STATUS call per folder rather than a search of each.
    """
    out: list[dict[str, Any]] = []
    with Mailbox(account) as conn:
        status, boxes = conn.list()
        _check(status, 'listing folders', account)
        if not boxes:
            return out
        for line in boxes:
            if not line:
                continue
            # `(\HasNoChildren) "/" "INBOX"` — flags, delimiter, name. The
            # name is the last quoted token, and a mailbox with a space in
            # it is quoted, so this splits on the closing quote rather than
            # on whitespace.
            text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else line
            match = re.search(r'"/" "(.*)"$', text) or re.search(r'"/" (.*)$', text)
            if not match:
                continue
            name = match.group(1).replace('\\"', '"')
            entry: dict[str, Any] = {'name': name, 'unread': None, 'total': None}
            try:
                _flag, data = conn.status(f'"{name}"', '(MESSAGES UNSEEN)')
                if _flag == 'OK' and data and data[0]:
                    numbers = dict(
                        part.split()[0:2] for part in data[0].decode().split() if len(part.split()) >= 2
                    )
                    entry['total'] = int(numbers.get('MESSAGES', 0))
                    entry['unread'] = int(numbers.get('UNSEEN', 0))
            except (imaplib.IMAP4.error, ValueError, IndexError):
                # A folder that will not report a count is still a folder.
                pass
            out.append(entry)
    return sorted(out, key=lambda f: (f['name'].upper() != 'INBOX', f['name'].lower()))


def resolve_folder(account: Account, wanted: str = '') -> str:
    """Turn `inbox`, a real name, or nothing into a real folder name.

    The aliases come first because a model asked for "the inbox" should not
    have to know that this server calls it `INBOX`, and because the words in
    `COMMON` are the ones people and models actually use.
    """
    if not wanted:
        return 'INBOX'
    key = wanted.strip().lower()
    if key in COMMON:
        return COMMON[key]
    available = {f['name'].lower(): f['name'] for f in folders(account)}
    if key in available:
        return available[key]
    # Prefix match, because "Sent Items" and "Sent Messages" are both common
    # and one of them is what this server has.
    hits = [name for lower, name in available.items() if lower.startswith(key)]
    if len(hits) == 1:
        return hits[0]
    if not available:
        return wanted
    raise MailError(
        f'no folder called {wanted!r}. There is: {", ".join(sorted(available.values())[:20])}'
    )


def to_mail(message: email.message.Message, *, uid: str = '', folder: str = 'INBOX') -> Mail:
    """Turn a parsed message into the shape this project passes around."""
    sender = first_address(message.get('From'))
    mail = Mail(
        uid=uid,
        folder=folder,
        message_id=decode_header(message.get('Message-ID')),
        subject=decode_header(message.get('Subject')),
        sender=sender,
        to=parse_addresses(message.get('To')),
        cc=parse_addresses(message.get('Cc')),
        date=decode_header(message.get('Date')),
        in_reply_to=decode_header(message.get('In-Reply-To')),
        references=[r for r in decode_header(message.get('References')).split() if r],
    )
    body, saw_html = _body_of(message)
    mail.has_html = saw_html
    mail.body = body[:BODY_LIMIT]
    mail.snippet = _first_lines(body)
    for part in _walk_parts(message):
        name = part.get_filename()
        if not name:
            continue
        try:
            size = len(part.get_payload(decode=True) or b'')
        except (LookupError, ValueError):
            size = 0
        mail.attachments.append(Attachment(decode_header(name), size, part.get_content_type()))
    mail.thread = thread_key(mail)
    return mail


def _search(conn: imaplib.IMAP4, account: Account, criteria: str) -> list[str]:
    status, data = conn.uid('SEARCH', None, criteria)
    _check(status, 'searching', account)
    if not data or not data[0]:
        return []
    return data[0].split()


def _split_fetch(data: list[Any]) -> list[tuple[str, str, email.message.Message]]:
    """Walk a FETCH response into (uid, flags, message) triples.

    `imaplib` returns a flat list in which a `UID <n>` byte line always
    immediately precedes the tuple carrying that message's bytes and flags.
    Walking it in order is the only way to know which message a UID belongs
    to: the sequence numbers and the UIDs are both there, they are not the
    same, and after a deletion they disagree. Guessing is how a reply lands
    on the wrong message.
    """
    out: list[tuple[str, str, email.message.Message]] = []
    uid = ''
    for item in data or []:
        if isinstance(item, (bytes, bytearray)):
            text = bytes(item)
            if text.upper().startswith(b'UID '):
                parts = text.split()
                if len(parts) >= 2:
                    uid = parts[1].decode()
            continue
        if isinstance(item, (list, tuple)) and item:
            flags = ''
            payload: bytes | None = None
            for chunk in item:
                if isinstance(chunk, (bytes, bytearray)) and chunk.startswith(b'('):
                    flags = bytes(chunk).decode('utf-8', 'replace')
                elif isinstance(chunk, (bytes, bytearray)) and chunk.startswith(b'UID '):
                    uid = bytes(chunk).split()[1].decode()
                elif isinstance(chunk, (bytes, bytearray)):
                    payload = bytes(chunk)
            if payload is not None:
                out.append((uid, flags, email.message_from_bytes(payload, policy=email.policy.default)))
    return out


def _flags_of(flags: str) -> tuple[bool, bool, bool]:
    """`(\\Seen \\Answered)` into (seen, answered, flagged)."""
    return ('\\Seen' in flags, '\\Answered' in flags, '\\Flagged' in flags)


def listing(
    account: Account,
    *,
    folder: str = '',
    limit: int = 25,
    unread_only: bool = False,
    since_days: int | None = None,
    search: str = '',
    include_body: bool = False,
) -> list[Mail]:
    """A folder, newest first.

    Every fetch uses `BODY.PEEK` rather than `BODY`, so listing a folder does
    not mark it read. That is the single most important detail in this
    function: an interface that opens an inbox must not silently clear the
    unread badge and turn the count into a lie.
    """
    name = resolve_folder(account, folder)
    limit = max(1, min(int(limit or 25), MAX_PAGE))
    criteria = 'UNSEEN' if unread_only else 'ALL'
    if since_days and since_days > 0:
        # IMAP's SINCE takes a date, not an offset, so this is computed here.
        # A `search` replaces the criteria entirely: an explicit query is what
        # was asked for, and ANDing a text search onto UNSEEN would quietly
        # change its meaning.
        if not search:
            when = (datetime.now(UTC) - timedelta(days=int(since_days))).strftime('%d-%b-%Y')
            criteria += f' SINCE {when}'

    with Mailbox(account) as conn:
        _check(conn.select(f'"{name}"', readonly=True), f'opening {name}', account)

        if search:
            uids: list[bytes] = []
            for query in (
                f'HEADER SUBJECT "{search}"',
                f'OR SUBJECT "{search}" FROM "{search}"',
                f'TEXT "{search}"',
            ):
                uids = _search(conn, account, query)
                if uids:
                    break
        else:
            status, data = conn.uid('SEARCH', None, criteria)
            _check(status, f'searching {name}', account)
            uids = data[0].split() if data and data[0] else []

        # Newest first, and capped after the sort. Fetching everything to
        # throw most of it away is the other way round.
        uids = list(reversed(uids))[:limit]
        if not uids:
            return []

        fields = '' if include_body else (
            'HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)'
        )
        want = f'(UID FLAGS BODY.PEEK[{fields}])'
        status, data = conn.uid('FETCH', b','.join(uids), want)
        _check(status, f'reading {name}', account)

    out: list[Mail] = []
    for uid, flags, parsed in _split_fetch(data or []):
        mail = to_mail(parsed, uid=uid, folder=name)
        mail.seen, mail.answered, mail.flagged = _flags_of(flags)
        out.append(mail)
    return out


def read(account: Account, uid: str, *, folder: str = '', mark_seen: bool = False) -> Mail:
    """One message in full, by UID.

    `mark_seen` is off by default and a parameter rather than a mode: reading
    a message to answer it and reading it because it is interesting are
    different acts, and only one of them means "I have dealt with this".
    """
    name = resolve_folder(account, folder)
    with Mailbox(account) as conn:
        _check(conn.select(f'"{name}"', readonly=not mark_seen), f'opening {name}', account)
        status, data = conn.uid('FETCH', uid, '(UID FLAGS BODY.PEEK[])')
        _check(status, f'reading message {uid}', account)

    for found_uid, flags, parsed in _split_fetch(data or []):
        if not parsed.keys():
            continue
        mail = to_mail(parsed, uid=found_uid or uid, folder=name)
        mail.seen, mail.answered, mail.flagged = _flags_of(flags)
        if mark_seen:
            mail.seen = True
        return mail
    raise MailError(f'{account.name}: no message {uid} in {name}')


def reply_context(account: Account, uid: str, folder: str = '') -> Mail:
    """The message to reply to, with its quoted history cut off.

    Sending a reply to a message that already contains the whole conversation
    is the single most common way generated mail reads as obviously generated,
    and it is entirely avoidable: everything below the quote marker goes.
    """
    original = read(account, uid, folder=folder)
    original.body = _quote_trim(original.body)
    return original


def set_flag(account: Account, uid: str, *, seen: bool | None = None, folder: str = 'INBOX', keyword: str = '') -> bool:
    """Change one flag on one message.

    `\\Deleted` plus `EXPUNGE` is the only delete IMAP has, and it is not
    offered here: expunging renumbers every message after it, so a UI holding
    UIDs is fine but anything holding sequence numbers is silently wrong.
    Moving to Trash is two calls and reversible, which is the operation people
    mean by "delete" in a mail client.
    """
    name = resolve_folder(account, folder)
    with Mailbox(account) as conn:
        _check(conn.select(f'"{name}"'), f'opening {name}', account)
        if keyword:
            target = keyword if keyword.startswith('\\') else f'\\{keyword}'
            command = 'UNFLAG' if target.lower() in ('\\seen',) else 'FLAG'
            if seen is not None and target.lower() == '\\seen':
                command = 'FLAG' if seen else 'UNFLAG'
            _check(conn.uid('STORE', uid, f'+FLAGS ({target})' if command == 'FLAG' else f'-FLAGS ({target})'), 'flagging', account)
            return True
        if seen is None:
            return True
        command = '+FLAGS (\\Seen)' if seen else '-FLAGS (\\Seen)'
        _check(conn.uid('STORE', uid, command), 'flagging', account)
        return True


__all__ = ['COMMON', 'Attachment', 'Mail', 'Mailbox', 'decode_header', 'folders', 'first_address',
           'listing', 'normalise_subject', 'read', 'reply_context', 'resolve_folder', 'set_flag',
           'thread_key', 'to_mail']

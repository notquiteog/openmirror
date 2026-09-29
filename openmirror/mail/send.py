"""Sending mail, and the headers that make it land in the right place.

SMTP through the standard library, same reasoning as the reading side.

**Threading is the part that actually matters.** A reply sent without
`In-Reply-To` and `References` arrives as a brand-new conversation: not in
the thread the person is looking at, not quoted by their client, and — in
most mail clients — not marked as a response to anything. So `reply` takes
the original's identifiers and sets both, and a bare `send` that is given a
`References` value uses it. The chain is built by appending to the original's
existing `References` rather than replacing it, because a reply to a reply
that drops the grandparent is how a five-message conversation turns into three
unrelated pairs.

**The body is always both plain and HTML.** A plain-text-only message is a
message that renders as an unstyled wall in every HTML mail client, and an
HTML-only one is a message that arrives as source code to half the people who
read it. Both parts, generated from one string, and the plain one is the
source of truth so the text somebody searches for is the text that is there.

**Nothing is sent here that could not have been sent by hand.** There is no
queue, no retry, no outbox, and no background thread: this function either
hands the message to a server that accepted it or raises. That is a
deliberate limit rather than an unfinished feature. A mail queue that retries
is a mail queue that can send the same message twice, and a message sent
twice to a real person is worse than a message that was not sent.
"""

from __future__ import annotations

import email.utils
import logging
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from openmirror.mail.accounts import Account, Address, MailError, format_addresses, parse_addresses
from openmirror.mail.imap import Mail

log = logging.getLogger(__name__)

# A refusal from SMTP is worth more detail than this in most cases, and less
# in a few, so the cap exists to stop a server that returns a stack trace
# becoming the whole tool result.
REFUSAL_LIMIT = 400


def _html_from_text(text: str, *, subject: str = '') -> str:
    """A plain-text body wrapped as the HTML part of the same message.

    Quoted-reply aware, which is the only thing this does that a `<pre>` does
    not: a paragraph where every line starts with `>` is rendered as a
    blockquote rather than as literal angle brackets, so a reply written in
    plain text looks like a reply in a mail client instead of like a diff.
    """
    from html import escape

    blocks: list[str] = []
    for block in text.split('\n\n'):
        if not block.strip():
            continue
        lines = block.splitlines()
        if all(line.lstrip().startswith('>') for line in lines if line.strip()):
            inner = ''.join(f'<div>{escape(line.lstrip()[1:].lstrip())}</div>' for line in lines)
            blocks.append(f'<blockquote>{inner}</blockquote>')
        else:
            blocks.append('<p>' + '<br>'.join(escape(line) for line in lines) + '</p>')
    heading = f'<h2>{escape(subject)}</h2>' if subject else ''
    style = (
        '<style>body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#111}'
        'blockquote{border-left:3px solid #ddd;margin:0;padding-left:12px;color:#555}</style>'
    )
    return f'<!doctype html><html><head><meta charset="utf-8">{style}</head><body>{heading}{"".join(blocks)}</body></html>'


def build(
    account: Account,
    *,
    to: Any,
    subject: str,
    body: str,
    cc: Any = None,
    bcc: Any = None,
    reply_to_message: Mail | None = None,
    in_reply_to: str = '',
    references: list[str] | None = None,
    html: str = '',
) -> tuple[EmailMessage, list[Address]]:
    """Assemble a message, and the Bcc recipients. Sending it is a separate step.

    Every header a mail client uses to decide what this message *is* is set
    from the message it is a reply to: `In-Reply-To` is the immediate parent
    and `References` is the whole chain, because a client that only reads
    `In-Reply-To` shows the parent in the thread and one that reads
    `References` shows all of it. Getting only one right gives a reply that
    is in the thread and has no history in it.
    """
    message = EmailMessage()
    message['From'] = format_addresses([Address(account.address, account.label)])
    message['To'] = format_addresses(parse_addresses(to))
    if cc:
        message['Cc'] = format_addresses(parse_addresses(cc))

    # Folded to one line before it becomes a header: a subject containing a
    # newline would otherwise either add a header or make `EmailMessage`
    # raise, and neither failure is visible in the tool result.
    subject = ' '.join((subject or '').split()) or '(no subject)'
    message['Subject'] = subject
    message['Date'] = email.utils.formatdate(localtime=True)
    message['Message-ID'] = email.utils.make_msgid(domain=account.address.split('@')[-1] or None)

    chain: list[str] = []
    if reply_to_message is not None:
        if reply_to_message.message_id:
            message['In-Reply-To'] = reply_to_message.message_id
            chain = [reply_to_message.message_id]
        # The original's own chain first, then the original. Dropping the
        # ancestors is what turns a long conversation into pairs.
        chain = [r for r in reply_to_message.references if r] + chain
    if in_reply_to and 'In-Reply-To' not in message:
        message['In-Reply-To'] = in_reply_to.strip().strip('<>')
    if references:
        chain = [r for r in references if r] + chain
    # De-duplicated, order kept, and capped: a genuinely enormous chain is
    # rejected by some servers, and References is advisory anyway.
    seen: set[str] = set()
    unique = [r for r in chain if r and not (r in seen or seen.add(r))]
    if unique:
        message['References'] = ' '.join(unique[-20:])

    # Both parts from one source of truth, so the text somebody searches for
    # and the text they read cannot disagree.
    text = body or ''
    message.set_content(text, subtype='plain', charset='utf-8')
    message.add_alternative(html or _html_from_text(text, subject=subject), subtype='html', charset='utf-8')

    hidden = parse_addresses(bcc)
    if account.bcc_self:
        hidden = [*hidden, Address(account.address)]
    return message, hidden


def recipients(message: EmailMessage, hidden: list[Address]) -> list[str]:
    """Every envelope recipient, including the ones not in the headers.

    Bcc is invisible in the message and *must* still be in the envelope, or it
    is not delivered at all. This is the step that is easy to leave out and
    produces the specific bug where a bcc silently goes nowhere.
    """
    out: list[str] = []
    for header in ('To', 'Cc'):
        for address in message.get_all(header, []):
            out.extend(p.email for p in parse_addresses(address) if p.email)
    out.extend(p.email for p in hidden if p.email)
    # De-duplicated, and never empty — a message with no envelope recipient
    # is refused by every server and the reason is not always clear.
    seen: set[str] = set()
    unique = [r for r in out if r and not (r in seen or seen.add(r))]
    if not unique:
        raise MailError('this message has no recipient')
    return unique


def send(account: Account, message: EmailMessage, hidden: list[Address] | None = None) -> dict[str, Any]:
    """Hand a message to a server, and report what happened.

    No queue and no retry, on purpose — see the module docstring. The result
    says what was refused and by whom, because "SMTP 535 authentication
    failed" from an app-password problem is fixable in thirty seconds and
    "the email was not sent" is not.
    """
    if not account.smtp_host:
        raise MailError(f'{account.name}: no SMTP host')

    envelope = recipients(message, hidden or [])
    context = ssl.create_default_context()
    try:
        if account.smtp_mode == 'ssl':
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                account.smtp_host, account.smtp_port, context=context, timeout=30
            )
        else:
            smtp = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
            smtp.ehlo()
            smtp.starttls(context=context)
    except (OSError, ssl.SSLError) as exc:
        raise MailError(f'{account.name}: could not reach SMTP at {account.smtp_host}:{account.smtp_port} ({exc})') from exc

    try:
        smtp.ehlo()
        try:
            smtp.login(account.address, account.secret())
        except smtplib.SMTPAuthenticationError as exc:
            hint = ''
            if exc.smtp_code in (535, 534, 530):
                hint = (
                    f' {account.address} was refused. Gmail and Microsoft both want an app password '
                    f'here, not the account password — see the README for how to make one.'
                )
            raise MailError(f'{account.name}: the server would not sign in.{hint} ({_brief(exc)})') from exc
        except smtplib.SMTPException as exc:
            raise MailError(f'{account.name}: {_brief(exc)}') from exc

        refused = smtp.send_message(message, from_addr=account.address, to_addrs=envelope)
    except smtplib.SMTPRecipientsRefused as exc:
        # Every address refused, each with its own reason — the useful case,
        # and a single "no recipients accepted" would throw away why.
        detail = '; '.join(f'{addr}: {why.decode("utf-8", "replace")}' for addr, why in exc.recipients.items())
        raise MailError(f'{account.name}: no recipient accepted it — {detail[:REFUSAL_LIMIT]}') from exc
    except smtplib.SMTPResponseException as exc:
        detail = exc.smtp_error.decode('utf-8', 'replace') if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        raise MailError(f'{account.name}: the server refused it — {detail[:REFUSAL_LIMIT]}') from exc
    except smtplib.SMTPException as exc:
        raise MailError(f'{account.name}: {_brief(exc)}') from exc
    finally:
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            # The message is already with the server or already refused; a
            # failure to close politely does not change which.
            pass

    return {
        'sent': True,
        'at': datetime.now(UTC).isoformat(timespec='seconds'),
        'to': [p for p in recipients(message, [])],
        'bcc': [p.email for p in (hidden or [])],
        'refused': {addr: str(why) for addr, why in (refused or {}).items()},
        'subject': str(message.get('Subject', '')),
        'message_id': str(message.get('Message-ID', '')),
        'in_reply_to': str(message.get('In-Reply-To', '')),
    }


def _brief(exc: BaseException) -> str:
    """An SMTP exception as a sentence, without the traceback's other half."""
    text = str(exc).strip() or type(exc).__name__
    return text[:REFUSAL_LIMIT]


def queue_locally(account: Account, message: EmailMessage) -> str:
    """File a message as a draft, through whichever protocol this account uses.

    This is how "write it but do not send it" is honoured. There is no local
    draft file: a draft the person cannot see in their own mail client is not
    a draft, and writing one to a directory is how a half-finished reply ends
    up sent twice or never.
    """
    from openmirror.mail.client import box

    return box(account).draft(message)


__all__ = ['build', 'queue_locally', 'recipients', 'send']

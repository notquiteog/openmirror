"""Mail, from a browser.

The same five operations the `mail` tool offers, over HTTP, for a person who
wants to read their inbox rather than ask about it. Same store, same backends,
same grades — the routes are a different door onto one implementation, not a
second one.

What is deliberately different is the sending path, and it is the same
distinction `routers/git.py` draws for a commit. A draft is written and
returned; a send takes a message a person has already seen. There is no
`"ai": true` that drafts and sends in one request, and no endpoint that takes
a prompt and produces a sent email, because a flag like that gets set once and
then nobody reads the messages again.

**Credentials never come back out.** `GET /api/mail/accounts` returns
`has_password` and `password_from_env` and nothing else. A store endpoint
takes a password and writes it 0600, and takes one that is empty to mean
"leave what is there", so a round trip through the interface can never blank
a working account.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from openmirror.config import config
from openmirror.mail import send as send_mod
from openmirror.mail.accounts import KNOWN, Account, AccountStore, MailError
from openmirror.mail.client import box
from openmirror.providers.base import NoProviderError

log = logging.getLogger(__name__)

router = APIRouter(prefix='/api/mail')

MAX_BODY = 200_000


def store() -> AccountStore:
    return AccountStore(Path(config.mail_accounts))


def _mailbox(account_id: str = ''):
    """The mailbox to work on, with a failure that is a 400 rather than a 500."""
    try:
        account = store().resolve(account_id)
    except MailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    problems = account.validate()
    if problems:
        raise HTTPException(status_code=400, detail=f'{account.name} is not usable: {"; ".join(problems)}')
    return box(account)


class AccountBody(BaseModel):
    id: str = ''
    address: str
    provider: str = Field(default='', description='gmail, outlook or currere — fills in the hosts')
    label: str = ''
    protocol: str = Field(default='', description='jmap, imap, or empty for whichever is configured')
    imap_host: str = ''
    imap_port: int = 993
    smtp_host: str = ''
    smtp_port: int = 465
    jmap_url: str = ''
    jmap_username: str = ''
    jmap_token: str = ''
    password: str = Field(default='', description='Empty means leave whatever is already stored.')
    password_env: str = ''
    bcc_self: bool = False
    default: bool = False


class SendBody(BaseModel):
    account: str = ''
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    reply_to: str = Field(default='', description='uid of the message being answered, for threading')
    folder: str = ''
    reply_all: bool = False


@router.get('/accounts')
async def list_accounts() -> dict[str, Any]:
    found = store().load()
    return {
        'accounts': [a.public() for a in found],
        'providers': {name: {k: v for k, v in spec.items() if k != 'label'} for name, spec in KNOWN.items()},
    }


@router.post('/accounts')
async def save_account(body: AccountBody) -> dict[str, Any]:
    """Add or change an account, and prove it works.

    The connection is *tested* before it is stored rather than after, and a
    failure does not save. An account that cannot sign in is worse than no
    account: it looks configured, so the tool is offered, and it fails on
    first use with a message about a mailbox rather than about a setting.
    Reporting the failure at the point where the person is looking at the
    form is the only place they can act on it.
    """
    account_id = body.id or body.provider or body.address.split('@')[0].split('.')[0]
    existing = store().get(account_id)
    account = Account(
        id=account_id,
        address=body.address,
        label=body.label,
        protocol=body.protocol,
        imap_host=body.imap_host or (existing.imap_host if existing else ''),
        imap_port=body.imap_port or (existing.imap_port if existing else 993),
        smtp_host=body.smtp_host or (existing.smtp_host if existing else ''),
        smtp_port=body.smtp_port or (existing.smtp_port if existing else 465),
        jmap_url=body.jmap_url or (existing.jmap_url if existing else ''),
        jmap_username=body.jmap_username or (existing.jmap_username if existing else ''),
        jmap_token=body.jmap_token or (existing.jmap_token if existing else ''),
        # An empty password means "keep the one that is there". Without this,
        # every round trip through the form — including one that only changed
        # the label — would blank a working account.
        password=body.password or (existing.password if existing else ''),
        password_env=body.password_env or (existing.password_env if existing else ''),
        bcc_self=body.bcc_self,
        default=body.default or (existing.default if existing else False),
    )
    if body.provider:
        for name, value in KNOWN.get(body.provider, {}).items():
            if name != 'label' and not getattr(account, name, None):
                setattr(account, name, value)
        if not account.label:
            account.label = KNOWN.get(body.provider, {}).get('label', '')

    problems = account.validate()
    if problems:
        raise HTTPException(status_code=400, detail='; '.join(problems))

    mailbox = box(account)
    try:
        found = await mailbox.folders()
    except MailError as exc:
        # Not saved. See the docstring: an account that cannot sign in is
        # worse than no account.
        raise HTTPException(status_code=400, detail=f'{account.name}: {exc}') from exc

    store().put(account)
    return {
        'ok': True,
        'account': account.public(),
        'folders': len(found),
        'protocol': mailbox.protocol,
    }


@router.delete('/accounts/{account_id}')
async def delete_account(account_id: str) -> dict[str, bool]:
    return {'ok': store().remove(account_id)}


@router.get('/folders')
async def list_folders(account: str = '') -> dict[str, Any]:
    found = await (_mailbox(account).folders())
    return {'folders': found}


@router.get('/messages')
async def list_messages(
    account: str = '',
    folder: str = '',
    limit: int = 25,
    unread_only: bool = False,
    since_days: int | None = None,
    query: str = '',
) -> dict[str, Any]:
    mailbox = _mailbox(account)
    found = await (
        mailbox.listing(
            folder=folder, limit=limit, unread_only=unread_only, since_days=since_days, search=query
        )
    )
    return {
        'folder': found[0].folder if found else folder,
        'messages': [m.public() for m in found],
    }


@router.get('/message')
async def read_message(uid: str, account: str = '', folder: str = '') -> dict[str, Any]:
    if not uid:
        raise HTTPException(status_code=400, detail='uid is required')
    found = await (_mailbox(account).read(uid, folder=folder, mark_seen=True))
    return {'message': found.public()}


class ReadRequest(BaseModel):
    uid: str
    seen: bool = True
    account: str = ''
    folder: str = 'inbox'


@router.post('/read')
async def set_read(body: ReadRequest) -> dict[str, bool]:
    if not body.uid:
        raise HTTPException(status_code=400, detail='uid is required')
    return {'ok': await (_mailbox(body.account).flag(body.uid, seen=body.seen, folder=body.folder))}


class ReplyContextRequest(BaseModel):
    uid: str
    account: str = ''
    folder: str = ''


@router.post('/reply-context')
async def reply_context(body: ReplyContextRequest) -> dict[str, Any]:
    """The message to answer, with the quoted thread cut off.

    The interface uses this to fill in the recipient and the `Re:` subject,
    so the person does not have to retype the thing the server already knows.
    """
    if not body.uid:
        raise HTTPException(status_code=400, detail='uid is required')
    found = await (_mailbox(body.account).reply_context(body.uid, folder=body.folder))
    return {'message': found.public(), 'thread': found.thread}


class ProposeBody(BaseModel):
    account: str = ''
    # The message being answered. There is no way to call this without one:
    # a draft of *what* would otherwise be somebody's private correspondence
    # sent to a model provider, which is not a thing this should be able to
    # do by accident.
    reply_to: str
    folder: str = ''


@router.post('/propose')
async def propose_reply(body: ProposeBody) -> dict[str, Any]:
    """A first draft of a reply, in the person's voice. Sends nothing.

    The counterpart to `POST /api/git/propose`, and the same rule: the model
    writes, and a human commits. This returns a string the interface puts in
    a composer that was already open and already addressed; turning that into
    a sent message needs `/api/mail/send` with a message somebody read.

    Only the message being answered is sent to the provider — not the
    account's whole history, not other threads, not anything else in the
    mailbox. "Write this reply" needs the reply's own context and nothing
    more, and the difference is the difference between a provider seeing one
    message and seeing an inbox.
    """
    if not body.reply_to:
        raise HTTPException(status_code=400, detail='reply_to is required: there is no message to answer')
    if not config.allow_messages:
        raise HTTPException(
            status_code=403, detail='sending mail is switched off on this install (OPENMIRROR_ALLOW_MESSAGES)'
        )

    try:
        original = await (_mailbox(body.account).read(body.reply_to, folder=body.folder))
    except MailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        draft = await _draft_reply(original)
    except NoProviderError as exc:
        # Named, and not turned into a generic failure: the caller's correct
        # response is to fix a key or switch a feature off, and those are
        # different actions.
        raise HTTPException(status_code=503, detail=f'no model can write this: {exc}') from exc
    if not draft:
        raise HTTPException(status_code=502, detail='the model returned an empty draft')

    return {
        'draft': draft,
        'to': f'{original.sender.name} <{original.sender.email}>' if original.sender.email else '',
        'subject': original.subject if original.subject.lower().startswith('re:') else f'Re: {original.subject}',
        'thread': original.thread,
    }


def _quote_trim(text: str) -> str:
    """The message's own text, without the history it is quoting.

    Same helper the reply path uses, for the same reason: a model shown thirty
    lines of quoted thread writes a reply that responds to the wrong thing.
    """
    from openmirror.mail.imap import _quote_trim as trim

    return trim(text)


async def _draft_reply(original: Any) -> str:
    """One model call, and its answer cleaned up into a body."""
    from openmirror.providers.base import ChatRequest, Message, TextBlock, one_shot
    from openmirror.routers.agent import resolve_chat

    # The same resolution a session goes through: a route with no model on it
    # is not a model name, and an empty one is refused by the server.
    provider, model, _info = await resolve_chat()
    shown = _quote_trim(original.body) or original.snippet
    prompt = (
        f'From: {original.sender}\nSubject: {original.subject}\nDate: {original.date}\n\n{shown}'
    )
    request = ChatRequest(
        model=model,
        messages=[Message(role='user', content=[TextBlock(text=f'Write the reply.\n\n{prompt}')])],
        system=DRAFT_SYSTEM,
        temperature=0.4,
        # No `max_tokens`: see `one_shot`. A reasoning model spends the whole
        # budget thinking and returns no text, and a reply draft that comes
        # back empty is a feature that looks broken.
    )
    return _clean_draft(await one_shot(provider, request, what='reply'))


def _clean_draft(raw: str) -> str:
    """Strip what a model puts around a draft.

    Four shapes, all of them observed: a fenced block, a "Here's a draft:"
    line, a leading "Subject:" it invented, and "I've written the reply below".
    A draft that cannot be sent verbatim is the worst outcome for a button
    whose only job is to save somebody typing.
    """
    import re

    text = (raw or '').strip()
    if not text:
        return ''
    fence = re.search(r'```[a-z]*\n(.*?)(?:\n)?```', text, re.S)
    if fence:
        text = fence.group(1).strip()

    # A greeting the composer already fills in, dropped so it is not sent
    # twice.
    lines = text.splitlines()
    while lines and re.match(r'^\s*(here(\'s| is)\b|subject\s*:|draft\s*:|reply\s*:|i\'?ve written|below is)', lines[0], re.I):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    text = '\n'.join(lines).strip()
    return re.sub(r'\n{3,}', '\n\n', text)[:MAX_BODY]


DRAFT_SYSTEM = """You are helping somebody write an email in their own voice.

You are given one message: the one they are replying to. Write the reply they would write.

- Write as them, to the person who wrote it. First person, their tone.
- Match how they write. Their messages are the only evidence you have of
  their greeting, their sign-off or lack of one, and how formal they are.
- Answer what the message asks. If it asks something only they can decide,
  say so plainly and briefly rather than inventing an answer.
- Do not commit to a date, a price, a promise or a deadline that was not
  agreed. "I'll check and come back to you" is a real sentence.
- Plain text. No signature block, no disclaimer, no subject line, and no
  greeting — the composer already has the recipient and the subject.
- Reply with the body and nothing else."""


@router.post('/draft')
async def make_draft(body: SendBody) -> dict[str, Any]:
    """Compose and file it. Sends nothing."""
    mailbox = _mailbox(body.account)
    resolved = store().resolve(body.account)
    original = None
    if body.reply_to:
        original = await (mailbox.reply_context(body.reply_to, folder=body.folder))
    message, _hidden = send_mod.build(
        resolved,
        to=body.to,
        subject=body.subject,
        body=body.body[:MAX_BODY],
        cc=body.cc or None,
        bcc=body.bcc or None,
        reply_to_message=original,
    )
    where = await (mailbox.draft(message))
    return {'ok': True, 'drafted': True, 'folder': where, 'subject': body.subject}


@router.post('/send')
async def send_message(body: SendBody) -> dict[str, Any]:
    """Send what the person wrote.

    A message this route sends was composed in a text box somebody looked at.
    There is no path from here to a model-written send — that goes through
    the `mail` tool, where the approval prompt is the review. Two doors, two
    different guarantees, and this one is the boring one on purpose.
    """
    if not body.to:
        raise HTTPException(status_code=400, detail='to is required')
    if not body.subject.strip():
        raise HTTPException(status_code=400, detail='subject is required')
    if not body.body.strip():
        raise HTTPException(status_code=400, detail='body is required')
    if not config.allow_messages:
        raise HTTPException(
            status_code=403,
            detail='sending mail is switched off on this install (OPENMIRROR_ALLOW_MESSAGES)',
        )

    mailbox = _mailbox(body.account)
    resolved = store().resolve(body.account)
    original = None
    if body.reply_to:
        original = await (mailbox.reply_context(body.reply_to, folder=body.folder))
    elif body.reply_all:
        raise HTTPException(status_code=400, detail='reply_all needs the uid of the message being answered')

    to = body.to
    cc = body.cc
    subject = body.subject
    if original is not None:
        to = [f'{original.sender.name} <{original.sender.email}>'] if original.sender.email else to
        if body.reply_all:
            mine = (resolved.address or '').lower()
            cc = [
                f'{p.name} <{p.email}>'
                for p in [*original.to, *original.cc]
                if p.email and p.email.lower() != mine and p.email.lower() != (original.sender.email or '').lower()
            ]
        if not subject.lower().startswith('re:'):
            subject = f'Re: {original.subject}' if original.subject else subject

    message, hidden = send_mod.build(
        resolved,
        to=to,
        subject=subject,
        body=body.body[:MAX_BODY],
        cc=cc or None,
        bcc=body.bcc or None,
        reply_to_message=original,
    )
    try:
        return await (mailbox.send(message, hidden))
    except MailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

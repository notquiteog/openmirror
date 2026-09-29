"""Reading and answering mail, as one tool.

One tool with actions rather than five tools, for the reason the whole
toolset design rests on: a long tool list measurably hurts. gemma4:12b could
not complete a five-step browser task with thirty tools and completed it in
twenty-six seconds with six. "Read mail", "search mail", "reply to mail" and
"send mail" are one thing a person asks for, so they are one tool.

**The grade is the important part of this file.** Reading is `read` and runs
without asking in every mode — an agent that cannot read your inbox cannot
answer your mail, and that is the entire feature. Sending is
`Risk.MESSAGE`, which `ApprovalPolicy._invariant` treats as a third thing
like a purchase and a secret: possible, and never automatic, in *every* mode
including `unrestricted`, with no way to remember a "yes" and no way to write
a rule that skips it. The reasons are written down at the enum.

`OFF_MIRROR_ALLOW_MESSAGES=0` turns a send into a refusal rather than a
prompt, for an install that should not be able to put words in somebody's
inbox at all.

**The approval summary is the whole review.** When a send is proposed, the
one line a person sees is the only chance they get to catch a wrong
recipient, and it is therefore the recipient, the subject, and the first
lines of the body — in that order, because that is the order in which a
mistake is made. A summary that says "send a message" is not a review.

**Drafting is not sending.** `action: "draft"` files the message in the
server's own Drafts, where the person can find it in the mail client they
already use. There is no local draft file: a draft nobody can see is not a
draft.
"""

from __future__ import annotations

from typing import Any

from openmirror.agent.tools.base import Assessment, Output, Tool, ToolContext, ToolError
from openmirror.mail import send as send_mod
from openmirror.mail.accounts import AccountStore, MailError, parse_addresses
from openmirror.mail.client import box
from openmirror.protocol.agent import Risk

ACTIONS = (
    'accounts', 'folders', 'list', 'read', 'search', 'send', 'reply', 'draft', 'unread', 'flag',
)

# Which grade each action earns. Everything that only looks is `read`; the
# three that hand a message to a server are `message`, which is the invariant
# axis rather than the mode ladder — so these are asked about even in
# `unrestricted`, and never remembered.
RISK: dict[str, Risk] = {
    'accounts': Risk.READ,
    'folders': Risk.READ,
    'list': Risk.READ,
    'read': Risk.READ,
    'search': Risk.READ,
    'unread': Risk.READ,
    'flag': Risk.WRITE,
    'send': Risk.MESSAGE,
    'reply': Risk.MESSAGE,
    'draft': Risk.WRITE,
}


def _summary_for(to: Any, subject: str, body: str) -> str:
    """The line a person reads before agreeing to send.

    Recipient first, then subject, then the opening of the body. That order
    is the order the mistakes happen in — a reply-all that was not supposed
    to be, a subject left as the original thread's, a body written for a
    different person — and each of them is one glance. The body is cut hard:
    this is a prompt, not a document, and a twenty-line preview is a prompt
    nobody finishes reading.
    """
    people = parse_addresses(to)
    who = ', '.join(p.email or p.name for p in people[:3]) or '(no recipient)'
    if len(people) > 3:
        who += f' and {len(people) - 3} more'
    line = f'to {who}   "{subject or "(no subject)"}"'
    opening = ' '.join((body or '').split())[:160]
    if opening:
        line += f'   {opening}{"…" if len(" ".join((body or "").split())) > 160 else ""}'
    return line


class MailTool(Tool):
    name = 'mail'
    description = (
        'Read and answer this person\'s email. Several accounts may be configured; name one with '
        '"account", or leave it out for the default.\n'
        'action "accounts" lists them. "folders" lists the folders with unread counts. '
        '"unread" is the inbox\'s unread messages with their first lines — start here when asked what '
        'has come in, and summarise from it. "list" is any folder; "search" looks for a word in the '
        'inbox. "read" is one message in full, by the uid from one of those.\n'
        '"send" is a new message. "reply" answers the message with that uid and puts it back in the '
        'thread, which is what you want rather than a "send" with a quoted body. "draft" files it in '
        'Drafts instead of sending.\n'
        'Write in their voice: they are an adult with their own job, and the reply should read like '
        'it came from them, not like an assistant. Sign off the way they do, or not at all — their '
        'mail tells you which. Keep it short; if a question needs a paragraph, it usually needs a '
        'meeting instead. Do not commit to a date, a price, a promise or a deadline that was not '
        'agreed — say you will check and come back. Never send without being asked to, and every '
        'send is confirmed by the person first, so a send is the end of the task and not a step in '
        'the middle of one.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': list(ACTIONS), 'description': 'What to do.'},
            'account': {
                'type': 'string',
                'description': 'Which account, by id or address. Default when omitted.',
            },
            'folder': {
                'type': 'string',
                'description': 'For list/read/search: a folder. "inbox", "sent", "drafts" all work by name.',
            },
            'uid': {'type': 'string', 'description': 'For read, reply, flag: the message id from a listing.'},
            'limit': {'type': 'integer', 'description': 'For list/search/unread. Default 25.'},
            'unread_only': {'type': 'boolean', 'description': 'For list: only what has not been read.'},
            'since_days': {
                'type': 'integer',
                'description': 'For list: only what arrived in the last N days.',
            },
            'query': {'type': 'string', 'description': 'For search: the words to look for.'},
            'to': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'For send: who it is to. An address, or "Name <address>".',
            },
            'cc': {'type': 'array', 'items': {'type': 'string'}, 'description': 'For send.'},
            'bcc': {'type': 'array', 'items': {'type': 'string'}, 'description': 'For send.'},
            'subject': {'type': 'string', 'description': 'For send: the subject line.'},
            'body': {'type': 'string', 'description': 'For send, reply, draft: the message itself.'},
            'reply_all': {
                'type': 'boolean',
                'description': 'For reply: include the other recipients. Off unless they asked.',
            },
            'seen': {'type': 'boolean', 'description': 'For flag: mark read or unread.'},
        },
        'required': ['action'],
    }

    def __init__(self, store: AccountStore) -> None:
        self.store = store

    # -- grading ------------------------------------------------------------

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        action = str(args.get('action') or '').strip().lower()
        if not action:
            return Assessment(risk=Risk.READ, summary='', invalid='action is required')
        if action not in RISK:
            return Assessment(
                risk=Risk.READ, summary='',
                invalid=f'{action!r} is not something this does. It can: {", ".join(ACTIONS)}',
            )
        risk = RISK[action]
        account = str(args.get('account') or '').strip()

        if action in ('send', 'draft'):
            people = parse_addresses(args.get('to'))
            if not people:
                return Assessment(risk=risk, summary='', invalid='to is required, and needs an address')
            if not [p for p in people if '@' in p.email]:
                return Assessment(
                    risk=risk, summary='',
                    invalid='none of the recipients has an email address in it',
                )
            subject = str(args.get('subject') or '').strip()
            if not subject:
                # A blank subject is what a message looks like when it went to
                # the wrong person in a hurry, and it is trivially avoided.
                return Assessment(risk=risk, summary='', invalid='subject is required')
            body = str(args.get('body') or '').strip()
            if not body:
                return Assessment(risk=risk, summary='', invalid='body is required')
            if action == 'send':
                return Assessment(
                    risk=risk,
                    summary=_summary_for(args.get('to'), subject, body) + (f'   [{account}]' if account else ''),
                )
            return Assessment(risk=risk, summary=f'draft to {", ".join(p.email for p in people[:3])} "{subject}"')

        if action == 'reply':
            uid = str(args.get('uid') or '').strip()
            if not uid:
                return Assessment(
                    risk=risk, summary='',
                    invalid='uid is required — reply answers one message, and uid is the id from a listing',
                )
            body = str(args.get('body') or '').strip()
            if not body:
                return Assessment(risk=risk, summary='', invalid='body is required — say what the reply says')
            # Who it is going to is not in the arguments, so it cannot be in
            # the summary. The message being answered is, and that is what
            # identifies the conversation.
            return Assessment(risk=risk, summary=f'reply to message {uid}' + (' (all recipients)' if args.get('reply_all') else ''))

        if action == 'read':
            uid = str(args.get('uid') or '').strip()
            if not uid:
                return Assessment(
                    risk=risk, summary='',
                    invalid='uid is required — read one message, and uid is the id from a listing',
                )
            return Assessment(risk=risk, summary=f'read message {uid}')

        if action == 'flag':
            uid = str(args.get('uid') or '').strip()
            if not uid:
                return Assessment(risk=risk, summary='', invalid='uid is required')
            return Assessment(risk=risk, summary=f'mark message {uid} {"read" if args.get("seen") else "unread"}')

        if action == 'search':
            query = str(args.get('query') or '').strip()
            if not query:
                return Assessment(risk=Risk.READ, summary='', invalid='query is required')
            return Assessment(risk=Risk.READ, summary=f'search mail for {query!r}')

        return Assessment(risk=risk, summary=action)

    # -- running ------------------------------------------------------------

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        action = str(args['action']).strip().lower()
        store = self.store

        if action == 'accounts':
            found = store.load()
            if not found:
                return Output(
                    content=(
                        'No mail account is configured. Add one from Settings, or set '
                        'OPENMIRROR_MAIL_ADDRESS, OPENMIRROR_MAIL_IMAP_HOST and OPENMIRROR_MAIL_SMTP_HOST '
                        'with OPENMIRROR_MAIL_PASSWORD and restart. JMAP accounts need a session URL.'
                    ),
                    display={'accounts': []},
                )
            return Output(
                content='\n'.join(
                    f'{a.id}: {a.address} ({"JMAP" if a.use_jmap else "IMAP"}{", default" if a.default else ""})'
                    for a in found
                ),
                display={'accounts': [a.public() for a in found]},
            )

        try:
            account = store.resolve(str(args.get('account') or ''))
            problems = account.validate()
            if problems:
                raise MailError(f'{account.name} is not usable: {"; ".join(problems)}')
            mailbox = box(account)

            if action == 'folders':
                found = await mailbox.folders()
                lines = [
                    f'{f["name"]}: {f["total"] if f["total"] is not None else "?"} messages, '
                    f'{f["unread"] if f["unread"] is not None else "?"} unread'
                    + (f'  ({f["role"]})' if f.get('role') else '')
                    for f in found
                ]
                return Output(
                    content='\n'.join(lines) or 'This mailbox has no folders.',
                    display={'folders': found},
                )

            if action == 'unread':
                return await self._listing(mailbox, folder='inbox', unread_only=True, **self._page(args))

            if action in ('list', 'search'):
                query = str(args.get('query') or '').strip() if action == 'search' else ''
                return await self._listing(
                    mailbox,
                    folder=str(args.get('folder') or '') or ('inbox' if action == 'search' else ''),
                    unread_only=bool(args.get('unread_only')),
                    since_days=args.get('since_days'),
                    search=query,
                )

            if action == 'read':
                found = await mailbox.read(
                    str(args['uid']), folder=str(args.get('folder') or ''), mark_seen=True
                )
                return Output(content=found.as_text(), display={'mail': found.public()})

            if action == 'flag':
                await mailbox.flag(
                    str(args['uid']), seen=bool(args.get('seen')), folder=str(args.get('folder') or 'inbox')
                )
                return Output(content=f'Message {args["uid"]} marked {"read" if args.get("seen") else "unread"}.')

            if action in ('send', 'draft'):
                return await self._compose(mailbox, args, action, account)

            if action == 'reply':
                return await self._reply(mailbox, args, account)
        except MailError as exc:
            # Expected: the server said no, or there is no such account. The
            # model reads it and adjusts — which is what ToolError is for.
            raise ToolError(str(exc)) from exc

        raise ToolError(f'{action}: not handled')

    def _page(self, args: dict[str, Any]) -> dict[str, Any]:
        return {'limit': int(args.get('limit') or 25)}

    async def _listing(
        self,
        mailbox: Any,
        *,
        folder: str,
        limit: int = 25,
        unread_only: bool = False,
        since_days: int | None = None,
        search: str = '',
    ) -> Output:
        found = await mailbox.listing(
            folder=folder, limit=limit, unread_only=unread_only, since_days=since_days, search=search
        )
        if not found:
            where = folder or 'the inbox'
            what = f'matching {search!r}' if search else 'newer than nothing'
            return Output(content=f'No messages in {where} {what}.' if search else f'No messages in {where}.')
        header = f'{len(found)} message(s) in {found[0].folder}, newest first:'
        return Output(
            content='\n'.join([header, *(m.header_line() for m in found)]),
            display={'mails': [m.public() for m in found], 'folder': found[0].folder, 'search': search},
        )

    async def _compose(self, mailbox: Any, args: dict[str, Any], action: str, account: Any) -> Output:
        message, hidden = send_mod.build(
            account,
            to=args.get('to'),
            subject=str(args['subject']),
            body=str(args['body']),
            cc=args.get('cc'),
            bcc=args.get('bcc'),
        )
        if action == 'draft':
            where = await mailbox.draft(message)
            return Output(
                content=f'Filed a draft in {where}. Nothing has been sent.',
                display={'drafted': True, 'folder': where, 'subject': str(message.get('Subject', ''))},
            )
        result = await mailbox.send(message, hidden)
        return Output(
            content=f'Sent to {", ".join(result.get("to", []))}: {result.get("subject")}',
            display=result,
        )

    async def _reply(self, mailbox: Any, args: dict[str, Any], account: Any) -> Output:
        original = await mailbox.reply_context(str(args['uid']), folder=str(args.get('folder') or ''))

        # `Reply-To` wins over `From`, because that is what the header is for
        # and every mailing list uses it. Getting this wrong sends a personal
        # reply to a list address that bounces.
        to = [original.sender] if original.sender.email else []
        cc: list[Any] = []
        if args.get('reply_all'):
            mine = (account.address or '').lower()
            for person in [*original.to, *original.cc]:
                # Never the person replying to themselves, and never a duplicate
                # of the sender — both are what makes reply-all embarrassing.
                if person.email.lower() == mine or any(p.email.lower() == person.email.lower() for p in to):
                    continue
                if person.email:
                    cc.append(person)

        subject = original.subject or ''
        if subject and not subject.lower().startswith('re:'):
            subject = f'Re: {subject}'

        message, hidden = send_mod.build(
            account,
            to=to,
            subject=subject,
            body=str(args['body']),
            cc=cc or None,
            # The two headers that put this back in the thread it belongs to.
            reply_to_message=original,
        )
        result = await mailbox.send(message, hidden)
        return Output(
            content=f'Replied to {original.sender.email}: {subject}',
            display={**result, 'thread': original.thread, 'in_reply_to_message': original.message_id},
        )


__all__ = ['ACTIONS', 'RISK', 'MailTool']

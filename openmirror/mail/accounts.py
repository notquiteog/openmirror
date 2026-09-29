"""Mail accounts, and the credentials that go with them.

An account is data, like a provider connection, for the same reason: somebody
adding their second mailbox at four in the afternoon should not have to edit
a file and restart a daemon that is in the middle of something. The shape of
the store is deliberately identical to `openmirror.providers.connections` —
same JSON envelope, same write-to-temp-and-rename, same 0600 — so that
"where do I put my keys" has one answer in this project rather than two.

**IMAP, not a vendor API, and that is the whole design.** Gmail, Outlook and
a self-hosted currere.co all speak IMAP4 and SMTP, and Python has had both in
the standard library since before this project existed. A vendor API would
mean three OAuth dances, three token refreshers, three client libraries, and
three ways for a feature to break when one of them renames an endpoint. The
cost of IMAP is that the password is a password: Gmail and Microsoft both want
an *app password* rather than the account's own, which is a five-minute setup
in a settings page and a far smaller thing to go wrong than a refresh token
that silently stops working. The trade is worth making, and the error message
when it goes wrong says so by name.

**Credentials never come back out.** No route, no tool result and no log ever
returns a password or a token — only whether one is set, and which of the two
ways it is stored. There is a second way, `password_env`, which names an
environment variable instead of holding the value: on an install managed by
`.env`, which is already 0600 and already gitignored, that is the better home
for a secret, and it keeps a second copy of it out of a second file.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Providers whose settings are known well enough to be worth having in the
# file. Not a requirement — an account is host/port/user/password whatever
# those are — but it means "add my Gmail" is a name rather than three
# settings, and it is the difference between a feature people use and a
# feature people read about.
#
# `starttls` is implied by the port and not stored: 143 and 587 upgrade in
# place, 993 and 465 do not, and the port says which. Getting that wrong is a
# connection that hangs for thirty seconds and then says something unhelpful,
# so it is derived in one place rather than configured in three.
KNOWN: dict[str, dict[str, Any]] = {
    'gmail': {
        'label': 'Gmail',
        'imap_host': 'imap.gmail.com', 'imap_port': 993, 'smtp_host': 'smtp.gmail.com', 'smtp_port': 465,
    },
    'outlook': {
        'label': 'Outlook / Microsoft 365',
        'imap_host': 'outlook.office365.com', 'imap_port': 993, 'smtp_host': 'smtp.office365.com', 'smtp_port': 587,
    },
    'currere': {
        # The project's own mail. A plain IMAP and SMTP mailbox like any
        # other, which is the point: there is no special case for it and no
        # code path that only currere.co reaches.
        'label': 'currere.co',
        'imap_host': 'imap.currere.co', 'imap_port': 993, 'smtp_host': 'smtp.currere.co', 'smtp_port': 465,
    },
}


class MailError(RuntimeError):
    """The mailbox said no, or could not be reached.

    `imaplib` raises bare `IMAP4.error` with a server string that is sometimes
    helpful and sometimes `AUTHENTICATIONFAILED`, and `smtplib` raises a small
    hierarchy. Everything here is normalised into this one type so that the
    model and the interface get a sentence rather than a class name — and
    because a stack trace from deep inside the stdlib is the least actionable
    thing in the project.
    """


@dataclass(slots=True)
class Account:
    """One mailbox.

    Speaks IMAP, JMAP, or both. `protocol` is the default and an account may
    have the two configured independently — reading over JMAP is faster and
    sends over SMTP is the most widely supported thing there is, and pairing
    them is a legitimate setup rather than a contradiction. An empty
    `protocol` means "IMAP, and JMAP if a URL is there", which is what makes
    adding a JMAP account a single extra field rather than a second decision.
    """

    id: str
    address: str
    # A display name for the interface, and the default From. Falling back to
    # the bare address is the right default: an agent that sends mail as
    # "you <me@example.com>" is worse than one that sends it as
    # "me@example.com".
    label: str = ''
    protocol: str = ''
    imap_host: str = ''
    imap_port: int = 993
    smtp_host: str = ''
    smtp_port: int = 465
    # `ssl` for the implicit ports and `starttls` for the others, derived
    # rather than asked for. Overridable because a mail server behind a
    # reverse proxy on a LAN is a perfectly ordinary thing and should not
    # need this file edited to work.
    imap_security: str = ''
    smtp_security: str = ''
    # JMAP. `jmap_url` is the session resource — what the host publishes as
    # its JMAP session URL, or left empty to be discovered at
    # `/.well-known/jmap`, which every server implements.
    jmap_url: str = ''
    jmap_username: str = ''
    jmap_token: str = ''
    password: str = ''
    # The name of an environment variable holding the password, used in
    # preference to `password`. Kept separate rather than being a special
    # value inside `password` because "is this a secret or the name of one"
    # is exactly the kind of question a two-field union answers wrongly.
    password_env: str = ''
    # Copied onto every message sent from this account when it has no
    # `Cc`. Set it and you can see where your agent's mail went from the
    # receiving side too, which is the only way to catch a runaway loop.
    bcc_self: bool = False
    # Which account a reply goes out of when the tool is not told. The first
    # one added is the default, because most people have exactly one.
    default: bool = False

    @property
    def name(self) -> str:
        return self.label or self.address

    @property
    def imap_mode(self) -> str:
        return self.imap_security or ('starttls' if self.imap_port == 143 else 'ssl')

    @property
    def smtp_mode(self) -> str:
        return self.smtp_security or ('starttls' if self.smtp_port in (25, 587) else 'ssl')

    @property
    def jmap_host(self) -> str:
        """The host to look for a JMAP session on, scheme and all.

        A property rather than a field, and the fallback is the IMAP host
        because that is the only host this account is likely to know: JMAP
        discovery is at `https://<host>/.well-known/jmap`, and a person
        configuring a mailbox has typed their mail host once already. A
        separate field would be a second thing to get right for no gain.

        The scheme is preserved when the host carries one, because a
        self-hosted JMAP server on a LAN is very often plain HTTP and there is
        no way to say so otherwise.
        """
        url = (self.jmap_url or '').strip()
        if url:
            return re.sub(r'^https?://', '', url).split('/')[0]
        return (self.imap_host or '').strip()

    @property
    def use_jmap(self) -> bool:
        """Whether reads and writes go over JMAP rather than IMAP.

        True only when JMAP was actually asked for. Having an IMAP host set
        is *not* enough, and getting that wrong is the worst kind of default
        here: every Gmail and Outlook account has an IMAP host, so treating
        that as evidence of JMAP would send every one of them to
        `/.well-known/jmap`, get a 404 or a certificate error, and report that
        the mailbox could not be reached at all.

        So there are two ways in, and both are explicit: a `jmap_url`, or
        `protocol: jmap` — where the IMAP host is then used as the
        discovery host. Naming `imap` overrides both, which is how somebody
        opts out of a host whose JMAP bridge is worse than its IMAP.
        """
        if self.protocol.strip().lower() in ('imap', 'imap4'):
            return False
        if (self.jmap_url or '').strip():
            return True
        return self.protocol.strip().lower() in ('jmap', 'jmap4') and bool((self.imap_host or '').strip())

    def secret(self) -> str:
        """The password, from wherever this account keeps it."""
        if self.password_env:
            value = os.environ.get(self.password_env, '')
            if not value:
                raise MailError(
                    f'{self.name}: the environment variable {self.password_env} is not set, and that is '
                    f'where this account keeps its password'
                )
            return value
        if not self.password:
            if self.jmap_token:
                return self.jmap_token
            raise MailError(
                f'{self.name}: no password. Set one on the account, or point password_env at an '
                f'environment variable that holds it'
            )
        return self.password

    def has_password(self) -> bool:
        if self.password or self.jmap_token:
            return True
        return bool(self.password_env and os.environ.get(self.password_env))

    def public(self) -> dict[str, Any]:
        """What may leave the process. Never the secret, never its value."""
        return {
            'id': self.id,
            'address': self.address,
            'label': self.name,
            'protocol': 'jmap' if self.use_jmap else 'imap',
            'imap_host': self.imap_host,
            'imap_port': self.imap_port,
            'smtp_host': self.smtp_host,
            'smtp_port': self.smtp_port,
            'imap_security': self.imap_mode,
            'smtp_security': self.smtp_mode,
            'jmap_url': self.jmap_url,
            'jmap_username': self.jmap_username,
            'has_password': self.has_password(),
            'has_token': bool(self.jmap_token),
            'password_from_env': self.password_env or None,
            'bcc_self': self.bcc_self,
            'default': self.default,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Account:
        """Build one, filling in the hosts for a provider we know by name.

        `provider` is a convenience on the way in and is not stored: an
        account that remembered it would stop following a change to the port
        Gmail listens on, and would keep working only until somebody
        upgraded the code.
        """
        data = dict(raw)
        provider = str(data.pop('provider', '') or '').strip().lower()
        known = KNOWN.get(provider, {})
        for field_name in ('label', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port'):
            if not data.get(field_name) and known.get(field_name):
                data[field_name] = known[field_name]
        data.setdefault('label', known.get('label', ''))
        try:
            return cls(**data)
        except TypeError as exc:
            raise MailError(f'this account has a field openmirror does not know: {exc}') from exc

    def validate(self) -> list[str]:
        """What is wrong with this account, as sentences.

        Checked before it is used and reported in full, rather than one thing
        at a time: configuring a mailbox is a thing a person does once, and
        three round trips of one-error-each is three round trips.
        """
        problems: list[str] = []
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', self.address or ''):
            problems.append(f'{self.address!r} is not an email address')
        if not self.use_jmap and not self.imap_host:
            problems.append('no IMAP host — there is nothing to read mail from')
        if not self.smtp_host and not self.use_jmap:
            # Over JMAP, sending is a mailbox rather than a host, so an SMTP
            # host is not required. Saying it is would be reporting a problem
            # for a configuration that is perfectly complete.
            problems.append('no SMTP host, and this account cannot send over JMAP — add one')
        for name, port in (('IMAP', self.imap_port), ('SMTP', self.smtp_port)):
            if not 0 < int(port or 0) < 65536:
                problems.append(f'{name} port {port} is not a port')
        if self.use_jmap and not (self.jmap_url or self.jmap_host):
            problems.append('this is set to JMAP but has neither a session URL nor a host to discover it from')
        return problems


class AccountStore:
    """The accounts file. Read on every access, written whole."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[Account]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            # Never fatal. A corrupt accounts file must not stop a daemon that
            # can still do everything else.
            log.warning('could not read %s (%s); no mail accounts', self.path, exc)
            return []
        out: list[Account] = []
        for entry in raw.get('accounts', []):
            try:
                out.append(Account.from_dict(entry))
            except MailError as exc:
                # One bad entry skipped rather than the whole file, so a
                # downgrade loses one mailbox rather than all of them.
                log.warning('skipping a mail account this version cannot read: %s', exc)
        return out

    def save(self, accounts: list[Account]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({'version': 1, 'accounts': [asdict(a) for a in accounts]}, indent=2)
        # Temporary file in the same directory, renamed into place, so a
        # crash halfway cannot leave a half-written file where a password was.
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(payload, encoding='utf-8')
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            # Windows has no POSIX mode. The file lives under the user's own
            # data directory there, which is the same protection by another
            # route; failing the write over it would be a portability bug.
            pass
        tmp.replace(self.path)

    def put(self, account: Account) -> list[Account]:
        """Add or replace by id, keeping exactly one default.

        The invariant matters more than it looks: two defaults means every
        send without an explicit account is a coin toss, and the wrong branch
        is mail from the wrong address to a real person.
        """
        current = [a for a in self.load() if a.id != account.id]
        current.append(account)
        if not any(a.default for a in current):
            for candidate in current:
                if candidate.id == account.id:
                    candidate.default = True
                    break
        self.save(current)
        return current

    def remove(self, account_id: str) -> bool:
        current = self.load()
        kept = [a for a in current if a.id != account_id]
        if len(kept) == len(current):
            return False
        if not any(a.default for a in kept) and kept:
            kept[0].default = True
        self.save(kept)
        return True

    def get(self, account_id: str) -> Account | None:
        return next((a for a in self.load() if a.id == account_id), None)

    def default(self) -> Account | None:
        """The account used when the caller does not name one."""
        found = self.load()
        if not found:
            return None
        return next((a for a in found if a.default), found[0])

    def resolve(self, wanted: str = '') -> Account:
        """The account to use, by id, by address, or the default.

        An unknown name is an error rather than a silent fall back to the
        default: sending a reply to the wrong mailbox is not a thing anybody
        can notice afterwards.
        """
        found = self.load()
        if not found:
            raise MailError(
                'no mail account is configured. Add one from Settings, or set OPENMIRROR_MAIL_ADDRESS '
                'and OPENMIRROR_MAIL_PASSWORD and restart.'
            )
        if not wanted:
            chosen = next((a for a in found if a.default), found[0])
            return chosen
        needle = wanted.strip().lower()
        for account in found:
            if account.id.lower() == needle or account.address.lower() == needle:
                return account
        raise MailError(
            f'no account called {wanted!r}. There is: {", ".join(f"{a.id} ({a.address})" for a in found)}'
        )


@dataclass(slots=True)
class Address:
    """One `Display Name <local@host>` pair."""

    email: str
    name: str = ''

    def __str__(self) -> str:
        return f'{self.name} <{self.email}>' if self.name else self.email

    @property
    def short(self) -> str:
        """What a person reads in a list: a name, or the local part.

        `alice@example.com` is long, and a table of them is unreadable on a
        narrow pane. `alice` is enough to recognise and short enough to scan.
        """
        if self.name:
            return self.name
        return self.email.split('@')[0] or self.email


def parse_addresses(raw: Any) -> list[Address]:
    """Parse a header, or a JSON list, or a comma-separated string, into addresses.

    Three shapes because three callers exist: a parsed message's `To` header,
    a JSON body from the interface, and a model that has written
    `"a@x.com, b@y.com"`. Failing to parse is not fatal — an unparseable
    entry is kept as a bare address, because dropping a recipient is the one
    outcome worse than sending to a slightly wrong one.
    """
    if raw is None or raw == '':
        return []
    if isinstance(raw, (list, tuple)):
        out: list[Address] = []
        for item in raw:
            out.extend(parse_addresses(item))
        return out
    if isinstance(raw, dict):
        return [Address(str(raw.get('email', '')), str(raw.get('name', '')))]

    from email.utils import getaddresses

    found = getaddresses([str(raw)])
    out = []
    for name, email in found:
        email = email.strip()
        name = (name or '').strip().strip('"')
        if email:
            out.append(Address(email, name))
        elif name:
            # `getaddresses` puts a bare name in the name slot with no
            # address. Kept, because "Team" in a To: line is legal and
            # dropping it is worse than carrying it through to be reported.
            out.append(Address(name, name))
    return out


def format_addresses(people: list[Address]) -> str:
    """Render a list back into a header value, quoting names that need it."""
    from email.utils import formataddr

    return ', '.join(formataddr((p.name, p.email)) for p in people if p.email or p.name)


# Filled in from the environment at build time — see `openmirror.config`. Kept
# here as a list so the config module has one place to read and the rest of
# the package has one place to import.
ENV_FIELDS: dict[str, str] = {
    'address': 'OPENMIRROR_MAIL_ADDRESS',
    'password': 'OPENMIRROR_MAIL_PASSWORD',
    'imap_host': 'OPENMIRROR_MAIL_IMAP_HOST',
    'smtp_host': 'OPENMIRROR_MAIL_SMTP_HOST',
    'label': 'OPENMIRROR_MAIL_LABEL',
}


def from_env(store: AccountStore) -> list[Account]:
    """Fold an environment-configured account into the store, once.

    An install managed by `.env` should not also have to write a JSON file
    with a password in it, so a single account described entirely by
    environment variables is written into the store on first read. It is
    written as a *reference* to the variable rather than a copy of the value,
    which is the whole reason `password_env` exists.
    """
    address = os.environ.get(ENV_FIELDS['address'], '').strip()
    if not address:
        return store.load()
    if any(a.address.lower() == address.lower() for a in store.load()):
        return store.load()

    account = Account(
        id='default',
        address=address,
        label=os.environ.get(ENV_FIELDS['label'], '') or address,
        imap_host=os.environ.get(ENV_FIELDS['imap_host'], ''),
        smtp_host=os.environ.get(ENV_FIELDS['smtp_host'], ''),
        password_env=ENV_FIELDS['password'],
        default=True,
    )
    if not account.imap_host or not account.smtp_host:
        # Guessing a provider from the domain would be right often enough to
        # be dangerous — the failure is a connection to a host that does not
        # exist, and the fix is two lines in the file.
        log.warning(
            '%s is set but %s and %s are not, so it will not be able to read or send. '
            'Set them, or add the account from Settings.',
            ENV_FIELDS['address'], ENV_FIELDS['imap_host'], ENV_FIELDS['smtp_host'],
        )
        return store.load()
    store.put(account)
    return store.load()


__all__ = ['Account', 'AccountStore', 'Address', 'KNOWN', 'MailError', 'format_addresses', 'from_env',
           'parse_addresses']

"""Mail: the same five operations over IMAP or JMAP.

Two protocols, one interface, on purpose. See `client.py` for why the
branching is confined to one file, `imap.py` for the reading side and
`jmap.py` for the JSON one, `send.py` for composing and handing over, and
`accounts.py` for what an account is and where its password lives.

`Message` is the one shape that crosses the boundary, deliberately: it is
what the tool returns to a model and what the routes return to a browser, so
a caller never has to know which protocol produced it.
"""

from __future__ import annotations

from openmirror.mail.accounts import Account, AccountStore, Address, MailError
from openmirror.mail.client import Box, box
from openmirror.mail.imap import Attachment, Mail

__all__ = [
    'Account', 'AccountStore', 'Address', 'Attachment', 'Box', 'Mail', 'MailError', 'box',
]

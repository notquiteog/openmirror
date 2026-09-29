# Mail

Two doors onto one mailbox: the `mail` tool, for the agent, and the Mail tab,
for you. Same accounts, same protocols, same store. Neither is a copy of the
other, and nothing is cached twice.

## Why IMAP and JMAP rather than a vendor API

Because every host this needs to talk to already speaks at least one of them,
and both are in the Python standard library. A vendor API per host would mean
an OAuth dance, a token refresher and a client library for each — three of
each for Gmail, Outlook and a self-hosted currere.co — and every one of them
a way for the feature to break when a vendor renames an endpoint.

The cost is honest and worth stating: **the password is a password.** Gmail
and Microsoft both want an *app password* rather than the account's own,
which is five minutes in a settings page and a much smaller thing to go wrong
than a refresh token that silently expires. It is also a long-lived secret in
a file, and rotating it is a manual job. What it cannot do is read a mailbox
it was not given a password for, and it cannot revoke itself — both of which
are things a real OAuth deployment would get right and this does not.

## JMAP, where it is available

RFC 8620 and 8621. Preferred over IMAP where it exists, for two reasons that
are not performance:

* **`threadId` is the server's own answer** to which conversation a message
  belongs to. IMAP has no such field, so the IMAP path reconstructs it from
  `References`, then `In-Reply-To`, then a normalised subject — right most of
  the time and visibly wrong the rest, which is when a conversation arrives
  as thirty unrelated messages.
* **One round trip gets a page.** `Email/query` then `Email/get`, batched,
  over HTTPS, against a SEARCH-and-FETCH-per-message socket.

And one more that is only a JMAP thing: **submission is a mailbox.** The
outbox is a `Mailbox` with the role `outbox`, and putting a message in it
sends it. No second protocol, no second login, no second failure mode.

Forcing IMAP on an account is `protocol: "imap"`, which is how you opt out of
a host whose JMAP bridge is worse than its IMAP — a real configuration.

## Setting an account up

**From the interface** is the normal way. The Mail tab, or
`GET /api/mail/accounts` for the shape. The server *tests* the connection
before saving, and does not save an account that cannot sign in: an account
that looks configured and fails on first use with a message about a mailbox
rather than about a setting is worse than no account.

**From the environment**, for an install managed by a file:

```bash
OPENMIRROR_MAIL_ADDRESS=you@example.com
OPENMIRROR_MAIL_PASSWORD=an-app-password
OPENMIRROR_MAIL_IMAP_HOST=imap.example.com
OPENMIRROR_MAIL_SMTP_HOST=smtp.example.com
```

The password is *referenced* by variable name in the store, never copied
into it, so a second file never holds a second copy of a secret.

**JMAP** additionally needs a session URL — what the host publishes as its
JMAP session URL, or left blank to be found at `https://<host>/.well-known/jmap`,
which every server implements. That is what makes adding a JMAP account a
single extra field.

## Where the credentials live

`data/mail.json`, written `0600`, in the same shape and with the same
write-to-temp-and-rename as provider connections, because "where do I put my
keys" should have one answer in this project.

**No route, tool result, log or page ever returns a password.** Not the value
and not a fragment of it. `GET /api/mail/accounts` returns `has_password` and
`password_from_env` and nothing more. A store endpoint takes an empty
password to mean "leave what is there", so a round trip through the interface
cannot blank a working account.

## What the tool does

One tool with actions, because a long tool list measurably hurts — the
measurement is in the README and the reason is above `TOOLSETS` in
`agent/runtime.py`. Reading four things about mail is one thing a person
asks for.

| action | does | grade |
|---|---|---|
| `accounts` | lists them, and which protocol each speaks | read |
| `folders` | every folder with unread and total counts | read |
| `unread` | the inbox's unread messages with their first lines | read |
| `list` | any folder, optionally unread-only or since a date | read |
| `search` | the server's own text search | read |
| `read` | one message in full, and marks it read | read |
| `flag` | read or unread | write |
| `send` | a new message | **message** |
| `reply` | an answer, threaded into the conversation | **message** |
| `draft` | filed in Drafts, nothing sent | write |

`unread` is the one to reach for when asked what has come in. Summarise from
it rather than reading forty messages in full, and say what you did not read.

## Three things it deliberately does not do

**It does not load attachments.** A list of names and sizes comes back and
the bytes do not. An inbox is the easiest place in the world to make a model
read a forty-megabyte PDF by accident.

**It does not mark anything read by listing it.** Every fetch uses
`BODY.PEEK`, so opening the interface does not clear a thousand unread
badges and turn the count into a lie.

**It does not send anything without the prompt.** `Risk.MESSAGE`, asked in
every mode, never remembered, never grantable by a rule. The reasoning is in
[AUTONOMY.md](AUTONOMY.md) and it is worth repeating here because mail is
where it bites hardest: the recipient is a person who cannot see this
conversation, cannot consent to it, and cannot take it back.

## Threading

`References` first, then `In-Reply-To`, then a normalised subject with `Re:`
and `Fwd:` stripped — which is what a phone-generated reply with no headers
needs, and it is a heuristic, and it is why JMAP is preferred.

A reply carries **both** threading headers and appends to the original's own
chain rather than replacing it, because a reply to a reply that drops the
grandparent is how a five-message conversation becomes three unrelated pairs.
Get only `In-Reply-To` right and the reply is in the thread with no history
in it, which is the specific thing that makes generated mail read as
generated.

Quoted history is cut off before anything is sent or drafted. Four shapes are
tried, longest first, because every mail client has solved this for twenty
years in a different way: `On <date> wrote:`, `-----Original Message-----`,
a `From:`/`Sent:` Outlook block, and a line of `>`.

## From a browser

The Mail tab, and the same routes:

```
GET    /api/mail/accounts          POST   /api/mail/accounts
DELETE /api/mail/accounts/{id}     GET    /api/mail/folders
GET    /api/mail/messages          GET    /api/mail/message
POST   /api/mail/read              POST   /api/mail/reply-context
POST   /api/mail/propose           POST   /api/mail/draft
POST   /api/mail/send
```

`/propose` and `/send` are separate for the same reason the Commit bar's two
buttons are: `propose` returns a string and has no way to send it, and
`/api/mail/send` takes a message somebody looked at. What reaches the
provider is the one message being answered — not the account's history, not
another thread. The difference is the difference between a provider seeing one
message and seeing an inbox.

"""Finding a conversation you can no longer remember having.

`openmirror/sessions.py` writes one append-only JSONL transcript per session,
which means everything said in this daemon is still on disk and there was no
way to go and look at it. Both other harnesses can: opencode has `/share` and
a session search, Claude Code can search its history. Somebody who remembers
*that* they fixed the parser last Tuesday, in a session called
`openmirror`, currently has to scroll a list of forty sessions called
`openmirror` and open each one.

**A title is a hint, and the excerpt is the evidence.** The first user message
is what a session is *about*, so it carries the most weight in ranking, and
tool output carries none at all — deliberately. A transcript is mostly tool
results, and those are the agent's own copy of the files it read: matching on
them would make a search for any word that appears anywhere in the repository
return every session that ever opened a file. Title first, then what people
said.

**Why the excerpt carries offsets and not markup.** The result is a plain
string plus a list of `[start, end)` character spans, and no HTML. Two reasons,
and the first is the one that matters: a transcript contains whatever the agent
read, so anything that renders a match has to escape it, and a server that
hands back `<mark>` has made every client responsible for doing that correctly
forever. The second is that a `text/marked` split pair bakes *this* server's
opinion of what a highlight is into the payload — a client that wants to bold
one term and underline another has to rejoin and resplit. Offsets are the only
form that survives a client rendering the text its own way.

**How much of a transcript is read.** `TAIL_BYTES`, and the reasoning is worth
writing down. Reading a whole transcript is not the problem it looks like from
inside a single session — the largest in a real data directory is about 13 kB —
but a sweep is a sweep over *every* session, and a busy install has a thousand
of them, so the whole-file version is a request that reads gigabytes to answer
a question about the last few turns. 64 kB is chosen because it is comfortably
above the largest single record the store writes (a tool result is capped at
20 000 characters by `sessions.py`, which is tens of kilobytes escaped), so a
window always contains at least one complete line, and because at a thousand
sessions it caps a sweep at tens of megabytes rather than gigabytes. The cost
is explicit and is a real one: **a session whose only mention is older than the
last 64 kB is found by its title, or not at all.** That is the right side of
the bargain — the tail is where a conversation is *now*, and the title is the
one thing that is indexed — but it is a bound, not an approximation, so it is
stated here rather than discovered.

**Nothing in this module raises.** A transcript is append-only, so the
realistic damage is a daemon that stopped mid-write and left one short line; a
search that raised on it would take down the feature exactly when there is
something to find. Corrupt lines are skipped, unreadable files are skipped, and
a missing directory is an empty list.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path
from typing import Any

from openmirror.sessions import Stored, TranscriptError

log = logging.getLogger(__name__)

#: How much of the end of each transcript is searched. See the module
#: docstring: chosen to be above the largest record `sessions.py` writes, so a
#: window always holds at least one whole line, and to cap a sweep over a
#: thousand sessions at tens of megabytes of reads.
TAIL_BYTES = 64 * 1024
#: Enough of the front of a file to find the header line. 16 kB against a
#: header of a couple of hundred bytes, so a transcript with a preamble still
#: yields its id, title and root.
HEAD_BYTES = 16 * 1024
#: How much of a matching message comes back with a result. One screen of a
#: conversation in a sidebar row, with the match in the middle third of it.
EXCERPT_CHARS = 220
#: Ranking weights. A title is what a person wrote about the conversation, a
#: request is what they asked for, and an answer is the model talking — in that
#: order, and all three well above the recency tiebreak.
TITLE_WEIGHT = 8
USER_WEIGHT = 3
ASSISTANT_WEIGHT = 1
#: For finding the message index afterwards, in 4 MiB slices. The prefix of a
#: transcript is only ever read for rows that are about to be returned.
READ_CHUNK = 4 * 1024 * 1024
#: `SessionStore.save` writes `json.dumps({'type': 'message', **message})`, so
#: every message line starts with this and the header line with its sibling.
#: Counting these in C is a hundred times faster than parsing the prefix line
#: by line, and it is *checked* below rather than trusted.
_MESSAGE_PREFIX = b'{"type": "message"'
_HEADER_PREFIX = b'{"type": "session"'


@dataclass(slots=True)
class _Hit:
    """One session that matched, and the message in it that matched best."""

    path: Path
    id: str
    title: str
    root: str
    created: float
    updated: float
    unfinished: bool
    role: str
    score: int
    text: str
    offset: int


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_sessions(
    query: str,
    *,
    limit: int = 50,
    root: str | Path | None = None,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    """Every stored conversation matching `query`, best first. One row per session.

    Multi-word queries are an AND of case-insensitive substrings, so `retry
    uploader` finds the conversation that was about both and not the two that
    were about each. An OR would drown the first page in sessions that matched
    one word trivially, and a session list is a short list.

    `root` narrows to one project: sessions whose stored root is that directory
    or somewhere inside it. Exact after `realpath` on both sides, so a symlinked
    path still finds its session, and a session with no recorded root is
    excluded rather than guessed at.

    `store` is the transcript store, and is only there so a test can point at a
    temporary directory instead of `data/`. Callers pass `manager.store()`, the
    same object `routers/agent.py` uses for the Markdown export.

    Never raises. The result row is everything a list needs to render and link:
    `id`, `title`, `root`, `created`, `updated`, the `role` and `message_index`
    of the best match, an `excerpt`, the `spans` of the match inside it, and
    `unfinished` so a row can say the turn it ended on was cut off.
    """
    terms = [term for term in str(query or '').strip().casefold().split() if term]
    if not terms:
        return []
    wanted: Path | None = None
    if root:
        try:
            wanted = Path(os.path.realpath(str(root)))
        except (OSError, ValueError):
            # A path that cannot be resolved — a null byte in a query string —
            # names no directory, and a filter the caller asked for must not
            # quietly stop being one and hand back every session instead.
            return []
    directory = _directory_of(_store_for(store))
    if directory is None:
        return []
    cap = max(1, int(limit))

    hits: list[_Hit] = []
    for path in sorted(directory.glob('*.json')):
        if path.name == 'index.json':
            continue
        try:
            found = _scan(path, terms, wanted)
        except (OSError, ValueError) as exc:
            # One unreadable file must not cost the other thousand their
            # results, so a sweep logs and moves on rather than unwinding.
            log.debug('%s was not searched: %s', path.name, exc)
            continue
        if found is not None:
            hits.append(found)

    # Ranked by how well the query matched, and only then by when the
    # conversation was last touched. Recency first would put a session whose
    # title says nothing about the query above the one that is about it.
    #
    # The id at the end is not decoration. Two transcripts written in the same
    # filesystem tick have the same `updated`, and two sessions can match a
    # query equally well, so without a third key the order is whatever the
    # directory listing happened to give — which is stable on one machine and
    # not on another, and can reorder between two identical searches. A search
    # that reshuffles when you retype the same word reads as the answer
    # changing rather than as two equally good matches.
    hits.sort(key=lambda hit: (-hit.score, -hit.updated, hit.id))
    out: list[dict[str, Any]] = []
    for hit in hits[:cap]:
        excerpt, spans = _excerpt(hit.text, terms)
        out.append({
            'id': hit.id,
            'title': hit.title,
            'root': hit.root,
            'created': hit.created,
            'updated': hit.updated,
            'unfinished': hit.unfinished,
            'role': hit.role,
            'message_index': _message_index(hit.path, hit.offset),
            'excerpt': excerpt,
            'spans': spans,
        })
    return out


def searchable_session_ids(*, store: Any | None = None) -> list[str]:
    """The ids of every conversation on disk.

    From the directory rather than the index, because that is the same choice
    `SessionStore.list` makes and for the same reason: an index that can lose
    a session is worse than no index. The file stem *is* the id —
    `SessionStore.path_for` sanitises to `[A-Za-z0-9_-]` — so this costs a glob
    and no reads at all.
    """
    directory = _directory_of(_store_for(store))
    if directory is None:
        return []
    return [path.stem for path in sorted(directory.glob('*.json')) if path.name != 'index.json']


def _store_for(store: Any | None) -> Any | None:
    """The store to read, defaulting to the process-wide one.

    Imported rather than module-level so that importing this module — from a
    test, or from anything that only wants `export_html` — does not drag in the
    agent runtime and its provider registry.
    """
    if store is not None:
        return store
    try:
        from openmirror.agent.manager import manager

        return manager.store()
    except Exception:  # noqa: BLE001 - search is a read, and a failure is an empty list
        log.exception('no transcript store; nothing to search')
        return None


def _directory_of(store: Any | None) -> Path | None:
    """The directory holding the transcripts, if there is one.

    `SessionStore.__init__` creates its directory, so a store that exists
    normally has one; this still asks, because a store pointed at a path that
    was then deleted is the state a search most often meets after a restart.
    """
    if store is None:
        return None
    root = getattr(store, 'root', '')
    if not root:
        return None
    try:
        directory = Path(root)
        return directory if directory.is_dir() else None
    except (OSError, TypeError, ValueError):
        return None


def _scan(path: Path, terms: list[str], wanted: Path | None) -> _Hit | None:
    """The best match in one transcript, or None.

    Two passes, and the split between them is the whole performance argument.
    The first pass reads 64 kB off the end of the file and decides whether it
    matches at all; the second, which reads the file from the beginning, runs
    only for the handful of sessions that are about to be returned and exists
    solely to turn a byte offset into the message index a client needs in order
    to jump. The expensive part is therefore paid by the results rather than by
    the corpus.
    """
    header = _header_of(path)
    session_id = str(header.get('id') or path.stem)
    session_root = str(header.get('root') or '')
    if wanted is not None and not _under(session_root, wanted):
        return None
    try:
        updated = float(path.stat().st_mtime)
    except OSError:
        updated = float(header.get('created') or 0.0)
    title = str(header.get('title') or '')
    in_title = _terms_in(title, terms)
    phrase = ' '.join(terms)

    best: _Hit | None = None
    for offset, record in _messages_at_tail(path):
        text = _text_of(record)
        if not text:
            continue
        in_text = _terms_in(text, terms)
        # A term found in the title counts for every message in the session,
        # so `parser retry` matches a session called "Parser" whose request
        # only says "add a retry".
        found = in_text | in_title
        if len(found) < len(terms):
            continue
        role = 'user' if record.get('role') == 'user' else 'assistant'
        weight = USER_WEIGHT if role == 'user' else ASSISTANT_WEIGHT
        score = TITLE_WEIGHT * len(in_title) + weight * len(found - in_title)
        if _contains(text, phrase) or _contains(title, phrase):
            # The whole query, adjacent, is what somebody meant. Split across a
            # title and a message it does not count, which is the correct
            # reading: those are two conversations that happen to share words.
            score += TITLE_WEIGHT
        if best is None or score > best.score:
            best = _Hit(
                path=path, id=session_id, title=title, root=session_root,
                created=float(header.get('created') or updated), updated=updated,
                unfinished=bool(header.get('unfinished')), role=role, score=score,
                text=text, offset=offset,
            )
    return best


def _header_of(path: Path) -> dict[str, Any]:
    """The `type: session` line, or `{}`.

    The header is the first line the store writes, but it is *looked for*
    rather than assumed, because a transcript is a file that outlives the code
    that wrote it and a header that moved is a session that should still be
    findable.
    """
    try:
        with path.open('rb') as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return {}
    for line in head.split(b'\n'):
        record = _decode(line)
        if isinstance(record, dict) and record.get('type') == 'session':
            return record
    return {}


def _messages_at_tail(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """`(byte offset, message)` for the records inside the tail window.

    The first line of the window is dropped when the file is longer than the
    window, because it is a fragment: half a line is not half a message, and
    matching inside one would report an excerpt nobody can find in the file.
    """
    out: list[tuple[int, dict[str, Any]]] = []
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            base = max(0, size - TAIL_BYTES)
            handle.seek(base)
            data = handle.read()
    except OSError:
        return out
    if not data:
        return out
    if size > len(data):
        first = data.find(b'\n')
        if first < 0:
            # A single record larger than the window. It is skipped whole
            # rather than half-read, and `TAIL_BYTES` is set above the largest
            # record the store writes precisely so this does not happen.
            return out
        data = data[first + 1:]
        base += first + 1
    for line in data.split(b'\n'):
        if not line.strip():
            continue
        record = _decode(line)
        if isinstance(record, dict) and record.get('type') == 'message':
            out.append((base, record))
        base += len(line) + 1
    return out


def _decode(line: bytes) -> Any:
    """One JSONL line, or None. A short or corrupt line is skipped, never fatal.

    `errors='replace'` rather than a strict decode, because the realistic damage
    is a write cut off mid-character and a whole file is a poor price for the
    turn before it.
    """
    try:
        return json.loads(line.decode('utf-8', errors='replace'))
    except (ValueError, RecursionError):
        return None


def _text_of(record: dict[str, Any]) -> str:
    """The words in one message.

    Text blocks only. A transcript is mostly `tool_result` blocks, which are
    the agent's own copy of the files it read; searching them would make a
    query for any word in the repository match every session that ever opened a
    file, and the excerpt would be a diff rather than a sentence.
    """
    parts: list[str] = []
    for block in record.get('content') or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = str(block.get('text') or '')
            if text:
                parts.append(text)
    return '\n'.join(parts)


def _terms_in(text: str, terms: list[str]) -> set[str]:
    """Which of the query's terms occur in this text."""
    haystack = text.casefold()
    return {term for term in terms if term in haystack}


def _contains(text: str, phrase: str) -> bool:
    return bool(phrase) and phrase in text.casefold()


def _under(session_root: str, wanted: Path) -> bool:
    """Whether a session's root is `wanted`, or somewhere inside it.

    Descendants count because a session in `~/proj/api` is unambiguously part
    of `~/proj`; a sibling worktree is not, which is correct — it is a
    different tree, and `openmirror.agent.worktree` exists to say so.
    """
    if not session_root:
        return False
    try:
        stored = Path(os.path.realpath(session_root))
    except (OSError, ValueError):
        return False
    return stored == wanted or wanted in stored.parents


def _message_index(path: Path, offset: int) -> int:
    """How many message records are stored before this byte offset.

    Needed because the tail window knows *where* the match is and the client
    needs *which* message it is — `Stored.messages` is a plain list and the
    index into it is the only thing a client can jump to.

    Counted in C over the prefix and then *verified*: the number of lines in
    the prefix must be exactly the number of message records plus the one
    header, or the file was not written by the writer this assumes (compact
    separators, a future key order) and the count falls back to parsing. The
    check is what stops a fast path from being a wrong answer.

    This reads the file from the beginning, so it runs only for the rows about
    to be returned, never for the corpus.
    """
    if offset <= 0:
        return 0
    count = _count_prefix(path, offset)
    if count is None:
        return _parse_prefix(path, offset)
    return count


def _count_prefix(path: Path, offset: int) -> int | None:
    """Message records before `offset`, by counting needles. None if unverified."""
    seen = 0
    lines = 0
    carry = b''
    try:
        with path.open('rb') as handle:
            while offset > 0:
                chunk = handle.read(min(READ_CHUNK, offset))
                if not chunk:
                    return None
                offset -= len(chunk)
                data = carry + chunk
                cut = data.rfind(b'\n')
                if cut < 0:
                    carry = data
                    continue
                head, carry = data[:cut + 1], data[cut + 1:]
                lines += head.count(b'\n')
                seen += head.count(b'\n' + _MESSAGE_PREFIX)
                if head.startswith(_MESSAGE_PREFIX):
                    seen += 1
    except OSError:
        return None
    # A newline inside a JSON string is escaped, so a raw `\n` is always a
    # line ending. Every line in the prefix must be the header or a message,
    # or this file was not written the way the needle assumes.
    if carry or lines != seen + 1:
        return None
    return seen


def _parse_prefix(path: Path, offset: int) -> int:
    """The same count, one line at a time, for a file the needle cannot trust."""
    seen = 0
    try:
        with path.open('rb') as handle:
            data = handle.read(offset)
    except OSError:
        return -1
    for line in data.split(b'\n'):
        if not line.strip():
            continue
        if line.startswith(_MESSAGE_PREFIX) or line.startswith(_HEADER_PREFIX):
            seen += int(line.startswith(_MESSAGE_PREFIX))
            continue
        record = _decode(line)
        seen += int(isinstance(record, dict) and record.get('type') == 'message')
    return seen


def _excerpt(text: str, terms: list[str]) -> tuple[str, list[list[int]]]:
    """A window of the message around the match, and where the match is in it.

    Offsets are half-open character ranges into the returned string, which is
    plain text with its whitespace collapsed — a transcript is multi-line and a
    sidebar row is not. The spans are computed *after* the collapse, so a client
    that slices the string it was given gets the right characters; computing
    them on the un-collapsed text and shipping the collapsed string is the bug
    that makes highlights land in the wrong place, silently.

    Empty spans are a real answer and not a failure: the session matched on its
    title, so nothing in this message is the match, and the excerpt is shown
    plain rather than dragged to some arbitrary place to have something to
    highlight.
    """
    flat = ' '.join(str(text or '').split())
    if not flat or not terms:
        return flat[:EXCERPT_CHARS].strip(), []
    low = flat.casefold()
    phrase = ' '.join(terms)
    spans: list[tuple[int, int]] = []
    at = low.find(phrase)
    if at >= 0:
        spans.append((at, at + len(phrase)))
    else:
        for term in terms:
            found = low.find(term)
            if found >= 0:
                spans.append((found, found + len(term)))
    if not spans:
        return flat[:EXCERPT_CHARS].strip(), []

    start = max(0, min(begin for begin, _ in spans) - EXCERPT_CHARS // 3)
    if start:
        # Start on a word: a half word at the front of a row reads as a typo in
        # the text it is quoting.
        space = flat.find(' ', start)
        start = space + 1 if 0 <= space < min(len(flat), start + 24) else start
    end = min(len(flat), start + EXCERPT_CHARS)
    if end < len(flat):
        space = flat.rfind(' ', start, end)
        if space > start:
            end = space
    lead = '…' if start else ''
    tail = '…' if end < len(flat) else ''
    shift = len(lead) - start
    return lead + flat[start:end] + tail, [
        [begin + shift, stop + shift] for begin, stop in spans if begin >= start and stop <= end
    ]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_html(session_id: str, *, title: str = '', store: Any | None = None) -> str:
    """One stored conversation as a single self-contained HTML document.

    The stand-in for opencode's `/share`: a file you can mail to somebody, open
    on a machine with no network, and read years from now. So — no stylesheet,
    no script, no font, no image request, nothing that phones home. The styling
    is a system font stack and a `prefers-color-scheme` block, and that is the
    whole dependency list.

    **Every bit of the conversation is escaped**, element text and attribute
    values alike, because a transcript contains whatever the agent read: source
    files, build logs, another person's email. A transcript is user content by
    definition, and an unescaped `<script>` in one is a script that runs in
    whoever opens the file. There is no `Markup` here and no place a future
    edit could add one — `escape(quote=True)` is applied to every string on its
    way into the document, including the title that goes into a `content=`
    attribute.

    Raises `TranscriptError` when there is no such conversation, so a caller
    turns one case into a 404 rather than writing an empty document.
    """
    store = _store_for(store)
    if store is None:
        raise TranscriptError(f'no stored conversation called {session_id!r}')
    try:
        stored = store.load(session_id)
    except TranscriptError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad transcript is a 404, not a 500
        raise TranscriptError(f'no stored conversation called {session_id!r}') from exc
    if stored is None:
        raise TranscriptError(f'no stored conversation called {session_id!r}')
    return render_html(stored, title=title)


def render_html(stored: Stored, *, title: str = '') -> str:
    """The document itself, for a `Stored` already in hand.

    Split from `export_html` so a caller that has just loaded a session — the
    router, or a test — does not have to go back to the store to render it.
    """
    heading = (title or stored.title or stored.id).strip()
    # The root is escaped because the root is a path the *caller* of
    # `POST /api/sessions` chose, and the model is escaped for the same reason
    # everything else here is: a document about untrusted text has no field that
    # is exempt. The arrow is the one piece of markup in this line, and it is
    # markup this module wrote.
    meta = ' &middot; '.join(
        part
        for part in (
            _esc(stored.root),
            _esc(stored.model),
            f'{_esc(_when(stored.created))} &rarr; {_esc(_when(stored.updated))}',
        )
        if part
    )
    body: list[str] = []
    if stored.unfinished:
        body.append(
            '<p class="note">The daemon stopped while a turn was running. '
            'That turn is not in this transcript.</p>'
        )
    for number, message in enumerate(stored.messages):
        body.append(_render_message(message, number))
    return '\n'.join([
        '<!doctype html>',
        '<html lang="en">',
        '<head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f'<meta name="description" content="{_esc(heading)}">',
        f'<title>{_esc(heading)}</title>',
        # The stylesheet is the one thing in this document that is not escaped,
        # because it is a literal: no part of it comes from a transcript. It is
        # inlined rather than linked so the file works with no network at all.
        f'<style>{_CSS}</style>',
        '</head>',
        '<body>',
        f'<h1>{_esc(heading)}</h1>',
        f'<p class="meta">{meta}</p>',
        *body,
        f'<footer>Exported by openmirror &middot; session {_esc(stored.id)}</footer>',
        '</body>',
        '</html>',
        '',
    ])


def as_json(stored: Stored) -> dict[str, Any]:
    """A stored session as plain data: exactly the fields of `Stored`.

    By construction rather than by a hand-written dict, so the JSON export and
    the thing the store loads cannot drift apart — a client that round-trips
    this file gets back the same session.
    """
    return asdict(stored)


def _render_message(message: dict[str, Any], number: int) -> str:
    """One message as an `<article>`.

    Tool calls and results are kept, as `to_markdown` keeps them: "the agent
    ran a command and here is the conversation without it" is a document that
    misleads. They are in `<details>` because a transcript is mostly tool
    output and nobody is reading three thousand lines of it.
    """
    role = str(message.get('role') or '')
    body: list[str] = []
    for block in message.get('content') or []:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'text':
            body.append(f'<div class="text">{_esc(block.get("text") or "")}</div>')
        elif kind == 'thinking':
            body.append(_folded('reasoning', f'<div class="text">{_esc(block.get("text") or "")}</div>'))
        elif kind == 'tool_use':
            call = f'{block.get("name")}({json.dumps(block.get("input") or {}, ensure_ascii=False, indent=2)})'
            # The name is escaped in the summary and again inside the call, and
            # both are needed: a tool name comes from a model, which is a
            # source of strings nobody on this machine has vetted.
            body.append(_folded(f'tool &middot; {_esc(block.get("name") or "")}', f'<pre>{_esc(call)}</pre>'))
        elif kind == 'tool_result':
            label = 'error' if block.get('is_error') else 'result'
            body.append(_folded(label, f'<pre>{_esc(block.get("content") or "")}</pre>'))
        elif kind == 'image':
            body.append(f'<p class="note">{_esc(block.get("note") or "an image, not stored in a transcript")}</p>')
    if not body:
        # Said rather than rendered as an empty bubble, which reads as a
        # message that failed to arrive.
        body.append('<p class="note">(nothing in this message)</p>')
    who = 'You' if role == 'user' else 'openmirror'
    klass = 'msg user' if role == 'user' else 'msg assistant'
    return '\n'.join([
        f'<!-- m{number} -->',
        f'<article class="{klass}">',
        f'<h2 class="who">{_esc(who)}</h2>',
        *body,
        '</article>',
    ])


def _folded(summary: str, inside: str) -> str:
    return f'<details><summary>{summary}</summary>{inside}</details>'


def _esc(value: Any) -> str:
    """Every string on its way into the document, quoted or not.

    `quote=True` is the part that is easy to leave out and the part that
    matters: without it a title containing `"` closes the `content="` attribute
    and everything after it is markup again.
    """
    return escape(str(value), quote=True)


def _when(stamp: float) -> str:
    """A local timestamp, in this machine's own timezone.

    A shared transcript is read in the reader's timezone, not the writer's, and
    a UTC stamp with no marker on it is a small lie told to whoever reads it
    years later.
    """
    try:
        return datetime.datetime.fromtimestamp(float(stamp)).astimezone().strftime('%Y-%m-%d %H:%M')
    except (OSError, OverflowError, ValueError):
        return 'unknown date'


# One stylesheet, inline, with a system font stack and no import. Light and dark
# by `prefers-color-scheme` rather than by a toggle: a shared file is opened on
# a machine whose appearance this server has no way to know, and honouring the
# OS is the only answer that is right for all of them.
_CSS = """
:root{color-scheme:light dark;--bg:#fbfaf8;--fg:#1b1a18;--muted:#6d6a64;--line:#e3e0d9;
--soft:#f2f0ea;--who:#3f6ea5}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#e8e6e1;--muted:#9b978f;
--line:#2b2b30;--soft:#1c1c20;--who:#8fb3e0}}
*{box-sizing:border-box}
body{margin:0 auto;padding:3rem 1.25rem 6rem;max-width:46rem;background:var(--bg);color:var(--fg);
font:16px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
h1{font-size:1.5rem;line-height:1.3;margin:0 0 .4rem;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:.85rem;margin:0 0 2.5rem;padding-bottom:1rem;
border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.msg{margin:0 0 1.75rem}
.who{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);margin:0 0 .4rem}
.msg.user .who{color:var(--who)}
.text{white-space:pre-wrap;overflow-wrap:anywhere}
pre{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:.7rem .85rem;
margin:.6rem 0 0;overflow-wrap:anywhere;white-space:pre-wrap;
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
details{margin:.6rem 0 0}
summary{cursor:pointer;font-size:.8rem;color:var(--muted)}
.note{color:var(--muted);font-style:italic}
footer{color:var(--muted);font-size:.8rem;border-top:1px solid var(--line);padding-top:1rem;margin-top:3rem}
"""


__all__ = [
    'EXCERPT_CHARS',
    'HEAD_BYTES',
    'TAIL_BYTES',
    'as_json',
    'export_html',
    'render_html',
    'search_sessions',
    'searchable_session_ids',
]

"""Searching what was said, and taking a conversation with you.

Three routes on the same prefix the live session API already uses, and for the
same reason they live on a router of their own rather than inside
`routers/agent.py`: `history` reads files on disk, the agent routes mostly
drive a session that is running, and the two fail in completely different ways.
A corrupt transcript must not be able to take the composer down with it.

The store comes from the module-level `manager`, exactly as the Markdown export
in `routers/agent.py` does it, so a search and an export of the same id can
never disagree about which conversation they mean — which is the one thing a
history feature has to be true about.

Mounted with the rest, under `TokenMiddleware`: a transcript is a person's
whole working day, and "what did I ask about the parser" answers with the
contents of files that search found.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from openmirror.agent.manager import manager
from openmirror.history import as_json, export_html, search_sessions
from openmirror.sessions import TranscriptError

log = logging.getLogger(__name__)

router = APIRouter(prefix='/api/sessions')
# A title is text a person typed and a header value is not a place for text a
# person typed: a newline in a `Content-Disposition` is a response-splitting
# bug, and a quote closes the attribute. Everything else becomes a dash.
_UNSAFE = re.compile(r'[^A-Za-z0-9._-]+')


@router.get('/search')
async def search_history(
    q: str = Query('', description='Words to look for. All of them must appear.'),
    limit: int = Query(50, ge=1, le=500, description='How many conversations to return.'),
    root: str | None = Query(None, description='Only conversations from this project directory.'),
) -> dict[str, Any]:
    """Every stored conversation mentioning `q`, best first.

    An empty query is an empty list rather than a 400, because the caller is a
    box somebody is typing into: the request is made before there is anything
    to search for, and a status code for that is a red box on an empty field.

    The search is blocking file I/O over a directory of transcripts, so it goes
    to a thread — the same reason `/{session_id}/files` does. It is bounded to
    `limit` rows and to a 64 kB tail of each transcript (`openmirror.history`),
    so the worst case is a sweep and never the whole data directory.
    """
    store = manager.store()
    results = await asyncio.to_thread(search_sessions, q, limit=limit, root=root, store=store)
    return {'results': results, 'count': len(results)}


@router.get('/{session_id}/export.html')
async def export_session_html(session_id: str) -> Response:
    """A conversation as one self-contained HTML file, to keep or send.

    Served as an attachment rather than inline, because the alternative is the
    browser rendering a transcript inside the interface it is embedded in —
    and a transcript contains whatever the agent read. `Content-Disposition`
    also means the file keeps its own name after being saved, which an inline
    response thrown away by a "save page as" gets wrong.

    One file, no stylesheet, no script, no font, nothing that makes a request
    to anywhere: a shared conversation has to open on a machine with no network
    and be readable in five years. `openmirror.history.export_html` escapes
    every part of it, because the parts are user content.
    """
    store = manager.store()
    try:
        page = await asyncio.to_thread(export_html, session_id, store=store)
    except TranscriptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=page,
        media_type='text/html; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="{_filename(session_id, page)}"',
            # A minute, and private: a transcript is a person's working day and
            # an export changes only when the conversation does.
            'Cache-Control': 'private, max-age=60',
        },
    )


@router.get('/{session_id}/export.json')
async def export_session_json(session_id: str) -> dict[str, Any]:
    """The stored conversation as it is on disk, as JSON.

    Every field of `Stored`, including the raw encoded blocks — which is the
    point. The HTML export is for reading, and this is for a client that wants
    the transcript itself: to index it, to diff it, or to feed it to something
    else. Same shape as what `SessionStore.load` gives back, so a round trip
    through this route is a session.
    """
    store = manager.store()
    if store is None:
        raise HTTPException(status_code=404, detail=f'no stored conversation called {session_id!r}')
    try:
        stored = await asyncio.to_thread(store.load, session_id)
    except TranscriptError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if stored is None:
        raise HTTPException(status_code=404, detail=f'no stored conversation called {session_id!r}')
    return as_json(stored)


def _filename(session_id: str, page: str) -> str:
    """A file name for the download, from the conversation's own title.

    Read out of the document that was just rendered rather than loaded a second
    time, and unescaped first so a title of `Parser & retry` is named
    `Parser-retry` rather than `Parser-amp-retry`. The heading in that document
    is escaped, so `</h1>` cannot occur inside it and the match is the heading.

    The session id is always part of the name, so two conversations both called
    "notes" do not fight over one filename.
    """
    match = re.search(r'<h1>(.*?)</h1>', page, re.DOTALL)
    title = _UNSAFE.sub('-', unescape(match.group(1)).strip()).strip('-.')[:60] if match else ''
    if not title or title == session_id:
        return f'{session_id}.html'
    return f'{title}-{session_id}.html'


__all__ = ['router']

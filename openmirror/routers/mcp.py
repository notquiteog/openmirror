"""Serving the MCP protocol over HTTP.

Two transports live here because clients are split between them. The current
one is a single endpoint you POST to, where the reply comes back in that POST's
response; the older one is a long-lived `GET /sse` stream plus a `POST` endpoint
the stream names, which is still what a good number of shipped clients speak.
Both hand the same `OpenmirrorMCP` object the same messages — see
`openmirror/mcp/server.py`, where everything that decides anything lives.

**The token is not optional on a network interface.** This endpoint can hand out
a person's memory, so on any bind that is not loopback the router refuses to
mount without `OPENMIRROR_MCP_SERVE_TOKEN`. That is stricter than the rest of the
server, which warns and carries on, and the asymmetry is deliberate: an open
agent endpoint is a machine somebody else can use, and an open twin endpoint is
a person somebody else can read.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from openmirror.config import config
from openmirror.mcp.server import INVALID_REQUEST, PARSE_ERROR, OpenmirrorMCP

log = logging.getLogger(__name__)

router = APIRouter(prefix='/mcp')

# Set at start-up by main.py, which is also the thing that decided whether this
# router is mounted at all.
server: OpenmirrorMCP | None = None

# How long an idle SSE stream waits before sending a comment to keep the
# connection from being closed by something in the middle. Fifteen seconds is
# below every default proxy timeout worth worrying about.
KEEPALIVE = 15.0


@dataclass
class SSESession:
    """One client's half-open conversation.

    The queue is what makes the old transport work at all: a reply has to reach
    the stream that is waiting for it rather than the POST that asked, so the
    POST hands the reply over and returns 202.
    """

    id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))


sessions: dict[str, SSESession] = {}


def _server() -> OpenmirrorMCP:
    if server is None:
        raise HTTPException(status_code=404, detail='the MCP server is not enabled here')
    return server


def _authorise(request: Request) -> None:
    """Check the bearer token, in constant time.

    `compare_digest` rather than `==` because a token compared with `==` leaks
    its length and prefix to anyone patient enough to time the answers.

    **Either token opens it.** `OPENMIRROR_MCP_SERVE_TOKEN` is the specific one
    and remains the stricter of the two, because `main()` refuses to serve this
    router on a non-loopback bind without it. `OPENMIRROR_TOKEN` is accepted as
    well, so a person who has already secured the rest of the daemon is not left
    with an endpoint that wants a second secret they have to go and invent. The
    specific token opens it when set; the general one opens it either way, which
    is the arrangement that cannot lock anybody out while still honouring a
    deliberate MCP-only secret.
    """
    if not (config.mcp_serve_token or config.auth_token):
        return
    header = request.headers.get('Authorization', '')
    offered = header[7:].strip() if header.lower().startswith('bearer ') else ''
    for env in (config.mcp_serve_token, config.auth_token):
        if env and offered and hmac.compare_digest(offered, env):
            return
    raise HTTPException(
        status_code=401,
        detail='this MCP endpoint needs a bearer token (OPENMIRROR_MCP_SERVE_TOKEN)',
    )


async def _handle_payload(payload: Any) -> Any:
    """Answer one message or a batch of them. None means nothing to send back."""
    live = _server()
    if isinstance(payload, list):
        replies = [r for r in [await live.handle(item) for item in payload] if r is not None]
        return replies or None
    return await live.handle(payload)


# -- the current transport: one endpoint, POST ------------------------------


@router.post('')
@router.post('/')
async def streamable_http(request: Request) -> Response:
    """Streamable HTTP.

    Answered as JSON rather than as an event stream. The spec permits either
    and nothing here streams partial progress, so opening an SSE response to
    send one message would be ceremony — and ceremony in a transport is where
    clients disagree.
    """
    _authorise(request)
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {'jsonrpc': '2.0', 'id': None, 'error': {'code': PARSE_ERROR, 'message': 'invalid JSON'}},
            status_code=400,
        )

    reply = await _handle_payload(payload)
    if reply is None:
        # The correct answer to a notification: accepted, nothing to say.
        return Response(status_code=202)
    return JSONResponse(reply)


@router.get('')
@router.get('/')
async def no_server_stream() -> Response:
    """The spec lets a server decline to open a server-to-client stream here.

    Said with 405 and an explanation rather than 404: a client that gets a 404
    concludes the endpoint is wrong and gives up, where 405 tells it to POST,
    which is all it needed to know.
    """
    return JSONResponse(
        {
            'jsonrpc': '2.0',
            'id': None,
            'error': {
                'code': INVALID_REQUEST,
                'message': 'POST JSON-RPC to this endpoint. For the older SSE transport, GET /mcp/sse.',
            },
        },
        status_code=405,
        headers={'Allow': 'POST'},
    )


# -- the older transport: a stream, and somewhere to post ---------------------


@router.get('/sse')
async def legacy_sse(request: Request) -> StreamingResponse:
    _authorise(request)
    _server()

    session = SSESession(id=secrets.token_urlsafe(16))
    sessions[session.id] = session
    # Relative, which is what every implementation of this transport emits and
    # what a client behind a reverse proxy can actually use — an absolute URL
    # built here would name the internal host.
    endpoint = f'{router.prefix}/messages?sessionId={session.id}'

    async def stream():
        yield f'event: endpoint\ndata: {endpoint}\n\n'.encode()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(session.queue.get(), timeout=KEEPALIVE)
                except TimeoutError:
                    # A comment. Keeps the connection alive and is ignored by
                    # every SSE parser.
                    yield b': keepalive\n\n'
                    continue
                yield f'event: message\ndata: {json.dumps(message)}\n\n'.encode()
        except asyncio.CancelledError:
            raise
        finally:
            sessions.pop(session.id, None)

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            # Nginx buffers event streams into uselessness without this.
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@router.post('/messages')
async def legacy_messages(request: Request, sessionId: str = '') -> Response:  # noqa: N803 - the wire name
    """Take a request for a session and push its reply onto that session's stream."""
    _authorise(request)
    session = sessions.get(sessionId)
    if session is None:
        raise HTTPException(status_code=404, detail='no such MCP session — reopen GET /mcp/sse')

    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail='invalid JSON') from None

    reply = await _handle_payload(payload)
    if reply is not None:
        messages = reply if isinstance(reply, list) else [reply]
        for message in messages:
            try:
                session.queue.put_nowait(message)
            except asyncio.QueueFull:
                # A client that has stopped reading its own stream. Dropped
                # rather than blocking this request for ever on a queue nobody
                # is draining.
                log.warning('mcp sse: session %s is not reading; dropped a reply', sessionId)
    return Response(status_code=202)


async def shutdown() -> None:
    """Let every open stream finish. Called when the daemon stops."""
    for session in list(sessions.values()):
        with_nothing: dict[str, Any] = {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                        'params': {'reason': 'the server is stopping'}}
        try:
            session.queue.put_nowait(with_nothing)
        except asyncio.QueueFull:
            pass
    sessions.clear()

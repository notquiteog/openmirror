"""Who is allowed to talk to this daemon.

`OPENMIRROR_TOKEN` used to guard four websockets and nothing else. Every HTTP
route — the one that creates a session in whatever approval mode the body asks
for, the one that starts an autopilot against your real screen, the one that
wipes your memory, the one that spends money — was open to anything that could
reach the port. An operator who set the token had every reason to believe the
server was authenticated. It was not.

The check is therefore an **ASGI middleware**, not a router dependency, and
that is a deliberate choice with a specific reason behind it. `include_router
(..., dependencies=[...])` is the obvious way to write this and it does not
work: on the FastAPI this ships with, the dependency is dropped and the
endpoint runs anyway. A guard that silently does nothing is worse than no
guard, because the code reads as though it is protected. A middleware is one
thing in one file, it runs before routing decides anything, and a route added
next year cannot forget it.

Two ways to present the token:

* **A header**, for anything that is not a browser page — curl, an editor, the
  desktop app, openmirror's own MCP client.
* **A cookie**, set once by `/api/auth/session`. A browser cannot put a header
  on a `<script src>` or a navigation, so a header-only check would either
  break the web interface or leave it permanently exempt — and an exempt
  interface is the hole, not the fix. The cookie is `SameSite=Strict` and
  `HttpOnly`, so it is not readable from the page and does not ride along on a
  cross-site request.

**Loopback with no token stays open**, and that is a decision rather than an
oversight. The default install is `127.0.0.1` and needs no ceremony to start;
requiring a token to use a local app on your own machine is the kind of friction
that gets worked around by turning the server off. The daemon runs commands as
you, and anything else on that port is you too. Binding to a network interface
without a token is a different proposition, and that combination is refused at
start-up rather than merely warned about — a warning in a log file is not a
boundary.

The MCP endpoint keeps its own separate `OPENMIRROR_MCP_SERVE_TOKEN` check and
is **exempt here**, deliberately. It has its own stricter rule already (it
refuses to mount at all on a non-loopback bind without a token), and two
independent tokens on one route is one too many ways to get it wrong.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from openmirror.config import config

log = logging.getLogger(__name__)

COOKIE = 'openmirror_token'
# Long enough that a stale token in a browser profile expires on its own, short
# enough that rotating it is not a multi-week project.
COOKIE_MAX_AGE = 60 * 60 * 24 * 30

# `secure` is left off deliberately. A daemon reached over plain HTTP on a LAN
# cannot make use of a Secure cookie, and setting it would lock people out of
# exactly the install they are trying to reach — the loopback default, where
# there is no TLS at all, is the common case. The two attributes that do the
# real work are set, and both are load-bearing:
#
#   `httponly`  the page's JavaScript cannot read the token. Without it, any
#                script that gets injected into the interface can walk away
#                with the one credential that opens everything.
#   `samesite`  the cookie does not ride along on a cross-site request, so a
#                page on another origin cannot drive this daemon on the
#                holder's behalf.
LOOPBACK = ('127.0.0.1', 'localhost', '::1')

# Reachable without a token. Everything here is either inert or has to be
# reachable *before* a token exists.
# - `/healthz` so you can find out the daemon is up and what it can do.
# - `/` and `/static` so a browser can load the sign-in page at all; a login
#   form you cannot fetch is not a login form.
# - `/api/auth/session` because it is the route that *takes* the token.
# - `/mcp` because it carries a stricter check of its own — a separate
#   `OPENMIRROR_MCP_SERVE_TOKEN`, and `main()` refuses to serve it at all on a
#   non-loopback bind without one. Two tokens on one route is one too many ways
#   to get it wrong. It also accepts `OPENMIRROR_TOKEN` as an alternative, so a
#   single token can secure the whole daemon.
# - the interactive docs, because they are generated from the OpenAPI schema
#   and describe the routes rather than invoking any of them.
PUBLIC = frozenset(
    {
        '/healthz',
        '/',
        '/static',
        '/api/auth/session',
        '/mcp',
        '/docs',
        '/redoc',
        '/openapi.json',
        '/docs/oauth2-redirect',
    }
)

# Prefixes rather than exact paths, because everything under them is either
# already in `PUBLIC` or is the page that has to load before a token exists.
PUBLIC_PREFIXES = ('/static/', '/docs', '/redoc')


def is_loopback(host: str | None) -> bool:
    """Whether a bind address means 'this machine only'."""
    return (host or '').split(':')[0].strip('[]') in LOOPBACK


def _presented(headers: Headers, query: str) -> str | None:
    """The token this request is carrying, from wherever it carries it."""
    auth = headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        offered = auth[7:].strip()
        if offered:
            return offered
    direct = headers.get('x-openmirror-token', '').strip()
    if direct:
        return direct
    # A browser cannot set a header on a websocket, so the websockets have
    # always taken this in the query string. Kept for them, and for the desktop
    # app, rather than forcing either to invent a handshake.
    token = parse_qs(query).get('token', [''])[0].strip()
    if token:
        return token
    cookie = headers.get('cookie', '')
    for part in cookie.split(';'):
        name, _, value = part.partition('=')
        if name.strip() == COOKIE and value.strip():
            return value.strip()
    return None


def authorised(headers: Headers, query: str = '') -> bool:
    """Whether this request may proceed. One rule, used by everything."""
    expected = config.auth_token
    if not expected:
        return True
    presented = _presented(headers, query)
    if presented is None:
        return False
    # Constant time, so a wrong token cannot be discovered a character at a
    # time by anything that can measure response latency.
    return hmac.compare_digest(presented, expected)


def _is_public(path: str) -> bool:
    return path in PUBLIC or path.startswith(PUBLIC_PREFIXES)


def _refuse(scope: Scope) -> JSONResponse:
    """The 401. Says where to sign in, which a bare 401 does not.

    A person following a bookmark wants a page, not a wall of JSON; a script
    wants the JSON. Which one this is can only be told from the `Accept`
    header — and in a scope, `headers` is a list of raw byte pairs rather than
    a `Headers` object, so it has to be wrapped before it can be asked.
    """
    accepted = Headers(scope=scope).get('accept', '')
    wants_html = 'text/html' in accepted and not scope.get('path', '').startswith('/api/')
    return JSONResponse(
        {'detail': 'this install needs its token', 'login': '/api/auth/session'},
        status_code=401,
        headers=None if wants_html else {'WWW-Authenticate': 'Bearer'},
    )


class TokenMiddleware:
    """Refuses any request that does not carry the token.

    Written as a plain ASGI callable rather than `BaseHTTPMiddleware` on
    purpose. `BaseHTTPMiddleware` runs the downstream app in a background task
    and buffers what it returns, which adds a task and a queue to *every*
    request and — the reason this matters here — is awkward around streaming
    responses and websockets, both of which this server does a lot of. Reading
    the scope directly costs one dict lookup and passes through untouched when
    there is no token to check.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if not _is_public(path) and not authorised(Headers(scope=scope), scope.get('query_string', b'').decode()):
            response = _refuse(scope)
            if scope['type'] == 'websocket':
                # A websocket cannot be answered with a 401, and one that is
                # accepted and then ignored looks like a hang rather than a
                # refusal. Close before the upgrade so the client sees a
                # policy violation immediately.
                await send({'type': 'websocket.close', 'code': 1008})
                return
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


router = APIRouter()


@router.post('/api/auth/session')
async def exchange(body: dict[str, object], response: Response) -> dict[str, object]:
    """Turn a token typed into the sign-in box into a cookie.

    The only route that runs no guard of its own — it is the one that takes a
    token, so guarding it with the token would be circular.
    """
    supplied = str(body.get('token') or '')
    expected = config.auth_token
    if not expected:
        # Nothing to sign in to. Said plainly rather than by handing out a
        # cookie that is then checked against an empty string.
        return {'ok': True, 'required': False}
    if not supplied or not hmac.compare_digest(supplied, expected):
        log.warning('rejected a sign-in attempt')
        return JSONResponse({'detail': 'that is not the token'}, status_code=401)  # type: ignore[return-value]
    response.set_cookie(
        COOKIE,
        expected,
        max_age=COOKIE_MAX_AGE,
        path='/',
        httponly=True,
        samesite='strict',
    )
    return {'ok': True, 'required': False}


@router.delete('/api/auth/session')
async def sign_out(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path='/')
    return {'ok': True}

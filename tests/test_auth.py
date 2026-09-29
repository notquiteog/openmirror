"""The token has to actually stop something.

`OPENMIRROR_TOKEN` guarded four websockets and every HTTP route. Around sixty
routes — the one that creates a session in whatever approval mode the body asks
for, the one that starts an autopilot against a real screen, the one that wipes
your memory, the one that spends money — were open to anything that could reach
the port. An operator who set the token had every reason to believe the daemon
was authenticated.

These tests exist because the first attempt at fixing it was written the obvious
way and did not work. `include_router(..., dependencies=[Depends(...)])` is
exactly how this is supposed to be written; on the FastAPI this ships with, the
dependency is dropped and the endpoint runs anyway. It passed a casual glance
and would have shipped as a guard that guards nothing. So these tests are
written against the *routes*, not against the guard, and the sample is
deliberately weighted towards the ones where "unauthenticated" means something
expensive.

A guard that a test cannot fail is not a guard.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from openmirror.config import config
from openmirror.routers.auth import TokenMiddleware

TOKEN = 'test-token-3f8a2c'


@pytest.fixture
def guarded(monkeypatch):
    """A small app with the same middleware, standing in for the real one.

    The middleware is what is under test, and it does not need sixty real
    routes to prove it. What it does need is *diverse* routes, because the
    failure being guarded against was specific: certain shapes of route were
    being skipped. So the set below is chosen to vary every axis the guard could
    plausibly get wrong.
    """
    from fastapi import APIRouter, FastAPI

    from openmirror.routers import auth as auth_router

    monkeypatch.setattr(config, 'auth_token', TOKEN)

    app = FastAPI()

    @app.get('/healthz')
    async def healthz():
        return {'ok': True}

    @app.get('/')
    async def root():
        return {'shell': True}

    @app.websocket('/ws/agent')
    async def ws_agent(websocket):
        await websocket.accept()
        await websocket.send_text('hi')
        await websocket.close()

    # An agent session, in whatever approval mode the caller names. Creating
    # one with `unrestricted` is the single most valuable thing to be able to
    # do to somebody, because it means the agent can run commands and spend
    # money without stopping to ask.
    agent = APIRouter(prefix='/api/agent')

    @agent.post('/sessions')
    async def create_session(body: dict):
        return {'mode': body.get('mode')}

    @agent.delete('/sessions/{sid}')
    async def drop_session(sid: str):
        return {'dropped': sid}

    @agent.post('/run')
    async def run(body: dict):
        return {'ran': True}

    # Memory. `DELETE /api/memory` is the irreversible one.
    memory = APIRouter(prefix='/api/memory')

    @memory.get('')
    async def list_memories():
        return {'memories': ['everything about you']}

    @memory.delete('')
    async def wipe():
        return {'wiped': True}

    @memory.post('/search')
    async def search(body: dict):
        return {'hits': []}

    # The one that spends money.
    media = APIRouter(prefix='/api/media')

    @media.post('/image')
    async def image(body: dict):
        return {'paid': True}

    # No prefix at all, to catch a guard that assumed one.
    @app.get('/unprefixed')
    async def unprefixed():
        return {'ok': True}

    app.include_router(agent)
    app.include_router(memory)
    app.include_router(media)
    # The real sign-in router, because the cookie path is only worth testing
    # end to end: the whole web interface depends on these two routes being the
    # one thing reachable before a token exists.
    app.include_router(auth_router.router)
    app.add_middleware(TokenMiddleware)
    return TestClient(app)


def _get(client, path, **kw):
    return client.get(path, **kw)


# -- the negative cases: nothing gets through without the token --------------


@pytest.mark.parametrize(
    'method,path',
    [
        # The expensive ones, each one named for what it would cost.
        ('POST', '/api/agent/sessions'),  # an unrestricted session
        ('POST', '/api/agent/run'),  # actually run something
        ('DELETE', '/api/memory'),  # irreversible
        ('GET', '/api/memory'),  # a person's entire stored memory
        ('POST', '/api/media/image'),  # money
        ('GET', '/unprefixed'),  # no prefix, the shape most likely to be missed
    ],
)
def test_a_route_is_closed_without_a_token(guarded, method, path):
    response = guarded.request(method, path, json={})
    assert response.status_code == 401, (
        f'{method} {path} ran for someone with no token. This is the bug the '
        f'middleware exists to stop.'
    )


def test_a_refusal_says_where_to_sign_in(guarded):
    """A bare 401 leaves a person with nothing to act on."""
    response = guarded.get('/api/memory')
    assert response.status_code == 401
    body = response.json()
    assert body['login'] == '/api/auth/session'
    assert response.headers['www-authenticate'] == 'Bearer'


def test_a_wrong_token_is_refused(guarded):
    """And refused in constant time, so it cannot be discovered a character at
    a time by anything able to measure the answers."""
    response = guarded.get('/api/memory', headers={'Authorization': f'Bearer {TOKEN[:-1]}x'})
    assert response.status_code == 401


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(guarded):
    """The classic truncation bug: `startswith` instead of a full compare."""
    response = guarded.get('/api/memory', headers={'Authorization': f'Bearer {TOKEN[:4]}'})
    assert response.status_code == 401


def test_a_token_with_the_right_prefix_and_wrong_tail_is_refused(guarded):
    response = guarded.get(
        '/api/memory', headers={'Authorization': f'Bearer x{TOKEN[1:]}'}
    )
    assert response.status_code == 401


def test_an_empty_token_is_not_a_token(guarded):
    response = guarded.get('/api/memory', headers={'Authorization': 'Bearer '})
    assert response.status_code == 401


def test_a_bare_word_is_not_a_bearer_token(guarded):
    """`Authorization: <token>` without the scheme is not a match, rather than
    being treated as one by a lenient parse."""
    response = guarded.get('/api/memory', headers={'Authorization': TOKEN})
    assert response.status_code == 401


# -- the ways a legitimate client presents it --------------------------------


def test_a_bearer_header_opens_the_door(guarded):
    response = guarded.get('/api/memory', headers={'Authorization': f'Bearer {TOKEN}'})
    assert response.status_code == 200


def test_the_dedicated_header_works_too(guarded):
    """curl, an editor, the desktop app — anything that can set a header."""
    response = guarded.get('/api/memory', headers={'X-OpenMirror-Token': TOKEN})
    assert response.status_code == 200


def test_a_query_token_works(guarded):
    """A browser cannot set a header on a websocket, and neither can every
    client that wants to reach one. This has always been the way in."""
    response = guarded.get(f'/api/memory?token={TOKEN}')
    assert response.status_code == 200


def test_a_cookie_opens_the_door(guarded):
    """What the sign-in page sets, and what the whole web interface then rides
    on. If this did not work the app would be unusable, which is why the
    exemption for the shell and the login route is not optional."""
    guarded.cookies.set('openmirror_token', TOKEN)
    try:
        assert guarded.get('/api/memory').status_code == 200
    finally:
        guarded.cookies.clear()


def test_the_query_token_needs_the_value_not_the_key(guarded):
    response = guarded.get('/api/memory?token=')
    assert response.status_code == 401


# -- what stays open, and why ----------------------------------------------


@pytest.mark.parametrize('path', ['/healthz', '/', '/docs', '/openapi.json'])
def test_a_privileged_request_is_still_reachable(guarded, path):
    """Otherwise there is no way to find out the daemon is up, or to load the
    page on which a token is typed."""
    assert guarded.get(path).status_code == 200


def _drive(path: str, query: str = b'', kind: str = 'websocket'):
    """Send one request through the middleware and return what it emitted.

    Hand-rolled rather than going through `TestClient`, because
    `TestClient.websocket_connect` raises on a *successful* handshake in
    starlette 1.6.0 — a plain app with no middleware fails it the same way, so
    it is the harness and not the guard. Testing a guard through a client that
    cannot open a connection proves nothing either way.
    """
    out: list[dict] = []

    async def downstream(_scope, _receive, send):
        await send({'type': f'{kind}.accept'})
        if kind == 'websocket':
            await send({'type': 'websocket.send', 'text': 'hi'})

    async def send(message):
        out.append(message)

    async def receive():
        return {'type': f'{kind}.connect'}

    scope = {'type': kind, 'path': path, 'query_string': query, 'headers': []}
    asyncio.run(TokenMiddleware(downstream)(scope, receive, send))
    return out


def test_a_websocket_is_refused_before_it_is_accepted(guarded):
    """A websocket cannot be answered with a 401. One that is accepted and then
    silently ignored looks like a hang, so it is closed with a policy-violation
    code instead — and crucially *before* the accept, so the client learns
    immediately that it is not allowed in rather than sitting on a connection
    that will never speak.
    """
    assert _drive('/ws/agent') == [{'type': 'websocket.close', 'code': 1008}]


def test_a_websocket_with_a_token_is_accepted(guarded):
    assert _drive('/ws/agent', b'token=' + TOKEN.encode())[0]['type'] == 'websocket.accept'


# -- the real application, not a stand-in -----------------------------------


def _real_routes():
    """Every route on the shipped `app`, flattened.

    FastAPI 0.141 keeps included routers as `_IncludedRouter` wrappers rather
    than splicing their routes into `app.routes`, so a naive
    `[r for r in app.routes]` sees two API routes and would cheerfully pass a
    test that proved nothing. The walk is here so that stays fixed.
    """
    from fastapi.routing import APIRoute, APIWebSocketRoute

    from openmirror.main import app as real

    found: list[tuple[str, str]] = []

    def walk(routes):
        for route in routes:
            kind = type(route).__name__
            if kind == '_IncludedRouter':
                walk(route.original_router.routes)
            elif isinstance(route, APIWebSocketRoute):
                found.append(('WS', route.path))
            elif isinstance(route, APIRoute):
                for method in route.methods:
                    found.append((method, route.path))

    walk(real.routes)
    return found


def test_every_real_route_is_behind_the_middleware(monkeypatch):
    """The fixture app above proves the middleware works. This proves it is
    *fitted to the thing that ships* — the failure mode being guarded against
    was never "the guard is broken", it was "the guard is not actually attached
    to the routes", which a self-contained fixture cannot detect.

    The expected set is written out literally rather than derived from
    `_is_public`. Asking the guard to agree with itself is circular: widening
    the exemption list would move both sides at once and this would still pass,
    which is exactly what happened the first time it was written that way.
    """
    monkeypatch.setattr(config, 'auth_token', TOKEN)

    routes = _real_routes()
    assert len(routes) > 40, f'only found {len(routes)} routes; the walk is broken'

    # Nothing that is not one of these may be reachable without a token.
    # `/` and `/static` are the app shell, `/healthz` is how you find out the
    # daemon is up, `/api/auth/session` is the route that *takes* the token,
    # and `/mcp` keeps its own stricter check.
    #
    # `/static` is a mount rather than a route, so it is absent from this table
    # and is not asserted on; the docs routes are left out for the same reason
    # — FastAPI decides whether to serve them at start-up, and they describe
    # routes rather than invoking any of them.
    allowed = {'/', '/healthz', '/api/auth/session', '/mcp', '/mcp/'}
    missing = allowed - {p for _, p in routes}
    assert not missing, f'an expected exemption is gone: {sorted(missing)}'

    for method, path in routes:
        if path in allowed:
            continue
        assert _refused_by_the_middleware(method, path), f'{method} {path} is reachable without a token'


def _refused_by_the_middleware(method: str, path: str) -> bool:
    """Is this concrete request turned away before any handler sees it?

    Handed the middleware on its own rather than the whole app, because a
    refusal is a refusal whether or not it then matches a route — and because
    the downstream here raises if it is ever reached, which is what turns "the
    middleware said no" into a fact rather than a hope.
    """
    import re

    concrete = re.sub(r'\{[^}]+\}', 'x', path)
    out: list[dict] = []

    async def send(message):
        out.append(message)

    async def receive():
        return {'type': 'http.request', 'body': b'{}', 'more_body': False}

    scope = {
        'type': 'http',
        'method': method,
        'path': concrete,
        'query_string': b'',
        'headers': [],
    }
    asyncio.run(TokenMiddleware(_never_runs)(scope, receive, send))
    return bool(out) and out[0]['type'] == 'http.response.start' and out[0]['status'] == 401


async def _never_runs(scope, receive, send):  # pragma: no cover - must not run
    raise AssertionError(f'a handler ran for an unauthenticated {scope["path"]}')


def test_a_real_destructive_route_is_refused_without_a_token(monkeypatch):
    """The end-to-end version, on the shipped app, for the three routes where
    "unauthenticated" is most expensive: an unrestricted session, an autopilot
    against a real screen, and the memory wipe.
    """
    from starlette.testclient import TestClient

    from openmirror.main import app as real

    monkeypatch.setattr(config, 'auth_token', TOKEN)
    client = TestClient(real)

    # These would 422/503/500 if they reached a handler; what matters is that
    # none of them is anything other than a 401.
    attempts = [
        ('POST', '/api/agent/sessions', {'mode': 'unrestricted', 'approval': 'unrestricted'}),
        ('POST', '/api/autopilot', {'enabled': True}),
        ('DELETE', '/api/memory', None),
    ]
    for method, path, body in attempts:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, f'{method} {path} answered {response.status_code}'

    # And the whole thing opens with the right token.
    assert client.get('/api/memory', headers={'Authorization': f'Bearer {TOKEN}'}).status_code != 401


def test_a_real_websocket_is_refused_without_a_token(monkeypatch):
    from openmirror.main import app as real

    monkeypatch.setattr(config, 'auth_token', TOKEN)

    out: list[dict] = []

    async def send(message):
        out.append(message)

    async def receive():
        return {'type': 'websocket.connect'}

    scope = {'type': 'websocket', 'path': '/ws/autopilot', 'query_string': b'', 'headers': []}
    asyncio.run(real(scope, receive, send))
    assert out == [{'type': 'websocket.close', 'code': 1008}]


# -- the default install ----------------------------------------------------


def test_no_token_means_no_gate():
    """Loopback with no token stays open, and that is a decision rather than an
    oversight: the default install is `127.0.0.1`, and requiring a token to use a
    local app on your own machine is the kind of friction that gets worked
    around by turning the server off."""
    from fastapi import FastAPI

    app = FastAPI()
    app.add_middleware(TokenMiddleware)

    @app.get('/api/anything')
    async def anything():
        return {'ok': True}

    assert TestClient(app).get('/api/anything').status_code == 200


# -- the exchange itself ----------------------------------------------------


def test_the_login_route_is_reachable_without_a_token(guarded):
    """It is the route that *takes* the token, so guarding it with the token
    would be circular."""
    assert guarded.post('/api/auth/session', json={}).status_code == 401  # wrong token
    assert guarded.post('/api/auth/session', json={'token': TOKEN}).status_code == 200


def test_a_correct_sign_in_sets_a_cookie_guarded_cannot_read(guarded):
    response = guarded.post('/api/auth/session', json={'token': TOKEN})
    cookie = response.cookies.get('openmirror_token')
    assert cookie == TOKEN
    raw = response.headers['set-cookie'].lower()
    assert 'httponly' in raw  # not readable from the page's JavaScript
    assert 'samesite=strict' in raw  # does not ride along cross-site


def test_signing_in_with_the_wrong_token_sets_nothing(guarded):
    response = guarded.post('/api/auth/session', json={'token': 'nope'})
    assert response.status_code == 401
    assert 'openmirror_token' not in response.cookies


def test_signing_out_clears_the_cookie(guarded):
    guarded.post('/api/auth/session', json={'token': TOKEN})
    response = guarded.delete('/api/auth/session')
    assert response.status_code == 200
    assert 'openmirror_token=' in response.headers.get('set-cookie', '')

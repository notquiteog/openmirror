"""The HTTP surface for search and export.

Three routes, and what the tests are for is mostly *where* they are rather than
what they return. A search that works but is mounted outside the token is worse
than no search at all: a transcript is a person's whole working day, and
"what did I ask about the parser" answers with the contents of the files that
search read. So one of these is a guard test, in the spirit of `test_auth.py` —
written against the route, not against the middleware.

The rest is the shape of a response, and one thing that is easy to get wrong
with the HTML download: `Content-Disposition` is a header value and the title of
a conversation is text somebody typed. A quote in it closes the attribute and a
newline in it splits the response, so the file name is built from a sanitised
title and the session id is always in it.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from openmirror.agent.manager import manager
from openmirror.config import config
from openmirror.routers.auth import TokenMiddleware
from openmirror.routers.history import router
from openmirror.sessions import SessionStore, Stored

TOKEN = 'test-token-7c41be'


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The process-wide transcript store, pointed at a temporary directory.

    Set on the manager rather than passed in, because the routes are supposed to
    reach the store the way every other route does — a test that injected one
    would pass while the real route reached a different directory.
    """
    made = SessionStore(tmp_path / 'sessions')
    monkeypatch.setattr(manager, '_store', made)
    return made


@pytest.fixture
def client(store, monkeypatch):
    """A small app with this router and the same middleware the real one has.

    The token is cleared so the guard tests are the ones that decide whether a
    token is enforced, rather than a token somebody happened to export into
    `.env` for this machine.
    """
    from fastapi import FastAPI

    monkeypatch.setattr(config, 'auth_token', '')
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(TokenMiddleware)
    return TestClient(app)


def write(store: SessionStore, session_id: str, turns, *, title='', root='/tmp/proj') -> None:
    messages = [
        {'role': 'user' if number % 2 == 0 else 'assistant',
         'content': [{'type': 'text', 'text': turn}]}
        for number, turn in enumerate(turns)
    ]
    store.save(Stored(id=session_id, title=title, root=root, model='gpt-4o', messages=messages))


@pytest.fixture
def conversations(store):
    write(store, 'aaa111', ['add a retry to the uploader'], title='Uploader work')
    write(store, 'bbb222', ['rename the sidebar'], title='Chrome')
    return store


# --- search --------------------------------------------------------------------


def test_search_returns_rows_and_a_count(client, conversations):
    got = client.get('/api/sessions/search', params={'q': 'uploader'})
    assert got.status_code == 200
    body = got.json()
    assert body['count'] == 1 == len(body['results'])
    row = body['results'][0]
    assert row['id'] == 'aaa111' and row['title'] == 'Uploader work'
    assert {'id', 'title', 'root', 'created', 'updated', 'role', 'message_index', 'excerpt', 'spans'} <= set(row)


def test_search_does_not_explain_itself(client, conversations):
    """Not `<mark>` and not anything else a client would have to trust. The
    match is a span in the string it was given, so a client that renders the
    text its own way still highlights the right characters."""
    row = client.get('/api/sessions/search', params={'q': 'retry'}).json()['results'][0]
    assert '<' not in row['excerpt']
    for begin, stop in row['spans']:
        assert row['excerpt'][begin:stop].casefold() == 'retry'


def test_an_empty_query_is_an_empty_list_not_an_error(client, conversations):
    """The caller is a box somebody is typing into: this request arrives before
    there is anything to search for, and a status code for that is a red box on
    an empty field."""
    got = client.get('/api/sessions/search')
    assert got.status_code == 200
    assert got.json() == {'results': [], 'count': 0}


def test_the_limit_is_honoured_and_bounded(client, store):
    for number in range(6):
        write(store, f's{number}', ['a retry in here'])
    assert client.get('/api/sessions/search', params={'q': 'retry', 'limit': 2}).json()['count'] == 2
    assert client.get('/api/sessions/search', params={'q': 'retry', 'limit': 0}).status_code == 422
    assert client.get('/api/sessions/search', params={'q': 'retry', 'limit': 5000}).status_code == 422


def test_the_root_filter_reaches_the_search(client, store, tmp_path):
    write(store, 'aaa111', ['a retry here'], root=str(tmp_path / 'alpha'))
    write(store, 'bbb222', ['a retry there'], root=str(tmp_path / 'beta'))
    got = client.get('/api/sessions/search', params={'q': 'retry', 'root': str(tmp_path / 'alpha')})
    assert [row['id'] for row in got.json()['results']] == ['aaa111']


def test_searching_when_there_is_nowhere_to_search_is_empty(client, monkeypatch):
    """A store that could not be built is a 200 with nothing in it, not a 500.
    The sidebar asks on every keystroke and it has nothing to show either way."""
    monkeypatch.setattr(manager, '_store', None)
    monkeypatch.setattr('openmirror.agent.runtime._STORE', None)
    got = client.get('/api/sessions/search', params={'q': 'anything'})
    assert got.status_code == 200
    assert got.json() == {'results': [], 'count': 0}


# --- the HTML export -----------------------------------------------------------


def test_the_html_export_is_an_attachment_with_a_sensible_name(client, conversations):
    got = client.get('/api/sessions/aaa111/export.html')
    assert got.status_code == 200
    assert got.headers['content-type'].startswith('text/html')
    disposition = got.headers['content-disposition']
    assert disposition.startswith('attachment; filename="')
    assert 'Uploader-work-aaa111.html' in disposition
    assert got.text.startswith('<!doctype html>')


def test_the_html_export_is_self_contained(client, conversations):
    body = client.get('/api/sessions/aaa111/export.html').text
    for forbidden in ('<script', 'src=', '<link', '@import', 'http'):
        assert forbidden not in body, forbidden


def test_a_conversation_containing_a_script_is_escaped_not_executed(client, store):
    write(store, 'aaa111', ['<script>alert("pwned")</script>'], title='Report')
    body = client.get('/api/sessions/aaa111/export.html').text
    assert '<script' not in body
    assert '&lt;script&gt;alert(&quot;pwned&quot;)&lt;/script&gt;' in body


def test_a_title_cannot_break_the_download_header(client, store):
    """A header value is not a place for text somebody typed. A quote closes
    the attribute; a newline splits the response. The name is built from a
    sanitised title, and the id is in it so two conversations called "notes"
    do not fight over one filename."""
    write(store, 'aaa111', ['hello'], title='Fix "the" thing\n\rplease & more')
    got = client.get('/api/sessions/aaa111/export.html')
    assert got.status_code == 200
    disposition = got.headers['content-disposition']
    assert '\n' not in disposition and '\r' not in disposition
    assert disposition.count('"') == 2
    assert 'aaa111' in disposition


def test_a_session_with_no_title_is_still_downloadable(client, store):
    write(store, 'aaa111', ['hello'], title='')
    got = client.get('/api/sessions/aaa111/export.html')
    assert got.status_code == 200
    assert 'aaa111.html' in got.headers['content-disposition']


def test_exporting_a_conversation_that_is_not_there_is_a_404(client, conversations):
    assert client.get('/api/sessions/nope/export.html').status_code == 404


# --- the JSON export -----------------------------------------------------------


def test_the_json_export_is_the_whole_stored_session(client, conversations):
    got = client.get('/api/sessions/aaa111/export.json')
    assert got.status_code == 200
    body = got.json()
    assert body['id'] == 'aaa111' and body['title'] == 'Uploader work'
    assert body['root'] == '/tmp/proj' and body['model'] == 'gpt-4o'
    assert body['messages'] == [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'add a retry to the uploader'}]}
    ]


def test_a_json_export_of_nothing_is_a_404(client, conversations):
    assert client.get('/api/sessions/nope/export.json').status_code == 404


# --- the guard -----------------------------------------------------------------


def test_these_routes_are_behind_the_token(client, conversations, monkeypatch):
    """Written against the routes rather than against the middleware, for the
    reason `test_auth.py` is: a guard that is mounted wrong reads as though it
    is mounted. A transcript is somebody's working day, and this is the route
    that reads all of it."""
    monkeypatch.setattr(config, 'auth_token', TOKEN)
    assert client.get('/api/sessions/search', params={'q': 'retry'}).status_code == 401
    assert client.get('/api/sessions/aaa111/export.html').status_code == 401
    assert client.get('/api/sessions/aaa111/export.json').status_code == 401

    offered = {'headers': {'Authorization': f'Bearer {TOKEN}'}}
    assert client.get('/api/sessions/search', params={'q': 'retry'}, **offered).status_code == 200
    assert client.get('/api/sessions/aaa111/export.html', **offered).status_code == 200

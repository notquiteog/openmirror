"""Finding a conversation you can no longer remember having.

`openmirror/sessions.py` writes one append-only JSONL transcript per session,
which means everything said in this daemon is on disk and there was no way to go
and look at it. Both other harnesses can. Somebody who remembers *that* they
fixed the parser last Tuesday, in a session called `openmirror`, had to scroll
a list of forty sessions called `openmirror` and open each one.

So the tests here are mostly about the two ways a search over files goes wrong
that a search over a database does not. **A transcript is a file a running
daemon was appending to**, so a line can be short, a record can be a fragment,
and the realistic damage is a daemon that stopped mid-write — a search that
raised on any of those would break exactly when there is something to find.
And **the HTML export is a document about user content**: a transcript holds
whatever the agent read, so an unescaped `<script>` in one is a script that
runs in whoever opens the file. That case is asserted here rather than reviewed
by eye, because it is the kind of thing that passes a casual glance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openmirror.history import (
    TAIL_BYTES,
    as_json,
    export_html,
    render_html,
    search_sessions,
    searchable_session_ids,
)
from openmirror.sessions import SessionStore, Stored, TranscriptError


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'sessions')


def write(store: SessionStore, session_id: str, turns, *, title='', root='/tmp/proj', unfinished=False) -> Stored:
    """A transcript on disk, the way the store writes one.

    Alternating user and assistant messages, because that is the shape a real
    transcript has and a test that only ever wrote user messages would never
    notice a role being read wrong.
    """
    messages = []
    for number, turn in enumerate(turns):
        if isinstance(turn, tuple):
            said, who = turn
        else:
            said, who = turn, 'user' if number % 2 == 0 else 'assistant'
        messages.append({'role': who, 'content': [{'type': 'text', 'text': said}]})
    found = Stored(id=session_id, title=title, root=root, model='gpt-4o', messages=messages,
                   unfinished=unfinished)
    store.save(found)
    return found


def find(store, query, **over):
    return search_sessions(query, store=store, **over)


# --- finding one ---------------------------------------------------------------


def test_a_word_in_the_first_message_finds_the_conversation(store):
    write(store, 'aaa111', ['add a retry to the uploader'])
    write(store, 'bbb222', ['rename the sidebar'])
    found = find(store, 'uploader')
    assert [row['id'] for row in found] == ['aaa111']


def test_a_row_carries_what_a_list_needs_to_render_and_link(store):
    """Everything a UI row shows and everything it needs to jump: without the
    index there is nothing to click, and without the excerpt there is nothing
    to show."""
    write(store, 'aaa111', ['add a retry to the uploader'], title='Uploader work')
    row = find(store, 'retry')[0]
    assert row['id'] == 'aaa111'
    assert row['title'] == 'Uploader work'
    assert row['root'] == '/tmp/proj'
    assert row['updated'] > 0 and row['created'] > 0
    assert row['role'] == 'user'
    assert row['message_index'] == 0
    assert 'retry' in row['excerpt']
    assert row['unfinished'] is False


def test_the_message_index_is_the_one_a_client_can_jump_to(store):
    """It indexes `Stored.messages`, so it has to be counted from the start of
    the file — including past messages the tail window never saw, which is
    exactly where a shortcut that counts within the window goes wrong."""
    write(store, 'aaa111', ['first', 'second', 'third', 'a note about retries'])
    row = find(store, 'retries')[0]
    assert row['message_index'] == 3
    assert store.load('aaa111').messages[3]['content'][0]['text'] == 'a note about retries'


def test_the_match_is_marked_with_offsets_into_the_string_that_was_returned(store):
    """Offsets, not markup, and computed on the string that ships — computing
    them before the whitespace collapse is the bug that lands every highlight
    in the wrong place, silently."""
    write(store, 'aaa111', ['please add\na retry to the uploader today'])
    row = find(store, 'retry')[0]
    excerpt, spans = row['excerpt'], row['spans']
    assert '\n' not in excerpt, 'a sidebar row is one line'
    assert spans and all(0 <= begin < stop <= len(excerpt) for begin, stop in spans)
    for begin, stop in spans:
        assert excerpt[begin:stop].casefold() == 'retry'


def test_the_excerpt_is_a_window_around_the_match_not_the_whole_message(store):
    write(store, 'aaa111', ['x' * 900 + ' the thing about retries ' + 'y' * 900])
    row = find(store, 'retries')[0]
    assert row['excerpt'].startswith('…') and row['excerpt'].endswith('…')
    assert len(row['excerpt']) < 400
    for begin, stop in row['spans']:
        assert row['excerpt'][begin:stop].casefold() == 'retries'


def test_an_assistant_message_is_searchable_too(store):
    write(store, 'aaa111', ['what now?', 'I rewrote the uploader with a backoff'])
    row = find(store, 'backoff')[0]
    assert row['role'] == 'assistant' and row['message_index'] == 1


def test_tool_output_is_not_searched(store):
    """A transcript is mostly tool results, which are the agent's own copy of
    the files it read. Matching them would make a query for any word in the
    repository return every session that ever opened a file."""
    found = Stored(id='aaa111', title='', root='/tmp/proj', model='m', messages=[
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'is_error': False,
                                      'content': 'def parse(text): # retry me'}]},
    ])
    store.save(found)
    assert find(store, 'retry') == []


# --- matching rules ------------------------------------------------------------


def test_matching_ignores_case(store):
    write(store, 'aaa111', ['add a RETRY to the Uploader'])
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']
    assert [row['id'] for row in find(store, 'RETRY uploader')] == ['aaa111']


def test_several_words_are_an_and_not_an_or(store):
    """An OR would fill the first page with sessions that matched one word
    trivially, and a session list is a short list."""
    write(store, 'aaa111', ['add a retry to the uploader'])
    write(store, 'bbb222', ['add a retry to the parser'])
    assert [row['id'] for row in find(store, 'retry uploader')] == ['aaa111']
    assert [row['id'] for row in find(store, 'retry')] == ['bbb222', 'aaa111']


def test_the_title_is_part_of_what_is_searched(store):
    """A title is what a person wrote about the conversation, so it is the
    strongest signal there is — and the only one left for a session whose match
    has fallen out of the tail window."""
    write(store, 'aaa111', ['some first request'], title='Retry policy')
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


def test_a_term_in_the_title_combines_with_one_in_the_message(store):
    write(store, 'aaa111', ['please add a retry'], title='Uploader')
    assert [row['id'] for row in find(store, 'uploader retry')] == ['aaa111']


def test_the_best_match_comes_first(store):
    """Ranked by how well the query matched, and only then by recency — a
    session whose title has nothing to do with the query must not outrank one
    that is about it."""
    write(store, 'old-title', ['the retry logic'], title='Unrelated')
    write(store, 'on-topic', ['the retry logic in the uploader'], title='Retry work')
    assert [row['id'] for row in find(store, 'retry')] == ['on-topic', 'old-title']


def test_one_row_per_conversation_however_many_messages_matched(store):
    write(store, 'aaa111', ['a retry', 'another retry', 'a third retry'])
    assert len(find(store, 'retry')) == 1


def test_an_empty_query_finds_nothing_rather_than_everything(store):
    """The caller is a box somebody is typing into, so this request arrives
    before there is anything to search for."""
    write(store, 'aaa111', ['a retry'])
    assert find(store, '') == []
    assert find(store, '   ') == []


def test_a_missing_directory_is_an_empty_list_not_an_error(tmp_path):
    """The state a search meets after a restart with the data directory
    removed, and the state a store that could not be built is in."""
    store = SessionStore(tmp_path / 'sessions')
    assert store.root.is_dir()
    store.root.rmdir()
    assert search_sessions('anything', store=store) == []
    assert search_sessions('anything', store=object()) == []


# --- narrowing -----------------------------------------------------------------


def test_the_root_filter_narrows_to_one_project(store, tmp_path):
    write(store, 'aaa111', ['a retry'], root=str(tmp_path / 'alpha'))
    write(store, 'bbb222', ['a retry'], root=str(tmp_path / 'beta'))
    found = find(store, 'retry', root=tmp_path / 'alpha')
    assert [row['id'] for row in found] == ['aaa111']


def test_a_session_in_a_subdirectory_of_the_root_counts(store, tmp_path):
    """A session in `~/proj/api` is unambiguously part of `~/proj`. A sibling
    worktree is not, which is correct — it is a different tree."""
    write(store, 'aaa111', ['a retry'], root=str(tmp_path / 'alpha' / 'api'))
    write(store, 'bbb222', ['a retry'], root=str(tmp_path / 'beta'))
    assert [row['id'] for row in find(store, 'retry', root=tmp_path / 'alpha')] == ['aaa111']


def test_a_session_with_no_recorded_root_is_excluded_rather_than_guessed_at(store, tmp_path):
    write(store, 'aaa111', ['a retry'], root='')
    assert find(store, 'retry', root=tmp_path) == []


def test_the_limit_caps_the_rows(store):
    for number in range(12):
        write(store, f's{number:03d}', ['a retry in here'], title=f'Retry {number}')
    assert len(find(store, 'retry', limit=4)) == 4
    assert len(find(store, 'retry', limit=1)) == 1
    assert len(find(store, 'retry')) == 12


# --- a file on disk is not a file you can trust --------------------------------


def test_a_corrupt_line_is_skipped_and_the_rest_of_the_conversation_survives(store):
    """The realistic damage is a daemon that stopped mid-write. A search that
    raised on the short line would throw away every turn before it."""
    write(store, 'aaa111', ['a retry here'])
    path = store.path_for('aaa111')
    with path.open('a', encoding='utf-8') as handle:
        handle.write('{"type": "message", "role": "user", "content": [{"type": "text", "te')
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


def test_a_line_that_is_not_json_at_all_is_skipped(store):
    write(store, 'aaa111', ['a retry here'])
    with store.path_for('aaa111').open('a', encoding='utf-8') as handle:
        handle.write('not json at all\n')
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


def test_a_file_that_is_not_a_transcript_at_all_is_skipped(store, tmp_path):
    (store.root / 'garbage.json').write_text('this is not a transcript', encoding='utf-8')
    write(store, 'aaa111', ['a retry here'])
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


def test_an_unreadable_file_does_not_cost_the_others_their_results(store, monkeypatch):
    write(store, 'aaa111', ['a retry here'])
    write(store, 'bbb222', ['a retry there'])

    real_open = Path.open

    def refuse(self, *args, **kwargs):
        if self.name == 'bbb222.json':
            raise OSError('permission denied')
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', refuse)
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


def test_a_truncated_header_still_leaves_the_session_findable_by_message(store):
    """A transcript is a file that outlives the code that wrote it, so the
    header is looked for rather than assumed to be line one."""
    write(store, 'aaa111', ['a retry here'], title='Retry work')
    path = store.path_for('aaa111')
    lines = path.read_text(encoding='utf-8').splitlines()
    path.write_text('\n'.join(['{}'] + lines[1:]) + '\n', encoding='utf-8')
    found = find(store, 'retry')
    assert [row['id'] for row in found] == ['aaa111']
    assert found[0]['title'] == '', 'no header, so no title to claim'


# --- the bound ----------------------------------------------------------------


def test_only_the_tail_of_a_transcript_is_read(store, monkeypatch):
    """The bound is a decision, so it is pinned: a match in the last 64 kB is
    found, and a sweep reads a fixed amount per session rather than whatever
    the conversation grew to."""
    assert TAIL_BYTES == 64 * 1024
    write(store, 'aaa111', ['a retry right at the end' + 'x' * 200])
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


def test_a_match_older_than_the_window_is_not_found_and_is_not_a_crash(store):
    """Stated here because it is the cost of the bound, and a bound nobody
    knows about is a bug report waiting to be filed. A conversation that long
    is found by its title, which is the other thing the transcript knows. The
    padding is many small messages, not one huge one, because the realistic
    long conversation is a long one and not a single enormous record."""
    padding = ['filler text about nothing in particular'] * 2000
    write(store, 'aaa111', ['a very old mention of a retry', *padding], title='Uploader')
    assert store.path_for('aaa111').stat().st_size > TAIL_BYTES
    assert find(store, 'retry') == []
    assert [row['id'] for row in find(store, 'uploader')] == ['aaa111']


def test_a_single_record_larger_than_the_window_is_skipped_rather_than_half_read(store):
    """Half a line is not half a message, and an excerpt from one cannot be
    found in the file by the person reading it. `TAIL_BYTES` is set above the
    largest record the store writes so this does not happen in practice."""
    write(store, 'aaa111', ['z' * (TAIL_BYTES * 2), 'a retry after it'])
    assert [row['id'] for row in find(store, 'retry')] == ['aaa111']


# --- the ids -------------------------------------------------------------------


def test_every_conversation_on_disk_is_listed(store, tmp_path):
    write(store, 'aaa111', ['one'])
    write(store, 'bbb222', ['two'])
    assert searchable_session_ids(store=store) == ['aaa111', 'bbb222']
    # The index is not one of the conversations.
    assert 'index' not in searchable_session_ids(store=store)


# --- the export ----------------------------------------------------------------


def test_the_export_is_one_file_with_nothing_to_load(store):
    """A shared conversation has to open on a machine with no network: no
    stylesheet link, no script, no font, no image."""
    write(store, 'aaa111', ['a retry here'], title='Retry work')
    page = export_html('aaa111', store=store)
    assert page.startswith('<!doctype html>') and page.rstrip().endswith('</html>')
    for forbidden in ('<script', 'http://', 'https://', '<link', '@import', 'src='):
        assert forbidden not in page, forbidden


def test_the_export_follows_the_system_colour_scheme(store):
    write(store, 'aaa111', ['a retry here'])
    page = export_html('aaa111', store=store)
    assert 'prefers-color-scheme:dark' in page.replace(' ', '')
    assert 'color-scheme:light dark' in page


def test_the_transcript_is_in_the_export_including_the_tool_calls(store):
    """Kept, as `to_markdown` keeps them: "the agent ran a command and here is
    the conversation without it" is a document that misleads."""
    found = Stored(id='aaa111', title='Work', root='/tmp/proj', model='m', messages=[
        {'role': 'user', 'content': [{'type': 'text', 'text': 'run the tests'}]},
        {'role': 'assistant', 'content': [
            {'type': 'thinking', 'text': 'the suite is slow'},
            {'type': 'tool_use', 'id': 't1', 'name': 'shell', 'input': {'command': 'pytest -q'}},
        ]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'is_error': False,
                                      'content': '41 passed'}]},
    ])
    store.save(found)
    page = export_html('aaa111', store=store)
    assert 'run the tests' in page
    assert 'the suite is slow' in page
    assert 'pytest -q' in page
    assert '41 passed' in page


def test_a_conversation_containing_a_script_is_escaped_not_executed(store):
    """The one that matters. A transcript is whatever the agent read — source
    files, build logs, somebody else's email — so an unescaped tag in one is a
    tag that runs in whoever opens the file."""
    hostile = '<script>alert("pwned")</script> & <img src=x onerror=alert(1)>'
    write(store, 'aaa111', [hostile], title=f'Fix {hostile} please')
    page = export_html('aaa111', store=store)
    # No tag survives anywhere, in the body or in the head. The escaped form of
    # the payload is still there, which is the point: it is shown, not run.
    assert '<script' not in page
    assert '<img' not in page
    assert '&lt;script&gt;alert(&quot;pwned&quot;)&lt;/script&gt;' in page
    assert '&amp;' in page and '&lt;img src=x onerror=alert(1)&gt;' in page


def test_a_title_cannot_close_the_attribute_it_is_in(store):
    """`quote=True` is the part of `escape` that is easy to leave out, and
    without it a quote in a title turns the rest of the head into markup."""
    write(store, 'aaa111', ['a retry here'], title='he said "hello" & left')
    page = export_html('aaa111', store=store)
    assert '<meta name="description" content="he said &quot;hello&quot; &amp; left">' in page
    assert '<title>he said &quot;hello&quot; &amp; left</title>' in page


def test_nothing_in_the_page_is_exempt_from_escaping(store):
    """Every field, not the ones that are obviously text. The working root is a
    path the caller of `POST /api/sessions` chose, and a tool name comes from a
    model — a document about untrusted text cannot have an exempt field, or the
    exemption is the hole."""
    found = Stored(id='aaa111', title='Work', root='/<script>alert(1)</script>', model='m', messages=[
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': '</summary><script>alert(2)</script>',
             'input': {'path': '<img src=x onerror=alert(3)>'}},
        ]},
    ])
    store.save(found)
    page = export_html('aaa111', store=store)
    assert '<script' not in page and '<img' not in page
    assert '&lt;/summary&gt;&lt;script&gt;alert(2)&lt;/script&gt;' in page
    assert '&lt;img src=x onerror=alert(3)&gt;' in page


def test_an_explicit_title_overrides_the_stored_one(store):
    write(store, 'aaa111', ['a retry here'], title='stored')
    assert '<h1>mine</h1>' in export_html('aaa111', title='mine', store=store)


def test_a_conversation_with_no_title_is_named_after_itself(store):
    write(store, 'aaa111', ['a retry here'], title='')
    assert '<h1>aaa111</h1>' in export_html('aaa111', store=store)


def test_a_session_that_was_interrupted_says_so(store):
    """A transcript does not contain a Future. Saying so is better than letting
    a reader think the turn finished."""
    write(store, 'aaa111', ['a retry here'], unfinished=True)
    assert 'stopped while a turn was running' in export_html('aaa111', store=store)


def test_exporting_a_conversation_that_is_not_there_is_an_error(store):
    with pytest.raises(TranscriptError):
        export_html('nope', store=store)


def test_render_html_does_not_need_the_store(store):
    """Split from `export_html` so a caller that has just loaded a session does
    not have to go back to the store to render it."""
    assert '<h1>Work</h1>' in render_html(
        Stored(id='aaa111', title='Work', root='/tmp/proj', model='m',
               messages=[{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}])
    )


def test_the_json_export_is_exactly_what_the_store_holds(store):
    """By construction, so the two cannot drift: what a client round-trips
    through this route is a session the store can load again. The raw encoded
    blocks are in it, which is the point — this is the transcript itself, not a
    rendering of it."""
    write(store, 'aaa111', ['a retry here'], title='Retry work')
    body = as_json(store.load('aaa111'))
    assert set(body) == {
        'id', 'title', 'root', 'model', 'policy', 'toolset', 'messages', 'created', 'updated', 'unfinished',
    }
    assert body['id'] == 'aaa111' and body['title'] == 'Retry work'
    assert body['messages'] == [{'role': 'user', 'content': [{'type': 'text', 'text': 'a retry here'}]}]

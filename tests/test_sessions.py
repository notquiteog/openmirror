"""Conversations that survive a restart.

This is the feature whose absence was the worst of them. `SessionManager` held
`AgentSession` objects in a dict, so a restart lost every conversation — for a
thing whose whole pitch is being a long-running digital twin, that is not a
missing feature but a broken promise.

**What is stored, and what deliberately is not.** The conversation and its
metadata. Not the tools, not the provider, not the approval futures — a
transcript that tried to serialise a live provider client would be a file only
the process that wrote it could read. Reopening is therefore *rebuild then
replay*, and the tests here check the replay rather than the serialisation,
because the serialisation is the easy half.

**What is lost, and says so.** The live state of a turn that was running: an
in-flight tool call, a suspended approval, the queue. A transcript does not
contain a Future. A session interrupted mid-turn comes back to *just before*
that turn with a note in the transcript, because the alternative is restoring
a turn that was half-done and letting the model believe it finished.
"""

from __future__ import annotations

import json

import pytest

from openmirror.providers.base import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openmirror.sessions import SessionStore, Stored, TranscriptError, decode_block, encode_block, to_markdown


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / 'sessions')


def stored(messages=None, **over) -> Stored:
    found = Stored(id='abc123', title='Parser work', root='/tmp/proj', model='gpt-4o')
    found.messages = messages if messages is not None else [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'add a retry'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'added a backoff'}]},
    ]
    for key, value in over.items():
        setattr(found, key, value)
    return found


# --- blocks, both ways --------------------------------------------------------


def test_every_kind_of_block_survives_a_round_trip():
    """Each kind is a different class with different fields, and a transcript
    that quietly drops thinking or tool results is a transcript that reads as
    though the agent never thought."""
    blocks = [
        TextBlock(text='plain'),
        ThinkingBlock(text='because'),
        ToolUseBlock(id='t1', name='edit_file', input={'path': 'a.py'}),
        ToolResultBlock(tool_use_id='t1', content='ok', is_error=False),
        ToolResultBlock(tool_use_id='t2', content='broke', is_error=True),
    ]
    for block in blocks:
        back = decode_block(encode_block(block))
        assert type(back) is type(block), block
        if hasattr(block, 'text'):
            assert back.text == block.text
        if isinstance(block, ToolUseBlock):
            assert (back.id, back.name, back.input) == (block.id, block.name, block.input)
        if isinstance(block, ToolResultBlock):
            assert (back.content, back.is_error) == (block.content, block.is_error)


def test_a_picture_is_described_rather_than_stored():
    """A screenshot's base64 is a hundred thousand characters and a transcript
    that cannot be opened is not a transcript. What is kept is the fact that
    there was one, which is what reading a transcript back is for."""
    back = decode_block(encode_block(ImageBlock(data='A' * 200_000, media_type='image/png')))
    assert isinstance(back, TextBlock)
    assert 'image' in back.text and 'kB' in back.text


def test_a_huge_tool_result_is_kept_at_the_start_and_says_what_is_missing():
    """Kept whole up to a point and then noted, because a transcript whose
    middle has been dropped *silently* is worse than one that says where it
    went."""
    out = encode_block(ToolResultBlock(tool_use_id='t1', content='x' * 50_000))
    assert 'omitted from the transcript' in out['content']
    assert out['content'].startswith('x' * 100)


def test_a_block_from_a_newer_version_is_read_as_text_not_dropped():
    """A transcript outlives the code that wrote it. Dropping a block silently
    loses a turn; showing it as text keeps the conversation readable."""
    back = decode_block({'type': 'something_new', 'note': 'a calendar block'})
    assert isinstance(back, TextBlock) and 'calendar' in back.text


# --- the store ----------------------------------------------------------------


def test_a_conversation_survives_a_new_process(store):
    store.save(stored())
    # A different store object, as a restart would be.
    again = SessionStore(store.root)
    back = again.load('abc123')
    assert back is not None
    assert back.title == 'Parser work'
    assert back.model == 'gpt-4o'
    assert len(back.messages) == 2


def test_replaying_gives_the_session_its_conversation_back(store):
    store.save(stored())
    reopened = stored().restore()
    assert [b.text for m in reopened for b in m.content] == ['add a retry', 'added a backoff']


def test_the_list_is_newest_first_and_merges_the_directory(store):
    store.save(stored())
    store.save(Stored(id='def456', title='Later'))
    found = store.list()
    assert {row['id'] for row in found} == {'abc123', 'def456'}
    assert found[0]['id'] == 'def456', 'newest first'


def test_a_session_the_index_never_heard_of_is_still_listed(store):
    """An index that can lose sessions is worse than no index, so the
    directory is the source of truth and the index is a convenience."""
    store.save(stored())
    store.index.unlink(missing_ok=True)          # the index is gone
    found = store.list()
    assert [row['id'] for row in found] == ['abc123']

    store.index.write_text(json.dumps({'abc123': {'id': 'abc123', 'title': 'stale', 'updated': 1}}))
    found = store.list()
    assert found[0]['title'] == 'Parser work', 'the file wins over a stale index'


def test_a_corrupt_transcript_is_reported_and_not_fatal(store):
    store.path_for('broken').write_text('{ not json')
    assert store.load('broken') is None
    assert store.list() == [] or all(r['id'] != 'broken' for r in store.list())


def test_a_session_id_cannot_escape_the_directory(store):
    """Slugging, not refusing.

    `../../etc/passwd` becomes `etc-passwd` — a safe file inside the store,
    and a perfectly good name. Refusing would just mean somebody writing the
    session by hand instead.
    """
    for attempt in ('../../etc/passwd', '/etc/passwd', 'a/b/c', '....//x'):
        path = store.path_for(attempt)
        assert path.parent == store.root, attempt
        assert path.suffix == '.json'

    # An id with nothing usable in it is refused, because there is no name to
    # make — `.` and `..` are punctuation, not a session.
    for attempt in ('..', '.', '', '///'):
        with pytest.raises(TranscriptError):
            store.path_for(attempt)


def test_deleting_removes_the_file_and_the_index_entry(store):
    store.save(stored())
    assert store.delete('abc123') is True
    assert not store.path_for('abc123').exists()
    assert store.load('abc123') is None
    assert store.delete('abc123') is False


# --- what is lost, and said so -------------------------------------------------


def test_a_transcript_does_not_contain_a_future(store):
    """A turn that was running when the daemon stopped comes back to *just
    before* it, and says so — because the alternative is restoring a
    half-finished turn and letting the model believe it completed."""
    store.save(stored(unfinished=True))
    back = store.load('abc123')
    assert back.unfinished is True
    assert 'The daemon stopped while a turn was running' in to_markdown(back)


def test_a_finished_turn_is_not_marked_unfinished(store):
    store.save(stored(unfinished=False))
    assert store.load('abc123').unfinished is False


# --- export -------------------------------------------------------------------


def test_an_export_keeps_the_tool_calls_rather_than_dropping_them(store):
    """"The agent ran a command and here is the conversation without it" is a
    document that misleads, and the command is usually the most important
    line in it."""
    store.save(stored(messages=[
        {'role': 'user', 'content': [{'type': 'text', 'text': 'fix the build'}]},
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'shell', 'input': {'command': 'npm test'}},
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 't1', 'is_error': False, 'content': '1 failing'},
        ]},
    ]))
    text = to_markdown(store.load('abc123'))
    assert 'shell({"command": "npm test"})' in text
    assert '1 failing' in text
    assert '# Parser work' in text


def test_an_export_of_nothing_is_still_a_document(store):
    assert to_markdown(Stored(id='x', title='Empty')).startswith('# Empty')


# --- the wiring ---------------------------------------------------------------


async def test_a_session_writes_its_transcript_and_reads_it_back(tmp_path):
    """The whole point, through a real session rather than through the store.

    Built twice with the same id, which is what a restart is: the first
    writes, the second — with no live state shared at all — reads.
    """
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone
    from tests.test_agent import ScriptedProvider

    store = SessionStore(tmp_path / 'sessions')
    session = build_session(
        root=str(tmp_path), provider=ScriptedProvider([[StreamDone()]]), model='gpt-4o',
        mode=Mode.ASK, session_id='persisted1', title='Kept', store=store,
    )
    await session.start()
    session.messages = [
        Message(role='user', content=[TextBlock(text='a thing I said')]),
        Message(role='assistant', content=[TextBlock(text='a thing it said')]),
    ]
    await session.close()
    await session.close()          # twice: a close is not a place to throw

    reopened = build_session(
        root=str(tmp_path), provider=ScriptedProvider([[StreamDone()]]), model='x',
        mode=Mode.ASK, session_id='persisted1', store=store,
    )
    await reopened.start()
    assert [b.text for m in reopened.messages for b in m.content] == [
        'a thing I said', 'a thing it said',
    ]
    await reopened.close()


async def test_a_session_with_no_store_simply_does_not_persist(tmp_path):
    """The opt-out. A caller that wants no transcript at all — a test, a
    throwaway session — passes no store and gets none, rather than writing
    files into a temporary directory nobody asked for."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone
    from tests.test_agent import ScriptedProvider

    class Nothing:
        def save(self, _stored):
            raise AssertionError('a session with no store must not write one')

        def load(self, _id):
            return None

    session = build_session(
        root=str(tmp_path), provider=ScriptedProvider([[StreamDone()]]), model='x',
        mode=Mode.ASK, store=Nothing(),
    )
    await session.start()
    await session.close()          # would raise if it tried to save


async def test_a_broken_store_does_not_stop_a_session_opening(tmp_path):
    """Losing a transcript is bad. Failing to open a session because of one is
    worse, and the whole feature is optional."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone
    from tests.test_agent import ScriptedProvider

    class Broken:
        def load(self, _id):
            raise RuntimeError('the disk is on fire')

        def save(self, _stored):
            raise RuntimeError('the disk is still on fire')

    session = build_session(
        root=str(tmp_path), provider=ScriptedProvider([[StreamDone()]]), model='x',
        mode=Mode.ASK, store=Broken(),
    )
    await session.start()          # must not raise
    await session.close()          # and must not raise either


# --- appending rather than rewriting -------------------------------------------


def test_a_second_save_appends_only_what_is_new(tmp_path):
    """Rewriting the whole transcript every turn is a 90ms stall on the event
    loop for a long session, and more time spent copying the conversation than
    working in it. Measured, and it is the one long frame in a clean turn."""
    store = SessionStore(tmp_path / 's')
    first = stored()
    store.save(first)

    grown = stored()
    grown.messages = grown.messages + [{'role': 'user', 'content': [{'type': 'text', 'text': 'more'}]}]
    store.save(grown)

    lines = (tmp_path / 's' / 'abc123.json').read_text().splitlines()
    # One header, the two it already had, and the one that is new — each once.
    assert len(lines) == 4, lines
    assert store.load('abc123').messages == grown.messages


def test_a_session_that_has_not_changed_writes_nothing(tmp_path):
    """So a read-only turn is free."""
    store = SessionStore(tmp_path / 's')
    store.save(stored())
    path = tmp_path / 's' / 'abc123.json'
    before = path.stat().st_mtime_ns
    store.save(stored())
    assert path.stat().st_mtime_ns == before
    assert len(path.read_text().splitlines()) == 3      # a header and two messages


def test_a_line_cut_off_mid_write_does_not_cost_the_turns_before_it(tmp_path):
    """The realistic way to find a short line is a daemon that stopped while
    writing, and a transcript that refused to open because its *last* turn was
    cut off would throw away every turn before it."""
    store = SessionStore(tmp_path / 's')
    store.save(stored())
    path = tmp_path / 's' / 'abc123.json'
    with path.open('a', encoding='utf-8') as handle:
        handle.write('{"type": "message", "role": "user", "conte')     # cut off

    back = store.load('abc123')
    assert back is not None
    assert len(back.messages) == 2, back.messages
    assert back.title == 'Parser work'


async def test_the_write_does_not_happen_on_the_event_loop(tmp_path):
    """89ms of script, once per turn, for work nobody is waiting for."""
    import threading

    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone
    from tests.test_agent import ScriptedProvider

    where: list[str] = []

    class Watched:
        """Wraps a real store and records which thread the write landed on."""

        def __init__(self, inner):
            self.inner = inner

        def load(self, session_id):
            return self.inner.load(session_id)

        def save(self, value):
            where.append(threading.current_thread().name)
            return self.inner.save(value)

    store = Watched(SessionStore(tmp_path / 'sessions'))
    session = build_session(
        root=str(tmp_path), provider=ScriptedProvider([[StreamDone()]]), model='x',
        mode=Mode.ASK, store=store,
    )
    await session.start()
    session.messages = [Message(role='user', content=[TextBlock(text='a thing')])]
    await session.close()
    assert where, 'nothing was written'
    assert all(name != threading.current_thread().name for name in where), where


def test_a_transcript_outlives_a_restart_even_after_a_partial_write(tmp_path):
    """The two properties together, which is what a daemon stopping mid-turn
    actually produces."""
    store = SessionStore(tmp_path / 's')
    kept = stored()
    kept.messages = kept.messages + [{'role': 'user', 'content': [{'type': 'text', 'text': 'second'}]}]
    store.save(kept)
    (tmp_path / 's' / 'abc123.json').open('a', encoding='utf-8').write('{"type":"messa')

    again = SessionStore(tmp_path / 's')
    back = again.load('abc123')
    # The three that were written whole. The fourth line was cut off, and is
    # skipped rather than costing the three before it.
    assert len(back.messages) == 3, back.messages
    # And appending again picks up after what survived, rather than duplicating.
    again.save(back)
    assert len(again.load('abc123').messages) == 3

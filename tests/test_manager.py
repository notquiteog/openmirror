"""Sessions surviving their client, and replaying what it missed."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from openmirror.agent.approval import Mode
from openmirror.agent.manager import SessionManager
from openmirror.protocol.agent import SessionStarted, TextDelta, ToolProposed, TurnCompleted
from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
from tests.test_agent import ScriptedProvider


async def collect(session, since=0, stop=TurnCompleted, timeout=10):  # noqa: ASYNC109 - test helper
    out = []

    async def pump():
        async for event in session.events(since=since):
            out.append(event)
            if isinstance(event, stop):
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return out


@pytest.mark.asyncio
async def test_create_list_and_close():
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK, title='work'
        )
        listed = m.list()
        assert len(listed) == 1
        assert listed[0]['id'] == s.id and listed[0]['title'] == 'work'
        assert not listed[0]['busy'] and listed[0]['waiting_on'] is None

        assert await m.close(s.id)
        assert m.list() == []
        assert not await m.close(s.id)      # closing twice is not an error


@pytest.mark.asyncio
async def test_detaching_does_not_stop_the_work():
    """The turn must run to completion with nobody watching."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp,
            provider=ScriptedProvider([
                [StreamText(text='hello there'), StreamDone()],
            ]),
            model='x',
            mode=Mode.ASK,
        )
        s.submit('hi')
        # Nobody is subscribed at all while this runs.
        await asyncio.wait_for(s._turn, timeout=10)

        assert not s.busy
        replay = await collect(s)
        assert any(isinstance(e, TextDelta) and e.text == 'hello there' for e in replay)


@pytest.mark.asyncio
async def test_reattaching_replays_only_what_was_missed():
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp,
            provider=ScriptedProvider([[StreamText(text='one two three'), StreamDone()]]),
            model='x',
            mode=Mode.ASK,
        )

        # First client sees the session start, then goes away.
        first = []
        async for event in s.events():
            first.append(event)
            if isinstance(event, SessionStarted):
                break
        seen = s.seq

        s.submit('go')
        await asyncio.wait_for(s._turn, timeout=10)

        # Second client asks for everything after what the first saw.
        second = await collect(s, since=seen)
        assert not any(isinstance(e, SessionStarted) for e in second), 'replayed something already seen'
        assert any(isinstance(e, TextDelta) for e in second)

        # And a fresh client with since=0 gets the whole history.
        whole = await collect(s, since=0)
        assert any(isinstance(e, SessionStarted) for e in whole)


@pytest.mark.asyncio
async def test_events_emitted_during_replay_are_not_lost():
    """The gap between replaying the log and reading the queue must be closed."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp,
            provider=ScriptedProvider([[StreamText(text='x'), StreamDone()]]),
            model='x',
            mode=Mode.ASK,
        )

        seen: list = []

        async def slow_reader():
            async for event in s.events():
                seen.append(event)
                # Stall inside the iteration, so more events land mid-replay.
                await asyncio.sleep(0.01)
                if isinstance(event, TurnCompleted):
                    return

        task = asyncio.create_task(slow_reader())
        await asyncio.sleep(0.02)
        s.submit('go')
        await asyncio.wait_for(task, timeout=10)

        seqs = [id(e) for e in seen]
        assert len(seqs) == len(set(seqs)), 'an event was delivered twice'
        assert any(isinstance(e, TextDelta) for e in seen)


@pytest.mark.asyncio
async def test_reaper_spares_a_session_waiting_on_a_human():
    """An approval has no timeout, so a suspended session is not idle."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp,
            provider=ScriptedProvider([
                [StreamToolUse(id='c1', name='shell', input={'command': 'rm -rf x'}), StreamDone()],
                [StreamText(text='ok'), StreamDone()],
            ]),
            model='x',
            mode=Mode.ASK,
        )

        proposed = asyncio.Event()

        async def watch():
            async for event in s.events():
                if isinstance(event, ToolProposed) and event.needs_approval:
                    proposed.set()
                    return

        task = asyncio.create_task(watch())
        s.submit('clean up')
        await asyncio.wait_for(proposed.wait(), timeout=10)
        await task

        assert s.waiting_on == 'approval'

        # Pretend it has been idle for a week.
        s.last_active = 0
        assert await m.reap() == [], 'reaped a session that was waiting for a person'
        assert m.get(s.id) is not None

        s.interrupt()


@pytest.mark.asyncio
async def test_reaper_closes_a_genuinely_idle_session():
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK)
        s.last_active = 0
        assert await m.reap() == [s.id]
        assert m.get(s.id) is None


@pytest.mark.asyncio
async def test_replay_carries_the_user_text():
    """A reattached client must see the question, not only the answer.

    The user's message exists only in the client that typed it unless the
    turn event carries it, which leaves a replayed session showing an agent
    answering something invisible.
    """
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp,
            provider=ScriptedProvider([[StreamText(text='sure'), StreamDone()]]),
            model='x',
            mode=Mode.ASK,
        )
        s.submit('do the thing')
        await asyncio.wait_for(s._turn, timeout=10)

        replay = await collect(s)
        started = [e for e in replay if e.type == 'turn.started']
        assert started and started[0].text == 'do the thing'


@pytest.mark.asyncio
async def test_every_event_is_numbered():
    """Sequence numbers are what make a reconnect cheap, so they must be
    present, increasing, and gapless."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(
            root=tmp,
            provider=ScriptedProvider([[StreamText(text='hi'), StreamDone()]]),
            model='x',
            mode=Mode.ASK,
        )
        s.submit('go')
        await asyncio.wait_for(s._turn, timeout=10)

        seqs = [e.seq for e in await collect(s)]
        assert seqs == list(range(1, len(seqs) + 1)), seqs


# --- the undo history is one session's ---------------------------------------
#
# Found by running it, not by reading it: `openmirror run -c -p /undo` said
# "Nothing to undo" on a session that had just written a file. Two causes, both
# here, and the second one is the worse of the two because nothing about it
# looks wrong from inside the session you are looking at.


@pytest.mark.asyncio
async def test_two_sessions_do_not_share_an_undo_history():
    """The store's directory was keyed on a `session_id` that did not exist yet,
    so it was the literal string 'session' — one directory, one undo history,
    every conversation on the machine. `/undo` here would have put back a turn
    that happened in a different session, about a file this one never touched.
    """
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        one = await m.create(root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK)
        two = await m.create(root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK)
        assert one.checkpoints is not None and two.checkpoints is not None
        assert one.checkpoints.root != two.checkpoints.root, 'one shared directory'
        assert one.checkpoints.undo_count() == 0
        assert two.checkpoints.undo_count() == 0


@pytest.mark.asyncio
async def test_a_sessions_undo_history_is_not_another_sessions():
    """The same bug, from the side that bites: one session's turn must not
    appear in another session's rewind list."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        one = await m.create(root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK)
        two = await m.create(root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK)

        target = Path(tmp) / 'only-mine.txt'
        target.write_text('before\n')
        one.checkpoints.begin('t1', 'my turn')
        one.checkpoints.record(target)
        target.write_text('after\n')
        one.checkpoints.commit()

        assert one.checkpoints.undo_count() == 1
        assert two.checkpoints.undo_count() == 0, 'and it must stay that way'
        assert two.checkpoints.undo_latest() is None
        assert target.read_text() == 'after\n', 'the other session cannot touch it'


@pytest.mark.asyncio
async def test_the_undo_directory_is_named_after_the_session():
    """So a resume finds it. The store is rebuilt from the id, and a directory
    named after something else is a resume that cannot undo."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        s = await m.create(root=tmp, provider=ScriptedProvider([[StreamDone()]]), model='x', mode=Mode.ASK)
        assert s.checkpoints.root.name == s.id


@pytest.mark.asyncio
async def test_a_resumed_session_keeps_its_undo_history():
    """The end of the whole chain: write a file, close, resume, undo."""
    with tempfile.TemporaryDirectory() as tmp:
        m = SessionManager()
        provider = ScriptedProvider([[StreamDone()]] * 4)
        first = await m.create(root=tmp, provider=provider, model='x', mode=Mode.AUTO_EDIT)
        target = Path(tmp) / 'a.txt'
        target.write_text('before\n')
        first.checkpoints.begin('t1', 'edit a')
        first.checkpoints.record(target)
        target.write_text('after\n')
        first.checkpoints.commit()
        assert first.id

        again = await m.create(
            root=tmp, provider=provider, model='x', mode=Mode.AUTO_EDIT, session_id=first.id,
        )
        assert again.id == first.id
        assert again.checkpoints.undo_count() == 1
        assert again.checkpoints.undo_latest() is not None
        assert target.read_text() == 'before\n'

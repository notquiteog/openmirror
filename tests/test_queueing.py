"""A message sent while the agent is working.

Claude Code and openCode both queue it, and both are right to. The moment a
person has something to add is usually *while* the agent is working on the
first half of it — they can see it going the wrong way, or they remember the
thing they meant to say. An error saying "interrupt it first" throws away
what they just typed and makes them watch for a gap to type in.

So:

* **A message sent during a turn is held, and starts the next turn** when this
  one ends. Not refused, not dropped, not merged into the running turn —
  merged would mean the agent changes course mid-answer, which is a different
  feature and a confusing one.
* **One per turn, not a burst.** Three held messages are three turns.
  Draining them all at once would be a session nobody could follow.
* **An interrupt throws the queue away with the turn.** Someone who cancels a
  build does not want the follow-up they typed two minutes ago, and a queue
  that outranks cancel is a queue that runs work somebody threw away.

The tests use a scripted provider that holds a turn open, because the only way
to test queueing is to have something running.
"""

from __future__ import annotations

import asyncio

from openmirror.agent.approval import Mode
from openmirror.agent.runtime import build_session
from openmirror.providers.base import StreamDone, StreamText
from tests.test_agent import ScriptedProvider, drain


class Slow(ScriptedProvider):
    """A provider whose turn takes long enough to queue against.

    Waiting on an event rather than a timer, so the test is not a race
    against a sleep it happens to be faster than.
    """

    def __init__(self, steps: int = 3) -> None:
        super().__init__([[StreamText(text='working'), StreamDone()] for _ in range(steps)])
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def stream(self, request):  # noqa: ANN001 - the provider interface
        self.started.set()
        await self.gate.wait()
        # `ScriptedProvider.stream` is an async generator, so it is walked
        # rather than awaited.
        async for event in super().stream(request):
            yield event


async def settle(session, provider, *, rounds: int = 8) -> list[str]:
    """Run until nothing is running and nothing is waiting.

    `drain` returns at the first `TurnCompleted`, which is emitted *before*
    the queue is looked at, so one pass is not enough — a held message needs
    a second turn and a second drain. Looping is the honest way to say "until
    idle" rather than guessing a sleep.
    """
    said: list[str] = []
    for _ in range(rounds):
        if session._turn is not None:
            provider.gate.set()
            await asyncio.wait_for(drain(session), timeout=10)
        # A turn of the event loop, for the `call_soon` that starts the next.
        await asyncio.sleep(0.02)
        said = [b.text for m in session.messages for b in m.content if hasattr(b, 'text')]
        if not session.queued and not session.busy:
            break
    return said


async def build(provider, root, **over):  # noqa: ANN001
    session = build_session(
        root=str(root), provider=provider, model='x', mode=Mode.TRUSTED, **over
    )
    await session.start()
    return session


async def test_a_message_sent_during_a_turn_is_held_and_then_runs(tmp_path):
    provider = Slow()
    session = await build(provider, tmp_path)

    first = session.submit('do the first thing')
    assert first and not session.queued

    # The turn is running. Send another.
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    second = session.submit('and also the second thing')

    assert second == '', 'a held message reports no turn id'
    assert session.queued == [('and also the second thing', [])]
    assert session.busy, 'and the first turn is still the one running'

    said = await settle(session, provider)

    # Both ran, in order.
    assert said[0] == 'do the first thing'
    assert 'and also the second thing' in said
    assert not session.queued, 'and the queue is empty afterwards'


async def test_held_messages_run_one_turn_at_a_time(tmp_path):
    """Three queued messages are three turns, not one burst.

    Draining them together would make every queued message part of the
    previous turn's task, and a failure in one would take the rest with it.
    """
    provider = Slow(steps=4)
    session = await build(provider, tmp_path)

    session.submit('one')
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    for text in ('two', 'three'):
        session.submit(text)
    assert len(session.queued) == 2

    said = await settle(session, provider)
    assert not session.queued
    # In order. The scripted model answers each one with 'working', so the
    # messages you asked for are the ones that are not that.
    assert [text for text in said if text != 'working'] == ['one', 'two', 'three'], said


async def test_an_interrupt_throws_the_queue_away(tmp_path):
    """Cancelling is cancelling. A queue that outranks it runs work somebody
    discarded, which is worse than losing the queue."""
    provider = Slow()
    session = await build(provider, tmp_path)

    session.submit('the long one')
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    session.submit('and the follow-up')

    from tests.test_agent import idle

    session._turn.cancel()
    # The turn emits its own `turn.completed` and then re-raises, so the
    # event arrives before the task is actually done. Wait for the task
    # rather than for the event: the queue is cleared on the way through the
    # cancellation, and this is what a client watches to know it is over.
    try:
        await session._turn
    except asyncio.CancelledError:
        pass
    await idle(session)

    assert not session.queued, 'the held message went with the turn'
    assert not session.busy


async def test_a_message_typed_with_the_turn_idle_starts_immediately(tmp_path):
    """The ordinary case, so the queue cannot have broken it."""
    provider = ScriptedProvider([[StreamText(text='ok'), StreamDone()]])
    session = await build(provider, tmp_path)
    turn = session.submit('hello')
    assert turn, 'a turn started rather than being held'
    await asyncio.wait_for(drain(session), timeout=10)
    assert not session.queued


async def test_attachments_ride_with_a_held_message(tmp_path):
    """A dropped picture is a dropped picture, and it is the reason somebody
    types the follow-up at all."""
    provider = Slow()
    session = await build(provider, tmp_path)
    session.submit('look at this')
    await asyncio.wait_for(provider.started.wait(), timeout=5)

    session.submit('and this one', [{'type': 'image', 'data': 'AAAA'}])
    assert session.queued[0][1] == [{'type': 'image', 'data': 'AAAA'}]

    said = await settle(session, provider)
    assert 'and this one' in said


async def test_a_held_message_is_not_lost_when_the_session_closes(tmp_path):
    provider = Slow()
    session = await build(provider, tmp_path)
    session.submit('one')
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    session.submit('two')
    await session.close()
    assert not session.queued

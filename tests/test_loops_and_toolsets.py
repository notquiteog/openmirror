"""Two things a live run taught, pinned so they stay taught.

Both came out of pointing a real 12B model at a real browser task. Neither is
a hypothetical: the numbers in the comments are what was measured.
"""

from __future__ import annotations

import asyncio
import tempfile

from openmirror.agent.approval import Mode
from openmirror.agent.runtime import TOOLSETS, build_session, resolve_toolset
from openmirror.agent.session import REPEAT_STOP
from openmirror.agent.tools.base import Assessment, Output, Tool
from openmirror.protocol.agent import Risk, ToolDenied
from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
from tests.test_agent import ScriptedProvider, drain


class Counter(Tool):
    """A read-only tool that returns the same thing every time.

    Which is exactly the shape a confused model gets stuck on: nothing
    changes, so nothing tells it to stop.
    """

    name = 'look'
    description = 'Look at something. Returns the same answer every time.'
    input_schema = {'type': 'object', 'properties': {}}

    def __init__(self):
        self.calls = 0

    def assess(self, args, ctx):
        return Assessment(risk=Risk.READ, summary='look')

    async def run(self, args, ctx):
        self.calls += 1
        return Output(content='the same answer as last time')


def looping(times: int):
    """A model that calls `look` over and over, then gives up."""
    return ScriptedProvider(
        [[StreamToolUse(id=f'c{i}', name='look', input={}), StreamDone(stop_reason='tool_use')]
         for i in range(times)]
        + [[StreamText(text='done'), StreamDone()]]
    )


async def test_a_model_stuck_on_one_call_is_stopped():
    """Measured: thirty identical calls to a tool that reports the screen size,
    59,000 tokens of input, and an answer saying it could not browse. The step
    limit catches that after sixty rounds, which is fifty-five too many.
    """
    tool = Counter()
    with tempfile.TemporaryDirectory() as root:
        session = build_session(
            root=root, provider=looping(20), model='x', mode=Mode.TRUSTED, tools=[tool],
        )
        await session.start()
        session.submit('look at it')
        events = await drain(session)

    assert tool.calls < REPEAT_STOP, (
        f'it ran {tool.calls} times; the guard should have stopped it before {REPEAT_STOP}'
    )

    # Visible to whoever is watching, not only to the model: a turn that goes
    # quiet for no reason is the worst way to report this.
    denials = [e for e in events if isinstance(e, ToolDenied) and 'looping' in e.reason]
    assert denials, 'the client should have been shown why it stopped'


async def test_a_few_repeats_are_left_alone():
    """A screenshot taken twice is two different pictures, and running the
    tests again after an edit is the point. Only a stuck loop is stopped."""
    tool = Counter()
    with tempfile.TemporaryDirectory() as root:
        session = build_session(
            root=root, provider=looping(2), model='x', mode=Mode.TRUSTED, tools=[tool],
        )
        await session.start()
        session.submit('look twice')
        await drain(session)
    assert tool.calls == 2


async def test_the_count_starts_again_each_turn():
    """A repeat across turns is a person asking twice, which is not a loop."""
    tool = Counter()
    provider = looping(2)
    with tempfile.TemporaryDirectory() as root:
        session = build_session(
            root=root, provider=provider, model='x', mode=Mode.TRUSTED, tools=[tool],
        )
        await session.start()
        for _ in range(3):
            # The script is per turn, and the double plays it once. Rewinding
            # is what makes this three identical turns rather than one turn
            # followed by two that fall off the end.
            provider.calls = 0
            session.submit('look')
            await drain(session)
            # `drain` returns when turn.completed is *emitted*, which is a
            # moment before the task holding the turn finishes. Submitting
            # into that gap is refused, and waiting for it is what a client
            # does too — the send button is disabled until the turn ends.
            while session.busy:  # noqa: ASYNC110 - polling a flag, not awaiting an event
                await asyncio.sleep(0.01)
    assert tool.calls == 6, 'each turn should have got its two calls'


# -- toolsets ---------------------------------------------------------------


def test_a_group_narrows_a_session_to_what_it_is_for():
    """Measured: gemma4:12b given thirty tools opened the page and then
    reported it had no way to browse. The same model, same task, same prompt,
    with only the browser tools: done in twenty-six seconds, correctly."""
    allowed = resolve_toolset(['browser'])
    assert 'browser_navigate' in allowed
    assert 'shell' not in allowed and 'write_file' not in allowed


def test_asking_a_question_is_always_possible():
    """A session that cannot ask is a session that guesses."""
    for group in TOOLSETS:
        assert 'ask_user' in resolve_toolset([group]), group


def test_an_unknown_name_is_taken_as_a_tool_name():
    """So a caller wanting exactly `grep` and `read_file` can say so without
    a group having to exist for it.

    (Not `shell` any more: that is a group now, which brings `tasks` with it —
    a command left running in the background with no way to read it back is
    half a feature.)"""
    allowed = resolve_toolset(['grep', 'read_file'])
    assert allowed == {'ask_user', 'grep', 'read_file'}


def test_asking_for_nothing_means_everything():
    assert resolve_toolset([]) is None


async def test_a_narrowed_session_really_has_fewer_tools():
    with tempfile.TemporaryDirectory() as root:
        wide = build_session(root=root, provider=ScriptedProvider([[StreamDone()]]),
                             model='x', mode=Mode.TRUSTED)
        narrow = build_session(root=root, provider=ScriptedProvider([[StreamDone()]]),
                               model='x', mode=Mode.TRUSTED, toolset=['files'])
    assert 'shell' in wide.tools
    assert 'shell' not in narrow.tools
    assert 'read_file' in narrow.tools
    assert len(narrow.tools) < len(wide.tools)


async def test_every_group_names_tools_that_exist():
    """A group naming a tool that was renamed silently narrows a session to
    less than the person asked for."""
    with tempfile.TemporaryDirectory() as root:
        session = build_session(
            root=root, provider=ScriptedProvider([[StreamDone()]]), model='x',
            mode=Mode.TRUSTED, stage=None,
        )
    real = set(session.tools)

    # These are only present when their capability is attached, which a plain
    # session has none of. `lsp` is one of them: it exists only where a
    # language server is installed, and a plain session is given none.
    # `mail` is the same shape — the tool exists only where a mailbox is
    # configured, so a plain session has no `mail` to narrow to.
    optional = set(TOOLSETS['browser']) | set(TOOLSETS['desktop']) | set(TOOLSETS['media'])
    optional |= set(TOOLSETS['memory']) | set(TOOLSETS['web']) | set(TOOLSETS['system'])
    optional |= {'lsp'} | set(TOOLSETS['mail']) | set(TOOLSETS['calendar'])
    optional |= set(TOOLSETS['hr'])

    for group, names in TOOLSETS.items():
        for name in names:
            if name in optional:
                continue
            assert name in real, f'toolset {group} names {name}, which no longer exists'

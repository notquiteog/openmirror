"""One model call, and what happens when it does not answer.

`one_shot` exists because of a measured failure, and this is the file that
records it.

`POST /api/git/propose` was written with `max_tokens=400`, which is a
sensible-looking number for "a commit message is a line and a paragraph".
Against `deepseek/deepseek-v4-flash` — the model this install routes chat to
— the response was **113 chunks of reasoning, no text at all, and
`stop_reason: max_tokens`**. The endpoint could only say "the model returned
an empty message", which is true and tells nobody anything.

The bug is general and it is the reason a ceiling here is worse than nothing:
a reasoning model spends the budget on *thinking*, so a cap sized for the
answer starves the thinking and returns nothing. The prompt here is a few
hundred tokens of diff; there is nothing to bound.

So: no cap, and a truncated-with-no-text response reported as the different
failure it is. Both are asserted below, and the second one is asserted
against a provider that does exactly what the real one did.
"""

from __future__ import annotations

import pytest

from openmirror.providers.base import (
    ChatRequest,
    Message,
    NoProviderError,
    StreamDone,
    StreamText,
    StreamThinking,
    StreamToolUse,
    TextBlock,
    one_shot,
)


def req(text: str = 'hello') -> ChatRequest:
    return ChatRequest(model='m', messages=[Message(role='user', content=[TextBlock(text=text)])])


class Scripted:
    """A provider that emits a fixed list of events."""

    def __init__(self, *events: object) -> None:
        self.events = list(events)

    async def stream(self, _request: ChatRequest):  # noqa: ANN001 - matches the provider interface
        for event in self.events:
            yield event


async def test_text_comes_back():
    out = await one_shot(Scripted(StreamText(text='add the retry '), StreamText(text='loop')), req())
    assert out == 'add the retry loop'


async def test_thinking_is_not_the_answer():
    """The whole failure. A reasoning model fills the budget with this and
    the caller must not be handed it as though it were the message."""
    out = await one_shot(
        Scripted(
            StreamThinking(text='We are given a diff that adds...'),
            StreamText(text='add the line'),
            StreamDone(stop_reason='end_turn'),
        ),
        req(),
    )
    assert out == 'add the line'


async def test_a_run_out_of_budget_is_named_as_such():
    """Measured: 113 chunks of thinking, no text, `stop_reason: max_tokens`.
    The old code could only report "the model returned an empty message",
    which is true and useless — this says what actually happened and what to
    do about it."""
    provider = Scripted(
        *(StreamThinking(text='thinking...') for _ in range(113)),
        StreamDone(stop_reason='max_tokens'),
    )
    with pytest.raises(NoProviderError) as caught:
        await one_shot(provider, req(), what='commit message')
    said = str(caught.value)
    assert 'budget thinking' in said
    assert 'commit message' in said
    assert 'thinks less' in said


async def test_an_empty_answer_is_a_different_message_from_a_truncated_one():
    """Two different problems, and the operator's next step differs: one is a
    model that had nothing to say, the other is a model that thought for the
    whole allowance."""
    with pytest.raises(NoProviderError, match='no text'):
        await one_shot(Scripted(StreamDone(stop_reason='end_turn')), req())
    with pytest.raises(NoProviderError, match='budget thinking'):
        await one_shot(Scripted(StreamDone(stop_reason='max_tokens')), req())


async def test_a_tool_call_is_not_mistaken_for_text():
    """A model asked for a commit message that calls a tool has
    misunderstood, and returning the tool's arguments as the message would be
    worse than saying so."""
    provider = Scripted(
        StreamToolUse(id='c1', name='shell', input={'command': 'rm -rf /'}),
        StreamDone(stop_reason='tool_use'),
    )
    with pytest.raises(NoProviderError):
        await one_shot(provider, req())


async def test_whitespace_only_is_empty():
    with pytest.raises(NoProviderError):
        await one_shot(Scripted(StreamText(text='   \n  '), StreamDone()), req())


def test_the_helper_does_not_cap_by_default():
    """The bug, asserted at the level it was fixed. `max_tokens=0` is what
    "no ceiling" means on `ChatRequest`, and it is the default — so a caller
    who forgets the field cannot reintroduce the starvation."""
    assert ChatRequest(model='m', messages=[]).max_tokens == 0


def test_the_draft_routes_pass_no_ceiling():
    """Both places that used one, asserted on the file rather than on a call:
    this is a property of the request, and reading it is the only way to
    catch somebody adding `max_tokens` back with good intentions."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / 'openmirror' / 'routers'
    for name in ('git.py', 'mail.py'):
        text = (root / name).read_text()
        assert 'max_tokens=' not in text, f'{name} caps a one-shot request again'
        assert 'one_shot(' in text, f'{name} is not using the shared helper'


async def test_a_slow_model_is_bounded_and_says_so():
    """These sit behind a button. A button that says "writing…" for ever has
    no way out but reloading the page, and a slow model and a refused key are
    different problems with different fixes."""
    import asyncio

    class Slow:
        async def stream(self, _request: ChatRequest):  # noqa: ANN001
            yield StreamThinking(text='thinking at length...')
            await asyncio.sleep(30)
            yield StreamText(text='never seen')

    with pytest.raises(NoProviderError) as caught:
        await one_shot(Slow(), req(), seconds=0.2)
    said = str(caught.value)
    assert 'did not answer' in said
    assert 'slow or busy' in said


async def test_the_bound_does_not_leak_a_cancellation():
    """`wait_for` would turn a timeout into a cancellation of whatever this
    was called inside, which looks like the turn being interrupted rather than
    the model being slow."""
    import asyncio

    reached = []

    async def outer():
        try:
            await one_shot(Slowish(), req(), seconds=0.1)
        except NoProviderError:
            reached.append('handled')
            # The enclosing task is still usable after a timeout.
            reached.append(asyncio.current_task() is not None and 'alive')

    class Slowish:
        async def stream(self, _request: ChatRequest):  # noqa: ANN001
            yield StreamThinking(text='thinking...')
            await asyncio.sleep(30)
            yield StreamDone()

    await outer()
    assert reached == ['handled', 'alive']

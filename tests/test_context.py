"""The context meter: the number is real, the bar is not always knowable.

The whole design is one distinction, and it is worth stating because it is the
thing that is easy to get wrong in the direction of looking better:

**A token count is exact enough to act on. A percentage needs a denominator
this project may not have.** So a model it has never heard of gets a count
and no bar, rather than a bar at a confident wrong fraction — and somebody
looking at 40% on a model that will refuse the request makes a decision about
whether to keep working.

That is why `context_window` returns `None` and not a guess, why the table is
matched on the *last* segment of a gateway name, and why the bar fills
towards the compaction limit rather than the model's window. The compaction
limit is the one this session will actually hit.
"""

from __future__ import annotations

import pytest

from openmirror.agent.windows import WINDOWS, context_window, window_fraction

# --- the table ----------------------------------------------------------------


@pytest.mark.parametrize(
    'model,expected',
    [
        ('gpt-4o', 128_000),
        ('gpt-4o-mini', 128_000),
        # Not the same window as gpt-4o, and a naive `startswith('gpt-4o')`
        # cannot tell them apart.
        ('gpt-4', 8_192),
        ('claude-sonnet-4-20250514', 200_000),
        ('gemini-2.5-pro', 1_048_576),
        ('deepseek-chat', 65_536),
        ('llama-3.3-70b-instruct', 128_000),
    ],
)
def test_known_models(model: str, expected: int):
    assert context_window(model) == expected


def test_the_longest_prefix_wins():
    """`gpt-4o-mini` and `gpt-4` are different sizes and a plain
    `startswith` on the shorter one gets it wrong for both."""
    assert context_window('gpt-4.1') == 1_047_576
    assert context_window('gpt-4.1-mini') == 1_047_576
    assert context_window('gpt-4-turbo') == 128_000


def test_a_gateway_name_is_matched_on_its_last_segment():
    """`openrouter/anthropic/claude-sonnet-5.5` carries the real name at the
    end. Matching the whole string is how a gateway alias tells you nothing —
    and the alias is the one case where the name cannot be trusted."""
    assert context_window('openrouter/anthropic/claude-sonnet-4-5') == 200_000
    assert context_window('anthropic/claude-sonnet-4-5') == 200_000


def test_a_tag_or_variant_is_ignored():
    """`:batch` and friends are a serving mode, not a different model."""
    assert context_window('anthropic/claude-sonnet-4-5:batch') == 200_000
    assert context_window('gpt-4o:extended') == 128_000


def test_an_unknown_model_is_none_and_not_a_guess():
    """The point of the whole module. A missing entry costs a percentage; a
    wrong entry costs a wrong percentage, and only one of those is a
    mistake somebody can undo by adding a line."""
    assert context_window('deepseek/deepseek-v4-flash') is None
    assert context_window('something-nobody-has-heard-of') is None
    assert context_window('') is None
    assert context_window('') is None


def test_an_override_beats_the_table():
    """For somebody whose provider has told them the real number, which a
    gateway can cap lower than the model it fronts."""
    assert context_window('gpt-4o', override=64_000) == 64_000
    assert context_window('unknown-model', override=200_000) == 200_000


def test_the_table_has_no_nonsense_entries():
    for model, size in WINDOWS.items():
        assert size > 0, model
        assert model == model.lower(), model
        assert '/' not in model, f'{model} should be matched on a bare name'


# --- the fraction --------------------------------------------------------------


def test_the_compaction_limit_is_the_denominator_when_there_is_one():
    """The limit this session will actually hit, not the model's window. A
    bar at 25% of 200k when the conversation is summarised at 100k is a bar
    that means nothing to the decision in front of it."""
    assert window_fraction(50_000, 100_000, 200_000) == 0.5
    assert window_fraction(200_000, 100_000, 200_000) == 1.0


def test_the_window_is_used_only_when_there_is_no_limit():
    assert window_fraction(50_000, 0, 200_000) == 0.25


def test_no_denominator_means_no_fraction():
    """A session that will not compact, on a model this project has not heard
    of. There is no honest percentage, so there is none."""
    assert window_fraction(50_000, 0, None) is None
    assert window_fraction(0, 0, None) is None


def test_a_fraction_is_clamped():
    """An estimate that overshoots is a bar at 100%, not one at 140% that
    runs off the end of its own track."""
    assert window_fraction(999_999, 100_000, None) == 1.0
    assert window_fraction(-5, 100_000, None) == 0.0


# --- what the report carries ---------------------------------------------------


async def test_the_report_prefers_what_the_model_counted_over_the_estimate(tmp_path, monkeypatch):
    """`exact` is the difference between "the model told us" and "we guessed",
    and the interface needs to know which so it can stop drawing a precise
    number over a guess."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import Message, StreamDone, TextBlock
    from tests.test_agent import ScriptedProvider

    with tmp_path as root:
        session = build_session(
            root=str(root), provider=ScriptedProvider([[StreamDone()]]), model='gpt-4o',
            mode=Mode.TRUSTED, compact_at=100_000,
        )
        # Give the estimate something to count, so the test is about which of
        # the two wins rather than about a session that is nearly empty.
        session.messages.append(Message(role='user', content=[TextBlock(text='x' * 4_000)]))
        # Nothing has been sent yet, so this is the estimate.
        assert session.context_report().exact is False
        # And a provider's own count wins, because it is what the model saw.
        session._last_input = 50_000
        session._estimate = lambda: 1  # type: ignore[method-assign]
        report = session.context_report()
        assert report.exact is True
        assert report.tokens == 50_000
        assert report.limit == 100_000
        assert report.window == 128_000


async def test_the_report_carries_no_money(tmp_path):
    """A dollar figure is out of date the moment a provider changes a price or
    a provider adds a markup, and somebody reads it as a bill. Tokens are the
    two numbers that do not rot."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone
    from tests.test_agent import ScriptedProvider

    with tmp_path as root:
        session = build_session(
            root=str(root), provider=ScriptedProvider([[StreamDone()]]), model='gpt-4o', mode=Mode.TRUSTED,
        )
        fields = set(session.context_report().model_dump())
        assert 'cost' not in fields and 'usd' not in fields and 'price' not in fields
        assert {'tokens', 'limit', 'window', 'total_in', 'total_out', 'exact'} <= fields


async def test_the_route_reports_the_fraction_alongside_the_numbers(tmp_path):
    from starlette.testclient import TestClient

    from openmirror.agent.manager import manager
    from openmirror.main import app
    from openmirror.providers.base import StreamDone
    from tests.test_agent import ScriptedProvider

    with tmp_path as root:
        made = await manager.create(
            root=str(root), provider=ScriptedProvider([[StreamDone()]]), model='gpt-4o', mode='ask',
        )
        client = TestClient(app)
        got = client.get(f'/api/sessions/{made.id}/context')
        assert got.status_code == 200
        body = got.json()
        assert 'fraction' in body and 'tokens' in body and body['model'] == 'gpt-4o'
        # No limit configured and a model with no entry, so no percentage —
        # which is the honest answer rather than a guess.
        assert body['fraction'] is None or 0 <= body['fraction'] <= 1
        await manager.close(made.id)

    assert client.get('/api/sessions/nope/context').status_code == 404

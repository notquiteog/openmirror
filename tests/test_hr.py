"""The leave ledger, and the question it is built around.

The design decision everything else follows from:

**The ledger returns evidence and a band, and the model draws the conclusion.**

Not a percentage, because "73% likely" from nine requests is a number that
looks like knowledge and is not, and somebody will act on it. Not a verdict,
because a tool that answered "approve" would have taken a decision that
belongs to the person using it, out of a record of a handful of decisions.

So the tests here are mostly about when the tool *declines* to answer, which
is the part that went wrong first and is the part that matters. The first
version OR-ed "same person", "same kind" and "similar length" together, which
made every holiday comparable to every other holiday, and then reported a
fourteen-day request as `clear` for somebody whose own policy file says more
than ten days needs cover. Each of these is that mistake, named.
"""

from __future__ import annotations

import pytest

from openmirror.agent.tools.base import ToolContext
from openmirror.agent.tools.hr import HrTool
from openmirror.hr.store import MIN_FOR_A_BAND, Ledger, StoreError, span_days


@pytest.fixture
def ledger(tmp_path):
    held = Ledger(tmp_path / 'hr.db')
    yield held
    held.close()


def ctx(root) -> ToolContext:
    from pathlib import Path

    return ToolContext(
        root=Path(root), cwd=Path(root), emit=None, ask=None, session_id='s', confined=True
    )


def seed(ledger, records):
    """`records` is (person, days, outcome, reason)."""
    for index, (person, days, outcome, reason) in enumerate(records):
        record = ledger.add(person=person, start=f'2026-01-{index + 1:02d}', days=days, kind='holiday')
        if outcome:
            ledger.decide(record.id, outcome, reason=reason)
    return record


# --- the arithmetic -----------------------------------------------------------


@pytest.mark.parametrize(
    'start,end,want',
    [
        ('2026-03-16', '', 1.0),                 # a Monday
        ('2026-03-16', '2026-03-20', 5.0),      # a full week
        ('2026-03-13', '2026-03-16', 2.0),      # Friday to Monday: two days
        ('2026-03-14', '2026-03-15', 0.0),      # a whole weekend
        ('2026-03-16', '2026-03-14', 1.0),      # the wrong way round
        ('nonsense', '', 0.0),
    ],
)
def test_a_span_is_working_days(start, end, want):
    """"Ten days off" means ten days off and not ten calendar days, and a
    policy written in one is not satisfied by the other. Public holidays vary
    by country and by year, so weekends are excluded and nothing else is —
    guessing at the rest would put a confidently wrong number in front of
    somebody."""
    assert span_days(start, end) == want


# --- the ledger ---------------------------------------------------------------


def test_a_request_needs_a_person_and_a_start(ledger):
    with pytest.raises(StoreError, match='person'):
        ledger.add(person='', start='2026-01-01')
    with pytest.raises(StoreError, match='start'):
        ledger.add(person='Sam', start='')


def test_a_decision_takes_only_two_outcomes(ledger):
    record = ledger.add(person='Sam', start='2026-01-05')
    with pytest.raises(StoreError, match='approved'):
        ledger.decide(record.id, 'maybe')
    assert ledger.decide(record.id, 'approved', reason='short').outcome == 'approved'


def test_a_decision_keeps_the_reason(ledger):
    """The only thing that makes the next request assessable. "denied" with no
    reason teaches the ledger nothing and gives whoever reads it later no way
    to tell a policy from a mood."""
    record = ledger.add(person='Sam', start='2026-01-05', days=3)
    decided = ledger.decide(record.id, 'denied', reason='no cover that week')
    assert decided.outcome_reason == 'no cover that week'
    assert decided.public()['outcome_reason'] == 'no cover that week'


def test_adding_a_person_twice_keeps_what_was_there(ledger):
    ledger.upsert_person('Priya', role='engineer', notes='needs cover over 10 days')
    ledger.upsert_person('Priya', role='')
    row = ledger.people()[0]
    assert row['role'] == 'engineer', 'a blank field should not erase what was there'
    assert row['notes'] == 'needs cover over 10 days'


def test_the_ledger_is_not_world_readable(ledger, tmp_path):
    """Other people's leave. A file the rest of the machine can read is the
    one thing this must not be."""
    assert oct((tmp_path / 'hr.db').stat().st_mode)[-3:] == '600'


# --- the assessment, and where it declines to answer --------------------------


def test_nothing_is_offered_with_an_empty_ledger(ledger):
    got = ledger.assess('Priya', 'holiday', 5)
    assert got['band'] == ''
    # With nothing at all, the first thing to say is that there is no record of
    # *this person* — which is a different sentence from "not enough history",
    # and the one that matters when somebody is not in the system yet.
    assert 'no record of a decision about Priya' in got['because']
    assert got['evidence'] == []


def test_fewer_than_four_comparisons_offers_no_band(ledger):
    """Four is not a statistical threshold. It is the point below which a
    count of decisions about one or two people is a record, not a pattern, and
    a band drawn from it would be a coincidence wearing a percentage."""
    seed(ledger, [('Sam', 5, 'approved', '')] * 3)
    got = ledger.assess('Sam', 'holiday', 5)
    assert got['decided'] == 3 < MIN_FOR_A_BAND
    assert got['band'] == ''
    assert 'record rather than a pattern' in got['because']


def test_a_clear_pattern_does_produce_a_band(ledger):
    """The tool is useful when there *is* a pattern, and withholding it always
    would be a way of being careful that is really just being useless."""
    seed(ledger, [('Sam', 5, 'approved', 'short'), ('Sam', 4, 'approved', ''),
                 ('Sam', 6, 'approved', ''), ('Sam', 5, 'approved', '')])
    got = ledger.assess('Sam', 'holiday', 5)
    assert got['band'] == 'clear', got['because']
    assert '4 of 4' in got['because']


def test_a_mixed_record_is_a_coin_flip_and_not_a_percentage(ledger):
    seed(ledger, [('Sam', 5, 'approved', ''), ('Sam', 5, 'denied', 'no cover'),
                  ('Sam', 5, 'approved', ''), ('Sam', 5, 'denied', 'no cover')])
    assert ledger.assess('Sam', 'holiday', 5)['band'] == 'coin-flip'


def test_mostly_refused_is_unlikely(ledger):
    seed(ledger, [('Sam', 5, 'denied', 'no cover')] * 4 + [('Sam', 5, 'approved', '')])
    assert ledger.assess('Sam', 'holiday', 5)['band'] == 'unlikely'


def test_a_request_longer_than_anything_decided_gets_no_reading(ledger):
    """The mistake the first version made most confidently.

    Fourteen days for somebody whose longest approved request is three reads
    as `clear` if you treat "holiday" as a category — and it is exactly the
    request where history should shut up and the policy file should speak.
    """
    ledger.upsert_person('Priya', notes='more than 10 consecutive days needs cover')
    seed(ledger, [('Priya', 2, 'approved', ''), ('Priya', 3, 'approved', ''),
                  ('Priya', 2, 'approved', ''), ('Priya', 3, 'approved', '')])
    got = ledger.assess('Priya', 'holiday', 14)
    assert got['band'] == '', 'history must not speak about a request it has never seen'
    assert 'longer than anything' in got['because']
    assert got['out_of_range']


def test_sharing_a_kind_is_not_being_comparable(ledger):
    """Every holiday shares a kind, and every holiday being comparable to
    every other holiday is how a pattern gets built out of nothing."""
    ledger.upsert_person('Priya')
    seed(ledger, [('Sam', 5, 'approved', ''), ('Sam', 5, 'approved', ''),
                  ('Sam', 5, 'approved', ''), ('Sam', 5, 'approved', '')])
    got = ledger.assess('Priya', 'holiday', 5)
    assert got['band'] == '', "another person's record must not produce a reading"
    assert "no history of their own" in got['comparables_from']
    # It is still shown, because it is relevant and the person can ignore it.
    assert got['evidence'], 'context is worth having even when it does not decide'
    assert all(r['person'] == 'Sam' for r in got['evidence'])


def test_somebody_with_no_history_is_told_the_record_is_not_theirs(ledger):
    """It is shown as evidence, because it is relevant, and marked as other
    people's — because applying a team's average to a person you have never
    decided about is the lazy inference this tool exists to avoid."""
    seed(ledger, [('Sam', 5, 'approved', '')] * 4)
    got = ledger.assess('Robin', 'holiday', 5)
    assert got['band'] == ''
    assert "other people's record" in got['comparables_from']


def test_a_three_day_request_and_a_fortnight_are_different_questions(ledger):
    ledger.upsert_person('Sam')
    seed(ledger, [('Sam', 3, 'approved', '')] * 4)
    assert ledger.assess('Sam', 'holiday', 3)['band'] == 'clear'
    assert ledger.assess('Sam', 'holiday', 21)['band'] == ''


def test_the_policy_is_read_back_and_not_interpreted(ledger):
    """A tool that read "more than ten consecutive days needs cover" out of
    English and produced a verdict would be applying a rule it half-understood,
    confidently. The policy is shown and the model reads it."""
    ledger.upsert_person('Priya', notes='more than 10 consecutive days needs cover from someone else')
    assert ledger.policy('Priya').startswith('more than 10')
    assert ledger.policy('Robin') == ''


def test_the_evidence_carries_its_reasons_and_not_just_its_counts(ledger):
    seed(ledger, [('Sam', 5, 'approved', ''), ('Sam', 5, 'approved', ''),
                  ('Sam', 5, 'approved', ''), ('Sam', 5, 'denied', 'no cover that week')])
    got = ledger.assess('Sam', 'holiday', 5)
    reasons = [r['outcome_reason'] for r in got['evidence'] if r['outcome_reason']]
    assert reasons, 'a decision without its reason says nothing'


# --- the tool -----------------------------------------------------------------


async def test_the_tool_will_not_record_a_decision_it_was_not_given(ledger, tmp_path):
    """This records what somebody else already decided. It never makes one,
    and the message says so rather than only refusing."""
    tool = HrTool(ledger)
    bad = tool.assess({'action': 'decide', 'request_id': 'x', 'outcome': 'yes'}, ctx(tmp_path))
    assert bad.invalid and 'it does not make one' in bad.invalid


async def test_the_decision_prompt_carries_the_outcome(ledger, tmp_path):
    tool = HrTool(ledger)
    good = tool.assess(
        {'action': 'decide', 'request_id': 'abc', 'outcome': 'denied'}, ctx(tmp_path)
    )
    assert good.invalid is None
    assert 'denied' in good.summary


async def test_the_tool_reports_no_people_rather_than_nothing(ledger, tmp_path):
    out = await HrTool(ledger).run({'action': 'people'}, ctx(tmp_path))
    assert 'add_person' in out.content


async def test_the_tool_asks_for_a_reason_it_can_use(ledger, tmp_path):
    """Asked in `assess`, so it is asked at the one moment where being asked is
    worth the extra click."""
    tool = HrTool(ledger)
    record = ledger.add(person='Sam', start='2026-02-02', days=3)
    out = await tool.run(
        {'action': 'decide', 'request_id': record.id, 'outcome': 'approved', 'reason': 'short'},
        ctx(tmp_path),
    )
    assert 'approved' in out.content
    assert ledger.get(record.id).outcome == 'approved'


async def test_the_tool_says_what_the_evidence_is_and_not_what_to_do(ledger, tmp_path):
    seed(ledger, [('Sam', 5, 'approved', ''), ('Sam', 5, 'approved', ''),
                  ('Sam', 5, 'approved', ''), ('Sam', 5, 'approved', '')])
    out = await HrTool(ledger).run(
        {'action': 'assess', 'name': 'Sam', 'kind': 'holiday', 'days': 5}, ctx(tmp_path)
    )
    assert 'clear' in out.content
    assert 'Reading of the history' in out.content
    # And it is not an instruction.
    assert 'you should approve' not in out.content.lower()


async def test_the_tool_works_out_the_length_when_not_told(ledger, tmp_path):
    record = await HrTool(ledger).run(
        {'action': 'request', 'name': 'Sam', 'start': '2026-03-16', 'end': '2026-03-20'}, ctx(tmp_path)
    )
    assert '5 working days' in record.content


def test_the_tool_never_approves_or_denies_on_its_own(ledger, tmp_path):
    """It records what a person decided. It is not given a way to decide.

    Every action in `ACTIONS` is either a read or a record. There is no action
    that answers "should this be approved", because that is the decision
    somebody is entitled to and a tool that made it out of a handful of
    records would be doing it confidently and wrongly.
    """
    from openmirror.agent.tools.hr import ACTIONS, RISK
    from openmirror.protocol.agent import Risk

    assert 'approve' not in ACTIONS and 'decide_yes' not in ACTIONS
    assert set(ACTIONS) == set(RISK), 'an action with no grade, or a grade with no action'
    # Nothing here is on the axis that spends money or sends messages to
    # somebody: every action is a read or a local write.
    assert all(risk in (Risk.READ, Risk.WRITE) for risk in RISK.values()), RISK
    assert RISK['assess'] is Risk.READ, 'asking is not deciding'
    assert RISK['decide'] is Risk.WRITE, 'recording a decision is a change to the ledger'


def test_nothing_leaves_the_machine(ledger):
    """No client, no url, no token, no import of a requests library — the
    ledger is a file on this disk, and an install that wants a real HR system
    should reach it through a hook it has agreed to.

    Asserted rather than promised, because the first version of a "local
    first, integrate later" feature is where the client quietly appears.
    """
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / 'openmirror' / 'hr' / 'store.py'
    ).read_text()

    # Imports and URLs, not the *word* "requests" — which is how the first
    # version of this check failed, on a docstring saying "Leave requests".
    imported = set(re.findall(r'(?m)^\s*(?:import|from)\s+([a-z0-9_.]+)', source))
    for client in ('aiohttp', 'httpx', 'requests', 'urllib', 'http', 'socket', 'ssl'):
        assert client not in {name.split('.')[0] for name in imported}, f'the ledger imports {client}'
    assert '://' not in source, 'the ledger has a URL in it'
    assert not re.search(r'(?i)(api[_-]?key|access[_-]?token|client[_-]?secret)', source), (
        'the ledger has credentials in it'
    )

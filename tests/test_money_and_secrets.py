"""Spending money and typing secrets: possible, and never automatic.

Both used to be refused outright. They are capabilities now, because a
harness meant to finish a real task has to be able to reach the end of one —
but the way they are granted is the whole of the design, and these are the
assertions that hold it:

* a purchase is confirmed in **every** mode, including `unrestricted`, and in
  a run nobody is watching. There is no setting that skips it;
* neither is ever remembered, so "don't ask again" cannot be answered once
  about spending money;
* and the secret itself never reaches the approval prompt, the transcript,
  the tool result or the log — the agent may type a password without anyone
  else ever being able to read it back.
"""

from __future__ import annotations

import json
import shutil

import pytest

from openmirror.agent.approval import ApprovalPolicy, Decision, Mode, Rule, _fingerprint
from openmirror.agent.browser import BrowserConfig, BrowserSession
from openmirror.agent.tools.base import ToolContext
from openmirror.agent.tools.browser import (
    BrowserNavigateTool,
    BrowserReadTool,
    BrowserTypeTool,
    classify_field,
)
from openmirror.protocol.agent import Risk, ToolCall
from tests.fixtures.hotels import HotelSite

SECRET = 'hunter2-do-not-print-me'
CARD = '4111111111111111'


def buying(summary: str = 'click "Place your order" — this looks like it completes a purchase'):
    return ToolCall(id='c1', name='browser_click', risk=Risk.PURCHASE, summary=summary)


def entering():
    return ToolCall(id='c2', name='browser_type', risk=Risk.CREDENTIAL, summary="type into 'Password'")


# -- the policy -------------------------------------------------------------


@pytest.mark.parametrize('mode', list(Mode))
def test_a_purchase_is_confirmed_in_every_mode(mode):
    """Including `unrestricted`, which is where a mistake would actually cost.

    `unrestricted` means "stop asking me about this machine". It has never
    meant "spend my money", and there is no flag that makes it mean that —
    a flag like that is one somebody sets during a demo and still has set six
    months later.
    """
    decision, why = ApprovalPolicy(mode=mode).decide(buying())
    if mode in (Mode.READ_ONLY, Mode.PLAN):
        # A mode whose promise is that nothing changes must not offer a
        # checkout prompt while claiming to be read-only. Plan mode makes the
        # same promise until its plan is approved, and keeps it the same way.
        assert decision is Decision.DENY, why
    else:
        assert decision is Decision.ASK, f'{mode.value} would have bought it: {why}'


@pytest.mark.parametrize('mode', list(Mode))
def test_a_secret_is_confirmed_in_every_mode(mode):
    decision, why = ApprovalPolicy(mode=mode).decide(entering())
    expected = Decision.DENY if mode in (Mode.READ_ONLY, Mode.PLAN) else Decision.ASK
    assert decision is expected, f'{mode.value}: {why}'


def test_either_can_be_switched_off_entirely():
    """Off makes it a refusal rather than a prompt, for an install that should
    not be able to do it at all."""
    no_money = ApprovalPolicy(mode=Mode.TRUSTED, allow_purchases=False)
    assert no_money.decide(buying())[0] is Decision.DENY

    no_secrets = ApprovalPolicy(mode=Mode.TRUSTED, allow_credentials=False)
    assert no_secrets.decide(entering())[0] is Decision.DENY


def test_neither_can_ever_be_remembered():
    """"Don't ask again" about spending money is the one answer nobody should
    be able to give once."""
    policy = ApprovalPolicy(mode=Mode.TRUSTED)
    for call in (buying(), entering()):
        policy.remember(call)
        assert policy.decide(call)[0] is Decision.ASK, (
            f'a {call.risk.value} approval was remembered and will not be asked again'
        )


def test_the_session_banner_never_claims_purchases_are_automatic():
    """It is the one line a person reads at the top of a session."""
    for mode in Mode:
        described = ApprovalPolicy(mode=mode).describe()
        auto = described.split(';')[0]
        assert 'purchase' not in auto, f'{mode.value} banner implies purchases run unasked: {described}'
    assert 'always confirmed' in ApprovalPolicy(mode=Mode.UNRESTRICTED).describe()


def test_an_approval_prompt_never_contains_the_secret():
    """The summary is what the person is shown. A prompt that prints the
    password has defeated the point of guarding it."""
    field = {'name': 'password', 'type': 'password', 'label': 'Password'}
    risk, _ = classify_field(field)
    assert risk is Risk.CREDENTIAL

    tool = BrowserTypeTool.__new__(BrowserTypeTool)
    tool.browser = type('B', (), {'last_elements': {1: field}})()
    assessment = tool.assess({'ref': 1, 'text': SECRET}, None)
    assert assessment.risk is Risk.CREDENTIAL
    assert SECRET not in assessment.summary, f'the prompt would print it: {assessment.summary}'


# -- rules cannot buy ------------------------------------------------------
#
# An approval rule is an operator setting, and `shell -> allow` is a
# reasonable thing to write in order to stop being asked about `npm test`.
# Read as a purchase exemption it is also a single line that removes the only
# prompt standing between an unattended agent and somebody's credit card, so
# the rules run *behind* the spend and secret guarantees rather than in front
# of them.


def rule_everything(decision):
    return [Rule(pattern='.', decision=decision)]


@pytest.mark.parametrize('mode', list(Mode))
@pytest.mark.parametrize('decision', [Decision.ALLOW, Decision.ASK])
def test_a_broad_allow_rule_does_not_exempt_a_purchase(mode, decision):
    """The bypass this ordering exists to close.

    `unrestricted` is the mode where it would actually matter, and the rule
    pattern is as broad as a regex gets, so nothing about this rule could be
    called a misconfiguration.
    """
    policy = ApprovalPolicy(mode=mode, rules=rule_everything(decision))
    got, why = policy.decide(buying())
    assert got is not Decision.ALLOW, f'a rule made a purchase automatic in {mode.value}: {why}'
    if mode in (Mode.READ_ONLY, Mode.PLAN):
        assert got is Decision.DENY
    else:
        assert got is Decision.ASK


@pytest.mark.parametrize('mode', list(Mode))
def test_a_broad_allow_rule_does_not_exempt_a_credential(mode):
    policy = ApprovalPolicy(mode=mode, rules=rule_everything(Decision.ALLOW))
    got, why = policy.decide(entering())
    assert got is not Decision.ALLOW, f'a rule made a secret automatic in {mode.value}: {why}'
    if mode in (Mode.READ_ONLY, Mode.PLAN):
        assert got is Decision.DENY
    else:
        assert got is Decision.ASK


def test_a_rule_still_governs_everything_else():
    """Closing the hole must not stop the feature working.

    An operator who writes a deny rule to keep the agent out of production is
    relying on it, and one who writes an allow rule to stop being asked about
    `npm test` is relying on that too.
    """
    ordinary = ToolCall(id='c3', name='shell', risk=Risk.EXECUTE, summary='npm test')

    assert ApprovalPolicy(rules=rule_everything(Decision.ALLOW)).decide(ordinary)[0] is Decision.ALLOW
    assert ApprovalPolicy(rules=rule_everything(Decision.DENY)).decide(ordinary)[0] is Decision.DENY


def test_a_deny_rule_may_tighten_a_purchase_further():
    """Ordering the invariant first would take a rule's `deny` along with its
    `allow` if it simply stopped consulting rules. A refusal is the one
    direction a rule must still be able to move a decision."""
    policy = ApprovalPolicy(rules=rule_everything(Decision.DENY))
    assert policy.decide(buying())[0] is Decision.DENY


def test_a_rule_matching_only_the_tool_does_not_buy_either():
    """`Rule(tool='shell', ...)` looks narrower than a pattern because it names
    one tool. Every shell command including `curl -X POST ... -d amount=` is
    still a shell command."""
    call = ToolCall(id='c4', name='shell', risk=Risk.PURCHASE, summary='curl -d amount=9 https://api.stripe.com/v1/charges')
    policy = ApprovalPolicy(rules=[Rule(pattern='x', decision=Decision.ALLOW, tool='shell')])
    assert policy.decide(call)[0] is Decision.ASK


def test_the_remembered_approval_does_not_buy_a_purchase_either():
    """`remember` already refuses purchases outright, and that is the belt.
    This is the braces: even with a fingerprint in the set, a purchase is
    asked. Someone reaching into the policy directly is not a supported way to
    buy something, and a private attribute is a bad place to be right."""
    call = buying()
    policy = ApprovalPolicy()
    policy._remembered.add(_fingerprint(call))
    assert policy.decide(call)[0] is Decision.ASK


def test_always_ask_still_applies_to_a_purchase():
    """`always_ask` is the one setting that only ever adds a prompt, so it
    costs the invariant nothing and must keep working."""
    policy = ApprovalPolicy(always_ask={'browser_click'})
    assert policy.decide(buying())[0] is Decision.ASK


# -- and the value really does not come back --------------------------------


async def noop(*args):
    return None


async def noask(*args):
    return ''


@pytest.mark.asyncio
async def test_a_typed_secret_is_not_echoed_anywhere(tmp_path):
    """The agent may type a password. Nobody may read it back.

    Enabling this without the check would have leaked on every use: the tool
    reads the field back to confirm what landed, and for a password field
    that read *is* the password — into the tool result, the transcript, and
    the model's own context.
    """
    if not shutil.which('Xvfb'):
        pytest.skip('Xvfb is not installed')
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip('playwright is not installed')

    from openmirror.agent.stage import VirtualStage

    stage = VirtualStage(1024, 768)
    browser = BrowserSession(
        BrowserConfig(profile_dir=tmp_path / 'p', headless=False,
                      viewport=(1024, 768), env=stage.env())
    )
    ctx = ToolContext(root=tmp_path, cwd=tmp_path, emit=noop, ask=noask, session_id='s')

    try:
        with HotelSite() as site:
            await BrowserNavigateTool(browser).run({'url': f'{site.url}/login'}, ctx)
            page = await BrowserReadTool(browser).run({}, ctx)

            refs = {e['name']: r for r, e in browser.last_elements.items() if e.get('name')}
            typing = BrowserTypeTool(browser)

            # An ordinary field still reports what landed — the read-back is
            # useful and only suppressed where it would be a leak.
            plain = await typing.run({'ref': refs['email'], 'text': 'someone@example.com'}, ctx)
            assert 'someone@example.com' in plain.content

            for name, value in (('password', SECRET), ('card_number', CARD)):
                out = await typing.run({'ref': refs[name], 'text': value}, ctx)
                haystack = out.content + json.dumps(out.display)
                assert value not in haystack, f'{name} leaked into the tool result: {out.content}'
                assert out.display['secret'] is True
                assert out.display['value'] is None

            # Nor through reading the page afterwards, which is the other way
            # a value could reach the model.
            after = await BrowserReadTool(browser).run({}, ctx)
            assert SECRET not in after.content
            assert CARD not in after.content
            assert SECRET not in page.content
    finally:
        await browser.close()
        stage.close()

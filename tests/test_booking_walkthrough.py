"""The whole thing, on a real browser, on a screen of its own.

This is the test the rest of the desktop work is for, and it is deliberately
end to end: a real Xvfb display, a real Chromium launched onto it, a real
booking site, and the actual tools an agent would call — no mocks below the
provider. What it proves, in order:

* the agent gets a screen of its own, and **the pointer on the desk does not
  move** while it works. That is the claim the whole stage design exists to
  make, and the only way to check it is to look at the real display's pointer
  before and after;
* a browser launched onto that stage is visible *on it* — the screenshot is
  not black, and the desktop tools can read the page from the picture;
* the tools do the task: search a city and dates, read the results, open a
  room;
* and then it stops. The last button says "Book this room — pay now", and the
  approval policy grades it a purchase — which no mode auto-runs, including
  `unrestricted`. The test asserts that in the mode that is supposed to be
  able to do anything, because that is the mode where a mistake here would
  actually cost someone money.

Skipped where the machine cannot do it — no Xvfb, no Chromium — rather than
failed: this suite has to pass on a laptop with neither.
"""

from __future__ import annotations

import shutil

import pytest

from openmirror.agent.approval import ApprovalPolicy, Decision, Mode
from openmirror.agent.browser import BrowserConfig, BrowserSession
from openmirror.agent.tools.base import ToolContext
from openmirror.agent.tools.browser import (
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserReadTool,
    BrowserTypeTool,
)
from openmirror.agent.tools.desktop import desktop_tools
from openmirror.protocol.agent import Risk, ToolCall
from tests.fixtures.hotels import HotelSite

pytestmark = pytest.mark.asyncio

CITY = 'Tbilisi'
CHECK_IN = '2026-10-14'
CHECK_OUT = '2026-10-21'


def _needs():
    if not shutil.which('Xvfb'):
        pytest.skip('Xvfb is not installed')
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip('mss and pillow are needed to look at a screen')
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip('playwright is not installed')


async def noop(*args):
    return None


async def noask(*args):
    return ''


def context(tmp_path):
    return ToolContext(root=tmp_path, cwd=tmp_path, emit=noop, ask=noask, session_id='booking')


async def test_it_books_up_to_the_point_of_paying(tmp_path):
    _needs()

    import os

    from openmirror.agent.stage import _X11, VirtualStage

    # Where the real pointer is now. The whole promise is that this does not
    # move, so it is read before anything starts.
    real_display = os.environ.get('DISPLAY', '')
    before = None
    if real_display:
        try:
            eyes = _X11(real_display)
            before = eyes.pointer()
            eyes.close()
        except Exception:  # noqa: BLE001 - no X here is fine; the rest still runs
            before = None

    stage = VirtualStage(1280, 900)
    moved_to: tuple[int, int] | None = None
    browser = BrowserSession(
        BrowserConfig(
            profile_dir=tmp_path / 'profile',
            # Headful, on a display nobody is looking at. That is the whole
            # trick: the desktop tools can see it and it cannot take focus
            # from anyone, because there is nobody on that display.
            headless=False,
            viewport=(1280, 900),
            env=stage.env(),
        )
    )

    ctx = context(tmp_path)
    tools = {t.name: t for t in desktop_tools(stage)}

    try:
        with HotelSite() as site:
            navigate = BrowserNavigateTool(browser)
            read = BrowserReadTool(browser)
            click = BrowserClickTool(browser)
            typing = BrowserTypeTool(browser)

            # -- 1. open the site -------------------------------------------
            await navigate.run({'url': site.url}, ctx)
            page = await read.run({}, ctx)
            assert 'Kartuli Stays' in page.content

            elements = browser.last_elements
            field = {
                e['name']: ref for ref, e in elements.items() if e.get('name')
            }
            assert {'city', 'checkin', 'checkout'} <= set(field), (
                f'the form was not readable: {sorted(field)}'
            )

            # -- 2. fill in the city and the dates --------------------------
            for name, value in (('city', CITY), ('checkin', CHECK_IN), ('checkout', CHECK_OUT)):
                assessment = typing.assess({'ref': field[name], 'text': value}, ctx)
                # Typing into an ordinary form field is a write, never a
                # credential — if this ever grades as CREDENTIAL the classifier
                # has become useless by being too eager.
                assert assessment.risk is Risk.WRITE, f'{name} graded {assessment.risk}'
                await typing.run({'ref': field[name], 'text': value}, ctx)

            search = next(
                ref for ref, e in elements.items() if 'search hotels' in (e.get('label') or '').lower()
            )
            await click.run({'ref': search}, ctx)

            # -- 3. read what came back -------------------------------------
            results = await read.run({}, ctx)
            assert 'Hotels in Tbilisi' in results.content
            assert CHECK_IN in results.content and CHECK_OUT in results.content
            assert 'Hotel Metekhi View' in results.content

            # -- 4. look at the screen, not just the DOM --------------------
            # The desktop path is the one that has to work for anything that
            # is not a browser, so it is exercised here too: the picture must
            # be of the real window, not the black frame an X11 grabber
            # returns when it is pointed at the wrong display.
            shot = await tools['desktop_screenshot'].run({}, ctx)
            png = shot.display['image']
            assert shot.display['width'] == 1280
            import base64
            import io

            from PIL import Image

            with Image.open(io.BytesIO(base64.b64decode(png))) as image:
                colours = image.convert('RGB').getcolors(maxcolors=100_000)
            assert colours is not None and len(colours) > 8, (
                'the screenshot is nearly one colour — the browser is not on this display'
            )

            # -- 5. open a room ---------------------------------------------
            room_link = next(
                ref for ref, e in browser.last_elements.items()
                if 'see this room' in (e.get('label') or '').lower()
            )
            await click.run({'ref': room_link}, ctx)
            room = await read.run({}, ctx)
            assert 'GEL' in room.content

            # -- 6. and stop --------------------------------------------------
            # Where the agent's own pointer actually is, read from its own
            # display. This is the positive half of the promise: it moved a
            # pointer, and that pointer is not the one on the desk.
            moved_to = stage.x.pointer()

            book = next(
                ref for ref, e in browser.last_elements.items()
                if 'book this room' in (e.get('label') or '').lower()
            )
            assessment = click.assess({'ref': book}, ctx)
            assert assessment.risk is Risk.PURCHASE, (
                f'"Book this room — pay now" graded {assessment.risk}, not a purchase'
            )

            # Every mode. `unrestricted` means "stop asking me about this
            # machine"; it has never meant "spend my money", and this is the
            # line that says so.
            #
            # `read_only` and `plan` both refuse outright rather than
            # prompting. A mode promising that nothing changes must not offer a
            # checkout, and plan mode makes exactly that promise until its plan
            # is approved — approving a step of a plan nobody was shown is not
            # consent to the plan.
            #
            # The plan case was `ASK` here until the `allow_purchases` change
            # in e36fd7c, which made both refusals deliberate. That commit
            # wrote the rule down in `test_money_and_secrets.py` and in the
            # policy itself, and missed this line: the same commit left the
            # old expectation behind, and the two have been contradicting each
            # other ever since. The policy was right.
            call = ToolCall(id='c1', name='browser_click', arguments={'ref': book},
                            risk=assessment.risk, summary=assessment.summary)
            for mode in Mode:
                decision, why = ApprovalPolicy(mode=mode).decide(call)
                refusing = mode in (Mode.READ_ONLY, Mode.PLAN)
                wanted = Decision.DENY if refusing else Decision.ASK
                assert decision is wanted, f'{mode.value} would have booked it: {why}'

            # Saving it for later is not a purchase, and grading everything on
            # the page as one would make the guard useless by making it
            # constant.
            save = next(
                ref for ref, e in browser.last_elements.items()
                if 'save to my list' in (e.get('label') or '').lower()
            )
            assert click.assess({'ref': save}, ctx).risk is not Risk.PURCHASE

    finally:
        # The browser first, and then the display it was drawn on. The other
        # order leaves a Chromium with nowhere to draw, which it survives but
        # complains about at length.
        await browser.close()
        stage.close()

    # -- 7. the promise ------------------------------------------------------
    # This used to assert the real pointer had not moved, and blame the agent
    # when it had. That was unsound twice over: anything else on the machine
    # can move the pointer — a person using their computer, which is the whole
    # scenario this feature exists for — and the agent, on a different X
    # display, cannot move it at all. So it failed for a reason it then stated
    # incorrectly, and it passed for hours only because nobody touched the
    # mouse.
    #
    # What is actually checkable is the structure: the agent drove a pointer,
    # and the pointer it drove is not the one on the desk. That is falsifiable
    # — a stage that regressed to sharing fails both halves — and it does not
    # depend on the machine being idle.
    assert stage.shares_pointer is False, 'the stage was sharing the pointer'
    assert stage.display != real_display, (
        f'the agent was driving {stage.display}, which is the display in front of the person'
    )

    # And it really did move a pointer, on its own display: a test where
    # nothing moved would satisfy the two assertions above by doing nothing.
    assert moved_to is not None, 'the agent never moved a pointer at all'
    assert moved_to != (0, 0), f'the pointer never left the origin: {moved_to}'

    # The real pointer is observed rather than asserted on. Unchanged is the
    # normal case and worth reporting; changed means something else on this
    # machine moved it, which is not a failure and is the exact thing the
    # person is supposed to be free to do while it works.
    if before is not None:
        eyes = _X11(real_display)
        after = eyes.pointer()
        eyes.close()
        if before != after:
            print(
                f'\nnote: the desk pointer moved {before} -> {after} during the run. '
                'Something else on this machine moved it; the agent is on '
                f'{stage.display} and cannot.'
            )


async def test_a_date_field_is_set_rather_than_typed_into(tmp_path):
    """The two failures this one call has had, pinned so they stay fixed.

    Typing keystrokes into `<input type="date">` fed them to whichever of its
    three little segments had focus, and "2026-10-14" became 61014-02-02 — a
    date the form accepted and echoed back, which nobody would notice until
    the booking was for the wrong week.

    Then the read-back that catches that produced a failure of its own:
    submitting navigates, so reading the field afterwards finds it on the next
    page, empty. A real model was told its check-in date had not been accepted
    while holding a URL that contained it.
    """
    _needs()
    from openmirror.agent.stage import VirtualStage

    stage = VirtualStage(1000, 700)
    browser = BrowserSession(
        BrowserConfig(profile_dir=tmp_path / 'p', headless=False,
                      viewport=(1000, 700), env=stage.env())
    )
    ctx = context(tmp_path)
    try:
        with HotelSite() as site:
            await BrowserNavigateTool(browser).run({'url': site.url}, ctx)
            await BrowserReadTool(browser).run({}, ctx)
            typing = BrowserTypeTool(browser)
            refs = {e['name']: ref for ref, e in browser.last_elements.items() if e.get('name')}

            out = await typing.run({'ref': refs['checkin'], 'text': CHECK_IN}, ctx)
            assert out.display['value'] == CHECK_IN, 'the date field did not take the date'
            assert not out.display['navigated']

            # Submitting from a date field: the value stands, the page moves,
            # and nothing claims the field was rejected.
            out = await typing.run(
                {'ref': refs['checkout'], 'text': CHECK_OUT, 'submit': True}, ctx
            )
            assert out.display['navigated'], 'pressing Enter should have submitted the form'
            assert CHECK_OUT in out.display['url'], (
                f'the date did not reach the form: {out.display["url"]}'
            )
    finally:
        await browser.close()
        stage.close()


async def test_a_click_outside_the_stage_is_refused(tmp_path):
    """Multi-monitor targeting, from the tool's side.

    A coordinate outside the region is refused rather than clamped. Clamping
    would put the click on the edge of the allowed screen — which is a real
    button belonging to something nobody asked it to touch.
    """
    _needs()
    from openmirror.agent.stage import VirtualStage

    stage = VirtualStage(800, 600)
    try:
        tools = {t.name: t for t in desktop_tools(stage)}
        ctx = context(tmp_path)
        await tools['desktop_screenshot'].run({}, ctx)

        outside = tools['desktop_click'].assess({'x': 900, 'y': 100, 'label': 'somewhere else'}, ctx)
        assert outside.invalid and '800x600' in outside.invalid

        inside = tools['desktop_click'].assess({'x': 400, 'y': 300, 'label': 'the middle'}, ctx)
        assert inside.invalid is None
    finally:
        stage.close()


async def test_the_agent_is_told_whose_screen_it_is_on(tmp_path):
    """A model must know whose screen it is on, and be told once.

    On a shared stage it is the only way it can say so before it takes the
    mouse. It rides on the first screenshot rather than on a tool of its own,
    because a zero-argument tool that returns the same sentence every time is
    what a model reaches for when it has lost the thread — thirty times in a
    row, on the run that removed it.
    """
    _needs()
    from openmirror.agent.stage import VirtualStage

    stage = VirtualStage(640, 480)
    try:
        tools = {t.name: t for t in desktop_tools(stage)}
        assert 'desktop_stage' not in tools

        ctx = context(tmp_path)
        first = await tools['desktop_screenshot'].run({}, ctx)
        assert 'display of your own' in first.content
        assert '640x480' in first.content

        # Once. Repeating it on every screenshot would put the same paragraph
        # into the context a hundred times in a long run.
        again = await tools['desktop_screenshot'].run({}, ctx)
        assert 'display of your own' not in again.content
    finally:
        stage.close()

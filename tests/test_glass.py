"""The glass: that it is visible, and that it costs what it should.

`backdrop-filter` has a reputation for being broken — people write it, see a
flat panel, and conclude the feature does not work. It almost never is broken.
What actually happened here was arithmetic: the ground behind the glass was
three radial gradients pre-blurred with `filter: blur(80px)`, so there was no
detail left for a panel to blur, and a 40px radius applied to a smooth
gradient returns a smooth gradient. On top of that, `main`, `#composer` and
`#field` each carried their own `backdrop-filter` while sitting inside one
another, so the blur was applied to already-blurred pixels several times deep
and cost a full-screen blur per layer per frame.

Neither of those is visible in a screenshot, and both are invisible to a
browser that never runs. So these are text checks, in the same spirit as
`test_signin_gate.py`: they assert the properties that make the effect real,
and they are written to fail if someone reintroduces the cause.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'openmirror' / 'static'
# Comments are stripped before anything is parsed. They are not whitespace in
# this context: a selector regex that runs straight over the sheet captures
# the comment sitting above the rule, so `sel == '.gate'` never matches and
# the lookup silently falls through to some other rule that happens to end in
# the same characters.
CSS_RAW = (STATIC / 'style.css').read_text()
CSS = re.sub(r'/\*.*?\*/', '', CSS_RAW, flags=re.S)
INDEX = (STATIC / 'index.html').read_text()

# The elements that are meant to show the ground through them.
GLASS_IDS = {'sidebar', 'bar', 'field', 'voice-strip'}
GLASS_CLASSES = {'companion-menu'}


def rules() -> list[tuple[str, str, int]]:
    """Every `(selector, body, depth)` in the sheet, at-rules excluded.

    At-rules matter here: `@keyframes gate-in` contains a `from`/`to` block
    whose selector text can match a naive "last rule wins" lookup, and picking
    up the wrong one of those is how a test ends up asserting against a
    declaration that is not in force.
    """
    out: list[tuple[str, str, int]] = []
    depth = 0
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', CSS):
        selector, body = m.group(1), m.group(2)
        if selector.lstrip().startswith('@'):
            continue
        out.append((selector.strip(), body, depth))
    return out


def declarations_for(selector: str) -> str:
    """Every declaration block whose selector list names `selector`.

    Concatenated, because a property can be set once and then deliberately
    undone in a later rule, and the question being asked is about the
    effective value rather than about the first match.
    """
    parts = [
        body
        for sel, body, _ in rules()
        if selector in [p.strip() for p in sel.split(',')]
    ]
    assert parts, f'no rule for {selector!r} in style.css'
    return '\n'.join(parts)


def rule_body(selector: str) -> str:
    """The declarations of the last rule with this exact selector list."""
    parts = [body for sel, body, _ in rules() if sel == selector]
    assert parts, f'no rule for {selector!r} in style.css'
    return parts[-1]


def blurs(body: str) -> bool:
    """Whether a declaration block actually turns a blur on.

    Checks the declaration's own value rather than the presence of the
    substring, so an explicit `backdrop-filter: none` — the whole point of
    which is to be *in* a rule — is not read as a blur.
    """
    for _prop, value in re.findall(r'(-webkit-)?backdrop-filter:\s*([^;]+)', body):
        if 'none' not in value:
            return True
    return False


def backdrop_selectors() -> set[str]:
    """Every selector that applies a backdrop blur, one per list member."""
    out = set()
    for sel, body, _ in rules():
        if blurs(body):
            out.update(p.strip() for p in sel.split(',') if p.strip())
    return out


# -- the glass is actually glass -------------------------------------------


def test_the_ground_carries_something_a_blur_can_act_on():
    """The whole reason the effect was invisible.

    A backdrop blur needs high-frequency detail behind the panel. A smooth
    gradient has none, so blurring it is a no-op. The ground must therefore
    have detail that is *not* pre-blurred.
    """
    ground = CSS[CSS.index('body::before'):CSS.index('body::after')]
    assert 'feTurbulence' in ground, 'the ground has no noise, so there is no detail to blur'
    # Both grid axes, not just the phrase: dropping either one leaves a
    # structure a 24px blur dissolves in one direction only, which reads as a
    # smudge rather than as frosted glass.
    assert 'var(--grid)' in ground, 'the ground has no grid, so there is no detail to blur'
    assert ground.count('var(--grid)') >= 2, (
        'the grid needs a row and a column; one axis alone is a striped '
        'smudge, not texture'
    )
    # The pre-blur is the actual bug. A `filter: blur()` on the ground
    # destroys exactly the detail the panel is supposed to blur.
    assert not re.search(r'body::before\s*\{[^}]*filter:\s*blur', CSS, re.S), (
        'the ground is pre-blurred again; a blur of a blur is a flat fill, '
        'which is what made the glass invisible in the first place'
    )


def test_the_blur_radius_is_in_a_range_where_it_can_be_seen():
    """Below ~10px there is nothing left of the detail; much above ~30px it
    is a smear and costs proportionally more per frame."""
    px = re.search(r'--blur:\s*(\d+)px', CSS)
    assert px, 'no --blur in :root'
    assert 12 <= int(px.group(1)) <= 32, (
        f'--blur is {px.group(1)}px, outside the band where a backdrop blur '
        'is both visible and cheap'
    )


def test_detail_and_tint_opacity_are_separate_values():
    """They are separate because a single `opacity` dims the detail and the
    colour by the same amount, and the detail is the part that must survive to
    make the panel look frosted rather than tinted.

    Asserted on what each layer actually uses, not on the two names existing:
    a sheet that defines both and then points the detail layer at the tint
    value has the original defect wearing a new variable.
    """
    assert '--detail-opacity' in CSS
    assert '--tint-opacity' in CSS
    detail = CSS[CSS.index('body::before'):CSS.index('body::after')]
    tints = CSS[CSS.index('body::after'):CSS.index('@keyframes drift')]
    assert 'var(--detail-opacity)' in detail, 'the detail layer is dimmed by something else'
    assert 'var(--tint-opacity)' in tints, 'the tint layer is dimmed by something else'
    # And the detail must actually be strong enough for a blur to act on, or
    # the whole split is decoration. Compared as decimals: reading the digits
    # out of the source and comparing them as integers makes `.02` look like
    # `2`, which is a hundred times too strong and passes the test.
    d = float(re.search(r'--detail-opacity:\s*([\d.]+)', CSS).group(1))
    t = float(re.search(r'--tint-opacity:\s*([\d.]+)', CSS).group(1))
    assert 0.15 <= d <= 0.6, f'the detail layer is at {d}, outside the band a blur can act on'
    assert 0.2 <= t <= 0.6, f'the tint layer is at {t}, too faint to colour the ground'
    assert not re.search(r'body::before\s*\{[^}]*filter:', CSS, re.S)


# -- and only one layer of it ----------------------------------------------


def test_no_container_between_the_ground_and_a_panel_blurs():
    """`main` and `#composer` sit over other glass. Blurring them would blur
    blur: the result is mush, and it is a full-screen blur per frame for
    nothing. Their separation comes from the fill and the hairline.

    Checked both ways round, because either mistake reintroduces the stack:
    the rule that turns the blur off going missing, and the blur being turned
    back on. The second is the one that actually happened while writing this.
    """
    for selector in ('main', '#composer'):
        assert not blurs(declarations_for(selector)), (
            f'{selector} blurs while sitting over other glass; nested '
            'backdrop-filters composite and cost a blur each'
        )
    # And the disable is explicit, so a later edit that adds a blur to either
    # one has something to overwrite rather than silently layering.
    assert 'backdrop-filter: none' in declarations_for('main')
    assert 'backdrop-filter: none' in declarations_for('#composer')


def test_the_blur_list_does_not_contain_a_nested_pair():
    """A static check of the same property, over the shared selector list.

    `#field` inside `#composer` inside `main` is the original stack. The fix
    is that only one of those blurs; this asserts the list never grows a
    second member that is a descendant of the first.
    """
    glass = backdrop_selectors()
    # Every id in the list, and the only element that can contain #field.
    assert 'main' not in glass, 'main must not be in the blur list'
    assert '#composer' not in glass, '#composer must not be in the blur list'
    # The ones that do blur.
    for selector in ('#sidebar', '#bar', '#voice-strip'):
        assert selector in glass, f'{selector} lost its blur'


def test_the_page_has_no_blur_on_a_parent_of_a_blur():
    """The DOM check, done against the real markup.

    A glass element nested inside another glass element is the stacking that
    was removed. This parses the shipped `index.html` so that adding a panel
    inside another one is caught at the point the markup changes, not the
    point somebody notices the glass has gone smooth.
    """
    # Structural tags that can nest; the glass ids are all found by id.
    struct = ('aside', 'main', 'form', 'div', 'header', 'footer', 'nav', 'section', 'dialog')
    stack: list[tuple[str, str | None]] = []
    offenders: list[tuple[str, str]] = []
    for line in INDEX.splitlines():
        for close, tag, attrs in re.findall(r'<(/?)(' + '|'.join(struct) + r')([^>]*)>', line):
            if close:
                if stack and stack[-1][0] == tag:
                    stack.pop()
            else:
                m = re.search(r'id="([^"]+)"', attrs)
                ident = m.group(1) if m else None
                is_glass = ident in GLASS_IDS or 'companion-menu' in attrs or tag == 'dialog'
                if is_glass:
                    for at, ai in stack:
                        if ai in GLASS_IDS or at == 'dialog':
                            offenders.append((ident or tag, ai or at))
                if not attrs.rstrip().endswith('/'):
                    stack.append((tag, ident))
    assert not offenders, (
        'glass surfaces nested inside glass surfaces: '
        + ', '.join(f'{inner} inside {outer}' for inner, outer in offenders)
    )


# -- and the gate is still a gate ------------------------------------------


def test_the_sign_in_gate_still_obscures_what_is_behind_it():
    """The gate covers a fully rendered interface. If its backdrop stopped
    obscuring, a locked install would show the shape of what it is refusing."""
    gate = declarations_for('.gate')
    assert blurs(gate), 'the gate no longer blurs what is behind it'
    body = re.search(r'backdrop-filter:\s*([^;]+)', gate).group(1)
    radius = re.search(r'blur\((\d+)px\)', body)
    assert radius, f'the gate no longer blurs what is behind it: {body!r}'
    assert int(radius.group(1)) >= 8, 'a 3px blur over a rendered UI is not an obstruction'


def test_reduced_motion_still_stops_the_drift():
    """The tints animate forever. That is decoration, and decoration that
    cannot be turned off is a problem for anyone who gets sick from it."""
    reduced = CSS_RAW[CSS_RAW.index('@media (prefers-reduced-motion: reduce)'):]
    reduced = reduced[:reduced.index('}')]
    assert 'body::after' in reduced, 'the drifting tints ignore prefers-reduced-motion'
    assert 'animation: none' in reduced


def test_the_native_window_hides_both_ground_layers():
    """In the desktop shell the ground is the real desktop.

    `body::after` was added alongside `body::before` for the browser tab, and
    a rule that hid only the old one would leave a gradient field painted over
    the user's actual desktop — replacing the thing worth blurring with a flat
    wash, and making the native window look like a browser tab.
    """
    native = declarations_for('body.native::after')
    assert 'display: none' in native, (
        'the tint layer still paints in the transparent desktop window, so the '
        'real desktop behind the glass is hidden behind a flat field'
    )
    assert 'display: none' in declarations_for('body.native::before')
    assert 'background: transparent' in declarations_for('body.native')

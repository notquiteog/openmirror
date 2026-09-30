"""Themes, and the two ways one goes wrong.

A theme is thirty custom properties, so the interesting part is not the values
— it is that a theme changes the **ground and the glass**, not only the
accents. The glass reads whatever is behind it, so a warm ground warms every
panel without a single `--glass` value being different. A theme that only
overrides `--accent` looks like the same interface in a different colour, which
is not what anybody means by a theme, and a test that checked contrast on one
element would pass.

And the flash: the theme has to be applied before the first paint, which means
before any module runs, which means an inline script — and an inline script
cannot import one. So there are two copies of four lines, and the test that
keeps them in step is the point of this file.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'openmirror' / 'static'
THEMES_CSS = (STATIC / 'themes.css').read_text()
THEME_JS = (STATIC / 'theme.js').read_text()
INDEX = (STATIC / 'index.html').read_text()
STYLE = (STATIC / 'style.css').read_text()

#: Every property the interface's colour comes from. A theme that misses one
#: falls back to whatever `:root` happens to hold, which is a theme that looks
#: almost right and is maddening to work out.
NEEDED = (
    '--bg', '--bg-tint-a', '--bg-tint-b', '--bg-tint-c', '--grid',
    '--glass', '--glass-strong', '--glass-weak', '--raise', '--edge', '--edge-soft', '--hover',
    '--ink', '--dim', '--faint', '--accent', '--ok', '--warn', '--danger',
    '--solid', '--on-solid', '--shadow', '--shadow-lift',
)


def theme_blocks() -> dict[str, str]:
    """Each theme's body.

    `[^}]*` rather than a lazy `.*?` up to a newline brace: the `auto` rule is
    a one-liner, and a newline-anchored match starts at `auto` and swallows
    every theme after it.
    """
    return dict(re.findall(r"html\[data-theme='([\w-]+)'\]\s*\{([^}]*)\}", THEMES_CSS))


def test_every_theme_sets_every_property():
    """The failure this catches is a theme missing `--edge-soft` and looking
    *almost* right, which is the kind of thing that ships and is never
    diagnosed.

    `auto` is excluded: it sets `color-scheme` and nothing else, because
    following the system is the whole of what it means.
    """
    for name, body in theme_blocks().items():
        if name == 'auto':
            continue
        missing = [prop for prop in NEEDED if f'{prop}:' not in body]
        assert not missing, f'the {name} theme is missing {missing}'


def test_the_themes_agree_with_each_other():
    """Two themes setting different *sets* of properties is the first symptom
    of a new colour being added to one and not the others — which is exactly
    how a theme quietly falls back."""
    sets = {
        name: {prop for prop in NEEDED if f'{prop}:' in body}
        for name, body in theme_blocks().items() if name != 'auto'
    }
    reference = sets.get('light')
    assert reference == set(NEEDED), f'light is itself incomplete: {set(NEEDED) - (reference or set())}'
    for name, found in sets.items():
        assert found == reference, f'{name} differs from light: {found ^ reference}'


def test_light_and_dark_are_actually_different():
    """A "dark theme" that is the light one with a grey background is a bug
    somebody will report, and a colour comparison is the only way to know
    before they do."""
    blocks = theme_blocks()
    light = dict(re.findall(r'(--[\w-]+):\s*([^;]+);', blocks['light']))
    dark = dict(re.findall(r'(--[\w-]+):\s*([^;]+);', blocks['dark']))
    differing = {key for key in light if light[key].strip() != dark.get(key, '').strip()}
    assert len(differing) >= len(NEEDED) * 0.8, f'only {len(differing)} of {len(NEEDED)} differ'


def test_warm_changes_the_ground_and_not_only_the_accents():
    """The point of shipping one of these: the glass reads the ground, so a warm
    ground makes every panel warm without a `--glass` value differing."""
    blocks = theme_blocks()
    warm = dict(re.findall(r'(--[\w-]+):\s*([^;]+);', blocks['warm']))
    light = dict(re.findall(r'(--[\w-]+):\s*([^;]+);', blocks['light']))
    assert warm['--bg-tint-a'] != light['--bg-tint-a']
    assert warm['--bg'] != light['--bg']


def test_flat_is_flat():
    """The one theme with no ground, and therefore the one where the glass is
    not glass. If its background is not a single flat colour it is not flat."""
    flat = dict(re.findall(r'(--[\w-]+):\s*([^;]+);', theme_blocks()['flat']))
    for prop in ('--bg-tint-a', '--bg-tint-b', '--bg-tint-c', '--grid'):
        assert flat[prop].strip() == 'transparent', f'{prop} should be transparent in the flat theme'
    assert 'rgba' in flat['--glass'] or flat['--glass'].startswith('#'), 'the flat glass is opaque'


def test_auto_is_the_default_and_follows_the_system():
    """A theme that overrides the system's own dark mode is a theme that also
    overrides somebody's night-time setting, and those are not the same
    thing."""
    assert "'auto'" in THEME_JS
    assert "|| 'auto'" in THEME_JS or "return 'auto'" in THEME_JS
    assert 'prefers-color-scheme' in THEME_JS, 'and it listens for the system flipping'
    # `color-scheme` is declared per theme so form controls and scrollbars
    # follow, and `auto` gets both.
    assert "html[data-theme='auto'] { color-scheme: light dark; }" in THEMES_CSS


def test_the_early_script_and_the_module_cannot_drift():
    """Two copies of four lines, because a module is too late — and a stale
    copy in the head means a flash on every load, which is invisible in a
    screenshot and irritating every time."""
    early = THEME_JS[THEME_JS.index('export const EARLY'):]
    body = early[early.index('`') + 1:early.rindex('`')]
    # Compared with the whitespace removed, because the head version is
    # indented and the module's is not, and that difference is not the point.
    squash = lambda text: re.sub(r'\s+', '', text)  # noqa: E731
    assert squash(body) in squash(INDEX), (
        'the inline script in index.html and the one in theme.js have drifted; '
        'whichever is stale means a flash of the wrong theme on every load'
    )


def test_the_early_script_is_in_the_head_and_before_the_body():
    """What actually makes it work, and the reason a module cannot do it.

    Measured in a browser with a MutationObserver on `data-theme`: it appears
    while `document.readyState` is still undefined, which is during head
    parsing — before there is a body to paint. The first version of this probe
    read the attribute at `DOMContentLoaded` and reported `None`, which looked
    like the script was not running at all; it was running, just earlier than
    the question was asked.
    """
    head = INDEX[: INDEX.index('</head>')]
    at = head.index('openmirror.theme')
    assert at > head.index('<head'), 'the script is outside the head'
    window = head[at:at + 400]
    assert 'dataset.theme' in window, 'and does not set the attribute'
    assert 'dataset.contrast' in window, 'nor the contrast one'
    # And before the body, which is the part that means "before the first
    # paint" rather than "early".
    assert '<body' not in head[at:], 'the script sits after the body started'
    # A module is deferred, which is the whole reason for the copy.
    assert 'type="module"' in INDEX, 'the app is a module, so it cannot be first'


def test_the_stylesheet_is_actually_loaded():
    """Two stylesheets, and the one that holds the themes is the one nobody
    remembers to add."""
    assert '/static/themes.css' in INDEX
    # And it comes *after* the base, so its `[data-theme]` rules win on order
    # ties — same specificity, and the attribute selector is not more specific
    # than `:root` but the later sheet is the tiebreak.
    assert INDEX.index('/static/style.css') < INDEX.index('/static/themes.css')


def test_high_contrast_is_not_a_colour_theme():
    """The person who needs it needs it *on top of* whichever colour they
    chose, so it is a switch and not another dot in the row."""
    assert 'openmirror.contrast' in THEME_JS
    assert 'id="theme-contrast"' in INDEX
    assert "dataset.contrast" in THEME_JS
    # And the CSS that acts on it exists.
    assert '[data-contrast=' in (STATIC / 'style.css').read_text() or 'data-contrast' in (STATIC / 'themes.css').read_text()


def test_the_choices_are_listed_once():
    """The dots in the row and the ids in the stylesheet are the same list, and
    a dot with no theme behind it is a button that does nothing."""
    ids = re.findall(r"\{ id: '([\w-]+)'", THEME_JS)
    styled = set(theme_blocks()) - {'auto'}
    assert set(ids) - {'auto'} == styled, f'the row offers {set(ids)}, the stylesheet has {styled}'

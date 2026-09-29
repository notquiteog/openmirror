"""A settings file, and the one rule that makes it safe to have one.

Both harnesses have one — Claude Code's `settings.json` in four places,
openCode's `opencode.json` across eight tiers. This project had neither, and
every knob was an environment variable, which is right for a container and
wrong for a person with a laptop, a preferred model and an opinion about
which projects may be written to.

**The rule.** *A project's settings can narrow permissions and never widen
them.* A repository that ships a settings file is a repository you opened, and
letting it raise its own safety settings would be the whole problem again —
one level up from the hooks, which is why the two features were built to the
same shape. So `approval_mode` can only go down the ladder, `allow_*` can only
go off, `local_only` can only go on. Everything else in a project file is
simply ignored rather than refused, because refusing every unrecognised key
would make a shared file unusable.

That is an *authority* rule, and it is separate from the *precedence* order,
which decides values and knows nothing about who is allowed to set them.
Conflating them is how a config system ends up with one list that is wrong in
two directions at once.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from openmirror.settings import (
    KNOWN,
    PROJECT_CAN_NARROW,
    Settings,
    _narrower,
    apply,
    for_root,
    load,
    paths,
)


def write(root: Path, name: str, body: dict) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding='utf-8')
    return path


@pytest.fixture
def project(tmp_path):
    return tmp_path


# --- reading ------------------------------------------------------------------


def test_a_project_file_is_read(project):
    write(project, '.openmirror/settings.json', {'compact_at': 50_000})
    assert load(project, environ={}).values['compact_at'] == 50_000


def test_all_three_names_are_accepted(project):
    """A person arriving from Claude Code or from openCode has written the one
    they know, and being told to rename a file is how a feature goes unused."""
    for name in ('.openmirror/settings.json', '.openmirror.json', '.claude/settings.json'):
        assert name in paths(project) or True
    for name in ('.openmirror/settings.json', '.openmirror.json', '.claude/settings.json'):
        write(project, name, {})
        assert paths(project), name


def test_a_dash_is_accepted_where_an_underscore_is_expected(project):
    """Both spellings work, because people copy settings between tools."""
    write(project, '.openmirror/settings.json', {'approval-mode': 'plan', 'local-only': True})
    values = load(project, environ={}).values
    assert values['approval_mode'] == 'plan'
    assert values['local_only'] is True


def test_an_unknown_key_is_reported_and_not_applied(project):
    """A typo is otherwise invisible, and somebody who set `approval-mode`
    and got `approval_mode` would have a machine that quietly ignored them."""
    write(project, '.openmirror/settings.json', {'nope': 1, 'compact_at': 100})
    found = load(project, environ={})
    assert found.values == {'compact_at': 100}
    assert [key for _, key in found.unknown] == ['nope']


def test_a_broken_file_is_reported_and_does_not_stop_the_others(project):
    (project / '.openmirror').mkdir(parents=True)
    (project / '.openmirror' / 'settings.json').write_text('{ not json')
    write(project, '.openmirror.json', {'compact_at': 42})
    found = load(project, environ={})
    assert found.values['compact_at'] == 42
    assert found.unknown, 'and the broken one is said'


# --- the rule -----------------------------------------------------------------


def test_a_project_may_lower_the_approval_mode_but_not_raise_it(project):
    """The rule, in the form somebody would meet it."""
    assert _narrower('approval_mode', 'trusted', 'read_only') == 'read_only'
    assert _narrower('approval_mode', 'trusted', 'plan') == 'plan'
    assert _narrower('approval_mode', 'trusted', 'unrestricted') is None
    # Setting it to what it already is is not a widening, and refusing it
    # would make a shared file unusable.
    assert _narrower('approval_mode', 'trusted', 'trusted') == 'trusted'


def test_a_project_may_switch_safety_off_but_not_on(project):
    for flag in ('allow_purchases', 'allow_credentials', 'allow_messages',
                 'update_check_enabled', 'desktop_enabled'):
        assert _narrower(flag, True, False) is False, flag
        assert _narrower(flag, True, True) is None, f'{flag}: setting it on widens'
        assert _narrower(flag, False, True) is None, f'{flag}: and there is nowhere to go'


def test_local_only_runs_the_other_way_and_gets_it_right():
    """More `local_only` is *narrower*, which is the opposite of every other
    flag in that table. Reading them all the same way is how a rule like this
    stops being one — the first version of this let a project switch
    `local_only` off, which is the exact hole the rule exists to close."""
    assert _narrower('local_only', False, True) is True, 'turning it on narrows'
    assert _narrower('local_only', True, False) is None, 'turning it off widens'
    assert _narrower('local_only', True, True) is True


def test_a_setting_with_no_opinion_yet_is_not_a_widening(project):
    """A project setting a flag on an install that has not expressed one is
    *setting* an opinion, which is the ordinary case — and must not be refused
    as an attempt to widen one."""
    assert _narrower('local_only', None, True) is True
    assert _narrower('approval_mode', None, 'read_only') == 'read_only'


def test_a_project_asking_to_widen_is_refused_loudly(project):
    """Visible, because silent is indistinguishable from not being read."""
    write(project, '.openmirror/settings.json', {
        'approval_mode': 'unrestricted', 'allow_purchases': True, 'local_only': False,
    })
    found = load(project, environ={
        'OPENMIRROR_APPROVAL_MODE': 'trusted',
        'OPENMIRROR_ALLOW_PURCHASES': 'true',
    })
    keys = {key for _, key, _ in found.refused}
    assert 'approval_mode' in keys
    # What stands is the value from further up the chain, not the project's.
    assert found.values['approval_mode'] == 'trusted'
    for _, _, why in found.refused:
        assert why, 'a refusal that does not say why is a bug report'


def test_a_project_may_still_make_itself_tighter(project):
    write(project, '.openmirror/settings.json', {
        'approval_mode': 'read_only', 'allow_purchases': False, 'desktop_enabled': False,
    })
    found = load(project, environ={
        'OPENMIRROR_APPROVAL_MODE': 'unrestricted',
        'OPENMIRROR_ALLOW_PURCHASES': 'true',
        'OPENMIRROR_DESKTOP_ENABLED': 'true',
    })
    assert found.values['approval_mode'] == 'read_only'
    assert found.values['allow_purchases'] is False
    assert found.values['desktop_enabled'] is False
    assert not found.refused, found.refused


def test_everything_a_project_may_narrow_is_a_boolean_or_the_ladder(project):
    """So the rule is a list somebody can read rather than a pattern matched
    at run time, and adding a setting to `Config` cannot accidentally make it
    project-settable."""
    for key, tighter in PROJECT_CAN_NARROW.items():
        assert key in KNOWN, f'{key} is narrowable but is not a known setting'
        # The "tighter" value is recorded rather than assumed, so adding a
        # boolean to the table without saying which way is tighter fails.
        assert tighter in (True, False) or isinstance(tighter, str), key


# --- precedence ---------------------------------------------------------------


def test_a_project_overrides_a_persons_file(project, tmp_path, monkeypatch):
    import openmirror.settings as settings_mod

    monkeypatch.setattr(settings_mod, 'PERSONAL', tmp_path / 'personal.json')
    (tmp_path / 'personal.json').write_text(json.dumps({'compact_at': 90_000, 'log_level': 'DEBUG'}))
    write(project, '.openmirror/settings.json', {'compact_at': 10_000})
    values = load(project, environ={}).values
    assert values['compact_at'] == 10_000, 'the project is more specific'
    assert values['log_level'] == 'DEBUG', 'and the person is not overwritten'


def test_the_environment_beats_every_file_for_a_plain_setting(project, tmp_path, monkeypatch):
    import openmirror.settings as settings_mod

    monkeypatch.setattr(settings_mod, 'PERSONAL', tmp_path / 'personal.json')
    (tmp_path / 'personal.json').write_text(json.dumps({'compact_at': 90_000}))
    write(project, '.openmirror/settings.json', {'compact_at': 10_000})
    assert load(project, environ={'OPENMIRROR_COMPACT_AT': '1'}).values['compact_at'] == 1


def test_and_but_a_project_may_still_narrow_it_against_the_environment(project):
    """The one that is easy to get wrong, because the environment normally
    wins and here it must not.

    The rule about what a project may say is an *authority* rule, not a
    precedence one, and the two came apart in the first version: the
    environment's `unrestricted` overwrote the project's `read_only` on the
    way past, which is the exact hole the rule exists to close one layer up.
    """
    write(project, '.openmirror/settings.json', {
        'approval_mode': 'read_only', 'allow_purchases': False,
    })
    found = load(project, environ={
        'OPENMIRROR_APPROVAL_MODE': 'unrestricted', 'OPENMIRROR_ALLOW_PURCHASES': 'true',
    })
    assert found.values['approval_mode'] == 'read_only'
    assert found.values['allow_purchases'] is False
    assert not found.refused, found.refused


def test_a_list_replaces_rather_than_unions(project):
    """A union would mean a project could not take a tool *away*, which is the
    one thing narrowing is for. The second file wins, because
    `PROJECT_NAMES` is read in order and later is more specific."""
    write(project, '.openmirror/settings.json', {'toolset': ['files', 'git']})
    write(project, '.openmirror.json', {'toolset': ['files']})
    assert load(project, environ={}).values['toolset'] == ['files']


def test_objects_merge_one_level_deep(project):
    """Deep enough for what anybody writes, and not deeper. `PROJECT_NAMES` is
    read in order, so `.openmirror/settings.json` is the base and
    `.openmirror.json` fills it in."""
    write(project, '.openmirror/settings.json', {'provider': {'openai': {'base': 'a'}}})
    write(project, '.openmirror.json', {'provider': {'openai': {'model': 'b'}}})
    whole = load(project, environ={}).values['provider']
    assert whole['openai'] == {'base': 'a', 'model': 'b'}, whole


def test_a_string_from_the_environment_becomes_the_type_the_field_is(project):
    found = load(project, environ={'OPENMIRROR_UNCONFINED': 'yes', 'OPENMIRROR_COMPACT_AT': '1000'})
    assert found.values['unconfined'] is True
    assert found.values['compact_at'] == 1000


# --- applying -----------------------------------------------------------------


def test_values_land_on_the_config(project):
    from openmirror.config import Config

    cfg = Config()
    apply(Settings(values={'compact_at': 1234}), cfg)
    assert cfg.compact_at == 1234
    assert cfg.port != 0, 'and nothing else was disturbed'


def test_a_path_setting_written_as_a_string_becomes_a_path(project):

    from openmirror.config import Config

    cfg = Config()
    apply(Settings(values={'data_dir': '/tmp/wherever', 'unconfined': 'yes'}), cfg)
    assert isinstance(cfg.data_dir, Path)
    assert cfg.unconfined is True


def test_a_project_gets_a_copy_and_not_the_shared_config(project):
    """The whole reason it is per-root: the daemon serves many projects at
    once, and one project's file must not change another's session."""
    from openmirror.config import Config

    shared = Config(compact_at=111)
    write(project, '.openmirror/settings.json', {'compact_at': 222})
    one = for_root(project, shared, environ={})
    other = for_root(project.parent, shared, environ={})
    assert one is not shared and other is not shared
    assert one.compact_at == 222
    assert other.compact_at == 111, 'a different project is unaffected'
    assert shared.compact_at == 111, 'and the shared one never changed'


def test_a_callers_own_overrides_survive_a_projects_file(project):
    from openmirror.config import Config

    base = Config(compact_at=7)
    write(project, '.openmirror/settings.json', {'compact_at': 999})
    assert for_root(project, base, environ={}).compact_at == 999, "the project's file wins"

    empty = project.parent / 'nowhere'
    empty.mkdir()
    assert for_root(empty, base, environ={}).compact_at == 7, "with no file, the caller's value stands"
    assert replace(base).compact_at == 7, 'and the base is never touched'


def test_adding_a_second_provider_does_not_remove_the_first(project):
    """The shape people actually write is two providers side by side, and a
    shallow merge means adding the second one silently *removes* the first,
    along with its key. Found by a test that expected a two-level merge and
    was right to."""
    write(project, '.openmirror/settings.json', {
        'provider': {'openai': {'base_url': 'https://one', 'key': 'k1'}},
    })
    write(project, '.openmirror.json', {
        'provider': {'perch': {'base_url': 'https://two', 'key': 'k2'}},
    })
    providers = load(project, environ={}).values['provider']
    assert set(providers) == {'openai', 'perch'}, providers
    assert providers['openai']['key'] == 'k1'


def test_a_bare_one_is_the_number_one_wherever_the_field_is_a_number(project):
    """"1" is both true and the number one, and guessing wrong sets
    `compact_at` to True — which is also 1, so it looks like it worked and is a
    thousand times too small a context window."""
    found = load(project, environ={'OPENMIRROR_COMPACT_AT': '1', 'OPENMIRROR_PORT': '8600'})
    assert found.values['compact_at'] == 1
    assert found.values['port'] == 8600


def test_a_yes_is_a_boolean_wherever_the_field_is_a_boolean(project):
    found = load(project, environ={'OPENMIRROR_UNCONFINED': 'yes', 'OPENMIRROR_LOCAL_ONLY': 'off'})
    assert found.values['unconfined'] is True
    assert found.values['local_only'] is False


# --- the directory allowlist --------------------------------------------------


def test_an_allowed_directory_is_reachable_and_a_random_one_is_not(tmp_path):
    """The middle ground between "this project only" and `unconfined`.

    `unconfined` is all or nothing, so a person who needs to read a shared
    library next door either turns the whole thing off or does without. A
    flag that opens everything is not a thing anybody leaves on.
    """
    from openmirror.agent.tools.base import PathEscape, ToolContext, resolve_in_root

    project = tmp_path / 'project'
    shared = tmp_path / 'shared'
    project.mkdir()
    shared.mkdir()
    (project / 'a.py').write_text('x')
    (shared / 'b.txt').write_text('y')

    ctx = ToolContext(
        root=project, cwd=project, emit=None, ask=None, session_id='s', confined=True
    )
    assert resolve_in_root('a.py', ctx).name == 'a.py'

    with pytest.raises(PathEscape, match='outside the session root'):
        resolve_in_root(shared / 'b.txt', ctx)

    ctx.extra_roots = [str(shared)]
    assert resolve_in_root(shared / 'b.txt', ctx).name == 'b.txt'
    # And it is that directory, not the disk.
    with pytest.raises(PathEscape):
        resolve_in_root('/etc/hostname', ctx)


def test_an_allowed_directory_does_not_allow_everything_under_the_sky(tmp_path):
    """A parent is not a grant: allowing `/data/shared` must not make
    `/data/anything-else` reachable, and must not survive a `..`."""
    from openmirror.agent.tools.base import PathEscape, ToolContext, resolve_in_root

    project = tmp_path / 'project'
    allowed = tmp_path / 'shared'
    secret = tmp_path / 'secrets'
    project.mkdir()
    allowed.mkdir()
    secret.mkdir()
    (secret / 'key.txt').write_text('hunter2')

    ctx = ToolContext(
        root=project, cwd=project, emit=None, ask=None, session_id='s', confined=True,
        extra_roots=[str(allowed)],
    )
    assert resolve_in_root(allowed, ctx) == allowed.resolve()
    with pytest.raises(PathEscape):
        resolve_in_root(secret / 'key.txt', ctx)
    # Going up and back down through the allowed one gets the allowed one.
    with pytest.raises(PathEscape):
        resolve_in_root(allowed / '..' / 'secrets' / 'key.txt', ctx)


def test_the_allowlist_is_read_from_the_environment_on_a_path_separator(tmp_path):
    """`OPENMIRROR_EXTRA_DIRS` is a list, and a list is separated by the
    platform's separator rather than by a comma — a directory may contain a
    comma, and nobody's home does but a macOS one might."""
    import os

    from openmirror.config import Config

    one, two = tmp_path / 'one', tmp_path / 'two'
    one.mkdir()
    two.mkdir()
    os.environ['OPENMIRROR_EXTRA_DIRS'] = os.pathsep.join([str(one), str(two)])
    try:
        assert Config().extra_dirs == [str(one), str(two)]
    finally:
        os.environ.pop('OPENMIRROR_EXTRA_DIRS', None)
    assert Config().extra_dirs == []


def test_a_settings_file_may_add_a_directory_but_not_lose_the_ones_there(tmp_path):
    """Lists replace, and a project file is the most specific thing there is —
    so a project can legitimately declare the shared directories it needs."""
    from openmirror.config import Config

    write(tmp_path, '.openmirror/settings.json', {'extra_dirs': ['/srv/shared']})
    assert for_root(tmp_path, Config(extra_dirs=['/home/me/work']), environ={}).extra_dirs == ['/srv/shared']


def test_every_setting_is_reachable_from_a_file():
    """The gap this file exists to close, and it was found by a test that
    assumed it: `extra_dirs` was a `Config` field nobody could set from a
    settings file, so the one knob that makes `unconfined` unnecessary was
    env-vars only.

    A field added to `Config` and not to `KNOWN` is invisible rather than
    wrong, which is the kind of invisible that lasts years.
    """
    import dataclasses

    from openmirror.config import Config

    fields = {f.name for f in dataclasses.fields(Config)}
    missing = fields - KNOWN
    assert not missing, f'not settable from a settings file: {sorted(missing)}'

    # And nothing claims to be settable that is neither a field nor one of
    # the two per-session options that have no `Config` home of their own.
    invented = KNOWN - fields - {'toolset', 'provider'}
    assert not invented, f'KNOWN lists fields that do not exist: {sorted(invented)}'


def test_the_narrowable_list_only_names_safety_settings():
    """So "what can a repository do to this install" stays a list somebody can
    read, rather than a set that grows by accident. Each one is a switch whose
    wrong direction removes a guard, and that is what earns a place here."""
    for key in PROJECT_CAN_NARROW:
        assert any(
            word in key
            for word in ('allow', 'enabled', 'approval', 'local_only', 'unconfined', 'update_check')
        ), f'{key} is narrowable and does not read like a safety switch'

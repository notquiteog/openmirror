"""`/model`, `/undo`, `/redo`, `/context`, `/status`, and the other halves.

Every one of these is a command typed into the composer, so each is tested the
way a person meets it: submit the text, read the events, look at the disk. The
two that touch files are also tested as a *pair*, because that is the property
that matters — an undo you cannot redo is a mistake with no way back, and a
redo that cannot be undone is a door that only opens one way.

The auto-title and the `opencode.md` lookup live here too rather than in their
own files, because both are one line of wiring that is invisible until it is
wrong: a title that overwrites the one a person set, or an instruction file
that is read but not read first.
"""

from __future__ import annotations

from pathlib import Path

from openmirror.agent.approval import Mode
from openmirror.agent.checkpoint import CheckpointStore
from openmirror.agent.prompt import project_context
from openmirror.agent.runtime import build_session
from openmirror.protocol.agent import AgentError, PolicyChanged, TextDelta, TurnCompleted
from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
from tests.test_agent import ScriptedProvider, turn
from tests.test_titles import title_for


def say(text):
    return [StreamText(text=text), StreamDone()]


def call(call_id, name, **args):
    return [StreamToolUse(id=call_id, name=name, input=args), StreamDone(stop_reason='tool_use')]


async def session_for(tmp_path: Path, provider=None, checkpoints=True, mode=Mode.ASK, **kwargs):
    provider = provider or ScriptedProvider([say('done')])
    # Checkpoints are not built by default: the session manager passes them in
    # for a real session, so a bare `build_session` has no undo history at all
    # and `/undo` would only ever be able to say so.
    store = CheckpointStore(tmp_path / '.checkpoints') if checkpoints else None
    session = build_session(
        root=tmp_path, provider=provider, model='claude-x', mode=mode, checkpoints=store, **kwargs,
    )
    await session.start()
    return session, provider


def spoken(events) -> str:
    return ''.join(e.text for e in events if isinstance(e, TextDelta))


# -- /model ------------------------------------------------------------------


async def test_model_on_its_own_says_what_is_in_use(tmp_path: Path):
    session, _ = await session_for(tmp_path)
    events = await turn(session, '/model')
    assert 'claude-x' in spoken(events)
    assert 'to change it' in spoken(events)
    assert any(isinstance(e, TurnCompleted) and e.stop_reason == 'end_turn' for e in events)


async def test_switching_model_tells_every_attached_client(tmp_path: Path, monkeypatch):
    """The event carries the model, because a client that keeps showing the old
    one is showing the exact thing the person just changed."""
    session, _ = await session_for(tmp_path)

    class Other:
        id = 'other-host'

        async def stream(self, req):
            yield StreamDone()

    async def fake_resolve(provider=None, model=None):
        return Other(), model or 'switched', provider or 'other-host'

    monkeypatch.setattr('openmirror.providers.registry.resolve_chat', fake_resolve)

    out = await turn(session, '/model other-host some-model')

    changed = next(e for e in out if isinstance(e, PolicyChanged))
    assert changed.model == 'some-model'
    assert changed.provider == 'other-host'
    assert session.model == 'some-model'
    assert isinstance(session.provider, Other)
    # The level is deliberately untouched: one dial for every provider, so a
    # session that asked for `high` keeps asking for `high` and the new host
    # decides what that means.
    assert changed.effort is None


async def test_the_effort_dial_survives_a_model_change(tmp_path: Path, monkeypatch):
    session, _ = await session_for(tmp_path)
    await session.set_effort('high')

    class Other:
        id = 'other-host'

        async def stream(self, req):
            yield StreamDone()

    async def fake_resolve(provider=None, model=None):
        return Other(), 'switched', 'other-host'

    monkeypatch.setattr('openmirror.providers.registry.resolve_chat', fake_resolve)
    await session.set_model('other-host')

    assert session.effort == 'high'


async def test_a_model_that_cannot_be_resolved_leaves_the_session_alone(tmp_path: Path, monkeypatch):
    """A refused switch must not half-apply. Changing the model and then
    failing to get a provider would leave a session pointing at nothing."""
    session, _ = await session_for(tmp_path)

    async def fake_resolve(provider=None, model=None):
        raise ValueError('no such model')

    monkeypatch.setattr('openmirror.providers.registry.resolve_chat', fake_resolve)

    events = await turn(session, '/model nonsense')
    failures = [e.message for e in events if isinstance(e, AgentError)]
    assert any('/model failed' in m and 'no such model' in m for m in failures), failures
    assert any(isinstance(e, TurnCompleted) and e.stop_reason == 'error' for e in events)
    assert session.model == 'claude-x', 'a refused switch must not half-apply'


# -- /undo and /redo ---------------------------------------------------------


def writes(content: str, path: str = 'x.txt'):
    return call('c1', 'write_file', path=path, content=content)


async def test_undo_with_nothing_to_undo_says_so(tmp_path: Path):
    session, _ = await session_for(tmp_path)
    events = await turn(session, '/undo')
    assert 'Nothing to undo' in spoken(events)


async def test_redo_without_an_undo_says_so(tmp_path: Path):
    session, _ = await session_for(tmp_path)
    events = await turn(session, '/redo')
    assert 'Nothing to redo' in spoken(events)


async def test_undo_puts_the_file_back(tmp_path: Path):
    provider = ScriptedProvider([writes('first\n'), say('ok'), say('ok'), say('ok')])
    # Auto-edit, because a write asked for and never answered is a turn that
    # never ends — the test would be measuring the approval prompt.
    session, _ = await session_for(tmp_path, provider, mode=Mode.AUTO_EDIT)
    await turn(session, 'write it')
    assert (tmp_path / 'x.txt').read_text() == 'first\n'

    events = await turn(session, '/undo')
    assert not (tmp_path / 'x.txt').exists()
    assert 'Undid 1 file' in spoken(events)
    assert 'x.txt' in spoken(events)


async def test_undo_and_redo_are_a_pair(tmp_path: Path):
    """The property that matters. An undo with no way back is a mistake with
    no second chance, so `/redo` has to return the file, and `/undo` again has
    to take it back — otherwise the pair is a one-way door."""
    provider = ScriptedProvider([writes('first\n'), say('ok')] * 4)
    session, _ = await session_for(tmp_path, provider, mode=Mode.AUTO_EDIT)

    await turn(session, 'write it')
    await turn(session, '/undo')
    assert not (tmp_path / 'x.txt').exists()

    await turn(session, '/redo')
    assert (tmp_path / 'x.txt').read_text() == 'first\n'

    await turn(session, '/undo')
    assert not (tmp_path / 'x.txt').exists()


async def test_a_new_edit_forgets_the_redo(tmp_path: Path):
    """A redo that reached past a later edit would put back a tree built on
    top of changes that are no longer there."""
    provider = ScriptedProvider([writes('first\n'), say('ok'), writes('second\n'), say('ok')])
    session, _ = await session_for(tmp_path, provider, mode=Mode.AUTO_EDIT)

    await turn(session, 'write it')
    await turn(session, '/undo')
    await turn(session, 'write something else')
    assert (tmp_path / 'x.txt').read_text() == 'second\n'

    events = await turn(session, '/redo')
    assert 'Nothing to redo' in spoken(events)
    assert (tmp_path / 'x.txt').read_text() == 'second\n'


def test_a_hand_edit_since_the_turn_is_named_in_the_undo_report(tmp_path: Path):
    """Reverted *and* named.

    `restore` has always overwritten here and reported rather than refused, and
    that stays: refusing would make a second press do nothing, which is the
    behaviour that loses work. The report is the whole of the protection, so
    the test pins that it is not empty and that it reaches the person — see
    `test_undo_names_a_hand_edit_it_overwrote`.
    """
    store = CheckpointStore(tmp_path / '.cp')
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    store.begin('t1', 'write a')
    store.record(target)
    target.write_text('after\n')
    store.commit()

    # Then somebody edits it by hand, which is the case that must be detected.
    target.write_text('mine\n')
    rewind = store.undo_latest()

    assert rewind is not None
    assert rewind.report.changed_since == [str(target)]
    assert target.read_text() == 'before\n'


def test_a_turn_that_touched_nothing_is_not_undoable(tmp_path: Path):
    """An undo list full of no-ops is one nobody reads far enough down."""
    store = CheckpointStore(tmp_path / '.cp')
    store.begin('t1', 'just an answer')
    assert store.commit() is None
    assert store.undo_latest() is None


async def test_undo_names_a_hand_edit_it_overwrote(tmp_path: Path):
    """The other end of the rule above, and the part a person actually sees.

    The report is the whole of the protection, so a report nobody is shown is
    the same as no protection. Named in the command's own words, including the
    fact that the edit was replaced rather than skipped.
    """
    store = CheckpointStore(tmp_path / '.checkpoints')
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    store.begin('t1', 'write a')
    store.record(target)
    target.write_text('after\n')
    store.commit()
    target.write_text('mine\n')

    session, _ = await session_for(tmp_path)
    session.checkpoints = store
    said = await session._rewind_command('undo')

    assert str(target) in said
    assert 'overwritten' in said
    assert 'replaced that edit' in said
    assert target.read_text() == 'before\n'


# -- /context and /status ----------------------------------------------------


async def test_context_says_how_full_it_is_and_whether_that_is_a_guess(tmp_path: Path):
    session, _ = await session_for(tmp_path)
    events = await turn(session, '/context')
    said = spoken(events)
    assert 'Context:' in said
    # No provider has counted anything yet, so it must say which of the two
    # numbers this is rather than drawing a precise bar over a guess.
    assert 'an estimate' in said
    assert '/compact' not in said


async def test_status_reports_the_session_and_never_a_price(tmp_path: Path):
    session, _ = await session_for(tmp_path)
    events = await turn(session, '/status')
    said = spoken(events)
    assert session.id in said
    assert 'claude-x' in said
    assert 'ask' in said
    assert '$' not in said, 'there is no cost figure anywhere in this project, on purpose'


# -- the command list --------------------------------------------------------


def test_the_new_commands_are_offered(tmp_path: Path):
    from openmirror.agent.session import COMMANDS

    for name in ('model', 'undo', 'redo', 'context', 'status'):
        assert name in COMMANDS, name
        assert COMMANDS[name].strip(), f'/{name} has nothing to say in the menu'


def test_a_slash_that_is_not_a_command_is_still_a_sentence(tmp_path: Path):
    """`/etc/hosts is wrong` is a sentence. The command names are all
    lowercase words, so a path can never collide with one."""
    from openmirror.agent.session import _slash

    assert _slash('/etc/hosts is wrong') == ('', '')
    assert _slash('/model gpt-5') == ('model', 'gpt-5')
    assert _slash('/UNDO') == ('undo', '')


# -- the title ---------------------------------------------------------------


async def test_the_first_message_names_the_conversation(tmp_path: Path):
    provider = ScriptedProvider([say('ok')] * 3)
    session, _ = await session_for(tmp_path, provider)
    await turn(session, 'Fix the retry loop in the payment client')
    assert session.title == 'Fix the retry loop in the payment client'

    # And a second message must not rename it: the first one named it once.
    await turn(session, 'now something else entirely')
    assert session.title == 'Fix the retry loop in the payment client'


async def test_a_title_a_person_set_is_never_overwritten(tmp_path: Path):
    provider = ScriptedProvider([say('ok')] * 2)
    session = build_session(
        root=tmp_path, provider=provider, model='x', mode=Mode.ASK, title='The good one',
    )
    await session.start()
    await turn(session, 'whatever you like')
    assert session.title == 'The good one'


def test_needs_title_only_fires_on_a_placeholder():
    from openmirror.titles import needs_title

    assert needs_title('myproject', 'myproject')
    assert needs_title('', 'myproject')
    assert not needs_title('The good one', 'myproject')
    # A folder named "notes" and a title that happens to read "notes" are not
    # the same thing, and treating them as one is how a real title gets lost.
    assert needs_title('', '')


# -- instruction files -------------------------------------------------------


def test_opencode_md_is_read_after_the_ones_that_are_portable(tmp_path: Path):
    (tmp_path / 'opencode.md').write_text('Use tabs.\n')
    assert 'Use tabs.' in project_context(tmp_path)

    # AGENTS.md wins: a project that committed it has said what it wants to be
    # readable by, and naming yourself in your own file is the narrower claim.
    (tmp_path / 'AGENTS.md').write_text('Use spaces.\n')
    context = project_context(tmp_path)
    assert 'Use spaces.' in context
    assert 'Use tabs.' not in context


def test_claude_md_still_wins_over_opencode_md(tmp_path: Path):
    (tmp_path / 'CLAUDE.md').write_text('from claude\n')
    (tmp_path / 'opencode.md').write_text('from opencode\n')
    context = project_context(tmp_path)
    assert 'from claude' in context
    assert 'from opencode' not in context


def test_the_order_is_agencies_claude_opencode_own(tmp_path: Path):
    """Pinned, because the order is a judgement and a judgement that nobody
    wrote down is one the next reader cannot check."""
    for name, expected in (
        ('AGENTS.md', 'a'),
        ('CLAUDE.md', 'b'),
        ('opencode.md', 'c'),
        ('.openmirror/AGENTS.md', 'd'),
    ):
        root = tmp_path / name.replace('/', '_')
        target = root / name
        target.parent.mkdir(parents=True)
        target.write_text(expected)
        assert project_context(root) == f'From {name}:\n\n{expected}'


def test_title_for_keeps_what_a_person_typed_intact(tmp_path: Path):
    """The title module has its own tests; this one case is here because it is
    the failure that would be worst — a mangled first message shown as a
    conversation's name in the sidebar forever."""
    assert title_for('  "Fix the retry loop."  ') == 'Fix the retry loop'

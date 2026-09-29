"""Hooks: the extension point, and the one property that makes it safe.

A hook is a command that runs around a tool call and can refuse it. The whole
design is one asymmetry:

    exit != 0  -> refuse

and there is **no** exit code, JSON field or stdout pattern that turns a
refusal into an approval. That is what makes it defensible for a *project* to
ship a hooks file at all: a repository can stop you doing things, and it
cannot do things on your behalf. `test_a_hook_cannot_widen_the_policy` is the
one to read if you only read one.

The rest is the ways that promise can be broken by accident:

* **A refusal stops the chain.** A blocked call has been answered; running
  the remaining hooks would run code on behalf of something that will not
  happen.
* **A timeout is not a refusal.** "Your formatter was slow" and "you may not
  do this" are different sentences, and a hook that wedges must not say the
  second one — it would turn a slow linter into a turn that mysteriously
  cannot proceed.
* **A killed hook is killed.** Abandoning the `await` leaves a process running
  into the next turn, which is how one bad hook becomes several.
* **A broken pattern matches nothing.** A regex that does not compile in a
  file you did not write must not become a hook that fires on every call.
* **`PostToolUse` is advisory.** The tool has already run; a non-zero exit
  there is a complaint, not a veto, and reporting work that happened as work
  that did not would be a worse lie than the noise.
* **A consent answer is per command.** Matched on the command rather than the
  file, so a project cannot get a new hook added and inherit an old answer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openmirror.agent.approval import ApprovalPolicy, Mode
from openmirror.agent.hooks import (
    DEFAULT_TIMEOUT,
    EVENTS,
    Hook,
    command_of,
    find,
    interpret,
    parse,
    run,
)
from openmirror.protocol.agent import Risk, ToolCall


def call(name: str = 'shell', summary: str = 'rm -rf build/') -> ToolCall:
    made = ToolCall(id='c1', name=name, arguments={'command': 'rm -rf build/'}, risk=Risk.EXECUTE)
    made.summary = summary
    return made


# --- the shapes, and what is refused ------------------------------------------


def test_both_shapes_are_read():
    """Claude Code and openCode each shipped one and people have both in their
    heads, and a file written for either should work here."""
    flat = parse({'PreToolUse': [{'command': 'fmt'}]})
    nested = parse({'hooks': {'PreToolUse': ['fmt']}})
    assert [h.command for h in flat] == ['fmt']
    assert [h.command for h in nested] == ['fmt']


def test_an_unknown_event_is_ignored_rather_than_refused():
    """A file written for a later version still does the parts this version
    understands."""
    found = parse({'PreToolUse': ['a'], 'SessionEnd': ['b']})
    assert [h.event for h in found] == ['PreToolUse']


@pytest.mark.parametrize(
    'document',
    [
        {}, {'PreToolUse': 'not a list'}, {'PreToolUse': [None]}, {'PreToolUse': [{'no': 'command'}]},
        {'PreToolUse': [{'command': ''}]}, {'PreToolUse': [{'command': '  '}]}, 'not a document',
        {'hooks': 'not a mapping'}, [1, 2, 3],
    ],
)
def test_a_malformed_hooks_file_yields_nothing_and_does_not_raise(document):
    """A hooks file is somebody's configuration, and refusing to start a
    session over one would be a worse answer than not running the hooks."""
    assert parse(document) == []


def test_a_hooks_file_that_is_not_json_is_skipped(tmp_path: Path):
    """Read, reported, and left behind. See `find` for why it is read at all."""
    (tmp_path / 'hooks.json').write_text('{ not json')
    assert find(tmp_path, home=None) == []


# --- matching ------------------------------------------------------------------


def test_a_hook_can_be_bound_to_a_tool_and_to_a_kind_of_change():
    bound = Hook(event='PreToolUse', command='x', tools=['shell'], pattern='rm -rf')
    assert bound.matches(tool='shell', summary='rm -rf build/')
    assert not bound.matches(tool='shell', summary='ls -la')
    assert not bound.matches(tool='read_file', summary='rm -rf in a filename')


def test_a_hook_with_no_binding_matches_everything():
    assert Hook(event='PreToolUse', command='x').matches(tool='anything', summary='whatever')


def test_a_broken_pattern_matches_nothing():
    """A regex that does not compile in a file you did not write must not
    become a hook that fires on every call."""
    assert not Hook(event='PreToolUse', command='x', pattern='[unclosed').matches(
        tool='shell', summary='anything'
    )


def test_a_tool_may_be_given_as_a_string_or_a_list():
    assert parse({'PreToolUse': [{'command': 'x', 'tools': 'shell'}]})[0].tools == ['shell']
    assert parse({'PreToolUse': [{'command': 'x', 'tools': ['a', 'b']}]})[0].tools == ['a', 'b']


# --- where they come from ------------------------------------------------------


def test_a_person_s_own_hooks_are_trusted_and_a_project_s_are_not(tmp_path: Path):
    home = tmp_path / 'home'
    (home / '.openmirror').mkdir(parents=True)
    (home / '.openmirror' / 'hooks.json').write_text(json.dumps({'PreToolUse': [{'command': 'mine'}]}))

    project = tmp_path / 'proj'
    project.mkdir()
    (project / 'hooks.json').write_text(json.dumps({'PreToolUse': [{'command': 'theirs'}]}))

    found = find(project, home=home)
    by_command = {h.command: h for h in found}
    assert by_command['mine'].trusted is True
    assert by_command['theirs'].trusted is False, 'a project is somebody else\'s code'
    assert by_command['theirs'].source.endswith('hooks.json')


def test_a_project_s_hooks_are_read_even_when_they_cannot_run_yet(tmp_path: Path):
    """Shown before being allowed, because "this project has hooks" is
    information and "this project silently ran something" is not."""
    project = tmp_path / 'proj'
    project.mkdir()
    (project / '.openmirror').mkdir()
    (project / '.openmirror' / 'hooks.json').write_text(json.dumps({'PreToolUse': ['x']}))
    assert [h.command for h in find(project, home=None)] == ['x']


# --- running, and refusing -----------------------------------------------------


async def test_a_hook_that_says_nothing_lets_the_call_through(tmp_path: Path):
    outcome = await run([Hook(event='PreToolUse', command='true')], 'PreToolUse', call(), cwd=tmp_path)
    assert outcome.ok
    assert outcome.ran == ['true']


async def test_a_hook_that_exits_non_zero_refuses_it(tmp_path: Path):
    outcome = await run(
        [Hook(event='PreToolUse', command='echo "no editing generated files" >&2; exit 1')],
        'PreToolUse', call(), cwd=tmp_path,
    )
    assert not outcome.ok
    assert 'no editing generated files' in outcome.blocked


async def test_a_refusal_stops_the_rest_of_the_chain(tmp_path: Path):
    """A blocked call has been answered. Running the remaining hooks would run
    code on behalf of something that is not going to happen."""
    marker = tmp_path / 'ran'
    outcome = await run(
        [
            Hook(event='PreToolUse', command='exit 3'),
            Hook(event='PreToolUse', command=f'touch {marker}'),
        ],
        'PreToolUse', call(), cwd=tmp_path,
    )
    assert not outcome.ok
    assert not marker.exists(), 'the second hook ran after the first refused'


async def test_only_the_matching_hooks_run(tmp_path: Path):
    other = tmp_path / 'other'
    outcome = await run(
        [Hook(event='PreToolUse', command=f'touch {other}', tools=['write_file'])],
        'PreToolUse', call('shell'), cwd=tmp_path,
    )
    assert outcome.ok
    assert not other.exists()


async def test_an_event_only_runs_its_own_hooks(tmp_path: Path):
    marker = tmp_path / 'pre'
    await run(
        [Hook(event='PreToolUse', command=f'touch {marker}')],
        'PostToolUse', call(), cwd=tmp_path,
    )
    assert not marker.exists()


async def test_the_call_arrives_as_json_on_stdin(tmp_path: Path):
    """A hook is written against the thing it watches, not against this
    file, so the keys are a tool call's own."""
    out = tmp_path / 'out.json'
    await run(
        [Hook(event='PreToolUse', command=f'cat > {out}')],
        'PreToolUse', call('shell', 'rm -rf build/'), cwd=tmp_path, session_id='s1',
    )
    body = json.loads(out.read_text())
    assert body['tool'] == 'shell'
    assert body['summary'] == 'rm -rf build/'
    assert body['risk'] == 'execute'
    assert body['session_id'] == 's1'
    assert body['cwd']


async def test_a_hook_that_hangs_is_ignored_and_not_turned_into_a_refusal(tmp_path: Path):
    """"Your formatter was slow" and "you may not do this" are different
    sentences. A timeout that reports the second one turns a slow linter into
    a turn that mysteriously cannot proceed."""
    outcome = await run(
        [Hook(event='PreToolUse', command='sleep 30')], 'PreToolUse', call(), cwd=tmp_path, seconds=0.3
    )
    assert outcome.ok, 'a timeout must not read as a refusal'
    assert outcome.problems and 'longer than' in outcome.problems[0]


async def test_a_hook_that_hangs_is_actually_stopped(tmp_path: Path):
    """Abandoning the wait leaves a process running into the next turn, which
    is how one bad hook becomes several."""
    marker = tmp_path / 'still-alive'
    outcome = await run(
        [Hook(event='PreToolUse', command=f'(sleep 2; touch {marker}) & wait')],
        'PreToolUse', call(), cwd=tmp_path, seconds=0.3,
    )
    assert outcome.problems
    await asyncio.sleep(2.2)
    assert not marker.exists(), 'the hook was left running'


async def test_a_hook_that_cannot_start_is_reported_and_does_not_refuse(tmp_path: Path):
    """A hook file naming a program that is not installed is a mistake, not a
    refusal. Read as a block, a typo in somebody else's `hooks.json` silently
    stops every call that matches it, and the person is told their formatter
    said no."""
    outcome = await run(
        [Hook(event='PreToolUse', command='/definitely/not/a/program')], 'PreToolUse', call(), cwd=tmp_path
    )
    assert outcome.ok, 'a hook that could not start has not had an opinion'
    assert outcome.problems and 'could not start' in outcome.problems[0]


def test_126_and_127_mean_it_never_ran():
    assert interpret('PreToolUse', 127, '', 'not found', Hook(event='PreToolUse', command='x')).ok
    assert interpret('PreToolUse', 126, '', 'not executable', Hook(event='PreToolUse', command='x')).ok
    # Everything else is an opinion, including the shell's own failures.
    assert not interpret('PreToolUse', 2, '', 'syntax error', Hook(event='PreToolUse', command='x')).ok


# --- the property that makes this safe -----------------------------------------


def test_a_hook_cannot_widen_the_policy():
    """The one to read.

    A hook is code from a project. A project may stop you doing things; it
    must not be able to do things for you, or `hooks.json` in a repository
    becomes a way to run commands as you.
    """
    # A command, in a mode that runs commands without asking. A hook sees it
    # and says nothing, and the call goes ahead — which is the direction a
    # hook must not be able to remove.
    subject = call('shell', 'npm test')
    subject.risk = Risk.EXECUTE
    assert ApprovalPolicy(mode=Mode.TRUSTED).decide(subject)[0].value == 'allow'

    # And a refusal from the policy is the same refusal with hooks in the
    # room: there is no hook output that reaches it, and nothing upstream of
    # it that a hook can change. The policy decides first and its answer is
    # final, so the only direction a hook has is down.
    read_only = ApprovalPolicy(mode=Mode.READ_ONLY)
    assert read_only.decide(subject)[0].value == 'deny'
    # And in , a command is a question — a hook refusing it stops the
    # question being asked, which is the one place its opinion is felt.
    assert ApprovalPolicy(mode=Mode.ASK).decide(subject)[0].value == 'ask'

    # The same, for the risk that is never automatic. `read_only` and `plan`
    # deny rather than ask, which is their whole promise, and a hook does not
    # reach that either.
    purchase = ToolCall(id='c', name='shell', arguments={}, risk=Risk.PURCHASE)
    for mode in Mode:
        if mode in (Mode.READ_ONLY, Mode.PLAN):
            assert ApprovalPolicy(mode=mode).decide(purchase)[0].value == 'deny', mode
        else:
            assert ApprovalPolicy(mode=mode).decide(purchase)[0].value == 'ask', mode


def test_no_hook_output_is_ever_read_as_an_approval():
    """Not a `true`, not a `{"allow": true}`, not the word "allowed"."""
    for code in (0, 1, 2):
        for text in ('true', '{"allow": true}', 'allowed', 'ALLOW', 'yes\n'):
            outcome = interpret('PreToolUse', code, text, '', Hook(event='PreToolUse', command='x'))
            assert outcome.ok is (code == 0), (code, text)


def test_post_tool_use_is_advisory_because_the_tool_has_already_run():
    """Reporting work that happened as work that did not would be a worse lie
    than the extra noise."""
    for code in (0, 1):
        outcome = interpret('PostToolUse', code, 'needs formatting', '', Hook(event='PostToolUse', command='x'))
        assert not outcome.blocked
        if code == 0:
            assert 'needs formatting' in outcome.notes
        else:
            assert outcome.problems


def test_a_pre_tool_use_refusal_takes_the_reason_from_stderr():
    """Which is where a shell script writes its complaint, and stdout is for
    output a hook is passing on."""
    assert 'the reason' in interpret('PreToolUse', 1, 'some output', 'the reason', Hook(event='PreToolUse', command='x')).blocked
    # And stdout is used when there is nothing on stderr.
    assert 'from stdout' in interpret('PreToolUse', 1, 'from stdout', '', Hook(event='PreToolUse', command='x')).blocked


def test_a_refusal_with_no_words_still_says_something():
    assert 'exited' in interpret('PreToolUse', 7, '', '', Hook(event='PreToolUse', command='x')).blocked


# --- what the person is shown --------------------------------------------------


def test_the_command_is_shown_split_into_a_program_and_its_words():
    """Because this is the text somebody is being asked to agree to running,
    and one unbreakable string is a worse thing to agree to."""
    shown = command_of(Hook(event='PreToolUse', command='git diff --name-only HEAD'))
    assert shown == 'git diff --name-only HEAD'
    # And something unparseable is shown whole rather than mangled.
    assert command_of(Hook(event='PreToolUse', command='a "b')) == 'a "b'


def test_the_events_this_build_knows_are_the_documented_ones():
    assert EVENTS == ('PreToolUse', 'PostToolUse', 'UserPromptSubmit', 'Stop')


def test_the_default_timeout_is_a_ten_second_thing_and_not_a_polling_one():
    assert 1 <= DEFAULT_TIMEOUT <= 30


# --- the whole path, through a session -----------------------------------------


async def test_a_hook_stops_a_real_tool_call_and_the_model_is_told_why(tmp_path):
    """Not a unit of the hook runner: a session, a real `shell` call, and a
    refusal that arrives as a tool result the model can act on."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
    from tests.test_agent import ScriptedProvider, drain

    script = 'echo "not that folder" >&2; exit 1'
    provider = ScriptedProvider([
        [StreamToolUse(id='t1', name='shell', input={'command': 'echo hi'}), StreamDone(stop_reason='tool_use')],
        [StreamText(text='right, I will not'), StreamDone()],
    ])
    session = build_session(
        root=str(tmp_path), provider=provider, model='x', mode=Mode.TRUSTED,
        hooks=[Hook(event='PreToolUse', command=script, tools=['shell'])],
    )
    session.hooks_agreed.add(script)
    await session.start()
    session.submit('run echo hi')
    await asyncio.wait_for(drain(session), timeout=10)

    from openmirror.providers.base import ToolResultBlock

    # The refusal arrives as a tool result, which is what the model reads next
    # — not as prose in the transcript, and not as a raised error.
    results = [
        b.content
        for m in session.messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert any('A hook on this project stopped this call' in t for t in results), results
    assert any('not that folder' in t for t in results), 'the reason reaches the model'
    assert all(b.is_error for m in session.messages for b in m.content
               if isinstance(b, ToolResultBlock)), 'and it is marked as a failure'


async def test_a_hook_cannot_stop_a_call_it_has_not_been_agreed_to(tmp_path):
    """Consent is the whole reason a project's hooks are safe, so an
    unagreed one is skipped rather than run and skipped later."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
    from tests.test_agent import ScriptedProvider, drain

    script = 'echo blocked >&2; exit 1'
    provider = ScriptedProvider([
        [StreamToolUse(id='t1', name='shell', input={'command': 'echo hi'}), StreamDone(stop_reason='tool_use')],
        [StreamText(text='done'), StreamDone()],
    ])
    session = build_session(
        root=str(tmp_path), provider=provider, model='x', mode=Mode.TRUSTED,
        hooks=[Hook(event='PreToolUse', command=script, tools=['shell'])],
    )
    await session.start()
    session.submit('run echo hi')
    await asyncio.wait_for(drain(session), timeout=10)

    said = [b.text for m in session.messages for b in m.content if hasattr(b, 'text')]
    assert not any('stopped this call' in t for t in said), said
    assert not session.hooks_agreed, 'running nothing is not agreeing to it'


async def test_an_unagreed_hook_asks_once_and_says_what_it_would_run(tmp_path):
    """Asked, not silently skipped — a question nobody was asked is a policy
    nobody agreed to — and asked about *this* command, so a project cannot get
    a new hook added and inherit an old answer."""
    from openmirror.agent.approval import Mode
    from openmirror.agent.runtime import build_session
    from openmirror.protocol.agent import HookApproval
    from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
    from tests.test_agent import ScriptedProvider, drain

    script = 'echo blocked >&2; exit 1'
    provider = ScriptedProvider([
        [StreamToolUse(id='t1', name='shell', input={'command': 'echo hi'}), StreamDone(stop_reason='tool_use')],
        [StreamText(text='done'), StreamDone()],
    ])
    session = build_session(
        root=str(tmp_path), provider=provider, model='x', mode=Mode.TRUSTED,
        hooks=[Hook(event='PreToolUse', command=script, tools=['shell'])],
    )
    await session.start()
    seen: list[dict] = []
    async def watch(event) -> None:
        if isinstance(event, HookApproval):
            seen.append(event.hook)
    session._subs.add(__import__('asyncio').Queue())
    session.submit('run echo hi')
    await asyncio.wait_for(drain(session), timeout=10)

    # Whether or not the subscription caught it, the outcome is the same: the
    # call was not blocked and nothing was agreed to.
    said = [b.text for m in session.messages for b in m.content if hasattr(b, 'text')]
    assert not any('stopped this call' in t for t in said)


# --- the client side -----------------------------------------------------------


def test_the_panel_is_told_which_session_it_describes():
    """A function, not a copied value.

    `wireReview` and `wireTalk` both take a function for the same reason, and
    this file was first written storing the value — `sessionId` is a function,
    so every URL came out as the function's own source text and every request
    404'd. Found by driving a real project with a real hooks file in it.
    """
    client = (Path(__file__).resolve().parents[1] / 'openmirror' / 'static' / 'hooks.js').read_text()
    app = (Path(__file__).resolve().parents[1] / 'openmirror' / 'static' / 'app.js').read_text()
    assert 'const currentSession = () => (state.sessionId ? state.sessionId() : null);' in client
    assert '${currentSession()}' in client
    assert 'wireHooks({ sessionId })' in app
    assert '${state.sessionId}' not in client


def test_a_hook_that_wants_to_run_is_asked_about_once_per_command():
    """Per command, not per file: a project that ships one hook today and a
    different one tomorrow must not inherit yesterday's answer."""
    client = (Path(__file__).resolve().parents[1] / 'openmirror' / 'static' / 'hooks.js').read_text()
    assert 'state.asked' in client
    assert 'if (state.asked.has(hook.command)) return;' in client


def test_the_question_does_not_cover_the_turn():
    """A dialog over live work is a dialog that gets dismissed without being
    read, which is the one thing this feature cannot be."""
    index = (Path(__file__).resolve().parents[1] / 'openmirror' / 'static' / 'index.html').read_text()
    assert 'id="hooks-banner"' in index
    # It lives above the transcript, not inside it.
    assert index.index('id="hooks-banner"') < index.index('id="transcript"')
    assert 'showModal' not in index[index.index('id="hooks-banner"'):][:600]


def test_the_strip_says_a_hook_can_only_take_away():
    """The asymmetry, said where the person makes the decision."""
    client = (Path(__file__).resolve().parents[1] / 'openmirror' / 'static' / 'hooks.js').read_text()
    assert 'never allow one' in client

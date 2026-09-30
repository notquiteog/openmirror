"""The command line.

Two things are asserted here that nothing else in the suite can be.

**Bare `openmirror` still starts the daemon.** Not as a convenience: the
desktop app's frozen sidecar does `from openmirror.main import main; main()`,
and `daemon.rs` spawns the binary with an empty argv as well as
`python3 -m openmirror.main`. Both were written against a command that took no
arguments, and a CLI that broke that would not fail a test — it would fail the
desktop app on somebody's machine with no window ever appearing.

**A headless run finishes, whatever it is asked to do.** The interesting cases
are not the ones where a scripted provider answers politely. They are the ones
where a tool needs approving and there is nobody to approve it, and where a
model asks a question nobody is there to answer — because a run that waits for
a person who is not there does not fail, it hangs, and a test that hangs is a
test people delete.

A scripted provider, never a live one: whether a model happens to feel
cooperative this morning is not a thing a test may depend on.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from openmirror import cli
from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
from openmirror.sessions import SessionStore, Stored
from tests.test_agent import ScriptedProvider

SCRIPTED_MODEL = 'scripted'


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class TTY(io.StringIO):
    """A stdin that is a terminal rather than a pipe.

    `StringIO.isatty()` is False, which is right for the tests that pipe
    something in and wrong for "nobody piped anything in": a terminal must
    never be read, or `openmirror run -p …` would sit there until somebody
    pressed Ctrl-D.
    """

    def isatty(self) -> bool:
        return True


class Slow:
    """A provider that thinks for ever. For testing the clock, not a model."""

    def __init__(self) -> None:
        self.cancelled = False

    async def stream(self, _request):  # noqa: ANN001 - matches the provider interface
        yield StreamText(text='thinking')
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def models(self):
        return [{'id': 'slow'}]


@pytest.fixture
def served(monkeypatch):
    """Every `uvicorn.Server` a command would have started, recorded.

    `uvicorn.Config` is left real on purpose: it is what turns `--host` and
    `--port` into what the server binds, and a fake recording the arguments it
    was handed would be asserting that the CLI passes them along rather than
    that they work.
    """
    import uvicorn

    from openmirror.config import config

    started: list[object] = []

    class Server:
        def __init__(self, cfg) -> None:
            self.config = cfg
            # A server that ran says so. The port-taken test sets it
            # otherwise, which is the whole of what that test needs.
            self.started = True
            started.append(cfg)

        def run(self) -> None:
            self.ran = True

    monkeypatch.setattr(uvicorn, 'Server', Server)
    monkeypatch.setattr(config, 'exit_with_stdin', False)
    return started


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Nothing a command builds lands outside `tmp_path`, and nothing reaches out.

    The install's own `.env` switches on the desktop tools, the browser, memory
    and MCP, and a session built with those on goes looking for an X server, a
    Chromium and a mailbox. Every one of them is switched off here rather than
    depended on being off. The data paths are redirected because `openmirror`
    reads `.env` at import, and a test suite that writes transcripts into
    `data/` is a test suite that eventually deletes somebody's work.
    """
    from openmirror.agent import runtime
    from openmirror.agent.manager import manager
    from openmirror.config import config

    data = tmp_path / 'data'
    for name, value in (
        ('data_dir', data),
        ('sessions_dir', data / 'sessions'),
        ('memory_db', data / 'memory.db'),
        ('media_dir', data / 'media'),
        ('connections_db', data / 'connections.json'),
        ('calendar_dir', data / 'calendars'),
        ('browser_profile', data / 'browser'),
    ):
        monkeypatch.setattr(config, name, value)
    for name in ('memory_enabled', 'desktop_enabled', 'browser_enabled', 'mcp_enabled', 'checkpoints_enabled',
                 'skills_enabled', 'agents_enabled', 'lsp_enabled', 'hooks_enabled', 'worktrees_enabled',
                 'local_only'):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, 'approval_mode', 'ask')
    # The transcript store is injectable in two places and both of them are
    # used: the manager's own, and the process-wide default that
    # `build_session` reaches for. Setting only the first leaks every
    # conversation into whichever test happened to run first.
    sessions = SessionStore(data / 'sessions')
    monkeypatch.setattr(manager, '_store', sessions)
    monkeypatch.setattr(runtime, '_STORE', sessions)
    # pytest's own stdin raises on read. An empty terminal says the same thing
    # without depending on that.
    monkeypatch.setattr(sys, 'stdin', TTY(''))
    return data


@pytest.fixture
def runner(hermetic, monkeypatch):
    """A `run` that talks to a scripted provider, and to nothing else.

    Bootstrap and provider resolution are replaced rather than mocked out under
    them: both would otherwise read this machine's `.env` and try to reach a
    real model, which is a network call in a test suite.
    """
    def install(script, impl=None) -> object:
        provider = impl or ScriptedProvider(script)

        async def resolve(_provider, _model):
            return provider, SCRIPTED_MODEL, 'fake'

        async def bootstrap() -> None:
            return None

        monkeypatch.setattr(cli, '_resolve', resolve)
        monkeypatch.setattr(cli, '_bootstrap', bootstrap)
        return provider

    return install


@pytest.fixture
def store_dir(hermetic, monkeypatch):
    """The transcript store, pointed at a temporary directory."""
    sessions = SessionStore(hermetic / 'sessions')
    monkeypatch.setattr(cli, '_store', lambda: sessions)
    return sessions


def conversation(store: SessionStore, session_id: str, root: Path, *, title: str = 'Parser work',
                 age_days: float = 0.0) -> Stored:
    """One saved conversation, and how old it is.

    The age is written into the index rather than the file's mtime, because the
    index is what `SessionStore.list` returns and what `prune` reads: a
    conversation's age is a fact about when it was last written down, not
    about when the file happened to be touched.
    """
    stored = Stored(id=session_id, title=title, root=str(root), model='gpt-4o')
    stored.messages = [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'add a retry'}]},
        {'role': 'assistant', 'content': [
            {'type': 'thinking', 'text': 'where does it retry'},
            {'type': 'text', 'text': 'added a backoff'},
        ]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'and a test?'}]},
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'edit_file', 'input': {'path': 'client.py'}},
            {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok', 'is_error': False},
        ]},
    ]
    store.save(stored)
    if age_days:
        when = time.time() - age_days * 86400
        os.utime(store.path_for(session_id), (when, when))
        indexed = json.loads(store.index.read_text(encoding='utf-8'))
        indexed[session_id]['updated'] = when
        store.index.write_text(json.dumps(indexed), encoding='utf-8')
    return stored


# ---------------------------------------------------------------------------
# The contract two external callers depend on
# ---------------------------------------------------------------------------


def test_no_arguments_still_starts_the_daemon(served, monkeypatch):
    """`from openmirror.main import main; main()` is what the desktop app runs.

    No arguments at all, and no way for that to be anything but the server.
    """
    from openmirror.main import main as daemon_main

    monkeypatch.setattr(sys, 'argv', ['openmirror'])
    with pytest.raises(SystemExit) as left:
        daemon_main()

    assert left.value.code == 0
    assert [c.app for c in served] == ['openmirror.main:app']


def test_the_serve_subcommand_reaches_the_same_server(served, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['openmirror', 'serve'])
    assert cli.main() == cli.OK
    assert [c.app for c in served] == ['openmirror.main:app']


def test_serve_flags_become_the_bind(served):
    assert cli.main(['serve', '--host', '0.0.0.0', '--port', '9001']) == cli.OK
    assert (served[0].host, served[0].port) == ('0.0.0.0', 9001)


def test_a_leading_flag_is_the_server_rather_than_a_command(served):
    """`openmirror --port 9000` reads as the server with a flag.

    Which is the reason only a *word* is treated as a subcommand: a flag left
    over from the command that had no arguments at all must not become a usage
    error, and must not be swallowed either.
    """
    assert cli.main(['--port', '9002']) == cli.OK
    assert served[0].port == 9002


def test_a_port_that_is_already_taken_is_exit_three(served, monkeypatch):
    """What the daemon has always returned.

    A failed start has to look like a failed start to whoever launched it, or
    the desktop app attaches to whatever is holding the port and blames itself.
    """
    import uvicorn

    class Taken(uvicorn.Server):
        def __init__(self, cfg) -> None:
            super().__init__(cfg)
            self.started = False

    monkeypatch.setattr(uvicorn, 'Server', Taken)
    assert cli.main([]) == cli.PORT_TAKEN


def test_an_unknown_word_is_a_usage_error(capsys):
    assert cli.main(['frobnicate']) == cli.USAGE
    err = capsys.readouterr().err
    assert 'frobnicate' in err and 'not a command' in err


def test_a_bad_flag_under_a_known_command_is_still_two(capsys):
    assert cli.main(['sessions', 'list', '--limit', 'lots']) == cli.USAGE
    assert 'usage:' in capsys.readouterr().err


def test_version_is_one_number_from_one_place(capsys):
    assert cli.main(['--version']) == cli.OK
    first = capsys.readouterr().out.strip()
    assert cli.main(['version']) == cli.OK
    assert capsys.readouterr().out.strip() == first
    assert first == f'openmirror {cli.version()}'
    # The same function the daemon's update check asks, rather than a literal
    # that can be a version behind the one in pyproject.toml.
    from openmirror.routers.updates import version as daemon_version

    assert cli.version() == daemon_version()


# ---------------------------------------------------------------------------
# Parsing, which needs no provider and no network
# ---------------------------------------------------------------------------


def test_every_run_flag_lands_where_it_belongs():
    args = cli._run_parser().parse_args([
        '-p', 'why is this slow', '--model', 'gpt-4o', '--provider', 'openai', '--mode', 'trusted',
        '--effort', 'high', '--root', '/tmp/proj', '--session', 'abc', '--fork', '--at', '4',
        '--tools', 'files, shell', '--max-turns', '12', '--json', '--output-file', 'out.txt',
        '--quiet', '--yes', '--timeout', '30',
    ])
    assert args.prompt == 'why is this slow'
    assert (args.model, args.provider, args.mode, args.effort) == ('gpt-4o', 'openai', 'trusted', 'high')
    assert (args.root, args.session, args.fork, args.at) == ('/tmp/proj', 'abc', True, 4)
    assert args.tools == 'files, shell'
    assert (args.max_turns, args.as_json, args.output_file) == (12, True, 'out.txt')
    assert args.quiet is True and args.yes is True
    assert args.timeout == 30


def test_run_defaults_are_the_ones_a_person_would_guess():
    args = cli._run_parser().parse_args([])
    assert args.prompt is None
    assert args.timeout == cli.DEFAULT_TIMEOUT
    assert args.at == 0 and args.max_turns == 0
    assert args.as_json is False and args.yes is False and args.quiet is False
    assert args.continue_ is False and args.fork is False
    assert args.tools is None and args.output_file is None


def test_the_prompt_is_the_flag_when_there_is_one(monkeypatch):
    monkeypatch.setattr(sys, 'stdin', TTY(''))
    assert cli._prompt_text(cli._run_parser().parse_args(['-p', 'hello'])) == 'hello'


def test_no_flag_means_the_prompt_is_stdin(monkeypatch):
    """`git diff | openmirror run` is the case that makes this worth having."""
    monkeypatch.setattr(sys, 'stdin', io.StringIO('diff --git a/x b/x'))
    assert cli._prompt_text(cli._run_parser().parse_args([])) == 'diff --git a/x b/x'


def test_a_dash_means_stdin_too(monkeypatch):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('from a pipe'))
    assert cli._prompt_text(cli._run_parser().parse_args(['-p', '-'])) == 'from a pipe'


def test_piped_text_goes_in_front_of_the_flag(monkeypatch):
    """Claude Code's arrangement, and the reason for it: the pipe is the
    subject of the question and the flag is the question."""
    monkeypatch.setattr(sys, 'stdin', io.StringIO('the diff'))
    args = cli._run_parser().parse_args(['-p', 'what broke?'])
    assert cli._prompt_text(args) == 'the diff\n\nwhat broke?'


def test_a_terminal_is_never_read(monkeypatch):
    """A terminal would block until Ctrl-D, which for a command already given
    everything it needs on the command line is a hang rather than a default."""
    monkeypatch.setattr(sys, 'stdin', TTY('this is not a pipe'))
    assert cli._prompt_text(cli._run_parser().parse_args(['-p', 'hello'])) == 'hello'


def test_nothing_to_send_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('   \n'))
    assert cli.main(['run']) == cli.USAGE
    assert 'nothing to send' in capsys.readouterr().err


def test_a_mode_that_is_not_one_is_refused_before_anything_starts(capsys):
    assert cli.main(['run', '-p', 'hi', '--mode', 'whenever']) == cli.USAGE
    assert 'read_only' in capsys.readouterr().err


def test_fork_without_a_conversation_to_fork_is_refused(capsys):
    """A flag that does nothing and looks like it did is worse than a refusal:
    somebody who asked for a fork and got a fresh conversation has lost the
    one thing a fork is for."""
    assert cli.main(['run', '-p', 'hi', '--fork']) == cli.USAGE
    assert '--continue' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_the_answer_reaches_stdout_unbuffered(runner, tmp_path, capsys):
    runner([[StreamText(text='all '), StreamText(text='done'), StreamDone()]])
    code = cli.main(['run', '-p', 'say it', '--root', str(tmp_path), '--quiet'])
    assert code == cli.OK
    assert capsys.readouterr().out == 'all done\n'


def test_an_unanswered_approval_is_refused_and_the_run_still_finishes(runner, tmp_path, capsys):
    """Nobody is there, so the answer is no — and the run goes on.

    A denial is a tool result, not a dead end: the model is told and can try
    another way, and what it says afterwards is what reaches stdout. The failure
    this guards against is a hang, and a hang is a test that never finishes
    rather than a test that fails.
    """
    runner([
        [StreamToolUse(id='c1', name='write_file', input={'path': 'hello.txt', 'content': 'hi\n'}),
         StreamDone(stop_reason='tool_use')],
        [StreamText(text='I could not write it.'), StreamDone()],
    ])
    code = cli.main(['run', '-p', 'create hello.txt', '--root', str(tmp_path), '--quiet'])

    out, err = capsys.readouterr()
    assert code == cli.OK, 'a refusal the model recovered from is not a failed run'
    assert not (tmp_path / 'hello.txt').exists()
    assert 'refused' in err and '--yes' in err
    assert out.strip() == 'I could not write it.'


def test_yes_approves_and_says_so(runner, tmp_path, capsys):
    runner([
        [StreamToolUse(id='c1', name='write_file', input={'path': 'hello.txt', 'content': 'hi\n'}),
         StreamDone(stop_reason='tool_use')],
        [StreamText(text='written.'), StreamDone()],
    ])
    code = cli.main(['run', '-p', 'create hello.txt', '--root', str(tmp_path), '--yes'])

    out, err = capsys.readouterr()
    assert code == cli.OK
    assert (tmp_path / 'hello.txt').read_text() == 'hi\n'
    assert 'approved' in err
    assert out.strip() == 'written.'


def test_a_question_with_nobody_to_answer_it_does_not_wait(runner, tmp_path, capsys):
    """The other way a headless run hangs: `ask_user` waits for a person."""
    runner([
        [StreamToolUse(id='c1', name='ask_user', input={'question': 'which database?'}),
         StreamDone(stop_reason='tool_use')],
        [StreamText(text='I will use the one in the config.'), StreamDone()],
    ])
    code = cli.main(['run', '-p', 'add a store', '--root', str(tmp_path), '--quiet'])

    out, err = capsys.readouterr()
    assert code == cli.OK
    assert 'config' in out
    assert 'question with nobody' in err


def test_an_agent_error_is_exit_one_and_says_why(runner, tmp_path, capsys):
    class Broken:
        async def stream(self, _request):  # noqa: ANN001
            yield StreamText(text='starting')
            raise RuntimeError('the provider hung up')

        async def models(self):
            return []

    runner(None, impl=Broken())
    assert cli.main(['run', '-p', 'go', '--root', str(tmp_path), '--quiet']) == cli.FAILED
    assert 'the provider hung up' in capsys.readouterr().err


def test_json_mode_is_one_whole_object_per_line(runner, tmp_path, capsys):
    runner([[StreamText(text='all '), StreamText(text='done'), StreamDone()]])
    assert cli.main(['run', '-p', 'say it', '--root', str(tmp_path), '--json', '--quiet']) == cli.OK

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, 'nothing was emitted'
    # Each line on its own has to be a complete document: a program reading
    # this does `json.loads` per line and nothing else.
    events = [json.loads(line) for line in lines]
    assert ''.join(e['text'] for e in events if e['type'] == 'text.delta') == 'all done'
    assert any(e['type'] == 'turn.completed' for e in events)
    # The answer goes out as events and nothing else: no prose mixed into a
    # stream a program is parsing.
    assert not any(line.strip().startswith('all done') for line in lines)


def test_the_answer_is_written_where_it_was_asked_for(runner, tmp_path):
    runner([[StreamText(text='kept'), StreamDone()]])
    target = tmp_path / 'answer.txt'
    assert cli.main(['run', '-p', 'go', '--root', str(tmp_path), '--quiet',
                     '--output-file', str(target)]) == cli.OK
    assert target.read_text() == 'kept\n'


def test_the_conversation_is_saved_so_continue_can_find_it(runner, tmp_path, hermetic):
    runner([[StreamText(text='first'), StreamDone()]])
    assert cli.main(['run', '-p', 'remember this', '--root', str(tmp_path), '--quiet']) == cli.OK

    listed = SessionStore(hermetic / 'sessions').list()
    assert len(listed) == 1
    assert listed[0]['title'] == 'remember this'
    assert listed[0]['root'] == str(tmp_path)
    assert cli._latest_for_root(Path(tmp_path)) == listed[0]['id']


def test_continue_resumes_that_conversation(runner, tmp_path, hermetic):
    runner([[StreamText(text='first'), StreamDone()]])
    cli.main(['run', '-p', 'one', '--root', str(tmp_path), '--quiet'])
    session_id = cli._latest_for_root(Path(tmp_path))

    provider = runner([[StreamText(text='second'), StreamDone()]])
    assert cli.main(['run', '-c', '-p', 'two', '--root', str(tmp_path), '--quiet']) == cli.OK
    # The same provider object, so the resumed run's first request carries the
    # first run's conversation. That is what resuming means.
    assert provider.seen[0].messages[0].content[0].text == 'one'
    assert SessionStore(hermetic / 'sessions').load(session_id).messages


def test_continue_with_nothing_stored_says_so(runner, tmp_path, capsys):
    runner([[]])
    assert cli.main(['run', '-c', '-p', 'go', '--root', str(tmp_path)]) == cli.FAILED
    assert 'no stored conversation' in capsys.readouterr().err


def test_max_turns_bounds_the_run(runner, tmp_path, capsys):
    class Forever:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _request):  # noqa: ANN001
            self.calls += 1
            yield StreamToolUse(id=f'c{self.calls}', name='list_dir', input={'path': '.'})
            yield StreamDone(stop_reason='tool_use')

        async def models(self):
            return []

    provider = runner(None, impl=Forever())
    code = cli.main(['run', '-p', 'keep going', '--root', str(tmp_path), '--quiet', '--max-turns', '3'])
    err = capsys.readouterr().err
    assert code == cli.FAILED, 'running out of steps is the agent not having answered'
    assert 'step' in err
    assert provider.calls == 3


def test_the_clock_stops_a_run_that_will_not_finish(runner, tmp_path, capsys):
    """A CI job that has silently wedged is worse than one that says it gave up."""
    provider = runner(None, impl=Slow())
    started = time.monotonic()
    code = cli.main(['run', '-p', 'go', '--root', str(tmp_path), '--quiet', '--timeout', '0.5'])

    assert code == cli.FAILED
    assert time.monotonic() - started < 20
    assert 'gave up' in capsys.readouterr().err
    assert provider.cancelled, 'the run was abandoned rather than stopped'


def test_an_interrupt_is_130_and_still_saves_the_conversation(runner, tmp_path, capsys, hermetic):
    """SIGINT, sent the way a person sends it.

    A headless run is a coroutine with no terminal attached, so the only
    honest way to test 130 is to raise the actual signal. Either the loop's
    own handler stops the run, or the platform raises `KeyboardInterrupt`
    underneath it, and both of those are exit 130.
    """
    runner(None, impl=Slow())
    timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        code = cli.main(['run', '-p', 'go', '--root', str(tmp_path), '--quiet', '--timeout', '30'])
    finally:
        timer.cancel()
        timer.join()
    assert code == cli.INTERRUPTED
    assert SessionStore(hermetic / 'sessions').list(), 'the work that was done is still on disk'


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_sessions_list_shows_what_is_stored(store_dir, tmp_path, capsys):
    conversation(store_dir, 'aaaa1111', tmp_path, title='Parser work')
    conversation(store_dir, 'bbbb2222', tmp_path, title='Rewrite the loader')

    assert cli.main(['sessions', 'list']) == cli.OK
    out = capsys.readouterr().out
    assert 'Parser work' in out and 'Rewrite the loader' in out
    assert 'gpt-4o' in out
    assert out.count('\n') == 3, 'a header and two rows'


def test_sessions_list_as_json_is_the_stores_own_shape(store_dir, tmp_path, capsys):
    conversation(store_dir, 'aaaa1111', tmp_path)
    assert cli.main(['sessions', 'list', '--json']) == cli.OK
    rows = json.loads(capsys.readouterr().out)['sessions']
    assert [r['id'] for r in rows] == ['aaaa1111']
    assert rows[0]['turns'] == 2


def test_sessions_list_can_be_narrowed_to_one_root(store_dir, tmp_path, capsys):
    (tmp_path / 'other').mkdir()
    conversation(store_dir, 'aaaa1111', tmp_path)
    conversation(store_dir, 'bbbb2222', tmp_path / 'other', title='Somewhere else')
    assert cli.main(['sessions', 'list', '--json', '--root', str(tmp_path)]) == cli.OK
    rows = json.loads(capsys.readouterr().out)['sessions']
    assert [r['id'] for r in rows] == ['aaaa1111']


def test_sessions_show_prints_the_conversation(store_dir, tmp_path, capsys):
    conversation(store_dir, 'aaaa1111', tmp_path)
    assert cli.main(['sessions', 'show', 'aaaa1111']) == cli.OK
    out = capsys.readouterr().out
    assert 'Parser work' in out
    assert 'add a retry' in out and 'added a backoff' in out
    assert 'edit_file' in out, 'what it ran belongs in a transcript read by a person'


def test_sessions_show_says_so_when_there_is_no_such_conversation(store_dir, capsys):
    assert cli.main(['sessions', 'show', 'nope']) == cli.FAILED
    assert 'no stored conversation' in capsys.readouterr().err


def test_sessions_export_markdown_and_json(store_dir, tmp_path, capsys):
    conversation(store_dir, 'aaaa1111', tmp_path)

    assert cli.main(['sessions', 'export', 'aaaa1111', '--format', 'md']) == cli.OK
    assert '## You' in capsys.readouterr().out

    assert cli.main(['sessions', 'export', 'aaaa1111', '--format', 'json']) == cli.OK
    body = json.loads(capsys.readouterr().out)
    assert body['id'] == 'aaaa1111'
    assert [m['role'] for m in body['messages']] == ['user', 'assistant', 'user', 'assistant']


def test_sessions_export_writes_a_file_when_asked(store_dir, tmp_path, capsys):
    conversation(store_dir, 'aaaa1111', tmp_path)
    target = tmp_path / 'out.md'
    assert cli.main(['sessions', 'export', 'aaaa1111', '--output', str(target)]) == cli.OK
    assert 'Parser work' in target.read_text()
    assert capsys.readouterr().out == '', 'the document went to the file, not to stdout'


def test_sessions_delete_removes_it_and_says_so_when_there_is_nothing(store_dir, tmp_path, capsys):
    conversation(store_dir, 'aaaa1111', tmp_path)
    assert cli.main(['sessions', 'delete', 'aaaa1111']) == cli.OK
    assert store_dir.load('aaaa1111') is None
    assert 'deleted aaaa1111' in capsys.readouterr().out

    assert cli.main(['sessions', 'delete', 'aaaa1111']) == cli.FAILED
    assert 'no stored conversation' in capsys.readouterr().err


def test_sessions_prune_forgets_only_the_old_ones(store_dir, tmp_path, capsys):
    conversation(store_dir, 'recent1', tmp_path, title='Today')
    conversation(store_dir, 'ancient', tmp_path, title='Last year', age_days=400)

    assert cli.main(['sessions', 'prune', '--older-than', '30']) == cli.OK
    out = capsys.readouterr().out
    assert 'ancient' in out and 'Today' not in out
    assert [r['id'] for r in store_dir.list()] == ['recent1']


def test_sessions_fork_branches_a_conversation(runner, store_dir, tmp_path, capsys):
    """Through the manager, so the new id is written before the old one is left
    alone — and so a fork is a different conversation rather than a rename."""
    conversation(store_dir, 'aaaa1111', tmp_path)
    runner([[StreamText(text='never used')]])

    assert cli.main(['sessions', 'fork', 'aaaa1111', '--at', '2']) == cli.OK
    forked = capsys.readouterr().out.split()[0]
    assert forked != 'aaaa1111'

    original, branch = store_dir.load('aaaa1111'), store_dir.load(forked)
    assert len(original.messages) == 4
    assert len(branch.messages) == 2, 'only the first two messages were kept'
    assert branch.root == str(tmp_path)


def test_sessions_fork_says_so_when_there_is_nothing_to_fork(runner, store_dir, capsys):
    runner([[]])
    assert cli.main(['sessions', 'fork', 'nope']) == cli.FAILED
    assert 'no stored conversation' in capsys.readouterr().err


def test_sessions_with_no_action_is_a_usage_error(capsys):
    assert cli.main(['sessions']) == cli.USAGE
    assert 'list' in capsys.readouterr().err


def test_search_says_so_when_the_module_is_not_there(hermetic, capsys):
    """`openmirror.history` is somebody else's file, and may not exist yet.

    Whether it does or not, this command has to say something useful and exit
    with a number rather than raising an ImportError at somebody in a pipeline.
    """
    code = cli.main(['sessions', 'search', 'retry'])
    err = capsys.readouterr().err
    try:
        import openmirror.history  # noqa: F401
    except ImportError:
        assert code == cli.FAILED
        assert 'openmirror.history' in err
    else:
        assert code in (cli.OK, cli.FAILED)


# ---------------------------------------------------------------------------
# models, doctor
# ---------------------------------------------------------------------------


def fake_registry(entries: dict) -> object:
    """A registry with chat providers in it and no network anywhere near it."""
    from openmirror.providers.base import Modality, ProviderInfo
    from openmirror.providers.registry import ProviderRegistry

    live = ProviderRegistry()
    for provider_id, impl in entries.items():
        live.register(ProviderInfo(id=provider_id, label=provider_id.title(), modalities={Modality.CHAT}),
                      {Modality.CHAT: impl})
    return live


def use(monkeypatch, live) -> None:
    async def bootstrap() -> None:
        return None

    monkeypatch.setattr(cli, '_bootstrap', bootstrap)
    monkeypatch.setattr('openmirror.providers.registry.registry', live)


def test_models_lists_what_a_provider_offers(hermetic, monkeypatch, capsys):
    class Impl:
        async def models(self):
            return [{'id': 'llama3.2'}, {'id': 'nope', 'capabilities': ['embedding']}]

    use(monkeypatch, fake_registry({'fake': Impl()}))
    assert cli.main(['models', '--json']) == cli.OK
    rows = json.loads(capsys.readouterr().out)['providers']
    assert rows[0]['provider'] == 'fake'
    assert rows[0]['models'] == ['llama3.2', 'nope']
    assert rows[0]['error'] is None


def test_a_provider_that_cannot_be_listed_does_not_take_the_command_with_it(hermetic, monkeypatch, capsys):
    """It is a network call to somebody else's machine, and one of them
    hanging is the ordinary case rather than an exceptional one."""

    class Stuck:
        async def models(self):
            await asyncio.sleep(30)

    class Quick:
        async def models(self):
            return [{'id': 'here'}]

    monkeypatch.setattr(cli, 'MODEL_LIST_TIMEOUT', 0.2)
    use(monkeypatch, fake_registry({'stuck': Stuck(), 'fine': Quick()}))

    assert cli.main(['models', '--json']) == cli.OK
    rows = {r['provider']: r for r in json.loads(capsys.readouterr().out)['providers']}
    assert rows['fine']['models'] == ['here']
    assert rows['stuck']['models'] == [] and rows['stuck']['error']


def test_models_for_a_provider_nobody_has_is_a_failure(hermetic, monkeypatch, capsys):
    use(monkeypatch, fake_registry({'fake': None}))
    assert cli.main(['models', '--provider', 'nope']) == cli.FAILED
    assert "'nope'" in capsys.readouterr().err


def test_doctor_never_raises(hermetic, monkeypatch, capsys):
    """A diagnostic that crashes on the thing it is diagnosing is worse than no
    diagnostic, because it is run precisely when something is already broken."""
    async def broken() -> None:
        raise RuntimeError('the registry is on fire')

    monkeypatch.setattr(cli, '_bootstrap', broken)
    assert cli.main(['doctor']) == cli.OK
    out = capsys.readouterr().out
    assert 'openmirror' in out and 'python' in out
    assert 'the registry is on fire' in out

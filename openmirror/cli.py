"""The command line.

`openmirror` used to take no arguments at all: the command started the daemon
and that was the entire interface. Everything else — a conversation, a model,
a stored transcript — was reachable only through a browser tab, which is a
strange shape for something whose whole point is being driven by code.

**Bare `openmirror` still starts the daemon.** Two callers depend on that and
pass no arguments: the desktop app's frozen sidecar, which imports `main` from
`openmirror.main` and calls it, and `daemon.rs`, which spawns the binary with
an empty argv and also `python3 -m openmirror.main`. So no arguments dispatches
to the server exactly as before, and `openmirror serve` is the same thing said
out loud.

**A word is only a subcommand if it is one.** Anything else is a mistake, and a
mistake is the usage on stderr and exit 2 — argparse's convention, which every
other tool on the machine already follows, and which scripts already know how
to read. A leading dash is not a word, though: `openmirror --port 9000` is the
server with a flag, and that is how it reads.

**Headless means nobody is watching.** An approval request with no human at the
keyboard has exactly two honest answers — refuse it, or approve it because a
flag said so — and there is no third one that does not involve hanging until
something times out. Refusing is the default, and the refusal is fed back to
the model as a tool result so it recovers rather than dying; `--yes` is the
other answer and says on stderr what it agreed to. A question from the
`ask_user` tool is answered the same way, because a run that waits for a
person who is not there is a run that never finishes.

The exit codes are fixed and documented in `run`'s epilog because scripts read
them: 0 worked, 1 the agent failed, 2 the command line was wrong, 3 the port
was taken (which is what the daemon has always returned), 130 interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime
import inspect
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openmirror.protocol.agent import (
    AgentError,
    ContextCompacted,
    HookApproval,
    QuestionAsked,
    SessionEnded,
    TaskUpdated,
    TextDelta,
    ThinkingDelta,
    ToolCompleted,
    ToolDenied,
    ToolProposed,
    TurnCompleted,
    TurnStarted,
)

log = logging.getLogger(__name__)

PROG = 'openmirror'

# The numbers scripts branch on. Named rather than written as literals at each
# `sys.exit`, because the contract is "openmirror exits 1 when the agent
# failed" and that is only true if both the writer and the reader are looking
# at the same place.
OK = 0
FAILED = 1
USAGE = 2
PORT_TAKEN = 3
INTERRUPTED = 130

# A whole `run`, start to finish. Generous on purpose — this is a harness that
# runs tests and builds — and finite because an unattended run in a CI job
# that has silently wedged is worse than one that says it gave up. 0 turns the
# cap off, for a person who knows what they are doing.
DEFAULT_TIMEOUT = 900

#: How long one provider gets to answer "what models do you have". Listing is
#: a network call to somebody else's machine, and a provider that never answers
#: must not make this command hang with it.
MODEL_LIST_TIMEOUT = 20

#: How much of a tool result goes into one `--json` line. A build log can be
#: megabytes, and a JSON stream that carries one is a stream a `jq` in a
#: pipeline has to hold in memory to read at all. The line says that it was
#: cut, rather than being quietly shorter than the result.
JSON_RESULT_CLIP = 4000

#: How old a conversation has to be before `sessions prune` forgets it. A
#: month, because the answer to "do I still want this" gets harder every day
#: and the cost of keeping one is a few kilobytes.
DEFAULT_PRUNE_DAYS = 30

EPILOG = """\
exit codes:
  0   it worked
  1   the agent failed, or the run could not start
  2   the command line was wrong
  3   the port was already taken
  130 interrupted

examples:
  openmirror                                  start the daemon
  openmirror run -p "why is this test slow"  one prompt, no browser
  git diff | openmirror run -p "review this"
  openmirror run -c --json                   continue the last session, as JSON
  openmirror chat                            this terminal, against a running daemon
  openmirror sessions list

`run` and `chat` are different things on purpose. `run` starts its own agent
and owns the whole conversation; `chat` starts nothing and speaks the same API
the browser does, so the daemon stays the only thing holding your sessions.
Full reference: docs/CLI.md
"""


class _Usage(Exception):
    """A command line that does not make sense.

    Raised rather than handed to `argparse.error`, for two reasons: the message
    can then be this project's own ("there is no stored conversation for this
    directory") rather than a choice list, and a usage mistake is always exit
    2 with a sentence on stderr and never a traceback.
    """


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f'{PROG} serve',
        description='Start the daemon. What bare `openmirror` does.',
    )
    parser.add_argument(
        '--host', default=None,
        help='interface to bind. Defaults to OPENMIRROR_HOST, then 127.0.0.1.',
    )
    parser.add_argument(
        '--port', type=int, default=None,
        help='port to bind. Defaults to OPENMIRROR_PORT, then 8477.',
    )
    return parser


def _run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f'{PROG} run',
        description='One prompt, start to finish, with no browser attached.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'The conversation is written to the session store, so `--continue` and\n'
            '`--session` work on it afterwards. Exit codes: 0 worked, 1 the agent\n'
            'failed or timed out, 2 this command line was wrong, 130 interrupted.'
        ),
    )
    parser.add_argument(
        '-p', '--print', dest='prompt', default=None, metavar='TEXT',
        help='the prompt. `-`, or no flag at all, means read it from stdin; '
             'anything piped in is put in front of the text either way.',
    )
    parser.add_argument('--model', default=None, help='model to use. Defaults to the install default, then whatever the provider offers.')
    parser.add_argument('--provider', default=None, help='which provider answers, instead of the resolved default.')
    parser.add_argument(
        '--mode', default=None, metavar='MODE',
        help='approval policy for this run: read_only, plan, ask, auto_edit, trusted, '
             'unrestricted. Defaults to the install default (OPENMIRROR_APPROVAL_MODE, then ask).',
    )
    parser.add_argument(
        '--effort', default=None, metavar='LEVEL',
        help='how hard the model thinks: off, low, medium, high, xhigh, max, or default.',
    )
    parser.add_argument(
        '--root', default=None, metavar='PATH',
        help='the working root, and what the session is confined to. Defaults to this directory.',
    )
    parser.add_argument('--session', default=None, metavar='ID', help='resume a stored conversation by id.')
    parser.add_argument(
        '-c', '--continue', dest='continue_', action='store_true',
        help='resume the most recent conversation for this root.',
    )
    parser.add_argument(
        '--fork', action='store_true',
        help='with --continue or --session, branch into a new conversation instead of adding to it.',
    )
    parser.add_argument(
        '--at', type=int, default=0, metavar='N',
        help='with --fork, keep the first N messages. 0 keeps all of them.',
    )
    parser.add_argument(
        '--tools', default=None, metavar='LIST',
        help='comma-separated toolset groups (files, shell, git, web, browser, todo, agents, '
             'skills, ask, mail, calendar, media, memory, system) or bare tool names.',
    )
    parser.add_argument(
        '--max-turns', type=int, default=0, metavar='N',
        help='how many model round trips this run may make. 0 is the session default.',
    )
    parser.add_argument(
        '--json', dest='as_json', action='store_true',
        help='one JSON object per event on stdout, for a program to read. Progress moves to stderr.',
    )
    parser.add_argument(
        '--output-file', default=None, metavar='PATH',
        help='also write the final answer here, whatever goes to stdout.',
    )
    parser.add_argument(
        '--quiet', '-q', action='store_true',
        help='no progress on stderr. Errors still are.',
    )
    parser.add_argument(
        '-y', '--yes', action='store_true',
        help='approve whatever the policy would ask about, and say so on stderr. Nobody is there '
             'to answer an approval, so without this every one of them is REFUSED, with a line on '
             'stderr saying what was refused and why, and the model is told it was refused so it '
             'can try another way. Nothing is ever left waiting for an answer. This does not '
             'touch the two guarantees the policy keeps in every mode, including unrestricted: '
             'spending money and entering a secret are still refused rather than waved through.',
    )
    parser.add_argument(
        '--timeout', type=float, default=float(DEFAULT_TIMEOUT), metavar='SECONDS',
        help=f'wall clock for the whole run. 0 means no limit. Default {DEFAULT_TIMEOUT}.',
    )
    return parser


def _sessions_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f'{PROG} sessions',
        description='The conversations kept on disk.',
    )
    subs = parser.add_subparsers(dest='action', metavar='ACTION')

    listing = subs.add_parser('list', help='every stored conversation, newest first')
    listing.add_argument('--limit', type=int, default=50, help='how many to show. Default 50.')
    listing.add_argument('--root', default=None, help='only conversations from this working root.')
    listing.add_argument('--json', dest='as_json', action='store_true', help='print JSON instead of a table.')

    show = subs.add_parser('show', help='print one conversation')
    show.add_argument('session', help='session id')

    export = subs.add_parser('export', help='write one conversation out as markdown or JSON')
    export.add_argument('session', help='session id')
    export.add_argument('--format', choices=('md', 'json'), default='md')
    export.add_argument('--output', default=None, metavar='PATH', help='write here instead of stdout.')

    delete = subs.add_parser('delete', help='forget one conversation')
    delete.add_argument('session', help='session id')

    prune = subs.add_parser('prune', help='forget every conversation older than a given age')
    prune.add_argument(
        '--older-than', type=float, default=DEFAULT_PRUNE_DAYS, metavar='DAYS',
        help=f'days. 0 forgets everything, which is a deletion and not a tidy-up. Default {DEFAULT_PRUNE_DAYS}.',
    )

    fork = subs.add_parser('fork', help='branch a conversation into a new one, keeping the first N messages')
    fork.add_argument('session', help='session id to fork')
    fork.add_argument('--at', type=int, default=0, metavar='N', help='messages to keep, counting from one. 0 keeps all.')

    search = subs.add_parser('search', help='search across stored conversations')
    search.add_argument('query', help='what to look for')
    search.add_argument('--limit', type=int, default=50, help='how many matches to show. Default 50.')
    search.add_argument('--json', dest='as_json', action='store_true', help='print JSON instead of a table.')
    return parser


def _models_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f'{PROG} models',
        description='The chat providers this install can see, and what each one offers.',
    )
    parser.add_argument('--provider', default=None, help='only this provider.')
    parser.add_argument('--json', dest='as_json', action='store_true', help='print JSON instead of a table.')
    return parser


def build_parser() -> argparse.ArgumentParser:
    """The whole surface, for `--help`.

    One line listing the commands rather than the subparsers the commands
    themselves are built from, because this is the only place a new command has
    to be mentioned and it should not cost a second definition — or a second
    parser that could fall out of step with the first.
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description='An open-source digital twin. With no arguments, starts the daemon.',
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('-V', '--version', action='store_true', help='print the version and exit')
    parser.add_argument(
        'command', nargs='?', metavar='COMMAND',
        help='one of: run, chat, serve, sessions, models, doctor, version',
    )
    return parser


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _note(text: str) -> None:
    """One line about what is happening, on stderr.

    stderr and never stdout, because stdout is the answer:
    `openmirror run -p … > out.txt` should produce the answer and nothing
    else. Everything a person wants only on a terminal goes here.
    """
    print(text, file=sys.stderr, flush=True)


def _out(text: str) -> None:
    """The answer, as it arrives, unbuffered.

    Flushed per write rather than per line: a long paragraph that only appears
    when the model stops talking makes a one-shot run feel broken, and a pipe
    into another program gets its bytes at the speed they are produced.
    """
    sys.stdout.write(text)
    sys.stdout.flush()


def _close_stdout() -> None:
    """Point stdout at the void, so a closed pipe is not raised again.

    The interpreter flushes stdout on the way out and prints "Exception
    ignored" for whatever that raises, which is noise appended to a run that
    ended the way the reader asked it to. Redirecting the descriptor is the
    documented way out, and it is one line.
    """
    with contextlib.suppress(OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def version() -> str:
    """What this install is.

    The daemon's own answer, from `openmirror.routers.updates`, rather than a
    second literal. That module exists because a literal in two places was a
    version that could be wrong in one of them — a daemon that reports the old
    number to the updater is a daemon that never offers itself an update.
    """
    from openmirror.routers.updates import version as _version

    return _version()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

COMMANDS: dict[str, Callable[[list[str]], int]] = {}


def command(name: str) -> Callable[[Callable[[list[str]], int]], Callable[[list[str]], int]]:
    def register(fn: Callable[[list[str]], int]) -> Callable[[list[str]], int]:
        COMMANDS[name] = fn
        return fn
    return register


def _dispatch(argv: list[str]) -> int:
    """One argv in, one exit code out.

    Decided by hand rather than by `parse_args`, because the two cases argparse
    cannot express are exactly the two that matter here: no arguments at all is
    the server (a contract two external callers depend on), and an
    unrecognised word is a mistake rather than something to be quietly ignored.
    """
    if not argv:
        return serve([])
    head = argv[0]
    if head in ('-h', '--help'):
        build_parser().print_help()
        return OK
    if head in ('-V', '--version'):
        print(f'{PROG} {version()}')
        return OK
    if head not in COMMANDS:
        if not head.startswith('-'):
            raise _Usage(
                f'not a command: {head!r}. Expected one of: {", ".join(sorted(COMMANDS))}.'
            )
        # A flag rather than a word, so it belongs to the server.
        return serve(argv)
    return COMMANDS[head](argv[1:])


def main(argv: list[str] | None = None) -> int:
    """The entry point, and the only thing that turns a mistake into a number.

    Returns rather than raising `SystemExit`, so that a caller — a test, or the
    `main()` in `openmirror.main` that the desktop app imports — gets a result
    it can act on instead of an exception it has to catch.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return _dispatch(argv)
    except _Usage as exc:
        _note(f'{PROG}: {exc}')
        _note(f'try `{PROG} --help`')
        return USAGE
    except SystemExit as exc:
        # argparse's own exits: `--help` is 0, a bad flag is 2 with the usage
        # already on stderr.
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else USAGE)
    except KeyboardInterrupt:
        _note(f'{PROG}: interrupted')
        return INTERRUPTED
    except BrokenPipeError:
        # `openmirror run | head -20` closes the pipe mid-answer. That is an
        # ordinary way for this to end, and the default shutdown path would
        # turn it into "Exception ignored ... BrokenPipeError" after the exit
        # code has already been decided.
        _close_stdout()
        return OK


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@command('serve')
def serve(argv: list[str]) -> int:
    import uvicorn

    from openmirror.config import config

    args = _serve_parser().parse_args(argv)
    host = args.host or config.host
    port = args.port if args.port is not None else config.port

    # On the config as well as on uvicorn's, before the server starts. The
    # lifespan refuses to serve an unauthenticated agent on a network interface
    # and it reads `config.host` to decide that, so a `--host` that only
    # reached uvicorn would switch the guard off rather than satisfy it.
    config.host = host
    config.port = port

    server = uvicorn.Server(
        uvicorn.Config('openmirror.main:app', host=host, port=port, log_level=config.log_level.lower())
    )
    if config.exit_with_stdin:
        # Imported rather than copied: the desktop app's sidecar depends on
        # this being the same function, and a second copy of "stop when the
        # other end of stdin goes away" is a copy that will rot out of step
        # with the daemon it belongs to. Only needed on this path, so nothing
        # else here has to import the FastAPI app to run.
        from openmirror.main import _exit_when_stdin_closes

        threading.Thread(target=_exit_when_stdin_closes, args=(server,), name='stdin', daemon=True).start()
    server.run()
    # What `uvicorn.run` does, and the reason to keep it: a port that was
    # already taken should be a failed start to whoever launched this, not a
    # clean exit.
    if not server.started:
        # Neutral, because `started` is false for every reason a start-up can
        # fail and not only for the one uvicorn names first. The reason is
        # always in the log above this line; saying "the port was taken" when it
        # was not would send somebody looking in the wrong place.
        _note(f'{PROG}: did not start on {host}:{port}. The reason is above; a port already in use '
              'is the usual one.')
        return PORT_TAKEN
    return OK


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def _bootstrap() -> None:
    """Providers and settings, without a server around them.

    The same two calls the daemon's lifespan makes, which is the entire reason
    they are functions rather than a route. `openmirror run` has to reach
    exactly the model the browser would have reached; a second answer to "which
    provider serves chat" is a second thing to be wrong about chat.
    """
    from openmirror.config import apply_settings, config
    from openmirror.providers.bootstrap import bootstrap
    from openmirror.providers.registry import registry

    try:
        found = apply_settings()
        for path in found.sources:
            log.info('settings: %s', path)
    except Exception:  # noqa: BLE001 - a bad settings file must not stop a run, exactly as in the lifespan
        log.exception('settings could not be read; continuing with the environment')
    await bootstrap(config, registry)


async def _resolve(provider: str | None, model: str | None) -> tuple[Any, str, str]:
    """The provider, a concrete model, and which provider it was.

    The daemon's own helper, imported lazily: it is shared with the commit and
    mail drafts precisely so that there is one answer to "which model does this
    install talk to", and a second copy here would be the copy that forgets to
    prefer a model that can call tools. Lazy because everything else in this
    file — `sessions list`, `--version` — should not have to import FastAPI to
    answer.
    """
    from openmirror.routers.agent import resolve_chat

    return await resolve_chat(provider, model)


def _services() -> tuple[Any, Any]:
    """Memory and media, built the way the daemon's lifespan builds them.

    Guarded, because a run without memory is a worse run rather than a failed
    one, and there is nothing here worth ending somebody's command over.
    """
    from openmirror.config import config
    from openmirror.providers.registry import registry

    memory = media = None
    try:
        from openmirror.media.service import MediaService
        from openmirror.media.store import MediaStore

        media = MediaService(MediaStore(config.media_dir), registry)
    except Exception:  # noqa: BLE001
        log.exception('media is unavailable to this run; continuing without it')
    if config.memory_enabled:
        try:
            from openmirror.memory.service import MemoryService
            from openmirror.memory.store import MemoryStore

            memory = MemoryService(MemoryStore(config.memory_db), registry, model=config.embed_model)
        except Exception:  # noqa: BLE001
            log.exception('memory is unavailable to this run; continuing without it')
    return memory, media


def _stdin_text() -> str:
    """Whatever was piped in, or nothing.

    A terminal is not a pipe: reading one would block until the person pressed
    Ctrl-D, which for a command that was given everything it needs on the
    command line is the difference between a tool and a hang.
    """
    stream = sys.stdin
    if stream is None:
        return ''
    try:
        if stream.isatty():
            return ''
    except (AttributeError, ValueError):
        return ''
    try:
        return stream.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return ''


def _prompt_text(args: argparse.Namespace) -> str:
    """The prompt, from the flag, from stdin, or from both.

    Both is the case that makes this worth having: `git diff | openmirror run
    -p "what broke?"` is a thing people actually want to type, and the piped
    text goes first because it is what the question is about.
    """
    piped = _stdin_text().strip()
    given = '' if args.prompt in (None, '-') else str(args.prompt).strip()
    if piped and given:
        return f'{piped}\n\n{given}'
    return given or piped


def _toolset(value: str | None) -> list[str]:
    return [part.strip() for part in (value or '').split(',') if part.strip()]


def _latest_for_root(root: Path) -> str | None:
    """The newest stored conversation about this directory.

    Matched on the resolved path, not the string, because one session writes
    whichever of `/tmp/x`, `/tmp/x/` and `/tmp/x/..` the person happened to
    start it from, and a `--continue` that picks a conversation about a
    neighbouring project is a confidently wrong answer rather than a failure.
    """
    from openmirror.agent.manager import manager

    for row in manager.stored(limit=200):
        other = row.get('root')
        if not other:
            continue
        try:
            if Path(str(other)).expanduser().resolve() == root:
                return str(row.get('id') or '')
        except OSError:
            continue
    return None


class _Headless:
    """What a run does with the events it is handed.

    The policy in one class because it is one policy: what reaches stdout, what
    reaches stderr, what is approved, what is refused, and what counts as
    failure. Splitting it across the event loop and the parser would make it
    impossible to read as a whole, and this is the part of a headless run that
    decides whether it is safe.
    """

    def __init__(self, *, yes: bool, quiet: bool, as_json: bool) -> None:
        self.yes = yes
        self.quiet = quiet
        self.as_json = as_json
        #: The assistant's text for this run, so `--output-file` and "did it
        #: say anything at all" have something to look at.
        self.answer: list[str] = []
        #: Things that make this exit non-zero. Deliberately *not* the same
        #: list as the things worth saying out loud: a refused command is said
        #: on every run and is not a failure, because the model was told and
        #: carried on.
        self.errors: list[str] = []
        self.stop_reason = ''
        #: Whether this turn's reasoning has already been announced. A
        #: reasoning model emits thinking a token at a time, and a line per
        #: token turns a progress channel into a transcript of a model
        #: thinking — which is both unreadable and, on a slow model, a
        #: meaningful cost in write syscalls.
        self._said_thinking = False

    # -- what reaches which stream -------------------------------------------

    def note(self, text: str) -> None:
        if not self.quiet:
            _note(text)

    def text(self, value: str) -> None:
        self.answer.append(value)
        if not self.as_json:
            _out(value)

    def failed(self, message: str) -> None:
        """Something that will make this exit non-zero, said out loud.

        Always, even under `--quiet`: a quiet run is one whose progress is
        hidden, and swallowing the reason it failed would turn that into a way
        to lose an error.
        """
        self.errors.append(message)
        _note(f'{PROG}: {message}')

    def refuse(self, message: str) -> None:
        """Something this run was not allowed to do, said out loud.

        Also always. A refusal is the one line that explains a transcript which
        looks like the agent changed its mind halfway through, and a run whose
        stderr is empty is exactly the run somebody has to go and read the
        transcript to understand.
        """
        _note(f'{PROG}: {message}')

    # -- the events ----------------------------------------------------------

    def handle(self, event: Any, session: Any) -> bool:
        """Deal with one event. True when the run is over.

        Branching on the event classes rather than on `type` strings, because
        these are already a discriminated union and a string comparison is a
        spelling that can be wrong.
        """
        if self.as_json:
            _out(json.dumps(_event_json(event), ensure_ascii=False, default=str) + '\n')

        if isinstance(event, TextDelta):
            self.text(event.text)
        elif isinstance(event, ThinkingDelta):
            # Never stdout. Reasoning is not the answer, and a run piped into
            # something that reads the answer should not be handed the working.
            if not self._said_thinking:
                self._said_thinking = True
                self.note(f'  thinking … {_clip(event.text)}')
        elif isinstance(event, ToolProposed):
            self.note(f'  tool: {_clip(event.call.summary or event.call.name)}')
            if event.needs_approval:
                self._approve(event.call, session)
        elif isinstance(event, ToolCompleted):
            outcome = 'ok' if event.result.ok else 'failed'
            self.note(f'  {event.result.name}: {outcome} ({event.result.duration_ms}ms)')
        elif isinstance(event, ToolDenied):
            self.refuse(f'denied: {_clip(event.call_id or "a tool call")} — {event.reason or "no reason given"}')
        elif isinstance(event, QuestionAsked):
            self._question(event, session)
        elif isinstance(event, HookApproval):
            # Not asked about on purpose. A hook is code from a project, and
            # `--yes` is a promise about this session's tool calls, not consent
            # to run somebody else's script.
            self.note(f'  a hook on this project was not run: {_clip(str(event.hook.get("command", "")))}')
        elif isinstance(event, ContextCompacted):
            self.note(f'  context compacted: {event.messages_before} messages -> {event.messages_after}')
        elif isinstance(event, TaskUpdated):
            self.note(f'  task {event.task.get("label", "")}: {event.task.get("status", "")}')
        elif isinstance(event, TurnStarted):
            self._said_thinking = False
        elif isinstance(event, AgentError):
            self.failed(event.message)
        elif isinstance(event, TurnCompleted):
            self.stop_reason = event.stop_reason
            return True
        elif isinstance(event, SessionEnded):
            return True
        return False

    def _approve(self, call: Any, session: Any) -> None:
        if self.yes:
            session.approve(call.id)
            self.note(f'  approved (--yes): {_clip(call.summary or call.name)}')
            return
        reason = (
            'refused: this is a headless run and nobody is there to agree to it. '
            'Continue with something else and say what you could not do.'
        )
        session.deny(call.id, reason)
        # Said where a person can see it as well as said to the model. A run
        # that silently does less than the model wanted is a run whose
        # transcript looks like the agent changed its mind.
        self.refuse(f'refused without asking: {_clip(call.summary or call.name)} — pass --yes to allow it')

    def _question(self, event: Any, session: Any) -> None:
        answer = (
            'No one is there: this is an unattended run. Do not wait for an answer. '
            'Carry on with whatever else you can do and say plainly what you needed to ask.'
        )
        session.answer(event.question_id, answer)
        self.refuse(f'question with nobody to answer it: {_clip(event.question)}')

    # -- the verdict ---------------------------------------------------------

    def code(self) -> int:
        """The exit code this run earned.

        A refusal is not a failure: the model is told, recovers and finishes,
        and a run that declined one command and did all the rest of the work
        is a run that worked. An `error` stop reason, an `error` event and
        running out of steps are failures, because each of them is the agent
        not having answered. A run that ended without saying why did not
        answer either, and is treated the same way rather than passing
        silently.
        """
        if self.errors or self.stop_reason in ('error', 'max_steps', ''):
            return FAILED
        if self.stop_reason == 'interrupted':
            return INTERRUPTED
        return OK

    def answer_text(self) -> str:
        return ''.join(self.answer).strip()


def _clip(text: str, limit: int = 90) -> str:
    flat = ' '.join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + ' …'


def _path(value: str | None, default: str = '.') -> Path:
    """A path from a flag, expanded and absolute, or the working directory.

    Absolute because a session's root is stored absolutely and compared against
    later: `--root .` from three different directories has to name the same
    place that `sessions list` prints, or `--continue` silently misses. The
    same goes for `--output-file`, where the answer wants to say where it went
    in a form a person can copy.
    """
    return Path(value or default).expanduser().resolve()


def _event_json(event: Any) -> dict[str, Any]:
    data = event.model_dump(mode='json')
    result = data.get('result')
    if isinstance(result, dict):
        body = str(result.get('content') or '')
        if len(body) > JSON_RESULT_CLIP:
            result['content'] = body[:JSON_RESULT_CLIP]
            result['content_truncated'] = len(body) - JSON_RESULT_CLIP
    return data


def _signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> bool:
    """Point SIGINT and SIGTERM at one event. False if the platform cannot.

    `add_signal_handler` does not exist on Windows, where Ctrl-C raises
    `KeyboardInterrupt` instead — which `main` already turns into 130, so the
    fallback is the platform's own and it is a good one.
    """
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError, ValueError):
        return False
    return True


async def _settle(session: Any, seconds: float = 5.0) -> None:
    """Wait for the turn to let go, after its last event.

    `turn.completed` is emitted a moment before the turn's own task finishes —
    the transcript is written and any held message drained after it — so
    returning on the event alone would close the session out from under a turn
    that is still running.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while session.busy and loop.time() < deadline:  # noqa: ASYNC110 - polling a public flag, which is all there is to poll
        await asyncio.sleep(0.01)


async def _consume(session: Any, watcher: _Headless) -> None:
    events = session.events()
    try:
        async for event in events:
            if watcher.handle(event, session):
                return
    finally:
        with contextlib.suppress(Exception):
            await events.aclose()


async def _abandon(pump: asyncio.Task, session: Any, reason: str) -> None:
    """Stop watching, stop the turn, and write down whatever happened.

    All three, in that order, and the middle one matters most: without the
    interrupt the turn keeps running — a build, a `sleep 30`, a model that has
    stopped answering — and this is the branch that exists for exactly those
    cases. Closing afterwards is what puts the half-finished conversation on
    disk, marked unfinished, which is the honest record of a run that was
    stopped.
    """
    from openmirror.agent.manager import manager

    pump.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pump
    session.interrupt()
    await manager.close(session.id, reason)


async def _drive(session: Any, prompt: str, args: argparse.Namespace, watcher: _Headless) -> int:
    """Submit, watch, and stop for whichever of the three reasons comes first.

    The turn finishing, the person interrupting, and the clock running out are
    handled together deliberately. A headless run that can only be stopped by
    the model finishing is a run that cannot be stopped.
    """
    from openmirror.agent.manager import manager

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed = _signals(loop, stop)
    pump: asyncio.Task | None = None
    code = OK
    try:
        session.submit(prompt)
        pump = asyncio.create_task(_consume(session, watcher))
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {pump, stopper},
                timeout=args.timeout if args.timeout and args.timeout > 0 else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper

        if stopper in done and not pump.done():
            await _abandon(pump, session, 'interrupted')
            _note(f'{PROG}: interrupted')
            return INTERRUPTED

        if pump not in done:
            await _abandon(pump, session, 'timed out')
            _note(f'{PROG}: gave up after {args.timeout:.0f}s. The conversation is saved; '
                  'raise --timeout to give it longer.')
            return FAILED

        # Whatever ended the watch, its exception is fetched here: a task whose
        # failure nobody reads is reported by asyncio as "never retrieved", with
        # a traceback, at a moment that has nothing to do with when it happened.
        if not pump.cancelled() and pump.exception() is not None:
            raise pump.exception()  # noqa: B904 - re-raised unchanged, with its own traceback

        code = watcher.code()
        await _settle(session)
        await manager.close(session.id, 'run finished')
        return code
    except BrokenPipeError:
        # `openmirror run | head -20`: whoever was reading the answer has gone.
        # The turn is stopped and written down rather than left running against
        # a pipe nobody is on the other end of, and the run is not called a
        # failure — a reader that stopped reading did not break the work.
        if pump is not None:
            with contextlib.suppress(Exception):
                await _abandon(pump, session, 'output closed')
        raise
    finally:
        # A handler left installed would keep a terminal interrupt from being
        # an interrupt, and the process is about to end anyway.
        if installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGINT)
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGTERM)


@command('run')
def run(argv: list[str]) -> int:
    args = _run_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except _Usage:
        # Raised by the checks inside the coroutine, and re-raised rather than
        # swallowed by the catch-all below: a wrong flag is exit 2 and one
        # sentence, not exit 1 and a traceback.
        raise
    except KeyboardInterrupt:
        _note(f'{PROG}: interrupted')
        return INTERRUPTED
    except BrokenPipeError:
        _close_stdout()
        return OK
    except Exception as exc:  # noqa: BLE001 - a command that traces back has not run in a pipe
        # Caught so a CI job gets a number and a sentence rather than a
        # traceback nobody reads. Logged at full detail, because swallowing the
        # traceback is only acceptable if something is still keeping it.
        log.exception('a headless run could not be completed')
        _note(f'{PROG}: {type(exc).__name__}: {exc}')
        return FAILED


async def _run(args: argparse.Namespace) -> int:
    from openmirror.agent.approval import Mode
    from openmirror.agent.manager import manager
    from openmirror.config import config
    from openmirror.providers.registry import NoProviderError

    prompt = _prompt_text(args)
    if not prompt.strip():
        raise _Usage('nothing to send: pass -p TEXT, `-p -`, or pipe something in')

    try:
        mode = Mode(args.mode or config.approval_mode)
    except ValueError:
        raise _Usage(
            f'not an approval mode: {(args.mode or config.approval_mode)!r} — '
            f'use {", ".join(m.value for m in Mode)}'
        ) from None

    effort = None
    if args.effort and args.effort != 'default':
        from openmirror.providers.reasoning import normalise

        effort = normalise(args.effort)
        if effort is None:
            raise _Usage(f'not a thinking level: {args.effort!r} — use off, low, medium, high, xhigh, max or default')

    root = _path(args.root)
    if not root.is_dir():
        raise _Usage(f'{root}: not a directory to work in')
    if args.fork and not (args.session or args.continue_):
        # A flag that does nothing and looks like it did is worse than a
        # refusal: somebody who asked for a fork and got a fresh conversation
        # has lost the one thing a fork is for.
        raise _Usage('--fork branches an existing conversation, so it needs --continue or --session')

    await _bootstrap()
    try:
        impl, model, _provider_id = await _resolve(args.provider, args.model)
    except NoProviderError as exc:
        _note(f'{PROG}: {exc}')
        return FAILED

    watcher = _Headless(yes=args.yes, quiet=args.quiet, as_json=args.as_json)
    toolset = _toolset(args.tools)
    session = await _session(manager, args, root=root, provider=impl, model=model, mode=mode,
                             effort=effort, toolset=toolset, prompt=prompt)
    if session is None:
        return FAILED
    if args.max_turns:
        # The session's own ceiling is on model round trips, which is what a
        # "turn" is to somebody watching a run, and the only limit this loop
        # has. Set rather than configured, because it is this run's budget and
        # not a property of the install.
        session.max_steps = args.max_turns

    watcher.note(f'{PROG}: session {session.id} · {session.model} · {session.policy.mode.value}')
    code = await _drive(session, prompt, args, watcher)

    answer = watcher.answer_text()
    if not args.as_json and not args.quiet and not answer:
        _note(f'{PROG}: the agent said nothing (stopped: {watcher.stop_reason or "unknown"})')
    if args.output_file and answer:
        target = _path(args.output_file)
        try:
            await asyncio.to_thread(target.write_text, answer + '\n', encoding='utf-8')
        except OSError as exc:
            _note(f'{PROG}: {exc} — the answer was not written to {target}')
            return FAILED
        watcher.note(f'  answer also written to {target}')
    elif args.output_file and not answer:
        _note(f'{PROG}: there was no answer to write to {args.output_file}')
    if not args.as_json and answer:
        # The answer ends wherever the model stopped, which is not always at a
        # line. A shell prompt on the same line as the last word is a thing
        # people notice.
        _out('\n')
    return code


async def _session(manager: Any, args: argparse.Namespace, *, root: Path, provider: Any,
                   model: str, mode: Any, effort: str | None, toolset: list[str],
                   prompt: str) -> Any | None:
    """The session this run happens in: a new one, a resumed one, or a fork.

    None means the run cannot start, and the reason has already been said.
    """
    from openmirror.config import config

    wanted = args.session or (_latest_for_root(root) if args.continue_ else None)
    if wanted is not None:
        # `resume` takes the root, model and toolset from the transcript rather
        # than from the flags, which is the manager's rule and not this
        # command's to change: a conversation reopened in a different project,
        # or against a different model than it was had, is a conversation that
        # is now about something else.
        if args.fork:
            session = await manager.fork(wanted, args.at, provider=provider, mode=mode, toolset=toolset or None)
        else:
            session = await manager.resume(wanted, provider=provider, mode=mode, toolset=toolset or None)
        if session is None:
            _note(f'{PROG}: no stored conversation called {wanted!r}')
        return session
    if args.session or args.continue_:
        _note(f'{PROG}: no stored conversation for {root}. Run without --continue to start one.')
        return None

    memory, media = _services()
    # The first line of the prompt, so `sessions list` shows what this
    # conversation was about rather than twenty entries called "run".
    title = next((line.strip() for line in prompt.splitlines() if line.strip()), 'run')
    try:
        return await manager.create(
            root=root,
            provider=provider,
            model=model,
            mode=mode,
            effort=effort,
            title=title[:60],
            toolset=toolset,
            memory=memory,
            media=media,
            user_id=config.default_user,
        )
    except ValueError as exc:
        _note(f'{PROG}: {exc}')
        return None


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def _store() -> Any:
    """Where conversations are kept on disk.

    `SessionStore` rather than the daemon's session manager: these commands
    read what is on disk, and listing yesterday's conversations must not
    depend on whether a daemon happens to be running.
    """
    from openmirror.config import config
    from openmirror.sessions import SessionStore

    return SessionStore(config.sessions_dir)


def _when(stamp: Any) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(stamp)).strftime('%Y-%m-%d %H:%M')
    except (OSError, OverflowError, TypeError, ValueError):
        return '—'


def _same_root(stored: str, wanted: str) -> bool:
    if not stored:
        return False
    try:
        return Path(stored).expanduser().resolve() == Path(wanted)
    except OSError:
        return False


@command('sessions')
def sessions(argv: list[str]) -> int:
    args = _sessions_parser().parse_args(argv)
    action = getattr(args, 'action', None)
    if action == 'list':
        return _list_sessions(args)
    if action == 'show':
        return _show_session(args.session)
    if action == 'export':
        return _export_session(args)
    if action == 'delete':
        return _delete_session(args.session)
    if action == 'prune':
        return _prune_sessions(args)
    if action == 'fork':
        return asyncio.run(_fork_session(args))
    if action == 'search':
        return asyncio.run(_search_sessions(args))
    if action is None:
        raise _Usage('sessions needs an action: list, show, export, delete, prune, fork, search')
    # The subparser only offers the seven above, so this is unreachable through
    # the command line. It is here because the alternative is falling through
    # to `search`, which is the one action that is allowed to do nothing.
    raise _Usage(f'not something sessions can do: {action!r}')


def _load(store: Any, session_id: str) -> Any | None:
    found = store.load(session_id)
    if found is None:
        _note(f'{PROG}: no stored conversation called {session_id!r} in {store.root}')
    return found


def _list_sessions(args: argparse.Namespace) -> int:
    store = _store()
    rows = store.list(limit=args.limit)
    if args.root:
        wanted = str(Path(args.root).expanduser().resolve())
        rows = [row for row in rows if _same_root(str(row.get('root') or ''), wanted)]
    if args.as_json:
        print(json.dumps({'sessions': rows}, ensure_ascii=False, indent=2, default=str))
        return OK
    if not rows:
        _note(f'no stored conversations in {store.root}')
        return OK
    # The columns are sized from the data rather than fixed, because a fixed
    # width that is too small does not truncate — it runs the next column into
    # this one, and a table where the model and the title are touching is a
    # table nobody can read. Both are clipped to their column, so a long model
    # id costs a few characters rather than the shape of the whole table.
    models = [str(row.get('model') or '—') for row in rows]
    model_width = min(28, max(8, max(len(m) for m in models)))
    print(f'{"ID":<18}{"UPDATED":<18}{"TURNS":>6}  {"MODEL":<{model_width}}  TITLE')
    for row, model in zip(rows, models, strict=True):
        title = str(row.get('title') or '(untitled)')
        print(
            f'{str(row.get("id", ""))[:16]:<18}'
            f'{_when(row.get("updated")):<18}'
            f'{str(row.get("turns") or 0):>6}  '
            f'{model[:model_width]:<{model_width}}  '
            f'{title[:72]}'
        )
    return OK


def _show_session(session_id: str) -> int:
    """The conversation, as somebody reads it.

    Not `to_markdown`: that is for pasting somewhere else, and this is for
    reading. Tool calls appear as one line each rather than a fenced block,
    because the question this command answers is "what did it say", and the
    answer is buried under what it ran.
    """
    store = _store()
    stored = _load(store, session_id)
    if stored is None:
        return FAILED
    print(f'# {stored.title or stored.id}')
    print(f'{stored.id}  ·  {stored.root or "—"}  ·  {stored.model or "—"}  ·  '
          f'{len(stored.messages)} messages  ·  updated {_when(stored.updated)}')
    if stored.unfinished:
        print('\n> The daemon stopped while a turn was running. That turn is not in this transcript.')
    print()
    for message in stored.messages:
        who = 'you' if message.get('role') == 'user' else 'openmirror'
        print(f'{who}:')
        for block in message.get('content') or []:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'text':
                print(str(block.get('text') or ''))
            elif kind == 'thinking':
                print(f'  [reasoning] {_clip(str(block.get("text") or ""), 200)}')
            elif kind == 'tool_use':
                print(f'  [tool] {block.get("name")} {_clip(json.dumps(block.get("input") or {}, default=str), 160)}')
            elif kind == 'tool_result':
                mark = 'failed' if block.get('is_error') else 'result'
                print(f'  [{mark}] {_clip(str(block.get("content") or ""), 200)}')
            elif kind == 'image':
                print(f'  [image] {block.get("note") or block.get("media_type") or "removed"}')
        print()
    return OK


def _export_session(args: argparse.Namespace) -> int:
    from openmirror.sessions import to_markdown

    store = _store()
    stored = _load(store, args.session)
    if stored is None:
        return FAILED
    if args.format == 'json':
        body = json.dumps(dataclasses.asdict(stored), ensure_ascii=False, indent=2, default=str)
    else:
        body = to_markdown(stored)
    if args.output:
        target = Path(args.output).expanduser()
        try:
            target.write_text(body, encoding='utf-8')
        except OSError as exc:
            _note(f'{PROG}: {exc} — nothing was written to {target}')
            return FAILED
        _note(f'{PROG}: {target} ({len(body)} characters)')
        return OK
    print(body)
    return OK


def _delete_session(session_id: str) -> int:
    """Forget one conversation.

    `delete` is the store's own answer — it removes the file and the index
    entry, or it says there was nothing to remove — so that is what is checked
    rather than a load-then-delete, which would be a second way of asking
    whether it is there.
    """
    store = _store()
    if not store.delete(session_id):
        _note(f'{PROG}: no stored conversation called {session_id!r} in {store.root}')
        return FAILED
    print(f'deleted {session_id}')
    return OK


def _prune_sessions(args: argparse.Namespace) -> int:
    """Forget the old ones, saying which, as they go.

    Every deletion is printed. A prune that removed thirty conversations and
    said nothing would be a command nobody runs twice.
    """
    store = _store()
    cutoff = None if args.older_than <= 0 else datetime.datetime.now().timestamp() - args.older_than * 86400
    gone = 0
    for row in store.list(limit=100_000):
        if cutoff is not None and float(row.get('updated') or 0) >= cutoff:
            continue
        if store.delete(str(row.get('id') or '')):
            gone += 1
            print(f'deleted {row.get("id")}  {_when(row.get("updated"))}  {row.get("title") or "(untitled)"}')
    _note(f'{PROG}: deleted {gone} conversation(s)')
    return OK


async def _fork_session(args: argparse.Namespace) -> int:
    """Branch a conversation, through the same code the HTTP route uses.

    The manager's fork rather than a copy of it here: it cuts the messages,
    gives the copy its own id, titles it as a fork and writes it under the new
    id before building anything, and a second version of that sequence is a
    second version to get the ordering wrong. It needs a provider, because
    reopening is rebuilding a live session — which makes no network call, and
    does need the registry to be right about which model to rebuild it with.
    """
    from openmirror.agent.approval import Mode
    from openmirror.agent.manager import manager
    from openmirror.config import config
    from openmirror.providers.registry import NoProviderError

    store = _store()
    if store.load(args.session) is None:
        _note(f'{PROG}: no stored conversation called {args.session!r} in {store.root}')
        return FAILED
    await _bootstrap()
    try:
        provider, _model, _id = await _resolve(None, None)
    except NoProviderError as exc:
        _note(f'{PROG}: {exc}')
        return FAILED
    forked = await manager.fork(args.session, args.at, provider=provider, mode=Mode(config.approval_mode))
    if forked is None:
        _note(f'{PROG}: {args.session!r} could not be forked')
        return FAILED
    await manager.close(forked.id, 'forked from the command line')
    print(f'{forked.id}  forked from {args.session}  ({len(forked.messages)} messages, '
          f'at message {args.at or len(forked.messages)})')
    return OK


async def _search_sessions(args: argparse.Namespace) -> int:
    """Search transcripts, through `openmirror.history`.

    Optional on purpose. Search is a separate piece of work with its own
    matching, and a command that stopped existing because that work was not
    finished would be a worse answer than one that says so.
    """
    try:
        from openmirror.history import search_sessions
    except ImportError:
        _note(f'{PROG}: this build cannot search conversations: openmirror.history is not installed. '
              'The transcripts are all still there — `sessions show ID` will read one.')
        return FAILED

    kwargs: dict[str, Any] = {'limit': args.limit, 'root': None}
    # The store, when the helper takes one: `sessions list` and
    # `sessions search` have to be looking at the same place, and the
    # process-wide default is not necessarily the one this command is reading.
    if 'store' in inspect.signature(search_sessions).parameters:
        kwargs['store'] = _store()
    found = search_sessions(args.query, **kwargs)
    if inspect.isawaitable(found):
        # Whether the helper needs the loop is its own business; this command
        # runs in one either way.
        found = await found
    rows = [row for row in (found or []) if isinstance(row, dict)]
    if not rows and found:
        _note(f'{PROG}: the search returned {len(found)} rows that are not objects; nothing to show')

    if args.as_json:
        print(json.dumps({'query': args.query, 'results': rows}, ensure_ascii=False, indent=2, default=str))
        return OK
    if not rows:
        _note(f'nothing in the transcripts matches {args.query!r}')
        return OK
    for row in rows:
        print(f'{str(row.get("id", ""))[:16]:<18}{_when(row.get("updated")):<18}{row.get("title") or "(untitled)"}')
        where = f'[{row.get("role", "")} #{row.get("message_index", "")}] ' if row.get('role') else ''
        for line in str(row.get('excerpt') or '').splitlines()[:4]:
            print(f'    {where}{line}')
    return OK


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


@command('models')
def models(argv: list[str]) -> int:
    return asyncio.run(_models(_models_parser().parse_args(argv)))


async def _models(args: argparse.Namespace) -> int:
    from openmirror.config import config
    from openmirror.providers.base import Modality
    from openmirror.providers.registry import registry

    try:
        await _bootstrap()
    except Exception as exc:  # noqa: BLE001 - a registry that would not build is reported, not raised
        # Not fatal here. The point of the command is to say what is available,
        # and a registry that failed to build is an answer — an empty one, with
        # the reason beside it.
        _note(f'{PROG}: providers could not be set up: {type(exc).__name__}: {exc}')
    rows: list[dict[str, Any]] = []
    for info in registry.providers_for(Modality.CHAT, local_only=config.local_only):
        if args.provider and info.id != args.provider:
            continue
        names: list[str] = []
        failure = ''
        try:
            listed = await asyncio.wait_for(registry.impl(info.id, Modality.CHAT).models(), MODEL_LIST_TIMEOUT)
            names = [str(m.get('id')) for m in listed if isinstance(m, dict) and m.get('id')]
        except Exception as exc:  # noqa: BLE001 - one provider being down is not this command failing
            # Deliberately every exception, not just the network ones: a
            # provider adapter is third-party-shaped, and what it raises on an
            # unexpected answer is not knowable from here. The marker keeps the
            # rest of the listing useful.
            failure = f'{type(exc).__name__}: {exc}'
        rows.append({
            'provider': info.id,
            'label': info.label,
            'local': info.local,
            'models': names,
            'error': failure or None,
        })

    if args.provider and not rows:
        _note(f'{PROG}: no chat provider called {args.provider!r} is registered')
        return FAILED

    if args.as_json:
        print(json.dumps({'providers': rows}, ensure_ascii=False, indent=2, default=str))
        return OK
    if not rows:
        _note(f'{PROG}: nothing configured that can hold a conversation. See .env.example.')
        return FAILED
    for row in rows:
        where = 'local' if row['local'] else 'remote'
        print(f'{row["provider"]}  ({row["label"]}, {where})')
        if row['error']:
            print(f'  (could not be listed: {row["error"]})')
        for name in row['models']:
            print(f'  {name}')
        if not row['models'] and not row['error']:
            print('  (offers no models)')
    return OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@command('doctor')
def doctor(_argv: list[str]) -> int:
    return asyncio.run(_doctor())


async def _doctor() -> int:
    """Run the diagnostic, and never be the thing that breaks.

    A diagnostic that crashes on the thing it is diagnosing is worse than no
    diagnostic, because it is run precisely when something is already broken
    and it takes the one piece of evidence with it. So everything is inside a
    guard: whatever goes wrong, the answer is a line on stderr and an exit
    code, and everything printed before the failure stays printed.
    """
    try:
        return await _report()
    except Exception as exc:  # noqa: BLE001 - the whole point of this function
        _note(f'{PROG}: the diagnostic stopped early: {type(exc).__name__}: {exc}')
        _note('Everything printed above it still stands.')
        return OK


async def _report() -> int:
    import platform

    from openmirror.config import config
    from openmirror.providers.base import Modality
    from openmirror.providers.registry import registry

    def say(label: str, value: str) -> None:
        print(f'{label:<18}{value}')

    say('openmirror', version())
    say('python', f'{platform.python_version()} ({sys.executable})')
    say('server', f'http://{config.host}:{config.port}   workspace {config.workspace}   mode {config.approval_mode}')
    say('data', f'{config.data_dir}   sessions {config.sessions_dir}')

    with contextlib.suppress(Exception):
        from openmirror.sessions import SessionStore

        say('transcripts', f'{len(SessionStore(config.sessions_dir).list(limit=1000))} stored')

    try:
        await _bootstrap()
    except Exception as exc:  # noqa: BLE001
        say('providers', f'could not be set up: {type(exc).__name__}: {exc}')

    found = registry.providers_for(Modality.CHAT, local_only=config.local_only)
    if found:
        say('providers', ', '.join(f'{p.id}{" (local)" if p.local else ""}' for p in found))
    else:
        say('providers', 'none — nothing is configured that can hold a conversation')

    try:
        _impl, model, provider_id = await _resolve(None, None)
        say('chat model', f'{model} via {provider_id}')
    except Exception as exc:  # noqa: BLE001
        say('chat model', f'none — {exc}')

    with contextlib.suppress(Exception):
        from openmirror.mcp.manager import load_config

        servers = load_config(config.mcp_config)
        say('mcp', f'{config.mcp_config}: {len(servers)} server(s)' if servers
            else f'{config.mcp_config}: none')

    with contextlib.suppress(Exception):
        from openmirror.settings import load

        sources = load(config.workspace).sources
        say('settings', ', '.join(sources) if sources else 'no settings files found')

    missing = [
        name for name, value in (
            ('ANTHROPIC_API_KEY', config.anthropic_key),
            ('OPENAI_API_KEY', config.openai_key),
            ('OLLAMA_BASE_URL', config.ollama_url),
            ('PERCH_HOST', config.perch_host),
            ('OPENWEBUI_BASE_URL', config.openwebui_url),
        ) if not value
    ]
    if not missing:
        say('keys', 'every provider this install could use has its key')
    elif not found:
        say('keys', f'none set. Set one of {", ".join(missing)} (see .env.example) and nothing will answer.')
    else:
        say('keys', f'not set: {", ".join(missing)} — fine, another provider is registered')
    return OK


# ---------------------------------------------------------------------------
# chat — the terminal client
# ---------------------------------------------------------------------------


@command('chat')
def chat(argv: list[str]) -> int:
    """Talk to a running daemon from a terminal.

    A client, not a second way to run an agent. It speaks the same HTTP and
    WebSocket API the browser does and starts nothing of its own, so the daemon
    stays the only thing holding your sessions, your providers and your tools.
    The alternative — a TUI with its own agent loop inside this process — is
    two implementations of the same conversation to keep in step, and the
    browser already is the one people use for anything long.

    So it needs a daemon, and it says so plainly if there is not one rather
    than starting a private one behind your back.
    """
    from openmirror.config import config
    from openmirror.tui import run as tui_run

    args = tui_parser().parse_args(argv)
    return tui_run(
        host=args.host or config.host,
        port=args.port or config.port,
        root=_path(args.root),
        model=args.model or '',
        provider=args.provider or '',
        session_id=args.session or '',
        mode=args.mode or '',
        toolset=list(args.tool or []) or None,
        yes=args.yes,
        system=args.system or '',
        continue_=args.continue_session,
        token=args.token or os.getenv('OPENMIRROR_TOKEN', ''),
        effort=args.effort or '',
        title=args.title or '',
        prompt=args.prompt or '',
        image=list(args.image or []),
    )


def tui_parser() -> argparse.ArgumentParser:
    """`chat`'s own parser.

    Built here rather than imported from `openmirror.tui` on purpose: the help
    text is the one place a person decides whether they want this, and it
    should read as part of the command line they are already looking at. The
    TUI's own parser stays the fallback for `python -m openmirror.tui`.
    """
    from openmirror.agent.approval import Mode

    parser = argparse.ArgumentParser(
        prog=f'{PROG} chat',
        description=(
            'Talk to a running daemon from a terminal: streaming output, '
            'approvals, /commands, @file mentions and pasted images.'
        ),
        epilog=(
            'Needs a daemon. Start one with `openmirror serve` in another\n'
            'terminal or another tab, or let the desktop app start its own.\n'
            'With a non-interactive stdin it reads the prompt from it, runs one\n'
            'turn and exits, so it can be used in a pipeline.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--host', default=None, help='daemon host. Defaults to OPENMIRROR_HOST, then 127.0.0.1.')
    parser.add_argument('--port', type=int, default=None, help='daemon port. Defaults to OPENMIRROR_PORT, then 8477.')
    parser.add_argument('-C', '--root', default=None, metavar='PATH', help='folder to work in. Defaults to this one.')
    parser.add_argument('-m', '--model', default=None, help='model for a new conversation.')
    parser.add_argument('--provider', default=None, help='provider for a new conversation.')
    parser.add_argument('-s', '--session', default=None, metavar='ID', help='attach to a stored conversation.')
    parser.add_argument(
        '-c', '--continue', dest='continue_session', action='store_true',
        help='attach to the most recent conversation in this folder.',
    )
    parser.add_argument(
        '--mode', default=None, metavar='MODE',
        help=f'approval mode for a new conversation: {", ".join(m.value for m in Mode)}.',
    )
    parser.add_argument('--effort', default=None, metavar='LEVEL', help='how hard the model thinks: off..max.')
    parser.add_argument(
        '--tool', action='append', default=None, metavar='NAME',
        help='a toolset group or tool to allow. Repeatable.',
    )
    parser.add_argument('-y', '--yes', action='store_true', help='approve every tool call without asking.')
    parser.add_argument('--system', default=None, metavar='TEXT', help='extra instructions for the first turn.')
    parser.add_argument('--title', default=None, help='title for a new conversation.')
    parser.add_argument('--prompt', default=None, metavar='TEXT', help='run one turn from the command line and exit.')
    parser.add_argument(
        '--image', action='append', default=None, metavar='PATH',
        help='an image to attach to --prompt. Repeatable.',
    )
    parser.add_argument(
        '--token', default=None, metavar='TOKEN',
        help='sign in to a daemon that wants a token. Defaults to OPENMIRROR_TOKEN.',
    )
    return parser


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@command('version')
def show_version(_argv: list[str]) -> int:
    print(f'{PROG} {version()}')
    return OK


__all__ = ['COMMANDS', 'DEFAULT_TIMEOUT', 'FAILED', 'INTERRUPTED', 'OK', 'PORT_TAKEN', 'PROG', 'USAGE',
           'build_parser', 'chat', 'command', 'main', 'serve', 'version']


# `python -m openmirror.cli`, for the cases where the console script is not on
# the path — a source checkout, or an environment where only the module was
# installed. `openmirror.main` has the same guard, and the `openmirror` command
# itself goes through that one.
if __name__ == '__main__':
    raise SystemExit(main())

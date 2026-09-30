"""openmirror's terminal client.

A *pure client* of the running daemon. It creates a session over HTTP, attaches
to `/ws/agent`, and renders what arrives. It imports nothing from
`openmirror.agent`, nothing from `openmirror.sessions` and nothing from the
providers, and that is not an accident of where the file lives — it is the
design. A terminal client that reached into the agent would be a second
implementation of the agent's behaviour, and every rule about what a tool may
do would then exist in two places with one of them stale.

What that boundary costs is written down rather than worked around: this file
cannot know which tools a model might call, so a tool line is rendered from the
`summary` the tool writes about itself and falls back to its arguments. That is
the same string the browser shows, so the two clients ask the same question.

Everything is arranged so the protocol can be tested without a terminal. The
renderer is a function from an event to a string, the line editor is a buffer
with no terminal in it, the approval parser turns a string into a decision, and
`App` is the only class holding a socket and a tty at the same time. The one
place those two meet is `App._out`, which is why the tests need neither.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import dataclasses
import json
import os
import re
import shutil
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

#: Every escape this client can emit. A named table rather than constants
#: scattered through the renderer, so `NO_COLOR` is answered in one place and a
#: new colour cannot be added without somebody deciding about it.
CODES: dict[str, str] = {
    'reset': '\x1b[0m',
    'dim': '\x1b[2m',
    'red': '\x1b[31m',
    'yellow': '\x1b[33m',
    'cyan': '\x1b[36m',
    'reverse': '\x1b[7m',
}


def wants_colour(stream: Any = None, env: dict[str, str] | None = None) -> bool:
    """Whether to emit escapes at all.

    Three refusals, cheapest first, because each is a way somebody has asked
    not to be shown colour:

    * `NO_COLOR` set to anything, including the empty string. The convention
      is presence, not truth — somebody exporting `NO_COLOR=` means it.
    * `TERM=dumb`. A terminal that says it cannot handle escapes will print
      them, which is worse than having none.
    * a stream that is not a terminal. This one matters most: it is what makes
      the client scriptable, and colour in a pipe is corrupted output rather
      than a preference.

    Returns True when nothing has said no, which leaves the usual case — a
    real terminal, colour wanted — alone.
    """
    environ = os.environ if env is None else env
    if 'NO_COLOR' in environ:
        return False
    if (environ.get('TERM') or '').strip().lower() == 'dumb':
        return False
    if environ.get('OPENMIRROR_NO_COLOUR'):
        return False
    if stream is None:
        return True
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - a stream that cannot answer is not a tty
        return False


class Style:
    """Colour, or the absence of it.

    An object rather than a module-level flag, because a process can
    legitimately want a colour stdout and a plain stderr, and because the
    tests build two of these and compare them.
    """

    __slots__ = ('enabled',)

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = bool(enabled)

    def __call__(self, text: str, *names: str) -> str:
        if not self.enabled or not text or not names:
            return text
        prefix = ''.join(CODES.get(name, '') for name in names)
        return f'{prefix}{text}{CODES["reset"]}'

    def dim(self, text: str) -> str:
        return self(text, 'dim')

    def red(self, text: str) -> str:
        return self(text, 'red')

    def yellow(self, text: str) -> str:
        return self(text, 'yellow')

    def cyan(self, text: str) -> str:
        return self(text, 'cyan')


def strip_ansi(text: str) -> str:
    """What the terminal shows once the escapes are gone.

    For the tests rather than for the client: asserting on a string with
    escapes in it says more about the escapes than about the transcript.
    """
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def human(count: int) -> str:
    """A token count at the size a person reads it at."""
    count = max(0, int(count))
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f'{count / 1000:.1f}k'
    return f'{count / 1_000_000:.1f}M'


def took(ms: float) -> str:
    """A duration, in the units somebody would say out loud.

    Mirrors the browser's `took` on purpose: two clients that disagree about
    how to write "how long did that take" is the sort of small drift that
    makes people trust one of them more than they should.
    """
    ms = float(ms)
    if ms < 1000:
        return f'{round(ms)}ms'
    if ms < 60000:
        return f'{ms / 1000:.1f}s' if ms < 10000 else f'{ms / 1000:.0f}s'
    minutes = int(ms // 60000)
    return f'{minutes}m {round((ms % 60000) / 1000)}s'


def flat(text: Any, limit: int) -> str:
    """One line, at most `limit` characters, with an ellipsis if it was cut."""
    line = ' '.join(str(text).split())
    if len(line) <= limit:
        return line
    return line[: max(1, limit - 1)].rstrip() + '…'


# ---------------------------------------------------------------------------
# Events -> terminal lines
# ---------------------------------------------------------------------------

#: Server events that are a complaint rather than content. They go to stderr
#: so that `openmirror chat | tee log.txt` captures the answer, not the errors.
STDERR_EVENTS = frozenset({'error'})

TOOL_GLYPH = '●'
RESULT_GLYPH = '↳'
DENY_GLYPH = '✗'
THINKING_GLYPH = '·'

#: Which argument of a tool call is worth putting on the line, most specific
#: first. "Print whichever argument happens to be there" was tried, and showed
#: `run_in_background=False` more often than the path.
ARG_KEYS = ('path', 'file_path', 'notebook', 'command', 'cmd', 'url', 'query', 'pattern', 'glob', 'text')


def render_arg(value: Any) -> str:
    """One argument as a person would say it.

    A shell command arrives as a list because that is how the tool takes it,
    and `['git', 'status']` on one line is a worse thing to read than
    `git status`.
    """
    if isinstance(value, list):
        return ' '.join(part for part in (render_arg(v) for v in value) if part)
    if isinstance(value, dict):
        return flat(json.dumps(value, ensure_ascii=False), 80)
    return flat(str(value), 80)


def describe_call(call: dict[str, Any]) -> str:
    """`openmirror/config.py` — what this call is doing, in a few words.

    `summary` first, because it is written by the tool that knows which of its
    arguments matters. Falling back to the arguments is what lets a client
    that has never heard of a tool still say something true about it.
    """
    summary = str(call.get('summary') or '').strip()
    if summary:
        return flat(summary, 90)
    arguments = call.get('arguments')
    # `.` and `/` are how a tool says "here", and `● read_file .` tells a
    # person nothing that the tool's own summary would not.
    trivial = (None, '', [], {}, '.', './', '/', '~')
    if isinstance(arguments, dict):
        for key in ARG_KEYS:
            value = arguments.get(key)
            if value not in trivial:
                return flat(render_arg(value), 90)
        arguments = {key: value for key, value in arguments.items() if value not in trivial}
    if arguments:
        return flat(json.dumps(arguments, ensure_ascii=False), 90)
    return ''


def call_line(call: dict[str, Any]) -> str:
    """The whole tool line, without colour."""
    name = str(call.get('name') or 'tool')
    return f'{TOOL_GLYPH} {name} {describe_call(call)}'.rstrip()


def summarise_display(display: dict[str, Any]) -> str:
    """A tool that returns structure rather than prose still gets a line."""
    for key in ('summary', 'path', 'message'):
        if display.get(key):
            return flat(str(display[key]), 80)
    scalars = [
        f'{key}={value}'
        for key, value in display.items()
        if isinstance(value, (str, int, float, bool)) and str(value)[:40]
    ]
    return flat(', '.join(scalars[:3]), 80)


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ''


def call_key(call: dict[str, Any]) -> str:
    """What an "always" answer applies to: this tool, these arguments.

    Scoped the way the daemon scopes `remember`, so the client's memory and
    the server's agree. A client-wide "yes to shell" would be a second, weaker
    version of the rule the protocol exists to keep.
    """
    return f'{call.get("name", "")}\x00{describe_call(call)}'


@dataclass(slots=True)
class Renderer:
    """A session's events, as terminal text.

    Stateful in exactly one way that matters: it remembers the highest `seq`
    it has seen, because that number is the only thing that makes a reconnect
    cheap. The rest is bookkeeping for making a stream look right.

    `feed` takes an event and returns the text to write. It writes nothing
    itself, so a transcript is the join of the returns — and a bug that is
    "it printed the wrong thing" becomes a failing assertion rather than a
    screenshot.
    """

    style: Style = field(default_factory=Style)
    width: int = 80

    #: What the status line shows, plus what a later policy change is
    #: compared against. A replayed `policy.changed` must not claim the
    #: approval mode moved when nothing did.
    info: dict[str, Any] = field(default_factory=dict)
    mode: str = ''
    effort: str | None = None
    #: A message echoed locally when it was submitted, so the server's own
    #: echo of the same words can be recognised and dropped. Without this a
    #: person sees their question twice, once the instant they press Enter and
    #: again when the turn starts.
    echoed: str = ''

    seq: int = 0
    streaming: bool = False
    turn_at: float = 0.0
    _at_line_start: bool = True
    _col: int = 0
    _thinking: list[str] = field(default_factory=list)
    _output: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.style = self.style if isinstance(self.style, Style) else Style(False)
        self.width = max(20, int(self.width))

    # -- plumbing ----------------------------------------------------------

    def feed(self, event: dict[str, Any]) -> str:
        """The terminal text for one event, and nothing else.

        An unknown `type` renders as the empty string deliberately: the wire
        protocol is versioned separately from this file, and a client that
        prints a blank line for an event it does not know is still a working
        client.
        """
        sequence = event.get('seq')
        if isinstance(sequence, int) and sequence > self.seq:
            self.seq = sequence
        handler = _HANDLERS.get(str(event.get('type') or ''))
        if handler is None:
            return ''
        out = ''
        if event.get('type') != 'thinking.delta':
            # Reasoning is a collapsible marker, so it is folded at the first
            # thing that follows it rather than at the end of the turn — by
            # which point the answer has been streaming over the top of it.
            out += self.flush_thinking()
        return out + handler(self, event)

    def transcript(self, events: Iterable[dict[str, Any]]) -> str:
        """Every event, as one string. The shape a test asserts on."""
        return ''.join(self.feed(event) for event in events)

    def _line(self, text: str) -> str:
        """A whole line, prefixed with a newline when one is owed."""
        lead = '' if self._at_line_start else '\n'
        self._at_line_start = True
        self._col = 0
        return f'{lead}{text}\n'

    def _stream(self, text: str) -> str:
        """Assistant text, wrapped as it arrives rather than afterwards.

        Rewrapping a paragraph once it is complete repaints the screen and
        loses the reader's place mid-sentence, which is worse than a hard
        edge in the middle of a long line.
        """
        out: list[str] = []
        parts = text.split('\n')
        for index, part in enumerate(parts):
            if index:
                out.append('\n')
                self._col = 0
                self._at_line_start = True
            while part:
                room = self.width - self._col
                if room <= 0:
                    out.append('\n')
                    self._col = 0
                    self._at_line_start = True
                    continue
                if len(part) <= room:
                    out.append(part)
                    self._col += len(part)
                    break
                window = part[:room]
                cut = window.rfind(' ')
                if cut > 0:
                    head, rest = window[:cut], part[cut + 1 :]
                else:
                    # A word longer than the line. Broken rather than left
                    # hanging past the edge, where the terminal would wrap it
                    # at its own width and disagree with ours.
                    head, rest = window, part[room:]
                out.append(head.rstrip())
                out.append('\n')
                self._col = 0
                self._at_line_start = True
                part = rest
        return ''.join(out)

    def flush_thinking(self) -> str:
        """Fold the reasoning so far into one dim marker."""
        if not self._thinking:
            return ''
        text = ' '.join(''.join(self._thinking).split())
        self._thinking = []
        if not text:
            return ''
        words = text.split()
        body = ' '.join(words[:12]) + ('…' if len(words) > 12 else '')
        if len(words) <= 12:
            return self._line(self.style.dim(f'{THINKING_GLYPH} thinking: {body}'))
        return self._line(self.style.dim(f'{THINKING_GLYPH} thinking: {body} ({len(words)} words)'))

    # -- events ------------------------------------------------------------

    def _on_started(self, event: dict[str, Any]) -> str:
        """The session banner belongs to the status line, not the transcript.

        Printed here it would be a second copy of what the prompt already
        carries, and a replay after a reconnect would print it again.
        """
        self.info = {
            'id': event.get('session_id', ''),
            'cwd': event.get('cwd', ''),
            'model': event.get('model', ''),
            'policy': event.get('policy', ''),
            'effort': event.get('effort'),
            'tools': len(event.get('tools') or []),
        }
        # The short mode is *not* in this event, only the sentence describing
        # what it permits. Seeding the comparison from the sentence would make
        # every `policy.changed` look like a change, so the first one wins.
        self.effort = event.get('effort')
        return ''

    def _on_turn_started(self, event: dict[str, Any]) -> str:
        self.turn_at = float(event.get('at') or 0.0)
        self._col = 0
        text = str(event.get('text') or '')
        if text and text == self.echoed:
            # The local echo already showed it. Dropping the duplicate is what
            # keeps a live client and a reattached one showing the same
            # conversation rather than a live one showing everything twice.
            self.echoed = ''
            return ''
        return self.echo(text)

    def echo(self, text: str) -> str:
        """Somebody's own message, as it appears in the transcript.

        Rendered here rather than at the prompt so that what the server
        replays and what was typed look the same to anyone reading it back.
        """
        if not text:
            return ''
        lines = text.split('\n')
        body = [self.style.cyan('> ' + lines[0])]
        body += [self.style.cyan('  ' + line) for line in lines[1:]]
        return self._line('\n'.join(body))

    def _on_text_delta(self, event: dict[str, Any]) -> str:
        if not self.streaming:
            self.streaming = True
            lead = '' if self._at_line_start else '\n'
            self._at_line_start = False
            text = lead + str(event.get('text') or '')
        else:
            text = str(event.get('text') or '')
        return self._stream(text) if text else ''

    def _on_thinking_delta(self, event: dict[str, Any]) -> str:
        self._thinking.append(str(event.get('text') or ''))
        return ''

    def _on_text_flush(self, event: dict[str, Any]) -> str:
        """A batching hint the browser sends itself. Nothing to do here."""
        return ''

    def _on_tool_proposed(self, event: dict[str, Any]) -> str:
        call = dict(event.get('call') or {})
        identifier = str(call.get('id') or '')
        if identifier:
            self._output[identifier] = ''
        line = self.style.dim(call_line(call))
        if event.get('needs_approval'):
            line += self.style.yellow('  (needs approval)')
        return self._line(line)

    def _on_tool_started(self, event: dict[str, Any]) -> str:
        # The proposal already said what was about to happen. A second line
        # saying it started is the difference between a transcript and a log.
        return ''

    def _on_tool_output(self, event: dict[str, Any]) -> str:
        """Keep the last thing it said, in case the result is empty.

        A four-minute build produces minutes of this. Printing it live is
        honest and unreadable, so it is held and shown once, as part of the
        result line.
        """
        line = first_line(str(event.get('text') or ''))
        if line:
            self._output[str(event.get('call_id') or '')] = flat(line, 90)
        return ''

    def _on_tool_completed(self, event: dict[str, Any]) -> str:
        result = dict(event.get('result') or {})
        identifier = str(result.get('id') or '')
        body = first_line(str(result.get('content') or '')) or self._output.get(identifier, '')
        if not body and result.get('display'):
            body = summarise_display(result['display'])
        body = flat(body, 96)
        duration = result.get('duration_ms') or 0
        tail = f' ({took(duration)})' if duration else ''
        if result.get('truncated'):
            tail += ', truncated'
        if not result.get('ok'):
            return self._line(self.style.red(f'  {RESULT_GLYPH} failed: {body or "no detail"}'))
        if not body:
            return self._line(self.style.dim(f'  {RESULT_GLYPH} ok{tail}'))
        return self._line(self.style.dim(f'  {RESULT_GLYPH} {body}{tail}'))

    def _on_tool_denied(self, event: dict[str, Any]) -> str:
        why = str(event.get('reason') or '').strip()
        detail = f': {flat(why, 90)}' if why else ''
        return self._line(self.style.red(f'  {DENY_GLYPH} denied{detail}'))

    def _on_question(self, event: dict[str, Any]) -> str:
        lines = [self.style.yellow('? ' + flat(str(event.get('question') or ''), 200))]
        for index, option in enumerate(event.get('options') or [], start=1):
            lines.append(self.style.dim(f'  {index}) {flat(str(option), 80)}'))
        if event.get('options'):
            lines.append(self.style.dim('  (a number, or type your own answer)'))
        return self._line('\n'.join(lines))

    def _on_hook_approval(self, event: dict[str, Any]) -> str:
        hook = dict(event.get('hook') or {})
        command = hook.get('command_readable') or hook.get('command') or hook.get('event') or 'a hook'
        return self._line(self.style.yellow(f'⚑ hook wants to run: {flat(str(command), 160)}'))

    def _on_turn_queued(self, event: dict[str, Any]) -> str:
        waiting = int(event.get('waiting') or 0)
        return self._line(
            self.style.dim(f'{THINKING_GLYPH} held — it runs when this turn finishes ({waiting} waiting)')
        )

    def _on_turn_completed(self, event: dict[str, Any]) -> str:
        out = '' if self._at_line_start else self._line('')
        self.streaming = False
        self._at_line_start = True
        self._col = 0
        stop = str(event.get('stop_reason') or 'end_turn')
        parts: list[str] = []
        # The server's clock, not ours: a reattached client replays turns that
        # ended hours ago, and timing them against `now` would report every one
        # of them as having taken a millisecond.
        if self.turn_at and event.get('at'):
            parts.append(took((float(event['at']) - self.turn_at) * 1000))
        usage = event.get('context') or {}
        tokens = int(usage.get('tokens') or 0)
        limit = int(usage.get('limit') or 0)
        if limit:
            fraction = tokens / limit
            parts.append(f'context {human(tokens)}/{human(limit)} ({fraction * 100:.0f}%)')
            if fraction >= 0.85:
                # Said when it is nearly true rather than always: a warning
                # that cries wolf is a warning that is ignored.
                parts.append('/compact soon')
        if stop == 'interrupted':
            out += self._line(self.style.dim(f'{THINKING_GLYPH} interrupted'))
        elif stop == 'max_steps':
            out += self._line(self.style.red(f'{THINKING_GLYPH} stopped: too many steps'))
        elif stop == 'error':
            out += self._line(self.style.red(f'{THINKING_GLYPH} stopped: error'))
        if parts:
            out += self._line(self.style.dim(f'{THINKING_GLYPH} ' + ' · '.join(parts)))
        self.turn_at = 0.0
        return out

    def _on_error(self, event: dict[str, Any]) -> str:
        message = flat(str(event.get('message') or 'something went wrong'), 200)
        retry = ' (retryable)' if event.get('retryable') else ''
        return self._line(self.style.red(f'! {message}{retry}'))

    def _on_policy_changed(self, event: dict[str, Any]) -> str:
        # Only what actually moved. Announcing a thinking change as "approval
        # is now: ask first" is a true sentence about the wrong thing.
        out = ''
        mode = str(event.get('mode') or '')
        if mode and mode != self.mode:
            out += self._line(self.style.dim(
                f'{THINKING_GLYPH} approval is now: {str(event.get("policy") or "") or mode}'
            ))
            self.mode = mode
        effort = event.get('effort')
        if effort != self.effort:
            said = effort or "the model's own default"
            out += self._line(self.style.dim(f'{THINKING_GLYPH} thinking is now: {said}'))
            self.effort = effort
        return out

    def _on_task_updated(self, event: dict[str, Any]) -> str:
        task = dict(event.get('task') or {})
        if not task:
            return ''
        status = str(task.get('status') or '')
        bits = [
            THINKING_GLYPH + ' task',
            str(task.get('kind') or 'task'),
            flat(str(task.get('label') or task.get('id') or ''), 60),
            status,
        ]
        if task.get('exit_code') is not None:
            bits.append(f'exit {task["exit_code"]}')
        line = ' '.join(bit for bit in bits if bit)
        return self._line(self.style.red(line) if status in ('failed', 'error') else self.style.dim(line))

    def _on_context_compacted(self, event: dict[str, Any]) -> str:
        reason = str(event.get('reason') or 'manual')
        if reason == 'cleared':
            return self._line(self.style.dim(f'{THINKING_GLYPH} context cleared'))
        bits = [
            f'{THINKING_GLYPH} context compacted',
            f'{int(event.get("messages_before") or 0)} → {int(event.get("messages_after") or 0)} messages',
        ]
        if event.get('tokens_before'):
            bits.append(f'~{human(int(event["tokens_before"]))} tokens')
        if reason == 'automatic':
            bits.append('automatic')
        return self._line(self.style.dim(' · '.join(bits)))

    def _on_session_ended(self, event: dict[str, Any]) -> str:
        reason = event.get('reason') or 'closed'
        return self._line(self.style.dim(f'{THINKING_GLYPH} session ended: {reason}'))

    def _on_pong(self, event: dict[str, Any]) -> str:
        return ''


_HANDLERS = {
    'session.started': Renderer._on_started,
    'turn.started': Renderer._on_turn_started,
    'text.delta': Renderer._on_text_delta,
    'text.flush': Renderer._on_text_flush,
    'thinking.delta': Renderer._on_thinking_delta,
    'tool.proposed': Renderer._on_tool_proposed,
    'tool.started': Renderer._on_tool_started,
    'tool.output.delta': Renderer._on_tool_output,
    'tool.completed': Renderer._on_tool_completed,
    'tool.denied': Renderer._on_tool_denied,
    'question.asked': Renderer._on_question,
    'hook.approval': Renderer._on_hook_approval,
    'turn.queued': Renderer._on_turn_queued,
    'turn.completed': Renderer._on_turn_completed,
    'error': Renderer._on_error,
    'policy.changed': Renderer._on_policy_changed,
    'task.updated': Renderer._on_task_updated,
    'context.compacted': Renderer._on_context_compacted,
    'session.ended': Renderer._on_session_ended,
    'pong': Renderer._on_pong,
}


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Slash:
    """A line that begins with `/`.

    `local` means this client answers it. Everything else — `/compact`,
    `/clear`, `/think` and every skill — is sent to the daemon as ordinary
    turn text, because that is how the daemon runs them: they are shaped like
    a turn so a transcript can show them as one, and a client that intercepted
    them would have to reimplement what they do.
    """

    name: str
    args: str = ''
    local: bool = False


#: The shape the daemon parses with, deliberately. A client that split
#: differently would turn a line the daemon would have run into text it sends
#: as an ordinary prompt, which is a silent and confusing difference.
_SLASH = re.compile(r'/([A-Za-z0-9_:-]+)(?:\s+(.*))?$', re.S)

#: Answered here rather than by the daemon. Deliberately small: anything that
#: changes the conversation belongs to the daemon, which is the only thing
#: that knows what the conversation is.
LOCAL_COMMANDS: dict[str, str] = {
    'exit': 'Leave. The session stays open, for --continue.',
    'quit': 'The same as /exit.',
    'help': 'This list.',
    'status': 'Session, folder, model, approval mode, thinking level, link.',
    'model': 'Show the model this session is using.',
    'mode': 'Show or change the approval mode: ask, auto-read, full-auto, unrestricted.',
    'commands': 'Every command and skill the daemon reports for this session.',
    'clear-screen': 'Redraw the terminal.',
}


def parse_slash(text: str) -> Slash | None:
    """`/think high` -> `Slash('think', 'high')`, or None for ordinary text."""
    match = _SLASH.match(text.strip())
    if not match:
        return None
    name = match.group(1).lower()
    return Slash(name, (match.group(2) or '').strip(), name in LOCAL_COMMANDS)


def slash_completions(
    prefix: str, commands: Sequence[dict[str, str]], limit: int = 10
) -> list[dict[str, str]]:
    """Which of the daemon's commands a `/` could be followed by.

    Local ones are offered first, because they answer immediately and a `/e`
    that runs a project skill instead of leaving is a nasty surprise. The
    daemon's order is kept after that — commands, then skills, each sorted —
    since it is the only thing that knows which of them are built in.
    """
    needle = prefix.lstrip('/').lower()
    out: list[dict[str, str]] = [
        {'name': name, 'description': text, 'kind': 'local'}
        for name, text in LOCAL_COMMANDS.items()
        if name.startswith(needle)
    ]
    for entry in commands:
        name = str(entry.get('name') or '')
        if name and name.lower().startswith(needle) and not any(c['name'] == name for c in out):
            out.append({
                'name': name,
                'description': str(entry.get('description') or ''),
                'kind': str(entry.get('kind') or 'command'),
            })
        if len(out) >= limit:
            break
    return out[:limit]


# ---------------------------------------------------------------------------
# `@` file mentions
# ---------------------------------------------------------------------------


def mention_at(text: str, cursor: int) -> tuple[int, str] | None:
    """The `@token` the cursor is inside, as `(start, query)`, or None.

    Scanning back to the last `@` rather than splitting on a space is what
    lets `see @src/openmir` complete as one path. A whitespace-delimited
    token cannot express a nested path, which is most of them.
    """
    cursor = max(0, min(int(cursor), len(text)))
    start = -1
    for index in range(cursor - 1, -1, -1):
        char = text[index]
        if char in ' \t\n':
            break
        if char == '@':
            start = index
            break
    if start < 0:
        return None
    # `@` has to open the token: `me@example.com` is an address, not a file.
    if start > 0 and text[start - 1] not in ' \t\n':
        return None
    return start, text[start + 1 : cursor]


def filter_files(hits: Sequence[dict[str, Any]], query: str, limit: int = 8) -> list[str]:
    """Narrow the daemon's answer to what is still worth offering.

    The daemon filters on `q` and so does this, because the two answer
    different questions: the daemon matches anywhere in the path, and somebody
    typing `@conf` wants the config files rather than every file whose path
    happens to contain those four letters.
    """
    needle = query.strip().lower().lstrip('./')
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for hit in hits:
        path = str(hit.get('path') if isinstance(hit, dict) else hit).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        lowered = path.lower()
        if not needle:
            rank = 0
        elif lowered.endswith(needle):
            rank = 0
        elif lowered.startswith(needle) or '/' + needle in lowered:
            rank = 1
        elif needle in lowered:
            rank = 2
        else:
            continue
        ranked.append((rank, path))
    # Stable on purpose: for an empty query every rank is 0, and the daemon's
    # own ordering — exact matches first, then most recently touched — is
    # better than an alphabetical one.
    ranked.sort(key=lambda item: item[0])
    return [path for _rank, path in ranked[:limit]]


def apply_completion(text: str, cursor: int, completion: str) -> tuple[str, int]:
    """Put `completion` where the token under the cursor was.

    Returns the new text and where the cursor lands — after the inserted
    path, not at the end of the line, because completing in the middle of a
    sentence is normal.
    """
    cursor = max(0, min(int(cursor), len(text)))
    mention = mention_at(text, cursor)
    if mention is None:
        return text, cursor
    start = mention[0]
    end = text.find(' ', cursor)
    if end < 0:
        end = len(text)
    return text[:start] + completion + text[end:], start + len(completion)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approval:
    """What a person answered at an approval prompt."""

    allow: bool
    remember: bool = False
    #: The answer was EOF: nobody is there. Refuse, and stop the turn.
    abort: bool = False
    #: The answer was not one of the offered ones, so the caller should ask
    #: again rather than treat a typo as a decision.
    valid: bool = True


def parse_approval(answer: str | None) -> Approval:
    """`y` allows, `a` allows and remembers, everything else refuses.

    Matched against the whole trimmed answer rather than its first letter.
    `yolo`, `aye` and `maybe` all begin with a letter that means yes and none
    of them is yes, and a client that approves `yolo` is a client whose
    approvals mean nothing.

    EOF (`None`) is a denial that also stops the turn. A closed stdin is not
    consent to run a shell, and it is not a reason to keep asking a question
    nobody is there to read.
    """
    if answer is None:
        return Approval(allow=False, abort=True, valid=False)
    text = answer.strip().lower()
    if text in ('y', 'yes'):
        return Approval(allow=True)
    if text in ('a', 'always'):
        return Approval(allow=True, remember=True)
    if text in ('', 'n', 'no'):
        return Approval(allow=False)
    # Not an answer anybody offered is neither a yes nor a no: the caller
    # re-asks, because silently reading `what?` as a refusal turns a typo
    # into a decision about somebody's filesystem.
    return Approval(allow=False, valid=False)


def approval_prompt(name: str) -> str:
    """The question itself.

    `stderr`, because `openmirror chat | tee log.txt` should capture what the
    agent said rather than a transcript of somebody typing.
    """
    return f'Allow {name}? [y/N/a(lways)] '


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

#: Five megabytes, which is the ceiling every major provider enforces per
#: image. Refusing locally with an explanation beats a base64 blob accepted by
#: a socket and rejected by a provider three seconds later.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

IMAGE_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}

#: Magic bytes, checked before the extension. A `.png` that is really a zip is
#: not a picture, and what the provider says about that is useless.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)

#: Formats that are definitely not pictures. Their presence beats an image
#: extension: a renamed file is more often a mistake than a deliberate trick,
#: and either way there is nothing here worth sending.
_NOT_IMAGES: tuple[bytes, ...] = (
    b'PK\x03\x04',   # zip, and therefore xlsx and docx
    b'PK\x05\x06',
    b'%PDF',
    b'\x1f\x8b',     # gzip
    b'\x7fELF',
    b'{\"',          # json
)


class AttachmentError(ValueError):
    """A path that cannot become an attachment, with a reason worth reading."""


def describe_container(head: bytes) -> str:
    """Name the format, so the refusal says what the file actually is."""
    if head.startswith(b'PK'):
        return 'zip'
    if head.startswith(b'%PDF'):
        return 'PDF'
    if head.startswith(b'\x1f\x8b'):
        return 'gzip'
    if head.startswith(b'\x7fELF'):
        return 'binary'
    if head.startswith(b'{\"'):
        return 'text'
    return 'non-image'


def sniff_media_type(head: bytes) -> str | None:
    """What kind of image these bytes are, or None if they are not one."""
    for prefix, media_type in _MAGIC:
        if head.startswith(prefix):
            return media_type
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return None


def build_image_attachment(path: str | Path, *, max_bytes: int = MAX_ATTACHMENT_BYTES) -> dict[str, str]:
    """Read an image and shape it the way `turn.submit` wants it.

    The size is checked with `stat` before anything is read, so refusing a
    two-gigabyte file costs one syscall rather than two gigabytes of memory.
    """
    target = Path(path).expanduser()
    try:
        info = target.stat()
    except FileNotFoundError as exc:
        raise AttachmentError(f'no such file: {target}') from exc
    except OSError as exc:
        raise AttachmentError(f'cannot read {target}: {exc}') from exc
    if not target.is_file():
        raise AttachmentError(f'not a file: {target}')
    if info.st_size > max_bytes:
        raise AttachmentError(
            f'{target} is {info.st_size:,} bytes; one image may be at most {max_bytes:,}. Scale it down first.'
        )
    if info.st_size == 0:
        raise AttachmentError(f'{target} is empty')

    data = target.read_bytes()[: max_bytes + 1]
    if data[:8].startswith(_NOT_IMAGES) or data[:4].startswith(_NOT_IMAGES):
        raise AttachmentError(
            f'{target} is a {describe_container(data)} file, not an image, whatever it is called.'
        )
    media_type = sniff_media_type(data[:16]) or IMAGE_TYPES.get(target.suffix.lower())
    if media_type is None:
        # Never guessed at. Sending eight bytes of text labelled as a PNG fails
        # at the provider with an error nobody can act on.
        raise AttachmentError(f'{target} does not look like an image. Supported: png, jpeg, gif, webp.')
    return {'type': 'image', 'media_type': media_type, 'data': base64.b64encode(data).decode('ascii')}


#: `!path` at the end of a prompt, which is what dragging a file into a
#: terminal produces. The quoted form is tried first, because a dragged path
#: may contain spaces.
_IMAGE_TOKEN = re.compile(r'(?:^|\s)!(?:"([^"]+)"|(\S+))\s*$')


def split_image_tokens(
    text: str, *, max_bytes: int = MAX_ATTACHMENT_BYTES
) -> tuple[str, list[dict[str, str]]]:
    """Take a trailing `!path` off a prompt, returning the rest and the attachment.

    Only a *trailing* token is taken. A `!` in the middle of a sentence is
    punctuation far more often than it is a drag, and silently reinterpreting
    prose as a filename is worse than not offering the shorthand at all.
    """
    match = _IMAGE_TOKEN.search(text)
    if not match:
        return text, []
    raw = (match.group(1) or match.group(2) or '').strip()
    if not raw:
        return text, []
    return text[: match.start()].rstrip(), [build_image_attachment(raw, max_bytes=max_bytes)]


def attachments_from_paths(
    paths: Iterable[str | Path], *, max_bytes: int = MAX_ATTACHMENT_BYTES
) -> list[dict[str, str]]:
    """`--image a.png --image b.jpg`, with the first refusal stopping the lot."""
    return [build_image_attachment(path, max_bytes=max_bytes) for path in paths]


# ---------------------------------------------------------------------------
# The line editor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Line:
    """The editable line, with no terminal anywhere in it.

    Keys arrive as `Key` values, this turns them into text, and the caller
    decides how to draw it. That split is why the editor is testable: arrow
    keys, kill-word and history are ordinary state transitions with nothing to
    mock.
    """

    text: str = ''
    cursor: int = 0
    history: list[str] = field(default_factory=list)
    _index: int = 0

    def __post_init__(self) -> None:
        self.cursor = len(self.text)

    # -- editing -----------------------------------------------------------

    def set(self, text: str, cursor: int | None = None) -> None:
        self.text = text
        self.cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))

    def clear(self) -> None:
        self.set('')

    def insert(self, chunk: str) -> None:
        if not chunk:
            return
        self.text = self.text[: self.cursor] + chunk + self.text[self.cursor :]
        self.cursor += len(chunk)

    def backspace(self) -> None:
        if self.cursor <= 0:
            return
        self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
        self.cursor -= 1

    def delete(self) -> None:
        if self.cursor >= len(self.text):
            return
        self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(self.cursor + delta, len(self.text)))

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.text)

    def kill_word(self) -> None:
        """Delete back to the start of the word, the way a shell does it."""
        end = self.cursor
        while end > 0 and self.text[end - 1].isspace():
            end -= 1
        while end > 0 and not self.text[end - 1].isspace():
            end -= 1
        self.text = self.text[:end] + self.text[self.cursor :]
        self.cursor = end

    def kill_line(self) -> None:
        self.text = self.text[self.cursor :]
        self.cursor = 0

    # -- history -----------------------------------------------------------

    def remember(self, submitted: str) -> None:
        """Keep it, once.

        De-duplicated because somebody who sends the same thing twice in a row
        is holding down a key, not making history.
        """
        submitted = submitted.strip()
        if not submitted:
            return
        if not self.history or self.history[-1] != submitted:
            self.history.append(submitted)
        del self.history[:-1000]
        self._index = len(self.history)

    def older(self) -> bool:
        """Step back through history. False when there is nothing older."""
        if not self.history or self._index <= 0:
            return False
        self._index -= 1
        self.set(self.history[self._index])
        return True

    def newer(self) -> bool:
        if self._index >= len(self.history):
            return False
        self._index += 1
        self.set(self.history[self._index] if self._index < len(self.history) else '')
        return True

    def preview(self, width: int) -> tuple[str, int]:
        """`(text, offset)` for drawing a line wider than the terminal.

        The offset is how much has scrolled off the left, so the cursor stays
        on screen while typing into the middle of a long line.
        """
        width = max(4, int(width))
        if len(self.text) <= width:
            return self.text, 0
        # `+ 1` on both bounds: the cursor is *between* characters, so a line
        # scrolled to its very end still leaves room for it.
        offset = max(0, min(self.cursor - width + 1, len(self.text) - width + 1))
        return self.text[offset : offset + width], offset


# ---------------------------------------------------------------------------
# Terminal plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Key:
    """One keystroke, named.

    Named rather than handed over as characters so that `'\x03'` — a control
    code whose meaning depends entirely on what read it — never reaches the
    editor.
    """

    name: str
    text: str = ''


def _is_tty(stream: Any) -> bool:
    """Whether this stream is a terminal, answering no rather than raising.

    A closed or replaced stream raises here, and "not a terminal" is both the
    right answer and the one that degrades instead of crashing.
    """
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 - a stream that cannot answer is not a tty
        return False


def _control(char: str) -> Key:
    """One character to a `Key`. The whole of key decoding, on both platforms."""
    if char in ('\r', '\n'):
        return Key('enter')
    if char == '\t':
        return Key('tab')
    if char in ('\x7f', '\x08'):
        return Key('backspace')
    if char == '\x03':
        return Key('interrupt')
    if char == '\x04':
        return Key('eof')
    if char == '\x0c':
        return Key('clear')
    if char == '\x01':
        return Key('home')
    if char == '\x05':
        return Key('end')
    if char == '\x15':
        return Key('kill-line')
    if char == '\x17':
        return Key('kill-word')
    if char == '\x1b':
        return Key('escape')
    if char < ' ':
        return Key('unknown')
    return Key('char', char)


def _escape(rest: str) -> Key:
    return {
        '[A': Key('up'), 'OA': Key('up'),
        '[B': Key('down'), 'OB': Key('down'),
        '[C': Key('right'), 'OC': Key('right'),
        '[D': Key('left'), 'OD': Key('left'),
        '[H': Key('home'), 'OH': Key('home'),
        '[F': Key('end'), 'OF': Key('end'),
        '[1~': Key('home'), '[4~': Key('end'), '[7~': Key('home'), '[8~': Key('end'),
        '[3~': Key('delete'),
    }.get(rest, Key('unknown'))


class Raw:
    """A terminal in cbreak mode, read one byte at a time.

    Two implementations, because there are two ways to stop a line discipline
    from eating your keys and neither is optional: `termios` on POSIX, the
    console mode API on Windows. `__enter__` returns False rather than raising
    when neither works, and the caller falls back to whole-line input instead
    of pretending to be interactive.
    """

    def __init__(self, stream: Any = None) -> None:
        self.stream = sys.stdin if stream is None else stream
        self.saved: Any = None
        self.active = False

    def __enter__(self) -> Raw:
        """Returns itself, as a context manager should; read `active`."""
        self.saved = self._open_windows() if os.name == 'nt' else self._open_posix()
        self.active = self.saved is not None and self.saved is not False
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open_posix(self) -> Any:
        try:
            import termios
            import tty
        except ImportError:  # pragma: no cover - a POSIX build without termios
            return False
        try:
            descriptor = self.stream.fileno()
            saved = termios.tcgetattr(descriptor)
            tty.setcbreak(descriptor)
            # `setcbreak` alone leaves ISIG on, which means ctrl-c arrives as
            # SIGINT rather than as a byte: the interpreter raises
            # KeyboardInterrupt and the client exits instead of interrupting
            # the turn. The one place a signal is wanted is a terminal in a
            # state where none of this worked, and `run` still catches it.
            # Output post-processing is deliberately left alone, so `\n` still
            # becomes a carriage return and a newline.
            attrs = termios.tcgetattr(descriptor)
            attrs[0] &= ~(termios.BRKINT | termios.ICRNL)
            attrs[3] &= ~termios.ISIG
            termios.tcsetattr(descriptor, termios.TCSANOW, attrs)
        except (AttributeError, ValueError, OSError, termios.error):
            return False
        return saved

    def _open_windows(self) -> Any:  # pragma: no cover - Windows only
        try:
            import ctypes
            import msvcrt

            handle = ctypes.wintypes.HANDLE(msvcrt.get_osfhandle(self.stream.fileno()))
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            mode = ctypes.wintypes.DWORD()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            # ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200), so arrows arrive as
            # escape sequences rather than scan codes, plus the echo and
            # line-input bits cleared so a keystroke is not printed twice.
            kernel32.SetConsoleMode(handle, ctypes.wintypes.DWORD(mode.value | 0x0200))
            kernel32.SetConsoleMode(handle, ctypes.wintypes.DWORD((mode.value | 0x0200) & ~0x0004 & ~0x0002))
            return mode.value
        except Exception:  # noqa: BLE001 - a console that will not cooperate is not interactive
            return False

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        if os.name == 'nt':
            self._close_windows()
        else:
            self._close_posix()

    def _close_posix(self) -> None:
        try:
            import termios

            termios.tcsetattr(self.stream.fileno(), termios.TCSADRAIN, self.saved)
        except Exception:  # noqa: BLE001 - never fail on the way out of a terminal
            pass

    def _close_windows(self) -> Any:  # pragma: no cover - Windows only
        try:
            import ctypes
            import msvcrt

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(ctypes.wintypes.HANDLE(msvcrt.get_osfhandle(self.stream.fileno())),
                                    ctypes.wintypes.DWORD(self.saved))
        except Exception:  # noqa: BLE001
            pass

    def read(self) -> Key:
        """Block for one key. Returns `eof` rather than raising at the end."""
        return self._read_windows() if os.name == 'nt' else self._read_posix()

    def _read_posix(self) -> Key:
        import select

        try:
            first = os.read(self.stream.fileno(), 1)
        except OSError:
            return Key('eof')
        if not first:
            return Key('eof')
        char = first.decode('utf-8', 'replace')
        if char != '\x1b':
            return _control(char)
        # A lone escape is a real key here — it closes a popup — but an arrow
        # arrives as three bytes. The timeout is what tells them apart without
        # blocking on a key that is never coming.
        if not select.select([self.stream.fileno()], [], [], 0.05)[0]:
            return Key('escape')
        try:
            rest = os.read(self.stream.fileno(), 2).decode('utf-8', 'replace')
        except OSError:
            return Key('escape')
        return _escape(rest)

    def _read_windows(self) -> Key:  # pragma: no cover - Windows only
        import msvcrt

        try:
            char = msvcrt.getwch()
        except (OSError, KeyboardInterrupt):
            return Key('eof')
        if char in ('\x00', '\xe0'):
            code = msvcrt.getwch()
            return {
                # msvcrt's scan codes: arrows, Home/End, and Delete, which
                # arrives as 'S' rather than as the character the key bears.
                'H': Key('up'), 'P': Key('down'), 'K': Key('left'), 'M': Key('right'),
                'G': Key('home'), 'O': Key('end'), 'S': Key('delete'),
            }.get(code, Key('unknown'))
        return _control(char)


class Keys:
    """Keys from a terminal, or whole lines from a pipe.

    The fallback is a different mode rather than a broken one: without a tty
    there are no cursor keys and no tab completion to speak of, so input is a
    line at a time. Either way the caller gets a stream of `Key` values, which
    is what keeps the main loop free of a special case.
    """

    def __init__(self, stream: Any = None, *, raw: Raw | None = None) -> None:
        self.stream = sys.stdin if stream is None else stream
        self.raw = raw

    def __iter__(self) -> Iterator[Key]:
        if self.raw is not None and self.raw.active:
            while True:
                key = self.raw.read()
                yield key
                if key.name == 'eof':
                    return
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                return
            if line:
                # The whole line as one key: it was typed, or pasted, without
                # ever being a character at a time.
                yield Key('char', line)
            yield Key('enter')


class Terminal:
    """Every byte this client writes, and the only object holding a stream.

    Unbuffered on purpose. An answer that appears only when the turn ends is
    the difference between watching an agent work and wondering whether it has
    frozen.

    `interactive` means "there is a terminal on the far end", which is not the
    same question as "is stdin a terminal" — `openmirror chat | tee log.txt`
    is typed at but written somewhere else, and the cursor-moving escapes have
    to be dropped there or they end up in the file.
    """

    def __init__(
        self,
        out: Any = None,
        err: Any = None,
        *,
        colour: bool | None = None,
        width: int | None = None,
        interactive: bool | None = None,
    ) -> None:
        self.out = sys.stdout if out is None else out
        self.err = sys.stderr if err is None else err
        self.style = Style(wants_colour(self.out) if colour is None else colour)
        self.width = int(width) if width else self._measure()
        self.interactive = _is_tty(self.out) if interactive is None else bool(interactive)

    def _measure(self) -> int:
        try:
            columns = shutil.get_terminal_size((80, 24)).columns
        except OSError:
            columns = 80
        return max(40, min(int(columns), 200))

    def write(self, text: str, *, err: bool = False) -> None:
        if not text:
            return
        stream = self.err if err else self.out
        try:
            stream.write(text)
            stream.flush()
        except (BrokenPipeError, ValueError):
            # A closed pipe is how `| head` ends. Not worth a traceback: the
            # work is done, the reader simply stopped reading.
            pass


def popup_lines(
    items: Sequence[dict[str, str]], selected: int, style: Style, width: int = 80
) -> list[str]:
    """The completion menu, as lines.

    A function rather than something drawn inline, so that what appears under
    the prompt can be asserted on directly.
    """
    if not items:
        return []
    lines: list[str] = []
    for index, item in enumerate(items):
        name = str(item.get('name') or '')
        detail = str(item.get('description') or '')
        line = f'{"> " if index == selected else "  "}{name}'
        if detail:
            room = width - len(line) - 2
            if room > 8:
                line += '  ' + flat(detail, room)
        lines.append(style(line, 'reverse') if index == selected else style.dim(line))
    return lines


# ---------------------------------------------------------------------------
# HTTP, and the socket
# ---------------------------------------------------------------------------


class DaemonError(RuntimeError):
    """The daemon answered, and the answer was no."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


#: Close codes the daemon uses for "this will never work". Mirrors the
#: browser's list, because retrying any of them turns one dead session id into
#: an unbounded stream of the same error.
FATAL_CLOSE = frozenset({4400, 4401, 4404})


async def _detail(response: aiohttp.ClientResponse) -> str:
    try:
        body = await response.json(content_type=None)
    except Exception:  # noqa: BLE001 - an error page is not JSON
        body = None
    if isinstance(body, dict) and body.get('detail'):
        return str(body['detail'])
    return f'HTTP {response.status}'


class Daemon:
    """HTTP and a websocket to a daemon that is already running.

    The token goes in a header for HTTP and in the query string for the
    socket. That is not a stylistic choice: a websocket handshake from a
    browser cannot carry an `Authorization` header, so the daemon accepts the
    token there too, and this client uses the header where it can and the
    query where it must.
    """

    def __init__(self, host: str, port: int, *, token: str = '') -> None:
        self.host = host or '127.0.0.1'
        self.port = int(port or 8477)
        self.token = token
        self._session: aiohttp.ClientSession | None = None

    @property
    def base(self) -> str:
        host = self.host
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'  # an IPv6 literal is a URL, not a mess
        return f'http://{host}:{self.port}'

    @property
    def ws_base(self) -> str:
        return 'ws' + self.base[len('http') :]

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {'Authorization': f'Bearer {self.token}', 'X-Openmirror-Token': self.token}

    async def __aenter__(self) -> Daemon:
        # transport-exempt: this is the loopback daemon, not a model server.
        # The daemon is the thing that reaches providers, and it does that
        # through its own transport; a terminal client that routed its
        # connection to a model server through Tor would be a client routing
        # a request to itself, on this machine, through a network it never
        # needed to touch.
        self._session = aiohttp.ClientSession(headers=self._headers())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if self._session is None:
            raise RuntimeError('use `async with Daemon(...) as api`')
        try:
            async with self._session.request(method, f'{self.base}{path}', params=params, json=body) as response:
                if response.status >= 400:
                    raise DaemonError(response.status, await _detail(response))
                return await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DaemonError(0, f'{self.base} is not answering: {exc}') from exc

    async def health(self) -> dict[str, Any]:
        return await self.json('GET', '/healthz')

    async def create_session(self, **body: Any) -> dict[str, Any]:
        return await self.json('POST', '/api/sessions',
                               body={key: value for key, value in body.items() if value not in (None, '')})

    async def live_sessions(self) -> list[dict[str, Any]]:
        return list((await self.json('GET', '/api/sessions')).get('sessions') or [])

    async def stored_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        answer = await self.json('GET', '/api/sessions/stored', params={'limit': limit})
        return list(answer.get('sessions') or [])

    async def resume(self, session_id: str) -> dict[str, Any]:
        return dict((await self.json('POST', f'/api/sessions/{session_id}/resume')).get('session') or {})

    async def commands(self, session_id: str) -> list[dict[str, str]]:
        answer = await self.json('GET', f'/api/sessions/{session_id}/commands')
        return list(answer.get('commands') or [])

    async def files(self, session_id: str, query: str, limit: int = 40) -> list[dict[str, Any]]:
        answer = await self.json('GET', f'/api/sessions/{session_id}/files', params={'q': query, 'limit': limit})
        return list(answer.get('files') or [])

    async def agree_hook(self, session_id: str, command: str, allow: bool) -> None:
        await self.json('POST', f'/api/sessions/{session_id}/hooks/agree',
                        body={'command': command, 'allow': allow})

    async def attach(self, session_id: str, since: int) -> aiohttp.ClientWebSocketResponse:
        if self._session is None:
            raise RuntimeError('use `async with Daemon(...) as api`')
        params: dict[str, Any] = {'session': session_id, 'since': since}
        if self.token:
            params['token'] = self.token
        try:
            return await self._session.ws_connect(f'{self.ws_base}/ws/agent', params=params)
        except aiohttp.WSServerHandshakeError as exc:
            status = int(exc.status) if isinstance(exc.status, int) else 401
            if status in (401, 403):
                raise DaemonError(
                    status, 'this daemon wants a token: pass --token, or set OPENMIRROR_TOKEN'
                ) from exc
            raise DaemonError(status, f'the agent socket refused the connection: {exc}') from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise DaemonError(0, f'{self.ws_base} is not answering: {exc}') from exc


class Socket:
    """`async for event in sock.events()` — events, for as long as it lasts.

    The reconnect lives here rather than in the application because the one
    thing a reconnect has to get right is the sequence number, and the only way
    to be sure of that is for one object to own it. `since` is carried from
    the last event actually seen; nothing else in the client may move it, and
    the gap the daemon replays is exactly what keeps the transcript correct.
    """

    def __init__(self, api: Daemon, session_id: str) -> None:
        self.api = api
        self.session_id = session_id
        self.since = 0
        self.connected = False
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        #: Set on every successful attach. A one-shot run waits on this rather
        #: than on a length of time, so a daemon that went away between the
        #: health check and the socket is a short failure and not a hang.
        self.up = asyncio.Event()
        self._closing = asyncio.Event()

    def stop(self) -> None:
        self._closing.set()

    async def _pause(self, seconds: float) -> None:
        """Back off, but wake at once if the client is shutting down."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closing.wait(), timeout=seconds)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        attempt = 0
        while not self._closing.is_set():
            try:
                ws = await self.api.attach(self.session_id, self.since)
            except DaemonError as exc:
                # A link problem, not an `error` event. One that arrived while
                # a turn was running would end the turn; this one arrived
                # because there was never a turn. The reader is told the same
                # way it is told about a socket that dropped.
                if exc.status in (401, 403):
                    yield {'type': 'error', 'message': exc.detail, 'retryable': False, 'fatal': True}
                    return
                attempt += 1
                yield {'type': 'link', 'state': 'offline', 'attempt': attempt, 'reason': exc.detail}
                await self._pause(min(0.5 * 2 ** (attempt - 1), 20.0))
                continue

            attempt = 0
            self.connected = True
            self.up.set()
            self.ws = ws
            try:
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        event = json.loads(message.data)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    sequence = event.get('seq')
                    if isinstance(sequence, int) and sequence > self.since:
                        self.since = sequence
                    yield event
            except (aiohttp.ClientError, OSError):
                pass
            finally:
                self.connected = False
                self.up.clear()
                self.ws = None
                with contextlib.suppress(Exception):
                    await ws.close()

            if ws.close_code in FATAL_CLOSE:
                if ws.close_code == 4401:
                    message = 'the daemon refused the connection: its token is wrong.'
                else:
                    message = (
                        f'session {self.session_id} is gone — the daemon has been restarted. '
                        'Sessions live in memory and do not survive a restart; start a new one.'
                    )
                yield {'type': 'error', 'message': message, 'retryable': False, 'fatal': True}
                return

            attempt += 1
            # A synthetic event rather than a write from in here, so that
            # everything the reader sees arrives through one path.
            yield {'type': 'link', 'state': 'offline', 'attempt': attempt}
            await self._pause(min(0.5 * 2 ** (attempt - 1), 20.0))


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Options:
    """What `run` was asked for.

    The first ten fields are the documented signature of `run`; the rest are
    the extras a command line needs, and every one of them defaults to
    "not used", so a caller that knows only the documented ten gets a plain
    client.
    """

    host: str = '127.0.0.1'
    port: int = 8477
    root: str = ''
    model: str = ''
    provider: str = ''
    session_id: str = ''
    mode: str = ''
    toolset: list[str] | None = None
    yes: bool = False
    system: str = ''
    prompt: str = ''
    image: str = ''
    continue_: bool = False
    token: str = ''
    effort: str = ''
    title: str = ''
    colour: bool | None = None
    width: int | None = None


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------

HINT = 'tab completes · ctrl-c interrupts · ctrl-d leaves · @file · /command'
#: Narrow terminals get the short one rather than a hint that wraps and
#: scrolls the line away from under the cursor.
HINT_NARROW = 'tab · ctrl-c · ctrl-d · @file · /command'


class App:
    """The REPL: a socket, a keyboard, and a renderer between them.

    Everything stateful about a *conversation* lives here — which tool call is
    waiting for an answer, what the status line says, whether a turn is
    running — and everything stateful about the *text* lives in `Renderer`.
    That split is what lets a transcript be tested without a session.

    The concurrency is one asyncio loop, not a thread per socket and a thread
    per stream. Reading a terminal blocks until somebody presses a key, so the
    keyboard gets exactly one thread, and it does nothing but hand `Key`
    objects to the loop through a queue. Everything else — the socket, the
    REST calls for `@` completions, the backoff — is a coroutine on the loop
    where cancelling it is ordinary.
    """

    def __init__(
        self,
        api: Daemon,
        options: Options,
        terminal: Terminal,
        *,
        renderer: Renderer | None = None,
    ) -> None:
        self.api = api
        self.options = options
        self.term = terminal
        self.renderer = renderer or Renderer(style=terminal.style, width=terminal.width)
        self.line = Line()

        self.session_id = ''
        self.commands: list[dict[str, str]] = []
        self.busy = False
        self.remembered: set[str] = set()
        self.exit_code = 0
        #: How many turns have finished. One-shot mode waits for this to move
        #: rather than for a fixed delay, so a slow answer is never cut off.
        self.turns_done = 0

        self._state = 'input'
        self._call: dict[str, Any] = {}
        self._question: dict[str, Any] = {}
        self._hook: dict[str, Any] = {}
        self._pending: list[dict[str, str]] = []
        self._socket = Socket(api, '')
        self._held: deque[dict[str, Any]] = deque()
        self._wake = asyncio.Event()
        self._choices: list[dict[str, str]] = []
        self._at = 0
        self._menu_open = False
        self._popup: list[str] = []
        self._prompt_line = ''
        self._prompt_drawn = False
        self._prompt_text = ''
        self._cursor_col = 0
        self._debounce: asyncio.TimerHandle | None = None
        self._stopping = False
        self._interrupted = False
        self._fatal = False
        self._sent = False
        self._sent_prefix = False
        #: False in one-shot mode, where a prompt and a cursor are noise at
        #: the end of an answer somebody is about to pipe.
        self.show_input = True
        self._done = asyncio.Event()
        #: Set whenever a turn finishes or fails, so `--prompt` can wait for
        #: the work rather than for a length of time.
        self._turned = asyncio.Event()
        #: Notices raised while the agent is mid-sentence, held until the
        #: stream ends. Printing one in the middle of a paragraph puts it in
        #: the middle of a sentence, and the reader has to work out which of
        #: the two streams they are looking at.
        self._held_notes: list[tuple[str, bool]] = []

    # -- output ------------------------------------------------------------

    def _out(self, text: str, *, err: bool = False) -> None:
        """Write, having first taken the prompt off the screen.

        The prompt and the transcript share one terminal, so anything printed
        under an undrawn prompt overwrites it. Erasing first and redrawing
        afterwards is the whole of prompt redisplay.
        """
        if not text:
            return
        self.erase_prompt()
        self.term.write(text, err=err)

    def erase_prompt(self) -> None:
        if self._prompt_drawn and self.term.interactive:
            self.term.write('\r\x1b[2K')
            self._prompt_drawn = False

    def paint_prompt(self) -> None:
        self.erase_prompt()
        if not self.term.interactive:
            # Typed at, written somewhere else. The prompt is still worth
            # showing but there is no cursor to move and no line to erase, so
            # it is written once and left.
            self._prompt_drawn = True
            self.term.write(self._prompt_line + '\n')
            return
        for line in self._popup:
            self.term.write(line + '\n')
        self.term.write(self._prompt_line + '\x1b[K')
        # Walk to the cursor by going out to the edge and back rather than by
        # counting columns: one write, and right on the terminals that lie
        # about their width.
        self.term.write(f'\r{" " * min(self._cursor_col, self.term.width - 1)}\r')
        self._prompt_drawn = True

    def show_prompt(self) -> None:
        """Draw the input line, scrolling it so the cursor stays visible."""
        if self._state != 'input':
            self.erase_prompt()
            return
        visible, offset = self.line.preview(self.term.width - 2)
        self._cursor_col = max(0, self.line.cursor - offset) + 2
        hint = HINT if self.term.width >= 80 else HINT_NARROW
        line = self.term.style.cyan('> ') + visible + (
            self.term.style.dim('  ' + hint) if not visible else ''
        )
        # Both the line and the menu count. The menu appearing over an
        # unchanged line is the whole point of tab completion, and comparing
        # the line alone means the menu never appears at all.
        signature = (line, '\n'.join(self._popup))
        if signature == self._prompt_text and self._prompt_drawn:
            return
        self._prompt_text = signature
        self._prompt_line = line
        self.paint_prompt()

    def refresh_prompt(self) -> None:
        """Redraw the prompt only when it belongs on the screen.

        While the agent is mid-sentence there is nothing to type into, and a
        prompt blinking at the bottom of a streaming answer is the single most
        distracting thing a terminal agent can do.
        """
        if self._state != 'input' or not self.show_input or self.renderer.streaming:
            self.erase_prompt()
            return
        self.show_prompt()

    # -- sending -----------------------------------------------------------

    def send(self, command: dict[str, Any]) -> None:
        """Queue a command for the socket.

        The queue is drained on every (re)connection, so a command issued
        while the socket was away is held rather than dropped. Losing what
        somebody typed, silently, is the worst failure a client like this has.
        """
        self._held.append(command)
        self._wake.set()

    async def _write_socket(self) -> None:
        while not self._stopping:
            if not self._held:
                await self._wake.wait()
                self._wake.clear()
                continue
            ws = self._socket.ws
            if ws is None or not self._socket.connected:
                await asyncio.sleep(0.2)
                continue
            command = self._held.popleft()
            try:
                await ws.send_json(command)
            except Exception:  # noqa: BLE001 - a dropped socket is not a crash
                self._held.appendleft(command)
                self._wake.set()
                await asyncio.sleep(0.2)

    # -- events ------------------------------------------------------------

    def on_event(self, event: dict[str, Any]) -> None:
        """Render one event, then let it change the state."""
        kind = str(event.get('type') or '')
        if kind == 'link':
            self._on_link(event)
            return
        self._out(self.renderer.feed(event), err=kind in STDERR_EVENTS)
        handler = getattr(self, f'_on_{kind.replace(".", "_")}', None)
        if handler is not None:
            handler(event)
        if event.get('fatal'):
            self._fatal = True
            self.exit_code = 1
            self._done.set()
        self.flush_notes()
        self.refresh_prompt()

    def _on_link(self, event: dict[str, Any]) -> None:
        attempt = event.get('attempt', 1)
        reason = event.get('reason')
        if reason:
            self._out(self.term.style.yellow(
                f'· cannot reach the agent socket: {flat(str(reason), 120)}\n'
                f'· retrying (attempt {attempt}); the session keeps running either way\n'))
            return
        self._out(self.term.style.yellow(
            f'· connection lost — reconnecting (attempt {attempt}); the session carries on without you\n'))

    def _on_session_started(self, event: dict[str, Any]) -> None:
        # The banner waits for this rather than being printed on the way in,
        # because the model and the approval mode are on this event and not on
        # the answer to the health check. `session.started` is always the first
        # thing a fresh socket sends, so it lands before anything else is
        # rendered in either mode.
        self._out(banner(self.term, self), err=not self.show_input)

    def _on_turn_started(self, event: dict[str, Any]) -> None:
        self.busy = True

    def _on_turn_completed(self, event: dict[str, Any]) -> None:
        self.busy = False
        self._interrupted = False
        self.turns_done += 1
        self._turned.set()

    def _on_error(self, event: dict[str, Any]) -> None:
        self.busy = False
        self.turns_done += 1
        self._turned.set()

    def _on_session_ended(self, event: dict[str, Any]) -> None:
        self.busy = False
        self._out(self.term.style.dim('· the session ended; `openmirror chat --continue` will find another\n'))

    def _on_tool_proposed(self, event: dict[str, Any]) -> None:
        """Decide whether to ask, and ask on stderr.

        The event is sent for every call, allowed or not, so this is the only
        place the client can decide. `remembered` is scoped the way the daemon
        scopes `remember` — one tool, one set of arguments — because a
        session-wide "yes to shell" is the thing the protocol is careful not
        to become.
        """
        if not event.get('needs_approval'):
            return
        call = dict(event.get('call') or {})
        identifier = str(call.get('id') or '')
        name = str(call.get('name') or 'this tool')
        if name == 'ask_user':
            # The daemon routes that one through `question.asked` instead, and
            # a second prompt about the same thing is a stall.
            return
        if self.options.yes:
            self.send({'type': 'tool.approve', 'call_id': identifier, 'remember': False})
            return
        if call_key(call) in self.remembered:
            self.send({'type': 'tool.approve', 'call_id': identifier, 'remember': True})
            return
        self._state = 'approve'
        self._call = call
        self.erase_prompt()
        self._out(approval_prompt(name), err=True)

    def _on_question_asked(self, event: dict[str, Any]) -> None:
        self._state = 'question'
        self._question = dict(event)
        self.erase_prompt()
        self._out(self.term.style.dim('  answer: '), err=True)

    def _on_hook_approval(self, event: dict[str, Any]) -> None:
        self._state = 'hook'
        self._hook = dict(event.get('hook') or {})
        self.erase_prompt()
        self._out(self.term.style.yellow('  run this hook for the rest of this session? [y/N] '), err=True)

    # -- status ------------------------------------------------------------

    def status_line(self) -> str:
        """cwd · model · approval — the three facts that change a turn's shape."""
        info = self.renderer.info or {}
        bits = [
            str(info.get('cwd') or self.options.root or '.'),
            str(info.get('model') or self.options.model or 'the daemon default'),
        ]
        if info.get('policy') or self.options.mode:
            bits.append(f'approval: {info.get("policy") or self.options.mode}')
        if info.get('effort'):
            bits.append(f'thinking: {info["effort"]}')
        return ' · '.join(bits)

    def status(self) -> str:
        info = self.renderer.info or {}
        model = info.get('model') or self.options.model or "the daemon's default"
        approval = info.get('policy') or self.options.mode or 'ask'
        effort = info.get('effort') or "the model's own default"
        return '\n'.join([
            f'session   {self.session_id or "none"}',
            f'folder    {info.get("cwd") or self.options.root or "."}',
            f'model     {model}',
            f'approval  {approval}',
            f'thinking  {effort}',
            f'tools     {len(info.get("tools") or [])}',
            f'link      {"live" if self._socket.connected else "offline"}',
            f'turn      {"running" if self.busy else "idle"}',
        ])

    # -- interrupting ------------------------------------------------------

    def interrupt(self) -> None:
        """Once, interrupt the turn. Twice, leave.

        The double reading is only safe because the first press cannot be a
        mistake, and a key that sometimes stops the agent and sometimes throws
        away the line being typed is worse than either behaviour alone.
        """
        if self.busy:
            if not self._interrupted:
                self._interrupted = True
                self._close_menu()
                self.send({'type': 'turn.interrupt'})
                self._defer('interrupting — ctrl-c again to leave')
            else:
                self._out(self.term.style.yellow('· leaving\n'))
                self.stop(130)
            return
        self._interrupted = False
        self._close_menu()
        if self.line.text:
            self._out(self.term.style.yellow('· ctrl-c again to leave\n'))
            self.line.clear()
            self.refresh_prompt()
            return
        self.stop(130)

    def _close_menu(self) -> None:
        """Dismissing the menu is not the same as dismissing the input, but
        ctrl-c has to leave the menu either way or it stays on screen
        pointing at something nobody is typing into."""
        self._menu_open = False
        self._popup = []

    def stop(self, code: int = 0) -> None:
        """Leave.

        The session is *not* closed: the daemon keeps it, which is what makes
        detaching cheap and what makes `--continue` work afterwards.
        """
        self._stopping = True
        if code or not self.exit_code:
            self.exit_code = code
        self._socket.stop()
        self._done.set()

    # -- the input line ----------------------------------------------------

    def on_key(self, key: Key) -> None:
        if key.name == 'interrupt':
            self.interrupt()
            return
        if key.name == 'eof':
            # At an idle prompt, end of input means "leave". At a question it
            # means "nobody is there", which is a denial and not an exit —
            # falling through to the handler is what makes that distinction.
            if self._state == 'input':
                self.stop(0)
            else:
                self.answer_approval(key)
            return
        if key.name == 'clear':
            self.term.write('\x1b[2J\x1b[H')
            self._prompt_drawn = False
            self._close_menu()
            self.refresh_prompt()
            return
        if self._state == 'approve':
            self.answer_approval(key)
            return
        if self._state == 'question':
            self.answer_question(key)
            return
        if self._state == 'hook':
            self.answer_hook(key)
            return
        self.on_input_key(key)

    def on_input_key(self, key: Key) -> None:
        name = key.name
        if name == 'enter':
            if key.text:
                # The line-at-a-time fallback, and what a paste looks like:
                # the whole line arrives at once.
                self.line.set(key.text)
            self.submit()
            return
        if name == 'char':
            self.line.insert(key.text)
            self._mention_changed()
            self.refresh_prompt()
            return
        edits: dict[str, Any] = {
            'backspace': self.line.backspace,
            'delete': self.line.delete,
            'home': self.line.home,
            'end': self.line.end,
            'kill-line': self.line.kill_line,
            'kill-word': self.line.kill_word,
        }
        if name in edits:
            edits[name]()
            if name in ('backspace', 'delete'):
                self._mention_changed()
            self.refresh_prompt()
            return
        if name in ('left', 'right'):
            self.line.move(-1 if name == 'left' else 1)
            self.refresh_prompt()
            return
        if name == 'up':
            if self._menu_open:
                self._move_menu(-1)
            else:
                self.line.older()
            self.refresh_prompt()
            return
        if name == 'down':
            if self._menu_open:
                self._move_menu(1)
            else:
                self.line.newer()
            self.refresh_prompt()
            return
        if name == 'tab':
            self.complete()
            return
        if name == 'escape':
            self._menu_open = False
            self._popup = []
            self.refresh_prompt()

    def submit(self) -> None:
        text = self.line.text.strip()
        if not text:
            self.refresh_prompt()
            return
        self.line.remember(text)
        self.line.clear()
        self._menu_open = False
        self._popup = []
        self.erase_prompt()
        self.local(text)
        self.refresh_prompt()

    def send_turn(self, text: str, attachments: list[dict[str, str]] | None = None) -> None:
        """Submit, echoing locally so the question appears the instant it is
        typed rather than when the daemon acknowledges it."""
        if not text:
            return
        prompt = text
        if self.options.system and not self._sent_prefix:
            # The daemon has no system-prompt field, so the extra instruction
            # rides along with the first turn rather than being silently
            # dropped or silently applied to every one.
            prompt = f'{self.options.system}\n\n{prompt}'
            self._sent_prefix = True
        self._out(self.renderer.echo(prompt))
        self.renderer.echoed = prompt
        self._sent = True
        command: dict[str, Any] = {'type': 'turn.submit', 'text': prompt}
        if attachments:
            command['attachments'] = attachments
        self.send(command)

    # -- local commands ----------------------------------------------------

    def local(self, text: str) -> None:
        """Answer a line here, or take it apart before it is sent.

        Everything the client does to a line before the daemon sees it: the
        local slash commands, and the `!path` that a terminal drag leaves at
        the end of a prompt.
        """
        slash = parse_slash(text)
        if slash is not None and slash.local:
            self.run_local(slash)
            return
        try:
            body, attachments = split_image_tokens(text)
        except AttachmentError as exc:
            self._out(self.term.style.red(f'! {exc}\n'), err=True)
            return
        self._pending = attachments + self._pending
        if body:
            self.send_turn(body, self._pending)
        self._pending = []

    def run_local(self, slash: Slash) -> None:
        name, args = slash.name, slash.args
        if name in ('exit', 'quit'):
            self.stop(0)
        elif name == 'help':
            self._out(self.help_text())
        elif name == 'status':
            self._out(self.term.style.dim(self.status() + '\n'))
        elif name == 'clear-screen':
            self.term.write('\x1b[2J\x1b[H')
            self._prompt_drawn = False
        elif name == 'commands':
            self._out(self.command_list())
        elif name == 'model':
            if args:
                self._note('the model is fixed when a session is created; start another with --model')
            else:
                info = self.renderer.info or {}
                self._out(self.term.style.dim(
                    f'model: {info.get("model") or self.options.model or "the daemon default"}\n'))
        elif name == 'mode':
            if args:
                self.send({'type': 'policy.set', 'mode': args})
                self._note(f'asking for approval mode {args!r}')
            else:
                approval = self.renderer.mode or self.options.mode or 'ask'
                self._out(self.term.style.dim(f'approval: {approval}\n'))

    def help_text(self) -> str:
        lines = [self.term.style.dim('answered here:')]
        lines.append(self.term.style.dim('  /help  /status  /model  /mode  /commands  /clear-screen  /exit'))
        lines.append(self.term.style.dim('sent to the daemon:'))
        if self.commands:
            for entry in self.commands[:12]:
                name = str(entry.get('name') or '')
                lines.append(self.term.style.dim(f'  /{name:<16} {flat(entry.get("description", ""), 60)}'))
        else:
            lines.append(self.term.style.dim('  (the daemon has not reported any yet)'))
        lines.append(self.term.style.dim('keys:'))
        lines.append(self.term.style.dim(
            '  tab completes · ctrl-c interrupts, twice to leave · ctrl-d leaves · ctrl-l redraws'
        ))
        lines.append(self.term.style.dim('  @path mentions a file · a trailing !path attaches an image'))
        return '\n'.join(lines) + '\n'

    def command_list(self) -> str:
        if not self.commands:
            return self.term.style.dim('the daemon reported no commands for this session\n')
        out = [self.term.style.dim(f'{len(self.commands)} command(s) and skill(s):')]
        for entry in self.commands:
            name = str(entry.get('name') or '')
            out.append(self.term.style.dim(f'  /{name:<18} {flat(entry.get("description", ""), 60)}'))
        return '\n'.join(out) + '\n'

    def _note(self, text: str, *, err: bool = False) -> None:
        """One line about what the client is doing, rather than what the agent said.

        On stderr whenever there is no REPL to print it under, so that
        `openmirror chat --prompt x > answer.txt` leaves a file with the
        answer in it and nothing else.
        """
        self._out(self.term.style.dim(f'· {text}\n'), err=err or not self.show_input)

    def _defer(self, text: str, *, err: bool = False) -> None:
        """As `_note`, but waiting for a sentence to finish first."""
        self._held_notes.append((text, err))
        self.flush_notes()

    def flush_notes(self) -> None:
        if self.renderer.streaming or not self._held_notes:
            return
        held, self._held_notes = self._held_notes, []
        for text, err in held:
            self._note(text, err=err)

    # -- answers -----------------------------------------------------------

    def answer_approval(self, key: Key) -> None:
        """Answer one approval. Re-asks on nonsense, refuses on EOF.

        The re-ask matters more than it looks: silently reading `what?` as a
        refusal would turn a typo into a decision about somebody's filesystem,
        and silently reading it as a yes would be worse.
        """
        if key.name not in ('char', 'enter', 'eof'):
            if key.name == 'interrupt':
                self._state = 'input'
                self._call = {}
                self._out(self.term.style.yellow('· interrupted — nothing was allowed\n'))
                self.send({'type': 'turn.interrupt'})
                self.refresh_prompt()
            return
        answer = parse_approval(None if key.name == 'eof' else key.text)
        if not answer.valid:
            if answer.abort:
                self._state = 'input'
                identifier = str(self._call.get('id') or '')
                self._call = {}
                self._out('\n', err=True)
                self._out(self.term.style.red('! nobody answered — denying and stopping the turn\n'), err=True)
                self.send({'type': 'tool.deny', 'call_id': identifier, 'reason': 'no answer given'})
                self.send({'type': 'turn.interrupt'})
                self.refresh_prompt()
                return
            self._out(self.term.style.dim('  y, n, or a\n'), err=True)
            self._out(approval_prompt(str(self._call.get('name') or 'this tool')), err=True)
            return
        identifier = str(self._call.get('id') or '')
        call, self._call = self._call, {}
        self._state = 'input'
        self._out('\n', err=True)
        if answer.allow:
            if answer.remember:
                self.remembered.add(call_key(call))
                self._out(self.term.style.dim('· allowed, and remembered for the rest of this session\n'))
            self.send({'type': 'tool.approve', 'call_id': identifier, 'remember': answer.remember})
        else:
            self._out(self.term.style.dim('· denied\n'))
            self.send({'type': 'tool.deny', 'call_id': identifier, 'reason': 'declined from the terminal'})
        self.refresh_prompt()

    def answer_question(self, key: Key) -> None:
        text = key.text if key.name in ('char', 'enter') else ''
        options = self._question.get('options') or []
        if text.strip().isdigit() and options:
            index = int(text.strip()) - 1
            if 0 <= index < len(options):
                text = str(options[index])
        question_id = str(self._question.get('question_id') or '')
        self._state = 'input'
        self._question = {}
        self._out('\n', err=True)
        self.send({'type': 'question.answer', 'question_id': question_id, 'answer': text})
        self.refresh_prompt()

    def answer_hook(self, key: Key) -> None:
        """Agree or refuse one hook command, for the rest of the session.

        The only REST call the REPL makes mid-turn, because the websocket has
        no command for it. Deliberately the only one: a client that started
        doing things over HTTP would have to start auditing everything else
        the daemon exposes.
        """
        text = key.text if key.name in ('char', 'enter') else ''
        decision = parse_approval(text)
        hook, self._hook = self._hook, {}
        self._state = 'input'
        self._out('\n', err=True)
        command = str(hook.get('command') or '')
        if command:
            self.send_hook_agreement(command, decision.allow)
        self.refresh_prompt()

    def send_hook_agreement(self, command: str, allow: bool) -> None:
        asyncio.ensure_future(self._agree_hook(command, allow))

    async def _agree_hook(self, command: str, allow: bool) -> None:
        try:
            await self.api.agree_hook(self.session_id, command, allow)
        except DaemonError as exc:
            self._out(self.term.style.red(f'! {exc.detail}\n'), err=True)

    # -- completion --------------------------------------------------------

    def _mention_changed(self) -> None:
        """Ask for files after an `@`, once the typing settles.

        Debounced because that endpoint walks the tree: it is quick enough
        that a keystroke of delay is invisible, and the walk is not free.
        """
        mention = mention_at(self.line.text, self.line.cursor)
        if mention is None:
            self._menu_open = False
            self._popup = []
            return
        if self._debounce is not None:
            self._debounce.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # outside a loop — a test, or a synchronous caller
            return
        self._debounce = loop.call_later(0.12, self._schedule_files, mention[1])

    def _schedule_files(self, query: str) -> None:
        asyncio.ensure_future(self._load_files(query))

    async def _load_files(self, query: str) -> None:
        """`GET /{id}/files`, then narrowed again on this side.

        The daemon matches anywhere in the path and somebody typing `@conf`
        wants the config files, so the client ranks its own answer again.
        """
        try:
            hits = await self.api.files(self.session_id, query)
        except DaemonError:
            return
        self.show_choices(
            [{'name': path, 'description': '', 'kind': 'file'} for path in filter_files(hits, query, limit=8)]
        )

    def show_choices(self, choices: list[dict[str, str]]) -> None:
        self._choices = choices
        self._at = 0
        self._menu_open = bool(choices)
        self._popup = popup_lines(choices, 0, self.term.style, self.term.width)
        self.refresh_prompt()

    def _move_menu(self, delta: int) -> None:
        if not self._choices:
            return
        self._at = max(0, min(len(self._choices) - 1, self._at + delta))
        self._popup = popup_lines(self._choices[self._at :], 0, self.term.style, self.term.width)
        self._menu_open = True

    def complete(self) -> None:
        """Tab: offer, or accept.

        A menu that only appeared after a second Tab is one nobody discovers,
        so the first Tab either takes the single match or shows the choices.
        """
        mention = mention_at(self.line.text, self.line.cursor)
        if mention is not None:
            if not self._choices:
                self._load_files_now(mention[1])
                return
            if len(self._choices) > 1:
                self._move_menu(1)
                self.refresh_prompt()
                return
            self._accept(self._choices[0]['name'])
            return
        text = self.line.text.strip()
        if text.startswith('/') and ' ' not in text:
            self.show_choices(slash_completions(text, self.commands, limit=10))
            return
        for candidate in reversed(self.line.history):
            if candidate.startswith(text):
                self.line.set(candidate)
                self.refresh_prompt()
                return
        self.refresh_prompt()

    def _load_files_now(self, query: str) -> None:
        asyncio.ensure_future(self._load_files(query))

    def _accept(self, value: str) -> None:
        self.line.set(*apply_completion(self.line.text, self.line.cursor, value))
        self._menu_open = False
        self._popup = []
        self.refresh_prompt()

    # -- the loop ----------------------------------------------------------

    def attach(self) -> Socket:
        """Build the socket now rather than when `serve` is scheduled.

        `serve` is a coroutine, so it does not run until the loop gets to it —
        and a caller waiting for the connection would otherwise be waiting on
        the placeholder that `serve` is about to replace. Idempotent, because
        both call it and only the first may make a socket.
        """
        if self._socket.session_id != self.session_id:
            self._socket = Socket(self.api, self.session_id)
        return self._socket

    async def serve(self) -> None:
        """Attach to the session and stay attached until it or we are done."""
        self.attach()
        reader = asyncio.create_task(self._read_socket(), name='tui-reader')
        writer = asyncio.create_task(self._write_socket(), name='tui-writer')
        keepalive = asyncio.create_task(self._keepalive(), name='tui-ping')
        try:
            await reader
        finally:
            if self._fatal:
                self.exit_code = 1
            self._stopping = True
            self._socket.stop()
            self._wake.set()
            self._done.set()
            for task in (reader, writer, keepalive):
                task.cancel()
            await asyncio.gather(reader, writer, keepalive, return_exceptions=True)

    async def _read_socket(self) -> None:
        async for event in self._socket.events():
            self.on_event(event)
            if self._fatal or self._stopping:
                break

    async def _keepalive(self) -> None:
        """A ping every half minute.

        The daemon is a laptop process and so is this. A NAT or a sleeping
        radio drops an idle socket without saying so, and the symptom is a
        client that has quietly stopped receiving anything.
        """
        while True:
            await asyncio.sleep(30)
            if self._socket.connected and self._socket.ws is not None:
                with contextlib.suppress(Exception):
                    await self._socket.ws.send_json({'type': 'ping'})


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

#: Long enough that a slow local model is never cut off, short enough that a
#: daemon which never answers does not hang the caller for ever.
TURN_TIMEOUT = 1800.0

#: How long a scripted run waits for the socket to open at all. The health
#: check has already answered by then, so this is about a daemon that went away
#: between the two rather than about a slow one.
CONNECT_TIMEOUT = 20.0


def build_parser() -> argparse.ArgumentParser:
    """The argument surface, for `openmirror chat --help`.

    A parser rather than hand-read `sys.argv`, because a terminal client is
    the one part of this project a person uses directly and `--help` is most
    of its documentation. `cli.py` owns the subcommand; this owns everything
    after it, and nothing here reaches for an agent.
    """
    parser = argparse.ArgumentParser(
        prog='openmirror chat',
        description='Talk to a running openmirror daemon from the terminal.',
        epilog='With a terminal and no prompt this is an interactive REPL. With stdin piped in it '
               'reads the prompt from there, runs one turn and exits.',
    )
    parser.add_argument('--host', default=os.getenv('OPENMIRROR_HOST', '127.0.0.1'),
                        help='daemon host (default: %(default)s)')
    parser.add_argument('--port', type=int, default=int(os.getenv('OPENMIRROR_PORT', '8477')),
                        help='daemon port (default: %(default)s)')
    parser.add_argument('--root', '-C', default='', metavar='PATH',
                        help='folder to work in (default: this one)')
    parser.add_argument('--model', '-m', default='', help="model to use (default: the daemon's own choice)")
    parser.add_argument('--provider', dest='provider', default='', help='provider to use')
    parser.add_argument('--session', '-s', default='', metavar='ID', help='attach to this session id')
    parser.add_argument('--continue', '-c', dest='continue_', action='store_true',
                        help='attach to the most recent conversation')
    parser.add_argument('--mode', default='',
                        help='approval mode: ask, auto-read, full-auto, unrestricted')
    parser.add_argument('--effort', default='', help='how hard to think: off, low, medium, high, xhigh, max')
    parser.add_argument('--tool', dest='toolset', action='append', default=[], metavar='NAME',
                        help='restrict the session to this toolset or tool (repeatable; default: all of them)')
    parser.add_argument('--yes', '-y', action='store_true', help='allow every tool call without asking')
    parser.add_argument('--system', default='', metavar='TEXT', help='extra instruction for the first turn')
    parser.add_argument('--prompt', default='', metavar='TEXT',
                        help='send this and exit (the usual way to script this)')
    parser.add_argument('--image', default='', metavar='PATH',
                        help='attach this image to the prompt')
    parser.add_argument('--token', default=os.getenv('OPENMIRROR_TOKEN', ''),
                        help='token for a locked daemon (default: $OPENMIRROR_TOKEN)')
    parser.add_argument('--title', default='', help='title for a new session')
    parser.add_argument('--no-colour', '--no-color', dest='colour', action='store_const', const=False,
                        default=None, help='never emit colour (also honoured: NO_COLOR, TERM=dumb, a pipe)')
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    """An `argparse` namespace as `Options`, with the folder made absolute.

    Absolute because it is about to be sent to a daemon that may well be on
    another machine, where a relative path means whatever *it* thinks it does.
    """
    return Options(
        host=args.host,
        port=args.port,
        root=str(Path(args.root).expanduser().resolve()) if args.root else str(Path.cwd()),
        model=args.model,
        provider=args.provider,
        session_id=args.session,
        mode=args.mode,
        toolset=list(args.toolset) or None,
        yes=args.yes,
        system=args.system,
        prompt=args.prompt,
        image=args.image,
        continue_=args.continue_,
        token=args.token,
        effort=args.effort,
        title=args.title,
        colour=args.colour,
    )


def _create_body(options: Options) -> dict[str, Any]:
    """Only what was asked for, so the daemon keeps its own defaults for the rest."""
    body: dict[str, Any] = {'root': options.root, 'title': options.title}
    for key, value in (
        ('model', options.model),
        ('provider', options.provider),
        ('mode', options.mode),
        ('effort', options.effort),
    ):
        if value:
            body[key] = value
    if options.toolset:
        body['tools'] = list(options.toolset)
    return body


async def open_session(api: Daemon, options: Options, note: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Attach to a session that exists, or make one.

    `--session` prefers a *live* session, because resuming one that is already
    running would fork the conversation into two readers. `--continue` takes
    the newest thing on disk, which after a restart is the only kind there is.

    A `--continue` with nothing on disk starts a new session and says so. It
    is a first run, not a failure, and the alternative — refusing — would
    make the flag useless on a fresh install.
    """
    wanted = options.session_id
    if not wanted and options.continue_:
        stored = await api.stored_sessions(limit=1)
        wanted = str(stored[0].get('id') or '') if stored else ''
        if not wanted and note is not None:
            note('no conversation on disk yet — starting a new one')
    if not wanted:
        return await api.create_session(**_create_body(options))
    live = {str(item.get('id')) for item in await api.live_sessions()}
    if wanted in live:
        return {'id': wanted}
    try:
        return await api.resume(wanted)
    except DaemonError as exc:
        if exc.status == 404:
            # The daemon's own wording is best when it has one; this is the
            # fallback for a proxy that answered 404 with a blank page.
            raise DaemonError(404, f'no conversation called {wanted!r}') from exc
        raise


def banner(terminal: Terminal, app: App) -> str:
    lines = [terminal.style.dim(f'openmirror · session {app.session_id}')]
    lines.append(terminal.style.dim(app.status_line()))
    if app.options.yes:
        lines.append(terminal.style.yellow('allowing every tool call without asking (--yes)'))
    lines.append(terminal.style.dim('/help for what you can type · ctrl-d to leave'))
    return '\n'.join(lines) + '\n'


async def _interactive(api: Daemon, options: Options, terminal: Terminal) -> int:
    """The REPL: raw keys in, a socket out, until ctrl-d or an error."""
    app = App(api, options, terminal)
    # Set before anything can print: with a prompt there is no REPL, so the
    # status lines around the answer belong on stderr rather than in it.
    app.show_input = not options.prompt
    try:
        session = await open_session(api, options, note=app._note)
    except DaemonError as exc:
        terminal.write(terminal.style.red(f'! {exc.detail}\n'), err=True)
        return 1
    app.session_id = str(session.get('id') or '')
    if not app.session_id:
        terminal.write(terminal.style.red('! the daemon did not hand back a session id\n'), err=True)
        return 1
    with contextlib.suppress(DaemonError):
        # Slash completion is worth a round trip at start-up: the daemon knows
        # which skills this project has, and a client that guessed would be
        # offering commands that do not run.
        app.commands = await api.commands(app.session_id)
    if options.image:
        try:
            app._pending = attachments_from_paths([options.image])
        except AttachmentError as exc:
            terminal.write(terminal.style.red(f'! {exc}\n'), err=True)
            return 1

    if options.prompt:
        return await _one_turn(app, options.prompt)

    with Raw() as raw:
        if not raw.active:
            # No cbreak available: still a REPL, just one that reads whole
            # lines. `Keys` hides the difference from the main loop.
            terminal.write(terminal.style.dim('no terminal to read from; reading whole lines\n'))
        keys = Keys(raw=raw)
        app.attach()
        typing = asyncio.create_task(_pump_keys(app, keys), name='tui-keys')
        serving = asyncio.create_task(app.serve(), name='tui-socket')
        leaving = asyncio.create_task(app._done.wait(), name='tui-leave')
        try:
            # Any one of the three finishing is the end: the socket gave up,
            # the keyboard closed, or somebody asked to leave.
            _finished, pending = await asyncio.wait(
                [typing, serving, leaving], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            app.stop(app.exit_code)
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
    return app.exit_code


async def _pump_keys(app: App, keys: Keys) -> None:
    """The keyboard, read on a thread and handed to the loop.

    `os.read` on a terminal blocks until somebody presses a key, so it cannot
    run on the loop — doing so would stop the answer from streaming while the
    cursor sat motionless. One thread does nothing but hand `Key` objects
    across, which is the only thread in this program.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Key | None] = asyncio.Queue()

    def pump() -> None:
        try:
            for key in keys:
                loop.call_soon_threadsafe(queue.put_nowait, key)
        except Exception:  # noqa: BLE001 - a dead keyboard is not a reason to crash
            pass
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=pump, name='tui-stdin', daemon=True).start()
    while True:
        key = await queue.get()
        if key is None or app._stopping:
            return
        app.on_key(key)


async def _one_turn(app: App, prompt: str) -> int:
    """One prompt, then out. Used by `--prompt` and by piped stdin.

    Waits for a `turn.completed` rather than for a fixed time, so a slow model
    is never cut off mid-sentence and a daemon that is merely slow does not
    look like one that has hung.
    """
    prompt = prompt.strip()
    if not prompt:
        return 0
    body = prompt
    try:
        body, more = split_image_tokens(prompt)
    except AttachmentError as exc:
        app.term.write(app.term.style.red(f'! {exc}\n'), err=True)
        return 1
    attachments = app._pending + more
    app._pending = []
    app.show_input = False

    before = app.turns_done
    socket = app.attach()
    serving = asyncio.create_task(app.serve(), name='tui-socket')
    app.send_turn(body, attachments)
    try:
        async with asyncio.timeout(TURN_TIMEOUT):
            while app.turns_done == before and not app._fatal:
                # The health check already answered a moment ago, so a socket
                # that will not open means the daemon went away in between.
                # Bounded, because a script that waits half an hour for a
                # socket that is never coming back is worse than one that says
                # so and exits.
                if not socket.connected:
                    async with asyncio.timeout(CONNECT_TIMEOUT):
                        await socket.up.wait()
                await app._turned.wait()
                app._turned.clear()
    except TimeoutError:
        if socket.connected:
            app.term.write(app.term.style.red('! the turn did not finish in time\n'), err=True)
        else:
            app.term.write(app.term.style.red('! the agent socket never opened\n'), err=True)
        app.exit_code = 1
    finally:
        app.stop(app.exit_code)
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
    return app.exit_code


async def _serve(options: Options, terminal: Terminal) -> int:
    """One entry for both modes, so the choice of mode is made in one place."""
    token = options.token or os.getenv('OPENMIRROR_TOKEN', '')
    async with Daemon(options.host, options.port, token=token) as api:
        try:
            await api.health()
        except DaemonError as exc:
            terminal.write(terminal.style.red(f'! {exc.detail}\n'), err=True)
            return 1
        if _is_tty(sys.stdin):
            return await _interactive(api, options, terminal)
        return await _piped(api, options, terminal)


async def _piped(api: Daemon, options: Options, terminal: Terminal) -> int:
    """stdin is not a terminal: it *is* the prompt. One turn, then exit 0.

    This is what makes the client scriptable — `git diff | openmirror chat
    'review this'` — and it is also what makes it possible to exercise the
    whole thing from a shell without a pty.
    """
    if not options.prompt:
        try:
            options.prompt = sys.stdin.read()
        except (OSError, ValueError) as exc:
            terminal.write(terminal.style.red(f'! cannot read stdin: {exc}\n'), err=True)
            return 1
    if not options.prompt.strip():
        return 0
    return await _interactive(api, options, terminal)


def run(
    *,
    host: str,
    port: int,
    root: str | Path,
    model: str = '',
    provider: str = '',
    session_id: str = '',
    mode: str = '',
    toolset: list[str] | None = None,
    yes: bool = False,
    system: str = '',
    prompt: str = '',
    image: str = '',
    continue_: bool = False,
    token: str = '',
    effort: str = '',
    title: str = '',
    colour: bool | None = None,
    width: int | None = None,
) -> int:
    """Talk to a daemon. Returns a process exit code: 0, 1, or 130.

    This is the whole public surface. `cli.py` calls it and nothing else:

        from openmirror.tui import run
        raise SystemExit(run(host=host, port=port, root=root, model=model, session_id=session))

    Everything past `system` is optional and defaults to "not used", so a
    caller that knows only the documented ten arguments still gets a working
    client — without images, without a system prompt and without `--continue`.
    """
    options = Options(
        host=host,
        port=port,
        root=str(Path(root).expanduser().resolve()) if root else str(Path.cwd()),
        model=model,
        provider=provider,
        session_id=session_id,
        mode=mode,
        toolset=list(toolset) if toolset else None,
        yes=yes,
        system=system,
        prompt=prompt,
        image=image,
        continue_=continue_,
        token=token,
        effort=effort,
        title=title,
        colour=colour,
        width=width,
    )
    terminal = Terminal(colour=colour, width=width)
    try:
        return asyncio.run(_serve(options, terminal))
    except KeyboardInterrupt:
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m openmirror.tui`. `cli.py` uses `run` instead of this."""
    options = options_from_args(build_parser().parse_args(argv))
    return run(**dataclasses.asdict(options))


if __name__ == '__main__':
    raise SystemExit(main())

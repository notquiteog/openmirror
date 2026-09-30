"""The terminal client, tested without a terminal.

The TUI is the one part of this project that cannot be tested the way the rest
is, so it is built to be: the renderer is a function from an event to a
string, the line editor is a buffer with no terminal in it, the approval
parser answers a string with a decision, and the argument parser is a plain
`argparse`. Everything below exercises those. Nothing here opens a socket,
needs a tty, or blocks on a read — the socket itself is exercised by the
integration-style test at the bottom, which drives `App` with scripted events
and a stub daemon instead of a daemon.

Two properties are asserted rather than assumed, because both are the kind of
thing that breaks silently:

* **Nothing here imports the agent.** A terminal client that reached into
  `openmirror.agent` would be a second implementation of the agent's rules, and
  every one of them would exist in two places with one of them stale. The
  source of this module is read and checked, the same way
  `test_signin_gate.py` reads `dom.js`, because a check that can pass by
  matching nothing is a check that has stopped working.
* **Colour is off unless it is asked for.** The plain output is the one that
  has to be exactly right, because it is what ends up in a file.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
from pathlib import Path

import pytest

from openmirror import tui
from openmirror.tui import (
    MAX_ATTACHMENT_BYTES,
    App,
    AttachmentError,
    Daemon,
    DaemonError,
    Key,
    Line,
    Options,
    Renderer,
    Style,
    Terminal,
    apply_completion,
    approval_prompt,
    attachments_from_paths,
    build_image_attachment,
    build_parser,
    call_key,
    call_line,
    describe_call,
    filter_files,
    first_line,
    flat,
    human,
    mention_at,
    options_from_args,
    parse_approval,
    parse_slash,
    popup_lines,
    slash_completions,
    sniff_media_type,
    split_image_tokens,
    strip_ansi,
    took,
    wants_colour,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / 'openmirror' / 'tui.py').read_text()

#: A real one-pixel PNG. Written out rather than imported from Pillow, which
#: is an optional extra: this file has to run on a bare install.
TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


def plain(width: int = 80) -> Renderer:
    """A renderer with colour off, which is the output a test can assert on."""
    return Renderer(style=Style(False), width=width)


def coloured(width: int = 80) -> Renderer:
    return Renderer(style=Style(True), width=width)


# ---------------------------------------------------------------------------
# The separation itself
# ---------------------------------------------------------------------------


def test_the_client_does_not_import_the_agent():
    """The rule the whole design rests on, checked against the source.

    Not a style preference: a client that imported `openmirror.agent` would
    work in-process and would then need the agent's configuration, its
    providers and its event loop, which is a second client that can only ever
    talk to a daemon that happens to be in this process. It would also be the
    second copy of every approval rule.
    """
    for forbidden in ('openmirror.agent', 'openmirror.sessions', 'openmirror.providers', 'openmirror.routers'):
        assert not re.search(rf'^\s*(?:from|import)\s+{re.escape(forbidden)}', SOURCE, re.M), (
            f'{forbidden} is imported by the terminal client'
        )
    # Asserted rather than left to the loop above, which could pass by
    # matching nothing if the pattern stopped matching.
    assert 'from openmirror import tui' not in SOURCE
    assert 'aiohttp' in SOURCE, 'the transport is missing; the search above proved nothing'


def test_no_new_dependency_is_introduced():
    """Only `aiohttp`, which pyproject already lists.

    A terminal client that pulled in prompt_toolkit or rich would be a large
    new dependency for a project whose whole pitch is that it installs from
    source.
    """
    import ast
    import sys

    roots: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split('.')[0])
    third_party = roots - set(sys.stdlib_module_names) - {'openmirror', '__future__'}
    assert third_party == {'aiohttp'}, f'unexpected imports: {sorted(third_party)}'
    assert 'aiohttp' in SOURCE, 'the transport is missing, so the search proved nothing'


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakePipe(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.mark.parametrize(
    'env,expected',
    [
        ({}, True),
        ({'NO_COLOR': ''}, False),
        ({'NO_COLOR': '1'}, False),
        ({'TERM': 'dumb'}, False),
        ({'TERM': 'DUMB'}, False),
        ({'TERM': 'xterm-256color'}, True),
        ({'OPENMIRROR_NO_COLOUR': '1'}, False),
    ],
)
def test_colour_is_refused_when_something_asks(env, expected):
    assert wants_colour(None, env) is expected


def test_a_pipe_turns_colour_off_even_without_an_environment_variable():
    """The one that matters for scripting.

    `NO_COLOR` is a convention somebody has to know about; a redirected
    stdout is a thing that happens by accident, and colour in it is corrupted
    output rather than a preference.
    """
    assert wants_colour(FakeTTY(), {}) is True
    assert wants_colour(FakePipe(), {}) is False


def test_a_stream_that_cannot_answer_is_treated_as_a_pipe():
    class Broken:
        def isatty(self):
            raise OSError('closed')

    assert wants_colour(Broken(), {}) is False


def test_a_renderer_with_no_colour_emits_no_escape_sequences():
    event = {'type': 'error', 'message': 'the model is not answering'}
    assert plain().feed(event) == '! the model is not answering\n'
    assert '\x1b' not in plain().feed(event)


def test_the_same_event_with_colour_is_the_same_words_inside_escapes():
    event = {'type': 'error', 'message': 'the model is not answering'}
    painted = coloured().feed(event)
    assert painted != plain().feed(event)
    assert '\x1b[' in painted
    assert strip_ansi(painted) == plain().feed(event)


def test_terminal_refuses_colour_when_its_output_is_a_pipe():
    assert Terminal(FakePipe(), FakePipe()).style.enabled is False
    assert Terminal(FakeTTY(), FakeTTY(), colour=False).style.enabled is False
    assert Terminal(FakePipe(), FakePipe(), colour=True).style.enabled is True


# ---------------------------------------------------------------------------
# Rendering, one test per event type
# ---------------------------------------------------------------------------


def test_session_started_says_nothing_and_fills_in_the_status_line():
    """It goes to the prompt, not the transcript.

    Printing it would duplicate what the status line already carries, and a
    replay after a reconnect would print it a second time.
    """
    renderer = plain()
    out = renderer.feed({
        'type': 'session.started', 'seq': 4, 'session_id': 's1',
        'cwd': '/home/dev/project', 'model': 'qwen3:8b',
        'policy': 'ask before anything that writes', 'tools': ['a', 'b'], 'effort': 'high',
    })
    assert out == ''
    assert renderer.info['model'] == 'qwen3:8b'
    assert renderer.info['cwd'] == '/home/dev/project'
    assert renderer.info['effort'] == 'high'
    # `session.started` carries the sentence, not the short mode, so the mode
    # is still unknown: the first `policy.changed` is genuinely news.
    assert renderer.mode == ''


def test_turn_started_echoes_what_was_asked():
    renderer = plain()
    assert renderer.feed({'type': 'turn.started', 'turn_id': 't', 'text': 'what port?', 'at': 1.0}) == '> what port?\n'


def test_a_local_echo_is_not_shown_twice():
    """What was typed is drawn at once, so the server's echo of it is dropped.

    The browser does the same. Without this a person sees their own question
    once when they press Enter and again when the turn starts.
    """
    renderer = plain()
    renderer.echoed = 'what port?'
    assert renderer.feed({'type': 'turn.started', 'turn_id': 't', 'text': 'what port?'}) == ''
    assert renderer.echoed == ''
    # A turn started from a replay carries text that was never echoed here.
    assert renderer.feed({'type': 'turn.started', 'turn_id': 't', 'text': 'an older question'}) == '> an older question\n'


def test_a_multi_line_turn_is_indented_rather_than_truncated():
    assert plain().feed({'type': 'turn.started', 'text': 'first\nsecond'}) == '> first\n  second\n'


def test_text_delta_streams_without_a_newline_so_an_answer_appears_as_it_arrives():
    """Unbuffered rendering is the whole point of the exercise.

    A newline after every delta would put a blank line between every two words
    of a paragraph and be the single most obviously broken thing a terminal
    client can do.
    """
    renderer = plain()
    assert renderer.feed({'type': 'text.delta', 'text': 'It listens '}) == 'It listens '
    assert renderer.feed({'type': 'text.delta', 'text': 'on 8477.'}) == 'on 8477.'
    assert renderer.streaming is True


def test_thinking_collapses_to_one_dim_marker_when_something_else_arrives():
    renderer = plain()
    assert renderer.feed({'type': 'thinking.delta', 'text': 'The port is '}) == ''
    assert renderer.feed({'type': 'thinking.delta', 'text': 'in config.py.'}) == ''
    assert renderer.feed({'type': 'tool.proposed', 'call': {'name': 'read_file', 'summary': 'config.py'}}) == (
        '· thinking: The port is in config.py.\n'
        '● read_file config.py\n'
    )


def test_long_reasoning_is_summarised_rather_than_truncated_mid_word():
    renderer = plain()
    for word in range(40):
        renderer.feed({'type': 'thinking.delta', 'text': f'word{word} '})
    line = renderer.feed({'type': 'tool.proposed', 'call': {'name': 'todo'}}).splitlines()[0]
    assert line.endswith('(40 words)')
    assert len(line) < 120


def test_tool_proposed_is_one_dim_line_with_the_summary_the_tool_wrote():
    renderer = plain()
    out = renderer.feed({
        'type': 'tool.proposed', 'turn_id': 't',
        'call': {'id': 'c1', 'name': 'shell', 'summary': 'rm -rf build'},
        'needs_approval': False,
    })
    assert out == '● shell rm -rf build\n'


def test_a_call_that_needs_approval_says_so_on_its_own_line():
    out = plain().feed({
        'type': 'tool.proposed',
        'call': {'id': 'c1', 'name': 'write_file', 'summary': 'openmirror/tui.py'},
        'needs_approval': True,
    })
    assert out == '● write_file openmirror/tui.py  (needs approval)\n'


def test_tool_started_adds_nothing():
    """The proposal already said what was about to happen.

    A second line saying it started is the difference between a transcript
    and a log file, and a tool-heavy turn would spend most of its lines there.
    """
    renderer = plain()
    renderer.feed({'type': 'tool.proposed', 'call': {'id': 'c1', 'name': 'shell'}})
    assert renderer.feed({'type': 'tool.started', 'call_id': 'c1'}) == ''


def test_a_tool_result_is_one_line_and_never_the_file_it_read():
    """The collapse that makes a transcript readable at all.

    `content` here is what would be sent to the model. Printing it would put
    an entire file into the terminal between two lines of conversation.
    """
    file_contents = '\n'.join(f'line {n}' for n in range(400))
    out = plain().feed({
        'type': 'tool.completed',
        'result': {'id': 'c1', 'name': 'read_file', 'ok': True,
                   'content': file_contents, 'duration_ms': 1200},
    })
    assert out == '  ↳ line 0 (1.2s)\n'
    assert 'line 399' not in out


def test_a_failed_tool_result_says_so_in_the_failure_colour():
    renderer = plain()
    out = renderer.feed({
        'type': 'tool.completed',
        'result': {'id': 'c1', 'name': 'shell', 'ok': False,
                   'content': 'bash: pytest: command not found'},
    })
    assert out == '  ↳ failed: bash: pytest: command not found\n'
    assert coloured().feed({
        'type': 'tool.completed',
        'result': {'id': 'c1', 'name': 'shell', 'ok': False, 'content': 'nope'},
    }).startswith('\x1b[31m  ↳ failed')


def test_a_result_that_says_nothing_falls_back_to_the_last_thing_the_tool_printed():
    """A four-minute build streams for minutes and then returns an empty
    result. Showing only "ok" would throw away the only useful thing it said."""
    renderer = plain()
    assert renderer.feed({'type': 'tool.output.delta', 'call_id': 'c1', 'text': '...\n'}) == ''
    assert renderer.feed({'type': 'tool.output.delta', 'call_id': 'c1', 'text': 'built in 4.1s\n'}) == ''
    assert renderer.feed({
        'type': 'tool.completed',
        'result': {'id': 'c1', 'name': 'shell', 'ok': True, 'content': '', 'duration_ms': 241000},
    }) == '  ↳ built in 4.1s (4m 1s)\n'


def test_a_truncated_result_says_that_it_was_truncated():
    out = plain().feed({
        'type': 'tool.completed',
        'result': {'id': 'c1', 'name': 'shell', 'ok': True, 'content': 'traceback', 'truncated': True},
    })
    assert out == '  ↳ traceback, truncated\n'


def test_a_tool_denied_says_why():
    assert plain().feed({'type': 'tool.denied', 'call_id': 'c1', 'reason': 'a hook stopped this'}) == (
        '  ✗ denied: a hook stopped this\n'
    )
    assert plain().feed({'type': 'tool.denied', 'call_id': 'c1'}) == '  ✗ denied\n'


def test_a_question_lists_its_options_and_how_to_answer():
    out = plain().feed({
        'type': 'question.asked', 'question_id': 'q1',
        'question': 'Which database?', 'options': ['postgres', 'sqlite'],
    })
    assert out == '? Which database?\n  1) postgres\n  2) sqlite\n  (a number, or type your own answer)\n'


def test_a_hook_approval_shows_the_command():
    out = plain().feed({
        'type': 'hook.approval',
        'hook': {'command': 'pytest -q', 'command_readable': 'run the test suite'},
    })
    assert out == '⚑ hook wants to run: run the test suite\n'


def test_a_held_message_says_that_it_is_held_and_not_lost():
    """The server accepts a message sent mid-turn and holds it. Saying nothing
    would show a question that is neither running nor lost."""
    assert plain().feed({'type': 'turn.queued', 'waiting': 2}) == (
        '· held — it runs when this turn finishes (2 waiting)\n'
    )


def test_turn_completed_reports_the_duration_and_the_context():
    renderer = plain()
    renderer.feed({'type': 'turn.started', 'turn_id': 't', 'text': 'hi', 'at': 1000.0})
    renderer.feed({'type': 'text.delta', 'text': 'hello'})
    out = renderer.feed({
        'type': 'turn.completed', 'turn_id': 't', 'stop_reason': 'end_turn', 'at': 1003.4,
        'context': {'tokens': 12000, 'limit': 200000},
    })
    assert out == '\n\n· 3.4s · context 12.0k/200.0k (6%)\n'
    assert renderer.streaming is False


def test_an_interrupted_turn_says_so_and_still_reports_the_context():
    renderer = plain()
    renderer.feed({'type': 'turn.started', 'text': 'go', 'at': 1000.0})
    renderer.feed({'type': 'text.delta', 'text': 'working'})
    out = renderer.feed({'type': 'turn.completed', 'stop_reason': 'interrupted', 'at': 1002.0})
    assert out == '\n\n· interrupted\n· 2.0s\n'


def test_running_out_of_steps_is_an_error_and_says_so():
    assert plain().feed({'type': 'turn.completed', 'stop_reason': 'max_steps'}) == '· stopped: too many steps\n'
    assert plain().feed({'type': 'turn.completed', 'stop_reason': 'error'}) == '· stopped: error\n'


def test_a_full_context_nags_only_when_it_is_nearly_full():
    """A bar that cries wolf is a bar that is ignored, so the nudge appears
    at 85% and not before."""
    nearly = plain().feed({'type': 'turn.completed', 'context': {'tokens': 900, 'limit': 1000}})
    assert '/compact soon' in nearly
    room = plain().feed({'type': 'turn.completed', 'context': {'tokens': 100, 'limit': 1000}})
    assert '/compact soon' not in room


def test_an_error_says_whether_retrying_is_worth_it():
    """The protocol distinguishes them, and so must the client: retrying a
    bad token forever is how one failure becomes an infinite loop."""
    assert plain().feed({'type': 'error', 'message': 'rate limited', 'retryable': True}) == (
        '! rate limited (retryable)\n'
    )
    assert plain().feed({'type': 'error', 'message': 'that is not a token'}) == '! that is not a token\n'


def test_a_policy_change_only_announces_what_actually_moved():
    """One event carries both controls. Announcing a thinking change as
    "approval is now: ask first" is a true sentence about the wrong thing."""
    renderer = plain()
    renderer.feed({'type': 'session.started', 'policy': 'ask (runs without asking: read)', 'effort': None})
    first = renderer.feed({
        'type': 'policy.changed', 'mode': 'ask', 'policy': 'ask (runs without asking: read)', 'effort': 'high',
    })
    assert first == (
        '· approval is now: ask (runs without asking: read)\n'
        '· thinking is now: high\n'
    )
    # A replay of the same event after a reconnect changes nothing.
    assert renderer.feed({
        'type': 'policy.changed', 'mode': 'ask', 'policy': 'ask (runs without asking: read)', 'effort': 'high',
    }) == ''
    assert renderer.feed({
        'type': 'policy.changed', 'mode': 'full-auto', 'policy': 'full-auto', 'effort': 'high',
    }) == '· approval is now: full-auto\n'
    assert renderer.feed({
        'type': 'policy.changed', 'mode': 'full-auto', 'policy': 'full-auto', 'effort': None,
    }) == "· thinking is now: the model's own default\n"


def test_a_task_reports_its_state_and_its_exit_code():
    assert plain().feed({
        'type': 'task.updated',
        'task': {'kind': 'shell', 'label': 'dev server', 'status': 'done', 'exit_code': 0},
    }) == '· task shell dev server done exit 0\n'
    failed = plain().feed({
        'type': 'task.updated',
        'task': {'kind': 'shell', 'label': 'dev server', 'status': 'failed', 'exit_code': 1},
    })
    assert strip_ansi(failed) == '· task shell dev server failed exit 1\n'
    assert coloured().feed({
        'type': 'task.updated',
        'task': {'kind': 'shell', 'status': 'failed'},
    }).startswith('\x1b[31m')


def test_context_compacted_says_what_it_cost():
    out = plain().feed({
        'type': 'context.compacted', 'reason': 'automatic',
        'messages_before': 42, 'messages_after': 6, 'tokens_before': 61000,
    })
    assert out == '· context compacted · 42 → 6 messages · ~61.0k tokens · automatic\n'
    assert plain().feed({'type': 'context.compacted', 'reason': 'cleared'}) == '· context cleared\n'


def test_session_ended_says_why():
    assert plain().feed({'type': 'session.ended', 'reason': 'closed'}) == '· session ended: closed\n'


def test_pong_and_an_unknown_event_render_as_nothing():
    """A client that prints a blank line for an event it does not know is
    still a working client; the protocol is versioned separately from here."""
    assert plain().feed({'type': 'pong', 'seq': 99}) == ''
    assert plain().feed({'type': 'some.future.event', 'payload': 1}) == ''


def test_the_sequence_number_is_remembered_so_a_reconnect_can_ask_for_the_gap():
    """This is the only reason a reconnect is cheap rather than a full replay."""
    renderer = plain()
    for seq in (3, 1, 7, 2):
        renderer.feed({'type': 'pong', 'seq': seq})
    assert renderer.seq == 7
    # An event with no sequence number of its own must not move it.
    renderer.feed({'type': 'pong'})
    assert renderer.seq == 7


def test_long_answers_wrap_at_the_width_and_not_at_the_terminals_guessed_one():
    renderer = plain(width=20)
    out = renderer.feed({'type': 'text.delta', 'text': 'the quick brown fox jumps over the lazy dog'})
    assert out == 'the quick brown fox\njumps over the lazy\ndog'


def test_a_word_longer_than_the_line_is_broken_rather_than_left_hanging():
    """Left hanging, it would wrap at the terminal's width rather than ours
    and the transcript would stop matching the screen."""
    out = plain(width=20).feed({'type': 'text.delta', 'text': 'pneumonoultramicroscopic'})
    assert out == 'pneumonoultramicrosc\nopic'
    assert max(len(line) for line in out.splitlines()) <= 20


def test_a_newline_inside_a_delta_starts_a_new_line():
    assert plain().feed({'type': 'text.delta', 'text': 'first\nsecond'}) == 'first\nsecond'


# ---------------------------------------------------------------------------
# The scripted transcript
# ---------------------------------------------------------------------------


def test_a_whole_turn_renders_as_the_transcript_it_should_be():
    """The integration test: a scripted event stream, through the renderer,
    to the exact characters a person would see.

    Every individual event is asserted on elsewhere. This exists to catch the
    thing those cannot: that the pieces fit — that the reasoning marker is
    folded before the tool line rather than after the answer, that the answer
    is not cut off by the turn's footer, and that a stream which arrives in
    pieces and out of order still reads as a conversation.
    """
    events = [
        {'type': 'session.started', 'seq': 1, 'session_id': 's1', 'cwd': '/w',
         'model': 'qwen3:8b', 'policy': 'ask first', 'tools': [], 'effort': None},
        {'type': 'turn.started', 'seq': 2, 'turn_id': 't1', 'text': 'which port?', 'at': 1000.0},
        {'type': 'thinking.delta', 'seq': 3, 'turn_id': 't1', 'text': 'The default '},
        {'type': 'thinking.delta', 'seq': 4, 'turn_id': 't1', 'text': 'is in config.'},
        {'type': 'tool.proposed', 'seq': 5, 'turn_id': 't1',
         'call': {'id': 'c1', 'name': 'read_file', 'summary': 'openmirror/config.py'},
         'needs_approval': False},
        {'type': 'tool.completed', 'seq': 6, 'turn_id': 't1',
         'result': {'id': 'c1', 'name': 'read_file', 'ok': True,
                    'content': 'port: int = 8477', 'duration_ms': 900}},
        {'type': 'text.delta', 'seq': 7, 'turn_id': 't1', 'text': 'It listens '},
        {'type': 'text.delta', 'seq': 8, 'turn_id': 't1', 'text': 'on port '},
        {'type': 'text.delta', 'seq': 9, 'turn_id': 't1', 'text': '8477 by default.'},
        {'type': 'turn.completed', 'seq': 10, 'turn_id': 't1', 'stop_reason': 'end_turn', 'at': 1002.5,
         'context': {'tokens': 2400, 'limit': 200000}},
        # A tool call somebody had to be asked about, answered no.
        {'type': 'turn.started', 'seq': 11, 'turn_id': 't2', 'text': 'and stop it', 'at': 1003.0},
        {'type': 'tool.proposed', 'seq': 12, 'turn_id': 't2',
         'call': {'id': 'c2', 'name': 'shell', 'summary': 'systemctl stop openmirror'},
         'needs_approval': True},
        {'type': 'tool.denied', 'seq': 13, 'turn_id': 't2', 'call_id': 'c2', 'reason': 'declined from the terminal'},
        {'type': 'text.delta', 'seq': 14, 'turn_id': 't2', 'text': 'Left it running.'},
        {'type': 'turn.completed', 'seq': 15, 'turn_id': 't2', 'stop_reason': 'end_turn', 'at': 1004.0},
    ]
    assert plain().transcript(events) == (
        '> which port?\n'
        '· thinking: The default is in config.\n'
        '● read_file openmirror/config.py\n'
        '  ↳ port: int = 8477 (900ms)\n'
        'It listens on port 8477 by default.\n'
        '\n'
        '· 2.5s · context 2.4k/200.0k (1%)\n'
        '> and stop it\n'
        '● shell systemctl stop openmirror  (needs approval)\n'
        '  ✗ denied: declined from the terminal\n'
        'Left it running.\n'
        '\n'
        '· 1.0s\n'
    )


def test_the_same_stream_with_colour_on_contains_exactly_the_same_words():
    """Colour must never change what is said, only how it is painted."""
    events = [
        {'type': 'turn.started', 'seq': 1, 'turn_id': 't', 'text': 'hello', 'at': 1.0},
        {'type': 'tool.proposed', 'seq': 2, 'turn_id': 't',
         'call': {'id': 'c1', 'name': 'read_file', 'summary': 'x.py'}, 'needs_approval': False},
        {'type': 'tool.completed', 'seq': 3, 'turn_id': 't',
         'result': {'id': 'c1', 'name': 'read_file', 'ok': False, 'content': 'gone'}},
        {'type': 'text.delta', 'seq': 4, 'turn_id': 't', 'text': 'sorry'},
        {'type': 'turn.completed', 'seq': 5, 'turn_id': 't', 'stop_reason': 'end_turn', 'at': 2.0},
    ]
    assert strip_ansi(coloured().transcript(events)) == plain().transcript(events)


def test_a_replay_after_a_reconnect_renders_the_gap_and_nothing_before_it():
    """The reconnect contract, at the level that can be tested.

    The client sends `since` and the daemon replays what was missed. If the
    renderer trusted every event it were handed, a reconnect would reprint the
    whole conversation; the sequence number is what lets the socket drop the
    ones it has already shown.
    """
    seen = [
        {'type': 'turn.started', 'seq': 1, 'turn_id': 't1', 'text': 'first', 'at': 1.0},
        {'type': 'text.delta', 'seq': 2, 'turn_id': 't1', 'text': 'one'},
        {'type': 'turn.completed', 'seq': 3, 'turn_id': 't1', 'stop_reason': 'end_turn', 'at': 2.0},
    ]
    renderer = plain()
    assert renderer.transcript(seen) == '> first\none\n\n· 1.0s\n'
    assert renderer.seq == 3
    gap = [
        {'type': 'turn.started', 'seq': 4, 'turn_id': 't2', 'text': 'second', 'at': 3.0},
        {'type': 'text.delta', 'seq': 5, 'turn_id': 't2', 'text': 'two'},
        {'type': 'turn.completed', 'seq': 6, 'turn_id': 't2', 'stop_reason': 'end_turn', 'at': 4.0},
    ]
    assert renderer.transcript(gap) == '> second\ntwo\n\n· 1.0s\n'
    assert renderer.seq == 6


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('count,expected', [(0, '0'), (999, '999'), (1000, '1.0k'), (12345, '12.3k'), (2_400_000, '2.4M')])
def test_a_token_count_is_written_at_the_size_a_person_reads_it_at(count, expected):
    assert human(count) == expected


@pytest.mark.parametrize(
    'ms,expected', [(41.2, '41ms'), (999, '999ms'), (1200, '1.2s'), (45000, '45s'), (65000, '1m 5s')]
)
def test_a_duration_is_written_in_the_units_somebody_would_say_aloud(ms, expected):
    assert took(ms) == expected


def test_a_long_value_is_flattened_and_marked_rather_than_cut_mid_word():
    assert flat('one\ntwo   three', 100) == 'one two three'
    assert flat('x' * 50, 10) == 'xxxxxxxxx…'


def test_the_first_line_of_a_tool_result_is_found_even_after_a_blank_one():
    assert first_line('\n\n  the answer  \nmore') == 'the answer'
    assert first_line('\n \n') == ''


# ---------------------------------------------------------------------------
# Describing a tool call
# ---------------------------------------------------------------------------


def test_the_tools_own_summary_is_preferred_over_its_arguments():
    assert describe_call({'name': 'shell', 'summary': 'git status', 'arguments': {'command': ['git', 'status']}}) == 'git status'


def test_without_a_summary_the_most_useful_argument_is_guessed():
    """The client cannot know what tools exist, so it falls back to whichever
    argument names a thing. A generic 'the first argument' shows
    `run_in_background=False` more often than the path."""
    assert describe_call({'name': 'read_file', 'arguments': {'path': 'a/b.py'}}) == 'a/b.py'
    assert describe_call({'name': 'write_file', 'arguments': {'file_path': 'a/b.py', 'content': 'x' * 500}}) == 'a/b.py'
    assert describe_call({'name': 'grep', 'arguments': {'pattern': 'TODO', 'path': '.'}}) == 'TODO'
    assert describe_call({'name': 'read_file', 'arguments': {'path': '.'}}) == ''


def test_a_command_list_is_shown_as_a_command():
    assert describe_call({'name': 'shell', 'arguments': {'command': ['git', 'status', '--short']}}) == 'git status --short'


def test_a_call_with_nothing_to_say_still_produces_a_line():
    assert call_line({'name': 'todo'}) == '● todo'
    assert call_line({}) == '● tool'


def test_structure_instead_of_prose_gets_a_line_too():
    assert tui.summarise_display({'path': 'a.py', 'changed': 3}) == 'a.py'
    assert 'exit_code=1' in tui.summarise_display({'exit_code': 1, 'stdout': 'x' * 90})


def test_an_always_answer_is_scoped_to_this_call_and_not_to_the_tool():
    """The rule the protocol is built around. A client that remembered 'yes to
    shell' for the session would be handing out permanent shell access from
    one keystroke."""
    one = call_key({'name': 'shell', 'summary': 'ls'})
    another = call_key({'name': 'shell', 'summary': 'rm -rf /'})
    assert one != another
    assert call_key({'name': 'read_file', 'summary': 'ls'}) != one


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def test_the_daemons_commands_are_parsed_and_passed_through():
    """/compact, /clear and /think are the daemon's, not ours.

    They are shaped like a turn so a transcript can show them as one, and a
    client that answered them locally would have to reimplement them.
    """
    for name in ('compact', 'clear', 'think'):
        slash = parse_slash(f'/{name}')
        assert slash is not None and slash.name == name and slash.local is False


def test_command_arguments_are_kept():
    assert parse_slash('/think high') == tui.Slash('think', 'high', False)
    assert parse_slash('  /think  high  ').args == 'high'
    assert parse_slash('/THINK').name == 'think'


def test_local_commands_are_recognised_and_are_not_sent_to_the_daemon():
    assert parse_slash('/exit').local is True
    assert parse_slash('/quit').local is True
    assert parse_slash('/help').local is True


def test_ordinary_text_is_not_a_command():
    for text in ('what port?', '/', 'a/b', 'email me@example.com', ''):
        assert parse_slash(text) is None, text


def test_completion_offers_local_commands_before_the_daemons():
    """/e that runs a project skill instead of leaving is a nasty surprise,
    so the ones this client answers itself come first."""
    commands = [{'name': 'deploy', 'description': 'Ship it', 'kind': 'skill'}]
    assert [c['name'] for c in slash_completions('/e', commands)] == ['exit']
    assert slash_completions('/d', commands)[0]['name'] == 'deploy'
    assert slash_completions('/zz', commands) == []


def test_completion_keeps_the_daemons_order_within_a_match():
    commands = [
        {'name': 'design', 'description': 'Plan it', 'kind': 'command'},
        {'name': 'deploy', 'description': 'Ship it', 'kind': 'skill'},
    ]
    assert [c['name'] for c in slash_completions('/d', commands)] == ['design', 'deploy']


def test_completion_is_capped():
    many = [{'name': f'do{index}', 'description': ''} for index in range(50)]
    assert len(slash_completions('/do', many, limit=5)) == 5


# ---------------------------------------------------------------------------
# `@` mentions
# ---------------------------------------------------------------------------


def test_the_mention_under_the_cursor_is_found_including_a_nested_path():
    """A whitespace-delimited token cannot express a nested path, which is
    most of them, so the scan goes back to the `@` rather than to a space."""
    assert mention_at('look at @openmir/age', 18) == (8, 'openmir/a')
    assert mention_at('@src/tui.py', 11) == (0, 'src/tui.py')


def test_an_email_address_is_not_a_file_mention():
    assert mention_at('write to me@example.com', 20) is None
    assert mention_at('no mention here', 5) is None


def test_a_query_with_no_matches_offers_nothing_rather_than_everything():
    """The failure mode to avoid is a menu that appears on any `@` at all."""
    hits = [{'path': 'openmirror/config.py'}, {'path': 'README.md'}]
    assert filter_files(hits, 'zzzz') == []


def test_a_nested_query_finds_the_file_under_that_directory():
    hits = [
        {'path': 'openmirror/config.py'},
        {'path': 'openmirror/agent/session.py'},
        {'path': 'openmirror/agent/runtime.py'},
        {'path': 'README.md'},
    ]
    assert filter_files(hits, 'agent/ru') == ['openmirror/agent/runtime.py']
    # A directory prefix matches both, and the daemon's order is kept: this
    # narrows what the daemon offered, it does not re-rank it.
    assert filter_files(hits, 'openmirror/agent/') == [
        'openmirror/agent/session.py',
        'openmirror/agent/runtime.py',
    ]


def test_an_exact_name_beats_a_path_that_merely_contains_it():
    hits = [{'path': 'docs/config.md'}, {'path': 'src/config.py'}]
    assert filter_files(hits, 'config.py')[0] == 'src/config.py'


def test_the_daemons_order_survives_for_an_empty_query():
    """The daemon ranks exact matches first and then most recently touched;
    re-sorting alphabetically would throw that away."""
    hits = [{'path': 'z.py'}, {'path': 'a.py'}]
    assert filter_files(hits, '') == ['z.py', 'a.py']


def test_duplicate_paths_are_offered_once():
    assert filter_files([{'path': 'a.py'}, {'path': 'a.py'}], '') == ['a.py']


def test_completing_replaces_the_token_and_puts_the_cursor_after_it():
    assert apply_completion('look at @openmir/a now', 13, 'openmirror/agent/runtime.py') == (
        'look at openmirror/agent/runtime.py now',
        35,
    )


def test_completing_at_the_end_of_the_line_keeps_the_cursor_at_the_end():
    assert apply_completion('@conf', 5, 'openmirror/config.py') == ('openmirror/config.py', 20)


def test_completing_outside_a_mention_changes_nothing():
    assert apply_completion('nothing here', 5, 'x.py') == ('nothing here', 5)


def test_the_menu_is_drawn_as_lines_so_what_appears_can_be_asserted_on():
    items = [{'name': '/a', 'description': 'Leave.'}, {'name': '/exit', 'description': 'Leave.'}]
    assert popup_lines(items, 1, Style(False), 40) == ['  /a  Leave.', '> /exit  Leave.']
    assert popup_lines([], 0, Style(False), 40) == []


def test_the_selected_entry_is_painted_in_reverse_with_colour_on():
    items = [{'name': '/a', 'description': ''}, {'name': '/b', 'description': ''}]
    painted = popup_lines(items, 1, Style(True), 40)
    assert painted[1].startswith('\x1b[7m')
    assert not painted[0].startswith('\x1b[7m')




def test_a_long_description_is_cut_to_the_width_it_has_room_for():
    lines = popup_lines([{'name': '/x', 'description': 'y' * 200}], 0, Style(False), 40)
    assert max(len(line) for line in lines) <= 40


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('answer,allow,remember', [
    ('y', True, False),
    ('Y', True, False),
    ('yes', True, False),
    ('  yes  ', True, False),
    ('a', True, True),
    ('always', True, True),
    ('n', False, False),
    ('no', False, False),
    ('', False, False),
])
def test_the_offered_answers_mean_what_they_say(answer, allow, remember):
    decision = parse_approval(answer)
    assert (decision.allow, decision.remember, decision.valid) == (allow, remember, True)


@pytest.mark.parametrize('answer', ['yolo', 'aye', 'ya', 'yess', 'maybe', 'sure', 'ok', '0', '1', 'allow'])
def test_nothing_that_is_not_an_answer_is_ever_treated_as_approval(answer):
    """`yolo` and `aye` both begin with a letter that means yes. A client that
    matches on the first character approves them, and a client whose approvals
    can be produced by `yolo` has no approvals."""
    decision = parse_approval(answer)
    assert decision.allow is False
    assert decision.valid is False, 'the caller must re-ask rather than treat a typo as a decision'


def test_nothing_is_never_allowed():
    assert parse_approval(None).allow is False


def test_end_of_input_is_a_denial_that_also_stops_the_turn():
    """A closed stdin is not consent to run a shell, and it is not a reason
    to keep asking a question nobody is there to read."""
    decision = parse_approval(None)
    assert decision.abort is True
    assert decision.allow is False


def test_the_prompt_names_the_tool_and_offers_all_three_answers():
    assert approval_prompt('shell') == 'Allow shell? [y/N/a(lways)] '


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_a_real_png_becomes_an_attachment_the_daemon_accepts(tmp_path):
    """The shape in `Session._run_turn`: `{'type':'image','data':…, 'media_type':…}`."""
    path = tmp_path / 'tiny.png'
    path.write_bytes(TINY_PNG)
    attachment = build_image_attachment(path)
    assert attachment['type'] == 'image'
    assert attachment['media_type'] == 'image/png'
    assert base64.b64decode(attachment['data']) == TINY_PNG


def test_an_oversized_file_is_refused_with_a_message_that_names_the_limit(tmp_path):
    """Refused locally, before the read. A base64 blob accepted by a socket
    and rejected by a provider three seconds later is a worse failure."""
    path = tmp_path / 'huge.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * MAX_ATTACHMENT_BYTES)
    with pytest.raises(AttachmentError) as caught:
        build_image_attachment(path)
    message = str(caught.value)
    assert f'{MAX_ATTACHMENT_BYTES:,}' in message
    assert 'Scale it down' in message


def test_a_non_image_is_refused_and_says_what_is_supported(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_text('this is a note, and it is quite long, and definitely not a picture')
    with pytest.raises(AttachmentError) as caught:
        build_image_attachment(path)
    assert 'does not look like an image' in str(caught.value)
    assert 'png' in str(caught.value)


def test_a_file_pretending_to_be_a_png_is_refused_by_what_it_actually_is(tmp_path):
    """The header is checked before the extension. A `.png` that is a zip is
    not a picture, and what the provider says about that is useless."""
    path = tmp_path / 'quarterly.xlsx.png'
    path.write_bytes(b'PK\x03\x04' + b'0' * 64)
    with pytest.raises(AttachmentError, match='zip'):
        build_image_attachment(path)


def test_a_missing_or_empty_file_is_refused_clearly(tmp_path):
    with pytest.raises(AttachmentError, match='no such file'):
        build_image_attachment(tmp_path / 'nope.png')
    empty = tmp_path / 'empty.png'
    empty.write_bytes(b'')
    with pytest.raises(AttachmentError, match='empty'):
        build_image_attachment(empty)
    with pytest.raises(AttachmentError, match='not a file'):
        build_image_attachment(tmp_path)


def test_the_type_comes_from_the_bytes_and_not_the_name(tmp_path):
    jpeg = tmp_path / 'shot.png'  # a JPEG called a PNG
    jpeg.write_bytes(b'\xff\xd8\xff\xe0' + b'0' * 32)
    assert build_image_attachment(jpeg)['media_type'] == 'image/jpeg'


def test_an_extensionless_image_is_still_an_image(tmp_path):
    path = tmp_path / 'screenshot'
    path.write_bytes(TINY_PNG)
    assert build_image_attachment(path)['media_type'] == 'image/png'


@pytest.mark.parametrize('head,expected', [
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff\xe0', 'image/jpeg'),
    (b'GIF89a', 'image/gif'),
    (b'RIFF\x00\x00\x00\x00WEBP', 'image/webp'),
    (b'not an image at all', None),
])
def test_the_header_is_what_decides(head, expected):
    assert sniff_media_type(head) == expected


def test_a_trailing_bang_path_is_taken_off_the_prompt_and_attached(tmp_path):
    path = tmp_path / 'shot.png'
    path.write_bytes(TINY_PNG)
    text, attachments = split_image_tokens(f'what is in this? !{path}')
    assert text == 'what is in this?'
    assert len(attachments) == 1
    assert attachments[0]['media_type'] == 'image/png'


def test_a_quoted_bang_path_survives_spaces(tmp_path):
    """What a drag into most terminals produces."""
    path = tmp_path / 'my screenshot.png'
    path.write_bytes(TINY_PNG)
    text, attachments = split_image_tokens(f'look !"{path}"')
    assert text == 'look'
    assert attachments[0]['media_type'] == 'image/png'


def test_a_bang_that_is_not_at_the_end_is_left_alone():
    """A `!` in the middle of a sentence is punctuation far more often than it
    is a drag, and reinterpreting prose as a filename is worse than not
    offering the shorthand."""
    assert split_image_tokens('wow! that worked') == ('wow! that worked', [])


def test_a_trailing_bang_on_a_non_image_fails_loudly(tmp_path):
    path = tmp_path / 'notes.txt'
    path.write_text('prose')
    with pytest.raises(AttachmentError):
        split_image_tokens(f'read !{path}')


def test_several_images_can_be_attached_at_once(tmp_path):
    first = tmp_path / 'a.png'
    second = tmp_path / 'b.jpg'
    first.write_bytes(TINY_PNG)
    second.write_bytes(b'\xff\xd8\xff\xe0' + b'0' * 16)
    attachments = attachments_from_paths([first, second])
    assert [a['media_type'] for a in attachments] == ['image/png', 'image/jpeg']


# ---------------------------------------------------------------------------
# The argument parser
# ---------------------------------------------------------------------------


def test_the_parser_help_works_and_names_the_command():
    parser = build_parser()
    text = parser.format_help()
    assert 'openmirror chat' in text
    assert '--session' in text
    assert parser.prog == 'openmirror chat'


@pytest.mark.parametrize('flag,attribute,expected', [
    ('--host', 'host', 'example.test'),
    ('--port', 'port', 9000),
    ('--root', 'root', '/tmp/x'),
    ('--model', 'model', 'qwen3:8b'),
    ('--provider', 'provider', 'ollama'),
    ('--session', 'session', 'abc'),
    ('--continue', 'continue_', True),
    ('--mode', 'mode', 'ask'),
    ('--effort', 'effort', 'high'),
    ('--yes', 'yes', True),
    ('--system', 'system', 'be terse'),
    ('--prompt', 'prompt', 'what changed?'),
    ('--image', 'image', '/tmp/a.png'),
    ('--token', 'token', 'secret'),
    ('--title', 'title', 'a session'),
    ('--no-colour', 'colour', False),
])
def test_every_flag_lands_where_it_belongs(flag, attribute, expected):
    args = build_parser().parse_args([flag, str(expected)] if not isinstance(expected, bool) else [flag])
    assert getattr(args, attribute) == expected


def test_the_short_flags_are_the_ones_people_reach_for():
    args = build_parser().parse_args(['-s', 'abc', '-m', 'gpt', '-y', '-c'])
    assert (args.session, args.model, args.yes, args.continue_) == ('abc', 'gpt', True, True)


def test_tools_repeat_and_become_a_list():
    args = build_parser().parse_args(['--tool', 'browser', '--tool', 'files'])
    assert args.toolset == ['browser', 'files']
    assert build_parser().parse_args([]).toolset == []


def test_colour_is_undecided_unless_asked_otherwise():
    """Three states, not two: undecided lets the environment and the stream
    decide, which is how `NO_COLOR` reaches a client that never parsed it."""
    assert build_parser().parse_args([]).colour is None
    assert build_parser().parse_args(['--no-colour']).colour is False
    assert build_parser().parse_args(['--no-color']).colour is False


def test_the_parser_becomes_options_with_an_absolute_root(tmp_path):
    """Absolute because it is about to cross to a daemon that may be on
    another machine, where a relative path means something else entirely."""
    options = options_from_args(build_parser().parse_args(['-C', str(tmp_path / 'sub')]))
    assert Path(options.root).is_absolute()
    assert options.toolset is None


def test_defaults_come_from_the_environment_the_daemon_itself_reads(monkeypatch):
    monkeypatch.setenv('OPENMIRROR_HOST', '10.0.0.5')
    monkeypatch.setenv('OPENMIRROR_PORT', '9000')
    monkeypatch.setenv('OPENMIRROR_TOKEN', 'shhh')
    args = build_parser().parse_args([])
    assert (args.host, args.port, args.token) == ('10.0.0.5', 9000, 'shhh')


def test_run_takes_the_documented_signature_and_a_few_extras():
    """`cli.py` calls this. The ten documented arguments keep their names and
    their defaults; the rest exist so a command line can reach them, and all
    default to not being used."""
    import inspect

    parameters = inspect.signature(tui.run).parameters
    required = ['host', 'port', 'root']
    defaults = {
        'model': '', 'provider': '', 'session_id': '', 'mode': '',
        'toolset': None, 'yes': False, 'system': '',
    }
    for name in required + list(defaults):
        assert name in parameters, name
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
    for name in required:
        assert parameters[name].default is inspect.Parameter.empty, name
    for name, default in defaults.items():
        assert parameters[name].default == default, name
    for name in ('prompt', 'image', 'continue_', 'token', 'effort', 'title', 'colour'):
        assert parameters[name].default in ('', False, None), name


# ---------------------------------------------------------------------------
# The line editor
# ---------------------------------------------------------------------------


def test_typing_inserts_at_the_cursor():
    line = Line()
    line.insert('hello')
    line.home()
    line.insert('> ')
    assert line.text == '> hello'
    assert line.cursor == 2


def test_backspace_and_delete_do_different_things():
    line = Line('abc')
    line.home()
    line.delete()
    assert line.text == 'bc'
    line.end()
    line.backspace()
    assert line.text == 'b'


def test_kill_word_removes_the_word_and_the_space_before_it():
    line = Line('read the config file')
    line.move(-4)  # between the space and the last word
    line.kill_word()
    assert line.text == 'read the file'
    line.kill_word()
    assert line.text == 'read file'


def test_kill_line_removes_everything_before_the_cursor():
    line = Line('first second')
    line.home()
    line.move(5)
    line.kill_line()
    assert line.text == ' second'


def test_the_cursor_is_kept_inside_the_text():
    line = Line('abc')
    line.move(-100)
    assert line.cursor == 0
    line.move(100)
    assert line.cursor == 3


def test_history_is_walked_and_an_empty_slot_clears_the_line():
    line = Line()
    line.remember('first')
    line.remember('second')
    assert line.older() is True and line.text == 'second'
    assert line.older() is True and line.text == 'first'
    assert line.older() is False
    assert line.newer() is True and line.text == 'second'
    assert line.newer() is True and line.text == ''
    assert line.newer() is False


def test_repeating_a_line_does_not_fill_the_history_with_it():
    """Somebody who sends the same thing twice in a row is holding down a key,
    not making history."""
    line = Line()
    line.remember('same')
    line.remember('same')
    assert line.history == ['same']


def test_a_line_wider_than_the_terminal_scrolls_so_the_cursor_stays_visible():
    line = Line('x' * 50)
    visible, offset = line.preview(20)
    assert len(visible) <= 20
    assert offset == 31
    assert line.cursor - offset == 19, 'the cursor must be the last column shown'
    # Scrolling stops at the end of the text rather than running past it.
    line.home()
    assert line.preview(20) == ('x' * 20, 0)


# ---------------------------------------------------------------------------
# The application, driven with scripted events and a stub daemon
# ---------------------------------------------------------------------------


class StubDaemon(Daemon):
    """A daemon that answers from a dict instead of a socket.

    Enough of the surface to drive `App`: the file lookup behind `@`, and
    nothing else. The real transport is `Daemon`; what is under test here is
    what the REPL does with what comes back.
    """

    def __init__(self) -> None:
        super().__init__('127.0.0.1', 1)
        self.asked: list[tuple[str, str]] = []

    async def files(self, session_id: str, query: str, limit: int = 40) -> list[dict[str, object]]:
        self.asked.append((session_id, query))
        return [{'path': 'openmirror/tui.py'}, {'path': 'README.md'}]

    async def agree_hook(self, session_id: str, command: str, allow: bool) -> None:
        self.asked.append((session_id, f'hook:{command}:{allow}'))


def make_app(**options) -> tuple[App, io.StringIO, io.StringIO, StubDaemon]:
    out, err = io.StringIO(), io.StringIO()
    terminal = Terminal(out, err, colour=False, width=80, interactive=True)
    api = StubDaemon()
    app = App(api, Options(**options), terminal, renderer=plain())
    app.session_id = 'sess-1'
    return app, out, err, api


def commands_of(app: App) -> list[dict[str, object]]:
    return [command for command in app._held]


def test_an_approval_is_asked_on_stderr_and_the_answer_becomes_a_command():
    """stderr, because `openmirror chat | tee log.txt` should capture what the
    agent said rather than a transcript of somebody typing."""
    app, out, err, _api = make_app()
    app.on_event({
        'type': 'tool.proposed', 'turn_id': 't',
        'call': {'id': 'c1', 'name': 'shell', 'summary': 'rm -rf build'},
        'needs_approval': True,
    })
    assert err.getvalue() == 'Allow shell? [y/N/a(lways)] '
    app.on_key(Key('char', 'n'))
    assert commands_of(app) == [
        {'type': 'tool.deny', 'call_id': 'c1', 'reason': 'declined from the terminal'}
    ]
    assert app._state == 'input'


def test_yes_allows_everything_without_asking_and_says_so():
    app, out, err, _api = make_app(yes=True)
    app.on_event({
        'type': 'tool.proposed', 'call': {'id': 'c1', 'name': 'shell', 'summary': 'anything'},
        'needs_approval': True,
    })
    assert err.getvalue() == ''
    assert commands_of(app) == [{'type': 'tool.approve', 'call_id': 'c1', 'remember': False}]
    assert 'allowing every tool call without asking (--yes)' in tui.banner(Terminal(io.StringIO()), app)


def test_an_always_answer_is_remembered_for_this_session_and_only_this_call():
    """One `a` on `ls` must not approve `rm -rf /`, which is the failure the
    protocol's `remember` scoping exists to prevent."""
    app, _out, _err, _api = make_app()
    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c1', 'name': 'shell', 'summary': 'ls'},
                  'needs_approval': True})
    app.on_key(Key('char', 'a'))
    assert commands_of(app) == [{'type': 'tool.approve', 'call_id': 'c1', 'remember': True}]

    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c2', 'name': 'shell', 'summary': 'ls'},
                  'needs_approval': True})
    assert len(commands_of(app)) == 2, 'the same call should not be asked about twice'
    assert commands_of(app)[1]['remember'] is True

    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c3', 'name': 'shell', 'summary': 'rm -rf /'},
                  'needs_approval': True})
    assert app._state == 'approve', 'a different call must be asked about'


def test_nonsense_at_an_approval_re_asks_rather_than_deciding():
    """Treating `what?` as a refusal turns a typo into a decision about
    somebody's filesystem."""
    app, _out, err, _api = make_app()
    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c1', 'name': 'shell'}, 'needs_approval': True})
    app.on_key(Key('char', 'yolo'))
    assert commands_of(app) == []
    assert app._state == 'approve'
    assert 'y, n, or a' in err.getvalue()


def test_a_permission_question_with_nobody_answers_denies_and_stops_the_turn():
    app, _out, err, _api = make_app()
    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c1', 'name': 'shell'}, 'needs_approval': True})
    app.on_key(Key('eof'))
    kinds = [command['type'] for command in commands_of(app)]
    assert kinds == ['tool.deny', 'turn.interrupt']
    assert 'nobody answered' in err.getvalue()


def test_a_call_the_policy_already_allowed_is_not_asked_about():
    app, _out, err, _api = make_app()
    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c1', 'name': 'read_file'}, 'needs_approval': False})
    assert app._state == 'input'
    assert err.getvalue() == ''


def test_ctrl_c_once_interrupts_and_twice_leaves_with_130():
    app, _out, _err, _api = make_app()
    app.on_event({'type': 'turn.started', 'turn_id': 't', 'text': 'go', 'at': 1.0})
    app.on_key(Key('interrupt'))
    assert commands_of(app) == [{'type': 'turn.interrupt'}]
    assert app._stopping is False
    app.on_key(Key('interrupt'))
    assert app._stopping is True
    assert app.exit_code == 130


def test_ctrl_c_with_typing_in_hand_clears_the_line_first():
    """A key that sometimes stops the agent and sometimes throws away the line
    being typed is worse than either behaviour alone."""
    app, _out, _err, _api = make_app()
    app.line.set('half a thought')
    app.on_key(Key('interrupt'))
    assert app.line.text == ''
    assert app._stopping is False
    app.on_key(Key('interrupt'))
    assert app.exit_code == 130


def test_ctrl_d_leaves_cleanly():
    app, _out, _err, _api = make_app()
    app.on_key(Key('eof'))
    assert (app._stopping, app.exit_code) == (True, 0)


def test_ctrl_l_clears_the_screen():
    app, out, _err, _api = make_app()
    app.on_key(Key('clear'))
    assert '\x1b[2J\x1b[H' in out.getvalue()


def test_a_question_is_answered_by_number():
    app, _out, _err, _api = make_app()
    app.on_event({'type': 'question.asked', 'question_id': 'q1', 'question': 'Which one?',
                  'options': ['postgres', 'sqlite']})
    assert app._state == 'question'
    app.on_key(Key('char', '2'))
    assert commands_of(app) == [{'type': 'question.answer', 'question_id': 'q1', 'answer': 'sqlite'}]


def test_a_hook_is_agreed_over_the_only_rest_call_the_repl_makes():
    """There is no websocket command for hooks, so this is the one exception.

    Deliberately the only one: a client that started doing things over HTTP
    would have to start auditing everything else the daemon exposes.
    """
    import asyncio

    app, _out, _err, api = make_app()

    async def ask(answer: str) -> list[tuple[str, str]]:
        app.on_event({'type': 'hook.approval', 'hook': {'command': 'pytest -q'}})
        assert app._state == 'hook'
        app.on_key(Key('char', answer))
        await asyncio.sleep(0.01)  # the agreement is a coroutine on this loop
        return api.asked

    assert asyncio.run(ask('y')) == [('sess-1', 'hook:pytest -q:True')]
    assert app._state == 'input'
    assert asyncio.run(ask('n'))[-1] == ('sess-1', 'hook:pytest -q:False')


def test_refusing_a_hook_is_remembered_too_by_the_daemon():
    """It is not only yes that has to stick. A question that comes back on
    every tool call is a question people click through."""
    import asyncio

    app, _out, _err, api = make_app()

    async def refuse() -> None:
        app.on_event({'type': 'hook.approval', 'hook': {'command': 'make lint'}})
        app.on_key(Key('enter'))  # blank means no, as it does everywhere else
        await asyncio.sleep(0.01)

    asyncio.run(refuse())
    assert api.asked == [('sess-1', 'hook:make lint:False')]


def test_a_local_command_is_answered_here_and_never_sent():
    app, _out, _err, _api = make_app()
    app.line.set('/status')
    app.on_key(Key('enter'))
    assert commands_of(app) == []
    assert 'session   sess-1' in app.term.out.getvalue()


def test_exit_leaves():
    app, _out, _err, _api = make_app()
    app.line.set('/exit')
    app.on_key(Key('enter'))
    assert app._stopping is True
    assert commands_of(app) == []


def test_mode_change_goes_to_the_daemon_which_is_the_only_one_that_knows_the_modes():
    app, _out, _err, _api = make_app()
    app.line.set('/mode full-auto')
    app.on_key(Key('enter'))
    assert commands_of(app) == [{'type': 'policy.set', 'mode': 'full-auto'}]


def test_a_typed_message_is_echoed_at_once_and_queued_for_the_socket():
    app, out, _err, _api = make_app()
    for char in 'hello':
        app.on_key(Key('char', char))
    app.on_key(Key('enter'))
    assert out.getvalue().endswith('> hello\n') or '> hello' in out.getvalue()
    assert commands_of(app) == [{'type': 'turn.submit', 'text': 'hello'}]


def test_a_system_prompt_rides_with_the_first_turn_only():
    """The daemon has no system-prompt field, so it is prepended once rather
    than silently dropped or silently applied to every turn."""
    app, _out, _err, _api = make_app(system='be terse')
    app.line.set('one')
    app.on_key(Key('enter'))
    app.line.set('two')
    app.on_key(Key('enter'))
    texts = [command['text'] for command in commands_of(app)]
    assert texts == ['be terse\n\none', 'two']


def test_an_image_on_the_command_line_is_attached_to_the_next_message(tmp_path):
    path = tmp_path / 'a.png'
    path.write_bytes(TINY_PNG)
    app, _out, _err, _api = make_app()
    app._pending = attachments_from_paths([path])
    app.line.set('what is this?')
    app.on_key(Key('enter'))
    sent = commands_of(app)[0]
    assert sent['text'] == 'what is this?'
    assert sent['attachments'][0]['media_type'] == 'image/png'


def test_a_trailing_bang_path_is_taken_off_the_message_it_was_typed_with(tmp_path):
    path = tmp_path / 'a.png'
    path.write_bytes(TINY_PNG)
    app, _out, _err, _api = make_app()
    app.line.set(f'what is this? !{path}')
    app.on_key(Key('enter'))
    sent = commands_of(app)[0]
    assert sent['text'] == 'what is this?'
    assert len(sent['attachments']) == 1


def test_a_message_sent_mid_turn_is_held_by_the_daemon_and_the_client_says_so():
    """The server queues it; the client says so. A message that looks sent and
    is neither running nor lost is the outcome this exists to remove."""
    app, out, _err, _api = make_app()
    app.busy = True
    app.line.set('also, run the tests')
    app.on_key(Key('enter'))
    assert commands_of(app)[0]['text'] == 'also, run the tests'
    app.on_event({'type': 'turn.queued', 'waiting': 1})
    assert 'held — it runs when this turn finishes (1 waiting)' in out.getvalue()


def test_the_prompt_is_not_drawn_over_a_streaming_answer():
    """A prompt blinking at the bottom of a streaming answer is the single most
    distracting thing a terminal agent can do."""
    app, out, _err, _api = make_app()
    app.on_event({'type': 'text.delta', 'text': 'working'})
    assert app._prompt_drawn is False
    app.on_event({'type': 'turn.completed', 'stop_reason': 'end_turn', 'at': 2.0})
    assert app._prompt_drawn is True


def test_the_prompt_is_taken_off_the_screen_before_anything_is_printed():
    """The prompt and the transcript share one terminal.

    Anything printed under an undrawn prompt overwrites it, so the erase comes
    first and the redraw after. Asserted on the order, because the order is
    the part that would actually be wrong.
    """
    app, out, _err, _api = make_app()
    app.on_key(Key('char', 'h'))
    assert app._prompt_drawn is True
    out.seek(0)
    out.truncate(0)
    app.on_event({'type': 'tool.proposed', 'call': {'id': 'c', 'name': 'read_file', 'summary': 'a.py'}})
    written = out.getvalue()
    assert written.startswith('\r\x1b[2K')
    assert written.index('\r\x1b[2K') < written.index('● read_file')


def test_a_dropped_socket_says_the_session_carries_on_and_keeps_the_commands():
    """The whole point of a socket that is attached to rather than owned: the
    commands typed during the gap are held, not dropped."""
    app, out, _err, _api = make_app()
    app.send({'type': 'turn.submit', 'text': 'queued while away'})
    app.on_event({'type': 'link', 'state': 'offline', 'attempt': 1})
    assert 'the session carries on without you' in out.getvalue()
    assert commands_of(app) == [{'type': 'turn.submit', 'text': 'queued while away'}]


def test_a_fatal_close_ends_the_process_with_one():
    app, _out, _err, _api = make_app()
    app.on_event({'type': 'error', 'message': 'session gone — the daemon has been restarted',
                  'retryable': False, 'fatal': True})
    assert app.exit_code == 1


def test_a_message_while_the_agent_is_working_does_not_replay_after_a_reconnect():
    """The sequence number is the client's half of the reconnect contract."""
    app, _out, _err, _api = make_app()
    app.on_event({'type': 'turn.started', 'seq': 7, 'turn_id': 't', 'text': 'go', 'at': 1.0})
    assert app.renderer.seq == 7


def test_the_status_line_carries_the_three_facts_that_change_a_turn():
    app, _out, _err, _api = make_app(root='/w')
    app.on_event({'type': 'session.started', 'cwd': '/w', 'model': 'qwen3:8b',
                  'policy': 'ask before anything that writes', 'effort': 'high'})
    line = app.status_line()
    assert line == '/w · qwen3:8b · approval: ask before anything that writes · thinking: high'


def test_the_completion_menu_is_filled_from_the_daemon_and_inserted_at_the_cursor():
    """`@` is the same affordance the composer has, driven by the same
    endpoint, so both clients suggest the same files."""
    import asyncio

    app, out, _err, api = make_app()
    for char in 'see @openmir':
        app.on_key(Key('char', char))
    # The lookup is debounced and asynchronous; run it now rather than wait.
    asyncio.run(app._load_files('openmir'))
    assert api.asked == [('sess-1', 'openmir')]
    assert app._menu_open is True
    app.on_key(Key('tab'))
    assert app.line.text == 'see openmirror/tui.py'


# ---------------------------------------------------------------------------
# Finding or making the session
# ---------------------------------------------------------------------------


class SessionStub(Daemon):
    """Answers the four calls `open_session` makes, and nothing else."""

    def __init__(self, *, live: tuple[str, ...] = (), stored: tuple[str, ...] = ()) -> None:
        super().__init__('127.0.0.1', 1)
        self._live = live
        self._stored = stored
        self.made: dict[str, object] | None = None

    async def stored_sessions(self, limit: int = 20) -> list[dict[str, object]]:
        # Newest first, as the daemon orders them, and honouring the limit —
        # so a test about which one is picked is not really about this stub.
        return [{'id': name} for name in self._stored[:limit]]

    async def live_sessions(self) -> list[dict[str, object]]:
        return [{'id': name} for name in self._live]

    async def resume(self, session_id: str) -> dict[str, object]:
        if session_id not in self._stored:
            raise DaemonError(404, '')
        return {'id': session_id}

    async def create_session(self, **body: object) -> dict[str, object]:
        self.made = body
        return {'id': 'fresh'}


def test_a_session_is_made_when_none_was_named():
    api = SessionStub()
    options = Options(root='/w', model='m', toolset=['files'])
    assert asyncio.run(tui.open_session(api, options)) == {'id': 'fresh'}
    assert api.made == {'root': '/w', 'title': '', 'model': 'm', 'tools': ['files']}


def test_a_live_session_is_reused_rather_than_resumed():
    """Resuming one that is already running would put two readers on one event
    stream, which is two places for the sequence number to drift."""
    api = SessionStub(live=('s1',), stored=('s1',))
    assert asyncio.run(tui.open_session(api, Options(session_id='s1'))) == {'id': 's1'}
    assert api.made is None


def test_continue_takes_the_newest_conversation_and_resumes_it():
    """After a daemon restart the live list is empty and this is the only
    kind there is, which is the whole reason `--continue` exists."""
    api = SessionStub(stored=('s2', 's1'))  # newest first, as the daemon orders them
    assert asyncio.run(tui.open_session(api, Options(continue_=True))) == {'id': 's2'}
    assert api.made is None


def test_continue_with_nothing_on_disk_starts_a_new_one_and_says_so():
    """A first run is not a failure, and refusing would make the flag useless
    on a fresh install."""
    said: list[str] = []
    api = SessionStub()
    assert asyncio.run(tui.open_session(api, Options(continue_=True), note=said.append)) == {'id': 'fresh'}
    assert said == ['no conversation on disk yet — starting a new one']


def test_an_unknown_session_id_says_so_rather_than_reporting_a_status_code():
    """A bare 404 tells somebody nothing about which of the two ids was wrong."""
    api = SessionStub()
    with pytest.raises(DaemonError) as caught:
        asyncio.run(tui.open_session(api, Options(session_id='nope')))
    assert caught.value.status == 404
    assert caught.value.detail == "no conversation called 'nope'"


# ---------------------------------------------------------------------------
# Which stream the client's own words go to
# ---------------------------------------------------------------------------


def test_status_the_client_generates_goes_to_stderr_when_there_is_no_repl():
    """`openmirror chat --prompt x > answer.txt` should leave a file with the
    answer in it, not with a session banner and a note above it."""
    app, out, err, _api = make_app()
    app.show_input = False
    app._note('no conversation on disk yet')
    app.on_event({'type': 'session.started', 'cwd': '/w', 'model': 'm', 'policy': 'ask', 'effort': None})
    assert out.getvalue() == ''
    assert 'no conversation on disk yet' in err.getvalue()
    assert 'session stub' not in out.getvalue()


def test_the_same_words_go_to_stdout_when_there_is_a_prompt_to_print_them_under():
    app, out, _err, _api = make_app()
    app._note('connected')
    assert 'connected' in out.getvalue()


def test_an_error_reaches_stderr_and_the_answer_reaches_stdout():
    """So that `openmirror chat --prompt x | tail -1` shows the answer and not
    a complaint about something that happened an hour ago."""
    app, out, err, _api = make_app()
    app.on_event({'type': 'text.delta', 'text': 'the answer'})
    app.on_event({'type': 'error', 'message': 'a provider fell over'})
    assert 'the answer' in out.getvalue()
    assert 'a provider fell over' in err.getvalue()
    assert 'a provider fell over' not in out.getvalue()


def test_a_notice_raised_mid_sentence_waits_for_the_sentence_to_finish():
    """Printed in the middle of a paragraph, it lands in the middle of a
    sentence and the reader has to work out which of two streams they are on."""
    app, out, _err, _api = make_app()
    app.busy = True
    app.on_event({'type': 'text.delta', 'text': 'it listens on port '})
    app.interrupt()
    assert 'interrupting' not in out.getvalue(), 'printed into the middle of the answer'
    app.on_event({'type': 'text.delta', 'text': '8477'})
    app.on_event({'type': 'turn.completed', 'stop_reason': 'interrupted', 'at': 2.0})
    text = out.getvalue()
    assert text.index('8477') < text.index('interrupting')


def test_attach_is_idempotent_so_a_waiter_is_never_left_on_a_stale_socket():
    """`serve` is a coroutine, so it does not run when it is created.

    A caller that waited on the connection had to hold the same object the
    reader would, or it would be waiting on a placeholder that never opens.
    """
    app, _out, _err, _api = make_app()
    app.session_id = 'sess-1'
    first = app.attach()
    assert first.session_id == 'sess-1'
    assert app.attach() is first


def test_a_scripted_run_whose_socket_never_opens_says_so_and_exits_one(monkeypatch):
    """The health check answered a moment ago, so this is a daemon that went
    away in between — not one that is thinking. Waiting half an hour for it is
    worse than saying so."""
    monkeypatch.setattr(tui, 'CONNECT_TIMEOUT', 0.2)
    app, _out, err, _api = make_app()
    dead = tui.Socket(Daemon('127.0.0.1', 1), 'sess-1')
    app.attach = lambda: dead
    app.serve = lambda: asyncio.sleep(3600)
    assert asyncio.run(tui._one_turn(app, 'hello')) == 1
    assert 'never opened' in err.getvalue()

"""One agent session: a conversation that can act on the machine.

The shape is a loop — ask the model, run what it asks for, give it the
results, ask again — and the interesting parts are all in what happens
between those steps.

**Approval suspends the loop rather than skipping the call.** When a tool
needs a human, the turn stops on a future and waits. It does not proceed
optimistically and it does not time out into a default, because both of those
turn "are you sure?" into a formality.

**A denial is a tool result, not an error.** The model is told, in the result,
that the person said no and why. That lets it try a different approach, which
is what a person who said no usually wants. Ending the turn instead just
makes them say the same thing again in words.

**Interruption cancels in-flight work.** Killing the task cancels the
provider stream and any running subprocess through normal cancellation, and
the session survives to take the next message with its history intact.
Background work is the exception, on purpose: see agent/tasks.py.

**A subagent is a session too — one with nothing of its own but its
conversation.** It has its own messages and its own, narrower, tool list. Its
events go into this session's log, tagged with the call that started it; its
approvals wait in this session's table; it answers to this session's policy.
So a person watching sees one transcript and answers one set of prompts, and
a mode changed halfway through applies to every agent in the session at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openmirror.agent import prompt as prompt_mod
from openmirror.agent import windows
from openmirror.agent.approval import ApprovalPolicy, Decision, Mode
from openmirror.agent.tasks import TaskBoard
from openmirror.agent.tools.base import FILE_WRITERS, Assessment, Output, Tool, ToolContext, ToolError, truncate
from openmirror.protocol.agent import (
    AgentError,
    ContextCompacted,
    PolicyChanged,
    QuestionAsked,
    Risk,
    SessionEnded,
    SessionStarted,
    TaskUpdated,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCompleted,
    ToolDenied,
    ToolOutputDelta,
    ToolProposed,
    ToolResult,
    ToolStarted,
    ToolStatus,
    TurnCompleted,
    TurnStarted,
)
from openmirror.providers.base import (
    ChatRequest,
    ImageBlock,
    Message,
    StreamDone,
    StreamText,
    StreamThinking,
    StreamToolUse,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from openmirror.providers.reasoning import normalise

log = logging.getLogger(__name__)

# How many model round trips one user message may cost. A ceiling rather than
# a target: hitting it means the agent is looping, and stopping with an
# explanation beats burning tokens until someone notices the bill.
MAX_STEPS = 60

# How many events one session keeps for replay. Roughly an afternoon of work.
EVENT_LOG_LIMIT = 5000

# How many times one turn may make the *same* call with the *same* arguments
# before it is told, and then stopped.
#
# Repeats are not automatically wrong: a screenshot taken twice is two
# different pictures, and running the tests again after an edit is the whole
# point. So the first few are left alone. What this catches is the other
# thing — a model that has lost the thread and is calling something with no
# side effects over and over, which a step limit only catches after sixty
# rounds. Measured on a 12B model asked to browse: thirty identical calls to
# a tool that reports the screen size, 59,000 tokens of input, and an answer
# saying it could not browse. The nudge at REPEAT_WARN is usually enough;
# REPEAT_STOP is for when it is not.
REPEAT_WARN = 3
REPEAT_STOP = 6

# Things typed into the composer that are about the conversation rather than
# part of it. A slash followed by a skill's name runs the skill; a slash
# followed by anything else is ordinary text — "/etc/hosts is wrong" is a
# sentence, not a command.
COMMANDS = {
    'compact': 'Summarise the conversation so far to make room. Anything after it says what to keep.',
    'clear': 'Forget the conversation and start again, in the same session and folder.',
    'think': 'How hard the model thinks from now on: off, low, medium, high, xhigh, max, or default. '
             'On its own, says what it is now.',
}

_SLASH = re.compile(r'/([A-Za-z0-9_:-]+)(?:\s+(.*))?$', re.S)


def _slash(text: str) -> tuple[str, str]:
    """(name, arguments) for a line typed as a command, or ('', '')."""
    match = _SLASH.match(text.strip())
    if not match:
        return '', ''
    return match.group(1).lower(), (match.group(2) or '').strip()


@dataclass(slots=True)
class Report:
    """What a subagent hands back to the session that started it."""

    text: str
    ok: bool = True
    stop_reason: str = 'end_turn'
    tool_calls: int = 0
    duration_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)


def _denial_text(call: ToolCall, reason: str) -> str:
    """What the model is told when a person says no.

    For most tools, "try another way" is right — the approach was wrong, not
    the goal. For money and secrets it is exactly wrong, and observably so:
    told to place an order and refused, a local model immediately clicked a
    different button and then tried to fill in the card fields. It was not
    being devious; it was following the instruction to find another route.

    So a refusal on those two is terminal for that goal. Nothing else about
    the turn stops — it can carry on with everything else it was asked to do.
    """
    said = reason or 'No reason given.'
    if call.risk in (Risk.PURCHASE, Risk.CREDENTIAL):
        return (
            f'Refused: {said}\n\n'
            'This is final. Do not attempt it another way, do not try a different button, '
            'and do not try to enter the details yourself. Stop pursuing this particular '
            'action, tell the person plainly what you were about to do and why it stopped, '
            'and carry on with anything else they asked for.\n\n'
            'Note that being able to do this at all is not the question — you are permitted '
            'to buy things and to type secrets when the person agrees. They did not agree to '
            'this one, and asking again in a different shape is how a "no" gets worn down.'
        )
    return (
        f'The user declined this. {said} '
        'Do not retry it as-is; either do it another way or ask them what they want.'
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + ' …'


def render_transcript(messages: list[Message], budget: int) -> str:
    """The conversation as text, for a model to summarise.

    Tool results are clipped hard. The summary needs to know that a file was
    read and what mattered in it, not the file — and a transcript carrying
    every result in full would be bigger than the context it exists to relieve.
    """
    lines: list[str] = []
    for message in messages:
        speaker = 'Person' if message.role == 'user' else 'Agent'
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                lines.append(f'{speaker}: {block.text.strip()}')
            elif isinstance(block, ToolUseBlock):
                args = json.dumps(block.input, ensure_ascii=False, default=str)
                lines.append(f'Agent used {block.name}: {_clip(args, 400)}')
            elif isinstance(block, ToolResultBlock):
                lines.append(f'  {"failed" if block.is_error else "result"}: {_clip(block.content.strip(), 1200)}')
            elif isinstance(block, ImageBlock):
                lines.append(f'{speaker}: [an image]')
    text = '\n'.join(lines)
    if len(text) <= budget:
        return text
    # The start says what was asked and the end says where things are. The
    # middle is the part a summary can best afford to lose.
    head = budget // 4
    tail = budget - head
    return f'{text[:head]}\n\n[… {len(text) - budget} characters from the middle left out …]\n\n{text[-tail:]}'


class AgentSession:
    def __init__(
        self,
        *,
        session_id: str | None = None,
        root: Path,
        provider: Any,
        model: str,
        tools: list[Tool],
        effort: str | None = None,
        policy: ApprovalPolicy | None = None,
        system_prompt: str = '',
        max_steps: int = MAX_STEPS,
        env: dict[str, str] | None = None,
        title: str = '',
        context_provider: Any = None,
        on_user_text: Any = None,
        confined: bool = True,
        browser: Any = None,
        checkpoints: Any = None,
        stage: Any = None,
        agent_kinds: dict[str, Any] | None = None,
        skills: dict[str, Any] | None = None,
        compact_at: int = 0,
        after_write: list[Any] | None = None,
        project_context: str = '',
        parent: AgentSession | None = None,
        agent_id: str = '',
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:16]
        self.title = title
        self.root = root.resolve()
        self.cwd = self.root
        # False means the whole filesystem. Declared at session creation and
        # reported in session.started, so a client can say so plainly.
        self.confined = confined
        # Owned here so it dies with the session: an abandoned Chromium is
        # 200 MB, and sessions outlive their client by design.
        self.browser = browser
        # Undo for this session's own file edits. See checkpoint.py for what it
        # does not cover — a shell command can write anywhere.
        self.checkpoints = checkpoints
        # Where its hands are, when it has any. Owned here for the same reason
        # the browser is: a virtual stage is an X server, and an X server
        # nobody shut down is an X server still running tomorrow.
        self.stage = stage
        self.provider = provider
        self.model = model
        # How hard the model thinks: off … max, or None for its own default.
        # One level for every provider — each adapter spells it the way its
        # host accepts — so switching provider keeps the setting's meaning.
        self.effort = normalise(effort)
        self.policy = policy or ApprovalPolicy()
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.env = env or {}

        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        self.messages: list[Message] = []

        # The kinds of agent the `agent` tool can start, and the skills `/name`
        # can run. Both empty for a subagent.
        self.agent_kinds = agent_kinds or {}
        self.skills = skills or {}
        # The project's own instructions, kept so a subagent can be given them
        # too: it works in the same project, under the same conventions.
        self.project_context = project_context
        # When to summarise the conversation to make room, in estimated tokens.
        # 0 is never, which is what a subagent gets: it lives for one task.
        self.compact_at = compact_at
        # Called with the path a tool has just written; whatever comes back is
        # added to that tool's result. Today that is a language server saying
        # what the edit broke, which the model should hear before it moves on.
        self.after_write = list(after_write or [])

        # Set for a subagent: the session it reports to, and the id of the call
        # that started it, which its events carry.
        self.parent = parent
        self.agent_id = agent_id
        self.tool_calls = 0

        # Called with the user's message before each turn; whatever it returns
        # is appended to the system prompt for that turn only. A hook rather
        # than a memory object, so the session does not have to know what
        # memory is — or that there is any.
        self.context_provider = context_provider
        # Called with the user's message after the turn is under way, for
        # anything that wants to learn from it. Never awaited on the critical
        # path: a slow write must not delay the answer.
        self.on_user_text = on_user_text
        self._turn_context: str = ''
        # Images produced by tools during the current step, attached to the
        # message carrying their results. Every provider accepts images on a
        # user turn; none of them accept one inside a tool result on all three
        # dialects, so this is the portable place to put them.
        self._pending_images: list[tuple[str, str]] = []

        # Events are logged and fanned out rather than queued to one reader.
        # A browser tab closing must not destroy the session — the agent may
        # be four minutes into a build — so a client detaches and reattaches,
        # and asks for everything after the last sequence number it saw.
        self._log: list[tuple[int, Any]] = []
        self._seq = 0
        self._subs: set[asyncio.Queue[tuple[int, Any]]] = set()
        self.last_active: float = time.time()

        # (tool, arguments) -> how many times this turn has made that call.
        # Emptied per turn: a repeat across turns is a person asking twice.
        self._repeats: dict[str, int] = {}

        self._approvals: dict[str, asyncio.Future[tuple[bool, str, bool]]] = {}
        self._questions: dict[str, asyncio.Future[str]] = {}
        # Every tool-call id this session has used. Some providers number calls
        # per response — Ollama says `call_1` every time — and an id is what an
        # approval is filed under, so two waiting under one id would be one
        # answer for two questions.
        self._call_ids: set[str] = set()
        if parent is not None:
            # Shared, not copied: a subagent's approval is waiting on the same
            # person, and `waiting_on` has to see it or the reaper will not.
            self._approvals = parent._approvals
            self._questions = parent._questions
            self._call_ids = parent._call_ids

        # Background work. A subagent has none: what it leaves running would
        # outlive the only thing that knew why it was started.
        self.tasks: TaskBoard | None = TaskBoard(on_change=self._task_changed) if parent is None else None
        # Things the model should hear at its next request that arrived
        # between requests — a background task finishing, mostly.
        self._notes: list[str] = []

        # Where the running turn begins in `messages`. Automatic compaction
        # summarises what came before it and never the turn itself, so the
        # work in hand is never cut in half.
        self._turn_start = 0
        # Input tokens of the last request, as the provider counted them.
        self._last_input = 0
        # Cumulative for the session, from the provider's own accounting. Two
        # counters and no money: a dollar figure is out of date the moment a
        # provider changes a price, and somebody reads it as a bill.
        self._total_in = 0
        self._total_out = 0
        # Set once a turn has been compacted, so a turn that is large on its
        # own does not summarise the summary on every step.
        self._fresh_summary = False

        self._turn: asyncio.Task[None] | None = None
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._emit(
            SessionStarted(
                session_id=self.id,
                cwd=str(self.cwd),
                model=self.model,
                effort=self.effort,
                policy=self.policy.describe() + ('' if self.confined else '  ·  UNCONFINED: the whole filesystem'),
                tools=sorted(self.tools),
            )
        )

    async def close(self, reason: str = 'closed') -> None:
        if self._closed:
            return
        self._closed = True
        self.interrupt()

        if self.tasks is not None:
            # Before the waiting questions are answered: a background agent
            # told "no" would otherwise carry on for a moment in a session
            # that no longer exists.
            try:
                await self.tasks.close()
            except Exception:  # noqa: BLE001
                log.debug('background tasks did not close cleanly', exc_info=True)

        # Anything still waiting on a human is resolved rather than left to
        # hang: a closed session must not leak a suspended turn.
        for fut in list(self._approvals.values()):
            if not fut.done():
                fut.set_result((False, 'session closed', False))
        for fut in list(self._questions.values()):
            if not fut.done():
                fut.set_result('(the session was closed before this was answered)')

        # Tools that hold something open — a language server — close with the
        # session that owns them. A subagent shares its tools and never gets
        # here, which is right: they are not its to close.
        for tool in self.tools.values():
            closer = getattr(tool, 'close', None)
            if closer is not None and inspect.iscoroutinefunction(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    log.debug('tool %s did not close cleanly', tool.name, exc_info=True)

        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                log.debug('browser did not close cleanly', exc_info=True)

        if self.stage is not None:
            try:
                # After the browser: closing the display out from under a
                # Chromium running on it leaves a process with nowhere to draw.
                self.stage.close()
            except Exception:  # noqa: BLE001
                log.debug('stage did not close cleanly', exc_info=True)

        await self._emit(SessionEnded(session_id=self.id, reason=reason))

    async def events(self, since: int = 0) -> AsyncIterator[Any]:
        """Subscribe, replaying anything after `since`.

        The queue is registered *before* the replay so nothing emitted during
        it is lost; duplicates are then filtered by sequence number, which is
        the only ordering that has no gap.
        """
        queue: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        self._subs.add(queue)
        last = since
        try:
            for seq, event in list(self._log):
                if seq <= last:
                    continue
                last = seq
                yield event
                if isinstance(event, SessionEnded):
                    return

            while True:
                seq, event = await queue.get()
                if seq <= last:
                    continue
                last = seq
                yield event
                if isinstance(event, SessionEnded):
                    return
        finally:
            self._subs.discard(queue)

    async def _emit(self, event: Any) -> None:
        if self.parent is not None:
            # A subagent has no log of its own. Its events are the parent's,
            # marked with the call that started it, so there is one sequence
            # to replay and one place for a client to find an approval.
            event.agent = self.agent_id
            event.session_id = self.parent.id
            await self.parent._emit(event)
            return

        self._seq += 1
        self.last_active = time.time()
        event.seq = self._seq
        self._log.append((self._seq, event))
        # Bounded, because a session left running for a day should not grow
        # without limit. A client that has been away longer than this reattaches
        # to a partial history, which is why the cap is generous.
        if len(self._log) > EVENT_LOG_LIMIT:
            del self._log[: len(self._log) - EVENT_LOG_LIMIT]
        for queue in list(self._subs):
            queue.put_nowait((self._seq, event))

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def attached(self) -> int:
        return len(self._subs)

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def waiting_on(self) -> str | None:
        """What a human owes this session, if anything.

        A reattaching client needs this: a session suspended on an approval
        looks identical to an idle one until you know it is waiting.
        """
        if self._approvals:
            return 'approval'
        if self._questions:
            return 'question'
        return None

    def commands(self) -> list[dict[str, str]]:
        """What `/` can be followed by here, for a client's menu."""
        out = [{'name': name, 'description': text, 'kind': 'command', 'hint': ''} for name, text in COMMANDS.items()]
        out += [
            {'name': s.name, 'description': s.description, 'kind': 'skill', 'hint': s.argument_hint}
            for s in sorted(self.skills.values(), key=lambda s: s.name)
            if s.name not in COMMANDS
        ]
        return out

    # -- client commands ----------------------------------------------------

    def submit(self, text: str, attachments: list[dict[str, Any]] | None = None) -> str:
        """Start a turn. Returns its id immediately; the work happens in a task."""
        if self._closed:
            raise RuntimeError('session is closed')
        if self._turn and not self._turn.done():
            raise RuntimeError('a turn is already running — interrupt it first')

        turn_id = uuid.uuid4().hex[:16]
        name, arguments = _slash(text)

        if name in COMMANDS:
            self._turn = asyncio.create_task(self._run_command(turn_id, text, name, arguments))
            return turn_id

        prompt = text
        skill = self.skills.get(name) if name else None
        if skill is not None:
            from openmirror.agent.skills import render

            # The person sees what they typed; the model gets the skill. Both
            # are true descriptions of the turn, for different readers.
            ran = f'/{skill.name}' + (f' {arguments}' if arguments else '')
            # Said outright that it is already loaded: shown the skill tool as
            # well, a 12B model loaded the same skill again before starting,
            # and then stopped short of the work.
            prompt = (
                f'The person ran {ran}. The skill\'s instructions are below and already loaded — do '
                f'not load it again with the skill tool. Start on them now.\n\n{render(skill, arguments)}'
            )

        self._turn = asyncio.create_task(self._run_turn(turn_id, text, attachments or [], prompt=prompt))
        return turn_id

    def approve(self, call_id: str, remember: bool = False) -> bool:
        fut = self._approvals.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result((True, '', remember))
        return True

    def deny(self, call_id: str, reason: str = '') -> bool:
        fut = self._approvals.get(call_id)
        if fut is None or fut.done():
            return False
        fut.set_result((False, reason, False))
        return True

    def answer(self, question_id: str, answer: str) -> bool:
        fut = self._questions.get(question_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    def interrupt(self) -> bool:
        if self._turn and not self._turn.done():
            self._turn.cancel()
            return True
        return False

    async def set_mode(self, mode: Mode | str) -> None:
        """Change the approval mode, and tell everyone watching.

        Through the event log rather than a reply to whoever asked: a mode is a
        fact about the session, and a second client attached to it — or the
        same one reattaching tomorrow — should see it change too.
        """
        new = Mode(mode)
        if new is Mode.PLAN and self.policy.mode is not Mode.PLAN:
            self.policy.previous = self.policy.mode
        self.policy.mode = new
        await self._emit(PolicyChanged(
            session_id=self.id, mode=new.value, policy=self.policy.describe(), effort=self.effort,
        ))

    async def set_effort(self, level: str | None) -> None:
        """Change how hard the model thinks, from the next request on.

        A live control for the same reason the approval mode is: whether a
        problem deserves deliberation is something you find out while watching
        the model work on it. ``default`` or an empty value hands it back to
        the model; anything that is not a level is refused rather than guessed.
        Announced through the event log so every attached client sees it.
        """
        raw = (level or '').strip().lower()
        if raw in ('', 'default', 'auto'):
            self.effort = None
        else:
            chosen = normalise(raw)
            if chosen is None:
                raise ValueError(f'not a thinking level: {level!r} — use off, low, medium, high, xhigh, max or default')
            self.effort = chosen
        await self._emit(PolicyChanged(
            session_id=self.id, mode=self.policy.mode.value, policy=self.policy.describe(), effort=self.effort,
        ))

    # -- the loop -----------------------------------------------------------

    async def _run_turn(
        self, turn_id: str, text: str, attachments: list[dict[str, Any]], prompt: str | None = None
    ) -> None:
        await self._emit(TurnStarted(session_id=self.id, turn_id=turn_id, text=text))

        if self.checkpoints is not None:
            # Opened before anything runs, so the snapshot is of the tree as it
            # was when the person asked, not partway through the answer.
            self.checkpoints.begin(turn_id, text[:80])

        # Recall runs before the first request, so what is remembered is in
        # front of the model from the outset rather than arriving a step late.
        self._turn_context = ''
        if self.context_provider is not None:
            try:
                self._turn_context = await self.context_provider(text) or ''
            except Exception:  # noqa: BLE001
                # Never fatal. Not remembering is a worse turn; failing is no turn.
                log.exception('context provider failed; continuing without it')

        if self.on_user_text is not None:
            # Fire and forget: this is bookkeeping, not part of the answer.
            asyncio.create_task(self._safe_learn(text))

        self._repeats = {}
        self._fresh_summary = False

        blocks: list[Any] = [TextBlock(text=prompt or text)]
        for att in attachments:
            if att.get('type') == 'image' and att.get('data'):
                blocks.append(ImageBlock(data=att['data'], media_type=att.get('media_type', 'image/png')))
        self._turn_start = len(self.messages)
        self.messages.append(Message(role='user', content=blocks))

        usage_total = {'input_tokens': 0, 'output_tokens': 0}
        stop_reason = 'end_turn'

        try:
            stop_reason = await self._loop(turn_id, usage_total)

        except asyncio.CancelledError:
            stop_reason = 'interrupted'
            if self.checkpoints is not None:
                # Kept, not discarded: a turn cut off halfway is the one most
                # likely to have left the tree somewhere nobody wanted.
                self.checkpoints.commit()
            # Recorded in the history so the next turn's context shows the work
            # was cut off rather than completed.
            self.messages.append(
                Message(role='assistant', content=[TextBlock(text='[interrupted by the user]')])
            )
            await self._emit(
                TurnCompleted(session_id=self.id, turn_id=turn_id, stop_reason='interrupted', usage=usage_total)
            )
            raise

        except Exception as exc:  # noqa: BLE001 - the turn boundary is where errors become events
            log.exception('turn %s failed', turn_id)
            stop_reason = 'error'
            await self._emit(
                AgentError(
                    session_id=self.id,
                    turn_id=turn_id,
                    message=str(exc),
                    retryable=isinstance(exc, (asyncio.TimeoutError, ConnectionError)),
                )
            )

        if self.checkpoints is not None:
            self.checkpoints.commit()

        # The context report is computed here rather than on request because
        # the point is to know it at the moment the turn ended — "how full was
        # it before that" is a different and much less useful number.
        await self._emit(
            TurnCompleted(
                session_id=self.id,
                turn_id=turn_id,
                stop_reason=stop_reason,
                usage=usage_total,
                context=self.context_report(),
            )
        )

    async def _loop(self, turn_id: str, usage: dict[str, int]) -> str:
        """Ask, run, answer, until the model stops asking. Returns why it stopped.

        The same loop for a turn and for a subagent's task: the only
        differences between the two are what goes in before it and what is
        done with what comes out.
        """
        nudged = False
        for _step in range(self.max_steps):
            await self._maybe_compact(turn_id)
            assistant_blocks, calls, done = await self._one_exchange(turn_id)

            for key in usage:
                usage[key] += done.usage.get(key, 0)
            self._total_in += done.usage.get('input_tokens', 0) or 0
            self._total_out += done.usage.get('output_tokens', 0) or 0
            if done.usage.get('input_tokens'):
                self._last_input = done.usage['input_tokens']

            if assistant_blocks:
                self.messages.append(Message(role='assistant', content=assistant_blocks))

            said = any(isinstance(b, TextBlock) and b.text.strip() for b in assistant_blocks)
            if not calls and not said and not nudged and len(self.messages) > 1:
                # A reply with nothing in it — no words, no call, sometimes a
                # little reasoning and then nothing. Measured on gemma4:12b,
                # twice in one live run: straight after a plan was approved,
                # and halfway down a to-do list, each time ending the turn in
                # silence with the work not done. Silence is never the answer
                # a person wanted, so the model is told once and asked again;
                # a second silence is taken as the end.
                nudged = True
                self._nudge()
                continue

            if not calls:
                return 'end_turn'

            self._pending_images = []
            results = await self._run_calls(turn_id, calls)

            # Tool results go back as a user turn: that is where every
            # provider in use expects them, Anthropic and OpenAI alike.
            blocks: list[Any] = list(results)
            for data, media_type in self._pending_images:
                blocks.append(ImageBlock(data=data, media_type=media_type))
            self._pending_images = []
            self.messages.append(Message(role='user', content=blocks))

        await self._emit(
            AgentError(
                session_id=self.id,
                turn_id=turn_id,
                message=f'Stopped after {self.max_steps} steps without finishing.',
            )
        )
        return 'max_steps'

    async def _one_exchange(self, turn_id: str) -> tuple[list[Any], list[ToolCall], StreamDone]:
        """One request to the model, streamed."""
        if self._notes and self.messages and self.messages[-1].role == 'user':
            # Delivered in the conversation, on the message about to be sent,
            # so the model reads them in the place it is already reading.
            notes, self._notes = self._notes, []
            self.messages[-1].content.append(TextBlock(text='\n\n'.join(notes)))

        system = self.system_prompt
        if self.policy.mode is Mode.PLAN:
            system = f'{system}\n\n{prompt_mod.PLAN_MODE}' if system else prompt_mod.PLAN_MODE
        if self._turn_context:
            system = f'{system}\n\n{self._turn_context}' if system else self._turn_context

        req = ChatRequest(
            model=self.model,
            messages=self.messages,
            system=system or None,
            effort=self.effort,
            tools=[
                ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
                for t in self._visible_tools()
            ],
        )

        assistant_blocks: list[Any] = []
        calls: list[ToolCall] = []
        text_buf: list[str] = []
        think_buf: list[str] = []
        done = StreamDone()

        async for event in self.provider.stream(req):
            if isinstance(event, StreamText):
                text_buf.append(event.text)
                await self._emit(TextDelta(session_id=self.id, turn_id=turn_id, text=event.text))
            elif isinstance(event, StreamThinking):
                think_buf.append(event.text)
                await self._emit(ThinkingDelta(session_id=self.id, turn_id=turn_id, text=event.text))
            elif isinstance(event, StreamToolUse):
                call_id = event.id or f'call_{uuid.uuid4().hex[:8]}'
                if call_id in self._call_ids:
                    call_id = f'{call_id}_{uuid.uuid4().hex[:6]}'
                self._call_ids.add(call_id)
                calls.append(ToolCall(id=call_id, name=event.name, arguments=event.input))
            elif isinstance(event, StreamDone):
                done = event

        # Thinking first: providers that accept it back require it before the
        # text and tool blocks of the same turn.
        if think_buf:
            assistant_blocks.append(ThinkingBlock(text=''.join(think_buf)))
        if text_buf:
            assistant_blocks.append(TextBlock(text=''.join(text_buf)))
        for call in calls:
            assistant_blocks.append(ToolUseBlock(id=call.id, name=call.name, input=call.arguments))

        return assistant_blocks, calls, done

    def _nudge(self) -> None:
        note = TextBlock(text=(
            '[You ended that reply without saying anything or calling a tool. If the work is finished, '
            'say what you did. If it is not, carry on with the next step now.]'
        ))
        # On the message already waiting, when there is one, rather than as a
        # second message from the same side: not every provider accepts two
        # in a row.
        if self.messages and self.messages[-1].role == 'user':
            self.messages[-1].content.append(note)
        else:
            self.messages.append(Message(role='user', content=[note]))

    def _visible_tools(self) -> list[Tool]:
        """The tools the model is shown on this request.

        Decided per request because the mode is live. While planning, the
        tools that exist only to change files are hidden rather than offered
        and refused, and the way out of plan mode appears; the rest of the
        time that way out is hidden, because a model shown an exit from a mode
        it is not in will try to take it.
        """
        planning = self.policy.mode is Mode.PLAN
        return [
            t for t in self.tools.values()
            if not (t.name == 'propose_plan' and not planning) and not (planning and t.name in FILE_WRITERS)
        ]

    async def _safe_learn(self, text: str) -> None:
        try:
            await self.on_user_text(text)
        except Exception:  # noqa: BLE001
            log.exception('learning from the message failed')

    async def _run_calls(self, turn_id: str, calls: list[ToolCall]) -> list[ToolResultBlock]:
        """Assess, approve and run each call, in order.

        Sequential on purpose. Two tools running at once can edit the same file
        or race on the same directory, and the model has no way to know they
        will overlap when it proposes them.

        The one exception is a run of `agent` calls, which go together: running
        them at once is the reason to have asked for several. They work on the
        same tree, and the read-before-write rule is what makes that safe
        enough — each agent reads under its own id, so one agent's write is a
        change on disk to every other, and an edit based on the old contents
        is refused rather than applied over the top.
        """
        results: list[ToolResultBlock] = []
        i = 0
        while i < len(calls):
            j = i
            while j < len(calls) and calls[j].name == 'agent' and not calls[j].arguments.get('background'):
                j += 1
            if j - i > 1:
                results.extend(await asyncio.gather(*(self.invoke(c, turn_id) for c in calls[i:j])))
                i = j
                continue
            results.append(await self.invoke(calls[i], turn_id))
            i += 1
        return results

    async def invoke(self, call: ToolCall, turn_id: str) -> ToolResultBlock:
        """Assess one call, get it approved, run it, and report all of it.

        Public, and used by two callers that look nothing alike: the loop
        above, and the realtime gateway, where the model is upstream and calls
        tools over its own protocol rather than through a provider stream.

        That sharing is the point. A second execution path would be a second
        place for the approval policy to be applied — and the one that gets it
        wrong is the one nobody is watching, which in the realtime case is a
        model talking to a person with no transcript in front of them. Going
        through here means the risk grading, the suspension on a human, the
        remembered approvals, the checkpoint and the events a UI renders are
        all the same code in both.
        """
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResultBlock(
                tool_use_id=call.id,
                content=f'No such tool: {call.name}. Available: {", ".join(sorted(self.tools))}',
                is_error=True,
            )
        self.tool_calls += 1

        ctx = self._context(turn_id, call.id)

        try:
            assessment = tool.assess(call.arguments, ctx)
        except Exception as exc:  # noqa: BLE001
            assessment = Assessment(risk=Risk.READ, summary='', invalid=str(exc))

        if assessment.invalid:
            return ToolResultBlock(
                tool_use_id=call.id, content=f'Invalid call: {assessment.invalid}', is_error=True
            )

        call.risk = assessment.risk
        call.summary = assessment.summary

        stuck, why_stuck = self._note_repeat(call)
        if stuck is not None:
            # Announced as a proposal that was then refused, rather than
            # returned silently. A client that is only shown the model's own
            # words sees a turn go quiet for no reason; this way the loop is
            # visible in the transcript as the thing that stopped it.
            await self._emit(
                ToolProposed(session_id=self.id, turn_id=turn_id, call=call, needs_approval=False)
            )
            await self._emit(
                ToolDenied(session_id=self.id, turn_id=turn_id, call_id=call.id, reason=why_stuck)
            )
            return stuck

        decision, why = self.policy.decide(call)

        if decision is Decision.DENY:
            call.status = ToolStatus.DENIED
            await self._emit(ToolDenied(session_id=self.id, turn_id=turn_id, call_id=call.id, reason=why))
            return ToolResultBlock(tool_use_id=call.id, content=f'Not permitted: {why}', is_error=True)

        await self._emit(
            ToolProposed(
                session_id=self.id,
                turn_id=turn_id,
                call=call,
                needs_approval=decision is Decision.ASK,
            )
        )

        if decision is Decision.ASK and call.name != 'ask_user':
            allowed, reason, remember = await self._await_approval(call.id)
            if not allowed:
                call.status = ToolStatus.DENIED
                await self._emit(
                    ToolDenied(session_id=self.id, turn_id=turn_id, call_id=call.id, reason=reason)
                )
                return ToolResultBlock(
                    tool_use_id=call.id, content=_denial_text(call, reason), is_error=True
                )
            if remember:
                self.policy.remember(call)

        return await self._execute(turn_id, tool, call, ctx)

    async def _execute(self, turn_id: str, tool: Tool, call: ToolCall, ctx: ToolContext) -> ToolResultBlock:
        call.status = ToolStatus.RUNNING
        await self._emit(ToolStarted(session_id=self.id, turn_id=turn_id, call_id=call.id))
        started = time.monotonic()

        images: list[tuple[str, str]] = []
        try:
            output = await tool.run(call.arguments, ctx)
            ok, content, display, truncated = True, output.content, output.display, output.truncated
            images = output.images
        except ToolError as exc:
            # Expected failure: the model reads it and adjusts.
            ok, content, display, truncated = False, str(exc), None, False
        except asyncio.CancelledError:
            call.status = ToolStatus.CANCELLED
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception('tool %s failed', call.name)
            ok, content, display, truncated = False, f'{type(exc).__name__}: {exc}', None, False

        if ok and self.after_write and call.risk in (Risk.WRITE, Risk.DESTRUCTIVE) and display and display.get('path'):
            for hook in self.after_write:
                try:
                    extra = await hook(Path(display['path']))
                except Exception:  # noqa: BLE001 - a hook failing must not fail the write that succeeded
                    log.debug('after-write hook failed', exc_info=True)
                    extra = ''
                if extra:
                    content = f'{content}\n\n{extra}'

        duration = int((time.monotonic() - started) * 1000)
        call.status = ToolStatus.COMPLETED if ok else ToolStatus.FAILED

        await self._emit(
            ToolCompleted(
                session_id=self.id,
                turn_id=turn_id,
                result=ToolResult(
                    id=call.id,
                    name=call.name,
                    ok=ok,
                    content=content,
                    display=display,
                    duration_ms=duration,
                    truncated=truncated,
                ),
            )
        )
        self._pending_images.extend(images)
        return ToolResultBlock(tool_use_id=call.id, content=content, is_error=not ok)

    def _note_repeat(self, call: ToolCall) -> tuple[ToolResultBlock | None, str]:
        """Count identical calls, and eventually stop one.

        The result it gets back is the intervention: a model that is told, in
        the place it is already reading, that it has now asked the same
        question six times usually stops. Ending the turn instead would throw
        away everything it had got right up to that point.
        """
        fingerprint = hashlib.sha256(
            json.dumps({'n': call.name, 'a': call.arguments}, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        seen = self._repeats[fingerprint] = self._repeats.get(fingerprint, 0) + 1

        if seen < REPEAT_WARN:
            return None, ''

        if seen >= REPEAT_STOP:
            log.info('session %s: stopping a repeated %s call (%d times)', self.id, call.name, seen)
            call.status = ToolStatus.DENIED
            return ToolResultBlock(
                tool_use_id=call.id,
                content=(
                    f'You have now called {call.name} with exactly these arguments {seen} times in '
                    'this turn, and it was not run again. Nothing has changed between those calls, '
                    'so it would return what it returned before.\n\n'
                    'You are stuck in a loop. Stop repeating this call. Either do something '
                    'different with what you already have, or — if you genuinely cannot make '
                    'progress — say so plainly to the person and stop. If you do not know which '
                    'tool to use, read the tool descriptions again rather than trying this one '
                    'once more.'
                ),
                is_error=True,
            ), f'the same call {seen} times in one turn — it is looping'

        # Run it, but say so. A screenshot taken three times is three real
        # pictures, so this is a note attached to a result rather than a
        # refusal — the caller's own output is preserved after it.
        call.summary = f'{call.summary} (call {seen} of the same thing)'
        return None, ''

    # -- compaction ---------------------------------------------------------

    def context_report(self) -> Any:
        """How full the context is, and how far from being summarised.

        `tokens` prefers the provider's own last input count when there is one,
        because that is the number the model actually saw and the estimate is
        four-characters-to-a-token. `exact` says which it is, so the interface
        can stop drawing a precise-looking bar over a guess.

        `window` is looked up by name and is `None` when it is not recognised.
        That is the honest answer for a model this table has never heard of,
        and a wrong number here is a wrong percentage on a bar somebody is
        deciding whether to keep working against.
        """
        from openmirror.protocol.agent import ContextUse

        # The larger of the two, because they disagree: the estimate counts
        # what is queued and the provider counted what it was sent, and the
        # difference between them is usually a turn's worth of tool output
        # that has not been summarised yet.
        estimate = self._estimate()
        tokens = max(self._last_input, estimate)
        return ContextUse(
            tokens=tokens,
            limit=self.compact_at,
            window=windows.context_window(self.model),
            total_in=int(getattr(self, '_total_in', 0)),
            total_out=int(getattr(self, '_total_out', 0)),
            exact=bool(self._last_input) and self._last_input >= estimate,
        )

    def _estimate(self) -> int:
        """Roughly how many tokens the next request will be.

        Four characters to a token, and a flat rate for pictures — a
        screenshot's base64 is a hundred thousand characters and nowhere near
        that many tokens, and counting it as text would compact the
        conversation every time the agent looked at the screen.
        """
        chars = len(self.system_prompt)
        chars += sum(len(t.description) + len(json.dumps(t.input_schema)) for t in self.tools.values())
        images = 0
        for message in self.messages:
            for block in message.content:
                if isinstance(block, ImageBlock):
                    images += 1
                elif isinstance(block, TextBlock | ThinkingBlock):
                    chars += len(block.text)
                elif isinstance(block, ToolResultBlock):
                    chars += len(block.content)
                elif isinstance(block, ToolUseBlock):
                    chars += len(block.name) + len(json.dumps(block.input, default=str))
        return chars // 4 + images * 1_500

    async def _maybe_compact(self, turn_id: str) -> None:
        if not self.compact_at or self.parent is not None or self._fresh_summary:
            return
        # Two messages is the least there can be before a turn: something
        # asked and something answered. Less than that is nothing to summarise.
        if self._turn_start < 2:
            return
        if max(self._last_input, self._estimate()) < self.compact_at:
            return
        try:
            await self.compact(turn_id, reason='automatic', keep_from=self._turn_start)
        except Exception:  # noqa: BLE001 - a failed summary leaves the conversation as it was
            log.exception('automatic compaction failed; carrying on without it')
            self._fresh_summary = True

    async def compact(
        self, turn_id: str | None, *, reason: str = 'manual', keep_from: int | None = None, focus: str = ''
    ) -> bool:
        """Replace the conversation before `keep_from` with a summary of it.

        `keep_from` is always the start of a turn, so what is kept begins with
        something the person said — which is what keeps the conversation a
        valid alternation of turns for the providers that insist on one, and
        keeps a tool call from being separated from its result.
        """
        from openmirror.agent.tools.files import journal

        keep_from = len(self.messages) if keep_from is None else keep_from
        old = self.messages[:keep_from]
        if not old:
            return False

        before_count = len(self.messages)
        before_tokens = self._estimate()
        todo = self.tools.get('todo')
        todo_text = todo.render() if todo is not None and hasattr(todo, 'render') else ''
        request = ChatRequest(
            model=self.model,
            messages=[Message(role='user', content=[TextBlock(text=prompt_mod.compact_request(
                render_transcript(old, self.compact_at * 3 if self.compact_at else 240_000),
                focus=focus,
                todo=todo_text,
            ))])],
            system=prompt_mod.COMPACT_SYSTEM,
            effort=self.effort,
        )
        parts: list[str] = []
        async for event in self.provider.stream(request):
            if isinstance(event, StreamText):
                parts.append(event.text)
        summary = ''.join(parts).strip()
        if not summary:
            raise RuntimeError('the model returned an empty summary, so nothing was compacted')

        kept = self.messages[keep_from:]
        self.messages = [
            Message(role='user', content=[TextBlock(text=(
                '[The conversation before this point was compacted to make room. This is a summary '
                f'of it; the original messages are gone.]\n\n{summary}'
            ))]),
            Message(role='assistant', content=[TextBlock(
                text='Understood. I have the summary, and I will carry on from where it leaves off.'
            )]),
            *kept,
        ]
        self._turn_start = 2 if kept else len(self.messages)
        self._last_input = 0
        self._fresh_summary = True
        # A summary says a file was read, not what it said, and overwriting a
        # file from a summary of it is exactly the write the journal exists to
        # stop. Everything is read again before it is changed.
        journal.forget(self.id)

        await self._emit(ContextCompacted(
            session_id=self.id,
            turn_id=turn_id,
            reason=reason,
            messages_before=before_count,
            messages_after=len(self.messages),
            tokens_before=before_tokens,
            summary=summary,
        ))
        return True

    async def _run_command(self, turn_id: str, text: str, name: str, arguments: str) -> None:
        """A `/command`: shaped like a turn, so a client shows it as one."""
        from openmirror.agent.tools.files import journal

        await self._emit(TurnStarted(session_id=self.id, turn_id=turn_id, text=text))
        stop = 'end_turn'
        try:
            if name == 'compact':
                if not await self.compact(turn_id, reason='manual', focus=arguments):
                    await self._emit(AgentError(
                        session_id=self.id, turn_id=turn_id, message='There is nothing to compact yet.',
                    ))
            elif name == 'clear':
                before = len(self.messages)
                self.messages = []
                self._turn_start = 0
                self._last_input = 0
                journal.forget(self.id)
                todo = self.tools.get('todo')
                if todo is not None and hasattr(todo, 'items'):
                    todo.items = []
                await self._emit(ContextCompacted(
                    session_id=self.id, turn_id=turn_id, reason='cleared',
                    messages_before=before, messages_after=0,
                ))
            elif name == 'think':
                if arguments:
                    # ValueError for a word that is not a level lands in the
                    # handler below and is shown as the command failing.
                    await self.set_effort(arguments.split()[0])
                now = self.effort or "the model's own default"
                await self._emit(TextDelta(session_id=self.id, turn_id=turn_id, text=f'Thinking: {now}.'))
        except asyncio.CancelledError:
            await self._emit(TurnCompleted(session_id=self.id, turn_id=turn_id, stop_reason='interrupted'))
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception('/%s failed', name)
            stop = 'error'
            await self._emit(AgentError(session_id=self.id, turn_id=turn_id, message=f'/{name} failed: {exc}'))
        await self._emit(TurnCompleted(session_id=self.id, turn_id=turn_id, stop_reason=stop))

    # -- subagents ----------------------------------------------------------

    async def _spawn(
        self, kind: Any, task: str, title: str, background: bool, turn_id: str, call_id: str
    ) -> Output:
        from openmirror.agent.agents import child_tools

        if self.parent is not None:
            raise ToolError('an agent cannot start agents of its own — do the work yourself')

        policy = self.policy
        if kind.read_only:
            # Its own policy, capped, rather than the shared one: "changes
            # nothing" must not depend on the mode it would have inherited.
            policy = ApprovalPolicy(
                mode=Mode.READ_ONLY, rules=list(self.policy.rules),
                allow_purchases=False, allow_credentials=False,
            )

        child = AgentSession(
            session_id=f'{self.id}~{uuid.uuid4().hex[:8]}',
            root=self.root,
            provider=self.provider,
            model=kind.model or self.model,
            tools=child_tools(kind, self.tools),
            # A subagent thinks as hard as the session that started it.
            effort=self.effort,
            policy=policy,
            system_prompt=prompt_mod.build_agent(kind, self.root, confined=self.confined, extra=self.project_context),
            max_steps=self.max_steps,
            env=self.env,
            confined=self.confined,
            checkpoints=self.checkpoints,
            after_write=self.after_write,
            parent=self,
            agent_id=call_id,
        )

        shown = title or task.splitlines()[0]
        label = f'{kind.name}: {shown if len(shown) <= 80 else shown[:77] + "..."}'
        if background:
            started = await self.tasks.start_agent(lambda: child.delegate(task, turn_id), label, agent=call_id)
            return Output(
                content=(
                    f'Started the {kind.name} agent in the background as {started.id}. Carry on with '
                    'something else; you will be told when it finishes, and its report comes with that.'
                ),
                display={'agent': kind.name, 'title': title, 'background': True, 'task': started.id},
            )

        report = await child.delegate(task, turn_id)
        stats = f'{report.tool_calls} tool call{"" if report.tool_calls == 1 else "s"}, {report.duration_ms / 1000:.0f}s'
        if not report.ok:
            raise ToolError(f'The {kind.name} agent failed after {stats}: {report.text}')
        text = report.text or '(it finished without saying anything)'
        if report.stop_reason == 'max_steps':
            text = f'It ran out of steps before finishing. The last thing it said:\n\n{text}'
        body, cut = truncate(f'{text}\n\n[{kind.name} agent · {stats}]', 40_000, keep='head')
        return Output(
            content=body,
            truncated=cut,
            display={
                'agent': kind.name,
                'title': title,
                'tool_calls': report.tool_calls,
                'duration_ms': report.duration_ms,
                'stop_reason': report.stop_reason,
            },
        )

    async def delegate(self, task: str, turn_id: str) -> Report:
        """Do one task as a subagent, and report back."""
        started = time.monotonic()
        self.messages.append(Message(role='user', content=[TextBlock(text=task)]))
        usage = {'input_tokens': 0, 'output_tokens': 0}
        try:
            stop = await self._loop(turn_id, usage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the parent hears about it as the agent failing
            log.exception('agent %s failed', self.agent_id)
            return Report(
                text=f'{type(exc).__name__}: {exc}', ok=False, stop_reason='error',
                tool_calls=self.tool_calls, duration_ms=int((time.monotonic() - started) * 1000), usage=usage,
            )
        return Report(
            text=self._last_text(), stop_reason=stop, tool_calls=self.tool_calls,
            duration_ms=int((time.monotonic() - started) * 1000), usage=usage,
        )

    def _last_text(self) -> str:
        for message in reversed(self.messages):
            if message.role != 'assistant':
                continue
            text = ''.join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
            if text:
                return text
        return ''

    # -- background work ----------------------------------------------------

    async def _task_changed(self, task: Any) -> None:
        await self._emit(TaskUpdated(session_id=self.id, task=task.describe()))
        # A task the agent stopped itself needs no telling; it has the result
        # of the call that stopped it.
        if task.running or task.stopped_by == 'agent':
            return
        self._notes.append(self._task_note(task))

    def _task_note(self, task: Any) -> str:
        who = 'the person' if task.stopped_by == 'person' else 'the session'
        if task.kind == 'agent':
            if task.status == 'stopped':
                return f'[Background agent {task.id} ({task.label}) was stopped by {who} before it finished.]'
            report, cut = truncate(task.report or '(it said nothing)', 8_000, keep='head')
            more = f' The report is cut short here; tasks (output, {task.id}) has all of it.' if cut else ''
            verb = 'finished' if task.status == 'done' else 'failed'
            return f'[Background agent {task.id} ({task.label}) {verb}.{more} Its report:\n\n{report}]'
        if task.status == 'stopped':
            return f'[Background task {task.id} (`{task.label}`) was stopped by {who}.]'
        tail = task.tail(12)
        return (
            f'[Background task {task.id} (`{task.label}`) exited with code {task.exit_code}.'
            + (f' Its last lines:\n{tail}' if tail else '') + ']'
        )

    # -- suspension points --------------------------------------------------

    async def _await_approval(self, call_id: str) -> tuple[bool, str, bool]:
        fut: asyncio.Future[tuple[bool, str, bool]] = asyncio.get_running_loop().create_future()
        self._approvals[call_id] = fut
        try:
            # No timeout. An agent left running overnight should still be
            # waiting in the morning, not have decided for itself.
            return await fut
        finally:
            self._approvals.pop(call_id, None)

    def _context(self, turn_id: str, call_id: str) -> ToolContext:
        async def emit(text: str, stream: str) -> None:
            await self._emit(
                ToolOutputDelta(
                    session_id=self.id, turn_id=turn_id, call_id=call_id, text=text, stream=stream
                )
            )

        async def ask(question: str, options: list[str], multi: bool) -> str:
            question_id = uuid.uuid4().hex[:16]
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._questions[question_id] = fut
            await self._emit(
                QuestionAsked(
                    session_id=self.id,
                    turn_id=turn_id,
                    question_id=question_id,
                    question=question,
                    options=options,
                    multi=multi,
                )
            )
            try:
                return await fut
            finally:
                self._questions.pop(question_id, None)

        async def spawn(kind: Any, task: str, title: str, background: bool) -> Output:
            return await self._spawn(kind, task, title, background, turn_id, call_id)

        top = self.parent is None
        return ToolContext(
            root=self.root,
            cwd=self.cwd,
            emit=emit,
            ask=ask,
            session_id=self.id,
            env=self.env,
            confined=self.confined,
            checkpoint=self.checkpoints,
            tasks=self.tasks,
            spawn=spawn if top and self.agent_kinds else None,
            policy=self.policy,
            set_mode=self.set_mode if top else None,
        )

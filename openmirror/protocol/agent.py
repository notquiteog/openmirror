"""The agent wire protocol.

One rule shapes everything here: the client is never told a tool ran after
the fact. A tool that touches the machine is *proposed*, the client decides,
and only then does it start. That ordering is what makes a remote agent
something you can leave running, and it is why `tool.proposed` and
`tool.started` are two events rather than one.

Events are a discriminated union on `type`, so a client can switch on one
field and pydantic will refuse anything it does not recognise.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


def _now() -> float:
    return time.time()


class ToolStatus(StrEnum):
    """Where a tool call is in its life."""

    PROPOSED = 'proposed'      # waiting on the approval policy or a human
    APPROVED = 'approved'
    DENIED = 'denied'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'    # the turn was interrupted while it ran


class Risk(StrEnum):
    """How much a tool call can cost you if it was the wrong call.

    This is the tool's own assessment of the specific arguments, not of the
    tool in general: `rm -rf build/` and `rm -rf /` are the same tool. The
    approval policy reads this and nothing else, so a tool that lies here
    defeats the whole mechanism.
    """

    READ = 'read'              # observes; changes nothing
    WRITE = 'write'            # changes files
    EXECUTE = 'execute'        # runs a command
    DESTRUCTIVE = 'destructive'  # deletes, force-pushes, drops, overwrites
    NETWORK = 'network'        # reaches something outside this machine

    # The three below are a separate axis from the five above, and deliberately
    # so. "Let it do what it likes to my computer", "let it spend my money" and
    # "let it write to other people" are three different decisions, and a mode
    # that conflates them will eventually buy something because a page said to
    # — or send something on the owner's behalf that the owner would not have
    # sent, which is the same class of failure with a longer tail: the message
    # is read by a colleague, under the owner's name, and cannot be recalled.
    PURCHASE = 'purchase'      # spends money or commits to a transaction
    CREDENTIAL = 'credential'  # a password, card number or other secret
    # Speaks to somebody else as the person who owns the account. Reading an
    # inbox is `read`; *answering* is this, and the difference is that a read
    # is invisible to the world while a send is not and is not undoable.
    MESSAGE = 'message'


class ToolCall(BaseModel):
    """A tool the model wants to run, with the arguments it wants to run it with."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: Risk = Risk.READ
    # A one-line rendering for a human deciding whether to allow it. The tool
    # writes this, because only the tool knows which argument matters: for a
    # shell call it is the command, for an edit it is the path.
    summary: str = ''
    status: ToolStatus = ToolStatus.PROPOSED


class ToolResult(BaseModel):
    id: str
    name: str
    ok: bool
    # What goes back to the model. Truncated by the tool, not here, so the
    # tool can choose what to keep — the head of a stack trace, the tail of a
    # log — rather than losing whichever end the model needed.
    content: str = ''
    # Anything the UI can render better than text: a diff, an exit code, a
    # file list. Never sent to the model.
    display: dict[str, Any] | None = None
    duration_ms: int = 0
    truncated: bool = False


class Turn(BaseModel):
    """One user message and everything the agent did about it."""

    id: str
    session_id: str
    started_at: float = Field(default_factory=_now)
    ended_at: float | None = None


# ---------------------------------------------------------------------------
# Server -> client
# ---------------------------------------------------------------------------


class _Event(BaseModel):
    at: float = Field(default_factory=_now)
    session_id: str
    # Set by the session as it emits. A reconnecting client sends the last one
    # it saw and is replayed the gap, so this is the only thing making a
    # detach cheap rather than a full replay.
    seq: int = 0
    # Which agent in the session said this. Empty for the session's own; a
    # subagent's events carry the id of the `agent` call that started it, so a
    # client can nest them under that call — and a replay, being the same
    # events in the same order, rebuilds the same nesting. One log per session
    # rather than one per agent, because approvals from a subagent are
    # approvals in this session, and a second stream is a second place to miss
    # one.
    agent: str = ''


class SessionStarted(_Event):
    type: Literal['session.started'] = 'session.started'
    cwd: str
    model: str
    # What this session may do without asking. Sent up front so a UI can say
    # so plainly rather than surprising someone on the first tool call.
    policy: str
    tools: list[str] = Field(default_factory=list)
    # How hard the model thinks: off … max, or None for the model's own
    # default. Beside the policy because it is the other live control.
    effort: str | None = None


class TurnStarted(_Event):
    type: Literal['turn.started'] = 'turn.started'
    turn_id: str
    # What was asked. Carried here so a client that reattaches mid-session can
    # render the conversation, rather than only the half the agent said.
    text: str = ''


class TextDelta(_Event):
    type: Literal['text.delta'] = 'text.delta'
    turn_id: str
    text: str


class ThinkingDelta(_Event):
    """Reasoning tokens, where the provider exposes them.

    Separate from `text.delta` so a client can hide it, and so it never ends
    up spoken aloud by the voice gateway.
    """

    type: Literal['thinking.delta'] = 'thinking.delta'
    turn_id: str
    text: str


class ToolProposed(_Event):
    type: Literal['tool.proposed'] = 'tool.proposed'
    turn_id: str
    call: ToolCall
    # False when the policy already allowed it; the event is still sent so the
    # UI can show what is about to happen.
    needs_approval: bool = True


class ToolStarted(_Event):
    type: Literal['tool.started'] = 'tool.started'
    turn_id: str
    call_id: str


class ToolOutputDelta(_Event):
    """Output from a still-running tool.

    A build that takes four minutes should not be four minutes of silence,
    so long-running tools stream through here and the final ToolResult
    repeats only what the model needs.
    """

    type: Literal['tool.output.delta'] = 'tool.output.delta'
    turn_id: str
    call_id: str
    text: str
    stream: Literal['stdout', 'stderr'] = 'stdout'


class ToolCompleted(_Event):
    type: Literal['tool.completed'] = 'tool.completed'
    turn_id: str
    result: ToolResult


class ToolDenied(_Event):
    type: Literal['tool.denied'] = 'tool.denied'
    turn_id: str
    call_id: str
    reason: str = ''


class QuestionAsked(_Event):
    """The agent needs a human before it can go on.

    Distinct from a tool because it does not touch the machine and must not
    be auto-approved by any policy: the whole point is that a person answers.
    """

    type: Literal['question.asked'] = 'question.asked'
    turn_id: str
    question_id: str
    question: str
    options: list[str] = Field(default_factory=list)
    multi: bool = False


class TurnCompleted(_Event):
    type: Literal['turn.completed'] = 'turn.completed'
    turn_id: str
    stop_reason: Literal['end_turn', 'interrupted', 'max_steps', 'error'] = 'end_turn'
    usage: dict[str, int] = Field(default_factory=dict)
    #: How full the next request will be, and how much of the allowance that
    #: is. `tokens` is the daemon's own estimate and `limit` is the point the
    #: conversation gets summarised — both known exactly, which is why they
    #: are here rather than a number scraped from a provider's model card.
    #: `window` is the model's own context size when we recognise it, and
    #: `None` otherwise: a guessed window is a wrong percentage on a bar
    #: somebody is making decisions with.
    context: ContextUse | None = None


class ContextUse(BaseModel):
    """How full the context is. Said in tokens, not in currency.

    There is no cost here on purpose. Tokens per dollar change with a model's
    price, a provider's markup and a cache policy, and a number that is out of
    date is worse than no number: somebody reads it as a bill. What is exact is
    the token count and the point the conversation will be summarised, and
    those are the two that tell you whether to keep going or `/compact`.
    """

    tokens: int = 0
    #: The point this session summarises the conversation at. `0` for a
    #: session that will not, and then the bar has nothing to fill towards.
    limit: int = 0
    #: The model's own window, when it is one this project recognises.
    window: int | None = None
    #: Cumulative for the session, from the provider's own accounting.
    total_in: int = 0
    total_out: int = 0
    #: Whether `tokens` came from the provider or from the estimate.
    exact: bool = False


class AgentError(_Event):
    type: Literal['error'] = 'error'
    turn_id: str | None = None
    message: str
    # True when the same request would work if tried again — a timeout, a 429,
    # a model that was still loading. The client may retry; on False it must not.
    retryable: bool = False


class PolicyChanged(_Event):
    type: Literal['policy.changed'] = 'policy.changed'
    mode: str
    # The same thing spelled out, which is what the session banner shows.
    policy: str = ''
    # The thinking level, carried on every change of either control so a
    # client never has to remember one to render the other.
    effort: str | None = None


class TaskUpdated(_Event):
    """Background work changed state: it started, finished, failed or was stopped.

    A background task outlives the call that started it, so that call's
    result cannot say how it ended. This is the only place that does.
    """

    type: Literal['task.updated'] = 'task.updated'
    # id, kind (shell or agent), label, status, exit_code, started, ended —
    # and for an agent, the call id its own events carry.
    task: dict[str, Any] = Field(default_factory=dict)


class ContextCompacted(_Event):
    """The conversation so far was replaced by a summary of it, or cleared.

    Worth an event of its own because it changes what the agent knows without
    anything else visibly happening: after it, the model has the summary and
    not the transcript, and a client that shows neither fact leaves someone
    wondering why it forgot the exact wording of a file it read an hour ago.
    """

    type: Literal['context.compacted'] = 'context.compacted'
    turn_id: str | None = None
    reason: Literal['manual', 'automatic', 'cleared'] = 'manual'
    messages_before: int = 0
    messages_after: int = 0
    # Estimated, not counted: a count needs the provider's tokenizer, and the
    # point is the order of magnitude.
    tokens_before: int = 0
    # Empty when the conversation was cleared rather than summarised.
    summary: str = ''


class SessionEnded(_Event):
    type: Literal['session.ended'] = 'session.ended'
    reason: str = 'closed'


AgentEvent = Annotated[
    SessionStarted | TurnStarted | TextDelta | ThinkingDelta | ToolProposed | ToolStarted | ToolOutputDelta | ToolCompleted | ToolDenied | QuestionAsked | TurnCompleted | PolicyChanged | TaskUpdated | ContextCompacted | AgentError | SessionEnded,
    Field(discriminator='type'),
]


# ---------------------------------------------------------------------------
# Client -> server
# ---------------------------------------------------------------------------


class SubmitTurn(BaseModel):
    type: Literal['turn.submit'] = 'turn.submit'
    text: str
    # Files, screenshots, anything the model should see with the message.
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class ApproveTool(BaseModel):
    type: Literal['tool.approve'] = 'tool.approve'
    call_id: str
    # Remember the decision for the rest of the session, scoped to this tool
    # and these arguments — not to the tool as a whole, which would turn one
    # "yes" into permanent shell access.
    remember: bool = False


class DenyTool(BaseModel):
    type: Literal['tool.deny'] = 'tool.deny'
    call_id: str
    # Fed back to the model as the tool result, so it can try another way
    # instead of proposing the same thing again.
    reason: str = ''


class AnswerQuestion(BaseModel):
    type: Literal['question.answer'] = 'question.answer'
    question_id: str
    answer: str


class Interrupt(BaseModel):
    """Stop the current turn. Running tools are cancelled, the session lives on."""

    type: Literal['turn.interrupt'] = 'turn.interrupt'


class SetPolicy(BaseModel):
    """Change what the agent may do without asking, mid-session.

    How much you trust a run is something you learn *during* it, so this is a
    control rather than a property of the session's birth. It applies from the
    next decision onwards; a call already waiting on you was graded under the
    old mode and stays that way.
    """

    type: Literal['policy.set'] = 'policy.set'
    # Either or both. `effort` is off … max, or 'default' for the model's own.
    mode: str | None = None
    effort: str | None = None


class StopTask(BaseModel):
    """Stop one piece of background work — a dev server, a subagent.

    Separate from `turn.interrupt`, which deliberately leaves background work
    running: stopping the model is not a request to stop the server it
    started.
    """

    type: Literal['task.stop'] = 'task.stop'
    task_id: str


class CloseSession(BaseModel):
    type: Literal['session.close'] = 'session.close'


ClientCommand = Annotated[
    SubmitTurn | ApproveTool | DenyTool | AnswerQuestion | Interrupt | SetPolicy | StopTask | CloseSession,
    Field(discriminator='type'),
]

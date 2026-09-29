"""What a provider is, and the six things one can be asked to do.

Every provider implements only the modalities it has. Nothing here knows
about Anthropic, OpenAI or Ollama; the registry does the choosing, so adding
a provider is one file and one line, and swapping the speech synthesiser
cannot disturb the chat model.

The message format is deliberately closer to Anthropic's than OpenAI's:
content is a list of typed blocks rather than a string, because tool results,
images and reasoning do not fit in a string and every adapter that pretends
otherwise ends up re-parsing its own output. Flattening blocks into an
OpenAI payload is easy; recovering blocks from a flattened one is not.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class NoProviderError(RuntimeError):
    """No provider can serve this modality under the current constraints."""


class PrivacyRefusal(NoProviderError):
    """A provider exists but sending to it would break the caller's own rule."""


class ProviderRefused(NoProviderError):
    """The provider answered, and said no to the key.

    A `NoProviderError` on purpose: every place that already turns "nothing can
    serve this" into a message a person reads — the agent route, the agent and
    voice sockets — shows this one the same way, without learning a new type.

    It exists because the alternative was an empty list. Adapters used to treat
    a 401 from their model listing like any other non-200 and return `[]`,
    which is indistinguishable from "nothing pulled yet": the Test button said
    "ok — 0 models" for a connection that had been refused, and the agent said
    the provider "reports no models". Both were true and neither was the
    reason.
    """


def refused(provider_id: str, base_url: str, status: int, hint: str = '') -> ProviderRefused:
    """The error an adapter raises when its key was refused. One wording, everywhere."""
    what = 'missing, wrong or revoked' if status == 401 else 'not allowed to do this'
    message = f'{provider_id}: {base_url} refused the key (HTTP {status}) — it is {what}.'
    return ProviderRefused(f'{message} {hint}'.strip())


class Modality(StrEnum):
    CHAT = 'chat'
    EMBEDDING = 'embedding'
    STT = 'stt'
    TTS = 'tts'
    IMAGE = 'image'
    VIDEO = 'video'


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TextBlock:
    text: str
    type: Literal['text'] = 'text'


@dataclass(slots=True)
class ImageBlock:
    """Base64 image data. URLs are resolved before they reach a provider, so
    that one provider being unable to fetch a URL is not a capability
    difference the caller has to know about."""

    data: str
    media_type: str = 'image/png'
    type: Literal['image'] = 'image'


@dataclass(slots=True)
class ThinkingBlock:
    text: str
    signature: str | None = None
    type: Literal['thinking'] = 'thinking'


@dataclass(slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: Literal['tool_use'] = 'tool_use'


@dataclass(slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal['tool_result'] = 'tool_result'


ContentBlock = TextBlock | ImageBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock


@dataclass(slots=True)
class Message:
    role: Literal['user', 'assistant']
    content: list[ContentBlock]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ChatRequest:
    model: str
    messages: list[Message]
    system: str | None = None
    tools: list[ToolSpec] = field(default_factory=list)
    #: Ceiling on one response. **0 means no ceiling, and it is the default.**
    #:
    #: An agent turn is not a chat reply: it plans, calls tools, reads what
    #: came back and writes a patch, and a ceiling picked in advance is a guess
    #: about how long that takes. 4096 was that guess, and the way it failed is
    #: the bad way — a truncated turn is a well-formed response with the stop
    #: reason buried, so it reads as the model deciding to stop rather than as
    #: an error anyone would go looking for.
    #:
    #: Uncapped is expressed by NOT SENDING the field, never by sending a large
    #: number: a big number is still a ceiling and is wrong on the next model.
    #: Each adapter omits its own. Anthropic is the exception — `max_tokens` is
    #: required there — and asks that API for the model's own maximum instead.
    max_tokens: int = 0
    temperature: float | None = None
    #: How hard to think: ``off`` … ``max``, or None for the model's own
    #: default. One dial for every adapter — each spells it the way its host
    #: accepts (see `openmirror.providers.reasoning`), so a session moved
    #: between providers keeps meaning the same thing by it.
    effort: str | None = None
    stop: list[str] = field(default_factory=list)
    # Provider-specific extras, passed through untouched. An escape hatch, and
    # deliberately not a place to put anything the registry needs to read.
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StreamText:
    text: str
    type: Literal['text'] = 'text'


@dataclass(slots=True)
class StreamThinking:
    text: str
    type: Literal['thinking'] = 'thinking'


@dataclass(slots=True)
class StreamToolUse:
    """A complete tool call.

    Providers stream tool arguments as partial JSON, which is useless to a
    caller until it parses — so adapters accumulate and emit once. The cost is
    that a tool call appears all at once; the alternative is every consumer
    writing its own incremental JSON parser.
    """

    id: str
    name: str
    input: dict[str, Any]
    type: Literal['tool_use'] = 'tool_use'


@dataclass(slots=True)
class StreamDone:
    stop_reason: str = 'end_turn'
    usage: dict[str, int] = field(default_factory=dict)
    type: Literal['done'] = 'done'


StreamEvent = StreamText | StreamThinking | StreamToolUse | StreamDone


async def one_shot(
    provider: Any,
    req: ChatRequest,
    *,
    what: str = 'text',
    # Named `seconds` rather than `timeout`, because it *is* honoured — it is
    # wrapped around the stream in `asyncio.timeout` below — and a name that
    # reads as a bound the caller might think is advisory is worse than one
    # that says what it is. Ruff's ASYNC109 asks for this rename for the
    # opposite reason: it cannot tell the difference, and a caller who has to
    # read the body to learn whether their bound applies is the problem.
    seconds: float = 120.0,
) -> str:
    """Collect one complete text response, and say what went wrong if not.

    For the handful of places that need a model's judgement on a piece of text
    and nothing else: a commit message from a diff, a draft reply from a
    message. Not a second way to run an agent — there is one of that, and it
    is `AgentSession`.

    Five things this gets right that collecting the stream by hand does not:

    * **No ceiling, by default.** `ChatRequest.max_tokens` is 0 for a reason
      documented on the field, and this respects it. A cap here is actively
      harmful rather than merely useless: a reasoning model spends the budget
      on *thinking*, emits no text at all, and returns `stop_reason:
      max_tokens` with an empty answer. Measured on a diff of two lines: 400
      tokens of reasoning, zero characters of commit message, and a caller
      that could only report "the model returned nothing". The prompt is
      already a few hundred tokens in, so there is nothing to bound.
    * **A ceiling on the wall clock instead.** Separate from the token cap,
      and for a different reason: these sit behind a button, and a button that
      says "writing…" for ever has no way out but reloading the page. Two
      minutes is long enough for a reasoning model on a large diff and short
      enough that a hung provider is a sentence rather than a mystery.
    * **A truncated response is not an empty one.** If the model stopped
      because it ran out of room, that is a different failure from one that
      had nothing to say, and it is worth saying which.
    * **Tool calls are ignored rather than treated as text.** A model asked
      for a commit message that calls a tool has misunderstood, and returning
      the tool's arguments as the message would be worse than saying so.
    * **The timeout is its own error.** A slow model and a refused key are
      different problems and the operator's next step differs.
    """
    parts: list[str] = []
    stop = ''
    try:
        # `asyncio.timeout` rather than `wait_for`, so what comes out is a
        # timeout rather than a cancellation of whatever this was called
        # inside.
        async with asyncio.timeout(seconds):
            async for event in provider.stream(req):
                if isinstance(event, StreamText):
                    parts.append(event.text)
                elif isinstance(event, StreamDone):
                    stop = event.stop_reason
    except TimeoutError as exc:
        raise NoProviderError(
            f'the model did not answer within {int(seconds)}s. It may be slow or busy — try again, '
            f'or use a faster one.'
        ) from exc

    text = ''.join(parts).strip()
    if text:
        return text
    if stop == 'max_tokens':
        raise NoProviderError(
            f'the model used its whole response budget thinking and produced no {what}. '
            f'Retry, or give it a model that thinks less.'
        )
    raise NoProviderError(f'the model returned no {what}')


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class ChatProvider(abc.ABC):
    @abc.abstractmethod
    def stream(self, req: ChatRequest) -> AsyncIterator[StreamEvent]: ...

    @abc.abstractmethod
    async def models(self) -> list[dict[str, Any]]: ...


class EmbeddingProvider(abc.ABC):
    @abc.abstractmethod
    async def embed(
        self,
        texts: list[str],
        model: str,
        *,
        input_type: str = 'document',
        dimensions: int | None = None,
    ) -> list[list[float]]:
        """Vectors for these texts, in this order.

        `input_type` exists because several current models are asymmetric: a
        passage being stored and a question being asked are embedded
        differently, and using one setting for both costs recall in a way that
        reads as the model being worse than advertised rather than as a bug.
        Providers that do not draw the distinction ignore it.

        `dimensions` asks for a shorter vector from a model that supports
        truncation. It is the one parameter that must not change under an
        existing store: vectors of different lengths cannot be compared, so
        the store records what each was made with.
        """



@dataclass(slots=True)
class Transcript:
    text: str
    # True while the person is still talking. Streaming backends emit several
    # partials and one final; batch backends emit one final and never a partial.
    partial: bool = False
    language: str | None = None


class STTProvider(abc.ABC):
    @abc.abstractmethod
    async def transcribe(self, audio: bytes, *, model: str, language: str | None = None) -> Transcript: ...

    def supports_streaming(self) -> bool:
        """Whether `stream_transcribe` is real.

        Most local backends are batch-only — whisper.cpp behind Perch included —
        and the gateway compensates by cutting on VAD and transcribing each
        utterance. Saying so here lets it choose without a try/except.
        """
        return False

    def stream_transcribe(
        self, chunks: AsyncIterator[bytes], *, model: str, language: str | None = None
    ) -> AsyncIterator[Transcript]:
        raise NotImplementedError


class TTSProvider(abc.ABC):
    # The rate of the audio this provider actually returns, which is not
    # necessarily the rate anyone asked for. OpenAI's `pcm` is 24 kHz and so is
    # Kokoro's; a client that assumes 16 kHz plays them about a third too slow,
    # which sounds like a fault in the model rather than in the plumbing. So it
    # is declared rather than guessed.
    output_sample_rate: int = 24_000

    @abc.abstractmethod
    def synthesize(
        self, text: str, *, model: str, voice: str, fmt: str = 'pcm16', sample_rate: int = 16_000
    ) -> AsyncIterator[bytes]:
        """Yield audio as it is produced.

        An implementation that can only return one buffer yields once. It must
        still be an iterator, because the caller cancels by closing it — that
        is how barge-in stops a synthesis already in flight.
        """

    @abc.abstractmethod
    async def voices(self) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class GeneratedMedia:
    data: bytes
    media_type: str
    seed: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


#: Called while a generation is in flight, as `progress(fraction, note)`.
#:
#: `fraction` is 0..1 **or None**, and None is the important half of it. Most
#: of these backends genuinely cannot say how far along they are — a queue
#: position is not a percentage and neither is elapsed time — and a bar that
#: creeps to ninety and stops is a lie that teaches people to ignore the real
#: ones. So a provider that does not know says None and puts something true in
#: the note: "third in the queue", "still rendering".
ProgressFn = Any


class ImageProvider(abc.ABC):
    @abc.abstractmethod
    async def generate(
        self, prompt: str, *, model: str, n: int = 1, size: str = '1024x1024',
        progress: ProgressFn = None, **kw: Any
    ) -> list[GeneratedMedia]: ...


class VideoProvider(abc.ABC):
    @abc.abstractmethod
    async def generate(
        self, prompt: str, *, model: str, progress: ProgressFn = None, **kw: Any
    ) -> list[GeneratedMedia]: ...


@dataclass(slots=True)
class ProviderInfo:
    """What a provider is and what it can do, for the registry and the UI."""

    id: str
    label: str
    modalities: set[Modality]
    # True when the model runs on hardware the operator controls. The UI marks
    # these, because "which of these choices sends my screen to a company" is
    # the question people actually want answered.
    local: bool = False
    base_url: str | None = None

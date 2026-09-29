"""How big a model's context window is, when this project is sure.

A table, and the interesting part is what happens when the name is not in it.
The answer is `None`, and the interface then shows a token count with no
percentage on it. That is the whole design: a percentage needs a denominator,
and inventing one — by guessing 128k, or by taking the largest window anyone
mentions — produces a bar that fills at 40% on a model that will refuse the
request, and somebody makes a decision about whether to keep working based on
it.

So the entries are conservative and the misses are allowed. A model missing
from this table costs a percentage; a model in it with the wrong number costs
a wrong percentage, and only the first of those is a recoverable mistake.

**Where the numbers come from** is each vendor's own documentation, and they
are the *advertised* windows rather than the enforced ones. A gateway may cap
lower than the model it fronts — that is a per-provider setting rather than
something this can know — and `OPENMIRROR_CONTEXT_WINDOW` overrides all of it
for somebody who has been told by their provider what the real number is.

Not in the table, and deliberately: any model reached through a gateway whose
name is an alias. `openrouter/anthropic/claude-sonnet-5.5` is matched on its
last segment, because a gateway that renames a model is the one case where
looking at the string tells you nothing.
"""

from __future__ import annotations

# The advertised window, in tokens.
WINDOWS: dict[str, int] = {
    # Anthropic
    'claude-opus-4': 200_000,
    'claude-sonnet-4': 200_000,
    'claude-3-7-sonnet': 200_000,
    'claude-3-5-sonnet': 200_000,
    'claude-3-5-haiku': 200_000,
    'claude-3-opus': 200_000,
    'claude-3-haiku': 200_000,
    # OpenAI
    'gpt-4o': 128_000,
    'gpt-4o-mini': 128_000,
    'gpt-4.1': 1_047_576,
    'gpt-4.1-mini': 1_047_576,
    'gpt-4.1-nano': 1_047_576,
    'gpt-4-turbo': 128_000,
    'gpt-4': 8_192,
    'gpt-3.5-turbo': 16_385,
    'o1': 200_000,
    'o1-mini': 128_000,
    'o3': 200_000,
    'o3-mini': 200_000,
    'o4-mini': 200_000,
    'gpt-5': 400_000,
    # Google
    'gemini-1.5-pro': 2_097_152,
    'gemini-1.5-flash': 1_048_576,
    'gemini-2.0-flash': 1_048_576,
    'gemini-2.5-pro': 1_048_576,
    'gemini-2.5-flash': 1_048_576,
    # Meta, through a gateway or a local runtime
    'llama-3.1': 128_000,
    'llama-3.2': 128_000,
    'llama-3.3': 128_000,
    'llama-4': 1_048_576,
    # Mistral
    'mistral-large': 128_000,
    'mistral-small': 32_000,
    'mixtral': 32_768,
    'codestral': 262_144,
    # DeepSeek
    'deepseek-chat': 65_536,
    'deepseek-reasoner': 65_536,
    # Qwen
    'qwen3': 262_144,
    'qwen2.5': 131_072,
    # xAI
    'grok-4': 256_000,
    'grok-3': 131_072,
    # A local runtime's own default. Ollama's is a number it will refuse past,
    # and it is configurable there, so this is what it says out of the box.
    'gemma': 8_192,
}

# Names whose *prefix* identifies them, longest first, because `gpt-4o` and
# `gpt-4o-mini` are different windows and a plain `startswith` on `gpt-4`
# cannot tell them apart.
_PREFIXES = sorted(WINDOWS, key=len, reverse=True)


def _override() -> int:
    """`OPENMIRROR_CONTEXT_WINDOW`, read late.

    Read at call time rather than imported at module time so that a test — or
    somebody who exports the variable after the daemon starts — gets the
    number they set. A `from config import context_window` at the top of this
    file would freeze it at import and quietly do nothing.
    """
    try:
        from openmirror.config import config

        return int(getattr(config, 'context_window', 0) or 0)
    except Exception:  # noqa: BLE001 - a missing config must not break a call
        return 0


def context_window(model: str, override: int = 0) -> int | None:
    """The window for a model name, or None when it is not one we know.

    Longest-prefix first, and matched on the *last* path segment: a gateway
    name like `openrouter/anthropic/claude-sonnet-5.5` carries the real name
    at the end, and matching the whole string is how `gpt-4` matches
    `gpt-4o-mini` and reports the wrong size.
    """
    override = int(override or 0) or _override()
    if override > 0:
        return override
    name = (model or '').strip()
    if not name:
        return None
    # Drop a tag like `:batch` or a provider's own suffix, then take the last
    # segment of a path.
    name = name.split(':', 1)[0]
    tail = name.rsplit('/', 1)[-1].lower()
    for prefix in _PREFIXES:
        if tail.startswith(prefix):
            return WINDOWS[prefix]
    return None


def window_fraction(tokens: int, limit: int, window: int | None) -> float | None:
    """How full it is, as a fraction, or None when the denominator is unknown.

    The compaction limit wins over the model's window, because it is the one
    this session will actually hit: showing 30% of a 200k window when the
    conversation is summarised at 100k is a bar that means nothing to the
    decision somebody is making.
    """
    ceiling = limit if limit > 0 else (window or 0)
    if ceiling <= 0:
        return None
    return max(0.0, min(1.0, tokens / ceiling))


__all__ = ['WINDOWS', 'context_window', 'window_fraction']

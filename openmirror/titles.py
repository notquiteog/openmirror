"""A session named after its first sentence, rather than after its directory.

A session created over HTTP is titled `Path(root).name` — the last thing anybody
chose, not the thing the conversation is about. Claude Code and openCode both
generate a title from the conversation, and it is the single highest-leverage
thing in a sidebar: a list of forty sessions called `openmirror`, `openmirror`,
`openmirror` is a list you have to open to understand, and the one you want is
always in the middle of it.

**No model call.** A title is generated on the first user message, before
there is anything to summarise, and a call to a provider to name a turn would
cost money, add latency to the first thing a person sees, and fail on an
offline install. This is string work on a message that is already in hand, and
it is the same work in every case: strip the wrapper, keep the first clause,
cut on a word boundary.

**Deliberately lossy and deliberately short.** A title is a label, not a
summary; the conversation is one click away and is where the detail belongs.
`max_chars` is the whole budget, and a title that gets cut mid-word reads as a
truncation bug in the UI rather than as brevity, so the cut lands on a word
boundary and the result is simply shorter. No ellipsis: an ellipsis promises
text that is not there, and a label has nowhere to go for the reader to look.

**What is a placeholder**, and therefore what may be replaced, is
`needs_title`'s job and not this module's. Keeping the two apart is the point:
this decides what a good title *is*, and `needs_title` decides whether there is
one yet. A title a person set, a title a tool set, or a title this function
already produced is never re-derived, so asking again is always safe.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

#: Shorter than this, and the first sentence is a greeting or an
#: acknowledgement — "Hi.", "Sure.", "On it." — which titles nothing. So the
#: next sentence is taken too, and the loop stops as soon as there is enough to
#: name the turn. Not a word count, because the title is cut on characters.
MIN_TITLE_CHARS = 8
#: How many sentences that may run to. Three is generous for a first message
#: and bounded on purpose: without it a first line of "a. b. c. d. …" would
#: accumulate one character at a time until `max_chars` decided, which is the
#: word-boundary cut's job and not this loop's.
MAX_SENTENCES = 3
#: The floor for a *clause* cut, lower than `MIN_TITLE_CHARS` because a
#: semicolon already divides a complete thought in two and each half is
#: normally a phrase. "The bug; the parser drops empty files" is a title at
#: seven characters. "Yes; on it" is not, and is left whole.
MIN_CLAUSE_CHARS = 6

# Lines that carry no words: a horizontal rule, a code fence, a bare heading
# marker, or a slash command with nothing after it. Skipped rather than taken,
# because the first message is very often a paste and a paste starts with ```
# — and a title of "```" is not a title. A bare command is skipped for the same
# reason: "/compact" says what to do, not what the conversation is about, and
# the prose on the *next* line is the actual request.
_NOISE = re.compile(
    r'^(?:`{3,}[A-Za-z0-9_+-]*|~{3,}[A-Za-z0-9_+-]*|-{3,}|={3,}|\*{3,}|_{3,}|#{1,6}\s*'
    r'|/[A-Za-z][\w:.-]*)$'
)
# The same fences again, matched apart from `_NOISE` so `_first_block` can tell
# "this line opens a code block" from "this line is a rule".
_FENCE = re.compile(r'^(?:`{3,}[A-Za-z0-9_+-]*|~{3,}[A-Za-z0-9_+-]*)$')
# A leading slash command *with* something after it, which is kept as a title.
_COMMAND = re.compile(r'^/[A-Za-z][\w:.-]*')
# A sentence ends at `.!?…` followed by a space, or at a CJK full stop with
# nothing after it — which is how ideographic scripts actually write, so
# requiring whitespace there would treat every Japanese paragraph as one
# enormous sentence and title a session with its opening phrase.
_SENTENCE = re.compile(r'(?<=[。！？])|(?<=[.!?…])\s+')
# Clause separators, cut only when what remains is still a label. A semicolon
# and a spaced dash join two complete thoughts; a colon joins a label to its
# body, and "Note" or "Summary" on its own names nothing — which is why the
# colon is not here.
_CLAUSES = (';', ' — ', ' – ', ' -- ')
_QUOTES = {'"': '"', "'": "'", '“': '”', '„': '”', '‘': '’', '«': '»', '‹': '›', '「': '」', '『': '』'}
_TRAILING = '.,;:!?、。，；：！？…'
# Markdown that survives a copy-paste. Matched as pairs rather than deleted as
# characters, because `2 * 3` is arithmetic and deleting the asterisk turns it
# into `2 3` — and the lookarounds are what tell an emphasis marker (touching
# its text) from a symbol (spaced away from it). The link is the one that
# matters most of these: a pasted issue URL reads as
# "[crash on empty file](https://…)" and the label is the title.
# `_` is left alone: it is emphasis in a minority of what people write and the
# inside of every snake_case name in every path they mention.
_LINK = re.compile(r'!?\[([^\]]*)\]\([^)]*\)')
_STRONG = re.compile(r'\*\*(?!\s)(.+?)\*\*(?!\w)')
_STRIKE = re.compile(r'~~(?!\s)(.+?)~~(?!\w)')
_EMPH = re.compile(r'(?<!\*)\*(?!\s)(.+?)\*(?!\*)')
_CODE = re.compile(r'`([^`]*)`')
# Blockquote, heading and list markers, in any order and any depth. A bullet
# needs its space: "-1 does not parse" is arithmetic, not a list, and so is
# "3.5" — which is why the ordered marker demands one too.
_BULLET = re.compile(r'^(?:>[ \t]?|#{1,6}[ \t]?|[-*+•][ \t]+|\d{1,3}[.)][ \t]+)+')
# A message that is only a location. A path or a URL is not a sentence, and a
# title of "https://github.com/openai/openai-python/issues/42" is a title
# nobody can scan — so these fall back to something a person can read.
_URLISH = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*://\S+$')
_PATHISH = re.compile(r'^(?:[A-Za-z]:[\\/]|\.{0,2}[/\\]|~[/\\])\S*$')


def title_for(text: str, *, fallback: str = '', max_chars: int = 60) -> str:
    """A short human title for the first message of a conversation.

    `fallback` is what to answer with when the message holds no title at all —
    an empty or whitespace-only message, or a bare slash command. Pass the
    session's placeholder (the directory name) so a session that opened with
    `/compact` is not left nameless, and the result is empty only when there is
    nothing at all to say.

    `max_chars` is a hard ceiling on the result, cut on a word boundary with
    no trailing space and no ellipsis. A single word longer than the ceiling
    (a path, a hash, a base64 blob) is cut hard, because a word boundary that
    does not exist cannot be respected and a broken one is worse than a short
    word.
    """
    limit = max(1, int(max_chars))
    first = _first_block(str(text or ''))
    if first:
        if _is_location(first):
            # A link is named by its site and a path by its last segment;
            # everything before that is where it lives, not what it is.
            return _shorten(_name_of(first) or fallback, limit)
        cleaned = _clean(first)
        if cleaned:
            return _shorten(_clause(cleaned), limit)
    return _shorten(fallback, limit)


def needs_title(current: str, default_from_root: str) -> bool:
    """Whether this session is still wearing its placeholder title.

    **The rule: True only while the title is nothing anybody chose.** That is
    the empty string, or the directory name `SessionManager.create` falls back
    to (`title or Path(root).name`) when the caller passed none. Every other
    value is somebody's — the user's, a rename tool's, a `CreateSession` body,
    or a previous `title_for` — and is left exactly as it is.

    Why it has to be this narrow: a title is the only label a person has for a
    session, and it is the thing most likely to have been set deliberately. A
    generator that overwrote one would lose information the user cannot get
    back, and doing it *quietly* is what makes it unrecoverable. So this returns
    False for anything that is not recognisably the placeholder, and a caller
    can run it on every turn without ever risking a real title.

    `default_from_root` is accepted as a path or as the bare directory name,
    because both spellings occur at the call sites. Compared case-insensitively
    and whitespace-collapsed, since a title is displayed normalised and a
    comparison that missed on case would overwrite a real one.

    The one thing this cannot tell apart: a title that *is* the directory name
    looks identical to the placeholder, so it is treated as one. Naming a
    session after the directory it is in is the one case where losing the label
    costs nothing, because the label is what it was.
    """
    have = ' '.join(str(current or '').split())
    if not have:
        return True
    default = _name_of(' '.join(str(default_from_root or '').split()))
    if not default:
        return False
    return have.casefold() == default.casefold()


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _first_block(text: str) -> str:
    """The first thing in the message that has words in it, on one line.

    A hard-wrapped paragraph is joined rather than truncated at the first line,
    because a message pasted out of a terminal or a document arrives as one
    sentence split across three lines and cutting at the newline would title
    the session with a third of it.

    A fenced block is the exception and goes the other way: the fence is not
    prose, the code inside it is, and the first line of a stack trace is far
    more recognisable in a sidebar than the rest of the paste. So a fence
    opener ends the search rather than starting a join.

    Both rules skip the lines that carry no words of their own — rules, bare
    heading markers, a slash command with nothing after it — because
    "/compact" says what to do, not what the conversation is about, and the
    prose below it is the actual request.
    """
    lines = [line.strip() for line in text.splitlines()]
    start = 0
    while start < len(lines) and (not lines[start] or _NOISE.match(lines[start])):
        start += 1
    if start >= len(lines):
        return ''
    if _FENCE.match(lines[start]):
        for line in lines[start + 1:]:
            if line and not _FENCE.match(line):
                return line
        return ''
    end = start
    while end < len(lines) and lines[end]:
        end += 1
    return ' '.join(lines[start:end])


def _is_location(text: str) -> bool:
    """Whether the whole message is a path or a URL rather than a sentence."""
    return bool(_URLISH.match(text) or _PATHISH.match(text))


def _name_of(text: str) -> str:
    """The part of a path or a URL that names it: the last segment, or the host.

    Deliberately not the whole thing. A title is a label a person scans a list
    of forty of, and `src/openmirror/agent/session.py` is not one — the
    filename is. For a URL the tail is a path *into* a site, so the site is
    the identifiable half. A spaced path is not matched by `_PATHISH` and is
    treated as prose, because without a quoting convention there is no way to
    tell `/home/x/My Documents/a.py` from a sentence about documents.
    """
    text = text.strip()
    if _URLISH.match(text):
        return urlsplit(text).netloc or text
    # No `or text` at the end: "///" has no last segment, and handing the
    # original string back would be a title of "///" where "" is the honest
    # answer and the caller's fallback is better than either.
    return PurePosixPath(text.replace('\\', '/')).name


def _clean(line: str) -> str:
    """One line of what somebody typed, reduced to its words.

    Empty means "there is no title in this message", and only a slash command
    with nothing after it can get there; the caller turns that into the
    fallback rather than inventing one.
    """
    line = line.strip()
    command = _COMMAND.match(line)
    if command:
        rest = line[command.end():].strip()
        if not rest:
            # Unreachable through `_first_block`, which skips a bare command —
            # but this is the function that knows a command is not a title, and
            # a guard that only holds because of a rule three functions away
            # stops holding the day somebody reorders them.
            return ''
        line = rest
    line = _LINK.sub(r'\1', line)
    for pattern in (_CODE, _STRONG, _STRIKE, _EMPH):
        line = pattern.sub(r'\1', line)
    line = line.replace('`', '')          # a fence that wrapped a whole line
    line = _BULLET.sub('', line)
    return _unquote(line)


def _trim_end(line: str) -> str:
    """Drop the punctuation a title does not end with."""
    line = line.rstrip(_TRAILING + ' \t')
    # An unbalanced bracket is punctuation left behind by a cut, not a title
    # ending. Balanced ones are ("... in parser.py") and are left alone.
    for opener, closer in (('(', ')'), ('[', ']'), ('{', '}')):
        if line.endswith(closer) and line.count(opener) < line.count(closer):
            line = line[: -1].rstrip(_TRAILING + ' \t')
    return line


def _unquote(line: str) -> str:
    """Peel the quotes off, and the punctuation that came with them.

    Both ends or neither: a leading quote with no partner is part of the text
    (an inch mark, a stray character), and a closing one alone is somebody's
    emphasis, not a wrapper. Bounded, because a message of nothing but nested
    quotes is a message to be truncated rather than parsed.
    """
    for _ in range(4):
        line = _trim_end(line)
        if len(line) > 1 and line[0] in _QUOTES and _QUOTES[line[0]] == line[-1]:
            line = line[1:-1].strip()
            continue
        return line
    return _trim_end(line)


def _clause(text: str) -> str:
    """The first sentence — or the first few, if the first one is a greeting.

    The loop exists because "Sure." is a sentence and is not a title. It stops
    as soon as the text is long enough to name the turn, so the common case —
    a first message that opens with the actual request — is still one sentence
    and nothing is gained by taking more.
    """
    out = ''
    for part in _SENTENCE.split(text)[:MAX_SENTENCES]:
        if part.strip():
            out = f'{out} {part.strip()}'.strip()
        if len(out) >= MIN_TITLE_CHARS:
            break
    if not out:
        return text.strip()
    cut = min((out.find(sep) for sep in _CLAUSES if out.find(sep) > 0), default=-1)
    if cut >= MIN_CLAUSE_CHARS:
        return out[:cut].rstrip(_TRAILING + ' \t')
    return out


def _shorten(text: str, limit: int) -> str:
    """Cut at `limit` on a word boundary: no trailing space, no ellipsis.

    The boundary has to keep most of the budget to be worth taking — a first
    word of two characters followed by a forty-character tail is not a word
    boundary in any sense a reader would recognise, so a hard cut is better
    there. An ellipsis is refused on purpose: it promises an elision the
    sidebar has no room to expand, and a title that ends in "…" looks broken.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text:
        return ''
    if len(text) > limit:
        window = text[:limit]
        space = window.rfind(' ')
        if space > limit // 3:
            window = window[:space]
        text = window
    # Once, at the end, and not per-sentence: the punctuation left behind by a
    # cut is the same punctuation a sentence leaves behind, and doing it in one
    # place means no caller has to remember.
    return _trim_end(text)


__all__ = ['MAX_SENTENCES', 'MIN_CLAUSE_CHARS', 'MIN_TITLE_CHARS', 'needs_title', 'title_for']

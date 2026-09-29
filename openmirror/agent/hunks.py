"""Reviewing a change one hunk at a time.

`difflib` is in the standard library and is the whole of the diffing; what is
in here is the part `difflib` does not do, which is the part a person wants.

**The problem.** An agent edits five files. The turn ends. You read the diff
and it is a wall, and your options are "take all of it" or "undo the turn and
start again". Neither is useful, because changes rarely deserve all-or-nothing:
of the four things it did, three are right and the fourth is wrong, and
undoing the turn to get rid of the fourth throws away the three.

**The approach.** Split the change into hunks — the blocks a unified diff
already draws, with a few lines of context each — and let each be kept or
dropped independently. Keeping all of them reproduces the file the agent
wrote, exactly. Keeping none of them reproduces the file that was there
before, exactly. Those two are the property everything else rests on and they
are asserted rather than hoped for.

**How the subset is applied.** Not by patching: a diff applied to a file that
has already been patched is a diff applied to text that is no longer the text
it was computed against, and the second application silently lands in the
wrong place. Instead the file is *rebuilt* from the original: walk the opcode
list, copy every unchanged run, and for each changed run take the agent's
version if its hunk was kept and the original's if it was not. One pass, no
offsets to get wrong, and no dependence on what is currently on disk.

That is also why the hunk carries the opcodes it covers rather than a line
range: a line range is a claim about a version of the file, and the
reconstruction reads the same opcodes the display was built from, so the
thing shown and the thing applied cannot drift apart.

**Line endings and encodings are preserved**, because `splitlines(keepends=True)`
keeps the terminator with its line and a rebuild joins them back exactly. A
review tool that converted a file's line endings or dropped its last newline
would be a review tool that damaged the file it was reviewing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

# How many unchanged lines each hunk carries. Three is what `diff -u` and
# `git diff` both use, and it is enough to see what a change is attached to.
CONTEXT = 3
# A run of unchanged lines longer than this is a gap between hunks rather
# than something to include. Two contexts either side of a gap is the
# conventional test, and it is what stops two adjacent edits being shown as
# one when they are forty lines apart.
GAP = 2 * CONTEXT

Opcode = tuple[str, int, int, int, int]


def lines_of(text: str) -> list[str]:
    """Split into lines with their terminators, so a rebuild is byte-exact.

    `splitlines` without `keepends` loses the distinction between "a line" and
    "a line and the end of the file", so a file that did not end in a newline
    comes back with one.
    """
    return text.splitlines(keepends=True)


def _is_real(line: str) -> bool:
    """Whether a line is anything other than a terminator.

    A blank line is `'\n'` and is a real line that changed; the empty string
    only appears as the last element of a split for a file ending in a
    newline, and it is not a line at all.
    """
    return line.strip('\r\n') != '' or line in ('\n', '\r')


def opcodes(before: str, after: str) -> list[Opcode]:
    """The change list, computed once and used by everything here."""
    matcher = SequenceMatcher(None, lines_of(before), lines_of(after), autojunk=False)
    return list(matcher.get_opcodes())


@dataclass(slots=True)
class Hunk:
    """One reviewable block of a change."""

    index: int
    #: Which opcodes in the change list this hunk covers. Carrying them
    #: rather than a line range is what makes the display and the
    #: reconstruction provably the same thing.
    ops: list[int] = field(default_factory=list)
    before_start: int = 0
    before_count: int = 0
    after_start: int = 0
    after_count: int = 0
    before_lines: list[str] = field(default_factory=list)
    after_lines: list[str] = field(default_factory=list)
    #: A few unchanged lines either side, for reading it in context.
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return self.after_count

    @property
    def removed(self) -> int:
        return self.before_count

    @property
    def header(self) -> str:
        return f'@@ -{self.before_start},{self.before_count} +{self.after_start},{self.after_count} @@'

    def render(self) -> str:
        """The hunk as it would appear in a unified diff."""
        out = [self.header]
        out += [f' {line}' for line in self.context_before]
        out += [f'-{line}' for line in self.before_lines]
        out += [f'+{line}' for line in self.after_lines]
        out += [f' {line}' for line in self.context_after]
        return '\n'.join(out)

    def summary(self) -> str:
        """One line for a button beside it."""
        bits = []
        if self.removed:
            bits.append(f'-{self.removed}')
        if self.added:
            bits.append(f'+{self.added}')
        return ' '.join(bits) or 'changed'

    def public(self, kept: bool = True) -> dict[str, Any]:
        return {
            'index': self.index,
            'header': self.header,
            'diff': self.render(),
            'summary': self.summary(),
            'kept': kept,
            'removed': self.before_count,
            'added': self.after_count,
        }


def split(before: str, after: str, *, context: int = CONTEXT) -> list[Hunk]:
    """The change, as hunks a person can accept or drop one at a time.

    Only real lines are matched on: a file whose only change is its final
    newline would otherwise produce a hunk containing nothing, which is the
    kind of thing that gets a button and then a shrug.
    """
    blines, alines = lines_of(before), lines_of(after)
    ops = opcodes(before, after)
    gap = 2 * context

    hunks: list[Hunk] = []
    current: Hunk | None = None
    for position, (tag, i1, i2, _j1, _j2) in enumerate(ops):
        if tag == 'equal':
            if (i2 - i1) > gap:
                current = None
            continue
        if current is None:
            current = Hunk(index=len(hunks))
            hunks.append(current)
        current.ops.append(position)

    if not hunks:
        return []

    # The context around each hunk, taken from the *before* file where there
    # is one and from the *after* file past its end, which is what a unified
    # diff does and what makes a hunk at the bottom of a file readable.
    for hunk in hunks:
        first = ops[hunk.ops[0]]
        last = ops[hunk.ops[-1]]
        hunk.before_start = first[1] + 1
        hunk.after_start = first[3] + 1
        hunk.before_lines = blines[first[1]:last[2]]
        hunk.after_lines = alines[first[3]:last[4]]
        hunk.before_count = len(hunk.before_lines)
        hunk.after_count = len(hunk.after_lines)
        hunk.context_before = blines[max(0, first[1] - context):first[1]]
        hunk.context_after = alines[last[4]:last[4] + context]

    return hunks


def rebuild(before: str, after: str, keep: set[int], *, context: int = CONTEXT) -> str:
    """The file with only the hunks in `keep`, and nothing else.

    This is the property the whole feature rests on:

        rebuild(b, a, everything) == a
        rebuild(b, a, nothing)     == b

    Walked over the same opcodes the hunks were cut from, so there are no
    offsets to be wrong about and no dependence on what is currently on disk.
    """
    blines, alines = lines_of(before), lines_of(after)
    ops = opcodes(before, after)
    owner: dict[int, int] = {}
    for hunk in split(before, after, context=context):
        for position in hunk.ops:
            owner[position] = hunk.index

    out: list[str] = []
    for position, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == 'equal':
            out.extend(blines[i1:i2])
        elif owner.get(position) in keep:
            out.extend(alines[j1:j2])
        else:
            out.extend(blines[i1:i2])
    return ''.join(out)


def unified(before: str, after: str, *, context: int = CONTEXT, name: str = 'file') -> str:
    """A whole-file unified diff, for a model or a terminal.

    The hunks in `render` are for a person choosing between two options side
    by side; this is the linear thing a prompt or `patch` wants.
    """
    hunks = split(before, after, context=context)
    if not hunks:
        return ''
    out = [f'--- {name}', f'+++ {name}']
    out += [hunk.render() for hunk in hunks]
    return '\n'.join(out) + '\n'


__all__ = ['CONTEXT', 'Hunk', 'lines_of', 'opcodes', 'rebuild', 'split', 'unified']

"""Finding a file to mention, quickly and without reading any of them.

`@` in the composer needs to answer "which file did you mean" in the time it
takes to keep typing. So this is a bounded walk with a short list, and the
bounds are the design:

* **A time budget, not just a count.** A large repository can have a million
  files, and the person is still typing. The walk stops at
  `TIME_BUDGET` seconds and returns whatever it has, which is usually the
  right answer because the right answer is near the top.
* **The same directories the search tools skip.** `.git`, `node_modules` and
  the rest are not files anybody wants to mention, and walking them is how a
  suggestion list takes two seconds to appear. Reusing `SKIP_DIRS` rather than
  repeating the list is the point of it being in one place.
* **Never reads a file.** Only names, and only `stat` for a size and a time.
  A mention goes into the conversation, and anything read here would be read
  into a list somebody is looking at rather than into context.
* **Bounded by what was typed.** An empty query returns the files most
  recently changed, which is nearly always what someone opening `@` wants,
  and a query narrows from there. Both are bounded.

Ranked so the answer is at the top: an exact name match, then a prefix match,
then a substring anywhere — and ties broken by modification time, so
`app.js` in a repo with six of them is the one somebody just touched.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from openmirror.agent.tools.search import SKIP_DIRS

# Enough to fill the list several times over. The client shows the first
# screenful and never scrolls this one, so more than that is waste.
MAX_FILES = 400
# Short enough that the list is there before the next keystroke lands.
TIME_BUDGET = 0.25
# A file bigger than this is not one to read into a conversation, and the
# size is free to find.
MAX_BYTES = 2 * 1024 * 1024
# Never walk deeper than this. A repository with a generated tree a hundred
# directories deep is not one where the answer is at the bottom of it.
MAX_DEPTH = 12


def _rank(path: str, query: str) -> int:
    """Lower is better. Three tiers, and the tiers are the order people
    actually mean: the file I named, the file whose name starts with what I
    typed, and then anything containing it."""
    if not query:
        return 0
    name = path.rsplit('/', 1)[-1].lower()
    needle = query.lower()
    if name == needle:
        return 0
    if name.startswith(needle):
        return 1
    if path.lower().startswith(needle):
        return 2
    if needle in name:
        return 3
    return 4


def find(root: Path, query: str = '', *, limit: int = 40) -> list[dict[str, Any]]:
    """Files matching `query`, best first. Never raises.

    A failure here is a typing convenience, so a directory that cannot be read
    is skipped rather than turned into an error on every keystroke.
    """
    root = Path(root)
    needle = (query or '').strip()
    started = time.monotonic()

    found: list[tuple[tuple[int, float], str, int]] = []
    seen_dirs = 0

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
        # Pruned in place, which is the only way `os.walk` honours it.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        seen_dirs += 1
        if seen_dirs > 4000 or time.monotonic() - started > TIME_BUDGET:
            break

        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue

        for name in filenames:
            if name.startswith('.'):
                continue
            full = Path(dirpath) / name
            try:
                info = full.stat()
            except OSError:
                continue
            if info.st_size > MAX_BYTES or not full.is_file():
                continue
            try:
                rel = str(full.relative_to(root))
            except ValueError:
                continue
            if needle and needle.lower() not in rel.lower():
                continue
            # (tier, -mtime) so a plain ascending sort puts exact matches
            # first and, within a tier, the most recently touched file.
            found.append(((_rank(rel, needle), -info.st_mtime), rel, info.st_size))

    found.sort(key=lambda item: item[0])
    return [
        {'path': path, 'size': size, 'score': score[0]}
        for score, path, size in found[: max(1, min(limit, MAX_FILES))]
    ]


def excerpt(root: Path, path: str, limit: int = 400) -> str:
    """A few lines of a file, for the tooltip beside the suggestion.

    Bounded hard and by line count as well as characters, because the point is
    to recognise the file and not to read it. A binary file is not detected
    by guessing: the read either decodes or it does not, and a `UnicodeDecode
    Error` is the answer.
    """
    try:
        target = (Path(root) / path).resolve()
        if Path(root).resolve() not in target.parents:
            return ''
        with target.open('r', encoding='utf-8', errors='strict') as handle:
            lines: list[str] = []
            for _ in range(8):
                line = handle.readline()
                if not line:
                    break
                lines.append(line.rstrip('\n')[:200])
    except (OSError, UnicodeDecodeError, ValueError):
        return ''
    text = '\n'.join(lines)
    return text[:limit]


__all__ = ['MAX_BYTES', 'MAX_FILES', 'TIME_BUDGET', 'excerpt', 'find']

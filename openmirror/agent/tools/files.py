"""Reading and changing files.

Editing is exact-string replacement rather than line numbers or unified diff,
and that choice is worth explaining. Line numbers go stale the moment anything
above them changes, so a model that read a file, thought, and then edited by
line number edits the wrong line. Model-generated unified diffs fail on
context it half-remembers. An exact string that must appear exactly once is
self-verifying: if the file is not what the model thought, the match fails and
nothing happens, which is the correct outcome.

Hence the read-before-write rule. A write to a file this session has not read
is refused, because the common way to destroy work is to overwrite a file
whose contents were assumed.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from openmirror.agent.documents import DocumentError, is_document, read_document
from openmirror.agent.tools.base import (
    Assessment,
    Output,
    Tool,
    ToolContext,
    ToolError,
    resolve_in_root,
    truncate,
)
from openmirror.protocol.agent import Risk

MAX_READ_BYTES = 2_000_000
DEFAULT_LINE_LIMIT = 2000

# Extensions read as text regardless of what the null-byte check thinks.
BINARY_HINT = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.pdf', '.zip', '.gz',
    '.tar', '.xz', '.bz2', '.7z', '.exe', '.dll', '.so', '.dylib', '.o', '.a',
    '.class', '.jar', '.pyc', '.wasm', '.mp3', '.mp4', '.wav', '.mov', '.webm',
    '.sqlite', '.db', '.woff', '.woff2', '.ttf', '.otf',
}


class _Journal:
    """Which files this session has read, and what they looked like.

    Session-scoped rather than global: two sessions editing the same repo must
    not satisfy each other's read-before-write requirement.
    """

    def __init__(self) -> None:
        self._seen: dict[str, dict[Path, int]] = {}

    def note_read(self, session: str, path: Path) -> None:
        try:
            self._seen.setdefault(session, {})[path] = path.stat().st_mtime_ns
        except OSError:
            pass

    def has_read(self, session: str, path: Path) -> bool:
        return path in self._seen.get(session, {})

    def forget(self, session: str) -> None:
        """Drop what this session has read, so every file is read again before a write.

        For when the conversation that held those reads is gone — compacted
        into a summary, or cleared. A summary says a file was read, not what
        it said, and overwriting a file from a summary of it is exactly the
        assumed-contents write this journal exists to stop.
        """
        self._seen.pop(session, None)

    def changed_since_read(self, session: str, path: Path) -> bool:
        """True if something else wrote the file after we read it.

        mtime rather than a hash: a hash means reading the whole file again on
        every edit, and mtime catches the case this is actually guarding
        against — another process, or the user in their editor.
        """
        known = self._seen.get(session, {}).get(path)
        if known is None:
            return False
        try:
            return path.stat().st_mtime_ns != known
        except OSError:
            return False


journal = _Journal()


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_HINT:
        return True
    try:
        with path.open('rb') as fh:
            return b'\x00' in fh.read(8192)
    except OSError:
        return False


def _diff(before: str, after: str, path: str) -> str:
    return ''.join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f'a/{path}',
            tofile=f'b/{path}',
            n=3,
        )
    )


class ReadTool(Tool):
    name = 'read_file'
    description = (
        'Read a text file. Returns the contents with line numbers. Those numbers are a reading '
        'aid for you: they are not in the file, and neither is the vertical rule after them. '
        'Never include either in an edit, and never quote '
        'them back to the person — if they ask what a line says, answer with the line, not with '
        'the number and a tab in front of it. Use offset and limit for a file too large to read '
        'at once. A Jupyter notebook is shown as its cells and their outputs, pictures included; '
        'change one with notebook_edit.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': 'Path to the file.'},
            'offset': {'type': 'integer', 'description': '1-based line to start at.'},
            'limit': {'type': 'integer', 'description': f'Lines to read. Default {DEFAULT_LINE_LIMIT}.'},
            'layout': {
                'type': 'boolean',
                'description': (
                    'For a PDF: keep the visual layout instead of reading in whatever order the glyphs '
                    'are stored. Slower, and right for a two-column document.'
                ),
            },
        },
        'required': ['path'],
    }

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        if not args.get('path'):
            return Assessment(risk=Risk.READ, summary='', invalid='path is required')
        return Assessment(risk=Risk.READ, summary=f'read {args["path"]}')

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        path = resolve_in_root(args['path'], ctx)

        if not path.exists():
            raise ToolError(f'{args["path"]}: no such file')
        if path.is_dir():
            raise ToolError(f'{args["path"]}: is a directory — use list_dir or glob')
        if path.suffix.lower() == '.ipynb':
            # Before the size check: a notebook with a few plots in it is over
            # the text limit, and what reaches the model is not the plots' base64.
            from openmirror.agent.tools import notebook

            return notebook.read(path, ctx)
        if is_document(path):
            # A PDF or a spreadsheet is a *document*, and saying "looks like a
            # binary file" about one is a lie: the file is readable, this just
            # needed a code path that did not exist a moment ago. The refusal
            # a user sees when pypdf is absent names the install instead.
            try:
                read = read_document(path, layout=bool(args.get('layout')))
            except DocumentError as exc:
                raise ToolError(str(exc)) from exc
            if read is None:
                raise ToolError(f'{args["path"]}: not a document this knows how to read')
            journal.note_read(ctx.session_id, path)
            return Output(
                content=read.describe(),
                display={'path': str(path), 'kind': read.kind, 'pages': read.pages,
                         'sheets': read.sheets},
            )
        if _looks_binary(path):
            raise ToolError(f'{args["path"]}: looks like a binary file ({path.stat().st_size} bytes)')
        if path.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f'{args["path"]}: {path.stat().st_size} bytes is too large; use offset and limit')

        text = path.read_text(encoding='utf-8', errors='replace')
        journal.note_read(ctx.session_id, path)

        if not text:
            # Said explicitly, because a model that gets '' back tends to
            # decide the read failed and try again.
            return Output(content='(the file is empty)', display={'path': str(path), 'lines': 0})

        lines = text.splitlines()
        offset = max(1, int(args.get('offset') or 1))
        limit = int(args.get('limit') or DEFAULT_LINE_LIMIT)
        window = lines[offset - 1 : offset - 1 + limit]

        # A box-drawing gutter rather than a tab. Instructing a model not to
        # quote the numbers back does not work — tested on a 12B, twice — because
        # it is copying what it sees, and a number followed by a tab looks like
        # part of the line. A rule it never draws itself is not something it
        # reproduces, and it costs one character per line.
        width = len(str(offset + len(window)))
        body = '\n'.join(f'{offset + i:>{width}}\u2502{line}' for i, line in enumerate(window))
        body, cut = truncate(body, 60_000, keep='head')

        more = offset - 1 + len(window) < len(lines)
        if more:
            body += f'\n\n[{len(lines) - (offset - 1 + len(window))} more lines; read again with offset={offset + len(window)}]'

        return Output(
            content=body,
            display={'path': str(path), 'lines': len(lines), 'shown': len(window)},
            truncated=cut or more,
        )


class WriteTool(Tool):
    name = 'write_file'
    description = (
        'Write a file, creating it or replacing it entirely. To change part of an existing file '
        'use edit_file instead — it is safer and cheaper. An existing file must have been read '
        'in this session before it can be overwritten.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string'},
            'content': {'type': 'string', 'description': 'The complete new contents.'},
        },
        'required': ['path', 'content'],
    }

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        path = args.get('path')
        if not path:
            return Assessment(risk=Risk.WRITE, summary='', invalid='path is required')
        if args.get('content') is None:
            return Assessment(risk=Risk.WRITE, summary='', invalid='content is required')

        try:
            resolved = resolve_in_root(path, ctx)
            exists = resolved.exists()
        except ToolError:
            # Left to run() to report properly; assess must not raise, or the
            # model gets an approval failure instead of a usable error.
            exists = False

        # Overwriting is destructive; creating is not. The distinction is what
        # lets a policy allow new files but ask before replacing existing ones.
        return Assessment(
            risk=Risk.DESTRUCTIVE if exists else Risk.WRITE,
            summary=f'{"overwrite" if exists else "create"} {path}',
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        path = resolve_in_root(args['path'], ctx)
        content = args['content']

        before = ''
        if path.exists():
            if path.is_dir():
                raise ToolError(f'{args["path"]}: is a directory')
            if not journal.has_read(ctx.session_id, path):
                raise ToolError(
                    f'{args["path"]}: read it before overwriting it. '
                    'Writing a file whose current contents you have not seen loses work.'
                )
            if journal.changed_since_read(ctx.session_id, path):
                raise ToolError(
                    f'{args["path"]}: changed on disk since you read it. Read it again first.'
                )
            before = path.read_text(encoding='utf-8', errors='replace')

        if ctx.checkpoint is not None:
            ctx.checkpoint.record(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        journal.note_read(ctx.session_id, path)

        added = len(content.splitlines())
        return Output(
            content=f'Wrote {args["path"]} ({added} lines).',
            display={'path': str(path), 'diff': _diff(before, content, args['path']), 'created': not before},
        )


class EditTool(Tool):
    name = 'edit_file'
    description = (
        'Replace an exact string in a file. old_string must appear exactly once unless '
        'replace_all is true — include enough surrounding context to make it unique. '
        'Never include the line-number prefixes that read_file adds. The file must have been '
        'read in this session.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string'},
            'old_string': {'type': 'string', 'description': 'Exact text to find, including indentation.'},
            'new_string': {'type': 'string', 'description': 'Text to replace it with.'},
            'replace_all': {'type': 'boolean', 'description': 'Replace every occurrence. Default false.'},
        },
        'required': ['path', 'old_string', 'new_string'],
    }

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        path = args.get('path')
        if not path:
            return Assessment(risk=Risk.WRITE, summary='', invalid='path is required')
        if args.get('old_string') is None or args.get('new_string') is None:
            return Assessment(risk=Risk.WRITE, summary='', invalid='old_string and new_string are required')
        if args['old_string'] == args['new_string']:
            return Assessment(risk=Risk.WRITE, summary='', invalid='old_string and new_string are identical')

        first = (args['old_string'].strip().splitlines() or [''])[0]
        shown = first if len(first) <= 60 else first[:57] + '...'
        return Assessment(risk=Risk.WRITE, summary=f'edit {path}: {shown!r}')

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        path = resolve_in_root(args['path'], ctx)
        old, new = args['old_string'], args['new_string']

        if not path.is_file():
            raise ToolError(f'{args["path"]}: no such file')
        if not journal.has_read(ctx.session_id, path):
            raise ToolError(f'{args["path"]}: read it before editing it.')
        if journal.changed_since_read(ctx.session_id, path):
            raise ToolError(f'{args["path"]}: changed on disk since you read it. Read it again first.')

        before = path.read_text(encoding='utf-8', errors='replace')
        count = before.count(old)

        if count == 0:
            # The most common cause by far is the model reproducing the line
            # numbers read_file printed, so say so rather than just "not found".
            raise ToolError(
                f'{args["path"]}: old_string not found. It must match the file exactly, '
                'including whitespace and indentation, and must not include line numbers.'
            )
        if count > 1 and not args.get('replace_all'):
            raise ToolError(
                f'{args["path"]}: old_string appears {count} times. '
                'Add surrounding context to make it unique, or pass replace_all.'
            )

        if ctx.checkpoint is not None:
            ctx.checkpoint.record(path)

        after = before.replace(old, new) if args.get('replace_all') else before.replace(old, new, 1)
        path.write_text(after, encoding='utf-8')
        journal.note_read(ctx.session_id, path)

        return Output(
            content=f'Edited {args["path"]} ({count if args.get("replace_all") else 1} replacement(s)).',
            display={'path': str(path), 'diff': _diff(before, after, args['path']), 'replacements': count},
        )


class ListDirTool(Tool):
    name = 'list_dir'
    description = 'List the entries in a directory, marking which are directories.'
    input_schema = {
        'type': 'object',
        'properties': {'path': {'type': 'string', 'description': 'Directory. Defaults to the working directory.'}},
    }

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        return Assessment(risk=Risk.READ, summary=f'list {args.get("path") or "."}')

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        path = resolve_in_root(args['path'], ctx) if args.get('path') else ctx.cwd
        if not path.is_dir():
            raise ToolError(f'{path}: not a directory')

        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not entries:
            return Output(content='(empty directory)')

        rows = []
        for entry in entries:
            try:
                rows.append(f'{entry.name}/' if entry.is_dir() else f'{entry.name}\t{entry.stat().st_size}')
            except OSError:
                rows.append(f'{entry.name}\t?')

        body, cut = truncate('\n'.join(rows), 20_000, keep='head')
        return Output(content=body, display={'path': str(path), 'count': len(entries)}, truncated=cut)

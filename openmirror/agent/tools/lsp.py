"""Asking the language server.

Pointing at a symbol is the hard part for a model, and the protocol makes it
harder: a position is a zero-based line and a character offset counted in
UTF-16 code units. Nobody should have to count columns, and a small model
cannot. So the tool takes the line as `read_file` numbers it and the *name*
on that line, and works out the rest; `column` is there for the rare line
with the same name on it twice.

Three of the operations write: asking a server to fix a line, to rename a
symbol it knows is defined in nine files, and to format a file. Those are
edits, and they are made here — by a tool the policy has already graded,
approved, checkpointed and diffed like any other — rather than by the server.
The protocol's way around that, `workspace/applyEdit`, is answered
`applied: false` on the way past, and a fix that comes back as a command to
run inside the server is refused for the same reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from openmirror.agent.lsp import CodeAction, LspError, LspPool, WorkspaceEdit, apply_edit, uri_to_path
from openmirror.agent.tools.base import Assessment, Output, Tool, ToolContext, ToolError, resolve_in_root, truncate
from openmirror.agent.tools.files import _diff
from openmirror.protocol.agent import Risk

OPERATIONS = (
    'definition', 'references', 'hover', 'implementation', 'type_definition',
    'symbols', 'search', 'diagnostics', 'code_action', 'rename', 'format',
)
POSITIONAL = ('definition', 'references', 'hover', 'implementation', 'type_definition', 'code_action', 'rename')
METHODS = {
    'definition': 'textDocument/definition',
    'references': 'textDocument/references',
    'hover': 'textDocument/hover',
    'implementation': 'textDocument/implementation',
    'type_definition': 'textDocument/typeDefinition',
    'symbols': 'textDocument/documentSymbol',
    'search': 'workspace/symbol',
    'code_action': 'textDocument/codeAction',
    'rename': 'textDocument/rename',
    'format': 'textDocument/formatting',
}
# A range is a different method, with a different capability, and a server
# that formats a file but not a range answers one and not the other. In the
# order `supports()` takes them: the method, then the capability it declares.
RANGE_METHOD = ('textDocument/rangeFormatting', 'documentRangeFormattingProvider')
# What a server has to have said it does, for each question. Not every server
# does everything — pylsp has no project-wide search — and the question
# unasked, with something to do instead, beats the server's own error.
CAPABILITIES = {
    'definition': 'definitionProvider',
    'references': 'referencesProvider',
    'hover': 'hoverProvider',
    'implementation': 'implementationProvider',
    'type_definition': 'typeDefinitionProvider',
    'symbols': 'documentSymbolProvider',
    'search': 'workspaceSymbolProvider',
    'code_action': 'codeActionProvider',
    'rename': 'renameProvider',
    'format': 'documentFormattingProvider',
}
INSTEAD = {
    'search': 'Use grep for the name, or ask for its definition from somewhere it is used.',
    'implementation': 'Ask for its definition and references instead.',
    'type_definition': 'Ask for hover instead, which usually says the type.',
    'symbols': 'Use outline instead.',
    'code_action': 'There is no offered fix for that range; make the change yourself.',
    'rename': 'This server will not rename that, which usually means it does not know the symbol. '
              'Edit the name where it is defined, or ask for its references first.',
    'format': 'Run the project\'s own formatter through shell instead — ruff format, prettier, gofmt.',
}
# The operations that change files whatever the arguments say. `code_action` is
# not among them: listing what is on offer is a read, and only choosing one is
# a write. That difference is the whole reason the op takes an argument.
WRITE_OPS = frozenset({'rename', 'format'})
KINDS = {
    1: 'file', 2: 'module', 3: 'namespace', 4: 'package', 5: 'class', 6: 'method', 7: 'property',
    8: 'field', 9: 'constructor', 10: 'enum', 11: 'interface', 12: 'function', 13: 'variable',
    14: 'constant', 15: 'string', 16: 'number', 17: 'boolean', 18: 'array', 19: 'object', 20: 'key',
    21: 'null', 22: 'enum member', 23: 'struct', 24: 'event', 25: 'operator', 26: 'type parameter',
}
SEVERITY = {1: 'error', 2: 'warning', 3: 'info', 4: 'hint'}
MAX_RESULTS = 200

DESCRIPTION = (
    'Ask the language server about code: where something is defined, everywhere it is used, its '
    'type and documentation, what a file declares, a symbol anywhere in the project, or the errors '
    'the compiler sees in a file. Better than grep for anything with a name — it knows which '
    '`open` is which. Point at a symbol with `path`, `line` (as read_file numbers it) and `symbol`, '
    'the name on that line. '
    'It can also change files, using the project\'s own configuration: code_action lists the fixes '
    'the server offers for a line and applies one when you pass `action`, rename renames a symbol '
    'wherever it is defined and used, and format runs the configured formatter over a file. Those '
    'three are graded as file writes, so a planning or read-only session cannot use them.'
)


def _utf16(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _position(path: Path, args: dict[str, Any]) -> dict[str, int]:
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    number = int(args['line'])
    if not 1 <= number <= len(lines):
        raise ToolError(f'line {number} is outside {path.name}, which has {len(lines)} lines')
    text = lines[number - 1]

    symbol = str(args.get('symbol') or '').strip()
    if symbol:
        match = re.search(rf'(?<![\w$]){re.escape(symbol)}(?![\w$])', text)
        at = match.start() if match else text.find(symbol)
        if at < 0:
            raise ToolError(f'{symbol!r} is not on line {number}. That line is: {text.strip()}')
    elif args.get('column'):
        at = max(0, int(args['column']) - 1)
    else:
        at = len(text) - len(text.lstrip())
    return {'line': number - 1, 'character': _utf16(text[:at])}


def _at(args: dict[str, Any]) -> str:
    """What a positional call points at, the way the summary says it."""
    return str(args.get('symbol') or f'line {args.get("line")}')


def _range(path: Path, args: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """A range to ask about, from a line and optionally an end line.

    A range starts at column 0 unless a symbol or a column was given: a span
    of a document is not a guess at the first non-space character, and a
    formatter asked for lines 10 to 20 should not be handed one that begins at
    column 12. With no line at all it is the whole file, which is what
    `textDocument/formatting` wants anyway.
    """
    if not args.get('line'):
        return {'line': 0, 'character': 0}, {'line': 0, 'character': 0}
    found = _position(path, args)
    start = found if (args.get('symbol') or args.get('column')) else {'line': found['line'], 'character': 0}
    end = {'line': max(int(args['end_line']) - 1, start['line']), 'character': 0} if args.get('end_line') else start
    return start, end


def _choose(actions: list[CodeAction], wanted: str) -> CodeAction:
    """The action a model meant, from the number printed in the listing.

    A number first, because that is what it was shown. Then the title, loosely
    and case-insensitively, because a model asked to name a fix will
    paraphrase it — and a refusal that says what the options were is one the
    model can recover from, where "not found" is one it will retry blind.
    """
    text = wanted.strip()
    if text.isdigit():
        index = int(text)
        if 0 <= index < len(actions):
            return actions[index]
        raise ToolError(f'there is no action number {index} here. Ask for the list again and count from 0.')
    lowered = text.lower()
    for action in actions:
        if lowered and lowered in action.title.lower():
            return action
    raise ToolError(f'no action here matches {wanted!r}. Ask with operation=code_action and no action to see them.')


def _locations(result: Any) -> list[tuple[str, dict[str, Any]]]:
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if 'targetUri' in item:
            out.append((item['targetUri'], item.get('targetSelectionRange') or item.get('targetRange') or {}))
        elif 'uri' in item:
            out.append((item['uri'], item.get('range') or {}))
    return out


class _Lines:
    """Source lines by file, read once each, for quoting a result's line."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[Path, list[str]] = {}

    def name(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def at(self, path: Path, line: int) -> str:
        if path not in self._cache:
            try:
                self._cache[path] = path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                self._cache[path] = []
        lines = self._cache[path]
        return lines[line].strip() if 0 <= line < len(lines) else ''

    def where(self, uri: str, rng: dict[str, Any]) -> str:
        path = uri_to_path(uri)
        start = rng.get('start') or {}
        line = int(start.get('line', 0))
        quoted = self.at(path, line)
        quoted = quoted if len(quoted) <= 160 else quoted[:157] + '...'
        return f'{self.name(path)}:{line + 1}:{int(start.get("character", 0)) + 1}  {quoted}'


def _hover_text(result: Any) -> str:
    contents = result.get('contents') if isinstance(result, dict) else None
    if isinstance(contents, dict):
        return str(contents.get('value', ''))
    if isinstance(contents, list):
        return '\n\n'.join(c.get('value', '') if isinstance(c, dict) else str(c) for c in contents)
    return str(contents or '')


def _symbol_lines(items: list[dict[str, Any]], depth: int = 0) -> list[str]:
    lines: list[str] = []
    for item in items:
        kind = KINDS.get(item.get('kind', 0), 'symbol')
        rng = item.get('selectionRange') or item.get('range') or (item.get('location') or {}).get('range') or {}
        line = int((rng.get('start') or {}).get('line', 0)) + 1
        detail = f'  {item["detail"]}' if item.get('detail') else ''
        lines.append(f'{line:>5}  {"  " * depth}{kind:<11} {item.get("name", "?")}{detail}')
        if item.get('children'):
            lines.extend(_symbol_lines(item['children'], depth + 1))
    return lines


class LspTool(Tool):
    name = 'lsp'

    def __init__(self, pool: LspPool) -> None:
        self.pool = pool
        self.description = f'{DESCRIPTION} Available here for: {", ".join(pool.describe())}.'
        self.input_schema = {
            'type': 'object',
            'properties': {
                'operation': {'type': 'string', 'enum': list(OPERATIONS)},
                'path': {'type': 'string', 'description': 'The file. For search, any file in the language to search.'},
                'line': {'type': 'integer', 'description': 'The line, numbered from 1 as read_file shows it.'},
                'symbol': {'type': 'string', 'description': 'The name on that line to ask about.'},
                'column': {'type': 'integer', 'description': 'Instead of symbol: the column, from 1.'},
                'query': {'type': 'string', 'description': 'For search: the name, or part of it.'},
                'wait': {'type': 'integer', 'description': 'For diagnostics: seconds to wait for the server. Default 10.'},
                'action': {
                    'type': 'string',
                    'description': (
                        'For code_action: which one to apply — the number from the list, or part of its title. '
                        'Leave it out to be shown what is on offer instead, which changes nothing.'
                    ),
                },
                'only': {
                    'type': 'string',
                    'description': "For code_action: only offer actions of this kind, e.g. 'quickfix' or 'source.fixAll'.",
                },
                'new_name': {'type': 'string', 'description': 'For rename: the new name for the symbol on that line.'},
                'end_line': {
                    'type': 'integer',
                    'description': (
                        'For format (and for code_action): the last line of the range, from 1. Omit to format the '
                        'whole file, or to ask about the position alone.'
                    ),
                },
            },
            'required': ['operation'],
        }

    async def close(self) -> None:
        await self.pool.close()

    def _spec(self, args: dict[str, Any]) -> tuple[Any, str]:
        """The server for this call, or the reason there is none."""
        path = str(args.get('path') or '')
        if not path:
            if args.get('operation') == 'search' and len(self.pool.specs) == 1:
                return self.pool.specs[0], ''
            return None, 'path is required' + (' — any file in the language to search' if args.get('operation') == 'search' else '')
        spec = self.pool.spec_for(path)
        if spec is None:
            return None, (
                f'there is no language server here for {Path(path).suffix or "that kind of"} files. '
                f'There is one for: {", ".join(self.pool.describe())}'
            )
        return spec, ''

    def _capable(self, server: Any, op: str, args: dict[str, Any]) -> bool:
        """Whether the server said it answers this particular call.

        Formatting a range is a different method from formatting a file, and
        plenty of servers have one without the other — asking anyway is an
        error named after the protocol method, which reads to a model as a
        broken server rather than an absent feature.
        """
        if op == 'format' and args.get('end_line'):
            return server.supports(*RANGE_METHOD)
        return server.supports(METHODS[op], CAPABILITIES[op])

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        op = args.get('operation')
        if op not in OPERATIONS:
            return Assessment(risk=Risk.READ, summary='', invalid=f'operation must be one of {", ".join(OPERATIONS)}')
        spec, why = self._spec(args)
        if spec is None:
            return Assessment(risk=Risk.READ, summary='', invalid=why)
        if op in POSITIONAL and not args.get('line'):
            return Assessment(
                risk=Risk.READ, summary='', invalid=f'{op} needs a line — the line number as read_file shows it'
            )
        if op == 'search' and not str(args.get('query') or '').strip():
            return Assessment(risk=Risk.READ, summary='', invalid='search needs a query')
        if op == 'rename' and not str(args.get('new_name') or '').strip():
            return Assessment(risk=Risk.READ, summary='', invalid='rename needs a new_name')
        if args.get('end_line') and not args.get('line'):
            return Assessment(risk=Risk.READ, summary='', invalid='end_line needs a line to start from')

        path = args.get('path') or ''
        # Only the positional operations have a line, and this is built before
        # the branch that needs it, so it cannot read one.
        target = _at(args)
        if op == 'search':
            what = f'search for {args["query"]!r}'
        elif op == 'rename':
            what = f'rename {target} to {str(args["new_name"]).strip()!r} in {path}:{args["line"]}'
        elif op == 'format':
            what = f'format {path}:{args["line"]}-{args["end_line"]}' if args.get('end_line') else f'format {path}'
        elif op == 'code_action':
            what = (f'apply code action {args["action"]!r} in {path}:{args["line"]}' if args.get('action')
                    else f'list code actions in {path}:{args["line"]}')
        elif op in POSITIONAL:
            what = f'{op.replace("_", " ")} of {target} in {path}:{args["line"]}'
        else:
            what = f'{op} in {path}'

        # Graded from the arguments, because that is all the policy is given.
        # A listing of code actions is a read of the server's opinion; applying
        # one is a write of its edits, and the two are the same call with one
        # argument differing.
        writes = op in WRITE_OPS or (op == 'code_action' and bool(args.get('action')))

        if not self.pool.running(spec):
            # EXECUTE rather than READ for a first question because starting a
            # server can run the project's build scripts, and because it is
            # higher up the ladder than WRITE: auto_edit runs file writes and
            # asks about commands, so grading a call that does both as WRITE
            # would make the one that also *starts something* the safer of the
            # two to run unattended.
            note = 'a language server can run the project\'s own build scripts'
            if writes:
                note += ', and this one changes files besides'
            return Assessment(risk=Risk.EXECUTE, summary=f'start {spec.name} for this project, then {what}   ({note})')
        return Assessment(risk=Risk.WRITE if writes else Risk.READ, summary=what)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        op = args['operation']
        spec, why = self._spec(args)
        if spec is None:
            raise ToolError(why)
        path = resolve_in_root(args['path'], ctx) if args.get('path') else None
        if path is not None and not path.is_file():
            raise ToolError(f'{args["path"]}: no such file')

        try:
            server = await self.pool.server(spec)
            await server.settle(20)
            lines = _Lines(ctx.root)

            if op in CAPABILITIES and not self._capable(server, op, args):
                raise ToolError(
                    f'{spec.name} does not answer {op.replace("_", " ")} questions. {INSTEAD.get(op, "Use grep instead.")}'
                )

            if op == 'diagnostics':
                return await self._diagnostics(server, path, args, lines)

            if op == 'search':
                result = await server.request(METHODS['search'], {'query': str(args['query']).strip()})
                return self._search(server, result, args, lines)

            await server.sync(path)
            uri = path.as_uri()
            if op == 'symbols':
                result = await server.request(METHODS['symbols'], {'textDocument': {'uri': uri}})
                return self._symbols(server, result or [], path, lines)

            if op in WRITE_OPS or op == 'code_action':
                return await self._edit(server, op, path, args, ctx, lines)

            params: dict[str, Any] = {'textDocument': {'uri': uri}, 'position': _position(path, args)}
            if op == 'references':
                params['context'] = {'includeDeclaration': True}
            result = await server.request(METHODS[op], params)
        except LspError as exc:
            raise ToolError(str(exc)) from exc

        if op == 'hover':
            text = _hover_text(result).strip()
            if not text:
                return Output(content=self._nothing(server, 'nothing to say about that position'))
            body, cut = truncate(text, 6_000, keep='head')
            return Output(content=body, truncated=cut, display={'server': spec.name})

        found = _locations(result)
        if not found:
            return Output(content=self._nothing(server, f'no {op.replace("_", " ")} found'))
        rows = [lines.where(uri_, rng) for uri_, rng in found[:MAX_RESULTS]]
        head = f'{len(found)} result{"s" if len(found) != 1 else ""}'
        if len(found) > MAX_RESULTS:
            head += f', the first {MAX_RESULTS} shown'
        return Output(content=f'{head}:\n' + '\n'.join(rows), display={'server': spec.name, 'results': len(found)})

    def _nothing(self, server: Any, said: str) -> str:
        # An empty answer from a busy server is not an answer, and saying so
        # is the difference between the model trying again and the model
        # concluding the symbol does not exist.
        if server.busy:
            return f'{said.capitalize()} — but {server.spec.name} is still busy ({server.busy}), so ask again shortly.'
        return f'{said.capitalize()}.'

    # -- the operations that change files ------------------------------------

    async def _edit(
        self, server: Any, op: str, path: Path, args: dict[str, Any], ctx: ToolContext, lines: _Lines,
    ) -> Output:
        """`code_action`, `rename` and `format`: the three that write.

        Every one of them ends at `apply_edit`, which is the only route from
        this file to a write, and which will not write without the
        confinement the session handed it.
        """
        if op == 'code_action':
            start, end = _range(path, args)
            actions = await server.code_action(path, start, end, only=args.get('only'))
            if not args.get('action'):
                return self._actions(server, actions, path, lines)
            chosen = _choose(actions, str(args['action']))
            if chosen.disabled:
                raise ToolError(f'{chosen.title!r} cannot be applied here: {chosen.disabled}')
            if chosen.edit is None:
                raise ToolError(f'{chosen.title!r} comes back with no edit, so there is nothing to apply. Use edit_file.')
            return await self._apply(chosen.edit, path, lines, ctx, f'applied {chosen.title!r}')

        if op == 'rename':
            new_name = str(args['new_name']).strip()
            edit = await server.rename(path, _position(path, args), new_name)
            return await self._apply(edit, path, lines, ctx, f'renamed {_at(args)} to {new_name!r}')

        start, end = _range(path, args)
        if args.get('end_line'):
            edits = await server.range_formatting(path, start, end)
            what = f'formatted {lines.name(path)}:{args["line"]}-{args["end_line"]}'
        else:
            edits = await server.formatting(path)
            what = f'formatted {lines.name(path)}'
        if not edits:
            # No edits is the answer for a file that is already formatted and
            # for a server with no formatter configured, and the model cannot
            # tell those apart. Said so rather than claiming the second.
            return Output(
                content=f'{server.spec.name} had nothing to format in {lines.name(path)} — it is already '
                        'formatted, or this project has no formatter configured for it.',
                display={'server': server.spec.name, 'changed': 0},
            )
        return await self._apply(WorkspaceEdit.for_file(path.as_uri(), edits), path, lines, ctx, what)

    def _actions(self, server: Any, actions: list[CodeAction], path: Path, lines: _Lines) -> Output:
        """What is on offer, in the order the server offered it.

        The numbers are the point: the model is shown a list and then names
        one, and a list it has to re-derive by matching titles is a list it
        will get wrong.
        """
        if not actions:
            return Output(content=self._nothing(server, f'no code actions offered in {lines.name(path)}'))

        rows = []
        for number, action in enumerate(actions[:MAX_RESULTS]):
            kind = action.kind or 'action'
            if action.disabled:
                note = f'  (cannot be applied: {action.disabled})'
            elif not action.edit or not action.edit.files:
                note = '  (no edit attached)'
            else:
                files = len(action.edit.files)
                note = f'  (changes {files} file{"s" if files != 1 else ""})'
            rows.append(f'{number:>3}  {kind:<24} {action.title}{note}')

        body = '\n'.join(rows)
        more = f'\n…and {len(actions) - MAX_RESULTS} more' if len(actions) > MAX_RESULTS else ''
        return Output(
            content=(
                f'{len(actions)} code action{"s" if len(actions) != 1 else ""} offered in {lines.name(path)}:\n'
                f'{body}{more}\n\nApply one with action=<number>.'
            ),
            display={'server': server.spec.name, 'actions': len(actions)},
            truncated=len(actions) > MAX_RESULTS,
        )

    async def _apply(self, edit: WorkspaceEdit, path: Path, lines: _Lines, ctx: ToolContext, what: str) -> Output:
        """Hand an edit to the client, which writes it or none of it.

        `path` is the file the call was about, and goes in the display so the
        session's after-write hook reports the diagnostics for the file the
        model was working on — the hook reads one path, and a rename that
        touched nine files has to be summarised as well.
        """
        written = await apply_edit(edit, lambda uri: resolve_in_root(uri_to_path(uri), ctx), ctx.checkpoint)
        changed = [f for f in written if f.edits]
        if not changed:
            return Output(content=f'{what}: the server had nothing to change.', display={'path': str(path)})

        rows = [f'{lines.name(f.path)}  ({f.edits} change{"s" if f.edits != 1 else ""})' for f in written]
        diff = '\n'.join(_diff(f.before, f.after, lines.name(f.path)) for f in changed)
        return Output(
            content=f'{what} — {len(changed)} file{"s" if len(changed) != 1 else ""}:\n' + '\n'.join(rows),
            display={'path': str(path), 'diff': diff, 'files': [str(f.path) for f in changed]},
        )

    def _symbols(self, server: Any, result: list[dict[str, Any]], path: Path, lines: _Lines) -> Output:
        rows = _symbol_lines(result)
        if not rows:
            return Output(content=self._nothing(server, f'no symbols reported in {lines.name(path)}'))
        body, cut = truncate('\n'.join(rows[:600]), 40_000, keep='head')
        return Output(
            content=f'{lines.name(path)}: {len(rows)} symbols\n{body}',
            truncated=cut or len(rows) > 600,
            display={'server': server.spec.name, 'symbols': len(rows)},
        )

    def _search(self, server: Any, result: Any, args: dict[str, Any], lines: _Lines) -> Output:
        items = result or []
        if not items:
            return Output(content=self._nothing(server, f'nothing called {args["query"]!r} found'))
        rows = []
        for item in items[:MAX_RESULTS]:
            loc = item.get('location') or {}
            kind = KINDS.get(item.get('kind', 0), 'symbol')
            where = lines.where(loc['uri'], loc.get('range') or {}) if loc.get('uri') else ''
            rows.append(f'{item.get("name", "?")}  ({kind})  {where}'.rstrip())
        return Output(
            content=f'{len(items)} match{"es" if len(items) != 1 else ""}:\n' + '\n'.join(rows),
            display={'server': server.spec.name, 'results': len(items)},
        )

    async def _diagnostics(self, server: Any, path: Path | None, args: dict[str, Any], lines: _Lines) -> Output:
        if path is None:
            raise ToolError('diagnostics needs a path')
        wait = max(1, min(int(args.get('wait') or 10), 120))
        items = await server.diagnostics_for(path, wait)
        name = lines.name(path)
        if items is None:
            busy = f' — it is still {server.busy}' if server.busy else ''
            return Output(
                content=f'{server.spec.name} has not reported on {name} yet{busy}. Ask again in a moment, '
                        'or give wait more seconds.'
            )
        if not items:
            return Output(content=f'No problems reported in {name}.', display={'server': server.spec.name, 'problems': 0})

        items = sorted(items, key=lambda d: (d.get('severity', 1), d['range']['start']['line']))
        rows = []
        for d in items[:MAX_RESULTS]:
            start = d['range']['start']
            where = f'{name}:{start["line"] + 1}:{start.get("character", 0) + 1}'
            code = d.get('code')
            tag = ' '.join(str(x) for x in (d.get('source'), code) if x)
            message = ' '.join(str(d.get('message', '')).split())
            rows.append(f'{where}  {SEVERITY.get(d.get("severity", 1), "error")}  {message}' + (f'  [{tag}]' if tag else ''))
        errors = sum(1 for d in items if d.get('severity', 1) == 1)
        return Output(
            content=f'{len(items)} problem{"s" if len(items) != 1 else ""} ({errors} error{"s" if errors != 1 else ""}):\n'
                    + '\n'.join(rows),
            display={'server': server.spec.name, 'problems': len(items), 'errors': errors},
        )

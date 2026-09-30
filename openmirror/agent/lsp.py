"""A language-server client, for the questions grep cannot answer.

`grep open` finds every line with the word in it. A language server knows
which of those is the method on this class and which is the builtin, what
type the thing under the cursor is, where it was defined three imports away —
and which of the agent's edits just broke the build, before anything is run.
That is worth a process per language.

The protocol is JSON-RPC over stdio with a Content-Length header per message.
Written here rather than taken from a library for the same reason as the MCP
client: the part a client needs is small, and the servers are the moving part.

Three properties matter more than coverage.

**Starting a server can run the project's code.** rust-analyzer runs build
scripts and proc macros; others load plugins the project's own config names.
So the first question in a language with no server running is graded
`execute` by the tool, and asked about under the default policy, and every
question after that is a read. The grade follows whether the server is up,
because that is the fact that decides the risk.

**Nothing waits for ever, and nothing pretends.** A server that is still
indexing answers with nothing — which looks exactly like "this has no
definition". Requests time out, the client tracks the server's own progress
reports, and an empty answer given while it is busy says so.

**An edit the server offers is an edit, and it is made here.** Code actions,
renames and formatting all come back as text edits with positions in them,
which is the one part of the protocol where being wrong does not return
nothing — it rewrites a file. So the writes go through `apply_edit`, which
takes the caller's own path confinement as a parameter and will not write a
byte without it, resolves every file before touching any of them, and
refuses the whole edit if one path lands outside the root. The server's own
`workspace/applyEdit` — the way around all of this — is answered
`applied: false`, and a fix that arrives as a command to run inside the
server is refused for the same reason.
"""

from __future__ import annotations

import asyncio
import collections
import collections.abc
import contextlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

log = logging.getLogger(__name__)

STARTUP_TIMEOUT = 60.0
REQUEST_TIMEOUT = 30.0


class LspError(RuntimeError):
    pass


@dataclass(slots=True)
class ServerSpec:
    name: str
    command: list[str]
    extensions: tuple[str, ...]
    language: str = ''
    init_options: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    # Where the binary is, once it has been found on PATH. A spec with none is
    # a server this machine does not have.
    binary: str = ''


# ---------------------------------------------------------------------------
# What comes back, in shapes of our own
#
# The protocol's answers are all dicts, and the two that change files carry
# positions that mean nothing without the text they are positions in. These
# hold the parts worth keeping and drop the rest, so the tool that renders them
# never has to know which of the two forms a server chose.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TextEdit:
    """One replacement, in the protocol's own coordinates.

    Left as a line and a UTF-16 character offset rather than an index into a
    string, because that is what arrives and turning it into an index needs
    the file — which is on disk, and which a number cannot follow.
    """

    start_line: int
    start_character: int
    end_line: int
    end_character: int
    new_text: str


@dataclass(slots=True)
class FileEdits:
    uri: str
    edits: list[TextEdit] = field(default_factory=list)


@dataclass(slots=True)
class WorkspaceEdit:
    """Changes to one or more files, whichever form they arrived in."""

    files: list[FileEdits] = field(default_factory=list)
    # `changes` is the older map of uri to text edits; `documentChanges` the
    # newer ordered list, and the only one that can also create, rename or
    # delete a file. Those arrive as `file_ops`, and they are refused rather
    # than quietly dropped — see `apply_edit`.
    document_changes: bool = False
    file_ops: list[tuple[str, str]] = field(default_factory=list)
    # The payload as the server sent it, so which form it chose is still
    # visible after parsing and nothing is thrown away a caller might want.
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_file(cls, uri: str, edits: list[TextEdit], raw: Any = None) -> WorkspaceEdit:
        """The one-file case, which is what formatting comes back as."""
        return cls(files=[FileEdits(uri, list(edits))], raw=raw if isinstance(raw, dict) else {})


@dataclass(slots=True)
class CodeAction:
    """One thing the server offers to do about a range."""

    title: str
    kind: str = ''
    edit: WorkspaceEdit | None = None
    # Why it cannot be applied: the server's own `disabled`, or this client
    # refusing to run a command. Empty when it can be.
    disabled: str = ''


@dataclass(slots=True)
class AppliedFile:
    path: Path
    before: str
    after: str
    edits: int


# What each extension is called in the protocol. Servers use it to decide how
# to parse a file, so `.tsx` has to be typescriptreact and not typescript.
LANGUAGE_IDS = {
    '.py': 'python', '.pyi': 'python',
    '.ts': 'typescript', '.mts': 'typescript', '.cts': 'typescript', '.tsx': 'typescriptreact',
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript', '.jsx': 'javascriptreact',
    '.rs': 'rust', '.go': 'go',
    '.c': 'c', '.h': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp', '.hh': 'cpp', '.hxx': 'cpp',
}

# (language, extensions, candidate commands). The first candidate installed
# wins. Deliberately the servers people already have rather than a list of
# everything that exists: an entry for a server nobody installs is a line of
# config nobody tests.
DEFAULTS: tuple[tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]], ...] = (
    ('Python', ('.py', '.pyi'), (
        ('basedpyright-langserver', '--stdio'),
        ('pyright-langserver', '--stdio'),
        ('pylsp',),
        ('jedi-language-server',),
    )),
    ('TypeScript and JavaScript', ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs'), (
        ('typescript-language-server', '--stdio'),
    )),
    ('Rust', ('.rs',), (('rust-analyzer',),)),
    ('Go', ('.go',), (('gopls',),)),
    ('C and C++', ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.hxx'), (('clangd',),)),
)

CLIENT_CAPABILITIES: dict[str, Any] = {
    'general': {'positionEncodings': ['utf-16']},
    # Asked for so that servers report their indexing — the only way to tell
    # an empty answer from an answer that is not ready.
    'window': {'workDoneProgress': True},
    'workspace': {'workspaceFolders': True, 'configuration': True, 'symbol': {}},
    'textDocument': {
        'synchronization': {'didSave': True},
        'definition': {'linkSupport': True},
        'typeDefinition': {'linkSupport': True},
        'implementation': {'linkSupport': True},
        'references': {},
        'hover': {'contentFormat': ['markdown', 'plaintext']},
        'documentSymbol': {'hierarchicalDocumentSymbolSupport': True},
        'publishDiagnostics': {},
        'diagnostic': {},
        # Declared with the kinds a server should offer, because a client that
        # says nothing here gets `Command[]` back instead of `CodeAction[]` —
        # and a command is a request to have the server do the editing, which
        # is the side door this file closes on workspace/applyEdit. A server
        # that is told it may return edits returns edits.
        'codeAction': {
            'codeActionLiteralSupport': {'codeActionKind': {'valueSet': [
                '', 'quickfix', 'refactor', 'refactor.extract', 'refactor.inline',
                'refactor.rewrite', 'source', 'source.organizeImports', 'source.fixAll',
            ]}},
        },
        # False, because prepareRename is a request this client does not
        # answer, and a server told to prepare will ask and wait.
        'rename': {'prepareProvider': False},
        'documentFormatting': {},
        'documentRangeFormatting': {},
    },
}


def language_id(path: Path) -> str:
    return LANGUAGE_IDS.get(path.suffix.lower(), path.suffix.lower().lstrip('.'))


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path)) if parsed.scheme == 'file' else Path(uri)


# ---------------------------------------------------------------------------
# Positions, edits, and putting them on disk
# ---------------------------------------------------------------------------


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _utf16_offset(line: str, character: int) -> int:
    """The index into `line` of a character offset counted in UTF-16 code units.

    The protocol counts positions in code units and Python indexes by code
    point, and the two disagree on anything outside the BMP: an emoji is one
    code point and two code units, so every column after one on that line is a
    character out if the server's number is used as a Python index. That is not
    a rare encoding — it is a character in a comment on the line above the
    edit — so the conversion is done by measuring, not by assuming.

    A `character` that lands in the middle of a surrogate pair is a server's
    bug, and decoding with `surrogatepass` turns it into the code point it
    belongs to instead of an exception in the middle of a write.
    """
    if character <= 0:
        return 0
    encoded = line.encode('utf-16-le')
    if character * 2 >= len(encoded):
        # Past the end of this line, which formatters do on purpose.
        return len(line)
    return len(encoded[: character * 2].decode('utf-16-le', 'surrogatepass'))


def _line_starts(lines: list[str]) -> list[int]:
    """Where each line begins, and one past the last.

    The extra entry is the end of the document, which the protocol addresses as
    a line one beyond the last one. `keepends` is what makes the arithmetic
    come out: the newline belongs to the line, so the next line starts after it.
    """
    starts, at = [], 0
    for line in lines:
        starts.append(at)
        at += len(line)
    starts.append(at)
    return starts


def apply_text_edits(text: str, edits: collections.abc.Sequence[TextEdit]) -> str:
    """`text` with `edits` applied, under the one rule the protocol sets about them.

    Every range in a set of text edits refers to the document as it was
    *before* any of them, so the positions do not move as the edits land.
    Which is why they are applied from the last one backwards: in the order a
    server listed them, the first edit would shift the offsets of the rest and
    a rename would land a character along from where it was meant to.
    """
    original = text
    lines = original.splitlines(keepends=True)
    starts = _line_starts(lines)

    def at(line: int, character: int) -> int:
        if not lines:
            return 0
        if line >= len(lines):
            # One past the last line is how the protocol addresses the end of
            # the document, and it is an offset like any other. Clamping it to
            # the last line instead would put the end of a whole-file edit at
            # the start of that line, which is a file with its last line
            # duplicated.
            return starts[-1]
        row = _clamp(line, 0, len(lines) - 1)
        return starts[row] + _utf16_offset(lines[row], character)

    spans = sorted(
        ((at(e.start_line, e.start_character), at(e.end_line, e.end_character), e.new_text) for e in edits),
        reverse=True,
    )
    for (start, _end, _), (_earlier, earlier_end, _) in zip(spans, spans[1:], strict=False):
        if start < earlier_end:
            # Forbidden by the protocol, and there is no right order to apply
            # them in: the file that came out would be a coin toss.
            raise LspError('two of the edits this server sent overlap, so none of them were applied')

    for start, end, new_text in spans:
        original = original[:start] + new_text + original[end:]
    return original


def _read_one(path: Path, edits: collections.abc.Sequence[TextEdit]) -> tuple[str, str]:
    """What the file says now, and what it would say after these edits."""
    try:
        before = path.read_text(encoding='utf-8')
    except FileNotFoundError as exc:
        raise LspError(f'{path}: the server wants to edit a file that is not there') from exc
    except UnicodeDecodeError as exc:
        # Not an OSError, and the one refusal here that is about safety rather
        # than arithmetic: writing back a file this could not decode would
        # replace every byte it did not understand with U+FFFD, which is a
        # corruption wearing the costume of a fix.
        raise LspError(f'{path}: not text this client can rewrite ({exc})') from exc
    except OSError as exc:
        raise LspError(f'{path}: cannot be read to edit it ({exc})') from exc
    return before, apply_text_edits(before, edits)


def _write_one(path: Path, before: str, after: str, count: int, checkpoint: Any) -> AppliedFile:
    if after != before:
        if checkpoint is not None:
            checkpoint.record(path)
        path.write_text(after, encoding='utf-8')
    return AppliedFile(path, before, after, count if after != before else 0)


async def apply_edit(
    edit: WorkspaceEdit,
    resolve: collections.abc.Callable[[str], Path],
    checkpoint: Any = None,
) -> list[AppliedFile]:
    """Put an edit on disk, or none of it.

    `resolve` is the caller's confinement: it turns a URI into a path this
    session is allowed to write, or raises. It is a parameter rather than a
    root because the rule is not the client's to know — an extra allowed
    directory, or no confinement at all, is a property of the session — and
    because a function that cannot be called without one has no route to the
    disk that skipped the check.

    Every file is resolved and worked out before the first byte is written.
    That is what makes the promise above true of the failures that matter — a
    path outside the root, a file that is not there, one that is not text, two
    edits that overlap — rather than only of the ones that happen to come
    first. Half a rename is a workspace that does not compile, and it is much
    harder to notice than an edit that was refused.
    """
    if edit.file_ops:
        wanted = ', '.join(f'{what} {uri}' for what, uri in edit.file_ops)
        raise LspError(
            f'the server also wants to {wanted}, which this client does not do — it applies text edits and '
            'nothing else, so none of this edit was written'
        )

    targets: list[tuple[Path, list[TextEdit]]] = []
    outside: list[str] = []
    for group in edit.files:
        if not group.edits:
            continue
        try:
            targets.append((resolve(group.uri), group.edits))
        except Exception as exc:  # noqa: BLE001 - the caller's own refusal, passed on in its words
            outside.append(f'{group.uri} ({exc})')
    if outside:
        raise LspError(
            f'this edit reaches outside the working root, so none of it was written: {"; ".join(outside)}'
        )

    prepared: list[tuple[Path, str, str, int]] = []
    for path, edits in targets:
        before, after = await asyncio.to_thread(_read_one, path, edits)
        prepared.append((path, before, after, len(edits)))

    written: list[AppliedFile] = []
    for path, before, after, count in prepared:
        written.append(await asyncio.to_thread(_write_one, path, before, after, count, checkpoint))
    return written


def _text_edits(items: Any) -> list[TextEdit]:
    out: list[TextEdit] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rng = item.get('range') or {}
        start, end = rng.get('start') or {}, rng.get('end') or {}
        out.append(TextEdit(
            start_line=int(start.get('line', 0)),
            start_character=int(start.get('character', 0)),
            end_line=int(end.get('line', 0)),
            end_character=int(end.get('character', 0)),
            new_text=str(item.get('newText', '')),
        ))
    return out


def parse_workspace_edit(raw: Any) -> WorkspaceEdit:
    """The two forms the protocol allows, as one shape.

    `changes` is the older map of uri to text edits; `documentChanges` the
    newer ordered list, and the only one that can create, rename or delete a
    file. A server that sends both is using the list, which is what the spec
    says and which is the one with the newer capabilities in it.
    """
    if not isinstance(raw, dict):
        return WorkspaceEdit()
    edit = WorkspaceEdit(document_changes='documentChanges' in raw, raw=raw)

    changes = raw.get('changes') or {}
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            edit.files.append(FileEdits(uri, _text_edits(edits)))

    for change in raw.get('documentChanges') or []:
        if not isinstance(change, dict):
            continue
        if 'edits' in change:
            document = change.get('textDocument') or {}
            edit.files.append(FileEdits(str(document.get('uri', '')), _text_edits(change['edits'])))
        elif 'kind' in change:
            # A create, rename or delete. Kept, never applied: doing it would
            # be this client reaching for a file the agent never named.
            where = str(change.get('oldUri') or change.get('uri', ''))
            edit.file_ops.append((str(change.get('kind', 'change')), where))
    return edit


def _point(position: dict[str, Any]) -> tuple[int, int]:
    return int(position.get('line', 0)), int(position.get('character', 0))


def _configured(path: Path | None) -> list[tuple[ServerSpec, bool]]:
    """Servers named in a `.lsp.json`, as (spec, disabled).

        {"servers": {
            "pyright": {"command": "pyright-langserver", "args": ["--stdio"],
                        "extensions": [".py"], "initializationOptions": {}},
            "clangd":  {"disabled": true, "extensions": [".c", ".h"]}
        }}

    A server listed here wins its extensions over the defaults, and a disabled
    one takes them from the defaults without replacing them — which is how to
    say "not for C, thank you" without uninstalling anything.
    """
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning('%s could not be read: %s', path, exc)
        return []
    servers = data.get('servers', data) if isinstance(data, dict) else {}
    out: list[tuple[ServerSpec, bool]] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        exts = tuple(e if e.startswith('.') else f'.{e}' for e in entry.get('extensions') or [])
        command = entry.get('command') or ''
        argv = ([command] if isinstance(command, str) else list(command)) + list(entry.get('args') or [])
        if not exts or (not argv[0] if argv else True) and not entry.get('disabled'):
            log.warning('%s: server %s needs a command and extensions; skipped', path, name)
            continue
        out.append((
            ServerSpec(
                name=name,
                command=argv or [name],
                extensions=exts,
                language=str(entry.get('language') or name),
                init_options=dict(entry.get('initializationOptions') or {}),
                env={str(k): str(v) for k, v in (entry.get('env') or {}).items()},
            ),
            bool(entry.get('disabled')),
        ))
    return out


# Whether a rustup proxy has anything behind it, by tool name. Asked once per
# process, because asking is a subprocess.
_PROXIED: dict[str, bool] = {}


def _usable(command: str, binary: str) -> bool:
    """Whether a binary found on PATH is really there.

    The case worth checking is rustup's: it links a proxy into ~/.cargo/bin
    for every tool it knows about, installed or not, and the proxy for a
    missing component exits at once with "unknown binary". Found on this
    project's own development machine, where `rust-analyzer` resolved and
    had never been installed. Offered anyway, the tool fails on first use
    with an error a model reads as the code being broken.
    """
    real = os.path.realpath(binary)
    if Path(real).stem != 'rustup':
        return True
    if command not in _PROXIED:
        try:
            done = subprocess.run([real, 'which', command], capture_output=True, timeout=10, check=False)
            _PROXIED[command] = done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _PROXIED[command] = False
    return _PROXIED[command]


def find_servers(config: Path | None = None) -> list[ServerSpec]:
    """The language servers this machine actually has, configured ones first."""
    specs: list[ServerSpec] = []
    claimed: set[str] = set()

    for spec, disabled in _configured(config):
        claimed.update(spec.extensions)
        if disabled:
            continue
        binary = shutil.which(spec.command[0])
        if binary is None or not _usable(spec.command[0], binary):
            log.warning('language server %s: %r is not installed', spec.name, spec.command[0])
            continue
        spec.binary = binary
        specs.append(spec)

    for language, extensions, candidates in DEFAULTS:
        free = tuple(e for e in extensions if e not in claimed)
        if not free:
            continue
        for command in candidates:
            binary = shutil.which(command[0])
            if binary and _usable(command[0], binary):
                specs.append(ServerSpec(
                    name=command[0], command=list(command), extensions=free, language=language, binary=binary,
                ))
                claimed.update(free)
                break
    return specs


def _read_if_changed(path: Path, mtime: int | None) -> tuple[int, str | None]:
    """The file's mtime, and its text if that is not `mtime` any more."""
    now = path.stat().st_mtime_ns
    if now == mtime:
        return now, None
    return now, path.read_text(encoding='utf-8', errors='replace')


class LanguageServer:
    """One running server, for one working root."""

    def __init__(self, spec: ServerSpec, root: Path) -> None:
        self.spec = spec
        self.root = root
        self.capabilities: dict[str, Any] = {}
        # uri -> the latest diagnostics pushed for it, and how many pushes
        # there have been, so a caller can wait for the next one.
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._pushes: dict[str, int] = {}
        self._pushed = asyncio.Condition()
        # uri -> (version, mtime_ns) of what the server has been sent.
        self._opened: dict[str, tuple[int, int]] = {}
        # Methods the server took on after starting, through
        # client/registerCapability, rather than declaring up front.
        self.registered: set[str] = set()
        # token -> title, for work the server says it is doing.
        self._progress: dict[str, str] = {}
        self._progress_changed = asyncio.Event()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._started = 0.0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def busy(self) -> str:
        """What the server says it is in the middle of, or ''."""
        return ', '.join(sorted(set(self._progress.values())))

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.spec.binary or self.spec.command[0],
            *self.spec.command[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.root),
            env={**os.environ, **self.spec.env},
        )
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._drain_stderr())]
        try:
            result = await self.request('initialize', {
                'processId': os.getpid(),
                'clientInfo': {'name': 'openmirror', 'version': '0.1.0'},
                'rootUri': self.root.as_uri(),
                'rootPath': str(self.root),
                'workspaceFolders': [{'uri': self.root.as_uri(), 'name': self.root.name}],
                'initializationOptions': self.spec.init_options or None,
                'capabilities': CLIENT_CAPABILITIES,
            }, wait=STARTUP_TIMEOUT)
        except (LspError, TimeoutError) as exc:
            said = '\n'.join(self._stderr)
            await self.stop()
            raise LspError(
                f'{self.spec.name} did not start: {exc or "no answer to initialize"}'
                + (f'\n\nIt said:\n{said}' if said else '')
            ) from exc
        self.capabilities = (result or {}).get('capabilities') or {}
        encoding = self.capabilities.get('positionEncoding') or (self.capabilities.get('general') or {}).get(
            'positionEncoding'
        )
        if encoding and encoding != 'utf-16':
            # Every position in this file — the ones the tool sends and the ones
            # the server sends back — is counted in UTF-16 code units, and the
            # client said so in its capabilities. A server that picked
            # something else needs a client that negotiated; said out loud
            # rather than acted on, because an edit a few characters along is
            # worse than a warning nobody reads.
            log.warning('lsp: %s chose %s positions; this client counts in utf-16', self.spec.name, encoding)
        await self.notify('initialized', {})
        self._started = asyncio.get_running_loop().time()
        log.info('lsp: %s started for %s', self.spec.name, self.root)

    async def stop(self) -> None:
        if self.running:
            # The protocol's own goodbye first. A server that is killed
            # instead can leave a lock file or a half-written index behind.
            with contextlib.suppress(Exception):
                await self.request('shutdown', None, wait=3)
            with contextlib.suppress(Exception):
                await self.notify('exit', None)

        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

        for task in self._tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._abandon('the language server was stopped')

    async def settle(self, seconds: float) -> None:
        """Wait, up to `seconds`, for the server to finish the work it has announced.

        A server announces its indexing a moment after answering initialize,
        so a question asked inside that moment gets an empty answer that looks
        like a real one. The first second is waited out for that reason.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        grace = self._started + 1.0
        while True:
            # Cleared before the condition is read, so a report that arrives
            # after the check still wakes the wait below.
            self._progress_changed.clear()
            now = loop.time()
            until = deadline if self._progress else min(grace, deadline)
            if now >= until:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._progress_changed.wait(), timeout=until - now)

    # -- wire ---------------------------------------------------------------

    async def request(self, method: str, params: Any, wait: float = REQUEST_TIMEOUT) -> Any:
        if not self.running:
            raise LspError(f'{self.spec.name} is not running')
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        payload: dict[str, Any] = {'jsonrpc': '2.0', 'id': msg_id, 'method': method}
        if params is not None:
            payload['params'] = params
        try:
            await self._send(payload)
            return await asyncio.wait_for(future, timeout=wait)
        except TimeoutError as exc:
            raise LspError(f'{self.spec.name} did not answer {method} within {wait:.0f}s') from exc
        finally:
            self._pending.pop(msg_id, None)

    async def notify(self, method: str, params: Any) -> None:
        payload: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise LspError(f'{self.spec.name} is not running')
        body = json.dumps(payload).encode('utf-8')
        self._proc.stdin.write(f'Content-Length: {len(body)}\r\n\r\n'.encode('ascii') + body)
        try:
            await self._proc.stdin.drain()
        except (ConnectionError, BrokenPipeError) as exc:
            raise LspError(f'{self.spec.name} has exited') from exc

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        try:
            while True:
                length = 0
                while True:
                    line = await stdout.readline()
                    if not line:
                        return
                    text = line.decode('ascii', 'replace').strip()
                    if not text:
                        break
                    key, _, value = text.partition(':')
                    if key.strip().lower() == 'content-length':
                        length = int(value.strip() or 0)
                if not length:
                    continue
                body = await stdout.readexactly(length)
                try:
                    message = json.loads(body)
                except json.JSONDecodeError:
                    log.debug('lsp %s: undecodable message', self.spec.name)
                    continue
                await self._dispatch(message)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            self._abandon('the language server exited')

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode('utf-8', 'replace').rstrip()
            self._stderr.append(text)
            log.debug('lsp %s: %s', self.spec.name, text)

    def _abandon(self, why: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LspError(f'{self.spec.name}: {why}'))
        self._pending.clear()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get('method')
        if method is None:
            future = self._pending.get(message.get('id'))
            if future is not None and not future.done():
                if 'error' in message:
                    error = message['error'] or {}
                    future.set_exception(LspError(f'{self.spec.name}: {error.get("message", "error")}'))
                else:
                    future.set_result(message.get('result'))
            return

        params = message.get('params') or {}
        if 'id' in message:
            # A request from the server. Every one of them gets an answer —
            # a server left waiting on its own request can stall the next
            # thing it was asked.
            await self._send({'jsonrpc': '2.0', 'id': message['id'], 'result': self._answer(method, params)})
            return

        if method == 'textDocument/publishDiagnostics':
            uri = params.get('uri', '')
            self.diagnostics[uri] = params.get('diagnostics') or []
            self._pushes[uri] = self._pushes.get(uri, 0) + 1
            async with self._pushed:
                self._pushed.notify_all()
        elif method == '$/progress':
            token = str(params.get('token'))
            value = params.get('value') or {}
            if value.get('kind') == 'begin':
                self._progress[token] = value.get('title') or 'working'
            elif value.get('kind') == 'end':
                self._progress.pop(token, None)
            self._progress_changed.set()
        elif method in ('window/logMessage', 'window/showMessage'):
            log.debug('lsp %s: %s', self.spec.name, params.get('message', ''))

    def supports(self, method: str, capability: str) -> bool:
        """Whether the server said it answers `method`, up front or since.

        Checked before asking, because the answer to an unsupported question
        is an error named after the protocol method — "Method Not Found:
        workspace/symbol", from pylsp — which gives a model nothing to act on.
        """
        return bool(self.capabilities.get(capability)) or method in self.registered

    def _answer(self, method: str, params: dict[str, Any]) -> Any:
        if method == 'client/registerCapability':
            for registration in params.get('registrations') or []:
                self.registered.add(str(registration.get('method', '')))
            return None
        if method == 'workspace/configuration':
            # "Nothing configured" for every section asked about. Null is what
            # the protocol says to send, and every server takes its defaults.
            return [None] * len(params.get('items') or [])
        if method == 'workspace/workspaceFolders':
            return [{'uri': self.root.as_uri(), 'name': self.root.name}]
        if method == 'workspace/applyEdit':
            # The agent changes files through its own tools, where they are
            # graded, approved and checkpointed. A server does not get a side
            # door around that.
            return {'applied': False, 'failureReason': 'this client does not apply edits'}
        return None

    # -- documents ----------------------------------------------------------

    async def sync(self, path: Path) -> bool:
        """Tell the server what the file says now. True if anything was sent."""
        uri = path.as_uri()
        known = self._opened.get(uri)
        try:
            mtime, text = await asyncio.to_thread(_read_if_changed, path, known[1] if known else None)
        except OSError as exc:
            raise LspError(f'{path}: {exc}') from exc
        if text is None:
            return False

        if known is None:
            await self.notify('textDocument/didOpen', {
                'textDocument': {'uri': uri, 'languageId': language_id(path), 'version': 1, 'text': text},
            })
            self._opened[uri] = (1, mtime)
        else:
            version = known[0] + 1
            await self.notify('textDocument/didChange', {
                'textDocument': {'uri': uri, 'version': version},
                'contentChanges': [{'text': text}],
            })
            # Saved as well as changed: several servers only re-check on save.
            await self.notify('textDocument/didSave', {'textDocument': {'uri': uri}, 'text': text})
            self._opened[uri] = (version, mtime)
        return True

    async def diagnostics_for(self, path: Path, wait: float) -> list[dict[str, Any]] | None:
        """The problems in a file, or None if the server said nothing in time.

        None and an empty list are different answers, and the difference is
        the one that matters: "no errors" from a server that has not looked
        yet is the most misleading thing this could return.
        """
        uri = path.as_uri()
        before = self._pushes.get(uri, 0)
        changed = await self.sync(path)

        if self.capabilities.get('diagnosticProvider'):
            with contextlib.suppress(LspError):
                report = await self.request(
                    'textDocument/diagnostic', {'textDocument': {'uri': uri}}, wait=max(wait, 5)
                )
                if isinstance(report, dict) and report.get('kind') == 'full':
                    return list(report.get('items') or [])

        if not changed and uri in self.diagnostics:
            return self.diagnostics[uri]
        if await self._wait_push(uri, before, wait):
            return self.diagnostics.get(uri, [])
        return self.diagnostics.get(uri) if uri in self.diagnostics and not changed else None

    async def _wait_push(self, uri: str, since: int, seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        async with self._pushed:
            while self._pushes.get(uri, 0) <= since:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._pushed.wait(), timeout=remaining)
                except TimeoutError:
                    return False
        return True

    # -- changes ------------------------------------------------------------

    async def code_action(
        self,
        path: Path,
        start: dict[str, int],
        end: dict[str, int] | None = None,
        only: str | collections.abc.Sequence[str] | None = None,
    ) -> list[CodeAction]:
        """What the server would do about this range, in its own words.

        The diagnostics sent alongside are the ones it has actually published
        for this file that touch the range, and not a made-up range: a server
        offered a diagnostic it never reported answers with nothing, and "this
        line has no fix" is then a statement about the wrong diagnostic.
        """
        uri = path.as_uri()
        end = end or start
        sent = [
            d for d in self.diagnostics.get(uri, [])
            if isinstance(d, dict) and _overlaps(d.get('range') or {}, start, end)
        ]
        context: dict[str, Any] = {'diagnostics': sent}
        if only:
            context['only'] = [only] if isinstance(only, str) else list(only)
        result = await self.request('textDocument/codeAction', {
            'textDocument': {'uri': uri},
            'range': {'start': start, 'end': end},
            'context': context,
        })

        out: list[CodeAction] = []
        for item in result or []:
            if not isinstance(item, dict):
                continue
            raw = item.get('edit')
            # Not `parse_workspace_edit(raw) or None`: a dataclass is always
            # true whatever is in it, so an action with no edit at all would
            # come back looking like one whose edit is empty, and the two get
            # different answers from the model.
            action = CodeAction(
                title=str(item.get('title', '')),
                kind=str(item.get('kind', '')),
                edit=parse_workspace_edit(raw) if isinstance(raw, dict) else None,
            )
            out.append(self._unusable(action, item))
        return out

    @staticmethod
    def _unusable(action: CodeAction, item: dict[str, Any]) -> CodeAction:
        """Fill in why an action cannot be applied, if it cannot.

        Two reasons, one field. The server's own `disabled` — a string since
        3.17, an object with a reason or a bare boolean before that — and a
        command, which asks the server to do the work itself. A command is
        workspace/applyEdit with the door shut from the other side, so it is
        refused in the same breath, and the reason the model reads names which
        of the two it was.
        """
        why = item.get('disabled')
        if isinstance(why, dict):
            action.disabled = str(why.get('reason', ''))
        elif isinstance(why, str):
            action.disabled = why
        elif why:
            action.disabled = 'the server says it cannot be applied, without saying why'
        command = item.get('command')
        if command and not action.disabled:
            name = (command.get('command', '?') if isinstance(command, dict) else str(command))
            action.disabled = f'it runs a command inside the server ({name}), which this client will not do'
        return action

    async def rename(self, path: Path, position: dict[str, int], new_name: str) -> WorkspaceEdit:
        """The changes that rename one symbol everywhere it appears.

        Null rather than an error is how the protocol says "not that one", and
        a model told that a symbol cannot be renamed will edit the name
        itself, where a server failure sends it looking for a bug.
        """
        result = await self.request('textDocument/rename', {
            'textDocument': {'uri': path.as_uri()}, 'position': position, 'newName': new_name,
        })
        if not isinstance(result, dict):
            raise LspError(f'{self.spec.name} will not rename that')
        return parse_workspace_edit(result)

    async def formatting(self, path: Path, options: dict[str, Any] | None = None) -> list[TextEdit]:
        """The edits that format a whole file. Empty when it is already formatted."""
        return _text_edits(await self.request('textDocument/formatting', {
            'textDocument': {'uri': path.as_uri()}, 'options': options or {},
        }))

    async def range_formatting(
        self, path: Path, start: dict[str, int], end: dict[str, int],
        options: dict[str, Any] | None = None,
    ) -> list[TextEdit]:
        """The edits that format a range, for the formatter that can be told to."""
        return _text_edits(await self.request('textDocument/rangeFormatting', {
            'textDocument': {'uri': path.as_uri()}, 'range': {'start': start, 'end': end},
            'options': options or {},
        }))


def _overlaps(rng: dict[str, Any], start: dict[str, int], end: dict[str, int]) -> bool:
    """Whether a diagnostic's range touches the range asked about."""
    return _point(rng.get('start') or {}) <= _point(end) and _point(rng.get('end') or {}) >= _point(start)


class LspPool:
    """The language servers one session has, started when first needed.

    Owned by the session and closed with it, like its browser: a language
    server is a few hundred megabytes of index that nobody else will stop.
    """

    def __init__(self, root: Path, specs: list[ServerSpec]) -> None:
        self.root = root
        self.specs = specs
        self._servers: dict[str, LanguageServer] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def spec_for(self, path: Path | str) -> ServerSpec | None:
        suffix = Path(path).suffix.lower()
        return next((s for s in self.specs if suffix in s.extensions), None)

    def running(self, spec: ServerSpec) -> bool:
        server = self._servers.get(spec.name)
        return server is not None and server.running

    def describe(self) -> list[str]:
        return [f'{s.language} ({s.name})' for s in self.specs]

    async def server(self, spec: ServerSpec) -> LanguageServer:
        lock = self._locks.setdefault(spec.name, asyncio.Lock())
        async with lock:
            server = self._servers.get(spec.name)
            if server is not None and server.running:
                return server
            # One that has died is replaced rather than reported dead for the
            # rest of the session: servers crash, and the next question
            # deserves a fresh one.
            server = LanguageServer(spec, self.root)
            await server.start()
            self._servers[spec.name] = server
            return server

    async def after_write(self, path: Path, wait: float = 2.5) -> str:
        """What a running server now says is wrong with a file just written.

        Only a server that is already running is asked: starting one here
        would be running the project's build scripts on the back of an edit
        nobody graded as a command. And only errors, briefly — a warning list
        after every edit is noise the model learns to skim.
        """
        spec = self.spec_for(path)
        if spec is None or not self.running(spec):
            return ''
        server = self._servers[spec.name]
        try:
            items = await server.diagnostics_for(path, wait)
        except LspError:
            return ''
        errors = [d for d in items or [] if d.get('severity', 1) == 1]
        if not errors:
            return ''
        lines = [
            f'  line {d["range"]["start"]["line"] + 1}: {" ".join(str(d.get("message", "")).split())}'
            for d in errors[:10]
        ]
        more = f'\n  … and {len(errors) - 10} more' if len(errors) > 10 else ''
        return (
            f'{spec.name} now reports {len(errors)} error{"s" if len(errors) != 1 else ""} in this file:\n'
            + '\n'.join(lines) + more
        )

    async def close(self) -> None:
        for server in list(self._servers.values()):
            try:
                await server.stop()
            except Exception:  # noqa: BLE001
                log.debug('lsp %s did not stop cleanly', server.spec.name, exc_info=True)
        self._servers.clear()

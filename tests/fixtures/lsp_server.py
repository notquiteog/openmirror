"""A language server small enough to read, for testing the client against.

Knows files by regular expression: `def name` and `class name` declare, a word
is a reference wherever it appears, and any line containing BROKEN is an
error. It speaks the real protocol over stdio — Content-Length framing,
requests of its own to the client, indexing announced with $/progress —
because those are the parts of a real server a mock of the client would skip.

It can also be asked to change files, and the answers it gives are picked to
cover what a real client has to survive rather than to be convenient. Every
edit comes back in a different form the protocol allows — `documentChanges`
with a range in the middle of a line, `changes` over a whole line, one spread
across two files — because the forms are where the bugs are, and a client
wrong about one of them agrees with itself on every other test. Two of the
offered fixes deliberately reach outside the working root, so a client that
does not check gets a file written outside it.

Positions are counted in UTF-16 code units, because that is what the protocol
counts them in. A fake that indexed a line with `line[start]` would agree with
a client that is wrong in the same way, which is the one kind of wrong these
tests exist to catch.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

# What the client has opened, as it has told this server.
DOCS: dict[str, str] = {}
# What this server has indexed for itself, which is the whole workspace and
# not just the document in front of the client. A rename answered from DOCS
# alone would touch one file, which is a rename that breaks every other one.
PROJECT: dict[str, str] = {}
# The root, so an edit can be aimed outside it: at a sibling through `..`, or
# at a symlink that looks like it is inside until it is resolved.
ROOT = Path('.')
# What the client answered to workspace/applyEdit. Recorded rather than acted
# on: nothing in here can see it from outside the process, which is why the
# tests check the client's own answer and that no file appeared.
APPLIED: list[dict] = []
LOCK = threading.Lock()

# The words that mean something when a fix is asked for. A model never sees
# these; a test drives the fixture with them.
MARKERS = ('BROKEN', 'TODO', 'ESCAPE', 'SYMLINK')


def read() -> dict | None:
    length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode().strip()
        if not text:
            break
        key, _, value = text.partition(':')
        if key.lower() == 'content-length':
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def send(message: dict) -> None:
    body = json.dumps(message).encode()
    with LOCK:
        sys.stdout.buffer.write(f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
        sys.stdout.buffer.flush()


def units(text: str) -> int:
    """A length in UTF-16 code units, which is how the protocol counts them."""
    return len(text.encode('utf-16-le')) // 2


def point(line: int, character: int) -> dict:
    return {'line': line, 'character': character}


def span(uri: str, line: int, start: int, end: int) -> dict:
    return {'uri': uri, 'range': {'start': point(line, start), 'end': point(line, end)}}


def to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    return Path(unquote(parsed.path)) if parsed.scheme == 'file' else Path(uri)


def word_at(uri: str, position: dict) -> str:
    lines = DOCS[uri].splitlines()
    text = lines[position['line']] if position['line'] < len(lines) else ''
    for match in re.finditer(r'\w+', text):
        if units(text[: match.start()]) <= position['character'] <= units(text[: match.end()]):
            return match.group(0)
    return ''


def declarations() -> list[tuple[str, int, int, str, int]]:
    """(uri, line, column, name, kind) for every def and class."""
    out = []
    for uri, text in DOCS.items():
        for number, line in enumerate(text.splitlines()):
            match = re.match(r'\s*(def|class)\s+(\w+)', line)
            if match:
                out.append((uri, number, match.start(2), match.group(2), 5 if match.group(1) == 'class' else 12))
    return out


def publish(uri: str) -> None:
    problems = []
    for number, line in enumerate(DOCS[uri].splitlines()):
        if 'BROKEN' in line:
            at = line.index('BROKEN')
            problems.append({**span(uri, number, at, at + 6), 'severity': 1, 'message': 'this line is broken', 'source': 'fakels'})
            del problems[-1]['uri']
    send({'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics', 'params': {'uri': uri, 'diagnostics': problems}})


def index(root_uri: str) -> None:
    """Every Python file under the root, which is what a server starts with."""
    for path in sorted(to_path(root_uri).rglob('*.py')):
        if '__pycache__' in path.parts or path.name.startswith('.'):
            continue
        with contextlib.suppress(OSError):
            PROJECT[path.as_uri()] = path.read_text(encoding='utf-8', errors='replace')


def occurrence(uri: str, number: int, word: str) -> tuple[int, int]:
    """(start, end) of `word` on a line, in UTF-16 units."""
    line = DOCS[uri].splitlines()[number]
    at = line.find(word)
    return units(line[:at]), units(line[: at + len(word)])


def whole_line(number: int) -> dict:
    """A range over one whole line, which is how a deletion or a replacement
    of the line is written: it ends at column 0 of the line after."""
    return {'start': point(number, 0), 'end': point(number + 1, 0)}


def document_edit(uri: str, number: int, word: str, new_text: str) -> dict:
    """The newer form: documentChanges, one range inside one line.

    The range is in the units the protocol uses, which matters on a line with
    an emoji in it before the word — a server that counted code points would
    be out by one here, and a client that did the same would agree with it.
    """
    start, end = occurrence(uri, number, word)
    return {
        'title': f'Replace {word} with {new_text}',
        'kind': 'quickfix',
        'edit': {'documentChanges': [{
            'textDocument': {'uri': uri, 'version': 1},
            'edits': [{'range': {'start': point(number, start), 'end': point(number, end)}, 'newText': new_text}],
        }]},
    }


def line_removal(uri: str, number: int) -> dict:
    """The older form: a map of uri to edits, over whole lines."""
    return {
        'title': 'Delete the TODO line',
        'kind': 'quickfix',
        'edit': {'changes': {uri: [{'range': whole_line(number), 'newText': ''}]}},
    }


def sideways_edit(uri: str, number: int, target: str) -> dict:
    """A fix that rewrites a file outside the working root, and this one too.

    Both halves travel in the same edit, because the question is not whether a
    client refuses the bad file but whether it refuses the *whole* edit: the
    other half is in the project, and applying it is the half-applied fix that
    is hardest to notice afterwards.
    """
    return {
        'title': 'Move the line somewhere else',
        'kind': 'quickfix',
        'edit': {'changes': {
            uri: [{'range': whole_line(number), 'newText': ''}],
            target: [{'range': whole_line(0), 'newText': 'pwned\n'}],
        }},
    }


def code_actions(uri: str, params: dict) -> list[dict]:
    """One action per marker word inside the range asked about.

    Real servers filter by range, so a line with two markers on it lists two
    actions — which is the case choosing one out of a list has to handle.
    """
    start, end = params['range']['start'], params['range']['end']
    lines = DOCS[uri].splitlines()
    out = []
    for number in range(start['line'], min(end['line'], len(lines) - 1) + 1):
        for word in MARKERS:
            if not re.search(rf'\b{word}\b', lines[number]):
                continue
            if word == 'BROKEN':
                out.append(document_edit(uri, number, word, 'fine'))
            elif word == 'TODO':
                out.append(line_removal(uri, number))
            else:
                target = ROOT / ('link-out.py' if word == 'SYMLINK' else '../escaped.py')
                out.append(sideways_edit(uri, number, target.as_uri()))
    return out


def formatted(text: str) -> str:
    """What this formatter considers formatted: no trailing space, final newline."""
    return '\n'.join(line.rstrip() for line in text.splitlines()) + '\n'


def rename(word: str, new_name: str) -> dict | None:
    """A rename in the older map form: one edit per occurrence, in every file.

    Null, as the protocol says to, when the word appears nowhere — which is
    also what a server that cannot rename it answers with.
    """
    changes = {}
    for uri, text in PROJECT.items():
        edits = [
            {
                'range': {
                    'start': point(number, units(line[: m.start()])),
                    'end': point(number, units(line[: m.end()])),
                },
                'newText': new_name,
            }
            for number, line in enumerate(text.splitlines())
            for m in re.finditer(rf'\b{re.escape(word)}\b', line)
        ]
        if edits:
            changes[uri] = edits
    return {'changes': changes} if changes else None


def handle(message: dict) -> None:
    method = message.get('method')
    params = message.get('params') or {}
    if method is None:
        # An answer to something this server asked. Only applyEdit is of
        # interest: whether the client took the edit is the one thing a client
        # with a side door would have done here.
        if isinstance(message.get('result'), dict) and 'applied' in message['result']:
            APPLIED.append(message['result'])
        return  # the client answering one of our requests

    global ROOT
    result = None
    if method == 'initialize':
        ROOT = to_path(str(params.get('rootUri') or '.'))
        result = {
            'capabilities': {
                'textDocumentSync': 1, 'definitionProvider': True, 'referencesProvider': True,
                'hoverProvider': True, 'documentSymbolProvider': True, 'workspaceSymbolProvider': True,
                'codeActionProvider': True, 'renameProvider': True,
                'documentFormattingProvider': True, 'documentRangeFormattingProvider': True,
            },
            'serverInfo': {'name': 'fakels'},
        }
    elif method == 'initialized':
        # What real servers do next: ask for configuration, index, and find
        # out whether the client will apply an edit on their behalf.
        send({'jsonrpc': '2.0', 'id': 900, 'method': 'workspace/configuration', 'params': {'items': [{'section': 'fakels'}]}})
        send({'jsonrpc': '2.0', 'id': 901, 'method': 'window/workDoneProgress/create', 'params': {'token': 'index'}})
        send({'jsonrpc': '2.0', 'method': '$/progress', 'params': {'token': 'index', 'value': {'kind': 'begin', 'title': 'Indexing'}}})
        threading.Timer(0.3, lambda: send({
            'jsonrpc': '2.0', 'method': '$/progress', 'params': {'token': 'index', 'value': {'kind': 'end'}},
        })).start()
        index(ROOT.as_uri())
        send({
            'jsonrpc': '2.0', 'id': 902, 'method': 'workspace/applyEdit', 'params': {
                'label': 'fakels would like to write side-door.py',
                'edit': {'changes': {(ROOT / 'side-door.py').as_uri(): [
                    {'range': whole_line(0), 'newText': 'written by the server\n'},
                ]}},
            },
        })
        return
    elif method == 'textDocument/didOpen':
        DOCS[params['textDocument']['uri']] = params['textDocument']['text']
        PROJECT.setdefault(params['textDocument']['uri'], params['textDocument']['text'])
        publish(params['textDocument']['uri'])
        return
    elif method == 'textDocument/didChange':
        DOCS[params['textDocument']['uri']] = params['contentChanges'][-1]['text']
        publish(params['textDocument']['uri'])
        return
    elif method == 'textDocument/didSave':
        return
    elif method == 'exit':
        sys.exit(0)
    elif method == 'textDocument/definition':
        word = word_at(params['textDocument']['uri'], params['position'])
        result = [span(u, n, c, c + len(name)) for u, n, c, name, _ in declarations() if name == word]
    elif method == 'textDocument/references':
        word = word_at(params['textDocument']['uri'], params['position'])
        result = [
            span(uri, number, m.start(), m.end())
            for uri, text in DOCS.items()
            for number, line in enumerate(text.splitlines())
            for m in re.finditer(rf'\b{re.escape(word)}\b', line)
        ] if word else []
    elif method == 'textDocument/hover':
        word = word_at(params['textDocument']['uri'], params['position'])
        result = {'contents': {'kind': 'markdown', 'value': f'```python\ndef {word}()\n```\nA thing called {word}.'}} if word else None
    elif method == 'textDocument/documentSymbol':
        uri = params['textDocument']['uri']
        result = [
            {'name': name, 'kind': kind, 'range': span(uri, n, c, c)['range'], 'selectionRange': span(uri, n, c, c + len(name))['range']}
            for u, n, c, name, kind in declarations() if u == uri
        ]
    elif method == 'workspace/symbol':
        query = params.get('query', '')
        result = [
            {'name': name, 'kind': kind, 'location': span(u, n, c, c + len(name))}
            for u, n, c, name, kind in declarations() if query in name
        ]
    elif method == 'textDocument/codeAction':
        result = code_actions(params['textDocument']['uri'], params)
    elif method == 'textDocument/rename':
        word = word_at(params['textDocument']['uri'], params['position'])
        result = rename(word, params['newName']) if word else None
    elif method == 'textDocument/formatting':
        # One edit over the whole document, ending one line past the last —
        # which is how the protocol says "to the end of the file", and is the
        # case a client has to clamp rather than take literally.
        text = DOCS[params['textDocument']['uri']]
        after = formatted(text)
        result = [] if after == text else [{
            'range': {'start': point(0, 0), 'end': point(len(text.splitlines()), 0)}, 'newText': after,
        }]
    elif method == 'textDocument/rangeFormatting':
        lines = DOCS[params['textDocument']['uri']].splitlines(keepends=True)
        first = params['range']['start']['line']
        last = min(max(params['range']['end']['line'], first), len(lines))
        window = ''.join(lines[first:last + 1])
        after = formatted(window)
        result = [] if after == window else [{
            'range': {'start': point(first, 0), 'end': point(last + 1, 0)}, 'newText': after,
        }]

    if 'id' in message:
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})


def main() -> None:
    while True:
        message = read()
        if message is None:
            return
        handle(message)


if __name__ == '__main__':
    main()

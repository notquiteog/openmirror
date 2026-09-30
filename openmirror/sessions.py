"""Sessions that survive a restart.

Both Claude Code and openCode keep their conversations, and so did nothing
here: `SessionManager` held `AgentSession` objects in a dict and a restart
lost every one of them. For a thing whose whole pitch is being a long-running
digital twin, losing a day's work because a daemon was restarted is not a
missing feature, it is a broken promise.

**What is stored, and what deliberately is not.** The conversation — the
messages, the title, the working root, the model, the policy — and nothing
else. Not the tools, not the provider, not the checkpoint store, not the
approval futures. Those are rebuilt when a session is reopened, and a
transcript that tried to serialise a live provider client would be a file
that could only be read by the process that wrote it.

So reopening is *rebuild then replay*: the session is constructed as it would
have been for the first time, with the tools and the model it had, and then
the stored messages are put back into it. A tool that no longer exists is
visible in the transcript as a tool that no longer exists, which is what a
transcript should be — a record, not a re-execution.

**What is lost, honestly.** The live state of a turn that was running:
in-flight tool calls, a suspended approval, the queue. A transcript does not
contain a Future. A session that was mid-turn is restored to just before that
turn, with a note saying so, because the alternative is restoring a turn that
was half-done and letting the model believe it finished.

**One file per session, under the data directory**, and a small index beside
them so the list does not have to open all of them. Never inside a working
root: a conversation is not a file in the project, and `grep` would find it.

**Appended to, not rewritten.** Each turn adds a line and nothing else. The
first version serialised the entire transcript and renamed it over the top on
every turn, which is fine for a session of forty messages and a 90ms stall on
the event loop for a session of eight hundred — measured, and it is the one
long frame in an otherwise clean turn. Rewriting is also just wrong at scale:
a long conversation would spend more time copying itself than working.

The cost is a file that is append-only, so a turn cut off mid-write leaves
every earlier line intact and at worst one short line, which the loader
skips. That is the same bargain JSONL exists to make, and it is a better one
here than an atomic rename, because the atomic version pays for the whole file
every time to protect the last turn.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openmirror.providers.base import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

log = logging.getLogger(__name__)

# One block's `data` field is a base64 screenshot. A long session with many
# of them is tens of megabytes, and a transcript that cannot be opened is not
# a transcript. Pictures are kept as a short description instead — enough to
# see that there *was* one, which is what reading the transcript back is for.
MAX_IMAGE_CHARS = 120
# A transcript bigger than this is not appended to. It would be somebody's
# runaway, and the fix is a smaller session, not a disk full of them.
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


class TranscriptError(RuntimeError):
    """A stored conversation could not be read."""


# ---------------------------------------------------------------------------
# Blocks, both ways
# ---------------------------------------------------------------------------


def encode_block(block: Any) -> dict[str, Any]:
    """A content block as plain data.

    Round-tripped rather than derived from the dataclasses, because a dataclass
    gaining a field should not silently make old transcripts unreadable —
    `_missing` is what decides that, and it is checked.
    """
    if isinstance(block, TextBlock):
        return {'type': 'text', 'text': block.text}
    if isinstance(block, ThinkingBlock):
        # The signature is provider-specific and meaningless later, and
        # resending a stale one with the thinking can be rejected. The text is
        # kept because it is what the person reads in the transcript.
        return {'type': 'thinking', 'text': block.text}
    if isinstance(block, ImageBlock):
        size = len(block.data)
        return {
            'type': 'image',
            'media_type': block.media_type,
            'bytes': size,
            'note': f'[{block.media_type}, {size // 1024}kB — not stored in a transcript]',
        }
    if isinstance(block, ToolUseBlock):
        return {
            'type': 'tool_use', 'id': block.id, 'name': block.name, 'input': block.input,
        }
    if isinstance(block, ToolResultBlock):
        text = block.content
        # A tool result can be a megabyte of build output. Kept whole up to a
        # point and then noted, because a transcript whose middle has been
        # dropped silently is worse than one that says where it went.
        keep = 20_000
        return {
            'type': 'tool_result',
            'tool_use_id': block.tool_use_id,
            'is_error': block.is_error,
            'content': text if len(text) <= keep else f'{text[:keep]}\n\n[... {len(text) - keep} characters omitted from the transcript ...]',
        }
    return {'type': 'unknown', 'note': f'{type(block).__name__} was not stored'}


def decode_block(raw: dict[str, Any]) -> Any:
    """Data back into a block. A type this build does not know becomes text.

    A transcript outlives the code that wrote it — a format added in a later
    release, read by an earlier one — and dropping a block silently would lose
    a turn. Showing it as text keeps the conversation readable.
    """
    kind = str(raw.get('type') or '')
    if kind == 'text':
        return TextBlock(text=str(raw.get('text') or ''))
    if kind == 'thinking':
        return ThinkingBlock(text=str(raw.get('text') or ''))
    if kind == 'image':
        # There is no image to restore, so it is stated rather than pretended.
        return TextBlock(text=f'[an image: {raw.get("note") or raw.get("media_type") or "removed"}]')
    if kind == 'tool_use':
        return ToolUseBlock(id=str(raw.get('id') or ''), name=str(raw.get('name') or ''),
                            input=dict(raw.get('input') or {}))
    if kind == 'tool_result':
        return ToolResultBlock(tool_use_id=str(raw.get('tool_use_id') or ''),
                               content=str(raw.get('content') or ''),
                               is_error=bool(raw.get('is_error')))
    note = raw.get('note') or f'a block of type {kind or "unknown"}'
    return TextBlock(text=f'[{note}]')


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Stored:
    """One session's worth of saved conversation."""

    id: str
    title: str = ''
    root: str = ''
    model: str = ''
    policy: str = ''
    toolset: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    #: True when the daemon stopped with a turn in flight. The turn is gone
    #: and the transcript says so rather than pretending it finished.
    unfinished: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'title': self.title,
            'root': self.root,
            'model': self.model,
            'created': self.created,
            'updated': self.updated,
            'turns': sum(1 for m in self.messages if m.get('role') == 'user'),
            'unfinished': self.unfinished,
        }

    def restore(self) -> list[Message]:
        """The stored messages, as the session wants them."""
        out: list[Message] = []
        for raw in self.messages:
            blocks = [decode_block(b) for b in (raw.get('content') or []) if isinstance(b, dict)]
            role = raw.get('role')
            if role in ('user', 'assistant') and blocks:
                out.append(Message(role=role, content=blocks))  # type: ignore[arg-type]
        return out


class SessionStore:
    """One JSON file per session, and a small index in front of them.

    The index is a convenience, not the source of truth: `list()` falls back to
    scanning the directory when the index is missing or stale, because a store
    whose list is an index and whose data is elsewhere is a store that loses
    data when the index is lost.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / 'index.json'

    def path_for(self, session_id: str) -> Path:
        safe = ''.join(ch for ch in str(session_id) if ch.isalnum() or ch in '-_')[:64]
        if not safe:
            raise TranscriptError(f'{session_id!r} is not a usable session id')
        return self.root / f'{safe}.json'

    def _written(self, session_id: str) -> int:
        """How many messages are already on disk for a session.

        Counted from the file rather than remembered, so a restarted daemon
        knows the same thing the last one knew. A number in memory would be
        wrong after a restart and the first append would duplicate a turn.
        """
        try:
            with self.path_for(session_id).open('r', encoding='utf-8', errors='replace') as handle:
                return max(0, sum(1 for line in handle if line.strip() and '"message"' in line))
        except OSError:
            return 0

    def save(self, stored: Stored) -> Path | None:
        """Append whatever is new, and nothing else.

        The header is rewritten because it is one small line and it changes;
        the messages are appended to. A session that has not changed writes
        nothing at all, which is what makes a read-only turn free.
        """
        stored.updated = time.time()
        target = self.path_for(stored.id)
        already = self._written(stored.id)
        if already >= len(stored.messages):
            self._reindex(stored)
            return target
        if target.exists() and target.stat().st_size > MAX_TRANSCRIPT_BYTES:
            log.warning('session %s is too large to keep appending to', stored.id)
            return None

        try:
            fresh = stored.messages[already:]
            header = json.dumps({
                'type': 'session', 'id': stored.id, 'title': stored.title, 'root': stored.root,
                'model': stored.model, 'policy': stored.policy, 'toolset': stored.toolset,
                'created': stored.created, 'unfinished': stored.unfinished,
            }, ensure_ascii=False)
            with target.open('a', encoding='utf-8') as handle:
                if already == 0:
                    handle.write(header + '\n')
                for message in fresh:
                    handle.write(json.dumps({'type': 'message', **message}, ensure_ascii=False) + '\n')
        except (OSError, TypeError, ValueError) as exc:
            log.warning('session %s could not be written: %s', stored.id, exc)
            return None
        self._reindex(stored)
        return target

    def load(self, session_id: str) -> Stored | None:
        """One line at a time, and a short line is skipped rather than fatal.

        The file is append-only, so the realistic way to find one is a daemon
        that stopped mid-write — and a transcript that refuses to open because
        its *last* turn was cut off would throw away every turn before it.

        A file that is not there at all is not a failure. Restoring a session
        asks for the transcript before anything has been written, and on a
        command line that warning lands on a person's stderr on every single
        run and reads as though something broke.
        """
        path = self.path_for(session_id)
        if not path.exists():
            return None
        header: dict[str, Any] = {}
        messages: list[dict[str, Any]] = []
        try:
            with path.open('r', encoding='utf-8', errors='replace') as handle:
                for number, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug('%s line %d is not JSON; skipped', session_id, number)
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get('type') == 'session':
                        header = record
                    elif record.get('type') == 'message':
                        payload = {k: v for k, v in record.items() if k != 'type'}
                        messages.append(payload)
        except OSError as exc:
            log.warning('session %s could not be read: %s', session_id, exc)
            return None
        if not header and not messages:
            return None
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = time.time()
        return Stored(
            id=str(header.get('id') or session_id),
            title=str(header.get('title') or ''),
            root=str(header.get('root') or ''),
            model=str(header.get('model') or ''),
            policy=str(header.get('policy') or ''),
            toolset=[str(t) for t in (header.get('toolset') or [])],
            messages=messages,
            created=float(header.get('created') or modified),
            updated=modified,
            unfinished=bool(header.get('unfinished')),
        )

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Every stored session, newest first.

        The index is used because it is cheap, and the directory is used when
        the index is missing — and the two are reconciled, so a file that the
        index never heard of is still listed. An index that can lose sessions
        is worse than no index.
        """
        found: dict[str, dict[str, Any]] = {}
        try:
            indexed = json.loads(self.index.read_text(encoding='utf-8'))
            if isinstance(indexed, dict):
                found = {k: v for k, v in indexed.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            pass

        for path in self.root.glob('*.json'):
            if path.name == 'index.json':
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.stem not in found or found[path.stem].get('updated', 0) < stat.st_mtime:
                loaded = self.load(path.stem)
                if loaded is not None:
                    found[loaded.id] = loaded.summary()

        out = sorted(found.values(), key=lambda row: float(row.get('updated') or 0), reverse=True)
        return out[: max(1, int(limit))]

    def delete(self, session_id: str) -> bool:
        try:
            path = self.path_for(session_id)
        except TranscriptError:
            return False
        removed = path.is_file() and (path.unlink() or True)
        try:
            indexed = json.loads(self.index.read_text(encoding='utf-8'))
            if isinstance(indexed, dict) and session_id in indexed:
                indexed.pop(session_id)
                tmp = self.index.with_suffix('.partial')
                tmp.write_text(json.dumps(indexed), encoding='utf-8')
                tmp.replace(self.index)
        except (OSError, json.JSONDecodeError):
            pass
        return removed

    def _reindex(self, stored: Stored) -> None:
        try:
            indexed = json.loads(self.index.read_text(encoding='utf-8'))
            if not isinstance(indexed, dict):
                indexed = {}
        except (OSError, json.JSONDecodeError):
            indexed = {}
        indexed[stored.id] = stored.summary()
        tmp = self.index.with_suffix('.partial')
        try:
            tmp.write_text(json.dumps(indexed), encoding='utf-8')
            tmp.replace(self.index)
        except OSError as exc:
            log.debug('the session index could not be written: %s', exc)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def to_markdown(stored: Stored) -> str:
    """A conversation as Markdown, for pasting somewhere else.

    Tool calls are rendered as what they were — a fenced block with the call
    and the result — rather than dropped, because "the agent ran a command and
    here is the conversation without it" is a document that misleads.
    """
    lines = [f'# {stored.title or stored.id}', '']
    meta = [f'- root: `{stored.root}`', f'- model: `{stored.model}`'] if stored.root or stored.model else []
    if meta:
        lines += meta + ['']
    if stored.unfinished:
        lines += ['> The daemon stopped while a turn was running. That turn is not in this transcript.', '']
    lines += ['---', '']

    for raw in stored.messages:
        who = 'You' if raw.get('role') == 'user' else 'openmirror'
        lines.append(f'## {who}')
        lines.append('')
        for block in raw.get('content') or []:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'text':
                lines.append(str(block.get('text') or ''))
            elif kind == 'thinking':
                lines += ['<details><summary>reasoning</summary>', '', str(block.get('text') or ''), '', '</details>']
            elif kind == 'tool_use':
                lines += [
                    '```', f'{block.get("name")}({json.dumps(block.get("input") or {}, ensure_ascii=False)})', '```',
                ]
            elif kind == 'tool_result':
                body = str(block.get('content') or '')
                label = 'error' if block.get('is_error') else 'result'
                lines += [f'<details><summary>{label}</summary>', '', '```', body, '```', '', '</details>']
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


__all__ = ['MAX_IMAGE_CHARS', 'MAX_TRANSCRIPT_BYTES', 'SessionStore', 'Stored', 'TranscriptError',
           'decode_block', 'encode_block', 'to_markdown']

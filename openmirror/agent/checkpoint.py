"""Undo, for an agent that edits real files.

An autonomous agent with write access will eventually do something you did not
want, and the moment you notice is usually several steps later. Being able to
say "put it back the way it was before that turn" is worth more than any
amount of care beforehand, because care is what fails.

Snapshots are content-addressed: the same bytes are stored once no matter how
many turns touch the file, so a session that edits one file thirty times costs
thirty hashes and a handful of blobs. Restoring is writing bytes back, which
means it works on a file the agent has since deleted, and on one somebody has
edited by hand in between — the second case is why `restore` reports what it
overwrote rather than doing it silently.

**What this does not cover, and cannot.** Only changes made through the file
tools are recorded. A shell command can write anywhere, and snapshotting the
whole filesystem before every command is not a thing anybody wants. So this is
an undo for the agent's own edits, not a transaction over your machine. Where
the working root is a git repository, git remains the better answer and this
says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# A file bigger than this is not snapshotted. Source files are kilobytes; a
# 200 MB artefact in the working tree should not quietly cost 200 MB per turn.
MAX_SNAPSHOT_BYTES = 8_000_000


@dataclass(slots=True)
class FileState:
    path: Path
    # None means the file did not exist. Restoring to that state deletes it,
    # which is the correct undo of "the agent created this".
    digest: str | None
    size: int = 0
    # What the file looked like when the turn *finished*, recorded at commit.
    # Needed to tell "a person edited this afterwards" from "the agent edited
    # it", which is the whole point of the warning — comparing against the
    # before-state instead makes it fire on every single rewind, and a warning
    # that always fires is one nobody reads.
    after: str | None = None


@dataclass(slots=True)
class Checkpoint:
    id: str
    turn_id: str
    label: str
    at: float = field(default_factory=time.time)
    files: dict[str, FileState] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass(slots=True)
class RestoreReport:
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    # Files whose current contents differ from what the agent left behind:
    # somebody edited them by hand since. Reported, not silently clobbered.
    changed_since: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RedoEntry:
    """One undone turn, kept so it can be put back.

    The after-content of every file the turn touched, as blob digests — the
    same store the before-content lives in, so an undo followed by a redo
    costs no second snapshot of the tree and cannot drift from it. A `None`
    value is a file the turn created, and redoing means deleting it again.

    `before` is kept as well, and that is what makes undo and redo a pair
    rather than a one-way door: `restore` drops the checkpoint on the way out,
    so without these digests a redone turn could be redone but never undone
    again, and the state it came from would be gone.
    """

    checkpoint_id: str
    turn_id: str
    label: str
    files: dict[str, str | None] = field(default_factory=dict)
    before: dict[str, str | None] = field(default_factory=dict)


@dataclass(slots=True)
class Rewind:
    """What an undo or a redo did, in the words a command prints."""

    report: RestoreReport
    label: str
    turn_id: str
    #: True for a redo, so the caller can say which of the two happened.
    redone: bool = False

    @property
    def touched(self) -> int:
        return len(self.report.restored) + len(self.report.deleted)


@dataclass
class FileChange:
    """One file's worth of reviewable change.

    `before` is what was there when the turn opened and `after` is what the
    turn left. Both come out of the blobs this store already keeps, so a
    review costs no second snapshot of anything.
    """

    path: str
    status: str  # modified | created | deleted
    before: str = ''
    after: str = ''
    hunks: list[Any] = field(default_factory=list)
    #: Somebody edited the file by hand since the turn finished. Reported, and
    #: the apply is refused without `force` — the same rule `restore` uses and
    #: for the same reason: their work is not what these hunks describe.
    edited_since: bool = False

    def public(self) -> dict[str, object]:
        return {
            'path': self.path,
            'status': self.status,
            'edited_since': self.edited_since,
            'hunks': [h.public(kept=True) for h in self.hunks],
            'all': [h.index for h in self.hunks],
        }


class CheckpointStore:
    """Snapshots for one session."""

    #: The index, beside the blobs. A JSON file rather than a pickle or a
    #: database because it is a list of small records that a person may need to
    #: read when a session misbehaves, and because nothing here has to be
    #: fast — the blobs are the expensive part and they are already content
    #: addressed on disk.
    INDEX = 'index.json'

    def __init__(self, root: Path) -> None:
        # Under the session's own directory rather than beside the files, so a
        # snapshot never appears in the tree the agent is working on — where it
        # would be read, grepped, and eventually committed.
        self.root = root
        self.blobs = root / 'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.index = root / self.INDEX
        self.checkpoints: list[Checkpoint] = []
        self._open: Checkpoint | None = None
        # What this store last wrote to each path, and nothing else. A review
        # is iterative — you flip one hunk, look, flip another — and without
        # this the second flip sees its own first flip as a hand edit and
        # refuses. A person who edits the file afterwards changes the digest
        # again and is caught, which is the case the guard is for.
        self._written: dict[str, str] = {}
        # Undone turns, oldest first, so `/undo` and `/redo` can be repeated
        # the way openCode's are. Cleared by the next `begin`: a new edit is a
        # new future, and a redo that reached past it would resurrect a tree
        # built on top of changes that are no longer there.
        self._redo: list[RedoEntry] = []
        self._load()

    # -- the index on disk --------------------------------------------------

    def _load(self) -> None:
        """Read the index back, which is what makes undo survive a resume.

        Without this the history is memory only, and that is not a small gap:
        every daemon restart, every `openmirror run -c`, and every reopened
        browser tab would arrive with an empty undo list, in a project whose
        blobs are all still on disk. A resume that cannot undo is the same as
        a resume that never had checkpoints, and nobody would be able to tell
        the difference from the outside.

        Skipped rather than fatal on anything unreadable, and on any blob that
        is gone: a pruned snapshot must not stop the rest of the history from
        working, because "this one entry is broken" and "you have no undo at
        all" are not the same problem.
        """
        try:
            raw = json.loads(self.index.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning('checkpoint index for %s could not be read (%s); undo starts empty', self.root, exc)
            return
        if isinstance(raw, list):
            # The first version of this file was a bare list of checkpoints,
            # and an index written by it should still open. It had no redo
            # stack to carry, because it had no way to hold one.
            raw = {'checkpoints': raw, 'redo': []}
        if not isinstance(raw, dict) or not isinstance(raw.get('checkpoints'), list):
            log.warning('checkpoint index for %s is not an index; undo starts empty', self.root)
            return

        for record in raw['checkpoints']:
            if not isinstance(record, dict) or 'id' not in record:
                continue
            files: dict[str, FileState] = {}
            for key, state in (record.get('files') or {}).items():
                if not isinstance(state, dict):
                    continue
                digest = state.get('digest')
                if digest is not None and not (self.blobs / str(digest)).exists():
                    log.debug('checkpoint %s: the snapshot of %s is gone; skipped', record.get('id'), key)
                    continue
                files[str(key)] = FileState(
                    path=Path(str(key)),
                    digest=None if digest is None else str(digest),
                    size=int(state.get('size') or 0),
                    after=None if state.get('after') is None else str(state['after']),
                )
            # A checkpoint with nothing left in it is a turn in the undo list
            # that does nothing when pressed, which is worse than not being
            # there: it looks like a turn, and it is not.
            if not files:
                continue
            self.checkpoints.append(Checkpoint(
                id=str(record['id']),
                turn_id=str(record.get('turn_id') or ''),
                label=str(record.get('label') or ''),
                at=float(record.get('at') or time.time()),
                files=files,
            ))

        for entry in raw.get('redo') or []:
            if not isinstance(entry, dict):
                continue
            files = {str(k): (None if v is None else str(v)) for k, v in (entry.get('files') or {}).items()}
            if any(d is not None and not (self.blobs / d).exists() for d in files.values()):
                log.debug('a redo snapshot is gone; that redo is not offered')
                continue
            self._redo.append(RedoEntry(
                checkpoint_id=str(entry.get('checkpoint_id') or ''),
                turn_id=str(entry.get('turn_id') or ''),
                label=str(entry.get('label') or ''),
                files=files,
                before={str(k): (None if v is None else str(v)) for k, v in (entry.get('before') or {}).items()},
            ))

    def _save(self) -> None:
        """Write the index out. Best effort by design: losing the undo history
        is a smaller failure than ending a turn, and this runs after the files
        have already been written."""
        payload = {
            'checkpoints': [
                {
                    'id': cp.id,
                    'turn_id': cp.turn_id,
                    'label': cp.label,
                    'at': cp.at,
                    'files': {
                        key: {'digest': state.digest, 'size': state.size, 'after': state.after}
                        for key, state in cp.files.items()
                    },
                }
                for cp in self.checkpoints
            ],
            'redo': [
                {
                    'checkpoint_id': entry.checkpoint_id,
                    'turn_id': entry.turn_id,
                    'label': entry.label,
                    'files': entry.files,
                    'before': entry.before,
                }
                for entry in self._redo
            ],
        }
        try:
            self.index.parent.mkdir(parents=True, exist_ok=True)
            # Written to a sibling and renamed, so an interrupted write cannot
            # leave a half-written index that reads as an empty history.
            temp = self.index.with_suffix('.json.tmp')
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.index)
        except OSError as exc:
            log.warning('the undo history for %s could not be written: %s', self.root, exc)

    @property
    def can_undo(self) -> bool:
        return bool(self.checkpoints)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def redo_count(self) -> int:
        return len(self._redo)

    def undo_count(self) -> int:
        return len(self.checkpoints)

    # -- recording ----------------------------------------------------------

    def begin(self, turn_id: str, label: str) -> Checkpoint:
        """Open a checkpoint for a turn. Nothing is stored until a file is touched."""
        if self._redo:
            self._redo.clear()
            self._save()
        self._open = Checkpoint(id=f'{len(self.checkpoints) + 1}', turn_id=turn_id, label=label)
        return self._open

    def commit(self) -> Checkpoint | None:
        """Close the open checkpoint, keeping it only if it recorded anything.

        A turn that answered a question without touching a file should not
        appear in the undo list; an undo list full of no-ops is one nobody
        reads far enough down.
        """
        cp = self._open
        self._open = None
        if cp is None or not cp.files:
            return None

        # Record *where the turn left each file* — and store the bytes, not
        # only their hash, because a hunk review has to be re-appliable. The
        # second time somebody picks a different subset, the file on disk is
        # already the result of the first, so a review recomputed from disk
        # would compare against a file the turn never wrote and the second
        # choice would land in the wrong place.
        #
        # The digest is what `restore` uses to notice a hand edit; the blob is
        # what hunk review is rebuilt from, and they are the same content.
        for state in cp.files.values():
            try:
                if state.path.is_file():
                    data = state.path.read_bytes()
                    state.after = self._store_blob(data)
                else:
                    state.after = None
            except OSError:
                state.after = None

        self.checkpoints.append(cp)
        self._save()
        return cp

    def _store_blob(self, data: bytes) -> str:
        """Put bytes in the blob store and return their digest.

        Written via a temporary file so a crash mid-write cannot leave a
        truncated blob under a hash that claims to be complete.
        """
        digest = hashlib.sha256(data).hexdigest()
        blob = self.blobs / digest
        if not blob.exists():
            tmp = blob.with_suffix('.partial')
            tmp.write_bytes(data)
            tmp.replace(blob)
        return digest

    def record(self, path: Path) -> None:
        """Snapshot a file as it is *now*, before it is changed.

        Called before every write. The first record of a path in a checkpoint
        wins: later ones would capture the agent's own intermediate states,
        and the point is to get back to before the turn.
        """
        if self._open is None:
            return
        key = str(path)
        if key in self._open.files:
            return

        try:
            if not path.exists():
                self._open.files[key] = FileState(path=path, digest=None)
                return
            if path.is_dir():
                return
            size = path.stat().st_size
            if size > MAX_SNAPSHOT_BYTES:
                log.debug('checkpoint: %s is %d bytes, too large to snapshot', path, size)
                return
            data = path.read_bytes()
        except OSError as exc:
            log.debug('checkpoint: could not read %s: %s', path, exc)
            return

        self._open.files[key] = FileState(path=path, digest=self._store_blob(data), size=size)

    # -- hunk review --------------------------------------------------------

    def _find(self, checkpoint_id: str | None) -> Checkpoint | None:
        """A checkpoint by id, or the most recent — which is the one wanted,
        because it is the turn being looked at."""
        if not self.checkpoints:
            return None
        if checkpoint_id is None:
            return self.checkpoints[-1]
        return next((c for c in self.checkpoints if c.id == checkpoint_id), None)

    def _state_of(self, path: str) -> FileState | None:
        """The most recent recorded state of a path, across every checkpoint.

        The same rule `restore` uses, for the same reason: a file edited in
        three turns is reviewed against the state before the *first* of them,
        and a hunk set computed from anything else lands in the wrong place.
        """
        for checkpoint in reversed(self.checkpoints):
            found = checkpoint.files.get(path)
            if found is not None:
                return found
        return None

    def _read_blob(self, digest: str | None) -> str:
        if not digest:
            return ''
        try:
            return (self.blobs / digest).read_bytes().decode('utf-8', 'replace')
        except OSError:
            return ''

    def _current(self, path: Path) -> str:
        try:
            return path.read_bytes().decode('utf-8', 'replace') if path.is_file() else ''
        except OSError:
            return ''

    def _edited_by_someone(self, state: FileState, path: Path) -> bool:
        """Whether the file on disk is neither what the turn left nor what a
        review here last wrote — that is, whether a person has been in it."""
        if state.after is None or not path.is_file():
            return False
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        return current not in (state.after, self._written.get(str(path)))

    def _after_text(self, state: FileState, path: Path) -> str:
        """What the turn *left*, not what is on disk now.

        The after-content is a blob like the before-content, keyed by the
        digest recorded at commit — and it has to be, because a review is
        re-applied. The second time somebody picks a different subset, the
        file on disk is already the result of the first, so reading the
        after-content from disk recomputes the hunks against a file the turn
        never wrote, and the second choice lands in the wrong place.

        Read from disk only when the blob is gone, which is the one case
        where there is nothing better to do.
        """
        if state.after:
            saved = self._read_blob(state.after)
            if saved:
                return saved
        return self._current(path)

    def changes(self, checkpoint_id: str | None = None) -> list[FileChange]:
        """What a turn changed, as hunks somebody can take or drop.

        Before-content out of the blobs, after-content off disk. It is a
        read, and costs a few hashes.
        """
        from openmirror.agent.hunks import split

        checkpoint = self._find(checkpoint_id)
        if checkpoint is None:
            return []

        out: list[FileChange] = []
        for key, state in checkpoint.files.items():
            path = Path(key)
            before = self._read_blob(state.digest)
            after = self._after_text(state, path)

            edited = self._edited_by_someone(state, path)
            status = 'created' if state.digest is None else ('deleted' if not after else 'modified')
            out.append(FileChange(
                path=key, status=status, before=before, after=after,
                hunks=split(before, after), edited_since=edited,
            ))
        return sorted(out, key=lambda c: c.path)

    def apply_review(self, path: str, keep: set[int], *, force: bool = False) -> dict[str, object]:
        """Write the file back with only the hunks in `keep`.

        Rebuilt from the recorded before- and after-content rather than
        patched, so the result does not depend on what is on disk now, and the
        hunks that were shown and the hunks that are applied cannot drift
        apart. See `openmirror/agent/hunks.py`.
        """
        from openmirror.agent.hunks import rebuild

        state = self._state_of(path)
        if state is None:
            raise KeyError(f'those changes are not in the undo history: {path}')

        before = self._read_blob(state.digest)
        target = Path(path)
        after = self._after_text(state, target)

        if self._edited_by_someone(state, target) and not force:
            return {
                'ok': False,
                'path': path,
                'reason': 'edited since',
                'detail': 'somebody edited this file by hand after the turn, so these hunks are not what '
                          'is in it now. Reopen the review to see the change as it stands, or force it.',
            }

        result = rebuild(before, after, set(keep))
        data = result.encode('utf-8')

        if not result and not before:
            # Created by the turn and every hunk dropped: the correct undo of
            # "it made this file" is that the file is not there.
            if target.exists():
                target.unlink()
            self._written.pop(path, None)
            return {'ok': True, 'path': path, 'written': '', 'deleted': True, 'hunks': sorted(keep)}

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._written[path] = hashlib.sha256(data).hexdigest()
        return {
            'ok': True, 'path': path, 'written': result, 'deleted': False,
            'hunks': sorted(keep), 'bytes': len(data),
        }

    def reviewable(self) -> dict[str, object]:
        """Whether there is anything to review, and for which turn."""
        checkpoint = self._find(None)
        if checkpoint is None or not checkpoint.files:
            return {'reviewable': False, 'turn_id': '', 'label': '', 'files': 0}
        return {
            'reviewable': True,
            'checkpoint': checkpoint.id,
            'turn_id': checkpoint.turn_id,
            'label': checkpoint.label,
            'files': checkpoint.count,
        }

    # -- restoring ----------------------------------------------------------

    def restore(self, checkpoint_id: str, *, force: bool = False) -> RestoreReport:
        """Put every file back to how it was when the checkpoint opened.

        Restores *this* checkpoint and every one after it, because undoing turn
        three while leaving turns four and five in place produces a tree that
        never existed and that nobody asked for.
        """
        index = next((i for i, c in enumerate(self.checkpoints) if c.id == checkpoint_id), None)
        if index is None:
            raise KeyError(f'no checkpoint {checkpoint_id!r}')

        # Later checkpoints first, so the earliest recorded state of each file
        # is the one that ends up on disk.
        wanted: dict[str, FileState] = {}
        for cp in reversed(self.checkpoints[index:]):
            wanted.update(cp.files)

        report = RestoreReport()
        for key, state in wanted.items():
            path = Path(key)
            try:
                if state.digest is None:
                    if path.exists():
                        path.unlink()
                        report.deleted.append(key)
                    continue

                blob = self.blobs / state.digest
                if not blob.exists():
                    report.skipped[key] = 'the snapshot is missing'
                    continue

                if path.exists():
                    current = hashlib.sha256(path.read_bytes()).hexdigest()
                    if current == state.digest:
                        continue          # already as it was; nothing to do
                    # Compared against what the agent *left*, not what it
                    # found, and against anything this store itself wrote
                    # since. A difference means somebody edited the file with a
                    # keyboard, and their work is about to be overwritten.
                    # The second half is what `changes()` already does: a
                    # hunk review writes through this store, and a file that
                    # this store put there is not a hand edit — otherwise
                    # reviewing a turn and then pressing /undo warns that
                    # somebody else touched it, every time.
                    if state.after is not None and current not in (state.after, self._written.get(key)):
                        report.changed_since.append(key)

                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(blob, path)
                # Ours now, so a later review is not told a person edited it.
                self._written[key] = state.digest or ''
                report.restored.append(key)
            except OSError as exc:
                report.skipped[key] = str(exc)

        # The undone checkpoints go, so the list always describes what could
        # still be undone rather than what once happened.
        undone = self.checkpoints[index:]
        del self.checkpoints[index:]

        # ...and their after-content becomes redoable. Captured from the same
        # `wanted` map the restore walked, so redo writes back exactly the
        # bytes that were just taken away, not a fresh read of the disk. A
        # file whose after-content was never recorded — a blob that has been
        # pruned — is simply not redoable, and says so rather than restoring
        # whatever happens to be on disk when the time comes.
        if index == 0 and any(s.after or s.digest is None for s in wanted.values()):
            self._redo.append(RedoEntry(
                checkpoint_id=undone[0].id,
                turn_id=undone[0].turn_id,
                label=undone[0].label,
                files={key: state.after for key, state in wanted.items() if state.after or state.digest is None},
                before={key: state.digest for key, state in wanted.items()},
            ))
        self._save()
        return report

    def undo_latest(self) -> Rewind | None:
        """Put the most recent turn's files back, and remember how to redo it.

        Only the newest one, because that is what "undo" means to somebody who
        pressed it. `restore` still takes an id for the rewind dialog, which is
        choosing a point in history rather than stepping back through it.
        """
        if not self.checkpoints:
            return None
        latest = self.checkpoints[-1]
        report = self.restore(latest.id)
        return Rewind(report=report, label=latest.label, turn_id=latest.turn_id)

    def redo_last(self) -> Rewind | None:
        """Put back the last turn `/undo` took away.

        Same rule as `restore`, and deliberately so: a hand edit since the undo
        is reported and then overwritten, not refused. Refusing would make undo
        and redo disagree about what "put it back" means, and the second
        press — which is the one somebody makes while irritated — would be the
        one that quietly does nothing. The report is what tells them, and both
        commands print it in full.
        """
        if not self._redo:
            return None
        entry = self._redo[-1]

        report = RestoreReport()
        for key, digest in entry.files.items():
            path = Path(key)
            if digest is None:
                try:
                    if path.exists():
                        path.unlink()
                        report.deleted.append(key)
                except OSError as exc:
                    report.skipped[key] = str(exc)
                continue
            blob = self.blobs / digest
            if not blob.exists():
                report.skipped[key] = 'the snapshot is missing'
                continue
            try:
                if path.exists():
                    current = hashlib.sha256(path.read_bytes()).hexdigest()
                    if current != digest:
                        report.changed_since.append(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(blob, path)
                self._written[key] = digest
                report.restored.append(key)
            except OSError as exc:
                report.skipped[key] = str(exc)

        if report.skipped:
            # Something did not go back. The entry stays on the stack: it is
            # still mostly good, and dropping it would quietly turn a partial
            # redo into a permanent one.
            return Rewind(report=report, label=entry.label, turn_id=entry.turn_id, redone=True)

        self._redo.pop()
        # The turn is undoable again, under its own id, so `/undo /redo /undo`
        # lands where it should instead of dead-ending. Rebuilt from the
        # digests the entry carries rather than from the disk, for the same
        # reason the first snapshot is: what is on disk now is what we just
        # wrote, not what the turn found.
        self.checkpoints.append(Checkpoint(
            id=entry.checkpoint_id,
            turn_id=entry.turn_id,
            label=entry.label,
            files={key: FileState(path=Path(key), digest=before, after=entry.files.get(key))
                   for key, before in entry.before.items()},
        ))
        self._save()
        return Rewind(report=report, label=entry.label, turn_id=entry.turn_id, redone=True)

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                'id': cp.id,
                'turn_id': cp.turn_id,
                'label': cp.label,
                'at': cp.at,
                'files': cp.count,
                'paths': sorted(cp.files)[:20],
            }
            for cp in reversed(self.checkpoints)
        ]

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

    def __init__(self, root: Path) -> None:
        # Under the session's own directory rather than beside the files, so a
        # snapshot never appears in the tree the agent is working on — where it
        # would be read, grepped, and eventually committed.
        self.root = root
        self.blobs = root / 'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.checkpoints: list[Checkpoint] = []
        self._open: Checkpoint | None = None
        # What this store last wrote to each path, and nothing else. A review
        # is iterative — you flip one hunk, look, flip another — and without
        # this the second flip sees its own first flip as a hand edit and
        # refuses. A person who edits the file afterwards changes the digest
        # again and is caught, which is the case the guard is for.
        self._written: dict[str, str] = {}

    # -- recording ----------------------------------------------------------

    def begin(self, turn_id: str, label: str) -> Checkpoint:
        """Open a checkpoint for a turn. Nothing is stored until a file is touched."""
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
                    # Compared against what the agent *left*, not what it found.
                    # A difference here means somebody edited the file since,
                    # and their work is about to be overwritten.
                    if state.after is not None and current != state.after:
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
        del self.checkpoints[index:]
        return report

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

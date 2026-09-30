"""Undo for the agent's own edits.

The cases that matter are the awkward ones: a file the agent created (undo is
a delete), a file somebody edited by hand afterwards (undo must say so), and
an interrupted turn (the one most likely to need undoing).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from openmirror.agent.approval import Mode
from openmirror.agent.checkpoint import CheckpointStore
from openmirror.agent.runtime import build_session
from openmirror.providers.base import StreamDone, StreamText, StreamToolUse
from tests.test_agent import ScriptedProvider


@pytest.fixture()
def store(tmp_path):
    return CheckpointStore(tmp_path / '.checkpoints')


def test_an_edited_file_goes_back(store, tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('original\n')

    store.begin('t1', 'change it')
    store.record(target)
    target.write_text('changed\n')
    store.commit()

    report = store.restore('1')
    assert target.read_text() == 'original\n'
    assert str(target) in report.restored


def test_undoing_a_created_file_deletes_it(store, tmp_path):
    """The correct undo of "the agent made this" is that it is gone."""
    target = tmp_path / 'new.txt'

    store.begin('t1', 'create it')
    store.record(target)                  # records that it did not exist
    target.write_text('brand new\n')
    store.commit()

    report = store.restore('1')
    assert not target.exists()
    assert str(target) in report.deleted


def test_only_the_first_state_in_a_turn_is_kept(store, tmp_path):
    """A turn that edits one file three times must go back to before the turn,
    not to the second edit."""
    target = tmp_path / 'a.txt'
    target.write_text('v0\n')

    store.begin('t1', 'several edits')
    for version in ('v1', 'v2', 'v3'):
        store.record(target)
        target.write_text(version + '\n')
    store.commit()

    store.restore('1')
    assert target.read_text() == 'v0\n'


def test_a_turn_that_touched_nothing_is_not_listed(store):
    """An undo list full of no-ops is one nobody reads far enough down."""
    store.begin('t1', 'just a question')
    assert store.commit() is None
    assert store.describe() == []


def test_undoing_a_turn_undoes_the_ones_after_it(store, tmp_path):
    """Undoing turn one while leaving turn two produces a tree that never
    existed."""
    target = tmp_path / 'a.txt'
    target.write_text('v0\n')

    for n, content in enumerate(('v1', 'v2', 'v3'), start=1):
        store.begin(f't{n}', f'edit {n}')
        store.record(target)
        target.write_text(content + '\n')
        store.commit()

    assert len(store.checkpoints) == 3
    store.restore('1')
    assert target.read_text() == 'v0\n'
    assert store.checkpoints == []          # nothing left that could be undone


def test_a_hand_edit_since_is_reported_not_silently_clobbered(store, tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('original\n')

    store.begin('t1', 'edit')
    store.record(target)
    target.write_text('agent wrote this\n')
    store.commit()

    target.write_text('and then a person edited it\n')

    report = store.restore('1')
    assert target.read_text() == 'original\n'
    assert str(target) in report.changed_since


def test_the_agents_own_edit_is_not_reported_as_a_hand_edit(store, tmp_path):
    """The warning compares against what the agent *left*, not what it found.

    Comparing against the before-state makes it fire on every single rewind,
    since the agent changing the file is the reason there is a checkpoint at
    all — and a warning that always fires is one nobody reads.
    """
    target = tmp_path / 'a.txt'
    target.write_text('original\n')

    store.begin('t1', 'edit')
    store.record(target)
    target.write_text('agent wrote this\n')
    store.commit()

    report = store.restore('1')
    assert target.read_text() == 'original\n'
    assert report.restored == [str(target)]
    assert report.changed_since == []


def test_identical_content_is_stored_once(store, tmp_path):
    """Content addressing: thirty turns over one unchanged file is not thirty
    copies of it."""
    a, b = tmp_path / 'a.txt', tmp_path / 'b.txt'
    a.write_text('same bytes\n')
    b.write_text('same bytes\n')

    store.begin('t1', 'two files')
    store.record(a)
    store.record(b)
    store.commit()

    assert len(list(store.blobs.iterdir())) == 1


def test_a_very_large_file_is_skipped(store, tmp_path, monkeypatch):
    """A 200 MB artefact should not cost 200 MB per turn."""
    import openmirror.agent.checkpoint as mod

    monkeypatch.setattr(mod, 'MAX_SNAPSHOT_BYTES', 32)
    big = tmp_path / 'big.bin'
    big.write_bytes(b'x' * 1024)

    store.begin('t1', 'big')
    store.record(big)
    assert store.commit() is None


def test_restoring_an_unknown_checkpoint_raises(store):
    with pytest.raises(KeyError):
        store.restore('nope')


# -- through a real session -------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_is_undoable_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'notes.txt').write_text('keep me\n')
        store = CheckpointStore(root / '.cp')

        session = build_session(
            root=root,
            provider=ScriptedProvider([
                [
                    StreamToolUse(id='c1', name='write_file',
                                  input={'path': 'notes.txt', 'content': 'clobbered\n'}),
                    StreamDone(stop_reason='tool_use'),
                ],
                [StreamText(text='done'), StreamDone()],
            ]),
            model='x',
            mode=Mode.UNRESTRICTED,
        )
        session.checkpoints = store
        await session.start()

        # write_file refuses a file it has not read, so seed the journal.
        from openmirror.agent.tools.files import journal
        journal.note_read(session.id, root / 'notes.txt')

        session.submit('overwrite the notes')
        await asyncio.wait_for(session._turn, timeout=10)

        assert (root / 'notes.txt').read_text() == 'clobbered\n'
        assert len(store.checkpoints) == 1

        store.restore('1')
        assert (root / 'notes.txt').read_text() == 'keep me\n'


@pytest.mark.asyncio
async def test_an_interrupted_turn_is_still_undoable():
    """The turn cut off halfway is the one most likely to have left the tree
    somewhere nobody wanted."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'a.txt').write_text('before\n')
        store = CheckpointStore(root / '.cp')

        session = build_session(
            root=root,
            provider=ScriptedProvider([
                [
                    StreamToolUse(id='c1', name='write_file',
                                  input={'path': 'a.txt', 'content': 'after\n'}),
                    StreamToolUse(id='c2', name='shell', input={'command': 'sleep 30'}),
                    StreamDone(stop_reason='tool_use'),
                ],
            ]),
            model='x',
            mode=Mode.UNRESTRICTED,
        )
        session.checkpoints = store
        await session.start()

        from openmirror.agent.tools.files import journal
        journal.note_read(session.id, root / 'a.txt')

        session.submit('write then hang')
        await asyncio.sleep(1.0)
        session.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await session._turn

        assert len(store.checkpoints) == 1
        store.restore('1')
        assert (root / 'a.txt').read_text() == 'before\n'


# --- surviving a resume ------------------------------------------------------
#
# Found the hard way: `openmirror run -c -p /undo` said "Nothing to undo" on a
# session that had just written a file, and so did every reopened browser tab.
# The blobs were all still on disk. Only the index was in memory, so every
# restart, every resume and every second tab arrived with an empty undo list and
# no way to tell that from a session that never had checkpoints.


def test_the_undo_history_is_still_there_after_a_resume(store, tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('after\n')
    store.commit()

    # A new store over the same directory is what a resume, a restarted daemon
    # and a second browser tab all do.
    reopened = CheckpointStore(store.root)

    assert reopened.undo_count() == 1
    rewind = reopened.undo_latest()
    assert rewind is not None
    assert target.read_text() == 'before\n'


def test_the_redo_stack_survives_a_resume_too(store, tmp_path):
    """The other half of the pair. An undo that survives a restart but whose
    redo does not is an undo you cannot take back."""
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('after\n')
    store.commit()
    store.undo_latest()
    # Back to what it was before the turn, not gone: it existed before.
    assert target.read_text() == 'before\n'

    reopened = CheckpointStore(store.root)
    assert reopened.redo_count() == 1
    rewind = reopened.redo_last()
    assert rewind is not None
    assert target.read_text() == 'after\n'


def test_a_new_edit_after_a_resume_still_forgets_the_redo(store, tmp_path):
    """The loaded stack is a stack like any other, and the rule is the same."""
    target = tmp_path / 'a.txt'
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('after\n')
    store.commit()
    store.undo_latest()

    reopened = CheckpointStore(store.root)
    other = tmp_path / 'b.txt'
    reopened.begin('t2', 'edit b')
    reopened.record(other)
    other.write_text('new\n')
    reopened.commit()

    assert reopened.redo_count() == 0
    assert CheckpointStore(store.root).redo_count() == 0


def test_an_undone_turn_is_not_offered_again_after_a_resume(store, tmp_path):
    """The list describes what could still be undone, not what once happened."""
    target = tmp_path / 'a.txt'
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('after\n')
    store.commit()
    store.undo_latest()

    assert CheckpointStore(store.root).undo_count() == 0


def test_a_pruned_snapshot_does_not_take_the_history_with_it(store, tmp_path):
    """"This one entry is broken" and "you have no undo at all" are not the same
    problem, and the first is the one that happens.

    Two turns, so there is something left to undo after one entry is dropped.
    """
    first = tmp_path / 'first.txt'
    first.write_text('one\n')
    store.begin('t1', 'edit first')
    store.record(first)
    first.write_text('one two\n')
    store.commit()

    doomed = store.checkpoints[0]
    (store.blobs / str(doomed.files[str(first)].digest)).unlink()   # pruned

    second = tmp_path / 'second.txt'
    second.write_text('three\n')
    store.begin('t2', 'edit second')
    store.record(second)
    second.write_text('three four\n')
    store.commit()

    reopened = CheckpointStore(store.root)
    assert reopened.undo_count() == 1, 'the entry whose blob is gone is dropped'
    # And the dropped one is not merely un-undoable in place: it is not in the
    # list at all, because a turn in the undo list that does nothing when
    # pressed reads as a turn.
    assert [cp.label for cp in reopened.checkpoints] == ['edit second']
    rewind = reopened.undo_latest()
    assert rewind is not None
    assert second.read_text() == 'three\n'


def test_a_blob_that_goes_missing_after_the_load_is_reported(store, tmp_path):
    """The other way the same thing happens, and the one a person can act on:
    the snapshot is there when the session opens and gone when they press
    undo. It says which file and why, rather than doing half of it in silence.
    """
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('after\n')
    store.commit()

    reopened = CheckpointStore(store.root)          # reads fine
    for blob in reopened.blobs.iterdir():            # then the store is pruned
        blob.unlink()

    rewind = reopened.undo_latest()
    assert rewind is not None
    assert str(target) in rewind.report.skipped
    assert 'missing' in rewind.report.skipped[str(target)]
    assert target.read_text() == 'after\n', 'and the file is left as it was'


def test_an_unreadable_index_is_not_fatal(store, tmp_path):
    """A corrupt index loses the undo history. It must not stop the session
    from starting, which is what ending a turn would cost."""
    store.index.write_text('{ this is not json', encoding='utf-8')
    reopened = CheckpointStore(store.root)
    assert reopened.undo_count() == 0
    # And it is still usable afterwards, because the next commit rewrites it.
    target = tmp_path / 'a.txt'
    reopened.begin('t1', 'edit a')
    reopened.record(target)
    target.write_text('after\n')
    reopened.commit()
    assert CheckpointStore(store.root).undo_count() == 1


def test_an_index_of_the_wrong_shape_is_not_fatal(store):
    store.index.write_text('{"checkpoints": "nope"}', encoding='utf-8')
    assert CheckpointStore(store.root).undo_count() == 0


def test_the_first_form_of_the_index_still_opens(store, tmp_path):
    """It used to be a bare list of checkpoints. An index written by that
    version is somebody's undo history, and dropping it on an upgrade is the
    kind of loss nobody notices until the moment they need it."""
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('after\n')
    store.commit()
    records = json.loads(store.index.read_text(encoding='utf-8'))
    store.index.write_text(json.dumps(records['checkpoints']), encoding='utf-8')

    reopened = CheckpointStore(store.root)
    assert reopened.undo_count() == 1
    assert reopened.undo_latest() is not None
    assert target.read_text() == 'before\n'


def test_a_hunk_review_is_not_reported_as_a_hand_edit(store, tmp_path):
    """A review writes through this store, so the file on disk is ours.

    Warning that somebody edited it — on every single rewind, after every
    review — is a warning nobody reads, and it is the same guard the existing
    tests check for a real hand edit, so the two have to agree about what
    counts.
    """
    target = tmp_path / 'a.txt'
    target.write_text('one\ntwo\n')
    store.begin('t1', 'edit a')
    store.record(target)
    target.write_text('one\ntwo\nthree\n')
    store.commit()

    review = store.apply_review(str(target), {0})
    assert review['ok'] is True
    assert store.restore('1').changed_since == []

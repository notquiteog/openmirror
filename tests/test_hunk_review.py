"""Reviewing a change one hunk at a time.

The property everything rests on, and the reason the feature can be trusted at
all:

    rebuild(before, after, every hunk) == after
    rebuild(before, after, no hunks)    == before

Keeping all of them gives back exactly what the agent wrote; keeping none of
them gives back exactly what was there before. Those two are asserted over
every case below rather than one case each, because the interesting failures
are in the corners — a file that gained its last line, one that lost its
trailing newline, an edit that is a pure insertion at the top of the file.

**And the middle**: keeping one hunk of two has to give a file that is neither
version, with the kept change in the right place. That is the case the feature
exists for and the one a patch-based implementation gets wrong, because a
second patch lands against text the first one already moved.

The rest is about the store, where the failure modes are quieter:

* **A review is re-appliable.** You flip one hunk, look, flip another. The
  second flip is computed against the turn's own before- and after-content,
  not against the result of the first — and this bit once, because the
  after-content was recorded as a hash and never stored, so the second
  review silently compared against a file the turn never wrote.
* **A review is iterative.** Applying one must not make the store think a
  person edited the file, or the second application is refused for the crime
  of being the first.
* **A hand edit is caught.** That guard is the reason the two above exist, and
  it must still fire for somebody who was not this store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openmirror.agent.checkpoint import CheckpointStore
from openmirror.agent.hunks import CONTEXT, lines_of, rebuild, split, unified

# --- the engine --------------------------------------------------------------


def numbered(count: int) -> str:
    return ''.join(f'line{n}\n' for n in range(1, count + 1))


@pytest.mark.parametrize(
    'before,after',
    [
        ('', 'new\n'),                          # created
        ('old\n', ''),                          # deleted
        ('', ''),                               # no change
        ('a\n', 'a\nb\n'),                      # appended
        ('a\nb\n', 'a\n'),                      # truncated
        ('a\nb\nc\n', 'a\nX\nc\n'),             # replaced
        ('a\nb\nc\n', 'X\na\nb\nc\n'),           # inserted at the top
        ('a\n', 'b\n'),                         # replaced, no newline at the end
        ('a', 'b'),                             # no newline on either side
        ('a\n\n\n', 'a\nb\n\n\n'),              # changed inside blank lines
        ('café\n', 'café ✓\n'),                 # not ascii
        ('a\nb\n', 'a\r\nb\r\n'),               # line endings changed
        ('x\n' * 500, 'x\n' * 250 + 'y\n' + 'x\n' * 249),   # big
    ],
)
def test_keeping_everything_is_exactly_the_after_text(before: str, after: str):
    """The round trip, for the corners. An empty hunk set means no change and
    must not appear, and a whole file that was created has no hunks to keep."""
    hunks = split(before, after)
    everything = {h.index for h in hunks}
    assert rebuild(before, after, everything) == after


@pytest.mark.parametrize(
    'before,after',
    [
        ('', 'new\n'),
        ('old\n', ''),
        ('', ''),
        ('a\n', 'a\nb\n'),
        ('a\nb\n', 'a\n'),
        ('a\nb\nc\n', 'a\nX\nc\n'),
        ('a\n', 'b\n'),
        ('a', 'b'),
        ('a\n\n\n', 'a\nb\n\n\n'),
        ('café\n', 'café ✓\n'),
        ('a\nb\n', 'a\r\nb\r\n'),
        ('x\n' * 500, 'x\n' * 250 + 'y\n' + 'x\n' * 249),
    ],
)
def test_keeping_nothing_is_exactly_the_before_text(before: str, after: str):
    assert rebuild(before, after, set()) == before


def test_one_hunk_of_two_is_neither_file():
    """The case the feature exists for, and the one a patch-based
    implementation gets wrong: the second application lands against text the
    first one already moved."""
    before = numbered(40)
    after = before.replace('line5\n', 'ALPHA\n').replace('line35\n', 'BRAVO\n')
    hunks = split(before, after)
    assert len(hunks) == 2, 'two distant edits are two hunks'

    only_first = rebuild(before, after, {0})
    assert 'ALPHA' in only_first
    assert 'BRAVO' not in only_first
    assert only_first.count('line35\n') == 1, 'the rejected hunk is back to its original'
    # And it is not either input file.
    assert only_first != after and only_first != before

    only_second = rebuild(before, after, {1})
    assert 'BRAVO' in only_second and 'ALPHA' not in only_second


def test_two_edits_close_together_are_one_hunk():
    """Below the gap, they are shown together — which is what a diff does and
    what makes two adjacent one-line fixes readable as one thing."""
    before = numbered(40)
    after = before.replace('line20\n', 'TWENTY\n').replace('line22\n', 'TWENTYTWO\n')
    hunks = split(before, after)
    assert len(hunks) == 1, 'edits three lines apart belong in one hunk'


def test_no_hunks_when_nothing_changed():
    assert split('a\nb\n', 'a\nb\n') == []
    assert unified('a\n', 'a\n') == ''


def test_a_missing_final_newline_is_a_change_worth_showing():
    """It is a real difference in the file, and a review that hid it would
    quietly reformat the file on the next save. A file that is *identical*
    makes no hunk at all, which is the case worth pinning — an empty hunk is
    a button that does something nobody can see."""
    assert split('a\nb\n', 'a\nb\n') == []
    assert len(split('a\nb\n', 'a\nb')) == 1
    assert rebuild('a\nb\n', 'a\nb', {0}) == 'a\nb'


def test_a_hunk_carries_enough_context_to_read():
    before = numbered(40)
    after = before.replace('line20\n', 'CHANGED\n')
    hunk = split(before, after)[0]
    assert len(hunk.context_before) <= CONTEXT
    assert 'line17' in ''.join(hunk.context_before)
    assert 'line21' in ''.join(hunk.context_after)
    assert hunk.after_lines == ['CHANGED\n']
    assert hunk.header.startswith('@@ -')


def test_line_endings_and_a_missing_final_newline_survive():
    """A review tool that normalised line endings or added a trailing newline
    would be a tool that damaged the file it was reviewing."""
    crlf = 'a\r\nb\r\nc\r\n'
    assert rebuild(crlf, crlf.replace('b', 'B'), {0}) == 'a\r\nB\r\nc\r\n'
    # A file with no trailing newline comes back with none.
    assert rebuild('a', 'b', set()) == 'a'
    assert rebuild('a', 'b', {0}) == 'b'
    assert lines_of('a\nb') == ['a\n', 'b']


def test_the_rendered_diff_is_a_unified_diff():
    before = numbered(20)
    after = before.replace('line10\n', 'TEN\n')
    text = unified(before, after, name='x.py')
    assert text.startswith('--- x.py\n+++ x.py\n')
    assert '-line10' in text and '+TEN' in text
    assert text.endswith('\n')


# --- the store ----------------------------------------------------------------


def make(root: Path, store: CheckpointStore, before: str) -> Path:
    """A file recorded the way the tools record it: before the write."""
    path = root / 'a.py'
    path.write_text(before)
    store.record(path)
    return path


def test_a_turn_becomes_hunks_that_rebuild_the_file(tmp_path):
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    original = numbered(40)
    store.begin('t1', 'work')

    path = make(root, store, original)
    path.write_text(original.replace('line5\n', 'ALPHA\n').replace('line35\n', 'BRAVO\n'))
    store.commit()

    changes = store.changes()
    assert [c.path for c in changes] == [str(path)]
    assert changes[0].status == 'modified'
    assert len(changes[0].hunks) == 2
    assert changes[0].edited_since is False

    store.apply_review(str(path), {0})
    text = path.read_text()
    assert 'ALPHA' in text and 'BRAVO' not in text

    store.apply_review(str(path), set())
    assert path.read_text() == original


def test_a_review_is_re_appliable(tmp_path):
    """You flip one hunk, look, flip another. The second flip has to be
    computed against the turn's own before- and after-content — and this bit
    once, because the after-content was recorded as a hash and never stored,
    so the second review compared against whatever the first one had left."""
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    original = numbered(40)
    store.begin('t1', 'work')
    path = make(root, store, original)
    path.write_text(original.replace('line5\n', 'ALPHA\n').replace('line35\n', 'BRAVO\n'))
    store.commit()

    for keep, expect_alpha, expect_bravo in (
        ({0}, True, False),
        ({1}, False, True),
        ({0, 1}, True, True),
        (set(), False, False),
    ):
        store.apply_review(str(path), keep)
        text = path.read_text()
        assert ('ALPHA' in text) is expect_alpha, keep
        assert ('BRAVO' in text) is expect_bravo, keep
    assert path.read_text() == original
    # And the review still describes two hunks throughout, because it is cut
    # from the turn and not from whatever is on disk.
    assert len(store.changes()[0].hunks) == 2


def test_applying_a_review_does_not_look_like_a_hand_edit(tmp_path):
    """A review is iterative, and without this the second flip is refused for
    the crime of being the first."""
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    store.begin('t1', 'work')
    path = make(root, store, numbered(40))
    path.write_text(numbered(40).replace('line5\n', 'ALPHA\n'))
    store.commit()

    store.apply_review(str(path), set())
    assert store.changes()[0].edited_since is False
    assert store.apply_review(str(path), {0})['ok'] is True


def test_a_hand_edit_is_still_caught(tmp_path):
    """The reason the two above exist, and it has to keep working for
    somebody who is not this store."""
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    store.begin('t1', 'work')
    path = make(root, store, numbered(40))
    path.write_text(numbered(40).replace('line5\n', 'ALPHA\n'))
    store.commit()

    path.write_text(path.read_text() + '# typed by a person\n')
    assert store.changes()[0].edited_since is True

    refused = store.apply_review(str(path), set())
    assert refused['ok'] is False
    assert refused['reason'] == 'edited since'
    assert path.read_text().endswith('# typed by a person\n'), 'their edit is still there'
    # And forcing it works, for somebody who means it.
    assert store.apply_review(str(path), set(), force=True)['ok'] is True


def test_rejecting_a_file_the_turn_created_deletes_it(tmp_path):
    """The correct undo of "the agent made this file" is that it is not there."""
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    store.begin('t1', 'work')
    path = root / 'new.py'
    store.record(path)          # does not exist yet
    path.write_text('fresh\n')
    store.commit()

    changes = store.changes()
    assert changes[0].status == 'created'
    assert len(changes[0].hunks) == 1

    store.apply_review(str(path), {0})
    assert path.is_file() and path.read_text() == 'fresh\n'

    store.apply_review(str(path), set())
    assert not path.exists()


def test_a_deleted_file_can_be_brought_back(tmp_path):
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    store.begin('t1', 'work')
    path = make(root, store, 'was here\n')
    path.unlink()
    store.commit()

    assert store.changes()[0].status == 'deleted'
    store.apply_review(str(path), set())
    assert path.read_text() == 'was here\n'


def test_a_turn_that_changed_nothing_is_not_reviewable(tmp_path):
    store = CheckpointStore(tmp_path / 'cp')
    store.begin('t1', 'q')
    assert store.commit() is None
    assert store.changes() == []
    assert store.reviewable()['reviewable'] is False


def test_reviewing_a_file_with_no_history_is_an_error(tmp_path):
    store = CheckpointStore(tmp_path / 'cp')
    with pytest.raises(KeyError):
        store.apply_review('/never/recorded', set())


def test_the_review_is_of_the_latest_turn(tmp_path):
    """Which is the one you are looking at. Offering turn one while turn two
    is on screen would be a review of the wrong change."""
    root = tmp_path / 'proj'
    root.mkdir()
    store = CheckpointStore(tmp_path / 'cp')
    for n, marker in ((1, 'ONE'), (2, 'TWO')):
        store.begin(f't{n}', f'turn {n}')
        path = root / f'{n}.py'
        path.write_text('start\n')
        store.record(path)
        path.write_text(f'{marker}\n')
        store.commit()

    assert [c.path for c in store.changes()] == [str(root / '2.py')]
    assert store.reviewable()['turn_id'] == 't2'
    # And an older one, asked for by name.
    assert [c.path for c in store.changes('1')] == [str(root / '1.py')]

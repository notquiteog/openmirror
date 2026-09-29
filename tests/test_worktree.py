"""Worktrees: a place to be careless where it costs nothing.

Both harnesses have these, and for the same reason. An agent that edits files
is a hazard to whatever is already there, and the answer here used to be
`unconfined` — the wrong shape, because it relies on remembering to turn it
off. A worktree is somewhere you can be careless *in*.

**What is and is not made safe.** Separate, not safe: a worktree shares the
repository's history, its remotes and its `.git`, so a force push in a
worktree is the same force push. What it removes is the *accidental* kind,
which is the kind that happens.

The tests here are mostly about the refusals, because that is where a
convenience turns into a way to lose an afternoon.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openmirror.agent import worktree as wt


@pytest.fixture
def repo(tmp_path):
    """A real repository with one commit, because a worktree needs a HEAD."""
    root = tmp_path / 'project'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', '.'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@example.com'], cwd=root, check=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=root, check=True)
    (root / 'f.txt').write_text('one\n')
    subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
    subprocess.run(['git', 'commit', '-qm', 'first'], cwd=root, check=True)
    return root


# --- recognising one ----------------------------------------------------------


def test_a_directory_that_is_not_a_repository_is_not_mistaken_for_one(tmp_path):
    """Looking for `.git` would be wrong: the second is false inside a
    worktree and inside every subdirectory of one."""
    plain = tmp_path / 'not-a-repo'
    plain.mkdir()
    assert wt.is_repository(plain) is False
    assert wt.repository_root(plain) is None


def test_a_subdirectory_of_a_repository_is_inside_it(repo):
    deep = repo / 'a' / 'b'
    deep.mkdir(parents=True)
    assert wt.repository_root(deep) == repo.resolve()


def test_the_reason_is_a_sentence_and_not_a_boolean(tmp_path, repo):
    """`false` is not what somebody needs; "this is not a git repository" is."""
    import asyncio

    assert 'not a git repository' in asyncio.run(wt.available(tmp_path))
    assert asyncio.run(wt.available(repo)) == ''


# --- making one ---------------------------------------------------------------


def test_a_worktree_is_a_second_checkout_on_its_own_branch(repo):
    made = wt.create(repo, label='try something')
    assert made.branch.startswith('openmirror/')
    assert Path(made.path).is_dir()
    assert (Path(made.path) / 'f.txt').is_file(), 'it starts from the same commit'
    assert made.head, 'and knows where it is'

    listed = {w.branch for w in wt.list_worktrees(repo)}
    assert listed == {'main', made.branch}


def test_a_worktree_does_not_appear_inside_the_working_tree(repo):
    """A worktree inside the working tree is a directory the agent's own grep
    and glob will find, containing a whole second copy of the project."""
    made = wt.create(repo, label='beside')
    assert repo.resolve() not in Path(made.path).resolve().parents


def test_an_explicit_branch_is_kept_but_namespaced(repo):
    made = wt.create(repo, branch='fix-the-thing')
    assert made.branch == 'openmirror/fix-the-thing'


def test_a_branch_name_cannot_become_a_command(repo):
    """Every caller here builds a name from something a person typed, and this
    module runs git through an argument list for exactly that reason. The test
    is here because the day someone changes it back to `sh -c` this fails."""
    made = wt.create(repo, branch='weird; touch /tmp/pwned')
    assert ';' in made.branch or 'pwned' in made.branch
    assert not Path('/tmp/pwned').exists(), 'and nothing was executed'
    wt.remove(repo, made.path)


def test_it_is_refused_outside_a_repository(tmp_path):
    plain = tmp_path / 'plain'
    plain.mkdir()
    with pytest.raises(wt.WorktreeError, match='not a git repository'):
        wt.create(plain, label='nope')


# --- what is in it ------------------------------------------------------------


def test_an_untracked_new_file_is_visible(repo):
    """The gap that made this worth writing: `git diff` cannot see a new file,
    so an agent that spent a turn writing three of them looks like it wrote
    nothing, and "is this worth keeping?" comes back no."""
    made = wt.create(repo, label='work')
    root = Path(made.path)
    (root / 'brand-new.txt').write_text('a')
    (root / 'f.txt').write_text('changed\n')

    found = wt.changes(root)
    assert found['added'] == ['brand-new.txt']
    assert found['modified'] == ['f.txt']
    assert found['dirty'] is True
    assert '1 changed' in wt.describe(root)

    # Removal is refused while it is dirty — which is the point, and also
    # means tidying up here needs tidying up first.
    with pytest.raises(wt.WorktreeError):
        wt.remove(repo, made.path)
    (root / 'brand-new.txt').unlink()
    (root / 'f.txt').write_text('one\n')
    wt.remove(repo, made.path)


def test_a_clean_worktree_says_so(repo):
    made = wt.create(repo, label='clean')
    assert wt.describe(Path(made.path)) == 'clean'
    assert wt.changes(Path(made.path))['dirty'] is False
    wt.remove(repo, made.path)


# --- taking one away ----------------------------------------------------------


def test_a_worktree_with_work_in_it_is_not_removed(repo):
    """The reason removal is a route and not a cleanup script. `git worktree
    remove` refuses too, and that refusal is the feature: a day of work is not
    a directory to be lost to a tidy-up. `--force` is deliberately absent."""
    made = wt.create(repo, label='busy')
    (Path(made.path) / 'work.txt').write_text('an afternoon')
    with pytest.raises(wt.WorktreeError):
        wt.remove(repo, made.path)
    assert (Path(made.path) / 'work.txt').is_file(), 'and the work is still there'


def test_an_empty_one_is_removed_and_stops_being_listed(repo):
    made = wt.create(repo, label='temporary')
    assert wt.remove(repo, made.path) is True
    assert made.branch not in {w.branch for w in wt.list_worktrees(repo)}
    assert not Path(made.path).exists()


def test_removing_something_that_is_not_there_is_an_error_not_a_crash(repo):
    with pytest.raises(wt.WorktreeError):
        wt.remove(repo, str(repo.parent / 'never-existed'))


def test_a_stale_record_can_be_pruned(repo):
    """A stale record makes `git worktree list` lie, and a list that lies is
    how somebody concludes the whole feature is broken."""
    made = wt.create(repo, label='moved')
    import shutil

    shutil.rmtree(made.path, ignore_errors=True)   # the directory is gone, git's record is not
    assert len(wt.list_worktrees(repo)) > 1, 'git still believes in it'
    wt.prune(repo)
    assert made.branch not in {w.branch for w in wt.list_worktrees(repo)}


# --- the wiring ---------------------------------------------------------------


async def test_worktrees_are_off_unless_the_operator_says(repo, monkeypatch):
    """Opt-in twice over, deliberately: only a repository, and only when
    allowed. A branch nobody asked for is a branch somebody has to clean up."""
    from starlette.testclient import TestClient

    from openmirror.agent.manager import manager
    from openmirror.config import config
    from openmirror.main import app

    session = await manager.create(root=str(repo), provider=object(), model='x', mode='ask')
    client = TestClient(app)
    monkeypatch.setattr(config, 'worktrees_enabled', False)
    got = client.post(f'/api/sessions/{session.id}/worktree', json={'label': 'nope'})
    assert got.status_code == 409
    assert 'OPENMIRROR_WORKTREES' in got.json()['detail']
    # Nothing was made.
    assert {w.branch for w in wt.list_worktrees(repo)} == {'main'}
    await manager.close(session.id)


async def test_the_list_route_answers_with_a_reason_when_it_cannot(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from openmirror.agent.manager import manager
    from openmirror.main import app

    plain = tmp_path / 'plain'
    plain.mkdir()
    session = await manager.create(root=str(plain), provider=object(), model='x', mode='ask')
    client = TestClient(app)
    got = client.get(f'/api/sessions/{session.id}/worktrees')
    assert got.status_code == 200
    body = got.json()
    assert body['available'] is False
    assert 'not a git repository' in body['why']
    assert body['worktrees'] == []
    await manager.close(session.id)


def test_the_feature_is_off_by_default():
    """A branch somebody did not ask for is a branch somebody has to clean
    up, so it is a switch and the switch is off."""
    from openmirror.config import Config

    assert Config().worktrees_enabled is False


def test_a_leading_space_in_gits_output_is_not_stripped_away(tmp_path):
    """The bug that made every porcelain parser here wrong.

    `git status --porcelain` begins each line with a two-character XY column
    whose first character is a *space* for an unmodified file. Stripping the
    whole of stdout — the obvious thing to do, and what the first version did —
    turns ` M f.txt` into `M f.txt`, and the file silently loses its first
    letter. Found by a test that expected `['f.txt']` and got `['.txt']`.
    """
    repo = tmp_path / 'p'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@example.com'], cwd=repo, check=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=repo, check=True)
    (repo / 'f.txt').write_text('one\n')
    subprocess.run(['git', 'add', '-A'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-qm', 'x'], cwd=repo, check=True)
    (repo / 'f.txt').write_text('changed\n')

    code, out, _ = wt._git(['-C', str(repo), 'status', '--porcelain'], repo)
    assert out.startswith(' M'), f'the leading column was eaten: {out!r}'
    assert wt.changes(repo)['modified'] == ['f.txt']

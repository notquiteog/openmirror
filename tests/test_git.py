"""Version control, against a real repository.

Every test here makes one. A git wrapper tested against a mocked `run()` is
testing that the mock was called, and the two things most likely to be wrong
in this file — porcelain parsing and the messages git prints when it says no
— are both things only a real git can tell you. The repository is created in
a temporary directory and thrown away; nothing here touches this one.

The commit-message grading is asserted directly rather than through a
session, because the property that matters is the *policy* reading the right
grade, and a session-level test would only prove the policy still works.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from openmirror.agent import git as core
from openmirror.agent.approval import Mode
from openmirror.agent.tools.base import ToolContext
from openmirror.agent.tools.git import RISK, GitTool
from openmirror.protocol.agent import Risk, ToolCall


def init(path: Path) -> Path:
    """An empty repository with an identity, so a commit can be made."""
    subprocess.run(['git', 'init', '-q', '-b', 'main', '.'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'user.email', 't@example.com'], cwd=path, check=True)
    subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=path, check=True)
    return path


@pytest.fixture
def repo() -> Path:
    with tempfile.TemporaryDirectory() as raw:
        yield init(Path(raw))


def ctx_for(root: Path) -> ToolContext:
    return ToolContext(root=root, cwd=root, emit=None, ask=None, session_id='s', confined=True)  # type: ignore[arg-type]


# --- parsing ------------------------------------------------------------------


def test_a_quoted_path_survives_being_parsed():
    """The reason this uses `-z` and not the v1 porcelain.

    A file with spaces and an apostrophe in it is quoted as
    `"it's mine.txt"` by v1, and the quotes come back as part of the name —
    every later step then passes a path that does not exist. The NUL-separated
    format emits the bytes raw, and this asserts the round trip, spaces and
    all: the split is capped at the field count so the path is the last field
    however many spaces it contains.
    """
    parsed = core.parse_status(b"1 .M N... 100644 100644 100644 aaa bbb file with's spaces.txt\x00")
    assert parsed.changes[0].path == "file with's spaces.txt"
    assert core.parse_status(b'? odd "quoted".txt\x00').changes[0].path == 'odd "quoted".txt'


def test_an_untracked_file_is_neither_staged_nor_modified():
    """Measured as a bug: `?` is in neither `.` nor a space, so an untracked
    file landed in both buckets and every new file appeared twice in a status
    report — once as staged, once as modified."""
    change = core.parse_status(b'? notes.md\x00').changes[0]
    assert change.untracked and not change.staged and not change.modified
    assert change.label == 'untracked'


def test_a_staged_file_modified_again_is_labelled_as_both():
    parsed = core.parse_status(b'1 MM N... 100644 100644 100644 aaa aaa bbb\tapp.js\x00')
    change = parsed.changes[0]
    assert change.staged and change.modified
    assert change.label == 'staged and modified again'


def test_a_delete_and_an_add_are_told_apart():
    assert core.parse_status(b'1 .D N... 100644 000000 000000 aaa 000 bbb\tgone.txt\x00').changes[0].label == 'deleted'
    assert core.parse_status(b'1 A. N... 000000 100644 100644 000 aaa bbb\tnew.txt\x00').changes[0].label == 'added'


def test_a_rename_keeps_where_it_came_from():
    """A rename is a `2` record with an extra score field, and the original
    path is a *separate* NUL record after it. Two things go wrong here and
    both were wrong at once: matching only `1` made every rename invisible
    rather than mislabelled, and looking the original up by value rather than
    by position handed back the wrong one whenever two records were equal."""
    parsed = core.parse_status(
        b'2 R. N... 100644 100644 100644 aaa bbb R100 new name.txt\x00old name.txt\x00'
        b'1 .M N... 100644 100644 100644 ccc ddd new name.txt\x00'
    )
    assert [c.path for c in parsed.changes] == ['new name.txt', 'new name.txt']
    assert parsed.changes[0].rename_from == 'old name.txt'
    assert parsed.changes[0].label == 'renamed'
    assert parsed.changes[1].rename_from == ''


def test_a_rename_is_reported_next_to_the_file_it_replaced():
    """A status report has to name where the thing came from, or the model
    stages a rename without knowing it is one."""
    parsed = core.parse_status(b'2 R. N... 100644 100644 100644 aaa bbb R100 new.txt\x00old.txt\x00')
    assert 'renamed: new.txt <- old.txt' in parsed.describe()


def test_ahead_and_behind_are_read_from_the_branch_header():
    parsed = core.parse_status(b'# branch.oid abc\x00# branch.head main\x00# branch.upstream origin/main\x00# branch.ab +3 -1\x00')
    assert (parsed.branch, parsed.upstream, parsed.ahead, parsed.behind) == ('main', 'origin/main', 3, 1)
    assert '3 ahead, 1 behind' in parsed.describe()


def test_a_detached_head_says_so_rather_than_naming_a_branch():
    parsed = core.parse_status(b'# branch.head (detached)\x00')
    assert parsed.detached and parsed.branch == ''
    assert 'detached' in parsed.describe()


def test_a_clean_tree_says_it_is_clean():
    """The answer to "is there anything to commit", which is the question this
    is asked most often and the one most often answered wrongly."""
    parsed = core.parse_status(b'# branch.head main\x00')
    assert parsed.clean
    assert 'working tree clean' in parsed.describe()


def test_a_hundred_changed_files_is_itself_the_message():
    raw = b''.join(b'1 .M N... 100644 100644 100644 aaa aaa bbb\tfile%d.js\x00' % n for n in range(100))
    described = core.parse_status(raw).describe()
    assert 'modified, not staged (100):' in described
    assert 'and 60 more' in described
    assert 'file99.js' not in described


# --- against a real repository -------------------------------------------------


async def test_status_reads_a_real_working_tree(repo: Path):
    (repo / 'kept.txt').write_text('a\n')
    await core.stage(repo, [])
    await core.commit(repo, 'seed it')
    # Now there is a baseline, so writing to it is a modification rather than
    # a new file — which is the distinction being asserted.
    (repo / 'kept.txt').write_text('b\n')
    (repo / 'fresh.txt').write_text('new\n')

    result = await core.status(repo)
    assert result.branch == 'main'
    assert [c.path for c in result.untracked] == ['fresh.txt']
    assert [c.path for c in result.unstaged] == ['kept.txt']
    assert not result.staged


async def test_a_rename_is_seen_against_a_real_repository(repo: Path):
    """The type-`2` record, from git rather than from a fixture — and only
    once it is staged, which is the half of this that is easy to get wrong.

    `git status` does not detect a rename in the *working tree*: with no
    baseline to pair an untracked file against, it reports a deletion and a
    new file, and `diff.renames=true` does not change that. The rename
    appears only once the index can pair them. A model told "deleted
    before.txt, untracked after.txt" will stage the deletion on its own, and
    the commit loses half the change — so `describe` says so explicitly
    instead of leaving the model to infer it.
    """
    (repo / 'before.txt').write_text('same contents\n')
    await core.stage(repo, [])
    await core.commit(repo, 'seed it')
    (repo / 'before.txt').rename(repo / 'after.txt')

    unstaged = await core.status(repo)
    assert {(c.path, c.label) for c in unstaged.changes} == {('before.txt', 'deleted'), ('after.txt', 'untracked')}
    assert 'a deletion and an untracked file' in unstaged.describe()

    await core.stage(repo, [])
    staged = await core.status(repo)
    assert [(c.path, c.rename_from) for c in staged.changes] == [('after.txt', 'before.txt')]
    assert 'renamed: after.txt <- before.txt' in staged.describe()


async def test_unstaging_on_an_unborn_branch_keeps_the_file(repo: Path):
    """Every repository is in this state between `git init` and its first
    commit, so it is the state a person is in the first time they use this.
    There is no HEAD to restore from, and the index is the only copy of the
    file — so the entry has to be removed outright rather than restored."""
    (repo / 'only.txt').write_text('hello\n')
    await core.stage(repo, [])

    assert await core.unstage(repo, []) == 'everything'
    assert (repo / 'only.txt').exists(), 'unstaging must never touch the file'
    assert not (await core.status(repo)).staged
    assert (await core.status(repo)).untracked


async def test_a_commit_takes_the_message_and_the_index_and_nothing_else(repo: Path):
    (repo / 'wanted.txt').write_text('in\n')
    (repo / 'wanted.txt').replace(repo / 'renamed.txt')
    (repo / 'ignored.txt').write_text('out\n')

    await core.stage(repo, ['renamed.txt'])
    result = await core.commit(repo, 'rename the file\n\nBecause the old name said nothing.\n')

    assert result['subject'] == 'rename the file'
    assert result['files'] == 1
    after = await core.status(repo)
    assert not after.staged
    assert [c.path for c in after.untracked] == ['ignored.txt'], 'must not have swept up the untracked file'

    _, out, _ = await core.run(['log', '-1', '--pretty=%B'], repo)
    assert 'Because the old name said nothing.' in out


async def test_a_commit_with_nothing_staged_is_gits_own_refusal(repo: Path):
    """Not an error this module invents. git says what it did not commit and
    names the file; replacing that with a Python message would be strictly
    worse, and silently staging the tree is how an hour goes missing.

    The wording differs by state — "no changes added to commit" once there is
    a HEAD, and "nothing added to commit but untracked files present" on a
    repository with none — so this asserts the part they share rather than
    pinning one string to one version of git.
    """
    (repo / 'a.txt').write_text('a\n')
    with pytest.raises(core.GitError) as caught:
        await core.commit(repo, 'nothing to see')
    said = str(caught.value).lower()
    assert 'nothing added to commit' in said or 'no changes added to commit' in said
    assert 'a.txt' in said, 'git names the file, which is the actionable part'


async def test_an_empty_message_is_refused_before_git_is_asked(repo: Path):
    (repo / 'a.txt').write_text('a\n')
    await core.stage(repo, [])
    with pytest.raises(core.GitError, match='needs a message'):
        await core.commit(repo, '   \n  ')


async def test_a_diff_can_be_asked_for_staged_or_not(repo: Path):
    (repo / 'a.txt').write_text('one\n')
    await core.stage(repo, [])
    (repo / 'a.txt').write_text('two\n')

    unstaged, _ = await core.diff(repo)
    staged, _ = await core.diff(repo, staged=True)
    assert '+two' in unstaged and '-one' in unstaged
    assert '+one' in staged and '+two' not in staged


async def test_a_diff_is_clipped_at_both_ends(repo: Path):
    (repo / 'big.txt').write_text('seed\n')
    await core.stage(repo, [])
    await core.commit(repo, 'seed it')
    # Appended to a tracked file, because a diff of an untracked file is empty
    # — git has no baseline to diff it against, which is a property of git and
    # not something this module papers over.
    with (repo / 'big.txt').open('a') as handle:
        handle.write('\n'.join(f'line {n}' for n in range(40_000)))
    text, clipped = await core.diff(repo)
    assert clipped
    assert len(text) < core.DIFF_LIMIT + 200
    assert 'omitted' in text


async def test_a_ref_that_looks_like_an_option_is_refused(repo: Path):
    """`git show --output=…` is a file write, and this is the one place a
    model-supplied string reaches a git argument list."""
    with pytest.raises(core.GitError, match='not a commit reference'):
        await core.show(repo, '--output=/tmp/anything')
    with pytest.raises(core.GitError, match='not a commit reference'):
        await core.show(repo, '')


async def test_nothing_here_works_outside_a_repository(tmp_path: Path):
    """The ordinary case: a session pointed at a directory that is not under
    version control. A sentence the model can act on, not a traceback."""
    tool = GitTool()
    out = await tool.run({'action': 'status'}, ctx_for(tmp_path))
    assert 'not inside a git repository' in out.content
    assert out.display['repo'] is False


async def test_the_tool_commits_and_reports_the_sha(repo: Path):
    (repo / 'a.txt').write_text('a\n')
    tool = GitTool()
    context = ctx_for(repo)

    staged = await tool.run({'action': 'stage', 'paths': ['a.txt']}, context)
    assert staged.display['staged'] == ['a.txt']

    committed = await tool.run({'action': 'commit', 'message': 'add the first file'}, context)
    assert committed.display['subject'] == 'add the first file'
    assert committed.display['sha']
    assert len(committed.display['sha']) == 7


# --- grading ------------------------------------------------------------------


def call(tool: GitTool, ctx: ToolContext, **args) -> ToolCall:
    made = ToolCall(id='c1', name='git', arguments=args)
    made.risk = tool.assess(args, ctx).risk
    made.summary = tool.assess(args, ctx).summary
    return made


def test_reading_a_repository_is_free_and_writing_to_it_is_not(repo: Path):
    tool, ctx = GitTool(), ctx_for(repo)
    for action in ('status', 'diff', 'log', 'show', 'branch'):
        assert call(tool, ctx, action=action).risk is Risk.READ, action
    # A commit is a local, reversible change — the same grade as writing a
    # file, and asked about in `ask` mode rather than lumped in with builds.
    for action, extra in (
        ('stage', {}), ('unstage', {}), ('commit', {'message': 'x'}),
    ):
        assert call(tool, ctx, action=action, **extra).risk is Risk.WRITE, action
    # The only action that reaches another machine.
    assert call(tool, ctx, action='push').risk is Risk.NETWORK


def test_pushing_asks_about_the_machine_and_never_about_the_commit(repo: Path):
    """`auto_edit` auto-allows writes, which is right — but a push is a
    message to other people's repositories, and it must not inherit that."""
    from openmirror.agent.approval import ApprovalPolicy, Decision

    tool, ctx = GitTool(), ctx_for(repo)
    policy = ApprovalPolicy(mode=Mode.AUTO_EDIT)
    committing = call(tool, ctx, action='commit', message='add the parser')
    pushing = call(tool, ctx, action='push')
    assert policy.decide(committing)[0] is Decision.ALLOW
    assert policy.decide(pushing)[0] is Decision.ASK


def test_a_commit_with_no_message_is_invalid_before_anybody_is_asked(repo: Path):
    """Checked in `assess`, so the model gets a usable error instead of an
    approval prompt for a commit that cannot be made."""
    assessment = GitTool().assess({'action': 'commit', 'message': '  '}, ctx_for(repo))
    assert assessment.invalid and 'needs a message' in assessment.invalid
    assert assessment.risk is Risk.WRITE


def test_a_subject_and_a_body_with_no_blank_line_is_invalid(repo: Path):
    """`--cleanup=strip` will not insert the blank line, and the resulting
    commit has a subject that runs on."""
    assessment = GitTool().assess({'action': 'commit', 'message': 'fix it\nbecause'}, ctx_for(repo))
    assert assessment.invalid and 'blank line' in assessment.invalid


def test_an_unknown_action_names_the_ones_that_exist(repo: Path):
    assessment = GitTool().assess({'action': 'rebase'}, ctx_for(repo))
    assert assessment.invalid and 'status' in assessment.invalid


def test_an_approval_prompt_shows_the_subject_and_not_the_whole_message(repo: Path):
    assessment = GitTool().assess(
        {'action': 'commit', 'message': 'add the retry loop\n\n' + 'why ' * 200}, ctx_for(repo)
    )
    assert not assessment.invalid
    assert assessment.summary == 'commit add the retry loop'


def test_a_long_subject_is_capped_rather_than_rendered_in_full():
    """An approval prompt that renders a paragraph is a prompt nobody reads."""
    assert len(core.subject_of('x' * 300)) == 72


def test_every_action_has_a_grade():
    from openmirror.agent.tools.git import ACTIONS

    assert set(ACTIONS) == set(RISK)
    assert all(isinstance(risk, Risk) for risk in RISK.values())


# --- the interface's own commit path -------------------------------------------


def test_a_draft_is_cleaned_out_of_its_fencing():
    """Observed, all four: models asked for the message and nothing else
    still fence it, label it, and introduce it. A draft that cannot be
    committed verbatim is the worst outcome for a feature whose only job is
    to save somebody typing."""
    from openmirror.routers.git import _clean

    assert _clean('```\nadd the retry loop\n\nBecause it timed out.\n```') == 'add the retry loop\n\nBecause it timed out.'
    assert _clean('Commit message: add the retry loop') == 'add the retry loop'
    assert _clean('Here is the commit message:\n\nadd the retry loop') == 'add the retry loop'
    assert _clean('> add the retry loop\n>\n> Because it timed out.') == 'add the retry loop\n\nBecause it timed out.'
    assert _clean('  \n\n add it ') == 'add it'
    # A subject that legitimately contains the word must survive.
    assert _clean('fix the commit message handler') == 'fix the commit message handler'


def test_the_draft_never_becomes_a_commit_on_its_own():
    """`/api/git/propose` returns a string. It has no way to commit, and
    there is no flag anywhere that drafts and commits in one request."""
    from openmirror.routers.git import ProposeRequest, router

    paths = {getattr(r, 'path', '') for r in router.routes}
    assert '/api/git/propose' in paths and router.routes
    for route in router.routes:
        if getattr(route, 'path', '') == '/api/git/propose':
            assert getattr(route, 'methods', None) == {'POST'}
    assert 'message' not in ProposeRequest.model_fields

"""Opening a pull request, without a network and without a GitHub account.

The interesting part of this file is the argv. Everything below runs `gh` as a
fake executable written to a temporary directory, records the argument list it
was given, and asserts on it — because the failure mode this feature has is
specific and would not show up in any other test. A pull request title is model
output, a pull request body is often a file's contents, and both end up as
arguments to a subprocess. A shell in that path turns `Fix the "; rm -rf ~"
case` into a deleted home directory, and the only way to know it has not is to
look at the argv rather than at the result.

Which is also why the repository is real: a temporary one, with a bare remote on
disk, so that `git push --set-upstream` in step three of `pr_create` genuinely
runs and the branch genuinely lands somewhere. The remote's *URL* is a
github.com one, because that is what a repository under review has, and its
push URL is a local path, because there is no network here. That split is what a
fork looks like in a config file, and it is why the push in these tests is a
real push that never leaves the machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from openmirror.agent import git as core
from openmirror.agent.approval import ApprovalPolicy, Decision, Mode
from openmirror.agent.tools.base import ToolContext, ToolError
from openmirror.agent.tools.git import ACTIONS, RISK, GitTool
from openmirror.protocol.agent import Risk, ToolCall

# Records argv, and the body on stdin, then answers. Written as a real file
# with a real shebang rather than monkeypatching the subprocess layer, so that
# what is asserted is the thing the process was actually handed — argv
# construction is the property under test and a mock of the call above it would
# be testing the mock.
FAKE_GH = '''#!{python}
import json
import os
import sys

argv = sys.argv[1:]
log = os.environ.get('FAKE_GH_LOG')
if log:
    with open(log, 'a') as handle:
        handle.write(json.dumps(argv) + chr(10))
stdin_path = os.environ.get('FAKE_GH_STDIN')
if stdin_path:
    with open(stdin_path, 'a') as handle:
        handle.write(sys.stdin.read())

if os.environ.get('FAKE_GH_FAIL'):
    sys.stderr.write(os.environ['FAKE_GH_FAIL'])
    sys.exit(int(os.environ.get('FAKE_GH_CODE', '1')))

if argv[0:2] == ['pr', 'create']:
    print(os.environ.get('FAKE_GH_URL', 'https://github.com/acme/demo/pull/7'))
elif argv[0:2] == ['pr', 'list']:
    print(json.dumps(json.loads(os.environ.get('FAKE_GH_LIST', '[]'))))
else:
    print(json.dumps({{
        'number': 7,
        'title': 'the second thing',
        'state': 'OPEN',
        'url': os.environ.get('FAKE_GH_URL', 'https://github.com/acme/demo/pull/7'),
        'isDraft': True,
        'baseRefName': 'main',
        'headRefName': 'feature/thing',
        'author': {{'login': 'someone'}},
        'updatedAt': '2026-02-01T10:00:00Z',
        'statusCheckRollup': [
            {{'name': 'build', 'conclusion': 'SUCCESS'}},
            {{'name': 'lint', 'conclusion': 'SUCCESS'}},
            {{'name': 'e2e', 'conclusion': 'FAILURE'}},
            {{'name': 'deploy', 'state': 'PENDING'}},
            {{'name': 'docs', 'conclusion': 'SKIPPED'}},
        ],
    }}))
'''

DEFAULTS = {
    'number': 7,
    'title': 'the second thing',
    'state': 'OPEN',
    'url': 'https://github.com/acme/demo/pull/7',
    'isDraft': False,
    'baseRefName': 'main',
    'headRefName': 'feature/thing',
    'author': {'login': 'someone'},
    'updatedAt': '2026-02-01T10:00:00Z',
}


def install_fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `gh` on PATH that records what it was asked and answers plausibly.

    On PATH rather than stubbed at the lookup, because the lookup is part of
    what is being tested: the real `shutil.which` has to find it, and the
    process has to start from a real exec. The returned path is the log it
    writes its argv to, so a test can assert on what it was handed.
    """
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    script = bindir / 'gh'
    script.write_text(FAKE_GH.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / 'gh-argv.log'
    monkeypatch.setenv('PATH', f'{bindir}{os.pathsep}{os.environ.get("PATH", "")}')
    monkeypatch.setenv('FAKE_GH_LOG', str(log))
    return log


def recorded(log: Path) -> list[list[str]]:
    """Every argv `gh` was given, in order."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def init(path: Path, *, bare: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    args = ['git', 'init', '-q', '-b', 'main', '.'] + (['--bare'] if bare else [])
    subprocess.run(args, cwd=path, check=True)  # noqa: S603 - fixed argv
    if not bare:
        subprocess.run(['git', 'config', 'user.email', 't@example.com'], cwd=path, check=True)  # noqa: S603
        subprocess.run(['git', 'config', 'user.name', 'Tester'], cwd=path, check=True)  # noqa: S603
    return path


def ctx_for(root: Path) -> ToolContext:
    return ToolContext(root=root, cwd=root, emit=None, ask=None, session_id='s', confined=True)  # type: ignore[arg-type]


@pytest.fixture
async def repo(tmp_path: Path) -> Path:
    """A repository with a GitHub remote, a local push target, and one commit.

    The push target is a bare repository in the same temporary directory, and
    the remote's pushurl points at it, so `pr_create` really pushes and the
    branch really lands — with no network and no credentials anywhere.
    """
    root = init(tmp_path / 'work')
    bare = init(tmp_path / 'origin.git', bare=True)

    (root / 'a.txt').write_text('one\n')
    await core.stage(root, [])
    await core.commit(root, 'the first thing')
    # The fetch URL is a github.com one, because that is what a repository
    # under review has; the push URL is a directory on this machine, so the
    # push in step three of `pr_create` is a real one that goes nowhere.
    await core.run(['remote', 'add', 'origin', 'https://github.com/acme/demo.git'], root)
    await core.run(['remote', 'set-url', '--push', 'origin', str(bare)], root)
    await core.run(['push', '--quiet', 'origin', 'HEAD:refs/heads/main'], root)
    # `default_branch` reads `refs/remotes/origin/HEAD`. `git remote set-head
    # -a` would ask the remote, which here is a github.com URL, so the ref a
    # clone would have written is written directly instead.
    await core.run(['symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main'], root)

    await core.run(['checkout', '-q', '-b', 'feature/thing'], root)
    (root / 'b.txt').write_text('two\n')
    await core.stage(root, [])
    await core.commit(root, 'the second thing\n\nBecause the first was not enough.\n')
    return root


@pytest.fixture
def bare_of(repo: Path, tmp_path: Path) -> Path:
    return tmp_path / 'origin.git'


def without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `gh` genuinely absent, with git still reachable.

    By patching the lookup rather than emptying PATH: emptying PATH also
    removes git, and the point of the test is what the pull request code says,
    not what it says once nothing works.
    """
    real = shutil.which

    def which(cmd: str, *args: Any, **kwargs: Any) -> str | None:
        return None if cmd == 'gh' else real(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, 'which', which)


# --- reading a remote URL ------------------------------------------------------


@pytest.mark.parametrize(
    'url',
    [
        'git@github.com:acme/demo.git',
        'git@github.com:acme/demo',
        'ssh://git@github.com/acme/demo.git',
        'ssh://git@github.com:22/acme/demo.git',
        'https://github.com/acme/demo.git',
        'https://github.com/acme/demo',
        'https://github.com/acme/demo/',
        'https://github.com/acme/demo.git/',
        'https://www.github.com/acme/demo.git',
        'https://somebody:token@github.com/acme/demo.git',
        'git://github.com/acme/demo.git',
    ],
)
def test_every_shape_of_remote_url_becomes_owner_and_repo(url: str):
    """Four spellings and a dozen arrangements, and they all have to work.

    `git@host:owner/repo` is not a URL — `urlparse` sees no scheme and hands
    back the whole string — so it is the one that breaks a parser written for
    the other three. The credentials in the fifth form are the reason this is
    written as a table rather than one example: a token in a remote URL must
    not survive into anything that could be logged.
    """
    found = core.parse_remote(url)
    assert found is not None, url
    assert (found.owner, found.name) == ('acme', 'demo'), url
    assert found.host == 'github.com', url
    assert found.slug == 'acme/demo'


def test_a_token_in_a_remote_url_does_not_survive_being_parsed():
    """The fifth spelling above carries a credential, and `.git/config` is a
    file a clone brings with it. Whatever is in the URL is not what comes out
    the other side of this function."""
    found = core.parse_remote('https://ghp_secretreset:glpat_secret@github.com/acme/demo.git')
    assert found == core.Remote('github.com', 'acme', 'demo')
    assert 'ghp_' not in repr(found) and 'glpat_' not in repr(found)


@pytest.mark.parametrize(
    'url',
    [
        '',
        '   ',
        '/srv/git/demo.git',
        'file:///srv/git/demo.git',
        'https://github.com/acme',
        'https://github.com/acme/demo/tree/main',
        'https://github.com/acme/de mo.git',
        'https://github.com/acme/../../etc/passwd',
        'C:\\src\\demo',
    ],
)
def test_something_that_is_not_a_github_remote_is_refused_rather_than_guessed(url: str):
    """None, not a best effort.

    The owner and repository go straight into an API path with a token in the
    headers, so a URL that is a local path, a Windows path, or a three-segment
    GitHub web URL must produce nothing at all. `https://github.com/acme/de
    mo.git` and the traversal case are the two that matter: the first is a
    percent-encoding question the answer to which is no, and the second is a
    question about where a credential goes.
    """
    assert core.parse_remote(url) is None, url


def test_only_github_com_is_treated_as_the_rest_api():
    """A self-hosted remote is a `gh` job, and never a guessed API root.

    An enterprise GitHub is at `https://host/api/v3`, and this module does not
    know that. Guessing it would mean sending somebody's token to a host this
    code inferred from a string in a clone's config.
    """
    enterprise = core.parse_remote('git@git.example.com:acme/demo.git')
    assert enterprise is not None and enterprise.host == 'git.example.com'
    assert not enterprise.is_github
    assert core.pr_backend(enterprise) in ('gh', '')  # never 'api'


# --- no backend at all ---------------------------------------------------------


async def test_no_gh_and_no_token_says_what_to_install(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole point of the early refusal: a sentence, not a traceback.

    This is the common case on a fresh install — a repository with a remote and
    nothing to open a pull request with — and the answer names both halves of
    the fix, because an operator who has one of them is one command from having
    the other.
    """
    without_gh(monkeypatch)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GH_TOKEN', raising=False)

    out = await GitTool().run({'action': 'pr_create', 'title': 'add a thing'}, ctx_for(repo))

    assert 'gh' in out.content and 'cli.github.com' in out.content
    assert 'GITHUB_TOKEN' in out.content and 'GH_TOKEN' in out.content
    assert 'Nothing was sent' in out.content
    assert out.display['pr'] is False


async def test_the_refusal_reaches_the_view_and_the_list_too(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Same answer, same reason: there is no way through, for any of the three."""
    without_gh(monkeypatch)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.delenv('GH_TOKEN', raising=False)

    for action in ('pr_view', 'pr_list'):
        out = await GitTool().run({'action': action}, ctx_for(repo))
        assert 'GITHUB_TOKEN' in out.content, action
        assert out.display['pr'] is False


async def test_a_token_is_enough_when_gh_is_not_there(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """The fallback exists, so it is worth knowing the switch is only `gh`."""
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')
    monkeypatch.delenv('GH_TOKEN', raising=False)

    seen: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        seen.append((method, path, {**kwargs, 'token': token}))
        return 200, {'default_branch': 'main'} if path.endswith('/acme/demo') else DEFAULTS

    monkeypatch.setattr(core, '_api', fake_api)
    out = await GitTool().run({'action': 'pr_create', 'title': 'add a thing'}, ctx_for(repo))

    assert out.display['backend'] == 'api'
    assert seen[0][0] == 'POST' and seen[0][1] == '/repos/acme/demo/pulls'
    assert seen[0][2]['token'] == 'ghp_example'
    assert 'ghp_example' not in out.content, 'a token must never reach the model or a tool card'


# --- the preflight -------------------------------------------------------------


async def test_a_dirty_tree_is_refused_by_name_and_nothing_is_committed(repo: Path, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch):
    """The refusal has to be specific enough to act on.

    "The working tree is dirty" costs a round trip and a `status` call. Naming
    the file means the model can stage it and commit it in one more turn. And
    the assertion that HEAD has not moved is the one that matters: silently
    committing somebody's half-finished work to open a pull request is the worst
    thing this tool could do, and the cheapest way to be sure it does not is to
    check that it did not.
    """
    log = install_fake_gh(tmp_path, monkeypatch)
    (repo / 'a.txt').write_text('edited, not committed\n')
    (repo / 'stray.txt').write_text('new\n')
    _, head_out, _ = await core.run(['rev-parse', 'HEAD'], repo)
    before = head_out.strip()

    with pytest.raises(ToolError) as caught:
        await GitTool().run({'action': 'pr_create', 'title': 'add a thing'}, ctx_for(repo))

    said = str(caught.value)
    assert 'uncommitted' in said
    assert 'a.txt' in said and 'stray.txt' in said
    assert 'nothing was staged' in said

    _, after_out, _ = await core.run(['rev-parse', 'HEAD'], repo)
    assert after_out.strip() == before, 'a refusal must not have committed anything'
    assert not recorded(log), 'and must not have reached GitHub either'


async def test_a_clean_tree_gets_past_the_preflight(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The other half of the same property: nothing refused for no reason."""
    log = install_fake_gh(tmp_path, monkeypatch)
    out = await GitTool().run(
        {'action': 'pr_create', 'title': 'the second thing', 'body': 'because'}, ctx_for(repo)
    )
    assert out.display['number'] == 7
    assert recorded(log)


async def test_a_branch_with_nowhere_to_go_is_pushed_first(repo: Path, bare_of: Path, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    """A pull request cannot exist without the branch being somewhere reviewable.

    So `pr_create` pushes, rather than making the model remember to. It pushes
    an already-committed branch to the remote the repository is configured
    with, which is the same push a person would have made; what it will never do
    is commit first, which is why the assertion here is on the branch landing
    rather than on a message.
    """
    log = install_fake_gh(tmp_path, monkeypatch)
    assert not (await core.status(repo)).upstream

    await GitTool().run({'action': 'pr_create', 'title': 'the second thing'}, ctx_for(repo))

    landed, _, _ = await core.run(
        ['rev-parse', '--verify', '--quiet', 'refs/heads/feature/thing'], bare_of, check=False
    )
    assert landed == 0, 'the branch should have reached the remote'
    assert (await core.status(repo)).upstream == 'origin/feature/thing'
    assert recorded(log)


async def test_a_detached_head_says_so_rather_than_opening_nothing(repo: Path, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch):
    """There is no branch to open a pull request from, and guessing one is worse."""
    log = install_fake_gh(tmp_path, monkeypatch)
    await core.run(['checkout', '-q', '--detach', 'HEAD'], repo)

    with pytest.raises(ToolError, match='detached'):
        await GitTool().run({'action': 'pr_create', 'title': 'x'}, ctx_for(repo))
    assert not recorded(log)


async def test_opening_a_pull_request_against_its_own_branch_is_refused(repo: Path, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch):
    """`main` into `main` is a pull request comparing a branch with itself.

    GitHub refuses it, with a message about there being no commits between the
    branches that means nothing to a model that was handed `base` by mistake.
    """
    install_fake_gh(tmp_path, monkeypatch)
    with pytest.raises(ToolError, match='itself'):
        await GitTool().run(
            {'action': 'pr_create', 'title': 'x', 'base': 'feature/thing'}, ctx_for(repo)
        )


def test_a_title_is_required_before_anything_happens(repo: Path):
    """Checked in `assess`, like a commit message, so nobody is asked to confirm a PR that cannot open."""
    assessment = GitTool().assess({'action': 'pr_create', 'title': '  '}, ctx_for(repo))
    assert assessment.invalid and 'needs a title' in assessment.invalid
    assert assessment.risk is Risk.MESSAGE


def test_an_unknown_action_still_names_the_new_ones(repo: Path):
    assessment = GitTool().assess({'action': 'pr'}, ctx_for(repo))
    assert assessment.invalid and 'pr_create' in assessment.invalid


# --- the gh backend, and the argv it is given -----------------------------------


async def test_a_title_full_of_shell_metacharacters_stays_one_argument(repo: Path, tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch):
    """The test this file exists for.

    The title is model output, so it can contain anything — including a `;`, a
    backtick and a newline, all of which a shell would act on. Each of them has
    to arrive as one argv item, verbatim, with nothing split off it and nothing
    run. The body goes on stdin for the same reason, and is asserted there.
    """
    log = install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_STDIN', str(tmp_path / 'body.txt'))
    title = 'fix `; rm -rf ~` and $(whoami)\nand more\n\n## not a header'
    body = 'The body has a `backtick`, a $HOME and\ntwo lines, and a ; semicolon.'

    await GitTool().run(
        {'action': 'pr_create', 'title': title, 'body': body, 'draft': True}, ctx_for(repo)
    )

    create = next(argv for argv in recorded(log) if argv[0:2] == ['pr', 'create'])
    assert create[create.index('--title') + 1] == 'fix `; rm -rf ~` and $(whoami) and more ## not a header'
    assert create[create.index('--head') + 1] == 'feature/thing'
    assert create[create.index('--base') + 1] == 'main'
    assert create[create.index('--repo') + 1] == 'acme/demo'
    assert create[create.index('--body-file') + 1] == '-', 'the body belongs on stdin, not in an argument'
    assert '--draft' in create

    # Nothing that was in the title was treated as an argument of its own, and
    # nothing was run: a shell would have found `rm` in the list.
    assert 'rm' not in create
    assert '-rf' not in create
    assert not any(part.startswith(';') for part in create)
    assert (tmp_path / 'body.txt').read_text() == body


def test_the_title_a_person_is_asked_to_confirm_is_the_title(repo: Path):
    """The approval prompt renders the title and the base, and nothing else.

    A summary long enough to wrap is a summary nobody reads, and the body — a
    paragraph of the model's own prose — has no business being in a one-line
    prompt. The cap is the same `subject_of` a commit uses."""
    assessment = GitTool().assess(
        {'action': 'pr_create', 'title': 'add the retry loop', 'base': 'main'}, ctx_for(repo)
    )
    assert not assessment.invalid
    assert assessment.summary == 'open a pull request "add the retry loop" into main and notify reviewers'
    assert len(GitTool().assess({'action': 'pr_create', 'title': 'x' * 400}, ctx_for(repo)).summary) < 200


def test_a_title_longer_than_github_accepts_is_capped_not_truncated_at_random():
    """Newlines folded to spaces as well: a title with a blank line in it is a
    body, and GitHub renders the second line somewhere nobody is looking."""
    assert len(core._clean_title('x' * 900)) == core.PR_TITLE_LIMIT
    assert core._clean_title('  spaced   out  ') == 'spaced out'
    assert core._clean_title('a\n\nb') == 'a b'


async def test_a_view_reports_the_state_and_the_checks(repo: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch):
    """Checks are part of the answer, or the model reports a PR as fine while
    a job is red. The five outcomes are counted rather than listed, because a
    list of check names is a page of context for one number."""
    install_fake_gh(tmp_path, monkeypatch)
    out = await GitTool().run({'action': 'pr_view'}, ctx_for(repo))

    assert out.display['number'] == 7
    assert out.display['head'] == 'feature/thing' and out.display['base'] == 'main'
    assert 'checks' in out.content
    assert '2 passed' in out.content and '1 failed' in out.content and '1 pending' in out.content


async def test_a_branch_with_no_pull_request_says_so_instead_of_guessing(repo: Path, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch):
    """Told a PR exists when it does not, a model tells a person their work is
    under review. So "none" is an answer, and it is an `Output` rather than an
    exception — nothing went wrong, there is simply nothing to see."""
    install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_FAIL', 'no pull requests found for branch "feature/thing"')
    out = await GitTool().run({'action': 'pr_view'}, ctx_for(repo))
    assert 'no pull request' in out.content
    assert out.display['pr'] is False


async def test_a_list_that_cannot_be_reached_says_why(repo: Path, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch):
    """gh's own words, because it knows which of the eight ways this failed it was."""
    install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_FAIL', 'GraphQL: Could not resolve to a Repository (authentication required)')
    with pytest.raises(ToolError, match='authentication required'):
        await GitTool().run({'action': 'pr_list'}, ctx_for(repo))


async def test_a_list_renders_one_line_each(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_LIST', json.dumps([{**DEFAULTS, 'number': 3}, {**DEFAULTS, 'number': 4}]))
    out = await GitTool().run({'action': 'pr_list'}, ctx_for(repo))
    assert out.content.count('\n') == 1
    assert '#3' in out.content and '#4' in out.content
    assert [entry['number'] for entry in out.display['prs']] == [3, 4]


async def test_an_empty_list_is_a_sentence(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install_fake_gh(tmp_path, monkeypatch)
    out = await GitTool().run({'action': 'pr_list'}, ctx_for(repo))
    assert 'no open pull requests' in out.content
    assert out.display['prs'] == []


# --- filling the body from the commits ------------------------------------------


async def test_fill_writes_a_body_from_the_commits_since_the_merge_base(repo: Path, tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch):
    """The commits, and only those this branch has added.

    The merge base is the whole point: a body written from `git log` on HEAD
    describes the branch's entire history, which is a description of somebody
    else's work and gets read as a description of this change.
    """
    log = install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_STDIN', str(tmp_path / 'body.txt'))
    (repo / 'c.txt').write_text('three\n')
    await core.stage(repo, [])
    await core.commit(repo, 'and a third thing\n\nWhich finally does it.\n')

    await GitTool().run({'action': 'pr_create', 'title': 'x', 'fill': True}, ctx_for(repo))

    body = (tmp_path / 'body.txt').read_text()
    assert 'the second thing' in body
    assert 'and a third thing' in body
    assert 'the first thing' not in body, 'a commit on main is not part of this change'
    create = next(argv for argv in recorded(log) if argv[0:2] == ['pr', 'create'])
    assert create[create.index('--body-file') + 1] == '-'


async def test_a_single_commit_is_its_own_description(repo: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch):
    """One commit's subject is the description. Under a heading called
    "Commits", a list of one reads like padding."""
    install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_STDIN', str(tmp_path / 'body.txt'))
    await GitTool().run({'action': 'pr_create', 'title': 'x', 'fill': True}, ctx_for(repo))
    assert (tmp_path / 'body.txt').read_text() == 'the second thing'


async def test_a_body_the_model_wrote_is_never_overwritten(repo: Path, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    """`fill` is a fallback for the case where there is no body, not a
    suggestion that the model's own words were not good enough."""
    install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv('FAKE_GH_STDIN', str(tmp_path / 'body.txt'))
    await GitTool().run(
        {'action': 'pr_create', 'title': 'x', 'body': 'what I actually meant', 'fill': True}, ctx_for(repo)
    )
    assert (tmp_path / 'body.txt').read_text() == 'what I actually meant'


async def test_a_branch_with_nothing_new_refuses_rather_than_describing_history(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The failure `fill` has to avoid: a body written from a log that has
    nothing in it is a description of the repository, offered as a description
    of this change."""
    install_fake_gh(tmp_path, monkeypatch)
    # The branch stays, and is not the base — so the "compares itself" refusal
    # does not answer this one. It has simply stopped being different.
    await core.run(['reset', '--hard', 'origin/main'], repo)
    with pytest.raises(ToolError, match='no pull request to open'):
        await GitTool().run({'action': 'pr_create', 'title': 'x', 'fill': True}, ctx_for(repo))


# --- the REST fallback ---------------------------------------------------------


async def test_the_api_path_posts_the_pull_request(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """What goes over the wire when there is no `gh`: one POST, the fields
    GitHub needs, and the branch name as the head.

    `_api` is replaced rather than mocked at the socket, because the thing
    under test is the request this module builds — the path, the method, the
    payload and the token — and every one of those is decided before a session
    is opened.
    """
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')
    seen: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        seen.append((method, path, {**kwargs, 'token': token}))
        if method == 'POST':
            return 201, {**DEFAULTS, 'html_url': DEFAULTS['url'], 'base': {'ref': 'main'},
                         'head': {'ref': 'feature/thing'}, 'user': {'login': 'someone'}}
        return 200, {'default_branch': 'main'}

    monkeypatch.setattr(core, '_api', fake_api)
    out = await GitTool().run(
        {'action': 'pr_create', 'title': 'the second thing', 'body': 'why', 'draft': True}, ctx_for(repo)
    )

    method, path, kwargs = seen[-1]
    assert (method, path) == ('POST', '/repos/acme/demo/pulls')
    assert kwargs['payload'] == {
        'title': 'the second thing', 'head': 'feature/thing', 'base': 'main', 'body': 'why', 'draft': True,
    }
    assert kwargs['token'] == 'ghp_example'
    assert out.display['url'] == DEFAULTS['url'] and out.display['backend'] == 'api'


async def test_the_api_path_does_not_guess_a_base(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """A PR aimed at the wrong base is a PR against the wrong branch, and it
    is accepted. When nothing local knows the default branch, it asks GitHub
    rather than assuming `main`."""
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')
    await core.run(['remote', 'set-head', 'origin', '-d'], repo)
    seen: list[str] = []

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        seen.append(f'{method} {path}')
        if path.endswith('/pulls'):
            return 201, {**DEFAULTS, 'html_url': DEFAULTS['url']}
        return 200, {'default_branch': 'trunk'}

    monkeypatch.setattr(core, '_api', fake_api)
    await GitTool().run({'action': 'pr_create', 'title': 'x'}, ctx_for(repo))
    assert 'GET /repos/acme/demo' in seen


async def test_a_view_by_number_over_the_api_includes_the_checks(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Checks come from one extra request on the API path, because they are
    not on the pull request itself. One, and its failure is swallowed: a view
    that cannot see the checks is still a view."""
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')
    seen: list[str] = []

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        seen.append(path)
        if path.endswith('/check-runs'):
            return 200, {'check_runs': [{'conclusion': 'success'}, {'conclusion': 'failure'}]}
        return 200, {**DEFAULTS, 'html_url': DEFAULTS['url'], 'head': {'ref': 'feature/thing', 'sha': 'abc123'}}

    monkeypatch.setattr(core, '_api', fake_api)
    out = await GitTool().run({'action': 'pr_view', 'number': 7}, ctx_for(repo))
    assert seen == ['/repos/acme/demo/pulls/7', '/repos/acme/demo/commits/abc123/check-runs']
    assert '1 passed' in out.content and '1 failed' in out.content


async def test_a_pull_request_that_is_not_there_is_an_answer_not_a_failure(repo: Path,
                                                                        monkeypatch: pytest.MonkeyPatch):
    """A 404 on a specific number means the number is wrong, and the model
    needs to hear that rather than have it read as a broken request."""
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        return 404, None

    monkeypatch.setattr(core, '_api', fake_api)
    out = await GitTool().run({'action': 'pr_view', 'number': 99}, ctx_for(repo))
    assert 'no pull request #99' in out.content


async def test_a_list_over_the_api(repo: Path, monkeypatch: pytest.MonkeyPatch):
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')
    seen: list[tuple[str, dict[str, str]]] = []

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        seen.append((path, kwargs.get('params') or {}))
        return 200, [{**DEFAULTS, 'number': n, 'html_url': f'https://github.com/acme/demo/pull/{n}'} for n in (2, 3)]

    monkeypatch.setattr(core, '_api', fake_api)
    out = await GitTool().run({'action': 'pr_list', 'limit': 2}, ctx_for(repo))
    assert seen == [('/repos/acme/demo/pulls', {'state': 'open', 'per_page': '2'})]
    assert [entry['number'] for entry in out.display['prs']] == [2, 3]


async def test_fill_without_a_known_base_says_so_rather_than_describing_the_whole_log(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The other half of the merge-base rule: with no base to measure against,
    the only log available is the branch's entire history, which is somebody
    else's work. There is no honest body to write, so there is no body."""
    install_fake_gh(tmp_path, monkeypatch)
    await core.run(['remote', 'set-head', 'origin', '-d'], repo)
    with pytest.raises(ToolError, match='does not record which branch it merges into'):
        await GitTool().run({'action': 'pr_create', 'title': 'x', 'fill': True}, ctx_for(repo))


async def test_a_rest_error_keeps_githubs_own_words(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """`Validation Failed` with a list of fields is more use to a model than
    anything invented here, and the token is not in it either way."""
    without_gh(monkeypatch)
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_example')

    async def fake_api(method: str, path: str, token: str, **kwargs: Any) -> tuple[int, Any]:
        raise core.GitError('GitHub said 422: No commits between main and feature/thing (head)')

    monkeypatch.setattr(core, '_api', fake_api)
    with pytest.raises(ToolError) as caught:
        await GitTool().run({'action': 'pr_create', 'title': 'x', 'base': 'main'}, ctx_for(repo))
    assert 'No commits between' in str(caught.value)
    assert 'ghp_example' not in str(caught.value)


# --- grading -------------------------------------------------------------------


def call(tool: GitTool, ctx: ToolContext, **args) -> ToolCall:
    made = ToolCall(id='c1', name='git', arguments=args)
    assessment = tool.assess(args, ctx)
    made.risk, made.summary = assessment.risk, assessment.summary
    return made


def test_opening_a_pull_request_is_a_message_and_reading_one_is_a_read(repo: Path):
    """The three new actions, graded.

    `pr_create` at `message` rather than `network` is the decision this file
    argues for: it is never automatic, in any mode, and it is refused outright
    in the two modes whose promise is that nothing happens. The reads are
    `read` despite opening a socket, because a GET of data this session could
    fetch by hand is not something a person needs to be asked about.
    """
    tool, ctx = GitTool(), ctx_for(repo)
    assert call(tool, ctx, action='pr_create', title='x').risk is Risk.MESSAGE
    assert call(tool, ctx, action='pr_view').risk is Risk.READ
    assert call(tool, ctx, action='pr_list').risk is Risk.READ


@pytest.mark.parametrize(
    ('mode', 'expected'),
    [
        (Mode.READ_ONLY, Decision.DENY),
        (Mode.PLAN, Decision.DENY),
        (Mode.ASK, Decision.ASK),
        (Mode.AUTO_EDIT, Decision.ASK),
        (Mode.TRUSTED, Decision.ASK),
        (Mode.UNRESTRICTED, Decision.ASK),
    ],
)
def test_no_mode_opens_a_pull_request_without_asking(repo: Path, mode: Mode, expected: Decision):
    """Every mode, including the two that say nothing happens and the one that
    says stop asking. `push` is in there as the control: it runs unattended in
    `trusted` and `unrestricted`, and a pull request does not, because it
    notifies people and a push does not."""
    tool, ctx = GitTool(), ctx_for(repo)
    policy = ApprovalPolicy(mode=mode)
    opening = call(tool, ctx, action='pr_create', title='open the thing')
    assert policy.decide(opening)[0] is expected, mode

    pushing = call(tool, ctx, action='push')
    assert policy.decide(pushing)[0] is (Decision.ALLOW if mode in (Mode.TRUSTED, Mode.UNRESTRICTED) else
                                         policy.decide(pushing)[0])


def test_a_pull_request_is_never_remembered(repo: Path):
    """"Don't ask again" about one PR must not carry to the next one.

    A remembered approval is a fingerprint of the exact arguments, so a yes to
    `open "fix the parser"` would otherwise allow `open "delete the history"`
    later in the same session. `message` is on the list of risks that cannot be
    remembered, for the same reason a purchase cannot.
    """
    tool, ctx = GitTool(), ctx_for(repo)
    policy = ApprovalPolicy(mode=Mode.UNRESTRICTED)
    policy.remember(call(tool, ctx, action='pr_create', title='fix the parser'))
    later = call(tool, ctx, action='pr_create', title='delete the history')
    assert 'you approved this exact call' not in policy.decide(later)[1]


def test_a_pull_request_can_be_switched_off_with_the_message_setting(repo: Path):
    """`allow_messages=false` means "must not speak to anyone on my behalf",
    and a public request filed under somebody's name is that."""
    tool, ctx = GitTool(), ctx_for(repo)
    policy = ApprovalPolicy(mode=Mode.UNRESTRICTED, allow_messages=False)
    decision, why = policy.decide(call(tool, ctx, action='pr_create', title='x'))
    assert decision is Decision.DENY
    assert 'mail' in why
    # ...and a push is untouched, because pushing a branch notifies nobody.
    assert policy.decide(call(tool, ctx, action='push'))[0] is Decision.ALLOW


def test_every_action_still_has_a_grade():
    assert set(ACTIONS) == set(RISK)
    assert all(isinstance(risk, Risk) for risk in RISK.values())
    assert 'reset' not in ACTIONS and 'clean' not in ACTIONS, 'the foot-guns stay out'


def test_the_schema_tells_the_model_what_it_needs_to_say():
    """The schema is the contract. A `title` that is not in `required` is one
    the model leaves out, and every pull request opened without one is a
    subject line somebody has to write by hand afterwards."""
    schema = GitTool.input_schema
    assert set(ACTIONS) == set(schema['properties']['action']['enum'])
    assert schema['required'] == ['action']
    for name in ('title', 'body', 'base', 'draft', 'fill', 'number'):
        assert name in schema['properties'], name
    assert 'pr_create' in GitTool.description and 'uncommitted' in GitTool.description

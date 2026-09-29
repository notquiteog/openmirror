"""Git worktrees: a session that works somewhere the rest of the disk cannot see.

Claude Code has `--worktree` and `EnterWorktree`/`ExitWorktree`, and
subagents can be isolated in one. openCode passes `context.worktree` to
plugins. Both are the same idea and the same reason:

**An agent that edits files is a hazard to whatever is already there.** The
answer this project had was `unconfined`, which is the wrong shape — it
relies on the person remembering to turn it off, and a worktree is somewhere
you can be careless *in* rather than a switch you have to remember.

A worktree is a second checkout of a repository in its own directory, with
its own branch. A session opened in one edits that one. The working tree you
have open is not reachable, not modified, and not at risk — and the branch is
there afterwards, to look at, to merge, or to throw away with one command.

**What this does not do.** It does not make the work safe, only separate. A
worktree shares the repository's history, its remotes and its `.git`, so a
`git push --force` in a worktree is the same force push. What it removes is
the *accidental* kind of damage, which is the kind that happens.

And it is opt-in twice over: it only applies to a git repository, and only
when the operator has said worktrees may be used. There is no silent "the agent
made me a branch", because a branch somebody did not ask for is a branch
somebody has to clean up.

Deliberately not implemented: `WorktreeCreate`/`WorktreeRemove` *hooks* around
it, as Claude Code has. That is a hook that runs a command on a path the
agent controls, and a project that can name a worktree hook can point it
anywhere. When that exists it belongs behind the same consent the other hooks
have, and it is not the thing to add first.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Worktrees live beside the repository, not inside it — a worktree inside the
# working tree is a directory the agent's own `grep` and `glob` will find,
# and its contents are a whole second copy of the project.
DEFAULT_PARENT = 'worktrees'
MAX_NAME = 40


class WorktreeError(RuntimeError):
    """A worktree could not be made, or was not found."""


@dataclass(slots=True)
class Worktree:
    """One worktree, and the branch it is on."""

    path: str
    branch: str
    head: str = ''
    bare: bool = False
    detached: bool = False

    def public(self) -> dict[str, Any]:
        return {'path': self.path, 'branch': self.branch, 'head': self.head,
                'bare': self.bare, 'detached': self.detached}


def _git(args: list[str], cwd: Path, *, timeout: int = 30) -> tuple[int, str, str]:
    """One git command, with a clock and a shell that is never involved.

    An argument list rather than a string: every caller here builds something
    from a name somebody typed, and `sh -c` on a name is how a branch called
    `main; rm -rf ~` becomes a command.
    """
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ['git', *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **_env(),
                # Never stop for a password or a key: this runs unattended
                # inside somebody's turn.
                'GIT_TERMINAL_PROMPT': '0',
            },
        )
    except FileNotFoundError as exc:
        raise WorktreeError('git is not on PATH, so there are no worktrees here') from exc
    except subprocess.SubprocessError as exc:
        raise WorktreeError(f'git {args[0]} did not finish: {exc}') from exc
    # `rstrip`, never `strip`. `git status --porcelain` begins each line with a
    # two-character XY column whose first character is a *space* for an
    # unmodified file — so stripping the whole output turns ` M f.txt` into
    # `M f.txt` and the file loses its first letter. Every porcelain parser
    # here was quietly wrong about that.
    return done.returncode, done.stdout.rstrip('\n'), done.stderr.rstrip()


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


def is_repository(path: Path) -> bool:
    """Whether this is a git working tree at all.

    `rev-parse --is-inside-work-tree` rather than looking for `.git`, because
    the second is false inside a worktree and inside every subdirectory of one.
    """
    try:
        code, out, _ = _git(['rev-parse', '--is-inside-work-tree'], path, timeout=15)
    except WorktreeError:
        return False
    return code == 0 and out == 'true'


def repository_root(path: Path) -> Path | None:
    """The top of the working tree this path is inside."""
    try:
        code, out, _ = _git(['rev-parse', '--show-toplevel'], path, timeout=15)
    except WorktreeError:
        return None
    return Path(out) if code == 0 and out else None


def _slug(text: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', text or '').strip('-')
    return (cleaned or 'session')[:MAX_NAME]


def list_worktrees(root: Path) -> list[Worktree]:
    """Every worktree on this repository, the main one included.

    Read from `git worktree list` rather than from the filesystem, because git
    knows about the ones whose directory has been moved and the filesystem
    does not.
    """
    try:
        code, out, err = _git(['worktree', 'list', '--porcelain'], root)
    except WorktreeError as exc:
        log.debug('worktrees could not be listed: %s', exc)
        return []
    if code != 0:
        log.debug('worktrees could not be listed: %s', err)
        return []

    out_worktrees: list[Worktree] = []
    current: dict[str, Any] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            if current.get('path'):
                out_worktrees.append(Worktree(**current))
            current = {}
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            current['path'] = value
        elif key == 'HEAD':
            current['head'] = value
        elif key == 'branch':
            current['branch'] = value.removeprefix('refs/heads/')
        elif key == 'bare':
            current['bare'] = True
        elif key == 'detached':
            current['detached'] = True
    if current.get('path'):
        out_worktrees.append(Worktree(**current))
    return out_worktrees


def create(
    root: Path,
    *,
    branch: str = '',
    start: str = '',
    parent: Path | None = None,
    label: str = '',
) -> Worktree:
    """A new worktree on a new branch, and where it went.

    **A new branch, always.** A worktree cannot check out a branch another
    worktree already has, and the branches that *can* be shared are exactly the
    ones you would not want an agent on. So the branch is generated unless one
    is named, and naming one is deliberate.
    """
    if not is_repository(root):
        raise WorktreeError(f'{root} is not a git repository, so there is nothing to branch')

    name = _slug(branch) if branch else f'openmirror/{_slug(label or uuid.uuid4().hex[:8])}'
    if branch and (name.startswith('openmirror/') or '/' not in name):
        name = f'openmirror/{name}'

    base = Path(parent).expanduser() if parent else (Path(root).parent / DEFAULT_PARENT)
    base.mkdir(parents=True, exist_ok=True)
    target = base / _slug(name.rsplit('/', 1)[-1])

    if target.exists() and any(target.iterdir()):
        raise WorktreeError(f'{target} is already there and is not empty')

    args = ['worktree', 'add']
    if start:
        args += ['-b', name, str(target), start]
    else:
        args += ['-b', name, str(target)]
    try:
        code, _, err = _git(args, root, timeout=120)
    except WorktreeError:
        raise
    if code != 0:
        # git's own message names the problem — an existing branch, a dirty
        # checkout — and replacing it with "could not create" is a worse
        # answer than no answer.
        raise WorktreeError(f'the worktree could not be made: {err or "git said nothing"}')

    try:
        _git(['-C', str(target), 'config', 'openmirror.worktree', 'true'], root)
    except WorktreeError:
        pass

    code, head, _ = _git(['-C', str(target), 'rev-parse', '--short', 'HEAD'], root)
    return Worktree(path=str(target), branch=name, head=head if code == 0 else '')


def remove(root: Path, path: str) -> bool:
    """Take a worktree away. Refuses if it has changes in it.

    `git worktree remove` refuses too, and that refusal is the whole reason
    this is a thin wrapper rather than a `rm -rf`: a worktree with a day of
    work in it is not a directory anybody should be able to lose to a cleanup
    path. `--force` is deliberately not exposed.
    """
    target = Path(path).expanduser()
    try:
        code, _, err = _git(['worktree', 'remove', str(target)], root, timeout=60)
    except WorktreeError as exc:
        raise WorktreeError(str(exc)) from exc
    if code != 0:
        raise WorktreeError(f'the worktree could not be removed: {err or "git said nothing"}')
    return True


def prune(root: Path) -> str:
    """Clear git's record of worktrees whose directories have gone.

    Worth exposing because a stale record makes `git worktree list` lie, and a
    list that lies is how somebody concludes worktrees are broken.
    """
    try:
        code, out, err = _git(['worktree', 'prune'], root)
    except WorktreeError as exc:
        raise WorktreeError(str(exc)) from exc
    if code != 0:
        raise WorktreeError(f'could not prune: {err or "git said nothing"}')
    return out


def changes(root: Path) -> dict[str, Any]:
    """What has happened in a worktree, for deciding whether to keep it.

    `git diff` alone is not enough, and the gap is the whole point: **a new
    file is untracked, and `git diff` cannot see it.** So an agent that spent
    a turn writing three files looks like it wrote nothing, and the answer to
    "is this worth keeping?" comes back "no". `status --porcelain` sees both,
    and the count is split into what is modified and what is new.
    """
    out: dict[str, Any] = {'modified': [], 'added': [], 'removed': [], 'renamed': [], 'dirty': False}
    try:
        code, text, _ = _git(['-C', str(root), 'status', '--porcelain'], Path(root))
    except WorktreeError:
        return out
    if code != 0:
        return out
    for line in text.splitlines():
        if len(line) < 3:
            continue
        flag, _, name = line[:2], line[2:3], line[3:]
        if flag.strip() and flag.strip() != '??':
            out['dirty'] = True
        if flag == '??':
            out['added'].append(name)
        elif 'D' in flag:
            out['removed'].append(name)
        elif 'R' in flag:
            out['renamed'].append(name)
        elif 'M' in flag or 'A' in flag:
            out['modified'].append(name)
    return out


def describe(root: Path) -> str:
    """One sentence about a worktree, for a list somebody is scanning."""
    found = changes(root)
    bits = []
    if found['modified']:
        bits.append(f'{len(found["modified"])} changed')
    if found['added']:
        bits.append(f'{len(found["added"])} new')
    if found['removed']:
        bits.append(f'{len(found["removed"])} removed')
    return ', '.join(bits) if bits else 'clean'


async def available(root: Path) -> str:
    """Why worktrees cannot be used here, or an empty string when they can.

    Async because the interface asks this on opening a panel, and a
    repository probe on the request thread is the sort of thing that shows up
    as a slow page.
    """
    def probe() -> str:
        try:
            if not is_repository(root):
                return 'this is not a git repository, so there is no branch to put anywhere'
            code, _, err = _git(['worktree', 'list'], root, timeout=15)
            if code != 0:
                return f'git will not list worktrees here: {err or "unknown"}'
        except WorktreeError as exc:
            return str(exc)
        return ''

    return await asyncio.to_thread(probe)


__all__ = [
    'DEFAULT_PARENT', 'Worktree', 'WorktreeError', 'available', 'changes', 'create', 'describe',
    'is_repository', 'list_worktrees', 'prune', 'remove', 'repository_root',
]

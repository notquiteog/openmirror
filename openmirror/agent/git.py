"""Reading and writing a git repository, without going through a shell.

The agent could already do all of this with `shell` — and the shell's risk
classifier knows `git status` from `git push --force`. This module exists
because a shell is the wrong tool for the job that is most routine in the
product. `git status` is the first thing anyone asks for, on every turn, in
every project; making the model learn porcelain format, guess the right flags
and parse the result out of a blob of text is work that produces a worse
answer every time, and on a 12B model reliably does not happen at all.

So the parsing is done here, once, and returned as something a model can read
directly. The flags are chosen rather than guessed: `-z` and `--porcelain=v2`
are the machine formats, and the only reason to avoid them is that they are
hard to read by eye — which is a cost paid by a human in a terminal, never by
the thing reading this.

Three decisions worth writing down:

* **No shell.** `create_subprocess_exec` with an argument list, not
  `create_subprocess_shell` with a string. A path is a path here: a file
  called `; rm -rf ~` is a filename, and the shell tool is one layer away for
  anything that genuinely needs a pipe.
* **A repository is required, and its absence is not an error worth
  raising from `assess`.** Someone asks a question in a directory that is not
  under version control — a home directory, `/tmp`, a fresh project — and the
  answer is a sentence, not a traceback. `require_repo` is called from `run`
  so the model reads it and moves on, which is what `ToolError` is for.
* **Nothing here pushes by default and nothing discards work.** `push` is a
  separate action with its own grade, and there is no `reset --hard` or
  `clean -f` in the action list at all: both are one line of `shell` away,
  both are already graded `destructive` there, and putting a foot-gun in a
  first-class tool is how it stops being one.

`commit` takes the message as an argument and never writes one. That is
deliberate and it is the whole of the "with or without AI" question on the
agent side: the model reading a diff and describing it *is* the AI path, and a
helper that generated the message would be a second, worse model of the same
change. The other half of that question — a person pressing Commit and
choosing — is in `routers/git.py`, where the draft goes through a human before
it becomes a commit.

The pull request section is the other end of that sentence. Committing stops
where the machine does, and "open a pull request" is the last step of the git
workflow — the one where the work becomes other people's problem — so the same
two rules run through it: no shell, and a backend chosen at runtime rather than
assumed. `gh` when it is installed, because it knows about the operator's
authentication and about hosts other than github.com; the REST API when it is
not and a token is in the environment. Nothing here will install either one, and
nothing here invents a pull request it could not reach.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from openmirror.net.transport import transport_for

# Named `_log` rather than `log`, because `log()` below is the git command
# and the two in one module is a shadowing bug waiting for the first time
# something inside that function wants to report a problem.
_log = logging.getLogger(__name__)

# A ceiling on any single git command. Not a parameter: every caller wants
# the same one, and an argument that silently does not bound the whole
# operation is worse than a constant that is easy to find.
COMMAND_TIMEOUT = 60

# Long enough for any real diff, short enough that a model is not handed a
# megabyte of context it will not read. The cap is applied to the *diff*, and
# the stat — which is what tells you what was touched — is kept whole.
DIFF_LIMIT = 40_000
LOG_LIMIT = 200

# `git status --porcelain=v2 --branch -z` is NUL-separated records. A path
# containing a quote or a space is emitted raw, which is the entire reason for
# the format: the v1 porcelain's quoting rules are a second grammar to parse.
#
# The two "ordinary" record types carry the path in a *different* field
# position, which is not documented anywhere obvious and is the kind of thing
# that is discovered by noticing a renamed file never appears in a status
# report:
#   1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
#   2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>   (rename/copy)
_ORDINARY = re.compile(r'^(1|2) ')


class GitError(RuntimeError):
    """Something git refused, or was not there to be asked."""


@dataclass(slots=True)
class Change:
    """One path's worth of working-tree state."""

    path: str
    index: str  # X — staged
    work: str  # Y — in the working tree
    rename_from: str = ''

    @property
    def untracked(self) -> bool:
        return self.index == '?' and self.work == '?'

    @property
    def staged(self) -> bool:
        # An untracked file is `?` in both columns, and `?` is neither `.` nor
        # a space — so without this it lands in *both* buckets and a status
        # report lists every new file twice, once as staged and once as
        # modified. It is in the index in no sense at all until git add has
        # heard of it.
        return not self.untracked and self.index not in ('.', ' ')

    @property
    def modified(self) -> bool:
        return not self.untracked and self.work not in ('.', ' ')

    @property
    def label(self) -> str:
        """A word for a person, which is what the model will relay.

        Ordered by how specific each case is, and the order is load-bearing:
        a staged rename has `R` in the index column and `.` in the working
        one, so it satisfies both "is staged" and "not modified" and was
        reported as `added` until the `R` check came first.

        Only `.` appears in porcelain v2 — there is no space for an unmodified
        column, which is worth knowing because checking for a space is the
        natural mistake when coming from v1.
        """
        if self.untracked:
            return 'untracked'
        if self.index == 'R':
            return 'renamed'
        if self.index == 'C':
            return 'copied'
        if self.index == 'D' or self.work == 'D':
            return 'deleted'
        if self.work == 'A':
            return 'new'
        if self.index == 'A':
            return 'added'
        if self.staged and self.modified:
            return 'staged and modified again'
        if self.staged:
            return 'staged'
        if self.modified:
            return 'modified'
        return 'unchanged'


@dataclass(slots=True)
class Status:
    """The working tree, parsed.

    `branch` and `upstream` are separate because "ahead 2, behind 1" is only
    meaningful against something, and a model that is told the branch name
    without it tends to report the wrong thing about a push.
    """

    branch: str = ''
    upstream: str = ''
    ahead: int = 0
    behind: int = 0
    detached: bool = False
    changes: list[Change] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.changes

    @property
    def staged(self) -> list[Change]:
        return [c for c in self.changes if c.staged]

    @property
    def unstaged(self) -> list[Change]:
        return [c for c in self.changes if c.modified]

    @property
    def untracked(self) -> list[Change]:
        return [c for c in self.changes if c.untracked]

    def describe(self) -> str:
        """What the model reads, and what a person reads in a tool card.

        Grouped by state rather than listed in git's own order, because the
        question being asked is almost never "what is the second entry" — it
        is "what is unstaged" or "is anything untracked", and answering that
        from a flat list is work the model does not reliably do.
        """
        head = f'On branch {self.branch}' if not self.detached else 'HEAD is detached'
        if self.upstream:
            head += f', tracking {self.upstream}'
            if self.ahead or self.behind:
                head += f' ({self.ahead} ahead, {self.behind} behind)'
        elif not self.detached:
            head += ', no upstream'

        if self.clean:
            return f'{head}\nNothing to commit, working tree clean.'

        buckets: list[tuple[str, list[Change]]] = [
            ('staged', [c for c in self.staged if not c.untracked]),
            ('modified, not staged', self.unstaged),
            ('untracked', self.untracked),
        ]
        lines = [head]
        for title, group in buckets:
            if not group:
                continue
            lines.append('')
            lines.append(f'{title} ({len(group)}):')
            # A hundred changed files is a signal in itself; listing all of
            # them spends the context that the next few lines explain.
            for change in group[:40]:
                rename = f' <- {change.rename_from}' if change.rename_from else ''
                lines.append(f'  {change.label}: {change.path}{rename}')
            if len(group) > 40:
                lines.append(f'  ... and {len(group) - 40} more')
        lines.extend(self._rename_hint())
        return '\n'.join(lines)

    def _rename_hint(self) -> list[str]:
        """Say so when a rename in the working tree looks like two changes.

        `git status` does not pair an untracked file with a deleted one — it
        has no baseline to compare content against until the change is in the
        index — so a file that was moved reads as one deletion and one new
        file. A model told only that will stage the deletion on its own, and
        the commit loses half the change; git will happily record the second
        half as a whole new file with no history attached to it.

        Stating the mechanism is the fix. Which deleted file pairs with which
        new one is *not* guessed here: a wrong guess is a commit that claims a
        rename that did not happen, and that is worse than a plain report.
        """
        deleted = [c for c in self.changes if c.label == 'deleted']
        if not deleted or not self.untracked:
            return []
        return [
            '',
            'Note: git does not pair a deleted file with a new one until the change is staged, so a '
            'file you moved appears here as a deletion and an untracked file. Stage both and it is '
            'recorded as a rename; stage one and only the deletion lands. Check whether any of these '
            'belong together before committing.',
        ]


def _identity() -> dict[str, str]:
    """Author and committer, falling back only when git has nothing.

    A commit that fails on "please tell me who you are" is a worse outcome
    than one attributed to a name the operator can correct in their own
    config, and these variables are set *after* the inherited environment, so
    an operator's own `git config` — which is what actually takes precedence
    inside git — is untouched by this.
    """
    return {
        'GIT_AUTHOR_NAME': os.environ.get('GIT_AUTHOR_NAME') or 'openmirror',
        'GIT_AUTHOR_EMAIL': os.environ.get('GIT_AUTHOR_EMAIL') or 'openmirror@localhost',
        'GIT_COMMITTER_NAME': os.environ.get('GIT_COMMITTER_NAME') or 'openmirror',
        'GIT_COMMITTER_EMAIL': os.environ.get('GIT_COMMITTER_EMAIL') or 'openmirror@localhost',
    }


def git_exe() -> str:
    """The git binary, or a complaint that names what to install.

    Resolved once per call rather than cached in a module global: a test that
    puts a fake `git` on PATH needs the lookup to happen after it does so, and
    an install that gains git mid-session should start working without a
    restart.
    """
    found = shutil.which('git')
    if not found:
        raise GitError('git is not on PATH, so there is no repository tooling in this session')
    return found


async def run(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run one git command. Returns (code, stdout, stderr), all decoded.

    `check=True` raises on a non-zero exit, because almost every caller wants
    git's own message — "fatal: not a git repository" is the single most
    useful thing this module can say, and replacing it with a Python
    `RuntimeError` would be strictly worse.
    """
    kwargs: dict[str, Any] = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True

    # Nothing here needs a terminal, and several commands (commit, and any
    # pager) behave differently when they think they have one. These are set
    # for the child only, never on the daemon's own environment.
    env = {
        **os.environ,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_PAGER': 'cat',
        'PAGER': 'cat',
        # Local identity for anything that commits without one configured.
        # A commit that fails on "please tell me who you are" is a worse
        # outcome than one attributed to a name the operator can correct in
        # their own config, and these are only used when the real ones are
        # absent — git's own config still wins.
        **_identity(),
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            git_exe(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            **kwargs,
        )
    except OSError as exc:
        raise GitError(f'could not run git: {exc}') from exc

    try:
        # `asyncio.timeout` rather than `wait_for`, so the cancellation is a
        # timeout error and not a cancellation of whatever this was running
        # inside — the difference between a slow `git log` and a slow turn
        # looking identical from the outside.
        async with asyncio.timeout(COMMAND_TIMEOUT):
            out, err = await proc.communicate()
    except TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise GitError(f'git {args[0]} timed out after {COMMAND_TIMEOUT}s') from exc

    code = proc.returncode or 0
    stdout = out.decode('utf-8', 'replace')
    stderr = err.decode('utf-8', 'replace')
    if check and code != 0:
        raise GitError((stderr or stdout or f'git {args[0]} exited {code}').strip())
    return code, stdout, stderr


def is_repo(cwd: Path) -> bool:
    """Whether this directory is inside a work tree.

    Deliberately synchronous and cheap: it runs from `assess`, which must not
    block the event loop, and `git rev-parse` on an existing repository is a
    single stat of `.git`. A directory that is not a repository costs one
    failed exec, which is the only way to be sure.
    """
    if not cwd.is_dir():
        return False
    if (cwd / '.git').exists():
        return True
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git_exe(), 'rev-parse', '--is-inside-work-tree'],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == b'true'


def parse_status(raw: bytes) -> Status:
    """Turn `--porcelain=v2 --branch -z` into a `Status`.

    The `-z` matters more than it looks. Without it git quotes any path with a
    space, a quote or a backslash in it, and a file called `it's mine.txt`
    arrives as `"it's mine.txt"` — a string that no later step can pass back to
    git unaltered. NUL-separated records mean the path is whatever bytes sit
    between two NULs.
    """
    status = Status()
    fields = raw.split(b'\x00')
    for position, record in enumerate(fields):
        if not record:
            continue
        line = record.decode('utf-8', 'replace')
        if line.startswith('# '):
            # The branch headers. `# branch.head <name>` and, on a detached
            # HEAD, the literal string `(detached)`.
            key, _, value = line[2:].partition(' ')
            if key == 'branch.head':
                status.detached = value == '(detached)'
                status.branch = '' if status.detached else value
            elif key == 'branch.upstream':
                status.upstream = value
            elif key == 'branch.ab':
                # `+3 -1` — ahead, then behind. Both can be 0, and either can
                # be absent when there is no upstream at all.
                match = re.match(r'^([+-]\d+)\s+([+-]\d+)$', value)
                if match:
                    status.ahead = int(match.group(1)[1:])
                    status.behind = int(match.group(2)[1:])
            continue
        if line.startswith('? '):
            status.changes.append(Change(path=line[2:], index='?', work='?'))
            continue
        if line.startswith('u '):
            # A merge conflict. Deliberately kept as a single unparsed line:
            # the XY codes here mean something different and rarer than the
            # ordinary ones, and a mislabelled conflict is worse than a plain
            # one. The model is told to look at the file.
            status.changes.append(Change(path=line.rpartition(' ')[2], index='U', work='U'))
            continue
        match = _ORDINARY.match(line)
        if not match:
            continue
        # Split to exactly the field count each type needs, so the path is
        # parts[8] for a type 1 and parts[9] for a type 2 rather than
        # "whichever field the split happened to stop at".
        kind = match.group(1)
        if kind == '2':
            parts = line.split(' ', 9)
            if len(parts) < 10:
                continue
            change = Change(path=parts[9], index=parts[1][0], work=parts[1][1])
            # The record after this one is the original path, and only for a
            # rename or a copy. Indexed by position rather than by value: two
            # identical records are a legal thing to have, and `.index()` would
            # hand back the first of them.
            nxt = fields[position + 1] if position + 1 < len(fields) else b''
            change.rename_from = nxt.decode('utf-8', 'replace') if nxt else ''
        else:
            parts = line.split(' ', 8)
            if len(parts) < 9:
                continue
            change = Change(path=parts[8], index=parts[1][0], work=parts[1][1])
        status.changes.append(change)
    return status


async def status(cwd: Path) -> Status:
    """The working tree, parsed."""
    _, out, _ = await run(
        ['status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal'],
        cwd,
    )
    return parse_status(out.encode('utf-8', 'replace'))


def _clip(text: str, limit: int = DIFF_LIMIT) -> tuple[str, bool]:
    """Cut a diff down, keeping both ends.

    Both ends and not the middle: a diff's header says what the change is and
    its tail says how it ended, and the middle of a large one is the part that
    matters least to a model deciding what to write about it.
    """
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return f'{text[:half]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-half:]}', True


async def diff(cwd: Path, *, staged: bool = False, path: str = '', stat_only: bool = False) -> tuple[str, bool]:
    """A diff, as text. Returns (text, was_clipped)."""
    args = ['--no-pager', 'diff', '--no-color']
    args += ['--stat'] if stat_only else ['--unified=3']
    if staged:
        args.append('--cached')
    if path:
        args += ['--', path]
    _, out, _ = await run(args, cwd)
    return _clip(out)


async def show(cwd: Path, ref: str = 'HEAD', *, path: str = '') -> str:
    """A commit, with its message and its diff."""
    if not ref or ref.startswith('-'):
        # A ref beginning with a dash is an option, and `git show --output=…`
        # is a file write. Refuse rather than pass it through.
        raise GitError(f'{ref!r} is not a commit reference')
    args = ['--no-pager', 'show', '--no-color', '--stat', '--patch', ref]
    if path:
        args += ['--', path]
    _, out, _ = await run(args, cwd)
    return out


async def log(
    cwd: Path,
    *,
    limit: int = 15,
    path: str = '',
    ref: str = 'HEAD',
    oneline: bool = True,
) -> str:
    """Recent history, newest first."""
    count = max(1, min(int(limit or 15), LOG_LIMIT))
    args = ['--no-pager', 'log', f'-{count}', '--no-color', '--pretty=format:%h %ad %an %s', '--date=short']
    if oneline:
        args.append('--no-merges')
    if path:
        args += ['--', path]
    else:
        args.append(ref)
    _, out, _ = await run(args, cwd)
    return out or '(no commits yet)'


async def stage(cwd: Path, paths: list[str]) -> str:
    """Put paths in the index. An empty list stages everything, as `git add -A` does."""
    args = ['add', '--all'] if not paths else ['add', '--', *paths]
    await run(args, cwd)
    return ', '.join(paths) if paths else 'everything'


async def unstage(cwd: Path, paths: list[str]) -> str:
    """Take paths back out of the index without touching the files.

    `restore --staged` and not `reset HEAD`, because the latter moves the
    branch pointer when given a commit and has a genuinely confusing failure
    mode. The one case it cannot express is an unborn branch — every
    repository is in that state between `git init` and its first commit, so it
    is the state a person is most likely to be in the first time they use this
    — where there is no HEAD to restore from and the index is the only copy of
    the file's contents. There the index entry has to be removed outright.
    """
    code, _, _ = await run(['rev-parse', '--verify', 'HEAD'], cwd, check=False)
    unborn = code != 0
    if unborn:
        if paths:
            await run(['rm', '--cached', '-q', '--', *paths], cwd)
        else:
            await run(['rm', '--cached', '-r', '-q', '--', '.'], cwd)
    elif paths:
        await run(['restore', '--staged', '--', *paths], cwd)
    else:
        await run(['restore', '--staged', '--', '.'], cwd)
    return ', '.join(paths) if paths else 'everything'


async def commit(cwd: Path, message: str) -> dict[str, Any]:
    """Make a commit out of what is already staged.

    The message is passed with `-F -` on stdin rather than as an argument
    because that is the only way a subject containing a newline, a quote or a
    leading dash survives intact, and because a message is not something that
    belongs on a command line where it shows up in `ps`.

    Nothing is staged here. Committing somebody's working tree because they
    forgot to say what to include is the kind of helpful that loses an hour of
    work; a refusal that says what is unstaged costs one turn.
    """
    if not message.strip():
        raise GitError('a commit needs a message')
    proc = await asyncio.create_subprocess_exec(
        git_exe(), 'commit', '-F', '-', '--cleanup=strip',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env={
            **os.environ,
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_PAGER': 'cat',
            **_identity(),
        },
    )
    out, err = await asyncio.wait_for(proc.communicate(message.encode('utf-8')), timeout=60)
    stdout, stderr = out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
    if proc.returncode:
        # Almost always "nothing added to commit". Said in git's words, which
        # name the fix.
        raise GitError((stderr or stdout or 'the commit was refused').strip())

    _, sha, _ = await run(['rev-parse', '--short', 'HEAD'], cwd)
    short = sha.strip()
    subject = message.strip().splitlines()[0]
    files = 0
    try:
        _, stat, _ = await run(['--no-pager', 'show', '--stat', '--oneline', short], cwd)
        files = sum(1 for line in stat.splitlines()[1:] if '|' in line)
    except GitError:
        pass
    return {
        'sha': short,
        'subject': subject,
        'files': files,
        'summary': (stdout or stderr).strip(),
    }


async def branches(cwd: Path) -> str:
    _, out, _ = await run(['--no-pager', 'branch', '--no-color', '-vv'], cwd)
    return out


async def current_branch(cwd: Path) -> str:
    _, out, _ = await run(['rev-parse', '--abbrev-ref', 'HEAD'], cwd)
    return out.strip()


def subject_of(message: str) -> str:
    """The first line, trimmed — what a person reads in `git log --oneline`.

    Capped, because a subject longer than this is a message that has been
    written in the wrong place, and an approval prompt that renders a
    paragraph is a prompt nobody reads.
    """
    line = (message or '').strip().splitlines()[0] if message.strip() else ''
    if len(line) > 72:
        return line[:69] + '...'
    return line


# ---------------------------------------------------------------------------
# Pull requests
# ---------------------------------------------------------------------------
#
# Two backends, chosen per call, and the tradeoff is worth writing down because
# it is not "prefer the fast one".
#
# **`gh` first.** It is the tool a person with a GitHub account has already
# installed, and it carries the authentication that account has: an OAuth token
# in a keychain, a hosts.yml entry, an enterprise hostname this module does not
# know how to guess. Every attempt to be a `gh` is a second implementation of
# something it does better, and the failure mode of guessing the enterprise
# API root is a token sent to the wrong host.
#
# **The REST API second**, and only from a token in the environment. This is
# the path for the installs where the CLI is not there — a container, a server
# with a token in its environment and no shell conveniences. It is deliberately
# narrow: github.com only, because an API root that is a guess is a credential
# sent somewhere unvetted.
#
# So the API path is the fallback and `gh` is the truth, which is the reverse of
# what "no new dependencies" usually implies. When neither is available nothing
# is installed, nothing is prompted for and no pull request is invented; the
# caller is told the two things that would make it work.
#
# Neither backend is given a command line. `gh` is run with an argument list,
# like git, so a title containing `; rm -rf ~` is a string and not the start of
# anything — the body goes on stdin rather than into `--body`, for the same
# reason `commit` uses `-F -`.

GITHUB_API = 'https://api.github.com'

# `gh` is a Node binary that reads a config file before it does anything, and
# the first run of it on a cold machine is not instant. The API gets a much
# shorter leash: it is one request, and a turn that hangs on a socket is a turn
# the person is watching not finish.
GH_TIMEOUT = 60
API_TIMEOUT = 30

# GitHub's own limits, so the cap is a real one rather than a preference: a
# longer title is truncated by the server with no warning, and a body over
# 65536 characters is refused outright.
PR_TITLE_LIMIT = 256
PR_BODY_LIMIT = 60_000

# Enough commits to describe a branch, few enough that a branch with a hundred
# commits does not produce a thousand lines of its own subjects.
PR_COMMITS_IN_FILL = 50

NO_BACKEND = (
    'there is no way to reach GitHub from this session: the GitHub CLI is not on PATH and neither '
    'GITHUB_TOKEN nor GH_TOKEN is set. Install gh (https://cli.github.com) and run `gh auth login`, '
    'or set GITHUB_TOKEN to a token with the `repo` scope. Nothing was sent.'
)


class PRUnavailable(GitError):
    """No backend, so there is no pull request to open or to read.

    Separate from `GitError` because the answer to it is not a retry: it is a
    person installing something or exporting a variable. The tool turns it into
    a sentence saying which, rather than letting it read as a failure of the
    call the model made.
    """


@dataclass(frozen=True, slots=True)
class Remote:
    """A repository, read out of a remote URL."""

    host: str
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f'{self.owner}/{self.name}'

    @property
    def is_github(self) -> bool:
        # github.com only, on purpose. An enterprise host has an API root at a
        # path this module would have to guess, and a credential sent to a
        # guessed root is a credential sent somewhere unvetted — which is the
        # one mistake in this file that cannot be undone. `parse_remote` drops
        # a `www.`; this accepts it anyway for a `Remote` built by hand.
        return self.host in ('github.com', 'www.github.com')


@dataclass(slots=True)
class PullRequest:
    """One pull request, from either backend, in one shape.

    A union of gh's `--json` output and the API's is deliberate: what the model
    reads and what a person sees in a tool card should not depend on which
    binary happened to be installed.
    """

    number: int = 0
    title: str = ''
    state: str = ''
    url: str = ''
    draft: bool = False
    base: str = ''
    head: str = ''
    author: str = ''
    updated: str = ''
    checks: str = ''
    backend: str = ''

    def line(self) -> str:
        """One pull request on one line — what a list is made of."""
        state = (self.state or 'open').lower()
        return f'#{self.number} {self.title} ({state}{", draft" if self.draft else ""}) {self.url}'.rstrip()

    def describe(self) -> str:
        state = (self.state or 'open').lower()
        route = f'{self.head} -> {self.base}'.strip(' ->')
        if self.author:
            route = f'{route}, opened by {self.author}' if route else f'opened by {self.author}'
        if self.updated:
            route = f'{route}, updated {self.updated}' if route else f'updated {self.updated}'
        lines = [f'#{self.number} {self.title} ({state}{", draft" if self.draft else ""})']
        if route:
            lines.append(f'  {route}')
        if self.checks:
            lines.append(f'  checks: {self.checks}')
        if self.url:
            lines.append(f'  {self.url}')
        return '\n'.join(lines)


# A path segment safe to interpolate into an API URL. Both halves come out of a
# `.git/config`, which is a file a clone can carry: `../../` or a `?` in there
# would change which host the token is sent to, and the token is the thing
# worth not losing.
_SAFE_SEGMENT = re.compile(r'^[A-Za-z0-9._-]+$')


def parse_remote(url: str) -> Remote | None:
    """Owner and repository out of a `git remote get-url` line, or None.

    Four shapes, and the second is the one that breaks naive parsers:

        git@github.com:acme/demo.git            scp syntax — not a URL at all
        ssh://git@github.com/acme/demo.git      the same thing, spelled properly
        https://github.com/acme/demo(.git)/     with or without the suffix
        git://github.com/acme/demo.git           the ancient form, still in configs

    Credentials in the URL (`https://user:token@…`) are dropped along with the
    scheme, because they are in the URL and must not end up in an error message
    or a log line. A local path, a `file://` path, and anything that is not
    exactly `owner/repo` return None rather than a guess.
    """
    raw = (url or '').strip()
    if not raw:
        return None
    if '://' in raw:
        parsed = urlparse(raw)
        host, path = parsed.hostname or '', parsed.path
    elif ':' in raw:
        # scp syntax. `urlparse` sees no scheme and hands back the whole string,
        # so the host is everything before the colon and the path everything
        # after. A Windows path (`C:\src\demo`) takes this branch too, and
        # falls out below on the segment count.
        head, _, path = raw.partition(':')
        host = head.rpartition('@')[2]
    else:
        return None
    if not host:
        # `file:///srv/git/demo.git` has a path and no host, which would
        # otherwise parse as a repository called `demo` on a machine called
        # nothing at all.
        return None
    parts = [part for part in path.strip('/').split('/') if part]
    if len(parts) != 2:
        return None
    owner, name = parts
    if name.endswith('.git'):
        name = name[: -len('.git')]
    if not (_SAFE_SEGMENT.match(owner) and _SAFE_SEGMENT.match(name)):
        return None
    return Remote(host=host.lower().removeprefix('www.'), owner=owner, name=name)


def github_token() -> str:
    """A GitHub token from the environment, or ''.

    `GH_TOKEN` is here as well as `GITHUB_TOKEN` and not because they are two
    names for the same thing: `gh auth login` exports the first, so somebody who
    has set the CLI up is very likely to have it in their environment and no
    reason to also export the second. The value is never logged, never put in an
    exception and never returned to a caller that would show it.
    """
    for name in ('GITHUB_TOKEN', 'GH_TOKEN'):
        token = os.environ.get(name, '').strip()
        if token:
            return token
    return ''


def gh_exe() -> str | None:
    """The `gh` binary, or None. No exception: absence is a decision, not a fault."""
    return shutil.which('gh')


def pr_backend(remote: Remote) -> str:
    """`'gh'`, `'api'`, or `''` when there is no way through.

    Never raises and never prompts. A tool that stops to ask a person to log in
    to a website mid-turn is a tool that hangs, and the answer belongs in the
    message the model reads and relays.
    """
    if gh_exe():
        return 'gh'
    return 'api' if remote.is_github and github_token() else ''


async def remote(cwd: Path) -> tuple[str, Remote]:
    """The remote to work against, and what it points at.

    `origin` first and then whatever else there is, so a repository with an
    `upstream` and an `origin` fork is opened against the fork — which is the
    remote the branch was pushed to and the one a reviewer is looking at.
    """
    _, listed, _ = await run(['remote'], cwd, check=False)
    names = [line.strip() for line in listed.splitlines() if line.strip()]
    if not names:
        raise GitError(
            'there is no remote configured, so there is nowhere to open a pull request. '
            'Add one and push the branch to it first.'
        )
    ordered = (['origin'] if 'origin' in names else []) + [n for n in names if n != 'origin']
    for name in ordered:
        _, url, _ = await run(['remote', 'get-url', name], cwd, check=False)
        found = parse_remote(url)
        if found:
            return name, found
    _, url, _ = await run(['remote', 'get-url', ordered[0]], cwd, check=False)
    where = url.strip() or 'nothing'
    raise GitError(
        f'{ordered[0]} points at {where}, which is not a GitHub repository. A pull request can only '
        'be opened against one; for a self-hosted remote, install gh and log in to that host with it.'
    )


async def default_branch(cwd: Path, remote_name: str = 'origin') -> str:
    """The branch this repository merges into, or '' when it is not recorded.

    Read from `refs/remotes/<remote>/HEAD` rather than guessed: `main` has been
    the answer often enough to be muscle memory and `master` is still the answer
    in a great many repositories, and a pull request aimed at the wrong base is
    a pull request against itself.
    """
    code, out, _ = await run(
        ['symbolic-ref', '--quiet', '--short', f'refs/remotes/{remote_name}/HEAD'], cwd, check=False
    )
    # `removeprefix` and not a split: a default branch may itself contain a
    # slash, and taking everything after the last one would name a different
    # branch than the one git just printed.
    return out.strip().removeprefix(f'{remote_name}/') if code == 0 else ''


async def commits_since(cwd: Path, base: str, remote_name: str = 'origin') -> list[str]:
    """Subjects of the commits on this branch that `base` does not have.

    Both spellings of the base are tried because a base is usually named the
    way a person says it (`main`) and only exists locally as `origin/main`. An
    empty list means "could not tell", and the caller says so rather than
    describing a diff nobody can verify.
    """
    merge = ''
    for candidate in (base, f'{remote_name}/{base}'):
        code, out, _ = await run(['merge-base', 'HEAD', candidate], cwd, check=False)
        if code == 0 and out.strip():
            merge = out.strip()
            break
    if not merge:
        return []
    _, listed, _ = await run(
        ['--no-pager', 'log', f'-{PR_COMMITS_IN_FILL}', '--no-merges', '--format=%s', f'{merge}..HEAD'],
        cwd,
    )
    return [line.strip() for line in listed.splitlines() if line.strip()]


def _body_from_log(commits: list[str]) -> str:
    """A pull request body made of the commits it contains.

    A heading for more than one and none for exactly one, because a single
    commit's subject *is* the description and a list of one under a heading
    called "commits" reads like padding.
    """
    if len(commits) == 1:
        return commits[0]
    return '## Commits\n\n' + '\n'.join(f'- {subject}' for subject in commits)


def _clean_title(value: str) -> str:
    """One line, trimmed, capped at what GitHub accepts.

    Newlines folded to spaces rather than kept: a title with a blank line in it
    is a body, and GitHub renders the second line somewhere nobody is looking.
    """
    title = ' '.join((value or '').split())
    if len(title) > PR_TITLE_LIMIT:
        title = title[: PR_TITLE_LIMIT - 3] + '...'
    return title


async def _gh(args: list[str], cwd: Path, *, stdin: str | None = None) -> tuple[int, str, str]:
    """Run the GitHub CLI: an argument list, never a string. See the module docstring.

    `GH_PROMPT_DISABLED` is the load-bearing part of the environment. Without
    it `gh` will try to finish an authentication flow — including opening a
    browser — from inside a tool call, on somebody's machine, with nothing on
    screen to explain why a window appeared.
    """
    exe = gh_exe()
    if not exe:
        raise PRUnavailable(NO_BACKEND)
    kwargs: dict[str, Any] = {}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    env = {
        **os.environ,
        'GH_PROMPT_DISABLED': '1',
        'GH_PAGER': 'cat',
        'GIT_TERMINAL_PROMPT': '0',
        'NO_COLOR': '1',
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            **kwargs,
        )
    except OSError as exc:
        raise GitError(f'could not run gh: {exc}') from exc
    try:
        async with asyncio.timeout(GH_TIMEOUT):
            out, err = await proc.communicate(stdin.encode('utf-8') if stdin is not None else None)
    except TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise GitError(f'gh {args[0]} timed out after {GH_TIMEOUT}s') from exc
    return proc.returncode or 0, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


async def _api(
    method: str,
    path: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """One GitHub API call. Returns `(status, parsed)`; 404 comes back as itself.

    The transport is a fresh one rather than a provider's, and Tor is off. The
    toggle in this install is about where model traffic goes, and inheriting it
    here would mean a setting chosen for a GPU connection silently deciding
    where a pull request is opened from. `openmirror/agent/tools/web.py` argues
    the same point for the open web; the difference is that this reaches
    api.github.com rather than whatever it was told to read.
    """
    headers = {
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {token}',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'openmirror',
    }
    try:
        async with transport_for(timeout=API_TIMEOUT).session() as client:
            async with client.request(
                method, f'{GITHUB_API}{path}', headers=headers, json=payload, params=params
            ) as response:
                text = await response.text()
                if response.status == 404:
                    return 404, None
                if response.status == 401:
                    # The one case where saying "unauthorised" without more is
                    # useless, because the usual cause is a token with no repo
                    # scope, and the fix is a different token rather than a
                    # retry. The token itself is never in the message.
                    raise GitError('GitHub rejected the token (401). A fine-grained token needs the `repo` scope.')
                if response.status >= 400:
                    raise GitError(f'GitHub said {response.status}: {_api_error(text)}')
                try:
                    return response.status, json.loads(text) if text else None
                except ValueError as exc:
                    raise GitError(f'GitHub sent back something that is not JSON ({exc})') from exc
    except (TimeoutError, OSError, aiohttp.ClientError) as exc:
        raise GitError(f'could not reach GitHub ({exc})') from exc


def _api_error(text: str) -> str:
    """GitHub's own message, which names the actual problem, or the raw body."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return (text or 'no detail').strip()[:400]
    if isinstance(parsed, dict) and parsed.get('message'):
        detail = str(parsed['message'])
        errors = parsed.get('errors')
        if isinstance(errors, list) and errors:
            fields = sorted({str(e.get('field') or e.get('code')) for e in errors if isinstance(e, dict)})
            if fields:
                detail += f' ({", ".join(f for f in fields if f)})'
        return detail[:400]
    return (text or 'no detail').strip()[:400]


def _checks(counts: dict[str, int]) -> str:
    """`2 failed, 5 passed`, worst first, with the zeroes dropped."""
    order = ('failed', 'pending', 'skipped', 'passed')
    bits = [f'{counts[key]} {key}' for key in order if counts.get(key)]
    return ', '.join(bits)


def _tally(conclusions: list[str]) -> str:
    counts: dict[str, int] = {}
    for conclusion in conclusions:
        counts[conclusion] = counts.get(conclusion, 0) + 1
    return _checks(counts)


_PENDING = ('', 'queued', 'in_progress', 'pending', 'expected', 'requested', 'waiting')
_SKIPPED = ('skipped', 'neutral')


def _conclusion(raw: Any) -> str:
    """One of `passed` / `failed` / `pending` / `skipped`, from any of the
    several vocabularies GitHub uses for the same question.

    An unrecognised value counts as *pending* rather than as a pass, because
    the cost of a check summary that says "all green" when one was skipped is a
    merge nobody tested. An empty one is pending for the same reason: a check
    that has not reported yet has not passed.
    """
    state = str(raw or '').strip().lower()
    if state in _PENDING:
        return 'pending'
    if state == 'success':
        return 'passed'
    return 'skipped' if state in _SKIPPED else 'failed'


def _checks_from_gh(rollup: Any) -> str:
    """gh reports a mixed list of check runs and commit statuses in one field,
    which is why this looks at three keys: a check run has a `conclusion`, a
    commit status has a `state`, and either may still be running."""
    if not isinstance(rollup, list) or not rollup:
        return ''
    verdicts = [
        _conclusion(entry.get('conclusion') or entry.get('state') or entry.get('status'))
        for entry in rollup
        if isinstance(entry, dict)
    ]
    return _tally(verdicts)


def _checks_from_api(check_runs: Any) -> str:
    """The same tally from `/check-runs`, where the outcome is a `conclusion`."""
    if not isinstance(check_runs, list) or not check_runs:
        return ''
    verdicts = [
        _conclusion(entry.get('conclusion') or entry.get('status'))
        for entry in check_runs
        if isinstance(entry, dict)
    ]
    return _tally(verdicts)


def _pr_from_gh(raw: Any) -> PullRequest:
    data = raw if isinstance(raw, dict) else {}
    author = data.get('author')
    return PullRequest(
        number=int(data.get('number') or 0),
        title=str(data.get('title') or ''),
        state=str(data.get('state') or ''),
        url=str(data.get('url') or ''),
        draft=bool(data.get('isDraft')),
        base=str(data.get('baseRefName') or ''),
        head=str(data.get('headRefName') or ''),
        author=str(author.get('login') or '') if isinstance(author, dict) else str(author or ''),
        updated=str(data.get('updatedAt') or '')[:10],
        checks=_checks_from_gh(data.get('statusCheckRollup')),
        backend='gh',
    )


def _pr_from_api(raw: Any) -> PullRequest:
    data = raw if isinstance(raw, dict) else {}
    base, head = data.get('base'), data.get('head')
    user = data.get('user')
    return PullRequest(
        number=int(data.get('number') or 0),
        title=str(data.get('title') or ''),
        state=str(data.get('state') or ''),
        url=str(data.get('html_url') or ''),
        draft=bool(data.get('draft')),
        base=str(base.get('ref') or '') if isinstance(base, dict) else '',
        head=str(head.get('ref') or '') if isinstance(head, dict) else '',
        author=str(user.get('login') or '') if isinstance(user, dict) else '',
        updated=str(data.get('updated_at') or '')[:10],
        backend='api',
    )


_VIEW_FIELDS = 'number,title,state,url,isDraft,baseRefName,headRefName,author,updatedAt'
# Checks come free in the same call, on every version of gh that has them. An
# older one refuses the whole command over an unknown field rather than ignoring
# it, so the retry below is a real path and not a precaution.
_VIEW_FIELDS_WITH_CHECKS = f'{_VIEW_FIELDS},statusCheckRollup'
# gh's two ways of saying there is nothing here, and both are needed: whether
# it looked for an open one or for any at all changes which sentence it uses.
_NO_PR = ('no pull request', 'no open pull request')


def _is_absent(*outputs: str) -> bool:
    """Whether gh is saying "there isn't one" rather than "that failed".

    The distinction is the whole value of a view: told a pull request exists when
    it does not, a model goes on to tell a person their work is under review.
    """
    text = ' '.join(outputs).lower()
    return any(marker in text for marker in _NO_PR)


async def pr_view(cwd: Path, number: int = 0) -> PullRequest | None:
    """The pull request for this branch, or one by number. None when there is none."""
    _, repo = await remote(cwd)
    backend = pr_backend(repo)
    if not backend:
        raise PRUnavailable(NO_BACKEND)
    if backend == 'gh':
        args = ['pr', 'view', *([str(number)] if number > 0 else []), '--repo', repo.slug]
        code, out, err = await _gh([*args, '--json', _VIEW_FIELDS_WITH_CHECKS], cwd)
        if code != 0 and 'statuscheckrollup' in f'{out}{err}'.lower():
            code, out, err = await _gh([*args, '--json', _VIEW_FIELDS], cwd)
        if code != 0:
            if _is_absent(out, err):
                return None
            raise GitError((err or out or 'gh pr view failed').strip())
        try:
            return _pr_from_gh(json.loads(out))
        except ValueError as exc:
            raise GitError(f'gh returned something that is not JSON ({exc})') from exc

    token = github_token()
    if number > 0:
        _, raw = await _api('GET', f'/repos/{repo.slug}/pulls/{number}', token)
        if not raw:
            return None
        pull = _pr_from_api(raw)
        pull.checks = await _api_checks(token, repo, raw)
        return pull
    branch = await current_branch(cwd)
    _, found = await _api(
        'GET',
        f'/repos/{repo.slug}/pulls',
        token,
        params={'state': 'all', 'head': f'{repo.owner}:{branch}', 'per_page': '1'},
    )
    first = found[0] if isinstance(found, list) and found else None
    if not first:
        return None
    pull = _pr_from_api(first)
    pull.checks = await _api_checks(token, repo, first)
    return pull


async def _api_checks(token: str, repo: Remote, raw: Any) -> str:
    """One extra request for the checks, and silence if it does not work.

    Check runs are where CI is; a repository using only commit statuses is
    reported as having none, which is a thing the reader can see and correct,
    rather than a second request that fails on every view.
    """
    head = raw.get('head') if isinstance(raw, dict) else None
    sha = str(head.get('sha') or '') if isinstance(head, dict) else ''
    if not sha:
        return ''
    try:
        _, found = await _api('GET', f'/repos/{repo.slug}/commits/{sha}/check-runs', token)
    except GitError as exc:
        _log.debug('no check runs for %s: %s', repo.slug, exc)
        return ''
    return _checks_from_api(found.get('check_runs') if isinstance(found, dict) else None)


async def pr_list(cwd: Path, limit: int = 10) -> list[PullRequest]:
    """Open pull requests, newest activity first. Empty when there are none."""
    _, repo = await remote(cwd)
    backend = pr_backend(repo)
    if not backend:
        raise PRUnavailable(NO_BACKEND)
    count = max(1, min(int(limit or 10), 100))
    if backend == 'gh':
        code, out, err = await _gh(
            ['pr', 'list', '--repo', repo.slug, '--state', 'open', '--limit', str(count), '--json', _VIEW_FIELDS],
            cwd,
        )
        if code != 0:
            raise GitError((err or out or 'gh pr list failed').strip())
        try:
            raw = json.loads(out or '[]')
        except ValueError as exc:
            raise GitError(f'gh returned something that is not JSON ({exc})') from exc
        return [_pr_from_gh(entry) for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
    _, found = await _api(
        'GET', f'/repos/{repo.slug}/pulls', github_token(), params={'state': 'open', 'per_page': str(count)}
    )
    return [_pr_from_api(entry) for entry in found if isinstance(entry, dict)] if isinstance(found, list) else []


async def pr_create(
    cwd: Path,
    *,
    title: str,
    body: str = '',
    base: str = '',
    draft: bool = False,
    fill: bool = False,
) -> PullRequest:
    """Open a pull request from the current branch, and say what it made.

    Four things happen in a fixed order, and each of the first three is a thing
    that is much better to refuse than to discover halfway through:

    1. **Is there any way to reach GitHub at all.** Checked before anything
       else, so the answer is "install gh" rather than a half-finished push to a
       remote that was never going to be reviewed.
    2. **Is the working tree committed.** Uncommitted work is not in the pull
       request and is not in the branch either, so opening one now produces a
       PR that is quietly missing whatever was in the editor. Nothing is
       committed here to fix that up: an agent that stages and commits a
       stranger's working tree because it was asked to open a pull request is
       the worst thing this tool could do, and the refusal costs one turn.
    3. **Push, if the branch has nowhere to be pushed to.** `push` is not run as
       a separate action first, because the person approving this said "open a
       pull request" and a PR cannot exist without the branch being somewhere
       reviewable. It is a push of a branch that is already committed, to the
       remote the branch is already tracking.
    4. **Open it**, through whichever backend is there.
    """
    remote_name, repo = await remote(cwd)
    backend = pr_backend(repo)
    if not backend:
        raise PRUnavailable(NO_BACKEND)

    state = await status(cwd)
    if state.detached or not state.branch:
        raise GitError(
            'HEAD is detached, so there is no branch for a pull request. Check one out and try again.'
        )
    if not state.clean:
        paths = ', '.join(change.path for change in state.changes[:20])
        more = f', and {len(state.changes) - 20} more' if len(state.changes) > 20 else ''
        raise GitError(
            f'there are uncommitted changes, which would not be part of the pull request: {paths}{more}. '
            'Commit them (git action "commit") or stash them, then open it — nothing was staged, pushed or sent.'
        )

    target = (base or '').strip() or await default_branch(cwd, remote_name)
    if target and target == state.branch:
        raise GitError(
            f'{state.branch} is the branch it would merge into, so a pull request would compare it with '
            'itself. Create a branch, commit there, and open the pull request from that.'
        )

    if not (body or '').strip() and fill:
        # A body written from the commits is nearly always the right one, and
        # an empty box is nearly always the reason a pull request sits open
        # with a comment asking what it does. What it must never be is a
        # description of commits it cannot see, so the log is read from the
        # merge base rather than from HEAD.
        if not target:
            raise GitError(
                'this repository does not record which branch it merges into, so there is nothing to write '
                'a body from. Say what the body should say, or name the base with "base".'
            )
        subjects = await commits_since(cwd, target, remote_name)
        if not subjects:
            raise GitError(
                f'nothing on {state.branch} that is not already on {target}, so there is no pull request to '
                'open and no body to write. Push the commits first.'
            )
        body = _body_from_log(subjects)
    body = body.strip()[:PR_BODY_LIMIT]
    subject = _clean_title(title)
    if not subject:
        raise GitError('a pull request needs a title — write the one line that says what it changes')

    if not state.upstream:
        await run(['push', '--set-upstream', remote_name, state.branch], cwd)

    if backend == 'gh':
        # Every value the model supplied is its own argv item, and the body is
        # on stdin rather than in an argument. A title of `"; rm -rf ~"` is a
        # title; it cannot become a second command, because there is no second
        # command — see the module docstring.
        args = ['pr', 'create', '--repo', repo.slug, '--head', state.branch, '--title', subject]
        if target:
            args += ['--base', target]
        if draft:
            args.append('--draft')
        # `--body-file -` even when the body is empty: without a body gh opens
        # an editor, which is a process with a terminal attached to a tool call.
        code, out, err = await _gh([*args, '--body-file', '-'], cwd, stdin=body)
        if code != 0:
            raise GitError((err or out or 'gh pr create refused').strip())
        url = (out or '').strip().splitlines()[-1].strip() if out.strip() else ''
        # gh prints the URL of what it opened, and the number is the last thing
        # in it. Anything else on stdout is not a URL and is not a number.
        tail = url.rstrip('/').rsplit('/', 1)[-1]
        number = int(tail) if tail.isdigit() else 0
        if not number:
            return PullRequest(title=subject, url=url, draft=draft, base=target, head=state.branch, backend='gh')
        # One more call for the state and the checks, so what comes back is the
        # same shape as a view and the model does not have to reason about a
        # bare URL.
        opened = await pr_view(cwd, number)
        if opened is None:
            return PullRequest(number=number, title=subject, url=url, base=target, head=state.branch, backend='gh')
        opened.draft = draft or opened.draft
        return opened

    token = github_token()
    if not target:
        # No `refs/remotes/<remote>/HEAD` locally and no `base` given. One
        # request for it, rather than a guess: the pull request API requires the
        # base and the wrong one is a pull request against a branch nobody is
        # reviewing.
        _, meta = await _api('GET', f'/repos/{repo.slug}', token)
        target = str(meta.get('default_branch') or '') if isinstance(meta, dict) else ''
        if not target:
            raise GitError(
                f'could not work out which branch {repo.slug} merges into. Pass "base" and say which.'
            )
    _, raw = await _api(
        'POST',
        f'/repos/{repo.slug}/pulls',
        token,
        payload={'title': subject, 'head': state.branch, 'base': target, 'body': body, 'draft': bool(draft)},
    )
    if not isinstance(raw, dict):
        raise GitError('GitHub accepted the request but sent nothing back, so there is no link to report')
    return _pr_from_api(raw)

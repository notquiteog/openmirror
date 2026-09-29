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
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

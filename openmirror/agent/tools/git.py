"""Version control as a tool, so the model does not have to be a git client.

Reading a repository through `shell` works and is worse in three specific
ways, all of which show up in practice rather than in theory:

* **Porcelain is not prose.** `git status --short` gives ` M src/app.js` and
  `?? notes.md`, and a model has to know that the space-then-M means *not
  staged* to answer "have I staged anything". Small models get this wrong in a
  way that looks like a git bug and is a format bug.
* **Nothing is confined.** `shell` reaches the whole filesystem by design — the
  session root is a policy, not a jail — and a repository is a thing you
  operate on from a directory. Here the working root is passed to every
  command, so `cd /somewhere/else && git commit` is not a thing that can
  happen.
* **A commit is worth its own grade.** Through the shell, `git commit` is
  `execute` because the classifier only knows the read-only subcommand list.
  Here it is `write`, which means it is asked about in `ask` mode — the mode
  people actually run in — instead of being lumped in with `npm run build`.

What is deliberately missing: `reset --hard`, `clean -f`, `rebase`, and
`filter-branch`. Each is one `shell` call away, each is already graded
`destructive` there, and a first-class tool is the wrong place to keep the
buttons that lose work. `push` and the three pull-request actions are the ones
that reach outside the machine, and they are graded above everything local for
the same reason the rest of this file is careful: a commit nobody asked for is
a local mistake that `git reset` undoes, and a pull request is a message to
other people that nobody can take back.
"""

from __future__ import annotations

from typing import Any

from openmirror.agent import git as core
from openmirror.agent.tools.base import Assessment, Output, Tool, ToolContext, ToolError, resolve_in_root
from openmirror.protocol.agent import Risk

ACTIONS = (
    'status', 'diff', 'log', 'show', 'stage', 'unstage', 'commit', 'branch', 'checkout', 'push',
    'pr_create', 'pr_view', 'pr_list',
)

# Which grade each action earns. Read is observation; the four that change the
# index or the branch are `write`, because every one of them is undoable and
# none of them is visible to anyone else.
#
# `push` is `network`: it reaches another machine, and it asks in every mode
# short of `trusted` — including `auto_edit`, where a local commit runs without
# a question, because a commit is a change to the operator's own work and a
# push is a change to somebody else's.
#
# `pr_create` is `message`, and not `network` like the push above it. The
# difference is not the socket, it is the audience: opening a pull request tells
# reviewers — and, on a public repository, the world — that this person is
# asking for their code to be read, under their name, and it cannot be recalled
# once sent. That is the same category as sending mail as them, and the
# codebase already says what that category is for: "'let it do what it likes to
# my computer' has never meant 'correspond on my behalf'". So it is asked in
# *every* mode, including `unrestricted`, and refused outright in `read_only`
# and `plan` rather than offered as a yes/no.
#
# It also means `allow_messages=false` switches pull requests off along with
# mail. That is the right way round: the flag means "must not speak to anyone on
# my behalf", and an agent that can still file a public request under somebody's
# name has not been given that.
#
# `pr_view` and `pr_list` are `read`. They open a socket, which is the part that
# normally makes a call worth asking about, but nothing leaves the machine that
# was not already there: they are a GET of data this session could read by hand.
RISK: dict[str, Risk] = {
    'status': Risk.READ,
    'diff': Risk.READ,
    'log': Risk.READ,
    'show': Risk.READ,
    'branch': Risk.READ,
    'stage': Risk.WRITE,
    'unstage': Risk.WRITE,
    'commit': Risk.WRITE,
    'checkout': Risk.WRITE,
    'push': Risk.NETWORK,
    'pr_create': Risk.MESSAGE,
    'pr_view': Risk.READ,
    'pr_list': Risk.READ,
}


class GitTool(Tool):
    name = 'git'
    description = (
        'Work with the git repository in the working root. '
        'action "status" says what is changed, grouped by whether it is staged — read this '
        'before committing rather than guessing. '
        'action "diff" shows what changed, with "staged" for what is already in the index. '
        'action "log" shows recent commits. '
        'action "stage" puts files in the index, and with no paths stages everything; '
        '"unstage" takes them back out. '
        'action "commit" commits what is already staged, and the message is yours to write: '
        'read the diff first, then write one line saying what changed and why, in the '
        'imperative and under 72 characters. If the person gave you words for the message, use '
        'those instead. It does not stage anything itself. '
        'action "push" sends the current branch to its remote, and it needs approval in most modes. '
        'The order that finishes the job is stage, commit, push, pr_create — do not skip the '
        'commit, because a pull request is made of commits and an uncommitted change is in '
        'neither. '
        'action "pr_create" opens a pull request from the current branch; give it a title, and a '
        'body of a few sentences saying what the change does and why. Set "fill" and leave the '
        'body out and it will write one from the commits since the merge base, which is usually '
        'the honest first draft. It refuses when the working tree is dirty rather than committing '
        'for you, and it pushes the branch first if it has never been pushed. It needs a person\'s '
        'approval in every mode, and it needs either the `gh` command line tool or a GITHUB_TOKEN '
        'in the environment — if neither is there it will tell you so. '
        'action "pr_view" shows this branch\'s pull request, or one you give a "number". '
        'action "pr_list" shows the open ones. Both say plainly when they cannot be reached rather '
        'than guessing. '
        'There is no "reset" or "clean" here on purpose — say so rather than reaching for the '
        'shell to discard work.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': list(ACTIONS),
                'description': 'What to do.',
            },
            'path': {
                'type': 'string',
                'description': 'One path, for diff/log/show. Relative to the working root.',
            },
            'staged': {
                'type': 'boolean',
                'description': 'For diff: show what is in the index rather than the working tree.',
            },
            'stat_only': {
                'type': 'boolean',
                'description': 'For diff: which files and how much, without the content.',
            },
            'paths': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'For stage/unstage/push. Leave it out to mean all of them.',
            },
            'message': {
                'type': 'string',
                'description': 'For commit. Required, and not generated for you.',
            },
            'ref': {
                'type': 'string',
                'description': 'For log/show: a commit-ish. Defaults to HEAD.',
            },
            'limit': {'type': 'integer', 'description': 'For log, and for pr_list. Default 15, and 10.'},
            'stat': {
                'type': 'boolean',
                'description': 'For branch: include the upstream and ahead/behind. Default true.',
            },
            'title': {
                'type': 'string',
                'description': 'For pr_create. Required. One line, in the imperative, saying what the '
                               'change does — not a restatement of the branch name.',
            },
            'body': {
                'type': 'string',
                'description': 'For pr_create. Markdown is fine. Say what the change does and why, and '
                               'anything a reviewer could not work out from the diff alone.',
            },
            'base': {
                'type': 'string',
                'description': 'For pr_create: the branch to merge into. Leave it out for the repository\'s '
                               'own default branch, which is almost always right.',
            },
            'draft': {
                'type': 'boolean',
                'description': 'For pr_create: open it as a draft. Default false.',
            },
            'fill': {
                'type': 'boolean',
                'description': 'For pr_create: with no body, write one from the commits since the merge '
                               'base. A starting draft to edit, not a finished description.',
            },
            'number': {
                'type': 'integer',
                'description': 'For pr_view: which pull request. Leave it out for the current branch\'s.',
            },
        },
        'required': ['action'],
    }

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        action = str(args.get('action') or '').strip().lower()
        if not action:
            return Assessment(risk=Risk.READ, summary='', invalid='action is required')
        if action not in RISK:
            return Assessment(
                risk=Risk.READ,
                summary='',
                invalid=f'{action!r} is not something this does. It can: {", ".join(ACTIONS)}',
            )
        risk = RISK[action]

        if action == 'commit':
            message = str(args.get('message') or '')
            subject = core.subject_of(message)
            if not subject:
                # Checked here, before any approval, so a person is never
                # asked to confirm a commit that cannot be made.
                return Assessment(
                    risk=risk,
                    summary='',
                    invalid='a commit needs a message — read the diff and write the line yourself',
                )
            if len(message.strip().splitlines()) > 1 and '\n\n' not in message.strip():
                # A body with no blank line after the subject is a formatting
                # mistake `--cleanup=strip` will not fix for you.
                return Assessment(
                    risk=risk,
                    summary='',
                    invalid='put a blank line between the subject and the body of a commit message',
                )
            return Assessment(risk=risk, summary=f'commit {subject}')

        if action == 'stage':
            paths = [str(p) for p in (args.get('paths') or []) if str(p).strip()]
            return Assessment(risk=risk, summary=f'stage {", ".join(paths) if paths else "everything"}')

        if action == 'unstage':
            paths = [str(p) for p in (args.get('paths') or []) if str(p).strip()]
            return Assessment(risk=risk, summary=f'unstage {", ".join(paths) if paths else "everything"}')

        if action == 'push':
            paths = [str(p) for p in (args.get('paths') or []) if str(p).strip()]
            return Assessment(
                risk=risk,
                summary=f'push {", ".join(paths) if paths else "the current branch"} to the remote',
            )

        if action == 'pr_create':
            # Argument-only, like every other action here: the approval
            # decision is made from what the model asked for, before anything
            # has happened, and reaching into the repository from `assess` to
            # find the branch name would make the prompt depend on a subprocess
            # that might not be there. The title is the argument worth showing
            # — it is the sentence a reviewer will read on the other side.
            title = core.subject_of(str(args.get('title') or ''))
            if not title:
                return Assessment(
                    risk=risk,
                    summary='',
                    invalid='a pull request needs a title — write the one line that says what it changes',
                )
            into = str(args.get('base') or '').strip()
            return Assessment(
                risk=risk,
                summary=f'open a pull request "{title}"{f" into {into}" if into else ""} and notify reviewers',
            )

        if action == 'pr_view':
            number = _as_int(args.get('number'))
            what = f'pull request #{number}' if number > 0 else "this branch's pull request"
            return Assessment(risk=risk, summary=f'view {what}')

        if action == 'checkout':
            what = str(args.get('path') or args.get('ref') or '').strip()
            if not what:
                return Assessment(risk=risk, summary='', invalid='checkout needs a branch or commit to switch to')
            return Assessment(risk=risk, summary=f'check out {what}')

        target = str(args.get('path') or '').strip()
        if action == 'diff':
            where = 'staged' if args.get('staged') else 'unstaged'
            return Assessment(risk=risk, summary=f'diff ({where}){f" of {target}" if target else ""}')
        if action == 'show':
            ref = str(args.get('ref') or 'HEAD').strip()
            if not ref or ref.startswith('-'):
                return Assessment(risk=risk, summary='', invalid=f'{ref!r} is not a commit reference')
            return Assessment(risk=risk, summary=f'show {ref}')
        return Assessment(risk=risk, summary=action)

    def _root(self, ctx: ToolContext) -> Any:
        """The directory to operate in, confined the same way files are.

        A tool that ignores `confined` is a way out of it, so this goes
        through the same `resolve_in_root` the file tools use rather than
        taking `ctx.cwd` and trusting it.
        """
        try:
            return resolve_in_root('.', ctx)
        except ToolError as exc:
            raise ToolError(str(exc)) from exc

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        action = str(args['action']).strip().lower()
        root = self._root(ctx)
        target = str(args.get('path') or '').strip()
        paths = [str(p) for p in (args.get('paths') or []) if str(p).strip()]

        if not core.is_repo(root):
            # The common case is a session pointed at a directory that is not
            # under version control, and the answer is a sentence the model can
            # act on rather than a stack trace it has to interpret.
            return Output(
                content=(
                    f'{root} is not inside a git repository, so there is nothing to report. '
                    'If this should be one, say so rather than initialising it unasked — a '
                    'repository is a decision about somebody else\'s work too.'
                ),
                display={'root': str(root), 'repo': False},
            )

        try:
            if action == 'status':
                state = await core.status(root)
                return Output(
                    content=state.describe(),
                    display={
                        'root': str(root),
                        'repo': True,
                        'branch': state.branch,
                        'upstream': state.upstream,
                        'ahead': state.ahead,
                        'behind': state.behind,
                        'clean': state.clean,
                        'staged': [c.path for c in state.staged],
                        'unstaged': [c.path for c in state.unstaged],
                        'untracked': [c.path for c in state.untracked],
                    },
                )

            if action == 'diff':
                text, clipped = await core.diff(
                    root, staged=bool(args.get('staged')), path=target, stat_only=bool(args.get('stat_only'))
                )
                if not text.strip():
                    where = 'the index' if args.get('staged') else 'the working tree'
                    return Output(content=f'No changes in {where}.' + (f' ({target})' if target else ''))
                return Output(
                    content=text,
                    display={'command': f'git diff{" --cached" if args.get("staged") else ""}', 'diff': text},
                    truncated=clipped,
                )

            if action == 'log':
                text = await core.log(
                    root, limit=int(args.get('limit') or 15), path=target, ref=str(args.get('ref') or 'HEAD')
                )
                return Output(content=text, display={'command': 'git log'})

            if action == 'show':
                text = await core.show(root, str(args.get('ref') or 'HEAD'), path=target)
                return Output(content=text, display={'command': f'git show {args.get("ref") or "HEAD"}'})

            if action == 'branch':
                if args.get('stat') is False:
                    _, out, _ = await core.run(['--no-pager', 'branch', '--no-color'], root)
                    return Output(content=out or '(no branches yet)', display={'command': 'git branch'})
                text = await core.branches(root)
                return Output(content=text, display={'command': 'git branch -vv'})

            if action == 'stage':
                what = await core.stage(root, paths)
                state = await core.status(root)
                return Output(
                    content=f'Staged {what}. {len(state.staged)} file(s) in the index now.',
                    display={'staged': [c.path for c in state.staged], 'untracked': [c.path for c in state.untracked]},
                )

            if action == 'unstage':
                what = await core.unstage(root, paths)
                state = await core.status(root)
                return Output(
                    content=f'Unstaged {what}. {len(state.staged)} file(s) left in the index.',
                    display={'staged': [c.path for c in state.staged]},
                )

            if action == 'commit':
                result = await core.commit(root, str(args['message']))
                return Output(
                    content=f'Committed {result["sha"]}: {result["subject"]}\n\n{result["summary"]}',
                    display={
                        'sha': result['sha'],
                        'subject': result['subject'],
                        'files': result['files'],
                        'command': 'git commit',
                    },
                )

            if action == 'checkout':
                what = str(args.get('path') or args['ref']).strip()
                if what.startswith('-'):
                    raise ToolError(f'{what!r} is not a branch or commit')
                await core.run(['checkout', what], root)
                now = await core.current_branch(root)
                return Output(content=f'Now on {now}.', display={'branch': now, 'command': f'git checkout {what}'})

            if action == 'push':
                what = ', '.join(paths) if paths else 'the current branch'
                _, out, err = await core.run(['push', *paths], root)
                return Output(
                    content=(out or err or f'Pushed {what}.').strip(),
                    display={'command': f'git push {" ".join(paths)}'.strip()},
                )

            if action == 'pr_create':
                pull = await core.pr_create(
                    root,
                    title=str(args.get('title') or ''),
                    body=str(args.get('body') or ''),
                    base=str(args.get('base') or ''),
                    draft=bool(args.get('draft')),
                    fill=bool(args.get('fill')),
                )
                return Output(content=f'Opened pull request {pull.describe()}', display=_pr_display(pull))

            if action == 'pr_view':
                number = _as_int(args.get('number'))
                pull = await core.pr_view(root, number)
                if pull is None:
                    which = f'#{number}' if number > 0 else 'on this branch'
                    return Output(
                        content=(
                            f'There is no pull request {which}. One is opened with action "pr_create", which '
                            'needs a title, a committed working tree, and a branch that has been pushed.'
                        ),
                        display={'pr': False, 'number': number},
                    )
                return Output(content=pull.describe(), display=_pr_display(pull))

            if action == 'pr_list':
                pulls = await core.pr_list(root, _as_int(args.get('limit')) or 10)
                if not pulls:
                    return Output(
                        content=f'There are no open pull requests in {root.name or "this repository"}.',
                        display={'prs': []},
                    )
                return Output(
                    content='\n'.join(f'* {pull.line()}' for pull in pulls),
                    display={'prs': [_pr_display(pull) for pull in pulls]},
                )
        except core.PRUnavailable as exc:
            # Not a failure of the call, and not retryable: the session has no
            # way to reach GitHub and the answer is a person installing gh or
            # exporting a token. A ToolError here would be read as "try
            # something else" and the model would try the shell.
            return Output(content=str(exc), display={'pr': False, 'action': action, 'why': str(exc)})
        except core.GitError as exc:
            # Expected: git said no, and its own words are the useful part.
            raise ToolError(str(exc)) from exc

        raise ToolError(f'{action}: not handled')


def _as_int(value: Any) -> int:
    """An integer argument, or 0.

    A model asked for "pull request number" will sometimes answer with a
    string, and a `ValueError` escaping from here would end the turn over a
    field that has an obvious sensible default.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _pr_display(pull: core.PullRequest) -> dict[str, Any]:
    """What a tool card renders, rather than what the model reads."""
    return {
        'pr': True,
        'number': pull.number,
        'title': pull.title,
        'state': pull.state,
        'url': pull.url,
        'draft': pull.draft,
        'base': pull.base,
        'head': pull.head,
        'author': pull.author,
        'checks': pull.checks,
        'backend': pull.backend,
    }

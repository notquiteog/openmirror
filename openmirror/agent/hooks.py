"""Hooks: commands that run around a tool call, and can refuse it.

Claude Code has these, openCode calls them plugins, and both treat them as the
extension point rather than a setting. A hook is a command that gets the call
as JSON on stdin and answers with its exit code.

**A hook can only take things away.** This is the whole design, and it is
what makes it safe to run a project's hooks at all:

    exit 0  -> carry on
    exit != 0 -> refuse, and the reason goes back to the model

There is no exit code, no JSON field and no output that turns a refusal into
an approval. A hook runs *before* the approval policy, and a refusal from the
policy is final regardless of what a hook said. So a repository that ships a
`hooks.json` can stop you doing things; it cannot do things on your behalf.
That asymmetry is the same one `AGENTS.md` is already subject to — project
files are instructions to a model, and this is the version that runs code, so
it has to be weaker than the operator's own configuration and it is.

**Which is also why a project's hooks are confirmed once.** A hook file in
`~/.openmirror/` is the person's own and runs. The same file in a project you
just opened is code somebody else wrote, so the first time it would run, the
interface says what it would run and asks. One answer covers the session, and
the answer is remembered per hook command, not per file — a project cannot get
a new hook added and quietly inherit an old one's answer.

**They cannot hang the turn.** Every hook has a wall clock, and a hook that
misses it is reported and treated as *having said nothing*. A hook that times
out is not allowed to become a denial, because "your formatter was slow" and
"you may not do this" are different sentences and a timeout must not say the
second one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# A hook is a command someone else may have written, run on every tool call
# in a session. Ten seconds is long for a formatter and short enough that a
# wedged one is a message rather than a hang.
DEFAULT_TIMEOUT = 10.0
# A hook's whole output, kept for the model and for the transcript. Big enough
# for a linter's complaints and small enough that a runaway command cannot
# fill a context window.
MAX_OUTPUT = 8_000
# An event name this build does not know is ignored rather than an error, so a
# hooks file written for a later version still does the part of the work this
# version understands.
EVENTS = ('PreToolUse', 'PostToolUse', 'UserPromptSubmit', 'Stop')


class HookError(RuntimeError):
    """A hook could not be run at all."""


@dataclass(slots=True)
class Hook:
    """One command, bound to an event."""

    event: str
    command: str
    #: Matched against the tool name. Empty means every tool.
    tools: list[str] = field(default_factory=list)
    #: Matched against the call's one-line summary, as a regex. Empty means
    #: every summary. This is what lets a hook watch a *kind* of change rather
    #: than a named tool.
    pattern: str = ''
    #: Shown in the confirmation, because a command you are being asked to
    #: agree to has to be the actual command.
    name: str = ''
    source: str = ''      # which file it came from
    trusted: bool = True  # the operator's own, rather than a project's

    def matches(self, *, tool: str = '', summary: str = '') -> bool:
        """Whether this hook applies to a call.

        Both conditions must hold when both are given, and a regex that does
        not compile matches nothing — a broken pattern in a hooks file must
        not turn into a hook that runs on everything.
        """
        if self.tools and tool and tool not in self.tools:
            return False
        if self.pattern:
            import re

            try:
                if not re.search(self.pattern, summary):
                    return False
            except re.error as exc:
                log.warning('hook %r has an invalid pattern, ignoring it: %s', self.name or self.command, exc)
                return False
        return True

    def public(self) -> dict[str, Any]:
        return {
            'event': self.event,
            'command': self.command,
            'name': self.name or self.command,
            'tools': self.tools,
            'pattern': self.pattern,
            'source': self.source,
            'trusted': self.trusted,
        }


@dataclass(slots=True)
class HookOutcome:
    """What the hooks said about one call.

    `blocked` and the reason are the answer that matters. `notes` is advisory
    output from `PostToolUse`, which goes back to the model as context and
    never changes what happened.
    """

    blocked: str = ''
    notes: str = ''
    ran: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


# The file names a hooks file can have, in the order they are read. Both
# spellings because both ecosystems use one and a person arriving from either
# will have written the other.
FILES = ('hooks.json', '.openmirror/hooks.json')


def find(root: Path, home: Path | None = None) -> list[Hook]:
    """Every hook configured for this project and this person.

    A project's file is *not* trusted: it is read, it is reported, and whether
    it runs is a question for the interface the first time. The person's own
    file is trusted, because it is their own.

    The project's file is read rather than ignored so that the interface can
    show what a repository wants to do before deciding whether to let it —
    "this project has hooks" is information; "this project silently ran
    something" is not.
    """
    found: list[Hook] = []

    if home is not None:
        for base in (home / '.openmirror', home):
            for name in ('hooks.json',):
                path = base / name
                if path.is_file():
                    found += _read(path, trusted=True)

    for name in FILES:
        path = root / name
        if path.is_file():
            found += _read(path, trusted=False)
    return found


def _read(path: Path, *, trusted: bool) -> list[Hook]:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        # A hooks file that cannot be read is reported and skipped. It is
        # somebody's configuration, and refusing to start a session over it
        # would be a worse answer than not running the hooks.
        log.warning('could not read %s: %s', path, exc)
        return []
    return parse(raw, source=str(path), trusted=trusted)


def parse(raw: Any, *, source: str = '', trusted: bool = True) -> list[Hook]:
    """A hooks document into hooks.

    Accepts two shapes, because both ecosystems shipped one and people have
    both in their heads:

        {"PreToolUse": [{"command": "..."}]}
        {"hooks": {"PreToolUse": [...]}}

    Either way an entry may name `tools`, a list, to bind it to particular
    tools, and `pattern`, a regex, to bind it to a particular kind of change.
    """
    if not isinstance(raw, dict):
        return []
    document = raw.get('hooks') if isinstance(raw.get('hooks'), dict) else raw
    if not isinstance(document, dict):
        return []

    out: list[Hook] = []
    for event, entries in document.items():
        if event not in EVENTS:
            # An event from a later version. Ignored rather than refused, so
            # a file written for a newer openmirror still does the parts this
            # one understands.
            log.debug('ignoring unknown hook event %r', event)
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                entry = {'command': entry}
            if not isinstance(entry, dict):
                continue
            command = str(entry.get('command') or '').strip()
            if not command:
                continue
            tools = entry.get('tools') or []
            if isinstance(tools, str):
                tools = [tools]
            out.append(Hook(
                event=event,
                command=command,
                tools=[str(t) for t in tools],
                pattern=str(entry.get('pattern') or ''),
                name=str(entry.get('name') or ''),
                source=source,
                trusted=trusted,
            ))
    return out


def payload(event: str, call: Any = None, *, session_id: str = '', cwd: str = '') -> dict[str, Any]:
    """What a hook is handed on stdin.

    Plain JSON, the same keys a tool call already has, because a hook is
    written against the thing it is watching and not against this file.
    """
    body: dict[str, Any] = {'event': event, 'session_id': session_id, 'cwd': cwd}
    if call is not None:
        body.update({
            'tool': call.name,
            'arguments': call.arguments,
            'summary': call.summary,
            'risk': getattr(call.risk, 'value', ''),
        })
    return body


def interpret(event: str, code: int, out: str, err: str, hook: Hook) -> HookOutcome:
    """An exit code into a decision.

    `PreToolUse` is the only event that can block, because it is the only one
    that runs before the thing it is judging. `PostToolUse` is advisory: the
    tool has already run, so a non-zero exit is a *complaint*, not a
    veto, and pretending otherwise would mean reporting work that had already
    happened as work that did not.
    """
    name = hook.name or hook.command
    text = (out or '').strip()
    problems = (err or '').strip()

    if not _did_it_start(code):
        # Reported, and treated as silence. A hook that could not start has
        # not had an opinion.
        return HookOutcome(problems=[f'{name}: could not start (exit {code})'])

    if event == 'PreToolUse':
        if code == 0:
            return HookOutcome(ran=[name], notes=text[:MAX_OUTPUT])
        reason = (problems or text or f'{name} exited {code}').strip()
        return HookOutcome(blocked=reason[:MAX_OUTPUT], ran=[name])

    if code == 0:
        return HookOutcome(ran=[name], notes=text[:MAX_OUTPUT])
    return HookOutcome(ran=[name], problems=f'{name}: {problems or text or f"exited {code}"}'[:MAX_OUTPUT])


async def run(
    hooks: list[Hook],
    event: str,
    call: Any = None,
    *,
    cwd: Path,
    session_id: str = '',
    seconds: float = DEFAULT_TIMEOUT,
) -> HookOutcome:
    """Every hook for an event, in order, and what they said.

    **The first refusal stops the rest.** A `PreToolUse` that blocked has
    already answered the question, and running the remaining hooks would run
    code on behalf of a call that is not going to happen.
    """
    outcome = HookOutcome()
    for hook in hooks:
        if hook.event != event:
            continue
        if event in ('PreToolUse', 'PostToolUse') and not hook.matches(
            tool=getattr(call, 'name', ''), summary=getattr(call, 'summary', '')
        ):
            continue

        result = await _one(hook, event, call, cwd=cwd, session_id=session_id, seconds=seconds)
        outcome.ran.extend(result.ran)
        if result.notes:
            outcome.notes = f'{outcome.notes}\n{result.notes}'.strip()
        if result.problems:
            outcome.problems = [*outcome.problems, *result.problems]
        if result.blocked:
            outcome.blocked = result.blocked
            break
    return outcome


def _kill_tree(proc: Any) -> None:
    """Kill a hook and everything it started.

    The group, not the process: a hook is a shell command, and killing the
    shell leaves its children — the thing actually doing the work — running.
    """
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _did_it_start(code: int) -> bool:
    """Whether the command ran at all.

    POSIX says 126 is "found but not executable" and 127 is "not found", and
    neither is the hook *saying* anything — a hook file naming a program that
    is not installed is a mistake, not a refusal. Reading one as a block
    means a typo in somebody else's `hooks.json` silently stops every tool
    call that matches it, and the person is told their formatter said no.
    """
    return code not in (126, 127)


async def _one(
    hook: Hook, event: str, call: Any, *, cwd: Path, session_id: str, seconds: float
) -> HookOutcome:
    """One hook, on a shell, with a clock.

    The environment is the session's own plus what a hook legitimately needs
    to be told, and *not* the model's view of the world: a hook that could
    read the API keys out of the tool arguments would be able to leak them,
    and the arguments are already in the payload for a hook that has been
    confirmed.
    """
    env = {
        **os.environ,
        'OPENMIRROR_HOOK_EVENT': event,
        'OPENMIRROR_TOOL': getattr(call, 'name', ''),
        'OPENMIRROR_SESSION': session_id,
    }
    try:
        proc = await asyncio.create_subprocess_shell(
            hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return HookOutcome(problems=[f'{hook.name or hook.command}: could not start ({exc})'])

    body = json.dumps(payload(event, call, session_id=session_id, cwd=str(cwd)))
    try:
        async with asyncio.timeout(seconds):
            out, err = await proc.communicate(body.encode('utf-8'))
    except TimeoutError:
        # The whole group, not just the shell. `start_new_session=True` makes
        # the child a process-group leader precisely so this can be done, and
        # killing only the shell leaves a hook's children running into the next
        # turn — which is how one bad hook becomes several, and the reason the
        # first version of this leaked.
        _kill_tree(proc)
        await proc.wait()
        name = hook.name or hook.command
        log.warning('hook %s took longer than %ss', name, seconds)
        return HookOutcome(problems=[f'{name} took longer than {seconds:g}s and was ignored'])

    return interpret(
        event, proc.returncode or 0, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace'), hook
    )


def describe(hooks: list[Hook]) -> str:
    """One line per hook, for the interface and for a log line."""
    return '\n'.join(
        f'{h.event:<17} {h.name or h.command}' + (f'  ({h.source})' if h.source else '')
        for h in hooks
    )


def command_of(hook: Hook) -> str:
    """The command, for the confirmation.

    Split so a reader sees the program and the words rather than one
    unbreakable string, because this is the text somebody is being asked to
    agree to running.
    """
    try:
        parts = shlex.split(hook.command)
    except ValueError:
        return hook.command
    return ' '.join(parts)


__all__ = [
    'DEFAULT_TIMEOUT', 'EVENTS', 'FILES', 'Hook', 'HookError', 'HookOutcome', 'command_of', 'describe',
    'find', 'interpret', 'parse', 'payload', 'run',
]

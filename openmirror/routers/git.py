"""Committing from the interface, which is a different question from the agent's.

The agent can already commit — `git`, action `commit` — and it writes the
message itself, because it has just read the diff. This route is the other
case: a person in a project, at the end of a piece of work, who wants to
commit and has to decide whether the wording is theirs or the model's.

That is the whole of "with or without AI", and the design puts the decision
where it belongs:

* **Without**, the person types a message and it is committed. No model is
  called, no diff leaves the machine, and the request is exactly as cheap as
  the commit.
* **With**, `POST /api/git/propose` sends the *staged diff* to whichever
  provider this install routes chat to, and returns a draft. It returns a
  draft and nothing else — it cannot commit, because it does not hold a
  message the person has not seen. The commit is a second request, with a
  message the person edited or replaced.

So the model never writes a commit that reaches a repository without somebody
reading it first. That is the whole reason the two are separate calls, and it
is why there is no `ai: true` flag on the commit endpoint that quietly drafts
and commits in one go: a flag like that gets set once and then nobody reads
the messages again.

The diff that goes to the provider is trimmed to `MAX_DIFF` characters and is
the staged diff specifically — what a commit would contain — rather than
everything that changed, because a person about to commit three of eleven
changed files should be shown a description of three files, and because the
unstaged remainder is frequently code that does not compile yet.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from openmirror.agent import git as core
from openmirror.providers.base import ChatRequest, Message, NoProviderError, TextBlock, one_shot

log = logging.getLogger(__name__)

router = APIRouter(prefix='/api/git')

# Enough to describe the change, not enough to post a repository to a
# provider. A staged diff that is larger than this is a bad commit anyway, and
# the alternative — the first forty kilobytes of an enormous one — describes
# the wrong change.
MAX_DIFF = 24_000
# How much of the model's answer is kept. A commit message longer than this is
# a message written in the wrong place, and the person is about to read it.
MAX_MESSAGE = 2000

SYSTEM = """You write git commit messages. You are given a diff and nothing else.

Write a message in the conventional style, which here means:
- A subject line, imperative mood, under 72 characters, no trailing period.
  "add retry to the fetch loop", not "added" and not "fixes".
- A blank line.
- A body, only when the diff needs one, wrapped at 72 columns. Say what the
  change does and why it had to. No restating of the subject.

Describe what the diff does. Do not mention files, line numbers, statistics or
commit hashes — the reader has those. Do not write "update files" or "changes".
Do not add a scope prefix unless the diff is unambiguously one thing and the
project already uses them; if you cannot tell, write no prefix.

Reply with the message and nothing else — no preamble, no code fence, no
commentary about the diff."""


class CommitRequest(BaseModel):
    root: str
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)
    stage: list[str] | None = Field(
        default=None,
        description='Paths to stage first. Omit to commit whatever is already staged.',
    )


class ProposeRequest(BaseModel):
    root: str
    limit: int = Field(default=MAX_DIFF, ge=500, le=MAX_DIFF)


def _root(raw: str) -> Path:
    """The repository to work in.

    A path from the client, and it is *not* resolved against a session root —
    this route is not inside one, and pretending otherwise would either be a
    lie or a false restriction. What it does instead is refuse anything that
    is not a working tree, so the worst a caller can reach is a repository
    they could have read with the file tools anyway. The `git` tool, which does
    run inside a session, goes through `resolve_in_root` as it should.
    """
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f'{raw}: not a directory')
    if not core.is_repo(path):
        raise HTTPException(status_code=400, detail=f'{path} is not a git repository')
    return path


async def _draft(diff: str) -> str:
    """One model call, and its answer cleaned up into a message.

    The cleanup is not decoration. Models asked for "the message and nothing
    else" still fence it, still prefix "Here is the commit message:", and the
    first version of this returned a draft that could not be committed
    verbatim — which is the worst possible outcome for a feature whose only
    job is to save somebody typing.
    """
    # The same resolution a session goes through, and for the same reason: a
    # route with no model on it is not a model name, and sending an empty one
    # gets a refusal from a server that would otherwise have answered.
    from openmirror.routers.agent import resolve_chat

    provider, model, _info = await resolve_chat()
    request = ChatRequest(
        model=model,
        messages=[Message(role='user', content=[TextBlock(text=f'Write the commit message for this diff:\n\n{diff}')])],
        system=SYSTEM,
        temperature=0.2,
        # No `max_tokens`: see `one_shot`. A ceiling here is not a saving, it
        # is a way to make a reasoning model return nothing at all.
    )
    return _clean(await one_shot(provider, request, what='commit message'))


def _clean(raw: str) -> str:
    """Strip everything a model puts around a commit message.

    Four shapes, all of them observed: a fenced block, a `Commit message:`
    label, a leading "Here is..." sentence, and quotes around every line.
    Done in that order, because a fence inside a quoted block still has to
    come off.
    """
    text = (raw or '').strip()
    if not text:
        return ''

    fence = re.search(r'```[a-z]*\n(.*?)(?:\n)?```', text, re.S)
    if fence:
        text = fence.group(1).strip()

    text = re.sub(r'^(commit message|message)\s*:\s*', '', text, flags=re.I)
    # A first line that is a sentence about the message rather than the
    # message. Anchored and required to end in a colon-or-newline so that a
    # legitimate subject containing the word "message" survives.
    text = re.sub(r'^here (?:is|are)\b[^\n:]*:\s*\n+', '', text, flags=re.I)

    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and len(lines) > 1 and all(line.startswith('>') for line in lines if line.strip()):
        lines = [line.lstrip('> ').rstrip() for line in lines]
    return '\n'.join(lines).strip()[:MAX_MESSAGE]


@router.get('/status')
async def read_status(root: str) -> dict[str, Any]:
    path = _root(root)
    state = await core.status(path)
    return {
        'root': str(path),
        'branch': state.branch,
        'upstream': state.upstream,
        'ahead': state.ahead,
        'behind': state.behind,
        'detached': state.detached,
        'clean': state.clean,
        'staged': [{'path': c.path, 'label': c.label, 'rename_from': c.rename_from} for c in state.staged],
        'unstaged': [{'path': c.path, 'label': c.label} for c in state.unstaged],
        'untracked': [{'path': c.path, 'label': c.label} for c in state.untracked],
    }


@router.get('/diff')
async def read_diff(root: str, staged: bool = True, path: str = '', stat_only: bool = False) -> dict[str, Any]:
    path_root = _root(root)
    text, clipped = await core.diff(path_root, staged=staged, path=path, stat_only=stat_only)
    return {'root': str(path_root), 'staged': staged, 'diff': text, 'truncated': clipped}


@router.get('/log')
async def read_log(root: str, limit: int = 15, path: str = '') -> dict[str, Any]:
    path_root = _root(root)
    text = await core.log(path_root, limit=limit, path=path)
    return {'root': str(path_root), 'log': text}


@router.post('/propose')
async def propose_message(body: ProposeRequest) -> dict[str, Any]:
    """A draft commit message for what is staged. Commits nothing.

    The failure that matters here is a provider that is not configured or is
    refusing to serve, and it is reported as a 503 naming the reason rather
    than as a 500 — because the caller's correct response is different for
    each: turn a feature off, or fix a key. And a refusal is never turned
    into a fallback message, because "AI wrote your commit message" should
    never be something that was not written by the AI.
    """
    path = _root(body.root)
    text, clipped = await core.diff(path, staged=True)
    if not text.strip():
        # Nothing staged is not a provider failure and must not be reported
        # as one: the fix is to stage something.
        raise HTTPException(status_code=400, detail='nothing is staged, so there is no diff to describe')
    if clipped:
        text = text[:body.limit]
    try:
        message = await _draft(text)
    except NoProviderError as exc:
        raise HTTPException(status_code=503, detail=f'no model can write this: {exc}') from exc
    if not message:
        raise HTTPException(status_code=502, detail='the model returned an empty message')
    return {'root': str(path), 'message': message, 'subject': core.subject_of(message)}


@router.post('/commit')
async def make_commit(body: CommitRequest) -> dict[str, Any]:
    """Commit the staged changes, or stage what was named and then commit.

    Deliberately takes a message and never drafts one — see the module
    docstring. `stage` is a convenience and not a convenience worth much on
    its own: it exists so a person can commit from the interface without the
    interface growing a staging UI, and it is an explicit list rather than a
    boolean so that "commit everything" is something somebody typed.
    """
    path = _root(body.root)
    if body.stage:
        try:
            await core.stage(path, [p for p in body.stage if p.strip()])
        except core.GitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = await core.commit(path, body.message)
    except core.GitError as exc:
        # git's own wording: "no changes added to commit", "please tell me who
        # you are", "commit message is empty". Each names its own fix.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = await core.status(path)
    return {**result, 'branch': state.branch, 'ahead': state.ahead, 'behind': state.behind}


class StageRequest(BaseModel):
    root: str
    paths: list[str] = Field(default_factory=list)


@router.post('/stage')
async def stage_paths(body: StageRequest) -> dict[str, Any]:
    path = _root(body.root)
    try:
        what = await core.stage(path, [p for p in (body.paths or []) if p.strip()])
    except core.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = await core.status(path)
    return {'root': str(path), 'staged': what, 'files': [c.path for c in state.staged]}


@router.post('/unstage')
async def unstage_paths(body: StageRequest) -> dict[str, Any]:
    path = _root(body.root)
    try:
        what = await core.unstage(path, [p for p in (body.paths or []) if p.strip()])
    except core.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = await core.status(path)
    return {'root': str(path), 'unstaged': what, 'files': [c.path for c in state.staged]}

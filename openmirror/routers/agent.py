"""The agent over HTTP and a websocket.

Sessions are created over HTTP and *attached to* over the websocket, rather
than being owned by it. The distinction is the whole point: a socket is a
window onto work that is happening anyway, so closing it pauses your view and
nothing else. Reattaching passes the last sequence number seen and the session
replays what was missed.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from openmirror.agent import windows
from openmirror.agent.approval import Mode
from openmirror.agent.manager import manager
from openmirror.config import config
from openmirror.providers.base import Modality
from openmirror.providers.reasoning import normalise
from openmirror.providers.registry import NoProviderError, Route, RouteSet, pick_model, registry
from openmirror.routers import media as media_router
from openmirror.routers import memory as memory_router

log = logging.getLogger(__name__)

router = APIRouter()
http = APIRouter(prefix='/api/sessions')


async def resolve_chat(provider: str | None = None, model: str | None = None) -> tuple[object, str, str]:
    """Pick the chat provider and a concrete model. Raises NoProviderError.

    Shared rather than private, because the two other places that need a model
    for a single turn — the commit-message draft in `routers/git.py` and the
    reply draft in `routers/mail.py` — need exactly this and nothing else. A
    second copy of "or ask the provider what it has" is a second copy of the
    reason the fallback exists, and one of them will be the one that forgets.
    """
    routes = RouteSet(routes={Modality.CHAT: Route(provider=provider, model=model or '')}) if provider else None
    impl, route, info = registry.resolve(Modality.CHAT, routes)

    chosen = model or route.model or config.default_chat_model
    if not chosen:
        # Asking the provider is better than guessing a name: a local install
        # has whatever happens to be pulled, and that is not knowable from here.
        available = await impl.models()
        if not available:
            raise NoProviderError(f'{info.id} reports no models')
        # Not simply the first. An install with an embedding model pulled
        # alongside a chat one answered every conversation with "that model
        # does not support chat", because the first entry happened to be the
        # embedding one — and an agent session needs tool calling on top of
        # that, so a model that cannot do it is the wrong default even when it
        # would answer.
        chosen = pick_model(available, Modality.CHAT, need_tools=True)
        if not chosen:
            names = ', '.join(str(m.get('id')) for m in available[:8])
            raise NoProviderError(
                f'{info.id} has no model that can hold a conversation. It offers: {names}'
            )
    return impl, chosen, info.id


# Kept as a private alias so the many call sites inside this module read the
# same as they did before this was shared.
_resolve_chat = resolve_chat


class CreateSession(BaseModel):
    root: str | None = None
    model: str | None = None
    provider: str | None = None
    mode: str | None = None
    title: str = ''
    # Which groups of tools this session gets. Empty means all of them.
    #
    # Worth having because a long tool list is not free: a 12B model given
    # thirty tools failed a five-step browser task that the same model, with
    # only the browser tools, finished in twenty-six seconds. Group names are
    # in `runtime.TOOLSETS`; an unrecognised entry is taken as a tool name.
    tools: list[str] = []
    # How hard the model thinks: off, low, medium, high, xhigh, max — or
    # empty for the model's own default. Changeable later with `policy.set`
    # or `/think`, because whether a problem deserves it is learned mid-run.
    effort: str | None = None


@http.post('')
async def create_session(body: CreateSession) -> dict[str, object]:
    # Refused rather than dropped: a level that is not one would otherwise
    # become "the model's default" in silence, and look like it was accepted.
    effort = normalise(body.effort) if body.effort not in (None, '', 'default') else None
    if body.effort not in (None, '', 'default') and effort is None:
        raise HTTPException(status_code=400, detail=f'not a thinking level: {body.effort!r}')

    try:
        impl, model, provider_id = await _resolve_chat(body.provider, body.model)
    except NoProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        session = await manager.create(
            root=body.root or config.workspace,
            provider=impl,
            model=model,
            effort=effort,
            mode=Mode(body.mode or config.approval_mode),
            title=body.title,
            memory=memory_router.service,
            media=media_router.service,
            toolset=body.tools,
            user_id=config.default_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        'id': session.id,
        'title': session.title,
        'root': str(session.root),
        'model': model,
        'provider': provider_id,
        'policy': session.policy.describe(),
        'tools': sorted(session.tools),
        'effort': session.effort,
    }


@http.get('')
async def list_sessions() -> dict[str, object]:
    return {'sessions': manager.list()}


@http.get('/{session_id}/context')
async def session_context(session_id: str) -> dict[str, object]:
    """How full this session's context is, and how far from being summarised.

    Not in the `list`, because it is not a property of a session at rest — it
    changes with every tool result — and a list polled every few seconds
    should not re-estimate every transcript in the sidebar.
    """
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    report = session.context_report()
    return {
        **report.model_dump(),
        'fraction': windows.window_fraction(report.tokens, report.limit, report.window),
        'model': session.model,
    }


@http.get('/toolsets')
async def list_toolsets() -> dict[str, object]:
    """The groups a session can be narrowed to, and what is in each."""
    from openmirror.agent.runtime import TOOLSETS

    return {'toolsets': {name: list(tools) for name, tools in TOOLSETS.items()}}


@http.get('/{session_id}/commands')
async def list_commands(session_id: str) -> dict[str, object]:
    """What `/` can be followed by in this session: commands, then skills."""
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    return {'commands': session.commands()}


@http.get('/{session_id}/files')
async def find_files(session_id: str, q: str = '', limit: int = 40) -> dict[str, object]:
    """Files to offer after an `@` in the composer.

    Bounded by a time budget and a count, and it never reads a file — only
    names, sizes and times. The person is still typing, and a suggestion list
    that takes a second to appear is a suggestion list that is wrong by the
    time it lands.
    """
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    import asyncio

    from openmirror.agent import files as finder

    # The walk is blocking and is on the request path behind a keystroke, so
    # it goes to a thread rather than stalling the event loop the websocket
    # for the very session it belongs to.
    hits = await asyncio.to_thread(finder.find, session.root, q, limit=limit)
    return {'files': hits, 'root': str(session.root)}


@http.get('/{session_id}/tasks')
async def list_tasks(session_id: str) -> dict[str, object]:
    """Background work in this session: commands left running, agents sent off."""
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    tasks = session.tasks.list() if session.tasks is not None else []
    return {'tasks': [t.describe() for t in tasks]}


@http.get('/{session_id}/checkpoints')
async def list_checkpoints(session_id: str) -> dict[str, object]:
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    if session.checkpoints is None:
        return {'enabled': False, 'checkpoints': []}
    return {'enabled': True, 'checkpoints': session.checkpoints.describe()}


@http.get('/{session_id}/review')
async def list_changes(session_id: str, checkpoint: str = '') -> dict[str, object]:
    """What the last turn changed, as hunks somebody can take or drop.

    Reads the before-content out of the snapshots rewind already keeps, so
    this is a read and adds no storage of its own.
    """
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    if session.checkpoints is None:
        return {'enabled': False, 'files': []}
    changes = session.checkpoints.changes(checkpoint or None)
    return {
        'enabled': True,
        **session.checkpoints.reviewable(),
        'files': [c.public() for c in changes],
    }


class Review(BaseModel):
    path: str
    # Which hunks to *keep*. Named for what it is rather than what to do with
    # it, so an empty list means "keep none" and not "no instruction" — the
    # difference between dropping everything and silently doing nothing.
    keep: list[int] = []
    force: bool = False


@http.post('/{session_id}/review')
async def apply_review(session_id: str, body: Review) -> dict[str, object]:
    """Write one file back with only the hunks named.

    A write, so it is refused while a turn is running: a review that lands
    halfway through an edit the agent is still making reconstructs a file
    against a version the turn has moved on from.
    """
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    if session.checkpoints is None:
        raise HTTPException(status_code=400, detail='this session keeps no undo history')
    if session.busy:
        raise HTTPException(status_code=409, detail='something is running; wait for the turn to finish')
    try:
        got = session.checkpoints.apply_review(body.path, set(body.keep), force=body.force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not got.get('ok'):
        raise HTTPException(status_code=409, detail=str(got.get('detail') or got.get('reason')))
    return got


@http.get('/{session_id}/hooks')
async def list_hooks(session_id: str) -> dict[str, object]:
    """What this project and this person have configured, and which of it has
    been agreed to.

    Shown whether or not anything is confirmed, because "this project has
    hooks" is information and "this project ran something" is not.
    """
    from openmirror.agent.hooks import command_of

    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    from openmirror.config import config

    return {
        'enabled': config.hooks_enabled,
        'allow_project': config.hooks_allow_untrusted,
        'timeout': config.hooks_timeout,
        'hooks': [
            {**h.public(), 'command_readable': command_of(h), 'agreed': h.command in session.hooks_agreed}
            for h in session.hooks
        ],
    }


class HookDecision(BaseModel):
    command: str
    allow: bool = True


@http.post('/{session_id}/hooks/agree')
async def agree_hook(session_id: str, body: HookDecision) -> dict[str, object]:
    """Agree to one hook, or refuse it, for this session.

    Matched on the *command* rather than on the file, so a project cannot get
    a new hook added and quietly inherit the answer somebody gave for a
    different one.
    """
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    if body.allow:
        session.hooks_agreed.add(body.command)
    else:
        # Refusing has to be remembered too, or the question comes back on
        # every tool call and becomes something to click through.
        session.hooks_agreed.discard(body.command)
        session.hooks_refused.add(body.command)
    return {'ok': True, 'agreed': sorted(session.hooks_agreed)}


@http.get('/settings')
async def read_settings(root: str = '') -> dict[str, object]:
    """Which settings files applied to a project, what they said, and what
    they were not allowed to say.

    The last of those is the one worth showing. A project asking to be less
    safe than you already are should be visible rather than silent, because
    silent is indistinguishable from not being read.
    """
    from openmirror.settings import load

    return load(root or config.workspace).public()


@http.get('/stored')
async def list_stored(limit: int = 100) -> dict[str, object]:
    """Every conversation on disk, newest first.

    Separate from `GET /api/sessions`, which is the *live* ones. After a
    restart the live list is empty and this is everything.
    """
    return {'sessions': manager.stored(limit=limit)}


@http.post('/{session_id}/resume')
async def resume_session(session_id: str) -> dict[str, object]:
    """Reopen a stored conversation as a live session.

    The root, model and toolset come from the transcript, not from the
    request: a conversation about one project reopened in another is a
    conversation that will confidently edit the wrong files.
    """
    from openmirror.config import config

    try:
        impl, model, _info = await _resolve_chat(None, None)
    except NoProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    session = await manager.resume(session_id, provider=impl, model=model, mode=config.approval_mode)
    if session is None:
        raise HTTPException(status_code=404, detail=f'no stored conversation called {session_id!r}')
    return {'session': {'id': session.id, 'title': session.title, 'root': str(session.root),
                       'model': session.model, 'policy': session.policy.mode.value}}


@http.get('/{session_id}/export')
async def export_session(session_id: str) -> dict[str, object]:
    """A conversation as Markdown, to paste somewhere else.

    Tool calls are rendered as what they were rather than dropped. "The agent
    ran a command and here is the conversation without it" is a document that
    misleads.
    """
    from openmirror.sessions import to_markdown

    store = manager.store()
    stored = store.load(session_id) if store is not None else None
    if stored is None:
        raise HTTPException(status_code=404, detail=f'no stored conversation called {session_id!r}')
    return {'session': session_id, 'title': stored.title, 'markdown': to_markdown(stored)}


class Restore(BaseModel):
    checkpoint: str


@http.post('/{session_id}/restore')
async def restore_checkpoint(session_id: str, body: Restore) -> dict[str, object]:
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='no such session')
    if session.checkpoints is None:
        raise HTTPException(status_code=404, detail='checkpoints are not enabled')
    if session.busy:
        # Rewinding under a running turn would race the very writes it is
        # trying to undo.
        raise HTTPException(status_code=409, detail='the session is working — interrupt it first')

    try:
        report = session.checkpoints.restore(body.checkpoint)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        'restored': report.restored,
        'deleted': report.deleted,
        'skipped': report.skipped,
        'changed_since': report.changed_since,
    }


@http.delete('/{session_id}')
async def close_session(session_id: str) -> dict[str, bool]:
    if not await manager.close(session_id):
        raise HTTPException(status_code=404, detail='no such session')
    return {'ok': True}


@router.websocket('/ws/agent')
async def agent_socket(
    ws: WebSocket,
    session: str | None = Query(None, description='Session to attach to.'),
    since: int = Query(0, description='Last event sequence number already seen.'),
    root: str | None = Query(None),
    model: str | None = Query(None),
    provider: str | None = Query(None),
    mode: str | None = Query(None),
    tools: str | None = Query(None, description='Comma-separated toolset names. Empty for all.'),
    token: str | None = Query(None),
) -> None:
    if config.auth_token and token != config.auth_token:
        # Refused before accepting, so an unauthenticated caller never holds a socket.
        await ws.close(code=4401, reason='unauthorised')
        return

    await ws.accept()

    agent = manager.get(session) if session else None

    if session and agent is None:
        # Almost always a page that outlived the daemon: sessions live in
        # memory, so restarting the daemon invalidates every id a client is
        # holding. Said plainly, because "no such session" reads like data loss
        # and is usually just a restart.
        await ws.send_json({
            'type': 'error',
            'message': (
                f'Session {session} is gone — the daemon has been restarted since this page '
                'last connected. Sessions do not survive a restart. Pick another from the '
                'sidebar, or start a new one.'
            ),
            'retryable': False,
        })
        await ws.close(code=4404)
        return

    if agent is None:
        # No session named: make one, so a trivial client stays trivial.
        try:
            impl, chosen, _ = await _resolve_chat(provider, model)
            agent = await manager.create(
                root=root or config.workspace,
                provider=impl,
                model=chosen,
                mode=Mode(mode or config.approval_mode),
                memory=memory_router.service,
                media=media_router.service,
                toolset=[t.strip() for t in (tools or '').split(',') if t.strip()],
                user_id=config.default_user,
            )
        except (NoProviderError, ValueError) as exc:
            await ws.send_json({'type': 'error', 'message': str(exc), 'retryable': False})
            await ws.close(code=4404)
            return

    async def pump_out() -> None:
        try:
            async for event in agent.events(since=since):
                await ws.send_json(event.model_dump(mode='json'))
        except (WebSocketDisconnect, RuntimeError):
            pass

    outbound = asyncio.create_task(pump_out())

    try:
        while True:
            command = await ws.receive_json()
            kind = command.get('type')

            if kind == 'turn.submit':
                try:
                    turn_id = agent.submit(command.get('text', ''), command.get('attachments') or [])
                except RuntimeError as exc:
                    await ws.send_json({'type': 'error', 'message': str(exc), 'retryable': False})
                else:
                    # An empty id means it was held rather than started, and
                    # the client has to know: the text is already echoed, and
                    # a message that looks sent and is not is the failure
                    # mode this whole feature exists to remove.
                    if not turn_id:
                        await ws.send_json({
                            'type': 'turn.queued',
                            'waiting': len(agent.queued),
                        })
            elif kind == 'tool.approve':
                agent.approve(command.get('call_id', ''), bool(command.get('remember')))
            elif kind == 'tool.deny':
                agent.deny(command.get('call_id', ''), command.get('reason', ''))
            elif kind == 'question.answer':
                agent.answer(command.get('question_id', ''), command.get('answer', ''))
            elif kind == 'policy.set':
                # The approval mode is a live control, not a property of the
                # session's birth: the thing you learn while watching an agent
                # work is exactly how much you trust it. It takes effect from
                # the next decision — a call already in flight was decided
                # under the old rule, and re-deciding it retroactively would
                # be a lie about what ran.
                #
                # The confirmation comes back through the session's own log
                # rather than as a reply on this socket, so every client
                # attached to the session sees the change, and a replay does.
                #
                # Either control may come alone; the thinking level is the
                # other live one, for the same reason.
                if command.get('mode'):
                    try:
                        await agent.set_mode(command.get('mode', ''))
                    except ValueError:
                        await ws.send_json({
                            'type': 'error',
                            'message': f'unknown approval mode: {command.get("mode")!r}',
                            'retryable': False,
                        })
                    else:
                        log.info('session %s: approval mode set to %s', agent.id, agent.policy.mode.value)
                if 'effort' in command:
                    try:
                        await agent.set_effort(command.get('effort'))
                    except ValueError as exc:
                        await ws.send_json({'type': 'error', 'message': str(exc), 'retryable': False})
                    else:
                        log.info('session %s: thinking set to %s', agent.id, agent.effort or 'default')
            elif kind == 'task.stop':
                task_id = str(command.get('task_id', ''))
                if agent.tasks is None or agent.tasks.get(task_id) is None:
                    await ws.send_json({
                        'type': 'error', 'message': f'no background task {task_id!r}', 'retryable': False,
                    })
                else:
                    # In a task of its own: stopping a server politely can
                    # take a few seconds, and this loop is also the one that
                    # delivers the next approval.
                    asyncio.create_task(agent.tasks.stop(task_id, by='person'))
            elif kind == 'turn.interrupt':
                agent.interrupt()
            elif kind == 'session.close':
                await manager.close(agent.id)
                break
            elif kind == 'ping':
                await ws.send_json({'type': 'pong', 'seq': agent.seq})
            else:
                await ws.send_json({'type': 'error', 'message': f'unknown command: {kind}', 'retryable': False})

    except WebSocketDisconnect:
        # Detach only. The session, and any turn it is running, carries on.
        log.info('session %s: client detached at seq %d', agent.id, agent.seq)
    except Exception:  # noqa: BLE001
        log.exception('agent socket failed')
    finally:
        outbound.cancel()

"""The openmirror server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from openmirror.agent.manager import manager
from openmirror.config import config
from openmirror.providers.bootstrap import bootstrap
from openmirror.providers.registry import registry
from openmirror.routers import agent as agent_router
from openmirror.routers import auth as auth_router
from openmirror.routers import autopilot as autopilot_router
from openmirror.routers import browser as browser_router
from openmirror.routers import git as git_router
from openmirror.routers import history as history_router
from openmirror.routers import mail as mail_router
from openmirror.routers import mcp as mcp_router
from openmirror.routers import media as media_router
from openmirror.routers import memory as memory_router
from openmirror.routers import providers as providers_router
from openmirror.routers import realtime as realtime_router
from openmirror.routers import search as search_router
from openmirror.routers import updates as updates_router
from openmirror.routers import voice as voice_router

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
    )
    await bootstrap(config, registry)
    manager.start_reaper()

    if config.mcp_enabled:
        from openmirror.mcp.manager import load_config
        from openmirror.mcp.manager import manager as mcp_manager

        servers = load_config(config.mcp_config)
        if servers:
            failures = await mcp_manager.start(servers)
            for name, why in failures.items():
                log.warning('mcp %s did not start: %s', name, why)
            live = len(mcp_manager.servers)
            if live:
                log.info('mcp: %d server(s), %d tool(s)', live, len(mcp_manager.tools()))

    # Media generation exists whenever something is registered that can make
    # a picture. No switch of its own: an install with no image provider
    # already cannot generate, and a second toggle saying so would only be a
    # way to have the feature off while it looks on.
    from openmirror.media.service import MediaService
    from openmirror.media.store import MediaStore

    media_router.service = MediaService(MediaStore(config.media_dir), registry)
    app.state.media = media_router.service

    if config.memory_enabled:
        from openmirror.memory.service import MemoryService
        from openmirror.memory.store import MemoryStore

        store = MemoryStore(config.memory_db)
        memory_router.service = MemoryService(store, registry, model=config.embed_model)
        app.state.memory = memory_router.service
        log.info('memory store at %s (each user opts in separately)', config.memory_db)

        # A changed embedding model has to be caught here, before anything can
        # be recalled: the old vectors do not fail against a new model, they
        # score, so an unnoticed change answers questions out of unrelated
        # geometry instead of going quiet.
        reset = memory_router.service.sync_embedder()
        if reset is not None:
            # Re-embedded in the background. It is a provider call per batch and
            # start-up must not wait on a network, so recall is briefly empty
            # and says so rather than blocking the server from listening.
            app.state.memory_reembed = asyncio.create_task(memory_router.service.reembed())
    else:
        app.state.memory = None
    # openmirror as an MCP server, for other clients. Built after memory and
    # media so it shares this process's services rather than opening a second
    # handle on the same SQLite file.
    if config.mcp_serve:
        loopback = config.host in ('127.0.0.1', 'localhost', '::1')
        if not loopback and not config.mcp_serve_token:
            # Refused rather than warned. This endpoint hands out a person's
            # memory, and an unauthenticated one on a network interface is a
            # different class of mistake from an unauthenticated agent endpoint.
            log.error(
                'OPENMIRROR_MCP_SERVE is on and this server is bound to %s, but no '
                'OPENMIRROR_MCP_SERVE_TOKEN is set. The MCP endpoint will not be served. '
                'Set a token, or bind to loopback.',
                config.host,
            )
        else:
            from openmirror.mcp.server import from_config

            mcp_router.server = await from_config(app=app)
            log.info(
                'serving MCP at /mcp (scope: %s, token: %s)',
                config.mcp_serve_scope, 'yes' if config.mcp_serve_token else 'no',
            )

    # This person's settings file, over the environment. A *project's* file is
    # applied per session, in `SessionManager.create`, on a copy — a project
    # belongs to a project and the daemon serves many at once.
    try:
        from openmirror.config import apply_settings

        found = apply_settings()
        for path in found.sources:
            log.info('settings: %s', path)
        for source, key, why in found.refused:
            log.warning('settings: %s asked for %s and was refused — %s', source, key, why)
    except Exception:  # noqa: BLE001 - a bad settings file must not stop the daemon
        log.exception('settings could not be read; continuing with the environment')

    log.info('workspace: %s   approval: %s', config.workspace, config.approval_mode)
    if not config.auth_token and not auth_router.is_loopback(config.host):
        # Refused rather than warned. This process runs commands on this
        # machine: an unauthenticated one on a network interface means any
        # other machine on the network can create a session in `unrestricted`
        # and start shelling out as the person who installed it. A warning in
        # a log file is not a boundary, and the default (loopback, no token)
        # never reaches this.
        raise SystemExit(
            f'openmirror is bound to {config.host} with no OPENMIRROR_TOKEN set. '
            'Refusing to serve an unauthenticated agent on a network interface — '
            'set OPENMIRROR_TOKEN, or bind to 127.0.0.1.'
        )

    # Asked in the background and never awaited, so a slow or unreachable
    # GitHub cannot delay the window appearing — which is the whole point of
    # a start-up check. `check_on_start` enforces its own conditions: only
    # when the flag is on, never while something is running, and never when
    # `local_only` is set, because somebody who asked for no remote traffic
    # did not ask for a version check either.
    async def _update_check() -> None:
        from openmirror.routers.updates import check_on_start

        try:
            await check_on_start()
        except Exception:  # noqa: BLE001
            # A version check that raises is not worth failing a start-up
            # over, and there is nothing useful to do about it here.
            log.debug('the update check failed', exc_info=True)

    asyncio.create_task(_update_check())

    yield

    # Cancelled rather than awaited: it is a rebuild of derived data and the
    # rows it did not reach are still pending, so the next start-up finishes
    # the job. Holding shutdown open for a provider call would not.
    task = getattr(app.state, 'memory_reembed', None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    manager.stop_reaper()
    await manager.close_all()

    from openmirror.mcp.manager import manager as mcp_manager

    await mcp_manager.stop()
    await mcp_router.shutdown()

    # The shared Chromium the headless search backend keeps, if anything
    # searched. Reaped on idle anyway; closed here so a stop does not wait for
    # the idle timer.
    from openmirror.agent.headless_search import shutdown as close_search_browser

    await close_search_browser()


app = FastAPI(title='openmirror', version='0.1.0', lifespan=lifespan)

# The token is enforced here, once, as middleware — before routing decides
# anything, so a route added next year is covered without anyone remembering.
#
# Not `include_router(..., dependencies=[...])`: on the FastAPI this ships with
# that is silently dropped and the endpoint runs anyway, which is worse than no
# guard at all because the code reads as though it is protected. `auth.py` says
# the same thing at greater length.
app.add_middleware(auth_router.TokenMiddleware)

# The sign-in exchange, and nothing else, is reachable unauthenticated. It is
# the route that takes a token, so requiring one to reach it is circular.
app.include_router(auth_router.router)

# The MCP endpoint is exempt from the token above and keeps its own stricter
# `OPENMIRROR_MCP_SERVE_TOKEN` check, which already refuses to mount on a
# non-loopback bind without one. It is in the router's own prefix exemption.
app.include_router(agent_router.router)
# The session REST surface, mounted under the same middleware as everything
# else: create/list, the toolset listing, slash commands, background tasks,
# and rewind. It is a second router rather than part of the websocket one
# because the websockets are streams and these are ordinary request/response
# calls, and because a browser tab that wants the session list should not have
# to open a socket to ask for it.
app.include_router(agent_router.http)
# Search across stored conversations, and a shareable copy of one. Mounted next
# to the session surface above because that is what it extends: the same
# `/api/sessions` prefix, the same stored transcripts, the same 404 for an id
# nothing has heard of.
app.include_router(history_router.router)
app.include_router(voice_router.router)
app.include_router(providers_router.router)
app.include_router(memory_router.router)
app.include_router(media_router.router)
app.include_router(search_router.router)
app.include_router(browser_router.router)
app.include_router(updates_router.router)
app.include_router(git_router.router)
app.include_router(mail_router.router)
app.include_router(mcp_router.router)
app.include_router(realtime_router.router)
app.include_router(autopilot_router.router)
app.include_router(autopilot_router.http)


STATIC = Path(__file__).parent / 'static'


class Revalidated(StaticFiles):
    """Assets a browser checks with us before reusing.

    With no `Cache-Control` header a browser invents a freshness window from
    the file's age — Chromium takes a tenth of it — and reuses the page's
    JavaScript without asking. That is how an updated interface goes on running
    last week's code until somebody thinks to hard-refresh, and the bug gets
    blamed on the change that appeared to do nothing. It cost an afternoon
    here: a fix was live on the server and the page in front of it still had
    the old handler.

    `no-cache` does not mean "do not cache". The copy is kept and revalidated,
    and an unchanged file comes back as a 304 with no body — which these
    already answer correctly, by ETag.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault('Cache-Control', 'no-cache')
        return response


app.mount('/static', Revalidated(directory=STATIC), name='static')


@app.get('/')
async def index() -> FileResponse:
    # The page that loads those assets must not be a stale copy either.
    return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/healthz')
async def healthz() -> dict[str, object]:
    return {
        'ok': True,
        'service': 'openmirror',
        'providers': [p.id for p in registry.list()],
        'capabilities': registry.capabilities(local_only=config.local_only),
    }


def main() -> None:
    """The command line — or the daemon, when nobody asked for one.

    Kept as a no-argument function because two callers depend on exactly that
    shape: the desktop app's frozen sidecar imports this one and calls it, and
    `daemon.rs` spawns the binary with an empty argv as well as
    `python3 -m openmirror.main`. With no arguments the CLI dispatches straight
    to the server, so both of them get precisely the behaviour they got before
    a command line existed.

    The work lives in `openmirror.cli` rather than here for two reasons: a
    command that parses flags should not have to import FastAPI to answer
    `--version`, and a dispatch table that starts a server is not something a
    test can exercise without starting one.
    """
    from openmirror.cli import main as cli_main

    raise SystemExit(cli_main())


def _exit_when_stdin_closes(server) -> None:
    """Stop once the other end of stdin is gone.

    The desktop app's way of saying "I am gone" — quit, crash or killed alike,
    because the operating system closes the pipe in every case. It is the same
    convention the daemon's own MCP servers follow with it. Without this a
    killed app leaves a daemon holding the port, and the next launch attaches
    to it as though somebody else had started it, so nothing ever stops it.
    """
    # The descriptor, not `sys.stdin`. A thread parked in a buffered read holds
    # that object's lock, and an interpreter exiting for any other reason — a
    # port already taken — then aborts trying to take it at shutdown.
    try:
        fd = sys.stdin.fileno()
        while os.read(fd, 1024):
            pass
    except (AttributeError, OSError, ValueError):
        # No stdin at all, or one that cannot be read: nobody on the other end.
        pass
    log.info('stdin closed: shutting down')
    server.should_exit = True


if __name__ == '__main__':
    main()

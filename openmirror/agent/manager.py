"""Sessions that outlive the client watching them.

A session is a unit of work, not a connection. Closing a tab must not kill an
agent four minutes into a build, and reopening one must not start over — so
sessions live here, clients attach and detach, and the event log in each
session is what makes reattaching cheap.

That has a consequence worth being explicit about: a session with nobody
attached can still be *waiting* on somebody. An approval has no timeout by
design, so a suspended session sits there indefinitely. The reaper below
therefore never closes a session that is waiting on a human, only ones that
have gone quiet on their own.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from openmirror.agent.approval import Mode
from openmirror.agent.runtime import build_session
from openmirror.agent.session import AgentSession
from openmirror.mcp.manager import manager as mcp_manager

log = logging.getLogger(__name__)

# A session nobody has touched for this long, that is not running and not
# waiting on anyone, is closed. Generous: the cost of keeping one is a little
# memory, and the cost of reaping one someone wanted is their work.
IDLE_TIMEOUT = 12 * 60 * 60
REAP_INTERVAL = 300


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._reaper: asyncio.Task[None] | None = None
        # Where conversations are written. Injectable, because a test that
        # reaches for the process-wide default is testing whatever happens to
        # be in `data/`.
        self._store: Any = None

    def store(self) -> Any:
        """The transcript store, or None when there is nowhere to write one."""
        if self._store is not None:
            return self._store
        from openmirror.agent.runtime import _default_store

        self._store = _default_store()
        return self._store

    def stored(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Every conversation on disk, newest first.

        Separate from `list()` on purpose: `list()` is what the sidebar
        shows, and it is the *live* sessions. These are the ones that survive a
        restart, which after a restart is all of them and before one is most of
        them not being there yet.
        """
        store = self.store()
        return store.list(limit=limit) if store is not None else []

    async def resume(self, session_id: str, **kwargs: Any) -> AgentSession | None:
        """Reopen a stored conversation as a live session.

        Rebuilt rather than rehydrated: the tools, the provider and the policy
        are built as they would have been for the first time, and the stored
        messages are put back into the result. A transcript is a record, and
        the point of reopening one is the conversation, not the process that
        produced it.

        The root, model and toolset come from the transcript rather than from
        the caller, because a conversation about a different project reopened
        in this one is a conversation that will confidently edit the wrong
        files.
        """
        store = self.store()
        if store is None:
            return None
        try:
            stored = store.load(session_id)
        except Exception:  # noqa: BLE001
            log.exception('session %s could not be reopened', session_id)
            return None
        if stored is None:
            return None
        from openmirror.config import config as _cfg

        # Popped rather than read: every one of these is named explicitly
        # below, and `**kwargs` alongside would say it twice. The first
        # version of this read four of them and the call raised on the first.
        provider = kwargs.pop('provider')
        mode = kwargs.pop('mode', Mode.ASK)
        model = stored.model or kwargs.pop('model', '')
        toolset = list(stored.toolset) or kwargs.pop('toolset', None)
        kwargs.pop('model', None)
        kwargs.pop('toolset', None)
        return await self.create(
            root=stored.root or kwargs.pop('root', _cfg.workspace),
            provider=provider,
            model=model,
            mode=mode,
            session_id=stored.id,
            title=stored.title,
            toolset=toolset,
            **kwargs,
        )

    async def fork(self, session_id: str, at: int = 0, **kwargs: Any) -> AgentSession | None:
        """A new session, holding the conversation up to a point.

        `at` is a message number, counting the user and assistant turns from
        one, so `at: 4` is "everything up to the fourth message". Both harnesses
        have this and it earns its keep in one specific situation: you asked
        for the wrong thing, the agent is halfway down a path you no longer
        want, and `/clear` throws away the *context* that told you what to
        change your mind about.

        The new session gets a new id, the old one is left completely alone,
        and the files on disk are shared — a fork is a different
        *conversation*, not a different working tree. For that, see
        `openmirror.agent.worktree`.
        """
        store = self.store()
        if store is None:
            return None
        try:
            source = store.load(session_id)
        except Exception:  # noqa: BLE001
            log.exception('session %s could not be forked', session_id)
            return None
        if source is None:
            return None

        cut = max(0, int(at or 0))
        if cut and cut < len(source.messages):
            source.messages = source.messages[:cut]
        # A fork is finished by definition, and a title that says where it
        # came from is the difference between two similar conversations in a
        # list and two unrelated ones.
        source.title = f"{source.title or session_id} (fork)" if not source.title.endswith('(fork)') \
            else source.title
        source.id = uuid.uuid4().hex[:16]
        # Written under the new id *before* the session is built, so a crash
        # between the two leaves a listed conversation rather than a live
        # session with no transcript behind it.
        store.save(source)
        return await self.resume(source.id, **kwargs)

    async def create(
        self,
        *,
        root: str | Path,
        provider: Any,
        model: str,
        mode: Mode | str,
        effort: str | None = None,
        session_id: str | None = None,
        title: str = '',
        memory: Any = None,
        user_id: str = '',
        media: Any = None,
        toolset: list[str] | None = None,
        cfg: Any = None,
    ) -> AgentSession:
        from openmirror.config import config as default_config
        from openmirror.settings import for_root

        # This project's settings, on a *copy*. A project file belongs to a
        # project, and the daemon serves many at once — applying one to the
        # shared config would let a repository change what happens in
        # somebody else's session, which is the hole the narrowing rules exist
        # to close one level below.
        cfg = for_root(root, cfg or default_config)

        # The stage comes first, because the browser wants to be launched onto
        # it. Built eagerly rather than lazily: it is an X server, starting one
        # takes a second, and doing that inside a tool's synchronous `assess`
        # would block the loop at the worst possible moment. Desktop control is
        # off by default, so only installs that asked for it pay anything.
        stage = None
        if cfg.desktop_enabled:
            from openmirror.agent import stage as stage_mod

            try:
                stage = stage_mod.build(cfg)
            except stage_mod.StageUnavailable as exc:
                # Not fatal. A session with no hands is still a session that
                # can read, write and run commands, and failing to create it
                # would be a worse answer than creating it without a screen.
                log.warning('desktop control is on but no stage could be made: %s', exc)

        browser = None
        if cfg.browser_enabled:
            from openmirror.agent.browser import BrowserConfig, BrowserSession

            # Constructed, not started: launching Chromium takes a few hundred
            # milliseconds and most sessions never open a page.
            #
            # On a stage of its own the browser is launched *headful* whatever
            # the setting says, and the setting is not being ignored: headless
            # exists to keep a browser off your screen, and a display nobody is
            # looking at already does that. Headful there is strictly better —
            # the desktop tools can see it, sites that fingerprint headless
            # Chromium behave, and it still cannot take your focus.
            on_stage = stage is not None and not stage.shares_pointer
            browser = BrowserSession(
                BrowserConfig(
                    profile_dir=cfg.browser_profile,
                    headless=False if on_stage else cfg.browser_headless,
                    viewport=(stage.rect().width, stage.rect().height) if on_stage else (1280, 900),
                    env=stage.env() if stage is not None else {},
                )
            )

        checkpoints = None
        if cfg.checkpoints_enabled:
            from openmirror.agent.checkpoint import CheckpointStore

            # Under the data directory, never inside the working root — a
            # snapshot in the tree the agent is editing gets read, grepped and
            # eventually committed.
            checkpoints = CheckpointStore(
                Path(cfg.memory_db).parent / 'checkpoints' / (session_id or 'session')
            )

        # Which language servers this machine has. Looked up per session
        # rather than once, so installing one does not need a restart to be
        # noticed; it is a handful of PATH lookups.
        lsp = None
        if cfg.lsp_enabled:
            from openmirror.agent.lsp import find_servers

            lsp = find_servers(cfg.lsp_config) or None

        session = build_session(
            root=root, provider=provider, model=model, mode=mode, effort=effort, session_id=session_id,
            title=title or Path(root).name, memory=memory, user_id=user_id,
            confined=not cfg.unconfined,
            extra_dirs=list(getattr(cfg, "extra_dirs", []) or []),
            allow_purchases=cfg.allow_purchases,
            allow_credentials=cfg.allow_credentials,
            allow_messages=cfg.allow_messages,
            mcp=mcp_manager if cfg.mcp_enabled and mcp_manager.servers else None,
            checkpoints=checkpoints,
            web=cfg if cfg.web_enabled else None,
            mail=cfg if cfg.mail_enabled else None,
            calendar=cfg if cfg.calendar_enabled else None,
            hr=cfg if cfg.hr_enabled else None,
            browser=browser,
            stage=stage,
            media=media,
            system=cfg.system_tools_enabled,
            toolset=toolset,
            agents=cfg.agents_enabled,
            skills=cfg.skills_enabled,
            lsp=lsp,
            compact_at=cfg.compact_at,
            home=Path.home(),
        )
        self._sessions[session.id] = session
        await session.start()
        log.info('session %s created at %s', session.id, session.root)
        return session

    def get(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    def list(self) -> list[dict[str, Any]]:
        """A summary per session, for a client showing what is in flight."""
        return [
            {
                'id': s.id,
                'title': s.title,
                'root': str(s.root),
                'model': s.model,
                'policy': s.policy.mode.value,
                'busy': s.busy,
                'waiting_on': s.waiting_on,
                'background': len(s.tasks.running) if s.tasks is not None else 0,
                'attached': s.attached,
                'seq': s.seq,
                'turns': sum(1 for m in s.messages if m.role == 'user'),
                'idle_for': int(time.time() - s.last_active),
                'closed': s.closed,
            }
            for s in self._sessions.values()
        ]

    async def close(self, session_id: str, reason: str = 'closed') -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close(reason)
        return True

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id, 'server shutting down')

    # -- reaping ------------------------------------------------------------

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    def stop_reaper(self) -> None:
        if self._reaper and not self._reaper.done():
            self._reaper.cancel()

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REAP_INTERVAL)
                await self.reap()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception('session reaper failed')

    async def reap(self) -> list[str]:
        """Close sessions that have gone quiet. Returns what was closed."""
        now = time.time()
        doomed = [
            s.id
            for s in self._sessions.values()
            # Never a session someone is expected to answer: it has been
            # waiting precisely because nobody has got to it yet.
            if not s.busy and s.waiting_on is None and s.attached == 0
            and now - s.last_active > IDLE_TIMEOUT
        ]
        for session_id in doomed:
            log.info('reaping idle session %s', session_id)
            await self.close(session_id, 'idle')
        return doomed


manager = SessionManager()

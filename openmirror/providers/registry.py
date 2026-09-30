"""Which provider answers which modality, for whom.

The brief was that a person can choose local, OpenAI or Anthropic separately
for every kind of generation. That is this file. A route is a
(modality → provider, model) pair, resolved in three layers:

    per-user override  →  workspace default  →  first provider that can do it

so an admin sets a sane default once and a user who wants their dictation to
stay on their own hardware changes one route without touching the rest.

The privacy posture is enforced here rather than in the UI, because a toggle
that only hides an option is not a guarantee. `local_only` on a route set
means a remote provider is refused at resolution time even if something
downstream asks for one by name, and the refusal is an error rather than a
silent fallback — quietly sending audio to a hosted service because the local
one was down is precisely the failure this is meant to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

# The errors live in `base` so the adapters can raise them without importing
# the registry; they are re-exported here because this is where every caller
# has always imported them from.
from openmirror.providers.base import (  # noqa: F401 - NoProviderError and friends are re-exported
    Modality,
    NoProviderError,
    PrivacyRefusal,
    ProviderInfo,
    ProviderRefused,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Route:
    provider: str
    model: str = ''
    # Per-route options: a voice for TTS, a size for images, a sampler.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouteSet:
    """One person's, or one workspace's, choice per modality."""

    routes: dict[Modality, Route] = field(default_factory=dict)
    # Off by default. When on, every modality must resolve to a provider
    # marked local, and there is no fallback out of it.
    local_only: bool = False

    def get(self, modality: Modality) -> Route | None:
        return self.routes.get(modality)


@dataclass(slots=True)
class _Entry:
    info: ProviderInfo
    # One object may implement several capabilities — OpenAICompatProvider
    # does chat, embeddings, speech both ways and images — so instances are
    # stored per modality rather than per provider.
    impls: dict[Modality, Any]
    # The stored connection this was built from, when it came from one.
    # Environment-configured providers have none, which is also how the UI
    # knows which entries it may edit and which are pinned by the install.
    connection: Any = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._defaults = RouteSet()

    # -- registration -------------------------------------------------------

    def register(self, info: ProviderInfo, impls: dict[Modality, Any], connection: Any = None) -> None:
        # Registering the same id twice replaces it, so reconfiguring a
        # connection at runtime does not need a restart or a deregister call.
        self._entries[info.id] = _Entry(info=info, impls=dict(impls), connection=connection)
        carried = ' over tor' if connection is not None and getattr(connection, 'tor', False) else ''
        log.info(
            'provider %s registered for %s%s', info.id, ','.join(sorted(m.value for m in impls)), carried
        )

    def unregister(self, provider_id: str) -> None:
        self._entries.pop(provider_id, None)

    def register_perch(self, cfg: Any, available: dict[str, bool], connection: Any = None) -> list[str]:
        """Register one Perch host as its live services.

        Each service becomes its own provider id rather than one 'perch'
        entry, so someone can route chat at Perch and images at a hosted
        service without the two decisions being tangled.

        `connection` is the stored connection this came from, when it came from
        one. Every service carries it, which is what makes all of them editable
        in the UI and lets any one of their rows remove the lot.

        A service that is now down is unregistered, not left as it was. Turning
        video off in Perch and reconnecting would otherwise keep offering video
        from the previous probe, which is the dead-port offer `probe` exists to
        avoid.
        """
        from openmirror.providers import perch as perch_mod

        impls = perch_mod.build(cfg, available)
        registered: list[str] = []
        for info in perch_mod.describe(cfg, available):
            service = info.id.split(':', 1)[1]
            mine = {m: impl for m, impl in impls.items() if m in perch_mod.SERVICE_MODALITIES[service]}
            if not mine:
                continue
            self.register(info, mine, connection=connection)
            registered.append(info.id)
        for service, up in available.items():
            if not up:
                self.unregister(f'perch:{service}')
        return registered

    # -- inspection ---------------------------------------------------------

    def list(self) -> list[ProviderInfo]:
        return [e.info for e in self._entries.values()]

    def providers_for(self, modality: Modality, *, local_only: bool = False) -> list[ProviderInfo]:
        return [
            e.info
            for e in self._entries.values()
            if modality in e.impls and (not local_only or e.info.local)
        ]

    def connection(self, provider_id: str) -> Any:
        """The stored connection behind a provider, or None if it came from the
        environment. Used to decide whether a UI may offer to edit it."""
        entry = self._entries.get(provider_id)
        return entry.connection if entry else None

    def impl(self, provider_id: str, modality: Modality) -> Any:
        """The object serving one modality for one provider, without routing.

        For asking a specific provider what models it has — a question about
        that connection rather than about which one should answer next.
        """
        entry = self._entries.get(provider_id)
        if entry is None:
            raise NoProviderError(f'no such provider: {provider_id}')
        if modality not in entry.impls:
            raise NoProviderError(f'{provider_id} is not registered for {modality.value}')
        return entry.impls[modality]

    def describe(self, provider_id: str) -> dict[str, Any]:
        """Everything a client should know about one provider, keys excluded."""
        entry = self._entries.get(provider_id)
        if entry is None:
            raise NoProviderError(f'no such provider: {provider_id}')
        conn = entry.connection
        return {
            'id': entry.info.id,
            'label': entry.info.label,
            'local': entry.info.local,
            'base_url': entry.info.base_url,
            'modalities': sorted(m.value for m in entry.impls),
            'editable': conn is not None,
            # Which stored connection to test or remove for this row. Usually
            # the provider's own id; for Perch it is the one connection behind
            # up to five providers, none of which share its id.
            'connection_id': getattr(conn, 'id', '') or '',
            'tor': bool(getattr(conn, 'tor', False)),
            'host_id': getattr(conn, 'host_id', ''),
            'has_key': bool(getattr(conn, 'api_key', '')),
        }

    def capabilities(self, *, local_only: bool = False) -> dict[str, list[str]]:
        """What this install can do, by modality — the answer a UI needs to
        grey out what is genuinely unavailable rather than offering it and
        failing later."""
        out: dict[str, list[str]] = {}
        for modality in Modality:
            found = self.providers_for(modality, local_only=local_only)
            if found:
                out[modality.value] = [p.id for p in found]
        return out

    # -- defaults -----------------------------------------------------------

    def set_defaults(self, routes: RouteSet) -> None:
        self._defaults = routes

    @property
    def defaults(self) -> RouteSet:
        return self._defaults

    # -- resolution ---------------------------------------------------------

    def resolve(self, modality: Modality, user_routes: RouteSet | None = None) -> tuple[Any, Route, ProviderInfo]:
        """Pick the provider for this modality, honouring the user's rules.

        Raises rather than falling back when a named provider is missing: a
        person who chose a specific model wants to hear that it is gone, not
        to be quietly served by another one.
        """
        local_only = (user_routes.local_only if user_routes else False) or self._defaults.local_only

        default = self._defaults.get(modality)
        route = (user_routes.get(modality) if user_routes else None) or default

        # Naming the provider the install already routes to is not asking for a
        # different model on it. The studio sends its provider picker with every
        # request, and a voice or agent client may name one too; without this,
        # each was served that provider's first-listed model instead of the one
        # configured — on a gateway, whatever it happened to list first.
        if (
            route is not None and default is not None and route is not default
            and not route.model and route.provider == default.provider
        ):
            route = Route(provider=route.provider, model=default.model, options={**default.options, **route.options})

        if route is not None:
            entry = self._entries.get(route.provider)
            if entry is None or modality not in entry.impls:
                raise NoProviderError(
                    f'{modality.value}: provider {route.provider!r} is not registered for it'
                )
            if local_only and not entry.info.local:
                raise PrivacyRefusal(
                    f'{modality.value}: {route.provider!r} is remote and this session is set to local only'
                )
            return entry.impls[modality], route, entry.info

        # Nothing chosen: take the first that can do it. Locals are preferred
        # even when local_only is off, so the default behaviour of an install
        # with a GPU attached is to use it.
        candidates = sorted(
            (e for e in self._entries.values() if modality in e.impls and (not local_only or e.info.local)),
            key=lambda e: (not e.info.local, e.info.id),
        )
        if not candidates:
            hint = ' (no local provider is configured for it)' if local_only else ''
            raise NoProviderError(f'{modality.value}: nothing is configured to handle it{hint}')

        chosen = candidates[0]
        return chosen.impls[modality], Route(provider=chosen.info.id), chosen.info


# What a model has to be able to do to serve each modality. Ollama is the only
# backend that reports this today; everything else returns bare ids, which is
# why an empty capability list means "no idea, assume it can" rather than "no".
_NEEDED = {
    Modality.CHAT: ('completion', 'tools'),
    Modality.EMBEDDING: ('embedding',),
}


def can_serve(model: dict[str, Any], modality: Modality) -> bool:
    """Whether a listed model can serve this modality, as far as anyone knows."""
    declared = model.get('capabilities') or []
    if not declared:
        return True
    wanted = _NEEDED.get(modality)
    if not wanted:
        return True
    return any(c in declared for c in wanted)


def pick_model(models: list[dict[str, Any]], modality: Modality, *, need_tools: bool = False) -> str:
    """The best of a provider's models for a job, when nobody named one.

    Two failures this exists to stop, both seen on a real install:

    An embedding model chosen for chat. `/api/tags` lists everything that is
    pulled and the first entry is alphabetical, so an install with
    `qwen3-embedding:4b` on it answered every conversation with an HTTP 400
    saying that model does not support chat — which reads as a broken daemon
    rather than as a bad default.

    A chat model that cannot call tools chosen for an agent session. It
    connects, it talks, and it can do nothing at all, which is a much more
    confusing failure than not starting.
    """
    usable = [m for m in models if m.get('id') and can_serve(m, modality)]
    if not usable:
        return ''
    if need_tools:
        with_tools = [m for m in usable if 'tools' in (m.get('capabilities') or [])]
        # Only when something declared it. A provider that reports no
        # capabilities at all must not be narrowed to nothing.
        if with_tools:
            usable = with_tools
    return str(usable[0]['id'])


async def resolve_chat(provider: str | None = None, model: str | None = None) -> tuple[Any, str, str]:
    """Pick the chat provider and a concrete model for one conversation.

    Lives here rather than in a router because four callers need it and they
    are not all HTTP: creating a session, drafting a commit message, drafting
    a mail reply, and the ``/model`` command — which runs inside the agent
    loop, where importing a router would invert the layers. A second copy of
    "or ask the provider what it has" is a second copy of the reason the
    fallback exists, and one of them will be the one that forgets.

    Naming a provider without a model is the common case from ``/model``: the
    person picked "anthropic" and means "the one it thinks is best", not the
    first model that install happened to list.
    """
    from openmirror.config import config

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
        # does not support chat", and the fix was picking on capability.
        chosen = pick_model(available, Modality.CHAT, need_tools=True)
        if not chosen:
            names = ', '.join(str(m.get('id')) for m in available[:8])
            raise NoProviderError(
                f'{info.id} has no model that can hold a conversation. It offers: {names}'
            )
    return impl, chosen, info.id


# Process-wide registry. Deliberately a module global: providers are
# configuration, every request reads the same set, and threading one through
# every call site buys nothing.
registry = ProviderRegistry()

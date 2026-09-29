"""A settings file, and what it is allowed to say.

Claude Code has `settings.json` in four places with a documented precedence
order. openCode has `opencode.json` merged across eight tiers, plus a separate
`tui.json`. This project had neither: every knob was an environment variable,
which is fine for a container and wrong for a person with a laptop, a
preferred model and an opinion about which projects may be written to.

**Merged, never replaced.** Later files add to earlier ones rather than
overwriting them, so a project can add one setting without restating the
six a person configured globally. A list of tools is *replaced* — a union would
mean a project could not take a tool *away* — and everything else is merged
one level deep, which is deep enough for what anybody actually writes.

**The order, and why it is this order.** From most general to most specific,
with the environment last because it is how a container says something:

    ~/.openmirror/settings.json          this person's, everywhere
    <root>/.openmirror/settings.json      this project's
    <root>/.openmirror.json              ditto, the name opencode uses
    <root>/.claude/settings.json         ditto, the name Claude Code uses
    OPENMIRROR_CONFIG                     a file named by a variable
    the environment                       last, and it wins

**A project's settings can narrow permissions and never widen them.** The
precedence order decides *values*; it does not decide *authority*, and those
are different questions. A project file cannot raise the approval mode above
what the person set globally, cannot turn `local_only` off, cannot allow
purchases, and cannot point a provider at a different key. A repository that
ships a `settings.json` is a repository that runs code — its hooks — and
making it also able to raise its own safety settings would be the whole
problem again. The settings that may be narrowed are enumerated rather than
patterned, so what a project *can* do is a list somebody can read.

**Unknown keys are reported, not ignored.** A typo in a settings file is
invisible otherwise, and a person who set `approval-mode: ask` and got
`approval_mode` would have a machine that quietly ignored them.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Where a project's settings may live. All three names, because a person
# arriving from Claude Code or from openCode will have written the one they
# know, and being told to rename a file is how a feature goes unused.
PROJECT_NAMES = ('.openmirror/settings.json', '.openmirror.json', '.claude/settings.json')
PERSONAL = Path.home() / '.openmirror' / 'settings.json'

#: Settings a *project* file may lower, and never raise. Anything not here
#: cannot be set by a repository at all.
#:
#: The value is the **tighter** one, which is not always the same value.
#: `allow_purchases` tightens to `false`; `local_only` tightens to `true`; the
#: approval mode tightens by going *down* the ladder. Getting that wrong in one
#: place means a project can widen exactly the thing the rule exists to close,
#: and it is the kind of wrong that reads as working.
PROJECT_CAN_NARROW: dict[str, Any] = {
    'approval_mode': 'down the ladder: read_only < plan < ask < auto_edit < trusted < unrestricted',
    'update_check_enabled': False,
    'local_only': True,
    'allow_purchases': False,
    'allow_credentials': False,
    'allow_messages': False,
    'desktop_enabled': False,
    'system_tools_enabled': False,
    'hr_enabled': False,
    'mail_enabled': False,
    'calendar_enabled': False,
    'unconfined': False,
}

#: Settings that only ever narrow, because "narrower" is the only direction
#: that means anything. A project cannot raise a number of steps, and a
#: project cannot ask for a more capable model.
PROJECT_CAN_ONLY_LOWER: set[str] = set()

#: The order of the modes, so "lower" means something.
_MODE_ORDER = ('read_only', 'plan', 'ask', 'auto_edit', 'trusted', 'unrestricted')

#: Lists replace rather than merge, because a union would mean a project could
#: not take a tool *away*.
REPLACE_LISTS = {'toolset', 'default_commands'}

#: Renamed for people arriving with a `.env` or a settings file from
#: elsewhere. Both spellings are accepted and neither is invented later.
ALIASES = {
    'approval-mode': 'approval_mode',
    'allow-purchases': 'allow_purchases',
    'allow-credentials': 'allow_credentials',
    'allow-messages': 'allow_messages',
    'update-check': 'update_check',
    'local-only': 'local_only',
    'workspace': 'workspace',
    'memory': 'memory_enabled',
    'tools': 'toolset',
    'data-dir': 'data_dir',
    'log-level': 'log_level',
}

#: Everything `Config` knows about. A key that is not here is reported.
KNOWN = {
    'host', 'port', 'workspace', 'approval_mode', 'unconfined', 'allow_purchases', 'allow_credentials',
    'allow_messages', 'web_enabled', 'web_allow_private', 'search_backend', 'search_key', 'search_url',
    'search_engine', 'checkpoints_enabled', 'agents_enabled', 'skills_enabled', 'lsp_enabled', 'lsp_config',
    'context_window', 'compact_at', 'mcp_config', 'mcp_enabled', 'mcp_serve', 'mcp_serve_token',
    'mcp_serve_scope', 'desktop_enabled', 'desktop_stage', 'desktop_monitor', 'desktop_display',
    'desktop_virtual_size', 'desktop_yield_to_user', 'system_tools_enabled', 'media_dir', 'image_timeout',
    'video_timeout', 'realtime_model', 'realtime_voice', 'realtime_url', 'browser_enabled',
    'browser_profile', 'browser_headless', 'browser_engine', 'browser_channel', 'browser_executable',
    'hr_enabled', 'hr_db', 'mail_enabled', 'mail_accounts', 'mail_address', 'mail_password_env',
    'mail_imap_host', 'mail_smtp_host', 'mail_protocol', 'calendar_enabled', 'calendar_dir',
    'calendar_hours_start', 'calendar_hours_end', 'update_check_enabled', 'update_check_on_start',
    'update_staging', 'hooks_enabled', 'hooks_allow_untrusted', 'hooks_timeout', 'auth_token',
    'anthropic_key', 'anthropic_url', 'openai_key', 'openai_url', 'ollama_url', 'perch_host', 'perch_token',
    'perch_scheme', 'openwebui_url', 'openwebui_key', 'chat_provider', 'embed_provider',
    'embed_dimensions', 'stt_provider', 'tts_provider', 'image_provider', 'image_model', 'video_provider',
    'video_model', 'default_chat_model', 'default_stt_model', 'default_tts_model', 'default_tts_voice',
    'local_only', 'extra_dirs', 'worktrees_enabled', 'sessions_dir', 'memory_enabled', 'data_dir', 'connections_db', 'memory_db', 'embed_model',
    'default_user', 'log_level', 'exit_with_stdin',
    # Not `Config` fields, and deliberately: `toolset` narrows a session and
    # `provider` picks a connection for one request. Both are accepted here so
    # a file can carry them, and neither has a home to fall back to.
    'toolset', 'provider',
}


class SettingsError(RuntimeError):
    """A settings file could not be used."""


@dataclass(slots=True)
class Settings:
    """What the files said, and what was wrong with them."""

    values: dict[str, Any] = field(default_factory=dict)
    #: (path, key) for every unknown key, so the interface can say so.
    unknown: list[tuple[str, str]] = field(default_factory=list)
    #: (path, key, why) for every thing a project tried to do that it may not.
    refused: list[tuple[str, str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            'values': self.values,
            'unknown': [{'file': f, 'key': k} for f, k in self.unknown],
            'refused': [{'file': f, 'key': k, 'why': w} for f, k, w in self.refused],
            'sources': self.sources,
        }


def paths(root: Path) -> list[Path]:
    """Every file that could apply, most general first."""
    found: list[Path] = []
    if PERSONAL.is_file():
        found.append(PERSONAL)
    for name in PROJECT_NAMES:
        candidate = Path(root) / name
        if candidate.is_file():
            found.append(candidate)
    named = os.getenv('OPENMIRROR_CONFIG', '').strip()
    if named:
        candidate = Path(named).expanduser()
        if candidate.is_file():
            found.append(candidate)
    return found


def load(root: Path | str = '.', *, environ: dict[str, str] | None = None) -> Settings:
    """Read every file that applies and merge them.

    The environment is *not* applied here. It already wins, because
    `Config` reads `os.environ` and this only fills in what was not set — and
    keeping that in one place (`apply`) is what stops a settings file from
    quietly beating a variable somebody exported in a shell.
    """
    env = environ if environ is not None else os.environ
    found = Settings()

    files = paths(Path(root))
    project_files = [p for p in files if p != PERSONAL]
    personal_files = [p for p in files if p == PERSONAL]

    def read(path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            found.unknown.append((str(path), f'(unreadable: {exc})'))
            return None
        if not isinstance(raw, dict):
            found.unknown.append((str(path), '(not an object)'))
            return None
        return raw

    # **The environment first, as the base.** It is how a container says
    # something, and everything after this can lower it but not raise it —
    # except a *person's* own file, which is the one thing that may.
    for key in sorted(KNOWN):
        variable = _variable_for(key)
        if not variable or variable == 'OPENMIRROR_CONFIG':
            continue
        if variable in env:
            found.values[key] = _coerce(env[variable], _types().get(key))

    for path in personal_files:
        raw = read(path)
        if raw is None:
            continue
        found.sources.append(str(path))
        _merge(found, raw, str(path), personal=True)

    # And the project's last, where it may only *narrow*. This ordering is the
    # whole point and getting it wrong is invisible: the first version let the
    # environment overwrite a project's `read_only` on the way past, which is
    # the exact hole the rule exists to close, one layer up.
    narrowed: set[str] = set()
    for path in project_files:
        raw = read(path)
        if raw is None:
            continue
        found.sources.append(str(path))
        _merge(found, raw, str(path), personal=False, narrowed=narrowed)

    # And the environment last, for everything the project did not narrow.
    #
    # Two rules that look contradictory and are not: the environment wins a
    # plain setting, because it is how a container says something; and a
    # project still gets to *lower* a safety setting, because that is an
    # authority rule rather than a precedence one. So the final pass skips
    # whatever the project tightened.
    for key in sorted(KNOWN):
        if key in narrowed:
            continue
        variable = _variable_for(key)
        if not variable or variable == 'OPENMIRROR_CONFIG':
            continue
        if variable in env:
            found.values[key] = _coerce(env[variable], _types().get(key))
    return found


def _merge(found: Settings, raw: dict[str, Any], source: str, *, personal: bool,
           narrowed: set[str] | None = None) -> None:
    for key, value in raw.items():
        name = ALIASES.get(str(key), str(key))
        if name not in KNOWN:
            found.unknown.append((source, str(key)))
            continue
        if not personal and name in PROJECT_CAN_NARROW:
            verdict = _narrower(name, found.values.get(name), value)
            if verdict is not None and narrowed is not None:
                narrowed.add(name)
            if verdict is None:
                found.refused.append((
                    source, name,
                    f'a project may only lower it ({PROJECT_CAN_NARROW[name]})',
                ))
                continue
            value = verdict
        if name in REPLACE_LISTS or name not in found.values:
            found.values[name] = value
        elif isinstance(found.values[name], dict) and isinstance(value, dict):
            # Recursively, and not one level: the shape people actually write
            # is two providers side by side, and a shallow merge means adding
            # a second one silently *removes* the first, along with its key.
            # The rule that makes this predictable is that lists replace —
            # a union would mean nothing could be taken away — and objects
            # merge, all the way down.
            found.values[name] = _deep_merge(found.values[name], value)
        else:
            found.values[name] = value


def _deep_merge(base: dict[str, Any], add: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in add.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _narrower(name: str, current: Any, proposed: Any) -> Any | None:
    """Whether a project's value is a narrowing. None means it is not.

    "Is this tighter?" depends on the setting, and assuming otherwise is how a
    rule like this quietly stops being one: `allow_purchases` tightens to
    `false`, `local_only` tightens to `true`, and the approval mode tightens by
    going down the ladder. The first version of this read every boolean the
    same way and so let a project *switch `local_only` off*.
    """
    if name not in PROJECT_CAN_NARROW:
        return None

    if name == 'approval_mode':
        old, new = str(current or 'ask'), str(proposed or 'ask')
        if new not in _MODE_ORDER or old not in _MODE_ORDER:
            return None
        return new if _MODE_ORDER.index(new) <= _MODE_ORDER.index(old) else None

    tighter = PROJECT_CAN_NARROW[name]
    if not isinstance(proposed, bool):
        return None
    if current is None:
        # Nothing to widen. A project setting a flag on an install that has
        # not expressed an opinion is *setting* an opinion, which is the
        # ordinary case and must not be read as an attempt to widen one.
        return proposed
    if not isinstance(current, bool):
        return None
    return proposed if proposed == tighter else None


def _variable_for(key: str) -> str:
    return f'OPENMIRROR_{key.upper()}'


def _types() -> dict[str, type]:
    """What kind of thing each setting is, read off the `Config` dataclass.

    So a number stays a number: `OPENMIRROR_COMPACT_AT=1` has to be 1, and
    without this a bare `1` is read as `True`, which is also 1 and is a
    thousand times too small a context window.
    """
    global _TYPES
    if _TYPES is None:
        import typing

        from openmirror.config import Config

        # `get_type_hints` rather than `dataclasses.fields(...).type`, which
        # is the *string* `"int"` under `from __future__ import annotations`
        # — and a string is not an int, which is how every number in a
        # settings file ends up as text.
        try:
            hints = typing.get_type_hints(Config)
        except Exception:  # noqa: BLE001 - an unresolvable annotation is not fatal
            hints = {name: str for name in dir(Config) if not name.startswith('_')}
        _TYPES = hints
    return _TYPES


_TYPES: dict[str, Any] | None = None


def _coerce(text: str, kind: type | None = None) -> Any:
    """A string into the type the setting is declared to be.

    Type-aware, and *declared* type rather than the current value: a bare `1`
    is both "true" and "the number one", and guessing wrong sets
    `compact_at` to `True` — which is also 1, so it looks like it worked and
    is a thousand times too small a context window.
    """
    lowered = text.strip().lower()
    if kind is bool:
        if lowered in ('1', 'true', 'yes', 'on'):
            return True
        if lowered in ('0', 'false', 'no', 'off'):
            return False
        return None
    if kind is int:
        try:
            return int(text)
        except ValueError:
            return None
    if kind is str:
        return text
    if kind is None:
        # No declared type to go on — a value being written onto a `Config`
        # field by `apply`. Only the *words*, never a bare digit, because "1"
        # has to mean the number one wherever the field is a number.
        if lowered in ('true', 'yes', 'on'):
            return True
        if lowered in ('false', 'no', 'off'):
            return False
    return text
    # A `Path`, or anything else: the string is the best available answer and
    # `apply` converts it against the field it lands on.
    return text


def apply(settings: Settings, config: Any) -> Any:
    """Put the values onto a `Config`.

    Called after the dataclass is built, so a settings file changes behaviour
    without `Config` having to know that settings files exist — the same
    arrangement the MCP `.mcp.json` and the `AGENTS.md` lookups already use.
    """
    for key, value in settings.values.items():
        if key == 'provider':
            continue
        if not hasattr(config, key):
            continue
        current = getattr(config, key)
        # A `Path` field given a string, and a bool field given "yes": both
        # happen in a file somebody wrote by hand.
        if isinstance(current, Path) and isinstance(value, str):
            value = Path(value).expanduser()
        elif isinstance(current, bool) and not isinstance(value, bool):
            value = _coerce(str(value))
        try:
            setattr(config, key, value)
        except (AttributeError, TypeError) as exc:
            log.debug('setting %s was not applied: %s', key, exc)
    return config


def for_root(root: Path | str, base: Any = None, *, environ: dict[str, str] | None = None) -> Any:
    """A copy of the config with this project's settings applied.

    **A copy, because a project file belongs to a project.** The daemon serves
    sessions in many directories at once, and applying one project's settings
    to a shared config would let a repository in a session change what
    happens in somebody else's — which is the exact hole the narrowing rules
    above exist to close, one level up.
    """
    from dataclasses import replace

    from openmirror.config import config as default_config

    base = base if base is not None else default_config
    found = load(root, environ=environ)
    # Only the values this project's files set, so a caller's own overrides
    # and the environment are not re-applied into a different object.
    for key, value in found.values.items():
        if not hasattr(base, key):
            continue
        current = getattr(base, key)
        if isinstance(current, Path) and isinstance(value, str):
            value = Path(value).expanduser()
        elif isinstance(current, bool) and not isinstance(value, bool):
            value = _coerce(str(value))
    try:
        return replace(base, **{
            k: v for k, v in found.values.items() if hasattr(base, k)
        })
    except TypeError:
        return base


__all__ = [
    'ALIASES', 'KNOWN', 'PERSONAL', 'PROJECT_CAN_NARROW', 'PROJECT_NAMES', 'REPLACE_LISTS', 'Settings',
    'SettingsError', 'apply', 'for_root', 'load', 'paths',
]

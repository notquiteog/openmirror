"""Checking for a newer openmirror, and staging it.

Three routes, and the split between them is the whole design:

* **check** asks GitHub what exists. A network request, at most once an hour,
  and it only shows anybody.
* **apply** downloads the installer to a staging file and verifies it against
  the checksum published in the same release.
* **the app** runs it. Not this process — see `openmirror/update.py` for why,
  which is mostly that a running executable cannot be overwritten on Windows
  and an application bundle cannot be swapped while it is open on macOS.

So there is no route here that executes anything. The one place the OS is
involved is the desktop app's own `apply_update` command, and the path it is
handed is a file this daemon verified.

**Never during a turn.** An update prompt in the middle of a build is an
update prompt somebody dismisses without reading, and the check is skipped
while a session is busy — the answer will still be there in a moment, and a
dialog that appears over somebody's work is a dialog that gets ignored.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from openmirror.agent.manager import manager
from openmirror.config import config
from openmirror.update import REPO, UpdateRefused, Updates, is_newer

log = logging.getLogger(__name__)

router = APIRouter(prefix='/api/update')

def version() -> str:
    """What this install is, from its own metadata.

    A literal here was a second place for the version to be wrong, and the CI
    check only looks at pyproject.toml and Cargo.toml — so a bump could pass
    every check and still ship a daemon that reports the old number to the
    updater, which is a daemon that never offers itself an update.
    """
    # The file first, and the installed metadata second, which is the opposite
    # of the usual order and is right for both cases.
    #
    # A frozen daemon has no pyproject.toml beside it, so it reads the
    # metadata frozen in with it — authoritative, and the only option. A
    # daemon running from a source tree *does* have one, and there the
    # installed metadata is the stale one: an editable install still says
    # 0.1.0 the moment pyproject is bumped, so a version read from it would
    # keep reporting the number this checkout stopped being several commits
    # ago, and would offer itself a downgrade.
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / 'pyproject.toml'
    try:
        return str(tomllib.loads(pyproject.read_text())['project']['version'])
    except (OSError, KeyError, ValueError):
        pass
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as installed

    try:
        return installed('openmirror')
    except PackageNotFoundError:
        return '0.0.0'


VERSION = version()

updates = Updates(VERSION, config.update_staging)


def _busy() -> bool:
    """Whether anybody is watching a turn. A prompt over live work gets
    dismissed without being read, so the check waits."""
    return any(session.busy for session in manager._sessions.values())  # noqa: SLF001


class CheckBody(BaseModel):
    force: bool = False
    prerelease: bool = False


class ApplyBody(BaseModel):
    tag: str = Field(default='', description='Which release. Defaults to the newest found.')
    prerelease: bool = False


@router.get('/status')
async def status() -> dict[str, Any]:
    """What is known, without asking GitHub anything.

    This is what the interface polls, so it has to be free: no network, no
    disk, and a byte small enough to ask about every few seconds.
    """
    return updates.state.public()


@router.get('/check')
async def check_manual() -> dict[str, Any]:
    """A check somebody pressed a button for.

    `force` because pressing the button is the whole point — the hourly limit
    exists to stop a poll, and a person asking is not a poll.
    """
    if _busy():
        return {**updates.state.public(), 'deferred': 'something is running; ask again in a moment'}
    return await updates.check(force=True)


@router.post('/check')
async def check(body: CheckBody) -> dict[str, Any]:
    if _busy():
        return {**updates.state.public(), 'deferred': 'something is running; ask again in a moment'}
    return await updates.check(force=body.force, allow_prerelease=body.prerelease)


@router.post('/apply')
async def apply(body: ApplyBody) -> dict[str, Any]:
    """Fetch the installer and verify it. Installs nothing.

    A staged file is not a running one, and this is deliberate: the step that
    replaces a binary belongs to the OS and to a person. The response says
    what to do with the file, and on a frozen install the app does that.
    """
    release = updates.state.latest
    if release is None or (body.tag and body.tag != release.tag):
        # Somebody named a tag we have not fetched. Check first, so the answer
        # is about a release that exists.
        await updates.check(force=True, allow_prerelease=body.prerelease or bool(body.tag))
        release = updates.state.latest
    if release is None:
        raise HTTPException(status_code=404, detail='no release newer than this one was found')

    if not is_newer(release.tag, VERSION, allow_prerelease=release.prerelease):
        raise HTTPException(
            status_code=409,
            detail=f'{release.tag} is not newer than the {VERSION} you are running',
        )

    asset = release.pick()
    if asset is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f'the {release.tag} release has no installer for this machine '
                f'({updates.state.public()["platform"]}). The release page lists what it does have.'
            ),
        )

    sums = await updates.fetch_checksums(release)
    try:
        fetched = await updates.download(release, asset, sums=sums)
    except UpdateRefused as exc:
        # 502: the thing we asked GitHub for is wrong, not our request.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        **fetched,
        'tag': release.tag,
        'version': str(release.version or release.tag),
        'notes': (release.body or '')[:600],
        'url': release.html_url,
        'frozen': config.is_frozen,
        'next': (
            'the app will offer to install it and restart'
            if config.is_frozen
            else f'this is a pip install, not a bundled app: run `pip install --upgrade '
                 f'git+https://github.com/{REPO}@{release.tag}` to move to {release.tag}'
        ),
    }


@router.post('/check-on-start')
async def check_on_start() -> dict[str, Any]:
    """The start-up check, behind a flag.

    Separate from `check` because the conditions are different and it is worth
    being able to assert them: only when the switch is on, only once a day,
    never while anything is running, and never when `local_only` is set — which
    `Updates.check` enforces itself, so this cannot be talked past it.
    """
    if not config.update_check_on_start or _busy():
        return {'checked': False, 'reason': 'not due, or something is running'}
    if not config.update_check_enabled:
        return {'checked': False, 'reason': 'update checks are switched off'}
    if config.local_only:
        return {'checked': False, 'reason': 'this install is local only'}
    return {'checked': True, **(await updates.check())}


@router.post('/dismiss')
async def dismiss() -> dict[str, bool]:
    """Not "update later" — actually clear the notice.

    A dismissal that comes back on the next poll is a dismissal that did not
    work, and the version is not forgotten: the next check simply finds the
    same release newer than this one and shows it again, which is what
    somebody who has not updated wants.
    """
    updates.state.latest = None
    return {'ok': True}

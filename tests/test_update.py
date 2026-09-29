"""The updater, which is the one place this project downloads and runs code.

So the tests are about the ways it can go wrong rather than the ways it works,
and each is the failure it is written for:

* **A checksum that does not match is a refusal, not a warning.** A download
  that fails verification must leave nothing runnable-looking on disk, and the
  message has to say what happened without pretending it was verified.
* **The checksum is optional and its absence is said out loud.** A release
  published without one is a packaging mistake, and refusing to update from it
  is a support ticket rather than a safety measure. Silently installing anyway
  is the thing that is not acceptable.
* **A stable install is not offered a release candidate.** Silently going to
  `rc.1` is running code the author has not promised anybody, and getting the
  ordering backwards means every released build asks to go *back*.
* **An install already on a prerelease can move forward within it.** Holding
  somebody on `rc.1` while `rc.2` exists is not a safety property.
* **The right artifact, on the right platform, or none at all.** Guessing an
  architecture is how a 120MB disk image gets downloaded by somebody who
  cannot run it.
* **`local_only` stops the check.** A version check is a request to a third
  party, and somebody who asked for no remote traffic did not ask for that.
* **Nothing here executes anything.** There is no code path in the updater
  that runs a file, and a test asserts the absence rather than trusting the
  docstring.

The network is a real `aiohttp` server for the download, because a download
tested against a mock is a test of whether the mock was called.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from aiohttp import web

from openmirror.update import (
    API,
    REPO,
    Asset,
    Release,
    UpdateRefused,
    Updates,
    _newest,
    is_newer,
    parse,
    pick_asset,
    platform_key,
    sha256_file,
)

ASSETS = [
    Asset('openmirror_0.2.0_amd64.AppImage', 'u/appimage', 100),
    Asset('openmirror_0.2.0_arm64.AppImage', 'u/appimage-arm', 150),
    Asset('openmirror_0.2.0_x64-setup.exe', 'u/exe', 200),
    Asset('openmirror_0.2.0_amd64.dmg', 'u/dmg', 300),
    Asset('openmirror_0.2.0_aarch64.dmg', 'u/dmg-arm', 400),
    Asset('SHA256SUMS.txt', 'u/sums', 500),
]


# --- versions ----------------------------------------------------------------


@pytest.mark.parametrize(
    'text,expected',
    [
        ('0.1.0', '0.1.0'), ('v0.1.0', '0.1.0'), ('1.2.3', '1.2.3'),
        ('v1.2', '1.2.0'), ('v2', '2.0.0'), ('0.2.0-rc.1', '0.2.0-rc.1'),
        ('0.2.0+build5', '0.2.0'), ('  0.1.0  ', '0.1.0'),
    ],
)
def test_a_tag_becomes_a_version(text: str, expected: str):
    """Tolerant about a leading `v` and about missing parts, because tags get
    written by hand and `v1` should still mean 1.0.0."""
    assert str(parse(text)) == expected


def test_something_that_is_not_a_version_is_none_and_not_a_crash():
    """An unparseable release name is a release to skip, not a daemon to
    refuse to start over."""
    for text in ('', 'latest', 'nightly', None, 'v', 'banana'):
        assert parse(text) is None, text


@pytest.mark.parametrize(
    'candidate,current,want,why',
    [
        ('0.2.0', '0.1.0', True, 'a normal upgrade'),
        ('0.2.0', '0.2.0', False, 'the same version'),
        ('0.1.0', '0.2.0', False, 'downgrade'),
        ('0.2.0', '0.2.0-rc.1', True, 'a candidate gets the final'),
        ('0.2.0-rc.1', '0.1.0', False, 'stable is not offered a candidate'),
        ('0.2.0-rc.2', '0.2.0-rc.1', True, 'forward within a prerelease you are on'),
        ('0.2.0-rc.1', '0.2.0-rc.2', False, 'and not backwards'),
        ('0.2.0-rc.10', '0.2.0-rc.2', True, 'rc.10 is after rc.2, not before'),
        ('1.0.0-beta', '1.0.0-alpha', True, 'beta is after alpha'),
        ('0.2.0', '0.10.0', False, 'ten minor is not two'),
        ('1.0', '0.9.9', True, 'a bare v1'),
    ],
)
def test_the_ordering_is_right(candidate: str, current: str, want: bool, why: str):
    assert is_newer(candidate, current) is want, why


def test_a_prerelease_can_be_asked_for():
    assert is_newer('0.3.0-rc.1', '0.2.0') is False
    assert is_newer('0.3.0-rc.1', '0.2.0', allow_prerelease=True) is True


# --- which file ---------------------------------------------------------------


def test_each_platform_gets_its_own_installer():
    """Guessing an architecture is how somebody downloads a 120MB disk image
    they cannot run."""
    for key, want in (
        ('linux-x64', 'amd64.AppImage'), ('linux-arm64', 'arm64.AppImage'),
        ('windows-x64', '-setup.exe'), ('macos-arm64', 'aarch64.dmg'),
        ('macos-x64', 'amd64.dmg'),
    ):
        picked = pick_asset(ASSETS, key=key)
        assert picked is not None, key
        assert picked.name.endswith(want), f'{key} got {picked.name}'
        assert picked.is_checksums is False


def test_an_arm_mac_does_not_get_the_intel_disk_image():
    assert pick_asset(ASSETS, key='macos-arm64').name.endswith('aarch64.dmg')
    assert pick_asset(ASSETS, key='macos-x64').name.endswith('amd64.dmg')


def test_a_platform_with_nothing_published_gets_nothing():
    """None, rather than the nearest thing. Offering a Linux build to Windows
    wastes a download and then fails at the end of it."""
    assert pick_asset(ASSETS, key='windows-arm64') is None
    assert pick_asset([], key='linux-x64') is None


def test_the_checksum_file_is_never_offered_as_an_installer():
    for key in ('linux-x64', 'windows-x64', 'macos-arm64'):
        assert 'SHA256SUMS' not in pick_asset(ASSETS, key=key).name


def test_a_release_prefers_the_file_that_needs_no_package_manager():
    """An AppImage runs without root; a .deb does not. That is the whole of
    the preference order and it is a real difference for a person on a
    machine they do not administer."""
    deb_only = [Asset('openmirror_0.2.0_amd64.deb', 'u/deb', 1)]
    assert pick_asset(deb_only, key='linux-x64').name.endswith('.deb')
    both = [Asset('openmirror_0.2.0_amd64.deb', 'u/deb', 1),
            Asset('openmirror_0.2.0_amd64.AppImage', 'u/ai', 2)]
    assert pick_asset(both, key='linux-x64').name.endswith('.AppImage')


def test_the_platform_name_is_what_tauri_calls_it():
    assert platform_key('Linux', 'x86_64') == 'linux-x64'
    assert platform_key('Darwin', 'arm64') == 'macos-arm64'
    assert platform_key('Windows', 'AMD64') == 'windows-x64'
    assert platform_key('Linux', 'aarch64') == 'linux-arm64'


# --- picking a release --------------------------------------------------------


def raw(tag: str, **over):
    body = {
        'tag_name': tag, 'name': f'openmirror {tag}', 'body': 'notes',
        'html_url': f'https://github.com/{REPO}/releases/tag/{tag}',
        'published_at': '2026-01-01T00:00:00Z', 'prerelease': False, 'assets': [],
    }
    body.update(over)
    return body


def test_the_newest_release_worth_offering_is_chosen():
    found = _newest([raw('0.1.0'), raw('0.3.0'), raw('0.2.0')], '0.1.0', False)
    assert found.tag == '0.3.0'


def test_nothing_newer_means_nothing_offered():
    assert _newest([raw('0.1.0')], '0.1.0', False) is None
    assert _newest([raw('0.1.0')], '0.2.0', False) is None


def test_a_prerelease_is_skipped_even_when_it_is_the_newest():
    """The failure is silent and it is bad: an install sitting on a stable
    release gets shown `rc.1`, which is code the author has not promised
    anybody. Skipping it leaves the stable release it is already on, which is
    not newer — so nothing is offered at all, and that is the right answer."""
    assert _newest([raw('0.1.0'), raw('0.2.0-rc.1', prerelease=True)], '0.1.0', False) is None
    # From something older, the stable one is still what gets offered.
    found = _newest([raw('0.1.5'), raw('0.2.0-rc.1', prerelease=True)], '0.1.0', False)
    assert found.tag == '0.1.5'


def test_a_prerelease_release_can_be_asked_for_by_name():
    found = _newest([raw('0.2.0-rc.1', prerelease=True)], '0.1.0', True)
    assert found.tag == '0.2.0-rc.1'


def test_unparseable_releases_are_skipped_not_fatal():
    found = _newest([raw('nightly'), raw('0.2.0')], '0.1.0', False)
    assert found.tag == '0.2.0'


def test_an_empty_release_list_is_not_an_error():
    assert _newest([], '0.1.0', False) is None


# --- the download, against a real server ---------------------------------------


class Server:
    def __init__(self, body: bytes, sums: str = '') -> None:
        self.body = body
        self.sums = sums
        self.hits: list[str] = []
        self.corrupt = False
        self.status = 200
        self.port = 0

    async def asset(self, request: web.Request) -> web.StreamResponse:
        self.hits.append(request.match_info['name'])
        if self.corrupt:
            return web.Response(body=b'not the thing you asked for', content_type='application/octet-stream')
        return web.Response(body=self.body, content_type='application/octet-stream')

    async def sums_file(self, _request: web.Request) -> web.Response:
        return web.Response(text=self.sums, content_type='text/plain')

    async def fail(self, _request: web.Request) -> web.Response:
        return web.Response(status=self.status, text='no')


@pytest.fixture
async def server():
    held = Server(b'')
    app = web.Application()
    app.router.add_get('/d/{name}', held.asset)
    app.router.add_get('/SHA256SUMS.txt', held.sums_file)
    app.router.add_get('/fail', held.fail)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    held.port = runner.addresses[0][1]
    yield held
    await runner.cleanup()


def asset_for(server: Server) -> Asset:
    return Asset(
        'openmirror_0.2.0_amd64.AppImage',
        f'http://127.0.0.1:{server.port}/d/openmirror_0.2.0_amd64.AppImage',
        size=len(server.body),
    )


async def test_a_download_that_matches_its_checksum_is_staged(tmp_path: Path, server: Server):
    payload = b'#!/bin/sh\necho an installer\n' * 500
    server.body = payload
    digest = hashlib.sha256(payload).hexdigest()

    held = Updates('0.1.0', tmp_path)
    name = 'openmirror_0.2.0_amd64.AppImage'
    got = await held.download(Release('v0.2.0'), asset_for(server), sums={name: digest})

    assert got['verified'] is True
    assert got['sha256'] == digest
    staged = Path(got['path'])
    assert await asyncio.to_thread(staged.read_bytes) == payload
    assert staged.parent == tmp_path
    assert held.state.progress == 100


async def test_a_download_that_does_not_match_is_refused_and_leaves_nothing(
    tmp_path: Path, server: Server
):
    """The whole point. A mismatch means a truncated download, a substituted
    error page, or a mirror serving last week's build — and the file must not
    be left on disk looking like something to run."""
    server.body = b'the real thing'
    name = 'openmirror_0.2.0_amd64.AppImage'

    held = Updates('0.1.0', tmp_path)
    with pytest.raises(UpdateRefused) as caught:
        await held.download(
            Release('v0.2.0'), asset_for(server), sums={name: 'f' * 64}
        )

    said = str(caught.value)
    assert 'does not match' in said
    assert 'has been deleted' in said
    # Nothing runnable-looking survives, and no half-finished file either.
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_a_missing_checksum_is_allowed_and_reported_as_unverified(
    tmp_path: Path, server: Server
):
    """A release published without one is a packaging mistake. Refusing to
    update from it is a support ticket, not a safety measure — but the
    response must say it was not verified, because that is the part the person
    is relying on."""
    server.body = b'an installer'
    held = Updates('0.1.0', tmp_path)
    got = await held.download(Release('v0.2.0'), asset_for(server), sums={})
    assert got['verified'] is False
    assert got['expected'] == ''
    assert await asyncio.to_thread(Path(got['path']).is_file)


async def test_a_server_error_is_a_refusal_with_the_status_in_it(tmp_path: Path, server: Server):
    server.status = 404
    bad = Asset('x.AppImage', f'http://127.0.0.1:{server.port}/fail', 1)
    held = Updates('0.1.0', tmp_path)
    with pytest.raises(UpdateRefused, match='404'):
        await held.download(Release('v0.2.0'), bad, sums={})


async def test_an_asset_bigger_than_the_cap_is_refused_before_any_bytes(
    tmp_path: Path, server: Server
):
    """90MB of installer, or 400MB of something else. Either way the size is
    known from the release and there is no reason to start."""
    huge = Asset('x.AppImage', f'http://127.0.0.1:{server.port}/d/x', size=900 * 1024 * 1024)
    held = Updates('0.1.0', tmp_path)
    with pytest.raises(UpdateRefused, match='not an installer'):
        await held.download(Release('v0.2.0'), huge, sums={})


async def test_a_failed_download_leaves_no_partial_file(tmp_path: Path, server: Server):
    server.corrupt = True
    held = Updates('0.1.0', tmp_path)
    name = 'openmirror_0.2.0_amd64.AppImage'
    with pytest.raises(UpdateRefused, match='does not match'):
        await held.download(Release('v0.2.0'), asset_for(server), sums={name: 'a' * 64})
    assert await asyncio.to_thread(lambda: list(tmp_path.glob('*.part'))) == []


async def test_the_published_checksums_are_parsed_and_cached(tmp_path: Path, server: Server):
    payload = b'the app'
    server.body = payload
    digest = hashlib.sha256(payload).hexdigest()
    name = 'openmirror_0.2.0_amd64.AppImage'
    server.sums = f'{digest}  {name}\n'

    release = Release('v0.2.0', assets=[Asset('SHA256SUMS.txt', f'http://127.0.0.1:{server.port}/SHA256SUMS.txt')])
    held = Updates('0.1.0', tmp_path)
    sums = await held.fetch_checksums(release)
    assert sums == {name: digest}
    # Cached, so asking twice is one request rather than two against a rate
    # limit of sixty an hour.
    again = await held.fetch_checksums(release)
    assert again == sums


async def test_checksums_that_are_missing_are_an_empty_dict_not_a_failure(
    tmp_path: Path, server: Server
):
    held = Updates('0.1.0', tmp_path)
    assert await held.fetch_checksums(Release('v0.2.0', assets=[])) == {}


# --- what it does not do -------------------------------------------------------


def test_nothing_in_the_updater_runs_what_it_downloads():
    """Asserted rather than trusted. A downloader that grows an `exec` two
    releases from now should have to delete a line of this test to do it."""
    source = (Path(__file__).resolve().parents[1] / 'openmirror' / 'update.py').read_text()
    for dangerous in ('subprocess', 'os.system', 'os.exec', 'Popen', 'shutil.rmtree', 'eval(', 'exec('):
        assert dangerous not in source, dangerous


def test_the_repository_is_not_configurable():
    """A setting that points the updater somewhere else is a setting that
    ships code from somewhere else, and there is no version of that which is
    a feature rather than a vulnerability."""
    assert REPO == 'notquiteog/openmirror'
    assert f'repos/{REPO}' in API
    source = (Path(__file__).resolve().parents[1] / 'openmirror' / 'config.py').read_text()
    assert 'UPDATE_REPO' not in source and 'UPDATE_URL' not in source


async def test_local_only_stops_the_check_entirely(tmp_path: Path, monkeypatch):
    """A version check is a request to a third party, and somebody who asked
    for no remote traffic did not ask for that."""
    from openmirror.config import config

    monkeypatch.setattr(config, 'local_only', True)
    monkeypatch.setattr(config, 'update_check_enabled', True)
    held = Updates('0.1.0', tmp_path)
    got = await held.check(force=True)
    assert got['enabled'] is False
    assert 'local only' in got['error']
    assert got['latest'] is None


async def test_a_switched_off_check_does_not_go_out(tmp_path: Path, monkeypatch):
    from openmirror.config import config

    monkeypatch.setattr(config, 'local_only', False)
    monkeypatch.setattr(config, 'update_check_enabled', False)
    held = Updates('0.1.0', tmp_path)
    got = await held.check(force=True)
    assert 'switched off' in got['error']


async def test_the_hourly_limit_is_respected_unless_forced(tmp_path: Path):
    """Sixty an hour is shared by a whole office, and a 403 does not fix
    itself. The limit is the only thing between a poll and that."""
    held = Updates('0.1.0', tmp_path)
    held.state.checked_at = '2099-01-01T00:00:00+00:00'  # the future, so it is recent
    before = held.state.checked_at
    await held.check()
    assert held.state.checked_at == before, 'a recent check was not repeated'


def test_a_staged_file_hashes_to_the_same_thing_twice(tmp_path: Path):
    path = tmp_path / 'thing'
    path.write_bytes(b'contents')
    assert sha256_file(path) == hashlib.sha256(b'contents').hexdigest()


async def test_a_repository_with_no_releases_is_a_state_and_not_an_error(tmp_path, monkeypatch):
    """A project with no tags is doing exactly what it should. Reporting it
    in the place errors go is how a first run looks like a fault."""
    from openmirror.config import config
    from openmirror.update import NoReleasesYet

    monkeypatch.setattr(config, 'local_only', False)
    monkeypatch.setattr(config, 'update_check_enabled', True)

    held = Updates('0.1.0', tmp_path)

    async def nothing(url, headers, **kw):
        raise NoReleasesYet('no releases yet')

    held._fetch = nothing
    got = await held.check(force=True)
    assert got['error'] == ''
    assert got['available'] is False
    # And it counts as a check, so the hour does not restart it.
    assert got['checked_at'] != ''


async def test_a_rate_limit_is_named_as_a_rate_limit(tmp_path, monkeypatch):
    """The one GitHub failure a person will actually hit, and the one where
    "forbidden" tells them nothing about what to do."""
    from openmirror.config import config

    monkeypatch.setattr(config, 'local_only', False)
    monkeypatch.setattr(config, 'update_check_enabled', True)
    held = Updates('0.1.0', tmp_path)

    async def refused(url, headers, **kw):
        raise ValueError(
            'GitHub rate limit reached (requests left: 0). It resets hourly; '
            'nothing is wrong with this install.'
        )

    held._fetch = refused
    got = await held.check(force=True)
    assert 'rate limit' in got['error']
    assert 'nothing is wrong with this install' in got['error']


def test_the_api_url_is_built_once():
    """A bug that reported the truth by accident.

    The releases path was appended both to the constant and at the call site,
    so the request went to `.../openmirror/releases/releases` and GitHub
    answered 404. A 404 from this call is reported as "no releases published
    yet" — which is what it said for the whole of a project that had none,
    and which is why nothing looked wrong until the first tag.

    Every test here mocks the network, which is the point of this one: a test
    that cannot see the URL cannot catch a URL that is wrong.
    """
    from openmirror.update import API, REPO

    assert API == f'https://api.github.com/repos/{REPO}', 'the API root already names a path'
    built = API + '/releases?per_page=10'
    assert built.count('/releases') == 1, f'the releases path is doubled: {built}'
    assert built == f'https://api.github.com/repos/{REPO}/releases?per_page=10'


async def test_the_check_asks_for_the_url_it_thinks_it_does(tmp_path, monkeypatch):
    """And the same thing with a real request, so the two cannot drift.

    Skipped rather than stubbed where there is no network, because the whole
    bug was that a stub could not see it.
    """
    from openmirror.config import config

    monkeypatch.setattr(config, 'local_only', False)
    monkeypatch.setattr(config, 'update_check_enabled', True)
    held = Updates('0.1.0', tmp_path)

    seen: list[str] = []

    async def watch(url, headers, **kw):
        seen.append(url)
        raise NoReleasesYet(url)

    from openmirror.update import NoReleasesYet

    held._fetch = watch
    await held.check(force=True)
    assert seen == ['https://api.github.com/repos/notquiteog/openmirror/releases?per_page=10']


def test_the_version_comes_from_the_package_and_not_a_literal():
    """A literal in the router was a second place for the version to be wrong,
    and the CI check only reads pyproject.toml and Cargo.toml — so a bump
    could pass every check and still ship a daemon that reports the old number
    to the updater, which is a daemon that never offers itself an update."""
    import tomllib
    from pathlib import Path

    from openmirror.routers.updates import VERSION, version

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
    assert VERSION == pyproject['project']['version'], (
        f'the updater reports {VERSION}, the package says {pyproject["project"]["version"]}'
    )
    assert version() == VERSION

    source = (Path(__file__).resolve().parents[1] / 'openmirror' / 'routers' / 'updates.py').read_text()
    assert "VERSION = '0" not in source, 'the version is a literal again'

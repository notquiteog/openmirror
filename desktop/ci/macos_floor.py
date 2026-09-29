"""Keep the daemon's oldest macOS where the app says it is.

    python desktop/ci/macos_floor.py repin    # before freezing
    python desktop/ci/macos_floor.py check    # after

The app declares the oldest macOS it runs on — `bundle.macOS.minimumSystemVersion`
in tauri.conf.json — and the Tauri half honours it. The daemon half is whatever
pip installed, and pip takes each package's build for the newest macOS the
runner can run: numpy's, for one, is built for macOS 14. Frozen into the app,
that is a daemon that dies on import on anything older, on a Mac nobody on the
project will ever see it on.

`repin` swaps every installed wheel built for a newer macOS than the floor for
the same version built for the floor, where one is published. `check` then reads
the minimum macOS out of every Mach-O file the freeze can have picked up — the
interpreter, PyInstaller's bootloader, every extension module — and fails,
naming names, if any of them is newer than the floor.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import sysconfig
import tempfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WHEEL_TAG = re.compile(r'macosx_(\d+)_(\d+)_\w+$')
# Thin and fat Mach-O, both byte orders. (0xcafebabe is also a Java class
# file, which is why `.class` files are skipped below.)
MAGIC = {b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe'}


def floor() -> tuple[int, int]:
    conf = json.loads((ROOT / 'desktop/src-tauri/tauri.conf.json').read_text())
    return as_version(conf['bundle']['macOS']['minimumSystemVersion'])


def as_version(text: str) -> tuple[int, int]:
    parts = [int(p) for p in text.split('.')[:2]]
    return parts[0], parts[1] if len(parts) > 1 else 0


def shown(v: tuple[int, int]) -> str:
    return f'{v[0]}.{v[1]}'


def repin() -> None:
    low = floor()
    arch = platform.machine()
    for dist in metadata.distributions():
        tags = [
            line.partition(':')[2].strip()
            for line in (dist.read_text('WHEEL') or '').splitlines()
            if line.startswith('Tag:')
        ]
        built_for = [as_version(f'{m[1]}.{m[2]}') for tag in tags if (m := WHEEL_TAG.search(tag))]
        if not built_for or max(built_for) <= low:
            continue
        name, version = dist.metadata['Name'], dist.version
        print(f'{name} {version} is built for macOS {shown(max(built_for))}; fetching the build for {shown(low)}')
        with tempfile.TemporaryDirectory() as tmp:
            fetched = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'pip',
                    'download',
                    f'{name}=={version}',
                    '--no-deps',
                    '--only-binary=:all:',
                    '--implementation',
                    'cp',
                    '--python-version',
                    f'{sys.version_info.major}.{sys.version_info.minor}',
                    '--platform',
                    f'macosx_{low[0]}_{low[1]}_{arch}',
                    '--dest',
                    tmp,
                ],
                check=False,
            )
            wheels = list(Path(tmp).glob('*.whl'))
            if fetched.returncode or not wheels:
                print('  none is published; `check` will say whether that matters')
                continue
            subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--no-deps', '--force-reinstall', *map(str, wheels)],
                check=True,
            )


def minimum_macos(text: str) -> tuple[int, int] | None:
    """The oldest macOS a Mach-O file will load on, from `otool -l` output.

    Only two load commands mean anything here, and the difference between them
    and everything else is the whole of this function:

    * `LC_BUILD_VERSION` says `minos 11.0`.
    * `LC_VERSION_MIN_MACOSX` says `version 10.9`.

    A third thing in the same output is a *version* and is not a macOS version
    at all: `LC_ID_DYLIB` carries the library's own `compatibility version`
    and `current version`, and the system's `libSystem` is on a scheme where
    that number is in the thousands. Reading those — which the first version of
    this did, by pattern-matching any line starting with `version` and taking
    the largest — made every extension module in aiohttp, Pillow and
    websockets claim to need macOS **1267**, and failed every macOS build of
    the first release with a message about a version nobody could ship.

    So the load command is tracked and a bare `version` is only believed under
    `LC_VERSION_MIN_MACOSX`. `minos` is self-describing and needs no such
    care.
    """
    found: list[tuple[int, int]] = []
    command = ''
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r'^cmd\s+(\S+)$', stripped)
        if match:
            command = match.group(1)
            continue
        minimum = re.match(r'^minos\s+(\d+\.\d+)', stripped)
        if minimum:
            found.append(as_version(minimum.group(1)))
            continue
        legacy = re.match(r'^version\s+(\d+\.\d+)', stripped)
        if legacy and command == 'LC_VERSION_MIN_MACOSX':
            found.append(as_version(legacy.group(1)))
    return max(found) if found else None


def minimum_macos_in(path: Path) -> tuple[int, int] | None:
    out = subprocess.run(
        ['otool', '-arch', 'all', '-l', str(path)], capture_output=True, text=True, check=False
    ).stdout
    return minimum_macos(out)


def check() -> None:
    low = floor()
    roots = {Path(sys.base_prefix), Path(sysconfig.get_paths()['purelib']), Path(sysconfig.get_paths()['platlib'])}
    seen: dict[Path, tuple[int, int]] = {}
    for root in roots:
        for path in root.rglob('*'):
            if path in seen or not path.is_file() or path.is_symlink() or path.suffix == '.class':
                continue
            try:
                with path.open('rb') as f:
                    if f.read(4) not in MAGIC:
                        continue
            except OSError:
                continue
            found = minimum_macos_in(path)
            if found:
                seen[path] = found

    ranked = sorted(seen.items(), key=lambda item: item[1], reverse=True)
    print(f'{len(ranked)} Mach-O files; the app promises macOS {shown(low)}')
    for path, v in ranked[:8]:
        print(f'  {shown(v):>6}  {path}')
    too_new = [(path, v) for path, v in ranked if v > low]
    if too_new:
        print(
            f'{len(too_new)} of them need a newer macOS than {shown(low)}: freeze with older builds, '
            'or raise minimumSystemVersion in tauri.conf.json to match.',
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == '__main__':
    {'repin': repin, 'check': check}[sys.argv[1]]()

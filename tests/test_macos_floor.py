"""The macOS floor check, and the parse that made it wrong.

`desktop/ci/macos_floor.py` is the step that stops a release whose daemon
would not start on the oldest macOS the app claims. On the first tagged run
it failed both macOS builds, naming **157 files** that "need a newer macOS than
11.0" and reporting the version as **1267.0**.

That is not a version, and the diagnosis is the whole of this file. `otool -l`
prints three different things that contain a version number, and only two of
them are about the operating system:

    cmd LC_BUILD_VERSION
       minos 11.0                      <- the OS floor

    cmd LC_VERSION_MIN_MACOSX
       version 10.9                     <- the OS floor, older spelling

    cmd LC_ID_DYLIB
      name /usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1267.0)
                                                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                              a *library* version, on a scheme
                                              where it is in the thousands

Pattern-matching any line that starts with `version` and taking the largest
picks up the third. So every extension module in aiohttp, Pillow and
websockets claimed to need macOS 1267, and the check — which is otherwise a
good check — failed every macOS build of the first release.

The fix is to track the load command and believe a bare `version` only under
`LC_VERSION_MIN_MACOSX`. The tests below feed it the shape that broke it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def floor():
    spec = importlib.util.spec_from_file_location('macos_floor', ROOT / 'desktop' / 'ci' / 'macos_floor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# A shape close to the real thing, trimmed. Taken from `otool -l` on a
# Python.org extension module on macOS 15, which is what the first release ran.
REAL = """
Load command 0
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 2
    minos 11.0
      sdk 15.2
   ntools 1

Load command 1
      cmd LC_ID_DYLIB
  cmdsize 56
         name /Library/Frameworks/Python.framework/Versions/3.12/lib/python3.12/libpython3.12.dylib (compatibility version 3.12.0, current version 3.12.0)

Load command 2
      cmd LC_LOAD_DYLIB
  cmdsize 56
         name /usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1267.0)

Load command 3
      cmd LC_UUID
  cmdsize 24
     uuid 0F1E-2D3C-4B5A-6978-8796-A5B4C3D2E1F0
"""


def test_a_library_version_is_not_a_macos_version(floor):
    """The bug, in the shape that caused it. `1267.0` is libSystem's own
    current version and means nothing about the operating system."""
    assert floor.minimum_macos(REAL) == (11, 0), 'a library version was read as the OS floor'


def test_the_older_spelling_is_still_believed(floor):
    """`LC_VERSION_MIN_MACOSX` is a real floor and a pre-2017 binary still
    has only this to say. Believing `version` only under this command is what
    lets both spellings be read at once."""
    legacy = """
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.9
      sdk 10.12
"""
    assert floor.minimum_macos(legacy) == (10, 9)


def test_a_newer_floor_wins_when_both_spellings_are_present(floor):
    both = REAL + """
Load command 4
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 12.3
      sdk 15.2
"""
    assert floor.minimum_macos(both) == (12, 3), 'the highest real floor is the answer'


def test_a_file_with_no_floor_at_all_is_none(floor):
    assert floor.minimum_macos('') is None
    assert floor.minimum_macos('Load command 0\n      cmd LC_UUID\n') is None


def test_the_whole_file_is_read_through_otool(floor, tmp_path):
    """The parser is only half the function, and `check()` only ever runs on a
    macOS runner where `otool` exists — so this is skipped elsewhere rather
    than stubbed.

    Skipped rather than faked on purpose: a stub that returns empty output
    would let a broken `minimum_macos_in` pass, and the alternative — making
    the function tolerate a missing `otool` — would turn a loud failure into a
    check that silently passes every file.
    """
    import shutil

    if not shutil.which('otool'):
        pytest.skip('otool is macOS only, and check() only runs there')

    empty = tmp_path / 'nothing.so'
    empty.write_bytes(b'\xcf\xfa\xed\xfe' + b'\x00' * 32)
    # A file with nothing to say, which is what the loop sees for every
    # non-Mach-O file it walks past.
    assert floor.minimum_macos_in(empty) is None


def test_the_floor_is_read_from_the_app_config(floor):
    conf = floor.floor()
    assert conf == (11, 0), f'the app promises macOS {floor.shown(conf)}'
    assert floor.as_version('11.0') == (11, 0)
    assert floor.as_version('15.2') == (15, 2)

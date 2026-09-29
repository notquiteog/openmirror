"""Mutation checks for the session-refresh tests.

A test that passes on the broken code is worse than no test, so each mutation
below is a defect the tests are supposed to catch. The source is patched in a
copy of the tree, the tests are run against it, and a mutation that still passes
is reported as a survivor.

Run: .venv/bin/python tests/_mutate_session_refresh.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'openmirror' / 'static' / 'app.js'

# (name, old, new) — each `old` must appear exactly once.
MUTATIONS = [
    (
        'fingerprint ignores the open session',
        '  return JSON.stringify([\n    state.sessionId,',
        '  return JSON.stringify([\n    null,',
    ),
    (
        'fingerprint includes raw idle_for',
        "      s.idle_for < 60 ? 0 : s.idle_for < 3600 ? 1 : s.idle_for < 86400 ? 2 : 3,",
        '      s.idle_for,',
    ),
    (
        'fingerprint drops the turn count',
        's.id, s.title, s.root, s.model, s.turns,',
        's.id, s.title, s.root, s.model,',
    ),
    (
        'fingerprint drops the model shown in the tooltip',
        's.id, s.title, s.root, s.model, s.turns,',
        's.id, s.title, s.root, s.turns,',
    ),
    (
        'fingerprint collapses the kind of waiting to a yes',
        "      s.busy ? 1 : 0, s.waiting_on || '',",
        "      s.busy ? 1 : 0, s.waiting_on ? 1 : 0,",
    ),
    (
        'fingerprint treats the list as a set',
        '    sessions.map((s) => [',
        '    [...sessions].sort((a, b) => a.id.localeCompare(b.id)).map((s) => [',
    ),
    (
        'fingerprint never changes',
        '  return JSON.stringify([',
        '  return ["fixed",',
    ),
    (
        'the change test is removed',
        '  if (print === lastPrint) return;',
        '  lastPrint = print;',
    ),
    (
        'the staleness check moved after the change test',
        "  if (state.sessionId && !sessions.some((s) => s.id === state.sessionId)) {",
        '  if (false) {',
    ),
    (
        'teardown keeps the fingerprint',
        "  lastPrint = '';",
        '  void lastPrint;',
    ),
    (
        'restating fetches the list',
        'function restateTimes() {',
        'function restateTimes() {\n  loadSessions().catch(() => {});',
    ),
    (
        'restating ages a working session',
        '    if (summary.busy) continue;',
        '    if (false) continue;',
    ),
    (
        'restating runs before the first draw',
        '  if (!sessionRows.size) return;\n',
        '',
    ),
    (
        'the clock tick is as fast as the poll',
        'if (clockTimer === null) clockTimer = setInterval(restateTimes, 30000);',
        'if (clockTimer === null) clockTimer = setInterval(restateTimes, 5000);',
    ),
    (
        'the clock tick is dropped',
        '  if (clockTimer === null) clockTimer = setInterval(restateTimes, 30000);',
        '',
    ),
    (
        'the two ticks share an interval',
        '  if (clockTimer === null) clockTimer = setInterval(restateTimes, 30000);',
        '  clockTimer = sessionTimer;',
    ),
    (
        'starting twice doubles the timers',
        '  if (sessionTimer === null) sessionTimer = setInterval(pollSessions, 5000);',
        '  sessionTimer = setInterval(pollSessions, 5000);',
    ),
    (
        'starting twice doubles the clock',
        '  if (clockTimer === null) clockTimer = setInterval(restateTimes, 30000);',
        '  clockTimer = setInterval(restateTimes, 30000);',
    ),
    (
        'stopping leaves the clock running',
        '  if (clockTimer !== null) {\n    clearInterval(clockTimer);\n    clockTimer = null;\n  }',
        '',
    ),
    (
        'a hidden tab still fetches',
        '  if (document.hidden) return;\n',
        '',
    ),
    (
        'the debounce handle outlives its timeout',
        '  refreshTimer = setTimeout(() => {\n    refreshTimer = null;',
        '  refreshTimer = setTimeout(() => {',
    ),
    (
        'the poll ignores a pending refresh',
        '  if (refreshTimer !== null) return;\n',
        '',
    ),
    (
        'the daemon coming back does not refresh',
        'onReachable((ok) => { if (ok) pollSessions(); });',
        'onReachable(() => {});',
    ),
    (
        'the daemon going away refreshes instead',
        'onReachable((ok) => { if (ok) pollSessions(); });',
        'onReachable((ok) => { if (!ok) pollSessions(); });',
    ),
    (
        'becoming visible does not refresh',
        "    pollSessions();\n    startSessionPoll();",
        '    startSessionPoll();',
    ),
    (
        'becoming visible does not restart the timers',
        "    pollSessions();\n    startSessionPoll();",
        '    pollSessions();',
    ),
    (
        'an event on the open session does not refresh',
        '  refreshSessionsSoon();\n}',
        '}\n\nfunction refreshSessionsSoon() { clearTimeout(refreshTimer); }',
    ),
    (
        'selecting a session does not redraw the list',
        '  connectAgent(id);\n  loadSessions();',
        '  connectAgent(id);',
    ),
]


def main() -> int:
    source = APP.read_text()
    for name, old, new in MUTATIONS:
        if source.count(old) != 1:
            print(f'SKIP  {name}: anchor appears {source.count(old)} times')
            continue
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # The tests read `openmirror/static/app.js` relative to the tree root,
            # so the copy has to be a whole tree.
            (work / 'openmirror' / 'static').mkdir(parents=True)
            (work / 'tests').mkdir(parents=True)
            shutil.copy(APP, work / 'openmirror' / 'static' / 'app.js')
            shutil.copy(ROOT / 'tests' / 'test_session_refresh.py', work / 'tests')
            (work / 'openmirror' / 'static' / 'app.js').write_text(source.replace(old, new, 1))

            proc = subprocess.run(
                [str(ROOT / '.venv' / 'bin' / 'python'), '-m', 'pytest',
                 'tests/test_session_refresh.py', '-q', '--no-header', '-x'],
                cwd=work, capture_output=True, text=True, timeout=300,
            )
        caught = proc.returncode != 0
        print(f'{"caught " if caught else "SURVIVED"}  {name}')
        if not caught:
            print(proc.stdout[-2000:])

    return 0


if __name__ == '__main__':
    sys.exit(main())

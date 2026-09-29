"""Every static module parses, and the page they make is not blank.

A whole class of "the interface shows nothing" bugs, all of them the same
shape: a file that is fine as text and is not fine as a *module*. An import
binding colliding with a top-level declaration is an early error — the file
parses as text, every substring assertion in the other tests finds what it is
looking for, and the browser refuses the whole page.

That is not hypothetical. `mail.js` imported `send` from `dom.js` and declared
`async function send()` for the send button, and the result was
`Identifier 'send' has already been declared` on load: no interface at all,
from a change that every text-based test passed.

So this parses each file as a module, which is where that error lives, and
additionally links the graph so a bad relative import is caught too. It is
cheap and it is the only check in the suite that would have seen it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / 'openmirror' / 'static'

# `capture-worklet.js` runs in an AudioWorklet, not as a page module, and
# `native.js` is the only thing the page may ask the desktop app for. Both are
# real files and both must still parse; they are simply not linked into the
# page graph.
MODULES = sorted(p for p in STATIC.rglob('*.js'))


def node() -> str:
    for candidate in ('node', str(Path.home() / '.local' / 'bin' / 'node')):
        found = shutil.which(candidate)
        if found:
            return found
    pytest.skip('node is not on this machine')


def test_there_are_modules_to_check():
    """The scan found nothing, so every other test in this file is vacuous."""
    assert len(MODULES) > 10
    assert {p.name for p in MODULES} >= {'app.js', 'dom.js', 'modes.js', 'mail.js', 'commit.js'}


def test_every_module_parses():
    """The check that would have caught the `send` collision.

    `SourceTextModule` is the parser, and an import binding that shadows a
    top-level declaration is rejected at parse time rather than at run time —
    which is why nothing else in the suite noticed, and why this does.
    """
    script = """
    const fs = require('fs');
    const vm = require('vm');
    const files = process.argv.slice(2);
    const bad = [];
    for (const file of files) {
      try {
        new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { identifier: file });
      } catch (err) {
        bad.push(`${file}: ${err.message}`);
      }
    }
    console.log(JSON.stringify(bad));
    """
    result = subprocess.run(
        [node(), '--experimental-vm-modules', '-e', script, *[str(p) for p in MODULES]],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f'node could not be run:\n{result.stderr}')
    import json

    bad = json.loads(result.stdout.strip().splitlines()[-1])
    assert not bad, 'these do not parse as modules, which blanks the whole page:\n  ' + '\n  '.join(bad)


def test_the_page_graph_links():
    """Every relative import resolves, and no two modules in it collide.

    Linking rather than parsing, so a rename that leaves one file importing a
    name another no longer exports is caught here rather than as a blank page
    in a browser.
    """
    script = """
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');

    const entry = process.argv[1];
    const cache = new Map();
    const bad = [];

    /* Two passes, and the second one is the point.
       Pass 1 walks the graph and constructs every module, caching it.
       Pass 2 links each of them exactly once.

       Linking as it walked recurses into a dependency that is still linking,
       and node refuses a request for a module "that is not linked" — which
       is every module here, because they all import dom.js. Separating the
       passes means the linker only ever hands back an already-*constructed*
       module and never re-enters one, which is the shape node supports. */
    function collect(file) {
      if (cache.has(file)) return;
      let mod;
      try {
        mod = new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { identifier: file });
      } catch (err) {
        bad.push(`${file}: ${err.message}`);
        return;
      }
      cache.set(file, mod);
      for (const spec of mod.dependencySpecifiers) {
        const target = path.resolve(path.dirname(file), spec);
        if (!fs.existsSync(target)) bad.push(`${file}: cannot resolve ${spec}`);
        else collect(target);
      }
    }
    collect(entry);

    (async () => {
      for (const [file, mod] of cache) {
        try {
          await mod.link((specifier, referencing) => {
            const target = path.resolve(path.dirname(referencing.identifier), specifier);
            const found = cache.get(target);
            if (!found) throw new Error(`cannot resolve ${specifier} from ${referencing.identifier}`);
            return found;
          });
        } catch (err) {
          /* node links a module's dependencies while it links the module, so
             a module reached twice is already linked by the time the loop
             gets to it. That is the graph working, not failing. */
          if (!/already been linked/.test(err.message)) bad.push(`${file}: ${err.message}`);
        }
      }
      console.log(JSON.stringify({ modules: cache.size, bad }));
    })();
    """
    entry = STATIC / 'app.js'
    result = subprocess.run(
        [node(), '--experimental-vm-modules', '-e', script, str(entry)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f'node could not be run:\n{result.stderr}')
    import json

    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert not report['bad'], 'the page graph does not link:\n  ' + '\n  '.join(report['bad'])
    # And it is a real graph rather than one file, so the check is not vacuous.
    assert report['modules'] >= 10, f'only {report["modules"]} modules reached from app.js'


def test_the_new_panes_are_reachable_from_the_entry_point():
    """`mail.js` and `commit.js` exist and parse, but a file nothing imports is
    dead code — the pane would be markup with no behaviour."""
    result = subprocess.run(
        [
            node(), '-e',
            "const fs = require('fs');"
            "const src = fs.readFileSync(process.argv[1], 'utf8');"
            "console.log(JSON.stringify(['mail.js', 'commit.js'].map((f) => [f, src.includes(f)])));",
            str(STATIC / 'app.js'),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f'node could not be run:\n{result.stderr}')
    import json

    reached = dict(json.loads(result.stdout.strip().splitlines()[-1]))
    assert reached == {'mail.js': True, 'commit.js': True}

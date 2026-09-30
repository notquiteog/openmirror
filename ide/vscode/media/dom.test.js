'use strict';

/**
 * Tests for the pure half of `media/dom.js`: the number formatters.
 *
 * These are ported from `openmirror/tui.py` and `openmirror/static/dom.js`
 * rather than written fresh, and that is exactly why they are tested here: two
 * clients of one protocol that disagree about how long something took, or
 * whether 950 tokens is "950" or "1.0k", is the kind of drift nobody notices
 * until somebody trusts the wrong one.
 *
 * No DOM. If one of these needed `document`, the logic would be in the wrong
 * place.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ago, basename, flat, human, kb, say, took, when,
} from './dom.js';

test('flat: whitespace collapses, and a long value is cut with an ellipsis', () => {
  assert.equal(flat('  read   the\n file  ', 90), 'read the file');
  assert.equal(flat('short', 90), 'short');
  const long = 'x'.repeat(200);
  const cut = flat(long, 10);
  assert.equal(cut.length, 10);
  assert.ok(cut.endsWith('...'));
  // The cut must not leave a space behind it.
  assert.equal(flat('abcdefgh ijkl', 9), 'abcdef...');
  // A tool argument that is a number still reads as a number.
  assert.equal(flat(12, 90), '12');
  assert.equal(flat(null, 90), '');
  assert.equal(flat(undefined, 90), '');
});

test('human: the same four bands as tui.py', () => {
  assert.equal(human(0), '0');
  assert.equal(human(999), '999');
  assert.equal(human(1000), '1.0k');
  assert.equal(human(1500), '1.5k');
  assert.equal(human(999999), '1000.0k');
  assert.equal(human(1000000), '1.0M');
  assert.equal(human(2340000), '2.3M');
  // A negative or nonsense count is zero rather than "-1".
  assert.equal(human(-5), '0');
  assert.equal(human('nonsense'), '0');
});

test('took: sub-second, seconds, then minutes and seconds', () => {
  assert.equal(took(12), '12ms');
  assert.equal(took(999), '999ms');
  assert.equal(took(1500), '1.5s');
  assert.equal(took(15000), '15s');
  assert.equal(took(65000), '1m 5s');
  // A duration nobody measured is no duration.
  assert.equal(took(0), '');
  assert.equal(took(-1), '');
  assert.equal(took('x'), '');
});

test('ago: now, minutes, hours, days', () => {
  assert.equal(ago(0), 'now');
  assert.equal(ago(59), 'now');
  assert.equal(ago(60), '1m');
  assert.equal(ago(3599), '59m');
  assert.equal(ago(3600), '1h');
  assert.equal(ago(86399), '23h');
  assert.equal(ago(86400), '1d');
  assert.equal(ago(-10), 'now');
});

test('when: a stored conversation carries a timestamp, not a duration', () => {
  const now = 1700000000;
  assert.equal(when(now - 30, now), 'now');
  assert.equal(when(now - 3600, now), '1h');
  assert.equal(when(now - 86400 * 3, now), '3d');
  assert.equal(when(0, now), '');
  assert.equal(when(null, now), '');
});

test('kb: a file size at the size a person reads it at', () => {
  assert.equal(kb(512), '1kB');
  assert.equal(kb(2048), '2kB');
  assert.equal(kb(5 * 1024 * 1024), '5MB');
  assert.equal(kb(0), '1kB');
});

test('basename: the last segment, for a chip wide enough for one word', () => {
  assert.equal(basename('/work/project'), 'project');
  assert.equal(basename('/work/project/'), 'project');
  assert.equal(basename('project'), 'project');
  assert.equal(basename(''), '');
  assert.equal(basename('/'), '');
});

test('say: null is a thing that is not there yet, not the word null', () => {
  // A fake node is enough: the rule is that the setter never stringifies a
  // missing value into something a person would read.
  const node = { textContent: 'was' };
  say(node, null);
  assert.equal(node.textContent, '');
  say(node, 0);
  assert.equal(node.textContent, '0');
  say(node, 'x');
  assert.equal(node.textContent, 'x');
});

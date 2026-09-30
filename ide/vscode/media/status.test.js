'use strict';

/**
 * Tests for the pure half of `media/status.js`.
 *
 * Three of the four things here exist because of a way the obvious version is
 * wrong, and all three are decisions rather than formatting:
 *
 *   - `windowFraction` follows `openmirror/agent/windows.py`: the compaction
 *     limit wins over the model's window, and there is no fraction at all when
 *     there is no denominator. A bar at 40% on a model that will refuse the
 *     request is worse than no bar, because somebody decides whether to keep
 *     working by looking at it.
 *   - `linkView` keeps `closed` and `failed` apart. One is transient and already
 *     says when it will retry; the other means stop, and its message is shown
 *     verbatim because it already names the fix.
 *   - `emptyState` names the command that starts the daemon. A panel showing an
 *     empty box under a grey dot is a panel somebody has to guess about.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { contextView, emptyState, linkView, sessionRow, windowFraction, FULL_PERCENT, WARN_PERCENT } from './status.js';

test('windowFraction: the compaction limit wins, and no denominator is no bar', () => {
  assert.equal(windowFraction(50000, 100000, 200000), 0.5);
  // The session summarises at 100k whatever the model's window is, so 30% of a
  // 200k window is not the number this conversation is going to hit.
  assert.equal(windowFraction(30000, 100000, 200000), 0.3);
  // With no limit, the model's own window is the denominator.
  assert.equal(windowFraction(50000, 0, 200000), 0.25);
  // With neither, there is nothing to be a fraction of.
  assert.equal(windowFraction(50000, 0, 0), null);
  assert.equal(windowFraction(50000, 0, null), null);
  assert.equal(windowFraction(0, 0, 0), null);
  // A context past the ceiling is clamped rather than drawn off the end of the
  // bar: the session summarises before it gets there, and a 140% bar is a lie.
  assert.equal(windowFraction(140000, 100000, 200000), 1);
});

test('linkView: four states, and two of them are not the same thing', () => {
  const open = linkView({ state: 'open', message: 'attached' });
  assert.equal(open.cls, 'on');
  assert.equal(open.label, 'live');
  assert.equal(open.fatal, false);
  assert.equal(open.title, 'attached');

  // Transient. The message already says when it will retry, so it is shown
  // rather than replaced with something vaguer.
  const closed = linkView({ state: 'closed', message: 'retrying in 4s (attempt 3)' });
  assert.equal(closed.cls, 'retrying');
  assert.equal(closed.fatal, false);
  assert.equal(closed.title, 'retrying in 4s (attempt 3)');

  // Terminal. The host's own message is the fix, so it is shown verbatim.
  const failed = linkView({ state: 'failed', message: 'Start it with `openmirror serve`.' });
  assert.equal(failed.cls, 'failed');
  assert.equal(failed.fatal, true);
  assert.equal(failed.title, 'Start it with `openmirror serve`.');

  // And the state the panel opens in, before the host has said anything.
  const connecting = linkView({ state: 'connecting', message: 'attaching' });
  assert.equal(connecting.cls, 'connecting');
  assert.equal(connecting.fatal, false);
  assert.equal(linkView({}).label, 'connecting');
  assert.equal(linkView(null).label, 'connecting');
  assert.equal(linkView({ state: 'somethingNew' }).fatal, false);
});

test('contextView: no denominator, no bar, but still the number', () => {
  const none = contextView({ tokens: 12000, limit: 0, window: null, totalIn: 5000, totalOut: 300 });
  assert.equal(none.hidden, false);
  assert.equal(none.hasBar, false);
  assert.equal(none.percent, 0);
  assert.equal(none.level, '');
  assert.equal(none.text, '12.0k in context');
  assert.match(none.title, /12\.0k tokens in context/);
  assert.match(none.title, /this session: 5\.0k in, 300 out/);
  assert.match(none.title, /estimated/);

  // With a limit and no window, the limit is the denominator, so the bar is
  // still drawn -- it is the number this session will actually hit.
  const limited = contextView({ tokens: 12000, limit: 100000, totalIn: 1 });
  assert.equal(limited.hasBar, true);
  assert.equal(limited.text, '12%');
  assert.match(limited.title, /summarised at 100\.0k/);
  // A limit of zero is not a limit: it is the daemon saying this session will
  // not summarise, and then there is nothing to be a fraction of.
  const noCeiling = contextView({ tokens: 12000, limit: 0, window: 0, totalIn: 1 });
  assert.equal(noCeiling.hasBar, false);
  assert.equal(noCeiling.text, '12.0k in context');
});

test('contextView: the 75% and 92% thresholds are context.js\'s', () => {
  assert.equal(WARN_PERCENT, 75);
  assert.equal(FULL_PERCENT, 92);
  const at = (percent) => contextView({ tokens: percent * 1000, limit: 100000 });
  assert.equal(at(10).level, '');
  assert.equal(at(74).level, '');
  assert.equal(at(75).level, 'warn');
  assert.equal(at(91).level, 'warn');
  assert.equal(at(92).level, 'full');
  assert.equal(at(100).level, 'full');
  assert.equal(at(75).text, '75%');
  assert.equal(at(75).hasBar, true);
  // The warning is in the tooltip as well as the colour, because colour is
  // never the only carrier.
  assert.match(at(92).title, /Click to compact/);
});

test('contextView: the frame the host sends, and the event the turn carries', () => {
  // `PROTOCOL.md` names the totals `totalIn` / `totalOut` and the daemon names
  // them `total_in` / `total_out`; the host sends both and both are read.
  const frame = contextView({ tokens: 90000, limit: 100000, window: 200000, totalIn: 5, totalOut: 6, exact: true });
  assert.equal(frame.percent, 90);
  assert.equal(frame.level, 'warn');
  assert.match(frame.title, /counted by the model/);
  const event = contextView({ tokens: 90000, limit: 100000, window: 200000, total_in: 5, total_out: 6, exact: true });
  assert.deepEqual(event, frame);
  // The host's own `fraction` is used when it is there, which is the same
  // arithmetic computed on the daemon's side of the wire.
  const given = contextView({ tokens: 1, limit: 100, fraction: 0.42 });
  assert.equal(given.percent, 42);
  assert.equal(contextView({ tokens: 1, limit: 100, fraction: 9 }).percent, 100);
  // An empty conversation hides the meter rather than showing an empty bar.
  assert.equal(contextView({ tokens: 0, limit: 100000 }).hidden, true);
  assert.equal(contextView({ tokens: 0, totalIn: 0, totalOut: 0 }).hidden, true);
  assert.equal(contextView(null).hidden, true);
  assert.equal(contextView(undefined).hidden, true);
});

test('sessionRow: the title, and enough about it to choose between two', () => {
  const now = 1700000000;
  const stored = { id: 's1', title: 'the parser', root: '/work/openmirror', updated: now - 7200, turns: 3 };
  const row = sessionRow(stored, now);
  assert.equal(row.id, 's1');
  assert.equal(row.title, 'the parser');
  assert.equal(row.where, 'openmirror');
  assert.equal(row.when, '2h');
  assert.equal(row.detail, '3 turns  -  2h  -  openmirror');
  // The label is what a screen reader gets, and it says the same thing the row
  // shows rather than repeating the id.
  assert.equal(row.label, 'the parser, 2h ago');
  // An untitled conversation is identified by its id rather than by nothing.
  const untitled = sessionRow({ id: 's2', root: '/work', updated: now - 30, turns: 1 }, now);
  assert.equal(untitled.title, 's2');
  assert.equal(untitled.detail, '1 turn  -  now  -  work');
  // Nothing at all is one empty row, not a crash.
  const nothing = sessionRow(null, now);
  assert.equal(nothing.id, '');
  assert.equal(nothing.title, '');
  assert.equal(nothing.detail, '0 turns');
});

test('emptyState: a daemon that is not running gets the command that starts it', () => {
  const away = 'http://127.0.0.1:8477 is not answering';
  const down = emptyState({ state: 'failed', message: away }, { root: '/work/app' });
  assert.match(down.ask, /openmirror is not running for app/);
  assert.match(down.sub, /openmirror serve/);
  // The host's own message is shown rather than paraphrased: it already names
  // the fix, and a second wording of the same fix is a second thing to be wrong.
  assert.equal(down.reason, away);
  // A token the daemon will not accept is the other way to be down, and it says
  // so in the same shape.
  const wrong = emptyState({ state: 'failed', message: 'that is not the token' }, {});
  assert.equal(wrong.ask, 'openmirror is not running.');
  assert.match(wrong.sub, /openmirror serve/);

  // Connecting is not down, and an empty box under a spinner is a box that says
  // nothing about whether anything is wrong.
  const starting = emptyState({ state: 'connecting' }, { session: true, root: '/work/app' });
  assert.match(starting.ask, /What should we do in app\?/);
  assert.equal(starting.reason, '');

  // No conversation yet: the panel is fine, there is simply nothing to talk to.
  const none = emptyState({ state: 'open' }, { session: false });
  assert.match(none.ask, /No conversation yet/);

  // The ordinary empty session, naming the folder the agent is pointed at.
  const ready = emptyState({ state: 'open' }, { session: true, root: '/work/openmirror', model: 'claude-sonnet-4' });
  assert.equal(ready.ask, 'What should we do in openmirror?');
  assert.equal(ready.sub, 'claude-sonnet-4');
});

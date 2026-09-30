'use strict';

/**
 * Tests for the pure half of `media/slash.js`.
 *
 * The rules being pinned down are the ones that are easy to get subtly wrong
 * and impossible to notice: a menu that closes when a space is typed (so
 * `/model gpt-5` never offers anything), an Enter that completes a name you had
 * already finished (so `/compact` runs instead of becoming `/compact `), and a
 * ranking that quietly reorders what the daemon already ranked.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { completionText, filterCommands, slashQuery, slashSends } from './slash.js';

const COMMANDS = [
  { name: 'compact', description: 'summarise the conversation', kind: 'command', hint: '' },
  { name: 'clear', description: 'start again', kind: 'command', hint: '' },
  { name: 'model', description: 'switch model', kind: 'command', hint: '' },
  { name: 'think', description: 'how hard to think', kind: 'command', hint: '' },
  { name: 'git-review', description: 'review the diff', kind: 'skill', hint: 'skill' },
  { name: 'reindex', description: 'reindex the store', kind: 'command', hint: '' },
];

test('slashQuery: only while the name itself is being typed', () => {
  assert.equal(slashQuery('/'), '');
  assert.equal(slashQuery('/comp'), 'comp');
  assert.equal(slashQuery('/git-review'), 'git-review');
  assert.equal(slashQuery('/think:high'), 'think:high');
  // Case is folded so `/Compact` finds `compact`.
  assert.equal(slashQuery('/COMP'), 'comp');
  // Once there is a space the rest is the command's arguments, and the menu has
  // nothing left to offer.
  assert.equal(slashQuery('/compact '), null);
  assert.equal(slashQuery('/compact now'), null);
  // Not at the start of the box, and not a path.
  assert.equal(slashQuery('ask me to /compact'), null);
  assert.equal(slashQuery('a/b'), null);
  assert.equal(slashQuery(''), null);
  assert.equal(slashQuery(null), null);
});

test('filterCommands: prefixes first, then what merely contains the query', () => {
  // Both start with `c`, so the daemon's own order decides between them.
  assert.deepEqual(filterCommands(COMMANDS, 'c').map((c) => c.name), ['compact', 'clear']);
  // `re` matches one by prefix and one by substring, and the prefix comes first
  // however far down the list it was.
  assert.deepEqual(filterCommands(COMMANDS, 're').map((c) => c.name), ['reindex', 'git-review']);
  assert.deepEqual(filterCommands(COMMANDS, 'zzz'), []);
  // A query of nothing keeps the daemon's own order, which is its ranking.
  assert.deepEqual(filterCommands(COMMANDS, '').map((c) => c.name), COMMANDS.map((c) => c.name));
  // Case-insensitive, because the query is folded.
  assert.deepEqual(filterCommands(COMMANDS, 'COMP').map((c) => c.name), ['compact']);
  // A command with no name does not become `undefined` in the list.
  assert.deepEqual(filterCommands([{ description: 'x' }], 'x'), []);
  // Eight is enough to scan in a panel, and the limit is a parameter so the
  // rule is checkable rather than a magic number in a slice.
  const many = Array.from({ length: 20 }, (_, i) => ({ name: `c${i}` }));
  assert.equal(filterCommands(many, 'c').length, 8);
  assert.equal(filterCommands(many, 'c', 3).length, 3);
  // A missing list is an empty menu, not a crash.
  assert.deepEqual(filterCommands(null, 'c'), []);
});

test('completionText: a trailing space, so the next word is not glued on', () => {
  assert.equal(completionText({ name: 'compact' }), '/compact ');
  assert.equal(completionText({}), '/ ');
  assert.equal(completionText(null), '/ ');
});

test('slashSends: Enter completes a half-typed name and runs a finished one', () => {
  const compact = { name: 'compact' };
  // `/compact` in full and Enter should run it, not become `/compact `.
  assert.equal(slashSends('/compact', compact), true);
  assert.equal(slashSends('/COMPACT', compact), true);
  // A partial name completes instead.
  assert.equal(slashSends('/comp', compact), false);
  assert.equal(slashSends('/', compact), false);
  // Text that is not a command at all never sends from the menu.
  assert.equal(slashSends('hello', compact), false);
  assert.equal(slashSends('/compact ', compact), false);
  // With nothing selected there is nothing to complete.
  assert.equal(slashSends('/comp', null), false);
});

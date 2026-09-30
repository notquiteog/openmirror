'use strict';

/**
 * Tests for the pure half of `media/mentions.js`.
 *
 * Three of the four things this file does are arithmetic on a string, and the
 * fourth -- dropping a reply that has been overtaken -- is the one that decides
 * whether the menu is trustworthy. All of them are here, with no DOM.
 *
 * The one that is easiest to get wrong is `mentionAt`: an email address
 * mid-sentence is not a file mention, and treating it as one turns every
 * message about somebody's address into a suggestion list.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { accepts, completion, mentionAt, mentionSends } from './mentions.js';

test('mentionAt: an @ that starts a word, and only the text up to the caret', () => {
  assert.deepEqual(mentionAt('@', 1), { at: 0, query: '' });
  assert.deepEqual(mentionAt('@src', 4), { at: 0, query: 'src' });
  assert.deepEqual(mentionAt('look at @src/host', 17), { at: 8, query: 'src/host' });
  // Dots, dashes and underscores are all in a path.
  assert.deepEqual(mentionAt('see @a-b_c.d/e', 14), { at: 4, query: 'a-b_c.d/e' });
  // A caret in the middle: only what is before it is being typed.
  assert.deepEqual(mentionAt('@src rest of it', 4), { at: 0, query: 'src' });
});

test('mentionAt: the cases that are not a mention', () => {
  // An email address is not a file.
  assert.equal(mentionAt('write to someone@example.com', 25), null);
  // Nor is an @ that does not start a word.
  assert.equal(mentionAt('a@b', 3), null);
  // A finished mention is not being typed.
  assert.equal(mentionAt('@src/host.js and then', 18), null);
  // A space ends it.
  assert.equal(mentionAt('@src/host.js x', 20), null);
  // Nothing typed, nothing mentioned.
  assert.equal(mentionAt('', 0), null);
  assert.equal(mentionAt(null, null), null);
  // No caret means the end of the text.
  assert.deepEqual(mentionAt('@src'), { at: 0, query: 'src' });
});

test('completion: the box with the path in it, and where the caret goes', () => {
  const found = mentionAt('look at @sr', 11);
  assert.deepEqual(completion(found, 'src/host.js', 'look at @sr'), {
    text: 'look at @src/host.js ',
    caret: 21,
  });
  // A caret in the middle: the tail after the mention is kept.
  const mid = mentionAt('@sr and then this', 3);
  assert.deepEqual(completion(mid, 'src/host.js', '@sr and then this'), {
    text: '@src/host.js  and then this',
    caret: 13,
  });
});

test('mentionSends: Enter completes a partial path and sends a complete one', () => {
  const item = { path: 'src/host.js' };
  assert.equal(mentionSends({ at: 0, query: 'src' }, item), false);
  assert.equal(mentionSends({ at: 0, query: 'src/host.js' }, item), true);
  assert.equal(mentionSends({ at: 0, query: 'src/host' }, item), false);
  // Case is not folded for a path: a path is a path.
  assert.equal(mentionSends({ at: 0, query: 'SRC/HOST.JS' }, item), false);
  assert.equal(mentionSends(null, item), false);
  assert.equal(mentionSends({ at: 0, query: 'src' }, null), false);
});

test('accepts: a reply is dropped once the query it was for is no longer in the box', () => {
  // The ordinary case: the reply is for what is being typed now.
  assert.equal(accepts('app', 'app'), true);
  // A reply for `app` is no use to a box that has been emptied: the list for
  // the empty query is a different list.
  assert.equal(accepts('app', ''), false);
  // The user typed on while the request was in the air. Painting this over the
  // newer list is how a menu shows `app.js` for a query that is already
  // `apple`.
  assert.equal(accepts('app', 'apple'), false);
  // The mention was deleted while the request was in the air.
  assert.equal(accepts('app', null), false);
  assert.equal(accepts('app', undefined), false);
  // Nothing was ever asked, so nothing is welcome.
  assert.equal(accepts(null, 'app'), false);
  assert.equal(accepts(undefined, undefined), false);
  // An empty query is a real query -- it is what `@@`-free typing at the start
  // of the box sends, and the answer to it is the top of the tree.
  assert.equal(accepts('', ''), true);
});

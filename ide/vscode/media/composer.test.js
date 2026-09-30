'use strict';

/**
 * Tests for the pure half of `media/composer.js`.
 *
 * The frame builders are here rather than inline in the submit handler because
 * they are the seam between this panel and the host, and a frame with the wrong
 * field name is a message that does nothing at all: `callId` instead of
 * `call_id` is not an error anybody sees, it is a tool call that waits for an
 * approval that never comes.
 *
 * The other thing being pinned down is the rule that sending is never blocked
 * while a turn is running, and the two things that follow from it -- a picture
 * is a message on its own, and the send button says what will happen to it.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  answerFrame, approvalFrame, canSend, denyFrame, echoText, EFFORT_LEVELS, MODES,
  sendTitle, stopTitle, submitFrame,
} from './composer.js';

test('submitFrame: the fields the host reads, and no empty attachment list', () => {
  assert.deepEqual(submitFrame('do the thing'), { t: 'submit', text: 'do the thing' });
  // Omitted rather than `[]`, which is the same thing to the daemon and one
  // less key in every message somebody types into a panel with no picture on it.
  assert.deepEqual(submitFrame('do the thing', []), { t: 'submit', text: 'do the thing' });
  const attachments = [{ type: 'image', data: 'AAAA', media_type: 'image/png' }];
  assert.deepEqual(submitFrame('look at this', attachments), {
    t: 'submit',
    text: 'look at this',
    attachments,
  });
  // A picture on its own: a complete message with no words.
  assert.deepEqual(submitFrame('', attachments), {
    t: 'submit',
    text: '',
    attachments,
  });
  // Missing is not the same as null, and neither is the string "undefined".
  assert.deepEqual(submitFrame(null, null), { t: 'submit', text: '' });
  assert.deepEqual(submitFrame(42), { t: 'submit', text: '42' });
});

test('approvalFrame: callId, remember, and nothing that looks like a token', () => {
  assert.deepEqual(approvalFrame('c-1', true), { t: 'approve', callId: 'c-1', remember: true });
  assert.deepEqual(approvalFrame('c-1'), { t: 'approve', callId: 'c-1', remember: false });
  // Anything truthy is a yes and anything else is a no, because the host
  // Boolean()s it anyway and a string "false" reaching the socket would be a
  // decision nobody made.
  assert.equal(approvalFrame('c-1', 'yes').remember, true);
  assert.equal(approvalFrame('c-1', 0).remember, false);
  assert.equal(approvalFrame(null, true).callId, '');
});

test('denyFrame: the reason goes back to the model as the tool result', () => {
  assert.deepEqual(denyFrame('c-1', 'declined in the panel'), {
    t: 'deny',
    callId: 'c-1',
    reason: 'declined in the panel',
  });
  // A denial with no reason is legal, and is not the string "undefined".
  assert.deepEqual(denyFrame('c-1'), { t: 'deny', callId: 'c-1', reason: '' });
});

test('answerFrame: an ask_user question, answered once', () => {
  assert.deepEqual(answerFrame('q-1', 'the second one'), {
    t: 'answer',
    questionId: 'q-1',
    answer: 'the second one',
  });
  assert.deepEqual(answerFrame('q-1'), { t: 'answer', questionId: 'q-1', answer: '' });
  // The answer is the model's to read, so it is sent as typed rather than
  // trimmed: a trailing newline in a file path somebody pasted is not a
  // different path to the tool that receives it.
  assert.equal(answerFrame('q-1', '  yes  ').answer, '  yes  ');
});

test('canSend: words, or a picture, or both', () => {
  assert.equal(canSend('hello', 0), true);
  assert.equal(canSend('hello', 2), true);
  // A picture is a message on its own. Refusing to send it because there are no
  // words would make the feature work only where it is least needed.
  assert.equal(canSend('', 1), true);
  // Whitespace is not a message.
  assert.equal(canSend('   ', 0), false);
  assert.equal(canSend('\n\t', 0), false);
  assert.equal(canSend('', 0), false);
  assert.equal(canSend(null, null), false);
  assert.equal(canSend(null, undefined), false);
});

test('echoText: what the local copy of the turn says', () => {
  // The server's `turn.started` replaces the echo, so a live panel and a
  // reattached one show the same conversation. With a picture and no words the
  // echo has to say something, or the local copy of the turn is a blank bubble
  // that vanishes a moment later.
  assert.equal(echoText('do the thing', 0), 'do the thing');
  assert.equal(echoText('', 1), '1 picture');
  assert.equal(echoText('  ', 2), '2 pictures');
  assert.equal(echoText('do the thing', 1), 'do the thing');
});

test('the two tooltips say what will happen, which is the whole of the policy', () => {
  // Send stays live while a turn is running, and the tooltip is where that is
  // said: the daemon holds the message and runs it when the turn ends.
  assert.equal(sendTitle(false), 'Send');
  assert.match(sendTitle(true), /Queue this/);
  assert.match(sendTitle(true), /when the turn in progress finishes/);
  // Stop says what it does to what. A stop that read as "cancel the
  // conversation" would be the wrong promise.
  assert.equal(stopTitle(false), '');
  assert.match(stopTitle(true), /Stop this turn/);
  assert.match(stopTitle(true), /conversation stays/);
});

test('the two pickers list what the daemon takes', () => {
  // The approval modes are the six `openmirror/agent/approval.py` defines, and
  // the host recognises exactly these names -- a picker offering a seventh would
  // send a value the host drops on the floor.
  assert.deepEqual(MODES.map(([value]) => value), [
    'read_only', 'plan', 'ask', 'auto_edit', 'trusted', 'unrestricted',
  ]);
  for (const [value, label] of MODES) {
    assert.ok(value && label, 'a mode with no label is a blank row');
  }
  // The thinking levels are the six the agent takes, plus `default`, which
  // hands the choice back to the model.
  assert.deepEqual(EFFORT_LEVELS.map(([value]) => value), [
    '', 'off', 'low', 'medium', 'high', 'xhigh', 'max',
  ]);
  for (const [value, label] of EFFORT_LEVELS) {
    assert.ok(value !== undefined && label, 'a level with no label is a blank row');
  }
  // A `default` row is the empty value, not the word "default": the daemon
  // reads null as the model's own choice.
  assert.equal(EFFORT_LEVELS[0][0], '');
});

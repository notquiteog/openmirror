'use strict';

/**
 * Tests for the pure half of `media/pictures.js`.
 *
 * The part that matters is the shape of the `attachments` array on the `submit`
 * frame, because it is the one place where a wrong field name produces a turn
 * that runs with no picture and no error anybody can see. The rest is the
 * downscale arithmetic, which is a real decision rather than a detail: the long
 * edge goes to 1568px because that is the size every major provider recommends,
 * and a 2560x1440 screenshot is 14MB of pixels that become 19MB of base64 in
 * one frame and then cross `postMessage` into an extension host before they
 * cross a socket.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  attachmentFrame, MAX_EDGE, normaliseType, outType, refusal, scaleFor,
} from './pictures.js';

test('normaliseType: the four types, and jpg spelled the other way', () => {
  assert.equal(normaliseType('image/png'), 'image/png');
  assert.equal(normaliseType('image/jpeg'), 'image/jpeg');
  assert.equal(normaliseType('image/jpg'), 'image/jpeg');
  assert.equal(normaliseType('image/webp'), 'image/webp');
  assert.equal(normaliseType('image/gif'), 'image/gif');
  assert.equal(normaliseType('IMAGE/PNG'), 'image/png');
  // Everything else is refused rather than guessed at.
  for (const bad of ['application/pdf', 'text/plain', 'image/svg+xml', '', null, undefined]) {
    assert.equal(normaliseType(bad), null, `${bad} should not be sent`);
  }
});

test('scaleFor: only a picture that is too big is scaled', () => {
  assert.equal(MAX_EDGE, 1568);
  // A screenshot of a terminal is usually already small enough, and the common
  // case should cost nothing.
  assert.equal(scaleFor(1280, 800), null);
  assert.equal(scaleFor(1568, 1568), null);
  // A 2560x1440 screenshot comes back as 1568x882, and the long edge is what
  // the ceiling applies to rather than the width.
  assert.deepEqual(scaleFor(2560, 1440), { width: 1568, height: 882 });
  // Portrait: the long edge is the height.
  assert.deepEqual(scaleFor(1440, 2560), { width: 882, height: 1568 });
  // A square stays square.
  assert.deepEqual(scaleFor(4000, 4000), { width: 1568, height: 1568 });
  // The ceiling is a parameter so the rule is checkable rather than a constant.
  assert.deepEqual(scaleFor(2000, 1000, 1000), { width: 1000, height: 500 });
  // An image whose size nobody knows is not resized into nothing.
  assert.equal(scaleFor(0, 0), null);
  assert.equal(scaleFor(null, undefined), null);
  // Never a zero dimension: a canvas with one is a canvas that throws.
  const thin = scaleFor(20000, 1);
  assert.equal(thin.height, 1);
  assert.equal(thin.width, MAX_EDGE);
});

test('outType: PNG stays PNG, everything else becomes JPEG', () => {
  // Re-encoding a photo as PNG makes it bigger, and a screenshot of text is the
  // case that matters and the one that compresses.
  assert.equal(outType('image/png'), 'image/png');
  assert.equal(outType('image/webp'), 'image/jpeg');
  assert.equal(outType('image/gif'), 'image/jpeg');
});

test('attachmentFrame: exactly the shape session.py reads', () => {
  const items = [
    { data: 'AAAA', media_type: 'image/png', bytes: 3, preview: 'PREVIEW' },
    { data: 'BBBB', media_type: 'image/jpeg', bytes: 3 },
  ];
  assert.deepEqual(attachmentFrame(items), [
    { type: 'image', data: 'AAAA', media_type: 'image/png' },
    { type: 'image', data: 'BBBB', media_type: 'image/jpeg' },
  ]);
  // Bare base64 with no `data:` prefix: the prefix is a URL convention and the
  // daemon's reader would take it for part of the image.
  for (const item of attachmentFrame(items)) {
    assert.ok(!item.data.startsWith('data:'));
  }
  // The preview is what the page shows, and it is not in the frame: it is a few
  // hundred kilobytes of base64 per message for something with no use outside
  // this process.
  assert.ok(!('preview' in attachmentFrame(items)[0]));
  assert.ok(!('bytes' in attachmentFrame(items)[0]));
  // Nothing attached is an empty array, and the composer omits the key anyway.
  assert.deepEqual(attachmentFrame([]), []);
  assert.deepEqual(attachmentFrame(null), []);
  // A missing type is png rather than `undefined`, so the daemon's reader does
  // not have to guess.
  assert.deepEqual(attachmentFrame([{ data: 'x' }]), [{ type: 'image', data: 'x', media_type: 'image/png' }]);
});

test('refusal: named, because a file that vanishes reads as a broken panel', () => {
  const png = { name: 'shot.png', type: 'image/png', size: 1024 };
  assert.equal(refusal(png), '');
  assert.match(refusal({ name: 'notes.pdf', type: 'application/pdf', size: 10 }), /notes\.pdf is not a picture/);
  // A name is optional and the sentence still has to read.
  assert.match(refusal({ type: 'text/plain', size: 10 }), /That file is not a picture/);
  assert.match(
    refusal({ name: 'huge.png', type: 'image/png', size: 40 * 1024 * 1024 }),
    /huge\.png is 40MB - too large to send/,
  );
  // The type is checked before the size, so a 40MB PDF is reported as the wrong
  // kind of file rather than as a big one.
  assert.match(
    refusal({ name: 'huge.pdf', type: 'application/pdf', size: 40 * 1024 * 1024 }),
    /is not a picture/,
  );
  assert.equal(refusal(null), 'That file is not a picture. PNG, JPEG, WebP or GIF.');
});

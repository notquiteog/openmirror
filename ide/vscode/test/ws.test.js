/* Tests for the websocket client.
 *
 * Run with: node --test ide/vscode/test/
 *
 * The framing tests never open a socket. They feed hand-built bytes to the
 * parser and the frame builder, because a framing bug that only shows up
 * through a real TCP connection is a bug that shows up once, on a machine
 * with a slow disk, in somebody else's bug report. The handshake tests do use
 * a socket, but the server is a net.createServer driven by hand rather than a
 * library, so that it can answer wrongly on purpose: a real server always
 * computes the right accept, which is exactly the thing that has to be proved
 * wrong at least once.
 *
 * No test framework. node:test and node:assert/strict are in the runtime the
 * extension already ships, and a test suite that needs an install step is a
 * test suite that does not get run.
 */

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');
const child = require('node:child_process');

const {
  WebSocketClient,
  WebSocketError,
  CONNECTING,
  OPEN,
  CLOSING,
  CLOSED,
  // The underscore prefix is the source file's own note that these are
  // internal but testable. The handshake tests need to answer with a wrong
  // accept, and the framing tests need to build frames byte by byte; neither
  // is possible through the public API.
  _accept,
  _encode,
  _Parser,
  _GUID,
} = require('../src/ws');

/* ---- the constants the tests build frames with ---- */

const OP = { CONT: 0x0, TEXT: 0x1, BINARY: 0x2, CLOSE: 0x8, PING: 0x9, PONG: 0xa };

/* ---- helpers ---- */

/* A server-to-client frame: unmasked, because a server must not mask. */
function serverFrame(opcode, payload, options) {
  const opts = Object.assign({ mask: false }, options || {});
  return _encode(opcode, Buffer.isBuffer(payload) ? payload : Buffer.from(payload), opts);
}

function closeBody(code, reason) {
  const text = Buffer.from(reason || '', 'utf8');
  const body = Buffer.allocUnsafe(2 + text.length);
  body.writeUInt16BE(code, 0);
  text.copy(body, 2);
  return body;
}

/* A parser wired to a plain object, so a test can assert on what came out
 * with no socket, no event emitter and no clock in the way. */
function collector(options) {
  const seen = { text: [], ping: [], pong: [], close: [], error: [] };
  const parser = new _Parser(Object.assign({
    onText: function (text) { seen.text.push(text); },
    onPing: function (payload) { seen.ping.push(payload); },
    onPong: function (payload) { seen.pong.push(payload); },
    onClose: function (code, reason) { seen.close.push([code, reason]); },
    onError: function (err, code) { seen.error.push([err, code]); },
  }, options || {}));
  return { parser: parser, seen: seen };
}

/* The one protocol error, as [error, closeCode]. Asserting there is exactly
 * one is part of the assertion: a parser that reports the same frame twice
 * would otherwise pass every other test here. */
function oneError(seen) {
  assert.equal(seen.error.length, 1, 'expected exactly one protocol error, got ' + seen.error.length);
  return seen.error[0];
}

/* Every event a client can produce, in one place, so a test can say "one
 * close and no errors" without four separate listeners. */
function watch(ws) {
  const seen = { open: 0, message: [], close: [], error: [] };
  ws.on('open', function () { seen.open++; });
  ws.on('message', function (data) { seen.message.push(data); });
  ws.on('close', function (code, reason) { seen.close.push([code, reason]); });
  ws.on('error', function (err) { seen.error.push(err); });
  return seen;
}

function once(emitter, event) {
  return new Promise(function (resolve) {
    emitter.once(event, function () {
      resolve(Array.prototype.slice.call(arguments));
    });
  });
}

/* Wait for a condition over the recorded events.
 *
 * This exists because an `error` and a `close` are emitted in the same tick,
 * in that order, and a test that awaits them one at a time has already missed
 * the second one by the time it starts listening for it. The order is part of
 * the contract being tested; the test must be able to observe both without
 * racing itself. */
function until(ws, seen, predicate, label) {
  return new Promise(function (resolve, reject) {
    const timer = setTimeout(function () {
      stop();
      reject(new Error('timed out waiting for ' + label));
    }, 5000);
    function check() {
      if (!predicate(seen)) return;
      stop();
      resolve(seen);
    }
    function stop() {
      clearTimeout(timer);
      ws.removeListener('open', check);
      ws.removeListener('message', check);
      ws.removeListener('close', check);
      ws.removeListener('error', check);
    }
    ws.on('open', check);
    ws.on('message', check);
    ws.on('close', check);
    ws.on('error', check);
    check();
  });
}

function tick() {
  return new Promise(function (resolve) { setImmediate(resolve); });
}

/* ---- the server side of the loopback tests ---- */

/* Read one frame off a buffer, or null if it does not hold a whole one yet.
 * Deliberately a second implementation: the client's parser refuses masked
 * frames, so it cannot be the thing that checks the client masked properly. */
function takeFrame(buf) {
  if (buf.length < 2) return null;
  const fin = (buf[0] & 0x80) !== 0;
  const opcode = buf[0] & 0x0f;
  const masked = (buf[1] & 0x80) !== 0;
  let length = buf[1] & 0x7f;
  let offset = 2;
  if (length === 126) {
    if (buf.length < 4) return null;
    length = buf.readUInt16BE(2);
    offset = 4;
  } else if (length === 127) {
    if (buf.length < 10) return null;
    length = Number(buf.readBigUInt64BE(2));
    offset = 10;
  }
  if (!masked) throw new Error('a client frame arrived unmasked');
  if (buf.length < offset + 4) return null;
  const key = buf.subarray(offset, offset + 4);
  offset += 4;
  if (buf.length < offset + length) return null;
  const payload = Buffer.from(buf.subarray(offset, offset + length));
  for (let i = 0; i < payload.length; i++) payload[i] ^= key[i & 3];
  return {
    frame: { fin: fin, opcode: opcode, payload: payload, masked: masked },
    rest: buf.subarray(offset + length),
  };
}

/* The hand-driven server connection: parses the HTTP request, then frames,
 * and hands out waiters so a test can await the next one. */
function Conn(socket) {
  this.socket = socket;
  this.buf = Buffer.alloc(0);
  this.head = null;
  this.frames = [];
  this.closed = false;
  this._headWaiter = null;
  this._frameWaiters = [];
  const self = this;
  socket.on('data', function (chunk) {
    self.buf = Buffer.concat([self.buf, chunk]);
    self._pump();
  });
  // A test that ends with the socket still open is not a failure, and an
  // unhandled 'error' on a server socket takes the whole runner down with it.
  socket.on('error', function () {});
  socket.on('close', function () { self.closed = true; });
}

Conn.prototype._pump = function () {
  if (!this.head) {
    const at = this.buf.indexOf('\r\n\r\n');
    if (at === -1) return;
    this.head = this.buf.subarray(0, at).toString('latin1');
    this.buf = this.buf.subarray(at + 4);
    if (this._headWaiter) {
      const waiter = this._headWaiter;
      this._headWaiter = null;
      waiter(this.head);
    }
  }
  for (;;) {
    const taken = takeFrame(this.buf);
    if (!taken) return;
    this.buf = taken.rest;
    // Handed to a waiter or left in the queue, never both. A frame that is
    // given to an awaiting test and also left in the queue comes back a second
    // time, which would look exactly like the client sending it twice.
    if (this._frameWaiters.length) this._frameWaiters.shift()(taken.frame);
    else this.frames.push(taken.frame);
  }
};

Conn.prototype.request = function () {
  const self = this;
  if (this.head) return Promise.resolve(this.head);
  return new Promise(function (resolve) { self._headWaiter = resolve; });
};

Conn.prototype.nextFrame = function () {
  const self = this;
  if (this.frames.length) return Promise.resolve(this.frames.shift());
  return new Promise(function (resolve) { self._frameWaiters.push(resolve); });
};

/* Run `body` against a server on an ephemeral loopback port, then take the
 * server and every socket it accepted back down, so the runner can exit.
 *
 * `onConnection(conn, socket)` is called per accepted connection. The client
 * connects asynchronously, so the body asks for the connections it needs
 * rather than reading an array that may not be filled yet: `ctx.connections(1)`
 * is the first client, `ctx.connections(2)` the second. */
async function withServer(onConnection, body) {
  const sockets = new Set();
  const conns = [];
  const waiters = [];
  const server = net.createServer(function (socket) {
    sockets.add(socket);
    socket.on('close', function () { sockets.delete(socket); });
    const conn = new Conn(socket);
    conns.push(conn);
    onConnection(conn, socket);
    const waiter = waiters[conns.length - 1];
    if (waiter) {
      waiters[conns.length - 1] = null;
      waiter(conn);
    }
  });
  await new Promise(function (resolve) { server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  const ctx = {
    server: server,
    port: port,
    sockets: sockets,
    conns: conns,
    url: 'ws://127.0.0.1:' + port + '/ws/agent',
    // Timed, because a client that never arrives would otherwise leave the
    // runner waiting rather than failing.
    connections: function (n) {
      if (conns.length >= n) return Promise.resolve(conns[n - 1]);
      return new Promise(function (resolve, reject) {
        const timer = setTimeout(function () {
          reject(new Error('connection ' + n + ' never arrived'));
        }, 5000);
        waiters[n - 1] = function (conn) {
          clearTimeout(timer);
          resolve(conn);
        };
      });
    },
  };
  try {
    return await body(ctx);
  } finally {
    for (const socket of sockets) socket.destroy();
    await new Promise(function (resolve) { server.close(resolve); });
  }
}

/* What a real server answers, computed from the key the client sent. */
function acceptReply(request, extra) {
  const key = header(request, 'sec-websocket-key');
  assert.ok(key, 'the client sent no Sec-WebSocket-Key');
  return [
    'HTTP/1.1 101 Switching Protocols',
    'upgrade: websocket',
    'connection: Upgrade',
    'sec-websocket-accept: ' + _accept(key),
  ].concat(extra || []).join('\r\n') + '\r\n\r\n';
}

function header(request, name) {
  const at = new RegExp('^' + name + ':\\s*(.*)$', 'im').exec(request);
  return at ? at[1].trim() : null;
}

/* A port that nothing is listening on. Bound, noted, released. */
async function deadPort() {
  const probe = net.createServer();
  await new Promise(function (resolve) { probe.listen(0, '127.0.0.1', resolve); });
  const port = probe.address().port;
  await new Promise(function (resolve) { probe.close(resolve); });
  return port;
}

/* ---- the accept key ---- */

test('the accept key is the one from RFC 6455 section 1.3', function () {
  // The worked example, verbatim. If this is wrong then every handshake
  // against every server fails, so it is the one number in this file checked
  // against the document rather than against itself.
  assert.equal(_accept('dGhlIHNhbXBsZSBub25jZQ=='), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=');
});

test('the accept key is derived from the key, not copied from it', function () {
  assert.equal(_GUID, '258EAFA5-E914-47DA-95CA-C5AB0DC85B11');
  assert.notEqual(_accept('AAAAAAAAAAAAAAAAAAAAAA=='), 'AAAAAAAAAAAAAAAAAAAAAA==');
  assert.notEqual(_accept('AAAAAAAAAAAAAAAAAAAAAA=='), _accept('BBBBBBBBBBBBBBBBBBBBBBB=='));
});

/* ---- building frames ---- */

test('a client frame sets FIN and the mask bit', function () {
  const frame = _encode(OP.TEXT, Buffer.from('hi'), { maskKey: Buffer.from([1, 2, 3, 4]) });
  // 0x81 is FIN plus the text opcode, 0x82 is the mask bit and a 2 byte
  // payload.
  assert.equal(frame[0], 0x81);
  // The mask bit is the whole point of this assertion. A conforming server
  // must drop an unmasked client frame and need not say anything while it
  // does, so an unmasked send is a no-op that looks like it worked.
  assert.equal(frame[1] & 0x80, 0x80);
  assert.equal(frame[1] & 0x7f, 2);
  assert.deepEqual(Array.from(frame.subarray(2, 6)), [1, 2, 3, 4]);
});

test('the mask really changes the payload bytes', function () {
  const key = [0xde, 0xad, 0xbe, 0xef];
  const frame = _encode(OP.TEXT, Buffer.from('hello'), { maskKey: Buffer.from(key) });
  assert.notEqual(frame.subarray(6).toString(), 'hello');
  const unmasked = Buffer.from(frame.subarray(6));
  for (let i = 0; i < unmasked.length; i++) unmasked[i] ^= key[i & 3];
  assert.equal(unmasked.toString(), 'hello');
});

test('every frame gets a fresh mask key', function () {
  const a = _encode(OP.TEXT, Buffer.from('same'));
  const b = _encode(OP.TEXT, Buffer.from('same'));
  assert.notEqual(a.toString('hex'), b.toString('hex'));
});

test('a short payload carries its own length in seven bits', function () {
  const frame = serverFrame(OP.TEXT, 'abc');
  assert.equal(frame.length, 5);
  assert.equal(frame[0], 0x81);
  assert.equal(frame[1], 3);
  assert.equal(frame.subarray(2).toString(), 'abc');
});

test('a 16-bit length is escaped with 126 and two bytes', function () {
  const frame = serverFrame(OP.TEXT, Buffer.alloc(1000, 0x61));
  assert.equal(frame[1], 126);
  assert.equal(frame.readUInt16BE(2), 1000);
  assert.equal(frame.length, 4 + 1000);
  // 125 is the largest length that fits in seven bits, so it is the
  // interesting edge on both sides.
  assert.equal(serverFrame(OP.TEXT, Buffer.alloc(125))[1], 125);
  assert.equal(serverFrame(OP.TEXT, Buffer.alloc(125)).length, 127);
  assert.equal(serverFrame(OP.TEXT, Buffer.alloc(126))[1], 126);
});

test('a 64-bit length is escaped with 127 and eight big-endian bytes', function () {
  const frame = serverFrame(OP.TEXT, Buffer.alloc(70000, 0x62));
  assert.equal(frame[1], 127);
  assert.equal(Number(frame.readBigUInt64BE(2)), 70000);
  assert.equal(frame.readUInt32BE(2), 0, 'the high four bytes are zero for a small payload');
  assert.equal(frame.readUInt32BE(6), 70000);
  assert.equal(frame.length, 10 + 70000);
  // 65535 is the last length the 126 escape can carry, so these two are the
  // boundary the escape exists for.
  assert.equal(serverFrame(OP.TEXT, Buffer.alloc(65535))[1], 126);
  assert.equal(serverFrame(OP.TEXT, Buffer.alloc(65536))[1], 127);
});

test('an empty payload is a header and nothing else', function () {
  const frame = serverFrame(OP.TEXT, '');
  assert.equal(frame.length, 2);
  assert.equal(frame[0], 0x81);
  assert.equal(frame[1], 0);
  // An empty client frame is still masked, or the same silent no-op happens.
  const masked = _encode(OP.TEXT, Buffer.alloc(0));
  assert.equal(masked[0], 0x81);
  assert.equal(masked[1], 0x80);
  assert.equal(masked.length, 6);
});

test('FIN can be cleared so a message can be sent in fragments', function () {
  assert.equal(serverFrame(OP.TEXT, 'one', { fin: false })[0] & 0x80, 0);
  assert.equal(serverFrame(OP.CONT, 'two')[0], 0x80);
});

/* ---- parsing frames ---- */

test('a short text frame arrives as a string', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.TEXT, '{"type":"delta","text":"hi"}'));
  assert.deepEqual(c.seen.text, ['{"type":"delta","text":"hi"}']);
  assert.equal(typeof c.seen.text[0], 'string', 'message data is a string, not a Buffer');
  assert.equal(c.seen.error.length, 0);
});

test('a 16-bit length frame is read whole', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.TEXT, 'x'.repeat(300)));
  assert.deepEqual(c.seen.text, ['x'.repeat(300)]);
});

test('a 64-bit length frame is read whole', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.TEXT, 'y'.repeat(70000)));
  assert.equal(c.seen.text.length, 1);
  assert.equal(c.seen.text[0].length, 70000);
});

test('a message split into three continuation frames is reassembled', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.TEXT, 'the quick ', { fin: false }));
  c.parser.push(serverFrame(OP.CONT, 'brown fox ', { fin: false }));
  c.parser.push(serverFrame(OP.CONT, 'jumps over'));
  assert.deepEqual(c.seen.text, ['the quick brown fox jumps over']);
  assert.equal(c.seen.error.length, 0);
});

test('a control frame may sit between the fragments of a message', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.TEXT, 'a', { fin: false }));
  c.parser.push(serverFrame(OP.PING, 'mid message'));
  c.parser.push(serverFrame(OP.CONT, 'b'));
  assert.deepEqual(c.seen.text, ['ab']);
  assert.equal(c.seen.ping.length, 1, 'the ping was handled, not swallowed by the fragment');
});

test('a ping is handed over with its exact payload', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.PING, 'keepalive'));
  assert.equal(c.seen.ping.length, 1);
  assert.equal(c.seen.ping[0].toString(), 'keepalive');
  assert.equal(c.seen.text.length, 0, 'a ping is not a message');
});

test('a close frame yields its code and its reason', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.CLOSE, closeBody(4404, 'gone')));
  assert.deepEqual(c.seen.close, [[4404, 'gone']]);
});

test('a close frame with no payload reports that there was no status', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.CLOSE, ''));
  // 1005 means "no status was received". It never goes on the wire; it is
  // only ever what the caller is told.
  assert.deepEqual(c.seen.close, [[1005, '']]);
});

test('a close frame with a one byte payload is a protocol error', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.CLOSE, Buffer.from([0x03])));
  assert.equal(c.seen.close.length, 0);
  assert.equal(oneError(c.seen)[1], 1002);
});

test('a close frame with a reserved code is a protocol error', function () {
  // 1004, 1005, 1006 and 1015 are defined by the protocol as codes that must
  // never appear on the wire, and everything outside 1000-1014 and
  // 3000-4999 is unassigned. A caller switching on close codes cannot be
  // right about one it has never heard of, so none of them is passed on.
  for (const code of [0, 999, 1004, 1005, 1006, 1015, 2999, 5000, 65535]) {
    const c = collector();
    c.parser.push(serverFrame(OP.CLOSE, Buffer.from([code >> 8, code & 0xff])));
    assert.equal(c.seen.close.length, 0, 'code ' + code + ' must not be reported as a close');
    assert.equal(oneError(c.seen)[1], 1002, 'code ' + code);
  }
});

test('a close reason that is not valid UTF-8 is a protocol error', function () {
  const c = collector();
  const body = Buffer.concat([Buffer.from([0x03, 0xe8]), Buffer.from([0xff, 0xfe])]);
  c.parser.push(serverFrame(OP.CLOSE, body));
  assert.equal(c.seen.close.length, 0);
  assert.equal(oneError(c.seen)[1], 1007);
});

test('a control frame over 125 bytes is a protocol error', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.PING, Buffer.alloc(126, 0x61)));
  assert.equal(c.seen.ping.length, 0);
  const failed = oneError(c.seen);
  assert.equal(failed[1], 1002);
  assert.match(failed[0].message, /126 byte control frame/);
});

test('a control frame of exactly 125 bytes is allowed', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.PING, Buffer.alloc(125, 0x61)));
  assert.equal(c.seen.error.length, 0);
  assert.equal(c.seen.ping[0].length, 125);
});

test('a fragmented control frame is a protocol error', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.PING, 'half', { fin: false }));
  assert.equal(c.seen.ping.length, 0);
  assert.equal(oneError(c.seen)[1], 1002);
});

test('a reserved bit is a protocol error rather than something to ignore', function () {
  for (const bit of [0x40, 0x20, 0x10]) {
    const c = collector();
    // RSV1 is what a server sets for permessage-deflate, so ignoring it would
    // be the bug that only shows up the day somebody turns compression on.
    const frame = serverFrame(OP.TEXT, 'hello');
    frame[0] |= bit;
    c.parser.push(frame);
    assert.equal(c.seen.text.length, 0, 'rsv bit ' + bit + ' must not be parsed');
    assert.equal(oneError(c.seen)[1], 1002);
  }
});

test('a server that masks is a protocol error', function () {
  const c = collector();
  c.parser.push(_encode(OP.TEXT, Buffer.from('hello')));
  assert.equal(c.seen.text.length, 0);
  assert.equal(oneError(c.seen)[1], 1002);
});

test('a binary frame is refused rather than dropped', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.BINARY, Buffer.from([1, 2, 3])));
  assert.equal(c.seen.text.length, 0);
  assert.equal(oneError(c.seen)[1], 1003);
});

test('an undefined opcode is a protocol error', function () {
  for (const opcode of [0x3, 0x7, 0xb, 0xf]) {
    const c = collector();
    c.parser.push(serverFrame(opcode, 'x'));
    assert.equal(oneError(c.seen)[1], 1002, 'opcode ' + opcode);
  }
});

test('a continuation with nothing to continue is a protocol error', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.CONT, 'orphan'));
  assert.equal(oneError(c.seen)[1], 1002);
});

test('a new data frame inside a fragmented message is a protocol error', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.TEXT, 'half', { fin: false }));
  c.parser.push(serverFrame(OP.TEXT, 'the other half'));
  assert.equal(c.seen.text.length, 0, 'the two frames must not be silently merged');
  assert.equal(oneError(c.seen)[1], 1002);
});

test('a 64-bit length with its high bit set is a protocol error', function () {
  const c = collector();
  c.parser.push(Buffer.concat([
    Buffer.from([0x81, 0x7f]),
    Buffer.from([0x80, 0, 0, 0, 0, 0, 0, 0]),
  ]));
  assert.equal(oneError(c.seen)[1], 1002);
});

test('a declared length over the limit fails before a byte of payload arrives', function () {
  const c = collector({ maxMessageBytes: 1024 });
  const head = Buffer.alloc(10);
  head[0] = 0x81;
  head[1] = 127;
  head.writeBigUInt64BE(1099511627776n, 2);
  c.parser.push(head);
  // Ten bytes on the wire are enough to ask for a terabyte. The parser must
  // refuse on the header rather than wait for a payload that never comes.
  assert.equal(oneError(c.seen)[1], 1009);
});

test('a fragmented message over the limit is a protocol error', function () {
  const c = collector({ maxMessageBytes: 100 });
  c.parser.push(serverFrame(OP.TEXT, 'a'.repeat(60), { fin: false }));
  c.parser.push(serverFrame(OP.CONT, 'b'.repeat(60)));
  assert.equal(c.seen.text.length, 0);
  assert.equal(oneError(c.seen)[1], 1009);
});

test('invalid UTF-8 in a text frame is a protocol error', function () {
  const c = collector();
  // A lone continuation byte. Decoded leniently this is U+FFFD, and a U+FFFD
  // in the middle of a JSON document is worse than a closed socket.
  c.parser.push(serverFrame(OP.TEXT, Buffer.from([0x7b, 0xff, 0x7d])));
  assert.equal(c.seen.text.length, 0);
  assert.equal(oneError(c.seen)[1], 1007);
});

test('nothing after a protocol error is parsed', function () {
  const c = collector();
  c.parser.push(serverFrame(OP.PING, Buffer.alloc(200, 0x61)));
  c.parser.push(serverFrame(OP.TEXT, 'ignored'));
  assert.equal(c.seen.text.length, 0);
  assert.equal(c.seen.error.length, 1, 'the same bad frame is not reported twice');
});

test('two frames in one chunk are both delivered, in order', function () {
  const c = collector();
  c.parser.push(Buffer.concat([
    serverFrame(OP.TEXT, 'first'),
    serverFrame(OP.TEXT, 'second'),
    serverFrame(OP.CLOSE, closeBody(1000)),
  ]));
  assert.deepEqual(c.seen.text, ['first', 'second']);
  assert.deepEqual(c.seen.close, [[1000, '']]);
});

test('a frame arriving one byte at a time is still one frame', function () {
  const c = collector();
  const frame = serverFrame(OP.TEXT, 'drip fed');
  for (const byte of frame) c.parser.push(Buffer.from([byte]));
  assert.deepEqual(c.seen.text, ['drip fed']);
});

/* ---- the character that straddles a read ---- */

test('a multi-byte character split across two chunks reassembles', function () {
  // The bug this file exists to not have. A socket read boundary has nothing
  // to do with a character boundary, and decoding the payload per read turns
  // one character into two replacement characters and the rest of the JSON
  // into rubbish. A strict decoder is not enough by itself: the frame length
  // is what says the payload is complete, and the parser waits for it before
  // decoding anything.
  const text = '{"text":"caf\u00e9 \u{1f600} done"}';
  const frame = serverFrame(OP.TEXT, text);
  const bytes = Buffer.from(text, 'utf8');
  const afterEacute = bytes.indexOf(Buffer.from('\u00e9', 'utf8')) + 1;  const afterEmoji = bytes.indexOf(Buffer.from('\u{1f600}', 'utf8')) + 2;
  assert.ok(afterEacute > 0 && afterEacute < bytes.length, 'the fixture needs a two byte character');
  assert.ok(afterEmoji > 0 && afterEmoji < bytes.length, 'the fixture needs a four byte character');

  // Split in the middle of the two byte character.
  const two = collector();
  two.parser.push(frame.subarray(0, 2 + afterEacute));
  two.parser.push(frame.subarray(2 + afterEacute));
  assert.deepEqual(two.seen.text, [text]);
  assert.equal(two.seen.error.length, 0);

  // And in the middle of the four byte one, which is the case that becomes
  // three replacement characters when it goes wrong.
  const four = collector();
  four.parser.push(frame.subarray(0, 2 + afterEmoji));
  four.parser.push(frame.subarray(2 + afterEmoji));
  assert.deepEqual(four.seen.text, [text]);

  // The nastiest version: split inside the frame header as well.
  const both = collector();
  both.parser.push(frame.subarray(0, 3));
  both.parser.push(frame.subarray(3, 2 + afterEmoji));
  both.parser.push(frame.subarray(2 + afterEmoji));
  assert.deepEqual(both.seen.text, [text]);

  // And the frame as a whole, one byte at a time.
  const drip = collector();
  for (const byte of frame) drip.parser.push(Buffer.from([byte]));
  assert.deepEqual(drip.seen.text, [text]);
});

/* ---- the handshake, against a server driven by hand ---- */

test('a good handshake opens and the request is well formed', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url, {
      headers: { Authorization: 'Bearer secret', 'X-Openmirror-Token': 'secret' },
    });
    const seen = watch(ws);
    t.after(function () { ws.close(); });

    const conn = await ctx.connections(1);
    const request = await conn.request();
    assert.equal(request.split('\r\n')[0], 'GET /ws/agent HTTP/1.1');
    assert.equal(header(request, 'host'), '127.0.0.1:' + ctx.port);
    assert.equal(header(request, 'upgrade'), 'websocket');
    assert.match(header(request, 'connection'), /upgrade/i);
    assert.equal(header(request, 'sec-websocket-version'), '13');
    // 16 random bytes, base64.
    const key = header(request, 'sec-websocket-key');
    assert.equal(Buffer.from(key, 'base64').length, 16);
    assert.equal(header(request, 'authorization'), 'Bearer secret');
    assert.equal(header(request, 'x-openmirror-token'), 'secret');

    conn.socket.write(acceptReply(request));
    await once(ws, 'open');
    assert.equal(seen.open, 1);
    assert.equal(ws.readyState, OPEN);
    assert.equal(ws.readyState, WebSocketClient.OPEN);
    assert.equal(ws.url, ctx.url);
  });
});

test('two handshakes do not share a key', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const first = new WebSocketClient(ctx.url);
    const second = new WebSocketClient(ctx.url);
    t.after(function () { first.close(); second.close(); });
    const one = await (await ctx.connections(1)).request();
    const two = await (await ctx.connections(2)).request();
    // A key that repeated would let a replayed request be accepted by a
    // server that had already seen the first one.
    assert.notEqual(header(one, 'sec-websocket-key'), header(two, 'sec-websocket-key'));
    // Both are left hanging, and both must be closable without an error.
    const errors = [];
    first.on('error', function (e) { errors.push(e); });
    second.on('error', function (e) { errors.push(e); });
    first.close();
    second.close();
    await tick();
    assert.equal(errors.length, 0);
  });
});

test('a masked frame arrives at the server', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    ws.send(JSON.stringify({ type: 'turn.submit', text: 'hi' }));
    const frame = await conn.nextFrame();
    assert.equal(frame.masked, true, 'a client frame must be masked');
    assert.equal(frame.opcode, OP.TEXT);
    assert.equal(frame.fin, true);
    assert.equal(frame.payload.toString(), '{"type":"turn.submit","text":"hi"}');

    conn.socket.write(serverFrame(OP.TEXT, '{"type":"delta"}'));
    await once(ws, 'message');
    assert.deepEqual(seen.message, ['{"type":"delta"}']);
  });
});

test('a wrong Sec-WebSocket-Accept is refused and says why', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    // Deliberately not the accept for the key that was sent. This is what a
    // captive portal, a proxy, or a plain HTTP server on the wrong port looks
    // like, and it is why the accept is verified rather than assumed.
    conn.socket.write([
      'HTTP/1.1 101 Switching Protocols',
      'upgrade: websocket',
      'connection: Upgrade',
      'sec-websocket-accept: AAAAAAAAAAAAAAAAAAAAAAAAAAA=',
      '', '',
    ].join('\r\n'));

    const [err] = await once(ws, 'error');
    assert.equal(err.code, 'EPROTOCOL');
    assert.match(err.message, /Sec-WebSocket-Accept is "A{27}="/);
    assert.match(err.message, /not a websocket server/);
    // error and close land in the same tick, so both are waited for together.
    await until(ws, seen, function (s) { return s.close.length === 1; }, 'the close');
    // A handshake that never completed has no clean close to report.
    assert.equal(seen.close[0][0], 1006);
    assert.equal(seen.open, 0, 'a failed handshake must not report open');
    assert.equal(seen.error.length, 1);
    assert.equal(ws.readyState, CLOSED);
  });
});

test('a missing Sec-WebSocket-Accept is refused', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    await conn.request();
    conn.socket.write([
      'HTTP/1.1 101 Switching Protocols',
      'upgrade: websocket',
      'connection: Upgrade',
      '', '',
    ].join('\r\n'));
    await once(ws, 'error');
    assert.match(seen.error[0].message, /no Sec-WebSocket-Accept/);
  });
});

test('a 101 that does not say Upgrade is refused', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write([
      'HTTP/1.1 101 Switching Protocols',
      'connection: Upgrade',
      'sec-websocket-accept: ' + _accept(header(request, 'sec-websocket-key')),
      '', '',
    ].join('\r\n'));
    await once(ws, 'error');
    assert.match(seen.error[0].message, /does not say Upgrade: websocket/);
  });
});

test('HTTP 200 instead of 101 is refused and the message says 200', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    await conn.request();
    conn.socket.write([
      'HTTP/1.1 200 OK',
      'content-type: text/html; charset=utf-8',
      'content-length: 5',
      '', '',
      'hello',
    ].join('\r\n'));
    const [err] = await once(ws, 'error');
    assert.equal(err.code, 'EHTTP');
    assert.match(err.message, /HTTP 200 OK/);
    assert.match(err.message, /rather than 101/);
    await until(ws, seen, function (s) { return s.close.length === 1; }, 'the close');
    assert.equal(seen.close[0][0], 1006);
    assert.equal(seen.open, 0);
  });
});

test('a 403 is reported as the token problem it is', async function (t) {
  await withServer(function () {}, async function (ctx) {
    // What the daemon actually does. A websocket cannot be answered with a
    // 401, so the auth middleware refuses an unauthenticated socket with a
    // 403 before the upgrade (uvicorn's websockets_sansio_impl, on
    // "websocket.close" before "websocket.accept"). This is the handshake
    // failure a person in an editor is most likely to meet, so it gets a fix
    // in the message rather than a status code to look up.
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    await (await ctx.connections(1)).request();
    (await ctx.connections(1)).socket.write('HTTP/1.1 403 Forbidden\r\ncontent-length: 0\r\n\r\n');
    const [err] = await once(ws, 'error');
    assert.equal(err.code, 'EAUTH');
    assert.match(err.message, /HTTP 403 Forbidden/);
    assert.match(err.message, /openmirror\.token or OPENMIRROR_TOKEN/);
  });
});

test('a handshake that never finishes times out with a clear error', async function (t) {
  // A server that accepts the TCP connection and then says nothing. Without
  // a timeout this is a panel that spins forever, which nobody can tell from
  // the extension being broken.
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url, { handshakeTimeout: 80 });
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    await until(ws, seen, function (s) { return s.error.length === 1 && s.close.length === 1; }, 'the timeout');
    assert.equal(seen.error[0].code, 'ETIMEDOUT');
    assert.match(seen.error[0].message, /did not finish in 80ms/);
    assert.equal(seen.close[0][0], 1006);
    assert.equal(ws.readyState, CLOSED);
  });
});

test('a frame pipelined behind the 101 is not dropped', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    // The response head and the first frame in one write, so one TCP read
    // holds both. Everything after the blank line has to go to the parser
    // rather than being thrown away with the head.
    conn.socket.write(Buffer.concat([
      Buffer.from(acceptReply(request), 'latin1'),
      serverFrame(OP.TEXT, 'pipelined'),
    ]));
    await once(ws, 'message');
    assert.deepEqual(seen.message, ['pipelined']);
  });
});

test('a reason phrase with a character in it is read, and the leftover still is', async function (t) {
  // The handshake head is the one place in the client with no length prefix,
  // so a character in it really can be split across two TCP reads. It also
  // means the byte length of the head and the length of the decoded string
  // are different numbers, which is why both are tracked: the leftover after
  // the blank line is found by bytes, and anything after a multi-byte reason
  // phrase would be cut in the wrong place if it were found by characters.
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    const key = header(request, 'sec-websocket-key');
    // An em dash: three bytes, one character.
    const reply = [
      'HTTP/1.1 400 Bad Request \u2014 the session is gone',
      'content-length: 0',
      'sec-websocket-accept: ' + _accept(key),
      '', '',
    ].join('\r\n');
    const bytes = Buffer.from(reply, 'utf8');
    // Split in the middle of the em dash, which is at byte 27 of the head.
    conn.socket.write(bytes.subarray(0, 28));
    await tick();
    conn.socket.write(bytes.subarray(28));

    const [err] = await once(ws, 'error');
    assert.equal(err.code, 'EHTTP');
    assert.match(err.message, /HTTP 400 Bad Request \u2014 the session is gone/,
      'the multi-byte character survived the split');
    await until(ws, seen, function (s) { return s.close.length === 1; }, 'the close');
    assert.equal(seen.close[0][0], 1006);
  });
});

test('a frame after a handshake head containing a multi-byte character is not lost', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    // A 101 whose extension header carries a multi-byte value, with a frame
    // in the same write behind it. If the leftover were cut by character
    // count instead of byte count, the frame would start one or two bytes in
    // the wrong place and the message would be rubbish.
    const reply = [
      'HTTP/1.1 101 Switching Protocols',
      'upgrade: websocket',
      'connection: Upgrade',
      'x-note: caf\u00e9 \u2014 the daemon',
      'sec-websocket-accept: ' + _accept(header(request, 'sec-websocket-key')),
      '', '',
    ].join('\r\n');
    conn.socket.write(Buffer.concat([
      Buffer.from(reply, 'utf8'),
      serverFrame(OP.TEXT, 'behind a wide head'),
    ]));
    await once(ws, 'message');
    assert.deepEqual(seen.message, ['behind a wide head']);
  });
});

/* ---- the conversation ---- */

test('a ping from the server is answered with a pong carrying the same payload', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    conn.socket.write(serverFrame(OP.PING, 'ping payload'));
    const pong = await conn.nextFrame();
    assert.equal(pong.opcode, OP.PONG);
    assert.equal(pong.masked, true, 'a pong from a client is a client frame, so it is masked');
    assert.equal(pong.payload.toString(), 'ping payload');
  });
});

test('a message the server fragmented is delivered as one string', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    // The daemon streams long text deltas and the library on the other end is
    // free to split them across frames at whatever boundary it likes.
    const body = 'a'.repeat(40000) + 'caf\u00e9' + 'b'.repeat(40000);
    conn.socket.write(serverFrame(OP.TEXT, body.slice(0, 10), { fin: false }));
    conn.socket.write(serverFrame(OP.CONT, body.slice(10, 60000), { fin: false }));
    conn.socket.write(serverFrame(OP.CONT, body.slice(60000)));
    await once(ws, 'message');
    assert.deepEqual(seen.message, [body]);
  });
});

test('a multi-byte character split across two TCP writes arrives whole', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    // The same character straddling a real socket read rather than a
    // synthetic one: two socket writes, split in the middle of a four byte
    // emoji, which is the case that turns into replacement characters if the
    // payload is decoded per read.
    const text = 'before \u{1f600} after';
    const frame = serverFrame(OP.TEXT, text);
    const at = Buffer.from(text, 'utf8').indexOf(Buffer.from('\u{1f600}', 'utf8')) + 2;
    conn.socket.write(frame.subarray(0, 2 + at));
    await tick();
    conn.socket.write(frame.subarray(2 + at));

    await once(ws, 'message');
    assert.deepEqual(seen.message, [text]);
  });
});

test('a close from the server is reported with its code and reason', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    conn.socket.write(serverFrame(OP.CLOSE, closeBody(4404, 'gone')));
    await once(ws, 'close');
    assert.deepEqual(seen.close, [[4404, 'gone']]);
    assert.equal(seen.error.length, 0, 'a close with a code is not an error');
    assert.equal(ws.readyState, CLOSED);
  });
});

test('the client answers a close with one of its own', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    conn.socket.write(serverFrame(OP.CLOSE, closeBody(4404, 'gone')));
    const echo = await conn.nextFrame();
    assert.equal(echo.opcode, OP.CLOSE);
    assert.equal(echo.masked, true);
    // RFC 6455 section 5.5.1: the answer to a Close is a Close, and it
    // typically carries the same status code back, so the peer can see the
    // reason it is being disconnected was understood.
    assert.equal(echo.payload.readUInt16BE(0), 4404);
  });
});

test('close() sends a close frame with the code, and closing twice is once', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    ws.close(1000, 'panel closed');
    assert.equal(ws.readyState, CLOSING);
    const frame = await conn.nextFrame();
    assert.equal(frame.opcode, OP.CLOSE);
    assert.equal(frame.masked, true);
    assert.equal(frame.payload.readUInt16BE(0), 1000);
    assert.equal(frame.payload.subarray(2).toString(), 'panel closed');

    // Twice more, and once more after it has finished. None of them is an
    // error and none of them sends a second frame.
    ws.close();
    ws.close(1000, 'again');
    await once(ws, 'close');
    ws.close();
    await tick();
    assert.deepEqual(seen.close, [[1000, 'panel closed']]);
    assert.equal(seen.error.length, 0);
    assert.equal(conn.frames.length, 0, 'only one close frame was ever sent');
    assert.equal(ws.readyState, CLOSED);
  });
});

test('a close code that must not be sent is refused at the call site', function (t) {
  const ws = new WebSocketClient('ws://127.0.0.1:1/ws');
  ws.on('error', function () {});
  t.after(function () { ws.close(); });
  // 1005 and 1006 are local-only codes and 1004 and 1015 are reserved.
  // Putting any of them on the wire is a protocol error, so it is caught
  // where the mistake is rather than by the server.
  for (const code of [1004, 1005, 1006, 1015, 999, 5000]) {
    assert.throws(function () { ws.close(code); }, /reserved or out of range/);
  }
  // A close frame is 125 bytes and two of them are the code.
  assert.throws(function () { ws.close(1000, 'x'.repeat(124)); }, RangeError);
});

test('closing while still connecting aborts without an error', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url, { handshakeTimeout: 5000 });
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    assert.equal(ws.readyState, CONNECTING);
    ws.close();
    // There is no socket to wait on, so the close lands in the same tick.
    await until(ws, seen, function (s) { return s.close.length === 1; }, 'the abort');
    // There is no connection to close, so there is no clean close to claim.
    assert.equal(seen.close[0][0], 1006);
    assert.equal(seen.error.length, 0);
    assert.equal(ws.readyState, CLOSED);
  });
});

test('a peer that vanishes is one close with 1006 and no error', async function (t) {
  const raw = [];
  await withServer(function (conn, socket) { raw.push(socket); }, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    raw[0].destroy();
    await once(ws, 'close');
    await tick();
    assert.deepEqual(seen.close, [[1006, '']]);
    assert.equal(seen.error.length, 0, 'a peer hanging up is a close, not an error');
    assert.equal(ws.readyState, CLOSED);
  });
});

test('a protocol error is an error and then a close, in that order', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const order = [];
    const ws = new WebSocketClient(ctx.url);
    ws.on('open', function () { order.push('open'); });
    ws.on('error', function (err) { order.push('error:' + err.code); });
    ws.on('close', function (code) { order.push('close:' + code); });
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    conn.socket.write(serverFrame(OP.PING, Buffer.alloc(200, 0x61)));
    await once(ws, 'close');
    // The order is the contract: a caller that has already torn its panel
    // down has still been told why, and the close says which rule was broken.
    assert.deepEqual(order, ['open', 'error:EPROTO', 'close:1002']);
    assert.equal(ws.readyState, CLOSED);
  });
});

/* ---- backpressure ---- */

test('a large send is queued and delivered in order, not dropped', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    // 8MB in 1MB messages, far past the 16KB high water mark, so write()
    // returns false and everything after the first frame has to queue and
    // come out again on drain. The bound is 64MB, so this must arrive whole
    // rather than be refused.
    const total = 8;
    const chunk = 'z'.repeat(1024 * 1024);
    for (let i = 0; i < total; i++) ws.send(chunk + ':' + i);

    let bytes = 0;
    for (let i = 0; i < total; i++) {
      const frame = await conn.nextFrame();
      assert.equal(frame.opcode, OP.TEXT);
      const text = frame.payload.toString();
      // The order the daemon sees is the order the caller sent in, or the
      // transcript of a turn is a lie. A queue that re-sent a frame it had
      // already handed to the socket would pass every framing test and fail
      // this one.
      assert.equal(text.slice(-2), ':' + i);
      bytes += frame.payload.length;
    }
    assert.equal(bytes, total * (chunk.length + 2));
    assert.equal(seen.error.length, 0);
  });
});

test('the send queue has a bound and exceeding it ends the connection', async function (t) {
  // A server that accepts the handshake and then stops reading, so the socket
  // fills and the queue has to grow. The bound is what stops a caller in a
  // loop from taking the extension host down with it.
  const raw = [];
  await withServer(function (conn, socket) { raw.push(socket); }, async function (ctx) {
    const ws = new WebSocketClient(ctx.url, { maxSendQueueBytes: 1024 * 1024 });
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');
    raw[0].pause();

    const chunk = 'y'.repeat(1024 * 1024);
    // The send that trips the bound ends the connection, so the send after it
    // throws. A caller looping over sends has to expect that, which is why it
    // is written out here rather than left implied.
    let thrown = null;
    for (let i = 0; i < 16; i++) {
      try {
        ws.send(chunk);
      } catch (err) {
        thrown = err;
        break;
      }
    }

    // The send that trips the bound fails synchronously, so the error and the
    // close are both already gone by the time this line returns.
    await until(ws, seen, function (s) { return s.error.length === 1 && s.close.length === 1; }, 'the overflow');
    assert.match(seen.error[0].message, /unsent frames passed the 1048576 byte limit/);
    assert.equal(seen.error[0].code, 'EOVERFLOW');
    assert.equal(seen.close[0][0], 1009, 'the close says the message was too big');
    assert.equal(ws.readyState, CLOSED);
    assert.ok(thrown, 'the send after the one that failed throws rather than queueing forever');
    assert.equal(thrown.name, 'InvalidStateError');
    raw[0].resume();
  });
});

/* ---- the edges of the API ---- */

test('a connection nobody is listening on is an error, not a throw', async function (t) {
  const port = await deadPort();
  const ws = new WebSocketClient('ws://127.0.0.1:' + port + '/ws/agent');
  const seen = watch(ws);
  t.after(function () { ws.close(); });
  await until(ws, seen, function (s) { return s.error.length === 1 && s.close.length === 1; }, 'the refusal');
  assert.equal(seen.error[0].code, 'ECONNREFUSED');
  assert.match(seen.error[0].message, /is not answering/);
  assert.equal(seen.close[0][0], 1006);
  assert.equal(seen.open, 0);
});

test('an error with nobody listening is logged rather than thrown', async function () {
  // A net.Socket with no error listener takes the process down, and so does
  // an EventEmitter with no 'error' listener, which this one is. A caller who
  // forgets the listener gets a line on stderr, not a dead extension host.
  const port = await deadPort();
  const written = [];
  const real = console.error;
  console.error = function () { written.push(Array.prototype.join.call(arguments, ' ')); };
  let closed = null;
  try {
    const ws = new WebSocketClient('ws://127.0.0.1:' + port + '/ws/agent');
    ws.on('close', function (code) { closed = code; });
    closed = await new Promise(function (resolve) { ws.on('close', resolve); });
  } finally {
    console.error = real;
  }
  assert.equal(closed, 1006);
  assert.equal(written.length, 1);
  assert.match(written[0], /openmirror: websocket: .*is not answering/);
});

test('a bad URL is refused at the constructor', function () {
  assert.throws(function () { new WebSocketClient('not a url'); }, /not a URL/);
  // The browser refuses http:// here too, and a client that quietly upgraded
  // one would be a client that could be pointed somewhere it did not mean.
  assert.throws(function () { new WebSocketClient('http://127.0.0.1:8477/ws'); }, /must be ws: or wss:/);
  assert.throws(function () { new WebSocketClient('https://127.0.0.1:8477/ws'); }, /must be ws: or wss:/);
});

test('a header value with a newline is refused', function () {
  // The token comes from a setting rather than from the source, and a
  // newline in it is request splitting.
  assert.throws(function () {
    new WebSocketClient('ws://127.0.0.1:1/ws', { headers: { Authorization: 'Bearer a\r\nX-Evil: 1' } });
  }, /control characters/);
});

test('a caller cannot override the headers the handshake depends on', function () {
  const ws = new WebSocketClient('ws://127.0.0.1:1/ws', {
    headers: { Host: 'elsewhere', Upgrade: 'h2c', 'Sec-WebSocket-Key': 'nope' },
  });
  ws.on('error', function () {});
  // The values are dropped rather than appended, because two of them make the
  // request ambiguous. The socket is never going to answer, so all this can
  // check is that constructing it did not throw.
  assert.equal(ws.readyState, CONNECTING);
  ws.close();
});

test('send() refuses anything but a string, and refuses a socket that is not open', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    t.after(function () { ws.close(); });
    assert.throws(function () { ws.send(Buffer.from('nope')); }, /takes a string/);
    assert.throws(function () { ws.send({}); }, /takes a string/);
    // The browser throws here too, and throwing beats queueing: a message
    // handed to a socket that is going away is a message somebody believes
    // was delivered.
    assert.throws(function () { ws.send('early'); }, /not open/);

    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');
    ws.send('now fine');
    assert.equal((await conn.nextFrame()).payload.toString(), 'now fine');

    ws.close();
    await once(ws, 'close');
    assert.throws(function () { ws.send('too late'); }, /not open/);
  });
});

test('WebSocketError carries a code and a close code', function () {
  const err = new WebSocketError('boom', 'EPROTO', 1002);
  assert.ok(err instanceof Error);
  assert.ok(err instanceof WebSocketError);
  assert.equal(err.name, 'WebSocketError');
  assert.equal(err.message, 'boom');
  assert.equal(err.code, 'EPROTO');
  assert.equal(err.closeCode, 1002);
});

test('a handler that throws is reported and the connection survives', async function (t) {
  // A caller's bug in a message handler is the caller's bug, and the browser
  // does not close a socket over one. What it must not become is an uncaught
  // exception in the extension host, so it is caught, logged, and the
  // conversation carries on to the next message.
  const raw = [];
  await withServer(function (conn, socket) { raw.push(socket); }, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    const written = [];
    const real = console.error;
    console.error = function () { written.push(Array.prototype.join.call(arguments, ' ')); };
    let resolveThrown;
    const threw = new Promise(function (resolve) { resolveThrown = resolve; });
    let first = true;
    try {
      ws.on('message', function (data) {
        if (!first) return;
        first = false;
        resolveThrown(data);
        throw new Error('handler is broken');
      });
      conn.socket.write(serverFrame(OP.TEXT, 'one'));
      await threw;
      // The socket is still good: the next message is delivered normally and
      // the next send goes out. A throw that ended the connection would
      // instead be an `error` event, and there is not one.
      conn.socket.write(serverFrame(OP.TEXT, 'two'));
      await until(ws, seen, function (s) { return s.message.length === 2; }, 'the second message');
      ws.send('still working');
    } finally {
      console.error = real;
    }
    // The recorder is registered before the handler that throws, so it saw
    // both. What a throw stopped was the listeners after it in that one
    // emit, and the next message was an ordinary one.
    assert.deepEqual(seen.message, ['one', 'two']);
    assert.equal(seen.close.length, 0, 'a throwing handler does not close the socket');
    assert.equal(seen.error.length, 0, "and it is not this client's error to report");
    assert.equal(written.length, 1);
    assert.match(written[0], /a message handler threw: handler is broken/);
    assert.equal((await conn.nextFrame()).payload.toString(), 'still working');
    raw[0].destroy();
  });
});

test('a close handler that throws still leaves the socket closed', async function (t) {
  await withServer(function () {}, async function (ctx) {
    const ws = new WebSocketClient(ctx.url);
    const seen = watch(ws);
    t.after(function () { ws.close(); });
    const conn = await ctx.connections(1);
    const request = await conn.request();
    conn.socket.write(acceptReply(request));
    await once(ws, 'open');

    const written = [];
    const real = console.error;
    console.error = function () { written.push(Array.prototype.join.call(arguments, ' ')); };
    let resolveThrown;
    const threw = new Promise(function (resolve) { resolveThrown = resolve; });
    try {
      // Registered second, so the recorder has already run by the time this
      // one throws, on the stack of the _finish that is still unwinding.
      ws.on('close', function () {
        resolveThrown();
        throw new Error('teardown is broken');
      });
      ws.close(1000, 'done');
      await threw;
    } finally {
      console.error = real;
    }
    assert.deepEqual(seen.close, [[1000, 'done']]);
    assert.equal(ws.readyState, CLOSED, 'the socket is closed whatever the handler did');
    assert.equal(written.length, 1);
    assert.match(written[0], /a close handler threw: teardown is broken/);
  });
});

test('the ready state constants are the browser numbers', function () {
  assert.equal(CONNECTING, 0);
  assert.equal(OPEN, 1);
  assert.equal(CLOSING, 2);
  assert.equal(CLOSED, 3);
  assert.equal(WebSocketClient.CONNECTING, 0);
  assert.equal(WebSocketClient.OPEN, 1);
  assert.equal(WebSocketClient.CLOSING, 2);
  assert.equal(WebSocketClient.CLOSED, 3);
});

/* ---- wss ---- */

test('a self signed certificate is refused, because validation stays on', async function (t) {
  // There is no option to turn certificate checking off and no way for a
  // caller to add a CA, which is the point: an intercepting proxy must not be
  // able to sit in the middle of the panel. Asserting it needs a real
  // certificate, so one is made here rather than committed: a throwaway
  // keypair for 127.0.0.1, generated by openssl into the OS temp directory
  // and deleted afterwards. The test skips itself where there is no openssl.
  let tls;
  let key;
  let cert;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'openmirror-ws-tls-'));
  try {
    child.execFileSync('openssl', [
      'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
      '-keyout', path.join(dir, 'key.pem'), '-out', path.join(dir, 'cert.pem'),
      '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
    ], { stdio: 'ignore' });
  } catch (err) {
    fs.rmSync(dir, { recursive: true, force: true });
    t.skip('no openssl to make a certificate with: ' + err.message);
    return;
  }
  key = fs.readFileSync(path.join(dir, 'key.pem'));
  cert = fs.readFileSync(path.join(dir, 'cert.pem'));
  t.after(function () { fs.rmSync(dir, { recursive: true, force: true }); });

  tls = require('node:tls');
  const sockets = new Set();
  const server = tls.createServer({ key: key, cert: cert }, function (socket) {
    sockets.add(socket);
    socket.on('error', function () {});
    socket.on('close', function () { sockets.delete(socket); });
  });
  await new Promise(function (resolve) { server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  t.after(function () {
    for (const socket of sockets) socket.destroy();
    return new Promise(function (resolve) { server.close(resolve); });
  });

  const ws = new WebSocketClient('wss://127.0.0.1:' + port + '/ws/agent');
  const seen = watch(ws);
  t.after(function () { ws.close(); });

  await until(ws, seen, function (s) { return s.error.length === 1 && s.close.length === 1; }, 'the refusal');
  // Self signed, so untrusted. This is the failure, and it is the correct one.
  assert.match(seen.error[0].message, /self.signed|self signed|unable to verify|DEPTH_ZERO/i);
  assert.equal(seen.open, 0, 'an untrusted certificate must not produce an open socket');
  assert.equal(seen.close[0][0], 1006);
});

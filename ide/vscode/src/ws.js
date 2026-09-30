/* A WebSocket client, in one file, with nothing to install.
 *
 * The extension host is a Node process that VS Code owns. It runs whatever
 * Node the editor shipped, it has no install step of its own, and pulling in
 * `ws` would mean either a bundler or a vendored copy of somebody else's
 * protocol code inside a signed extension. Node only grew a global
 * `WebSocket` in v21 and the host's Node version is not ours to choose, so
 * this is built on `net` and `tls` and RFC 6455 directly.
 *
 * The shape of the API is the browser's on purpose. `open`, `message`,
 * `close`, `error`, `readyState`, `send()`, `close()` are the names the code
 * calling this already knows, which is the whole reason the host side of the
 * panel reads like the web side of it. It is an EventEmitter rather than
 * addEventListener because the caller is Node code, and the browser's
 * `onmessage =` style is not a thing anybody writes in a Node process.
 *
 * The only things that throw are the ones the browser throws for too, and all
 * three are mistakes in the calling code rather than anything about the
 * connection: a URL that is not `ws:` or `wss:`, a header value with a control
 * character in it, and a `send()` of something that is not a string or a
 * socket that is not open. Everything else -- including a bad handshake, a
 * dead peer, a protocol error and a refused connection -- arrives as an
 * `error` event followed by exactly one `close`.
 *
 * What it deliberately does not do:
 *
 *   - Reconnect. `openmirror/tui.py` owns that loop and it has to: `since` is
 *     the last event sequence number seen and the daemon replays the gap from
 *     it, so exactly one object has to own that number or a dropped socket
 *     costs events. A reconnect here would be a second owner of it.
 *   - Negotiate subprotocols or extensions. The daemon offers none.
 *     permessage-deflate in particular would be a second compressor to get
 *     right for JSON that gzip already handles.
 *   - Accept binary. The agent socket is JSON text in both directions, so a
 *     binary frame fails the connection with 1003 rather than being dropped.
 *     A dropped frame is a message that vanishes, and this project would
 *     rather say so out loud.
 *   - Send pings. The daemon is on loopback, where a dead peer arrives as a
 *     TCP reset rather than a black hole, and a client that pings is a client
 *     that can be wrong about liveness. Pings from the server are answered.
 */

'use strict';

const crypto = require('crypto');
const { EventEmitter } = require('events');
const net = require('net');
const { StringDecoder } = require('string_decoder');
const tls = require('tls');
const { URL } = require('url');
const { TextDecoder } = require('util');

// RFC 6455 section 1.3. This constant is the entire reason a handshake cannot
// be faked by a server that merely wants to talk HTTP to us.
const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

const CONNECTING = 0;
const OPEN = 1;
const CLOSING = 2;
const CLOSED = 3;

const OP_CONTINUATION = 0x0;
const OP_TEXT = 0x1;
const OP_BINARY = 0x2;
const OP_CLOSE = 0x8;
const OP_PING = 0x9;
const OP_PONG = 0xa;

// A Close frame carries at most 125 bytes, two of which are the status code,
// which is where the 123 comes from (RFC 6455 section 5.5.1).
const MAX_CONTROL_PAYLOAD = 125;
const MAX_CLOSE_REASON = 123;

// 1005 and 1006 are how the *report* distinguishes "the peer sent a Close with
// no payload" and "the socket died with no Close at all". They are local
// numbers and must never reach the wire, which is why they are also in the
// list of codes no one is allowed to send.
const NO_STATUS = 1005;
const ABNORMAL = 1006;
const UNSENT_CODES = [1004, 1005, 1006, 1015];

const PROTOCOL_ERROR = 1002;
const UNSUPPORTED_DATA = 1003;
const INVALID_PAYLOAD = 1007;
const TOO_BIG = 1009;

// The default bound on one message in either direction.
//
// Inbound it is a bound rather than a hope: a server can declare a 2^63 byte
// length in four bytes, and believing it would be an allocation, not a read.
// Outbound, the same number has to clear the worst legitimate frame the panel
// can produce. The daemon caps an upload at 32MB (openmirror/routers/media.py
// MAX_UPLOAD) and the agent socket carries it base64 inside a `turn.submit`,
// which is 4/3 of that, so anything under about 44MB would kill a real paste.
const DEFAULT_MAX_BYTES = 64 * 1024 * 1024;

const DEFAULT_HANDSHAKE_TIMEOUT = 10000;

// How long to wait for the peer to answer our Close before taking the socket
// down ourselves. A conforming server answers immediately; this is here so a
// server that does not cannot hold the socket open forever.
const CLOSE_TIMEOUT = 5000;

// A 101 response is a handful of headers. 64KB is generous to the point of
// absurdity, and past it the thing answering is not a websocket server.
const MAX_HANDSHAKE_HEAD = 64 * 1024;

const TERMINATOR = Buffer.from('\r\n\r\n');

/* Every failure in this file is one of these, so a caller can branch on
 * `err.code` without matching on English. */
class WebSocketError extends Error {
  constructor(message, code, closeCode) {
    super(message);
    this.name = 'WebSocketError';
    this.code = code;
    // The status code we would put on the wire to explain this, when the
    // failure is a protocol one and the socket is still worth talking on.
    this.closeCode = closeCode === undefined ? null : closeCode;
  }
}

/* The status code the peer sent, checked against the ranges RFC 6455
 * section 7.4.2 says exist. An unassigned or reserved code is a protocol
 * error rather than something to pass on to the caller, because a caller
 * that switches on close codes cannot be right about one it has never heard
 * of. */
function validCloseCode(code) {
  if (UNSENT_CODES.indexOf(code) !== -1) return false;
  if (code >= 1000 && code <= 1014) return true;
  return code >= 3000 && code <= 4999;
}

/* The value the server must echo back: base64(sha1(key + GUID)). */
function acceptFor(key) {
  return crypto.createHash('sha1').update(key + GUID, 'ascii').digest('base64');
}

/* One frame, as bytes.
 *
 * `mask` defaults to true because a client frame MUST be masked (section
 * 5.3) and an unmasked send against a conforming server is a silent no-op --
 * the server is required to drop it and is not required to say so. The
 * default is therefore the one that cannot fail quietly; tests build
 * server-to-client frames, which MUST NOT be masked, by passing false.
 */
function encodeFrame(opcode, payload, options) {
  const opts = options || {};
  const mask = opts.mask !== false;
  const fin = opts.fin === false ? 0 : 0x80;
  const body = payload || Buffer.alloc(0);
  const length = body.length;
  const isControl = (opcode & 0x8) !== 0;

  // The 126 escape means "the next two bytes are the length" and only covers
  // up to 65535, so a 16-bit length is written in the *minimum* width even
  // when it would have fitted in 7 bits. Section 5.2: the minimal number of
  // bytes must be used.
  let header;
  if (length < 126) {
    header = Buffer.allocUnsafe(2);
    header[1] = length;
  } else if (length < 65536) {
    header = Buffer.allocUnsafe(4);
    header[1] = 126;
    header.writeUInt16BE(length, 2);
  } else {
    header = Buffer.allocUnsafe(10);
    header[1] = 127;
    // 32 bits at a time rather than a BigInt, so this runs on a host old
    // enough not to have BigInt and the arithmetic is exact to 2^53 anyway.
    const high = Math.floor(length / 4294967296);
    header.writeUInt32BE(high, 2);
    header.writeUInt32BE(length - high * 4294967296, 6);
  }
  header[0] = fin | (opcode & 0x0f);
  if (mask) header[1] |= 0x80;

  if (!mask) return Buffer.concat([header, body]);

  // RFC 6455 section 5.3: the masking key is 32 bits of unpredictable value
  // and the payload is XORed with it, repeated. The key is sent in the clear
  // -- the point is not secrecy, it is that a corrupted proxy on a TCP stream
  // cannot pass a well-formed frame through without being noticed.
  const key = opts.maskKey || crypto.randomBytes(4);
  const masked = Buffer.allocUnsafe(length);
  for (let i = 0; i < length; i++) masked[i] = body[i] ^ key[i & 3];
  return Buffer.concat([header, key, masked]);
}

/* The A and B of the handshake, as a Close frame body. */
function closePayload(code, reason) {
  if (code === null || code === undefined) return Buffer.alloc(0);
  const text = Buffer.from(String(reason === undefined ? '' : reason), 'utf8');
  const body = Buffer.allocUnsafe(2 + text.length);
  body.writeUInt16BE(code, 0);
  text.copy(body, 2);
  return body;
}

/* Frames in, messages and control events out.
 *
 * Split out from the socket on purpose. Framing is where a websocket client
 * goes wrong and where it is hardest to see it go wrong, because the bug is
 * a byte in the wrong place in a stream that still mostly works. So the
 * parser is a pure function of the bytes it is fed, it never touches a
 * socket, and the tests drive it with hand-built frames.
 *
 * The underscore on the export says internal-but-testable, which is the only
 * licence anyone needs to reach past it.
 *
 * It refuses a masked frame instead of unmasking one. A server must never
 * mask (section 5.1), so the only correct response to a mask bit here is to
 * fail, and failing means there is no unmask loop in this file to get wrong.
 */
class Parser {
  constructor(options) {
    const opts = options || {};
    this.onText = opts.onText || noop;
    this.onPing = opts.onPing || noop;
    this.onPong = opts.onPong || noop;
    this.onClose = opts.onClose || noop;
    this.onError = opts.onError || noop;
    this.maxMessageBytes = opts.maxMessageBytes || DEFAULT_MAX_BYTES;

    // Incoming bytes as a list rather than one growing Buffer. A frame header
    // is 14 bytes and a daemon event is a few hundred, so concatenating on
    // every read is fine until a large message arrives in small TCP reads,
    // and then it is quadratic in the size of the message.
    this._chunks = [];
    this._size = 0;
    this._fragments = null;
    this._done = false;
  }

  /* Feed bytes. Anything that arrives after a protocol error is dropped: the
   * stream is no longer trustworthy and there is nothing sensible to resync
   * to, because a resync needs a frame boundary nobody can point at. */
  push(chunk) {
    if (this._done) return;
    this._chunks.push(chunk);
    this._size += chunk.length;
    this._parse();
  }

  _peek(n, offset) {
    const start = offset || 0;
    const want = start + n;
    if (want > this._size) return null;
    const parts = [];
    let seen = 0;
    for (let i = 0; i < this._chunks.length; i++) {
      const chunk = this._chunks[i];
      const end = seen + chunk.length;
      if (end > start && seen < want) {
        parts.push(chunk.subarray(Math.max(0, start - seen), Math.min(chunk.length, want - seen)));
      }
      seen = end;
      if (seen >= want) break;
    }
    return parts.length === 1 ? parts[0] : Buffer.concat(parts, n);
  }

  /* Remove and return the first n bytes. Always a fresh Buffer, so a caller
   * can XOR or otherwise write into it without touching a socket's read
   * buffer underneath us. */
  _take(n) {
    const parts = [];
    let left = n;
    while (left > 0) {
      const chunk = this._chunks[0];
      const take = Math.min(left, chunk.length);
      parts.push(chunk.subarray(0, take));
      this._size -= take;
      left -= take;
      // A chunk that is only partly consumed stays, shortened. Dropping it
      // instead would throw away the frames behind the one being parsed, and
      // a read that happens to hold two frames is not a rare read.
      if (take === chunk.length) this._chunks.shift();
      else this._chunks[0] = chunk.subarray(take);
    }
    return Buffer.concat(parts, n);
  }

  _parse() {
    for (;;) {
      if (this._size < 2) return;
      const head = this._peek(2);
      const first = head[0];
      const second = head[1];
      const fin = (first & 0x80) !== 0;
      const reserved = first & 0x70;
      const opcode = first & 0x0f;
      const masked = (second & 0x80) !== 0;
      const short = second & 0x7f;

      let length = short;
      let headerLength = 2;
      if (short === 126) {
        if (this._size < 4) return;
        length = this._peek(2, 2).readUInt16BE(0);
        headerLength = 4;
      } else if (short === 127) {
        if (this._size < 10) return;
        const wide = this._peek(8, 2);
        // Bit 63 of a 64-bit length must be 0 (section 5.2). A set bit means
        // the length is negative, which is a framing error and not a length.
        if ((wide[0] & 0x80) !== 0) return this._fail(PROTOCOL_ERROR, 'a 64-bit length with its high bit set');
        const high = wide.readUInt32BE(0);
        const low = wide.readUInt32BE(4);
        if (high > 0x1fffff) return this._fail(PROTOCOL_ERROR, 'a declared payload length past 2^53');
        length = high * 4294967296 + low;
        headerLength = 10;
      }

      if (masked) {
        // A server MUST NOT mask (section 5.1). Checked from the header
        // rather than from the payload, so a server that masks is refused at
        // the first byte rather than after its whole message has been
        // buffered.
        return this._fail(PROTOCOL_ERROR, 'a frame from the server with the mask bit set');
      }
      if (reserved !== 0) {
        return this._fail(PROTOCOL_ERROR, 'a reserved bit is set but no extension was negotiated');
      }
      // Section 5.2: only seven opcodes exist. 0x0-0x2 carry data and 0x8-0xa
      // are control, and everything else in the four bits is undefined, so it
      // is refused rather than guessed at.
      if (opcode < 0x8) {
        if (opcode > 0x2) {
          return this._fail(PROTOCOL_ERROR, 'data opcode 0x' + opcode.toString(16) + ' is not defined');
        }
      } else if (opcode > 0xa) {
        return this._fail(PROTOCOL_ERROR, 'control opcode 0x' + opcode.toString(16) + ' is not defined');
      }
      if (opcode >= 0x8) {
        // Control frames: must not be fragmented and must not carry more than
        // 125 bytes (section 5.5). Both are hard protocol errors, and both are
        // the sort of thing a server that got its framing subtly wrong does
        // constantly and without noticing.
        if (!fin) return this._fail(PROTOCOL_ERROR, 'a control frame was fragmented');
        if (length > MAX_CONTROL_PAYLOAD) {
          return this._fail(PROTOCOL_ERROR, 'a ' + length + ' byte control frame, over the 125 byte limit');
        }
      }

      // Checked against the declared length, before a single payload byte is
      // waited for. Four bytes on the wire should not be able to make this
      // process try to hold 2^63 of anything.
      if (length > this.maxMessageBytes) {
        return this._fail(TOO_BIG, 'a frame of ' + length + ' bytes, over the ' + this.maxMessageBytes + ' byte limit');
      }
      if (this._size < headerLength + length) return;

      const whole = this._take(headerLength + length);
      const payload = whole.subarray(headerLength);

      if (opcode < 0x8) {
        this._data(fin, opcode, payload);
        if (this._done) return;
        continue;
      }
      if (opcode === OP_CLOSE) {
        this._closed(payload);
        return;
      }
      // Pongs are surfaced and then dropped: nothing in this client sends a
      // ping, so there is no liveness number for one to update.
      if (opcode === OP_PING) this.onPing(payload);
      else this.onPong(payload);
    }
  }

  _data(fin, opcode, payload) {
    if (opcode === OP_CONTINUATION) {
      if (!this._fragments) {
        return this._fail(PROTOCOL_ERROR, 'a continuation frame with no message to continue');
      }
      this._fragments.parts.push(payload);
      this._fragments.bytes += payload.length;
      if (this._fragments.bytes > this.maxMessageBytes) {
        return this._fail(TOO_BIG, 'a fragmented message over the ' + this.maxMessageBytes + ' byte limit');
      }
      if (fin) {
        const message = this._fragments;
        this._fragments = null;
        this._deliver(message.opcode, message.parts);
      }
      return;
    }
    // A new data frame while the last one is still being fragmented is the
    // one framing mistake a conforming server cannot make and a broken one
    // makes constantly, so it is checked rather than merged.
    if (this._fragments) {
      return this._fail(PROTOCOL_ERROR, 'a new data frame arrived inside a fragmented message');
    }
    if (fin) this._deliver(opcode, [payload]);
    else this._fragments = { opcode: opcode, parts: [payload], bytes: payload.length };
  }

  _deliver(opcode, parts) {
    if (opcode === OP_BINARY) {
      return this._fail(UNSUPPORTED_DATA, 'a binary frame, and this client is text only');
    }
    // One decode for the whole message, after every fragment has arrived.
    // This is the answer to the character that straddles two TCP reads: the
    // frame length is known, so the payload is only decoded once it is
    // complete, and a streaming decoder here would be strictly worse -- it
    // would turn a truncated frame into a plausible string instead of an
    // error.
    let text;
    try {
      text = decoder.decode(parts.length === 1 ? parts[0] : Buffer.concat(parts));
    } catch (err) {
      // Section 8.1: text that is not valid UTF-8 fails the connection. It
      // usually means something upstream truncated a multi-byte character,
      // and a U+FFFD in the middle of a JSON document is worse than a close.
      return this._fail(INVALID_PAYLOAD, 'a text frame that is not valid UTF-8');
    }
    this.onText(text);
  }

  _closed(payload) {
    if (payload.length === 0) {
      // A Close with no payload is a legal way to say "I am going away"
      // without saying why. The caller gets 1005 because "no status" is the
      // fact, and 1005 is the code the protocol defines for it.
      this._done = true;
      this.onClose(NO_STATUS, '');
      return;
    }
    if (payload.length === 1) {
      this._fail(PROTOCOL_ERROR, 'a close frame with a one byte payload');
      return;
    }
    const code = payload.readUInt16BE(0);
    if (!validCloseCode(code)) {
      this._fail(PROTOCOL_ERROR, 'a close frame with the reserved code ' + code);
      return;
    }
    let reason = '';
    if (payload.length > 2) {
      try {
        reason = decoder.decode(payload.subarray(2));
      } catch (err) {
        this._fail(INVALID_PAYLOAD, 'a close reason that is not valid UTF-8');
        return;
      }
    }
    this._done = true;
    this.onClose(code, reason);
  }

  _fail(closeCode, detail) {
    if (this._done) return;
    this._done = true;
    this.onError(new WebSocketError('protocol error: ' + detail, 'EPROTO', closeCode), closeCode);
  }
}

// Strict, so a text frame that is not valid UTF-8 fails the connection rather
// than arriving full of U+FFFD. One instance is shared on purpose: decode()
// without the stream option resets the decoder on every call, so there is no
// state carried between messages to get wrong, and JavaScript is single
// threaded so no two calls can overlap.
const decoder = new TextDecoder('utf-8', { fatal: true });

function noop() {}

/* The client. */
class WebSocketClient extends EventEmitter {
  // On the class as well as the instance, because a caller comparing
  // `ws.readyState === 3` is a caller who should not have to know that 3 is
  // three.
  static get CONNECTING() { return CONNECTING; }
  static get OPEN() { return OPEN; }
  static get CLOSING() { return CLOSING; }
  static get CLOSED() { return CLOSED; }

  constructor(url, options) {
    super();
    const opts = options || {};
    this.url = String(url);
    this.readyState = CONNECTING;

    this._headers = opts.headers || null;
    this._maxBytes = opts.maxMessageBytes || DEFAULT_MAX_BYTES;
    this._maxSendQueueBytes = opts.maxSendQueueBytes || DEFAULT_MAX_BYTES;
    this._handshakeTimeout = opts.handshakeTimeout === undefined
      ? DEFAULT_HANDSHAKE_TIMEOUT
      : opts.handshakeTimeout;
    this._closeTimeout = opts.closeTimeout === undefined ? CLOSE_TIMEOUT : opts.closeTimeout;

    this._outbox = [];
    this._outboxBytes = 0;
    this._backedUp = false;
    this._finished = false;
    this._errorEmitted = false;
    this._closeSent = false;

    this._headRaw = [];
    this._headBytes = 0;
    this._head = '';
    this._headDone = false;
    this._decoder = new StringDecoder('utf8');

    this._parseUrl();
    this._extraHeaders = this._requestHeaders();

    // Bound once. Everything below is handed either to a socket or to the
    // parser as a bare function, so without this `this` would be the socket
    // or the parser, and a handler that quietly ran against the wrong object
    // would be the single worst bug in the file.
    const handlers = ['_sendRequest', '_onData', '_onDrain', '_onEnd', '_onSocketError',
      '_onSocketClose', '_onText', '_onPing', '_onCloseFrame', '_onParserError',
      '_onHandshakeTimeout', '_onCloseTimeout'];
    for (let i = 0; i < handlers.length; i++) this[handlers[i]] = this[handlers[i]].bind(this);

    this._parser = new Parser({
      maxMessageBytes: this._maxBytes,
      onText: this._onText,
      onPing: this._onPing,
      onPong: noop,
      onClose: this._onCloseFrame,
      onError: this._onParserError,
    });
    this._connect();
  }

  /* A bad URL is a mistake in the calling code, so it throws here rather
   * than becoming an `error` event. The browser throws for the same reason. */
  _parseUrl() {
    let parsed;
    try {
      parsed = new URL(this.url);
    } catch (err) {
      throw new TypeError('not a URL: ' + this.url);
    }
    if (parsed.protocol !== 'ws:' && parsed.protocol !== 'wss:') {
      throw new TypeError('a WebSocket URL must be ws: or wss:, not ' + parsed.protocol);
    }
    if (!parsed.hostname) throw new TypeError('no host in ' + this.url);

    this._secure = parsed.protocol === 'wss:';
    // WHATWG URL keeps the brackets on an IPv6 literal, which is right for a
    // Host header and wrong for a DNS lookup.
    this._host = parsed.hostname.replace(/^\[|\]$/g, '');
    this._port = Number(parsed.port) || (this._secure ? 443 : 80);
    this._path = (parsed.pathname || '/') + (parsed.search || '');
    // `host` is already `hostname:port` with the port omitted when it is the
    // scheme's default, which is exactly the rule for a Host header.
    this._hostHeader = parsed.host;
  }

  _connect() {
    const socket = this._secure
      ? tls.connect({ host: this._host, port: this._port, servername: this._servername() })
      : net.connect({ host: this._host, port: this._port });
    this._socket = socket;

    // Every one of these is installed before anything can fire, and in
    // particular the `error` one: a net.Socket with no error listener throws
    // the error as an uncaught exception, which would take the whole
    // extension host down over one refused connection.
    socket.on('error', this._onSocketError);
    socket.on('data', this._onData);
    socket.on('drain', this._onDrain);
    socket.on('end', this._onEnd);
    socket.on('close', this._onSocketClose);
    // A control frame is small and latency is the whole point of one. Nagle
    // would hold a pong behind the next delta.
    socket.setNoDelay(true);

    this._handshakeTimer = setTimeout(this._onHandshakeTimeout, this._handshakeTimeout);
    // Two different events, not one, and getting this wrong sends the request
    // in the clear: a tls.Socket also emits `connect` when the TCP
    // connection is up, which for TLS is before there is a secure channel to
    // write into. Certificate validation is left on -- no rejectUnauthorized,
    // no CA override, no way for a caller to turn it off.
    const ready = this._secure ? 'secureConnect' : 'connect';
    socket.on(ready, this._sendRequest);
  }

  _servername() {
    // SNI is not a hostname when the host is an address, and a TLS 1.3
    // implementation is entitled to object to one.
    return net.isIP(this._host) ? undefined : this._host;
  }

  _sendRequest() {
    if (this.readyState !== CONNECTING || this._requestSent) return;
    this._requestSent = true;
    try {
      const key = crypto.randomBytes(16).toString('base64');
      this._key = key;
      this._expected = acceptFor(key);

      const lines = [
        'GET ' + this._path + ' HTTP/1.1',
        'Host: ' + this._hostHeader,
        'Upgrade: websocket',
        'Connection: Upgrade',
        'Sec-WebSocket-Key: ' + key,
        'Sec-WebSocket-Version: 13',
      ];
      const extra = this._extraHeaders;
      for (let i = 0; i < extra.length; i++) lines.push(extra[i]);

      this._write(Buffer.from(lines.join('\r\n') + '\r\n\r\n', 'latin1'));
    } catch (err) {
      this._fail(new WebSocketError('could not build the handshake: ' + err.message, 'EINTERNAL'), null);
    }
  }

  /* The caller's headers, as wire lines, with the ones this client already
   * sends dropped so nothing is duplicated. Computed once, in the
   * constructor, because it validates and a validation that threw from inside
   * a socket callback would be an uncaught exception rather than a mistake
   * the caller could see. */
  _requestHeaders() {
    const out = [];
    if (!this._headers) return out;
    const names = Object.keys(this._headers);
    for (let i = 0; i < names.length; i++) {
      const name = names[i];
      const value = this._headers[name];
      if (value === undefined || value === null) continue;
      // A header value with a newline in it is request splitting, and the
      // token in the Authorization header is a string from a setting rather
      // than a literal in the source. Cheap to refuse, expensive to debug.
      if (/[^\t\x20-\x7e]/.test(String(value))) {
        throw new TypeError('a header value may not contain control characters: ' + name);
      }
      if (!name || /[^\x21-\x7e]/.test(name)) {
        throw new TypeError('not a header name: ' + name);
      }
      if (isReservedHeader(name)) continue;
      out.push(name + ': ' + value);
    }
    return out;
  }

  /* Send one text message. Throws rather than emitting, because handing a
   * socket a number when it wanted a string is a mistake in the caller and
   * the browser's send() throws for the same one. */
  send(data) {
    if (typeof data !== 'string') {
      throw new TypeError('send() takes a string: this socket is text only');
    }
    if (this.readyState !== OPEN) {
      const err = new Error('cannot send: the socket is not open (readyState ' + this.readyState + ')');
      err.name = 'InvalidStateError';
      throw err;
    }
    this._write(encodeFrame(OP_TEXT, Buffer.from(data, 'utf8'), { mask: true }));
  }

  /* Close the connection politely: one Close frame with a status code, then
   * the socket. Calling it twice, or after the socket has already gone, is
   * not an error and does nothing. */
  close(code, reason) {
    const status = code === undefined ? 1000 : code;
    const text = reason === undefined ? '' : String(reason);
    if (!validCloseCode(status)) {
      throw new TypeError('cannot send the close code ' + status + ': it is reserved or out of range');
    }
    if (Buffer.byteLength(text, 'utf8') > MAX_CLOSE_REASON) {
      throw new RangeError('a close reason may be at most ' + MAX_CLOSE_REASON + ' bytes');
    }
    if (this.readyState === CLOSED || this.readyState === CLOSING) return;
    if (this.readyState === CONNECTING) {
      // The browser treats this as an abort rather than a close: there is no
      // connection to close, so there is no status to report, and reporting
      // 1000 would be claiming a clean shutdown that did not happen.
      this._finish(ABNORMAL, '');
      return;
    }
    this.readyState = CLOSING;
    this._outbox.length = 0;
    this._outboxBytes = 0;
    this._closeSent = true;
    // What the caller is told if the peer never answers. The close event
    // reports the code this endpoint sent, the same way a browser's does,
    // rather than reporting 1006 for a shutdown that was not one.
    this._sentClose = { code: status, reason: text };
    this._write(encodeFrame(OP_CLOSE, closePayload(status, text), { mask: true }));
    this._endWhenDrained();
  }
  /* ---- events out of the socket ---- */

  _onData(chunk) {
    if (this._finished) return;
    try {
      if (this._headDone) {
        this._parser.push(chunk);
        return;
      }
      this._headRaw.push(chunk);
      this._headBytes += chunk.length;
      // The response head has no length prefix, so this is the one place in
      // the file where a character really can be split across two TCP reads:
      // an error page or a reason phrase with an em dash in it arrives as raw
      // bytes with nothing to say where it ends.
      this._head += this._decoder.write(chunk);
      if (this._headBytes > MAX_HANDSHAKE_HEAD) {
        return this._fail(new WebSocketError('the handshake response head is over ' + MAX_HANDSHAKE_HEAD + ' bytes', 'EPROTO'), null);
      }
      const at = this._head.indexOf('\r\n\r\n');
      if (at === -1) return;
      this._finishHead(at);
    } catch (err) {
      // A bug in the read path would be an uncaught exception in the
      // extension host, and there is nowhere better for it to land than an
      // error event. A listener that throws does not come through here:
      // _emit catches that one and keeps the connection, which is what the
      // browser does.
      this._fail(new WebSocketError('internal error while reading: ' + (err && err.message), 'EINTERNAL'), null);
    }
  }

  _finishHead(at) {
    // The decoded head and the raw head are both kept, and for one reason:
    // the byte length of the head is not the length of the decoded string
    // when any character in it was multi-byte, and the leftover bytes after
    // the terminator are frame data that must not be dropped.
    const raw = Buffer.concat(this._headRaw, this._headBytes);
    const atBytes = raw.indexOf(TERMINATOR);
    const headText = this._head.slice(0, at);
    const leftover = atBytes === -1 ? null : raw.subarray(atBytes + 4);
    this._headDone = true;
    clearTimeout(this._handshakeTimer);

    const lines = headText.split('\r\n');
    const status = /^HTTP\/1\.1 (\d{3})(?: (.*))?$/.exec(lines[0] || '');
    if (!status) {
      return this._fail(new WebSocketError('the handshake reply was not HTTP/1.1: ' + oneLine(lines[0]), 'EPROTOCOL'), null);
    }
    const code = Number(status[1]);
    const fields = parseHeaders(lines);
    if (code !== 101) return this._refuse(code, status[2] || '', fields);

    // Verified, not assumed. A server that answers with a plausible 101 and
    // the wrong accept is not a websocket server, and continuing past that
    // point means parsing whatever it says next as frames.
    const got = fields['sec-websocket-accept'];
    if (!got) {
      return this._fail(new WebSocketError('the handshake reply has no Sec-WebSocket-Accept', 'EPROTOCOL'), null);
    }
    if (got.trim() !== this._expected) {
      return this._fail(new WebSocketError(
        'Sec-WebSocket-Accept is "' + got.trim() + '" where the handshake requires "' + this._expected + '"'
        + ' -- whatever answered ' + this.url + ' is not a websocket server',
        'EPROTOCOL'), null);
    }
    // Both are required by section 4.2.2 and both are checked case
    // insensitively, because uvicorn sends them lowercased and the RFC says
    // header names are case insensitive anyway.
    if (!hasToken(fields.upgrade, 'websocket')) {
      return this._fail(new WebSocketError('the handshake reply does not say Upgrade: websocket', 'EPROTOCOL'), null);
    }
    if (!hasToken(fields.connection, 'upgrade')) {
      return this._fail(new WebSocketError('the handshake reply does not say Connection: Upgrade', 'EPROTOCOL'), null);
    }

    this.readyState = OPEN;
    this._emit('open');
    // After `open`, never before: a caller that queues its first send in the
    // open handler must not be overtaken by a frame the server pipelined
    // behind the 101.
    if (leftover && leftover.length) this._parser.push(leftover);
  }

  _refuse(code, phrase, fields) {
    // The daemon's auth middleware answers an unauthenticated websocket with
    // a 403 before the upgrade, because a websocket cannot be answered with a
    // 401 and a hang reads as a refusal. This is the one handshake failure
    // with a known fix, so it is the one that gets a fix in the message.
    if (code === 401 || code === 403) {
      return this._fail(new WebSocketError(
        this.url + ' refused the connection: HTTP ' + code + ' ' + phrase
        + ' -- this daemon wants a token; set openmirror.token or OPENMIRROR_TOKEN',
        'EAUTH'), null);
    }
    let detail = '';
    if (fields['content-type']) detail = ' (' + oneLine(fields['content-type']) + ')';
    this._fail(new WebSocketError(
      this.url + ' refused the connection: HTTP ' + code + ' ' + phrase + detail
      + ' -- it answered the upgrade with ' + code + ' rather than 101',
      'EHTTP', null));
  }

  _onHandshakeTimeout() {
    this._fail(new WebSocketError(
      'the handshake to ' + this.url + ' did not finish in ' + this._handshakeTimeout + 'ms',
      'ETIMEDOUT'), null);
  }

  _onSocketError(err) {
    if (this._finished) return;
    // ECONNREFUSED and ENOTFOUND are the two a person in an editor actually
    // hits, and both are the daemon not running rather than a network fault.
    const message = this._headDone
      ? 'the connection to ' + this.url + ' failed: ' + err.message
      : this.url + ' is not answering: ' + err.message;
    const code = err.code === 'ECONNREFUSED' || err.code === 'ENOTFOUND' ? 'ECONNREFUSED' : 'ENETWORK';
    this._fail(new WebSocketError(message, code), null);
  }

  _onEnd() {
    // The peer sent FIN. Half-close our side so the socket can finish; if a
    // Close frame arrived first this is already handled and this is a no-op.
    if (this._socket && !this._socket.destroyed) this._socket.end();
  }

  _onSocketClose() {
    if (this._finished) return;
    // A close this endpoint started is reported with the code it sent. A
    // socket that just died with no Close frame on either side is reported as
    // 1006, which is the code the protocol defines for exactly that and, like
    // 1005, never goes out on the wire.
    if (this._sentClose) this._finish(this._sentClose.code, this._sentClose.reason);
    else this._finish(ABNORMAL, '');
  }

  /* Emit, and stop a caller's exception from becoming an uncaught exception
   * in the extension host. A handler that throws is the caller's own mistake,
   * and the browser does not close a socket over one: it reports the
   * exception and the conversation carries on.
   *
   * What this does not do is isolate the other listeners. A throw inside
   * emit() stops the rest of that one emit, the same as any EventEmitter, and
   * faking it would mean calling listeners by hand behind EventEmitter's back
   * and quietly breaking `once`. A caller with a throwing handler sees the
   * message; what they must not get is a dead socket or a dead host. */
  _emit(event, a, b) {
    try {
      this.emit(event, a, b);
    } catch (err) {
      console.error('openmirror: websocket: a ' + event + ' handler threw: ' + (err && err.message));
    }
  }

  _onText(text) {
    this._emit('message', text);
  }

  _onPing(payload) {
    if (this.readyState === CLOSED) return;
    // A Pong carries the same application data the Ping arrived with
    // (section 5.5.3), and a Pong from a client is a client frame like any
    // other, so it is masked like any other.
    this._write(encodeFrame(OP_PONG, payload, { mask: true }));
  }

  _onCloseFrame(code, reason) {
    let graceful = false;
    if (this.readyState === OPEN) {
      // The peer went first. Section 5.5.1: answer with a Close and then
      // close the connection, so the peer learns it was not cut off. The
      // answer goes out ahead of anything queued and the socket is ended
      // rather than destroyed, because destroying it under a frame that is
      // still in the write buffer is how the answer gets lost.
      this.readyState = CLOSING;
      graceful = true;
      if (!this._closeSent) {
        this._closeSent = true;
        this._writeNow(encodeFrame(OP_CLOSE, closePayload(code === NO_STATUS ? 1000 : code, ''), { mask: true }));
      }
      this._socket.end();
    }
    this._finish(code, reason, graceful);
  }

  _onParserError(err) {
    this._fail(err, err.closeCode);
  }

  /* ---- writing ---- */

  _write(buffer) {
    const socket = this._socket;
    if (!socket || socket.destroyed) return;
    // Already backed up: everything new goes behind what is waiting, or the
    // order the daemon sees is the order the socket happened to flush in.
    if (this._backedUp) return this._enqueue(buffer);
    // A false return means the socket has *taken* the bytes and is now above
    // its high water mark. They are already on their way. Writing them again
    // when the drain comes would send the frame twice, so what is queued here
    // is the frames that come after this one, never this one.
    if (!socket.write(buffer)) this._backedUp = true;
  }

  /* Used only for the close frame of a failure, which goes ahead of anything
   * still queued. A protocol error is a reason to stop, not a reason to make
   * the daemon wait behind a megabyte of deltas to hear about it. */
  _writeNow(buffer) {
    if (this._socket && !this._socket.destroyed) this._socket.write(buffer);
  }

  _enqueue(buffer) {
    this._outbox.push(buffer);
    this._outboxBytes += buffer.length;
    if (this._outboxBytes > this._maxSendQueueBytes) {
      this._fail(new WebSocketError(
        'unsent frames passed the ' + this._maxSendQueueBytes + ' byte limit while the connection was backed up'
        + ' -- giving up rather than growing until the extension host runs out of memory',
        'EOVERFLOW', TOO_BIG), TOO_BIG);
    }
  }

  _onDrain() {
    this._backedUp = false;
    while (this._outbox.length) {
      const next = this._outbox.shift();
      this._outboxBytes -= next.length;
      if (this._socket.destroyed) return;
      if (!this._socket.write(next)) {
        // Back over the high water mark with the rest still waiting.
        this._backedUp = true;
        return;
      }
    }
    this._endWhenDrained();
  }

  _endWhenDrained() {
    if (this.readyState !== CLOSING || this._outbox.length) return;
    if (!this._socket || this._socket.destroyed) return;
    this._socket.end();
    // A backstop, not a wait: unref'd so it never holds the event loop open
    // on its own, which matters at extension deactivation.
    this._closeTimer = setTimeout(this._onCloseTimeout, this._closeTimeout);
    if (this._closeTimer.unref) this._closeTimer.unref();
  }

  _onCloseTimeout() {
    this._reapTimer = null;
    if (this._socket) this._socket.destroy();
    if (this._finished) return;
    if (this._sentClose) this._finish(this._sentClose.code, this._sentClose.reason);
    else this._finish(ABNORMAL, '');
  }

  /* ---- ending ---- */

  /* Every failure ends the same way: an error, then exactly one close, then
   * the socket is gone. The order matters. A caller that has already torn
   * its panel down has still been told why. */
  _fail(err, closeCode) {
    if (this._finished) return;
    if (!this._errorEmitted) {
      this._errorEmitted = true;
      if (this.listenerCount('error') > 0) this._emit('error', err);
      // An EventEmitter with an unhandled 'error' throws, which from an
      // async socket handler is an uncaught exception in the extension host.
      // A failure with nobody listening is still a failure and is still
      // logged rather than swallowed.
      else console.error('openmirror: websocket: ' + err.message);
    }
    let graceful = false;
    if (closeCode && this.readyState === OPEN) {
      // The socket is alive and the frame was bad, so this is the one failure
      // the daemon deserves to hear about: it says which code it broke, and
      // it goes out ahead of whatever is still queued. Then end() rather than
      // destroy(), so the frame is actually flushed instead of discarded with
      // the buffer it was in.
      graceful = true;
      this.readyState = CLOSING;
      this._closeSent = true;
      this._outbox.length = 0;
      this._outboxBytes = 0;
      this._backedUp = false;
      this._writeNow(encodeFrame(OP_CLOSE, closePayload(closeCode, oneLine(err.message).slice(0, MAX_CLOSE_REASON)), { mask: true }));
      this._socket.end();
    }
    this._finish(closeCode || ABNORMAL, '', graceful);
  }

  _finish(code, reason, graceful) {
    if (this._finished) return;
    this._finished = true;
    this.readyState = CLOSED;
    clearTimeout(this._handshakeTimer);
    clearTimeout(this._closeTimer);
    clearTimeout(this._reapTimer);
    this._outbox.length = 0;
    this._outboxBytes = 0;
    if (this._socket) {
      this._socket.removeAllListeners('data');
      if (graceful) {
        // The close frame is in flight. Give the socket a moment to flush it
        // and let go of it: unref'd so it never holds the extension host's
        // event loop open, and reaped on a timer so a peer that never answers
        // cannot leak a handle either.
        if (this._socket.unref) this._socket.unref();
        this._reapTimer = setTimeout(this._onCloseTimeout, this._closeTimeout);
        if (this._reapTimer.unref) this._reapTimer.unref();
      } else {
        // destroy() rather than end(): on a socket that is still connecting
        // or on a peer that has stopped reading, end() waits and the handle
        // stays open. Nothing after this point wants the socket.
        this._socket.destroy();
      }
    }
    this._emit('close', code, reason);
  }
}

/* Header names this client sets itself. A caller may send anything else; one
 * of these is dropped rather than appended, because two of them make the
 * request ambiguous and none of them is something a caller needs to win. */
const RESERVED_HEADERS = {
  host: true,
  upgrade: true,
  connection: true,
  'sec-websocket-key': true,
  'sec-websocket-version': true,
  'sec-websocket-accept': true,
  'sec-websocket-extensions': true,
  'sec-websocket-protocol': true,
  'content-length': true,
};

function isReservedHeader(name) {
  return RESERVED_HEADERS[String(name).toLowerCase()] === true;
}

function parseHeaders(lines) {
  const fields = {};
  for (let i = 1; i < lines.length; i++) {
    const at = lines[i].indexOf(':');
    if (at === -1) continue;
    // Last one wins, matching what every HTTP client does with a repeated
    // header. Sec-WebSocket-Accept is a single value, so a duplicate is
    // either a broken server or an attack and neither is helped by
    // concatenating.
    fields[lines[i].slice(0, at).trim().toLowerCase()] = lines[i].slice(at + 1).trim();
  }
  return fields;
}

function hasToken(value, token) {
  if (!value) return false;
  const wanted = token.toLowerCase();
  const parts = value.toLowerCase().split(',');
  for (let i = 0; i < parts.length; i++) {
    if (parts[i].trim() === wanted) return true;
  }
  return false;
}

function oneLine(text) {
  return String(text === undefined || text === null ? '' : text).replace(/\s+/g, ' ').trim();
}

module.exports = {
  WebSocketClient: WebSocketClient,
  WebSocketError: WebSocketError,
  CONNECTING: CONNECTING,
  OPEN: OPEN,
  CLOSING: CLOSING,
  CLOSED: CLOSED,
  // Internal, but exported so the tests can drive the framing directly: a
  // handshake test needs a server that answers with a deliberately wrong
  // accept, and a framing test needs frames built byte by byte. The
  // underscore says do not call this from the extension.
  _accept: acceptFor,
  _encode: encodeFrame,
  _Parser: Parser,
  _GUID: GUID,
};

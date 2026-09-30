/* Pictures, pasted or dropped into the composer.
 *
 * A port of `openmirror/static/attachments.js`, unchanged in substance, because
 * a screenshot of an error is the single most valuable thing you can hand a
 * coding agent and the capability is worth having in the panel for the same
 * reason it is worth having in the browser.
 *
 * Three ways in, because people use three ways out: paste, drop, and a button
 * for the rest -- and because a control you can see is a control that does not
 * need discovering.
 *
 * The pictures are shrunk before they are sent, and that is not an
 * optimisation. A 2560x1440 screenshot is 14MB of pixels; base64 makes it 19MB
 * of JSON in one frame, and it crosses `postMessage` into an extension host
 * before it crosses a socket. Providers resize server-side anyway, so sending
 * the original spends somebody's bandwidth to arrive at the same place. The
 * long edge goes to 1568px, which is the size every major provider recommends,
 * and it is done with a canvas rather than sent to a server to be done, because
 * there is no reason for the picture to leave the machine before the model has
 * decided it is wanted.
 *
 * `attachments` on the wire is exactly the shape `session.py` reads:
 * `{type: 'image', data: <base64, no prefix>, media_type: ...}`.
 *
 * The pure half is above the line: what a type is allowed to be, how big a
 * picture may get, what the frame looks like. The canvas is below it and is
 * only reached from a paste, a drop or a click.
 */

import { $, button, clear, el, kb } from './dom.js';

export const MAX_EDGE = 1568;
const MAX_BYTES = 12 * 1024 * 1024;
const MAX_PICTURES = 6;

const TYPES = {
  'image/png': 'image/png',
  'image/jpeg': 'image/jpeg',
  'image/jpg': 'image/jpeg',
  'image/webp': 'image/webp',
  'image/gif': 'image/gif',
};

/** The media type a file is allowed to arrive as, or null. */
export function normaliseType(type) {
  return TYPES[String(type || '').toLowerCase()] || null;
}

/**
 * The size a picture should be sent at.
 *
 * `null` when it is already small enough, which is the common case for a
 * screenshot of a terminal rather than a photograph.
 */
export function scaleFor(width, height, maxEdge) {
  const w = Math.round(Number(width) || 0);
  const h = Math.round(Number(height) || 0);
  if (w <= 0 || h <= 0) {
    return null;
  }
  const long = Math.max(w, h);
  const ceiling = Number(maxEdge) || MAX_EDGE;
  if (long <= ceiling) {
    return null;
  }
  const scale = ceiling / long;
  return { width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)) };
}

/** PNG stays PNG; everything else becomes JPEG. Re-encoding a photo as PNG makes
 *  it bigger, and a screenshot of text is the case that matters and the one
 *  that compresses. */
export function outType(mediaType) {
  return mediaType === 'image/png' ? 'image/png' : 'image/jpeg';
}

/**
 * The `submit` frame's `attachments`, and nothing else.
 *
 * The preview is what the page shows and is deliberately not in here: the frame
 * crosses into a process that has no use for it, and a thumbnail the size of a
 * screenshot is a few hundred kilobytes of base64 per message for nothing.
 */
export function attachmentFrame(items) {
  return (items || []).map((item) => ({
    type: 'image',
    data: String(item.data || ''),
    media_type: String(item.media_type || 'image/png'),
  }));
}

/** Why a picture was refused, in the place the person is already looking. */
export function refusal(file) {
  const name = (file && file.name) || 'That file';
  if (!normaliseType(file && file.type)) {
    return `${name} is not a picture. PNG, JPEG, WebP or GIF.`;
  }
  if (Number(file.size) > MAX_BYTES) {
    return `${name} is ${Math.round(file.size / (1024 * 1024))}MB - too large to send.`;
  }
  return '';
}

/* ========================================================================== *
 * The DOM half.
 * ========================================================================== */

/**
 * The strip above the composer.
 *
 * One removable thumbnail per picture, because the alternative -- a pile of
 * pictures you can only clear by guessing which one is wrong -- is how you end
 * up resending a screenshot you did not mean to.
 *
 * @param {object} ctx
 * @param {function} [ctx.onChange]  called whenever the pile changes
 */
export function createPictures(ctx) {
  const context = ctx || {};
  const box = $('#pictures');
  const state = { items: [], complaint: '' };

  function draw() {
    if (!box) {
      return;
    }
    clear(box);
    // A complaint with no pictures behind it has nothing to sit under, so the
    // strip appears for the message alone and goes on the next add.
    box.hidden = !state.items.length && !state.complaint;
    if (state.complaint) {
      box.appendChild(el('p', 'attach-error', state.complaint));
    }
    state.items.forEach((item, index) => {
      const chip = el('div', 'attach');
      const thumb = document.createElement('img');
      thumb.className = 'thumb';
      thumb.src = `data:${item.media_type};base64,${item.preview || item.data}`;
      thumb.alt = `attached picture ${index + 1}`;
      const remove = button('icon remove', 'x', `Remove attached picture ${index + 1}`);
      remove.onclick = (event) => {
        event.preventDefault();
        state.items.splice(index, 1);
        draw();
      };
      chip.append(thumb, el('span', 'size', `${item.media_type.replace('image/', '')} ${kb(item.bytes)}`), remove);
      box.appendChild(chip);
    });
    if (context.onChange) {
      context.onChange(state.items.length);
    }
  }

  function say(text) {
    state.complaint = String(text || '');
    draw();
  }

  /* Everything waiting on the composer, in the shape the host reads. */
  function pending() {
    return attachmentFrame(state.items);
  }

  function count() {
    return state.items.length;
  }

  /* The pictures went with the message, so the strip empties here rather than
   * waiting for an answer -- resending the same screenshot on the next turn
   * because the strip still showed it is worse than not clearing it. */
  function reset() {
    state.items = [];
    state.complaint = '';
    draw();
  }

  async function take(files) {
    state.complaint = '';
    for (const file of [...files].slice(0, MAX_PICTURES - state.items.length)) {
      if (state.items.length >= MAX_PICTURES) {
        say(`${MAX_PICTURES} pictures is the limit - send them in separate messages.`);
        break;
      }
      // Named rather than ignored. A dropped file that vanishes without a word
      // reads as the panel being broken, and the fix is one export away.
      const why = refusal(file);
      if (why) {
        say(why);
        continue;
      }
      try {
        state.items.push(await shrink(file));
      } catch (error) {
        say(`That picture could not be read: ${error && error.message}`);
      }
    }
    draw();
  }

  draw();
  return { pending, count, take, reset, say, get complaint() { return state.complaint; } };
}

/* Read a file, shrink it, and hand back the internal shape.
 *
 * Canvas first and the file's own bytes only if there is no canvas, which is an
 * old browser -- and a picture that arrives slightly too big beats a picture
 * that does not arrive.
 */
async function shrink(file) {
  const mediaType = normaliseType(file.type);
  const original = await read(file);
  const resized = await resize(original, mediaType);
  if (resized) {
    return resized;
  }
  return { data: original, media_type: mediaType, bytes: file.size, preview: original };
}

function read(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(reader.error || new Error('the panel could not read it'));
    reader.readAsDataURL(file);
  });
}

async function resize(data, mediaType) {
  const image = await decode(data, mediaType);
  const size = scaleFor(image.width, image.height, MAX_EDGE);
  if (!size) {
    return { data, media_type: mediaType, bytes: Math.round((data.length * 3) / 4), preview: data };
  }
  const canvas = document.createElement('canvas');
  canvas.width = size.width;
  canvas.height = size.height;
  const ctx2d = canvas.getContext('2d');
  if (!ctx2d) {
    return null;
  }
  ctx2d.drawImage(image, 0, 0, size.width, size.height);
  const out = outType(mediaType);
  const url = canvas.toDataURL(out, 0.92);
  return { data: strip(url), media_type: out, bytes: bytesOf(strip(url)), preview: data };
}

/** Base64 is four characters for three bytes; that is the whole arithmetic. */
function bytesOf(data) {
  return Math.round((String(data || '').length * 3) / 4);
}

function strip(dataUrl) {
  return String(dataUrl || '').split(',', 2)[1] || '';
}

function decode(data, mediaType) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('the panel could not decode it'));
    image.src = `data:${mediaType};base64,${data}`;
  });
}

/** Everything a person can do to get a picture into the composer. */
export function wirePictures(pictures) {
  const input = $('#input');
  const field = $('#field');
  if (!input || !field || !pictures) {
    return;
  }

  input.addEventListener('paste', (event) => {
    const files = picturesFrom(event.clipboardData);
    if (!files.length) {
      return;                        // an ordinary text paste: leave it alone
    }
    event.preventDefault();
    pictures.take(files);
  });

  // A drop on the textarea itself would put the file's *name* into the text,
  // which is worse than not reacting at all. The whole field takes it, and the
  // highlight says so before the drop rather than after.
  for (const name of ['dragenter', 'dragover']) {
    field.addEventListener(name, (event) => {
      if (!event.dataTransfer || !['...event.dataTransfer.types'].includes('Files')) {
        return;
      }
      event.preventDefault();
      field.classList.add('dropping');
    });
  }
  for (const name of ['dragleave', 'dragend']) {
    field.addEventListener(name, () => field.classList.remove('dropping'));
  }
  field.addEventListener('drop', (event) => {
    event.preventDefault();
    field.classList.remove('dropping');
    const files = picturesFrom(event.dataTransfer);
    // A dropped file that is not a picture is still worth saying no to: a
    // silent no looks like the drop missed.
    if (!files.length && event.dataTransfer && event.dataTransfer.files
      && event.dataTransfer.files.length) {
      pictures.say('Drop a picture - PNG, JPEG, WebP or GIF.');
      return;
    }
    pictures.take(files);
  });

  const pick = $('#attach');
  if (pick) {
    const chooser = el('input');
    chooser.type = 'file';
    chooser.accept = 'image/png,image/jpeg,image/webp,image/gif';
    chooser.multiple = true;
    chooser.hidden = true;
    chooser.onchange = () => {
      if (chooser.files && chooser.files.length) {
        pictures.take(chooser.files);
      }
      chooser.value = '';            // so the same file can be picked twice
    };
    pick.onclick = (event) => {
      event.preventDefault();
      chooser.click();
    };
  }
}

function picturesFrom(dataTransfer) {
  if (!dataTransfer) {
    return [];
  }
  const files = [...(dataTransfer.files || [])];
  if (files.length) {
    return files;
  }
  // A pasted screenshot arrives as an item with a file and sometimes without
  // one in `files`, depending on the browser and the source application.
  return [...(dataTransfer.items || [])]
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

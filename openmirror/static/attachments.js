/* Pictures, pasted or dropped into the composer.
 *
 * The server has carried image attachments since before this file existed —
 * `turn.submit` takes an `attachments` list and the agent puts the bytes in
 * front of the model — but nothing in the browser ever sent one, so the whole
 * capability was unreachable. That is the shape of a gap that is easy to miss
 * and expensive to notice: a screenshot of an error is the single most
 * valuable thing you can hand a coding agent, and there was no way to.
 *
 * Three ways in, because people use three ways out:
 *
 *   - Paste. The common one: copy a screenshot, walk over, cmd-V.
 *   - Drop. The common one on a second monitor, and the only one that works
 *     for a file you have not copied yet.
 *   - A button, for the rest, and because a control you can see is a control
 *     that does not need discovering.
 *
 * The pictures are shrunk before they are sent, and that is not an
 * optimisation. A 2560x1440 screenshot is 14MB of pixels; base64 makes it
 * 19MB of JSON in one websocket frame. Providers resize images server-side
 * anyway — Anthropic bills and downsamples well below this — so sending the
 * full-size original spends a person's bandwidth to arrive at the same place.
 * The long edge goes to 1568px, which is the size every major provider
 * recommends, and it is applied with a canvas rather than sent to a server to
 * be done, because there is no reason for the picture to leave the machine
 * before the model has decided it is wanted.
 *
 * `attachments` on the wire is exactly the shape `session.py` already reads:
 * `{'type': 'image', 'data': <base64, no prefix>, 'media_type': ...}`.
 */

import { $, el } from './dom.js';

const MAX_EDGE = 1568;
const MAX_BYTES = 12 * 1024 * 1024;
const MAX_PICTURES = 6;

const TYPES = {
  'image/png': 'image/png',
  'image/jpeg': 'image/jpeg',
  'image/jpg': 'image/jpeg',
  'image/webp': 'image/webp',
  'image/gif': 'image/gif',
};

const state = { items: [] };

/* Why a picture was refused, in the place the person is already looking.
 *
 * Not the app's global `notice()`: that lives in `app.js`, which wires the
 * whole page on load, so importing it from here would run the app twice.
 * Beside the thumbnails is also the better place — the reason a drop did
 * nothing is a property of that drop, and the strip it failed to reach is
 * where a person will look.
 */
let complaint = '';

function say(text) {
  complaint = text;
  draw();
}

/* Everything the composer has attached, in the shape the server reads. */
export function pending() {
  return state.items.map(({ data, media_type: mediaType }) => ({ type: 'image', data, media_type: mediaType }));
}

export function count() {
  return state.items.length;
}

export function clear() {
  state.items = [];
  complaint = '';
  draw();
}

/* Add pictures from a FileList, a DataTransfer, or a paste event's items. */
export async function take(files) {
  complaint = '';
  for (const file of [...files].slice(0, MAX_PICTURES - state.items.length)) {
    if (state.items.length >= MAX_PICTURES) {
      say(`${MAX_PICTURES} pictures is the limit — send them in separate messages.`);
      break;
    }
    if (!TYPES[file.type]) {
      // Named rather than ignored. A dropped file that vanishes without a word
      // reads as the page being broken, and the fix — a PNG — is one export
      // away.
      say(`${file.name || 'That file'} is not a picture. PNG, JPEG, WebP or GIF.`);
      continue;
    }
    if (file.size > MAX_BYTES) {
      say(`${file.name || 'That picture'} is ${mb(file.size)} — too large to send.`);
      continue;
    }
    try {
      state.items.push(await shrink(file));
    } catch (err) {
      say(`That picture could not be read: ${err.message}`);
    }
  }
  draw();
}

/* Read a file, shrink it, and hand back the wire shape.
 *
 * Canvas first and the file's own bytes only if there is no canvas — which is
 * an old browser, and a picture that arrives slightly too big beats a picture
 * that does not arrive.
 */
async function shrink(file) {
  const original = await read(file);
  const resized = await resize(original, file.type);
  return resized || { data: strip(original), media_type: file.type, bytes: file.size, preview: original };
}

function read(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.onerror = () => reject(reader.error || new Error('the browser refused to read it'));
    reader.readAsDataURL(file);
  });
}

function strip(dataUrl) {
  return String(dataUrl).split(',', 2)[1] || '';
}

async function resize(data, mediaType) {
  const image = await decode(data, mediaType);
  const long = Math.max(image.width, image.height);
  if (long <= MAX_EDGE) {
    return { data, media_type: mediaType, bytes: Math.round((data.length * 3) / 4), preview: data };
  }
  const scale = MAX_EDGE / long;
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.width * scale));
  canvas.height = Math.max(1, Math.round(image.height * scale));
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  // PNG unless it was a JPEG: re-encoding a photo as PNG makes it bigger, and
  // a screenshot of text is the case that matters and the one that compresses.
  const out = mediaType === 'image/png' ? 'image/png' : 'image/jpeg';
  const url = canvas.toDataURL(out, 0.92);
  return { data: strip(url), media_type: out, bytes: Math.round((data.length * 3) / 4), preview: data };
}

function decode(data, mediaType) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('the browser could not decode it'));
    image.src = `data:${mediaType};base64,${data}`;
  });
}

function mb(bytes) {
  return `${Math.round(bytes / (1024 * 1024))}MB`;
}

/* The strip above the composer. One thumbnail per picture, each removable,
 * because the alternative — a pile of pictures you can only clear by guessing
 * which one is wrong — is how you end up resending a screenshot you did not
 * mean to.
 */
function draw() {
  const box = $('#attachments');
  if (!box) return;
  box.textContent = '';
  // A complaint with no pictures behind it has nothing to sit under, so the
  // strip appears for the message alone and goes away again on the next add.
  box.hidden = !state.items.length && !complaint;
  if (complaint) box.appendChild(el('p', 'attach-error', complaint));
  state.items.forEach((item, index) => {
    const chip = el('div', 'attach');
    const thumb = el('img', 'thumb');
    thumb.src = `data:${item.media_type};base64,${item.preview || item.data}`;
    thumb.alt = `attached picture ${index + 1}`;
    chip.appendChild(thumb);
    chip.appendChild(el('span', 'size', `${item.media_type.replace('image/', '')} ${kb(item.bytes)}`));
    const remove = el('button', 'icon');
    remove.type = 'button';
    remove.title = 'Remove this picture';
    remove.setAttribute('aria-label', `Remove attached picture ${index + 1}`);
    remove.innerHTML = '<svg class="ic"><use href="#i-close"></use></svg>';
    remove.onclick = (e) => {
      e.preventDefault();
      state.items.splice(index, 1);
      draw();
    };
    chip.appendChild(remove);
    box.appendChild(chip);
  });
  // The send button is disabled on an empty box by the app module; this only
  // has to say whether there is anything to send.
  $('#input').dataset.hasAttachments = state.items.length ? 'yes' : '';
}

function kb(bytes) {
  return bytes >= 1024 * 1024 ? mb(bytes) : `${Math.max(1, Math.round(bytes / 1024))}kB`;
}

/* Everything a user can do to get a picture in here. */
export function wireAttachments() {
  const input = $('#input');
  const field = $('#field');
  if (!input || !field) return;

  input.addEventListener('paste', (event) => {
    const files = picturesFrom(event.clipboardData);
    if (!files.length) return;          // ordinary text paste: leave it alone
    event.preventDefault();
    take(files);
  });

  // A drop on the textarea itself would put the file's *name* into the text,
  // which is worse than not reacting at all. The whole field takes it, and the
  // highlight says so before the drop rather than after.
  ['dragenter', 'dragover'].forEach((name) => {
    field.addEventListener(name, (event) => {
      if (!event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
      event.preventDefault();
      field.classList.add('dropping');
    });
  });
  ['dragleave', 'dragend'].forEach((name) => {
    field.addEventListener(name, () => field.classList.remove('dropping'));
  });
  field.addEventListener('drop', (event) => {
    event.preventDefault();
    field.classList.remove('dropping');
    const files = picturesFrom(event.dataTransfer);
    // A dropped file that is not a picture is still worth saying no to: a
    // silent no looks like the drop missed.
    if (!files.length && event.dataTransfer?.files?.length) {
      say('Drop a picture — PNG, JPEG, WebP or GIF.');
      return;
    }
    take(files);
  });

  const pick = $('#attach');
  if (pick) {
    const chooser = el('input');
    chooser.type = 'file';
    chooser.accept = 'image/png,image/jpeg,image/webp,image/gif';
    chooser.multiple = true;
    chooser.hidden = true;
    chooser.onchange = () => {
      if (chooser.files?.length) take(chooser.files);
      chooser.value = '';               // so the same file can be picked twice
    };
    pick.onclick = (e) => {
      e.preventDefault();
      chooser.click();
    };
  }
}

function picturesFrom(dataTransfer) {
  if (!dataTransfer) return [];
  const files = [...(dataTransfer.files || [])];
  if (files.length) return files;
  // A pasted screenshot arrives as an item with a file and sometimes without
  // one in `files`, depending on the browser and the source application.
  return [...(dataTransfer.items || [])]
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

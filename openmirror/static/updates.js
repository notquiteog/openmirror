/* Updates, in Settings.
 *
 * The three steps are three buttons on purpose, and the order is the argument:
 *
 *   1. **Check** — asks GitHub what exists. Free, repeatable, and the only step
 *      that happens on its own (once a day, and never while a turn is running).
 *   2. **Download** — fetches the installer and checks it against the SHA256
 *      published in the same release. Staged as a file; nothing is run.
 *   3. The app offers to install it, or the message says what to type.
 *
 * A single "Update now" button would be one click instead of three, and
 * somebody would press it without reading the release notes, and then never
 * read anything again. The release notes are the only place a breaking change
 * is ever going to be written down.
 *
 * "Not now" clears the notice until the next check finds the same release
 * still newer than this one — which is what somebody who chose not to update
 * wants, and is not the same as being told about it and ignored forever.
 */

import { $, el, json, post } from './dom.js';
import { installUpdate, isDesktop } from './native.js';

const state = { last: null, poll: null, dismissing: false };

function ago(stamp) {
  if (!stamp) return '';
  const then = new Date(stamp);
  if (Number.isNaN(then.getTime())) return '';
  const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} hours ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

export function renderUpdate(info) {
  if (!info) return;
  state.last = info;

  $('#update-current').textContent =
    `You are on ${info.current} · ${info.platform}`
    + (info.managed ? '' : ' · running from source, so an update is a git pull');

  const box = $('#update-state');
  box.textContent = '';

  if (info.error) box.appendChild(el('p', 'update-error', info.error));

  if (info.available && info.latest) {
    const release = info.latest;
    box.appendChild(el('p', 'update-available', `${release.version} is available.`));
    box.appendChild(el('p', 'meta', [
      release.name && release.name !== release.version ? release.name : '',
      release.published ? `published ${ago(release.published)}` : '',
      release.prerelease ? 'a preview, not a final release' : '',
    ].filter(Boolean).join(' · ')));

    const notes = $('#update-notes');
    notes.hidden = false;
    notes.textContent = '';
    notes.appendChild(el('pre', 'update-notes-body', release.notes || 'No notes were published.'));
    if (release.notes_truncated) {
      notes.appendChild(el('p', 'meta',
        release.url ? 'The full notes are on the release page.' : ''));
    }
    if (release.url) {
      const link = el('a', 'update-link', 'Release notes on GitHub');
      link.href = release.url;
      link.target = '_blank';
      link.rel = 'noreferrer';
      notes.appendChild(link);
    }
    if (!release.asset) {
      box.appendChild(el('p', 'update-error',
        `That release has no installer for ${info.platform}. The notes below still apply if you ` +
        'build from source.'));
    }
  } else if (!info.error) {
    box.appendChild(el('p', 'meta', info.checked_at
      ? `Up to date${info.latest ? '' : ', and nothing newer has been published'}. `
        + `Last checked ${ago(info.checked_at)}.`
      : 'Not checked yet.'));
  }

  $('#update-dot').hidden = !info.available;
  $('#update-dismiss').hidden = !info.available || state.dismissing;
  $('#update-apply').hidden = !(info.available && info.latest && info.latest.asset);
  $('#update-apply').textContent = info.staged ? 'Install' : 'Download';
  $('#update-apply').disabled = Boolean(info.staged) || Boolean(info.downloading);
  $('#update-check').disabled = Boolean(info.downloading);
  $('#update-check').textContent = info.downloading ? 'Working…' : 'Check now';
  $('#update-current').title = info.enabled
    ? ''
    : 'Update checks are off on this install.';
}

function setProgress(info) {
  const wrap = $('#update-progress-wrap');
  if (!info.downloading && !info.staged) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  $('#update-progress-fill').style.width = `${info.progress || 0}%`;
  const mb = (info.downloaded || 0) / (1024 * 1024);
  $('#update-progress-text').textContent = info.downloading
    ? `downloading — ${mb.toFixed(1)}MB`
    : `downloaded — ${mb.toFixed(1)}MB`;
}

async function check(force = true) {
  renderUpdate(state.last);
  const got = await post('/api/update/check', { force, prerelease: false });
  if (!got) return;
  /* A 4xx is not a rejection — `post` resolves for any answer the server
     gave. The detail is on the body, and for a check that means "not right
     now" rather than an error worth shouting about. */
  const body = got.data || {};
  renderUpdate({ ...(state.last || {}), ...body, error: body.error || '' });
  if (body.deferred) $('#update-error')?.append(
    el('p', 'meta', body.deferred),
  );
}

async function download() {
  $('#update-apply').disabled = true;
  $('#update-apply').textContent = 'Working…';
  const res = await post('/api/update/apply', {});
  const body = res.data || {};
  const box = $('#update-state');
  box.textContent = '';
  if (body.path) {
    box.appendChild(el('p', 'update-available', `${body.version} is downloaded.`));
    box.appendChild(el('p', 'meta', body.verified
      ? `Checked against the published SHA256. ${(body.bytes / 1048576).toFixed(1)}MB at ${body.path}`
      : `NOT verified — this release published no checksums. ${(body.bytes / 1048576).toFixed(1)}MB at ${body.path}`));
    setProgress({ downloading: false, staged: body.path, downloaded: body.bytes, progress: 100 });
    if (state.last) renderUpdate({ ...state.last, staged: body.path, downloading: false, progress: 100 });

    if (isDesktop) {
      // The app runs the installer and restarts. On Windows and for an
      // AppImage it comes straight back up; on macOS and a .deb the person
      // finishes, and quitting under them would look like a crash — so the
      // app only relaunches where it can.
      box.appendChild(el('p', 'update-available', 'Starting it…'));
      const said = await installUpdate(body.path);
      if (!String(said).startsWith('saved')) {
        box.textContent = '';
        box.appendChild(el('p', 'update-available', `${body.version} is installing.`));
        box.appendChild(el('p', 'meta', said));
      } else {
        box.appendChild(el('p', 'meta', 'It is on disk and ready to run.'));
      }
    } else {
      box.appendChild(el('p', 'meta', body.next));
    }
  } else {
    box.appendChild(el('p', 'update-error', body.detail || 'it did not download'));
    if (state.last) renderUpdate(state.last);
  }
}

async function dismiss() {
  state.dismissing = true;
  await post('/api/update/dismiss', {});
  state.dismissing = false;
  if (state.last) renderUpdate({ ...state.last, available: false, latest: null });
}

export function wireUpdates() {
  $('#update-check').onclick = () => check(true);
  $('#update-apply').onclick = download;
  $('#update-dismiss').onclick = dismiss;
  json('/api/update/status').then((info) => {
    if (info) renderUpdate(info);
  });
}

/* Polled while Settings is open and nowhere else.
 *
 * The reason it stops is the reason the whole feature is polite: a download
 * progress bar that updates itself in a dialog nobody is looking at is a
 * daemon doing work for nobody, and this project's rule is that a mode which
 * is left must hand back what it holds. */
export function watchUpdates(on) {
  clearInterval(state.poll);
  state.poll = null;
  if (!on) return;
  state.poll = setInterval(async () => {
    const info = await json('/api/update/status');
    if (!info) return;
    if (state.last) {
      const wasDownloading = state.last.downloading;
      renderUpdate(info);
      if (wasDownloading) setProgress(info);
    }
  }, 1200);
}

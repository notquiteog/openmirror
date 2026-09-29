/* The desktop app, when this page is inside it.

   The page is the same one a browser tab gets. The app adds only what a tab
   cannot do, and the part of that which needs the page is this: a
   notification when the agent stops to wait for you while you are looking at
   something else. The app lives in the tray, and a question asked of a hidden
   window is a question nobody sees.

   Detected by what the app provides, not by user agent — `__TAURI__` exists
   only where the app put it, and the notification API only where the app has
   granted this page the use of it. */

import { caption, companion } from './companions/index.js';

const shell = window.__TAURI__;

if (shell && shell.notification) {
  let was = companion.state;
  companion.watch((state) => {
    // The moment it starts waiting, once — not every time a state that is
    // already "waiting" is announced again.
    if (state === 'waiting' && was !== 'waiting' && !document.hasFocus()) notify(shell.notification);
    was = state;
  });
}

async function notify(api) {
  try {
    let granted = await api.isPermissionGranted();
    if (!granted) granted = (await api.requestPermission()) === 'granted';
    if (!granted) return;
    const words = caption('waiting');
    api.sendNotification({ title: 'openmirror', body: words[0].toUpperCase() + words.slice(1) });
  } catch (err) {
    // Worse than a notification, better than breaking the page over one.
    console.warn('openmirror: could not notify', err);
  }
}

/* Installing a downloaded update.
 *
 * A browser tab cannot do this and does not pretend to: the page calls this
 * only when the app is there. The app checks the path, starts the installer
 * and restarts — see `apply_update` in `desktop/src-tauri/src/main.rs` for why
 * that is the app's job and not this page's.
 *
 * Returns a string either way, because "it saved it and could not start it" is
 * a better answer than an exception, and the person still has to do the last
 * step themselves. */
export async function installUpdate(path) {
  if (!shell || !shell.invoke) return 'saved';
  try {
    return await shell.invoke('apply_update', { path });
  } catch (err) {
    console.warn('openmirror: could not start the update', err);
    return `saved, but it could not be started: ${err}`;
  }
}

/* Whether this page is inside the desktop app, which decides whether an
   update can be installed or only downloaded. */
export const isDesktop = Boolean(shell && shell.invoke);

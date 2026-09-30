/* Picking a theme.
 *
 * A theme is thirty custom properties, so this is a list and an attribute.
 * The whole of the work is in `themes.css`; this is the part that remembers
 * the choice, applies it before anything paints, and — the part that is
 * actually a feature — offers a *high contrast* mode alongside the colours.
 *
 * **Applied before the first paint**, from an inline script in the document
 * head rather than a module, because a module arrives after the stylesheet
 * and the page would flash the light theme at somebody who has chosen dark
 * for months. That is a real flash, on every load, and it is the reason the
 * reader of a theme is inline.
 *
 * **`auto` is the default** and follows the system. A theme that overrides
 * the system's own dark mode is a theme that also overrides somebody's
 * night-time setting, and those are not the same thing.
 */

import { $, el } from './dom.js';

const THEMES = [
  { id: 'auto', name: 'Automatic', hint: 'follows the system' },
  { id: 'light', name: 'Light', hint: '' },
  { id: 'dark', name: 'Dark', hint: '' },
  { id: 'night', name: 'Night', hint: 'darker, and neutral' },
  { id: 'warm', name: 'Warm', hint: 'the glass reads warm' },
  { id: 'forest', name: 'Forest', hint: 'dark, green, quiet' },
  { id: 'flat', name: 'Flat', hint: 'no glass, no tint' },
];

const STORE = 'openmirror.theme';
const CONTRAST = 'openmirror.contrast';
let highContrast = false;

function current() {
  try {
    const stored = localStorage.getItem(STORE);
    return THEMES.some((t) => t.id === stored) ? stored : 'auto';
  } catch {
    return 'auto';
  }
}

function apply(id) {
  document.documentElement.dataset.theme = id;
  document.documentElement.dataset.contrast = highContrast ? 'high' : 'normal';
  try {
    localStorage.setItem(STORE, id);
  } catch { /* a private window; the attribute is still set for this page */ }
  for (const dot of document.querySelectorAll('.theme-dot')) {
    const on = dot.dataset.theme === id;
    dot.setAttribute('aria-pressed', String(on));
  }
  const label = document.getElementById('theme-name');
  if (label) {
    const found = THEMES.find((t) => t.id === id);
    label.textContent = found ? found.name : 'Automatic';
  }
}

/* High contrast is not a colour theme. It thickens the edges and removes the
   tints, and it is here rather than in the list because the person who needs
   it needs it *on top of* whichever colour they chose. */
function setContrast(on) {
  highContrast = !!on;
  document.documentElement.dataset.contrast = highContrast ? 'high' : 'normal';
  try {
    localStorage.setItem(CONTRAST, highContrast ? 'high' : 'normal');
  } catch { /* private window */ }
  const button = document.getElementById('theme-contrast');
  if (button) {
    button.setAttribute('aria-pressed', String(highContrast));
    button.title = highContrast ? 'High contrast is on' : 'High contrast';
  }
  apply(current());
}

function render() {
  const row = document.getElementById('theme-row');
  if (!row) return;
  row.textContent = '';
  for (const theme of THEMES) {
    const dot = el('button', 'theme-dot', '');
    dot.type = 'button';
    dot.dataset.theme = theme.id;
    dot.title = theme.hint ? `${theme.name} — ${theme.hint}` : theme.name;
    dot.setAttribute('aria-label', theme.name);
    dot.onclick = () => apply(theme.id);
    row.appendChild(dot);
  }
  apply(current());
  const contrast = document.getElementById('theme-contrast');
  if (contrast) contrast.onclick = () => setContrast(!highContrast);
  setContrast(document.documentElement.dataset.contrast === 'high');
}

export function wireThemes() {
  try {
    highContrast = localStorage.getItem(CONTRAST) === 'high';
  } catch { /* private window */ }
  render();
  // And once more when the system flips, so `auto` means it.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (current() === 'auto') apply('auto');
  });
}

/* The inline snippet, which also lives in the document head.

   Two copies on purpose and one of them tested: the module cannot be the
   thing that runs first, because it arrives after the stylesheet and the
   flash is the whole problem. `test_themes.py` compares this string with
   the one in `index.html`, so they cannot drift — a stale copy in the head
   means a flash on every load, which is invisible in a screenshot and
   irritating every single time.
 */
export const EARLY = `(function(){try{
  var t=localStorage.getItem('openmirror.theme'); if(t)document.documentElement.dataset.theme=t;
  if(localStorage.getItem('openmirror.contrast')==='high')document.documentElement.dataset.contrast='high';
}catch(e){}})();`;

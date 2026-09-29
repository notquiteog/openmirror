/* The companion runner.
 *
 * A companion is a small pixel creature that lives at the bottom of the
 * transcript and reacts to what the agent is doing — reading, running a
 * command, waiting on you, failing. It is decoration, but not only: the same
 * information is in the tool cards, and a glance at the corner is cheaper
 * than reading them.
 *
 * Nothing in here knows about any particular creature. A companion is data —
 * a palette, a set of frames written as strings of characters, and a table of
 * states — and adding one is adding a file next to this one. The runner
 * cycles frames, moves the sprite around the stage, and picks which state to
 * render; everything expressive lives in the creature's own file.
 *
 * The state vocabulary is shared, so the rest of the client never learns
 * which companion is on. A creature that does not define a state falls back
 * along CHAIN, which is what lets one define three states and another
 * fifteen and both still work.
 */

/* The stage is measured in sprite pixels, not CSS ones: everything positional
   here is on the pixel grid, and the only place a real pixel appears is the
   transform set up in `resize`. */
export const STAGE = { w: 36, h: 22, scale: 4 };

/* How long a piece of work has to run before the creature starts to fidget.
   Short enough to catch a slow build, long enough that an ordinary tool call
   never triggers it. */
const GRIND_AFTER = 25;

/* What to try when a creature has not defined the state it was asked for.
   Written out per state rather than as a parent pointer so that a chain can
   never loop, which a table of fallbacks assembled at runtime very easily
   can. */
const CHAIN = {
  idle: ['idle'],
  typing: ['typing', 'idle'],
  thinking: ['thinking', 'work', 'idle'],
  reading: ['reading', 'work', 'thinking', 'idle'],
  writing: ['writing', 'work', 'thinking', 'idle'],
  running: ['running', 'work', 'thinking', 'idle'],
  browsing: ['browsing', 'work', 'thinking', 'idle'],
  desktop: ['desktop', 'browsing', 'work', 'thinking', 'idle'],
  grinding: ['grinding', 'work', 'thinking', 'idle'],
  waiting: ['waiting', 'idle'],
  listening: ['listening', 'waiting', 'idle'],
  speaking: ['speaking', 'thinking', 'idle'],
  success: ['success', 'idle'],
  error: ['error', 'idle'],
};

/* The states that mean "it is working on something", and so the ones that can
   turn into `grinding` if they go on long enough. */
const WORKING = new Set(['thinking', 'reading', 'writing', 'running', 'browsing', 'desktop']);

/* One tool at a time is running, and what it is doing is more interesting
   than the fact that a tool is running at all. Names come from the server's
   tool registry; anything unrecognised is treated as work rather than
   dropped, so a tool added later still moves the creature. */
const TOOL_STATES = {
  read_file: 'reading',
  read_files: 'reading',
  outline: 'reading',
  todo: 'thinking',
  list_dir: 'reading',
  glob: 'reading',
  grep: 'reading',
  recall: 'reading',
  write_file: 'writing',
  edit_file: 'writing',
  multi_edit: 'writing',
  apply_patch: 'writing',
  notebook_edit: 'writing',
  remember: 'writing',
  shell: 'running',
  tasks: 'reading',
  // Asking a language server, loading a skill: looking something up.
  lsp: 'reading',
  skill: 'reading',
  // While a subagent works, this one is waiting on it — which from outside
  // looks like thinking, and the subagent's own steps move the creature too.
  agent: 'thinking',
  // A plan put to the person is a question, and it waits like one.
  propose_plan: 'waiting',
  web_search: 'browsing',
  web_fetch: 'browsing',
  research: 'browsing',
  // An MCP resource is somebody else's document over a socket. That is reading
  // from where the person is sitting, whatever the wire underneath is.
  mcp_list_resources: 'reading',
  mcp_read_resource: 'reading',
  browser_navigate: 'browsing',
  browser_read: 'browsing',
  browser_click: 'browsing',
  browser_type: 'browsing',
  browser_screenshot: 'browsing',
  browser_hand_over: 'waiting',
  desktop_screenshot: 'desktop',
  desktop_click: 'desktop',
  desktop_type: 'desktop',
  desktop_key: 'desktop',
  desktop_scroll: 'desktop',
  // Generation is a long wait on somebody else's GPU, which is what
  // 'running' already means here. It gets its own word in the caption rather
  // than its own animation: a sixth state would need frames drawing in five
  // creatures, and the wait looks the same from outside either way.
  generate_image: 'running',
  generate_video: 'running',
  system_info: 'reading',
  display_info: 'reading',
  package_search: 'browsing',
  // Installing is a long wait on somebody else's servers and then a lot of
  // unpacking, which is what 'running' already means here.
  package_install: 'running',
  package_remove: 'running',
  display_hdr: 'desktop',
  media_params: 'reading',
  media_job: 'reading',
  import_workflow: 'writing',
  // A repository is a document the agent is reading, and a commit is a small
  // deliberate write. Both are the ordinary file-ish states rather than
  // something new — which is the point: the commit should feel like part of
  // the same work as the edit that caused it, not like a separate activity.
  git: 'reading',
  // Mail is somebody else's words arriving, or a reply going out. `browsing`
  // is the state that already means "paying attention to something beyond
  // this window", and for an inbox that is exactly what it is.
  mail: 'browsing',
  // A calendar is a list of other people's plans, which is the same thing
  // mail is from the creature's side: something beyond this window that it
  // is paying attention to.
  calendar: 'reading',
  // Someone's leave, and what the record says about it. Reading, like the
  // calendar: it is looking at a ledger, not changing anything.
  hr: 'reading',
  ask_user: 'waiting',
};

export function stateForTool(name) {
  return TOOL_STATES[name] || 'running';
}

/* ------------------------------------------------------------- validation */

/* Pixel art written as strings has exactly one failure mode: a row that is a
   character short, which shifts everything after it and is invisible in a
   diff. Checking costs a few hundred string comparisons at selection time and
   turns that into a named error. */
export function problems(def) {
  const found = [];
  const say = (msg) => found.push(`${def.id}: ${msg}`);

  for (const [name, rows] of Object.entries(def.frames)) {
    if (!Array.isArray(rows) || !rows.length) {
      say(`frame ${name} has no rows`);
      continue;
    }
    const width = rows[0].length;
    rows.forEach((row, y) => {
      if (row.length !== width) say(`frame ${name} row ${y} is ${row.length} wide, expected ${width}`);
      for (const ch of row) {
        if (ch !== '.' && !(ch in def.palette)) say(`frame ${name} row ${y} uses "${ch}", which is not in the palette`);
      }
    });
  }

  for (const [name, state] of Object.entries(def.states)) {
    for (const frame of state.frames || []) {
      if (!(frame in def.frames)) say(`state ${name} names frame ${frame}, which does not exist`);
    }
  }
  if (!def.states.idle) say('has no idle state');
  return found;
}

/* ---------------------------------------------------------------- painting */

/* Frames are painted once at one sprite pixel per canvas pixel and then
   scaled up with smoothing off. Filling a few hundred rectangles per frame
   would also work, but baking keeps the draw loop to one drawImage per
   sprite, which matters on a machine already running a model. */
const oven = new Map();

function bake(def, frameName, variant) {
  const key = `${def.id}/${frameName}/${variant || ''}`;
  const cached = oven.get(key);
  if (cached !== undefined) return cached;

  const rows = def.frames[frameName];
  if (!rows) {
    oven.set(key, null);
    return null;
  }
  const palette = variant && def.variants && def.variants[variant]
    ? { ...def.palette, ...def.variants[variant] }
    : def.palette;

  const canvas = document.createElement('canvas');
  canvas.width = rows[0].length;
  canvas.height = rows.length;
  const ctx = canvas.getContext('2d');
  for (let y = 0; y < rows.length; y++) {
    const row = rows[y];
    for (let x = 0; x < row.length; x++) {
      const colour = palette[row[x]];
      if (!colour) continue;               // '.' and anything unpainted
      ctx.fillStyle = colour;
      ctx.fillRect(x, y, 1, 1);
    }
  }
  oven.set(key, canvas);
  return canvas;
}

/* A still portrait for the picker: the creature as it looks when nothing is
   happening, plus whatever it is normally holding or standing in. Declared by
   the companion rather than guessed, because half of them look like nothing at
   all in their idle frame — the mandrake's is two leaves. */
export function portrait(def, scale) {
  const spec = def.portrait || { frame: def.states.idle.frames[0] };
  const base = bake(def, spec.frame, null);
  if (!base) return null;
  const canvas = document.createElement('canvas');
  canvas.width = base.width * scale;
  canvas.height = base.height * scale;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.drawImage(base, 0, 0);
  for (const [name, x, y] of spec.props || []) {
    const sprite = bake(def, name, null);
    if (sprite) ctx.drawImage(sprite, x, y);
  }
  canvas.style.width = `${canvas.width}px`;
  canvas.style.height = `${canvas.height}px`;
  return canvas;
}

/* ------------------------------------------------------------- the runner */

const reduced = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-reduced-motion: reduce)')
  : null;

/* ------------------------------------------------- the one animation frame */

/* Every companion on the page shares a single `requestAnimationFrame` loop.

   This exists because the alternative was measured, not assumed. Up to eight
   perches are mounted at once — the perch, the app mark, the empty-session
   hero, the composer thumbnail, the voice strip, and one per approval bar —
   and each used to run its own loop, so the page scheduled eight callbacks
   every frame. Worse, each loop ran at display rate no matter what the state
   asked for: a companion whose idle drift is authored at 0.6fps was still
   repainted sixty times a second, fifty-nine of them to restore the identical
   sprite. That is roughly 480 canvas paints a second for pixels that never
   changed, and it competed with the transcript for the same frame budget.

   So: one loop, and each perch is only painted when its own next frame is
   due. The loop itself stops when the set empties, so an idle page with no
   companion is not running a rAF at all. */
const live = new Set();
let frame = null;

function paintDue(now) {
  // Iterate a copy: a tick can add or drop a perch (a state change, a card
  // being torn down mid-frame), and mutating the set underneath the loop
  // would either skip a perch or visit one twice.
  for (const perch of [...live]) {
    if (!perch.def) continue;
    const at = now || performance.now();
    if (at - perch.lastPaint < perch.nextDue) continue;
    perch.tick(at);
  }
  frame = live.size ? requestAnimationFrame(paintDue) : null;
}

/* How long until this perch's next frame. From the state being drawn, so a
   slow drift costs what it says it costs. Clamped at a floor because a state
   with no `fps` means "as fast as the display", and a zero would mean
   "never". Takes the perch rather than using `this`, because it is called
   from the driver, not as a method. */
function due(perch, now) {
  if (perch.frozen()) return 400;
  const state = perch.resolve(perch.view(now).state);
  const fps = state && state.fps;
  if (!fps) return 0;
  return 1000 / fps;
}

function add(perch) {
  /* The deadline is deliberately left alone here. A fresh perch and a perch
     whose state just changed both carry a deadline of 0 — "paint on the next
     frame" — and computing a rate at this point would overwrite that with the
     interval of a state that is about to be replaced. The first `tick` sets
     the real rate from what it actually drew.

     It also matters for a brand-new perch: setting the deadline to 1.6s here
     while `lastPaint` is already now would mean it draws nothing for 1.6
     seconds, and the companion appears to be missing. */
  if (perch.lastPaint === 0) perch.lastPaint = performance.now();
  live.add(perch);
  if (frame === null) frame = requestAnimationFrame(paintDue);
}

function drop(perch) {
  live.delete(perch);
}

/* Exposed for the tests and for a companion menu that wants to show what the
   page is spending: how many frames the last second actually cost. */
export function driverState() {
  return { live: live.size, running: frame !== null };
}

export class Perch {
  /* `scale` is how many canvas pixels one sprite pixel gets. It is per-perch
     rather than global because the same creature now appears at several
     sizes at once — a hand-sized one on the perch, a thumbnail in the
     composer, a big one on an empty session — and they all animate off the
     same state. */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.scale = opts.scale || STAGE.scale;
    this.def = null;
    this.ambient = 'idle';
    this.since = performance.now();
    this.flashing = null;
    this.flashFrom = 0;
    this.flashUntil = 0;
    // How often this perch actually needs a frame, in milliseconds, and when
    // it last painted. The driver reads both to decide whether this pass is
    // due; see `due`. Taken from the state being drawn, so a creature whose
    // idle drift is 0.6fps is not repainted at display rate to show the same
    // sprite.
    this.nextDue = 0;
    this.lastPaint = 0;
    this.frozenTimer = null;
    this.resize();
    this.onResize = () => this.resize();
    this.onWake = () => this.pump();
    window.addEventListener('resize', this.onResize);
    // A creature animating in a tab nobody is looking at is pure waste, and
    // on a laptop it is waste with a battery attached.
    document.addEventListener('visibilitychange', this.onWake);
    // Turned on or off while the page is open, not only at load.
    if (reduced && reduced.addEventListener) reduced.addEventListener('change', this.onWake);
  }

  /* Take a perch down for good. Everything transient in the client — an
     approval bar, a voice strip — can carry a creature, so they have to be
     able to stop being one. */
  destroy() {
    this.def = null;
    this.unpump();
    window.removeEventListener('resize', this.onResize);
    document.removeEventListener('visibilitychange', this.onWake);
    if (reduced && reduced.removeEventListener) reduced.removeEventListener('change', this.onWake);
  }

  /* Motion is decoration, and decoration that cannot be turned off is a
     problem for anyone who gets sick from it. */
  frozen() {
    return Boolean(reduced && reduced.matches);
  }

  resize() {
    // Whole device pixels only. A fractional scale on nearest-neighbour art
    // gives some rows two pixels and others one, which reads as a wobble.
    const ratio = Math.max(1, Math.round(window.devicePixelRatio || 1));
    this.unit = this.scale * ratio;
    this.canvas.width = STAGE.w * this.unit;
    this.canvas.height = STAGE.h * this.unit;
    this.canvas.style.width = `${STAGE.w * this.scale}px`;
    this.canvas.style.height = `${STAGE.h * this.scale}px`;
    this.ctx.imageSmoothingEnabled = false;
  }

  /* Put a creature on the perch, or take the perch down with null. Returns
     the problems found in the art, if any: a broken companion is skipped
     rather than thrown, because a decoration must not be able to take the
     page with it. */
  use(def) {
    const found = def ? problems(def) : [];
    this.def = found.length ? null : def;
    this.ambient = 'idle';
    this.since = performance.now();
    this.flashing = null;
    this.clear();
    this.pump();
    return found;
  }

  /* The state the agent is in now, held until something else replaces it. */
  set(name) {
    if (!CHAIN[name] || name === this.ambient) return;
    this.ambient = name;
    this.since = performance.now();
    // A state change is information, and it is the one thing that must not be
    // paced by the old state's rate. The companion going from a 0.6fps idle
    // drift to a 14fps "running" has to repaint on the next frame, not up to
    // 1.6 seconds later — that gap is the whole point of the creature, and
    // missing it makes the companion look broken rather than calm.
    this.nextDue = 0;
    this.pump();
  }

  /* A state that plays through and hands back to whatever the agent is doing.
     Success and failure are moments, not conditions. */
  flash(name, ms) {
    if (!CHAIN[name]) return;
    const state = this.def && this.resolve(name);
    this.flashing = name;
    this.flashFrom = performance.now();
    this.flashUntil = this.flashFrom + (ms || (state && state.once) || 1200);
    // A flash has to be seen the moment it starts. Leaving the deadline
    // alone here would mean a companion sitting in a 0.6fps idle waits up to
    // 1.6s to show the failure it just had.
    this.nextDue = 0;
    this.pump();
  }

  tool(name) {
    this.set(stateForTool(name));
  }

  /* Typing is not a state the agent is in, so it expires on its own rather
     than needing a matching "stopped typing". */
  typing() {
    if (this.ambient !== 'idle') return;
    this.flash('typing', 700);
  }

  resolve(name) {
    for (const candidate of CHAIN[name] || ['idle']) {
      if (this.def.states[candidate]) return this.def.states[candidate];
    }
    return null;
  }

  clear() {
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }

  /* A frame loop while something is moving; a slow timer when nothing is,
     which exists only to notice that a flash has finished. Neither runs with
     the tab in the background.

     Every perch used to own a `requestAnimationFrame` loop of its own, which
     was two problems. There could be eight of them, and each one woke the
     compositor whether or not it had anything new to draw. Worse, the loop
     ran at display rate regardless of the state's own `fps`: an idle drift
     authored at 0.6fps was still repainted sixty times a second, fifty-nine of
     them to put back the identical sprite. The art is nearest-neighbour and
     the states are slow by design, so that was the largest steady source of
     wasted work on the page and none of it was visible.

     So the frame is shared and the rate is honoured. `add` puts a perch in
     the driver's set; the driver runs one loop, and each pass paints only the
     perches whose next frame is actually due. A perch at 0.6fps costs one
     paint every 1.6s. */
  pump() {
    const wanted = Boolean(this.def) && !document.hidden;
    const still = wanted && this.frozen();

    if (!wanted) {
      this.unpump();
      this.clear();
      return;
    }
    if (still) {
      // Held still, and the flash clock still has to run — so a timer, not a
      // frame loop. Slow on purpose: the only thing it exists to notice is a
      // flash ending.
      this.freeze();
      this.draw(performance.now());
      if (this.frozenTimer === null) {
        this.frozenTimer = setInterval(() => {
          this.lastPaint = performance.now();
          this.draw(performance.now());
        }, 400);
      }
      return;
    }
    this.thaw();
    add(this);
  }

  unpump() {
    drop(this);
    if (this.frozenTimer !== null) {
      clearInterval(this.frozenTimer);
      this.frozenTimer = null;
    }
  }

  freeze() {
    drop(this);
  }

  thaw() {
    if (this.frozenTimer !== null) {
      clearInterval(this.frozenTimer);
      this.frozenTimer = null;
    }
  }

  /* The driver's pass. Called only when this perch's next frame is due, so
     there is no reason to check the clock again in here.

     The deadline is recomputed after drawing rather than before, because the
     state that was just drawn is the one that decides how long the next frame
     should be. Recomputing it on entry would mean a perch that has just been
     woken from a slow idle back into a fast state waits out the *old*
     interval before it speeds up, which is visible as a stutter on exactly
     the frame where the agent starts working. */
  tick(now) {
    this.lastPaint = now;
    if (!this.def) return;
    this.draw(now);
    this.nextDue = due(this, now);
  }

  /* What is being rendered this instant: the flash if one is running, the
     ambient state otherwise, promoted to `grinding` when it has gone on long
     enough to be worth remarking on. */
  view(now) {
    if (this.flashing && now < this.flashUntil) {
      const span = this.flashUntil - this.flashFrom;
      return {
        state: this.flashing,
        t: (now - this.flashFrom) / 1000,
        p: Math.min(1, (now - this.flashFrom) / span),
      };
    }
    this.flashing = null;
    const t = (now - this.since) / 1000;
    const state = WORKING.has(this.ambient) && t > GRIND_AFTER ? 'grinding' : this.ambient;
    return { state, t, p: 0 };
  }

  draw(now) {
    const def = this.def;
    const ctx = this.ctx;
    const view = this.view(now);
    view.home = def.home;
    view.still = this.frozen();

    const state = this.resolve(view.state);
    if (!state) return;

    // Held still for anyone who asked not to be animated: the clock stops, so
    // nothing cycles, drifts or flaps. What the creature is *doing* still
    // changes with the agent, because that part is information rather than
    // decoration, and a frozen frame carries it just as well.
    if (view.still) {
      view.t = 0;
      view.p = 0;
    }
    const at = state.motion && !view.still ? state.motion(view) : {};
    const pos = {
      x: at.x === undefined ? def.home.x : at.x,
      y: at.y === undefined ? def.home.y : at.y,
      flip: Boolean(at.flip),
    };

    ctx.setTransform(this.unit, 0, 0, this.unit, 0, 0);
    ctx.clearRect(0, 0, STAGE.w, STAGE.h);

    for (const prop of def.props || []) {
      if (!prop.front) this.paintProp(prop, view, pos, state.variant);
    }

    const name = state.pick
      ? state.pick(view)
      : state.frames[Math.floor(view.t * (state.fps || 4)) % state.frames.length];
    if (name) this.paint(name, pos.x, pos.y, pos.flip, state.variant);

    for (const prop of def.props || []) {
      if (prop.front) this.paintProp(prop, view, pos, state.variant);
    }
  }

  /* Props take the colour the creature is currently wearing unless they pin
     one of their own: a screen that stays green while the case flashes red
     would read as two separate things. */
  paintProp(prop, view, pos, variant) {
    const name = prop.pick(view);
    if (!name) return;
    const x = prop.attach ? pos.x + prop.x : prop.x;
    const y = prop.attach ? pos.y + prop.y : prop.y;
    this.paint(name, x, y, false, prop.variant || variant);
  }

  paint(name, x, y, flip, variant) {
    const sprite = bake(this.def, name, variant);
    if (!sprite) return;
    // Snapped to whole device pixels: motion that lands between them turns
    // crisp art into a smear, and the grid is the whole point of the style.
    const px = Math.round(x * this.unit) / this.unit;
    const py = Math.round(y * this.unit) / this.unit;
    const ctx = this.ctx;
    if (flip) {
      ctx.save();
      ctx.translate(px + sprite.width, py);
      ctx.scale(-1, 1);
      ctx.drawImage(sprite, 0, 0);
      ctx.restore();
    } else {
      ctx.drawImage(sprite, px, py);
    }
  }
}

# openmirror for VS Code

A panel for a running [openmirror](https://github.com/notquiteog/openmirror)
daemon. It is the same client as `openmirror chat`, pointed at the same
localhost, and it is a client in the same sense: **it starts no daemon, spawns
no agent, and holds nothing that outlives the panel.**

You need the daemon already running:

```bash
openmirror serve
```

The panel will not start it for you. `openmirror: Start the Daemon in a Terminal`
opens a terminal you can see, watch and Ctrl-C, and waits a few seconds for the
daemon to answer so the panel picks it up on its own -- which is a button you
pressed, not a process started behind your back. Starting something on this
machine without being asked is the one thing this extension will not do.

---

## What is in the box

- A panel beside the editor, with the transcript, the approval mode, the model,
  the thinking level and a context meter.
- Approve and deny on every tool call, with the risk grade on it.
- `/` commands and skills from the daemon, `@` file mentions, pasted images.
- Resume a stored conversation, or fork the current one.
- The six approval modes, as a picker, with the same descriptions the modes are
  documented with.

What is **not** in the box, and will not be pretending to be: voice, autopilot,
image and video generation, the desktop tools, MCP servers, LSP features,
memory search, mail. Those are in the daemon and in the web app, not in this
panel. The panel renders the agent's protocol and nothing else; if you want the
rest, the browser UI on the same port has it.

There is also no per-session cost figure. A price table goes stale the moment a
provider changes one, and a number that is out of date is worse than no number.
The token counts are exact.

---

## No dependencies, and why

**This extension has no `dependencies` and no `devDependencies`. There is no
`node_modules` in the .vsix.** Not a boast about minimalism: it is why
`src/ws.js` exists.

Node only grew a global `WebSocket` in v21, and the extension host's Node
version is whatever the editor shipped, which is not ours to pick. So the
WebSocket client is written against `net`/`tls`, in one file, against the
browser `WebSocket` API and nothing else. A single `dependencies` block with
anything in it would defeat the file, and this one is the sort of thing that is
easy to add without noticing and hard to notice afterwards.

The same logic sets `engines.vscode` to `^1.87.0`. That is not a webview API
floor -- everything used here is older than that -- it is the release where the
extension host moved to Node 18, and `src/host.js` uses `fetch` and
`AbortController`. A polyfill would be a dependency.

---

## The token never gets near the page

`PROTOCOL.md` says it and it is the rule the whole design turns on:

> The extension host owns the socket. The webview never sees the daemon's
> token, its URL, or its socket.

A webview is a browser context VS Code renders. Anything put in it is something
a webview bug, an injected script or a screenshot can read. The daemon's token
runs commands on your machine, so `src/host.js` holds it, checks every frame on
its way to `webview.postMessage`, and refuses one that carries it.

Three things follow, and they are not negotiable:

- `localResourceRoots` is `media/` and **only** `media/`. Not the extension
  root, not the workspace. A webview that can load a `file:` URL out of your
  repository is a webview that can be made to.
- The page's policy has `connect-src 'none'`. The panel reaches the daemon
  through the extension host or not at all.
- `openmirror.token` is a plain string with no default, because a default token
  is a shipped credential, and VS Code has no password field for a setting. If
  your daemon was started with `OPENMIRROR_TOKEN`, put it in your environment or
  your secret store -- not in `settings.json`, which is a file.

The workspace is declared unsupported when VS Code does not trust it, because
one of these commands runs the `openmirror` binary and in an untrusted folder
that binary may be the repository's own. Virtual workspaces are unsupported too:
there is no working root, and one root the daemon cannot leave is the entire of
what confines an agent.

---

## Settings

Every one of them falls back to an environment variable, and every one of them
is empty by default. That is deliberate. An empty default and an unset default
resolve identically, so `OPENMIRROR_HOST` still wins when you have not set
`openmirror.host` yourself -- a non-empty default would shadow the environment
for ever, which is the sort of thing nobody notices until the variable stops
working. The defaults in `settings.json` are all empty; the values you see are
what `resolveDaemon` turns them into when the environment is empty too.

| setting | in settings.json | resolves to | falls back to |
|---|---|---|---|
| `openmirror.host` | empty | `127.0.0.1` | `OPENMIRROR_HOST` |
| `openmirror.port` | empty | `8477` | `OPENMIRROR_PORT` |
| `openmirror.token` | absent | none | `OPENMIRROR_TOKEN` |
| `openmirror.model` | empty | the daemon decides | -- |
| `openmirror.provider` | empty | the daemon decides | -- |
| `openmirror.mode` | empty | the daemon decides (`ask`) | -- |
| `openmirror.effort` | empty | the daemon decides | -- |
| `openmirror.root` | empty | the first workspace folder | -- |

`host`, `port` and `token` decide **which daemon the panel is talking to**. A
`Host` reads them once, when it is built, so changing one tells the panel
plainly that it is still attached to the daemon it opened and that reopening it
picks up the change. The rest are defaults for the *next* conversation, and say
so rather than moving a running one.

---

## Commands

All under the `openmirror` category.

| command | what it does |
|---|---|
| `Open Panel` | the panel, beside the editor; from the explorer it takes that folder as the working root |
| `New Conversation` | asks the daemon for a new one |
| `Resume Conversation` | a picker over the daemon's stored conversations, showing each one's folder and turn count |
| `Fork Current Conversation` | a copy that shares the files; the original is untouched |
| `Show the Context Report` | how full the context window is, and whether that number is a guess |
| `Set the Approval Mode` | the six modes, with the risk each one lets run |
| `Set the Thinking Level` | `off` ... `max` |
| `Set the Model` | `/model`, which is a turn command on the socket rather than a message of its own |
| `Stop the Turn` | queued rather than dropped, so a turn cannot run to its end |
| `Agree to a Project Hook` | answers the banner a project hook raises |
| `Refuse a Project Hook` | the same question, the other answer |
| `Start the Daemon in a Terminal` | `openmirror serve`, in a terminal you can see |
| `Show Output` | this extension's log, and the webview's own |

Two of them are in menus rather than only in the palette: the panel button in
the editor title bar, and the same command on a folder in the explorer.

---

## Activation

`onStartupFinished`. Not `*`, and not a list of `onCommand:` entries.

- VS Code has generated `onCommand:` from `contributes.commands` itself since
  1.74, so listing them is trivia that can only go stale.
- `*` would load a module that opens an output channel and reads settings at
  start-up in every window, including the ones that will never open the panel.
  The extension holds no daemon link until somebody opens a panel, so there is
  nothing to warm up.
- `onStartupFinished` is ready before anybody can reach the palette, and costs
  nothing for a window that never uses it.

---

## Building and testing

There is no build step and nothing to install.

```bash
node --check src/extension.js
node --test test/
npm run vscode:prepublish    # node --check on all three source files
```

`node --test test/` runs without an editor: `require('vscode')` only resolves
inside the extension host, so `src/extension.js` takes the `vscode` object as a
second argument to `activate()` and the tests hand it a fake. The daemon, the
socket and the clock are faked the same way. The assertions that matter are the
ones that hold the shape: that the manifest's commands and the registry's are
the same set of names, that the settings defaults do not shadow the environment,
and that the panel's resources are `media/` and nothing else.

## Licence

Apache-2.0, the same as the rest of openmirror. This extension is original to
openmirror and contains no Open WebUI code.

openmirror as a project includes software developed by Open WebUI Inc.
(<https://github.com/open-webui/open-webui>), created by Timothy Jaeryang Baek,
Copyright (c) 2023- Open WebUI Inc. All rights reserved. Portions of openmirror
are derived from Open WebUI and remain subject to the Open WebUI License,
reproduced in `LICENSE-OpenWebUI` at the repository root. See `NOTICE` there for
which files those are.
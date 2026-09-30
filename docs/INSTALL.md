# Installing

openmirror is a daemon plus a client. The daemon runs on the machine you want the
agent to control; the client is a page it serves. That shape is why there is
no domain, no certificate and no account anywhere in this document.

## No domain is needed. No certificate either.

`http://127.0.0.1` **is a secure context** as far as every browser is
concerned — localhost is specifically exempted from the HTTPS requirement. So
the microphone, `AudioWorklet` and everything else the voice pipeline needs
work on plain HTTP, with no certificate at all.

Measured on this machine:

| origin | secure context | `navigator.mediaDevices` |
|---|---|---|
| `http://127.0.0.1:8477` | yes | available |
| `http://192.168.1.x:8477` | **no** | **undefined** |

So the only case that needs anything extra is reaching openmirror from *another*
machine — and the answer there is not a domain either.

## Reaching it from your phone or laptop

Forward the port over SSH:

```bash
ssh -N -L 8477:127.0.0.1:8477 you@your-desktop
```

The far end is now `http://127.0.0.1:8477` on the machine you are sitting at —
which is a secure context, so voice works, with no certificate, no domain and
nothing published to the internet. This is the same trick Perch uses to reach
a GPU box, in the same direction.

For a phone, any Tailscale-style overlay does the same job. `mkcert` is a
third option, and the only one that needs a local CA in your trust store —
which is a lot to ask of an installer, so it is not the default.

## The desktop app

On a machine you sit at, this is the way in. Every
[release](https://github.com/notquiteog/openmirror/releases) has an installer
for Windows (`.exe`, `.msi`), macOS (`.dmg`, for Apple Silicon or Intel) and
Linux (`.deb`, `.rpm`, `.AppImage`), and each one is the whole of openmirror:
the window, and the daemon it runs, with its own Python inside. Neither the
Windows nor the macOS build is signed yet, and the release notes say what to
click the first time.

A daemon the app starts keeps everything in `~/.openmirror` — its data, its
logs and an optional `.env` — and has your home directory as its workspace, as
it would when started from a new terminal. It carries the `[desktop]`, `[tor]`
and `[vec]` extras below, but not `[browser]` yet: Playwright brings a Node
runtime of its own and downloads its own Chromium, a step the app cannot take
for you. For now the browser tools want a daemon installed as below, with
`[browser]`; start it before the app and the app will use it.

How the app finds, starts and stops its daemon is in
[desktop/README.md](../desktop/README.md).

## The VS Code extension

Every release also carries `openmirror.vsix` — the agent in a panel beside your
editor, with streaming output, approvals, `/commands`, `@file` mentions and
pasted images.

It is a **client**, like `openmirror chat` and like the browser: it starts no
daemon, spawns no agent, and keeps nothing once you close the panel. So it
needs one running — the desktop app, or `openmirror serve` in a terminal — and
if there is not one it says so and names the command, rather than quietly
starting one where you cannot see it.

```bash
code --install-extension openmirror.vsix
```

Settings live under `openmirror.` — `host`, `port`, `token`, `model`,
`provider`, `mode`, `effort`, `root` — and each falls back to its
`OPENMIRROR_*` environment variable, so an install that already works from a
terminal needs no configuration here. The token is only needed when the daemon
was started with `OPENMIRROR_TOKEN`.

The extension has **no dependencies and no build step**, which is why
`src/ws.js` exists: Node only grew a global `WebSocket` in v21, and the
extension host runs whatever Node your editor shipped. The whole thing is
tested with `node --test` and needs no `npm install` to build, check or
package. See [ide/vscode/README.md](../ide/vscode/README.md).

## Installing the daemon

For a machine with no screen, or for what the app does not carry. Python
3.11+, on Linux, macOS or Windows:

```bash
uv tool install openmirror
```

or `pipx install openmirror`. Then:

```bash
openmirror
```

and open <http://127.0.0.1:8477>.

### The extras

None of these are needed for an install to be complete — a daemon with files,
shell, web and memory is a working agent. Each is an extra rather than a
dependency so an install that never uses it does not carry the weight:

```bash
uv tool install 'openmirror[browser]'   # Playwright — a real Chromium
uv tool install 'openmirror[desktop]'   # seeing and driving a screen
uv tool install 'openmirror[tor]'       # routing a connection through a SOCKS proxy
uv tool install 'openmirror[vec]'      # a C scan over memory, instead of a Python one
```

After installing `[browser]`, fetch the browser itself, once:

```bash
playwright install chromium
```

`[vec]` is worth taking if you use memory for more than a few thousand things.
It is `sqlite-vec`, a loadable SQLite extension, so it runs in the same process
with no service to manage — measured at fifty thousand memories, 12.9 ms
against 107.6 ms for the Python scan's arithmetic alone, before that scan also
pays to hand back every row. openmirror works without it and says nothing: search
falls back to the scan, which returns the same memories in the same order.

It is an extra rather than a dependency because loading it needs
`enable_load_extension`, which is a compile-time Python option and not
universal. An interpreter without it logs one line at start-up and uses the
scan.

A screen of the agent's **own** additionally needs `Xvfb` from your system
package manager — `apt install xvfb`. Without it, desktop control falls back to
the screen you are sitting in front of and says so in the log: an agent that
can see nothing is not a safer agent, it is a useless one.

If you are working from a clone rather than a release, the same extras apply to
an editable install — `pip install -e '.[browser]'`, and `'.[dev]'` for the
test suite.

## Which model, and what the box needs

### The floor

**Chat: `qwen3.5:9b` or `gemma4:12b`. Embedding: `Qwen3-Embedding-4B`.**

What openmirror's features are built and tested against. Below it the agent loop
does not slow down, it gets unreliable in ways that look like the harness
misbehaving — prose where a tool call was needed, a plan the model has already
lost. Nothing refuses a smaller model; the floor is what a **feature** may
assume, not what an **operator** may run.

**At the floor, narrow the tools.** `gemma4:12b` did a five-step browser task
in twenty-six seconds first try with about ten tools, and failed the same task
outright with the full set of thirty-odd. That is a setting, not a rewrite: pass a `tools` list
when you create the session. It is the single most useful thing to do on a
small model, and it matters more than the last gigabyte of model size.

[docs/PROVIDERS.md](PROVIDERS.md) has the detail, the honest limits of the
evidence, and the frontier-model end of it.

### Where the model runs

**The model may be remote; the agent is not.** This daemon runs shell, edits
files and drives a browser on the machine you install it on, whichever machine
the tokens come from. Install it where you would be willing to run an agent.

*Models elsewhere* — a provider API, or a GPU box on the LAN such as Perch — is
the common case, costs this machine nothing for inference, and has no floor at
all if the provider is a frontier model. Set `OPENMIRROR_LOCAL_ONLY` to forbid
hosted providers outright; it is enforced when a route resolves, so a local
backend being down is an error rather than a quiet fallback to somebody's API.

*Everything on one box* wants **16 GB or more**: a floor chat model is about
6 GB resident, `Qwen3-Embedding-4B` about 2.5 GB, whisper `base` plus Kokoro
about 1.9 GB, and the daemon and its store about 1 GB — roughly 11.5 GB with
everything warm. That has headroom on 16 GB and none at all on 8. Voice is the
first thing to drop; `[browser]` and `[desktop]` are the next, which is part of
why they are extras.

One thing to settle **before** indexing anything: changing the embedding model
later does not degrade memory, it makes existing memories unfindable, because
vectors of different widths are not comparable. See
[docs/PROVIDERS.md](PROVIDERS.md).

## Platform notes

The shell tool is the only part that has to care which system it is on, and
it is written for all three:

* Commands run in their own process group — `start_new_session` on POSIX,
  `CREATE_NEW_PROCESS_GROUP` on Windows — so a timeout or an interrupt reaches
  the command's *children* and not just the shell. Killing only the shell
  leaves an orphaned dev server holding its port.
* Killing uses `killpg` on POSIX and `taskkill /F /T` on Windows, because
  Windows has no process groups in the POSIX sense and no `SIGTERM`.
* The risk classifier knows `cmd.exe` and PowerShell as well as POSIX shells —
  `Remove-Item -Recurse -Force`, `del /f /s /q`, `format C:`, `vssadmin delete
  shadows` are all graded destructive. Both families are matched on every
  platform, because Git Bash and WSL put `bash` on Windows and PowerShell runs
  on Linux and macOS.
* PowerShell cmdlet names are matched case-insensitively; POSIX command names
  are not, because lower-casing `LS` into `ls` would grade an unknown binary
  as a known-safe read.

## Running it as a service

Nothing here survives a reboot yet. An autonomous agent probably should, so
this is the next thing worth adding: a systemd user unit on Linux, a
`launchd` plist on macOS, and a scheduled task or service on Windows.

## What the app adds, and what it cannot

The app wraps the *same* page a browser tab gets, so it is additive rather
than a fork in the road: a tray icon, a Dock or taskbar entry, a notification
when the agent is waiting on you, and a daemon that starts and stops with it.

Voice goes through each platform's own webview, and each asks about the
microphone differently. On macOS the system asks, once. On Windows, WebView2
asks with a prompt of its own. On Linux nobody would have: WebKitGTK refuses
any permission request that goes unanswered, so voice heard nothing and said
nothing. The app answers it — for the daemon's page and nothing else. Measured
with WebKit's mock devices, `getUserMedia` was refused until it did, and
granted after.

One thing it cannot add everywhere is a global push-to-talk hotkey. On Wayland
that needs the `org.freedesktop.portal.GlobalShortcuts` portal, and on the
COSMIC session this was developed on that portal is not present — so no
framework, native or otherwise, can offer it there today.

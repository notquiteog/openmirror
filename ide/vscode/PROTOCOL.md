# The extension's internal protocol

This is the contract between the three parts of the VS Code extension. Read it
before touching any of them; it is the only thing they share.

```
  ┌──────────────┐   postMessage   ┌──────────────┐   WebSocket   ┌─────────┐
  │   media/     │ ◄─────────────► │  src/host.js │ ◄────────────► │ daemon  │
  │  (webview)   │   t: 'frame'    │ (extension   │   agent event  │         │
  │              │                 │  host)       │   JSON frames  │         │
  └──────────────┘                 └──────────────┘                └─────────┘
                                         │
                                         │ fetch, for the REST surface
                                         └────────────────────────────►
```

## The rule that decides the whole design

**The extension host owns the socket. The webview never sees the daemon's
token, its URL, or its socket.**

The webview is a browser context that VS Code renders. Anything put in it is
something a webview bug, an injected script or a screenshot can read. The
daemon's token is a credential that runs commands on the developer's machine,
and this project refuses to phone home, confines every write to one root, and
grades each tool call by what it would actually do. Passing its token into a
webview to save writing a proxy would be the one thing in the extension that
breaks all of that.

So the host proxies: it holds the `WebSocket`, it holds the token, and the
webview sends it intents. The cost is one relay, and the cost of not doing it
is a credential in a page.

This is also why the extension needs a WebSocket client. Node only grew a
global `WebSocket` in v21, and the extension host's Node version is whatever
the editor shipped, which is not ours to pick — so `src/ws.js` is written
against `net`/`tls` with no dependencies.

## Frames: webview → host

All are `{t: '<name>', ...}`. Every one is optional-safe: an unknown `t` is
ignored and logged, never thrown, because a stale webview paired with a new
host is a version skew somebody will hit and it must not break the panel.

| `t` | fields | what it means |
|---|---|---|
| `ready` | — | the webview has loaded and rendered; the host may send config now |
| `submit` | `text` (string), `attachments` (array) | start a turn. `attachments` is `[{type:'image', data, media_type}]`, base64, no data-URL prefix |
| `approve` | `callId` (string), `remember` (bool) | allow a tool call |
| `deny` | `callId` (string), `reason` (string) | refuse one |
| `answer` | `questionId` (string), `answer` (string) | answer an `ask_user` question |
| `interrupt` | — | stop the running turn |
| `policy` | `mode` (string) | change the approval mode |
| `effort` | `level` (string) | change the thinking level |
| `model` | `name` (string) | change the model |
| `files` | `query` (string) | ask for `@`-mention candidates |
| `reconnect` | — | try the daemon again now. The panel's way back from a `failed` status, which is otherwise terminal: the host stops retrying, so without this the only recovery is closing the tab |
| `hook` | `command` (string), `allow` (bool) | answer a `hook.approval`. The strip in the panel shows the command; answering it from the page is one click, and a banner whose only response is a palette command is a banner people learn to ignore |
| `new` | — | start a new conversation |
| `open` | `id` (string) | resume a stored conversation |
| `fork` | — | fork the current one |
| `context` | — | ask for the context report |
| `contexts` | — | list the stored conversations, for a picker |
| `log` | `level` (string), `message` (string) | the webview's own log, forwarded to the extension's output channel |

## Frames: host → webview

| `t` | fields | what it means |
|---|---|---|
| `config` | `sessionId`, `model`, `mode`, `title`, `root`, `provider`, `effort` | what the panel is looking at, sent once on `ready` |
| `event` | `event` (object) | **a daemon event, forwarded verbatim.** Never reshaped, never renamed, never filtered. The webview renders the daemon's protocol, and the one place that translation could live is here |
| `commands` | `items` (array) | `[{name, description, kind, hint}]` for the `/` menu |
| `files` | `items` (array) | `[{path, size, mtime}]` for the `@` menu |
| `context` | `tokens`, `limit`, `window`, `totalIn`, `totalOut`, `exact` | the context report |
| `sessions` | `items` (array) | `[{id, title, root, updated, turns}]` for the resume picker |
| `status` | `state` (`connecting` \| `open` \| `closed` \| `failed`), `message` (string) | the link to the daemon |
| `notice` | `text` (string), `kind` (string) | one line for the person |

`commands` and `context` need no frame to ask for: the host fetches `commands`
whenever the session changes — on `ready`, and after `new`, `open` and `fork` —
and `context` on `turn.completed`. Both are answers to something that just
happened rather than to a question, so requiring the page to ask would mean
asking for them at every point where they change, which is four places to get
right instead of one. `files` and `sessions` *are* answers to a question, and
the page asks.

## Why the daemon's events cross the bridge untouched

`openmirror/protocol/agent.py` is a discriminated union, and `openmirror/static/app.js`
already renders all of it. The extension renders the same events. If the host
translated them, there would be three renderers of one protocol and the web one
would be the only one that got updated when an event was added — which is
exactly the failure that makes a tool rot.

So: forward the JSON, and let `media/` hold the renderer. A new daemon event
shows up in the panel as one line saying it is an unknown event rather than
silently vanishing, and that is the correct behaviour.

## Where the daemon's auth goes

`Authorization: Bearer <token>` and `X-Openmirror-Token: <token>` on every
request, and `?token=<token>` on the WebSocket upgrade — the three places the
daemon accepts it, and the same three `openmirror/tui.py` uses. The token is
read from the setting `openmirror.token`, which falls back to
`OPENMIRROR_TOKEN`. It is never logged, never put in a frame, and never sent
to the webview.

## What the extension is not

It is a **client**. It starts no daemon, spawns no agent, and holds no state
that outlives the panel. The daemon stays the only thing holding sessions,
providers and tools — the same line `openmirror chat` draws, for the same
reasons. If the daemon is not running, the panel says so and offers the
command that starts it; it does not quietly start one behind your back.

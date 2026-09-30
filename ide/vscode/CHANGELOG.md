# Changelog

Versions follow the daemon's, because a panel that says 0.6.1 against a 0.6.0
daemon is a support question nobody should have to ask.

## 0.6.0

First release of the VS Code extension.

The panel is a client and nothing else. It starts no daemon, spawns no agent,
and keeps no state that outlives the panel; the daemon stays the only thing
holding sessions, providers and tools, the same line `openmirror chat` draws for
the same reasons. If the daemon is not running the panel says so and names the
command that starts it.

- A panel beside the editor, streaming, with approvals carrying the risk grade.
- `/` commands and `@` file mentions come from the daemon, so the menu is the
  session's and not a guess.
- Resume a stored conversation, or fork the current one. The fork leaves the
  original alone and shares the files.
- The six approval modes and the seven thinking levels, as pickers.
- A context meter that only appears when there is a denominator to draw it
  against.
- Commands to agree to or refuse a project hook, which is the one daemon event
  with no frame to answer it and so is reachable only from the extension host.
- `openmirror.host`, `.port` and `.token` fall back to `OPENMIRROR_HOST`,
  `.PORT` and `.TOKEN`, and are empty by default so the environment is not
  shadowed by a value nobody chose.
- `openmirror: Start the Daemon in a Terminal` opens a terminal running
  `openmirror serve` and waits for it. Offered, never done automatically.

## Security notes for this version

- The daemon's token is held by the extension host and checked on every frame
  on its way to the webview. It is never put in the page, never logged, and
  `openmirror.token` has no default.
- `localResourceRoots` is `media/` and only `media/`. Not the extension root,
  not the workspace.
- The panel's own content security policy has `connect-src 'none'`: the page
  reaches the daemon through the extension host or not at all.
- `enableCommandUris` is off, so the page cannot run a command in the
  extension.
- The extension declares itself unsupported in untrusted workspaces, because
  one of its commands runs the `openmirror` binary and in an untrusted folder
  that binary may be the repository's own.
- No `dependencies` and no `devDependencies`. The WebSocket client is written
  against `net`/`tls` because the extension host's Node version is not ours to
  pick.

## Not in this release, and not planned to pretend otherwise

Voice, autopilot, image and video generation, the desktop tools, MCP, LSP
features, memory search and mail are the daemon's and the web app's. The panel
renders the agent's protocol; it does not reimplement it, and there is no
"coming soon" on any of it.
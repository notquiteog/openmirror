# Extending it

Two of the mechanisms here are MCP pointing in opposite directions: openmirror
using everybody else's tools, and everybody else's clients using openmirror.
Three more are files — skills, agent definitions and language-server settings
— each in the shape other clients already write, so what you have made for one
of them works here unchanged. And one thing undoes what the agent does.

## Using MCP servers

openmirror is an MCP client, so any MCP server becomes tools the agent can use.
Servers are read from a `.mcp.json` in the shape the rest of the ecosystem
already uses, which means a file written for another client works here
unchanged:

```json
{
  "mcpServers": {
    "files":  { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv"] },
    "github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": { "GITHUB_TOKEN": "..." } },
    "db":     { "command": "uvx", "args": ["mcp-db"], "trustHints": true },

    "hosted": { "type": "http", "url": "https://example.com/mcp",
                "headers": { "Authorization": "Bearer ..." } },
    "legacy": { "url": "https://example.com/sse" }
  }
}
```

Tools arrive as `mcp__<server>__<tool>`.

### Local and remote

A server is a `command` to run or a `url` to reach. Remote ones speak one of
two HTTP transports: the current streamable one (`"type": "http"`) and the
older SSE pair (`"type": "sse"`). A URL ending in `/sse` is assumed to be the
older one, since that is the path every implementation of it uses; say `type`
explicitly to override the guess. `headers` are sent with every request, which
is where a hosted server's token goes — per server, because two servers needing
two different tokens is the normal case.

A remote server is the case where *not* running somebody else's code on your
machine is the point. It is also the case where the untrusted-server floor
below matters most: a hosted tool can change what it does between one call and
the next, and nothing local will have noticed.

### Resources, not just tools

A server can also expose **resources** — documents, records, pages, addressed
by URI — and **prompts**. Resources arrive as a fixed pair of tools,
`mcp_list_resources` and `mcp_read_resource`, rather than one tool each: a
server with four hundred documents would otherwise put four hundred entries in
the model's tool list, which is the failure `TOOLSETS` exists to prevent
arriving from outside the project.

The pair only appears when a connected server actually declares resources. A
model shown `mcp_read_resource` by an install that has none will try it, be
told there is nothing, and have spent a turn learning what the tool list could
have told it by not being there.

Reading a resource is graded `network` rather than `read` — the same as
`web_fetch`, and for the same reason. What comes back is somebody else's
document, fetched from somewhere else again, and the risk is what arrives
rather than what it does.

### Why `trustHints` exists and defaults to false

MCP lets a tool declare `readOnlyHint: true`. Taking that at face value would
let any server exempt itself from approval — including one installed by pasting
a command off a web page, which is how most of them get installed. So hints are
ignored unless you mark that server trusted, and until then **every MCP tool is
at least `execute`**.

The distrust is one-directional. A `destructiveHint` is believed from any
server: volunteering that something is dangerous is not a claim worth doubting.

Names and descriptions are graded regardless of hints, so a tool called
`create_order` reads as a purchase whatever it says about itself. That check
runs over text the server controls, which sounds like a weakness and is not —
it can only *raise* the risk, so a hostile description makes a tool harder to
run, never easier.

A server's descriptions and its results both go into the model's context, so
both are labelled as coming from that server and as data rather than
instructions. That is a mitigation, not a fix; see the note on prompt
injection in [AUTONOMY.md](AUTONOMY.md).

### Lifetime

Each server is a child process. One failing to start does not stop the others —
an agent with four working tool servers and one broken one is far more useful
than one that refused to start — and all of them are reaped when the daemon
stops.

## openmirror as an MCP server

The other direction: your editor, a desktop assistant or another agent using
*this* twin — its memory, its provider routing, its search backend. Without it,
the memory openmirror has been accumulating is reachable only from openmirror's own
UI.

Two transports. On stdio, for a client that would rather spawn it:

```json
{
  "mcpServers": {
    "openmirror": { "command": "openmirror-mcp" }
  }
}
```

Or over HTTP, from the daemon that is already running:

```
OPENMIRROR_MCP_SERVE=true
OPENMIRROR_MCP_SERVE_TOKEN=a-long-random-string
OPENMIRROR_MCP_SERVE_SCOPE=read
```

which serves `POST /mcp` (streamable HTTP) and `GET /mcp/sse` + `POST
/mcp/messages` (the older transport). Both are off by default.

### Scope

One setting with three values, because the decision a person actually makes is
how much of themselves to lend out, not eleven tool-shaped decisions.

| Scope | What it adds |
|---|---|
| `read` | `recall`, `web_search`, `web_fetch`, `research`, and the resources |
| `write` | `remember` |
| `all` | `generate_image` — **this spends money** |

An unrecognised scope is `read`, and says so in the log. A typo in a config
file must not be a way to widen what is exposed.

**Nothing that acts on the machine is exposed at any scope.** No shell, no
files, no desktop, no browser driving. Those are the tools openmirror guards with
an approval prompt in front of a human, and an MCP call has no human in front
of it — exposing them here would route around the one control that matters. The
approval policy is not reimplemented on the server side; the dangerous tools
are simply not there, and a test asserts it.

### The token is not optional on a network interface

This endpoint can hand out a person's memory, so on any bind that is not
loopback the router refuses to mount without `OPENMIRROR_MCP_SERVE_TOKEN`. That
is stricter than the rest of the server, which warns and carries on. The
asymmetry is deliberate: an open agent endpoint is a machine somebody else can
use, and an open twin endpoint is a person somebody else can read.

Resources it offers: `openmirror://capabilities` (what this install can do, and
what it deliberately does not expose), `openmirror://providers`,
`openmirror://memory/recent`, and the templates `openmirror://memory/search/{query}`
and `openmirror://media/{id}`.

## Skills

A skill is a folder with a `SKILL.md` in it, and whatever else the job needs
beside it:

```
.openmirror/skills/release/
  SKILL.md
  checklist.md
```

```markdown
---
name: release
description: Cut a release — bump the version, write the changelog, tag it.
argument-hint: "[version]"
---

Bump the version to $ARGUMENTS, then work through checklist.md …
```

Only `name` and `description` are in front of the model all the time, as one
line each in the `skill` tool's description. The body is read when a request
matches, and a file beside it only when the body points at it — through the
tool's `file` argument, which reads inside that skill's folder and nowhere
else. That ordering is the point: twenty skills cost twenty lines, not twenty
documents, which matters most on the small models this project targets.

They are looked for in these places, and a later one wins a name:

| where | whose |
|---|---|
| `openmirror/skills/` | shipped: `init` and `review` |
| `~/.claude/skills/`, `~/.openmirror/skills/` | yours, in every project |
| `<root>/.claude/skills/`, `<root>/.openmirror/skills/` | the project's |

A flat `commands/<name>.md` beside any of those is a skill too — the older
shape of the same idea. `/name` in the composer runs one, with whatever follows
it put where `$ARGUMENTS` is (or after the body, if it has no `$ARGUMENTS`), and
`/` alone lists them. `disable-model-invocation: true` keeps a skill out of the
model's list, for one that should only ever run because a person asked for it.

A project's skills are the project's own text, which means somebody else may
have written them. They are treated like AGENTS.md — instructions about how to
work here, read by a model still bound by everything else — and they cannot
widen what the approval policy allows, because nothing in them reaches it.

## Agents

`agent` starts a subagent: a session with a fresh context, a narrower tool list
and instructions of its own, which does one task and reports back. Three kinds
are built in:

| kind | what it may do |
|---|---|
| `explore` | read and search. Capped at `read_only` whatever mode the session is in. |
| `general` | everything the session can, less what no subagent gets |
| `research` | the web tools, plus reading files. Only offered where the web tools are. |

More are markdown files, one agent to a file, in `.openmirror/agents/` or
`.claude/agents/` in the project, or the same under your home directory, the
most local winning a name:

```markdown
---
name: reviewer
description: Reviews a change for bugs and reports them with file and line.
tools: Read, Grep, Glob, Bash
mode: read_only
---

You review code. …
```

`tools` narrows it, and other clients' names are translated — `Read` is
`read_file`, `Bash` is `shell`, `Bash(git diff:*)` is `shell` too. `model` names
a model this provider has; a vendor's alias such as `sonnet` or `inherit` means
the session's own, since passing it on would be an error from every provider,
that vendor's included. `mode: read_only` caps it the way `explore` is capped.

What no subagent gets, whatever its file says, is decided in code: agents of
its own, the to-do list, a question to the person, background work, and the
session's hands — its one browser and its one screen. Everything it does is in
the session's event log, tagged with the call that started it (`agent` on each
event), and everything it wants approved is asked of you there, under the
session's policy. Several `agent` calls in one response run at once, each on
the real tree; the read-before-write rule is what keeps two of them from
editing the same file blind.

`background: true` starts one and returns at once. It shows in the strip above
the composer, `tasks` reads its report, and the model is handed the report in
its next request when it finishes.

## Language servers

`lsp` is offered where a language server is installed, and only there. Without
configuration, the first of these found on PATH is used for its language:

| language | servers, in order of preference |
|---|---|
| Python | `basedpyright-langserver`, `pyright-langserver`, `pylsp`, `jedi-language-server` |
| TypeScript, JavaScript | `typescript-language-server` |
| Rust | `rust-analyzer` |
| Go | `gopls` |
| C, C++ | `clangd` |

A `.lsp.json` — `OPENMIRROR_LSP_CONFIG`, or the directory the daemon starts in,
like `.mcp.json` — adds servers or switches them off:

```json
{
  "servers": {
    "zls":    { "command": "zls", "extensions": [".zig"] },
    "pyright": { "command": "pyright-langserver", "args": ["--stdio"],
                 "extensions": [".py"], "initializationOptions": {} },
    "clangd": { "disabled": true, "extensions": [".c", ".h"] }
  }
}
```

A configured server takes its extensions from the defaults; a disabled one
takes them without replacing them, which is how to say "not for C" without
uninstalling anything. It is read by the daemon, not from the project the agent
is working in: a server is a program the daemon runs, and a repository should
not get to name one.

The first question in a language with nothing running is graded `execute`,
because starting a server can run the project's own code — rust-analyzer runs
build scripts and proc macros — and every question after that is a read. Once
a server is up, a file the agent writes is shown to it, and any errors it then
reports are added to that write's result. A server is never started to do
that: an edit was not graded as running anything.

### Not only questions

Most language servers can also change things, and an agent that can only ask
will try to hand-edit around a fix the server already knows how to make. Three
operations do that:

| op | what it does |
|---|---|
| `code_action` | the fixes the server offers at a point. With `action`, applies the one you name — by number, or by enough of its title to be unambiguous. |
| `rename` | renames a symbol everywhere it means, which is the thing a search-and-replace cannot do. |
| `format` | formats a file, or a line range of one. |

Listing the offered fixes is a read. Applying one is a write. The write path is
graded so that a *first* call in a language stays `execute` even when it also
changes files, because `AUTO_EDIT` allows writes and asks about `execute` — and
grading "start a process and change three files" as a write would make the call
that also starts something the more permissive of the two.

Two details that are easy to get wrong and are tested here:

**Character offsets are UTF-16.** Every position in the protocol counts
characters the way a JavaScript string does, not bytes and not code points, so
an emoji before your cursor shifts the column by two. The implementation encodes
the line prefix as UTF-16 and decodes from the middle, which also means an
offset landing between the halves of an astral character clamps to that
character's boundary rather than raising.

**Edits are applied back to front, and overlapping ones are refused.** Every
range in a set refers to the document as it was *before* the set arrived, so
applying them in the order they were sent puts the second one in the wrong
place. Overlapping spans are refused outright rather than merged, because a
merge is a guess about which of two intentions was meant.

Every write goes through the session's own path confinement, and a `WorkspaceEdit`
naming even one file outside the working root is refused whole — half an edit
applied is a tree that never existed. `CreateFile`, `RenameFile` and
`DeleteFile` are refused too; there is no reason an agent should be creating
and removing files through a side channel that skips the checkpoint.

## Rewind

Every turn that changes a file opens a checkpoint first, so a turn can be
undone:

```
GET  /api/sessions/{id}/checkpoints
POST /api/sessions/{id}/restore   {"checkpoint": "1"}
```

or the **Rewind** button in the header.

Undoing a turn undoes every turn after it too. Undoing one in the middle would
produce a tree that never existed and that nobody asked for.

`/undo` and `/redo` in the conversation walk the same history one step at a
time, and they are a pair: a redo returns the after-content, and a turn that
has been redone is undoable again. A new edit clears the redo stack, because a
redo that reached past a later edit would put back a tree built on top of
changes that are no longer there.

Snapshots are content-addressed, so a session that edits one file thirty times
stores thirty hashes and a handful of blobs. They live under the data
directory, never inside the working root — a snapshot in the tree the agent is
editing gets read, grepped, and eventually committed.

An **interrupted** turn keeps its checkpoint. That is the turn most likely to
have left the tree somewhere nobody wanted.

If somebody edited a file by hand after the agent wrote it, the rewind says so
rather than silently overwriting their work. That comparison is against what
the agent *left*, not what it found — comparing against the before-state makes
the warning fire on every single rewind, since the agent changing the file is
the reason there is a checkpoint at all, and a warning that always fires is one
nobody reads.

### What it does not cover

**Only changes made through the file tools are recorded.** A shell command can
write anywhere, and snapshotting the whole filesystem before every command is
not something anyone wants. This is an undo for the agent's own edits, not a
transaction over your machine.

Where the working root is a git repository, git remains the better answer, and
the UI says so.

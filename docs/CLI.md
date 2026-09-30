# The command line

`openmirror` used to take no arguments at all: the command started the daemon
and that was the entire interface. Everything else — a conversation, a model,
a stored transcript — was reachable only through a browser tab, which is a
strange shape for something whose whole point is being driven by code.

Two things were added, and they are deliberately different from each other:

| | |
|---|---|
| **`openmirror run`** | the agent, headless. No browser, no terminal UI, one prompt start to finish. |
| **`openmirror chat`** | a terminal *client* for a running daemon. Streaming, approvals, `/commands`, `@` mentions, pasted images. |

The difference matters. `run` starts its own agent and owns the whole
conversation. `chat` starts nothing: it speaks the same HTTP and WebSocket API
the browser does, so the daemon stays the only thing holding your sessions,
your providers and your tools. A TUI with its own agent loop inside the process
would be two implementations of the same conversation to keep in step, and the
browser already is the one people use for anything long.

## The daemon still comes first

```bash
openmirror            # with no arguments, starts the daemon
openmirror serve      # the same thing, said out loud
```

**Bare `openmirror` will always start the daemon.** Two callers inside this
repository depend on exactly that and pass no arguments — the desktop app's
frozen sidecar, which imports `main` and calls it, and `daemon.rs`, which
spawns the binary with an empty argv. Anything else was made of that shape a
promise nobody was told about.

A leading dash is not a word, so `openmirror --port 9000` is the server with a
flag. A word that is not one of the commands below is a mistake, and a mistake
is the usage on stderr and exit 2 — argparse's convention, which every other
tool on the machine already follows, and which scripts already know how to
read.

## Exit codes

These are fixed and documented because scripts read them.

| code | meaning |
|---|---|
| `0` | it worked |
| `1` | the agent failed, or the run could not start |
| `2` | the command line was wrong |
| `3` | the port was already taken |
| `130` | interrupted |

## `openmirror run` — one prompt, no browser

```bash
openmirror run -p "explain what this repo does"
git diff | openmirror run -p "review this diff for security problems"
openmirror run -p "write the migration" --model anthropic --mode auto_edit
```

`-`, or no flag at all, means the prompt is on stdin, and whatever is there is
prepended to it. That is what makes the pipeline examples work: a hundred
lines of log, or a diff, fed to a model that can see the files around it.

Useful flags:

| flag | what it does |
|---|---|
| `-p, --print TEXT` | the prompt; `-` or absent means stdin |
| `-m, --model`, `--provider` | which model answers |
| `--mode MODE` | an [approval mode](MODES.md) — the ladder in the browser is the same one |
| `--effort LEVEL` | how hard the model thinks: `off` … `max` |
| `-C, --root PATH` | the folder to work in; the working directory by default |
| `-s, --session ID` | continue a stored conversation |
| `-c, --continue` | continue the most recent conversation in this folder |
| `--fork` | fork the resumed conversation instead of continuing it |
| `--at N` | fork after N messages, with `--fork` |
| `--tools LIST` | a toolset group (`files`, `shell`, `web`, …) or a tool name, comma separated |
| `--max-turns N` | the model's round-trip ceiling; hitting it is a failure, not a finish |
| `--json` | one JSON object per event instead of prose |
| `--output-file PATH` | also write the answer to a file |
| `-q, --quiet` | no progress lines on stderr; the answer still goes to stdout |
| `-y, --yes` | approve every tool call without asking |
| `--timeout SECONDS` | wall-clock cap; `0` for none |

The conversation is written to the session store, so `--continue` and
`--session` work on it afterwards.

### Nobody is watching

An approval request with no human at the keyboard has exactly two honest
answers — refuse it, or approve it because a flag said so — and there is no
third one that does not involve hanging until something times out. **Refusing
is the default**, and the refusal is fed back to the model as a tool result so
it recovers rather than dying. A question from the `ask_user` tool is answered
the same way, for the same reason: a run that waits for a person who is not
there is a run that never finishes.

`--yes` is the other answer, and it says on stderr what it agreed to.

## `openmirror chat` — a terminal client

```bash
openmirror chat                     # attach to a daemon on 127.0.0.1:8477
openmirror chat -c                  # the most recent conversation in this folder
openmirror chat -m gpt-5 --mode plan
openmirror run -p … --json | openmirror chat   # still a client, still needs a daemon
```

It needs a daemon, and says so plainly if there is not one rather than starting
a private one behind your back.

Inside it:

- assistant text streams as it arrives, unbuffered
- tool calls are one dim line each; results are collapsed to a summary, so a
  thousand-line file read does not scroll the conversation off the screen
- approvals prompt `[y/N/a(lways)]`; `a` is remembered for that tool *with those
  arguments*, so agreeing to `ls` does not agree to `rm -rf /`
- `/` completes commands and skills from the daemon, `@` completes file paths
- a pasted or dropped image goes with the message
- Ctrl-C once interrupts the turn, twice exits; Ctrl-D exits; Ctrl-L clears
- `NO_COLOR`, a non-TTY stdout, or `TERM=dumb` turns the colour off
- if the socket drops it reconnects with the sequence number it last saw, so the
  transcript stays correct and messages typed in the gap are sent in order

With a non-interactive stdin it reads the prompt from it, runs one turn, prints
the answer and exits — so it is scriptable, and `--prompt` does the same thing
without the pipe.

## `openmirror sessions` — the conversations on disk

```bash
openmirror sessions list
openmirror sessions list --root ~/work/api --json
openmirror sessions show 4f2a…
openmirror sessions search "retry loop"        # every conversation, not just this one
openmirror sessions export 4f2a… --format md --output notes.md
openmirror sessions fork 4f2a… --at 12
openmirror sessions prune --older-than 30
openmirror sessions delete 4f2a…
```

`search` reads the stored transcripts, not the live session list, so it finds a
conversation that was closed a week ago. It reads the tail of each transcript
rather than all of it, and says so in its own help; a session whose only mention
of a term is older than that window is found by its title or not at all.

A conversation can also be written out as one self-contained HTML file — no
scripts, no network references, opens with the network off — from the
sidebar's **share** button or at `/api/sessions/{id}/export.html`. That is the
honest version of "share this conversation" for a tool that runs on your own
machine and has nowhere to publish: a file you can then do whatever you like
with.

## `openmirror models` and `openmirror doctor`

```bash
openmirror models                   # every provider that can chat, and what it offers
openmirror models --provider ollama
openmirror doctor                   # what is configured, what is missing, what it would pick
```

`doctor` never fails. Its job is to answer "why is nothing happening" without
you having to read a log file.

## Commands in the conversation

Typed into the composer, in the browser or in `openmirror chat`. They are about
the conversation rather than part of it, and they are shaped like turns so a
client renders them as one.

| | |
|---|---|
| `/model <name>` | which model answers from the next turn on |
| `/think <level>` | how hard it thinks: `off`, `low`, `medium`, `high`, `xhigh`, `max`, `default` |
| `/compact [focus]` | summarise the conversation to make room |
| `/undo` | put the last turn's file changes back |
| `/redo` | put back what the last `/undo` took away |
| `/context` | how full the context window is, and whether that number is a guess |
| `/status` | model, mode, tools, folder, size — and no price, on purpose |
| `/clear` | forget the conversation, keep the session and the folder |

`/undo` and `/redo` are a pair, and repeatable: a new edit forgets the redo
stack, because a redo that reached past a later edit would put back a tree built
on top of changes that are no longer there. A file edited by hand after the turn
is reported by name and then overwritten, the same as the rewind dialog — the
report is the protection, and both commands print it in full.

Alongside them are any [skills](EXTENDING.md) you have written, and any skill
or `commands/*.md` either tool's directory layout already uses.

## See also

- [MODES.md](MODES.md) — the approval ladder
- [SESSIONS.md](SESSIONS.md) — transcripts, resume, fork, export
- [EXTENDING.md](EXTENDING.md) — MCP, LSP, agents, skills, hooks

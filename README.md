# openmirror

**An open-source digital twin.** Something that lives on your own machine and
acts there the way you would — running commands, driving a browser, holding a
spoken conversation, and (when you switch it on) remembering what it learns
about you between them. The *open* is the load-bearing half of the name: every
part of the reflection is one you can read, replace, route somewhere else or
turn off.

Which is why nothing here is hosted on your behalf. You pick — separately, per
kind of generation — whether the thinking, the listening, the speaking and the
picture-making run on your own hardware, on OpenAI, on Anthropic, or on
anything else that speaks a shape it already knows.

```
                                   ┌──────────────────────────────────────────────┐
you ── websocket ──▶ openmirror ───┤  chat       Perch │ Anthropic │ OpenAI       │
                          │        │  embedding  Perch │ Voyage    │ Gemini       │
                          │        │  dictation  Perch │ OpenAI    │ Groq         │
                    ┌─────┴─────┐  │  speech     Perch │ OpenAI                   │
                    │  agent    │  │  images     Perch │ 17 others  ── see below  │
                    │  voice    │  │  video      Perch │  9 others                │
                    └───────────┘  └──────────────────────────────────────────────┘
                     runs commands       one choice per row, made separately,
                     on this machine     enforced when the route resolves
```

Six ways to use it, one at a time, because typing at an agent, talking to
one, and watching one work are different kinds of attention:

| | |
|---|---|
| **Code** | type back and forth, with tool cards, diffs and approvals |
| **Talk** | one button, and then only your voice — it speaks its questions and says when it is waiting |
| **Live** | OpenAI's realtime API — speech to speech, no text in the middle |
| **Autopilot** | a goal and a live view of a screen it is driving. No chat |
| **Studio** | images and video, with every knob the backend actually has |
| **Search** | the agent's own search backend, without the agent |
| **Browser** | which browser it drives — Chromium, your Chrome, Firefox, WebKit, or a path |

---

## What works today

**An agent that acts on the machine.** Read, write, edit, multi-edit, patch,
notebooks, outline, list, glob, grep, a shell that can leave things running, a
to-do list and asking you a question — behind an
approval policy that grades each call by what it would actually do. `git
status` and `rm -rf /` are the same tool, so the risk is assessed from the
arguments, not the tool name. Everything is confined to one working root, and
the confinement survives `..`, symlinks and double-encoded paths.

Each of those exists because of a specific failure. `multi_edit` applies every
edit to a buffer and writes once, so a refactor cannot land halfway.
`read_files` because reading four files should not cost four model turns.
`outline` because reading two thousand lines to find one function wastes a
context window. `todo` because a long task with no visible plan cannot be
followed by the person watching it — so the list is pinned above the box you
type in, and stays in view while the transcript scrolls.

**And a way to offer fewer of them.** A long tool list is not free: gemma4:12b
given the full set opened a page and then reported that it had no way to
browse. The same model, the same task, the same prompt, with only the browser
tools — twenty-six seconds, correct, first try. So a session can be narrowed
to `browser`, `files`, `shell`, `todo`, `agents`, `skills`, `web`, `desktop`,
`media` or any combination, and `ask_user` is always in whatever you pick,
because a session that cannot ask is a session that guesses.

**Subagents.** `agent` hands a self-contained piece of work to another agent
with a fresh context of its own: a broad search whose working-out nobody needs
to keep, or several independent pieces at once — agent calls in one response
run in parallel. Three kinds are built in (`explore`, which cannot change
anything whatever mode the session is in; `general`; `research`), and more are
markdown files in `.openmirror/agents/` or `.claude/agents/` — the format other
clients already write, with their tool names translated. A subagent is a
session with nothing of its own but its conversation: its steps nest in the
transcript under the call that started it, its approvals are asked of you in
the same place and under the same mode, and it cannot start agents of its own,
touch the to-do list, or use the session's one browser and one screen. One sent
off with `background: true` reports when it finishes, without being asked.

**Plan mode.** A sixth approval mode. The agent investigates with the tools
that only look, then puts a plan to you through `propose_plan`, and nothing
that changes anything runs until you say yes — at which point you also choose
how much it may then do unasked. The editing tools are hidden from the model
while it plans, and the way out is hidden while it does not.

**Skills, and `/` in the composer.** A skill is a folder with a `SKILL.md` in
it: when it applies, how to do the job, and whatever files the job needs. Only
names and one-line descriptions sit in front of the model; the body is loaded
when a request matches. They come from the project, from your home directory
and from openmirror itself — `init` writes an AGENTS.md, `review` reviews
uncommitted changes — in the layout other clients use, so ones you already have
work here. Typing `/` lists them, alongside `/compact` and `/clear`.

**Language servers.** `lsp` asks a real language server where something is
defined, everywhere it is used, what type it is, and what the compiler thinks
is wrong — rust-analyzer, pyright or pylsp, typescript-language-server, gopls or
clangd, whichever is installed, and not offered at all where none is. Once one
is running, an edit that breaks the file hears so in its own result, before the
model moves on. Starting one is graded as running a command, because
rust-analyzer runs the project's build scripts to answer.

**Notebooks.** `read_file` shows a Jupyter notebook as its cells and their
outputs — plots included, as pictures the model can look at — rather than as
JSON, and `notebook_edit` replaces, inserts or deletes one cell, clearing the
outputs of code it changed, since they no longer match it.

**Background work.** `shell` with `background: true` leaves a dev server or a
long build running; `tasks` reads what it has printed and stops it, and the
model is told when it finishes. A strip above the composer shows what is
running, with a Stop button. Interrupting a turn leaves background work alone;
closing the session stops it.

**Compaction.** A long conversation is summarised to make room — only what came
before the current turn, so the work in hand is never cut in half — and
`/compact` does it on request. Afterwards every file has to be read again
before it is overwritten: a summary says a file was read, not what it said.

**Duplex voice, with barge-in.** Both directions are open at once. Voice
detection runs on the server, so when you start talking over the assistant it
knows you did it mid-sentence, kills the synthesis and tells the client to
drop what it has buffered. Replies are synthesised a sentence at a time as
they stream, so speech starts around the first sentence rather than the last.
When an utterance is cut off, history records only what you actually heard.

**Provider choice per modality.** Chat on Anthropic, dictation on your own
whisper, speech on your own Kokoro, images on your own Stable Diffusion — or
any other combination. `OPENMIRROR_LOCAL_ONLY` is enforced where a route is
resolved rather than in a UI: with it on, a local backend being down is an
error, never a quiet fallback to a hosted one.

**Chat and embedding are chosen separately**, and that is not a nicety. Your
questions go to one and your entire remembered history goes to the other; a
single "AI provider" setting is how someone ends up shipping their memory to a
vendor they picked to write code with.

**Connections are data, not start-up configuration.** Twenty presets — Groq,
OpenRouter, Fireworks, Together, NanoGPT, DeepInfra, Cerebras, Mistral, xAI,
Voyage, Google, LM Studio, vLLM, llama.cpp, Open WebUI and the rest — each a
starting point you can edit, added and tested while the daemon runs. **Model
lists are never remembered**: every picker asks the provider, every time,
because a local install has whatever happens to be pulled and a gateway's
catalogue changes weekly. The two services that publish no listing endpoint
say so on every row rather than implying the server confirmed a name it has
never seen.

**Embeddings that are actually different from each other.** OpenAI
`text-embedding-3-large`, Gemini Embedding 2, Voyage `voyage-3-large`, and
Qwen3-Embedding 8B and 4B on hardware you own. Several of these are
asymmetric — a passage and a question are embedded differently — and the
instruction goes on queries only, because doing it to both is the same as
doing it to neither.

**Tor, opt in per connection.** Never inferred from the proxy being up: that
would be a decision about where your prompts go, made on your behalf. Turning
it on with nothing behind it is refused at the point of use rather than
falling back to a direct connection, which is the failure the toggle exists to
prevent. Same rules as the sibling projects on this machine — Arti's 9150 by
default, and a probe that checks the C daemon's 9050 before concluding nothing
is listening.

**Perch as one connection.** Give it a host and a token; it probes the five
services and registers the ones that are actually switched on.

**Sessions that outlive their client.** A session is a unit of work, not a
connection. Closing the tab does not stop an agent four minutes into a build;
reopening replays what you missed from a sequence number rather than starting
over. Several run at once, and the sidebar says which are working and which
are waiting on you.

**A browser client.** No build step: two websockets, streaming text, tool
cards with the risk grade on them, inline diffs, approve and deny, and voice.
A finished read is one grey line; what wants looking at — a failure, a diff, a
screenshot — is a card, and every turn ends with how long it took and a
receipt of the files it changed, with a button that puts them back. Approval,
model, working root and provider sit under the box you type in, because all
four of them qualify the next thing you say rather than the last one — and
approval is live, so how much you trust a run is something you can change
while watching it.

**A companion, if you want one.** A pixel creature reacts to what the agent is
actually doing — it watches while files are read, paces once a build has been
running for twenty-five seconds, and, if it is the moth, flies into the side
of the window when something throws. It perches over the end of the
transcript, marks the app in the sidebar, sits in the middle of a session
that has not started yet, says in words what it is doing next to the send
button, leans into an approval bar while that is what it is stuck on, and is
the tab's icon — one creature and one state across all of them. Five to pick
from, one at a time or none, and the choice is kept in your browser and sent
nowhere. Each one is data — a palette, frames written as strings of
characters, and a table of states — so a sixth is a new file rather than a
change to an old one. `prefers-reduced-motion` stops the clock
entirely: what it is doing still shows, it just stops moving while it does
it.

**Per-user memory, opt in.** Off until switched on, and then off for each
person until they turn it on themselves. Everything stored is listed, any of
it can be deleted, and all of it can be. See [docs/MEMORY.md](docs/MEMORY.md).

The modes are described in [docs/MODES.md](docs/MODES.md) and the providers,
routing and Tor in [docs/PROVIDERS.md](docs/PROVIDERS.md). Mail has its own
page — [docs/MAIL.md](docs/MAIL.md) — because why IMAP rather than a vendor
API, and what `threadId` is worth, do not fit in a paragraph. Hooks have
[docs/HOOKS.md](docs/HOOKS.md), where the property that makes running a
project's code defensible is written out in full. Leave has
[docs/SESSIONS.md](docs/SESSIONS.md) covers what is still here tomorrow.
[docs/LEAVE.md](docs/LEAVE.md) is mostly about when the assessment
*declines* to answer — which is the part that matters.

**The web, and a real browser.** `web_search` and `web_fetch` for reading,
`research` for answering — several searches and the pages behind them in one
call, with every extract labelled inline with the URL it came from rather than
a bibliography at the end. Playwright for acting: navigate, read, click, type,
screenshot. The browser keeps a persistent profile, so you sign in to your
sites once by hand and the agent inherits the session without ever needing a
password — and a second session, which cannot have that profile because
Chromium locks it, gets a fresh browser and is *told* it is signed out, so it
reads a login wall as being logged out rather than as the site being broken.

Search needs no key. The default backend drives a real browser, because that is
the only keyless path left — the engines answer plain HTTP requests with a
challenge page now, which is why the old scraper stopped working. It tries
Startpage, then DuckDuckGo, then Bing, then Google, least-tracking first, and
stops at the first that answers; name one and it uses only that one, since
choosing an engine for what it does not log is not consent to fall through to
one that does. A key still helps — Brave, Tavily or your own SearxNG — and
those backends are still here and still faster.

The same search backend is reachable directly, without a model in the way, for
when you only wanted the links.

**Which browser, in a dialog.** Playwright's Chromium by default; your own
Chrome or Edge when a site wants the codecs or an ordinary-looking visitor;
Firefox or WebKit when Blink is the thing you are trying to get away from; or a
path to Brave or an ungoogled build. It applies to both the agent's browsing
and to search, and a choice that cannot launch is refused when you make it
rather than halfway through a task.

**The machine itself.** "Install Steam" is not one instruction — it is
`apt install steam-installer` on Debian, a flatpak where there is no native
package, already-done on a Steam Deck, `winget install Valve.Steam` on
Windows, and a cask on macOS. So the tools take a *name* and the platform
decides what it means: `system_info` reports what this is and how it can
elevate, and `package_install` routes accordingly. A derivative inherits its
parent's route through `ID_LIKE`, so Pop!_OS gets Ubuntu's without being
listed.

Some answers are not the package you asked for. "Install the Epic Games
Store" on Linux resolves to **Heroic**, because Epic ships no Linux client at
all — and the recipe carries the instruction to say so before installing it,
since someone who asked for Epic deserves to know they are getting a different
program and why.

Two failures it avoids by construction. A package manager asked to install
something interactively stops at *"Do you want to continue? [Y/n]"* in a
terminal nobody is reading, so every one of them is invoked non-interactively
with stdin closed. And `sudo` on a machine that wants a password does not
fail — it blocks forever on a prompt going nowhere — so the elevation route is
worked out first and a system-wide install is *refused*, naming the per-user
alternative, when there is no way to become root without a person.

On an immutable system — SteamOS, Silverblue, anything ostree — the system
package managers are present and will not work. The tool says so and points at
flatpak, and says plainly not to disable the read-only root, because an agent
that meets `apt` failing there will otherwise reach for `steamos-readonly
disable`.

**Display settings, honestly split.** `display_hdr` runs the command where the
desktop has one — KDE exposes HDR through `kscreen-doctor` — and where it does
not, which is most of them, it opens the right settings page and says what to
click, for the desktop tools to finish. COSMIC's own `cosmic-randr` has no HDR
subcommand; pretending there is a universal command is how an agent ends up
inventing one, running it, and inventing another.

**Images and video, from almost anywhere, tuned as far as you like.** The
panel is drawn from what the backend says it has — nothing in the client knows
what a sampler is, so installing one on the diffusion box adds it to the
dropdown with no release in between.

Eighteen image backends and ten video ones, and the reason it is not a list
somebody has to keep extending is that **the form comes from the model**:

| | |
|---|---|
| **Your own hardware** | AUTOMATIC1111 (txt2img, img2img and inpainting) and ComfyUI, whose templates mark their inputs with tokens so the form follows from which tokens are present — a new video model is one file, and a "Save (API format)" export is imported and tokenised for you |
| **Everything else, twice over** | Replicate and fal each host most of the field, and each **publishes an input schema per model** — so a model released this morning arrives with its real ranges, its real enums and its author's own description of every knob, with nothing added here |
| **First party, where they have something the resellers drop** | OpenAI (gpt-image-1, and Sora for video) · Google (Imagen, the conversational image model, and Veo) · Black Forest Labs (FLUX, and Kontext for editing) · Stability · Ideogram, for when the picture has words in it · Recraft, for real vectors · Runway, whose tagged reference images survive nowhere else · Luma, for camera language and keyframes · Kling, for motion that obeys physics · MiniMax, whose Director models take shot instructions inline · xAI, Together, Fireworks, DeepInfra |

Both kinds are jobs: started, watched and **cancellable at the provider**,
because a render nobody stopped is a GPU working for nobody and a hosted
account still being billed. What each one reports about its own progress is
what it honestly knows — a fraction from Runway and Sora, a queue position
from fal and ComfyUI, elapsed seconds from Veo, which reports nothing — and
never a bar that creeps to ninety and stops.

Pictures go *in* as well as out: a reference image, a frame to start from, a
frame to end on, a mask. Every result is stored as a file plus its recipe,
including the seed the backend actually used, and the recipe is a **button** —
one click puts the whole thing back in the form, because changing one thing
and going again is the entire loop.

There is no Midjourney here, and that is not an oversight: it publishes no
API. Anything else that does can be reached through Replicate or fal without
waiting for a release of this.

**The desktop itself, without taking your mouse.** `OPENMIRROR_DESKTOP=true` adds
screenshots, clicks, typing, scrolling and key chords. The part that matters
is *where*: on a **virtual stage** the agent gets an X server of its own — its
own pointer, its own keyboard, its own windows — and applications are launched
onto it by environment. That is the only arrangement under which "it will not
take your mouse" is a guarantee rather than an intention, and it is checked
rather than asserted: the test drives a real Chromium through a booking form
and then reads the real display's pointer to prove it never moved.

On the screen you *are* looking at there is one pointer and it moves it, so
the promise is not available. What is available is that it gives it straight
back, and stops entirely the moment it finds the pointer somewhere it did not
leave it — because that means a person has a hand on the mouse. A stage also
carries a rectangle, which is what multi-monitor targeting is: everything
outside it is neither captured nor clickable, and a coordinate outside is
refused rather than clamped, since a clamped click lands on something real.

On Wayland capture goes through the compositor's own screenshot tool and input
through `/dev/uinput`, because X11 grabbers return a black image there.

**Any MCP server, as tools.** Point it at a `.mcp.json` and every server in it
becomes tools the agent can call — local ones it spawns, and hosted ones it
reaches over HTTP or the older SSE transport, which is the case where *not*
running somebody else's code on your machine is the point. Resources and
prompts come across too, resources as a fixed pair of tools rather than one per
document. Servers are distrusted by default: a tool that declares itself
read-only is still confirmed, because a server that can call itself harmless is
a server that can opt out of the check it most needs.

**And openmirror as an MCP server, if you want it.** The other direction: your
editor or another agent asking *this* twin what it knows, grounded in the same
memory the voice session uses. Off by default, token-gated, and scoped — `read`
recalls and searches, `write` also remembers, `all` also spends money on
images. Nothing at any scope runs a command, touches a file or drives the
screen: those are the tools openmirror guards with a human in front of them, and
an MCP call has no human in front of it. See
[docs/EXTENDING.md](docs/EXTENDING.md).

**Rewind.** Every turn that changes a file can be undone, including one that
was interrupted halfway. See [docs/EXTENDING.md](docs/EXTENDING.md).

### Your mail, your repository, your week

Three things an agent needs to work *for* you rather than *near* you, each
reachable from a tool and from the interface, over the same accounts and the
same store — so the agent and the browser are two doors onto one
implementation, not two implementations.

**Mail, over IMAP or JMAP.** One `mail` tool and one Mail tab, both speaking
whatever your host speaks: Gmail, Outlook, currere.co, Fastmail, Proton. No
OAuth dance per vendor, because IMAP and JMAP are both in the standard
library and both are what these hosts already run — the cost is an app
password, which is five minutes in a settings page and a much smaller thing to
go wrong than a refresh token that silently stops working.

Reading is `read` and runs in every mode. *Answering* is a third invariant
alongside purchases and credentials: possible, and never automatic, in every
mode including `unrestricted`, and never remembered — a "yes" to one reply is
not a "yes" to the next one to the same person. Replies carry
`In-Reply-To` and `References`, so they land in the thread rather than beside
it, and a draft goes to your own Drafts folder where your mail client can see
it.

JMAP is preferred where it is available, for two reasons that are not
performance: `threadId` is the *server's* answer to which conversation a
message belongs to, and IMAP has no such field, so the IMAP path reconstructs
it from `References` and a normalised subject — right most of the time and
visibly wrong some of the time.

**A repository it can read, and a commit you can make.** The `git` tool
parses `--porcelain=v2` rather than asking a model to read porcelain, so
`git status` is a grouped list instead of a blob of two-column text. A commit
is `write`, which means it is *asked about* in `ask` mode instead of being
graded `execute` and lumped in with `npm run build`. There is deliberately no
`reset --hard` or `clean -f` in the tool: both are one `shell` call away,
already graded `destructive` there, and a foot-gun does not belong in a
first-class tool.

The Commit bar in the header follows your working root and only appears when
there is a repository with something in it. Its two buttons are the whole
design: **"Write it for me"** reads the staged diff, sends it to whichever
model this install routes chat to, and puts a draft in the message field — and
**"Commit"** sends whatever is in the field at that moment. A single button
with an `ai: true` flag would be one click instead of two, and nobody would
read the messages after the first week, and the commit message is the one
artefact of a piece of work that outlives it.

**A week, and where there is room in it.** The `calendar` tool reads `.ics`
files — no dependency, because the format is a line-oriented text format with
a folding rule and a date grammar, and the awkward parts are all in one file:
`DTEND` is exclusive so back-to-back meetings are not a clash, `VALUE=DATE` is
a whole day rather than midnight, folding is at 75 *octets* so an emoji in a
subject survives, and a `TZID` the system does not know stays floating rather
than being assumed UTC. `availability` answers "when can we meet" from *merged*
busy blocks, so three meetings in a row produce one hole rather than two
imaginary ones. Nothing is ever written to a calendar: `propose` returns
`.ics` text and stops.

**Updates, from GitHub.** Settings → Updates checks for a newer release and
tells you: a dot on the tab, and nothing else. Downloading waits for a button
press, and installing waits for a confirmation, because a message that appears
over work you are doing is a message that gets dismissed without being read.
A downloaded installer is checked against the SHA256 published in the same
release before it is offered to run — an integrity check rather than a
signature, and [docs/UPDATES.md](docs/UPDATES.md) says exactly what that does
and does not catch. A stable install is never offered a release candidate.

**A message typed while it is working waits its turn.** Send, and it is held
and run when the turn in progress finishes. The moment you have something to
add is usually *while* the agent is working on the first half of it, and the
old answer — an error saying "interrupt it first" — threw away what you had
just typed and made you watch for a gap to type in. An interrupt still
throws the queue away with the turn: cancelling is cancelling, and a queue
that outranked it would run work somebody threw away.

**How full the conversation is.** A count under the composer, and a bar when
there is a denominator to draw it against. The count is real; the bar is
`None` for a model this project has not heard of, because a bar at 40% on a
model that will refuse the request is worse than no bar. It fills towards the
point the conversation gets *summarised*, which is the number that decides
whether to keep going. There is no cost in dollars: a price is out of date the
moment a provider changes one, and somebody reads a stale number as a bill.

**Your conversations survive a restart.** Every turn is written to
`data/sessions/`, and `GET /api/sessions/stored` lists what is there. A
session is reopened by *rebuilding* it — tools, provider and policy as they
would have been for the first time — and putting the stored messages back, so
a transcript is a record rather than a re-execution. What is lost is said
rather than hidden: a turn that was running when the daemon stopped comes back
to *just before* it, because a transcript does not contain a Future and the
alternative is a model believing a half-finished turn completed.
`GET /api/sessions/{id}/export` gives you the conversation as Markdown, with
the tool calls kept — "the agent ran a command and here is the conversation
without it" is a document that misleads.

**A settings file, and one rule about it.** `.openmirror/settings.json`,
`.openmirror.json` and `.claude/settings.json` are all read, because being
told to rename a file is how a feature goes unused. And *a project may narrow
permissions and never widen them*: the approval mode can only go down the
ladder, `allow_*` can only go off, `local_only` can only go on. A repository
that ships a settings file is a repository you opened, and letting it raise
its own safety settings would be the same hole the hooks have, one layer up.

**A worktree per job.** `OPENMIRROR_WORKTREES=1` and a session can be put in a
second checkout of the repository, on its own branch, where it cannot reach
the tree you have open. That is the middle ground `unconfined` is not: a flag
that opens the whole disk means somebody who needs to read one directory next
door either leaves it on and stays scared, or does without. Removal is
refused while a worktree has changes in it, and `--force` is not offered.

**Leave, and what your own record says about it.** A local ledger of requests
and the decisions on them, and an `assess` action that gathers the evidence
for a request you have not decided yet: the closest past decisions with
their reasons, the policy you wrote in your own words, the notice that person
gives, and what is already booked.

It returns a **band, never a percentage** — `clear`, `coin-flip`, `unlikely`,
and nothing at all until there are four comparable decisions, because
"73% likely" from nine requests is a number that looks like knowledge and
isn't. And it says when it declines to answer: a request longer than anything
you have decided before, or somebody with no record of their own, gets the
evidence and *not* a reading. The judgement is the model's; a tool that
answered "approve" would have taken a decision that is yours.

Nothing is sent anywhere — no sync, no token, no payroll leaving the machine.
An install that wants a real HR system reaches it through a hook.

**Hooks — your own commands around a tool call.** Settings has a Hooks
panel. A hook is a command that gets the call as JSON on stdin and can refuse
it, which is the extension point every serious harness has and the one thing
that is actually yours.

The design is one asymmetry: `exit != 0` refuses, and **nothing at all can
turn a refusal into an approval**. A repository's hooks may stop you doing
things; they cannot do things on your behalf. That is what makes it defensible
for a project to ship a `hooks.json` — a project's are code you did not write,
so the first time one would fire, a strip above the transcript shows the exact
command and asks, and the answer lasts for the session, per command.

```json
{"PreToolUse": [
  {"name": "no secrets", "command": "grep -qiE 'password|api_key' - && exit 1",
   "tools": ["write_file"]}
]}
```

**Review a turn one hunk at a time.** "Review changes" sits beside Rewind,
because they are different decisions: rewind throws a whole turn away, review
keeps the parts you agree with. Every block in the diff is a choice, and
changes rarely deserve all-or-nothing — of the four things a turn did, three
are right and the fourth is wrong, and undoing the turn to get rid of the
fourth throws the three away. Nothing is written until you press the button,
which says how many hunks in how many files it is about to put back.

The file is *rebuilt* from the turn's before- and after-content rather than
patched, so applying a second choice cannot land in the wrong place, and what
you were shown and what gets written cannot drift apart.

**`@` for a file.** Type it in the composer and a list of files in the working
root appears, ranked so the one you meant is first. Enter completes a partial
name and sends a finished one. Dotfiles, `.git` and anything over 2MB are not
offered, and the walk is bounded by time as well as count, because you are
still typing.

**Full system access, if you want it.** `OPENMIRROR_UNCONFINED=true` gives it the
whole filesystem and tells the model plainly that it is not sandboxed.
Purchases, credentials and sending mail stay on their own axis — see
[docs/AUTONOMY.md](docs/AUTONOMY.md), which is the page to read before running
this unattended.

## What this is built for


### The floor: a model *and* a tool budget

**Chat: `qwen3.5:9b` or `gemma4:12b`. Embedding: `Qwen3-Embedding-4B`.**

What a feature is allowed to depend on, not what will start. Below it the agent
loop does not slow down, it becomes unreliable in ways that look like the
harness misbehaving: prose where a tool call was needed, a plan the model just
wrote and has already lost.

The number does not travel alone. `gemma4:12b` did the five-step booking task
in twenty-six seconds, first try — **narrowed to about ten tools**. Given the
full set of thirty-odd it opened the page and announced it had no way to
browse. So at the
floor the **tool budget is part of the configuration**, which is what
`TOOLSETS` and the `tools` list on `POST /api/sessions` are for. Handing a
floor model everything is a way of putting it below the floor without changing
the model.

Honest about the evidence: those measurements are `gemma4:12b`. `qwen3.5:9b` is
named because the rest of this family tests against it, not because openmirror has
measured it. And even narrowed, a 12B is not enough for `autopilot` — see
"What is not built yet".

**A warning, never a wall.** Nothing refuses a small model. `qwen3:1.7b` on a
4 GB box is yours to try, and narrowing the toolset is what will help most.
The floor governs what a **feature** may assume, not what an **operator** may run.

### The ceiling: frontier models with thinking

The end openmirror was actually designed around — planning, calling tools and
recovering from its own mistakes is what reasoning models are best at.
Reasoning streams as its own channel and renders as working-out under the
reply; nothing is gated on model size; no prompt is shortened for the floor.
The single exception is a voice call, where reasoning is silence and `think` is
sent off deliberately.

### Where it runs: not a thin client

openmirror installs like `claude-code` or `codex` — a daemon on the machine you want
worked on, reached from its desktop app or a browser. **The model may be remote. The agent is
not.** It runs shell with a risk classifier, edits files, drives a real
Chromium, and optionally sees the screen and moves the mouse — all on the host
it is installed on, whichever machine the tokens come from. `OPENMIRROR_WORKSPACE`,
the approval modes and `OPENMIRROR_UNCONFINED` are all about *that* box.

Worth being blunt about, because "points at a remote AI box" invites the
reading that the local install is just a UI. It is not, and somewhere you would
not put an agent is not somewhere to put this.

Two shapes follow:

**Models elsewhere.** A strong machine running openmirror, models on a separate AI
box (Perch, an Ollama on the LAN) or a provider API. Best answers, and the
local box spends nothing on inference. This is the common case and it is what
`OPENMIRROR_LOCAL_ONLY` exists to *constrain* — set it and a hosted provider is an
error at route resolution, never a quiet fallback.

**Everything on one box, 16 GB+.** One machine, local models only, nothing
leaving the room: Ollama, whisper, Kokoro, the vector store and the daemon
together. Supported, and 16 GB is a real floor rather than a comfortable one —
a floor chat model is ~6 GB resident, `Qwen3-Embedding-4B` ~2.5 GB, whisper
`base` plus Kokoro ~1.9 GB, the daemon and store ~1 GB: about 11.5 GB with
everything warm. Headroom on 16 GB, none at all on 8. Voice is the first thing
to drop, and the browser and desktop extras are the next — they are separate
installs (`[browser]`, `[desktop]`) precisely so a small box need not carry
them.

## Verified against real hardware

Everything above has been run end to end against a live Perch — all five
services, on one connection:

| | |
|---|---|
| chat | `gemma4:12b` drove the agent loop: read a file, proposed a write, waited for approval, wrote it |
| dictation | whisper transcribed real speech verbatim |
| speech | Kokoro synthesised the reply, 24 kHz, first chunk at ~0.8 s |
| duplex | real speech interrupted a real reply mid-sentence — **zero bytes** went out for the cancelled utterance afterwards |
| memory | remembered a fact, recalled it, and put it in front of the model |
| browser | `gemma4:12b` opened a page, read it and reported its buttons |
| money | told to "place the order, just do it, do not ask me" in **unrestricted** mode, it stopped for a human |
| desktop | captured a 2560x1440 Wayland screen and answered a specific question about it correctly |
| MCP | called a tool on a live MCP server; the untrusted-server floor held |
| rewind | overwrote a file, then put it back from the UI |

And the newer half, on the same machine:

| | |
|---|---|
| booking | `gemma4:12b` opened a hotel site in a real Chromium, typed a city and two dates, submitted the form, read the results and reported three hotels with correct prices — **26 s**, first try, with the toolset narrowed to `browser` + `web`. Given all thirty-odd tools the same run failed outright, so the narrowing is part of the result rather than a detail of it |
| your mouse | throughout that, on a virtual stage, the real display's pointer did not move. The test asserts it |
| the real web | the same browser, on the same private display, loaded google.com and found its twenty interactive elements |
| autopilot | a goal, a live view at ~1.4 fps, 27 JPEG frames in 19 s, and a Stop button that stopped it |
| money | "Book this room — pay now" graded a purchase, and refused in **every** mode including `unrestricted` |
| studio | an A1111-shaped server's samplers, schedulers, upscalers and LoRAs came back as real controls; a generation went through with the settings the form had set, stored with the seed the backend used |
| connections | Groq-shaped connection added, tested and removed at runtime; the Tor probe found the C daemon on 9050 and said which implementation it was |
| model choice | an install with an embedding model pulled alongside a chat one picked the chat one — see below |

Three things that only a live model found, all now fixed and pinned by tests:

* **`<input type="date">` cannot be typed into.** Keystrokes go to whichever
  of its three segments has focus, and `2026-10-14` became `61014-02-02` — a
  date the form accepted and echoed back. Segmented inputs are set rather
  than typed; ordinary text still gets keystrokes, because that is what an
  autocomplete listens for.
* **The first model in a list is not necessarily one that can talk.**
  `/api/tags` listed `qwen3-embedding:4b` first, so every conversation came
  back as an HTTP 400 saying that model does not support chat — which reads as
  a broken daemon rather than a bad default. Models are now chosen by declared
  capability, and an agent session prefers one that can call tools.
* **A model that has lost the thread will call a harmless tool forever.**
  Thirty identical calls, 59,000 tokens of input, and an answer saying it
  could not browse. Identical calls are now counted per turn: the first few
  run, and a loop is stopped with a tool result that says so — visibly, so a
  turn that stops does not simply go quiet.
* **A program on PATH is not a program that is installed.** On the machine
  this was built on, `rust-analyzer` resolved — to rustup's proxy, which
  rustup links for every tool it knows about and which exits at once when the
  component is missing. The language-server tool was offered and failed on
  first use. A rustup proxy is now asked whether anything is behind it.
* **An id is not unique because it is called one.** Ollama numbers tool calls
  per response, so every response says `call_1`. Harmless while calls ran one
  at a time; the moment two subagents waited on approvals at once, two
  questions were filed under one answer. The session now makes every id it
  hands out its own.
* **`**/` includes the folder you are in.** fnmatch reads `**/*.py` as "a
  directory, a slash, then something ending in .py", so a file at the top of
  the search never matched — and the first thing gemma4:12b tried, in a folder
  holding two Python files, was exactly that pattern, which told it there were
  none. `glob` now tries the pattern with the `**/` taken out as well.
* **A reply can be empty.** gemma4:12b through Ollama sometimes answers a tool
  result with nothing — no words, no call, only reasoning, into which its
  template leaks `<|channel>thought` — and the turn used to end there,
  silently, with the work not done. It is now told once and asked again. That
  turns some of those silences into work, not all of them: with reasoning off
  it stops falling silent but loops and ticks off steps it has not done, so
  reasoning stays on and the nudge is a mitigation rather than a fix.

## What is not built yet

**Employee management is not integrated.** There is no BambooHR, Gusto, Rippling
or Workday connector, and no time-off request workflow, and that is a decision
rather than an oversight worth hiding: every one of those is a vendor's own
API with its own scopes and its own rate limits, and a wrong guess at any of
them is a system that reads a payroll. What *is* built is the part that does
not need a vendor — the memory store can already hold "how they answer a time
off request" as an ordinary remembered fact, and the approval policy already
grades the irreversible half. When a connector is added it plugs in behind the
same tool, and the memory it reads is already there.

**Mail is IMAP and JMAP, not OAuth.** Which means app passwords rather than
delegated access, so it cannot read a mailbox it was not given a password for,
and cannot revoke itself. The cost is real: an app password is a long-lived
secret in a file, and rotating it is a manual job. The alternative is three
OAuth dances, three token refreshers and three client libraries, and a vendor
that renames an endpoint then breaks the feature for everyone on it.

**Calendar subscriptions are fetch-and-merge, not sync.** A subscribed
calendar is downloaded and merged on its `UID`, so an event *removed* upstream
survives until the cache is deleted. Making it match would need a sync token
per host, and a wrong token is a calendar that quietly stops updating. Local
files are never touched: `propose` returns `.ics` text and stops, because an
agent that can put an event into a calendar can put one in at three in the
morning on a Sunday.

**Bookings and travel are still the browser.** There is no hotel API, no
flight search and no checkout of its own. The harness will book a hotel to the
point of paying, in a real browser, and the final button is refused in every
mode — see [docs/AUTONOMY.md](docs/AUTONOMY.md). That is deliberate for now:
the only part worth automating past "fill in the form" is the part that spends
money, and a vendor API for it would be a much larger thing to get right than
a click on the page the hotel already has.

Video generation still needs a ComfyUI workflow template per model, and none
ship — but bringing one is now a command rather than an editing job: export
your graph with **Save (API format)**, import it, and the prompt, seed, size
and sampler settings are found and turned into controls, with a list of what
it found so you can see whether it worked. None ship because a template names
the checkpoints and custom nodes *your* ComfyUI has; one written here would
fail at the queue with an error about a missing key.

Anthropic, OpenAI, Voyage, Google and the OpenAI-compatible gateways are
written against their documented APIs and have not been exercised against a
live key here — Perch is what this machine has. The realtime adapter is in
the same position, and it is the one with the least margin for error, since
its event names moved between the beta and GA and only one of the two
spellings can be checked without a key.

**There are no accounts, and there are not going to be.** openmirror is a client —
you install it on a machine you control, the way you install `claude-code` or
`codex`, and the person at the keyboard is the person it works for. Everything
belongs to one user id because there is one user.

That is not a smaller ambition, it is a different axis. Scale here is never
"more users"; it is **one person, for years**, with a memory that is meant to
hold basically everything about them. The store therefore grows without a
natural ceiling, and the brute-force scan in `openmirror/memory/store.py` has a
measured limit that the intended product walks through rather than approaches:
comfortable at a few thousand memories, 53 ms at ten thousand, 283 ms at fifty.
Two fixes are identified and neither is built — a keyed projection to a narrow
fixed width, which is what keeps a large store loadable, and then a scan in C.
The file carries the numbers and the order.

That is a decision rather than a gap, and it is worth stating because the code
looks like it is waiting for accounts and is not: the memory store is keyed by
user and its vectors are scrambled with a per-user key. Both stay. The keying
earns its place with one user — a stolen database is still not a pile of
embeddings anyone can run through a public model to recover the text, which is
the property that matters when the database is a file on somebody's laptop.
The cross-user half of the argument is simply not load-bearing here.

**Small models are the real constraint on the autonomous modes.** gemma4:12b
does a five-step browser task reliably when the tool list is narrowed to what
it needs, and wanders when it is not — in autopilot, where nobody is answering
it, it wandered off to Google and then produced an unrelated refusal. The
harness held: the loop guard stopped it, the frames kept streaming, Stop
stopped it. But "fully autonomous" is a claim about the model as much as about
the harness, and on a 12B it is not yet true.

One known rough edge: a 12B model quotes `read_file`'s line-number gutter back
at you when asked what a line says. Three prompt variations did not stop it —
it is copying what it sees. The gutter is kept anyway, because `3│` cannot be
mistaken for real leading whitespace in an edit and `3⇥` can.

---

## Running it

**On a machine you sit at, install the app.** Every
[release](https://github.com/notquiteog/openmirror/releases) has installers for
Windows, macOS and Linux, and each carries the daemon inside it — no Python,
no terminal, nothing else to install. It is a window that lives in the tray
when you close it and tells you when the agent is waiting on you; the daemon
runs for as long as the app does. See [desktop/README.md](desktop/README.md).

**From source, or on a machine with no screen** — a server, a GPU box — run the
daemon by itself. Linux, macOS and Windows. Python 3.11+.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env      # then edit it — it is read at start-up
.venv/bin/openmirror
```

The optional extras, none of which an install needs to be complete:

```bash
pip install -e '.[browser]'   # Playwright, then: playwright install chromium
pip install -e '.[desktop]'   # seeing and driving a screen
pip install -e '.[tor]'       # routing a connection through a SOCKS proxy
```

A screen of the agent's own additionally needs `Xvfb` from your package
manager — `apt install xvfb`. Without it, desktop control falls back to the
screen you are looking at and says so in the log, because an agent that cannot
see anything is not a safer agent, it is a useless one.

No domain and no certificate: `http://127.0.0.1` is a secure context, so the
microphone works on plain HTTP. To reach it from another machine, forward the
port over SSH rather than publishing it — see
[docs/INSTALL.md](docs/INSTALL.md).

Then open <http://localhost:8477>.

```bash
curl -s localhost:8477/healthz
```

Bound to loopback with no token by default, which is deliberate: this process
runs commands on your machine. Set `OPENMIRROR_TOKEN` before moving it.

## Approval modes

| mode | runs without asking |
|---|---|
| `read_only` | reads. Writing tools are not even offered to the model. |
| `plan` | reads, until you approve its plan. `propose_plan` asks you, and your answer picks the mode it carries on in. |
| `ask` *(default)* | reads |
| `auto_edit` | reads, file writes |
| `trusted` | reads, writes, commands, network |
| `unrestricted` | everything |

`unrestricted` exists because the alternative is people approving everything
by reflex, which is worse: a policy answered without being read is a policy
that does nothing while claiming to. `ask_user` is never auto-answered, in any
mode.

Note what is in none of those rows: **purchases, credentials, and sending
mail**. They are not on that ladder at all — they are their own axes, and all
three are *on*, because a harness meant to finish a real task has to be able
to reach the end of one. It can buy things. It can type a password, a card
number or a one-time code. It can answer your email.

What it cannot do is any of them without you.

A purchase is confirmed **in every mode, including `unrestricted`, and
including an autopilot run nobody is watching**. There is no setting that
skips that prompt — deliberately, because a setting like that is one somebody
turns on during a demo and still has on six months later. Neither is ever
remembered either: "don't ask again" about spending money is the one answer
nobody should be able to give once. `OPENMIRROR_ALLOW_PURCHASES=false` turns a
purchase into a refusal rather than a prompt, for an install that should not
be able to buy anything at all.

**Sending mail is the same shape, and the same reason.** Reading your inbox is
a read and runs everywhere, because an agent that cannot read your mail cannot
answer it. *Sending* is a third invariant: confirmed in every mode, never
remembered, never grantable by a rule. The reason is not the amount of money
involved — it is that the recipient is a person who cannot see this
conversation, cannot consent to it, and cannot take it back. A remembered
approval would be worse here than for a purchase, because it is a fingerprint
of the exact message: a "yes" to one reply to Alice would carry over to the
next one, which is a different message saying a different thing.
`OPENMIRROR_ALLOW_MESSAGES=false` turns a send into a refusal rather than a
prompt.

And a secret never becomes readable by anything else. Not in the approval
prompt, not in the tool result, not in the transcript, and not in the page
read — `browser_read` redacts a field's value *and* its label when the field
holds a secret, which is how a typed password was coming back out. That was
found by the test that now proves it, against a real browser and a real form.
`browser_hand_over` is still there and is still better when you are at the
keyboard: a value the agent never receives cannot leak from anywhere.

## With Perch

```bash
PERCH_HOST=127.0.0.1 PERCH_TOKEN=... .venv/bin/openmirror
```

Perch's services already speak shapes openmirror has adapters for — Ollama and
OpenAI for chat, OpenAI for dictation and speech, A1111 for images, ComfyUI
for video — so there is nothing to translate. Services that are switched off
are left unregistered rather than registered and allowed to fail later.

## With Open WebUI

Two directions, and they are independent:

**Open WebUI as a provider.** Set `OPENWEBUI_BASE_URL`. It serves OpenAI's
shapes at `/api/v1`, so every connection configured over there becomes
reachable through one openmirror entry — including providers openmirror has no adapter
for.

**openmirror as Open WebUI's terminal server.** Open WebUI's `terminals.py` is a
reverse proxy to an external terminal server; openmirror's agent websocket is
meant to be that server.

## Testing

```bash
.venv/bin/python -m pytest tests/ -q
```

1293 tests. Some groups carry more weight than the rest.

The shell risk classifier is tested as the security control it is: every case
is a regression guard, and a change that moves any of them out of
`destructive` is a change that makes an approval prompt lie.

The memory tests prove the claim the design rests on — that scrambling
vectors with a per-user key preserves cosine similarity *exactly*, so the
privacy property costs nothing in recall quality.

The browser tests are the ones standing between an autonomous agent and your
card: every buying-button phrasing and every secret-field name is a case, and
each was written after watching the classifier miss something real.

The git and calendar tests run against a **real** repository and a **real**
`.ics` file, because both are mostly parsers and a parser tested against a
mock is only tested against the mock. The JMAP tests run against a real
`aiohttp` server speaking the actual protocol — that one earned its keep
immediately, finding that a JMAP batch is `[name, args, callId]` arrays
rather than objects, that a failed call comes back inside a `200` response,
and that `Blob/set` wants base64url rather than bytes. Each of those was a
send that would have failed at the last step with nothing about the body in
the message.

The approval tests for mail assert through the real `ApprovalPolicy` rather
than the tool, because the property that matters is not what a send calls
itself — it is that no mode, no rule and no remembered approval can turn it
into silence.

**The frame budget is measured, not asserted.** `test_live_frames.py` drives
a real turn against a real provider in a real browser, on a transcript of 850
turns, and times the frames: median 16.7ms, p99 16.8ms, nothing over 20ms.
It also samples the node count *during* the turn, because the first version
of it passed while measuring two nodes — creating the session over HTTP left
the page to clear the transcript before the turn started, and a test that
trusted the count at the end would have been measuring nothing.
`test_scroll_coalescing.py` lifts the real scroll-follow out of `app.js` and
proves it reads the layout once per frame rather than once per append, which
is the change that number depends on and which a real turn cannot distinguish.

`test_static_modules.py` parses every shipped `.js` file as a module and links
the page graph. That is not ceremony: `mail.js` once imported `send` from
`dom.js` and declared its own `send()`, which is an early error rather than a
runtime one, and the result was `Identifier 'send' has already been declared`
on load — a blank page, from a change that every text-based test passed.

`test_booking_walkthrough.py` is end to end and has no mocks below the
provider: a real Xvfb display, a real Chromium launched onto it, a real
booking form filled in, and then the two assertions the whole desktop design
exists for — that the real display's pointer did not move, and that "Book this
room — pay now" is refused in every approval mode. It skips where the machine
has no Xvfb or no Chromium rather than failing, because the suite has to pass
on a laptop with neither.

The Tor tests pin the proxy rules with no socket and no network: the
precedence, the Arti default, and the diagnosis that names the C daemon's port
before concluding nothing is listening. The failure they prevent is silent —
an operator who moved their proxy and finds half their traffic still dialling
the old port gets no error, only something that does not work.

The companion tests are the odd ones out and are there for a dull reason: the
art is arrays of strings, and a row one character short shifts every pixel
after it, survives review, and draws the wrong shape without erroring. So
every frame is measured.

## Licence and provenance

Apache 2.0. See `NOTICE` for what is derived from Open WebUI and the
conditions that carries — in short, its copyright notice is retained and its
branding is not removed from anything that embeds it.

openmirror contains **no code from Anthropic's Claude Code.** The agent runtime is
an independent implementation, and Anthropic API compatibility is written
against Anthropic's published documentation.

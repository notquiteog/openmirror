# Sessions, worktrees, settings

Three things that are not modes, and that are all about the same question:
**what is still here tomorrow?**

## Conversations

Every turn is written to `data/sessions/`, one JSON file per session plus a
small index in front of them.

* `GET /api/sessions/stored` — every conversation on disk, newest first.
* `POST /api/sessions/{id}/resume` — reopen one. The root, model and toolset
  come from the transcript, not the request: a conversation about one project
  reopened in another is a conversation that will confidently edit the wrong
  files.
* `POST /api/sessions/{id}/fork` — a new session holding the conversation up
  to a message number. The old one is left completely alone and the files are
  shared: a fork is a different *conversation*. `at: 0` is a copy under a new
  name rather than a fork, which is a legitimate thing to want.
* `GET /api/sessions/{id}/export` — Markdown, for pasting somewhere else.

**What is stored, and what deliberately is not.** The messages, the title, the
root, the model, the toolset. Not the tools, not the provider, not the
approval futures — a transcript that serialised a live provider client would be
a file only the process that wrote it could read. Reopening is therefore
*rebuild then replay*.

**What is lost, and says so.** The live state of a turn that was running: an
in-flight tool call, a suspended approval, the queue. A session interrupted
mid-turn comes back to just before that turn with a note in the transcript,
because the alternative is restoring a half-done turn and letting the model
believe it finished.

**Two things are deliberately not kept whole.** A screenshot's base64 is a
hundred thousand characters, so pictures are recorded as a description; a
megabyte of build output is kept to twenty thousand characters and then noted,
because a transcript whose middle has been dropped *silently* is worse than
one that says where it went.

## Worktrees

`OPENMIRROR_WORKTREES=1` and `Worktree` in the sidebar. A second checkout of
the repository, on its own branch, that a session works in.

This is separate, not safe. A worktree shares the repository's history, its
remotes and its `.git`, so a force push in one is the same force push. What it
removes is the *accidental* kind of damage.

It is opt-in twice over: only a git repository, and only when the operator
has said so. A branch nobody asked for is a branch somebody has to clean up.

Removal is refused while a worktree has changes in it, and `--force` is
deliberately not offered. `git worktree remove` refuses too, and that refusal
is the feature.

Worktrees live *beside* the repository rather than inside it — a worktree in
the working tree is a directory the agent's own `grep` and `glob` find,
containing a whole second copy of the project.

## Settings

`.openmirror/settings.json`, `.openmirror.json` and `.claude/settings.json`,
plus `~/.openmirror/settings.json`, plus whatever `OPENMIRROR_CONFIG` names.
Dashes and underscores both work, because people copy settings between tools.

Merged, never replaced — later files add to earlier ones, so a project can add
one setting without restating the six a person set globally. Lists replace,
because a union would mean a project could not take a tool *away*.

### The rule

**A project may narrow permissions and never widen them.**

| setting | a project may |
|---|---|
| `approval_mode` | go *down* the ladder only |
| `allow_purchases`, `allow_credentials`, `allow_messages` | turn off only |
| `local_only` | turn **on** only |
| `*_enabled` | turn off only |

This is an *authority* rule and it is separate from precedence, which decides
values and knows nothing about who may set them. A repository that ships a
settings file is a repository you opened; letting it raise its own safety
settings would be the same hole the hooks have, one layer up.

It bites in a specific way worth knowing: the environment normally beats every
file, and here it does not. `OPENMIRROR_APPROVAL_MODE=unrestricted` alongside
a project's `read_only` gives you `read_only`, because a project lowering
something is a ceiling nothing raises.

An unknown key is **reported, not ignored** — a typo is otherwise invisible,
and somebody who set `approval-mode` and got `approval_mode` would have a
machine that quietly ignored them. `GET /api/sessions/settings` lists what
applied, what was unknown, and what was refused.


## How it is written

One JSON file per session, **appended to**: a header line and then one line
per message. The first version rewrote the whole transcript on every turn,
which for an eight-hundred-message session is 89ms of script on the event
loop — measured, and it was the only long frame in an otherwise clean turn.
Rewriting is also just wrong at scale: a long conversation would spend more
time copying itself than working.

The write happens in a thread, because it is work nobody is waiting for — the
answer has already been given.

Appending means a turn cut off mid-write leaves every earlier line intact and
at worst one short line, which the loader skips. That is a better bargain than
an atomic rename, which pays for the whole file every time in order to protect
the last turn.

# Hooks

Commands that run around a tool call, and can refuse it. Claude Code calls
them hooks, openCode calls them plugins, and both treat them as *the*
extension point rather than a setting — which is the right call, because
everything else about an agent is already covered and this is the one thing
that is yours.

## The one property

```
exit 0      -> carry on
exit != 0  -> refuse, and the reason goes back to the model
```

There is no exit code, no JSON field and no output pattern that turns a
refusal into an approval. That is the whole design, and it is what makes it
defensible for a **repository** to ship a hooks file at all:

> A project can stop you doing things. It cannot do things on your behalf.

A refusal from the approval policy is final whatever a hook said, and hooks
run *before* the policy — so the only direction a hook has is down. Read
`test_a_hook_cannot_widen_the_policy` if you read one.

That asymmetry is the same one `AGENTS.md` is already under: project files are
instructions to a model, and this is the version that runs code, so it has to
be strictly weaker than your own configuration. It is.

## The events

| event | when | can refuse |
|---|---|---|
| `PreToolUse` | before a tool runs | **yes** |
| `UserPromptSubmit` | before a turn starts | **yes** |
| `PostToolUse` | after a tool ran | no — advisory |
| `Stop` | when a turn ends | no — advisory |

Which are which is one list in `openmirror/agent/hooks.py`, and
`test_every_event_the_docs_say_can_refuse_actually_can` reads this table and
compares. It is here because `UserPromptSubmit` was listed as refusable here
and implemented only for `PreToolUse`, so a hook that stopped a turn was
silently advisory.

`PostToolUse` is advisory because the tool has already run. A linter
complaining about a failed edit is worth hearing about; reporting work that
happened as work that did not would be a worse lie than the extra noise.

An event name this build does not know is ignored rather than refused, so a
hooks file written for a later openmirror still does the parts this one
understands.

## Writing one

Two shapes, because both ecosystems shipped one:

```json
{
  "PreToolUse": [
    { "name": "no secrets", "command": "grep -qiE 'password|api_key' - && exit 1" }
  ]
}
```

```json
{
  "hooks": {
    "PreToolUse": [
      { "name": "no secrets", "command": "grep -qiE 'password|api_key' - && exit 1" }
    ]
  }
}
```

An entry may be a bare string (just the command) and may narrow itself:

```json
{ "command": "...", "tools": ["shell", "write_file"], "pattern": "rm -rf" }
```

* `tools` — only these tool names.
* `pattern` — a regex matched against the call's one-line summary, which is
  what lets a hook watch a *kind* of change rather than a named tool. A regex
  that does not compile matches **nothing**; a broken pattern must not become a
  hook that fires on everything.

The command gets the call as JSON on stdin:

```json
{"event": "PreToolUse", "tool": "shell", "arguments": {"command": "..."},
 "summary": "rm -rf build/", "risk": "execute", "session_id": "...", "cwd": "..."}
```

and these in the environment: `OPENMIRROR_HOOK_EVENT`, `OPENMIRROR_TOOL`,
`OPENMIRROR_SESSION`.

**Write the reason to stderr.** That is what a person reads in the transcript
and what the model is told. Stdout is for output you want passed along.

## Where they come from, and which ones run

| file | trusted | runs |
|---|---|---|
| `~/.openmirror/hooks.json` | yes | as written |
| `hooks.json` or `.openmirror/hooks.json` in the project | **no** | only after you agree |

A project's are read, listed in the Hooks panel, and *not run* until the first
time one would fire — at which point a strip appears above the transcript
with the exact command and two buttons. The answer is remembered for the
session, **per command**: a project cannot get a new hook added tomorrow and
inherit the answer you gave today for a different one.

Set `OPENMIRROR_HOOKS_ALLOW_PROJECT=1` to skip the question. It is off by
default, and the reason is that a hooks file is code you did not write.

## What a hook cannot do

* **Allow anything.** A refusal from the policy is final.
* **Hang the turn.** `OPENMIRROR_HOOKS_TIMEOUT` seconds, default 10, and the
  whole process *group* is killed rather than the shell — a hook's children
  outlive the shell, and abandoning the `await` leaves them running into the
  next turn.
* **Refuse by being slow.** A timeout is reported and treated as silence.
  "Your formatter was slow" and "you may not do this" are different sentences,
  and a wedged hook must not say the second one — it would turn a slow linter
  into a turn that mysteriously cannot proceed.
* **Refuse because it could not start.** Exit 126 (found, not executable) and
  127 (not found) are reported and ignored. A hook file naming a program that
  is not installed is a mistake, and read as a refusal it silently stops every
  call that matches it.

## The first refusal stops the chain

A blocked call has been answered. Running the remaining hooks would run code on
behalf of something that is not going to happen.

## Settings

| setting | default | does |
|---|---|---|
| `OPENMIRROR_HOOKS` | `true` | hooks are read at all |
| `OPENMIRROR_HOOKS_ALLOW_PROJECT` | `false` | project hooks run without asking |
| `OPENMIRROR_HOOKS_TIMEOUT` | `10` | seconds for one hook |

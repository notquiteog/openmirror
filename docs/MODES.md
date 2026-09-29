# The seven modes

Not tabs over a shared workspace. Typing at an agent, talking to one, and
watching one work are different kinds of attention, and a layout that keeps a
text box on screen during a voice call is a layout that tells you to type.

One is on screen at a time, chosen from the rail on the left. Each hands back
what it holds when you leave it — a microphone left open because somebody
clicked away is a microphone left open.

## Code

The one everything else is measured against: a transcript, tool cards with the
risk grade on them, inline diffs, approve and deny, and a receipt at the end
of each turn saying what changed and offering to put it back.

The controls that qualify *the next thing you say* — approval mode, model,
working root, provider — sit under the box you type in rather than in a title
bar three hundred pixels away.

## Talk

One button, and then only your voice.

The button is in the bar and it does the whole thing: switches the app, opens
the microphone, starts listening. A "talk mode" that then needs a second
control to actually start talking is two buttons wearing one coat.

There is no composer here on purpose. Acting on the machine is a tick box, off
by default and decided per call, because consenting to be listened to is not
the same as consenting to have commands run — and the voice gateway enforces
that separately, so the box only decides whether an agent session is attached
at all.

Barge-in works the way it does everywhere else in openmirror: talk over it and the
synthesis is killed server-side, the client drops what it has buffered, and
the history records only what you actually heard.

**It speaks its questions, and says when it is waiting.** This was the mode's
worst failure for a while: only assistant prose was spoken, so when the agent
asked something or stopped for an approval the call simply went quiet, and
somebody not looking at a screen had no way to know the machine was waiting on
them. Both are now said out loud, with the options read out — on a screen they
are buttons, and in a voice call they are the only way to know what answers
are accepted.

The reply goes back to the thing that is waiting rather than starting a new
turn. Answering out loud while it is still reading the question out stops the
speech and keeps the turn, because cancelling there would throw away the work
the answer was about to unblock.

**Nothing is ever guessed.** "Go" and "no" are one phoneme apart, and
"don't do it" contains "do it" — so a refusal wins whenever both appear, and
anything that is neither is repeated back rather than resolved to the likelier
reading. Refusing an ambiguous utterance costs a repeat; approving one costs
whatever the tool was about to do.

**Money and secrets need a word that is not a mishearing.** A bare "yes" will
not spend anything: those need "confirm", which is not a word that turns up in
conversation by accident and is not a plausible mistranscription of "no". It
says so when it asks, not after.

`Ctrl+Shift+T` from anywhere. `Escape` stops whatever is talking.

## Live

OpenAI's realtime API: speech to speech, one model, no text in the middle.

What you gain is latency and prosody — the model hears *how* something was
said and answers before a transcript would have existed. What you give up is
choice, and the pane says so: it is one vendor's model, it cannot be pointed
at your own hardware, and everything said in the room goes to them while it is
open. Ordinary Talk routes dictation, thinking and speech separately, so each
of those can stay on your machine.

Its tool calls go through the same approval path as the typed loop. If the
policy says a human decides, the model waits — and it is told, in the tool
result, that it is waiting on a person, so it says so out loud rather than
going quiet.

## Autopilot

A goal, and a window. No composer, no transcript, no tool cards: a live view
of a screen it is driving and one line of what it is doing.

It gets a screen of its own by default — see [AUTONOMY.md](AUTONOMY.md) — so
you keep using your computer while you watch. Running on *your* screen is
possible and has to be asked for in words; the refusal happens before anything
moves.

The picture and the narration come down separate channels, which is not an
accident. The narration is small, ordered and replayed to a client that
reconnects; the video is large, continuous and worthless five seconds later.
On one channel you would either store an hour of stale frames to replay, or
lose the record of what happened when someone reloads the page.

If it stops to ask something, the question appears in Code, with the full
context around it.

## Studio

Images and video, with every control the backend actually has.

The panel is drawn from what the server says: ask an A1111 and its own
samplers, schedulers, upscalers and LoRA names come back as real dropdowns,
each with a sentence explaining what it does. Nothing in the client knows what
a sampler is, so installing one on the diffusion box adds it here with no
release in between.

"Everything else" is a fold, not a lock. Every parameter is present and
settable; the ones you need most days are open and the other twenty are one
click away. Hiding one behind a preference would mean the person who wants it
has to find out it exists first.

Video is a job rather than a request — minutes, not seconds — so it starts,
reports how long it has been going, and can be stopped. There is no progress
bar because ComfyUI behind Perch exposes polling and nothing else, and a
percentage would be a fiction.

Every result is stored as a file plus a JSON sidecar holding the prompt, the
seed the backend *actually used*, and every setting. A picture you cannot
reproduce is a picture you cannot iterate on.

### Bringing a ComfyUI workflow

None ship, because a template names the checkpoints and custom nodes your
ComfyUI has. Bringing yours is one command — see
[openmirror/providers/workflows/README.md](../openmirror/providers/workflows/README.md).
The prompt, seed, size and sampler settings are found and turned into
controls, and what it found is reported back so you can see whether it worked.

## Search

The agent's search backend, without the agent.

Worth having for a reason that is not obvious: that backend is configured
once, with a key, and is usually better than a browser tab — a self-hosted
SearxNG, or a Brave key that does not track you. Looking something up should
not have to cost a model turn.

Results can be read in place, through the same fetch the agent uses — so the
private-address guard applies to a person clicking a link, not only to a model
following one.


## Mail

Your inbox, opened without leaving what you were doing. Folders with unread
counts, a list, a reader, and a composer.

It is a mode rather than a dialog for the reason the others are: reading a
message is a different kind of attention from writing to an agent about it,
and switching to it should not be possible halfway through a reply.

Three things it does that a mail client does not:

* **The agent reads the same inbox.** The `mail` tool and this pane are two
  doors onto one store, so "what came in today" costs you nothing and "draft
  a reply to the Acme thread" costs the agent one turn. Neither is a copy of
  the other, and nothing is cached twice.
* **"Write it for me" fills the composer and stops.** It sends the one
  message being answered to whichever model this install routes chat to, and
  puts a draft in a box that is already open, already addressed and already
  subject-lined. Sending is a separate button on purpose — see
  [AUTONOMY.md](AUTONOMY.md).
* **Listing a folder does not mark it read.** The server uses `BODY.PEEK` and
  the `$seen` keyword is only touched when you open a message, so the unread
  count is still true after you have looked through the list.

## Where the commit bar lives

Not a mode — it is in the Code header, and it only appears when your working
root is a git repository with something in it. It counts *changed files* rather
than lines, because that is the number that answers "is there anything to do
here", and a diffstat of a reformat is large and says nothing.

The two buttons are the design. "Write it for me" reads the **staged** diff —
what a commit would contain, not everything that changed, and usually not the
half that does not compile yet — and puts a draft in the message field.
"Commit" sends the message in the field at that moment, which may be that
draft, an edit of it, or something typed from nothing.

Nothing is staged for you. `git add` is a checkbox per file and there is a
button for all of them, and both of those are things you press.

## Updates, the context meter, and `@`

Not modes, and not a tab. Three small things that live in the places you would
look for them.

**The update dot** is on the Updates tab in Settings, and nowhere else. A
banner would be more noticeable and would also be dismissed without being
read, which for a message that says "a new version exists" is the most likely
outcome. The tab dot says the same thing without taking anything away, and
Settings → Updates is where the release notes are.

**The context meter** is under the composer, beside the thing that would
change it. The number is exact; the bar appears only when there is a
denominator. It fills towards the point the conversation will be summarised
rather than towards the model's window, because that is the one the session
will actually hit. Clicking it is the same as typing `/compact`.

**`@` in the composer** opens the files in the working root, ranked. Enter
completes a partial name and sends a finished one — the same rule the slash
menu uses and for the same reason: `@app` then Enter means the file, and
`@src/app.js` then Enter means send it. Escape closes the list and keeps the
text.

## Review, and the queue

Neither is a mode, and both are worth a paragraph because each is a decision
rather than a place.

**Review changes** is beside Rewind in the rail, not inside it. They are
different acts: rewind throws a whole turn away, review keeps the parts you
agree with. Every block in the diff is a choice, and the button says how many
hunks in how many files it is about to put back before you press it.

The file is rebuilt from the turn's recorded before- and after-content rather
than patched. That is why applying a second choice cannot land in the wrong
place — a patch applied to a file an earlier patch already changed is a patch
applied to text that is no longer what it was computed against.

If a file was edited by hand after the turn, the review says so and the apply
is refused until you confirm: those hunks are not what is in the file, and
writing over it would lose the edit.

**The composer takes a message while a turn is running**, and holds it until
that turn finishes. The send button stays on screen and says what it will do;
before, it was replaced by Stop, so the server could queue but nothing could
reach it. An interrupt still discards the queue — cancelling is cancelling.

# Running it autonomously

openmirror will do whatever you configure it to do. This page is about what the
switches actually mean, because three of them are not the same decision even
though they look adjacent.

## The three axes

**`OPENMIRROR_APPROVAL_MODE`** is about *this machine*.

| mode | runs without asking |
|---|---|
| `read_only` | reads. The writing tools are not even offered to the model. |
| `ask` *(default)* | reads |
| `auto_edit` | reads, file writes |
| `trusted` | reads, writes, commands, network |
| `unrestricted` | everything above, including destructive |

**`OPENMIRROR_ALLOW_PURCHASES`**, **`OPENMIRROR_ALLOW_CREDENTIALS`** and
**`OPENMIRROR_ALLOW_MESSAGES`** are not on that scale, and deliberately so.
`unrestricted` means *stop asking me about this machine*. It is not the
sentence *spend my money*, and if one switch bought both, everybody who wanted
the first would get the second by accident.

So no approval mode auto-runs a purchase, and none auto-sends mail. You turn
each on separately, in full knowledge of what you are turning on.

### What sending mail is, and why it is its own axis

Reading an inbox is `read` and runs in every mode — an agent that cannot read
your mail cannot answer it, and that is the entire feature. *Answering* is
`Risk.MESSAGE`, and it behaves exactly like a purchase:

* **Never automatic, in any mode, including `unrestricted`.** The reason is
  not that it is as dangerous as spending money. It is that the recipient is
  a person who cannot see this conversation, cannot consent to it, and cannot
  take it back. A purchase is recoverable in a way a message is not.
* **Never remembered.** A remembered approval is a fingerprint of the exact
  arguments, so a "yes" to *reply to Alice about the invoice* would carry over
  to the *next* message to Alice — a different message, saying a different
  thing, composed by a model that has since read different mail. What was
  agreed to was one message, not a channel to a person.
* **No rule can allow it.** A rule is a reasonable thing to write in order to
  stop being asked about `npm test`; read as a mail exemption it is a line
  that removes the only prompt between an unattended agent and somebody's
  outbox. A rule may still *deny* — it can take freedom away, never hand it
  out.
* **The approval prompt is the review.** Recipient, subject, and the opening
  of the body, in that order, because that is the order mistakes are made in.
  A summary that says "send a message" is not a review.

`OPENMIRROR_ALLOW_MESSAGES=false` turns a send into a *refusal* rather than a
prompt, for an install that should not be able to put words in somebody's
inbox at all. Nothing to click is a stronger guarantee than a yes/no whose
default is no.

### The two AI buttons, and why they are two buttons

"Write it for me" in the Commit bar and in the Mail pane both do the same
thing: read the thing being worked on, send it to whichever model this install
routes chat to, and put a draft in a field a person can read. Neither commits
and neither sends. There is no `ai: true` that drafts *and* does the thing, on
purpose — a flag like that gets set once and then nobody reads the messages
again, and a commit message is the one artefact of a piece of work that
outlives it.

What reaches a provider is the minimum that answers the question. A draft reply
sends the one message being answered, not the account's history and not any
other thread: the difference between a provider seeing one message and seeing
an inbox. A commit draft sends the *staged* diff, not everything that changed,
because a person about to commit three of eleven changed files should be shown
a description of three files.


## Confinement

`OPENMIRROR_UNCONFINED=false` (the default) confines the **file tools** to the
workspace: `read_file` and `edit_file` refuse a path outside it, and refuse
`..` and symlinks out, because they resolve before they compare.

**The shell cannot be confined.** What a command touches is decided at
runtime; no string inspection changes that. What openmirror does instead is refuse
to *grade* it as harmless: a command naming a path outside the root is
escalated from `read` to `execute`, so under the default policy it stops and
asks rather than running silently. That is honesty about the boundary, not
enforcement of it.

If you need actual containment, it has to come from the OS — a container, a
namespace, a seccomp profile. openmirror does not pretend to provide it.

`OPENMIRROR_UNCONFINED=true` drops the file-tool checks too and tells the model
plainly that it is not sandboxed, which is worth doing: a model that believes
it is in a sandbox is careless in ways one that knows it isn't will not be.

## Money

Clicking is graded before it happens, from the label, name, id and href of
the element under the cursor — normalised first, so `Place your order`,
`placeOrderBtn` and `/checkout/confirm` all read the same. Anything that looks
like completing a transaction is `PURCHASE`.

Free trials are in that set. They are the most common way an agent commits
someone to a recurring charge, precisely because the button never says "pay".

The classifier is biased toward over-reporting: a false positive costs one
confirmation, a false negative costs money.

## Credentials

The agent cannot type into password, card, CVV, one-time-code or seed-phrase
fields. This is a refusal, not a prompt — and the distinction matters. An
approval prompt would still mean the secret had passed through the model's
context and into the transcript. Instead `browser_hand_over` gives the browser
to you, you type it, and the agent reads no value back out.

This is also why the browser uses a **persistent profile**. You sign in once,
by hand; the agent inherits the session and never needs the password at all.

## What a denial means

For most tools, being refused means the approach was wrong and the model
should find another route. For purchases and credentials that instruction is
exactly wrong, and measurably so: told to place an order and refused, a local
model immediately clicked a different button and then went for the card
fields. It was not being devious — it was following the ordinary denial
message.

So a refusal on money or secrets is terminal for that goal. The turn carries
on with everything else.

## The thing that has no defence

**Prompt injection.** Anything read from the web is written by someone who is
not you, and it can be as fluent as you are. openmirror labels fetched content as
data from a named source and tells the model it is not addressed to it. That
is a mitigation, not a fix; there is no reliable fix.

What actually protects you is that consequential actions need a human. Turn
that off — `unrestricted`, purchases on, over content you did not write — and
the protection is gone. That combination is available because you asked for
it, and it is the one configuration worth thinking twice about.


## Desktop control is the weak spot

Turning on `OPENMIRROR_DESKTOP` gives the agent the screen and the mouse. It is the
widest capability here and it has the thinnest safety net, for a reason worth
understanding before you use it unattended.

In the browser, a click is graded by **reading the thing being clicked** — its
label, its name, its href. That is what makes "Place your order" recognisable
as a purchase *before* the click happens. On a raw desktop there is nothing to
read: a bitmap and a pair of coordinates, and no amount of care turns those
into "this button charges your card".

Three weaker things stand in:

* **A click must declare what it is clicking.** The model passes a label and
  that label is graded exactly as a browser element would be. It has no
  incentive to misdescribe its own action, and you see both the label and the
  coordinates in the prompt.
* **Coordinates go stale.** A click is refused unless a screenshot was taken
  in the last 45 seconds. A notification sliding in is enough to move what is
  under a point.
* **No desktop click is ever graded `read`.** The browser can auto-run clicks
  under a permissive policy because it verified them first. Here the floor is
  `execute`, always.

If a task can be done in the browser, do it there.

## Watching it work

Every screenshot the agent takes is rendered inline in the transcript, and a
desktop click draws a marker on the frame it was looking at when it decided.
So a run can be reviewed after the fact as a sequence of *what it saw* and
*where it went*, rather than as a sequence of sentences it wrote about what it
did.

That distinction was not academic. An early version showed the screenshot to
the human but never sent it to the model, which then described the screen
fluently and entirely wrongly. `display` is what the UI renders; `images` is
what the model receives; they are separate fields precisely so that failure
cannot recur silently.

---

## Running unattended, on a screen

Autopilot is the mode where the questions on this page stop being theoretical:
there is a goal, there is a screen, and there is nobody answering. Four things
are worth knowing before you start one.

**It refuses to take your screen unless you say so in words.** The check
happens before anything moves, so "this would take your mouse" is something
you are told rather than something you discover when the cursor jumps. Without
Xvfb installed there is no screen it can have to itself, and the run is
refused rather than quietly borrowing yours.

**The approval policy is the same one.** There is no separate autopilot mode
in the policy and there must not be — the temptation to add one is exactly how
an agent ends up with permissions nobody granted it, in a mode nobody was
watching. What autopilot changes is the *prompt*: it is told to finish rather
than to check in. `trusted` is the honest default for it, and it still stops
for anything destructive, for money and for secrets.

**Money still stops.** Being unattended does not relax that; it makes it more
important. An autopilot run *can* buy things and *can* type card details —
those are capabilities it has — but neither happens without you saying yes to
that specific step. A run that reaches a checkout waits, indefinitely, for a
person, and if nobody comes, nothing is bought. There is no mode and no
setting that changes it.

**`ask_user` is still there, and the prompt tells it to use it.** A genuinely
stuck agent — a login it cannot complete, a choice only you can make — should
suspend rather than guess. Guessing at an irreversible step on somebody's
behalf is worse than waiting until they are back.

The honest limit is the model. A 12B model completes a five-step browser task
reliably when the tool list is narrowed to what it needs, and wanders when it
is not; unattended, with nobody to correct it, it wandered off the task
entirely. The harness held — the loop guard stopped it, the frames kept
streaming, Stop stopped it — but "fully autonomous" is a claim about the model
as much as about the harness. Narrow the toolset, and watch the first few.

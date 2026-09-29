# Leave

Requests, the decisions on them, and what the history says about a new one.

## Why this is a ledger and not a connector

Every employee-management system — BambooHR, Gusto, Rippling, Workday, Personio
— is a vendor's own API with its own scopes, its own rate limits and its own
ideas about what a "request" is. A wrong guess at any of them is a system that
can read a payroll, and the guesses do not age well: they break when a vendor
renames a field, and they break *silently*, which is the worst way.

What is portable is the part that is not the vendor's: **the decisions you
have made, why you made them, and the policy you said out loud.** That is a
ledger, and it is yours.

So nothing is sent anywhere. There is no sync, no token to paste, and no
company data leaving the machine. A test asserts that — no HTTP client
imported, no URL, no credential-shaped string in the store.

An install that *does* want to reach a real HR system should do it through a
[hook](HOOKS.md): a command you have agreed to, pointed at your own
integration. That is the right shape for it, because a hook can only ever
stop something — and a payroll export is not something that should be able to
refuse and equally is not something this should be able to do on its own.

## The tool

`hr` — `people`, `add_person`, `request`, `pending`, `decide`, `assess`,
`policy`.

```json
{"action": "assess", "name": "Priya", "kind": "holiday", "days": 14}
```

## The assessment is evidence, not a verdict

This is the whole design, and it is worth being blunt about.

`assess` returns **facts**: the closest past decisions with their outcomes
*and their reasons*, how much notice the person gives, what is already booked,
and the policy on file. Then it stops.

It does not return a percentage. "73% likely" from nine requests is a number
that looks like knowledge and is not, and somebody will act on it. It returns
a **band**, and only when there is enough history for one to mean anything:

| band | when |
|---|---|
| `clear` | at least four comparable decisions, at least 75% approved |
| `coin-flip` | at least four, and it was mixed |
| `unlikely` | at least four, at most 25% approved |
| *(none)* | fewer than four comparable decisions |

Four is not a statistical threshold. It is the point below which a count of
decisions about one or two people is a *record*, not a pattern.

**The judgement is the model's.** The tool gathers facts and the model reasons
about them — the same division this project uses everywhere else. A tool that
answered "approve this" would have taken a decision that belongs to the person
using it, out of a record of a handful of decisions, and dressed it up as
analysis.

## When it refuses to answer, and why

These are the cases worth knowing about, because the first version got all
three of them wrong and confidently.

**A request longer than anything you have decided before gets no reading.**
Fourteen days for somebody whose longest approved request is three is exactly
the case where the pattern should shut up and the policy file should speak.
The first version reported it as `clear`, because it treated "holiday" as a
category — and every holiday sharing a kind is not evidence about this one.

**Somebody with no record of their own gets no reading either.** Their
comparable requests are *shown*, because they are relevant, and marked as
other people's — because grading a request for a person you have never
decided about by the team's average is the lazy inference this exists to
avoid.

**Three days and a fortnight are different questions.** "Similar" means within
a factor of two, and comparability means the same person *and* a similar
length — not any one of the two.

## Spans are working days

"Ten days off" means ten days off and not ten calendar days, and a policy
written in terms of one is not satisfied by the other. Weekends are excluded
and public holidays are not: they vary by country and by year, and guessing
at them here would put a confidently wrong number in front of somebody.

## The policy is read back, not interpreted

```json
{"action": "add_person", "name": "Priya",
 "notes": "more than 10 consecutive days needs cover from someone else"}
```

That text is stored and shown. Nothing parses it. A tool that tried to read
"more than ten consecutive days needs cover" out of English and produce a
verdict would be applying a rule it half-understood, confidently — and the
model reads it the way a person would.

## Recording a decision

`decide` asks for a reason, and that is not decoration. It is the only thing
that makes the next request assessable: **"denied" with no reason teaches the
ledger nothing** and gives whoever reads it later no way to tell a policy from
a mood.

Recording is a write. Nothing here approves or denies anything, and there is
no action that will — the tool can record what a person decided and it cannot
decide.

## Where it lives

`data/hr.db`, SQLite, mode `0600`. It is other people's leave, and a file the
rest of the machine can read is the one thing this must not be.

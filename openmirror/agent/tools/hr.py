"""Leave requests and what the history says about them.

**The division of labour, and it is the important part.** This tool gathers
*evidence* — the comparable past decisions, the stated policy, the notice
given, what is already booked — and the model draws the conclusion. A tool
that returned "approve" would have taken a decision that belongs to the person
using it, out of a record of a handful of decisions, and dressed it up as
analysis. So the assessment here is a set of facts and a band, and the
reasoning is left where the judgement lives.

**The band is not a percentage.** "73% likely" from nine requests is a number
that looks like knowledge and is not, and somebody will act on it. What is
offered is `clear` / `coin-flip` / `unlikely`, and only once there are at
least four comparable decisions — below that the ledger is a record, not a
pattern, and no band is given at all.

**Recording a decision is a write, and an approval is not the agent's to
make.** Nothing here approves or denies anything. It writes down what a person
decided and why, and the reasons are the point: "denied" with no reason
teaches the ledger nothing and gives whoever reads it later no way to tell a
policy from a mood.

**Nothing is sent anywhere.** The ledger is a local file. An install that
wants to reach a real HR system should do it through a
[hook](https://github.com/notquiteog/openmirror/blob/main/docs/HOOKS.md) — a
command the operator has agreed to — rather than through code here guessing at
somebody's API. See `openmirror/hr/store.py` for why that is the shape.
"""

from __future__ import annotations

from typing import Any

from openmirror.agent.tools.base import Assessment, Output, Tool, ToolContext, ToolError
from openmirror.hr.store import Ledger, StoreError
from openmirror.protocol.agent import Risk

ACTIONS = ('people', 'add_person', 'request', 'pending', 'decide', 'assess', 'policy')

RISK: dict[str, Risk] = {
    # Reads.
    'people': Risk.READ,
    'request': Risk.WRITE,      # logging one is a change to the ledger
    'pending': Risk.READ,
    'assess': Risk.READ,
    'policy': Risk.WRITE,       # the stated policy is the person's own words
    # Both of these write down what a *person* decided. The agent never makes
    # the decision, so the grade is a write rather than anything on the
    # purchase axis — but `decide` is the one action here that has
    # consequences for somebody who is not in the room, and the reason is
    # asked for below.
    'add_person': Risk.WRITE,
    'decide': Risk.WRITE,
}


class HrTool(Tool):
    name = 'hr'
    description = (
        "Leave requests, the decisions on them, and what your history says about a new one.\n"
        "action \"add_person\" records somebody and their leave policy in their own words. "
        "action \"request\" logs a request somebody has made. action \"pending\" lists what is waiting. "
        "action \"decide\" records what the person using this decided and why — the reason is the whole "
        "point, and a decision without one teaches the ledger nothing.\n"
        "action \"assess\" gathers the evidence for a request that has not been decided: the closest past "
        "decisions with their reasons, the stated policy, the notice the person gives, and what is already "
        "booked. It returns facts and possibly a band (clear, coin-flip, unlikely) — not a verdict and not "
        "a percentage, and no band at all until there are enough comparable decisions to mean anything.\n"
        "Read the evidence and decide yourself; the pattern is evidence, not an answer. Be even-handed: "
        "these are decisions about real people's time, and a request that is unlike the ones in the "
        "ledger deserves a decision on its own facts rather than a verdict borrowed from a neighbour's. "
        "If the request is for a person who is not in the ledger, say so and ask rather than guessing from "
        "everybody else's history."
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': list(ACTIONS), 'description': 'What to do.'},
            'name': {'type': 'string', 'description': 'The person, for add_person and the others.'},
            'role': {'type': 'string', 'description': 'For add_person.'},
            'joined': {'type': 'string', 'description': 'For add_person: the date they started.'},
            'notes': {
                'type': 'string',
                'description': (
                    'For add_person: their leave policy in their own words. Kept as written and read '
                    'back, not interpreted — a rule read imperfectly and applied confidently is worse '
                    'than no rule.'
                ),
            },
            'kind': {
                'type': 'string',
                'description': 'For request and assess: holiday, sick, parental, unpaid, and so on.',
            },
            'start': {'type': 'string', 'description': 'The first day, YYYY-MM-DD.'},
            'end': {'type': 'string', 'description': 'The last day, YYYY-MM-DD. One day if omitted.'},
            'days': {
                'type': 'number',
                'description': 'Working days. Worked out from the dates when not given.',
            },
            'note': {'type': 'string', 'description': 'For request: what the person said about it.'},
            'request_id': {'type': 'string', 'description': 'For decide.'},
            'outcome': {'type': 'string', 'description': 'For decide: "approved" or "denied".'},
            'reason': {
                'type': 'string',
                'description': 'For decide: why, in the person\'s words. Asked for because it is the '
                               'only thing that makes the next request assessable.',
            },
        },
        'required': ['action'],
    }

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        action = str(args.get('action') or '').strip().lower()
        if not action:
            return Assessment(risk=Risk.READ, summary='', invalid='action is required')
        if action not in RISK:
            return Assessment(
                risk=Risk.READ, summary='',
                invalid=f'{action!r} is not something this does. It can: {", ".join(ACTIONS)}',
            )
        risk = RISK[action]

        if action in ('request', 'assess'):
            who = str(args.get('name') or args.get('person') or '').strip()
            if not who:
                return Assessment(risk=risk, summary='', invalid='name is required')
            if action == 'request' and not str(args.get('start') or '').strip():
                return Assessment(risk=risk, summary='', invalid='start is required, as YYYY-MM-DD')
            return Assessment(risk=risk, summary=f'{action} for {who}')

        if action == 'decide':
            what = str(args.get('outcome') or '').strip().lower()
            if what not in ('approved', 'denied'):
                return Assessment(
                    risk=risk, summary='',
                    invalid="outcome is 'approved' or 'denied' — this records a decision somebody else "
                            'already made, it does not make one',
                )
            if not str(args.get('request_id') or '').strip():
                return Assessment(risk=risk, summary='', invalid='request_id is required')
            return Assessment(
                risk=risk,
                # The reason is in the prompt, because "denied" with no reason
                # is a record that cannot be learned from, and a person
                # approving one is the one moment where being asked why is
                # worth the extra click.
                summary=f'record the decision on {str(args.get("request_id"))}: {what}',
            )

        if action in ('add_person', 'policy'):
            who = str(args.get('name') or '').strip()
            if not who:
                return Assessment(risk=risk, summary='', invalid='name is required')
            return Assessment(risk=risk, summary=f'{action} for {who}')

        return Assessment(risk=risk, summary=action)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        action = str(args['action']).strip().lower()
        try:
            if action == 'people':
                found = self.ledger.people()
                if not found:
                    return Output(
                        content='Nobody in the ledger yet. Use "add_person" to record somebody and their '
                                'leave policy.',
                        display={'people': []},
                    )
                lines = []
                for row in found:
                    bits = [f'{row["name"]}']
                    if row['role']:
                        bits.append(row['role'])
                    bits.append(f'{row["approved"]}/{row["requests"]} approved')
                    if row['notes']:
                        bits.append(f'policy: {row["notes"]}')
                    lines.append(' — '.join(bits))
                return Output(content='\n'.join(lines), display={'people': found})

            if action == 'add_person':
                who = self.ledger.upsert_person(
                    str(args['name']),
                    role=str(args.get('role') or ''),
                    joined=str(args.get('joined') or ''),
                    notes=str(args.get('notes') or ''),
                )
                return Output(
                    content=f'Recorded {who}.',
                    display={'person': who},
                )

            if action == 'policy':
                who = self.ledger.upsert_person(
                    str(args['name']), notes=str(args.get('notes') or '')
                )
                return Output(content=f'The policy for {who} is now on file.', display={'person': who})

            if action == 'request':
                record = self.ledger.add(
                    person=str(args['name']),
                    kind=str(args.get('kind') or 'holiday'),
                    start=str(args.get('start') or ''),
                    end=str(args.get('end') or ''),
                    days=args.get('days'),
                    note=str(args.get('note') or ''),
                    reason=str(args.get('reason') or ''),
                )
                return Output(
                    content=f'Logged {record.kind} for {record.person}, {record.days:g} working days from '
                            f'{record.start}. It is waiting for a decision.',
                    display={'request': record.public()},
                )

            if action == 'pending':
                found = self.ledger.list(pending_only=True, limit=int(args.get('limit') or 100))
                if not found:
                    return Output(content='Nothing is waiting for a decision.', display={'pending': []})
                lines = [
                    f'{r.id}  {r.person}  {r.kind}  {r.days:g}d from {r.start}'
                    + (f'  — "{r.note}"' if r.note else '')
                    for r in found
                ]
                return Output(
                    content='\n'.join(lines),
                    display={'pending': [r.public() for r in found]},
                )

            if action == 'decide':
                record = self.ledger.decide(
                    str(args['request_id']), str(args['outcome']), reason=str(args.get('reason') or '')
                )
                return Output(
                    content=f'Recorded: {record.outcome} for {record.person}'
                    + (f' — "{record.outcome_reason}"' if record.outcome_reason else '')
                    + '.',
                    display={'request': record.public()},
                )

            if action == 'assess':
                found = self.ledger.assess(
                    str(args['name']),
                    str(args.get('kind') or 'holiday'),
                    float(args.get('days') or 0) or _days_from(args),
                    start=str(args.get('start') or ''),
                )
                return Output(content=_read_out(found), display={'assessment': found})
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise ToolError(f'{exc}') from exc

        raise ToolError(f'{action}: not handled')


def _days_from(args: dict[str, Any]) -> float:
    from openmirror.hr.store import span_days

    return span_days(str(args.get('start') or ''), str(args.get('end') or ''))


def _read_out(found: dict[str, Any]) -> str:
    """The evidence, in words, for the model to reason about."""
    lines = [f'{found["person"]} — {found["kind"]}, {found["days"]:g} working days.']
    if found.get('band'):
        lines.append(f'Reading of the history: {found["band"]}. {found["because"]}')
    else:
        lines.append(found['because'] or 'Not enough history to read anything into it.')
    if found.get('policy'):
        lines.append(f'Policy on file: {found["policy"]}')
    if found.get('notice') is not None:
        lines.append(f'Notice this person has given on open requests: {found["notice"]} working days.')
    if found.get('pending'):
        lines.append('Already requested:')
        lines += [
            f'  {r["start"]} to {r["end"] or r["start"]} — {r["kind"]}'
            for r in found['pending']
        ]
    if found.get('evidence'):
        lines.append('The closest decisions:')
        for record in found['evidence'][:8]:
            reason = f' — "{record["outcome_reason"]}"' if record['outcome_reason'] else ''
            lines.append(
                f'  {record["outcome"]}: {record["person"]}, {record["kind"]}, {record["days"]:g}d'
                f' from {record["start"]}{reason}'
            )
    else:
        lines.append('Nothing in the ledger that resembles it.')
    return '\n'.join(lines)


def hr_tools(ledger: Ledger) -> list[Any]:
    """The one tool, when there is somewhere to keep the ledger."""
    return [HrTool(ledger)]


__all__ = ['ACTIONS', 'HrTool', 'hr_tools']

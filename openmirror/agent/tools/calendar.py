"""Scheduling, as one tool.

`agenda`, `availability`, `check` and `propose` are one question asked four
ways — *when can this happen* — so they are one tool with actions rather than
four tools. The toolset comment above `TOOLSETS` records the measurement that
makes that a rule rather than a preference: a 12B model given thirty tools
could not finish a five-step browser task that the same model finished in
twenty-six seconds with six.

**Nothing here writes to a calendar.** `propose` returns `.ics` text and
stops. An agent that can put an event into a calendar can put one in at three
in the morning on a Sunday, and the only guard that has ever worked against
that is a person deciding to. The one exception is a *subscription refresh*,
which overwrites a cache this project owns and changes nothing the person
wrote.

**Conflicts are reported, never resolved.** `check` says what a proposed slot
lands on. Deciding whether a clash matters — a double-booked coffee with
somebody you were going to see anyway is not a problem — is a judgement, and
the tool hands the facts to a model that can be asked about it rather than
making the call.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from openmirror.agent.tools.base import Assessment, Output, Tool, ToolContext, ToolError
from openmirror.calendar.store import CalendarStore, CalendarStoreError, agenda, availability, clash, proposed, to_ics
from openmirror.protocol.agent import Risk

log = logging.getLogger(__name__)

ACTIONS = ('calendars', 'events', 'availability', 'check', 'propose', 'refresh')

RISK: dict[str, Risk] = {
    'calendars': Risk.READ,
    'events': Risk.READ,
    'availability': Risk.READ,
    'check': Risk.READ,
    'propose': Risk.READ,
    # The only one that touches anything, and what it touches is a cache this
    # project owns — never a calendar the person wrote.
    'refresh': Risk.NETWORK,
}

NINE_TO_FIVE = (time(9), time(17))


class CalendarTool(Tool):
    name = 'calendar'
    description = (
        'Look at a calendar, and work out when something can happen. Calendars are .ics files; '
        'list them with "calendars" if you are not sure which to read.\n'
        'action "events" is what is coming, in order. action "availability" is where there is room '
        'in a day — give it a date, and optionally working hours; that is the answer to "when can we '
        'meet", and it is usually a better one than you would give by reading the list.\n'
        'action "check" takes a proposed time and says what it lands on. action "propose" builds a '
        'meeting and returns it as a calendar file to import — it does not put anything in a '
        'calendar, and neither does anything else here. action "refresh" re-fetches a subscribed '
        'calendar.\n'
        'Times are in the person\'s own time zone; say which one you are working in when you propose '
        'something, because "Thursday at 2" is ambiguous and "Thursday at 2 in Europe/London" is not. '
        'Prefer availability over proposing a time yourself, and if everything is booked, say so and '
        'offer the nearest two slots outside working hours rather than silently moving something.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': list(ACTIONS), 'description': 'What to do.'},
            'calendar': {'type': 'string', 'description': 'Which calendar. All of them when omitted.'},
            'date': {
                'type': 'string',
                'description': 'For availability and check: the day, as YYYY-MM-DD.',
            },
            'days': {'type': 'integer', 'description': 'For events: how many days ahead. Default 7.'},
            'title': {'type': 'string', 'description': 'For check and propose: what the meeting is.'},
            'start': {
                'type': 'string',
                'description': 'For check and propose: when it starts, as YYYY-MM-DDTHH:MM.',
            },
            'end': {
                'type': 'string',
                'description': 'For check and propose: when it ends. One hour after the start if omitted.',
            },
            'all_day': {'type': 'boolean', 'description': 'For check and propose: a whole day.'},
            'timezone': {
                'type': 'string',
                'description': 'IANA zone, e.g. Europe/London. Say it; a bare local time is ambiguous.',
            },
            'hours': {
                'type': 'string',
                'description': 'Working hours for availability, as "09:00-17:00". Default nine to five.',
            },
            'minimum_minutes': {
                'type': 'integer',
                'description': 'For availability: the shortest meeting worth suggesting. Default 30.',
            },
            'attendees': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'For propose: who is coming. Free text; the person\'s own client resolves it.',
            },
            'location': {'type': 'string', 'description': 'For propose.'},
            'description': {'type': 'string', 'description': 'For propose: an agenda, or the reason.'},
        },
        'required': ['action'],
    }

    def __init__(self, store: CalendarStore) -> None:
        self.store = store

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
        which = str(args.get('calendar') or '').strip()

        if action == 'availability':
            day = str(args.get('date') or '').strip()
            if not day:
                return Assessment(risk=risk, summary='', invalid='date is required, as YYYY-MM-DD')
            try:
                date.fromisoformat(day)
            except ValueError:
                return Assessment(risk=risk, summary='', invalid=f'{day!r} is not a date — use YYYY-MM-DD')
            return Assessment(risk=risk, summary=f'free time on {day}{f" in {which}" if which else ""}')

        if action in ('check', 'propose'):
            start = str(args.get('start') or '').strip()
            if not start:
                return Assessment(
                    risk=risk, summary='',
                    invalid='start is required, as YYYY-MM-DDTHH:MM, or YYYY-MM-DD with all_day',
                )
            title = str(args.get('title') or '').strip() or 'a meeting'
            return Assessment(risk=risk, summary=f'{action} {title!r} at {start}')

        if action == 'refresh':
            if not which:
                return Assessment(risk=risk, summary='', invalid='calendar is required — which subscription?')
            return Assessment(risk=risk, summary=f'refetch the {which} calendar')

        return Assessment(risk=risk, summary=action)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        action = str(args['action']).strip().lower()
        which = str(args.get('calendar') or '').strip()
        try:
            if action == 'calendars':
                found = self.store.list()
                if not found:
                    return Output(
                        content=(
                            'There are no calendars yet. Put a .ics file in the calendar directory, or '
                            'subscribe to one. Ask the person where they want them and the path will be '
                            'in the error above.'
                        ),
                        display={'calendars': []},
                    )
                return Output(
                    content='\n'.join(f'{c.id}: {c.name}' for c in found),
                    display={'calendars': [{'id': c.id, 'name': c.name} for c in found]},
                )

            if action == 'refresh':
                from openmirror.calendar.store import Calendar

                calendar = self.store.list()
                match = next((c for c in calendar if c.id == which), None)
                if match is None:
                    raise CalendarStoreError(f'there is no calendar called {which!r}')
                events, changed = await self.store.fetch_subscription(
                    Calendar(id=match.id, name=match.name, url=_subscription_url(self.store, match.id))
                )
                return Output(
                    content=f'{len(events)} event(s) on {match.name}' + ('' if changed else ' — nothing new'),
                    display={'calendar': match.id, 'events': len(events), 'changed': changed},
                )

            events = self._events(which)

            if action == 'events':
                start = datetime.now(UTC)
                when = _date_only(args)
                found = agenda(events, start=start, days=int(args.get('days') or 7))
                if when:
                    found = [e for e in found if e.start_at.date() == when]
                if not found:
                    return Output(content='Nothing in that window.')
                return Output(
                    content='\n'.join(e.describe() for e in found),
                    display={'events': [e.public() for e in found]},
                )

            if action == 'availability':
                day = date.fromisoformat(str(args['date']))
                hours = _hours(args)
                slots = availability(
                    events,
                    day=day,
                    hours=hours,
                    minimum_minutes=int(args.get('minimum_minutes') or 30),
                    zone=_zone(args),
                )
                if not slots:
                    return Output(
                        content=(
                            f'No room on {day} between {hours[0]:%H:%M} and {hours[1]:%H:%M}. Say so, '
                            f'and offer another day rather than a time that is already taken.'
                        ),
                        display={'day': day.isoformat(), 'slots': []},
                    )
                return Output(
                    content=f'Free on {day} between {hours[0]:%H:%M} and {hours[1]:%H:%M}:\n'
                    + '\n'.join(f'  {s["start"][11:16]} to {s["end"][11:16]}' for s in slots),
                    display={'day': day.isoformat(), 'slots': slots},
                )

            event = self._proposed(args)
            clashes = clash(event, events)
            if action == 'check':
                if not clashes:
                    return Output(
                        content=f'{event.summary} at {event.start} is free — nothing on any calendar.',
                        display={'free': True, 'event': event.public()},
                    )
                return Output(
                    content=f'{event.summary} at {event.start} clashes with:\n'
                    + '\n'.join(f'  {c.describe()}' for c in clashes),
                    display={'free': False, 'clashes': [c.public() for c in clashes]},
                )

            text = to_ics(event)
            return Output(
                content=(
                    f'Here it is as a calendar file, {len(text)} characters. It has not been added to '
                    f'anything — the person imports it.\n\n' + text
                ),
                display={'ics': text, 'event': event.public(),
                         'clashes': [c.public() for c in clash(event, events)]},
            )
        except CalendarStoreError as exc:
            raise ToolError(str(exc)) from exc
        except ValueError as exc:
            raise ToolError(f'{exc}') from exc

        raise ToolError(f'{action}: not handled')

    def _events(self, which: str) -> list[Any]:
        if which:
            return self.store.read(which)
        calendars = self.store.list()
        if not calendars:
            raise CalendarStoreError(
                'there are no calendars yet. Ask the person to put a .ics file in the calendar '
                'directory, or to subscribe to one, rather than guessing where it should live.'
            )
        found: list[Any] = []
        for calendar in calendars:
            try:
                found.extend(self.store.read(calendar.id))
            except CalendarStoreError as exc:
                # One unreadable calendar must not hide the readable ones —
                # logged rather than raised, because a corrupt file in a
                # folder of twenty should not cost you the other nineteen.
                log.warning('calendar %s skipped: %s', calendar.id, exc)
        return found

    def _proposed(self, args: dict[str, Any]) -> Any:
        raw = str(args['start']).strip()
        if args.get('all_day') or 'T' not in raw:
            day = date.fromisoformat(raw[:10])
            return proposed(
                summary=str(args.get('title') or 'a meeting'),
                start=day,
                end=day + timedelta(days=1) if not args.get('end') else date.fromisoformat(str(args['end'])[:10]),
                description=str(args.get('description') or ''),
                location=str(args.get('location') or ''),
                attendees=[str(a) for a in (args.get('attendees') or [])],
            )
        try:
            start = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f'{raw!r} is not a date-time — use YYYY-MM-DDTHH:MM') from exc
        if start.tzinfo is None:
            zone = _zone(args)
            if zone is not None:
                start = start.replace(tzinfo=zone)
        end = None
        if args.get('end'):
            end = datetime.fromisoformat(str(args['end']))
            if end.tzinfo is None and start.tzinfo is not None:
                end = end.replace(tzinfo=start.tzinfo)
        return proposed(
            summary=str(args.get('title') or 'a meeting'),
            start=start,
            end=end,
            description=str(args.get('description') or ''),
            location=str(args.get('location') or ''),
            attendees=[str(a) for a in (args.get('attendees') or [])],
        )


def _date_only(args: dict[str, Any]) -> date | None:
    raw = str(args.get('date') or '').strip()
    if not raw:
        return None
    return date.fromisoformat(raw[:10])


def _hours(args: dict[str, Any]) -> tuple[time, time]:
    raw = str(args.get('hours') or '').strip()
    if not raw:
        return NINE_TO_FIVE
    match = re.match(r'^(\d{1,2}):?(\d{2})?\s*-\s*(\d{1,2}):?(\d{2})?$', raw)
    if not match:
        raise ValueError(f'{raw!r} is not a range of hours — use "09:00-17:00"')
    return (
        time(int(match.group(1)), int(match.group(2) or 0)),
        time(int(match.group(3)), int(match.group(4) or 0)),
    )


def _zone(args: dict[str, Any]) -> Any:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = str(args.get('timezone') or '').strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(
            f'{name!r} is not a time zone this machine knows. Say one it does, or leave it out and '
            f'ask the person which they are in.'
        ) from exc


def _subscription_url(store: CalendarStore, calendar_id: str) -> str:
    """Where a subscription's URL lives.

    A sidecar file rather than a field in the `.ics` itself: the file is the
    server's bytes, and rewriting it to record our own metadata would make
    every comparison against the source wrong.
    """
    sidecar = store.root / f'{_slug_of(calendar_id)}.url'
    return sidecar.read_text(encoding='utf-8').strip() if sidecar.is_file() else ''


def _slug_of(text: str) -> str:
    from openmirror.calendar.store import _slug

    return _slug(text)


__all__ = ['ACTIONS', 'RISK', 'CalendarTool']

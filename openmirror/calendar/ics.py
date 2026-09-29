"""A calendar, in `.ics`, with the arithmetic that scheduling actually needs.

No dependency, because the format is not the hard part. iCalendar is a
line-oriented text format with a folding rule and a date grammar, and the
whole of what this project needs from it is: read the events, work out when
somebody is free, add one without landing on top of another, and put it back
where the person's own calendar will show it. That is a few hundred lines
rather than a package, and a package would have brought a second opinion about
time zones that this file would then have to argue with.

Four things that are easy to get wrong and are therefore the content of this
docstring:

* **All-day events are dates, not datetimes.** `DTSTART;VALUE=DATE:20260314`
  is the fourteenth, with no time and no zone, and reading it as midnight
  local makes an all-day event look like it is free at nine. It occupies the
  whole day.

* **An exclusive end.** `DTEND` is the first moment *after* the event, so a
  one-hour meeting from 10:00 has `DTEND:20260314T110000`. Treating it as
  inclusive gives back-to-back meetings a one-minute overlap and then reports
  a conflict for a meeting that is not one. The one exception is an
  all-day event, whose `DTEND` is the day *after* the last day, and which is
  handled by the date path rather than the datetime one.

* **Line folding is not optional.** RFC 5545 folds a line at 75 octets with a
  leading space on the continuation, and it folds in the middle of a UTF-8
  sequence as often as not. Unfolding is therefore bytes-first: strip the CRLF,
  drop the space, and only then decode. Decoding first turns a folded emoji in
  a subject into replacement characters.

* **Time zones are kept as written.** A `TZID` is resolved through the system
  zoneinfo database when it is there, and kept as the literal name when it is
  not — a floating or unknown-zone event is still an event, and dropping it
  because the zone database is missing would be worse than being approximate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

# Unfolding: a CRLF (or a bare LF, which half the world's writers emit)
# followed by a single space or tab is one line.
_FOLD = re.compile(r'\r?\n[ \t]')

# `NAME:value;NAME=value:value`. Quoted values and escaped text are handled
# by the small unescaper below rather than by the regex, because a subject
# containing a semicolon is not a parameter list.
_PROPERTY = re.compile(r'^(?P<name>[A-Za-z0-9-]+)(?P<params>(?:;[^:]*)*):(?P<value>.*)$', re.S)


class CalendarError(ValueError):
    """The calendar file is not readable, or the request is not schedulable."""


# ---------------------------------------------------------------------------
# Unescaping, per RFC 5545 §3.3.11
# ---------------------------------------------------------------------------

_UNESCAPE = {'n': '\n', 'N': '\n', ',': ',', ';': ';', '\\': '\\', '/': '/'}


def unescape(text: str) -> str:
    """`\\n\\, \\; \\\\` and friends, which is most of a real subject line."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == '\\' and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt in ('n', 'N'):
                out.append('\n')
                index += 2
                continue
            if nxt in _UNESCAPE:
                out.append(_UNESCAPE[nxt])
                index += 2
                continue
        out.append(char)
        index += 1
    return ''.join(out)


def escape(text: str) -> str:
    """The other direction, for writing a file back out."""
    return (
        (text or '')
        .replace('\\', '\\\\')
        .replace('\r\n', '\\n')
        .replace('\n', '\\n')
        .replace('\r', '\\n')
        .replace(';', '\\;')
        .replace(',', '\\,')
    )


def fold(line: str) -> str:
    """Fold to 75 octets, never in the middle of a character.

    Octets, not characters: the limit is on the encoded line, and a line of
    emoji hits it in about a third of the characters a server will accept. The
    continuation carries one leading space, which is removed on the way back
    in by `_FOLD`.
    """
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    out: list[str] = []
    start = 0
    # 74 for the first chunk, 74 for the rest (75 minus the leading space).
    while start < len(raw):
        width = 75 if not out else 74
        end = min(start + width, len(raw))
        # Back off to a character boundary rather than splitting a UTF-8
        # sequence, which is what makes an emoji in a subject survive.
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        out.append(raw[start:end].decode('utf-8'))
        start = end
    return '\r\n '.join(out)


# ---------------------------------------------------------------------------
# Dates and times
# ---------------------------------------------------------------------------


def _zone(name: str | None) -> Any:
    """A `ZoneInfo`, or None when the name is unknown.

    None means "floating": the time is whatever wall-clock the person is in,
    which is exactly what a `TZID` that is not in the database *means* to a
    reader. Guessing UTC for those is how a nine-in-the-morning meeting ends
    up at two.
    """
    if not name or name.upper() == 'UTC':
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning('unknown time zone %r; treating those times as local wall clock', name)
        return None


def parse_when(value: str, params: dict[str, str], *, all_day: bool | None = None) -> tuple[Any, bool]:
    """A `DTSTART`/`DTEND` value into a `date` or an aware `datetime`.

    Returns (moment, is_date). A `date` for a date-only value, a `datetime`
    for everything else. A naive datetime is *kept* naive rather than being
    assumed UTC, for the reason in `_zone`: a floating time is floating.

    **A value with no `T` in it is a date**, whatever `VALUE` says. That is
    the shape every exporter writes for an all-day event — `DTSTART:20260316`
    rather than `DTSTART;VALUE=DATE:20260316` — and reading it as midnight
    makes an all-day event look free at nine in the morning, which is the
    most expensive way to be wrong about a calendar.
    """
    raw = (value or '').strip()
    if not raw:
        raise CalendarError('a date or date-time is required')
    is_date = all_day if all_day is not None else (params.get('VALUE', '').upper() == 'DATE')

    try:
        if raw.endswith('Z'):
            return datetime.strptime(raw, '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC), False

        if 'T' in raw:
            # A trailing Z was handled; anything else with a T is a local
            # time, made aware only if a usable TZID came with it.
            moment = datetime.strptime(raw, '%Y%m%dT%H%M%S')
            zone = _zone(params.get('TZID'))
            return (moment.replace(tzinfo=zone) if zone else moment), False

        parsed = datetime.strptime(raw, '%Y%m%d').date()
        if is_date:
            return parsed, True
        # No `T` and no `VALUE=DATE`: a date written the short way. The
        # override above is for an explicit `VALUE` on the *other* property,
        # which is how a `DTEND` inherits its `DTSTART`'s shape.
        return parsed, True
    except ValueError as exc:
        # `strptime`'s own wording is about the format string, which means
        # nothing to somebody who typed a date wrong. This says which value
        # and what was expected.
        raise CalendarError(
            f'{raw!r} is not a date or date-time. Expected YYYYMMDD or YYYYMMDDTHHMMSS, '
            f'with a Z or a TZID parameter for a zone.'
        ) from exc


def _as_aware(moment: Any, *, reference: datetime | None = None) -> datetime:
    """A `date` or a naive datetime as an aware one, for comparison.

    An all-day event becomes midnight in the *reference* zone, so that a
    whole-day event on the fifth overlaps a meeting on the fifth morning in
    whatever zone the person is actually in. Converting it to UTC midnight
    would put it on the wrong day west of Greenwich and the wrong day again
    east of it.
    """
    if isinstance(moment, datetime):
        if moment.tzinfo is not None:
            return moment
        zone = reference.tzinfo if reference and reference.tzinfo else None
        return moment.replace(tzinfo=zone) if zone else moment
    zone = reference.tzinfo if reference and reference.tzinfo else UTC
    return datetime.combine(moment, time(0), tzinfo=zone)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Event:
    uid: str = ''
    summary: str = ''
    description: str = ''
    location: str = ''
    start: Any = None  # date | datetime
    end: Any = None
    all_day: bool = False
    rrule: str = ''
    attendees: list[str] = field(default_factory=list)
    organizer: str = ''
    status: str = ''
    busy: bool = True

    @property
    def start_at(self) -> datetime:
        return _as_aware(self.start)

    @property
    def end_at(self) -> datetime:
        """The end, as a moment — never earlier than the start.

        An event with no `DTEND`, or one whose end is before its start, is
        real: a half-typed entry in somebody's calendar, and a recurrence with
        a duration but no end date. Treated as an hour rather than as zero, so
        it occupies something and therefore conflicts with something.

        **An all-day event is a whole day in every branch of that.** Getting it
        wrong here is the trap the module docstring names, and it was live: an
        all-day event whose `DTEND` equalled its `DTSTART` — which is what a
        writer that does not know the end is exclusive produces — fell through
        to the one-hour fallback, so a day-long offsite blocked one hour of
        somebody's day and the rest of it read as free.
        """
        whole_day = self.all_day
        if self.end is None:
            return self.start_at + (timedelta(days=1) if whole_day else timedelta(hours=1))
        end = _as_aware(self.end, reference=self.start_at)
        if end <= self.start_at:
            return self.start_at + (timedelta(days=1) if whole_day else timedelta(hours=1))
        return end

    def overlaps(self, other: Event) -> bool:
        """Whether two events collide.

        Half-open: touching is not overlapping, because a meeting that ends at
        11:00 and one that starts at 11:00 are back to back and not a
        conflict. Half-open is also what `DTEND` already means, so this is
        arithmetic rather than a policy decision.
        """
        return self.start_at < other.end_at and other.start_at < self.end_at

    def describe(self) -> str:
        when = self.start.isoformat() if self.start else '(no start)'
        if not self.all_day and self.end and self.end != self.start:
            when = f'{when} to {self.end.isoformat()}'
        elif self.all_day and self.end:
            when = f'{when} (all day, until {self.end.isoformat()})'
        bits = [when, self.summary or '(no title)']
        if self.location:
            bits.append(f'at {self.location}')
        if self.attendees:
            bits.append('with ' + ', '.join(self.attendees[:4]) + (f' and {len(self.attendees) - 4} more'
                                                                   if len(self.attendees) > 4 else ''))
        return ' — '.join(bits[:2]) + (f' — {" — ".join(bits[2:])}' if len(bits) > 2 else '')

    def public(self) -> dict[str, Any]:
        return {
            'uid': self.uid,
            'summary': self.summary,
            'description': self.description,
            'location': self.location,
            'start': self.start.isoformat() if self.start else None,
            'end': self.end.isoformat() if self.end else None,
            'all_day': self.all_day,
            'recurring': bool(self.rrule),
            'attendees': self.attendees,
            'organizer': self.organizer,
            'status': self.status,
            'busy': self.busy,
        }


def parse(text: str) -> list[Event]:
    """Every `VEVENT` in an `.ics` document.

    Unfolding is bytes-first on purpose — see the module docstring. Everything
    else this touches is a property line, and a property this does not know is
    ignored rather than treated as an error, because a calendar full of
    `X-` properties is a calendar that should still open.

    **Nesting is counted, not assumed.** A `VEVENT` contains a `VALARM` in
    almost every calendar anybody has, and a parser that ends the event on the
    first `END:` it sees loses every meeting that has a reminder on it — which
    is every meeting in a subscribed calendar and nearly all of the ones
    exported from a phone. The event ends when the *matching* `END:VEVENT`
    arrives, at the depth it started at.
    """
    events: list[Event] = []
    current: Event | None = None
    depth = 0
    event_depth = 0

    for line in _FOLD.sub('', text or '').replace('\r\n', '\n').split('\n'):
        match = _PROPERTY.match(line)
        if not match:
            continue
        name = match.group('name').upper()
        params = _params(match.group('params'))
        value = match.group('value')
        kind = value.upper()

        if name == 'BEGIN':
            depth += 1
            if kind == 'VEVENT' and current is None:
                current, event_depth = Event(), depth
            continue

        if name == 'END':
            if kind == 'VEVENT' and current is not None and depth == event_depth:
                events.append(current)
                current = None
            # Not `elif`: a VCALENDAR closes at depth 0 and there is nothing
            # to do for it, but the depth still has to come down or every
            # later record is at the wrong level.
            depth = max(0, depth - 1)
            continue

        if current is None or depth != event_depth:
            # Inside a VALARM, inside a VTIMEZONE, or before the first event.
            # `DTSTART` inside a VTIMEZONE is not this event's start, and
            # reading it as one is how a calendar's recurrence rules end up
            # in the wrong place.
            continue

        if name == 'UID':
            current.uid = value.strip()
        elif name == 'SUMMARY':
            current.summary = unescape(value)
        elif name == 'DESCRIPTION':
            current.description = unescape(value)
        elif name == 'LOCATION':
            current.location = unescape(value)
        elif name == 'DTSTART':
            current.start, all_day = parse_when(value, params)
            current.all_day = all_day
        elif name == 'DTEND':
            current.end, _ = parse_when(value, params, all_day=current.all_day)
        elif name == 'DURATION':
            # A duration and an end are mutually exclusive, and a duration is
            # the common case for something a phone put in.
            current.end = _add_duration(current.start, value) if current.start is not None else None
        elif name == 'RRULE':
            current.rrule = value.strip()
        elif name == 'ATTENDEE':
            # `mailto:` and the display name that precedes it, both optional.
            who = value.split(':', 1)[-1].removeprefix('mailto:')
            if params.get('CN'):
                who = f'{unescape(params["CN"])} <{who}>'
            if who:
                current.attendees.append(who)
        elif name == 'ORGANIZER':
            current.organizer = value.split(':', 1)[-1].removeprefix('mailto:')
        elif name == 'STATUS':
            current.status = value.upper()
        elif name == 'TRANSP':
            # TRANSP:TRANSPARENT is the only way to say "do not block me", and
            # it is how a reminder and a real meeting are told apart.
            current.busy = value.upper() != 'TRANSPARENT'

    # A VEVENT with no matching END is truncated but real — a file written by
    # something that crashed halfway. Kept, because a calendar where the last
    # meeting is missing is worse than one with a malformed trailing entry.
    if current is not None:
        events.append(current)
    return events


def _params(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (raw or '').split(';'):
        if not chunk:
            continue
        key, _, value = chunk.partition('=')
        out[key.strip().upper()] = unquote(value.strip())
    return out


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _add_duration(start: Any, value: str) -> Any:
    """`PT1H30M`, `P1D`, `-PT15M`. Applied to the start, so a duration is
    always consistent with it whatever the end was meant to be."""
    match = re.fullmatch(
        r'(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?'
        r'(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?',
        value.strip().upper(),
    )
    if not match:
        return None
    delta = timedelta(
        weeks=int(match.group('weeks') or 0),
        days=int(match.group('days') or 0),
        hours=int(match.group('hours') or 0),
        minutes=int(match.group('minutes') or 0),
        seconds=int(match.group('seconds') or 0),
    )
    if match.group('sign') == '-':
        delta = -delta
    if isinstance(start, datetime):
        return start + delta
    return start + delta


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def build(
    *,
    summary: str,
    start: datetime | date,
    end: datetime | date | None = None,
    description: str = '',
    location: str = '',
    attendees: list[str] | None = None,
    uid: str = '',
    alarm_minutes: int = 0,
) -> str:
    """A complete, valid `VCALENDAR` with one event in it.

    `VTIMEZONE` is deliberately absent. A `VTIMEZONE` block is a full set of
    historical transitions and writing a wrong one is worse than relying on
    the consumer's own zoneinfo — which is what every real client does, and
    what RFC 5545 permits: a `TZID` is a reference, not an inline definition.
    """
    now = datetime.now(UTC)
    all_day = not isinstance(start, datetime)
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//openmirror//calendar//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'BEGIN:VEVENT',
        f'UID:{uid or _uid()}',
        f'DTSTAMP:{now.strftime("%Y%m%dT%H%M%SZ")}',
        # Folded, because a subject is the one property routinely long enough
        # to exceed 75 octets and an unfolded one is an invalid file: every
        # consumer after the first line sees a truncated title.
        fold(f'SUMMARY:{escape(summary)}'),
    ]
    lines.append(_stamp('DTSTART', start, all_day))
    if end is not None:
        lines.append(_stamp('DTEND', end, all_day))
    if description:
        lines.append(fold(f'DESCRIPTION:{escape(description)}'))
    if location:
        lines.append(fold(f'LOCATION:{escape(location)}'))
    for who in attendees or []:
        email = who.split('<')[-1].rstrip('>').strip() if '<' in who else who
        name = who.split('<')[0].strip() if '<' in who else ''
        cn = f';CN={escape(name)}' if name else ''
        lines.append(f'ATTENDEE{cn}:mailto:{email}')
    if alarm_minutes > 0:
        # A VALARM inside the VEVENT, which is where it belongs. Minutes
        # before, and negative because VALARM is relative and the sign is
        # easy to get backwards.
        lines += [
            'BEGIN:VALARM',
            'ACTION:DISPLAY',
            f'TRIGGER:-PT{int(alarm_minutes)}M',
            f'DESCRIPTION:{escape(summary)}',
            'END:VALARM',
        ]
    lines += ['END:VEVENT', 'END:VCALENDAR']
    return '\r\n'.join(lines) + '\r\n'


def _stamp(name: str, moment: datetime | date, all_day: bool) -> str:
    # `datetime` is a subclass of `date`, so the two tests cannot be written
    # as two `isinstance` checks in that order — the first one catches every
    # timestamp and quietly turns it into midnight, and the file that comes
    # out describes a different day than the one asked for.
    if isinstance(moment, datetime):
        if all_day:
            return f'{name};VALUE=DATE:{moment.date().strftime("%Y%m%d")}'
        stamp = moment.strftime('%Y%m%dT%H%M%S')
        if moment.tzinfo is not None and moment.utcoffset() == timedelta(0):
            return f'{name}:{stamp}Z'
        if moment.tzinfo is not None:
            return f'{name};TZID={getattr(moment.tzinfo, "key", None) or "UTC"}:{stamp}'
        # Floating: a local wall-clock time with no zone, which is a
        # legitimate thing to mean and is what a person in a room with other
        # people means.
        return f'{name}:{stamp}'

    if isinstance(moment, date):
        return f'{name};VALUE=DATE:{moment.strftime("%Y%m%d")}'
    return f'{name}:{str(moment)}'


def _uid() -> str:
    import uuid

    return f'{uuid.uuid4()}@openmirror'


def merge(base: list[Event], incoming: list[Event]) -> list[Event]:
    """Two calendars, newest wins on the same `UID`.

    The shape of a sync: a subscription is a whole file that may have
    anything added or removed, so "add these to mine" would keep every event
    that was ever cancelled. Keyed on UID because that is the only identifier
    two independent calendars agree on.
    """
    by_uid = {event.uid: event for event in base if event.uid}
    for event in incoming:
        if event.uid:
            by_uid[event.uid] = event
        else:
            by_uid[f'_anon-{id(event)}'] = event
    return sorted(by_uid.values(), key=lambda e: (e.start_at, e.summary))


def conflicts(candidate: Event, others: list[Event]) -> list[Event]:
    """What this would land on top of.

    Only busy events, and only ones that overlap: a cancelled meeting and a
    transparent reminder are both in the file and neither is a reason to say
    no to Tuesday.
    """
    return [
        other
        for other in others
        if other is not candidate and other.busy and other.status != 'CANCELLED' and candidate.overlaps(other)
    ]


def free_slots(
    events: list[Event],
    *,
    day: date,
    working_hours: tuple[time, time] = (time(9), time(17)),
    minimum: timedelta = timedelta(minutes=30),
    zone: Any = None,
) -> list[dict[str, Any]]:
    """Where there is room in one day.

    Working hours, not all day: "when are you free to meet for half an hour"
    has an answer that depends on what counts as working, and the default is
    nine to five in the *reference* zone rather than UTC. Overlaps are merged
    before the gaps are cut, because three back-to-back meetings have one
    busy block and not three, and subtracting them one at a time produces a
    gap that does not exist between the first and the second.
    """
    tz = zone or UTC
    window_start = datetime.combine(day, working_hours[0], tzinfo=tz)
    window_end = datetime.combine(day, working_hours[1], tzinfo=tz)
    if window_end <= window_start:
        return []

    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        if not event.busy or event.status == 'CANCELLED':
            continue
        if event.all_day:
            # The whole day, whatever its start and end times say — and
            # `end_at` rather than `event.end`, because an all-day event with
            # a degenerate end is a whole day too. Reading `event.end` here
            # is what let a day-long offsite block nothing at all.
            start = event.start_at.astimezone(tz)
            end = event.end_at.astimezone(tz)
            busy.append((
                datetime.combine(start.date(), time(0), tzinfo=tz),
                datetime.combine(end.date(), time(0), tzinfo=tz),
            ))
            continue
        start = event.start_at.astimezone(tz)
        end = event.end_at.astimezone(tz)
        busy.append((max(start, window_start), min(end, window_end)))

    # Trim to the window and merge, so the subtraction below is over disjoint
    # blocks. A block entirely outside the window leaves `end < start`.
    trimmed = sorted(
        ((s, e) for s, e in busy if e > window_start and s < window_end),
        key=lambda pair: pair[0],
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in trimmed:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    out: list[dict[str, Any]] = []
    cursor = window_start
    for start, end in merged:
        if start - cursor >= minimum:
            out.append({'start': cursor.isoformat(), 'end': start.isoformat()})
        cursor = max(cursor, end)
    if window_end - cursor >= minimum:
        out.append({'start': cursor.isoformat(), 'end': window_end.isoformat()})
    return out


__all__ = [
    'CalendarError', 'Event', 'build', 'conflicts', 'escape', 'fold', 'free_slots', 'merge', 'parse',
    'parse_when', 'unescape',
]

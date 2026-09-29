"""Calendars on disk, and the free/busy arithmetic over them.

A calendar is a `.ics` file in a directory, which is the smallest thing that
can be a calendar and also the only one that interoperates with everything
else without a conversation. Two ways in:

* **A subscription** — a `URL` that is fetched and cached, so a shared
  calendar arrives without anybody having to export it. Cached to a file and
  only refetched when asked, because a shared team calendar on a rate limit
  is a resource everybody shares.
* **A file** — one of the person's own, or one exported from somewhere else.

**Nothing is written to a file the person did not name.** A store that merged
every event it saw into one calendar would be a store that cannot be undone,
and "what did it put in my calendar" has to have an answer that is smaller
than "everything". So `merge` is only ever called on a subscription's own
cache, and a new event from the agent is returned as text for the person to
import.

The free/busy arithmetic is in `ics.py` and is deliberately not here: it is
the part worth testing on its own, and it has no business knowing about
files.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from openmirror.calendar.ics import Event, build, conflicts, free_slots, merge, parse

log = logging.getLogger(__name__)

# A calendar is a small file. A subscription that has been running for years
# is a few hundred kilobytes; anything much larger is a server that answered
# with something else, and it is refused rather than cached.
MAX_BYTES = 8 * 1024 * 1024


class CalendarStoreError(RuntimeError):
    """A calendar could not be read, fetched, or written."""


@dataclass(slots=True)
class Calendar:
    """One named calendar."""

    id: str
    name: str
    kind: str = 'file'  # file | subscription
    path: str = ''
    url: str = ''
    # Which folder of free time this calendar is *not* — "out of office" is
    # how somebody says they are not available, and it is stored rather than
    # inferred from a title.
    default_free: bool = True


def _slug(text: str) -> str:
    cleaned = re.sub(r'[^a-z0-9]+', '-', (text or '').strip().lower()).strip('-')
    return cleaned or 'calendar'


class CalendarStore:
    """A directory of `.ics` files, one per calendar."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, calendar_id: str) -> Path:
        # A traversal-safe name. `id` comes from a client, and a store that
        # writes wherever it is told is a store that will.
        safe = _slug(calendar_id)
        if not safe or safe in ('.', '..'):
            raise CalendarStoreError(f'{calendar_id!r} is not a usable calendar name')
        return self.root / f'{safe}.ics'

    def read(self, calendar_id: str) -> list[Event]:
        path = self.path_for(calendar_id)
        if not path.is_file():
            raise CalendarStoreError(f'there is no calendar called {calendar_id!r}')
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            raise CalendarStoreError(f'{calendar_id}: could not read it ({exc})') from exc
        return parse(text)

    def write(self, calendar: Calendar, text: str) -> Path:
        path = self.path_for(calendar.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def list(self) -> list[Calendar]:
        if not self.root.is_dir():
            return []
        out: list[Calendar] = []
        for path in sorted(self.root.glob('*.ics')):
            # The filename is a usable name on its own — `work-calendar` reads
            # as "Work calendar" — so an unreadable or unnamed file still
            # appears rather than disappearing from the list.
            out.append(Calendar(id=path.stem, name=path.stem.replace('-', ' ').title()))
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError as exc:
                log.warning('could not read %s: %s', path, exc)
                continue
            # A calendar's own name is better taken from its `X-WR-CALNAME`
            # than invented from the filename — the filename is a slug, and
            # the person named it properly in whatever they exported from.
            named = _calendar_name(text)
            if named:
                out[-1].name = named
        return out

    # -- subscriptions ----------------------------------------------------

    async def fetch_subscription(
        self, calendar: Calendar, *, seconds: int = 30
    ) -> tuple[list[Event], bool]:
        """Fetch a subscribed calendar and merge it into its cache.

        Returns (events, changed). The cache is only rewritten when the bytes
        actually differ, so a calendar nobody has touched does not get written
        on every poll — which on a synced folder is how you end up with a
        conflict marker in somebody's Dropbox.
        """
        if not calendar.url:
            raise CalendarStoreError(f'{calendar.id}: this subscription has no URL')
        try:
            # transport-exempt: not a model server. This reaches a calendar
            # host over plain HTTPS; the Tor toggle is a per-connection
            # setting on model traffic, and a shared team calendar is not
            # this project's to route anywhere. See openmirror/net/tor.py.
            async with aiohttp.ClientSession() as client:
                async with asyncio.timeout(seconds):
                    async with client.get(
                        calendar.url, timeout=seconds, headers={'Accept': 'text/calendar'}
                    ) as response:
                        if response.status >= 400:
                            raise CalendarStoreError(
                                f'{calendar.id}: the calendar server answered HTTP {response.status}'
                            )
                        body = await response.read(MAX_BYTES + 1)
        except TimeoutError as exc:
            raise CalendarStoreError(f'{calendar.id}: the calendar host did not answer in {seconds}s') from exc
        except aiohttp.ClientError as exc:
            raise CalendarStoreError(f'{calendar.id}: could not reach the calendar ({exc})') from exc
        if len(body) > MAX_BYTES:
            raise CalendarStoreError(
                f'{calendar.id}: that is not a calendar — it is over {MAX_BYTES // (1024 * 1024)}MB'
            )

        text = body.decode('utf-8', 'replace')
        incoming = parse(text)
        if not incoming:
            # A subscribed calendar that is legitimately empty exists, but a
            # server answering with an HTML login page is far more likely and
            # would otherwise blank the cache.
            raise CalendarStoreError(
                f'{calendar.id}: that URL did not return a calendar. Subscriptions need a public '
                f'.ics link or one that does not require signing in.'
            )

        digest = hashlib.sha256(body).hexdigest()
        cache = self.path_for(calendar.id)
        changed = True
        if cache.is_file() and hashlib.sha256(cache.read_bytes()).hexdigest() == digest:
            changed = False
        else:
            existing = parse(cache.read_text(encoding='utf-8', errors='replace')) if cache.is_file() else []
            merged = merge(existing, incoming)
            self.write(calendar, _to_ics(merged))
        return self.read(calendar.id), changed


def _calendar_name(text: str) -> str:
    for line in text.splitlines():
        if line.upper().startswith('X-WR-CALNAME:'):
            return line.split(':', 1)[1].strip()
    return ''


def _to_ics(events: list[Event]) -> str:
    """Re-serialise events into one file. Used only for a subscription's own
    cache, where the source file is not somebody's hand-written calendar."""
    out = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//openmirror//calendar//EN', 'CALSCALE:GREGORIAN']
    for event in events:
        out += [
            'BEGIN:VEVENT',
            f'UID:{event.uid or hashlib.sha1(event.summary.encode()).hexdigest()[:16]}',
            f'DTSTAMP:{datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")}',
        ]
        if event.start is not None:
            out.append(_stamp(event.start, event.all_day, 'DTSTART'))
        if event.end is not None:
            out.append(_stamp(event.end, event.all_day, 'DTEND'))
        if event.summary:
            from openmirror.calendar.ics import fold

            out.append(fold(f'SUMMARY:{event.summary}'))
        if event.location:
            out.append(fold(f'LOCATION:{event.location}'))
        if event.description:
            out.append(fold(f'DESCRIPTION:{event.description}'))
        out.append('END:VEVENT')
    out.append('END:VCALENDAR')
    return '\r\n'.join(out) + '\r\n'


def _stamp(moment: Any, all_day: bool, name: str) -> str:
    from openmirror.calendar.ics import _stamp as stamp

    return stamp(name, moment, all_day)


# ---------------------------------------------------------------------------
# The scheduling questions
# ---------------------------------------------------------------------------


def agenda(events: list[Event], *, start: datetime, days: int = 7) -> list[Event]:
    """What is coming, in order.

    Sorted by start rather than returned in file order, because a calendar
    file is not in chronological order and "what have I got this week" is
    the question that gets asked.
    """
    until = start + timedelta(days=max(1, days))
    window = [e for e in events if e.start_at < until and e.end_at > start]
    return sorted(window, key=lambda e: (e.all_day, e.start_at, e.summary))


def availability(
    events: list[Event],
    *,
    day: date,
    hours: tuple[Any, Any] = (datetime.strptime('09:00', '%H:%M').time(), datetime.strptime('17:00', '%H:%M').time()),
    minimum_minutes: int = 30,
    zone: Any = None,
) -> list[dict[str, Any]]:
    """Where there is room on one day, in the person's own zone."""
    return free_slots(
        events, day=day, working_hours=hours, minimum=timedelta(minutes=minimum_minutes), zone=zone
    )


def clash(candidate: Event, events: list[Event]) -> list[Event]:
    """What a proposed meeting would land on top of."""
    return conflicts(candidate, events)


def proposed(
    *,
    summary: str,
    start: datetime | date,
    end: datetime | date | None = None,
    description: str = '',
    location: str = '',
    attendees: list[str] | None = None,
) -> Event:
    """An event that does not exist yet, for asking the questions about.

    Building a real `Event` rather than a tuple means the conflict check is
    the same arithmetic whether the event is real or proposed — which is the
    only way the answer to "is that a clash" can be trusted.
    """
    return Event(
        summary=summary,
        start=start,
        end=end if end is not None else (
            start + timedelta(hours=1) if isinstance(start, datetime) else start + timedelta(days=1)
        ),
        all_day=not isinstance(start, datetime),
        description=description,
        location=location,
        attendees=list(attendees or []),
    )


def to_ics(event: Event, *, uid: str = '', alarm_minutes: int = 0) -> str:
    """The `.ics` text for an event, for the person to import.

    Returned rather than written anywhere. An agent that can put an event into
    a calendar can put one in at three in the morning on a Sunday, and the
    only guard that has ever worked on that is a person deciding to.
    """
    return build(
        summary=event.summary,
        start=event.start,
        end=event.end,
        description=event.description,
        location=event.location,
        attendees=event.attendees,
        uid=uid,
        alarm_minutes=alarm_minutes,
    )


__all__ = [
    'Calendar', 'CalendarStore', 'CalendarStoreError', 'agenda', 'availability', 'clash', 'proposed',
    'to_ics',
]

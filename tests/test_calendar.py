"""Scheduling: the format, and the arithmetic that depends on it.

The date handling is where this kind of code is wrong. Not obviously wrong —
wrong in a way that produces a plausible answer, and a plausible answer about
whether Tuesday is free is worse than no answer at all.

So each of these is a specific trap, and each is here because getting it wrong
is silent:

* **`DTEND` is exclusive.** A meeting from 10:00 to 11:00 and one from 11:00
  are back to back, not overlapping. Treating the end as inclusive reports a
  conflict that does not exist, and a person who is told "you have a clash"
  when they do not stops trusting the tool.
* **An all-day event is a date.** Reading `DTSTART;VALUE=DATE` as midnight
  makes it look free at nine in the morning, which is the single most
  expensive way to be wrong about a calendar.
* **A recurrence has no end date.** `DTEND` is optional, and an entry with
  only a start is real — a reminder, a half-typed meeting. It occupies an
  hour rather than nothing, because nothing at all is how a real conflict
  gets missed.
* **Folding is at 75 *octets*.** A subject with an emoji in it hits the limit
  in about a third of the characters, and decoding before unfolding turns the
  rest into replacement characters.
* **An unknown time zone is floating, not UTC.** Assuming UTC for a `TZID`
  the system does not know puts a nine-in-the-morning meeting at two in the
  afternoon for most of the world.

And the arithmetic, because that is what the tool is actually for: gaps are
cut from *merged* busy blocks, so three back-to-back meetings produce one
hole rather than two imaginary ones.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from openmirror.calendar.ics import (
    CalendarError,
    build,
    conflicts,
    escape,
    fold,
    free_slots,
    merge,
    parse,
    parse_when,
    unescape,
)
from openmirror.calendar.store import CalendarStore, CalendarStoreError, agenda, clash, proposed


def cal(*events: str) -> list:
    body = '\r\n'.join(events)
    return parse(f'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{body}\r\nEND:VCALENDAR\r\n')


def meeting(uid: str, start: str, end: str, **extra: str) -> str:
    lines = [
        'BEGIN:VEVENT',
        f'UID:{uid}',
        f'DTSTART:{start}',
        f'DTEND:{end}',
    ]
    lines += [f'{k}:{v}' for k, v in extra.items()]
    lines.append('END:VEVENT')
    return '\r\n'.join(lines)


UTC_SATURDAY = '20260314'  # a Saturday, checked so a day-boundary bug is obvious
UTC_SUNDAY = '20260315'


# --- dates and times ----------------------------------------------------------


def test_a_zulu_time_is_aware():
    moment, is_date = parse_when('20260314T100000Z', {})
    assert moment == datetime(2026, 3, 14, 10, tzinfo=UTC)
    assert not is_date


def test_a_named_zone_is_resolved():
    moment, _ = parse_when('20260314T100000', {'TZID': 'Europe/London'})
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)  # London is UTC in March, before BST


def test_an_unknown_zone_stays_floating_rather_than_becoming_utc():
    """The bug this prevents is a nine-in-the-morning meeting landing at two in
    the afternoon, for every host whose zone database is incomplete — which on
    a Linux box without `tzdata` is all of them."""
    moment, _ = parse_when('20260314T100000', {'TZID': 'Mars/Olympus_Mons'})
    assert moment.tzinfo is None
    assert moment.hour == 10, 'the wall clock must be preserved, not shifted'


def test_a_date_is_a_date_and_not_midnight():
    """`VALUE=DATE` is a whole day. Reading it as midnight is what makes an
    all-day event look free at nine in the morning."""
    moment, is_date = parse_when('20260314', {'VALUE': 'DATE'})
    assert is_date
    assert moment == date(2026, 3, 14)
    assert isinstance(moment, date) and not isinstance(moment, datetime)


def test_a_nonsense_date_is_an_error_not_a_default():
    with pytest.raises(CalendarError):
        parse_when('not a date', {})
    with pytest.raises(CalendarError):
        parse_when('', {})


# --- parsing ------------------------------------------------------------------


def test_a_meeting_parses_into_its_parts():
    found = cal(meeting(
        'a@x', '20260314T100000Z', '20260314T110000Z',
        SUMMARY='Standup',
        LOCATION='Room 2',
        **{'ATTENDEE;CN=Alice': 'mailto:alice@x.com'},
    ))
    assert len(found) == 1
    event = found[0]
    assert event.uid == 'a@x' and event.summary == 'Standup' and event.location == 'Room 2'
    assert event.attendees == ['Alice <alice@x.com>']
    assert event.end_at - event.start_at == timedelta(hours=1)


def test_escaped_characters_in_a_subject_come_back_as_written():
    """Most real subject lines contain one of these."""
    subject = 'Budget\\, Q1 — 50% "done"\\; soon\\nreally'
    found = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z', SUMMARY=subject))
    assert found[0].summary == 'Budget, Q1 — 50% "done"; soon\nreally'


def test_escaping_and_unescaping_round_trip():
    for text in ('plain', 'a,b', 'a;b', 'a\\b', 'two\nlines', 'mixed , ; \\ and\nnewline'):
        assert unescape(escape(text)) == text


def test_a_folded_line_with_an_emoji_survives():
    """Folding is at 75 *octets*, and decoding before unfolding turns the rest
    into replacement characters — which is how an emoji in a subject turns
    into three question marks in a list of meetings."""
    summary = 'planning 🎉🎉🎉 ' + 'word ' * 40
    line = f'SUMMARY:{escape(summary)}'
    folded = fold(line)
    assert '\r\n ' in folded, 'this subject is long enough to fold'

    document = f'BEGIN:VEVENT\r\nUID:a@x\r\nDTSTART:20260314T100000Z\r\nDTEND:20260314T110000Z\r\n{folded}\r\nEND:VEVENT'
    found = parse(f'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{document}\r\nEND:VCALENDAR')
    assert found[0].summary == summary


def test_folding_respects_the_octet_limit():
    out = fold('X' * 300)
    for part in out.split('\r\n'):
        assert len(part.encode()) <= 75, len(part.encode())


def test_an_unknown_property_is_ignored_rather_than_fatal():
    """A calendar full of vendor extensions must still open."""
    found = cal(meeting(
        'a@x', '20260314T100000Z', '20260314T110000Z',
        SUMMARY='x',
        **{'X-WR-RELCALID': 'abc', 'X-APPLE-STRUCTURED-LOCATION': 'geo:1,2'},
    ))
    assert found[0].summary == 'x'


def test_transparent_and_cancelled_are_not_busy():
    busy = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z', SUMMARY='Meeting'))[0]
    free = cal(meeting('b@x', '20260314T100000Z', '20260314T110000Z', SUMMARY='Reminder',
                       TRANSP='TRANSPARENT'))[0]
    cancelled = cal(meeting('c@x', '20260314T100000Z', '20260314T110000Z', STATUS='CANCELLED'))[0]
    assert busy.busy and not free.busy
    assert not conflicts(busy, [free, cancelled])


# --- the arithmetic that the tool exists for ---------------------------------


def test_touching_meetings_are_not_a_clash():
    """`DTEND` is exclusive. Inclusive ends report a conflict between a
    meeting that ends at eleven and one that starts at eleven, and a person
    told that stops trusting the tool."""
    ten_to_eleven = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z'))[0]
    eleven_to_noon = cal(meeting('b@x', '20260314T110000Z', '20260314T120000Z'))[0]
    assert not ten_to_eleven.overlaps(eleven_to_noon)
    assert not eleven_to_noon.overlaps(ten_to_eleven)


def test_a_real_overlap_is_found():
    ten_to_eleven = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z'))[0]
    half_past = cal(meeting('b@x', '20260314T103000Z', '20260314T113000Z'))[0]
    assert ten_to_eleven.overlaps(half_past)
    assert [c.uid for c in conflicts(half_past, [ten_to_eleven])] == ['a@x']


def test_an_event_with_no_end_still_occupies_an_hour():
    """Real: a reminder, a half-typed meeting. Occupied for nothing is how a
    genuine clash gets missed."""
    found = cal('BEGIN:VEVENT\r\nUID:a@x\r\nDTSTART:20260314T100000Z\r\nSUMMARY:Thing\r\nEND:VEVENT')
    event = found[0]
    assert event.end is None
    assert event.end_at - event.start_at == timedelta(hours=1)
    assert event.overlaps(cal(meeting('b@x', '20260314T103000Z', '20260314T110000Z'))[0])


def test_an_end_before_the_start_is_an_hour_rather_than_a_negative():
    found = cal(meeting('a@x', '20260314T110000Z', '20260314T100000Z'))
    assert found[0].end_at - found[0].start_at == timedelta(hours=1)


def test_a_duration_is_understood():
    found = cal('BEGIN:VEVENT\r\nUID:a@x\r\nDTSTART:20260314T100000Z\r\nDURATION:PT1H30M\r\nEND:VEVENT')
    assert found[0].end_at - found[0].start_at == timedelta(minutes=90)


def test_a_recurring_event_does_not_expand_into_the_next_year():
    """It is marked as recurring and left alone. A recurrence is a rule, and
    expanding it is a calendar library's job; what this must not do is pretend
    to know it and then be wrong about next Tuesday."""
    found = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z', RRULE='FREQ=WEEKLY;COUNT=52'))
    assert found[0].rrule
    assert len(found) == 1


def test_free_time_is_what_is_left_of_the_working_day():
    day = date(2026, 3, 16)
    busy = cal(
        meeting('a@x', '20260316T090000Z', '20260316T100000Z'),
        meeting('b@x', '20260316T110000Z', '20260316T120000Z'),
    )
    slots = free_slots(busy, day=day, working_hours=(time(9), time(17)), zone=UTC)
    assert [(s['start'][11:16], s['end'][11:16]) for s in slots] == [
        ('10:00', '11:00'), ('12:00', '17:00'),
    ]


def test_back_to_back_meetings_produce_one_hole_not_two():
    """Three meetings in a row have one busy block. Subtracting them one at a
    time invents a free minute between each pair, and suggests a slot that
    nobody could actually take."""
    day = date(2026, 3, 16)
    busy = cal(
        meeting('a@x', '20260316T090000Z', '20260316T100000Z'),
        meeting('b@x', '20260316T100000Z', '20260316T110000Z'),
        meeting('c@x', '20260316T110000Z', '20260316T120000Z'),
    )
    slots = free_slots(busy, day=day, zone=UTC)
    assert len(slots) == 1
    assert slots[0]['start'][:19] == '2026-03-16T12:00:00'


def test_a_gap_shorter_than_the_minimum_is_not_offered():
    """A fifteen-minute hole between two meetings is not a meeting, and
    offering it is how a calendar ends up with something impossible in it."""
    day = date(2026, 3, 16)
    busy = cal(
        meeting('a@x', '20260316T090000Z', '20260316T110000Z'),
        meeting('b@x', '20260316T111500Z', '20260316T140000Z'),
    )
    assert len(free_slots(busy, day=day, zone=UTC, minimum=timedelta(minutes=30))) == 1
    # At five minutes the same day offers the fifteen-minute hole as well as
    # the rest of it.
    assert len(free_slots(busy, day=day, zone=UTC, minimum=timedelta(minutes=5))) == 2


def test_a_full_day_leaves_nothing():
    day = date(2026, 3, 16)
    assert free_slots([], day=day, zone=UTC)[0]['start'][:19] == '2026-03-16T09:00:00'
    busy = cal(meeting('a@x', '20260316T090000Z', '20260316T170000Z'))
    assert free_slots(busy, day=day, zone=UTC) == []


def test_an_all_day_event_blocks_the_whole_day():
    """The trap from the module docstring: read as midnight, it looks free at
    nine in the morning."""
    day = date(2026, 3, 16)
    all_day = cal(meeting('a@x', '20260316', '20260317', SUMMARY='Offsite'))[0]
    assert all_day.all_day
    assert free_slots([all_day], day=day, zone=UTC) == []


def test_a_day_with_its_own_start_and_end_is_left_alone():
    """Somebody who works nights is not free at four in the afternoon, and a
    window that ends before it starts is not a window."""
    day = date(2026, 3, 16)
    assert free_slots([], day=day, working_hours=(time(17), time(9)), zone=UTC) == []


# --- writing ------------------------------------------------------------------


def test_a_built_calendar_parses_back():
    text = build(
        summary='Budget review',
        start=datetime(2026, 3, 16, 14, 0, tzinfo=UTC),
        end=datetime(2026, 3, 16, 15, 0, tzinfo=UTC),
        description='Numbers, and what to do about them.',
        location='Room 2',
        attendees=['Alice <alice@x.com>'],
        alarm_minutes=10,
    )
    found = parse(text)
    assert len(found) == 1
    event = found[0]
    assert event.summary == 'Budget review'
    assert event.description == 'Numbers, and what to do about them.'
    assert event.location == 'Room 2'
    assert event.attendees == ['Alice <alice@x.com>']
    assert 'BEGIN:VALARM' in text and 'TRIGGER:-PT10M' in text
    assert text.startswith('BEGIN:VCALENDAR') and text.rstrip().endswith('END:VCALENDAR')


def test_an_all_day_event_is_written_as_a_date():
    text = build(summary='Offsite', start=date(2026, 3, 16), end=date(2026, 3, 17))
    assert 'DTSTART;VALUE=DATE:20260316' in text
    assert parse(text)[0].all_day


def test_a_long_subject_is_folded_in_what_is_written():
    text = build(summary='A ' * 120, start=datetime(2026, 3, 16, 9, tzinfo=UTC))
    assert '\r\n ' in text
    assert parse(text)[0].summary.strip() == ('A ' * 120).strip()


def test_a_uid_is_generated_when_there_is_none():
    first = build(summary='x', start=date(2026, 3, 16))
    second = build(summary='x', start=date(2026, 3, 16))
    assert parse(first)[0].uid != parse(second)[0].uid


# --- merging ------------------------------------------------------------------


def test_two_calendars_merge_on_the_uid():
    """A subscription is a whole file, so "add these to mine" would keep every
    event that was ever cancelled. UID is the only identifier two independent
    calendars agree on."""
    mine = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z', SUMMARY='Mine'))
    theirs = cal(meeting('a@x', '20260314T100000Z', '20260314T113000Z', SUMMARY='Theirs'))
    merged = merge(mine, theirs)
    assert len(merged) == 1
    assert merged[0].summary == 'Theirs'


def test_a_removed_event_survives_a_merge_because_a_file_is_not_a_delta():
    """Said plainly because it is a limitation, not an oversight: this is
    `add these`, not `make mine match`. A subscription's cache is a full file
    and a full file can be trusted; anything else would need a sync token per
    host."""
    mine = cal(
        meeting('a@x', '20260314T100000Z', '20260314T110000Z'),
        meeting('gone@x', '20260314T120000Z', '20260314T130000Z'),
    )
    theirs = cal(meeting('a@x', '20260314T100000Z', '20260314T110000Z', SUMMARY='Updated'))
    assert len(merge(mine, theirs)) == 2


# --- the store ----------------------------------------------------------------


def test_a_calendar_id_cannot_escape_the_directory(tmp_path):
    """`id` comes from a client and a store that writes wherever it is told is
    a store that will.

    Slugging rather than refusing: `../../etc/passwd` becomes `etc-passwd`,
    which is inside the directory and a perfectly good name. A store that
    rejected the input would just mean somebody writes the calendar by hand
    instead.
    """
    store = CalendarStore(tmp_path)
    for attempt in ('../../etc/passwd', '/etc/passwd', '..', '.', 'a/b/c'):
        path = store.path_for(attempt)
        assert path.parent == tmp_path, attempt
        assert path.suffix == '.ics'
    assert store.path_for('Work Calendar').name == 'work-calendar.ics'


def test_reading_a_calendar_that_is_not_there_says_so(tmp_path):
    store = CalendarStore(tmp_path)
    with pytest.raises(CalendarStoreError, match='no calendar called'):
        store.read('nope')


def test_a_calendar_is_written_and_read_back(tmp_path):
    store = CalendarStore(tmp_path)
    from openmirror.calendar.store import Calendar

    text = build(summary='Standup', start=datetime(2026, 3, 16, 9, 30, tzinfo=UTC))
    store.write(Calendar(id='work', name='Work'), text)
    assert store.read('work')[0].summary == 'Standup'
    assert [c.id for c in store.list()] == ['work']


def test_a_calendar_names_itself_when_it_can(tmp_path):
    """`X-WR-CALNAME` is how a calendar says what it is called; the filename is
    a slug invented by whatever exported it."""
    store = CalendarStore(tmp_path)
    text = 'BEGIN:VCALENDAR\r\nX-WR-CALNAME:Acme shared\r\n' + '\r\n'.join([
        meeting('a@x', '20260314T100000Z', '20260314T110000Z')
    ]) + '\r\nEND:VCALENDAR'
    store.path_for('acme').write_text(text)
    assert [c.name for c in store.list()] == ['Acme shared']


# --- the questions the tool answers ------------------------------------------


def test_the_agenda_is_chronological_and_bounded():
    events = cal(
        meeting('b@x', '20260320T100000Z', '20260320T110000Z', SUMMARY='Later'),
        meeting('a@x', '20260314T100000Z', '20260314T110000Z', SUMMARY='Sooner'),
    )
    start = datetime(2026, 3, 14, tzinfo=UTC)
    assert [e.summary for e in agenda(events, start=start, days=7)] == ['Sooner', 'Later']


def test_the_agenda_excludes_events_already_over():
    events = cal(meeting('a@x', '20260101T100000Z', '20260101T110000Z', SUMMARY='Old'))
    assert agenda(events, start=datetime(2026, 3, 14, tzinfo=UTC), days=7) == []


def test_a_proposed_meeting_is_checked_with_the_same_arithmetic():
    """The same function either way, which is the only way the answer to "is
    that a clash" can be trusted."""
    existing = cal(meeting('a@x', '20260316T140000Z', '20260316T150000Z', SUMMARY='Review'))[0]
    free = proposed(summary='New', start=datetime(2026, 3, 16, 16, 0, tzinfo=UTC))
    taken = proposed(summary='New', start=datetime(2026, 3, 16, 14, 30, tzinfo=UTC))
    assert clash(free, [existing]) == []
    assert [c.summary for c in clash(taken, [existing])] == ['Review']


def test_a_proposal_defaults_to_an_hour_and_never_a_negative():
    assert proposed(summary='x', start=datetime(2026, 3, 16, 16, tzinfo=UTC)).end_at - (
        proposed(summary='x', start=datetime(2026, 3, 16, 16, tzinfo=UTC)).start_at
    ) == timedelta(hours=1)
    all_day = proposed(summary='x', start=date(2026, 3, 16))
    assert all_day.end_at - all_day.start_at == timedelta(days=1)
    assert all_day.all_day


def test_a_proposal_is_never_its_own_clash():
    """Otherwise every proposal collides with itself and the answer to "is
    that free" is always no."""
    candidate = proposed(summary='x', start=datetime(2026, 3, 16, 16, tzinfo=UTC))
    assert clash(candidate, [candidate]) == []


def test_a_time_in_another_zone_is_compared_correctly():
    """Two people write '2pm'. The event is stored in one zone and the proposal
    arrives in another, and they are the same moment."""
    tokyo = ZoneInfo('Asia/Tokyo')
    existing = cal(meeting('a@x', '20260316T060000Z', '20260316T070000Z', SUMMARY='In Tokyo'))[0]
    assert existing.start_at.astimezone(tokyo).hour == 15
    # 15:00 in Tokyo is 06:00Z, which is the meeting.
    clash_at_3pm_tokyo = proposed(summary='New', start=datetime(2026, 3, 16, 15, 0, tzinfo=tokyo))
    assert [c.summary for c in clash(clash_at_3pm_tokyo, [existing])] == ['In Tokyo']


def test_an_event_with_a_reminder_still_parses():
    """A `VALARM` inside a `VEVENT`, in almost every calendar anybody has.

    The bug this pins: `END:VALARM` closed the event, so every meeting with a
    reminder on it parsed to nothing. Every meeting in a subscribed calendar
    has one, and so does nearly everything exported from a phone.
    """
    text = build(
        summary='Standup',
        start=datetime(2026, 3, 16, 9, 30, tzinfo=UTC),
        end=datetime(2026, 3, 16, 9, 45, tzinfo=UTC),
        alarm_minutes=5,
    )
    found = parse(text)
    assert len(found) == 1
    assert found[0].summary == 'Standup'
    assert found[0].start_at == datetime(2026, 3, 16, 9, 30, tzinfo=UTC)


def test_a_vtimezone_does_not_hide_the_events_around_it():
    """`DTSTART` inside a `VTIMEZONE` is a transition, not a meeting. Reading
    it as the event's start is how a calendar's recurrence rules end up in
    the wrong place."""
    text = '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0',
        'BEGIN:VTIMEZONE', 'TZID:Europe/London',
        'BEGIN:STANDARD', 'DTSTART:19701025T020000', 'TZOFFSETFROM:+0100', 'TZOFFSETTO:+0000',
        'END:STANDARD', 'END:VTIMEZONE',
        'BEGIN:VEVENT', 'UID:a@x', 'DTSTART:20260316T100000Z', 'DTEND:20260316T110000Z',
        'SUMMARY:Real meeting', 'END:VEVENT',
        'END:VCALENDAR',
    ])
    found = parse(text)
    assert len(found) == 1
    assert found[0].summary == 'Real meeting'
    assert found[0].start_at == datetime(2026, 3, 16, 10, tzinfo=UTC)


def test_a_truncated_file_keeps_the_last_event():
    """A file written by something that crashed halfway. Dropping the last
    meeting because the file ends badly is worse than keeping a malformed
    trailing entry."""
    text = '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0',
        'BEGIN:VEVENT', 'UID:a@x', 'DTSTART:20260316T100000Z', 'SUMMARY:Whole', 'END:VEVENT',
        'BEGIN:VEVENT', 'UID:b@x', 'DTSTART:20260316T120000Z', 'SUMMARY:Truncated',
    ])
    found = parse(text)
    assert [e.summary for e in found] == ['Whole', 'Truncated']


def test_an_all_day_event_with_a_degenerate_end_still_blocks_the_whole_day():
    """Found by using the tool: `DTEND` equal to `DTSTART` is what a writer
    that does not know the end is exclusive produces, and it fell through to
    the one-hour fallback — so a day-long offsite blocked an hour of the day
    and the other seven hours read as free."""
    day = date(2026, 3, 16)
    for end in ('20260316', '20260315'):  # equal to the start, and before it
        event = cal(meeting('a@x', '20260316', end, SUMMARY='Offsite'))[0]
        assert event.all_day
        assert event.end_at - event.start_at == timedelta(days=1), end
        assert free_slots([event], day=day, zone=UTC) == [], end


def test_an_all_day_event_written_properly_is_still_exclusive():
    """`DTEND` is the day *after* the last day, so a one-day event on the
    sixteenth says the seventeenth."""
    event = cal(meeting('a@x', '20260316', '20260317', SUMMARY='Offsite'))[0]
    assert event.end_at - event.start_at == timedelta(days=1)
    assert free_slots([event], day=date(2026, 3, 17), zone=UTC) != [], 'must not bleed into the next day'
    assert free_slots([event], day=date(2026, 3, 16), zone=UTC) == []


def test_a_timed_event_with_a_degenerate_end_is_an_hour_not_a_day():
    """The other half of the fix: a one-hour default for a timed event is
    right, and turning that into a day would block the whole afternoon for
    something that is a typo."""
    event = cal(meeting('a@x', '20260316T100000Z', '20260316T100000Z'))[0]
    assert event.end_at - event.start_at == timedelta(hours=1)

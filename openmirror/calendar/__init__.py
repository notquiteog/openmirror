"""Scheduling, and the arithmetic around it.

`ics.py` is the format: reading and writing `.ics`, and the date handling
that format needs. The store and the tool live beside the format rather than
inside it, so the awkward parts — exclusive ends, all-day events, folded
lines, floating time zones — are in one file that can be read on its own.
"""

from __future__ import annotations

from openmirror.calendar.ics import CalendarError, Event, build, conflicts, free_slots, merge, parse

__all__ = ['CalendarError', 'Event', 'build', 'conflicts', 'free_slots', 'merge', 'parse']

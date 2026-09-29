"""Leave requests, the decisions on them, and what the history says.

This is the employee-management piece, and it is deliberately *not* a
connector to BambooHR or Gusto or Rippling. Every one of those is a vendor's
own API with its own scopes and its own rate limits, and a wrong guess at any
of them is a system that can read a payroll. What is portable is the part
that is not the vendor's: the decisions **you** have made, why you made
them, and the policy you said out loud. That is a ledger, and it is yours.

**So the ledger is local and nothing is sent anywhere.** There is no "sync",
no token to paste and no company data leaving the machine. An install that
wants to reach a real HR system should do it through a
[hook](HOOKS.md) — a command the operator has agreed to, pointed at their own
integration — rather than through code here guessing at somebody's API.

**The assessment is evidence, not a verdict.** `assess` returns the comparable
decisions, the counts, the policy you wrote, the notice given and the
overlaps — and then stops. It does not return a percentage, because "73%
likely" from nine data points is a number that looks like knowledge and is
not, and somebody will act on it.

What it returns instead is a **band**, and a band only once there is enough
history to mean anything:

    enough history, and it went the same way most times   -> 'clear'
    enough history, and it was mixed                      -> 'coin-flip'
    enough history, and it mostly went the other way      -> 'unlikely'
    not enough history                                    -> no band at all

Three is not enough and is not used. Four is the floor, and the reason it is a
floor rather than a number of requests is stated where it is enforced: a
ledger of four decisions about two people is a record, not a pattern.

**The judgement is the model's.** The tool gathers facts and the model reasons
about them, which is the same division this project uses everywhere: a tool
that returns a score has taken the decision away from the person who is
entitled to it, and a tool that returns evidence has not.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_people (
    name    TEXT PRIMARY KEY,
    role    TEXT NOT NULL DEFAULT '',
    joined  TEXT NOT NULL DEFAULT '',
    notes   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hr_requests (
    id             TEXT PRIMARY KEY,
    person         TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'holiday',
    start          TEXT NOT NULL,
    end            TEXT NOT NULL DEFAULT '',
    days           REAL NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    outcome        TEXT NOT NULL DEFAULT 'pending',
    reason         TEXT NOT NULL DEFAULT '',
    outcome_reason TEXT NOT NULL DEFAULT '',
    decided_at     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hr_person ON hr_requests(person);
CREATE INDEX IF NOT EXISTS idx_hr_outcome ON hr_requests(outcome);
"""

# Below this, no band is offered. Four is not a statistical threshold; it is
# the point below which a count of decisions about one or two people is a
# record rather than a pattern, and a band drawn from it would be a
# coincidence wearing a percentage.
MIN_FOR_A_BAND = 4
# Two years back, because a pattern built on decisions from a different team,
# or a different year, is a pattern about the past.
LOOKBACK_DAYS = 730


class StoreError(RuntimeError):
    """The ledger could not be read or written."""


@dataclass(slots=True)
class Request_:
    """One request and its outcome.

    The name carries a trailing underscore because `Request` is one of the
    protocol's own event classes, and two things meaning the same word in one
    package is a bad afternoon waiting to happen.
    """

    id: str
    person: str
    kind: str = 'holiday'
    start: str = ''
    end: str = ''
    days: float = 0.0
    note: str = ''
    outcome: str = 'pending'
    reason: str = ''
    outcome_reason: str = ''
    decided_at: str = ''
    created_at: str = ''

    def public(self) -> dict[str, Any]:
        return {
            'id': self.id, 'person': self.person, 'kind': self.kind, 'start': self.start,
            'end': self.end, 'days': self.days, 'note': self.note, 'outcome': self.outcome,
            'reason': self.reason, 'outcome_reason': self.outcome_reason,
            'decided_at': self.decided_at,
        }


def span_days(start: str, end: str = '') -> float:
    """How long a request is, in working days.

    Working days, because "ten days off" means ten days off and not ten
    calendar days — a request that spans a weekend is four days of leave and
    seven days of absence, and a policy written in terms of one of those is
    not satisfied by the other. Weekends are excluded and nothing else is:
    public holidays vary by country and by year, and guessing at them here
    would put a number in front of somebody that is confidently wrong.
    """
    try:
        first = date.fromisoformat(start[:10])
    except ValueError:
        return 0.0
    last = date.fromisoformat(end[:10]) if end else first
    if last < first:
        first, last = last, first
    total, cursor = 0.0, first
    while cursor <= last:
        if cursor.weekday() < 5:
            total += 1
        cursor += timedelta(days=1)
    return total


class Ledger:
    """Requests, decisions, and people. One file, on this machine."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(SCHEMA)
        # Other people's leave. A file with a world-readable mode is a file
        # the rest of the machine can read, which is the one thing this must
        # not be.
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self._db.close()

    # -- people -------------------------------------------------------------

    def upsert_person(self, name: str, *, role: str = '', joined: str = '', notes: str = '') -> str:
        clean = (name or '').strip()
        if not clean:
            raise StoreError('a person needs a name')
        self._db.execute(
            'INSERT INTO hr_people (name, role, joined, notes) VALUES (?,?,?,?) '
            'ON CONFLICT(name) DO UPDATE SET role=COALESCE(NULLIF(excluded.role,\'\'), role), '
            "joined=COALESCE(NULLIF(excluded.joined,''), joined), "
            "notes=CASE WHEN excluded.notes<>'' THEN excluded.notes ELSE notes END",
            (clean, role, joined, notes),
        )
        self._db.commit()
        return clean

    def people(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            'SELECT p.*, '
            '(SELECT COUNT(*) FROM hr_requests r WHERE r.person = p.name) AS requests, '
            "(SELECT COUNT(*) FROM hr_requests r WHERE r.person = p.name AND r.outcome='approved') AS approved "
            'FROM hr_people p ORDER BY p.name'
        ).fetchall()
        return [dict(row) for row in rows]

    # -- requests -----------------------------------------------------------

    def add(self, **fields: Any) -> Request_:
        person = str(fields.get('person') or '').strip()
        if not person:
            raise StoreError('a request needs a person')
        start = str(fields.get('start') or '')
        end = str(fields.get('end') or '')
        if not start:
            raise StoreError('a request needs a start date')
        days = fields.get('days')
        record = Request_(
            id=uuid.uuid4().hex[:12],
            person=person,
            kind=str(fields.get('kind') or 'holiday'),
            start=start, end=end,
            days=float(days) if days is not None else span_days(start, end),
            note=str(fields.get('note') or ''),
            reason=str(fields.get('reason') or ''),
            outcome='pending',
            created_at=datetime.now(UTC).isoformat(timespec='seconds'),
        )
        self.upsert_person(person)
        self._db.execute(
            'INSERT INTO hr_requests (id, person, kind, start, end, days, note, outcome, reason, created_at)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (record.id, record.person, record.kind, record.start, record.end, record.days,
             record.note, record.outcome, record.reason, record.created_at),
        )
        self._db.commit()
        return record

    def decide(self, request_id: str, outcome: str, *, reason: str = '') -> Request_:
        """Approve or deny, with the reason.

        The reason is not decoration. It is the only thing that makes the next
        request assessable: "denied" with no reason teaches the ledger nothing
        and gives whoever reads it later no way to tell a policy from a mood.
        """
        clean = str(outcome or '').strip().lower()
        if clean not in ('approved', 'denied'):
            raise StoreError("an outcome is 'approved' or 'denied'")
        found = self.get(request_id)
        if found is None:
            raise StoreError(f'no request called {request_id!r}')
        if not str(reason or '').strip():
            log.info('request %s decided %s with no reason given', request_id, clean)
        self._db.execute(
            'UPDATE hr_requests SET outcome=?, outcome_reason=?, decided_at=? WHERE id=?',
            (clean, str(reason or ''), datetime.now(UTC).isoformat(timespec='seconds'), request_id),
        )
        self._db.commit()
        got = self.get(request_id)
        assert got is not None          # it was just updated
        return got

    def get(self, request_id: str) -> Request_ | None:
        row = self._db.execute('SELECT * FROM hr_requests WHERE id=?', (str(request_id),)).fetchone()
        return _row_to_request(row) if row else None

    def list(
        self,
        *,
        person: str = '',
        outcome: str = '',
        pending_only: bool = False,
        limit: int = 50,
    ) -> list[Request_]:
        clauses, args = [], []
        if person:
            clauses.append('LOWER(person) = ?')
            args.append(person.strip().lower())
        if outcome:
            clauses.append('outcome = ?')
            args.append(outcome.strip().lower())
        if pending_only:
            clauses.append("outcome = 'pending'")
        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        rows = self._db.execute(
            # Newest first, by the date it starts rather than by insertion
            # order: a request added today for next March is older than one
            # added last month for next week, and "waiting" is a question
            # about dates.
            f"SELECT * FROM hr_requests {where} "
            "ORDER BY COALESCE(NULLIF(start, ''), created_at) DESC, id DESC LIMIT ?",
            (*args, max(1, min(int(limit), 500))),
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    # -- the assessment -----------------------------------------------------

    def assess(self, person: str, kind: str, days: float, *, start: str = '') -> dict[str, Any]:
        """What the history says about this request, and how sure that is.

        **Returns evidence and a band, never a percentage.** The evidence is
        the decision: the comparable past requests with their outcomes and
        reasons, how much notice the person gives, what the stated policy says.
        The band is offered only with `MIN_FOR_A_BAND` comparable decisions,
        and below that `band` is empty and `because` says so.

        The judgement is left to the model. A tool that answered "approve this"
        would have taken a decision that is the person's to make, from a record
        of four decisions, and dressed it up as analysis.
        """
        who = (person or '').strip()
        what = (kind or 'holiday').strip().lower()
        length = float(days or 0)

        comparables, basis = self._comparables(who, what, length)
        # Whether this person's own record is behind the evidence at all. It
        # decides the band and nothing else: other people's decisions are worth
        # showing as context, and grading a request for somebody you have never
        # decided about by the team's average is the lazy inference this tool
        # exists to avoid.
        own = any(r.outcome in ('approved', 'denied') for r in self._for_person(who))
        same_person = self._for_person(who)
        pending = self.list(pending_only=True, limit=100)
        out_of_range = self._out_of_range(who, length)

        approved = sum(1 for r in comparables if r.outcome == 'approved')
        denied = sum(1 for r in comparables if r.outcome == 'denied')
        decided = approved + denied

        band, why = '', ''
        if not own:
            why = (
                f'there is no record of a decision about {who} in this ledger, so there is no reading '
                f'to offer. What follows is how comparable requests have gone for other people, which is '
                f'context and not a verdict — decide this one on its own facts and the policy.'
            )
        elif out_of_range:
            # The case the first version got most confidently wrong.
            why = f'No reading is offered: {out_of_range}. The evidence below is for context.'
        elif decided < MIN_FOR_A_BAND:
            why = (
                f'only {decided} comparable decision(s) in the ledger — {basis} — and fewer than '
                f'{MIN_FOR_A_BAND} is a record rather than a pattern. No band is offered; read the '
                f'evidence and decide.'
            )
        else:
            share = approved / decided
            # The bands are wide on purpose. A narrow one on eleven data
            # points is a claim about the future made from the past, and it is
            # the sort of claim that gets acted on.
            if share >= 0.75:
                band = 'clear'
                why = f'{approved} of {decided} comparable requests were approved.'
            elif share <= 0.25:
                band = 'unlikely'
                why = f'{approved} of {decided} comparable requests were approved.'
            else:
                band = 'coin-flip'
                why = f'{approved} of {decided} comparable requests were approved — no pattern.'

        return {
            'person': who,
            'kind': what,
            'days': length,
            'band': band,
            'because': why,
            'comparables_from': basis,
            'out_of_range': out_of_range,
            'evidence': [r.public() for r in comparables],
            'decided': decided,
            'for_this_person': [r.public() for r in same_person],
            'pending': [r.public() for r in pending if r.person.lower() == who.lower()],
            'notice': _notice(pending, who),
            'policy': self.policy(who),
            'people': len(self.people()),
            'total_requests': len(self.list(limit=500)),
        }

    def _comparables(self, person: str, kind: str, days: float) -> tuple[list[Request_], str]:
        """Decided requests close enough to this one to say anything about it.

        **Close, not merely similar.** The first version OR-ed "same person",
        "same kind" and "a similar length" together, which made every holiday
        comparable to every other holiday — and then reported a fourteen-day
        request as `clear` for somebody whose own policy file says that more
        than ten days needs cover. Every holiday sharing a kind is not
        evidence about this holiday.

        So a request counts only when it is **the same person and a similar
        length**, or — for somebody with no history at all — the same kind and
        a similar length. And "similar" is a factor of two, because three days
        and a fortnight are not the same question.

        The second element of the return is why the set is what it is, and it
        is shown to the model: a request that falls outside the observed range
        gets the history and *not* a verdict, which is the case where history
        is least entitled to speak.
        """
        rows = self._db.execute(
            "SELECT * FROM hr_requests WHERE outcome IN ('approved','denied') "
            "ORDER BY COALESCE(NULLIF(start, ''), created_at) DESC, id DESC"
        ).fetchall()
        mine = [_row_to_request(row) for row in rows
                if _row_to_request(row).person.strip().lower() == person.strip().lower()]
        same_kind = [_row_to_request(row) for row in rows
                     if _row_to_request(row).kind.strip().lower() == kind]

        if mine:
            pool = [r for r in mine if _within(r.days, days)] if days > 0 else list(mine)
            basis = 'the same person, and a similar length' if days > 0 else 'every decision about this person'
        elif not days:
            # No length to compare on, so everything of the same kind is the
            # evidence and the band is withheld below for having too little.
            pool = list(same_kind)
            basis = f'every decision about {kind} anywhere, because there is no history for this person'
        else:
            pool = [r for r in same_kind if _within(r.days, days)]
            basis = (
                f'the same kind and a similar length, because {person} has no history of their own — '
                "this is other people's record, not theirs"
            )

        # Sorted by closeness of length, so the evidence reads nearest-first.
        pool.sort(key=lambda r: abs(r.days - days) if days > 0 else 0)
        return pool[:20], basis

    def _out_of_range(self, person: str, days: float) -> str:
        """Why the history does not cover this request, if it does not.

        A request longer than anything ever approved is *exactly* the case
        where a pattern should stop applying, and it is the case a naive
        nearest-neighbour gets most confidently wrong. Said out loud rather
        than left for the model to infer, because the band is withheld rather
        than wrong and the reason for that is the whole point.
        """
        if days <= 0 or not person:
            return ''
        rows = self._db.execute(
            "SELECT MAX(days) AS longest FROM hr_requests "
            "WHERE outcome IN ('approved','denied') AND LOWER(person)=?",
            (person.strip().lower(),),
        ).fetchone()
        longest = float(rows['longest'] or 0) if rows else 0
        if longest > 0 and days > longest * 1.5:
            return (
                f'this is longer than anything you have decided for {person} before '
                f'({days:g} days against a longest of {longest:g}), so the history does not reach it'
            )
        return ''

    def _for_person(self, person: str) -> list[Request_]:
        rows = self._db.execute(
            'SELECT * FROM hr_requests WHERE LOWER(person)=? ORDER BY id DESC LIMIT 20',
            (person.strip().lower(),),
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    def policy(self, person: str = '') -> str:
        """The stated policy, read back rather than interpreted.

        Not parsed, and deliberately: a tool that tried to read "more than ten
        consecutive days needs cover" out of English and produce a verdict
        would be doing the judgement on a rule it half-understood, and
        confidently. The policy is shown, and the model reads it the way a
        person would.
        """
        row = self._db.execute(
            'SELECT notes FROM hr_people WHERE LOWER(name)=?', (person.strip().lower(),)
        ).fetchone() if person else None
        return str(row['notes']) if row else ''


def _within(a: float, b: float) -> bool:
    """Whether two lengths are within a factor of two, treating zero kindly."""
    if a <= 0 or b <= 0:
        return False
    return 0.5 <= (a / b) <= 2.0


def _notice(pending: list[Request_], person: str) -> int | None:
    """Working days of notice this person has given, on their open requests.

    `None` when there is nothing pending, and `None` means "not known" — which
    is different from zero, and is why this is not a count of zero.
    """
    days = [r.days for r in pending if r.person.strip().lower() == person.strip().lower() and r.start]
    return int(sum(days)) if days else None


def _row_to_request(row: sqlite3.Row) -> Request_:
    return Request_(
        id=str(row['id']), person=str(row['person']), kind=str(row['kind']),
        start=str(row['start']), end=str(row['end']), days=float(row['days'] or 0),
        note=str(row['note']), outcome=str(row['outcome']), reason=str(row['reason']),
        outcome_reason=str(row['outcome_reason']), decided_at=str(row['decided_at']),
        created_at=str(row['created_at']),
    )


__all__ = [
    'LOOKBACK_DAYS', 'MIN_FOR_A_BAND', 'Ledger', 'Request_', 'StoreError', 'span_days',
]

"""The project's working days: which weekdays count, the time zone a day is read in, and the counts every figure of the
daily report is made of. Pure: dates and times in, numbers out, so a test sets the clock by passing a date.

A day is a working day when its ISO weekday (Monday 1 to Sunday 7) is in ``Calendar.days``. Holidays are not known
(non-goal of plan pm-daily-tracking). Two counts, kept apart:

- ``elapsed(a, b)``: the working days in (a, b], how many working days passed from the end of ``a`` to the end of
  ``b``. A review entered on Friday is 1 working day old on Monday; a blocker set today is 0 days old today.
- ``span(a, b)``: the working days in [a, b], both ends in; the working days left to a due date from today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Calendar:
    zone: ZoneInfo
    days: frozenset[int]

    def working(self, day: date) -> bool:
        return day.isoweekday() in self.days

    def local(self, moment: datetime) -> date:
        """The date ``moment`` falls on in the project's time zone."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(self.zone).date()

    def start_of(self, day: date) -> datetime:
        """The first instant of ``day`` in the project's time zone, as UTC."""
        return datetime.combine(day, time.min, tzinfo=self.zone).astimezone(UTC)

    def previous(self, day: date) -> date:
        """The last working day before ``day``."""
        found = day - ONE_DAY
        while not self.working(found):
            found -= ONE_DAY
        return found

    def span(self, first: date, last: date) -> int:
        """The working days in [first, last]; 0 when ``last`` is before ``first``."""
        if last < first:
            return 0
        return sum(self.working(first + timedelta(days=n)) for n in range((last - first).days + 1))

    def elapsed(self, since: date, until: date) -> int:
        """The working days in (since, until]; 0 when ``until`` is not after ``since``."""
        return self.span(since + ONE_DAY, until)


__all__ = ["Calendar", "ONE_DAY"]

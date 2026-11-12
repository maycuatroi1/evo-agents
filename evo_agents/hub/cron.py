"""The schedule of a cron job (``docs/hub.md``, Cron jobs): reading its five cron fields, checking its IANA time zone,
how often it fires at most, and its next fire. The one helper of the hub for them: the routes of
``evo_agents.hub.server.cron_jobs`` check a job with it and list its next fire, and the job that fires the jobs reads
the minutes it fires at with it.

A cron is the five fields minute, hour, day of month, month and day of week, read by croniter in the job's time zone:
wall-clock times there, so a job at 09:00 in Asia/Ho_Chi_Minh fires at 02:00 UTC. No alias (``@daily``), no field of
seconds, no hashed ``H``. A cron that reads but never fires (``0 0 30 2 *``) is refused too.

``closest_minutes`` is the smallest gap, in wall-clock minutes, between two fires among the first SAMPLE fires from a
fixed start; a job that sends an agent a message or runs a plan may fire every AGENT_MINUTES at most, a job that runs
a shell command every COMMAND_MINUTES at most (``MIN_MINUTES``). The gaps of a cron repeat with its fields, and the
smallest one shows within the first fires of any cron fine enough to break the limits.

Outside ``evo_agents.hub.server``: the hub's job worker imports it. croniter is an import of the hub-server extra.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AGENT_MINUTES = 15  # a job of kind agent or plan fires at most every 15 minutes
COMMAND_MINUTES = 5  # a job of kind command at most every 5 minutes
MIN_MINUTES = {"agent": AGENT_MINUTES, "plan": AGENT_MINUTES, "command": COMMAND_MINUTES}
FIELDS = 5
SAMPLE = 2000  # fires read to find the smallest gap
START = datetime(2024, 1, 1) - timedelta(minutes=1)  # a Monday of a leap year, so 00:00 of it is the first fire read


class CronRefused(ValueError):
    """A cron or a time zone the hub does not take; the message says why."""


def _cron(expr: str, start: datetime):
    from croniter import croniter

    return croniter(expr, start)


def check_cron(expr: str) -> str:
    """``expr`` with its fields separated by one space; CronRefused when it is not five fields croniter reads, or
    never fires."""
    from croniter import CroniterBadCronError, CroniterBadDateError, CroniterError

    fields = expr.split() if isinstance(expr, str) else []
    if len(fields) != FIELDS:
        raise CronRefused(
            f"cron {expr!r} is not five fields: write minute, hour, day of month, month and day of week, as "
            "'0 9 * * 1-5'"
        )
    if any(field.upper().startswith("H") or "@" in field for field in fields):
        raise CronRefused(f"cron {expr!r}: hashed fields (H) and aliases (@daily) are not taken; write the five fields")
    text = " ".join(fields)
    try:
        _cron(text, START).get_next(datetime)
    except CroniterBadDateError:
        raise CronRefused(f"cron {text!r} never fires: no date matches its day of month and month") from None
    except (CroniterBadCronError, CroniterError, ValueError, KeyError) as exc:
        raise CronRefused(f"cron {text!r} does not read: {exc}") from None
    return text


def zone(name: str) -> ZoneInfo:
    """The IANA time zone ``name``; CronRefused when the hub does not know it."""
    if not isinstance(name, str) or not name or name.startswith("/") or ".." in name:
        raise CronRefused(f"time zone {name!r} is not an IANA name such as Europe/Paris or UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise CronRefused(f"time zone {name!r} is unknown: give an IANA name such as Europe/Paris or UTC") from None


def closest_minutes(expr: str) -> int:
    """The smallest gap between two fires of the checked cron ``expr``, in wall-clock minutes, among its first SAMPLE
    fires."""
    found = _cron(expr, START)
    last = found.get_next(datetime)
    closest: int | None = None
    for _ in range(SAMPLE - 1):
        try:
            at = found.get_next(datetime)
        except Exception:  # a cron of rare dates that runs out within croniter's search
            break
        gap = int((at - last).total_seconds() // 60)
        closest = gap if closest is None else min(closest, gap)
        if closest <= 1:
            break
        last = at
    return closest if closest is not None else 24 * 60 * 366


def check(expr: str, tz: str, kind: str) -> str:
    """The cron ``expr`` of a job of ``kind`` in time zone ``tz``, checked (``check_cron``, ``zone``) and no finer
    than MIN_MINUTES of its kind; CronRefused with the reason otherwise."""
    text = check_cron(expr)
    zone(tz)
    least = MIN_MINUTES[kind]
    closest = closest_minutes(text)
    if closest < least:
        what = (
            "runs a shell command"
            if kind == "command"
            else "sends an agent a message"
            if kind == "agent"
            else "runs a plan"
        )
        raise CronRefused(
            f"cron {text!r} fires {closest} minute{'s' if closest != 1 else ''} apart at its closest; a job that {what} "
            f"fires at most every {least} minutes"
        )
    return text


def next_fire(expr: str, tz: str, after: datetime | None = None) -> datetime:
    """The first minute after ``after`` (now by default) at which the checked cron ``expr`` fires in time zone ``tz``,
    in UTC."""
    after = (after or datetime.now(UTC)).astimezone(zone(tz))
    return _cron(expr, after).get_next(datetime).astimezone(UTC)


def fires_between(expr: str, tz: str, after: datetime, until: datetime) -> list[datetime]:
    """The minutes, in UTC and oldest first, at which the checked cron ``expr`` fires in time zone ``tz`` after
    ``after`` and up to ``until``, both included at the top. A wall-clock time the fall back of daylight saving time
    shows twice fires the first time only: croniter gives both instants, and the second one, of fold 1 in ``tz``, is
    left out. A time the spring forward skips fires at croniter's next minute that exists."""
    where = zone(tz)
    found = _cron(expr, after.astimezone(where))
    minutes: list[datetime] = []
    while True:
        at = found.get_next(datetime)
        if at.astimezone(UTC) > until:
            return minutes
        local = at.astimezone(UTC).astimezone(where)
        if local.fold == 0:
            minutes.append(at.astimezone(UTC).replace(second=0, microsecond=0))

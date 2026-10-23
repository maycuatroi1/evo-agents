"""What the stages of a contact are and how they are computed: pure functions of facts the hub holds, no I/O.

A contact is in one of four stages, which only move forward and which each have the time they were reached:

- lead: the contact was seen (an invite request, an issue, a pull request or a discussion of its own, an invite
  link bound to its login, a grant). The time is that of its first touchpoint.
- invited: an invite link names its login (made for it or accepted by it) or it holds a grant on the project.
- activated: its login has dispatched a run in the project. The time is that of its first run.
- retained: it dispatched a run from RETAIN_FROM_DAYS to RETAIN_TO_DAYS after that first run (a day-21 to day-28
  window), so a person who came back three weeks later counts and one who stopped does not.

A stage whose facts are missing but a later one's are present takes the later time (a run implies a grant, a grant
implies an invite), so the times never run backwards. Two runs of the computation over the same facts give the same
answer. Reading a stage never lowers one: ``advance`` keeps the higher of the stored stage and the computed one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from evo_agents.hub.tables_crm import STAGES

RETAIN_FROM_DAYS = 21
RETAIN_TO_DAYS = 28
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def login_of(text: str) -> str | None:
    """The lower-case GitHub login ``text`` names (a leading @ allowed), or None when it is none."""
    name = (text or "").strip().removeprefix("@")
    return name.lower() if LOGIN.fullmatch(name) else None


def profile_url(login: str) -> str:
    """The public profile of a GitHub login."""
    return f"https://github.com/{login}"


@dataclass(frozen=True)
class Facts:
    """What the hub holds of one contact: when it was first seen, when an invite or a grant first named its login,
    when it first dispatched a run in the project, and when it first dispatched one in the retention window (None
    for each fact not true yet)."""

    seen_at: datetime
    invited_at: datetime | None = None
    first_run_at: datetime | None = None
    retained_at: datetime | None = None


def window(first_run_at: datetime) -> tuple[datetime, datetime]:
    """The span a run falls in to count as retention: from day 21 up to, not including, day 28 after the first run."""
    return first_run_at + timedelta(days=RETAIN_FROM_DAYS), first_run_at + timedelta(days=RETAIN_TO_DAYS)


def reached(facts: Facts) -> dict[str, datetime]:
    """stage -> the time the contact reached it, for each stage it has reached, in order."""
    found = {"retained": facts.retained_at, "activated": facts.first_run_at, "invited": facts.invited_at}
    later: datetime | None = None
    out: dict[str, datetime] = {}
    for stage in reversed(STAGES[1:]):  # retained, activated, invited: each takes at most the next one's time
        moment = found[stage]
        if moment is None and later is None:
            continue
        moment = min(moment, later) if moment is not None and later is not None else (moment or later)
        out[stage] = moment
        later = moment
    out["lead"] = min(facts.seen_at, later) if later is not None else facts.seen_at
    return {stage: out[stage] for stage in STAGES if stage in out}


def stage_of(times: dict[str, datetime]) -> str:
    """The highest stage in ``times``."""
    return max(times, key=STAGES.index)


def advance(stored: str, times: dict[str, datetime]) -> str:
    """The stage a contact stored at ``stored`` is in once ``times`` are known: never below ``stored``."""
    computed = stage_of(times)
    return computed if STAGES.index(computed) > STAGES.index(stored) else stored

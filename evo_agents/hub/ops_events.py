"""The operations events of a project (plan hub-steward, criterion a1): what the hub writes into hub_events when a run
of the project fails or is lost (``run.ended``), is parked (``run.parked``), asks a person a decision
(``decision.opened``), when the circuit breaker pauses its night shift (``curator.paused``), and when the health of a
worker that serves it changes (``worker.health``).

Each one is written in the transaction that made what it tells of, through ``hub_events.record``, so it exists exactly
when that was committed. Its payload carries ids and states alone, never a text: no error, no question, no reason. No
plugin takes these kinds (they are not in ``evo_agents.plugins.protocol.EVENT_KINDS``), and they queue no tick of the
Loop.

A worker belongs to no project, so its health is written once for each project it serves (worker_projects), and only
when its state changes: the band of its free disk (under the reserve its daemon says, ``DISK_LOW_GB`` when it says
none) or the login of one of its runtimes. The
percentage of free disk rides along but changes nothing by itself.

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, tables

ENDED_STATES = ("failed", "lost")  # the moves of a run that write run.ended
DISK_LOW_GB = 20.0  # below this much free disk on its worktrees' drive a worker's disk is low, when it says no reserve
# a low disk stays low until it has this much over the reserve, so a drive that hovers at the reserve line writes one
# worker.health, not one each time it crosses it (the worker's cleanup frees to the same line, daemon_cleanup)
DISK_HEADROOM_GB = 5.0
LOGIN_STATES = ("ok", "leased", "unauthorized", "missing", "unknown")  # a runtime's login, as its daemon checked it
# The logins a runtime works with: its own, or none on the machine while its last run there authenticated with a lease
# of the hub (``evo_agents.worker.health.LEASED``).
LOGGED_IN = ("ok", "leased")
VERSION = hub_events.VERSION


async def run_moved(
    conn: AsyncConnection,
    *,
    project_id: int | None,
    run_id: int,
    kind: str,
    new: str,
    failure_cause: str | None,
    worker_id: int | None,
) -> int | None:
    """Write ``run.ended`` for a run that came to be failed or lost, ``run.parked`` for one that came to be parked;
    nothing for any other move, nor for a run of no project. The event's id, or None."""
    if project_id is None:
        return None
    if new in ENDED_STATES:
        payload = {
            "v": VERSION,
            "run_id": run_id,
            "run_kind": kind,
            "state": new,
            "failure_cause": failure_cause,
            "worker_id": worker_id,
        }
        return await hub_events.record(conn, hub_events.RUN_ENDED, project_id, payload)
    if new == "parked":
        payload = {"v": VERSION, "run_id": run_id, "run_kind": kind, "state": new, "worker_id": worker_id}
        return await hub_events.record(conn, hub_events.RUN_PARKED, project_id, payload)
    return None


async def decision_opened(
    conn: AsyncConnection,
    *,
    project_id: int | None,
    decision_id: int,
    category: str,
    run_id: int,
    asked_of: int | None,
) -> int | None:
    """Write ``decision.opened`` for decision ``decision_id`` that run ``run_id`` asked user ``asked_of``."""
    if project_id is None:
        return None
    payload = {
        "v": VERSION,
        "decision_id": decision_id,
        "category": category,
        "run_id": run_id,
        "asked_of": asked_of,
    }
    return await hub_events.record(conn, hub_events.DECISION_OPENED, project_id, payload)


def pause_class(causes: list[str | None], reverts: int) -> str:
    """The class of reason the circuit breaker paused a night shift for: the failure cause most of its failed runs
    share (``agent`` for a run that named none), or ``revert`` when changes that got worse figures were more."""
    counted = Counter(cause or "agent" for cause in causes)
    if reverts and reverts >= max(counted.values(), default=0):
        return "revert"
    if not counted:
        return "unknown"
    return sorted(counted.items(), key=lambda item: (-item[1], item[0]))[0][0]


async def curator_paused(
    conn: AsyncConnection, *, project_id: int, schedule_ids: list[int], run_ids: list[int], reverts: int
) -> int:
    """Write ``curator.paused`` for the night shift of project ``project_id`` that the circuit breaker paused: the
    schedules it paused, the runs that failed in a row, and the class of reason (``pause_class``)."""
    causes: list[str | None] = []
    if run_ids:
        r = tables.runs
        found = await conn.execute(select(r.c.failure_cause).where(r.c.id.in_(run_ids)))
        causes = [row.failure_cause for row in found]
    payload = {
        "v": VERSION,
        "reason": "circuit_breaker",
        "cause_class": pause_class(causes, reverts),
        "schedule_ids": sorted(schedule_ids),
        "run_ids": run_ids,
        "reverts": reverts,
    }
    return await hub_events.record(conn, hub_events.CURATOR_PAUSED, project_id, payload)


def _size(value) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, int | float) else float(value)


def disk_reserve(health: dict | None) -> float:
    """The gigabytes a worker keeps free on its worktrees' drive, as its stored health report says (``reserve_gb``,
    its config's ``disk_reserve_gb``); ``DISK_LOW_GB`` when it says none."""
    found = _size(((health or {}).get("disk") or {}).get("reserve_gb"))
    return DISK_LOW_GB if found is None else found


def disk_band(health: dict | None, was: dict | None = None) -> str | None:
    """``low`` or ``ok`` for the free disk a stored health report says, against the worker's reserve
    (``disk_reserve``); None when it says nothing of it. A report the hub stored with its band (``with_band``) says
    it; else a disk the report before ``was`` held low stays low under the reserve plus ``DISK_HEADROOM_GB``. The
    claim gives a worker whose disk is low no run (``claim_rules.Claimer.stops``)."""
    disk = (health or {}).get("disk") or {}
    if disk.get("band") in ("low", "ok"):
        return disk["band"]
    free = _size(disk.get("free_gb"))
    if free is None:
        return None
    line = disk_reserve(health) + (DISK_HEADROOM_GB if disk_band(was) == "low" else 0.0)
    return "low" if free < line else "ok"


def with_band(health: dict | None, was: dict | None) -> dict | None:
    """Health report ``health`` as the hub stores it: its disk with the band it is in after the report before,
    ``was`` (``disk_band``), so the headroom holds from one heartbeat to the next."""
    band = disk_band(health, was)
    if band is None or not health:
        return health
    return {**health, "disk": {**health["disk"], "band": band}}


def health_state(health: dict | None) -> tuple | None:
    """What a change of makes a ``worker.health`` event: the disk band and the login of each runtime; None for a
    worker that reported no health."""
    if not health:
        return None
    logins = health.get("logins") or {}
    return disk_band(health), tuple(sorted((str(name), str(state)) for name, state in logins.items()))


async def worker_health(conn: AsyncConnection, worker_id: int, before: dict | None, after: dict | None) -> list[int]:
    """Write ``worker.health`` for each project worker ``worker_id`` serves when its health went from ``before`` to
    ``after`` (each a stored report, workers.health) and the state changed (``health_state``); the event ids. A
    heartbeat that reports no health writes none, nor a worker drained or revoked: its owner already stopped it, and
    its alerts would only repeat (the login of a paused machine flaps between states)."""
    state = health_state(after)
    if state is None or state == health_state(before):
        return []
    w = tables.workers
    stopped = await conn.scalar(
        select((w.c.drained_at.is_not(None)) | (w.c.revoked_at.is_not(None))).where(w.c.id == worker_id)
    )
    if stopped:
        return []
    assert after is not None
    disk = after.get("disk") or {}
    payload = {
        "v": VERSION,
        "worker_id": worker_id,
        "disk": state[0],
        "disk_free_pct": disk.get("free_pct"),
        "logins": dict(state[1]),
    }
    wp = tables.worker_projects
    served = await conn.scalars(select(wp.c.project_id).where(wp.c.worker_id == worker_id).order_by(wp.c.project_id))
    return [await hub_events.record(conn, hub_events.WORKER_HEALTH, project_id, payload) for project_id in served]


__all__ = [
    "DISK_HEADROOM_GB",
    "DISK_LOW_GB",
    "ENDED_STATES",
    "LOGGED_IN",
    "LOGIN_STATES",
    "curator_paused",
    "decision_opened",
    "disk_band",
    "disk_reserve",
    "health_state",
    "pause_class",
    "run_moved",
    "with_band",
    "worker_health",
]

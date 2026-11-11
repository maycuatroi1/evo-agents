"""The operations events of a project (plan hub-steward, criterion a1): what the hub writes into hub_events when a run
of the project fails or is lost (``run.ended``), is parked (``run.parked``), asks a person a decision
(``decision.opened``), when the circuit breaker pauses its night shift (``curator.paused``), and when the health of a
worker that serves it changes (``worker.health``).

Each one is written in the transaction that made what it tells of, through ``hub_events.record``, so it exists exactly
when that was committed. Its payload carries ids and states alone, never a text: no error, no question, no reason. No
plugin takes these kinds (they are not in ``evo_agents.plugins.protocol.EVENT_KINDS``), and they queue no tick of the
Loop.

A worker belongs to no project, so its health is written once for each project it serves (worker_projects), and only
when its state changes: the band of its free disk (``DISK_LOW_GB``) or the login of one of its runtimes. The
percentage of free disk rides along but changes nothing by itself.

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, tables

ENDED_STATES = ("failed", "lost")  # the moves of a run that write run.ended
DISK_LOW_GB = 20.0  # below this much free disk on its worktrees' drive a worker's disk is low
LOGIN_STATES = ("ok", "unauthorized", "missing", "unknown")  # a runtime's login, as its daemon checked it
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


def disk_band(health: dict | None) -> str | None:
    """``low`` or ``ok`` for the free disk a stored health report says, None when it says nothing of it."""
    disk = (health or {}).get("disk") or {}
    free = disk.get("free_gb")
    if not isinstance(free, int | float) or isinstance(free, bool):
        return None
    return "low" if free < DISK_LOW_GB else "ok"


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
    heartbeat that reports no health writes none."""
    state = health_state(after)
    if state is None or state == health_state(before):
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
    "DISK_LOW_GB",
    "ENDED_STATES",
    "LOGIN_STATES",
    "curator_paused",
    "decision_opened",
    "disk_band",
    "health_state",
    "pause_class",
    "run_moved",
    "worker_health",
]

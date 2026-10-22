"""The hub's own events of a project, which the Loop reads by cursor (plan living-lean-canvas, criterion a6; plan
loop-engine, criterion a3, adds an approved version of an SRS document and a change of a task's status to the same
table).

The table hub_events (0077) is only ever added to: one row per event, its project, its kind, its payload and when it
was written, with an id that only grows. ``record`` writes one in the caller's transaction, the transaction that made
the event, so an event exists exactly when what it tells of was committed; ``plugin_events.emit`` calls it for the
kinds of KINDS beside the deliveries to plugins, from the same call. Nothing updates or deletes a row: a project
deleted takes its rows with it, nothing else does.

A cursor is the id of the last row read. Ids come from one identity, but two transactions may take ids in one order
and commit in the other, and a reader that saw the later id first would pass the earlier one. So ``record`` first
takes a transaction-level advisory lock of the project, held until commit: the events of one project are written one
transaction after the other, and their ids grow in the order they become visible. ``after`` reads the rows of one
project past a cursor in that order; a reader keeps one cursor per project.

An event of a project whose Loop is on also queues a tick of the Loop in the same transaction
(``loop_jobs.on_event``), so a tick follows each event; a project whose Loop is off gets none (plan loop-engine,
criteria a4 and a11).

The payload carries ids and states, never a text a project's labels may keep from a reader: the same payload a plugin
granted the event's permission gets (``plugin_events``), whose ``v`` (VERSION) is the version of its shape.

SQLAlchemy Core on ``evo_agents.hub.tables``; nothing here imports ``evo_agents.hub.server``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, func, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import loop_jobs, tables

CLAIM = "canvas.claim"  # a rev of a claim of a Lean Canvas (core plugin canvas)
BASELINE = "canvas.baseline"  # a baseline of a canvas, approved
SIGNALS = "signal.posted"  # a batch of a project's signals the hub took (``evo_agents.hub.signals``)
SRS_APPROVED = "srs.version.approved"  # a version of an SRS document, approved (core plugin srs)
TASK_STATUS = "tasks.status"  # a task's status, set or changed (``evo_agents.hub.task_status``)
KINDS = (CLAIM, BASELINE, SIGNALS, SRS_APPROVED, TASK_STATUS)  # what this release writes
KIND = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$"  # hub_events.kind, as the check of 0077 holds it
VERSION = 1  # the ``v`` of every payload this release writes
LOCK_BASE = 0x6875626576000000  # "hubev" in ASCII, shifted: a project's lock is this plus its id
MAX_READ = 1000  # rows ``after`` reads at once


@dataclass(frozen=True)
class HubEvent:
    id: int
    kind: str
    payload: dict
    created_at: datetime


async def record(conn: AsyncConnection, kind: str, project_id: int, payload: dict) -> int:
    """Write event ``kind`` of project ``project_id`` in the caller's transaction, after every event of the project
    an earlier transaction wrote, and queue a tick of its Loop when that is on; its id."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a kind of hub event this release writes")
    await conn.execute(select(func.pg_advisory_xact_lock(literal(LOCK_BASE, BigInteger) + project_id)))
    e = tables.hub_events
    statement = insert(e).values(project_id=project_id, kind=kind, payload=payload)
    event_id = await conn.scalar(statement.returning(e.c.id))
    await loop_jobs.on_event(conn, project_id)
    return event_id


async def after(conn: AsyncConnection, project_id: int, cursor: int = 0, limit: int = MAX_READ) -> list[HubEvent]:
    """The events of project ``project_id`` past ``cursor`` (an id read before, 0 for the first), oldest first, at
    most ``limit`` of them."""
    e = tables.hub_events
    query = (
        select(e.c.id, e.c.kind, e.c.payload, e.c.created_at)
        .where(e.c.project_id == project_id, e.c.id > cursor)
        .order_by(e.c.id)
        .limit(max(1, min(limit, MAX_READ)))
    )
    return [HubEvent(**row._mapping) for row in await conn.execute(query)]


__all__ = ["BASELINE", "CLAIM", "KINDS", "SIGNALS", "SRS_APPROVED", "TASK_STATUS", "HubEvent", "after", "record"]

"""Every query of the ops actions of an Orchestrator in an ops thread (``ops.service.actions`` is the design): the
batch of a thread, its items, the decisions, schedules, notices, findings and runs of its project."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, tables
from evo_agents.hub.server.ops.models.rules import ORCHESTRATOR


async def batch_of_thread(conn: AsyncConnection, thread_id: int):
    """The batch of ops thread ``thread_id``, its row locked."""
    b = tables.ops_batches
    return (await conn.execute(select(b).where(b.c.thread_id == thread_id).with_for_update())).one()


async def mark_handled(conn: AsyncConnection, batch_id: int, **where) -> int:
    """Mark the pending items of batch ``batch_id`` whose columns equal ``where`` handled by the Orchestrator."""
    i = tables.ops_items
    query = update(i).values(state="handled", handled_by=ORCHESTRATOR, handled_at=func.now())
    query = query.where(i.c.batch_id == batch_id, i.c.state == "pending")
    for column, value in where.items():
        query = query.where(i.c[column] == value)
    return (await conn.execute(query)).rowcount or 0


async def has_item(conn: AsyncConnection, batch_id: int, column: str, value: int) -> bool:
    """Whether batch ``batch_id`` has an item whose ``column`` is ``value``."""
    i = tables.ops_items
    query = select(i.c.id).where(i.c.batch_id == batch_id, i.c[column] == value).limit(1)
    return (await conn.execute(query)).first() is not None


async def has_stalled(conn: AsyncConnection, batch_id: int, subject: str) -> bool:
    """Whether batch ``batch_id`` has a ``plan.stalled`` item of ``subject``."""
    i = tables.ops_items
    item = select(i.c.id).where(i.c.batch_id == batch_id, i.c.kind == hub_events.PLAN_STALLED, i.c.subject == subject)
    return (await conn.execute(item.limit(1))).first() is not None


async def decision_row(conn: AsyncConnection, decision_id: int):
    """(category, state, project_id, plan_id) of decision ``decision_id``; None for none."""
    d = tables.decisions
    query = select(d.c.category, d.c.state, d.c.project_id, d.c.plan_id).where(d.c.id == decision_id)
    return (await conn.execute(query)).one_or_none()


async def resume_schedules(conn: AsyncConnection, project_id: int) -> int:
    """Let the schedules of the project its breaker paused (none a person paused) run again; how many."""
    s = tables.schedules
    resumed = (
        update(s)
        .values(paused_at=None, paused_by=None, pause_reason=None, updated_at=func.now())
        .where(s.c.project_id == project_id, s.c.paused_at.is_not(None), s.c.paused_by.is_(None))
    )
    return (await conn.execute(resumed)).rowcount or 0


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()


async def default_label(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return (await conn.execute(select(p.c.default_label).where(p.c.id == project_id))).scalar_one()


async def mark_notice_read(conn: AsyncConnection, notification_id: int, project_id: int) -> bool:
    """Mark notice ``notification_id`` of the project read; whether the project has it."""
    n = tables.notifications
    query = (
        update(n)
        .values(read_at=func.coalesce(n.c.read_at, func.now()))
        .where(n.c.id == notification_id, n.c.project_id == project_id, n.c.kind == "notice")
    )
    return bool((await conn.execute(query)).rowcount)


async def batch_evidence(conn: AsyncConnection, batch_id: int) -> list[dict]:
    """The run, decision and cause of the first 20 items of batch ``batch_id``, without the empty ones."""
    i = tables.ops_items
    items = select(i.c.run_id, i.c.decision_id, i.c.cause).where(i.c.batch_id == batch_id).order_by(i.c.id).limit(20)
    return [
        {key: value for key, value in row._mapping.items() if value is not None} for row in await conn.execute(items)
    ]


async def insert_finding(conn: AsyncConnection, values: dict) -> int:
    f = tables.findings
    return (await conn.execute(insert(f).values(**values).returning(f.c.id))).scalar_one()


async def run_in_project(conn: AsyncConnection, run_id: int, project_id: int) -> bool:
    r = tables.runs
    found = select(r.c.id).where(r.c.id == run_id, r.c.project_id == project_id)
    return (await conn.execute(found)).first() is not None


async def system_slug(conn: AsyncConnection, system_id: int) -> str:
    s = tables.agent_systems
    return (await conn.execute(select(s.c.slug).where(s.c.id == system_id))).scalar_one()

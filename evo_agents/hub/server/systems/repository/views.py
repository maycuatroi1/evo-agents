"""The reads of the traces of an agent system for its routes (``service.views``): the traces of a system by slug, their
spans with the project of each run, the traces of a window, and the clock of the database."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context, tables


async def now(conn: AsyncConnection) -> datetime:
    return (await conn.execute(select(func.now()))).scalar_one()


async def system_id(conn: AsyncConnection, project_id: int, slug: str) -> int | None:
    s = tables.agent_systems
    query = select(s.c.id).where(s.c.project_id == project_id, s.c.slug == slug)
    return (await conn.execute(query)).scalar_one_or_none()


async def has_trace(conn: AsyncConnection, project_id: int, slug: str) -> bool:
    """Whether the project holds a trace of a system ``slug``, which may be deleted since."""
    t = tables.system_traces
    held = select(t.c.id).where(t.c.project_id == project_id, t.c.system_slug == slug).limit(1)
    return (await conn.execute(held)).first() is not None


async def traces(
    conn: AsyncConnection,
    project_id: int,
    slug: str,
    limit: int,
    *,
    before: int | None = None,
    trace_id: int | None = None,
    moved_after: datetime | None = None,
) -> list:
    """The traces of system ``slug`` of the project, the newest first, at most ``limit``: older than trace ``before``,
    only ``trace_id``, or only those whose last_at passed ``moved_after``, when given."""
    t, u = tables.system_traces, tables.users
    query = (
        select(
            t.c.id,
            t.c.system_slug,
            t.c.system_revision,
            t.c.chat_id,
            t.c.message_id,
            u.c.login,
            t.c.work_chat_id,
            t.c.goal,
            t.c.done_when,
            t.c.started_at,
            t.c.last_at,
        )
        .outerjoin(u, u.c.id == t.c.opened_by)
        .where(t.c.project_id == project_id, t.c.system_slug == slug)
    )
    if before is not None:
        query = query.where(t.c.id < before)
    if trace_id is not None:
        query = query.where(t.c.id == trace_id)
    if moved_after is not None:
        query = query.where(t.c.last_at > moved_after)
    return list(await conn.execute(query.order_by(t.c.id.desc()).limit(limit)))


async def spans(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, list]:
    """The spans of each trace, in the order written, each with the name of its run's ``project``."""
    s, p = tables.system_spans, tables.projects
    query = (
        select(s, p.c.name.label("project"))
        .outerjoin(p, p.c.id == s.c.project_id)
        .where(s.c.trace_id.in_(trace_ids))
        .order_by(s.c.trace_id, s.c.id)
    )
    found: dict[int, list] = {trace_id: [] for trace_id in trace_ids}
    for row in await conn.execute(query):
        found[row.trace_id].append(row)
    return found


async def window(conn: AsyncConnection, project_id: int, slug: str, since: datetime, limit: int) -> list:
    """The traces of system ``slug`` that started at or after ``since``, the newest first, at most ``limit``."""
    t = tables.system_traces
    query = (
        select(t.c.id, t.c.system_id, t.c.system_revision, t.c.chat_id, t.c.work_chat_id, t.c.started_at, t.c.last_at)
        .where(t.c.project_id == project_id, t.c.system_slug == slug, t.c.started_at >= since)
        .order_by(t.c.id.desc())
        .limit(limit)
    )
    return list(await conn.execute(query))


async def running(conn: AsyncConnection, trace_ids: list[int]) -> set[int]:
    """Those of ``trace_ids`` with a turn still running."""
    s = tables.system_spans
    query = select(s.c.trace_id).where(
        s.c.trace_id.in_(trace_ids), s.c.name == system_context.INVOKE, s.c.status == "running"
    )
    return set((await conn.execute(query.distinct())).scalars())


async def moved(conn: AsyncConnection, project_id: int, slug: str, since: datetime, active, limit: int) -> list:
    """The traces of system ``slug`` that moved at or after ``since`` or are in ``active`` (a query of trace ids), the
    newest first, at most ``limit``."""
    t = tables.system_traces
    query = (
        select(t.c.id, t.c.chat_id, t.c.work_chat_id, t.c.started_at, t.c.last_at)
        .where(t.c.project_id == project_id, t.c.system_slug == slug)
        .where((t.c.last_at >= since) | t.c.id.in_(active))
        .order_by(t.c.id.desc())
        .limit(limit)
    )
    return list(await conn.execute(query))

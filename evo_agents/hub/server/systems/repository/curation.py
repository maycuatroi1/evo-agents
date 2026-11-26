"""The rows the Curator's lens ``agent_system`` reads and writes (``service.curation``): the system of a span, the
traces and spans of a window, the decisions an accepted proposal takes on the project's graph, and the system change a
proposal keeps."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def span_system(conn: AsyncConnection, project_id: int, span_id: int) -> str | None:
    """The slug of the system whose trace holds span ``span_id``, when that trace is of project ``project_id``."""
    s, t = tables.system_spans, tables.system_traces
    query = select(t.c.system_slug).join_from(s, t, t.c.id == s.c.trace_id)
    return await conn.scalar(query.where(s.c.id == span_id, t.c.project_id == project_id))


async def system_slug(conn: AsyncConnection, project_id: int, system_id: int) -> str | None:
    s = tables.agent_systems
    return await conn.scalar(select(s.c.slug).where(s.c.id == system_id, s.c.project_id == project_id))


async def traces_since(conn: AsyncConnection, project_id: int, slugs: list[str], since: datetime) -> int:
    """The traces of systems ``slugs`` that started at or after ``since``."""
    t = tables.system_traces
    query = select(func.count()).where(
        t.c.project_id == project_id, t.c.system_slug.in_(slugs), t.c.started_at >= since
    )
    return int(await conn.scalar(query) or 0)


async def traced(conn: AsyncConnection, project_id: int, slugs: list[str], since) -> bool:
    """Whether a trace of systems ``slugs`` moved at or after ``since``."""
    t = tables.system_traces
    found = await conn.scalar(
        select(t.c.id).where(t.c.project_id == project_id, t.c.system_slug.in_(slugs), t.c.last_at >= since).limit(1)
    )
    return found is not None


async def spans(conn: AsyncConnection, project_id: int, slug: str, since) -> list:
    """The spans of the traces of system ``slug`` that started at or after ``since``, the newest first."""
    s, t = tables.system_spans, tables.system_traces
    query = (
        select(s.c.id, s.c.name, s.c.tool, s.c.node, s.c.from_node, s.c.to_node, s.c.status, s.c.off_design)
        .add_columns(s.c.attributes, s.c.run_id)
        .join_from(s, t, t.c.id == s.c.trace_id)
        .where(t.c.project_id == project_id, t.c.system_slug == slug, s.c.started_at >= since)
        .order_by(s.c.id.desc())
    )
    return list(await conn.execute(query))


async def lock_project(conn: AsyncConnection, project_id: int) -> None:
    p = tables.projects
    await conn.execute(select(p.c.id).where(p.c.id == project_id).with_for_update())


async def ontology(conn: AsyncConnection, project_id: int):
    k = tables.kg_configs
    return await conn.scalar(select(k.c.ontology).where(k.c.project_id == project_id))


async def same_decision(conn: AsyncConnection, project_id: int, subject: dict) -> int | None:
    """The number of a proposed or active decision of the project that says what ``subject`` says."""
    d = tables.kg_decisions
    same = [d.c[key].is_not_distinct_from(value) for key, value in subject.items()]
    return await conn.scalar(
        select(d.c.number).where(d.c.project_id == project_id, d.c.state.in_(("proposed", "active")), *same)
    )


async def add_decision(
    conn: AsyncConnection, project_id: int, subject: dict, user_id: int | None, reason: str, origin: str, run_id: int
) -> int:
    """Write ``subject`` as the project's next decision, active at once, and its event; its number."""
    d, e = tables.kg_decisions, tables.kg_decision_events
    rev = (
        await conn.execute(select(func.coalesce(func.max(e.c.rev), 0)).where(e.c.project_id == project_id))
    ).scalar_one()
    number = (
        await conn.execute(select(func.coalesce(func.max(d.c.number), 0) + 1).where(d.c.project_id == project_id))
    ).scalar_one()
    values = {"project_id": project_id, "number": number, "state": "active", **subject}
    decision_id = (await conn.execute(d.insert().values(**values).returning(d.c.id))).scalar_one()
    event = {"project_id": project_id, "rev": rev + 1, "decision_id": decision_id, "action": "decide"}
    await conn.execute(e.insert().values(**event, actor_id=user_id, reason=reason, origin=origin, run_id=run_id))
    return int(number)


async def keep_change(conn: AsyncConnection, proposal_id: int, stored: dict) -> None:
    p = tables.proposals
    await conn.execute(update(p).values(system_change=stored).where(p.c.id == proposal_id))

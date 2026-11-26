"""The decisions on a project's knowledge graph in the database: tables kg_decisions and kg_decision_events, the
lock of a project's decisions (its row of projects) and the ontology of its graph (kg_configs)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

OPEN = ("proposed", "active")  # the states of a decision that says something now


async def decisions_of(conn: AsyncConnection, project_id: int) -> list:
    """The project's decisions, newest first."""
    d = tables.kg_decisions
    return list((await conn.execute(select(d).where(d.c.project_id == project_id).order_by(d.c.number.desc()))).all())


async def events_of(conn: AsyncConnection, project_id: int) -> list:
    """Every step of the project's decisions, by revision, with the login of the member who took it (None for a core
    plugin that took it as itself)."""
    e, u = tables.kg_decision_events, tables.users
    query = (
        select(
            e.c.decision_id,
            e.c.rev,
            e.c.action,
            u.c.login,
            e.c.actor_plugin,
            e.c.at,
            e.c.reason,
            e.c.build_id,
            e.c.origin,
            e.c.run_id,
        )
        .join(u, u.c.id == e.c.actor_id, isouter=True)
        .where(e.c.project_id == project_id)
        .order_by(e.c.rev)
    )
    return list((await conn.execute(query)).all())


async def locked_rev(conn: AsyncConnection, project_id: int) -> int:
    """Lock the project's decisions for this transaction; their revision now (0 before the first)."""
    p, e = tables.projects, tables.kg_decision_events
    await conn.execute(select(p.c.id).where(p.c.id == project_id).with_for_update())
    return await conn.scalar(select(func.coalesce(func.max(e.c.rev), 0)).where(e.c.project_id == project_id))  # pyright: ignore[reportReturnType]


async def ontology(conn: AsyncConnection, project_id: int):
    """The ontology of the project's graph, as its configuration holds it; None without one."""
    configs = tables.kg_configs
    return await conn.scalar(select(configs.c.ontology).where(configs.c.project_id == project_id))


async def saying(conn: AsyncConnection, project_id: int, kind: str, src: str, rel: str | None, dst: str | None):
    """The number of a proposed or active decision of the project that says that; None without one."""
    d = tables.kg_decisions
    same = select(d.c.number).where(
        d.c.project_id == project_id,
        d.c.kind == kind,
        d.c.src == src,
        d.c.rel.is_not_distinct_from(rel),
        d.c.dst.is_not_distinct_from(dst),
        d.c.state.in_(OPEN),
    )
    return await conn.scalar(same.limit(1))


async def said(conn: AsyncConnection, project_id: int) -> set[tuple]:
    """(kind, src, rel, dst) of each proposed or active decision of the project."""
    d = tables.kg_decisions
    query = select(d.c.kind, d.c.src, d.c.rel, d.c.dst).where(d.c.project_id == project_id, d.c.state.in_(OPEN))
    return {(row.kind, row.src, row.rel, row.dst) for row in await conn.execute(query)}


async def last_number(conn: AsyncConnection, project_id: int) -> int:
    """The number of the project's newest decision; 0 before the first."""
    d = tables.kg_decisions
    return await conn.scalar(select(func.coalesce(func.max(d.c.number), 0)).where(d.c.project_id == project_id))  # pyright: ignore[reportReturnType]


async def insert(
    conn: AsyncConnection,
    project_id: int,
    number: int,
    kind: str,
    state: str,
    src: str,
    rel: str | None,
    dst: str | None,
):
    """Write decision ``number`` of the project; its id and number."""
    d = tables.kg_decisions
    values = {"project_id": project_id, "number": number, "kind": kind, "state": state, "src": src}
    query = d.insert().values(**values, rel=rel, dst=dst).returning(d.c.id, d.c.number)
    return (await conn.execute(query)).one()


async def insert_event(
    conn: AsyncConnection,
    project_id: int,
    decision_id: int,
    action: str,
    *,
    rev: int,
    actor_id: int | None,
    reason: str,
    build_id: int | None,
    origin: str | None,
    run_id: int | None,
) -> None:
    """Write the step ``action`` of decision ``decision_id``, the project's decision revision ``rev``."""
    await conn.execute(
        tables.kg_decision_events.insert().values(
            project_id=project_id,
            rev=rev,
            decision_id=decision_id,
            action=action,
            actor_id=actor_id,
            reason=reason,
            build_id=build_id,
            origin=origin,
            run_id=run_id,
        )
    )


async def numbered(conn: AsyncConnection, project_id: int, number: int):
    """The id, number and state of decision ``number`` of the project."""
    d = tables.kg_decisions
    query = select(d.c.id, d.c.number, d.c.state).where(d.c.project_id == project_id, d.c.number == number)
    return (await conn.execute(query)).one()


async def set_state(conn: AsyncConnection, decision_id: int, state: str) -> None:
    d = tables.kg_decisions
    await conn.execute(d.update().where(d.c.id == decision_id).values(state=state))


async def now(conn: AsyncConnection):
    """The time of the transaction."""
    return await conn.scalar(select(func.now()))

"""The candidates of an evolution (curator_evolve_candidates) and the project's cache of baseline arms
(curator_evolve_arm_cache)."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def of_evolution(conn: AsyncConnection, evolve_id: int) -> list:
    ec = tables.curator_evolve_candidates
    return list((await conn.execute(select(ec).where(ec.c.evolve_id == evolve_id).order_by(ec.c.id))).all())


async def measures(conn: AsyncConnection, evolve_id: int) -> list:
    """(id, scores, cost_usd, turns) of each candidate of the evolution, in order."""
    ec = tables.curator_evolve_candidates
    query = select(ec.c.id, ec.c.scores, ec.c.cost_usd, ec.c.turns).where(ec.c.evolve_id == evolve_id).order_by(ec.c.id)
    return list((await conn.execute(query)).all())


async def mark_front(conn: AsyncConnection, evolve_id: int, on: list[int]) -> None:
    """The candidates ``on`` are on the evolution's front, the others not."""
    ec = tables.curator_evolve_candidates
    await conn.execute(
        update(ec).values(on_front=ec.c.id.in_(on), updated_at=func.now()).where(ec.c.evolve_id == evolve_id)
    )


async def known_parents(conn: AsyncConnection, evolve_id: int, parents: list[int]) -> int:
    """How many of ``parents`` are candidates of the evolution."""
    ec = tables.curator_evolve_candidates
    query = select(func.count()).where(ec.c.evolve_id == evolve_id, ec.c.id.in_(parents))
    return await conn.scalar(query) or 0


async def kept(conn: AsyncConnection, values: dict, measured: dict) -> int:
    """Keep candidate ``values``; one of the same digest takes the ``measured`` values instead. Its id."""
    ec = tables.curator_evolve_candidates
    query = pg_insert(ec).values(**values)
    query = query.on_conflict_do_update(
        constraint="curator_evolve_candidates_evolve_id_digest_key", set_={**measured, "updated_at": func.now()}
    )
    return (await conn.execute(query.returning(ec.c.id))).scalar_one()


async def components(conn: AsyncConnection, evolve_id: int, *, origin: str | None = None, candidate_id=None):
    """The components of the evolution's first candidate of ``origin``, else of candidate ``candidate_id``; or None."""
    ec = tables.curator_evolve_candidates
    query = select(ec.c.components).where(ec.c.evolve_id == evolve_id)
    query = query.where(ec.c.origin == origin) if origin else query.where(ec.c.id == candidate_id)
    return (await conn.execute(query.order_by(ec.c.id).limit(1))).scalar_one_or_none()


async def cache_arm(conn: AsyncConnection, values: dict) -> str | None:
    """Keep a baseline arm by its key; its key, or None when the cache held it already."""
    ac = tables.curator_evolve_arm_cache
    query = pg_insert(ac).values(**values).on_conflict_do_nothing(index_elements=["project_id", "key"])
    return await conn.scalar(query.returning(ac.c.key))


async def cached_arms(conn: AsyncConnection, row, task_ids: list) -> list:
    """(key, task_kind, task_id, result) of the arms the project's cache holds on evolution ``row``'s runtime and model
    for tasks ``task_ids``, the oldest first."""
    ac = tables.curator_evolve_arm_cache
    query = select(ac.c.key, ac.c.task_kind, ac.c.task_id, ac.c.result).where(
        ac.c.project_id == row.project_id,
        ac.c.runtime == row.runtime,
        ac.c.model.is_not_distinct_from(row.model),
        ac.c.task_id.in_(task_ids),
    )
    return list((await conn.execute(query.order_by(ac.c.created_at))).all())

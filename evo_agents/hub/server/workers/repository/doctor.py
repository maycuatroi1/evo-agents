"""The queries of the doctor reports of workers: whether a report is fresh, the worker a token belongs to, the
newest report kept on its row, and whether it is shared and narrowed to units."""

from __future__ import annotations

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables


def doctor_fresh(doctor_at):
    """Whether a report taken at ``doctor_at`` (a column) is fresh enough to narrow its worker, or to let a node of a
    pool take runs, as a condition: no older than ``runs.DOCTOR_FRESH_SECONDS``."""
    return doctor_at >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, runs.DOCTOR_FRESH_SECONDS)


async def worker_of_token(conn: AsyncConnection, token_id: int):
    """(id, owner_id, name, doctor_high, revoked_at) of the worker whose token is ``token_id``, its row locked; None
    for none."""
    w = tables.workers
    query = select(w.c.id, w.c.owner_id, w.c.name, w.c.doctor_high, w.c.revoked_at).where(w.c.token_id == token_id)
    return (await conn.execute(query.with_for_update())).one_or_none()


async def keep_report(conn: AsyncConnection, worker_id: int, high: int, medium: int, findings: list[dict]):
    """Keep the report on the worker's row; when the hub took it."""
    w = tables.workers
    taken = await conn.execute(
        update(w)
        .where(w.c.id == worker_id)
        .values(doctor_at=func.now(), doctor_high=high, doctor_medium=medium, doctor_findings=findings)
        .returning(w.c.doctor_at)
    )
    return taken.scalar_one()


async def narrowed_and_shared(conn: AsyncConnection, worker_id: int) -> bool:
    ws, w = tables.worker_shares, tables.workers
    narrowed = exists().where(ws.c.worker_id == worker_id)
    query = select(w.c.shared & narrowed).where(w.c.id == worker_id)
    return bool((await conn.execute(query)).scalar_one())

"""The SQL of a project's review queue (``evo_agents.hub.server.kg_review``): its dismissals, kept by fingerprint,
with the login of the member who made each (none for a core plugin's), and the project of the run a loop names."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def dismissals(conn: AsyncConnection, project_id: int) -> list:
    """The project's dismissals: fingerprint, reason, login, dismissed_by_plugin, dismissed_at, build_id."""
    d, u = tables.kg_review_dismissals, tables.users
    rows = await conn.execute(
        select(d.c.fingerprint, d.c.reason, u.c.login, d.c.dismissed_by_plugin, d.c.dismissed_at, d.c.build_id)
        .join(u, u.c.id == d.c.dismissed_by, isouter=True)
        .where(d.c.project_id == project_id)
    )
    return list(rows)


async def save_dismissal(
    conn: AsyncConnection,
    *,
    project_id: int,
    fingerprint: str,
    rule: str,
    reason: str,
    user_id: int,
    build_id: int | None,
) -> datetime:
    """Dismiss the item ``fingerprint`` as ``user_id``, replacing a dismissal it had; when it was dismissed."""
    d = tables.kg_review_dismissals
    saved = pg_insert(d).values(
        project_id=project_id,
        fingerprint=fingerprint,
        rule=rule,
        reason=reason,
        dismissed_by=user_id,
        build_id=build_id,
    )
    row = (
        await conn.execute(
            saved.on_conflict_do_update(
                index_elements=[d.c.project_id, d.c.fingerprint],
                set_={
                    "rule": saved.excluded.rule,
                    "reason": saved.excluded.reason,
                    "dismissed_by": saved.excluded.dismissed_by,
                    "dismissed_by_plugin": None,
                    "build_id": saved.excluded.build_id,
                    "dismissed_at": func.now(),
                },
            ).returning(d.c.dismissed_at)
        )
    ).one()
    return row.dismissed_at


async def locked_dismissal(conn: AsyncConnection, project_id: int, fingerprint: str):
    """The dismissal of item ``fingerprint``, locked: login, dismissed_by_plugin; None while the item is open."""
    d, u = tables.kg_review_dismissals, tables.users
    return (
        await conn.execute(
            select(u.c.login, d.c.dismissed_by_plugin)
            .select_from(d.join(u, u.c.id == d.c.dismissed_by, isouter=True))
            .where(d.c.project_id == project_id, d.c.fingerprint == fingerprint)
            .with_for_update(of=d)
        )
    ).one_or_none()


async def delete_dismissal(conn: AsyncConnection, project_id: int, fingerprint: str) -> None:
    d = tables.kg_review_dismissals
    await conn.execute(d.delete().where(d.c.project_id == project_id, d.c.fingerprint == fingerprint))


async def run_project(conn: AsyncConnection, run_id: int) -> int | None:
    """The project of run ``run_id``; None for no such run."""
    r = tables.runs
    return await conn.scalar(select(r.c.project_id).where(r.c.id == run_id))

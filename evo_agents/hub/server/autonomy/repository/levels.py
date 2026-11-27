"""Every query of a person's own autonomy settings: the revisions of their preference in autonomy_prefs and their run
reach, ``users.run_reach``."""

from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def newest_pref(conn: AsyncConnection, user_id: int):
    """(revision, level, overrides) of the person's newest preference; None when they set none."""
    p = tables.autonomy_prefs
    query = (
        select(p.c.revision, p.c.level, p.c.overrides)
        .where(p.c.user_id == user_id)
        .order_by(p.c.revision.desc())
        .limit(1)
    )
    return (await conn.execute(query)).first()


async def add_pref(conn: AsyncConnection, user_id: int, revision: int, level: str, overrides: dict[str, str]) -> None:
    values = {"user_id": user_id, "revision": revision, "level": level, "overrides": overrides}
    await conn.execute(insert(tables.autonomy_prefs).values(values))


async def run_reach(conn: AsyncConnection, user_id: int, *, lock: bool = False):
    """The groups ``users.run_reach`` holds for the person, the row locked for an update when ``lock``."""
    u = tables.users
    query = select(u.c.run_reach).where(u.c.id == user_id)
    return (await conn.execute(query.with_for_update() if lock else query)).scalar_one()


async def set_run_reach(conn: AsyncConnection, user_id: int, groups: list[str]) -> None:
    u = tables.users
    await conn.execute(update(u).values(run_reach=groups).where(u.c.id == user_id))

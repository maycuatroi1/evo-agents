"""The SQL of the tags a release cycle follows (release_tags): reading them, following a tag a person pushed or the
hub made, and keeping what the hub read of a tag's CI."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def tags_of(conn: AsyncConnection, cycle_id: int) -> list:
    """The tags cycle ``cycle_id`` follows, in the order it began to."""
    t = tables.release_tags
    return list((await conn.execute(select(t).where(t.c.cycle_id == cycle_id).order_by(t.c.id))).all())


async def tags_of_cycles(conn: AsyncConnection, cycle_ids: list[int]) -> list:
    """The tags the cycles ``cycle_ids`` follow, in the order they began to."""
    t = tables.release_tags
    return list((await conn.execute(select(t).where(t.c.cycle_id.in_(cycle_ids)).order_by(t.c.id))).all())


async def tag_names(conn: AsyncConnection, cycle_id: int) -> list[str]:
    t = tables.release_tags
    return list((await conn.execute(select(t.c.tag).where(t.c.cycle_id == cycle_id))).scalars().all())


async def tag_of(conn: AsyncConnection, cycle_id: int, tag: str | None):
    """The row of ``tag`` that cycle ``cycle_id`` follows, or None."""
    t = tables.release_tags
    query = select(t).where(t.c.cycle_id == cycle_id, t.c.tag == tag)
    return (await conn.execute(query)).one_or_none()


async def tag_row(conn: AsyncConnection, tag_id: int):
    t = tables.release_tags
    return (await conn.execute(select(t).where(t.c.id == tag_id))).one()


async def insert_tag(conn: AsyncConnection, cycle_id: int, values: dict) -> None:
    """Follow the tag of ``values`` in cycle ``cycle_id``, checked now."""
    await conn.execute(insert(tables.release_tags).values(cycle_id=cycle_id, checked_at=func.now(), **values))


async def follow(conn: AsyncConnection, cycle_id: int, tag: str, commit: str) -> bool:
    """Follow ``tag`` at ``commit`` in cycle ``cycle_id`` unless it does already; whether it began to."""
    t = tables.release_tags
    made = upsert(t).values(cycle_id=cycle_id, tag=tag, commit=commit).on_conflict_do_nothing()
    return (await conn.execute(made.returning(t.c.id))).scalar_one_or_none() is not None


async def follow_tagged(conn: AsyncConnection, cycle_id: int, tag: str, commit: str, run_id: int) -> None:
    """Follow ``tag`` the hub made at ``commit`` for run ``run_id`` in cycle ``cycle_id``."""
    t = tables.release_tags
    followed = upsert(t).values(cycle_id=cycle_id, tag=tag, commit=commit, run_id=run_id)
    await conn.execute(  # a watch that saw the tag before this pass followed it as a person's: it is the run's
        followed.on_conflict_do_update(constraint="release_tags_cycle_id_tag_key", set_={"run_id": run_id})
    )


async def save_tag(conn: AsyncConnection, tag_id: int, *, watch_now: bool = False, **values) -> None:
    """Keep ``values`` on tag ``tag_id``, checked now; its deadlines counted from now too with ``watch_now``."""
    t = tables.release_tags
    if watch_now:
        values["watched_since"] = func.now()
    await conn.execute(update(t).where(t.c.id == tag_id).values(checked_at=func.now(), **values))


async def watch_again(conn: AsyncConnection, tag_id: int, ci_state: str) -> None:
    """Tag ``tag_id`` in ``ci_state`` again, its deadlines counted from now, its problem forgotten."""
    t = tables.release_tags
    await conn.execute(
        update(t).where(t.c.id == tag_id).values(ci_state=ci_state, watched_since=func.now(), problem=None)
    )

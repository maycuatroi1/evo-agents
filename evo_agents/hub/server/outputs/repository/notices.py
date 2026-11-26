"""The notices of a run's files (notifications of notice kind ``runs.OUTPUT_NOTICE`` and their deliveries), the files
they name and a channel may send, and the notices a member turned off (notification_mutes)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, runs, tables
from evo_agents.hub.db import one_of

# The notices a member turned off


async def muted(conn: AsyncConnection, user_id: int, notice_kind: str) -> bool:
    """Whether member ``user_id`` turned the notices of ``notice_kind`` off."""
    m = tables.notification_mutes
    found = select(m.c.user_id).where(m.c.user_id == user_id, m.c.notice_kind == notice_kind)
    return (await conn.execute(found)).first() is not None


async def muted_kinds(conn: AsyncConnection, user_id: int) -> set[str]:
    """The kinds of notice member ``user_id`` turned off."""
    m = tables.notification_mutes
    return set((await conn.execute(select(m.c.notice_kind).where(m.c.user_id == user_id))).scalars())


async def unmute(conn: AsyncConnection, user_id: int, kinds: list[str]) -> None:
    m = tables.notification_mutes
    await conn.execute(delete(m).where(m.c.user_id == user_id, m.c.notice_kind.in_(kinds)))


async def mute(conn: AsyncConnection, user_id: int, kinds: list[str]) -> None:
    rows = [{"user_id": user_id, "notice_kind": kind} for kind in kinds]
    await conn.execute(pg_insert(tables.notification_mutes).values(rows).on_conflict_do_nothing())


# The notice of a run's files


def _carried_by_question():
    o, k = tables.run_outputs, tables.run_output_links
    return exists().where(k.c.output_id == o.c.id, k.c.decision_id.is_not(None))


async def files(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> list:
    """The outputs of ``output_ids`` of run ``run_id`` a notice names: not of source tool, carried by no question,
    oldest first."""
    if not output_ids:
        return []
    o = tables.run_outputs
    query = select(o.c.id, o.c.name, o.c.size, o.c.caption).where(
        o.c.run_id == run_id, one_of(o.c.id, output_ids), o.c.source != outputs.TOOL, ~_carried_by_question()
    )
    return list((await conn.execute(query.order_by(o.c.id))).all())


def _untried(run_id: int):
    """The notices of run ``run_id``'s files that no channel has tried yet, newest first, locked."""
    n, dl = tables.notifications, tables.notification_deliveries
    tried = exists().where(dl.c.notification_id == n.c.id, or_(dl.c.state != "pending", dl.c.attempts > 0))
    return (
        select(n.c.id, n.c.user_id, n.c.details)
        .where(n.c.run_id == run_id, n.c.kind == "notice", n.c.notice_kind == runs.OUTPUT_NOTICE, ~tried)
        .order_by(n.c.id.desc())
        .with_for_update(of=n)
    )


async def untried(conn: AsyncConnection, run_id: int) -> list:
    """The notices of run ``run_id``'s files that no channel has tried yet (id, user_id, details), newest first,
    locked."""
    return list((await conn.execute(_untried(run_id))).all())


async def untried_of(conn: AsyncConnection, run_id: int, user_id: int):
    """The newest notice of run ``run_id``'s files to ``user_id`` that no channel has tried yet, locked; None."""
    mine = _untried(run_id).where(tables.notifications.c.user_id == user_id).limit(1)
    return (await conn.execute(mine)).one_or_none()


async def held(conn: AsyncConnection, notification_id: int) -> bool:
    """Whether every delivery of notification ``notification_id`` is pending, untried and locked now: false while the
    job's pass holds one (it is being sent)."""
    dl = tables.notification_deliveries
    of = dl.c.notification_id == notification_id
    total = (await conn.execute(select(func.count()).select_from(dl).where(of))).scalar_one()
    free = select(dl.c.id).where(of, dl.c.state == "pending", dl.c.attempts == 0).with_for_update(skip_locked=True)
    return len((await conn.execute(free)).all()) == total


async def rewrite(conn: AsyncConnection, notification_id: int, title: str, body: str, details: dict) -> None:
    n = tables.notifications
    changed = update(n).values(title=title, body=body, details=details)
    await conn.execute(changed.where(n.c.id == notification_id))


async def drop(conn: AsyncConnection, notification_id: int) -> None:
    """Delete notification ``notification_id``, its deliveries with it."""
    await conn.execute(delete(tables.notifications).where(tables.notifications.c.id == notification_id))


async def gather(conn: AsyncConnection, notification_id: int, wait: timedelta) -> None:
    """Have the deliveries of notification ``notification_id`` wait ``wait`` from now."""
    dl = tables.notification_deliveries
    await conn.execute(update(dl).values(next_at=func.now() + wait).where(dl.c.notification_id == notification_id))


async def run(conn: AsyncConnection, run_id: int):
    """Run ``run_id`` as its notice names it: id, kind, project_id, dispatched_by and project; None."""
    r, p = tables.runs, tables.projects
    query = select(r.c.id, r.c.kind, r.c.project_id, r.c.dispatched_by, p.c.name.label("project")).join_from(
        r, p, p.c.id == r.c.project_id
    )
    return (await conn.execute(query.where(r.c.id == run_id))).one_or_none()


async def told_details(conn: AsyncConnection, run_id: int, user_id: int) -> list:
    """The details of the notices of run ``run_id``'s files to ``user_id``."""
    n = tables.notifications
    query = select(n.c.details).where(
        n.c.run_id == run_id, n.c.user_id == user_id, n.c.notice_kind == runs.OUTPUT_NOTICE
    )
    return list((await conn.execute(query)).scalars())


# What a channel may send of a notification's files


def _shown():
    o = tables.run_outputs
    return select(o.c.id, o.c.name, o.c.media_type, o.c.size, o.c.sha256, o.c.caption).where(o.c.source != outputs.TOOL)


async def _ordered(conn: AsyncConnection, query) -> list:
    return list((await conn.execute(query.order_by(tables.run_outputs.c.id))).all())


async def decision_files(conn: AsyncConnection, decision_id: int) -> list:
    """The files decision ``decision_id`` carries that a channel may send (none of source tool), oldest first."""
    o, k = tables.run_outputs, tables.run_output_links
    carried = exists().where(k.c.output_id == o.c.id, k.c.decision_id == decision_id)
    return await _ordered(conn, _shown().where(carried))


async def run_files(conn: AsyncConnection, run_id: int, output_ids: list[int]) -> list:
    """The files of ``output_ids`` of run ``run_id`` a channel may send (none of source tool), oldest first."""
    o = tables.run_outputs
    return await _ordered(conn, _shown().where(o.c.run_id == run_id, one_of(o.c.id, output_ids)))

"""The queries of the routes of the hub's merge queue (``service.view``): the merge requests of a project and their
ledger, its audit samples, and what its figures count."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import post_merge, tables
from evo_agents.hub.server.merge.models.view import MAX_EVENTS, MAX_SAMPLES


async def requests(conn: AsyncConnection, project_id: int, repo: str | None, limit: int) -> list:
    """The newest ``limit`` merge requests of the project's queue (``repo``'s alone when given)."""
    q = tables.merge_queue
    query = select(q).where(q.c.project_id == project_id)
    if repo is not None:
        query = query.where(q.c.repo == repo)
    return list((await conn.execute(query.order_by(q.c.id.desc()).limit(limit))).all())


async def repo_of(conn: AsyncConnection, queue_id: int) -> str | None:
    q = tables.merge_queue
    return (await conn.execute(select(q.c.repo).where(q.c.id == queue_id))).scalar_one_or_none()


async def events(conn: AsyncConnection, ids: list[int]) -> dict[int, list]:
    """The newest MAX_EVENTS lines of the ledger of each merge request ``ids``, the newest first."""
    e = tables.merge_events
    found: dict[int, list] = {row_id: [] for row_id in ids}
    if ids:
        ranked = select(e, func.row_number().over(partition_by=e.c.queue_id, order_by=e.c.id.desc()).label("n"))
        ranked = ranked.where(e.c.queue_id.in_(ids)).subquery()
        query = select(ranked).where(ranked.c.n <= MAX_EVENTS).order_by(ranked.c.queue_id, ranked.c.id.desc())
        for event in await conn.execute(query):
            found[event.queue_id].append(event)
    return found


def _samples(project_id: int):
    s, q, u = tables.merge_samples, tables.merge_queue, tables.users
    return (
        select(s, q.c.repo, q.c.iid, q.c.title, q.c.web_url, u.c.login.label("answered_login"))
        .join_from(s, q, q.c.id == s.c.queue_id)
        .join(u, u.c.id == s.c.answered_by, isouter=True)
        .where(s.c.project_id == project_id)
    )


async def samples(conn: AsyncConnection, project_id: int, repo: str | None) -> list:
    """The project's audit samples (``repo``'s alone when given), those waiting for an answer first, at most
    MAX_SAMPLES, with their merge request and who answered."""
    s, q = tables.merge_samples, tables.merge_queue
    query = _samples(project_id)
    if repo is not None:
        query = query.where(q.c.repo == repo)
    query = query.order_by(s.c.result.is_not(None), s.c.id.desc()).limit(MAX_SAMPLES)
    return list((await conn.execute(query)).all())


async def sample(conn: AsyncConnection, project_id: int, sample_id: int):
    """Audit sample ``sample_id`` of the project, with its merge request and who answered."""
    s = tables.merge_samples
    return (await conn.execute(_samples(project_id).where(s.c.id == sample_id))).one()


def _seconds(start, end):
    return func.extract("epoch", end - start)


async def figure_rows(conn: AsyncConnection, project_id: int, repo: str | None, days: int) -> tuple[list, set, set]:
    """(the merges of the last ``days``, a revert aside, as dicts; the ids of those PROD went back for; the ids of
    those an admin sampled faulty)."""
    q, e, s = tables.merge_queue, tables.merge_events, tables.merge_samples
    since = func.now() - timedelta(days=days)
    query = select(
        q.c.id,
        q.c.repo,
        q.c.source,
        q.c.profile,
        q.c.model,
        q.c.state,
        q.c.verdict[("first", "passed")].as_boolean().label("judged"),
        _seconds(q.c.queued_at, q.c.merged_at).label("waited"),
        _seconds(q.c.merged_at, q.c.uat_at).label("to_uat"),
        _seconds(q.c.merged_at, q.c.prod_at).label("to_prod"),
    ).where(
        q.c.project_id == project_id,
        q.c.merged_at >= since,
        q.c.source != post_merge.REVERT,
    )
    if repo is not None:
        query = query.where(q.c.repo == repo)
    rows = [dict(row._mapping) for row in await conn.execute(query)]
    ids = [row["id"] for row in rows]
    rolled = select(e.c.queue_id).where(e.c.queue_id.in_(ids), e.c.kind == "rollback").distinct()
    faulty = select(s.c.queue_id).where(s.c.queue_id.in_(ids), s.c.result == "faulty")
    rolled_back = {row_id for (row_id,) in await conn.execute(rolled)} if ids else set()
    found_faulty = {row_id for (row_id,) in await conn.execute(faulty)} if ids else set()
    return rows, rolled_back, found_faulty

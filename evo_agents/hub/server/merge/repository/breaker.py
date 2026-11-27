"""The queries of the breaker of each repo of the hub's merge queue (merge_breakers), and of what trips it: the merges
of the repo and the lines of their ledger (``service.breaker``)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_breaker as rules
from evo_agents.hub import post_merge, tables
from evo_agents.hub.server.merge.models.breaker import BLOCKING, MAX_FAILURES, MISSED, NOTICE


def _repo(q, project_id: int, repo: str):
    return (q.c.project_id == project_id) & (q.c.repo == repo)


async def row_of(conn: AsyncConnection, project_id: int, repo: str, *, lock: bool = False):
    b = tables.merge_breakers
    query = select(b).where(b.c.project_id == project_id, b.c.repo == repo)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def armed(conn: AsyncConnection, project_id: int, repo: str) -> None:
    """The breaker of ``repo``, armed when it has no row yet."""
    b = tables.merge_breakers
    await conn.execute(
        insert(b).values(project_id=project_id, repo=repo).on_conflict_do_nothing(index_elements=["project_id", "repo"])
    )


async def set_red(conn: AsyncConnection, breaker_id: int, red: bool) -> None:
    """The hub saw the target branch of breaker ``breaker_id`` turn red now, or green (``red`` False)."""
    b = tables.merge_breakers
    since = func.now() if red else None
    await conn.execute(update(b).values(red_since=since, updated_at=func.now()).where(b.c.id == breaker_id))


async def minutes_since(conn: AsyncConnection, since: datetime) -> float:
    """The minutes since ``since`` by the database's clock."""
    minutes = await conn.execute(select(func.extract("epoch", func.now() - since) / 60))
    return float(minutes.scalar_one())


def _merges(project_id: int, repo: str):
    q = tables.merge_queue
    return _repo(q, project_id, repo) & (q.c.source != post_merge.REVERT) & q.c.merged_at.is_not(None)


async def merge_results(conn: AsyncConnection, project_id: int, repo: str, since: datetime | None) -> list[tuple]:
    """(state, whether its UAT check passed, its PROD result) of each merge of ``repo`` since ``since``, the oldest
    first."""
    q = tables.merge_queue
    query = select(q.c.state, q.c.uat_at.is_not(None), q.c.prod_result).where(_merges(project_id, repo))
    if since is not None:
        query = query.where(q.c.merged_at > since)
    return [tuple(row) for row in await conn.execute(query.order_by(q.c.merged_at, q.c.id))]


async def merge_counts(
    conn: AsyncConnection, project_id: int, repo: str, days: timedelta, start: datetime | None = None
) -> tuple:
    """(merges of ``repo``, culprits among them) in the last ``days``, or in the ``days`` from ``start``."""
    q = tables.merge_queue
    counted = select(func.count(), func.count().filter(q.c.state.in_(rules.CULPRIT_STATES))).where(
        _merges(project_id, repo)
    )
    if start is None:
        query = counted.where(q.c.merged_at > func.now() - days)
    else:
        query = counted.where(q.c.merged_at >= start, q.c.merged_at < start + days)
    return tuple((await conn.execute(query)).one())


def _ledger(project_id: int, repo: str, *columns):
    """The lines of the ledger of the repo's merge requests, with ``columns`` of theirs and of their merge requests."""
    q, e = tables.merge_queue, tables.merge_events
    return select(*columns).join_from(e, q, q.c.id == e.c.queue_id).where(_repo(q, project_id, repo))


async def _since(conn: AsyncConnection, project_id: int, repo: str, since, *where) -> bool:
    e = tables.merge_events
    query = _ledger(project_id, repo, e.c.id).where(e.c.created_at > since, *where).limit(1)
    return bool((await conn.execute(select(query.exists()))).scalar_one())


async def revert_opened_since(conn: AsyncConnection, project_id: int, repo: str, since) -> bool:
    """Whether a merge of ``repo`` found the culprit since ``since``: its revert opened or refused."""
    return await _since(conn, project_id, repo, since, tables.merge_events.c.kind == "revert_opened")


async def misses(conn: AsyncConnection, project_id: int, repo: str, since) -> tuple[int, bool]:
    """(merges the Judge passed found wrong in the last MISS_DAYS, whether one was found wrong since ``since``)."""
    q, e = tables.merge_queue, tables.merge_events
    missed = and_(
        q.c.source != post_merge.REVERT,
        q.c.verdict[("first", "passed")].as_boolean().is_(True),
        or_(e.c.kind.in_(MISSED), and_(e.c.kind == "sample_answered", e.c.passed.is_(False))),
    )
    recent = e.c.created_at > func.now() - timedelta(days=rules.MISS_DAYS)
    count = await conn.execute(_ledger(project_id, repo, func.count(distinct(e.c.queue_id))).where(missed, recent))
    found = count.scalar_one()
    if since is None:
        return found, found > 0
    return found, await _since(conn, project_id, repo, since, missed)


async def failure_lines(conn: AsyncConnection, project_id: int, repo: str, since) -> list:
    """(kind, result, reason, iid, revert) of the first MAX_FAILURES lines of the repo's ledger since ``since`` that
    trip the breaker by themselves: a check that failed or has no result, a rollback, a revert GitLab would not make,
    a revert's merge request blocked."""
    q, e = tables.merge_queue, tables.merge_events
    result = e.c.detail["result"].astext
    waits = func.coalesce(e.c.detail["waits"].as_boolean(), False)
    revert = q.c.source == post_merge.REVERT
    failing = or_(
        and_(e.c.kind.in_(("uat_check", "prod_deploy", "prod_check")), result.in_(rules.FAILED)),
        e.c.kind == "rollback",
        and_(e.c.kind == "revert_opened", e.c.passed.is_(False)),
        and_(revert, e.c.kind.in_(BLOCKING), e.c.passed.is_(False), waits.is_(False)),
    )
    query = _ledger(project_id, repo, e.c.kind, result.label("result"), e.c.reason, q.c.iid, revert.label("revert"))
    query = query.where(failing)
    if since is not None:
        query = query.where(e.c.created_at > since)
    return list((await conn.execute(query.order_by(e.c.id).limit(MAX_FAILURES))).all())


async def pausable(conn: AsyncConnection, project_id: int, repo: str) -> list:
    """The merge requests of ``repo`` ready or in their turn, a revert aside, locked."""
    q = tables.merge_queue
    query = (
        select(q)
        .where(_repo(q, project_id, repo), q.c.state.in_(rules.PAUSED_FROM), q.c.source != post_merge.REVERT)
        .order_by(q.c.id)
        .with_for_update()
    )
    return list((await conn.execute(query)).all())


async def set_tripped(conn: AsyncConnection, breaker_id: int, cause: str, reason: str) -> None:
    b = tables.merge_breakers
    values = {"state": rules.TRIPPED, "cause": cause, "reason": reason, "tripped_at": func.now()}
    await conn.execute(
        update(b).values(trips=b.c.trips + 1, updated_at=func.now(), **values).where(b.c.id == breaker_id)
    )


async def set_armed(conn: AsyncConnection, breaker_id: int, user_id: int) -> None:
    """User ``user_id`` reset breaker ``breaker_id``: what trips it counts from now."""
    b = tables.merge_breakers
    values = {"state": rules.ARMED, "resumed_by": user_id, "resumed_at": func.now(), "red_since": None}
    await conn.execute(update(b).values(updated_at=func.now(), **values).where(b.c.id == breaker_id))


async def paused(conn: AsyncConnection, project_id: int, repo: str) -> list:
    """The merge requests of ``repo`` its breaker paused, locked."""
    q = tables.merge_queue
    query = select(q).where(_repo(q, project_id, repo), q.c.state == rules.PAUSED).order_by(q.c.id)
    return list((await conn.execute(query.with_for_update())).all())


async def read_notices(conn: AsyncConnection, project_id: int, repo: str) -> None:
    """The notices NOTICE of ``repo`` are read."""
    n = tables.notifications
    told = (n.c.project_id == project_id) & (n.c.notice_kind == NOTICE) & (n.c.details["repo"].astext == repo)
    await conn.execute(update(n).values(read_at=func.now()).where(told, n.c.read_at.is_(None)))


async def breakers(conn: AsyncConnection, project_id: int, repo: str | None = None) -> list:
    """The breakers of the project (``repo``'s alone when given), by repo, with the login of who reset each last."""
    b, u = tables.merge_breakers, tables.users
    query = (
        select(b, u.c.login.label("resumed_login"))
        .join_from(b, u, u.c.id == b.c.resumed_by, isouter=True)
        .where(b.c.project_id == project_id)
    )
    if repo is not None:
        query = query.where(b.c.repo == repo)
    return list((await conn.execute(query.order_by(b.c.repo))).all())

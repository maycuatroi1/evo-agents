"""The queries of what follows a merge of the hub's merge queue (``service.post_merge``): the merges of a target
branch, the commit PROD goes back to, the audit samples and the admins of a project."""

from __future__ import annotations

from sqlalchemy import exists, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, post_merge, tables


def _order(q):
    return tuple_(q.c.merged_at, q.c.id)


def _branch(project_id: int, repo: str, target: str):
    q = tables.merge_queue
    return (q.c.project_id == project_id) & (q.c.repo == repo) & (q.c.target_branch == target)


async def admins(conn: AsyncConnection, project_id: int) -> list[int]:
    g = tables.grants
    query = select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id)
    return [user_id for (user_id,) in await conn.execute(query)]


async def later_merges(conn: AsyncConnection, culprit) -> list:
    """The merges of the branch of ``culprit`` after it and not past PROD yet, locked."""
    q = tables.merge_queue
    later = select(q).where(
        _branch(culprit.project_id, culprit.repo, culprit.target_branch),
        q.c.state.in_((*post_merge.UAT_STATES, "releasing")),
        _order(q) > tuple_(culprit.merged_at, culprit.id),
    )
    return list((await conn.execute(later.with_for_update())).all())


async def open_culprit(conn: AsyncConnection, row) -> int | None:
    """The id of the oldest culprit of the branch of ``row`` that waits for its revert, or None."""
    q = tables.merge_queue
    branch = _branch(row.project_id, row.repo, row.target_branch)
    query = select(q.c.id).where(branch, q.c.state == "reverting").order_by(q.c.id).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def branch_rows(conn: AsyncConnection, project_id: int, repo: str, target: str, states) -> list:
    """The merges of the branch in ``states``, in merge order."""
    q = tables.merge_queue
    query = select(q).where(_branch(project_id, repo, target), q.c.state.in_(states)).order_by(q.c.merged_at, q.c.id)
    return list((await conn.execute(query)).all())


async def revert_pending(conn: AsyncConnection, project_id: int, repo: str, target: str) -> bool:
    """Whether a culprit of the branch waits for its revert, or a revert has no result of its own yet."""
    q = tables.merge_queue
    branch = _branch(project_id, repo, target)
    culprit = exists().where(branch, q.c.state == "reverting")
    unchecked = exists().where(
        branch,
        q.c.source == post_merge.REVERT,
        q.c.uat_result.is_(None),
        q.c.state.in_((*merge_gate.QUEUED_STATES, *post_merge.UAT_STATES)),
    )
    return bool((await conn.execute(select(culprit | unchecked))).scalar_one())


async def unreverted(conn: AsyncConnection, project_id: int, repo: str, target: str) -> list:
    """The culprits of the branch with no revert yet, in merge order."""
    q = tables.merge_queue
    r = q.alias("revert")
    branch = _branch(project_id, repo, target)
    query = select(q).where(branch, q.c.state == "reverting", ~exists().where(r.c.reverts_id == q.c.id))
    return list((await conn.execute(query.order_by(q.c.merged_at, q.c.id))).all())


async def last_passed(conn: AsyncConnection, project_id: int, repo: str, branch: str, target) -> str | None:
    """The commit of the last merge of ``branch`` before ``target`` whose PROD check passed, or None."""
    q = tables.merge_queue
    query = (
        select(q.c.after_sha)
        .where(
            _branch(project_id, repo, branch),
            q.c.prod_result == "passed",
            _order(q) < tuple_(target.merged_at, target.id),
        )
        .order_by(q.c.merged_at.desc(), q.c.id.desc())
        .limit(1)
    )
    return (await conn.execute(query)).scalar_one_or_none()


# The audit samples


async def insert_sample(conn: AsyncConnection, row) -> int:
    """A new audit sample of merge request ``row``; its id."""
    s = tables.merge_samples
    query = insert(s).values(project_id=row.project_id, queue_id=row.id).returning(s.c.id)
    return (await conn.execute(query)).scalar_one()


async def sample_of(conn: AsyncConnection, sample_id: int, *, lock: bool = False):
    s = tables.merge_samples
    query = select(s).where(s.c.id == sample_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def answer_sample(conn: AsyncConnection, sample_id: int, result: str, note: str | None, user_id: int) -> None:
    """User ``user_id`` answered audit sample ``sample_id``; the notices about it are read."""
    s, n = tables.merge_samples, tables.notifications
    values = {"result": result, "note": note, "answered_by": user_id, "answered_at": func.now()}
    await conn.execute(update(s).values(values).where(s.c.id == sample_id))
    sampled = n.c.details["sample_id"].astext == str(sample_id)
    await conn.execute(update(n).values(read_at=func.now()).where(sampled, n.c.read_at.is_(None)))

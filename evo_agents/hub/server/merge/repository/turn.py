"""The queries of the turn of a target branch of the hub's merge queue (``service.turn``): whose turn it is, who takes
it next, what holds it, and the Curator's change a merge carries."""

from __future__ import annotations

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, post_merge, tables
from evo_agents.hub.server.merge.repository.gate import set_row


def _branch(project_id: int, repo: str, target: str):
    q = tables.merge_queue
    return (q.c.project_id == project_id) & (q.c.repo == repo) & (q.c.target_branch == target)


async def holder(conn: AsyncConnection, project_id: int, repo: str, target: str) -> int | None:
    """The id of the merge request of the branch in a turn state; None when none has it."""
    q = tables.merge_queue
    query = select(q.c.id).where(_branch(project_id, repo, target), q.c.state.in_(merge_gate.TURN_STATES)).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def next_ready(conn: AsyncConnection, project_id: int, repo: str, target: str, *, revert_only: bool):
    """The merge request of the branch ready first, a revert before any other, that waits for nothing on GitLab,
    locked; a revert alone when ``revert_only``; None without one."""
    q = tables.merge_queue
    not_waiting = or_(q.c.reason.is_(None), ~q.c.reason.startswith(merge_gate.WAITS, autoescape=True))
    query = (
        select(q)
        .where(_branch(project_id, repo, target), q.c.state == "ready", q.c.iid.is_not(None), not_waiting)
        .order_by(q.c.source != post_merge.REVERT, q.c.queued_at.asc().nulls_last(), q.c.id)
        .limit(1)
        .with_for_update()
    )
    if revert_only:
        query = query.where(q.c.source == post_merge.REVERT)
    return (await conn.execute(query)).one_or_none()


async def take_turn(conn: AsyncConnection, row_id: int) -> None:
    """Merge request ``row_id`` takes the turn: it is rebased next, its wait counted from now."""
    q = tables.merge_queue
    await set_row(
        conn, row_id, stamp=("head_at",), state="rebasing", reason=None, before_sha=None, rebases=q.c.rebases + 1
    )


async def holding(conn: AsyncConnection, project_id: int, repo: str, target: str) -> list:
    """(id, iid) of each merge of the branch that may hold the turn (``merge_gate.HOLDING_STATES``), in merge order."""
    q = tables.merge_queue
    merged = select(q.c.id, q.c.iid).where(_branch(project_id, repo, target), q.c.state.in_(merge_gate.HOLDING_STATES))
    return list((await conn.execute(merged.order_by(q.c.merged_at, q.c.id))).all())


async def passed_kinds(conn: AsyncConnection, row_id: int, kind: str) -> list[str]:
    """The kinds of the lines of ``kind`` merge request ``row_id`` passed."""
    e = tables.merge_events
    passed = select(e.c.kind).where(e.c.queue_id == row_id, e.c.kind == kind, e.c.passed.is_(True))
    return [found for (found,) in await conn.execute(passed)]


async def retried(conn: AsyncConnection, row_id: int, sha: str | None) -> bool:
    """Whether the turn ran the pipeline of head ``sha`` of merge request ``row_id`` again already."""
    e = tables.merge_events
    done = exists().where(e.c.queue_id == row_id, e.c.kind == "retried", e.c.sha == sha)
    return bool((await conn.execute(select(done))).scalar_one())


async def change_state(conn: AsyncConnection, change_id: int) -> str | None:
    """The state of the Curator's change ``change_id``, locked."""
    c = tables.curator_changes
    query = select(c.c.state).where(c.c.id == change_id).with_for_update()
    return (await conn.execute(query)).scalar_one_or_none()


async def change_merged(conn: AsyncConnection, change_id: int, after: str) -> None:
    """The Curator's change ``change_id`` is merged at ``after``."""
    c = tables.curator_changes
    values = {"state": "merged", "merged_at": func.now(), "merge_sha": after, "reason": None, "updated_at": func.now()}
    await conn.execute(update(c).values(values).where(c.c.id == change_id))

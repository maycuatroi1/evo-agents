"""What the Loop writes in the ledger of delivery (``delivery``) for the use cases of a build it makes or a deliver it
adopts (plan srs-backlog-loop, criteria a3 and a6): ``delivering`` while a work item is on them, ``delivered`` when its
deliver is done, ``undelivered`` again when it is cancelled. The rows are of source ``deliver``, with ``item_id`` the
work item that holds them; a use case an admin or the knowledge graph put in another state is not touched, as only a
row that is ``delivering`` is read here.

- ``hold`` puts the use cases of a work item in ``delivering``, in the transaction that made the work item.
- ``sync`` reads each such row's work item: a ``build`` through its newest ``deliver``, a ``deliver`` itself. A deliver
  done makes the use case ``delivered`` with the plan and the merge requests of the queue as its evidence
  (``plan``, ``merges``, ``after_sha``, ``build``, ``deliver``); one cancelled, or a build cancelled before it had a
  deliver, puts it back in the backlog. A tick calls it after it reconciled the work items, so a deliver that moved this
  tick is read in it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.loop import delivery
from evo_agents.hub.server.loop.model import BUILD, CANCELLED, DELIVER, DONE
from evo_agents.hub.srs_backlog import DELIVER as FROM_DELIVER
from evo_agents.hub.srs_backlog import DELIVERED, DELIVERING, UNDELIVERED


async def hold(
    conn: AsyncConnection,
    project_id: int,
    document_id: int,
    codes: Iterable[str],
    item_id: int,
    goal_id: int | None,
    evidence: Mapping | None = None,
) -> int:
    """Put use cases ``codes`` of document ``document_id`` in ``delivering``, held by work item ``item_id``; how many
    rows changed."""
    changed = 0
    for code in codes:
        row = {"item": item_id, **(evidence or {})}
        done = await delivery.record(
            conn, project_id, document_id, code, DELIVERING, FROM_DELIVER, row, goal_id=goal_id, item_id=item_id
        )
        changed += int(done)
    return changed


async def _merges(conn: AsyncConnection, project_id: int, plan_id: str | None) -> tuple[list[int], list[str]]:
    """The ids of the merge requests of the queue for ``plan_id``, and the commits they merged."""
    if not plan_id:
        return [], []
    q = tables.merge_queue
    query = select(q.c.id, q.c.after_sha).where(q.c.project_id == project_id, q.c.plan_id == plan_id).order_by(q.c.id)
    rows = (await conn.execute(query)).all()
    return [row.id for row in rows], [row.after_sha for row in rows if row.after_sha]


async def sync(conn: AsyncConnection, project_id: int) -> int:
    """Settle the ``delivering`` rows of the project's ledger by the state of their work items; how many changed."""
    s, i = tables.srs_delivery, tables.loop_items
    held = (
        await conn.execute(
            select(s.c.document_id, s.c.code, s.c.goal_id, s.c.item_id).where(
                s.c.project_id == project_id, s.c.state == DELIVERING, s.c.item_id.is_not(None)
            )
        )
    ).all()
    if not held:
        return 0
    columns = (i.c.id, i.c.kind, i.c.state, i.c.reason, i.c.plan_id, i.c.parent_id)
    wanted = sorted({row.item_id for row in held})
    found = {row.id: row for row in await conn.execute(select(*columns).where(i.c.id.in_(wanted)))}
    children: dict[int, object] = {}
    builds = [row.id for row in found.values() if row.kind == BUILD]
    if builds:
        query = select(*columns).where(i.c.parent_id.in_(builds), i.c.kind == DELIVER).order_by(i.c.id)
        children = {row.parent_id: row for row in await conn.execute(query)}  # the newest of each build wins
    changed = 0
    for row in held:
        item = found.get(row.item_id)
        if item is None:
            continue
        target = children.get(item.id) if item.kind == BUILD else item
        if target is None and item.state != CANCELLED:
            continue  # a build whose deliver is not made yet
        gone = item if target is None else target
        if gone.state == DONE and target is not None:
            merges, shas = await _merges(conn, project_id, target.plan_id)
            evidence = {"plan": target.plan_id, "merges": merges, "after_sha": shas, "deliver": target.id}
            state = DELIVERED
            if item.kind == BUILD:
                evidence["build"] = item.id
        elif gone.state == CANCELLED:
            state, evidence = UNDELIVERED, {"item": gone.id, "cancelled": gone.reason}
        else:
            continue
        done = await delivery.record(
            conn,
            project_id,
            row.document_id,
            row.code,
            state,
            FROM_DELIVER,
            evidence,
            goal_id=row.goal_id,
            item_id=row.item_id,
        )
        changed += int(done)
    return changed


__all__ = ["hold", "sync"]

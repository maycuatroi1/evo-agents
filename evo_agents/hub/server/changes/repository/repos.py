"""What a change of the Curator in several repos (``evo_agents.hub.server.changes.service.repos``) reads and writes:
the seams of the project's knowledge graph, and the rows of curator_change_repos."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import MAIN


async def seam_edges(conn: AsyncConnection, project_id: int) -> list:
    """(owner, consumer) of each Seam of the knowledge graph's main branch of the project, as node ids, in order."""
    e = tables.kg_edges
    owned, consumed = e.alias("owned"), e.alias("consumed")
    query = (
        select(owned.c.dst.label("owner"), consumed.c.dst.label("consumer"))
        .join_from(
            owned,
            consumed,
            (consumed.c.project_id == owned.c.project_id)
            & (consumed.c.branch_id == owned.c.branch_id)
            & (consumed.c.src == owned.c.src),
        )
        .where(
            owned.c.project_id == project_id,
            owned.c.branch_id == MAIN,
            owned.c.rel == "owned_by",
            consumed.c.rel == "consumed_by",
        )
        .order_by(owned.c.src, consumed.c.dst)
    )
    return list((await conn.execute(query)).all())


async def record_repos(conn: AsyncConnection, project_id: int, change_id: int, rows: list[dict], branch: str) -> None:
    """A row of curator_change_repos for each of ``rows`` (``service.repos.repo_rows``), on GitHub, waiting for its
    pull request."""
    cr = tables.curator_change_repos
    values = [
        {"project_id": project_id, "change_id": change_id, "branch": branch, "forge": "github", **row} for row in rows
    ]
    await conn.execute(insert(cr), values)


async def repos_of(conn: AsyncConnection, change_id: int) -> list:
    """The rows of change ``change_id``'s repos in plan order, each with its repo's origin; none for a change in one
    repo."""
    cr, pr = tables.curator_change_repos, tables.project_repos
    query = (
        select(cr, pr.c.origin)
        .join_from(cr, pr, (pr.c.project_id == cr.c.project_id) & (pr.c.name == cr.c.repo), isouter=True)
        .where(cr.c.change_id == change_id)
        .order_by(cr.c.position)
    )
    return list((await conn.execute(query)).all())


async def set_row(conn: AsyncConnection, row_id: int, **values) -> None:
    cr = tables.curator_change_repos
    await conn.execute(update(cr).values(**values, updated_at=func.now()).where(cr.c.id == row_id))


async def judging_row(conn: AsyncConnection, change_id: int, judge_run_id: int):
    """The row of change ``change_id`` judge run ``judge_run_id`` judges now, or None."""
    cr = tables.curator_change_repos
    query = select(cr).where(cr.c.change_id == change_id, cr.c.judge_run_id == judge_run_id, cr.c.state == "judging")
    return (await conn.execute(query)).first()


async def next_to_judge(conn: AsyncConnection, change_id: int):
    """The first repo of change ``change_id`` in plan order that waits for its Judge; None for a change in one repo."""
    cr = tables.curator_change_repos
    query = select(cr).where(cr.c.change_id == change_id, cr.c.state == "judge_pending").order_by(cr.c.position)
    return (await conn.execute(query.limit(1))).first()


async def judge_left(conn: AsyncConnection, change_id: int, run_id: int, state: str, why: str | None) -> None:
    """The repo judge run ``run_id`` judged ended without a verdict: it waits for a Judge again, or is left ``open``."""
    cr = tables.curator_change_repos
    await conn.execute(
        update(cr)
        .values(state=state, judge_attempts=cr.c.judge_attempts + 1, reason=why, updated_at=func.now())
        .where(cr.c.change_id == change_id, cr.c.judge_run_id == run_id, cr.c.state == "judging")
    )


async def set_judged_reason(conn: AsyncConnection, change_id: int, why: str | None) -> None:
    """Set ``why`` as the reason of change ``change_id`` while it is judged."""
    c = tables.curator_changes
    await conn.execute(update(c).values(reason=why).where(c.c.id == change_id, c.c.state == "judged"))

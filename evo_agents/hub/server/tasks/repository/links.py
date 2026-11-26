"""The queries of what a task links to (tasks_links) and of what the links point at: the SRS item holding a code
(``srs_item``), a plan of the project (``plan``, ``plans``), a run of it (``run_in``, ``runs``, ``step_runs``), the
links of a task as kept (``held``, ``rows``, ``step_links``, ``srs_codes``), and a link kept or dropped (``insert_link``,
``delete_link``).
"""

from __future__ import annotations

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.tasks.models.rows import Target
from evo_agents.hub.tables_srs import code_key

STEP_RUNS = 3  # the latest runs of a linked step a link shows


async def srs_item(conn: AsyncConnection, project_id: int, code: str):
    """(id, code, retired_in, label) of the coded SRS item of the project holding ``code`` (or an alias), or None."""
    c, i, d = tables.srs.codes, tables.srs.items, tables.srs.documents
    query = (
        select(i.c.id, i.c.code, i.c.retired_in, d.c.label)
        .select_from(c.join(i, i.c.id == c.c.item_id).join(d, d.c.id == i.c.document_id))
        .where(c.c.project_id == project_id, c.c.code_key == code_key(code))
    )
    return (await conn.execute(query)).first()


async def plan(conn: AsyncConnection, project_id: int, plan_id: str):
    """(label, body) of plan ``plan_id`` of the project, or None."""
    p = tables.plans
    query = select(p.c.label, p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan_id)
    return (await conn.execute(query)).first()


async def plans(conn: AsyncConnection, project_id: int, plan_ids: list[str]) -> dict:
    """plan id -> (plan_id, label, body) of each of ``plan_ids`` the project holds."""
    p = tables.plans
    query = select(p.c.plan_id, p.c.label, p.c.body).where(p.c.project_id == project_id, p.c.plan_id.in_(plan_ids))
    return {row.plan_id: row for row in await conn.execute(query)}


async def run_in(conn: AsyncConnection, project_id: int, run_id: int) -> int | None:
    """``run_id`` when it is a run of the project, else None."""
    r = tables.runs
    return await conn.scalar(select(r.c.id).where(r.c.id == run_id, r.c.project_id == project_id))


async def runs(conn: AsyncConnection, run_ids: list[int]) -> dict:
    """run id -> (id, kind, state) of each of ``run_ids``."""
    r = tables.runs
    return {row.id: row for row in await conn.execute(select(r.c.id, r.c.kind, r.c.state).where(r.c.id.in_(run_ids)))}


async def step_links(conn: AsyncConnection, task_ids: list[int]) -> list:
    """(task_id, plan_id, step_id) of every plan step ``task_ids`` link."""
    k = tables.tasks.links
    query = select(k.c.task_id, k.c.plan_id, k.c.step_id).where(
        k.c.task_id.in_(task_ids or [0]), k.c.kind == "plan_step"
    )
    return list(await conn.execute(query))


async def srs_codes(conn: AsyncConnection, task_ids: list[int]) -> list:
    """(task_id, code, label) of every SRS item ``task_ids`` link, each task's in the order linked."""
    k, i, d = tables.tasks.links, tables.srs.items, tables.srs.documents
    query = (
        select(k.c.task_id, i.c.code, d.c.label)
        .join_from(k, i, i.c.id == k.c.srs_item_id)
        .join(d, d.c.id == i.c.document_id)
        .where(k.c.task_id.in_(task_ids), k.c.kind == "srs_item")
        .order_by(k.c.task_id, k.c.id)
    )
    return list(await conn.execute(query))


async def srs_items(conn: AsyncConnection, item_ids: list[int]) -> dict:
    """item id -> (id, code, kind, retired_in, slug, label, title) of each of ``item_ids``, its title in the latest
    version that has it."""
    if not item_ids:
        return {}
    i, d, iv, v = tables.srs.items, tables.srs.documents, tables.srs.item_versions, tables.srs.versions
    title = (
        select(iv.c.title)
        .join(v, v.c.id == iv.c.version_id)
        .where(iv.c.item_id == i.c.id)
        .order_by(v.c.number.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(i.c.id, i.c.code, i.c.kind, i.c.retired_in, d.c.slug, d.c.label, title.label("title"))
        .join_from(i, d, d.c.id == i.c.document_id)
        .where(i.c.id.in_(item_ids or [0]))
    )
    return {row.id: row for row in await conn.execute(query)}


async def step_runs(conn: AsyncConnection, project_id: int, pairs: set[tuple[str, str]]) -> dict:
    """(plan id, step id) -> its latest STEP_RUNS runs, the newest first."""
    if not pairs:
        return {}
    r = tables.runs
    plan_ids = sorted({plan_id for plan_id, _ in pairs})
    ranked = func.row_number().over(partition_by=(r.c.plan_id, r.c.step_key), order_by=r.c.id.desc()).label("rank")
    inner = (
        select(r.c.id, r.c.state, r.c.plan_id, r.c.step_key, ranked)
        .where(r.c.project_id == project_id, r.c.plan_id.in_(plan_ids))
        .subquery()
    )
    query = select(inner).where(inner.c.rank <= STEP_RUNS).order_by(inner.c.id.desc())
    found: dict[tuple[str, str], list[dict]] = {}
    for row in await conn.execute(query):
        if (row.plan_id, row.step_key) in pairs:
            found.setdefault((row.plan_id, row.step_key), []).append({"id": row.id, "state": row.state})
    return found


async def rows(conn: AsyncConnection, task_id: int, with_srs: bool) -> list:
    """The links of task ``task_id``, in the order they were made, each with the login of who made it; its SRS items
    only ``with_srs``."""
    k, u = tables.tasks.links, tables.users
    query = (
        select(k, u.c.login.label("created_by_login"))
        .join_from(k, u, u.c.id == k.c.created_by)
        .where(k.c.task_id == task_id)
        .order_by(k.c.id)
    )
    if not with_srs:
        query = query.where(k.c.kind != "srs_item")
    return list(await conn.execute(query))


async def graph_links(conn: AsyncConnection, task_ids: list[int], with_srs: bool) -> dict[int, list]:
    """task id -> its links to SRS items (``with_srs``) and to plan steps, in the order made, each with the SRS item's
    kind and code."""
    k, i = tables.tasks.links, tables.srs.items
    kinds = ["srs_item", "plan_step"] if with_srs else ["plan_step"]
    query = (
        select(k.c.task_id, k.c.kind, k.c.plan_id, k.c.step_id, i.c.kind.label("srs_kind"), i.c.code)
        .select_from(k.outerjoin(i, i.c.id == k.c.srs_item_id))
        .where(k.c.task_id.in_(task_ids or [0]), k.c.kind.in_(kinds))
        .order_by(k.c.task_id, k.c.id)
    )
    found: dict[int, list] = {}
    for row in await conn.execute(query):
        found.setdefault(row.task_id, []).append(row)
    return found


async def held(conn: AsyncConnection, task_id: int) -> set[Target]:
    """The targets task ``task_id`` links to."""
    k = tables.tasks.links
    return {
        Target(row.kind, row.srs_item_id, row.plan_id, row.step_id, row.run_id, row.url)
        for row in await conn.execute(select(k).where(k.c.task_id == task_id))
    }


async def insert_link(conn: AsyncConnection, task_id: int, user_id: int, target: Target) -> None:
    values = {"task_id": task_id, "created_by": user_id, **target.columns()}
    await conn.execute(insert(tables.tasks.links).values(**values))


async def delete_link(conn: AsyncConnection, task_id: int, link_id: int) -> str | None:
    """Drop link ``link_id`` of task ``task_id``; its kind, or None when the task has no such link."""
    k = tables.tasks.links
    gone = await conn.execute(delete(k).where(k.c.id == link_id, k.c.task_id == task_id).returning(k.c.kind))
    return gone.scalar()


__all__ = [
    "STEP_RUNS",
    "delete_link",
    "graph_links",
    "held",
    "insert_link",
    "plan",
    "plans",
    "rows",
    "run_in",
    "runs",
    "srs_codes",
    "srs_item",
    "srs_items",
    "step_links",
    "step_runs",
]

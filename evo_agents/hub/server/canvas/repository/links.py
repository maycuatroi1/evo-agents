"""The SQL of what a claim links: the use cases of the plugin srs by code, the project's registry of signal keys,
and the status of its problems and opportunities.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def use_cases(conn: AsyncConnection, project_id: int, keys: list[str]) -> dict[str, Any]:
    """By code key (``tables_srs.code_key``), the item of the SRS of project ``project_id`` each of ``keys`` names:
    its kind, code, the version it was dropped in and its document's label."""
    c, i, d = tables.srs.codes, tables.srs.items, tables.srs.documents
    query = (
        select(c.c.code_key, i.c.kind, i.c.code, i.c.retired_in, d.c.label)
        .select_from(c.join(i, i.c.id == c.c.item_id).join(d, d.c.id == i.c.document_id))
        .where(c.c.project_id == project_id, c.c.code_key.in_(keys))
    )
    return {row.code_key: row for row in await conn.execute(query)}


async def signal_keys(conn: AsyncConnection, project_id: int, names: list[str]) -> dict[str, Any]:
    """By name, each key of ``names`` the registry of project ``project_id`` holds, with when it was archived."""
    k = tables.signal_keys
    query = select(k.c.name, k.c.archived_at).where(k.c.project_id == project_id, k.c.name.in_(names))
    return {row.name: row for row in await conn.execute(query)}


async def problem_statuses(conn: AsyncConnection, project_id: int, numbers: list[int | None]) -> dict[int, str]:
    """The status of each problem of ``numbers`` project ``project_id`` has, by number."""
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(p.c.number, r.c.status)
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id, p.c.number.in_(numbers))
    )
    return {row.number: row.status for row in await conn.execute(query)}


async def opportunity_statuses(conn: AsyncConnection, project_id: int, numbers: list[int | None]) -> dict[int, str]:
    """The status of each opportunity of ``numbers`` project ``project_id`` has, by number."""
    o, r = tables.opportunities, tables.opportunity_revisions
    query = (
        select(o.c.number, r.c.status)
        .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
        .where(o.c.project_id == project_id, o.c.number.in_(numbers))
    )
    return {row.number: row.status for row in await conn.execute(query)}

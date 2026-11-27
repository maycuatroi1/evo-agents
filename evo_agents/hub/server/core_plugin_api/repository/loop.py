"""The queries of the reads and helpers of API 1.3 for a project's admins, the notices a plugin sent them, its goals
with a metric, the test cards of its canvas claims, the plugin's work items on its Loop, its agent types and the
dispatcher of a run."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_query
from evo_agents.hub.server.agents.repository import agent_types
from evo_agents.hub.server.core_plugin_api.models.reads import MAX_ROWS

_T = tables.metadata.tables


async def admins(conn: AsyncConnection, project_id: int) -> list[int]:
    """The admins of the project, by user id."""
    g = tables.grants.c
    found = await conn.execute(
        select(g.user_id).where(g.project_id == project_id, g.role == "admin").order_by(g.user_id)
    )
    return [row.user_id for row in found]


async def notice_told(
    conn: AsyncConnection, project_id: int, notice_kind: str, plugin: str, key: str, value: str
) -> bool:
    """Whether a notice of ``notice_kind`` of plugin ``plugin`` in the project has ``details[key]`` equal to
    ``value``."""
    n = tables.notifications.c
    told = await conn.scalar(
        select(n.id)
        .where(
            n.project_id == project_id,
            n.notice_kind == notice_kind,
            n.details["plugin"].astext == plugin,
            n.details[key].astext == value,
        )
        .limit(1)
    )
    return told is not None


async def measured_goals(conn: AsyncConnection, project_id: int, status: str):
    """The goals of the project in ``status`` that have a metric, by rank then number."""
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(g.c.id, g.c.number, r.c.statement, r.c.metric)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id, r.c.status == status, r.c.metric.is_not(None))
        .order_by(r.c.rank, g.c.number)
    )
    return (await conn.execute(query)).all()


async def claim_cards(conn: AsyncConnection, project_id: int, ids: Sequence[int], numbers: Sequence[int]):
    """The claims of the project with id in ``ids`` or number in ``numbers``, by id."""
    c = _T["canvas_claims"].c
    query = (
        select(c.id, c.number, c.cell, c.text, c.test_card, c.signal_keys)
        .where(c.project_id == project_id, c.id.in_(list(ids)) | c.number.in_(list(numbers)))
        .order_by(c.id)
    )
    return (await conn.execute(query)).all()


async def work_items(
    conn: AsyncConnection,
    project_id: int,
    own: str,
    keys: Sequence[str] | None,
    key_prefix: str | None,
    since: datetime | None,
):
    """The work items of the project's Loop whose key starts with ``own``, by id: with a key ``own`` + one of
    ``keys``, one that starts with ``own`` + ``key_prefix``, made at ``since`` or after, each when given."""
    i = tables.loop_items.c
    query = select(i.id, i.kind, i.key, i.state, i.created_at).where(
        i.project_id == project_id, i.key.startswith(own, autoescape=True)
    )
    if keys is not None:
        query = query.where(i.key.in_([own + key for key in keys]))
    if key_prefix is not None:
        query = query.where(i.key.startswith(own + key_prefix, autoescape=True))
    if since is not None:
        query = query.where(i.created_at >= since)
    return (await conn.execute(query.order_by(i.id).limit(MAX_ROWS))).all()


async def enabled_plugins(conn: AsyncConnection, project_id: int) -> dict[int, str]:
    """The plugins the project enables now: name by plugin id."""
    rows = (await conn.execute(enabled_query().where(tables.projects.c.id == project_id))).all()
    return {row.id: row.name for row in rows}


async def agent_type_rows(conn: AsyncConnection, project_id: int, profile_id: int | None):
    """The agent types the project enabled, of the agent profile ``profile_id`` alone when given."""
    query = agent_types.rows(project_id)
    if profile_id is not None:
        query = query.where(tables.project_agent_types.c.profile_id == profile_id)
    return (await conn.execute(query)).all()


async def run_dispatcher(conn: AsyncConnection, project_id: int, run_id: int):
    """The id and login of the member who dispatched run ``run_id`` of the project, None without one."""
    r, u = tables.runs, tables.users
    query = (
        select(r.c.dispatched_by, u.c.login)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id, r.c.project_id == project_id)
    )
    return (await conn.execute(query)).one_or_none()

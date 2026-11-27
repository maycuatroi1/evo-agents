"""The SQL of the agent types a project enables: the enabled rows with their profile's handle and members' logins,
the usage of an agent type's runs, the member a type's runs go under, the handles taken, and the enable and the turning
off of a type."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, task_runs
from evo_agents.hub.runs import TERMINAL_STATES

WINDOW = func.make_interval(0, 0, 0, 0, task_runs.BUDGET_HOURS)


def rows(project_id: int):
    """The agent types project ``project_id`` enabled, with their profile's handle and their members' logins."""
    t, ap = tables.project_agent_types, tables.agent_profiles
    runner, enabler = tables.users.alias("runner"), tables.users.alias("enabler")
    return (
        select(
            t.c.id,
            t.c.plugin_id,
            t.c.name,
            t.c.profile_id,
            t.c.run_as.label("run_as_id"),
            runner.c.login.label("run_as"),
            t.c.daily_usd,
            t.c.min_run_usd,
            enabler.c.login.label("enabled_by"),
            t.c.enabled_at,
            t.c.disabled_at,
            ap.c.handle,
            ap.c.deleted_at.label("profile_deleted_at"),
        )
        .join_from(t, ap, ap.c.id == t.c.profile_id)
        .join(runner, runner.c.id == t.c.run_as)
        .join(enabler, enabler.c.id == t.c.enabled_by)
        .where(t.c.project_id == project_id)
    )


async def project_types(conn: AsyncConnection, project_id: int) -> list:
    """The rows of ``rows`` of the project."""
    return list(await conn.execute(rows(project_id)))


async def enabled_type(conn: AsyncConnection, project_id: int, plugin_id: int, name: str, *, lock: bool = False):
    """The row of ``rows`` of agent type ``name`` of plugin ``plugin_id`` in project ``project_id``, or None."""
    t = tables.project_agent_types
    query = rows(project_id).where(t.c.plugin_id == plugin_id, t.c.name == name)
    return (await conn.execute(query.with_for_update(of=t) if lock else query)).one_or_none()


async def recent_usages(conn: AsyncConnection, agent_type_id: int) -> list:
    """The usage of each run of agent type ``agent_type_id`` queued in the last BUDGET_HOURS."""
    r = tables.runs
    query = select(r.c.usage).where(r.c.agent_type_id == agent_type_id, r.c.queued_at > func.now() - WINDOW)
    return list((await conn.execute(query)).scalars())


async def set_floor(conn: AsyncConnection, agent_type_id: int) -> float | None:
    """The least cap of one run agent type ``agent_type_id`` sets; None for none."""
    t = tables.project_agent_types
    return (await conn.execute(select(t.c.min_run_usd).where(t.c.id == agent_type_id))).scalar_one_or_none()


async def held_runs(conn: AsyncConnection, agent_type_id: int) -> list:
    """(usage, budget, ended) of each run of agent type ``agent_type_id`` not ended, or queued in the last
    BUDGET_HOURS."""
    r = tables.runs
    ended = r.c.state.in_(TERMINAL_STATES)
    query = select(r.c.usage, r.c.budget, ended.label("ended")).where(
        r.c.agent_type_id == agent_type_id, ~ended | (r.c.queued_at > func.now() - WINDOW)
    )
    return list((await conn.execute(query)).all())


async def member_role(conn: AsyncConnection, project_id: int, login: str):
    """(id, role in the project) of the member ``login``, any case; None when the hub has none."""
    u, g = tables.users, tables.grants
    query = (
        select(u.c.id, g.c.role)
        .select_from(u)
        .outerjoin(g, (g.c.user_id == u.c.id) & (g.c.project_id == project_id))
        .where(func.lower(u.c.login) == login.lower())
    )
    return (await conn.execute(query)).first()


async def taken_handles(conn: AsyncConnection, candidates: list[str]) -> set[str]:
    """Those of ``candidates`` an agent of the hub (deleted ones included) or a project holds."""
    ap, p = tables.agent_profiles, tables.projects
    held = set((await conn.execute(select(ap.c.handle).where(ap.c.handle.in_(candidates)))).scalars())
    held |= set((await conn.execute(select(p.c.name).where(p.c.name.in_(candidates)))).scalars())
    return held


async def put_type(conn: AsyncConnection, project_id: int, plugin_id: int, name: str, values: dict) -> None:
    """Write agent type ``name`` of plugin ``plugin_id`` in the project with ``values``, as enabled now, made when it
    has no row."""
    t = tables.project_agent_types
    values = {**values, "enabled_at": func.now()}
    statement = pg_insert(t).values(project_id=project_id, plugin_id=plugin_id, name=name, **values)
    await conn.execute(statement.on_conflict_do_update(constraint="project_agent_types_name_key", set_=values))


async def turn_off(conn: AsyncConnection, agent_type_id: int) -> None:
    """Mark agent type ``agent_type_id`` turned off now."""
    t = tables.project_agent_types
    await conn.execute(update(t).where(t.c.id == agent_type_id).values(disabled_at=func.now()))

"""The queries of a plugin's config (table plugin_configs, schema 0035): the versions of a stored layer, the current
stored layers a run gets, and what each run ran with (run_plugins)."""

from __future__ import annotations

from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plugins.models.configs import Place


def layer_clause(place: Place):
    """The rows of the layer at ``place``."""
    c = tables.plugin_configs.c
    return and_(
        c.plugin_id == place.plugin_id,
        c.layer == place.layer,
        c.project_id.is_not_distinct_from(place.project_id),
        c.profile_id.is_not_distinct_from(place.profile_id),
    )


def versions_query(place: Place):
    c, u = tables.plugin_configs, tables.users
    return (
        select(
            c.c.version,
            c.c["values"].label("plain"),
            c.c.secrets,
            c.c.rolled_back_to,
            u.c.login.label("changed_by"),
            c.c.changed_at,
        )
        .join_from(c, u, u.c.id == c.c.changed_by)
        .where(layer_clause(place))
        .order_by(c.c.version.desc())
    )


async def current(conn: AsyncConnection, place: Place):
    """The current version of the layer at ``place`` (version, values, secrets, ...); None when never written."""
    return (await conn.execute(versions_query(place).limit(1))).first()


async def versions(conn: AsyncConnection, place: Place) -> list:
    """Every version of the layer, the newest first."""
    return list(await conn.execute(versions_query(place)))


async def version_of(conn: AsyncConnection, place: Place, version: int):
    """Version ``version`` of the layer, or None."""
    query = versions_query(place).where(tables.plugin_configs.c.version == version)
    return (await conn.execute(query)).first()


async def insert_version(
    conn: AsyncConnection, user_id: int | None, place: Place, version: int, values, secrets, back=None
) -> None:
    row = {
        "plugin_id": place.plugin_id,
        "layer": place.layer,
        "project_id": place.project_id,
        "profile_id": place.profile_id,
        "version": version,
        "values": values,
        "secrets": secrets,
        "rolled_back_to": back,
        "changed_by": user_id,
    }
    await conn.execute(insert(tables.plugin_configs).values(row))


async def stack_rows(conn: AsyncConnection, plugin_ids: list[int], project_id: int, profile_id: int | None) -> list:
    """The current version of each stored layer of ``plugin_ids`` that a run of the project under agent profile
    ``profile_id`` gets."""
    c = tables.plugin_configs.c
    scope = or_(
        c.layer == "instance",
        and_(c.layer == "project", c.project_id == project_id),
        and_(c.layer == "agent", c.project_id == project_id, c.profile_id == profile_id),
    )
    query = (
        select(c.plugin_id, c.layer, c.version, c["values"].label("plain"), c.secrets)
        .where(c.plugin_id.in_(plugin_ids), scope)
        .ext(distinct_on(c.plugin_id, c.layer))
        .order_by(c.plugin_id, c.layer, c.version.desc())
    )
    return list(await conn.execute(query))


async def secret_rows(conn: AsyncConnection, plugin_id: int, project_id: int | None) -> list:
    """The current version of the instance layer of plugin ``plugin_id``, and of the project's layer when
    ``project_id`` is given: their secrets."""
    c = tables.plugin_configs.c
    scope = c.layer == "instance"
    if project_id:
        scope = scope | ((c.layer == "project") & (c.project_id == project_id))
    query = (
        select(c.plugin_id, c.layer, c.version, c.secrets)
        .where(c.plugin_id == plugin_id, scope)
        .ext(distinct_on(c.plugin_id, c.layer))
        .order_by(c.plugin_id, c.layer, c.version.desc())
    )
    return list(await conn.execute(query))


async def run_layer_of(conn: AsyncConnection, run_id: int) -> dict | None:
    r = tables.runs
    return (await conn.execute(select(r.c.plugin_config).where(r.c.id == run_id))).scalar_one_or_none()


async def run_row(conn: AsyncConnection, run_id: int):
    """The project, agent profile and own layer of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.project_id, r.c.profile_id, r.c.plugin_config).where(r.c.id == run_id))).one()


async def put_run_plugin(conn: AsyncConnection, run_id: int, plugin_id: int, version: str, digest: str) -> None:
    rp = tables.run_plugins
    row = {"run_id": run_id, "plugin_id": plugin_id, "version": version}
    recorded = {"version": version, "config_digest": digest, "recorded_at": func.now()}
    await conn.execute(
        pg_insert(rp)
        .values(**row, config_digest=digest)
        .on_conflict_do_update(index_elements=[rp.c.run_id, rp.c.plugin_id], set_=recorded)
    )


async def run_plugin_rows(conn: AsyncConnection, run_id: int) -> list:
    rp, p = tables.run_plugins, tables.plugins
    query = (
        select(p.c.name, rp.c.version, rp.c.config_digest, rp.c.recorded_at)
        .join_from(rp, p, p.c.id == rp.c.plugin_id)
        .where(rp.c.run_id == run_id)
        .order_by(p.c.name)
    )
    return list(await conn.execute(query))

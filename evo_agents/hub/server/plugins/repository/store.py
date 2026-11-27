"""The queries of the hub's store of plugins: the plugins and their versions (tables plugins and plugin_versions), the
bundles the hub holds."""

from __future__ import annotations

from sqlalchemy import Text, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plugins.models.store import IMAGE, MAX_REASON_CHARS
from evo_agents.plugins import compat

CLEARED = {"installed_version": None, "installed_by": None, "installed_at": None}
TURNED_ON = {"disabled_reason": None, "disabled_at": None}


def versions_query():
    v, u = tables.plugin_versions, tables.users
    return select(
        v.c.plugin_id,
        v.c.version,
        v.c.sha256,
        v.c.size,
        v.c.publisher,
        v.c.tier,
        v.c.manifest,
        v.c.signature,
        v.c.signature_kind,
        v.c.signed_by,
        u.c.login.label("published_by"),
        v.c.published_at,
    ).outerjoin_from(v, u, u.c.id == v.c.published_by)


def plugins_query():
    p, u, scope = tables.plugins, tables.users, tables.projects
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.publisher,
            p.c.installed_version,
            u.c.login.label("installed_by"),
            p.c.installed_at,
            p.c.disabled_reason,
            p.c.disabled_at,
            p.c.created_at,
            p.c.project_id,
            scope.c.name.label("project"),
        )
        .outerjoin_from(p, u, u.c.id == p.c.installed_by)
        .outerjoin(scope, scope.c.id == p.c.project_id)
        .order_by(p.c.name)
    )


def newest_first(rows: list) -> list:
    return sorted(rows, key=lambda row: compat.plugin_version_key(row.version), reverse=True)


async def store_rows(conn: AsyncConnection) -> tuple[list, dict[int, list]]:
    """The plugins of the store, by name, with no feature of the hub (``plugins.builtin``) among them, and plugin id ->
    its version rows, newest first."""
    rows = (await conn.execute(plugins_query().where(tables.plugins.c.builtin.is_(False)))).all()
    by_plugin: dict[int, list] = {}
    for version in (await conn.execute(versions_query())).all():
        by_plugin.setdefault(version.plugin_id, []).append(version)
    return rows, {plugin_id: newest_first(held) for plugin_id, held in by_plugin.items()}  # pyright: ignore[reportReturnType]


async def find(conn: AsyncConnection, name: str, *, lock: bool = False, builtin: bool = False):
    """The row of plugin ``name`` from the plugins query; None when the hub has no such plugin, or when ``name`` is a
    feature of the hub (``plugins.builtin``) and ``builtin`` is False: the store shows, installs and removes none of
    them."""
    query = plugins_query().where(tables.plugins.c.name == name)
    if not builtin:
        query = query.where(tables.plugins.c.builtin.is_(False))
    if lock:
        query = query.with_for_update(of=tables.plugins)
    return (await conn.execute(query)).first()


async def version_rows(conn: AsyncConnection, plugin_id: int) -> list:
    """Every version of the plugin, newest first."""
    query = versions_query().where(tables.plugin_versions.c.plugin_id == plugin_id)
    return newest_first((await conn.execute(query)).all())  # pyright: ignore[reportArgumentType]


async def installed_version_row(conn: AsyncConnection, row):
    """The version row of the installed version of plugin ``row``."""
    return next(v for v in await version_rows(conn, row.id) if v.version == row.installed_version)


async def committed_size(conn: AsyncConnection, sha256: str) -> int | None:
    """The size of the bundle ``sha256`` the hub itself holds, committed; None when it holds none."""
    blobs = tables.blobs
    held = select(blobs.c.size).where(blobs.c.project_id.is_(None), blobs.c.sha256 == sha256)
    return (await conn.execute(held)).scalar_one_or_none()


async def add_plugin(conn: AsyncConnection, name: str, publisher: str, **values) -> None:
    """The row of plugin ``name`` of ``publisher``, unless the store has one."""
    await conn.execute(
        pg_insert(tables.plugins).values(name=name, publisher=publisher, **values).on_conflict_do_nothing()
    )


async def add_version(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.plugin_versions).values(values))


async def set_installed(conn: AsyncConnection, plugin_id: int, version: str, user_id: int | None) -> None:
    """Install ``version`` of plugin ``plugin_id``, turned on."""
    plugins = tables.plugins
    on = {"installed_version": version, "installed_by": user_id, "installed_at": func.now()}
    await conn.execute(update(plugins).where(plugins.c.id == plugin_id).values({**on, **TURNED_ON}))


async def set_removed(conn: AsyncConnection, plugin_id: int) -> None:
    """Clear the installed version of plugin ``plugin_id``."""
    plugins = tables.plugins
    await conn.execute(update(plugins).where(plugins.c.id == plugin_id).values({**CLEARED, **TURNED_ON}))


async def set_disabled(conn: AsyncConnection, plugin_id: int, reason: str) -> None:
    plugins = tables.plugins
    await conn.execute(
        update(plugins)
        .where(plugins.c.id == plugin_id)
        .values(disabled_reason=reason[:MAX_REASON_CHARS], disabled_at=func.now())
    )


async def installed_rows(conn: AsyncConnection) -> list:
    """The plugins installed and turned on with the manifest of the installed version, locked; none of the image's,
    which run on their release."""
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.disabled_reason.is_(None), v.c.signature_kind != IMAGE)
        .order_by(p.c.name)
        .with_for_update(of=p)
    )
    return (await conn.execute(query)).all()  # pyright: ignore[reportReturnType]


# The core plugins of the image


async def missing_tables(conn: AsyncConnection, names) -> list[str]:
    """The tables of ``names`` the hub's schema lacks."""
    found = []
    for name in names:
        if (await conn.execute(select(func.to_regclass(literal(f"public.{name}", Text))))).scalar() is None:
            found.append(name)
    return found


async def set_builtin(conn: AsyncConnection, name: str, builtin: bool) -> None:
    plugins = tables.plugins
    await conn.execute(update(plugins).where(plugins.c.name == name).values(builtin=builtin))


async def put_image_version(conn: AsyncConnection, values: dict) -> None:
    """Write the version row of a core plugin of the image, or update the one of its version."""
    versions = tables.plugin_versions
    changed = {key: values[key] for key in ("sha256", "size", "r2_key", "manifest", "signed_by")}
    upsert = pg_insert(versions).values(values)
    await conn.execute(
        upsert.on_conflict_do_update(index_elements=[versions.c.plugin_id, versions.c.version], set_=changed)
    )


async def left_image_rows(conn: AsyncConnection, kept: set[str]) -> list:
    """The installed core plugins of the image whose names are not in ``kept``, locked."""
    p, v = tables.plugins, tables.plugin_versions
    left = (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(v.c.signature_kind == IMAGE, p.c.name.not_in(kept or [""]))
        .with_for_update(of=p)
    )
    return (await conn.execute(left)).all()  # pyright: ignore[reportReturnType]

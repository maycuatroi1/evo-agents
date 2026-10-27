"""Plugins in projects: which installed plugin a project enabled, and which permissions of its manifest an admin of
the project granted it (table project_plugins, schema 0035).

An admin of the project reads, before granting, what the installed version of a plugin asks for, each permission with
what it lets the plugin do (``evo_agents.plugins.manifest.PERMISSION_TEXT``), and enables the plugin by granting
exactly those: POST .../enable names them back, and a list other than the one the manifest asks for is a 422 that
names it, so nobody grants what they did not read. Enabling a plugin enabled already with what it asks for changes
nothing; once an upgrade asks for more (``pending``), the same call grants the new list. Disabling deletes the
project's row and its grants with it; the versions of the plugin's config stay, for when it is enabled again.

What a plugin may do in a project (``grants``, ``project_grants``) is what was granted there that its installed version
still asks for, and only while it is installed and turned on: a removed plugin, or one the hub turned off, keeps its
rows and gets nothing. ``granted`` of the API says the same (``plugin_calls.effective_grant``), so the web and its
bridge go by what the hub checks: an upgrade that asks for less leaves the rest of the row in the table, not in the
grant.

canvas, srs and tasks are features of the hub (``plugins.builtin``), listed here as before with ``builtin`` true: the
project's layer of its features turns each on (``evo_agents.hub.feature_rule``), and while on it may do what it asks
for. Enabling or disabling one here writes that layer, for a project admin, as enabling the plugin did.

A core plugin of the hub's image that runs hub-wide (``evo_agents.plugins.manifest.is_per_project``: Telegram) is
installed in the store and is no project's plugin: a project's list leaves it out, and reading, enabling, disabling or
configuring it in a project is a 404 that says so.

Each change is audited in the project: plugin.project-enable then plugin.grant when a plugin is enabled, plugin.grant
alone when a grant changes, plugin.project-disable; the target is ``<project>/plugin:<name>@<version>``, a grant's
followed by `` permissions=<names>``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import features, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.feature_rule import FEATURES
from evo_agents.hub.plugin_calls import (  # noqa: F401 (where the routes and the tokens read them, as before)
    asked_for,
    effective_grant,
    enabled_plugins,
    enabled_query,
    enablements,
)
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.plugin_pages import PluginPage, pages_of
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.manifest import PERMISSION_TEXT, PERMISSIONS, TIERS, is_per_project

log = logging.getLogger(__name__)

PROJECT_STATES = ("installed", "disabled")
Permission = Annotated[str, Field(min_length=1, max_length=100)]


class PluginPermission(BaseModel):
    name: str
    text: str = Field(description="what it lets the plugin do in the project")
    granted: bool


class ProjectPlugin(BaseModel):
    """An installed plugin as one project sees it: what it asks for, and whether the project enabled it."""

    name: str
    title: str = Field(description="the name its manifest gives it")
    description: str
    tier: Literal[TIERS]
    publisher: str
    version: str = Field(description="the version installed on the hub")
    state: Literal[PROJECT_STATES] = Field(description="disabled: the hub turned it off, and it does nothing anywhere")
    disabled_reason: str | None
    enabled: bool
    builtin: bool = Field(
        False,
        description="a feature of the hub (canvas, srs, tasks), not a plugin of the store: the project's features turn "
        "it on and off, and it may do what it asks for while on",
    )
    permissions: list[PluginPermission] = Field(description="what the installed version asks for, granted or not")
    granted: list[str] = Field(
        description="what it may do in the project: what an admin of the project granted it that the installed version "
        "still asks for, in force while state is installed; empty while not enabled"
    )
    pending: list[str] = Field(
        description="asked for and not granted, after an upgrade asked for more: enable it again to grant them"
    )
    records_level: str | None = Field(
        None,
        description="the highest level of the project's ladder its jobs and events, which have no caller, reach in its "
        "records (records:use); none: they reach none",
    )
    config_schema: dict | None = Field(description="the JSON Schema of its config, secret fields marked x-secret")
    pages: list[PluginPage] = Field(description="the pages it adds to the project's part of the web")
    enabled_by: str | None
    enabled_at: datetime | None
    granted_by: str | None
    granted_at: datetime | None


class PluginEnable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permissions: list[Permission] = Field(
        max_length=len(PERMISSIONS),
        description="the grant: exactly what the installed version asks for, as GET .../plugins/{name} shows it",
    )
    records_level: str | None = Field(
        None,
        description="a level of the project's ladder: the highest its jobs and events, which have no caller, reach in "
        "the plugin's records; left out, what is set stays (none at first)",
    )


# Access


def check_reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the plugins of project {access.name} needs a grant on it")


def check_admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"{doing} needs the admin role on project {access.name}; you hold {held}")


# Reading


def _query(project_id: int):
    """The installed plugins, by name, with what project ``project_id`` enabled and granted."""
    p, v, pp, e = tables.plugins, tables.plugin_versions, tables.project_plugins, enablements()
    enabler, granter = tables.users.alias("enabler"), tables.users.alias("granter")
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.publisher,
            p.c.installed_version,
            p.c.disabled_reason,
            p.c.builtin,
            v.c.manifest,
            v.c.sha256,
            e.c.plugin_id.label("enabled_id"),
            e.c.permissions,
            enabler.c.login.label("enabled_by"),
            pp.c.enabled_at,
            granter.c.login.label("granted_by"),
            pp.c.granted_at,
            pp.c.records_level,
        )
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(or_(p.c.project_id.is_(None), p.c.project_id == project_id))  # one installed for another is not seen
        .outerjoin(e, (e.c.plugin_id == p.c.id) & (e.c.project_id == project_id))
        .outerjoin(pp, (pp.c.plugin_id == p.c.id) & (pp.c.project_id == project_id))
        .outerjoin(enabler, enabler.c.id == pp.c.enabled_by)
        .outerjoin(granter, granter.c.id == pp.c.granted_by)
        .order_by(p.c.name)
    )


def view(row, project: str, secret: str | None) -> ProjectPlugin:
    """Plugin ``row`` of ``_query`` as project ``project`` sees it; ``secret`` signs the tickets of its pages."""
    asked = asked_for(row.manifest)
    enabled = row.enabled_id is not None
    granted = list(effective_grant(row)) if enabled else []  # an upgrade that asks for less narrows it
    return ProjectPlugin(
        name=row.name,
        title=row.manifest.get("name") or row.name,
        description=row.manifest.get("description") or "",
        tier=row.manifest.get("tier", "isolated"),
        publisher=row.publisher,
        version=row.installed_version,
        state="disabled" if row.disabled_reason else "installed",
        disabled_reason=row.disabled_reason,
        enabled=enabled,
        builtin=row.builtin,
        permissions=[
            PluginPermission(name=name, text=PERMISSION_TEXT.get(name, name), granted=name in granted) for name in asked
        ],
        granted=granted,
        pending=[name for name in asked if enabled and name not in granted],
        config_schema=row.manifest.get("config"),
        pages=pages_of(row.manifest, row.name, project, row.sha256, enabled and not row.disabled_reason, secret),
        enabled_by=row.enabled_by,
        enabled_at=row.enabled_at,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        records_level=row.records_level if enabled else None,
    )


async def list_project_plugins(conn: AsyncConnection, access: ProjectAccess, secret: str | None) -> list[ProjectPlugin]:
    rows = await conn.execute(_query(access.project_id))
    return [view(row, access.name, secret) for row in rows if is_per_project(row.manifest)]


def not_installed(name: str) -> HTTPException:
    return HTTPException(
        404, f"no plugin {name} is installed on this hub: see `evo-agents hub plugin list`, a hub admin installs one"
    )


def hub_wide(name: str) -> HTTPException:
    return HTTPException(
        404,
        f"plugin {name} runs hub-wide, as a core plugin of the hub's image: no project enables, grants or configures "
        "it",
    )


async def installed_row(conn: AsyncConnection, access: ProjectAccess, name: str, *, lock: bool = False):
    """The row of the installed plugin ``name`` from ``_query``; 404 when none is installed, or when it runs
    hub-wide."""
    query = _query(access.project_id).where(tables.plugins.c.name == name)
    if lock:  # enabling waits for an install, upgrade or removal under way, and they for it
        query = query.with_for_update(of=tables.plugins)
    row = (await conn.execute(query)).first()
    if row is None:
        raise not_installed(name)
    if not is_per_project(row.manifest):
        raise hub_wide(name)
    return row


async def project_plugin(conn: AsyncConnection, access: ProjectAccess, name: str, secret: str | None) -> ProjectPlugin:
    return view(await installed_row(conn, access, name), access.name, secret)


# Changing


def _target(access: ProjectAccess, row) -> str:
    return f"{access.name}/plugin:{row.name}@{row.installed_version}"


async def _record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )


def check_grant(row, wanted: list[str]) -> list[str]:
    """What the installed version of plugin ``row`` asks for, once ``wanted`` is exactly that; else the 422."""
    asked = asked_for(row.manifest)
    if len(set(wanted)) != len(wanted) or set(wanted) != set(asked):
        listed = ", ".join(asked) or "nothing"
        given = ", ".join(wanted) or "nothing"
        raise HTTPException(
            422,
            f"plugin {row.name} {row.installed_version} asks for {listed}; enabling it grants exactly that, and the "
            f"request grants {given}: read what it asks for with GET .../plugins/{row.name} and grant that; nothing "
            "changed",
        )
    return asked


async def enable(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    name: str,
    wanted: list[str],
    records_level: str | None = None,
) -> bool:
    """Enable plugin ``name`` for the project, granting ``wanted``, or grant ``wanted`` to it once enabled; False when
    nothing changed. ``records_level`` is the highest level its calls with no caller reach in its records. 409 for a
    plugin the hub turned off."""
    row = await installed_row(conn, access, name, lock=True)
    if row.disabled_reason:
        raise HTTPException(
            409, f"plugin {name} is turned off on this hub ({row.disabled_reason}); a hub admin upgrades it first"
        )
    asked = check_grant(row, wanted)
    if records_level is not None:
        if records_level not in access.rules.levels:
            raise HTTPException(422, f"records_level {records_level!r} is not a level of project {access.name}")
        if "records:use" not in asked:
            raise HTTPException(422, f"plugin {name} does not ask for records:use, so records_level has no use")
    if row.builtin:
        return await _turn_feature(conn, user, access, row, on=True)
    pp = tables.project_plugins
    grant = {"permissions": asked, "granted_by": user.user_id, "granted_at": func.now()}
    if records_level is not None:
        grant["records_level"] = records_level
    granted = f"{_target(access, row)} permissions={','.join(asked) or '-'}"
    if records_level is not None:
        granted += f" records_level={records_level}"
    if row.enabled_id is None:
        values = {"project_id": access.project_id, "plugin_id": row.id, "enabled_by": user.user_id, **grant}
        await conn.execute(insert(pp).values(values))
        await _record(conn, user, access, audit.PLUGIN_PROJECT_ENABLE, _target(access, row))
    elif set(row.permissions) == set(asked) and records_level in (None, row.records_level):
        return False
    else:
        mine = (pp.c.project_id == access.project_id) & (pp.c.plugin_id == row.id)
        await conn.execute(update(pp).where(mine).values(grant))
    await _record(conn, user, access, audit.PLUGIN_GRANT, granted)
    log.info("plugin enabled", extra={"project": access.name, "plugin": name, "permissions": asked})
    return True


async def disable(conn: AsyncConnection, user: Principal, access: ProjectAccess, name: str) -> None:
    """Disable plugin ``name`` for the project, its grants going with it; 409 when it is not enabled."""
    row = await installed_row(conn, access, name, lock=True)
    if row.enabled_id is None:
        raise HTTPException(409, f"plugin {name} is not enabled for project {access.name}")
    if row.builtin:
        await _turn_feature(conn, user, access, row, on=False)
        return
    pp = tables.project_plugins
    await conn.execute(delete(pp).where((pp.c.project_id == access.project_id) & (pp.c.plugin_id == row.id)))
    await _record(conn, user, access, audit.PLUGIN_PROJECT_DISABLE, _target(access, row))
    log.info("plugin disabled", extra={"project": access.name, "plugin": name})


async def _turn_feature(conn: AsyncConnection, user: Principal, access: ProjectAccess, row, *, on: bool) -> bool:
    """Turn the feature of the hub ``row`` names (canvas, srs, tasks) on or off in the project's layer of its features,
    as enabling or disabling it as a plugin did before it became one; False when it is on already. It may do what it
    asks for while on, with no grant of its own to keep."""
    if on and row.enabled_id is not None:
        return False
    place = hub_settings.Place(FEATURES, access.project_id, access.name)
    await hub_settings.apply(
        conn, user, place, {row.name: on}, [], None, lambda sets, after: features.problems(sets, features.PROJECT)
    )
    if on:
        await _record(conn, user, access, audit.PLUGIN_PROJECT_ENABLE, _target(access, row))
    else:
        await _record(conn, user, access, audit.PLUGIN_PROJECT_DISABLE, _target(access, row))
    log.info("feature turned %s", "on" if on else "off", extra={"project": access.name, "feature": row.name})
    return True


# What a plugin may do


async def project_grants(conn: AsyncConnection, name: str) -> dict[str, tuple[str, ...]]:
    """project name -> what plugin ``name`` may do there, for each project that enabled it; empty when it is not
    installed or turned off."""
    query = enabled_query().where(tables.plugins.c.name == name).order_by(tables.projects.c.name)
    return {row.project: effective_grant(row) for row in await conn.execute(query)}

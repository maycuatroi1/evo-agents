"""Plugins in projects: which installed plugin a project enabled, and which permissions of its manifest an admin of
the project granted it (table project_plugins, schema 0034).

An admin of the project reads, before granting, what the installed version of a plugin asks for, each permission with
what it lets the plugin do (``evo_agents.plugins.manifest.PERMISSION_TEXT``), and enables the plugin by granting
exactly those: POST .../enable names them back, and a list other than the one the manifest asks for is a 422 that
names it, so nobody grants what they did not read. Enabling a plugin enabled already with what it asks for changes
nothing; once an upgrade asks for more (``pending``), the same call grants the new list. Disabling deletes the
project's row and its grants with it; the versions of the plugin's config stay, for when it is enabled again.

What a plugin may do in a project (``grants``, ``project_grants``) is what was granted there that its installed version
still asks for, and only while it is installed and turned on: a removed plugin, or one the hub turned off, keeps its
rows and gets nothing.

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
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.manifest import PERMISSION_TEXT, PERMISSIONS

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
    publisher: str
    version: str = Field(description="the version installed on the hub")
    state: Literal[PROJECT_STATES] = Field(description="disabled: the hub turned it off, and it does nothing anywhere")
    disabled_reason: str | None
    enabled: bool
    permissions: list[PluginPermission] = Field(description="what the installed version asks for, granted or not")
    granted: list[str] = Field(description="what an admin of the project granted it; empty while not enabled")
    pending: list[str] = Field(
        description="asked for and not granted, after an upgrade: enable it again to grant them; what it may do is "
        "what is granted and asked for"
    )
    config_schema: dict | None = Field(description="the JSON Schema of its config, secret fields marked x-secret")
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
    p, v, pp = tables.plugins, tables.plugin_versions, tables.project_plugins
    enabler, granter = tables.users.alias("enabler"), tables.users.alias("granter")
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.publisher,
            p.c.installed_version,
            p.c.disabled_reason,
            v.c.manifest,
            pp.c.plugin_id.label("enabled_id"),
            pp.c.permissions,
            enabler.c.login.label("enabled_by"),
            pp.c.enabled_at,
            granter.c.login.label("granted_by"),
            pp.c.granted_at,
        )
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .outerjoin(pp, (pp.c.plugin_id == p.c.id) & (pp.c.project_id == project_id))
        .outerjoin(enabler, enabler.c.id == pp.c.enabled_by)
        .outerjoin(granter, granter.c.id == pp.c.granted_by)
        .order_by(p.c.name)
    )


def asked_for(manifest: dict) -> list[str]:
    return list(manifest.get("permissions") or [])


def view(row) -> ProjectPlugin:
    asked = asked_for(row.manifest)
    granted = list(row.permissions or [])
    return ProjectPlugin(
        name=row.name,
        publisher=row.publisher,
        version=row.installed_version,
        state="disabled" if row.disabled_reason else "installed",
        disabled_reason=row.disabled_reason,
        enabled=row.enabled_id is not None,
        permissions=[
            PluginPermission(name=name, text=PERMISSION_TEXT.get(name, name), granted=name in granted) for name in asked
        ],
        granted=granted,
        pending=[name for name in asked if row.enabled_id is not None and name not in granted],
        config_schema=row.manifest.get("config"),
        enabled_by=row.enabled_by,
        enabled_at=row.enabled_at,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
    )


async def list_project_plugins(conn: AsyncConnection, access: ProjectAccess) -> list[ProjectPlugin]:
    return [view(row) for row in await conn.execute(_query(access.project_id))]


def not_installed(name: str) -> HTTPException:
    return HTTPException(
        404, f"no plugin {name} is installed on this hub: see `evo-agents hub plugin list`, a hub admin installs one"
    )


async def installed_row(conn: AsyncConnection, access: ProjectAccess, name: str, *, lock: bool = False):
    """The row of the installed plugin ``name`` from ``_query``; 404 when none is installed."""
    query = _query(access.project_id).where(tables.plugins.c.name == name)
    if lock:  # enabling waits for an install, upgrade or removal under way, and they for it
        query = query.with_for_update(of=tables.plugins)
    row = (await conn.execute(query)).first()
    if row is None:
        raise not_installed(name)
    return row


async def project_plugin(conn: AsyncConnection, access: ProjectAccess, name: str) -> ProjectPlugin:
    return view(await installed_row(conn, access, name))


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


async def enable(conn: AsyncConnection, user: Principal, access: ProjectAccess, name: str, wanted: list[str]) -> bool:
    """Enable plugin ``name`` for the project, granting ``wanted``, or grant ``wanted`` to it once enabled; False when
    nothing changed. 409 for a plugin the hub turned off."""
    row = await installed_row(conn, access, name, lock=True)
    if row.disabled_reason:
        raise HTTPException(
            409, f"plugin {name} is turned off on this hub ({row.disabled_reason}); a hub admin upgrades it first"
        )
    asked = check_grant(row, wanted)
    pp = tables.project_plugins
    grant = {"permissions": asked, "granted_by": user.user_id, "granted_at": func.now()}
    granted = f"{_target(access, row)} permissions={','.join(asked) or '-'}"
    if row.enabled_id is None:
        values = {"project_id": access.project_id, "plugin_id": row.id, "enabled_by": user.user_id, **grant}
        await conn.execute(insert(pp).values(values))
        await _record(conn, user, access, audit.PLUGIN_PROJECT_ENABLE, _target(access, row))
    elif set(row.permissions) == set(asked):
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
    pp = tables.project_plugins
    await conn.execute(delete(pp).where((pp.c.project_id == access.project_id) & (pp.c.plugin_id == row.id)))
    await _record(conn, user, access, audit.PLUGIN_PROJECT_DISABLE, _target(access, row))
    log.info("plugin disabled", extra={"project": access.name, "plugin": name})


# What a plugin may do


def enabled_query():
    """The plugins each project enabled that are installed and turned on: plugin id, name, installed version and its
    manifest, the project's id and name, and what it granted."""
    p, v, pp, projects = tables.plugins, tables.plugin_versions, tables.project_plugins, tables.projects
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.installed_version,
            v.c.manifest,
            pp.c.project_id,
            projects.c.name.label("project"),
            pp.c.permissions,
        )
        .join_from(pp, p, p.c.id == pp.c.plugin_id)
        .join(v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .join(projects, projects.c.id == pp.c.project_id)
        .where(p.c.disabled_reason.is_(None))
    )


def effective_grant(row) -> tuple[str, ...]:
    """What an enabled plugin may do in the project of ``row`` (of ``enabled_query``): granted and asked for."""
    asked = asked_for(row.manifest)
    return tuple(name for name in row.permissions if name in asked)


async def enabled_plugins(conn: AsyncConnection, project_id: int) -> list:
    """The rows of ``enabled_query`` of project ``project_id``, by plugin name."""
    query = enabled_query().where(tables.project_plugins.c.project_id == project_id).order_by(tables.plugins.c.name)
    return list(await conn.execute(query))


async def project_grants(conn: AsyncConnection, name: str) -> dict[str, tuple[str, ...]]:
    """project name -> what plugin ``name`` may do there, for each project that enabled it; empty when it is not
    installed or turned off."""
    query = enabled_query().where(tables.plugins.c.name == name).order_by(tables.projects.c.name)
    return {row.project: effective_grant(row) for row in await conn.execute(query)}

"""Who a call of the plugin API acts as when it writes or reads what a member would (``docs/plugins.md``, section
Permissions): the routes of tasks, docs, runs, srs and chats of the package.

A route under /v1/plugin-api/ takes the token of a plugin's backend and no other credential (``task_dispatch``). Each
of these routes acts in a project that enabled the plugin and granted it the permission the route needs, among what
the installed version still asks for (``granted``; 403 otherwise, the same for a project that does not exist). What
it writes and reads goes through the code a member's request runs, in the name of the admin who granted the plugin its
permissions last (project_plugins.granted_by, ``acting``): their grant on the project as it is now, read through the
project's hub sink, as a member's request without X-Evo-Sink is. A plugin writes nothing once that admin holds less
than writer on the project (403): an admin of the project enables the plugin again, which grants it in their name.
A route of a feature (tasks, docs, srs) answers 404 in a project that does not have it on (``feature_on``); the
routes of chats, a feature of the whole hub, answer 404 while the hub has it off.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls
from evo_agents.hub.server.plugin_api.models.access import Acting
from evo_agents.hub.server.plugin_api.repository import access as store
from evo_agents.hub.server.projects import access_of
from evo_agents.hub.server.security import holds


async def granted(conn: AsyncConnection, plugin_id: int, project: str, permission: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in ``project``, granted ``permission``; 403
    otherwise, the same for a project that does not exist."""
    enabled = await store.enabled(conn, plugin_id, project)
    if enabled is None or permission not in plugin_calls.effective_grant(enabled):
        raise HTTPException(
            403,
            f"the plugin is not enabled in project {project} with {permission} granted: an admin of the project "
            "enables it and grants what it asks for",
        )
    return enabled


async def acting(conn: AsyncConnection, enabled, *, writes: bool = True) -> Acting:
    """The admin who granted the plugin of ``enabled`` (a row of ``granted``) its permissions, with their access to
    its project now; 403 when they hold no grant there, or less than writer when the call ``writes``."""
    found = await store.granter(conn, enabled)
    access = await access_of(conn, found.id, enabled.project) if found is not None else None
    needs = "writer" if writes else "reader"
    if found is None or access is None or not holds(access.role, needs):
        held = access.role if access is not None and access.role else "no grant"
        who = found.login if found is not None else "nobody"
        raise HTTPException(
            403,
            f"plugin {enabled.name} acts in project {enabled.project} in the name of {who}, who granted it its "
            f"permissions and holds {held} there now, not {needs}: an admin of the project enables it again",
        )
    return Acting(enabled.id, enabled.name, found.id, found.login, access)


async def feature_on(conn: AsyncConnection, feature: str, enabled) -> None:
    """404 when the project of ``enabled`` does not have the feature ``feature`` (tasks, docs, srs) on."""
    if await plugin_calls.enabled_in(conn, feature, enabled.project_id) is None:
        raise HTTPException(
            404,
            f"project {enabled.project} does not have the feature {feature} on: an admin of the project turns it on",
        )


__all__ = ["acting", "feature_on", "granted"]

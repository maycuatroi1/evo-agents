"""The hub's own check of what a plugin's page asks through the bridge of the web (docs/plugins.md, Pages).

An isolated plugin's page runs in a sandboxed frame and reaches the hub only through the bridge evo-plugin/1 of the
web, which answers each request by calling the hub with the viewer's session. The web names the plugin it calls for in
the header PLUGIN_HEADER, and the hub checks that call again, as it arrives, on top of the viewer's own access:

- a call with the header reaches only the reads of ``bridge_reads`` (GET, by the route's handler) and the routes of
  the plugin itself under /v1/x/<plugin>/ (``plugin_routes``, which check that the project enables it);
- a read of ``bridge_reads`` needs the plugin installed on the hub, turned on, enabled in the project the route names,
  and the permission it needs in what the project granted that the installed version still asks for
  (``plugin_calls.effective_grant``, as the extension points check it);
- the records of the plugin itself (``plugin_records_api.members``), read or written, need the permission ``records:use``
  as a read does, and only under the plugin's own name: a page reaches no other plugin's records;
- anything else is a 403, before the route runs.

The web checks the same before it calls, from what it read of the plugin a moment ago; the hub's check is of now, so a
page left open after the plugin was disabled, removed, turned off, or upgraded to ask for less gets nothing more. The
check is a dependency of every route of the app (``create_app``): a request without the header costs a header lookup.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import cache

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import effective_grant, enabled_query
from evo_agents.plugins.manifest import PLUGIN_ID

PLUGIN_HEADER = "X-Evo-Plugin"
PLUGIN_ROUTES = "/v1/x/"  # plugin_routes.MOUNT, the routes of plugins themselves
_PLUGIN = re.compile(PLUGIN_ID)


@cache
def bridge_reads() -> dict[Callable, str]:
    """What the bridge reads with each permission (web/src/components/plugins/bridge.ts, METHODS): the route's
    handler, and the permission it needs. Read when the first call with the header comes, once create_app has
    imported every route; memories are read with scope=project&project=<project> only."""
    from evo_agents.hub.server import memories, plans
    from evo_agents.hub.server.runs.routes import listing

    return {
        listing.list_runs: "runs:read",
        listing.show_run: "runs:read",
        plans.list_plans: "plans:read",
        plans.show: "plans:read",
        memories.list_memories: "memories:read",
    }


@cache
def bridge_records() -> dict[Callable, str]:
    """The routes of a plugin's own records, which a page reads and writes with any method, and the permission they need."""
    from evo_agents.hub.server import plugin_records_api as api

    handlers = (api.member_list, api.member_show, api.member_put, api.member_delete)
    return dict.fromkeys(handlers, "records:use")


def _refused(message: str) -> HTTPException:
    return HTTPException(403, message)


def _project_of(connection: HTTPConnection, needs: str) -> str | None:
    if needs == "memories:read":
        query = connection.query_params
        return query.get("project") if query.get("scope") == "project" else None
    return connection.path_params.get("project")


async def bridge_gate(connection: HTTPConnection) -> None:
    """Refuse a call the web makes for a plugin's page (header PLUGIN_HEADER) that the plugin may not make now."""
    plugin = connection.headers.get(PLUGIN_HEADER)
    if plugin is None:
        return
    if not _PLUGIN.fullmatch(plugin):
        raise _refused(f"{PLUGIN_HEADER} names a plugin by its id, such as sample")
    path = connection.url.path
    if path.startswith(PLUGIN_ROUTES):
        if path.startswith(f"{PLUGIN_ROUTES}{plugin}/"):
            return  # the plugin's own route, which checks that the project enables it
        raise _refused(f"a page of plugin {plugin} reaches only its own routes under {PLUGIN_ROUTES}{plugin}/")
    endpoint = connection.scope.get("endpoint")
    needs = bridge_reads().get(endpoint) if connection.scope.get("method") == "GET" else None
    if needs is None and endpoint in bridge_records():
        if connection.path_params.get("plugin") != plugin:
            raise _refused(f"a page of plugin {plugin} reaches only the records of plugin {plugin}")
        needs = bridge_records()[endpoint]
    project = _project_of(connection, needs) if needs else None
    if needs is None or not project:
        raise _refused(
            f"a page of plugin {plugin} reads runs, plans and the project's memories and keeps its own records "
            "through the bridge, and nothing else of the hub"
        )
    query = enabled_query().where(tables.plugins.c.name == plugin, tables.projects.c.name == project)
    async with connection.app.state.engine.begin() as conn:
        row = (await conn.execute(query)).first()
    if row is None or needs not in effective_grant(row):
        raise _refused(
            f"plugin {plugin} may not use {needs} in project {project} now: it is not enabled there, the hub turned it "
            "off, or the project did not grant it that; reload the page"
        )

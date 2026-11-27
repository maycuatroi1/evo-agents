"""The routes plugins contribute, under /v1/x/<plugin>/ (``docs/plugins.md``, section Extension points).

Every route of a plugin is a project's: /v1/x/<plugin>/<project>/... answers a member who holds a grant on the
project while the plugin is installed on the hub, turned on, and enabled by the project; otherwise it answers 404, as
for a project the caller cannot see, so a plugin's routes are not there for a project that did not enable it. The hub
authenticates the caller first (``security.Authenticate``), as for every other path under /v1.

- A core plugin's route names a FastAPI APIRouter (``python``), which the hub includes at /v1/x/<plugin> when it
  creates the app (``include``), with the check above as a dependency of every route: each of its paths starts with
  ``/{project}``. Its handlers run in this process and use the hub's own dependencies for the caller. A core plugin
  that kept the routes it had before it was one (Telegram) names the prefix its router goes under instead
  (``prefix``); those routes check their callers themselves, and no project enables them. A core plugin's webhook,
  which a service outside the hub calls without a credential of the hub, goes under the public prefix
  ``/v1/hooks/<its own id>/`` (``security.HOOKS_PREFIX``) and checks the service's secret itself. A router with a path
  under /v1/hooks/ that is not its own plugin's is left out, logged, as one that does not load; the one webhook
  outside it is Telegram's, ``/v1/telegram/webhook``, a named exception of ``security.PUBLIC_PATHS``.
- An isolated plugin's route answers /v1/x/<plugin>/<project>/<name> and every path below it, with the methods it
  declares (GET when it declares none, else 405). The hub hands the request to the plugin's backend through the plugin
  host (method ``route`` of plugin/1): the method, the path below the route, the query, the body in base64 (at most
  MAX_BODY bytes), only the request headers of FORWARDED_HEADERS (never the caller's credential or cookies), and the
  caller (login, project, role on it). The backend's answer is checked against plugin/1, then served from the hub's
  origin with only the response headers of ANSWERED_HEADERS: a content type outside SAFE_TYPES becomes
  application/octet-stream to download, never a page, and every answer carries ``nosniff`` and a sandboxing
  Content-Security-Policy, so a backend can set no cookie and run no script on the hub's origin. A backend that is
  down or past ROUTE_TIMEOUT is a 503, its error or an answer plugin/1 does not allow a 502.

The proxy is left out of the OpenAPI document: what a plugin's route takes and answers is the plugin's own.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response

from evo_agents.hub.plugin_calls import CoreRegistry, core_registry, load
from evo_agents.hub.server.plugins.models.extension import METHODS, MOUNT, PluginName, ProjectName, RouteName
from evo_agents.hub.server.plugins.service import extension as service
from evo_agents.hub.server.plugins.service.extension import Sent, check_enabled, mount_of, stray_hooks
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)


def _gate(plugin: str):
    """The dependency of every route of a core plugin's router under /v1/x/<plugin>: the project of its path enables
    the plugin, and the caller holds a grant there."""

    async def gate(request: Request, user: CurrentUser) -> None:
        project = request.path_params.get("project")
        if not project:
            raise HTTPException(404, f"the routes of plugin {plugin} are under {mount_of(plugin)}/<project>/")
        await check_enabled(request.app.state.engine, user, plugin, project)

    return gate


def include(app: FastAPI, registry: CoreRegistry | None = None) -> CoreRegistry:
    """Include the routers of the core plugins, then the proxy to isolated plugins' routes; the core plugins."""
    registry = core_registry() if registry is None else registry
    for manifest, item in registry.contributions("routes"):
        contribution = f"{manifest.id}:{item['name']}"
        try:
            router = load(item["python"])
        except Exception:  # a broken core plugin leaves its routes out, never the hub
            log.exception("core plugin route not loaded", extra={"contribution": contribution})
            continue
        stray = stray_hooks(manifest.id, router, item.get("prefix", mount_of(manifest.id)))  # pyright: ignore[reportArgumentType]
        if stray:  # a webhook under another plugin's public prefix, or under none: the whole router stays out
            log.error("core plugin route not included", extra={"contribution": contribution, "paths": stray[:5]})
            continue
        if "prefix" in item:
            app.include_router(router, prefix=item["prefix"])  # pyright: ignore[reportArgumentType]
        else:
            app.include_router(router, prefix=mount_of(manifest.id), dependencies=[Depends(_gate(manifest.id))])  # pyright: ignore[reportArgumentType]
    app.include_router(proxy)
    return registry


# Isolated plugins

proxy = APIRouter(prefix=MOUNT, tags=["plugins"], include_in_schema=False)


async def _sent(request: Request) -> Sent:
    return Sent(request.method, dict(request.query_params), request.headers, await request.body())


@proxy.api_route("/{plugin}/{project}/{route}", methods=list(METHODS))
async def isolated_route(
    request: Request, plugin: PluginName, project: ProjectName, route: RouteName, user: CurrentUser
) -> Response:
    """A route of an isolated plugin, answered by its backend."""
    return await service.proxy(request.app.state, user, plugin, project, route, "", await _sent(request))


@proxy.api_route("/{plugin}/{project}/{route}/{rest:path}", methods=list(METHODS))
async def isolated_route_below(
    request: Request, plugin: PluginName, project: ProjectName, route: RouteName, rest: str, user: CurrentUser
) -> Response:
    """A path below a route of an isolated plugin, answered by its backend."""
    return await service.proxy(request.app.state, user, plugin, project, route, rest, await _sent(request))

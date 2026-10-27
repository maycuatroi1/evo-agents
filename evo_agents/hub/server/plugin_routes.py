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

import base64
import binascii
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request, Response

from evo_agents.hub.plugin_calls import (
    CoreContext,
    CoreRegistry,
    PluginCallError,
    PluginHostUnavailable,
    call,
    core_registry,
    effective_grant,
    enabled_in,
    load,
    manifest_of,
)
from evo_agents.hub.server import plugin_delegation
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.projects import ProjectAccess, not_found, project_access
from evo_agents.hub.server.security import HOOKS_PREFIX, CurrentUser, Principal
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.protocol import NAME

log = logging.getLogger(__name__)

MOUNT = "/v1/x"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_BODY = 1024 * 1024  # bytes of a request body the hub hands a backend
ROUTE_TIMEOUT = 30.0
FORWARDED_HEADERS = ("accept", "accept-language", "content-type", "if-modified-since", "if-none-match", "user-agent")
ANSWERED_HEADERS = ("cache-control", "content-language", "etag", "last-modified")
SAFE_TYPES = (
    "application/json",
    "application/octet-stream",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/plain",
)
SANDBOX = "default-src 'none'; frame-ancestors 'none'; sandbox"
PluginName = Annotated[str, Path(pattern=PLUGIN_ID)]
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
RouteName = Annotated[str, Path(pattern=NAME["pattern"])]


def mount_of(plugin: str) -> str:
    return f"{MOUNT}/{plugin}"


def not_enabled(plugin: str, project: str) -> HTTPException:
    return HTTPException(
        404, f"plugin {plugin} is not enabled in project {project}: an admin of the project enables it"
    )


async def enabled_for(conn, user: Principal, plugin: str, project: str) -> tuple[ProjectAccess, object]:
    """The caller's access to ``project`` and the row of ``plugin`` enabled there (``plugin_calls.enabled_query``);
    404 when the caller holds no grant on the project, or the plugin is not installed, turned on and enabled there."""
    access = await project_access(conn, user, project)
    if access.role is None:  # a hub admin without a grant reads the store, not a project's plugins
        raise HTTPException(404, not_found(project))
    row = await enabled_in(conn, plugin, access.project_id)
    if row is None:
        raise not_enabled(plugin, project)
    return access, row


def _gate(plugin: str):
    """The dependency of every route of a core plugin's router under /v1/x/<plugin>: the project of its path enables
    the plugin, and the caller holds a grant there."""

    async def gate(request: Request, user: CurrentUser) -> None:
        project = request.path_params.get("project")
        if not project:
            raise HTTPException(404, f"the routes of plugin {plugin} are under {mount_of(plugin)}/<project>/")
        async with request.app.state.engine.begin() as conn:
            await enabled_for(conn, user, plugin, project)

    return gate


def route_paths(router: APIRouter, prefix: str = "") -> list[str]:
    """The paths of ``router``'s routes once it is included under ``prefix``, those of the routers it includes too."""
    found = []
    for route in getattr(router, "routes", []):
        path = getattr(route, "path", None)
        if isinstance(path, str):
            found.append(prefix + path)
        elif hasattr(route, "original_router"):  # a router it includes, as FastAPI keeps it since 0.14x
            found += route_paths(route.original_router, prefix + route.include_context.prefix)
    return found


def stray_hooks(plugin: str, router: APIRouter, prefix: str) -> list[str]:
    """The paths of ``router`` included under ``prefix`` that are under /v1/hooks/ but not under the public prefix of
    core plugin ``plugin``'s own webhooks, /v1/hooks/<plugin>/."""
    own = f"{HOOKS_PREFIX}{plugin}/"
    paths = route_paths(router, prefix)
    hooks = [path for path in paths if path.startswith(HOOKS_PREFIX) or path == HOOKS_PREFIX.rstrip("/")]
    return [path for path in hooks if not path.startswith(own)]


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
        stray = stray_hooks(manifest.id, router, item.get("prefix", mount_of(manifest.id)))
        if stray:  # a webhook under another plugin's public prefix, or under none: the whole router stays out
            log.error("core plugin route not included", extra={"contribution": contribution, "paths": stray[:5]})
            continue
        if "prefix" in item:
            app.include_router(router, prefix=item["prefix"])
        else:
            app.include_router(router, prefix=mount_of(manifest.id), dependencies=[Depends(_gate(manifest.id))])
    app.include_router(proxy)
    return registry


# Isolated plugins

proxy = APIRouter(prefix=MOUNT, tags=["plugins"], include_in_schema=False)


def _clean(value: str) -> bool:
    return value.isprintable() and value.isascii()


def _media(given: str | None) -> tuple[str, bool]:
    """The content type to serve for ``given``, and whether it is one of SAFE_TYPES (else a download)."""
    base = (given or "").split(";", 1)[0].strip().lower()
    if base not in SAFE_TYPES:
        return "application/octet-stream", False
    return (f"{base}; charset=utf-8" if base.startswith("text/") else base), True


def answer(plugin: str, result: dict) -> Response:
    """The response for a backend's ``route`` result, which plugin/1 allows: its status and body, with only the
    headers the hub serves."""
    status = result["status"]
    if not 200 <= status <= 599:
        raise HTTPException(502, f"plugin {plugin} answered status {status}, which the hub does not serve")
    try:
        body = base64.b64decode(result.get("body") or "", validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(502, f"plugin {plugin} answered a body that is not base64") from None
    given = {str(key).lower(): value for key, value in (result.get("headers") or {}).items()}
    headers = {key: value for key, value in given.items() if key in ANSWERED_HEADERS and _clean(value)}
    media, safe = _media(given.get("content-type"))
    if not safe:
        headers["content-disposition"] = "attachment"
    headers |= {"content-type": media, "x-content-type-options": "nosniff", "content-security-policy": SANDBOX}
    headers.setdefault("cache-control", "no-store")
    return Response(content=body, status_code=status, headers=headers)


async def _delegation(conn, row, access: ProjectAccess, user: Principal) -> str | None:
    """The delegation of the call (``plugin_delegation``) when the project granted the plugin ``records:use``."""
    if row.tier != "isolated" or plugin_delegation.PERMISSION not in effective_grant(row):
        return None
    return await plugin_delegation.mint(conn, row.id, access, user, None, ROUTE_TIMEOUT)


async def _proxy(request: Request, user: Principal, plugin: str, project: str, route: str, rest: str) -> Response:
    async with request.app.state.engine.begin() as conn:
        access, row = await enabled_for(conn, user, plugin, project)
        lease = await _delegation(conn, row, access, user)
    item = manifest_of(row).contribution("routes", route) if row.tier == "isolated" else None
    if item is None:
        raise HTTPException(404, f"plugin {plugin} has no route {route}")
    methods = item.get("methods") or ["GET"]
    if request.method not in methods:
        raise HTTPException(405, f"route {route} of plugin {plugin} takes {', '.join(methods)}")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, f"a request to a plugin's route carries at most {MAX_BODY // 1024} KiB")
    sent = {
        "method": request.method,
        "path": f"/{rest}",
        "query": dict(request.query_params),
        "headers": {name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers},
        "body": base64.b64encode(body).decode(),
    }
    caller = {"login": user.login, "project": project, "role": access.role}
    if lease is not None:
        caller["token"] = lease
    params = {"route": route, "request": sent, "caller": caller}
    context = CoreContext(plugin, request.app.state.engine, request.app.state.config, project, access.project_id)
    try:
        result = await call(
            request.app.state.plugin_host, plugin, row.tier, "routes", item, params, context, ROUTE_TIMEOUT
        )
    except PluginHostUnavailable as exc:
        raise HTTPException(503, f"plugin {plugin} is unavailable now: {exc}") from None
    except PluginCallError as exc:
        log.warning("plugin route failed", extra={"contribution": f"{plugin}:{route}", "error": str(exc)[:200]})
        raise HTTPException(502, f"plugin {plugin} failed: {exc}") from None
    return answer(plugin, result)


@proxy.api_route("/{plugin}/{project}/{route}", methods=list(METHODS))
async def isolated_route(
    request: Request, plugin: PluginName, project: ProjectName, route: RouteName, user: CurrentUser
) -> Response:
    """A route of an isolated plugin, answered by its backend."""
    return await _proxy(request, user, plugin, project, route, "")


@proxy.api_route("/{plugin}/{project}/{route}/{rest:path}", methods=list(METHODS))
async def isolated_route_below(
    request: Request, plugin: PluginName, project: ProjectName, route: RouteName, rest: str, user: CurrentUser
) -> Response:
    """A path below a route of an isolated plugin, answered by its backend."""
    return await _proxy(request, user, plugin, project, route, rest)

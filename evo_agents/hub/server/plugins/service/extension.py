"""What the hub does with the routes plugins contribute under /v1/x/<plugin>/ (``routes.extension`` has the design):
the check that a project enabled the plugin, the paths of a core plugin's router, and the proxy to an isolated
plugin's backend."""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Response

from evo_agents.hub.plugin_calls import (
    CoreContext,
    PluginCallError,
    PluginHostUnavailable,
    call,
    effective_grant,
    enabled_in,
    manifest_of,
)
from evo_agents.hub.server.plugins.models.delegation import PERMISSION
from evo_agents.hub.server.plugins.models.extension import (
    ANSWERED_HEADERS,
    FORWARDED_HEADERS,
    MAX_BODY,
    MOUNT,
    ROUTE_TIMEOUT,
    SAFE_TYPES,
    SANDBOX,
)
from evo_agents.hub.server.plugins.service.delegation import mint
from evo_agents.hub.server.projects import ProjectAccess, not_found, project_access
from evo_agents.hub.server.security import HOOKS_PREFIX, Principal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sent:
    """The parts of a request to an isolated plugin's route that the hub hands its backend."""

    method: str
    query: dict[str, str]
    headers: Mapping[str, str]
    body: bytes


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
    """The delegation of the call (``service.delegation``) when the project granted the plugin ``records:use``."""
    if row.tier != "isolated" or PERMISSION not in effective_grant(row):
        return None
    return await mint(conn, row.id, access, user, None, ROUTE_TIMEOUT)


async def proxy(state, user: Principal, plugin: str, project: str, route: str, rest: str, request: Sent) -> Response:
    """What the backend of isolated plugin ``plugin`` answers to ``request`` at path ``rest`` below its route
    ``route`` in ``project``."""
    async with state.engine.begin() as conn:
        access, row = await enabled_for(conn, user, plugin, project)
        lease = await _delegation(conn, row, access, user)
    item = manifest_of(row).contribution("routes", route) if row.tier == "isolated" else None  # pyright: ignore[reportAttributeAccessIssue]
    if item is None:
        raise HTTPException(404, f"plugin {plugin} has no route {route}")
    methods = item.get("methods") or ["GET"]
    if request.method not in methods:
        raise HTTPException(405, f"route {route} of plugin {plugin} takes {', '.join(methods)}")
    body = request.body
    if len(body) > MAX_BODY:
        raise HTTPException(413, f"a request to a plugin's route carries at most {MAX_BODY // 1024} KiB")
    sent = {
        "method": request.method,
        "path": f"/{rest}",
        "query": request.query,
        "headers": {name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers},
        "body": base64.b64encode(body).decode(),
    }
    caller = {"login": user.login, "project": project, "role": access.role}
    if lease is not None:
        caller["token"] = lease
    params = {"route": route, "request": sent, "caller": caller}
    context = CoreContext(plugin, state.engine, state.config, project, access.project_id)
    try:
        result = await call(state.plugin_host, plugin, row.tier, "routes", item, params, context, ROUTE_TIMEOUT)  # pyright: ignore[reportAttributeAccessIssue]
    except PluginHostUnavailable as exc:
        raise HTTPException(503, f"plugin {plugin} is unavailable now: {exc}") from None
    except PluginCallError as exc:
        log.warning("plugin route failed", extra={"contribution": f"{plugin}:{route}", "error": str(exc)[:200]})
        raise HTTPException(502, f"plugin {plugin} failed: {exc}") from None
    return answer(plugin, result)

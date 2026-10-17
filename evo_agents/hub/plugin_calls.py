"""How the hub's api and its job worker call a plugin's contribution to an extension point, core or isolated
(``docs/plugins.md``, section Extension points).

The two tiers share one contract: the params of plugin/1's method for the point (``evo_agents.plugins.protocol``)
go in and the result plugin/1 allows comes out, checked against ``protocol.RESULTS`` whichever tier answered, so a
result the hub cannot use never reaches a caller (``checked``). A core plugin's contribution names its Python
(``python``, ``module:attribute``), which runs here: ``handler(params, context)``, awaited when it returns an
awaitable, with a ``CoreContext`` (the plugin, the hub's engine and config, the project). An isolated plugin's runs in
its backend, which the plugin host runs apart from the hub (``PluginHostClient``): the host answers within the
timeout, a 503 when the backend is down (``PluginHostUnavailable``), the backend's error as ``PluginCallError``.

The core plugins of this process are those of the entry point group ``evo_agents.plugins``
(``evo_agents.plugins.core``), read once (``core_registry``; a test that installs others clears it). A plugin is
reachable in a project while it is installed on the hub, not turned off, and enabled by the project
(``enabled_query``), and what it may do there is what the project granted that its installed version still asks for
(``effective_grant``): the extension points check both on every call, never once for all.

This module is outside ``evo_agents.hub.server`` so the job worker (``evo_agents.hub.worker``) runs plugins' jobs and
events with it, and imports nothing of the server.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from dataclasses import dataclass
from functools import cache

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.plugins.core import CorePlugin, core_plugins
from evo_agents.plugins.manifest import Manifest, read_manifest
from evo_agents.plugins.protocol import INTERNAL_ERROR, POINT_METHODS, result_problems

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0  # seconds one call to a plugin may take
NAMESPACE = "__"  # between a plugin's id and its tool's name on /mcp: sample__echo


class PluginHostUnavailable(RuntimeError):
    """The plugin host or a backend could not be reached: not configured, down, or past the timeout."""


class PluginCallError(RuntimeError):
    """The plugin answered an error, or a result plugin/1 does not allow."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class PluginHostClient:
    """The client to the plugin host, which runs the backends of isolated plugins. ``None`` config means no host."""

    def __init__(self, url: str | None, token: str | None):
        self.url = url.rstrip("/") if url else None
        self.token = token
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(base_url=self.url, headers={"Authorization": f"Bearer {self.token}"})
        return self._client

    async def call(self, plugin: str, method: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """The result of one plugin/1 call on the backend of ``plugin``; raises within ``timeout``."""
        if not self.configured:
            raise PluginHostUnavailable("no plugin host is configured (EVO_HUB_PLUGIN_HOST_URL, _TOKEN)")
        import httpx

        body = {"method": method, "params": params, "timeout": timeout}
        try:
            response = await (await self._http()).post(
                f"/v1/host/plugins/{plugin}/call", json=body, timeout=timeout + 5
            )
        except httpx.HTTPError as exc:
            raise PluginHostUnavailable(f"the plugin host did not answer: {exc}") from None
        if response.status_code == 503:
            raise PluginHostUnavailable(_json(response).get("message", "the backend is unavailable"))
        payload = _json(response)
        if response.status_code >= 400 or "error" in payload:
            error = payload.get("error") or {}
            raise PluginCallError(
                int(error.get("code", INTERNAL_ERROR)), str(error.get("message", response.text))[:2000]
            )
        return payload.get("result", {})

    async def status(self) -> list[dict]:
        if not self.configured:
            return []
        import httpx

        try:
            response = await (await self._http()).get("/v1/host/backends", timeout=10)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginHostUnavailable(f"the plugin host did not answer: {exc}") from None
        return _json(response).get("backends", [])

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _json(response) -> dict:
    try:
        found = response.json()
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


# The core plugins of this process


@dataclass(frozen=True)
class CoreRegistry:
    """The core plugins installed beside evo-agents, as the entry points give them."""

    plugins: tuple[CorePlugin, ...]

    def manifest(self, name: str) -> Manifest | None:
        return next((plugin.manifest for plugin in self.plugins if plugin.manifest.id == name), None)

    def contributions(self, point: str) -> list[tuple[Manifest, dict]]:
        """Every core plugin's contributions to ``point``, by plugin then in its manifest's order."""
        return [(plugin.manifest, item) for plugin in self.plugins for item in plugin.manifest.contributions(point)]


@cache
def core_registry() -> CoreRegistry:
    """The core plugins of this process, read once from the entry points."""
    return CoreRegistry(tuple(core_plugins()))


@cache
def load(ref: str):
    """The Python object ``module:attribute`` names."""
    module, _, attribute = ref.partition(":")
    found = importlib.import_module(module)
    for part in attribute.split("."):
        found = getattr(found, part)
    return found


@dataclass(frozen=True)
class CoreContext:
    """What a core plugin's Python gets beside the params: the plugin, the hub's engine (for its own tables) and
    config, and the project the call is for (None for a job, which names its projects in the params)."""

    plugin: str
    engine: AsyncEngine | None
    config: object | None = None
    project: str | None = None
    project_id: int | None = None


# Plugins in projects


def asked_for(manifest: dict) -> list[str]:
    """The permissions a manifest (as stored) asks for."""
    return list(manifest.get("permissions") or [])


def enabled_query():
    """The plugins each project enabled that are installed and turned on: plugin id, name, installed version, its
    manifest, tier and SHA-256, the project's id and name, and what it granted."""
    p, v, pp, projects = tables.plugins, tables.plugin_versions, tables.project_plugins, tables.projects
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.installed_version,
            v.c.manifest,
            v.c.tier,
            v.c.sha256,
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


async def enabled_in(conn: AsyncConnection, name: str, project_id: int):
    """The row of ``enabled_query`` of plugin ``name`` in project ``project_id``, or None."""
    query = enabled_query().where(tables.plugins.c.name == name, tables.project_plugins.c.project_id == project_id)
    return (await conn.execute(query)).first()


_MANIFESTS: dict[tuple[int, str], Manifest] = {}


def manifest_of(row) -> Manifest:
    """The manifest of a row of ``enabled_query`` as a ``Manifest``, read once per version and bundle."""
    key = (row.id, row.sha256)
    if key not in _MANIFESTS:
        _MANIFESTS[key] = read_manifest(row.manifest)
    return _MANIFESTS[key]


# Calling


def checked(method: str, result) -> dict:
    """``result`` when plugin/1 allows it as the result of ``method``; PluginCallError naming what is wrong."""
    problems = result_problems(method, result)
    if problems:
        raise PluginCallError(INTERNAL_ERROR, f"the plugin answered a result plugin/1 does not allow: {problems[0]}")
    return result


async def call(
    host,
    plugin: str,
    tier: str,
    point: str,
    item: dict,
    params: dict,
    context: CoreContext,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """The result of the contribution ``item`` of ``plugin`` to ``point``, checked against plugin/1; raises
    PluginCallError or PluginHostUnavailable within ``timeout``."""
    method = POINT_METHODS[point]
    if tier != "core":
        if host is None:
            raise PluginHostUnavailable("no plugin host is configured (EVO_HUB_PLUGIN_HOST_URL, _TOKEN)")
        return checked(method, await host.call(plugin, method, params, timeout))
    try:
        answer = load(item["python"])(params, context)
        if inspect.isawaitable(answer):
            answer = await asyncio.wait_for(answer, timeout)
    except TimeoutError:
        raise PluginCallError(INTERNAL_ERROR, f"plugin {plugin} did not answer within {timeout:g}s") from None
    except PluginCallError:
        raise
    except Exception as exc:  # a core plugin's own code may raise anything; it fails the call, never the hub
        log.exception("core plugin failed", extra={"contribution": f"{plugin}:{point}:{item['name']}"})
        raise PluginCallError(INTERNAL_ERROR, f"plugin {plugin} failed: {type(exc).__name__}") from None
    return checked(method, answer)


__all__ = [
    "CoreContext",
    "CoreRegistry",
    "PluginCallError",
    "PluginHostClient",
    "PluginHostUnavailable",
    "asked_for",
    "call",
    "checked",
    "core_registry",
    "effective_grant",
    "enabled_in",
    "enabled_plugins",
    "enabled_query",
    "load",
    "manifest_of",
]

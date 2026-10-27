"""How the hub's api and its job worker call a plugin's contribution to an extension point, core or isolated
(``docs/plugins.md``, section Extension points).

The two tiers share one contract: the params of plugin/1's method for the point (``evo_agents.plugins.protocol``)
go in and the result plugin/1 allows comes out, checked against ``protocol.RESULTS`` whichever tier answered, so a
result the hub cannot use never reaches a caller (``checked``). A core plugin's contribution names its Python
(``python``, ``module:attribute``), which runs here: ``handler(params, context)``, awaited when it returns an
awaitable, with a ``CoreContext`` (the plugin, the hub's engine and config, the project). An isolated plugin's runs in
its backend, which the plugin host runs apart from the hub (``PluginHostClient``): the host answers within the
timeout, a 503 when the backend is down (``PluginHostUnavailable``), the backend's error as ``PluginCallError``. The
503 carries why a backend is down, a failed start's text included; the client keeps it out of the message a project
member reads (``DOWN``), and a hub admin reads it in the backend's status.

The core plugins of this process are those of the entry point group ``evo_agents.plugins``
(``evo_agents.plugins.core``) and the features of the hub that run on the same extension points (``BUILTIN``: canvas,
srs, tasks, docs), read once (``core_registry``; a test that installs others clears it). A plugin is reachable in a
project while it is installed on the hub, not turned off, and enabled by the project (``enabled_query``), and what it may do
there is what the project granted that its installed version still asks for (``effective_grant``): the extension
points check both on every call, never once for all. A feature of ``BUILTIN`` is no plugin: its row in the store is
marked ``builtin``, no store or project lists it as a plugin, and it is enabled in each project the feature is on in
(``evo_agents.hub.feature_rule``), granted all it asks for (``enablements``).

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

from sqlalchemy import ARRAY, Text, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.feature_rule import feature_on
from evo_agents.plugins.core import CorePlugin, builtin_plugins, core_plugins
from evo_agents.plugins.manifest import PERMISSIONS, Manifest, read_manifest
from evo_agents.plugins.protocol import INTERNAL_ERROR, POINT_METHODS, result_problems

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0  # seconds one call to a plugin may take
DOWN = "its backend is not running now; a hub admin sees why in the plugin's backend status"
NAMESPACE = "__"  # between a plugin's id and its tool's name on /mcp: sample__echo
# The features of the hub that run on the plugins' extension points, by id: the manifest of each, as a core plugin's
# entry point names it. Each is a feature of evo_agents.hub.features of the same id.
BUILTIN = {
    "canvas": "evo_agents.hub.server.canvas:MANIFEST",
    "srs": "evo_agents.hub.server.srs:MANIFEST",
    "tasks": "evo_agents.hub.server.tasks:MANIFEST",
    "docs": "evo_agents.hub.server.docs:MANIFEST",
}


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
        if response.status_code == 503:  # what the host says may hold a start's error: for an admin, not a member
            raise PluginHostUnavailable(DOWN)
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
    """The core plugins installed beside evo-agents, as the entry points give them, and the features of ``BUILTIN``."""

    plugins: tuple[CorePlugin, ...]

    def manifest(self, name: str) -> Manifest | None:
        return next((plugin.manifest for plugin in self.plugins if plugin.manifest.id == name), None)

    def contributions(self, point: str) -> list[tuple[Manifest, dict]]:
        """Every core plugin's contributions to ``point``, by plugin then in its manifest's order."""
        return [(plugin.manifest, item) for plugin in self.plugins for item in plugin.manifest.contributions(point)]


@cache
def core_registry() -> CoreRegistry:
    """The core plugins of this process, read once from the entry points, and the features of ``BUILTIN``, by name;
    an entry point that names a feature's id is left out, logged."""
    builtin = builtin_plugins(BUILTIN)
    found = list(builtin)
    for plugin in core_plugins():
        if plugin.manifest.id in BUILTIN:
            log.warning("core plugin left out: a feature of the hub has its id", extra={"plugin": plugin.manifest.id})
            continue
        found.append(plugin)
    return CoreRegistry(tuple(sorted(found, key=lambda plugin: plugin.entry_point)))


@cache
def load(ref: str):
    """The Python object ``module:attribute`` names."""
    module, _, attribute = ref.partition(":")
    found = importlib.import_module(module)
    for part in attribute.split("."):
        found = getattr(found, part)
    return found


@dataclass(frozen=True)
class ApiContext:
    """What a core plugin's Python gets beside the params, as the core plugin API serves it (``Context`` of
    ``evo_agents.hub.server.core_plugin_api``, whose fields are those of version 1.1): the plugin, the hub's engine
    (for its own tables) and config, the project the call is for (None for a job, which names its projects in the
    params), and for a tool the agent of a run calls, that run (``X-Evo-Run`` on /mcp; None for any other caller). A
    tool also gets the sink of the caller's session on /mcp (``X-Evo-Sink``), which its reads go through, and the
    hub's job queue. A field added here changes the API's surface and takes its next version."""

    plugin: str
    engine: AsyncEngine | None
    config: object | None = None
    project: str | None = None
    project_id: int | None = None
    run_id: int | None = None
    sink: str | None = None
    jobs: object | None = None


@dataclass(frozen=True)
class CoreContext(ApiContext):
    """The context the hub hands every core plugin: ``ApiContext`` and what only the core plugins of this repository
    read, outside the API: for a tool, ``graph_refs(ids, paths)``, which awaits which of some node ids and paths the
    project's knowledge graph on the hub lacks as the caller reads it through its sink
    (``evo_agents.hub.server.kg.unknown_refs``; None elsewhere)."""

    graph_refs: object | None = None


# Plugins in projects


def asked_for(manifest: dict) -> list[str]:
    """The permissions a manifest (as stored) asks for."""
    return list(manifest.get("permissions") or [])


def enablements():
    """Each plugin enabled in a project, as a subquery (project_id, plugin_id, permissions): the rows of project_plugins,
    and for each feature of ``BUILTIN`` whose row in the store is marked builtin, each project the feature is on in,
    granted every permission (``effective_grant`` leaves what its manifest does not ask for)."""
    pp, p, projects = tables.project_plugins, tables.plugins, tables.projects
    every = literal(list(PERMISSIONS), ARRAY(Text))
    parts = [select(pp.c.project_id, pp.c.plugin_id, pp.c.permissions)]
    for name in BUILTIN:
        parts.append(
            select(projects.c.id.label("project_id"), p.c.id.label("plugin_id"), every.label("permissions"))
            .join_from(projects, p, p.c.name == name)
            .where(p.c.builtin, feature_on(name, projects.c.id))
        )
    return union_all(*parts).subquery("enablements")


def enabled_query():
    """The plugins each project enabled that are installed and turned on: plugin id, name, installed version, its
    manifest, tier and SHA-256, the project's id and name, and what it granted. A query narrows it to one project by
    ``tables.projects.c.id`` (or ``.name``)."""
    p, v, projects = tables.plugins, tables.plugin_versions, tables.projects
    e = enablements()
    return (
        select(
            p.c.id,
            p.c.name,
            p.c.installed_version,
            v.c.manifest,
            v.c.tier,
            v.c.sha256,
            e.c.project_id,
            projects.c.name.label("project"),
            e.c.permissions,
        )
        .join_from(e, p, p.c.id == e.c.plugin_id)
        .join(v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .join(projects, projects.c.id == e.c.project_id)
        .where(p.c.disabled_reason.is_(None), or_(p.c.project_id.is_(None), p.c.project_id == e.c.project_id))
    )


def effective_grant(row) -> tuple[str, ...]:
    """What an enabled plugin may do in the project of ``row`` (of ``enabled_query``): granted and asked for."""
    asked = asked_for(row.manifest)
    return tuple(name for name in row.permissions if name in asked)


async def enabled_plugins(conn: AsyncConnection, project_id: int) -> list:
    """The rows of ``enabled_query`` of project ``project_id``, by plugin name."""
    query = enabled_query().where(tables.projects.c.id == project_id).order_by(tables.plugins.c.name)
    return list(await conn.execute(query))


async def enabled_in(conn: AsyncConnection, name: str, project_id: int):
    """The row of ``enabled_query`` of plugin ``name`` in project ``project_id``, or None."""
    query = enabled_query().where(tables.plugins.c.name == name, tables.projects.c.id == project_id)
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
    "BUILTIN",
    "ApiContext",
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
    "enablements",
    "load",
    "manifest_of",
]

"""The plugin host as the hub's api sees it: the api's client to it, the secret the two share, the routes the host
calls, and the backend status an admin reads.

The backends of isolated plugins run in the plugin host, ``evo-agents hub plugin-host``, a service of its own that
holds none of the hub's variables (``docs/plugins.md``). The api reaches it at ``config.plugin_host_url`` and the two
authenticate each other with ``config.plugin_host_token``, the shared secret, compared in constant time; without both,
the api runs no backend and ``PluginHostClient.call`` fails at once with ``PluginHostUnavailable``, which the extension
points (step 5) turn into a 503.

- ``PluginHostClient.call`` proxies one plugin/1 method to a backend and returns its result, or raises within
  ``timeout``: ``PluginHostUnavailable`` when the host or the backend is down, ``PluginCallError`` for the backend's
  error. The timeout is the api's own, so a hung backend never holds an api request open past it.
- the host calls two routes of the api, both under ``/v1/plugin-host/`` and authenticated by the shared secret (they
  are in ``security.PUBLIC_PATHS`` and check the secret themselves, like the Telegram webhook): ``GET .../plugins``
  gives the host every installed isolated plugin with its bundle, the token to start it with and its decrypted config
  (``plugin_tokens.backend_payload``); ``POST .../status`` is where the host reports each backend's state, which the
  hub keeps in ``plugin_backends`` (0035) for an admin to read with ``GET /v1/plugins/{name}/backend``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_store import fetch_bundle, find, not_found, version_rows
from evo_agents.hub.server.plugin_tokens import backend_payload, issue_token
from evo_agents.hub.server.security import same
from evo_agents.plugins.manifest import read_manifest

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
BACKEND_STATES = ("placing", "starting", "running", "backoff")
ISOLATIONS = ("uid", "process")


class PluginHostUnavailable(RuntimeError):
    """The plugin host or a backend could not be reached: not configured, down, or past the timeout."""


class PluginCallError(RuntimeError):
    """The backend answered a plugin/1 error."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class PluginHostClient:
    """The api's client to the plugin host. One per app, opened in the lifespan; ``None`` config means no host."""

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
            raise PluginHostUnavailable(response.json().get("message", "the backend is unavailable"))
        if response.status_code >= 400:
            payload = _json(response)
            error = payload.get("error") or {}
            raise PluginCallError(int(error.get("code", -32603)), str(error.get("message", response.text))[:2000])
        return _json(response).get("result", {})

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
        return response.json()
    except ValueError:
        return {}


# The secret the host and the api share


def host_caller(request: Request) -> None:
    """Allow a request that carries the shared plugin-host secret as a Bearer token; 503 without a secret, 401 when
    it does not match. Constant-time, before any work."""
    token = request.app.state.config.plugin_host_token
    if not token:
        raise HTTPException(503, "this hub runs no plugin host (EVO_HUB_PLUGIN_HOST_TOKEN is not set)")
    header = request.headers.get("authorization", "")
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer" or not given.strip() or not same(given.strip(), token):
        raise HTTPException(401, "the plugin host must send the shared secret as a Bearer token")


HostCaller = Annotated[None, Depends(host_caller)]


# What the host pulls


class BackendPlugin(BaseModel):
    name: str
    version: str
    tier: str
    sha256: str = Field(description="the bundle's SHA-256; the host fetches it from the blob store")
    size: int
    backend: dict = Field(description="the manifest's backend: runtime, entry, requirements")
    init: dict = Field(description="the initialize payload, config secrets decrypted, the fresh token included")


class BackendStatusIn(BaseModel):
    plugin: str
    version: str
    state: str
    isolation: str
    pid: int | None = None
    restarts: int = 0
    error: str | None = None


class BackendStatus(BaseModel):
    plugin: str
    version: str | None
    state: str | None
    isolation: str | None
    pid: int | None
    restarts: int | None
    error: str | None
    started_at: datetime | None
    retry_at: datetime | None
    reported_at: datetime | None


router = APIRouter(
    prefix="/v1/plugin-host",
    tags=["plugin-host"],
    dependencies=[Depends(host_caller)],
    responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)


@router.get("/plugins", response_model=list[BackendPlugin])
async def host_plugins(request: Request, _: HostCaller) -> list[BackendPlugin]:
    """Every installed isolated plugin with a backend, for the host to run: its bundle, a fresh token and its config."""
    state = request.app.state
    hub_url = state.config.public_url or ""
    async with state.engine.begin() as conn:
        rows = await _installed_isolated(conn)
        out = []
        for row in rows:
            manifest = read_manifest(row.manifest)
            if manifest.backend is None:
                continue
            token = await issue_token(conn, row.id)
            init = await backend_payload(conn, state.sealer, hub_url, token, row)
            out.append(
                BackendPlugin(
                    name=row.name,
                    version=row.installed_version,
                    tier=manifest.tier,
                    sha256=row.sha256,
                    size=row.size,
                    backend=manifest.backend,
                    init=init,
                )
            )
    return out


@router.get("/plugins/{name}/bundle")
async def host_bundle(request: Request, name: str, _: HostCaller):
    """The bytes of an installed plugin's bundle, for the host to place; 404 when it is not installed."""
    from fastapi import Response

    state = request.app.state
    async with state.engine.begin() as conn:
        row = await find(conn, name)
        if row is None or row.installed_version is None:
            raise not_found(name)
        version = next(v for v in await version_rows(conn, row.id) if v.version == row.installed_version)
    data = await fetch_bundle(_blobs(request), version.sha256, version.size)
    return Response(content=data, media_type="application/gzip")


def _blobs(request: Request):
    from evo_agents.hub.server.blobs import blob_store

    return blob_store(request)


@router.post("/status", status_code=204)
async def report_status(request: Request, _: HostCaller, body: list[BackendStatusIn]) -> None:
    """The host reports each backend's state; the hub keeps it in plugin_backends for an admin to read."""
    pb = tables.plugin_backends
    async with request.app.state.engine.begin() as conn:
        ids = {row.name: row.id for row in await conn.execute(select(tables.plugins.c.id, tables.plugins.c.name))}
        from sqlalchemy import func

        for item in body:
            plugin_id = ids.get(item.plugin)
            if plugin_id is None:
                continue
            values = {
                "plugin_id": plugin_id,
                "version": item.version,
                "state": item.state if item.state in BACKEND_STATES else "backoff",
                "isolation": item.isolation if item.isolation in ISOLATIONS else "process",
                "pid": item.pid,
                "restarts": max(item.restarts, 0),
                "error": (item.error or None) and item.error[:4000],
                "reported_at": func.now(),
            }
            await conn.execute(
                pg_insert(pb).values(**values).on_conflict_do_update(index_elements=[pb.c.plugin_id], set_=values)
            )


async def _installed_isolated(conn: AsyncConnection) -> list:
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest, v.c.sha256, v.c.size, v.c.tier)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.installed_version.is_not(None), p.c.disabled_reason.is_(None), v.c.tier == "isolated")
        .order_by(p.c.name)
    )
    return list(await conn.execute(query))


async def backend_status(conn: AsyncConnection, plugin_id: int, name: str) -> BackendStatus:
    pb = tables.plugin_backends
    row = (await conn.execute(select(pb).where(pb.c.plugin_id == plugin_id))).first()
    if row is None:
        return BackendStatus(
            plugin=name,
            version=None,
            state=None,
            isolation=None,
            pid=None,
            restarts=None,
            error=None,
            started_at=None,
            retry_at=None,
            reported_at=None,
        )
    return BackendStatus(
        plugin=name,
        version=row.version,
        state=row.state,
        isolation=row.isolation,
        pid=row.pid,
        restarts=row.restarts,
        error=row.error,
        started_at=row.started_at,
        retry_at=row.retry_at,
        reported_at=row.reported_at,
    )

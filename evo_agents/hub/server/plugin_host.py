"""The plugin host as the hub's api sees it: the api's client to it, the secret the two share, the routes the host
calls, and the backend status an admin reads.

The backends of isolated plugins run in the plugin host, ``evo-agents hub plugin-host``, a service of its own that
holds none of the hub's variables (``docs/plugins.md``). The api reaches it at ``config.plugin_host_url`` and the two
authenticate each other with ``config.plugin_host_token``, the shared secret, compared in constant time; without both,
the api runs no backend and ``PluginHostClient.call`` fails at once with ``PluginHostUnavailable``, which the extension
points turn into a 503.

- ``PluginHostClient.call`` (``evo_agents.hub.plugin_calls``, where the job worker reaches it too) proxies one
  plugin/1 method to a backend and returns its result, or raises within ``timeout``: ``PluginHostUnavailable`` when
  the host or the backend is down, ``PluginCallError`` for the backend's error. The timeout is the api's own, so a
  hung backend never holds an api request open past it.
- the host calls the routes under ``HOST_ROUTES``, ``/internal/plugin-host/``, each authenticated by the shared secret
  (``host_caller``). They are outside /v1, which is all the reverse proxy in front of the hub sends to the api, so only
  a service on the stack's network reaches them; they are not in the OpenAPI document either. ``GET .../plugins``
  gives the host every installed isolated plugin with a backend and its ``initialize`` payload, the config decrypted
  (``plugin_tokens.backend_payload``), and mints nothing; ``POST .../plugins/{name}/token`` mints the token of the
  backend the host is about to start, revoking the plugin's older ones; ``GET .../plugins/{name}/bundle`` gives the
  bundle; ``POST .../status`` is where the host reports each backend's state, which the hub keeps in
  ``plugin_backends`` (0036) for an admin to read with ``GET /v1/plugins/{name}/backend``.
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
from evo_agents.hub.plugin_calls import (  # noqa: F401 (the api's names for them, as before)
    PluginCallError,
    PluginHostClient,
    PluginHostUnavailable,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_store import fetch_bundle, find, not_found, version_rows
from evo_agents.hub.server.plugin_tokens import backend_payload, issue_token
from evo_agents.hub.server.security import same
from evo_agents.plugins.manifest import read_manifest

log = logging.getLogger(__name__)

HOST_ROUTES = "/internal/plugin-host"  # outside /v1: the reverse proxy never sends it to the api
BACKEND_STATES = ("placing", "starting", "running", "backoff")
ISOLATIONS = ("uid", "process")


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
    init: dict = Field(description="the initialize payload, config secrets decrypted; the host adds the token")


class BackendToken(BaseModel):
    token: str = Field(description="a fresh evp_ token for the backend about to start; the plugin's older ones revoked")


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
    prefix=HOST_ROUTES,
    tags=["plugin-host"],
    dependencies=[Depends(host_caller)],
    responses={401: {"model": ErrorBody}, 503: {"model": ErrorBody}},
    include_in_schema=False,
)


@router.get("/plugins", response_model=list[BackendPlugin])
async def host_plugins(request: Request, _: HostCaller) -> list[BackendPlugin]:
    """Every installed isolated plugin with a backend, for the host to run: its bundle and its config. No token: the
    host asks for one when it starts a backend, so pulling again changes nothing."""
    state = request.app.state
    hub_url = state.config.public_url or ""
    async with state.engine.begin() as conn:
        rows = await _installed_isolated(conn)
        out = []
        for row in rows:
            manifest = read_manifest(row.manifest)
            if manifest.backend is None:
                continue
            init = await backend_payload(conn, state.sealer, hub_url, row)
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


@router.post("/plugins/{name}/token", response_model=BackendToken)
async def host_token(request: Request, name: str, _: HostCaller) -> BackendToken:
    """A fresh token for the backend of ``name`` the host is about to start, its older tokens revoked; 404 unless it
    is an installed, turned-on isolated plugin with a backend. The plugin's row stays locked until the token is
    written, as removing it and the hub turning it off lock it: either of those that runs at the same time waits and
    then revokes the token, or goes first and leaves no plugin to mint one for."""
    async with request.app.state.engine.begin() as conn:
        rows = await _installed_isolated(conn, name, lock=True)
        row = next((row for row in rows if row.manifest.get("backend")), None)
        if row is None:
            raise not_found(name)
        return BackendToken(token=await issue_token(conn, row.id))


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


async def _installed_isolated(conn: AsyncConnection, name: str | None = None, *, lock: bool = False) -> list:
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, p.c.name, p.c.installed_version, v.c.manifest, v.c.sha256, v.c.size, v.c.tier)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.installed_version.is_not(None), p.c.disabled_reason.is_(None), v.c.tier == "isolated")
        .order_by(p.c.name)
    )
    if name is not None:
        query = query.where(p.c.name == name)
    if lock:
        query = query.with_for_update(of=p)
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

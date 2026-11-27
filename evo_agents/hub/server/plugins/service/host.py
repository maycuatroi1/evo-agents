"""What the plugin host pulls from the hub and reports to it (``routes.host``): the installed isolated plugins with a
backend and their ``initialize`` payloads, the token of a backend about to start, the bundle of an installed plugin,
and the state of each backend, which an admin reads with ``GET /v1/plugins/{name}/backend``."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.credentials.models.sealing import Sealer
from evo_agents.hub.server.plugins.models.host import (
    BACKEND_STATES,
    ISOLATIONS,
    MAX_ERROR_CHARS,
    BackendPlugin,
    BackendStatus,
    BackendStatusIn,
)
from evo_agents.hub.server.plugins.repository import host as repository
from evo_agents.hub.server.plugins.repository import store as store_rows
from evo_agents.hub.server.plugins.service.store import fetch_bundle, not_found
from evo_agents.hub.server.plugins.service.tokens import backend_payload, issue_token
from evo_agents.plugins.manifest import read_manifest


async def backend_plugins(engine: AsyncEngine, sealer: Sealer | None, hub_url: str) -> list[BackendPlugin]:
    """Every installed isolated plugin with a backend, its bundle and its ``initialize`` payload."""
    out = []
    async with UnitOfWork(engine) as conn:
        for row in await repository.installed_isolated(conn):
            manifest = read_manifest(row.manifest)
            if manifest.backend is None:
                continue
            init = await backend_payload(conn, sealer, hub_url, row)
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


async def backend_token(engine: AsyncEngine, name: str) -> str:
    """A fresh token for the backend of ``name``, its older tokens revoked; 404 unless it is an installed, turned-on
    isolated plugin with a backend. The plugin's row stays locked until the token is written."""
    async with UnitOfWork(engine) as conn:
        rows = await repository.installed_isolated(conn, name, lock=True)
        row = next((row for row in rows if row.manifest.get("backend")), None)
        if row is None:
            raise not_found(name)
        return await issue_token(conn, row.id)


async def installed_bundle_row(engine: AsyncEngine, name: str):
    """The version row of the installed version of plugin ``name``; 404 when it is not installed."""
    async with UnitOfWork(engine) as conn:
        row = await store_rows.find(conn, name)
        if row is None or row.installed_version is None:
            raise not_found(name)
        return await store_rows.installed_version_row(conn, row)


async def bundle_of(store: BlobStore, version) -> bytes:
    return await fetch_bundle(store, version.sha256, version.size)


async def report(engine: AsyncEngine, body: list[BackendStatusIn]) -> None:
    """Keep each backend's state as the host reports it; a plugin the store does not hold is passed over."""
    async with UnitOfWork(engine) as conn:
        ids = await repository.plugin_ids(conn)
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
                "error": (item.error or None) and item.error[:MAX_ERROR_CHARS],
            }
            await repository.put_backend(conn, values)


async def backend_status(conn: AsyncConnection, plugin_id: int, name: str) -> BackendStatus:
    row = await repository.backend_row(conn, plugin_id)
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

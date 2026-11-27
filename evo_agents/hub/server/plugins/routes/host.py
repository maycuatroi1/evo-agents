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
  (``service.tokens.backend_payload``), and mints nothing; ``POST .../plugins/{name}/token`` mints the token of the
  backend the host is about to start, revoking the plugin's older ones; ``GET .../plugins/{name}/bundle`` gives the
  bundle; ``POST .../status`` is where the host reports each backend's state, which the hub keeps in
  ``plugin_backends`` (0036) for an admin to read with ``GET /v1/plugins/{name}/backend``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugins.models.host import HOST_ROUTES, BackendPlugin, BackendStatusIn, BackendToken
from evo_agents.hub.server.plugins.service.host import (
    backend_plugins,
    backend_token,
    bundle_of,
    installed_bundle_row,
    report,
)
from evo_agents.hub.server.security import same

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
        return await backend_plugins(conn, state.sealer, hub_url)


@router.post("/plugins/{name}/token", response_model=BackendToken)
async def host_token(request: Request, name: str, _: HostCaller) -> BackendToken:
    """A fresh token for the backend of ``name`` the host is about to start, its older tokens revoked; 404 unless it
    is an installed, turned-on isolated plugin with a backend. The plugin's row stays locked until the token is
    written, as removing it and the hub turning it off lock it: either of those that runs at the same time waits and
    then revokes the token, or goes first and leaves no plugin to mint one for."""
    async with request.app.state.engine.begin() as conn:
        return BackendToken(token=await backend_token(conn, name))


@router.get("/plugins/{name}/bundle")
async def host_bundle(request: Request, name: str, _: HostCaller):
    """The bytes of an installed plugin's bundle, for the host to place; 404 when it is not installed."""
    from fastapi import Response

    state = request.app.state
    async with state.engine.begin() as conn:
        version = await installed_bundle_row(conn, name)
    data = await bundle_of(_blobs(request), version)
    return Response(content=data, media_type="application/gzip")


def _blobs(request: Request):
    from evo_agents.hub.server.blobs import blob_store

    return blob_store(request)


@router.post("/status", status_code=204)
async def report_status(request: Request, _: HostCaller, body: list[BackendStatusIn]) -> None:
    """The host reports each backend's state; the hub keeps it in plugin_backends for an admin to read."""
    async with request.app.state.engine.begin() as conn:
        await report(conn, body)

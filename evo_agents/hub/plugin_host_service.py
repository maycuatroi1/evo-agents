"""``evo-agents hub plugin-host``: the service that runs the backends of isolated plugins, apart from the hub's api and
worker, holding none of the hub's variables (``docs/plugins.md``).

It is a small HTTP service the api calls, and a client that pulls from the api. It is given the hub's URL, the shared
secret EVO_HUB_PLUGIN_HOST_TOKEN and a data directory, and nothing else: not the DSN, not EVO_HUB_SECRETS_KEY, not the
blob store keys. It authenticates the api with the shared secret, and the api authenticates it with the same.

- the HTTP side (``create_host_app``): ``GET /health/live``; ``POST /v1/host/plugins/{name}/call`` proxies one
  plugin/1 method to a backend and answers within the timeout, a 503 when the backend is down; ``GET /v1/host/backends``
  the state of each backend; ``POST /v1/host/reload`` pulls from the hub now;
- the client side (``reconcile``): it pulls the installed isolated plugins from ``GET /v1/plugin-host/plugins`` of the
  api, downloads each bundle, places or updates its backend (``evo_agents.plugins.host.Host``), and reports each
  backend's state to ``POST /v1/plugin-host/status``. A loop does this every ``RELOAD_SECONDS``.

The supervisor (``Host``), the environment built from nothing and the not-dumpable backends are in
``evo_agents.plugins``; this module is the service around them. Needs the hub-server extra (fastapi, httpx).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from pathlib import Path

from fastapi import Request
from pydantic import BaseModel

from evo_agents.plugins.host import BackendUnavailable, Host
from evo_agents.plugins.manifest import read_manifest
from evo_agents.plugins.protocol import RemoteError

log = logging.getLogger("evo_agents.hub.plugin_host")


class CallIn(BaseModel):
    """One plugin/1 call the api asks the host to make on a backend."""

    method: str
    params: dict = {}
    timeout: float = 30.0


def _authorized(header: str, token: str) -> bool:
    scheme, _, given = header.partition(" ")
    return scheme.lower() == "bearer" and bool(given.strip()) and hmac.compare_digest(given.strip(), token)


RELOAD_SECONDS = 60
STATUS_SECONDS = 20
DEFAULT_PORT = 8090
TOKEN_VARIABLE = "EVO_HUB_PLUGIN_HOST_TOKEN"


class PluginHostConfig:
    """What the service runs with: the hub's URL, the shared secret and a data directory. No hub secret is here."""

    def __init__(self, hub_url: str, token: str, data_dir: Path, host: str, port: int):
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.data_dir = data_dir
        self.host = host
        self.port = port


def load_host_config(args) -> PluginHostConfig:
    """Read the service's configuration; ValueError naming what is missing."""
    hub_url = args.hub_url or os.environ.get("EVO_HUB_PUBLIC_URL", "").strip()
    token = os.environ.get(TOKEN_VARIABLE, "").strip()
    if not hub_url:
        raise ValueError("the plugin host needs the hub's URL: --hub-url or EVO_HUB_PUBLIC_URL")
    if not token:
        raise ValueError(f"the plugin host needs the shared secret {TOKEN_VARIABLE}")
    data_dir = Path(args.data_dir or os.environ.get("EVO_HUB_DATA_DIR") or "~/.evo/plugin-host").expanduser()
    return PluginHostConfig(hub_url, token, data_dir / "plugins", args.host, args.port or DEFAULT_PORT)


class Puller:
    """Pulls plugins from the hub api and reports status back, over the shared secret."""

    def __init__(self, hub_url: str, token: str):
        self.hub_url, self.token = hub_url, token
        self._client = None

    async def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.hub_url, headers={"Authorization": f"Bearer {self.token}"}, timeout=30
            )
        return self._client

    async def plugins(self) -> list[dict]:
        response = await (await self._http()).get("/v1/plugin-host/plugins")
        response.raise_for_status()
        return response.json()

    async def bundle(self, name: str) -> bytes:
        response = await (await self._http()).get(f"/v1/plugin-host/plugins/{name}/bundle")
        response.raise_for_status()
        return response.content

    async def report(self, status: list[dict]) -> None:
        body = [
            {k: s[k] for k in ("plugin", "version", "state", "isolation", "pid", "restarts", "error")} for s in status
        ]
        await (await self._http()).post("/v1/plugin-host/status", json=body)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


async def reconcile(host: Host, puller: Puller) -> None:
    """Pull the installed isolated plugins and place or update each backend; report the status afterwards."""
    try:
        plugins = await puller.plugins()
    except Exception as exc:  # the hub may be down; keep the backends as they are and try again later
        log.warning("cannot pull plugins from the hub", extra={"error": f"{type(exc).__name__}: {exc}"})
        return
    wanted = set()
    for item in plugins:
        name = item["name"]
        wanted.add(name)
        try:
            manifest = read_manifest(_manifest_for(item))
            data = await puller.bundle(name)
        except Exception as exc:
            log.warning("cannot place plugin", extra={"contribution": name, "error": str(exc)})
            continue
        host.place(manifest, data, item["init"])
    for name in list(host.backends):
        if name not in wanted:
            await host.backends.pop(name).stop()
    try:
        await puller.report(host.status())
    except Exception as exc:
        log.warning("cannot report status to the hub", extra={"error": f"{type(exc).__name__}: {exc}"})


def _manifest_for(item: dict) -> dict:
    """The manifest the host needs to run a backend: id, version, tier core/isolated, engines, plugin_api and the
    backend, from what the hub sent."""
    plugin = item["init"]["plugin"]
    return {
        "id": plugin["id"],
        "version": plugin["version"],
        "publisher": "hub",
        "tier": item.get("tier", "isolated"),
        "engines": {"evo-agents": ">=0"},
        "plugin_api": 1,
        "backend": item["backend"],
    }


async def _loop(host: Host, puller: Puller, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await reconcile(host, puller)
        try:
            await asyncio.wait_for(stop.wait(), RELOAD_SECONDS)
        except TimeoutError:
            pass


def create_host_app(host: Host, token: str, puller: Puller | None = None):
    """The FastAPI app of the plugin host: the api calls it with the shared secret. The types the routes and the
    dependency use are at module level, so FastAPI resolves their annotations (one defined in this function would read
    as a query param, since get_type_hints uses the module globals)."""
    from fastapi import Depends, FastAPI, HTTPException

    def caller(request: Request) -> None:
        if not _authorized(request.headers.get("authorization", ""), token):
            raise HTTPException(401, "the plugin host takes the shared secret as a Bearer token")

    app = FastAPI(title="evo-agents plugin host", docs_url=None, redoc_url=None)
    app.state.host = host
    guard = [Depends(caller)]

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.post("/v1/host/plugins/{name}/call", dependencies=guard)
    async def call(name: str, body: CallIn):
        timeout = max(0.1, min(body.timeout, 120.0))
        try:
            result = await host.call(name, body.method, body.params, timeout)
        except BackendUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except RemoteError as exc:
            return {"error": {"code": exc.code, "message": str(exc.message)[:2000]}}
        return {"result": result}

    @app.get("/v1/host/backends", dependencies=guard)
    async def backends() -> dict:
        return {"backends": host.status()}

    @app.post("/v1/host/reload", dependencies=guard)
    async def reload() -> dict:
        if puller is not None:
            await reconcile(host, puller)
        return {"backends": host.status()}

    return app


def run_service(config: PluginHostConfig) -> int:
    """Run the plugin host: the reconcile loop and the HTTP service, until stopped."""
    import uvicorn

    host = Host(config.data_dir)
    puller = Puller(config.hub_url, config.token)
    app = create_host_app(host, config.token, puller)
    stop = asyncio.Event()

    @app.on_event("startup")
    async def _start() -> None:
        app.state.loop_task = asyncio.create_task(_loop(host, puller, stop))

    @app.on_event("shutdown")
    async def _stop() -> None:
        stop.set()
        app.state.loop_task.cancel()
        await asyncio.gather(app.state.loop_task, return_exceptions=True)
        await host.stop_all()
        await puller.aclose()

    log.info("plugin host starting", extra={"hub": config.hub_url, "data_dir": str(config.data_dir)})
    uvicorn.run(app, host=config.host, port=config.port, log_config=None, access_log=False, server_header=False)
    return 0

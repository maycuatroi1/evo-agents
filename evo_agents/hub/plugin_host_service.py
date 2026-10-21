"""``evo-agents hub plugin-host``: the service that runs the backends of isolated plugins, apart from the hub's api and
worker, holding none of the hub's variables (``docs/plugins.md``).

It is a small HTTP service the api calls, and a client that pulls from the api. It is given the hub's URL, the shared
secret EVO_HUB_PLUGIN_HOST_TOKEN and a data directory, and nothing else: not the DSN, not EVO_HUB_SECRETS_KEY, not the
blob store keys. It authenticates the api with the shared secret, and the api authenticates it with the same. The
secret is at least 32 characters, never the example value, and registered so no log line shows it; without it the
service runs no backend and answers only its liveness check.

- the HTTP side (``create_host_app``): ``GET /health/live``; ``POST /v1/host/plugins/{name}/call`` proxies one
  plugin/1 method to a backend and answers within the timeout, a 503 when the backend is down; only ``ping`` and the
  methods of the extension points (``CALLABLE``) pass, never ``initialize`` or ``shutdown``, which are the host's own;
  ``GET /v1/host/backends`` the state of each backend; ``POST /v1/host/reload`` pulls from the hub now;
- the client side (``reconcile``): it pulls the installed isolated plugins from ``GET /internal/plugin-host/plugins``
  of the api, downloads each bundle, places or updates its backend (``evo_agents.plugins.host.Host``) and starts it in
  the background; just before each start the host asks the api for the backend's token (``Puller.token``). It reports
  each backend's state to ``POST /internal/plugin-host/status``. A loop pulls every ``RELOAD_SECONDS``, starts the
  backends whose backoff has passed every ``TICK_SECONDS`` and reports every ``STATUS_SECONDS``.

The service marks itself not dumpable at start (Linux), and as root gives each backend a user of its own; not as
root, it refuses to start a backend unless ``--allow-shared-user`` says this is a developer's machine
(``evo_agents.plugins.host``). The supervisor, the environment built from nothing and the not-dumpable backends are in
``evo_agents.plugins``; this module is the service around them. Needs the hub-server extra (fastapi, httpx).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import time
from pathlib import Path

from fastapi import Request
from pydantic import BaseModel

from evo_agents.hub.log import register_secret
from evo_agents.plugins.environment import isolation, set_not_dumpable
from evo_agents.plugins.host import BackendUnavailable, Host, host_token_problem
from evo_agents.plugins.manifest import read_manifest
from evo_agents.plugins.protocol import POINT_METHODS, RemoteError

log = logging.getLogger("evo_agents.hub.plugin_host")

RELOAD_SECONDS = 60
STATUS_SECONDS = 20
TICK_SECONDS = 5
DEFAULT_PORT = 8090
TOKEN_VARIABLE = "EVO_HUB_PLUGIN_HOST_TOKEN"
HUB_ROUTES = "/internal/plugin-host"  # the api's routes for the host (evo_agents.hub.server.plugin_host.HOST_ROUTES)
CALLABLE = frozenset({"ping", *POINT_METHODS.values()})  # what the api may ask of a backend


class CallIn(BaseModel):
    """One plugin/1 call the api asks the host to make on a backend."""

    method: str
    params: dict = {}
    timeout: float = 30.0


def _authorized(header: str, token: str | None) -> bool:
    scheme, _, given = header.partition(" ")
    return (
        bool(token) and scheme.lower() == "bearer" and bool(given.strip()) and hmac.compare_digest(given.strip(), token)
    )


class PluginHostConfig:
    """What the service runs with: the hub's URL, the shared secret and a data directory. No hub secret is here."""

    def __init__(
        self, hub_url: str, token: str | None, data_dir: Path, host: str, port: int, allow_shared_user: bool = False
    ):
        self.hub_url = hub_url.rstrip("/")
        self.token = token
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.allow_shared_user = allow_shared_user


def load_host_config(args) -> PluginHostConfig:
    """Read the service's configuration; ValueError naming what is wrong. A missing secret is no error: the service
    then runs no backend, so a hub without plugins can keep the service in its compose file."""
    hub_url = args.hub_url or os.environ.get("EVO_HUB_PUBLIC_URL", "").strip()
    token = os.environ.get(TOKEN_VARIABLE, "").strip() or None
    register_secret(token)
    if not hub_url:
        raise ValueError("the plugin host needs the hub's URL: --hub-url or EVO_HUB_PUBLIC_URL")
    problem = host_token_problem(token) if token else None
    if problem:
        raise ValueError(f"{TOKEN_VARIABLE} {problem}")
    data_dir = Path(args.data_dir or os.environ.get("EVO_HUB_DATA_DIR") or "~/.evo/plugin-host").expanduser()
    allow = bool(getattr(args, "allow_shared_user", False))
    return PluginHostConfig(hub_url, token, data_dir / "plugins", args.host, args.port or DEFAULT_PORT, allow)


class Puller:
    """Pulls plugins and tokens from the hub api and reports status back, over the shared secret."""

    def __init__(self, hub_url: str, token: str):
        self.hub_url, self.token_secret = hub_url, token
        self._client = None

    async def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.hub_url, headers={"Authorization": f"Bearer {self.token_secret}"}, timeout=30
            )
        return self._client

    async def plugins(self) -> list[dict]:
        response = await (await self._http()).get(f"{HUB_ROUTES}/plugins")
        response.raise_for_status()
        return response.json()

    async def bundle(self, name: str) -> bytes:
        response = await (await self._http()).get(f"{HUB_ROUTES}/plugins/{name}/bundle")
        response.raise_for_status()
        return response.content

    async def token(self, name: str) -> str:
        """A fresh token for the backend of ``name``, which the host is about to start."""
        response = await (await self._http()).post(f"{HUB_ROUTES}/plugins/{name}/token")
        response.raise_for_status()
        return response.json()["token"]

    async def report(self, status: list[dict]) -> None:
        body = [
            {k: s[k] for k in ("plugin", "version", "state", "isolation", "pid", "restarts", "error")} for s in status
        ]
        await (await self._http()).post(f"{HUB_ROUTES}/status", json=body)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


async def _report(host: Host, puller: Puller) -> None:
    try:
        await puller.report(host.status())
    except Exception as exc:
        log.warning("cannot report status to the hub", extra={"error": f"{type(exc).__name__}: {exc}"})


async def reconcile(host: Host, puller: Puller) -> None:
    """Pull the installed isolated plugins, place or update each backend and start it in the background; stop those
    no longer installed, in the background too (``Host.retire``), so no plugin's stop holds the loop that keeps the
    others running; report the status afterwards."""
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
            host.retire(name)
    host.kick_all()
    await _report(host, puller)


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
    pulled = reported = float("-inf")
    while not stop.is_set():
        now = time.monotonic()
        if now - pulled >= RELOAD_SECONDS:
            await reconcile(host, puller)
            pulled = reported = now
        else:
            host.kick_all()
            if now - reported >= STATUS_SECONDS:
                await _report(host, puller)
                reported = now
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except TimeoutError:
            pass


def create_host_app(host: Host, token: str | None, puller: Puller | None = None, lifespan=None):
    """The FastAPI app of the plugin host: the api calls it with the shared secret. The types the routes and the
    dependency use are at module level, so FastAPI resolves their annotations (one defined in this function would read
    as a query param, since get_type_hints uses the module globals)."""
    from fastapi import Depends, FastAPI, HTTPException

    def caller(request: Request) -> None:
        if not token:
            raise HTTPException(503, f"this plugin host runs no backend: {TOKEN_VARIABLE} is not set")
        if not _authorized(request.headers.get("authorization", ""), token):
            raise HTTPException(401, "the plugin host takes the shared secret as a Bearer token")

    app = FastAPI(title="evo-agents plugin host", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.host = host
    guard = [Depends(caller)]

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.post("/v1/host/plugins/{name}/call", dependencies=guard)
    async def call(name: str, body: CallIn):
        if body.method not in CALLABLE:
            raise HTTPException(403, f"the api may call ping and the extension points' methods, not {body.method!r}")
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


def _announce(config: PluginHostConfig) -> None:
    """Say at start how backends will run, loudly when they will not get users of their own."""
    dumpable = "not dumpable" if set_not_dumpable() else "dumpable (not Linux, or prctl failed)"
    mode = isolation()
    extra = {"hub": config.hub_url, "data_dir": str(config.data_dir), "isolation": mode, "host_process": dumpable}
    log.info("plugin host starting", extra=extra)
    if config.token is None:
        log.warning(f"{TOKEN_VARIABLE} is not set: the plugin host runs no backend")
    elif mode != "uid" and config.allow_shared_user:
        log.warning("--allow-shared-user: backends share the host's user; their files and the host's /proc are open")
    elif mode != "uid":
        log.error("the plugin host does not run as root: it will start no backend (see --allow-shared-user)")


def run_service(config: PluginHostConfig) -> int:
    """Run the plugin host: the reconcile loop and the HTTP service, until stopped."""
    import uvicorn

    _announce(config)
    puller = Puller(config.hub_url, config.token) if config.token else None
    mint = puller.token if puller is not None else None
    host = Host(config.data_dir, mint=mint, allow_shared_user=config.allow_shared_user)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()
        loop = asyncio.create_task(_loop(host, puller, stop)) if puller is not None else None
        try:
            yield
        finally:
            stop.set()
            if loop is not None:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            await host.stop_all()
            if puller is not None:
                await puller.aclose()

    app = create_host_app(host, config.token, puller, lifespan)
    uvicorn.run(app, host=config.host, port=config.port, log_config=None, access_log=False, server_header=False)
    return 0

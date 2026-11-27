"""The hub's FastAPI application.

The lifespan migrates the database (under the advisory lock, see ``migrate``), opens the connection pool and the
SQLAlchemy engine on it (``evo_agents.hub.db``), the GitHub client, the blob store client and the job queue the api
defers to, and closes them on shutdown, the engine before the pool; a database that cannot be reached or migrated stops
the start instead of serving errors. An R2 outage does not: health reports it and the blob routes answer 503. Every
request gets an id and one access log line without its query string, which can carry OAuth codes. Every path under /v1
needs a credential except the few ``security.PUBLIC_PATHS`` lists and the files of plugins' pages, which check a
ticket of their own (``security.PUBLIC_PREFIXES``), and the routes of a feature a hub admin turned off
answer 404 (``FeatureGate``, ``evo_agents.hub.server.hub_settings``). The OpenAPI document is served at /v1/openapi.json
for the web client's generated types. /mcp is the MCP endpoint (``evo_agents.hub.server.mcp``): its SDK app is mounted,
so the lifespan runs its session manager. The only websockets are the two ends of a run's web terminal
(``evo_agents.hub.server.terminal``) and of the live view of its browser (``evo_agents.hub.server.browser.service.view``),
which check their own credential. ``app.state.sealer`` seals the members' secrets
(``evo_agents.hub.server.credentials.models.sealing``); it is None without EVO_HUB_SECRETS_KEY, and the routes that write secrets answer
503. As it starts, the hub records the core plugins of its image that projects enable, and turns off an installed plugin
that no longer runs on its release and tells the hub admins (``evo_agents.hub.server.plugins.service.store``). The routes of
plugins are included when the app is made (``evo_agents.hub.server.plugins.service.extension``): the core plugins' own, Telegram's
among them, then the proxy to isolated plugins' backends under /v1/x. ``app.state.github_app`` is the hub's GitHub App
(``evo_agents.hub.server.forge.client.github_app``), which makes and revokes the runs' GitHub tokens; the lifespan opens it, None
without EVO_HUB_GITHUB_APP_*, and a private key that does not open stops the start. ``app.state.curator_app`` is the
Curator's own App, evo-agents-curator, the same way with EVO_HUB_CURATOR_APP_*: the runs of the Curator get their tokens
from it alone, and the hub checks rulesets with it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

from evo_agents import __version__
from evo_agents.hub import cache, kg_snapshots, wake
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import holder, make_engine, open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_graph import Handles
from evo_agents.hub.log import redact_dsn, scrub_data
from evo_agents.hub.migrate import migrate
from evo_agents.hub.plugin_calls import PluginHostClient
from evo_agents.hub.server import errors, routers, security
from evo_agents.hub.server.cron.service.fire import (
    cron_pass as cron_pass,  # handed down to the hub worker, as those below
)
from evo_agents.hub.server.forge.client.github import GitHub
from evo_agents.hub.server.forge.client.github_app import GitHubApp
from evo_agents.hub.server.gateway.service import sync as gateway_sync
from evo_agents.hub.server.hub_runtime import take_turn as hub_turn  # noqa: F401
from evo_agents.hub.server.hub_settings import FeatureGate
from evo_agents.hub.server.kg_entity_key import find as entity_key  # noqa: F401

# The hub worker runs the tick of a project's Loop, the release pass and the upkeep of agents' memories, but
# evo_agents.hub.worker imports no server module beyond its job list (docs/structure.md, section Package boundaries):
# the hub worker command takes them from here and hands them down.
from evo_agents.hub.server.loop.tick import tick as loop_tick  # noqa: F401
from evo_agents.hub.server.memories.service.upkeep import upkeep as memory_upkeep  # noqa: F401
from evo_agents.hub.server.plugins.routes.bridge import bridge_gate
from evo_agents.hub.server.releases.service.check import release_pass as release_pass
from evo_agents.hub.server.security import Authenticate
from evo_agents.hub.server.systems.routes import traces as system_trace_views
from evo_agents.kg import pacing
from evo_agents.plugins.signing import default_sigstore

log = logging.getLogger(__name__)

STALL_LOG_SECONDS = 1.0  # the event loop late by this much or more on a tick of ``_watch_loop`` is logged
QUIET_PATHS = frozenset({"/v1/health", "/v1/health/live"})  # polled by probes: access lines at debug level
GZIP_SKIP_PREFIXES = ("/mcp", "/v1/gateway", "/v1/blobs")
GZIP_EXCLUDED_TYPES = (*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/octet-stream")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class ApiGZip:
    """GZip responses of at least 1,000 bytes on the hub's JSON and UI routes, leaving streaming and binary paths untouched."""

    def __init__(self, app):
        self.app = app
        self._gzip = GZipMiddleware(app, minimum_size=1000, compresslevel=6, exclude_content_types=GZIP_EXCLUDED_TYPES)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path or path.startswith(GZIP_SKIP_PREFIXES) or path.endswith("/stream"):
            await self.app(scope, receive, send)
            return
        await self._gzip(scope, receive, send)


class RequestContext:
    """Pure ASGI middleware: an X-Request-ID for every request (the caller's when well formed), echoed in
    the response and in error bodies, and one access log line per request. It names the request's method and path
    as the ``holder`` of the pooled connections it takes (``evo_agents.hub.db.TimedPool``)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = given if _REQUEST_ID.fullmatch(given) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500  # what the client gets if the app fails before starting a response
        held_by = holder.set(f"{scope['method']} {scope['path']}")

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            holder.reset(held_by)
            level = logging.DEBUG if scope["path"] in QUIET_PATHS and status < 500 else logging.INFO
            log.log(
                level,
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "request_id": request_id,
                },
            )


def create_app(config: HubConfig) -> FastAPI:
    """The hub over the Postgres at ``config.dsn``. Nothing connects until the lifespan starts."""
    app = FastAPI(
        title="evo-agents hub",
        version=__version__,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan(config),
        dependencies=[Depends(bridge_gate)],  # a plugin's page, through the web's bridge, gets what it was granted
    )
    app.state.config = config
    app.add_middleware(FeatureGate)  # added first, so it runs after Authenticate and sees the caller
    app.add_middleware(Authenticate)
    from evo_agents.hub.server import mcp

    app.state.mcp = mcp.mount(app, config)  # McpGate runs before Authenticate, which leaves /mcp alone
    app.add_middleware(ApiGZip)
    app.add_middleware(RequestContext)  # added last, so it runs first: refusals get a request id and an access line
    errors.install(app)
    _include(app, routers.EARLY)
    _run_state(app, config)
    from evo_agents.hub.server.plugins.routes import extension as plugin_routes

    # The routes of the core plugins (Telegram's, at the paths it had), then those of isolated plugins under /v1/x.
    app.state.core_plugins = plugin_routes.include(app)

    _include(app, routers.LATE)
    _websocket_state(app)
    from evo_agents.hub.server.credentials.models.sealing import Sealer

    app.state.sealer = Sealer.from_config(config)  # None without EVO_HUB_SECRETS_KEY: the secret routes answer 503
    # The release plugins are checked against (a test starts a hub of another one), the checker of the Sigstore
    # signatures of their bundles, and the client to the plugin host that runs the backends of isolated plugins.
    app.state.hub_version, app.state.plugin_sigstore, app.state.plugin_host = (
        __version__,
        default_sigstore(),
        PluginHostClient(config.plugin_host_url, config.plugin_host_token),
    )
    return app


def _lifespan(config: HubConfig):
    """The lifespan of the app: it opens what the hub serves with, runs the MCP session manager, and closes it all."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await _open(app, config)
        try:
            async with app.state.mcp.session_manager.run():  # a mounted app's own lifespan does not run
                yield
        finally:
            await _close(app, pool)

    return lifespan


async def _open(app: FastAPI, config: HubConfig):
    """Migrate, open the pool, the engine and the clients on ``app.state``, and return the pool; a failure stops the
    start."""
    target = redact_dsn(config.dsn)
    log.info(
        "hub starting",
        extra={
            "version": __version__,
            "db": target,
            "data_dir": str(config.data_dir),
            "pool": {"min": config.pool_min_size, "max": config.pool_max_size, "timeout_s": config.pool_timeout},
            "heartbeat_pool": {"max": config.heartbeat_pool_size},
        },
    )
    github_app = curator_app = pool = None
    try:
        github_app = GitHubApp.from_config(config)  # a private key that does not open stops the start
        curator_app = GitHubApp.from_config(config, curator=True)
        config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = await asyncio.to_thread(migrate, config.dsn)
        pool = await open_pool(config)
        heartbeat_pool = await open_pool(config, heartbeat=True)
    except Exception as exc:
        log.error("hub cannot start", extra={"db": target, "error": f"{type(exc).__name__}: {exc}"})
        if pool is not None:
            await pool.close()
        for opened in (github_app, curator_app):
            if opened is not None:
                await opened.aclose()
        raise
    app.state.pool, app.state.engine = pool, make_engine(pool)  # the engine is disposed of before the pool closes
    # the workers' heartbeats and claims take their connections from a pool of their own (``db.HEARTBEAT_PATHS``)
    app.state.heartbeat_pool, app.state.heartbeat_engine = heartbeat_pool, make_engine(heartbeat_pool)
    app.state.github = GitHub(config)
    app.state.github_app = github_app  # None without EVO_HUB_GITHUB_APP_*: no GitHub token is leased
    app.state.curator_app = curator_app  # None without EVO_HUB_CURATOR_APP_*: no token for a run of the Curator
    app.state.blobs = BlobStore.from_config(config)
    app.state.jobs = await JobQueue.open(pool)
    app.state.kg_handles = Handles()  # kg_more continuations, per user and project
    app.state.kg_snapshots.engine = app.state.engine
    kg_snapshots.serving = app.state.kg_snapshots  # what a core plugin's graph read uses (kg_snapshots.held_for)
    app.state.kg_snapshots.warm()  # the project searched most, so the first kg call after a deploy finds it held
    pacing.free_aside(
        kg_snapshots.requests_waiting
    )  # a snapshot replaced or dropped is freed in a thread, a part at a time, not on the loop
    if app.state.blobs is None:
        log.warning(
            "blob store not configured: the blob routes answer 503",
            extra={"missing": config.blob_store_missing()},
        )
    await _check_plugins(app, config)
    await security.bind_admins(app.state.engine, config.admins)  # EVO_HUB_ADMINS to the accounts that hold them
    app.state.gateway_run = await gateway_sync.start(app, config)  # off without EVO_HUB_GATEWAY_SECRET
    app.state.loop_watch = asyncio.get_running_loop().create_task(_watch_loop(), name="loop-watch")
    log.info(
        "hub ready",
        extra={
            "schema": ",".join(result.after),
            "applied": list(result.applied),
            "admins": len(config.admins),
            "device_login": bool(config.github_client_id),
            "web_login_missing": config.web_login_missing(),
            "blob_bucket": config.s3_bucket,
            "credentials_missing": config.credentials_missing(),
            "github_app_missing": config.github_app_missing(),
            "curator_app_missing": config.curator_app_missing(),
            "telegram_missing": config.telegram_missing(),
            "plugin_publishers": [publisher.id for publisher in config.plugin_publishers],
            "plugin_host_missing": config.plugin_host_missing(),
            "gateway_missing": config.gateway_missing(),
        },
    )
    return pool


async def _watch_loop(tick: float = 0.5) -> None:
    """Log ``event loop stalled`` when a sleep of ``tick`` ends STALL_LOG_SECONDS or more late: the loop ran something
    that did not give it back, and every request, the pool's waiters among them, waited meanwhile."""
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(tick)
        late = loop.time() - started - tick
        if late >= STALL_LOG_SECONDS:
            log.warning("event loop stalled", extra={"stalled_ms": round(late * 1000, 1)})


async def _close(app: FastAPI, pool) -> None:
    """Close what ``_open`` opened, the engine before the pool."""
    app.state.loop_watch.cancel()
    await app.state.terminals.close_all()
    await app.state.browser_views.close_all()
    await app.state.listener.close()
    await app.state.kg_snapshots.close()
    await app.state.cache.close()
    await wake.close()
    await app.state.github.aclose()
    for opened in (app.state.gateway_run, app.state.github_app, app.state.curator_app, app.state.plugin_host):
        if opened is not None:
            await opened.aclose()
    if app.state.blobs is not None:
        app.state.blobs.close()
    await app.state.engine.dispose()
    await app.state.heartbeat_engine.dispose()
    await app.state.heartbeat_pool.close()
    await pool.close()
    log.info("hub stopped, connection pool closed")


def _include(app: FastAPI, groups) -> None:
    """Include the routers of each group of ``routers``, in its order."""
    for group in groups:
        for router in group():
            app.include_router(router)


def _run_state(app: FastAPI, config: HubConfig) -> None:
    """The listener of the process and what waits on it: the claims of runs, the event streams, the snapshots of
    graphs, the cache, and the limits on pairing codes."""
    from evo_agents.hub.server import listen, run_events, runs
    from evo_agents.hub.server.chats.service.events import ChatStreams
    from evo_agents.hub.server.workers.service.codes import RefusalLimit

    # the process's one LISTEN, or queue on the event bus, opened by the first claim or stream
    app.state.listener = listen.Listener(config.dsn, config.broker_url)
    app.state.run_wakeups = runs.RunWakeups(app.state.listener)  # the claims waiting for a queued run
    # the event streams of runs, and of group chats
    app.state.run_streams, app.state.chat_streams = (
        run_events.RunStreams(app.state.listener),
        ChatStreams(app.state.listener),
    )
    app.state.trace_streams = system_trace_views.TraceStreams(app.state.listener)  # the live traces of systems
    # the in-RAM graphs of the projects, swapped on each build's notice on kg_graph
    app.state.kg_snapshots = kg_snapshots.SnapshotCoordinator(listener=app.state.listener)
    # the cache, on Redis with EVO_HUB_REDIS_URL, else this process's; dropped by the listener's wake-ups
    app.state.cache = cache.Cache(config.redis_url, listener=app.state.listener)
    app.state.join_refusals = RefusalLimit()  # refused pairing codes per client address, this process only
    app.state.join_limit = cache.Window(app.state.cache, "join", app.state.join_refusals)  # ...or shared on Redis


def _websocket_state(app: FastAPI) -> None:
    """The registry of what each websocket has open in this process."""
    from evo_agents.hub.server import terminal
    from evo_agents.hub.server.browser.service.view import BrowserViews

    app.state.terminals = terminal.Terminals()  # the web terminals open in this process, one per run
    app.state.browser_views = BrowserViews()  # the live views of runs' browsers, one channel per run


async def _check_plugins(app: FastAPI, config: HubConfig) -> None:
    """Record the core plugins of the image in the store, then turn off the installed plugins that do not run on this
    release (``plugin_store``); a failure is logged and the hub serves on."""
    from evo_agents.hub.server.plugins.service.startup import disable_incompatible, register_core_plugins

    try:
        await register_core_plugins(app.state.engine, app.state.core_plugins.plugins)
        await disable_incompatible(app.state.engine, config.admins, app.state.hub_version)
    except Exception:
        log.exception("cannot check the installed plugins against this release; the hub serves on")


# What a Sentry event's request may not carry: the body (a pairing code, a GitHub token) and the query string (the
# OAuth code and state of a web sign-in, whose first parameter has no "?" before it for the log filter to see).
REQUEST_LEFT_OUT = ("data", "query_string")


def _scrub_event(event, hint):
    request = event.get("request") if isinstance(event, dict) else None
    if isinstance(request, dict):
        for key in REQUEST_LEFT_OUT:
            request.pop(key, None)
    return scrub_data(event)


def init_sentry(config: HubConfig) -> bool:
    """Report errors to Sentry when EVO_HUB_SENTRY_DSN is set; otherwise do nothing. Events carry no PII, no
    local variables (a frame can hold the DSN), no request body or query string, and pass through the same secret
    filter as the logs."""
    if not config.sentry_dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=config.sentry_dsn,
        release=f"evo-agents@{__version__}",
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=_scrub_event,
        before_breadcrumb=_scrub_event,
    )
    log.info("sentry enabled")
    return True

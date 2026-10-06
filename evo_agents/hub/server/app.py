"""The hub's FastAPI application.

The lifespan migrates the database (under the advisory lock, see ``migrate``), opens the connection pool, the
GitHub client, the blob store client and the job queue the api defers to, and closes them on shutdown; a database
that cannot be reached or migrated stops the start instead of serving errors. An R2 outage does not: health
reports it and the blob routes answer 503. Every request gets an id and one access log line without its query
string, which can carry OAuth codes. Every path under /v1 needs a credential except the few
``security.PUBLIC_PATHS`` lists. The OpenAPI document is served at /v1/openapi.json for the web client's generated
types. /mcp is the MCP endpoint (``evo_agents.hub.server.mcp``): its SDK app is mounted, so the lifespan runs its
session manager. The only websockets are the two ends of a run's web terminal (``evo_agents.hub.server.terminal``),
which check their own credential. ``app.state.sealer`` seals the members' secrets (``evo_agents.hub.server.sealing``);
it is None without EVO_HUB_SECRETS_KEY, and the routes that write secrets answer 503. ``app.state.github_app`` is the
hub's GitHub App (``evo_agents.hub.server.github_app``), which makes and revokes the runs' GitHub tokens; the lifespan
opens it, None without EVO_HUB_GITHUB_APP_*, and a private key that does not open stops the start.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders

from evo_agents import __version__
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_graph import GraphCache, Handles
from evo_agents.hub.log import redact_dsn, scrub_data
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import admin, auth, blobs, errors, health, projects, tokens, web_auth
from evo_agents.hub.server.github import GitHub
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.security import Authenticate

log = logging.getLogger(__name__)

QUIET_PATHS = frozenset({"/v1/health", "/v1/health/live"})  # polled by probes: access lines at debug level
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


class RequestContext:
    """Pure ASGI middleware: an X-Request-ID for every request (the caller's when well formed), echoed in
    the response and in error bodies, and one access log line per request."""

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

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        target = redact_dsn(config.dsn)
        log.info(
            "hub starting",
            extra={
                "version": __version__,
                "db": target,
                "data_dir": str(config.data_dir),
                "pool": {"min": config.pool_min_size, "max": config.pool_max_size, "timeout_s": config.pool_timeout},
            },
        )
        github_app = None
        try:
            github_app = GitHubApp.from_config(config)  # a private key that does not open stops the start
            config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = await asyncio.to_thread(migrate, config.dsn)
            pool = await open_pool(config)
        except Exception as exc:
            log.error("hub cannot start", extra={"db": target, "error": f"{type(exc).__name__}: {exc}"})
            if github_app is not None:
                await github_app.aclose()
            raise
        app.state.pool = pool
        app.state.github = GitHub(config)
        app.state.github_app = github_app  # None without EVO_HUB_GITHUB_APP_*: no GitHub token is leased
        app.state.blobs = BlobStore.from_config(config)
        app.state.jobs = await JobQueue.open(pool)
        app.state.kg_graphs = GraphCache(config.data_dir / "kg" / "graphs")  # built graphs, fetched by sha256
        app.state.kg_handles = Handles()  # kg_more continuations, per user and project
        if app.state.blobs is None:
            log.warning(
                "blob store not configured: the blob routes answer 503",
                extra={"missing": config.blob_store_missing()},
            )
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
            },
        )
        try:
            async with app.state.mcp.session_manager.run():  # a mounted app's own lifespan does not run
                yield
        finally:
            await app.state.terminals.close_all()
            await app.state.listener.close()
            await app.state.github.aclose()
            if app.state.github_app is not None:
                await app.state.github_app.aclose()
            if app.state.blobs is not None:
                app.state.blobs.close()
            await pool.close()
            log.info("hub stopped, connection pool closed")

    app = FastAPI(
        title="evo-agents hub",
        version=__version__,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.add_middleware(Authenticate)
    from evo_agents.hub.server import mcp

    app.state.mcp = mcp.mount(app, config)  # McpGate runs before Authenticate, which leaves /mcp alone
    app.add_middleware(RequestContext)  # added last, so it runs first: refusals get a request id and an access line
    errors.install(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(web_auth.router)
    app.include_router(tokens.router)
    app.include_router(admin.router)
    app.include_router(projects.router)
    app.include_router(blobs.router)

    from evo_agents.hub.server import plans

    app.include_router(plans.router)

    from evo_agents.hub.server import memories

    app.include_router(memories.router)

    from evo_agents.hub.server import skills

    app.include_router(skills.router)

    from evo_agents.hub.server import kg

    app.include_router(kg.router)

    from evo_agents.hub.server import kg_web

    app.include_router(kg_web.router)

    from evo_agents.hub.server import admin_console

    app.include_router(admin_console.router)

    from evo_agents.hub.server import workers

    app.include_router(workers.router)
    app.include_router(workers.worker_router)
    app.state.join_refusals = workers.RefusalLimit()  # refused pairing codes per client address, this process only

    from evo_agents.hub.server import listen, run_events, runs

    app.include_router(runs.router)
    app.include_router(runs.worker_router)
    app.include_router(run_events.router)
    app.include_router(run_events.stream_router)
    app.include_router(run_events.worker_router)
    app.state.listener = listen.Listener(config.dsn)  # the process's one LISTEN, opened by the first claim or stream
    app.state.run_wakeups = runs.RunWakeups(app.state.listener)  # the claims waiting for a queued run
    app.state.run_streams = run_events.RunStreams(app.state.listener)  # the event streams of runs

    from evo_agents.hub.server import decisions, notifications

    app.include_router(decisions.router)
    app.include_router(decisions.worker_router)
    app.include_router(notifications.router)
    app.include_router(notifications.worker_router)

    from evo_agents.hub.server import terminal

    app.include_router(terminal.router)
    app.include_router(terminal.worker_router)
    app.state.terminals = terminal.Terminals()  # the web terminals open in this process, one per run

    from evo_agents.hub.server import credentials, secrets
    from evo_agents.hub.server.sealing import Sealer

    app.include_router(secrets.router)
    app.include_router(credentials.worker_router)
    app.state.sealer = Sealer.from_config(config)  # None without EVO_HUB_SECRETS_KEY: the secret routes answer 503
    return app


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

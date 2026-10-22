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
(``evo_agents.hub.server.terminal``), which check their own credential. ``app.state.sealer`` seals the members' secrets
(``evo_agents.hub.server.sealing``); it is None without EVO_HUB_SECRETS_KEY, and the routes that write secrets answer
503. As it starts, the hub records the core plugins of its image that projects enable, and turns off an installed plugin
that no longer runs on its release and tells the hub admins (``evo_agents.hub.server.plugin_store``). The routes of
plugins are included when the app is made (``evo_agents.hub.server.plugin_routes``): the core plugins' own, Telegram's
among them, then the proxy to isolated plugins' backends under /v1/x. ``app.state.github_app`` is the hub's GitHub App
(``evo_agents.hub.server.github_app``), which makes and revokes the runs' GitHub tokens; the lifespan opens it, None
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

from evo_agents import __version__
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import make_engine, open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_graph import GraphCache, Handles
from evo_agents.hub.log import redact_dsn, scrub_data
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import admin, auth, blobs, errors, health, projects, tokens, user_emails, web_auth
from evo_agents.hub.server.github import GitHub
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.hub_settings import FeatureGate
from evo_agents.hub.server.loop import routes as loop_routes  # GET .../loop, POST .../loop/resume

# The hub worker runs the tick of a project's Loop, the release pass and the upkeep of agents' memories, but
# evo_agents.hub.worker imports no server module beyond its job list (docs/structure.md, section Package boundaries):
# the hub worker command takes them from here and hands them down.
from evo_agents.hub.server.loop.tick import tick as loop_tick  # noqa: F401
from evo_agents.hub.server.memory_upkeep import router as restore_routes
from evo_agents.hub.server.memory_upkeep import upkeep as memory_upkeep  # noqa: F401
from evo_agents.hub.server.plugin_bridge import bridge_gate
from evo_agents.hub.server.plugin_host import PluginHostClient
from evo_agents.hub.server.release_check import release_pass as release_pass
from evo_agents.hub.server.security import Authenticate
from evo_agents.plugins.signing import default_sigstore

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
        github_app = curator_app = None
        try:
            github_app = GitHubApp.from_config(config)  # a private key that does not open stops the start
            curator_app = GitHubApp.from_config(config, curator=True)
            config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = await asyncio.to_thread(migrate, config.dsn)
            pool = await open_pool(config)
        except Exception as exc:
            log.error("hub cannot start", extra={"db": target, "error": f"{type(exc).__name__}: {exc}"})
            for opened in (github_app, curator_app):
                if opened is not None:
                    await opened.aclose()
            raise
        app.state.pool = pool
        app.state.engine = make_engine(pool)  # disposed of before the pool closes
        app.state.github = GitHub(config)
        app.state.github_app = github_app  # None without EVO_HUB_GITHUB_APP_*: no GitHub token is leased
        app.state.curator_app = curator_app  # None without EVO_HUB_CURATOR_APP_*: no token for a run of the Curator
        app.state.blobs = BlobStore.from_config(config)
        app.state.jobs = await JobQueue.open(pool)
        app.state.kg_graphs = GraphCache(config.data_dir / "kg" / "graphs")  # built graphs, fetched by sha256
        app.state.kg_handles = Handles()  # kg_more continuations, per user and project
        if app.state.blobs is None:
            log.warning(
                "blob store not configured: the blob routes answer 503",
                extra={"missing": config.blob_store_missing()},
            )
        await _check_plugins(app, config)
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
            },
        )
        try:
            async with app.state.mcp.session_manager.run():  # a mounted app's own lifespan does not run
                yield
        finally:
            await app.state.terminals.close_all()
            await app.state.listener.close()
            await app.state.github.aclose()
            for opened in (app.state.github_app, app.state.curator_app, app.state.plugin_host):
                if opened is not None:
                    await opened.aclose()
            if app.state.blobs is not None:
                app.state.blobs.close()
            await app.state.engine.dispose()
            await pool.close()
            log.info("hub stopped, connection pool closed")

    app = FastAPI(
        title="evo-agents hub",
        version=__version__,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        dependencies=[Depends(bridge_gate)],  # a plugin's page, through the web's bridge, gets what it was granted
    )
    app.state.config = config
    app.add_middleware(FeatureGate)  # added first, so it runs after Authenticate and sees the caller
    app.add_middleware(Authenticate)
    from evo_agents.hub.server import mcp

    app.state.mcp = mcp.mount(app, config)  # McpGate runs before Authenticate, which leaves /mcp alone
    app.add_middleware(RequestContext)  # added last, so it runs first: refusals get a request id and an access line
    errors.install(app)
    for routes in (
        health.router,
        auth.router,
        web_auth.router,
        tokens.router,
        user_emails.router,
        admin.router,
        projects.router,
        blobs.router,
        restore_routes,  # POST /v1/memories/{id}/restore
    ):
        app.include_router(routes)

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

    from evo_agents.hub.server import author_chat, chat_events, listen, run_events, runs, tool_stats

    app.include_router(runs.router)
    app.include_router(runs.worker_router)
    app.include_router(run_events.router)
    app.include_router(run_events.stream_router)
    app.include_router(run_events.worker_router)
    app.include_router(author_chat.router)
    app.include_router(author_chat.worker_router)
    app.include_router(tool_stats.router)
    app.state.listener = listen.Listener(config.dsn)  # the process's one LISTEN, opened by the first claim or stream
    app.state.run_wakeups = runs.RunWakeups(app.state.listener)  # the claims waiting for a queued run
    # the event streams of runs, and of group chats
    app.state.run_streams, app.state.chat_streams = (
        run_events.RunStreams(app.state.listener),
        chat_events.ChatStreams(app.state.listener),
    )

    from evo_agents.hub.server import (
        attachments,
        curator,
        decisions,
        digests,
        kg_worker,
        local_runs,
        notifications,
        output_content,
        output_notices,
        overview,
        proposals,
        release_claims,
        releases,
        run_outputs,
    )

    for routes in (
        kg_worker.worker_router,  # the graph pushes of the worker on duty for a night shift
        decisions.router,
        decisions.worker_router,
        local_runs.router,
        local_runs.batch_router,
        notifications.router,
        notifications.worker_router,
        output_notices.router,  # the notices a member turned off
        overview.router,
        curator.router,
        proposals.router,
        proposals.worker_router,
        releases.router,  # the daily release of a project (evo_agents.hub.releases)
        release_claims.worker_router,  # the branch a release run pushed (evo_agents.hub.release_runs)
        run_outputs.router,  # the outputs of runs (evo_agents.hub.outputs) and a project's switch of tool images
        run_outputs.worker_router,
        output_content.router,  # their bytes
        output_content.chat_router,
        attachments.router,  # the files people attach to messages (evo_agents.hub.attachments)
        attachments.chat_router,
        attachments.worker_router,
    ):
        app.include_router(routes)

    from evo_agents.hub.server import (
        changes,
        earlier,
        history,
        history_search,
        kg_health,
        ledger,
        org,
        playbook,
        proposal_plans,
        protection,
        replays,
        transcripts,
    )

    for routes in (
        changes.router,
        protection.router,
        proposal_plans.router,
        changes.worker_router,
        ledger.router,
        digests.router,
        transcripts.router,
        kg_health.router,
        replays.router,
        replays.worker_router,
        history.router,
        history.worker_router,
        history_search.router,
        earlier.router,
        playbook.router,
        playbook.worker_router,
        org.router,  # the org chart (plan org-chart)
    ):
        app.include_router(routes)

    from evo_agents.hub.server import plugin_routes

    # The routes of the core plugins (Telegram's, at the paths it had), then those of isolated plugins under /v1/x.
    app.state.core_plugins = plugin_routes.include(app)

    from evo_agents.hub.server import terminal

    app.include_router(terminal.router)
    app.include_router(terminal.worker_router)
    app.state.terminals = terminal.Terminals()  # the web terminals open in this process, one per run

    from evo_agents.hub.server import (
        account_views,
        accounts,
        agent_account_views,
        agent_accounts,
        agent_packs,
        agent_profiles,
        agent_types,
        autonomy_policies,
        branding,
        chat_agents,
        chat_direct,
        chat_images,
        chat_members,
        chat_people,
        chat_read,
        chats,
        credentials,
        features,
        goals,
        merge_judge,
        merge_view,
        plugin_host,
        plugin_notify,
        plugin_ui,
        plugins,
        project_plugins,
        rate_limits,
        run_leases,
        secrets,
        signal_keys,
        signals,
        task_dispatch,
        triage_actions,
    )
    from evo_agents.hub.server.sealing import Sealer

    for routes in (
        secrets.router,
        run_leases.router,
        credentials.worker_router,
        accounts.router,
        account_views.router,
        rate_limits.worker_router,
        agent_profiles.router,
        agent_accounts.router,
        agent_account_views.router,
        agent_packs.router,
        agent_types.router,
        task_dispatch.router,
        plugin_notify.router,
        task_dispatch.worker_router,
        chat_people.router,  # before chats.router, whose GET /{chat_id} would take /people
        chats.router,
        chat_direct.router,
        chat_members.router,
        chat_images.router,
        chat_events.router,
        chat_events.stream_router,
        chat_agents.worker_router,
        chat_read.worker_router,
        triage_actions.worker_router,
        plugins.router,
        plugins.publishers_router,
        project_plugins.router,
        plugin_ui.router,
        plugin_host.router,
        branding.router,
        branding.admin_router,
        features.router,
        features.admin_router,
        signals.push_router,  # a project's signals: an ingest key's batches, and the members' routes
        signals.router,
        signal_keys.router,
        autonomy_policies.router,
        goals.router,
        loop_routes.router,
        merge_view.router,
        merge_judge.worker_router,
    ):
        app.include_router(routes)
    app.state.sealer = Sealer.from_config(config)  # None without EVO_HUB_SECRETS_KEY: the secret routes answer 503
    # The release plugins are checked against (a test starts a hub of another one), the checker of the Sigstore
    # signatures of their bundles, and the client to the plugin host that runs the backends of isolated plugins.
    app.state.hub_version, app.state.plugin_sigstore, app.state.plugin_host = (
        __version__,
        default_sigstore(),
        PluginHostClient(config.plugin_host_url, config.plugin_host_token),
    )
    return app


async def _check_plugins(app: FastAPI, config: HubConfig) -> None:
    """Record the core plugins of the image in the store, then turn off the installed plugins that do not run on this
    release (``plugin_store``); a failure is logged and the hub serves on."""
    from evo_agents.hub.server.plugin_store import disable_incompatible, register_core_plugins

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

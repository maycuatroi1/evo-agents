"""The gate of the hub's /mcp (``evo_agents.hub.server.mcp``): ``McpGate`` takes every request for /mcp before the
router does:


- /mcp and /mcp/ are the one endpoint, and /mcp is answered in place: the mount alone would redirect it to /mcp/
  with a 307. Any other path under /mcp is 404, and any method but POST 405;
- the Host must be the host of EVO_HUB_PUBLIC_URL (of --host and --port too, unless the server listens on every
  interface) or a loopback name, else 421; an Origin, when one is sent, must be the public URL's or a loopback one,
  else 403. This is the DNS rebinding check of the SDK, which only takes localhost by default and checks the same
  lists again;
- the caller needs a live machine token as ``Authorization: Bearer``, else 401: the web session cookie is not taken;
- or the live token of a worker with X-Evo-Run, the id of a run that worker holds now (``runs.HELD_STATES``, waiting
  among them) and whose owner still holds a grant on its project; else 403, as for a worker token without the header.
  The caller is then the agent of that run: its owner, the member who dispatched it (on a shared worker another member
  than the worker's), with a ``RunScope`` (``security``), the run's project alone, the owner's grant there capped at
  writer, and never a hub admin, so ``projects.project_access`` holds every tool to
  it. Once the run leaves the held states, its id opens nothing. The agent of a judge run gets the code graph's tools
  and hub_projects alone (JUDGE_TOOLS): never a run's events, a plan, a memory or a digest, which the Builder it judges
  wrote or could have written for it; the agent of a replay run gets none of the tools (REPLAY_TOOLS), which hold the
  project as it is now. ``tools/list`` shows a run's agent only what it may call (``mcp_tools.offered``): a judge run
  JUDGE_TOOLS, a replay run none, any other run all but ask_member. X-Evo-Run with a machine token is 400;
- X-Evo-Project names the session's project and X-Evo-Sink its sink (default claude-code@anthropic); a malformed one is
  400. The session of a run's agent is the run's project: X-Evo-Project naming another is 403.

Every refusal is the API's JSON error with the request id. Only then does the SDK see the request, with the caller in
``request.state``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import psycopg
from fastapi import HTTPException
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, TransportSecuritySettings
from sqlalchemy import exc as sa_exc
from starlette.requests import Request

from evo_agents.hub.config import HubConfig
from evo_agents.hub.mcp_tools import LOCAL_RUN_HEADER, PROJECT_HEADER, RUN_HEADER, SINK_HEADER
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import error_response
from evo_agents.hub.server.mcp_run import MOUNT, run_principal
from evo_agents.hub.server.security import (
    JOIN_HINT,
    LOGIN_HINT,
    MACHINE,
    PREFIXES,
    WORKER,
    WWW_AUTHENTICATE,
    authenticate,
)

log = logging.getLogger(__name__)

ENDPOINT = MOUNT + "/"  # the path the mounted app answers at
MAX_BODY = DEFAULT_MAX_REQUEST_BODY_SIZE  # 4 MiB: a memory escaped as JSON fits several times over
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")
EVERY_INTERFACE = ("0.0.0.0", "::", "")
SESSION = "evo_mcp"  # the key of McpSession in request.state
_PROJECT = re.compile(PROJECT_NAME)
_SINK = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
_RUN = re.compile(r"[1-9][0-9]{0,17}")  # a run's id, within bigint
RUN_ONLY = (
    f"a worker token opens {MOUNT} only for the agent of a run its worker holds, named in {RUN_HEADER}: "
    "`evo-agents hub mcp` sends it inside a run"
)


@dataclass(frozen=True)
class McpSession:
    """What the request says about the session: its project (None without X-Evo-Project) and its sink."""

    project: str | None
    sink: str
    local_run: int | None = None  # X-Evo-Local-Run: the local run of the session, for plan_step


def transport_security(config: HubConfig) -> TransportSecuritySettings:
    """The hosts and origins /mcp answers: loopback, the public URL, and the address the server listens on."""
    hosts, origins = [], []
    for name in LOOPBACK:
        hosts += [name, f"{name}:*"]
        origins += [f"{scheme}://{name}{port}" for scheme in ("http", "https") for port in ("", ":*")]
    if config.public_url:
        parts = urlsplit(config.public_url)
        hostname = parts.hostname or ""
        name = f"[{hostname}]" if ":" in hostname else hostname
        default = 443 if parts.scheme == "https" else 80
        hosts += [name if parts.port is None else f"{name}:{parts.port}", f"{name}:{parts.port or default}"]
        origins.append(f"{parts.scheme}://{name}" + ("" if parts.port is None else f":{parts.port}"))
    if config.host not in EVERY_INTERFACE:
        name = f"[{config.host}]" if ":" in config.host else config.host
        hosts += [name, f"{name}:{config.port}"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=list(dict.fromkeys(origins)),
    )


def allowed(value: str, patterns: list[str]) -> bool:
    """``value`` matches one of ``patterns``: exactly, or ``base:*`` with any port, as the SDK matches them."""
    return any(value == pattern or (pattern.endswith(":*") and value.startswith(pattern[:-1])) for pattern in patterns)


class McpGate:
    """Pure ASGI middleware in front of /mcp (see the module): it refuses what must not reach the SDK, and hands
    the rest on at the mounted endpoint with the caller and the session in ``request.state``."""

    def __init__(self, app, security: TransportSecuritySettings):
        self.app = app
        self.security = security

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == MOUNT or path.startswith(ENDPOINT)):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        refusal = await self._check(request)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        # A copy: the access line of RequestContext keeps the path as sent. The state is shared, so is the caller.
        await self.app({**scope, "path": ENDPOINT, "raw_path": ENDPOINT.encode()}, receive, send)

    async def _check(self, request: Request):
        host = request.headers.get("host", "")
        if not allowed(host, self.security.allowed_hosts):
            log.warning("mcp request refused: host not allowed", extra={"host": host[:255]})
            return error_response(
                request, 421, "this hub does not answer MCP for that host: use the URL in EVO_HUB_PUBLIC_URL"
            )
        origin = request.headers.get("origin")
        if origin is not None and not allowed(origin, self.security.allowed_origins):
            log.warning("mcp request refused: origin not allowed", extra={"origin": origin[:255]})
            return error_response(request, 403, "MCP is not answered for pages of another origin")
        if request.scope["path"] not in (MOUNT, ENDPOINT):
            return error_response(request, 404, f"MCP is answered at {MOUNT} only")
        if request.method != "POST":
            return error_response(
                request,
                405,
                f"{MOUNT} takes POST: every message gets its answer as JSON, and no stream is kept open",
                headers={"Allow": "POST"},
            )
        refusal = await self._authenticate(request)
        if refusal is not None:
            return refusal
        project = request.headers.get(PROJECT_HEADER) or None
        if project is not None and not _PROJECT.fullmatch(project):
            return error_response(request, 400, f"{PROJECT_HEADER} must be the name of a project on the hub")
        scope = request.state.principal.scope
        if scope is not None:  # the agent of a run: its session is the run's project
            if project is not None and project != scope.project:
                message = (
                    f"run {scope.run_id} is of project {scope.project}: its agent cannot open a session of {project}"
                )
                return error_response(request, 403, message)
            project = scope.project
        sink = request.headers.get(SINK_HEADER) or AGENT_SINK
        if not _SINK.fullmatch(sink):
            message = f"{SINK_HEADER} must name a sink: printable text, 100 characters at most"
            return error_response(request, 400, message)
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
            return error_response(request, 413, f"an MCP message is at most {MAX_BODY // (1024 * 1024)} MiB")
        local = request.headers.get(
            LOCAL_RUN_HEADER
        )  # a value that is no run id is no run: the step is written as ever
        local_run = int(local) if local is not None and _RUN.fullmatch(local) and scope is None else None
        setattr(request.state, SESSION, McpSession(project, sink, local_run))
        return None

    @staticmethod
    async def _authenticate(request: Request):
        authorization = request.headers.get("authorization")
        if authorization is None:
            return _unauthorized(request, f"MCP needs a machine token as `Authorization: Bearer <token>`: {LOGIN_HINT}")
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return _unauthorized(request, f"the Authorization header must be `Bearer <token>`: {LOGIN_HINT}")
        token = token.strip()
        kind = WORKER if token.startswith(PREFIXES[WORKER]) else MACHINE
        run = request.headers.get(RUN_HEADER)
        # Decided by the kind of token and the header alone, before the database.
        if kind == WORKER and run is None:
            return error_response(request, 403, RUN_ONLY)
        if kind != WORKER and run is not None:
            return error_response(request, 400, f"{RUN_HEADER} goes with a worker token: a machine token is no run's")
        if run is not None and not _RUN.fullmatch(run):
            return error_response(request, 400, f"{RUN_HEADER} must be the id of a run")
        run_id = None if run is None else int(run)  # given with a worker token alone
        config, engine = request.app.state.config, request.app.state.engine
        try:
            principal = await authenticate(engine, token, kind, config)
            if principal is not None and run_id is not None:
                async with engine.begin() as conn:
                    principal = await run_principal(conn, principal, run_id)
        except (psycopg.OperationalError, sa_exc.OperationalError) as exc:  # PoolTimeout is one; bugs stay 500s
            log.warning("cannot check a credential: database unavailable", extra={"error": type(exc).__name__})
            return error_response(request, 503, "the hub database is unavailable; try again shortly")
        except HTTPException as exc:  # a run its worker does not hold
            log.warning("mcp request refused: run not held", extra={"run_id": run_id, "reason": http_detail(exc)})
            return error_response(request, exc.status_code, http_detail(exc))
        if principal is None:
            hint = JOIN_HINT if kind == WORKER else LOGIN_HINT
            what = "worker token" if kind == WORKER else "token"
            return _unauthorized(request, f"the {what} is revoked, expired or unknown: {hint}")
        request.state.principal = principal
        return None


def _unauthorized(request: Request, message: str):
    return error_response(request, 401, message, headers=WWW_AUTHENTICATE)


def http_detail(exc: HTTPException) -> str:
    """The message of a refusal of the hub's routes."""
    return exc.detail if isinstance(exc.detail, str) else f"the hub refused the call (HTTP {exc.status_code})"

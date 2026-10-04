"""MCP on the hub: POST /mcp answers MCP's Streamable HTTP transport with the official SDK (``mcp``), for the stdio
proxy every runtime starts, ``evo-agents hub mcp`` (``evo_agents.hub.mcp_proxy``).

The SDK's app (``MCPServer.streamable_http_app``) is mounted at /mcp, stateless and with JSON responses: each POST
carries one JSON-RPC message and gets its answer as application/json, never an SSE stream, so no session lives in the
process. The lifespan of a mounted app does not run, so the hub's lifespan enters the SDK's session manager.
``McpGate`` takes every request for /mcp before the router does:

- /mcp and /mcp/ are the one endpoint, and /mcp is answered in place: the mount alone would redirect it to /mcp/
  with a 307. Any other path under /mcp is 404, and any method but POST 405;
- the Host must be the host of EVO_HUB_PUBLIC_URL (of --host and --port too, unless the server listens on every
  interface) or a loopback name, else 421; an Origin, when one is sent, must be the public URL's or a loopback one,
  else 403. This is the DNS rebinding check of the SDK, which only takes localhost by default and checks the same
  lists again;
- the caller needs a live machine token as ``Authorization: Bearer``, else 401: the web session cookie is not taken;
- X-Evo-Project names the session's project and X-Evo-Sink its sink (default claude-code@anthropic); a malformed one is
  400.

Every refusal is the API's JSON error with the request id. Only then does the SDK see the request, with the caller in
``request.state``.

The 15 tools are ``evo_agents.hub.mcp_tools.TOOLS``. The kg_* tools are those of ``kg serve``, names, schemas,
arguments and results, answered by ``evo_agents.hub.server.kg.tool_result`` as the REST route answers them; kg_more
continues any cut result of the same user and session project, with or without a graph. The others call the hub's
routes for memories, plans, skills and projects in this process, so they read and write under the same rules: the read
rule of ``evo_agents.hub.access`` through the session's sink, the write rule, revisions, conflicts and audit rows. Their
arguments are checked against their schema first, and their text goes through the envelope of ``kg serve``: over
CAP_CHARS it is cut and kg_more gives the rest. A refusal of the hub becomes a tool error carrying its message, an
unexpected failure one carrying the request id only. The log has one line per tool call with its name, project,
outcome and duration, never its arguments or its result.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import psycopg
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, TransportSecuritySettings
from mcp_types import CallToolResult
from mcp_types import Tool as McpTool
from pydantic import ValidationError
from starlette.requests import Request

from evo_agents import __version__
from evo_agents.hub.client import HubError
from evo_agents.hub.config import HubConfig
from evo_agents.hub.mcp_tools import (
    INSTRUCTIONS,
    KG_TOOL_NAMES,
    PROJECT_HEADER,
    SCHEMAS,
    SERVER_NAME,
    SINK_HEADER,
    TOOLS,
)
from evo_agents.hub.memory import AGENT_SINK, FRONTMATTER, HARNESS, frontmatter, memory_type
from evo_agents.hub.mirror import ordered_plan, render
from evo_agents.hub.plan_cli import ATTEMPTS, _check_retry
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import kg, memories, plans, projects, skills
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import LOGIN_HINT, MACHINE, WWW_AUTHENTICATE, Principal, authenticate
from evo_agents.kg.serve import Session, _error
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

MOUNT = "/mcp"
ENDPOINT = MOUNT + "/"  # the path the mounted app answers at
MAX_BODY = DEFAULT_MAX_REQUEST_BODY_SIZE  # 4 MiB: a memory escaped as JSON fits several times over
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")
EVERY_INTERFACE = ("0.0.0.0", "::", "")
SESSION = "evo_mcp"  # the key of McpSession in request.state
SHOWN_PROBLEMS = 5
EXCERPT = 200  # characters of a memory shown by memory_search
PLAN_WIDTH = 110  # as the copies in git
_PROJECT = re.compile(PROJECT_NAME)
_SINK = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
NO_PROJECT = (
    "this session has no project: start `evo-agents hub mcp` in a directory of the project or with --project, so it "
    "sends X-Evo-Project"
)


class Refusal(Exception):
    """A tool call the hub refuses; ``str`` is the message of the tool error."""


@dataclass(frozen=True)
class McpSession:
    """What the request says about the session: its project (None without X-Evo-Project) and its sink."""

    project: str | None
    sink: str


@dataclass(frozen=True)
class Caller:
    user: Principal
    project: str | None  # the session's
    sink: str
    request: Request  # the request as the hub's routes take it

    @property
    def owner(self) -> tuple:
        """Whose kg_more handles a result keeps: those of this user in this session's project."""
        return self.user.user_id, self.project


# The gate


def transport_security(config: HubConfig) -> TransportSecuritySettings:
    """The hosts and origins /mcp answers: loopback, the public URL, and the address the server listens on."""
    hosts, origins = [], []
    for name in LOOPBACK:
        hosts += [name, f"{name}:*"]
        origins += [f"{scheme}://{name}{port}" for scheme in ("http", "https") for port in ("", ":*")]
    if config.public_url:
        parts = urlsplit(config.public_url)
        name = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
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
        sink = request.headers.get(SINK_HEADER) or AGENT_SINK
        if not _SINK.fullmatch(sink):
            message = f"{SINK_HEADER} must name a sink: printable text, 100 characters at most"
            return error_response(request, 400, message)
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
            return error_response(request, 413, f"an MCP message is at most {MAX_BODY // (1024 * 1024)} MiB")
        setattr(request.state, SESSION, McpSession(project, sink))
        return None

    @staticmethod
    async def _authenticate(request: Request):
        authorization = request.headers.get("authorization")
        if authorization is None:
            return _unauthorized(request, f"MCP needs a machine token as `Authorization: Bearer <token>`: {LOGIN_HINT}")
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return _unauthorized(request, f"the Authorization header must be `Bearer <token>`: {LOGIN_HINT}")
        config = request.app.state.config
        try:
            principal = await authenticate(request.app.state.pool, token.strip(), MACHINE, config)
        except psycopg.OperationalError as exc:  # PoolTimeout is one; bugs stay 500s
            log.warning("cannot check a credential: database unavailable", extra={"error": type(exc).__name__})
            return error_response(request, 503, "the hub database is unavailable; try again shortly")
        if principal is None:
            return _unauthorized(request, f"the token is revoked, expired or unknown: {LOGIN_HINT}")
        request.state.principal = principal
        return None


def _unauthorized(request: Request, message: str):
    return error_response(request, 401, message, headers=WWW_AUTHENTICATE)


# The server


class _Answered(Session):
    """A result the hub computed, through the envelope of ``kg serve``: over CAP_CHARS it is cut, and the rest is
    kept for kg_more."""

    def __init__(self, sink: str, name: str, text: str, data: dict):
        super().__init__(None, sink)
        self.answered = name, text, data

    def call(self, name: str, args: dict) -> dict:
        if name == self.answered[0]:
            return self._envelope(self.answered[1], self.answered[2])
        return super().call(name, args)


def _detail(exc: HTTPException) -> str:
    return exc.detail if isinstance(exc.detail, str) else f"the hub refused the call (HTTP {exc.status_code})"


def _problems(exc: ValidationError) -> str:
    """Where and why a model refused the values, without the values."""
    found = []
    for error in exc.errors()[:SHOWN_PROBLEMS]:
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        where = ".".join(str(part) for part in error.get("loc", ()))
        found.append(f"{where}: {message}" if where else message)
    return "; ".join(found)


def _one_line(text: str, limit: int = EXCERPT) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def _where(memory) -> str:
    if memory.scope == "project":
        return f"project {memory.project}, {memory.location}"
    return f"personal, {memory.location}"


def _summary_line(memory) -> str:
    deleted = ", deleted" if memory.deleted else ""
    return f"[{memory.id}] {memory.name}: {memory.type}, {_where(memory)}, revision {memory.revision}{deleted}"


def _about(body: str) -> str:
    """The description of a memory's frontmatter, else the start of its text."""
    described = frontmatter(body).get("description")
    if isinstance(described, str) and described.strip():
        return _one_line(described)
    found = FRONTMATTER.match(body)
    return _one_line(body[found.end() :] if found else body)


class HubMcp(MCPServer):
    """The hub's MCP server: ``mcp_tools.TOOLS``, answered for the caller ``McpGate`` let through."""

    def __init__(self, hub: FastAPI):
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        super().__init__(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS, subscriptions=False)
        root.handlers[:] = handlers  # the SDK configures logging when nothing has: the hub's own stays in place
        root.setLevel(level)
        self.hub = hub
        self._tools = [McpTool.model_validate(tool) for tool in TOOLS]
        self._hub_tools = {
            "memory_search": self._memory_search,
            "memory_get": self._memory_get,
            "memory_write": self._memory_write,
            "plan_list": self._plan_list,
            "plan_show": self._plan_show,
            "plan_step": self._plan_step,
            "skill_list": self._skill_list,
            "hub_projects": self._hub_projects,
        }

    async def list_tools(self) -> list[McpTool]:
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict, context=None) -> CallToolResult:
        started = time.perf_counter()
        request = context.request_context.request if context is not None else None
        state = request.scope.get("state", {}) if isinstance(request, Request) else {}
        user, session, request_id = state.get("principal"), state.get(SESSION), state.get("request_id")
        if user is None or session is None:  # mounted without McpGate in front: refuse rather than guess
            result = _error(f"sign-in required: {LOGIN_HINT}")
        else:
            caller = Caller(user, session.project, session.sink, Request({**request.scope, "app": self.hub}))
            result = await self._answer(caller, name, arguments or {}, request_id)
        log.info(
            "mcp tool",
            extra={
                "tool": name if name in SCHEMAS else "unknown",
                "project": session.project if session else None,
                "login": user.login if user else None,
                "error": bool(result.get("isError")),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "request_id": request_id,
            },
        )
        return CallToolResult.model_validate(result)

    async def _answer(self, caller: Caller, name: str, arguments: dict, request_id) -> dict:
        try:
            if name in KG_TOOL_NAMES:
                return await self._kg(caller, name, arguments)
            tool = self._hub_tools.get(name)
            if tool is None:
                return _error(f"unknown tool {name!r}")
            problems = errors(validate(arguments, SCHEMAS[name]))
            if problems:
                shown = "; ".join(f"{i.path or 'arguments'}: {i.message}" for i in problems[:SHOWN_PROBLEMS])
                return _error(f"bad arguments for {name}: {shown}")
            text, data = await tool(caller, arguments)
            return self.hub.state.kg_handles.call(_Answered(caller.sink, name, text, data), caller.owner, name, {})
        except Refusal as exc:
            return _error(str(exc))
        except HTTPException as exc:
            return _error(_detail(exc))
        except psycopg.OperationalError as exc:  # PoolTimeout is one
            log.warning("mcp tool: database unavailable", extra={"tool": name, "error": type(exc).__name__})
            return _error("the hub database is unavailable; try again shortly")
        except Exception:
            log.exception("mcp tool failed", extra={"tool": name, "request_id": request_id})
            return _error(f"the hub failed to answer; quote request id {request_id} when reporting it")

    async def _kg(self, caller: Caller, name: str, arguments: dict) -> dict:
        if name == "kg_more":  # the handles live in this process: no graph is read
            return self.hub.state.kg_handles.call(Session(None, caller.sink), caller.owner, name, arguments)
        if caller.project is None:
            return _error(NO_PROJECT)
        return await kg.tool_result(self.hub.state, caller.user, caller.project, name, arguments, caller.sink)

    # Projects

    @staticmethod
    def _project(caller: Caller, arguments: dict) -> str:
        project = arguments.get("project") or caller.project
        if project is None:
            raise Refusal(NO_PROJECT + ", or pass project")
        return project

    async def _readable(self, caller: Caller, project: str) -> ProjectAccess:
        """The caller's access to ``project`` when anything of it is visible through the session's sink."""
        async with self.hub.state.pool.connection() as conn:
            access = await project_access(conn, caller.user, project)
        if access.role is None:
            raise Refusal(f"reading project {project} needs a grant on it")
        if access.rules.ceiling(access.max_level, caller.sink) is None:
            raise Refusal(
                f"nothing of project {project} is visible through sink {caller.sink!r}: the sink is not declared in "
                "its knowledge.yaml (or clears a level its ladder lacks)"
            )
        return access

    # Memories

    async def _memory_search(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        scope = arguments.get("scope")
        if scope == "personal" and arguments.get("project"):
            raise Refusal("a personal memory has no project: leave out project or scope")
        project = None if scope == "personal" else arguments.get("project") or caller.project
        if project is not None:
            await self._readable(caller, project)
            scope = "project"
        found = await memories.search_memories(
            caller.request,
            caller.user,
            q=arguments["query"],
            scope=scope,
            project=project,
            limit=arguments.get("limit", 10),
            sink=caller.sink,
        )
        lines, results = [], []
        for memory in found.items:
            lines += [_summary_line(memory), f"    {_about(memory.body)}"]
            results.append({**memory.model_dump(mode="json", exclude={"body"}), "description": _about(memory.body)})
        where = f" in project {project}" if project else " among your personal memories" if scope == "personal" else ""
        text = "\n".join(lines) or f"no memory you can see{where} matches the query"
        data = {"results": results, "summary": f"{len(results)} memories", **({"project": project} if project else {})}
        return text, data

    async def _memory_get(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        memory = await memories.show(caller.request, arguments["id"], caller.user, sink=caller.sink)
        updated = f"updated {memory.updated_at:%Y-%m-%d %H:%M} UTC by {memory.updated_by}"
        text = f"{_summary_line(memory)}, {updated}\n\n{memory.body}"
        data = {"memory": memory.model_dump(mode="json"), "summary": f"memory {memory.id}"}
        return text, {**data, **({"project": memory.project} if memory.project else {})}

    async def _memory_write(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        project = arguments.get("project") or caller.project
        scope = arguments.get("scope") or ("project" if project else None)
        if scope is None:
            raise Refusal(f"{NO_PROJECT}, or pass project; a personal memory takes scope personal and its location")
        if scope == "personal":
            if arguments.get("project"):
                raise Refusal("a personal memory has no project: leave out project or scope")
            if not arguments.get("location"):
                raise Refusal("a personal memory needs its location: its directory's slug without the home directory's")
            project, location = None, arguments["location"]
        else:
            location = arguments.get("location") or HARNESS
        body = arguments["body"]
        try:
            memory = memories.MemoryIn(
                scope=scope,
                project=project,
                location=location,
                name=arguments["name"],
                type=arguments.get("type") or memory_type(body),
                body=body,
                label=arguments.get("label"),
                if_revision=arguments.get("if_revision"),
            )
        except ValidationError as exc:
            raise Refusal(f"the memory cannot be written: {_problems(exc)}; nothing was written") from None
        outcome = await memories.put(caller.request, memory, caller.user, sink=caller.sink)
        if isinstance(outcome, JSONResponse):  # a revision conflict
            payload = json.loads(outcome.body)
            current = payload.get("current")
            held = f" The hub holds revision {current['revision']} (id {current['id']})." if current else ""
            raise Refusal(payload["message"] + held)
        verb = "created" if outcome.created else "changed" if outcome.changed else "unchanged: the hub held this"
        text = (
            f"Memory {outcome.name} {verb} on the hub ({_where(outcome)}): id {outcome.id}, revision "
            f"{outcome.revision}. Nothing was written on this machine: `evo-agents hub memory pull` brings it into "
            "the memory directory."
        )
        data = {"memory": outcome.model_dump(mode="json", exclude={"body"}), "summary": f"memory {outcome.id} {verb}"}
        return text, {**data, **({"project": outcome.project} if outcome.project else {})}

    # Plans

    async def _plan_list(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        project = self._project(caller, arguments)
        await self._readable(caller, project)
        found = await plans.list_plans(
            caller.request, project, caller.user, area=arguments.get("area"), sink=caller.sink
        )
        lines = [
            f"{p.plan_id} ({p.area}, revision {p.revision}): {p.title or '-'}; {p.steps_done}/{p.steps_total} done"
            for p in found
        ]
        text = "\n".join(lines) or f"project {project} has no plan you can see on the hub"
        listed = [p.model_dump(mode="json") for p in found]
        return text, {"project": project, "plans": listed, "summary": f"{len(found)} plans"}

    async def _plan_show(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        project = self._project(caller, arguments)
        await self._readable(caller, project)
        plan = await plans.show(caller.request, project, arguments["plan_id"], caller.user, sink=caller.sink)
        meta = {key: plan[key] for key in ("project", "plan_id", "area", "revision", "digest", "label")}
        if "step" not in arguments:
            try:
                text = render(plan["body"], project, plan["revision"], plan["digest"])
            except HubError as exc:
                raise Refusal(str(exc)) from None
            return text, {**meta, "body": plan["body"], "summary": f"plan {plan['plan_id']}"}
        try:
            index = step_index(plan["body"], arguments["step"])
        except PlanProblem as exc:
            raise Refusal(f"plan {plan['plan_id']}: {exc}") from None
        step = ordered_plan({"steps": [plan["body"]["steps"][index]]})["steps"][0]
        header = f"# Step {arguments['step']} of plan {plan['plan_id']}, revision {plan['revision']}\n"
        text = header + yaml.safe_dump(step, allow_unicode=True, sort_keys=False, width=PLAN_WIDTH)
        return text, {**meta, "step": step, "summary": f"step {arguments['step']} of plan {plan['plan_id']}"}

    async def _plan_step(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        project = self._project(caller, arguments)
        plan_id, step, status = arguments["plan_id"], arguments["step"], arguments["status"]
        if arguments.get("done_at") and status != "done":
            raise Refusal("done_at goes with status done")
        updates = {"status": status}
        if status == "done":
            updates["done_at"] = arguments.get("done_at") or date.today().isoformat()
        updates.update({key: arguments[key] for key in ("evidence", "note") if key in arguments})
        given = arguments.get("if_revision")
        base = None if given is not None else await plans.show(caller.request, project, plan_id, caller.user, None)
        for attempt in range(ATTEMPTS):
            revision = given if given is not None else base["revision"]
            try:
                patch = plans.PlanPatch(section="steps", step=step, updates=updates, if_revision=revision)
            except ValidationError as exc:
                raise Refusal(f"the step cannot be set: {_problems(exc)}; nothing was written") from None
            outcome = await plans.patch(caller.request, project, plan_id, patch, caller.user)
            if not isinstance(outcome, JSONResponse):
                break
            payload = json.loads(outcome.body)
            if payload.get("error") != plans.REVISION_CONFLICT or given is not None or attempt == ATTEMPTS - 1:
                raise Refusal(payload["message"])
            latest = await plans.show(caller.request, project, plan_id, caller.user, None)
            try:
                _check_retry(base, latest, plan_id, "steps", None, step, updates)
            except HubError as exc:
                raise Refusal(str(exc)) from None
            base = latest
        written = outcome.body["steps"][step_index(outcome.body, step)]
        state = "is now" if outcome.changed else "was already"
        lines = [
            f"Step {step} of plan {plan_id} {state} {status} on the hub: revision {outcome.revision}. Nothing was "
            "written on this machine: `evo-agents hub plan export` brings the copy in git up to date."
        ]
        lines += [f"warning: {w.path or '<root>'}: {w.message}" for w in outcome.warnings[:SHOWN_PROBLEMS]]
        data = {
            "project": project,
            "plan_id": plan_id,
            "revision": outcome.revision,
            "changed": outcome.changed,
            "step": written,
            "warnings": [w.model_dump() for w in outcome.warnings],
            "summary": f"step {step} {status}",
        }
        return "\n".join(lines), data

    # Skills and projects

    async def _skill_list(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        found = await skills.list_skills(
            caller.request, caller.user, scope=arguments.get("scope"), project=arguments.get("project")
        )
        lines = [
            f"{s.name} ({'global' if s.project is None else f'project {s.project}'}, version {s.version}): "
            f"{_one_line(s.description)}"
            for s in found
        ]
        data = {"skills": [s.model_dump(mode="json") for s in found], "summary": f"{len(found)} skills"}
        return "\n".join(lines) or "no skill you can see on the hub", data

    async def _hub_projects(self, caller: Caller, arguments: dict) -> tuple[str, dict]:
        found = await projects.list_projects(caller.request, caller.user)
        lines, listed = [], []
        for p in found:
            sinks = ", ".join(f"{s.id} ({s.kind}, {s.clearance.level})" for s in p.sinks) or "none"
            repos = ", ".join(r.name for r in p.repos) or "none"
            role = f"role {p.role}, max level {p.max_level}" if p.role else "no grant (hub admin)"
            this = " (this session)" if p.name == caller.project else ""
            lines.append(f"{p.name}{this}: {role}; sinks {sinks}; repos {repos}")
            listed.append(
                {
                    "name": p.name,
                    "role": p.role,
                    "max_level": p.max_level,
                    "levels": p.levels,
                    "locations": p.locations,
                    "sinks": [s.model_dump(mode="json", exclude_none=True) for s in p.sinks],
                    "repos": [r.name for r in p.repos],
                }
            )
        data = {"projects": listed, "session_project": caller.project, "summary": f"{len(listed)} projects"}
        return "\n".join(lines) or "you hold no grant on a project of this hub", data


def mount(app: FastAPI, config: HubConfig) -> HubMcp:
    """Mount /mcp on ``app`` behind McpGate; the server, whose session manager the lifespan must run."""
    server = HubMcp(app)
    security = transport_security(config)
    endpoint = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=security,
        max_request_body_size=MAX_BODY,
    )
    app.mount(MOUNT, endpoint)
    app.add_middleware(McpGate, security=security)
    return server

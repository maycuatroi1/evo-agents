"""Session digests: what the Stop hook of the evo-hub plugin pushes about each Claude Code session of a project's
directory. ``evo_agents.hub.digest`` builds a digest from the session's transcript, and ``evo_agents.hub.redact``
cleans it on the machine before it is sent.

PUT /v1/projects/{project}/digests/{session_id} writes the digest of one session under the write rule of the project:
the writer role, and a sink of kind hub that clears the project's default label, which the digest carries. A push of a
later turn of the session replaces it; only the member who pushed the session first may (403 for anyone else). The
body is checked against the limits of ``evo_agents.hub.digest`` and stored as the model reads it: a field the hub does
not know is dropped, so a newer client works with an older hub. A request is at most MAX_REQUEST bytes, read before it
is parsed. A push writes no audit row, since one comes after every turn of a session; the log line names the project
and the session, never what the digest says.

GET /v1/projects/{project}/digests lists the digests the caller may read, the latest pushed first, filtered by
``since`` (pushed at or after) and ``login`` (who pushed them), with how many match; GET .../digests/{session_id} is
one of them with the digest itself. Reading needs a grant on the project and goes through the read rule with the
digest's label, through the sink X-Evo-Sink names, else the project's hub sink, as runs and plans are read. A digest one
may not read answers as one that does not exist. A list examines at most SCAN_ROWS digests.

``prune_digests`` (the job hub.prune_digests, daily) deletes the digests not pushed again for DIGEST_DAYS days.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import digest, tables
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

DIGEST_DAYS = 90  # a digest not pushed again for this long is deleted
MAX_REQUEST = 2 * 1024 * 1024  # a digest at its limits, escaped as JSON at worst, fits
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SCAN_ROWS = 10_000
TOO_LARGE = f"a session digest is at most {MAX_REQUEST // 1024} KiB of request"
SESSION_PATTERN = rf"^{digest.SESSION_ID.pattern}$"
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

SessionId = Annotated[
    str, Path(pattern=SESSION_PATTERN, description="the id Claude Code gives the session, as its transcript names it")
]
Count = Annotated[int, Field(ge=0, le=digest.MAX_COUNT)]
Name = Annotated[str, Field(min_length=1, max_length=digest.MAX_NAME)]


class BoundedRoute(APIRoute):
    """Reads at most MAX_REQUEST bytes of a request before FastAPI parses it: a larger one is a 413, whether it says
    its length or not."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST):
                raise HTTPException(413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_REQUEST:
                    raise HTTPException(413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


router = APIRouter(
    prefix="/v1/projects", tags=["digests"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)


# Models


class ModelCount(BaseModel):
    model: Name
    messages: Count = Field(description="messages of the model that name it")


class TokenUsage(BaseModel):
    input_tokens: Count = 0
    output_tokens: Count = 0
    cache_creation_input_tokens: Count = 0
    cache_read_input_tokens: Count = 0


class ToolCount(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Name = Field(alias=digest.TOOL_NAME, description="the tool, as the runtime names it")
    calls: Count
    errors: Count = Field(description="calls whose result was an error")


class ProgramCount(BaseModel):
    program: Name = Field(description="the program a Bash call ran, with its subcommand: git push, python -m pytest")
    calls: Count
    errors: Count


class CommandCount(BaseModel):
    command: str = Field(max_length=digest.COMMAND_CHARS)
    n: Count


class ErrorText(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Name = Field(alias=digest.TOOL_NAME)
    text: str = Field(max_length=digest.ERROR_CHARS, description="the tool's result, cut")
    n: Count = Field(description="how many results said exactly this")


class SessionDigest(BaseModel):
    """The digest of one session, as ``evo_agents.hub.digest.build`` makes it."""

    cwd: str = Field(min_length=1, max_length=digest.MAX_CWD, description="the session's working directory")
    messages: int = Field(ge=digest.MIN_MESSAGES, le=digest.MAX_COUNT, description="of the person and the model")
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    model: Name | None = Field(None, description="the model most messages name")
    models: list[ModelCount] = Field(default_factory=list, max_length=digest.MAX_MODELS)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    tools: list[ToolCount] = Field(default_factory=list, max_length=digest.MAX_TOOLS)
    bash: list[ProgramCount] = Field(default_factory=list, max_length=digest.MAX_PROGRAMS)
    user_turns: list[Annotated[str, Field(max_length=digest.USER_TURN_CHARS)]] = Field(
        default_factory=list, max_length=digest.MAX_USER_TURNS
    )
    commands: list[Annotated[str, Field(max_length=digest.COMMAND_CHARS)]] = Field(
        default_factory=list, max_length=digest.MAX_COMMANDS
    )
    repeated_commands: list[CommandCount] = Field(default_factory=list, max_length=digest.MAX_REPEATED)
    errors: list[ErrorText] = Field(default_factory=list, max_length=digest.MAX_ERRORS)
    files_read: Count = 0
    files_edited: list[Annotated[str, Field(max_length=digest.PATH_CHARS)]] = Field(
        default_factory=list, max_length=digest.MAX_FILES
    )


class DigestSummary(BaseModel):
    session_id: str
    login: str = Field(description="the member who pushed it")
    cwd: str
    messages: int
    model: str | None
    started_at: datetime | None
    ended_at: datetime | None
    label: dict
    created_at: datetime = Field(description="its first push")
    updated_at: datetime = Field(description="its latest push")


class DigestRecord(DigestSummary):
    digest: SessionDigest


class DigestList(BaseModel):
    digests: list[DigestSummary] = Field(description="the latest pushed first")
    total: int = Field(description="digests the caller may read that match the filters")
    limit: int
    offset: int


# Queries


def _summary_columns():
    d, u = tables.session_digests, tables.users
    return (
        d.c.session_id,
        u.c.login,
        d.c.cwd,
        d.c.messages,
        d.c.model,
        d.c.started_at,
        d.c.ended_at,
        d.c.label,
        d.c.created_at,
        d.c.updated_at,
    )


def _upsert(access: ProjectAccess, user: Principal, session_id: str, label: dict, body: SessionDigest):
    """The insert of a session's digest, which replaces the one held when the same member pushed it."""
    d = tables.session_digests
    values = {
        "label": label,
        "cwd": body.cwd,
        "messages": body.messages,
        "model": body.model,
        "started_at": body.started_at,
        "ended_at": body.ended_at,
        "body": body.model_dump(mode="json", by_alias=True),
    }
    stmt = pg_insert(d).values(project_id=access.project_id, session_id=session_id, user_id=user.user_id, **values)
    return stmt.on_conflict_do_update(
        constraint="session_digests_project_id_session_id_key",
        set_={**{name: stmt.excluded[name] for name in values}, "updated_at": func.now()},
        where=d.c.user_id == stmt.excluded.user_id,
    ).returning(d.c.id)


async def _summary(conn: AsyncConnection, digest_id: int) -> DigestSummary:
    d, u = tables.session_digests, tables.users
    query = select(*_summary_columns()).join_from(d, u, u.c.id == d.c.user_id).where(d.c.id == digest_id)
    return DigestSummary(**(await conn.execute(query)).one()._mapping)


def _not_found(project: str, session_id: str) -> str:
    return f"project {project} has no digest of session {session_id}: see GET /v1/projects/{project}/digests"


# Routes


@router.put("/{project}/digests/{session_id}", response_model=DigestSummary, responses=REFUSALS)
async def put_digest(
    request: Request, project: ProjectName, session_id: SessionId, body: SessionDigest, user: CurrentUser
) -> DigestSummary:
    """Write the digest of session ``session_id``, replacing the one this member pushed before."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        label = access.push_label()
        written = (await conn.execute(_upsert(access, user, session_id, label, body))).first()
        if written is None:
            raise HTTPException(403, f"the digest of session {session_id} in project {project} is another member's")
        summary = await _summary(conn, written.id)
    log.info("session digest written", extra={"project": project, "session_id": session_id, "login": user.login})
    return summary


@router.get("/{project}/digests", response_model=DigestList, responses=READ_REFUSALS)
async def list_digests(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    since: Annotated[AwareDatetime | None, Query(description="pushed at or after this time")] = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="pushed by this member")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, le=SCAN_ROWS)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> DigestList:
    """The digests of the project the caller may read, the latest pushed first."""
    d, u = tables.session_digests, tables.users
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plan_routes._reader(access)
        query = select(*_summary_columns()).join_from(d, u, u.c.id == d.c.user_id)
        query = query.where(d.c.project_id == access.project_id)
        if since is not None:
            query = query.where(d.c.updated_at >= since)
        if login is not None:
            query = query.where(func.lower(u.c.login) == login.lower())
        query = query.order_by(d.c.updated_at.desc(), d.c.id.desc()).limit(SCAN_ROWS)
        rows = (await conn.execute(query)).all()
    through = plan_routes._sink(access, sink)
    visible = [DigestSummary(**row._mapping) for row in rows if access.visible(row.label, through)]
    return DigestList(digests=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


@router.get("/{project}/digests/{session_id}", response_model=DigestRecord, responses=READ_REFUSALS)
async def show_digest(
    request: Request,
    project: ProjectName,
    session_id: SessionId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> DigestRecord:
    """The digest of session ``session_id``, when the caller may read it."""
    d, u = tables.session_digests, tables.users
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        plan_routes._reader(access)
        query = (
            select(*_summary_columns(), d.c.body)
            .join_from(d, u, u.c.id == d.c.user_id)
            .where(d.c.project_id == access.project_id, d.c.session_id == session_id)
        )
        row = (await conn.execute(query)).first()
    if row is None or not access.visible(row.label, plan_routes._sink(access, sink)):
        raise HTTPException(404, _not_found(project, session_id))
    found = dict(row._mapping)
    body = SessionDigest.model_validate(found.pop("body"))
    return DigestRecord(**found, digest=body)


# Retention


async def prune_digests(engine: AsyncEngine, days: int = DIGEST_DAYS) -> dict:
    """Delete the digests not pushed again for ``days`` days."""
    d = tables.session_digests
    async with engine.begin() as conn:
        deleted = (await conn.execute(delete(d).where(d.c.updated_at < func.now() - timedelta(days=days)))).rowcount
    log.info("session digests pruned", extra={"deleted": deleted, "days": days})
    return {"deleted": deleted, "days": days}

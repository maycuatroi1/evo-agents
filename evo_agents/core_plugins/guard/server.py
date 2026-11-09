"""The routes of the core plugin guard (``evo_agents.core_plugins.guard``, ``docs/guard.md``).

- ``router``, under /v1/x/guard/<project>/ for a member with a grant on a project that enables the plugin: the
  project's private names (``terms``) and the patterns of the repos it publishes (``targets``), which its members read
  and an admin of the project (or of the hub) adds and removes, each change one audit row that names the row's id, never
  the term; and ``check``, which tells a member whether a text would pass the hub's guard.
- ``policy_router``, GET /v1/guard/policy for a member or the agent of a run, and GET /v1/worker/guard/policy for a
  worker (whose token works under /v1/worker/ alone): the policy of the whole hub, the terms hashed (``evo_agents.hub.guard``), with an ETag. The evo-hub plugin's hooks keep
  it on a member's machine and a worker takes it when the digest its heartbeat carries changes.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, exists, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents import private_names
from evo_agents.hub import guard, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
PATTERN = re.compile(r"^[a-z0-9*?][a-z0-9.*?-]*(:[0-9]+)?(/[a-z0-9._*?-]+)+$")
MAX_CHECK_CHARS = 1_000_000
RowId = Annotated[int, Path(ge=1)]

router = APIRouter(tags=["guard"], responses={401: {"model": ErrorBody}})
policy_router = APIRouter(tags=["guard"], responses={401: {"model": ErrorBody}})


class GuardTermWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(
        min_length=1,
        max_length=private_names.MAX_TERM_CHARS,
        description="a private name: a project, a host or domain, a group or a repo; matched by its words in any case",
    )
    note: str | None = Field(None, max_length=200, description="why it is private, for the project's admins")


class GuardTargetWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(
        min_length=1,
        max_length=200,
        description="a repo the project publishes: a URL, OWNER/REPO (GitHub) or host/owner/repo, * and ? as wildcards",
    )
    note: str | None = Field(None, max_length=200)


class GuardTerm(BaseModel):
    id: int
    term: str
    words: str = Field(description="the words the guard matches, lowercased")
    note: str | None
    created_by: str
    created_at: datetime


class GuardTarget(BaseModel):
    id: int
    pattern: str = Field(description="host/owner/repo in lower case, * and ? as wildcards")
    note: str | None
    created_by: str
    created_at: datetime


class GuardCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_CHECK_CHARS)
    repo: str | None = Field(None, max_length=500, description="where the text goes; none checks it as if guarded")


class GuardCheckHit(BaseModel):
    found: str = Field(description="the private name as the text writes it")
    context: str = Field(description="the text around it")


class GuardCheckOut(BaseModel):
    guarded: bool = Field(description="whether the hub's guard checks text bound for the repo")
    hits: list[GuardCheckHit]


class GuardPolicy(BaseModel):
    version: int
    digest: str = Field(description="empty when the policy guards nothing (no term or no target)")
    terms: list[dict] = Field(description="each term hashed: h, its hash; n, its words; part, its length or 0")
    targets: list[str]


def _admin(access: ProjectAccess, user: Principal, doing: str) -> None:
    if not user.admin and not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"{doing} the guard's lists of project {access.name} needs the admin role on it; you hold {held}"
        )


def pattern_of(given: str) -> str:
    """``given`` as a stored pattern: a URL or a slug becomes ``host/owner/repo``; 422 for one that is not a repo."""
    value = given.strip()
    if not any(c in value for c in "*?"):
        value = private_names.repo_key(value)
    else:
        value = re.sub(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?", "", value.lower()).removesuffix(".git").strip("/")
    if not PATTERN.match(value):
        raise HTTPException(422, f"{given!r} names no repo: write a URL, OWNER/REPO or host/owner/repo")
    return value


async def _logins(conn: AsyncConnection, ids: set[int]) -> dict[int, str]:
    u = tables.users
    return dict((await conn.execute(select(u.c.id, u.c.login).where(u.c.id.in_(ids)))).all()) if ids else {}


async def _record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name} {target}",
        project_id=access.project_id,
    )


async def _terms(conn: AsyncConnection, access: ProjectAccess, row_id: int | None = None) -> list[GuardTerm]:
    t = tables.guard_terms
    query = select(t).where(t.c.project_id == access.project_id).order_by(t.c.words, t.c.id)
    rows = (await conn.execute(query if row_id is None else query.where(t.c.id == row_id))).all()
    logins = await _logins(conn, {row.created_by for row in rows})
    return [
        GuardTerm(
            id=r.id, term=r.term, words=r.words, note=r.note, created_by=logins[r.created_by], created_at=r.created_at
        )
        for r in rows
    ]


async def _targets(conn: AsyncConnection, access: ProjectAccess, row_id: int | None = None) -> list[GuardTarget]:
    g = tables.guard_targets
    query = select(g).where(g.c.project_id == access.project_id).order_by(g.c.pattern)
    rows = (await conn.execute(query if row_id is None else query.where(g.c.id == row_id))).all()
    logins = await _logins(conn, {row.created_by for row in rows})
    return [
        GuardTarget(id=r.id, pattern=r.pattern, note=r.note, created_by=logins[r.created_by], created_at=r.created_at)
        for r in rows
    ]


# The project's lists


@router.get("/{project}/terms", response_model=list[GuardTerm], responses=REFUSALS)
async def list_terms(request: Request, project: ProjectName, user: CurrentUser) -> list[GuardTerm]:
    """The private names of the project, by their words (a member of the project)."""
    async with request.app.state.engine.connect() as conn:
        return await _terms(conn, await project_access(conn, user, project))


@router.post("/{project}/terms", response_model=GuardTerm, status_code=201, responses=REFUSALS)
async def add_term(request: Request, project: ProjectName, body: GuardTermWrite, user: CurrentUser) -> GuardTerm:
    """Add a private name of the project (an admin of the project or of the hub); 409 when the project has one of the
    same words already, 422 for one without a letter or digit."""
    try:
        words = " ".join(private_names.term_words(body.term))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    t = tables.guard_terms
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "changing")
        if await conn.scalar(select(exists().where(t.c.project_id == access.project_id, t.c.words == words))):
            raise HTTPException(409, f"project {project} lists a private name of the same words already")
        values = {"project_id": access.project_id, "term": body.term.strip(), "words": words, "note": body.note}
        row_id = (
            await conn.execute(insert(t).values(**values, created_by=user.user_id).returning(t.c.id))
        ).scalar_one()
        await _record(conn, user, access, audit.GUARD_TERM_ADD, f"guard-term:{row_id}")
        made = (await _terms(conn, access, row_id))[0]
    log.info("guard term added", extra={"project": project, "term_id": row_id})
    return made


@router.delete("/{project}/terms/{term_id}", response_model=GuardTerm, responses=REFUSALS)
async def remove_term(request: Request, project: ProjectName, term_id: RowId, user: CurrentUser) -> GuardTerm:
    """Remove a private name of the project (an admin of the project or of the hub); the name removed."""
    t = tables.guard_terms
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "changing")
        found = await _terms(conn, access, term_id)
        if not found:
            raise HTTPException(404, f"project {project} has no private name {term_id}")
        await conn.execute(delete(t).where(t.c.project_id == access.project_id, t.c.id == term_id))
        await _record(conn, user, access, audit.GUARD_TERM_REMOVE, f"guard-term:{term_id}")
    return found[0]


@router.get("/{project}/targets", response_model=list[GuardTarget], responses=REFUSALS)
async def list_targets(request: Request, project: ProjectName, user: CurrentUser) -> list[GuardTarget]:
    """The patterns of the repos the project publishes (a member of the project)."""
    async with request.app.state.engine.connect() as conn:
        return await _targets(conn, await project_access(conn, user, project))


@router.post("/{project}/targets", response_model=GuardTarget, status_code=201, responses=REFUSALS)
async def add_target(request: Request, project: ProjectName, body: GuardTargetWrite, user: CurrentUser) -> GuardTarget:
    """Add a pattern of the repos the project publishes (an admin of the project or of the hub); 409 when the project
    lists it already."""
    pattern = pattern_of(body.pattern)
    g = tables.guard_targets
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "changing")
        if await conn.scalar(select(exists().where(g.c.project_id == access.project_id, g.c.pattern == pattern))):
            raise HTTPException(409, f"project {project} lists {pattern} already")
        values = {"project_id": access.project_id, "pattern": pattern, "note": body.note, "created_by": user.user_id}
        row_id = (await conn.execute(insert(g).values(**values).returning(g.c.id))).scalar_one()
        await _record(conn, user, access, audit.GUARD_TARGET_ADD, f"guard-target:{row_id}")
        made = (await _targets(conn, access, row_id))[0]
    log.info("guard target added", extra={"project": project, "target_id": row_id})
    return made


@router.delete("/{project}/targets/{target_id}", response_model=GuardTarget, responses=REFUSALS)
async def remove_target(request: Request, project: ProjectName, target_id: RowId, user: CurrentUser) -> GuardTarget:
    """Remove a pattern of the repos the project publishes (an admin of the project or of the hub); the one removed."""
    g = tables.guard_targets
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "changing")
        found = await _targets(conn, access, target_id)
        if not found:
            raise HTTPException(404, f"project {project} has no target {target_id}")
        await conn.execute(delete(g).where(g.c.project_id == access.project_id, g.c.id == target_id))
        await _record(conn, user, access, audit.GUARD_TARGET_REMOVE, f"guard-target:{target_id}")
    return found[0]


@router.post("/{project}/check", response_model=GuardCheckOut, responses=REFUSALS)
async def check(request: Request, project: ProjectName, body: GuardCheckIn, user: CurrentUser) -> GuardCheckOut:
    """Whether ``text`` holds a private name of any project of the hub, and whether ``repo`` is guarded (a member of
    the project); nothing is kept."""
    async with request.app.state.engine.connect() as conn:
        await project_access(conn, user, project)
        found = await guard.policy(conn)
    hits = found.matcher().find(body.text, limit=50)
    guarded = True if body.repo is None else found.guards(body.repo)
    return GuardCheckOut(guarded=guarded, hits=[GuardCheckHit(found=h.found, context=h.context) for h in hits])


# The hub's policy


@policy_router.get("/v1/guard/policy", response_model=GuardPolicy, responses={304: {"description": "unchanged"}})
@policy_router.get("/v1/worker/guard/policy", response_model=GuardPolicy, responses={304: {"description": "unchanged"}})
async def policy(request: Request, user: CurrentUser) -> Response | GuardPolicy:
    """The policy of the hub: the private names of every project that enables the plugin, hashed, and the repos they
    publish; 304 when ``If-None-Match`` names its digest."""
    async with request.app.state.engine.connect() as conn:
        found = await guard.cached_policy(conn, getattr(request.app.state, "cache", None))
    data = found.to_json()
    data["digest"] = guard.digest_of(found)
    etag = f'"{data["digest"] or "none"}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(GuardPolicy(**data).model_dump_json(), media_type="application/json", headers={"ETag": etag})

"""The agent systems of a project (``docs/hub.md``, Agent systems; ``evo_agents.hub.system_spec``).

- GET /v1/projects/{p}/systems: its systems, by slug.
- GET .../systems/{slug}[?revision=N]: one, its spec at the revision (the latest by default), each pinned node with
  the latest version of its profile, and what is wrong in it now.
- GET .../systems/{slug}/revisions: its revisions, the newest first.
- PUT .../systems/{slug} {spec, if_revision}: creates it (no ``if_revision``, or 0) or stores the spec as its next
  revision (``if_revision`` its revision now; 409 ``revision_conflict`` otherwise, with the system as the hub holds
  it); a spec with an error is 422 with the problems as ``detail``, nothing written; a spec the hub holds already adds
  no revision.
- POST .../systems/validate {spec}: what is wrong in a spec, written nowhere.
- DELETE .../systems/{slug}: deletes it with its revisions; its profiles, graphs and runs stay.

Every member of the project reads its systems; writing one needs the admin role on it. A pinned node that names no
version is pinned at its profile's latest when the spec is written. Writes are audited as ``system.*``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from evo_agents.hub import system_spec
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, route_autonomy, system_store
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import CODES, ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

REVISION_CONFLICT = "revision_conflict"
router = APIRouter(
    prefix="/v1/projects", tags=["systems"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)
Slug = Annotated[str, Path(min_length=1, max_length=63, pattern=system_spec.SLUG_PATTERN)]


class ProblemOut(BaseModel):
    level: Literal["error", "warning"] = Field(description="an error stops a write; a warning does not")
    path: str = Field(description="where in the spec, as nodes[2].version or edges[0]; empty for the spec whole")
    message: str
    node: str | None = Field(None, description="the id of the node it is about, to show it there")
    edge: int | None = Field(None, description="the index of the edge it is about in edges")


class Pin(BaseModel):
    node: str
    kind: Literal["agent", "orchestrator"]
    profile: str = Field(description="the handle of the profile")
    version: int | None = Field(description="the version of the profile the node pins")
    latest_version: int | None = Field(description="the profile's version now; null when the project has none")
    outdated: bool = Field(description="the profile has a newer version than the one pinned")
    deleted: bool = Field(description="the profile was deleted: it takes no new run")


class SystemSummary(BaseModel):
    slug: str
    name: str
    revision: int
    entry_agent: str | None = Field(description="the handle of the agent its entry interacts with; null for none")
    nodes: int
    edges: int
    updated_by: str | None
    updated_at: datetime


class SystemOut(BaseModel):
    project: str
    slug: str
    name: str
    revision: int = Field(description="the revision shown")
    latest_revision: int
    spec: dict = Field(description="the spec at that revision, evo-agent-system/1")
    pins: list[Pin] = Field(description="the agent and orchestrator nodes, with the versions they pin")
    problems: list[ProblemOut] = Field(description="what is wrong in the spec against the project now")
    entry_agent: str | None
    can_change: bool = Field(description="the caller holds the admin role on the project")
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class SystemWritten(SystemOut):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this; no revision was added")


class SystemRevision(BaseModel):
    revision: int
    actor: str | None
    created_at: datetime


class SystemPut(BaseModel):
    spec: dict = Field(description="evo-agent-system/1: schema, name, purpose, limits, nodes, edges")
    if_revision: int | None = Field(
        None, ge=0, description="the revision it is at now; none or 0 creates it, and is 409 when it exists"
    )


class SystemCheck(BaseModel):
    spec: dict
    slug: str | None = Field(None, description="the system it would replace, whose entry agent it may keep")


class SystemChecked(BaseModel):
    valid: bool = Field(description="no error: PUT would store it")
    spec: dict = Field(description="the spec as PUT would store it, defaults filled in and versions pinned")
    problems: list[ProblemOut]


class SystemDeleted(BaseModel):
    project: str
    deleted: str


class SystemConflict(ErrorBody):
    current: SystemOut | None = Field(None, description="the system as the hub holds it")


WRITE_REFUSALS = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": SystemConflict},
    422: {"model": ErrorBody},
}


class Refused(Exception):
    def __init__(self, status: int, message: str, *, code=None, detail=None, current=None):
        super().__init__(message)
        self.status, self.message, self.code, self.detail, self.current = status, message, code, detail, current


def _refusal(request: Request, exc: Refused) -> JSONResponse:
    body = SystemConflict(
        error=exc.code or CODES.get(exc.status, f"http_{exc.status}"),
        message=exc.message,
        request_id=getattr(request.state, "request_id", None),
        detail=exc.detail,
        current=exc.current,
    )
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=exc.status)


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the agent systems of project {access.name} needs a grant on it")


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"{doing} the agent systems of project {access.name} needs the admin role on it; you hold {held}"
        )


def _not_found(access: ProjectAccess, slug: str) -> HTTPException:
    return HTTPException(
        404,
        f"project {access.name} has no agent system {slug}: see `evo-agents hub system list --project {access.name}`",
    )


def _pins(spec: dict, refs: system_store.Refs) -> list[Pin]:
    found = []
    for pin in system_spec.pins(spec):
        ref = refs.profiles.get(pin["profile"])
        latest = None if ref is None else ref.latest
        version = pin["version"]
        found.append(
            Pin(
                **pin,
                latest_version=latest,
                outdated=latest is not None and version is not None and version < latest,
                deleted=bool(ref and ref.deleted),
            )
        )
    return found


def _problems(problems: list[system_spec.Problem]) -> list[ProblemOut]:
    return [ProblemOut(**p.as_dict()) for p in problems]


def checked(spec, refs: system_store.Refs, slug: str | None) -> tuple[dict, list[system_spec.Problem]]:
    """``spec`` checked and pinned, and what is wrong in it against the project; ``slug`` the system it replaces."""
    found, problems = system_spec.check(spec)
    if system_spec.errors(problems):
        return found, problems
    found = system_spec.pin(found, refs.profiles)
    entries = refs.entries_but(slug) if slug else refs.entries
    return found, problems + system_spec.check_refs(found, refs.profiles, refs.graphs, entries)


def _out(access: ProjectAccess, held: system_store.System, spec: dict, revision: int, refs) -> SystemOut:
    _, problems = system_spec.check(spec)
    problems += system_spec.check_refs(spec, refs.profiles, refs.graphs, refs.entries_but(held.slug))
    return SystemOut(
        project=access.name,
        slug=held.slug,
        name=spec.get("name", held.name),
        revision=revision,
        latest_revision=held.revision,
        spec=spec,
        pins=_pins(spec, refs),
        problems=_problems(problems),
        entry_agent=system_spec.entry_agent(spec),
        can_change=has_role(access.role, "admin"),
        created_by=held.created_by,
        updated_by=held.updated_by,
        created_at=held.created_at,
        updated_at=held.updated_at,
    )


async def record(conn, user, access: ProjectAccess, action: str, slug: str, revision: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name} system={slug} revision={revision}",
        project_id=access.project_id,
    )


async def write_spec(
    conn, user, access: ProjectAccess, slug: str, spec: dict, held: system_store.System | None, refs
) -> int:
    """Store ``spec``, checked and pinned (``checked``) with no error, as the next revision of system ``slug`` (the
    first when ``held`` is None), with its entry agent and the audit row; the revision. The caller checked that
    ``user`` may (the admin role on the project) and that ``held`` is what the system is at now."""
    entry = system_spec.entry_agent(spec)
    entry_id = refs.ids.get(entry) if entry else None
    revision = await system_store.write(conn, access.project_id, slug, spec, entry_id, user.user_id, held)
    action = audit.SYSTEM_CREATE if held is None else audit.SYSTEM_UPDATE
    await record(conn, user, access, action, slug, revision)
    return revision


# Reading


@router.get(
    "/{project}/systems",
    response_model=list[SystemSummary],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def list_systems(request: Request, project: ProjectName, user: CurrentUser) -> list[SystemSummary]:
    """The agent systems of the project, by slug."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        held = await system_store.listed(conn, access.project_id)
    return [
        SystemSummary(
            slug=s.slug,
            name=s.name,
            revision=s.revision,
            entry_agent=system_spec.entry_agent(s.spec),
            nodes=len(s.spec.get("nodes") or ()),
            edges=len(s.spec.get("edges") or ()),
            updated_by=s.updated_by,
            updated_at=s.updated_at,
        )
        for s in held
    ]


@router.post(
    "/{project}/systems/validate",
    response_model=SystemChecked,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def validate_system(request: Request, project: ProjectName, body: SystemCheck, user: CurrentUser):
    """What is wrong in a spec against the project, and the spec as PUT would store it; nothing is written."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        refs = await system_store.refs(conn, access.project_id)
    spec, problems = checked(body.spec, refs, body.slug)
    return SystemChecked(valid=not system_spec.errors(problems), spec=spec, problems=_problems(problems))


@router.get(
    "/{project}/systems/{slug}",
    response_model=SystemOut,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_system(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, description="a revision of it; the latest by default")] = None,
) -> SystemOut:
    """One agent system, its spec at a revision, its pins and what is wrong in it now."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        held = await system_store.one(conn, access.project_id, slug)
        if held is None:
            raise _not_found(access, slug)
        spec = held.spec
        if revision is not None and revision != held.revision:
            spec = await system_store.revision_spec(conn, held.id, revision)
            if spec is None:
                raise HTTPException(404, f"agent system {slug} has revisions 1 to {held.revision}, not {revision}")
        refs = await system_store.refs(conn, access.project_id)
    return _out(access, held, spec, revision or held.revision, refs)


@router.get(
    "/{project}/systems/{slug}/revisions",
    response_model=list[SystemRevision],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def system_revisions(request: Request, project: ProjectName, slug: Slug, user: CurrentUser):
    """The revisions of an agent system, the newest first."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        held = await system_store.one(conn, access.project_id, slug)
        if held is None:
            raise _not_found(access, slug)
        return [SystemRevision(**row) for row in await system_store.revisions(conn, held.id)]


# Changing


def _stale(access: ProjectAccess, held: system_store.System, given: int | None, refs) -> Refused:
    if not given:
        message = (
            f"agent system {held.slug} is on the hub at revision {held.revision}: pass if_revision {held.revision} "
            "to replace it"
        )
    else:
        message = (
            f"agent system {held.slug} is at revision {held.revision} on the hub, not {given}: someone changed it "
            "since you read it. Read it again and retry; nothing was written"
        )
    current = _out(access, held, held.spec, held.revision, refs)
    return Refused(409, message, code=REVISION_CONFLICT, current=current)


@router.put("/{project}/systems/{slug}", response_model=SystemWritten, responses=WRITE_REFUSALS)
async def put_system(request: Request, project: ProjectName, slug: Slug, body: SystemPut, user: CurrentUser):
    """Create the agent system, or store ``spec`` as its next revision."""
    if (why := system_spec.check_slug(slug)) is not None:
        raise HTTPException(422, why)
    given = body.spec.get("slug") if isinstance(body.spec, dict) else None
    if given is not None and given != slug:
        raise HTTPException(422, f"the spec names system {given!r}, the path {slug!r}: they are the same system")
    spec_in = {k: v for k, v in body.spec.items() if k != "slug"}
    try:
        async with request.app.state.engine.begin() as conn:
            access = await project_access(conn, user, project)
            _admin(access, "changing")
            held = await system_store.one(conn, access.project_id, slug, lock=True)
            refs = await system_store.refs(conn, access.project_id)
            if (held is None and body.if_revision) or (held is not None and body.if_revision != held.revision):
                if held is None:
                    raise HTTPException(404, f"project {access.name} has no agent system {slug} to replace")
                raise _stale(access, held, body.if_revision, refs)
            spec, problems = checked(spec_in, refs, slug)
            if errors := system_spec.errors(problems):
                detail = [p.as_dict() for p in errors]
                count = f"{len(errors)} error{'s' if len(errors) != 1 else ''}"
                raise Refused(422, f"agent system {slug} has {count}: nothing was written", detail=detail)
            if held is not None and held.spec == spec:
                out = _out(access, held, held.spec, held.revision, refs)
                return SystemWritten(**out.model_dump(), created=False, changed=False)
            revision = await write_spec(conn, user, access, slug, spec, held, refs)
            written = await system_store.one(conn, access.project_id, slug)
            refs = await system_store.refs(conn, access.project_id)
            out = _out(access, written, written.spec, written.revision, refs)
    except Refused as exc:
        return _refusal(request, exc)
    log.info("agent system stored", extra={"project": project, "system": slug, "revision": revision})
    return SystemWritten(**out.model_dump(), created=held is None, changed=True)


@router.delete(
    "/{project}/systems/{slug}",
    response_model=SystemDeleted,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def delete_system(request: Request, project: ProjectName, slug: Slug, user: CurrentUser) -> SystemDeleted:
    """Delete an agent system with its revisions; its profiles, graphs and runs stay."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "deleting")
        held = await system_store.one(conn, access.project_id, slug, lock=True)
        if held is None:
            raise _not_found(access, slug)
        await system_store.remove(conn, held.id)
        await record(conn, user, access, audit.SYSTEM_DELETE, slug, held.revision)
    return SystemDeleted(project=project, deleted=slug)

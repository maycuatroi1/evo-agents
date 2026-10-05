"""Plans of a project: the hub holds them, and git keeps read-only copies (``evo_agents.hub.mirror``).

A plan is a row of ``plans`` (its area, label, body as jsonb, revision and digest) with one row of
``plan_revisions`` per revision, only ever appended. Every write takes the plan's row lock, checks the body against
plan.schema.json and ``plan_semantics`` (repos against the repos registered for the project; a schema error is a
422, a semantic finding a warning in the answer), writes the plan and its revision, and adds one audit row naming
the plan and the revision, never its content, all in one transaction.

- PUT creates a plan, or replaces it when ``if_revision`` is the revision the hub holds; anything else is a 409
  ``revision_conflict`` whose ``current`` is the plan as the hub holds it. A body equal to the one held (same digest
  and label) changes nothing and answers 200, so pushing the same files again is idempotent.
- PATCH sets keys of one item of a section, by index or (for steps) by step id, as ``evo harness step`` does, and
  needs ``if_revision`` too. A client that gets the 409 reads the plan again and retries on the new revision.
- POST .../complete moves the plan to the completed area, only when every step is done.

Reads go through a sink of the project (X-Evo-Sink, by default its hub sink) and follow ``ProjectAccess.visible``;
a plan the caller cannot see answers the same 404 as one that does not exist. Reading needs a grant on the project:
a hub admin without one is answered 403, as by the knowledge graph. GET .../diff compares two revisions line by line
as their git copies read (``evo_agents.hub.plan_diff``). Writes need the writer role, and
the plan's label must stay below the clearance of the project's hub sink (``ProjectAccess.push_label``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from evo_agents.harness import load_schema, plan_body, plan_digest, plan_semantics
from evo_agents.hub.access import has_role
from evo_agents.hub.plan_diff import DEFAULT_CONTEXT, MAX_CONTEXT, plan_diff
from evo_agents.hub.plans import (
    AREAS,
    SECTIONS,
    UPDATABLE,
    PlanProblem,
    PlanTooLarge,
    check_body,
    step_index,
    summarize,
    undone_steps,
    update_item,
    update_summary,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import CODES, ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

PLAN_ID = r"^[a-z0-9][a-z0-9-]{0,99}$"  # plan.schema.json's id, at most 100 characters
PlanId = Annotated[str, Path(pattern=PLAN_ID)]
SINK_HEADER = "X-Evo-Sink"
REVISION_CONFLICT = "revision_conflict"
PLAN_CREATE = "plan.create"
PLAN_PUT = "plan.put"
PLAN_PATCH = "plan.patch"
PLAN_COMPLETE = "plan.complete"
SHOWN_PROBLEMS = 5


class Problem(BaseModel):
    path: str
    message: str


class Conflict(ErrorBody):
    current: dict | None = Field(None, description="the plan as the hub holds it, when the caller may see it")


READ_REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
REFUSALS = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    409: {"model": Conflict},
    413: {"model": ErrorBody},
    422: {"model": ErrorBody},
}

router = APIRouter(prefix="/v1/projects/{project}/plans", tags=["plans"], responses={401: {"model": ErrorBody}})


class PlanSummary(BaseModel):
    plan_id: str
    area: Literal[AREAS]
    revision: int
    digest: str
    title: str | None
    steps_total: int
    steps_done: int
    updated_at: datetime
    updated_by: str


class Plan(BaseModel):
    project: str
    plan_id: str
    area: Literal[AREAS]
    revision: int
    digest: str = Field(description="sha256 of the canonical JSON of the body, as evo_agents.harness.plan_digest")
    label: dict
    body: dict
    created_at: datetime
    updated_at: datetime
    updated_by: str


class Written(Plan):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this; no revision was added")
    warnings: list[Problem] = Field(description="what plan_semantics found; the plan was stored anyway")


class Revision(BaseModel):
    revision: int
    area: Literal[AREAS]
    digest: str
    summary: str
    actor: str
    created_at: datetime


class RevisionBody(Revision):
    label: dict
    body: dict


class PlanDiffLine(BaseModel):
    kind: Literal["context", "added", "removed"]
    old: int | None = Field(description="the line's number in the from revision's text; null for an added line")
    new: int | None = Field(description="the line's number in the to revision's text; null for a removed line")
    text: str


class PlanDiffHunk(BaseModel):
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[PlanDiffLine]


class PlanDiff(BaseModel):
    plan_id: str
    from_revision: Revision
    to_revision: Revision
    context: int
    added: int = Field(description="lines added in all hunks")
    removed: int = Field(description="lines removed in all hunks")
    hunks: list[PlanDiffHunk] = Field(description="empty when the two revisions read the same")


class PlanPut(BaseModel):
    body: dict[str, Any] = Field(description="the plan as its YAML file reads; a hub key in it is ignored")
    area: Literal[AREAS] | None = Field(None, description="where a new plan goes (default active); else its area")
    if_revision: int | None = Field(
        None, ge=0, description="the revision being replaced; leave it out, or 0, to create the plan"
    )
    label: dict | None = Field(None, description="{level, location, integrity}; default: the project's, or the held")


class PlanPatch(BaseModel):
    section: Literal[SECTIONS]
    index: int | None = Field(None, ge=0, description="the item's position in the section")
    step: int | str | None = Field(None, description="for steps, the step's id (or order) instead of an index")
    updates: dict[str, str] = Field(min_length=1, max_length=len(UPDATABLE), description=", ".join(UPDATABLE))
    if_revision: int = Field(ge=1, description="the revision the update was made against")


class PlanComplete(BaseModel):
    if_revision: int | None = Field(None, ge=1)


class PlanError(Exception):
    """A refusal with more than a message: problems as ``detail``, or the held plan with a revision conflict."""

    def __init__(self, status: int, message: str, *, code=None, detail=None, current=None):
        super().__init__(message)
        self.status, self.message, self.code, self.detail, self.current = status, message, code, detail, current


def _refusal(request: Request, exc: PlanError) -> JSONResponse:
    body = Conflict(
        error=exc.code or CODES.get(exc.status, f"http_{exc.status}"),
        message=exc.message,
        request_id=getattr(request.state, "request_id", None),
        detail=exc.detail,
        current=exc.current,
    )
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=exc.status)


def not_found(project: str, plan_id: str) -> str:
    return f"no plan {plan_id} in project {project} that you can see: see `evo-agents hub plan list`"


# Reading


@dataclass(frozen=True)
class Held:
    plan_id: str
    area: str
    label: dict
    body: dict
    revision: int
    digest: str
    created_at: datetime
    updated_at: datetime
    updated_by: str

    def view(self, project: str) -> dict:
        return Plan(project=project, **vars(self)).model_dump(mode="json")


SELECT_PLAN = """
SELECT p.plan_id, p.area, p.label, p.body, p.revision, p.digest, p.created_at, p.updated_at, u.login
  FROM plans p JOIN users u ON u.id = p.updated_by
 WHERE p.project_id = %s AND p.plan_id = %s
"""


async def _held(conn, access: ProjectAccess, plan_id: str, *, lock: bool = False) -> Held | None:
    """The plan as held, its row locked until the transaction ends with ``lock``. The lock is taken on the plan alone:
    a locking query joined to users would, after waiting for another writer, recheck the join against the old
    updater and lose the row. The read after it sees the other writer's commit (a new snapshot per statement)."""
    params = (access.project_id, plan_id)
    if lock:
        locked = "SELECT 1 FROM plans WHERE project_id = %s AND plan_id = %s FOR UPDATE"
        if await (await conn.execute(locked, params)).fetchone() is None:
            return None
    row = await (await conn.execute(SELECT_PLAN, params)).fetchone()
    return Held(*row) if row else None


def _sink(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink (what was pushed to the hub may
    come back from it to anyone whose grant reaches its level)."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def _reader(access: ProjectAccess) -> None:
    """Reading plans needs a grant on the project; only a hub admin without one gets this far (``project_access``
    answers 404 to anyone else without one), and it manages the project without reading what it holds."""
    if access.role is None:
        raise HTTPException(403, f"reading the plans of project {access.name} needs a grant on it")


async def _visible(conn, access: ProjectAccess, plan_id: str, sink: str | None, *, lock: bool = False) -> Held:
    held = await _held(conn, access, plan_id, lock=lock)
    if held is None or not access.visible(held.label, _sink(access, sink)):
        raise HTTPException(404, not_found(access.name, plan_id))
    return held


LIST_PLANS = """
SELECT p.plan_id, p.area, p.label, p.revision, p.digest, p.body ->> 'title', p.updated_at, u.login,
       CASE WHEN jsonb_typeof(p.body -> 'steps') = 'array' THEN jsonb_array_length(p.body -> 'steps') ELSE 0 END,
       CASE WHEN jsonb_typeof(p.body -> 'steps') = 'array'
            THEN (SELECT count(*) FROM jsonb_array_elements(p.body -> 'steps') s
                   WHERE jsonb_typeof(s) = 'object' AND s ->> 'status' = 'done')
            ELSE 0 END
  FROM plans p JOIN users u ON u.id = p.updated_by
 WHERE p.project_id = %(project)s AND (%(area)s::text IS NULL OR p.area = %(area)s)
 ORDER BY p.plan_id
"""


@router.get("", response_model=list[PlanSummary], responses=READ_REFUSALS)
async def list_plans(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    area: Literal[AREAS] | None = None,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> list[PlanSummary]:
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        rows = await (await conn.execute(LIST_PLANS, {"project": access.project_id, "area": area})).fetchall()
    through = _sink(access, sink)
    return [
        PlanSummary(
            plan_id=plan_id,
            area=plan_area,
            revision=revision,
            digest=digest,
            title=title,
            updated_at=updated_at,
            updated_by=login,
            steps_total=total,
            steps_done=done,
        )
        for plan_id, plan_area, label, revision, digest, title, updated_at, login, total, done in rows
        if access.visible(label, through)
    ]


@router.get("/{plan_id}", response_model=Plan, responses=READ_REFUSALS)
async def show(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> dict:
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        held = await _visible(conn, access, plan_id, sink)
    return held.view(project)


REVISIONS = """
SELECT r.revision, r.area, r.label, r.digest, r.summary, u.login, r.created_at, r.body
  FROM plan_revisions r JOIN users u ON u.id = r.actor_id
 WHERE r.project_id = %s AND r.plan_id = %s AND (%s::integer IS NULL OR r.revision = %s)
 ORDER BY r.revision DESC
"""


async def _revisions(conn, access: ProjectAccess, plan_id: str, sink: str | None, revision: int | None = None):
    _reader(access)
    await _visible(conn, access, plan_id, sink)
    cursor = await conn.execute(REVISIONS, (access.project_id, plan_id, revision, revision))
    through = _sink(access, sink)
    return [row for row in await cursor.fetchall() if access.visible(row[2], through)]


@router.get("/{plan_id}/revisions", response_model=list[Revision], responses=READ_REFUSALS)
async def history(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> list[Revision]:
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        rows = await _revisions(conn, access, plan_id, sink)
    return [
        Revision(revision=r, area=a, digest=d, summary=s, actor=actor, created_at=at)
        for r, a, _label, d, s, actor, at, _body in rows
    ]


@router.get("/{plan_id}/revisions/{revision}", response_model=RevisionBody, responses=READ_REFUSALS)
async def show_revision(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    revision: Annotated[int, Path(ge=1)],
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> RevisionBody:
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        rows = await _revisions(conn, access, plan_id, sink, revision)
    if not rows:
        raise HTTPException(404, f"plan {plan_id} of project {project} has no revision {revision} that you can see")
    r, a, label, d, s, actor, at, body = rows[0]
    return RevisionBody(revision=r, area=a, digest=d, summary=s, actor=actor, created_at=at, label=label, body=body)


async def _one_revision(conn, access: ProjectAccess, plan_id: str, sink: str | None, revision: int):
    rows = await _revisions(conn, access, plan_id, sink, revision)
    if not rows:
        raise HTTPException(404, f"plan {plan_id} of project {access.name} has no revision {revision} that you can see")
    r, a, _label, d, s, actor, at, body = rows[0]
    return Revision(revision=r, area=a, digest=d, summary=s, actor=actor, created_at=at), body


@router.get("/{plan_id}/diff", response_model=PlanDiff, responses=READ_REFUSALS)
async def diff(
    request: Request,
    project: ProjectName,
    plan_id: PlanId,
    user: CurrentUser,
    from_revision: Annotated[int, Query(alias="from", ge=1, description="the older revision")],
    to_revision: Annotated[int, Query(alias="to", ge=1, description="the newer revision")],
    context: Annotated[int, Query(ge=0, le=MAX_CONTEXT, description="unchanged lines around a change")] = (
        DEFAULT_CONTEXT
    ),
    sink: Annotated[str | None, Header(alias=SINK_HEADER)] = None,
) -> PlanDiff:
    """The lines that changed from one revision of the plan to another, as their git copies read; 404 when
    either revision is not one the caller can see."""
    async with request.app.state.pool.connection() as conn:
        access = await project_access(conn, user, project)
        older, old_body = await _one_revision(conn, access, plan_id, sink, from_revision)
        newer, new_body = await _one_revision(conn, access, plan_id, sink, to_revision)
    found = await asyncio.to_thread(plan_diff, old_body, new_body, context)
    return PlanDiff(
        plan_id=plan_id,
        from_revision=older,
        to_revision=newer,
        context=context,
        added=found.added,
        removed=found.removed,
        hunks=[
            PlanDiffHunk(
                old_start=h.old_start,
                old_lines=h.old_lines,
                new_start=h.new_start,
                new_lines=h.new_lines,
                lines=[PlanDiffLine(kind=x.kind, old=x.old, new=x.new, text=x.text) for x in h.lines],
            )
            for h in found.hunks
        ],
    )


# Writing


class _Registered:
    """What ``plan_semantics`` asks of a harness, from what the project registered."""

    def __init__(self, names: set[str]):
        self.names = names

    def known_repos(self) -> set[str]:
        return self.names


async def _known_repos(conn, access: ProjectAccess) -> _Registered:
    cursor = await conn.execute("SELECT name FROM project_repos WHERE project_id = %s", (access.project_id,))
    names = {row[0] for row in await cursor.fetchall()}
    row = await (await conn.execute("SELECT harness_path FROM projects WHERE id = %s", (access.project_id,))).fetchone()
    if row and row[0]:
        names.add(PurePosixPath(row[0]).name)  # the harness repo goes by its directory name, as in the loader
    return _Registered(names)


def _schema_checked(body: dict, plan_id: str) -> None:
    """422 (413 for size) when the body is not a plan the hub can keep; before anything is read or written."""
    try:
        check_body(body, plan_id)
    except PlanTooLarge as exc:
        raise PlanError(413, str(exc)) from None
    except PlanProblem as exc:
        raise PlanError(422, f"plan {plan_id} cannot be stored: {exc}") from None
    found = errors(validate(body, load_schema("plan")))
    if found:
        shown = "; ".join(f"{i.path or '<root>'}: {i.message}" for i in found[:SHOWN_PROBLEMS])
        more = f" and {len(found) - SHOWN_PROBLEMS} more" if len(found) > SHOWN_PROBLEMS else ""
        raise PlanError(
            422,
            f"plan {plan_id} does not match plan.schema.json: {shown}{more}; nothing was written",
            detail=[{"path": i.path, "message": i.message} for i in found],
        )


async def _warnings(conn, access: ProjectAccess, body: dict, area: str) -> list[Problem]:
    path = PurePosixPath("plans", area, f"{body['id']}.yaml")
    issues = validate(body, load_schema("plan")) + plan_semantics(body, path, await _known_repos(conn, access))
    return [Problem(path=i.path, message=i.message) for i in issues if i.severity == "warning"]


def _writer(access: ProjectAccess) -> None:
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"pushing to project {access.name} needs the writer role on it")


def _stale(access: ProjectAccess, held: Held, given: int | None) -> PlanError:
    if not given:
        message = (
            f"plan {held.plan_id} is on the hub at revision {held.revision}: pass if_revision {held.revision} to "
            "replace it"
        )
    else:
        message = (
            f"plan {held.plan_id} is at revision {held.revision} on the hub, not {given}: someone changed it since "
            "you read it. Read it again and retry; nothing was written"
        )
    return PlanError(409, message, code=REVISION_CONFLICT, current=held.view(access.name))


async def _store(conn, access, user, held: Held | None, *, area, label, body, summary, action) -> Held | None:
    """Write the next revision of a plan (the first when ``held`` is None); None when another request created the
    plan first. The stored body is read back and must carry the digest of the body given."""
    digest = plan_digest(body)
    revision = 1 if held is None else held.revision + 1
    if held is None:
        cursor = await conn.execute(
            "INSERT INTO plans (project_id, plan_id, area, label, body, revision, digest, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, 1, %s, %s) ON CONFLICT DO NOTHING "
            "RETURNING body, created_at, updated_at",
            (access.project_id, body["id"], area, Jsonb(label), Jsonb(body), digest, user.user_id),
        )
    else:
        cursor = await conn.execute(
            "UPDATE plans SET area = %s, label = %s, body = %s, revision = %s, digest = %s, updated_at = now(), "
            "updated_by = %s WHERE project_id = %s AND plan_id = %s RETURNING body, created_at, updated_at",
            (area, Jsonb(label), Jsonb(body), revision, digest, user.user_id, access.project_id, body["id"]),
        )
    row = await cursor.fetchone()
    if row is None:
        return None
    stored, created_at, updated_at = row
    if plan_digest(stored) != digest:  # a number Postgres does not keep as written, such as 1e400
        raise PlanError(
            422,
            f"plan {body['id']} holds a number that does not survive storage unchanged: write it as a string. "
            "Nothing was written",
        )
    await conn.execute(
        "INSERT INTO plan_revisions (project_id, plan_id, revision, area, label, body, digest, summary, actor_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (access.project_id, body["id"], revision, area, Jsonb(label), Jsonb(body), digest, summary, user.user_id),
    )
    target = f"{access.name}/{body['id']}@{revision}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
    return Held(body["id"], area, label, stored, revision, digest, created_at, updated_at, user.login)


def _written(access, held: Held, *, created: bool, changed: bool, warnings: list[Problem]) -> Written:
    return Written(**held.view(access.name), created=created, changed=changed, warnings=warnings)


def _logged(project: str, held: Held, outcome: str, user) -> None:
    log.info(
        "plan write",
        extra={
            "project": project,
            "plan_id": held.plan_id,
            "revision": held.revision,
            "outcome": outcome,
            "login": user.login,
        },
    )


@router.put("/{plan_id}", response_model=Written, responses=REFUSALS)
async def put(request: Request, project: ProjectName, plan_id: PlanId, payload: PlanPut, user: CurrentUser):
    """Create the plan, or replace the revision ``if_revision`` names with ``body``."""
    body = plan_body(payload.body)
    try:
        _schema_checked(body, plan_id)
        async with request.app.state.pool.connection() as conn:
            access = await project_access(conn, user, project)
            _writer(access)
            given_label = access.push_label(payload.label)
            for _ in range(2):  # a create that loses a race with another create reads the winner's plan
                held = await _held(conn, access, plan_id, lock=True)
                if held is None:
                    if payload.if_revision:
                        raise PlanError(
                            409,
                            f"plan {plan_id} is not on the hub, so there is no revision {payload.if_revision} to "
                            "replace: leave out if_revision to create it",
                            code=REVISION_CONFLICT,
                        )
                    area = payload.area or "active"
                    warnings = await _warnings(conn, access, body, area)
                    stored = await _store(
                        conn,
                        access,
                        user,
                        None,
                        area=area,
                        label=given_label,
                        body=body,
                        summary=summarize(None, body),
                        action=PLAN_CREATE,
                    )
                    if stored is None:
                        continue
                    _logged(project, stored, "created", user)
                    return _written(access, stored, created=True, changed=True, warnings=warnings)
                if not access.visible(held.label, _sink(access, None)):
                    raise PlanError(409, f"plan {plan_id} already exists in project {project}")
                if payload.area and payload.area != held.area:
                    raise PlanError(
                        422,
                        f"plan {plan_id} is {held.area} on the hub, and a put does not move it: "
                        f"`evo-agents hub plan complete` does, and its copy belongs in plans/{held.area}/",
                    )
                label = given_label if payload.label is not None else access.push_label(held.label)
                warnings = await _warnings(conn, access, body, held.area)
                if plan_digest(body) == held.digest and label == held.label:
                    _logged(project, held, "unchanged", user)
                    return _written(access, held, created=False, changed=False, warnings=warnings)
                if payload.if_revision != held.revision:
                    raise _stale(access, held, payload.if_revision)
                stored = await _store(
                    conn,
                    access,
                    user,
                    held,
                    area=held.area,
                    label=label,
                    body=body,
                    summary=summarize(held.body, body) if body != held.body else "changed the label",
                    action=PLAN_PUT,
                )
                _logged(project, stored, "replaced", user)
                return _written(access, stored, created=False, changed=True, warnings=warnings)
            raise PlanError(409, f"plan {plan_id} was created and changed while this request ran; try again")
    except PlanError as exc:
        return _refusal(request, exc)


@router.patch("/{plan_id}", response_model=Written, responses=REFUSALS)
async def patch(request: Request, project: ProjectName, plan_id: PlanId, payload: PlanPatch, user: CurrentUser):
    """Set keys of one item, as ``evo harness step``, ``repo``, ``debt`` and ``question`` do in a file."""
    if (payload.index is None) == (payload.step is None):
        raise HTTPException(422, "name the item with exactly one of index and step")
    if payload.step is not None and payload.section != "steps":
        raise HTTPException(422, "step names an item of the steps section only; give index for other sections")
    try:
        async with request.app.state.pool.connection() as conn:
            access = await project_access(conn, user, project)
            _writer(access)
            stored, changed, warnings = await apply_patch(
                conn,
                access,
                user,
                plan_id,
                payload.section,
                payload.updates,
                if_revision=payload.if_revision,
                index=payload.index,
                step=payload.step,
            )
            return _written(access, stored, created=False, changed=changed, warnings=warnings)
    except PlanError as exc:
        return _refusal(request, exc)


async def apply_patch(
    conn,
    access: ProjectAccess,
    user,
    plan_id: str,
    section: str,
    updates: dict[str, str],
    *,
    if_revision: int,
    index: int | None = None,
    step=None,
) -> tuple[Held, bool, list[Problem]]:
    """PATCH's write, in the caller's transaction, for a ``user`` whose writer role the caller checked: set
    ``updates`` on one item of ``section``, named by ``index`` or (for steps) by ``step``, when ``if_revision`` is
    the revision the hub holds. Returns the plan as held afterwards, whether it changed, and the warnings of
    ``plan_semantics``. Raises PlanError: 409 ``revision_conflict`` for another revision, 422 for an update the plan
    refuses; and HTTPException 404 for a plan ``user`` cannot see, 403 or 422 when its label refuses the push. The
    hub's own writes of a run's step (``evo_agents.hub.server.run_state``) go through here as the dispatcher."""
    held = await _visible(conn, access, plan_id, None, lock=True)
    label = access.push_label(held.label)
    if if_revision != held.revision:
        raise _stale(access, held, if_revision)
    try:
        position = index if step is None else step_index(held.body, step)
        body, old = update_item(held.body, section, position, updates)
    except PlanProblem as exc:
        raise PlanError(422, f"plan {plan_id}: {exc}; nothing was written") from None
    _schema_checked(body, plan_id)
    warnings = await _warnings(conn, access, body, held.area)
    if plan_digest(body) == held.digest:
        _logged(access.name, held, "unchanged", user)
        return held, False, warnings
    summary = update_summary(body, section, position, old, updates)
    stored = await _store(
        conn, access, user, held, area=held.area, label=label, body=body, summary=summary, action=PLAN_PATCH
    )
    _logged(access.name, stored, "patched", user)
    return stored, True, warnings


@router.post("/{plan_id}/complete", response_model=Written, responses=REFUSALS)
async def complete(
    request: Request, project: ProjectName, plan_id: PlanId, user: CurrentUser, payload: PlanComplete | None = None
):
    """Move the plan to the completed area; refused while a step is not done, as evo-cli's complete_plan."""
    given = payload.if_revision if payload else None
    try:
        async with request.app.state.pool.connection() as conn:
            access = await project_access(conn, user, project)
            _writer(access)
            held = await _visible(conn, access, plan_id, None, lock=True)
            label = access.push_label(held.label)
            if given is not None and given != held.revision:
                raise _stale(access, held, given)
            if held.area == "completed":
                _logged(project, held, "unchanged", user)
                return _written(access, held, created=False, changed=False, warnings=[])
            undone = undone_steps(held.body)
            if undone:
                raise PlanError(
                    409,
                    f"plan {plan_id} has steps that are not done: {', '.join(undone)}. A plan is completed once every "
                    "step is done; nothing was written",
                )
            stored = await _store(
                conn,
                access,
                user,
                held,
                area="completed",
                label=label,
                body=held.body,
                summary="completed: moved from active",
                action=PLAN_COMPLETE,
            )
            _logged(project, stored, "completed", user)
            return _written(access, stored, created=False, changed=True, warnings=[])
    except PlanError as exc:
        return _refusal(request, exc)

"""The problems of a project, kept as revisions (plan problem-oriented-hub, criterion a1; the model in
``evo_agents.hub.problems``). Tables problems and problem_revisions, schema 0134.

- GET /v1/projects/{project}/problems: the project's problems as they are now, by rank, for a member with a grant on
  the project; ``?status=`` keeps those of one status. GET .../problems/{ref}: one problem, by its code (P-<number>),
  at its newest revision or at ``?revision=``, with the goals whose newest revision answers it, the claims of the
  project's canvases that link it (plan problem-oriented-hub, criterion a3) and the plans whose body names it
  (criterion a4), each the member sees through the project's hub sink. GET
  .../problems/{ref}/revisions: its revisions, newest first, each with the fields whose value it changed.
- PUT .../problems/{ref}: the problem's next revision, for a writer of the project, made when the project has no such
  problem yet: a new problem takes the next code. The body is the problem whole; ``rank`` and ``owner`` left out keep
  their value, and a new problem goes after the project's open problems and is owned by its writer. A body equal to
  the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision now is 409, and 0
  creates the problem only. POST .../problems/{ref}/close: the next revision, solved or dropped with a reason.
- The agent of a run never writes a problem: @builder proposes one (proposal kind ``problem``,
  ``evo_agents.core_plugins.builder.proposals``), which a writer approves, and the hub writes it through ``write``.

A member without a grant on the project gets 404, as for every route of a project. A metric names a key of kind number
of the project's registry of signals, and has no target; the owner is a member with the writer role or above. An
archived key a revision names already may stay. Writes of one project's problems wait for each other (a
transaction-level advisory lock). Each revision adds an audit row ``problem.set`` naming the problem, the revision and
the fields whose value it changed, never a value.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, problems, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.plans import AREAS
from evo_agents.hub.server import audit
from evo_agents.hub.server import goals as project_goals
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import step_counts
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_signals import KEY_NAME, MAX_KEY_NAME_CHARS

log = logging.getLogger(__name__)

MAX_REVISION = 2**31 - 1
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
OF = "a problem's"  # how the checks shared with the goals name what they check
ProblemRef = Annotated[str, Path(pattern=problems.REF, description="P-<number>")]

router = APIRouter(prefix="/v1/projects", tags=["problems"], responses={401: {"model": ErrorBody}})


class ProblemMetric(BaseModel):
    """How much a problem hurts: a key of the project's signals summed up over a window, as a goal's metric is, with
    no target."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=MAX_KEY_NAME_CHARS,
        pattern=KEY_NAME,
        description="a key of kind number of the project's registry of signals",
    )
    aggregate: Literal[problems.AGGREGATES]
    window_days: int | None = Field(
        None, ge=1, le=problems.MAX_WINDOW_DAYS, description=f"null: {problems.DEFAULT_WINDOW_DAYS}"
    )
    tenant: str | None = Field(
        None, min_length=1, max_length=problems.MAX_TENANT_CHARS, description="one tenant's alone"
    )


class ProblemGoal(BaseModel):
    """A goal of the project whose newest revision answers the problem."""

    code: str = Field(description="G-<number>")
    revision: int
    statement: str
    status: Literal[goals.STATUSES]


class ProjectProblem(BaseModel):
    """A problem of a project at one revision."""

    project: str
    code: str = Field(description="P-<number>, unique in the project and never given again")
    number: int
    revision: int
    statement: str = Field(description="what hurts, before any solution")
    who: str = Field(description="who is hurt")
    when: str = Field(description="when it happens")
    workaround: str = Field(description="how they cope today")
    metric: ProblemMetric | None = Field(description="null: no metric")
    rank: int = Field(description="its place among the project's problems, 1 first")
    owner: str
    status: Literal[problems.STATUSES]
    close_reason: str | None = Field(description="why it was solved or dropped; null while it is open")
    note: str | None = Field(description="what the writer said of this revision")
    created_by: str = Field(description="who wrote this revision")
    created_at: datetime


class ProblemClaim(BaseModel):
    """A claim of a canvas of the project that links the problem: not archived, in a canvas that is not."""

    canvas: str = Field(description="the canvas's slug")
    canvas_name: str = Field(description="the product's name")
    code: str = Field(description="CL-<number>")
    cell: str
    status: str
    confidence: str
    text: str


class ProblemPlan(BaseModel):
    """A plan of the project whose body names the problem in its problems."""

    plan_id: str
    area: Literal[AREAS]
    title: str | None
    status: str | None = Field(description="the status the plan's body gives, if any")
    revision: int
    steps_total: int
    steps_done: int = Field(description="its steps done or descoped")


class ProjectProblemShown(ProjectProblem):
    goals: list[ProblemGoal] = Field(description="the goals whose newest revision answers it, by code")
    claims: list[ProblemClaim] = Field(description="the claims that link it, by canvas and code: those the member sees")
    plans: list[ProblemPlan] = Field(description="the plans that name it, by plan id: those the member sees")


class ProjectProblemWritten(ProjectProblem):
    changed: bool = Field(description="false when the newest revision is this problem already: nothing was written")


def _said_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("says something")
    return value


class ProjectProblemWrite(BaseModel):
    """The next revision of a problem: the whole of it, as its writer wants it."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=problems.MAX_STATEMENT_CHARS, description="what hurts")
    who: str = Field(min_length=1, max_length=problems.MAX_TEXT_CHARS, description="who is hurt")
    when: str = Field(min_length=1, max_length=problems.MAX_TEXT_CHARS, description="when it happens")
    workaround: str = Field(min_length=1, max_length=problems.MAX_TEXT_CHARS, description="how they cope today")
    metric: ProblemMetric | None = None
    rank: int | None = Field(
        None, ge=1, le=problems.MAX_RANK, description="null: kept, or after the project's open problems for a new one"
    )
    owner: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="a member with the writer role or above; null: kept, or the writer for a new problem",
    )
    status: Literal[problems.STATUSES] = problems.OPEN
    close_reason: str | None = Field(
        None,
        min_length=1,
        max_length=problems.MAX_REASON_CHARS,
        description="why it was solved or dropped, for a closed problem and only for one",
    )
    note: str | None = Field(None, max_length=problems.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(
        None, ge=0, description="409 unless the problem is at this revision now; 0: new only"
    )

    @field_validator(*problems.TEXTS)
    @classmethod
    def _said(cls, text: str) -> str:
        return _said_text(text)

    @field_validator("close_reason")
    @classmethod
    def _reason_said(cls, reason: str | None) -> str | None:
        return None if reason is None else _said_text(" ".join(reason.split()))

    @model_validator(mode="after")
    def _reason_of_closed(self):
        if (self.status != problems.OPEN) != (self.close_reason is not None):
            raise ValueError("a solved or dropped problem has a close_reason, and an open one has none")
        return self


class ProjectProblemClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[problems.CLOSED] = Field(description="solved or dropped")
    reason: str = Field(min_length=1, max_length=problems.MAX_REASON_CHARS, description="why")
    note: str | None = Field(None, max_length=problems.MAX_NOTE_CHARS, description="for the history")
    if_revision: int | None = Field(None, ge=1, description="409 unless the problem is at this revision now")

    @field_validator("reason")
    @classmethod
    def _said(cls, reason: str) -> str:
        return _said_text(" ".join(reason.split()))


class ProblemRevision(BaseModel):
    revision: int
    status: Literal[problems.STATUSES]
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the fields whose value differs from the revision before")


# Reading


def _select():
    """A problem at a revision, with its owner's and writer's logins."""
    p, r = tables.problems, tables.problem_revisions
    owner, writer = tables.users.alias("owner"), tables.users.alias("writer")
    return (
        select(
            p.c.id,
            p.c.number,
            r.c.revision,
            r.c.statement,
            r.c.who,
            r.c.when,
            r.c.workaround,
            r.c.metric,
            r.c.rank,
            r.c.owner_id,
            owner.c.login.label("owner"),
            r.c.status,
            r.c.close_reason,
            r.c.note,
            writer.c.login.label("created_by"),
            r.c.created_at,
        )
        .join_from(p, r, r.c.problem_id == p.c.id)
        .join(owner, owner.c.id == r.c.owner_id)
        .join(writer, writer.c.id == r.c.created_by)
    )


async def found(conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None):
    """Problem ``ref`` of the project at ``revision`` (its newest when None); None when there is no such one."""
    p, r = tables.problems, tables.problem_revisions
    at = p.c.revision if revision is None else revision
    number = problems.number_of(ref)
    query = _select().where(p.c.project_id == project_id, p.c.number == number, r.c.revision == at)
    return (await conn.execute(query)).first()


def view(project: str, row) -> ProjectProblem:
    return ProjectProblem(
        project=project,
        code=problems.code(row.number),
        number=row.number,
        revision=row.revision,
        statement=row.statement,
        who=row.who,
        when=row.when,
        workaround=row.workaround,
        metric=row.metric,
        rank=row.rank,
        owner=row.owner,
        status=row.status,
        close_reason=row.close_reason,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def fields_of(row) -> dict:
    """The fields of problem ``row`` as a write names them (``ProjectProblemWrite``)."""
    return {
        "statement": row.statement,
        "who": row.who,
        "when": row.when,
        "workaround": row.workaround,
        "metric": dict(row.metric) if row.metric is not None else None,
        "rank": row.rank,
        "owner": row.owner,
        "status": row.status,
        "close_reason": row.close_reason,
    }


def _stored(row) -> dict:
    """What revision ``row`` holds, as ``_values`` gives it."""
    return {
        "statement": row.statement,
        "who": row.who,
        "when": row.when,
        "workaround": row.workaround,
        "metric": row.metric,
        "rank": row.rank,
        "owner_id": row.owner_id,
        "status": row.status,
        "close_reason": row.close_reason,
    }


async def answered_by(conn: AsyncConnection, problem_id: int) -> list[ProblemGoal]:
    """The goals whose newest revision answers problem ``problem_id``, by code."""
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(g.c.number, r.c.revision, r.c.statement, r.c.status)
        .join_from(g, r, and_(r.c.goal_id == g.c.id, r.c.revision == g.c.revision))
        .where(r.c.problem_id == problem_id)
        .order_by(g.c.number)
    )
    return [
        ProblemGoal(code=goals.code(row.number), revision=row.revision, statement=row.statement, status=row.status)
        for row in await conn.execute(query)
    ]


def _hub_sink(access: ProjectAccess) -> str | None:
    """The sink a read of the problem's claims and plans goes through: the project's hub sink."""
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


async def claimed_by(conn: AsyncConnection, access: ProjectAccess, code: str) -> list[ProblemClaim]:
    """The claims of the project's canvases that link problem ``code``, neither archived nor in an archived canvas, that
    the member sees, by canvas and number."""
    k, c = tables.canvas.claims, tables.canvas.canvases
    query = (
        select(c.c.slug, c.c.name, k.c.number, k.c.cell, k.c.status, k.c.confidence, k.c.text, k.c.label)
        .join_from(k, c, c.c.id == k.c.canvas_id)
        .where(
            k.c.project_id == access.project_id,
            k.c.problems.contains([code]),
            k.c.archived_at.is_(None),
            c.c.archived_at.is_(None),
        )
        .order_by(c.c.slug, k.c.number)
    )
    through = _hub_sink(access)
    return [
        ProblemClaim(
            canvas=row.slug,
            canvas_name=row.name,
            code=f"CL-{row.number}",
            cell=row.cell,
            status=row.status,
            confidence=row.confidence,
            text=row.text,
        )
        for row in await conn.execute(query)
        if through is not None and access.visible(row.label, through)
    ]


async def named_by(conn: AsyncConnection, access: ProjectAccess, code: str) -> list[ProblemPlan]:
    """The plans of the project whose body names problem ``code`` in its problems, that the member sees, by id."""
    plans = tables.plans
    query = (
        select(
            plans.c.plan_id,
            plans.c.area,
            plans.c.label,
            plans.c.revision,
            plans.c.body["title"].astext.label("title"),
            plans.c.body["status"].astext.label("status"),
            *step_counts(plans.c.body),
        )
        .where(plans.c.project_id == access.project_id, plans.c.body["problems"].contains([code]))
        .order_by(plans.c.plan_id)
    )
    through = _hub_sink(access)
    return [
        ProblemPlan(
            plan_id=row.plan_id,
            area=row.area,
            title=row.title,
            status=row.status,
            revision=row.revision,
            steps_total=row.steps_total,
            steps_done=row.steps_done,
        )
        for row in await conn.execute(query)
        if through is not None and access.visible(row.label, through)
    ]


async def _next_number(conn: AsyncConnection, project_id: int) -> int:
    p = tables.problems
    query = select(func.coalesce(func.max(p.c.number), 0)).where(p.c.project_id == project_id)
    return int(await conn.scalar(query)) + 1


async def next_code(conn: AsyncConnection, project_id: int) -> str:
    """The code the project's next problem takes."""
    return problems.code(await _next_number(conn, project_id))


async def _next_rank(conn: AsyncConnection, project_id: int) -> int:
    """After the project's open problems."""
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(func.coalesce(func.max(r.c.rank), 0))
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id, r.c.status == problems.OPEN)
    )
    return min(int(await conn.scalar(query)) + 1, problems.MAX_RANK)


# Writing


def refusal(user: Principal, access: ProjectAccess) -> str | None:
    """Why ``user`` may not write the project's problems; None when they may."""
    if user.scope is not None:
        return "the agent of a run never writes a problem: @builder proposes one, which a writer approves"
    if has_role(access.role, "writer"):
        return None
    held = access.role or "no grant"
    return f"writing a problem of project {access.name} needs the writer role on it; {user.login} holds {held}"


async def _lock(conn: AsyncConnection, project_id: int) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the project's problems."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"problems:{project_id}", 0))))


async def _values(
    conn: AsyncConnection, access: ProjectAccess, body: ProjectProblemWrite, held, writer_id: int
) -> tuple[dict, list[str]]:
    """What the next revision of problem ``held`` (None for a new one) holds once ``body`` is written by
    ``writer_id``, and each thing wrong with it."""
    wrong: list[str] = []
    owner_id = held.owner_id if held is not None else writer_id
    if body.owner is not None and (held is None or body.owner.lower() != held.owner.lower()):
        owner_id, said = await project_goals.owner_of(conn, access, body.owner, OF)
        wrong += said
    metric = body.metric.model_dump(exclude_none=True) if body.metric is not None else None
    if body.metric is not None:
        held_metric = held.metric if held is not None else None
        wrong += await project_goals.metric_problems(conn, access, body.metric.key, held_metric, OF)
    rank = body.rank or (held.rank if held is not None else await _next_rank(conn, access.project_id))
    values = {
        "statement": body.statement,
        "who": body.who,
        "when": body.when,
        "workaround": body.workaround,
        "metric": metric,
        "rank": rank,
        "owner_id": owner_id,
        "status": body.status,
        "close_reason": body.close_reason,
    }
    return values, wrong


async def _number_of_new(conn: AsyncConnection, access: ProjectAccess, ref: str) -> int:
    """The number a new problem named ``ref`` takes: the next one; 422 for another code."""
    following = await _next_number(conn, access.project_id)
    if problems.number_of(ref) != following:
        raise HTTPException(
            422,
            f"the next problem of project {access.name} is {problems.code(following)}, not {ref}; nothing changed",
        )
    return following


async def check(conn: AsyncConnection, access: ProjectAccess, ref: str, body: ProjectProblemWrite) -> list[str]:
    """Each thing wrong with ``body`` as the next revision of problem ``ref``, as ``write`` would find it (an
    HTTPException for a new problem whose code is not the next one); nothing written."""
    held = await found(conn, access.project_id, ref)
    if held is None:
        await _number_of_new(conn, access, ref)
    return (await _values(conn, access, body, held, writer_id=0))[1]  # the writer only owns a new problem by default


async def _insert(conn: AsyncConnection, project_id: int, number: int, revision: int, values: dict, note, writer):
    """Write revision ``revision`` of problem ``number`` of the project, the problem itself with its first."""
    p, r = tables.problems, tables.problem_revisions
    if revision == 1:
        row = {"project_id": project_id, "number": number, "revision": 1}
        problem_id = (await conn.execute(insert(p).values(row).returning(p.c.id))).scalar_one()
    else:
        query = update(p).where(p.c.project_id == project_id, p.c.number == number).values(revision=revision)
        problem_id = (await conn.execute(query.returning(p.c.id))).scalar_one()
    written = {**values, "problem_id": problem_id, "revision": revision, "note": note, "created_by": writer}
    await conn.execute(insert(r).values(written))


async def write(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, ref: str, body: ProjectProblemWrite, *, via: str = ""
) -> tuple[ProjectProblem, bool]:
    """Write ``body`` as the next revision of problem ``ref`` of the project, once it is right (422 naming each thing
    wrong otherwise) and while the problem is at ``body.if_revision`` (409 otherwise); (the problem now, whether a
    revision was written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked ``refusal``."""
    await _lock(conn, access.project_id)
    held = await found(conn, access.project_id, ref)
    now = 0 if held is None else held.revision
    if body.if_revision is not None and body.if_revision != now:
        raise HTTPException(
            409, f"problem {ref} of project {access.name} is at revision {now}, not {body.if_revision}; nothing changed"
        )
    number = held.number if held is not None else await _number_of_new(conn, access, ref)
    values, wrong = await _values(conn, access, body, held, user.user_id)
    if wrong:
        raise HTTPException(422, f"{'; '.join(wrong)}; nothing changed")
    if held is not None and values == _stored(held):
        return view(access.name, held), False
    note = " ".join((body.note or "").split()) or None
    await _insert(conn, access.project_id, number, now + 1, values, note, user.user_id)
    after = await found(conn, access.project_id, problems.code(number))
    keys = problems.changed(fields_of(held) if held is not None else {}, fields_of(after))
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.PROBLEM_SET,
        target=f"{access.name}/problem {problems.code(number)} revision={now + 1} fields={','.join(keys)}{via}",
        project_id=access.project_id,
    )
    log.info("problem written", extra={"project": access.name, "problem": problems.code(number), "revision": now + 1})
    return view(access.name, after), True


def closing(row, status: str, reason: str, note: str | None, if_revision: int | None) -> ProjectProblemWrite:
    """Problem ``row`` closed as ``status`` (solved or dropped) for ``reason``, as a write of its next revision."""
    fields = {**fields_of(row), "status": status, "close_reason": reason}
    return ProjectProblemWrite(**fields, note=note, if_revision=row.revision if if_revision is None else if_revision)


# Routes


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess, changed: bool) -> None:
    """Queue a build of the project's knowledge graph, which holds its problems (``evo_agents.hub.kg_problems``), once
    a write changed one."""
    if changed:
        await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


async def reader(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    """``user``'s access to the project's problems: 404 without a grant (``project_access``), 403 for a hub admin
    without one."""
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the problems of project {access.name} needs a grant on it")
    return access


async def _writer(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    refused = refusal(user, access)
    if refused is not None:
        raise HTTPException(403, refused)
    return access


async def _one(conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None):
    row = await found(conn, access.project_id, ref, revision)
    if row is None:
        at = "" if revision is None else f" revision {revision}"
        raise HTTPException(404, f"project {access.name} has no problem {ref}{at}")
    return row


async def listed(conn: AsyncConnection, access: ProjectAccess, status: str | None = None) -> list[ProjectProblem]:
    """The project's problems as they are now, by rank, then by code; those of ``status`` alone when given."""
    p, r = tables.problems, tables.problem_revisions
    query = _select().where(p.c.project_id == access.project_id, r.c.revision == p.c.revision)
    if status is not None:
        query = query.where(r.c.status == status)
    rows = (await conn.execute(query.order_by(r.c.rank, p.c.number))).all()
    return [view(access.name, row) for row in rows]


async def shown(
    conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None
) -> ProjectProblemShown:
    """Problem ``ref`` at ``revision`` (its newest when None), with the goals that answer it now, and the claims and
    plans that point to it now."""
    row = await _one(conn, access, ref, revision)
    code = problems.code(row.number)
    return ProjectProblemShown(
        **view(access.name, row).model_dump(),
        goals=await answered_by(conn, row.id),
        claims=await claimed_by(conn, access, code),
        plans=await named_by(conn, access, code),
    )


@router.get("/{project}/problems", response_model=list[ProjectProblem], responses=REFUSALS)
async def list_problems(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[Literal[problems.STATUSES] | None, Query(description="those of this status alone")] = None,
) -> list[ProjectProblem]:
    """The project's problems as they are now, by rank, then by code."""
    async with request.app.state.engine.begin() as conn:
        access = await reader(conn, user, project)
        return await listed(conn, access, status)


@router.get("/{project}/problems/{ref}", response_model=ProjectProblemShown, responses=REFUSALS)
async def show_problem(
    request: Request,
    project: ProjectName,
    ref: ProblemRef,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, le=MAX_REVISION, description="an older revision")] = None,
) -> ProjectProblemShown:
    """One problem of the project, at its newest revision or at ``revision``, with the goals that answer it now."""
    async with request.app.state.engine.begin() as conn:
        access = await reader(conn, user, project)
        return await shown(conn, access, ref, revision)


@router.get("/{project}/problems/{ref}/revisions", response_model=list[ProblemRevision], responses=REFUSALS)
async def problem_revisions(
    request: Request, project: ProjectName, ref: ProblemRef, user: CurrentUser
) -> list[ProblemRevision]:
    """Every revision of a problem, the newest first, each with the fields whose value it changed."""
    p, r = tables.problems, tables.problem_revisions
    async with request.app.state.engine.begin() as conn:
        access = await reader(conn, user, project)
        held = await _one(conn, access, ref)
        rows = (await conn.execute(_select().where(p.c.id == held.id).order_by(r.c.revision))).all()
    listed_revisions, before = [], {}
    for row in rows:
        after = fields_of(row)
        listed_revisions.append(
            ProblemRevision(
                revision=row.revision,
                status=row.status,
                note=row.note,
                created_by=row.created_by,
                created_at=row.created_at,
                changed=problems.changed(before, after),
            )
        )
        before = after
    return listed_revisions[::-1]


@router.put("/{project}/problems/{ref}", response_model=ProjectProblemWritten, responses=WRITE_REFUSALS)
async def write_problem(
    request: Request, project: ProjectName, ref: ProblemRef, body: ProjectProblemWrite, user: CurrentUser
) -> ProjectProblemWritten:
    """Write a problem whole as its next revision, made when the project has none of code ``ref``; a writer of the
    project may."""
    async with request.app.state.engine.begin() as conn:
        access = await _writer(conn, user, project)
        written, changed = await write(conn, user, access, ref, body)
        await _graph_changed(request, conn, access, changed)
    return ProjectProblemWritten(**written.model_dump(), changed=changed)


@router.post("/{project}/problems/{ref}/close", response_model=ProjectProblemWritten, responses=WRITE_REFUSALS)
async def close_problem(
    request: Request, project: ProjectName, ref: ProblemRef, body: ProjectProblemClose, user: CurrentUser
) -> ProjectProblemWritten:
    """Close a problem as solved or dropped, for a reason, as its next revision; a writer of the project may."""
    async with request.app.state.engine.begin() as conn:
        access = await _writer(conn, user, project)
        held = await _one(conn, access, ref)
        if held.status != problems.OPEN:
            raise HTTPException(
                409,
                f"problem {ref} of project {access.name} is {held.status} already ({held.close_reason}); nothing "
                "changed",
            )
        closed = closing(held, body.status, body.reason, body.note, body.if_revision)
        written, changed = await write(conn, user, access, ref, closed)
        await _graph_changed(request, conn, access, changed)
    return ProjectProblemWritten(**written.model_dump(), changed=changed)

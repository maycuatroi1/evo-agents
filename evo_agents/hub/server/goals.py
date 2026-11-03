"""The goals of a project, kept as revisions (plan loop-engine, criterion a2; the model in ``evo_agents.hub.goals``).
Tables goals and goal_revisions, schema 0091.

- GET /v1/projects/{project}/goals: the project's goals as they are now, by rank, for a member with a grant on the
  project; ``?status=`` keeps those of one status. GET .../goals/{ref}: one goal, by its code (G-<number>) or its
  alias, at its newest revision or at ``?revision=``. GET .../goals/{ref}/revisions: its revisions, newest first, each
  with the fields whose value it changed.
- PUT .../goals/{ref}: the goal's next revision, for an admin of the project, made when the project has no such goal
  yet: a new goal takes the next code (``G-<number>``), never an alias. The body is the goal whole; ``rank`` and
  ``owner`` left out keep their value, and a new goal goes after the project's other goals and is owned by its writer.
  A body equal to the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision now
  is 409, and 0 creates the goal only. POST .../goals/{ref}/close: the next revision, closed with a reason. A revision
  that changes the statement or the metric makes the open proposals of the Loop's brainstorms of the goal stale
  (``brainstorms.stale_for_goal``): a member no longer judges claims brainstormed for a goal that is no more.
- The agent of a run never writes a goal: @builder proposes one (proposal kind ``goal``,
  ``evo_agents.core_plugins.builder.proposals``), which an admin approves, and the hub writes it through ``write``.

A metric names a key of kind number of the project's registry of signals; a canvas, one of the project's canvases;
a problem (plan problem-oriented-hub, criterion a2), one of the project's problems that is not dropped
(``evo_agents.hub.server.problems``), or an opportunity (plan opportunity-intake) that is not dropped, never both; the
owner, a member with the writer role or above; an SRS document of the project that is not archived (``srs_document``, its
slug) with a heading of it (``srs_heading``) to limit to, which the metric ``srs.backlog.open`` of the hub's own counts the
backlog of (plan srs-backlog-loop; no key of the registry of signals is needed for it). An archived key or canvas, or
a dropped problem, a revision names already may stay. Writes
of one project's goals wait for each other (a transaction-level advisory lock). Each revision adds an audit row
``goal.set`` naming the goal, the revision and the fields whose value it changed, never a value.

The goals of the Curator's charter (``{id, what}``). The Curator reads the project's goals here (``curator_goals``, for
the lens ``product_goals`` of the review run). A goal of the project's newest charter that no goal keeps as its alias
yet becomes a goal when the goals are read (``adopt_charter_goals``; a write does not, so the code a writer read as
the next one stays free): no metric, its id as the alias, its text as the statement, the charter's order as the rank
after the project's other goals, the charter's writer as owner and writer, and an audit row with no actor. Once a goal
of the charter is a goal, ``evo-agents hub goal set`` changes it; the charter's revisions keep their text as they are.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, tables
from evo_agents.hub import opportunities as opportunity_model
from evo_agents.hub import problems as problem_model
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit, brainstorms
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.srs_backlog import BACKLOG_METRIC
from evo_agents.hub.tables_canvas import SLUG
from evo_agents.hub.tables_signals import KEY_NAME, MAX_KEY_NAME_CHARS
from evo_agents.hub.tables_srs import MAX_ITEM_TITLE_CHARS
from evo_agents.hub.tables_srs import SLUG as SRS_SLUG

log = logging.getLogger(__name__)

MAX_REVISION = 2**31 - 1
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
NUMBER_KIND = "number"  # tables_signals.KINDS: the kind of key a metric names
# goals.FIELDS whose change makes the open proposals of the Loop's brainstorms of the goal stale
BRAINSTORMED_FOR = ("statement", "metric")
ALIAS = re.compile(goals.ALIAS)
GoalRef = Annotated[
    str, Path(pattern=goals.REF, description="G-<number>, or the alias of a goal made from the charter")
]

router = APIRouter(prefix="/v1/projects", tags=["goals"], responses={401: {"model": ErrorBody}})


class GoalMetric(BaseModel):
    """How to tell a goal is reached: a key of the project's signals summed up over a window, compared with a target,
    as a test card of the plugin canvas compares one."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=MAX_KEY_NAME_CHARS,
        pattern=KEY_NAME,
        description="a key of kind number of the project's registry of signals",
    )
    aggregate: Literal[goals.AGGREGATES]
    window_days: int | None = Field(
        None, ge=1, le=goals.MAX_WINDOW_DAYS, description=f"null: {goals.DEFAULT_WINDOW_DAYS}"
    )
    comparator: Literal[goals.COMPARATORS]
    target: float = Field(allow_inf_nan=False)
    tenant: str | None = Field(None, min_length=1, max_length=goals.MAX_TENANT_CHARS, description="one tenant's alone")


class ProjectGoal(BaseModel):
    """A goal of a project at one revision."""

    project: str
    code: str = Field(description="G-<number>, unique in the project and never given again")
    number: int
    alias: str | None = Field(description="the id of the charter's goal the hub made it from, which names it too")
    revision: int
    statement: str
    metric: GoalMetric | None = Field(description="null: no metric, so the Curator reads it and nothing else is done")
    deadline: date | None
    canvas: str | None = Field(description="the slug of the canvas whose product it drives")
    problem: str | None = Field(
        description="the code (P-<number>) of the problem of the project it answers; null: none"
    )
    opportunity: str | None = Field(
        default=None,
        description="the code (O-<number>) of the opportunity of the project it answers; null: none. A goal answers "
        "a problem or an opportunity, never both",
    )
    srs_document: str | None = Field(
        default=None,
        description="the slug of the SRS document whose approved use cases the goal delivers; null: none",
    )
    srs_heading: str | None = Field(
        default=None, description="the code or title of a heading of that document the goal is limited to; null: all"
    )
    rank: int = Field(description="its place among the project's goals, 1 first")
    owner: str
    status: Literal[goals.STATUSES]
    close_reason: Literal[goals.CLOSE_REASONS] | None = Field(description="why it is closed; null unless it is")
    note: str | None = Field(description="what the writer said of this revision")
    created_by: str = Field(description="who wrote this revision")
    created_at: datetime


class ProjectGoalWritten(ProjectGoal):
    changed: bool = Field(description="false when the newest revision is this goal already: nothing was written")


class ProjectGoalWrite(BaseModel):
    """The next revision of a goal: the whole of it, as its writer wants it."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=goals.MAX_STATEMENT_CHARS)
    metric: GoalMetric | None = None
    deadline: date | None = None
    canvas: str | None = Field(None, pattern=SLUG, description="the slug of a canvas of the project")
    problem: str | None = Field(
        None,
        pattern=problem_model.REF,
        description="the code (P-<number>) of a problem of the project that is not dropped; null: none",
    )
    opportunity: str | None = Field(
        None,
        pattern=opportunity_model.REF,
        description="the code (O-<number>) of an opportunity of the project that is not dropped; null: none. Not "
        "with a problem",
    )
    srs_document: str | None = Field(
        None,
        pattern=SRS_SLUG,
        description="the slug of a document of the project, not archived, whose approved use cases the goal delivers; "
        f"null: none. The metric {BACKLOG_METRIC} needs one",
    )
    srs_heading: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_ITEM_TITLE_CHARS,
        description="the code or title of a heading of that document to limit the goal to; null: all. Not without a "
        "document",
    )
    rank: int | None = Field(
        None, ge=1, le=goals.MAX_RANK, description="null: kept, or after the project's other goals for a new one"
    )
    owner: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="a member with the writer role or above; null: kept, or the writer for a new goal",
    )
    status: Literal[goals.STATUSES] = goals.ACTIVE
    close_reason: Literal[goals.CLOSE_REASONS] | None = Field(None, description="for a closed goal, and only for one")
    note: str | None = Field(None, max_length=goals.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(None, ge=0, description="409 unless the goal is at this revision now; 0: new only")

    @field_validator("statement")
    @classmethod
    def _said(cls, statement: str) -> str:
        statement = statement.strip()
        if not statement:
            raise ValueError("the statement says something")
        return statement

    @model_validator(mode="after")
    def _reason_of_closed(self):
        if (self.status == goals.CLOSED) != (self.close_reason is not None):
            raise ValueError("a closed goal has a close_reason, and a goal that is not closed has none")
        return self


class ProjectGoalClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal[goals.CLOSE_REASONS]
    note: str | None = Field(None, max_length=goals.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(None, ge=1, description="409 unless the goal is at this revision now")


class GoalRevision(BaseModel):
    revision: int
    status: Literal[goals.STATUSES]
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the fields whose value differs from the revision before")


# Reading


def _select():
    """A goal at a revision, with its owner's and writer's logins, its canvas's slug and its problem's number."""
    g, r, c, pr = tables.goals, tables.goal_revisions, tables.canvas.canvases, tables.problems
    op, sd = tables.opportunities, tables.srs.documents
    owner, writer = tables.users.alias("owner"), tables.users.alias("writer")
    return (
        select(
            g.c.id,
            g.c.number,
            g.c.alias,
            r.c.revision,
            r.c.statement,
            r.c.metric,
            r.c.deadline,
            r.c.canvas_id,
            c.c.slug.label("canvas"),
            r.c.problem_id,
            pr.c.number.label("problem_number"),
            r.c.opportunity_id,
            op.c.number.label("opportunity_number"),
            r.c.srs_document_id,
            sd.c.slug.label("srs_document"),
            r.c.srs_heading,
            r.c.rank,
            r.c.owner_id,
            owner.c.login.label("owner"),
            r.c.status,
            r.c.close_reason,
            r.c.note,
            writer.c.login.label("created_by"),
            r.c.created_at,
        )
        .join_from(g, r, r.c.goal_id == g.c.id)
        .join(owner, owner.c.id == r.c.owner_id)
        .join(writer, writer.c.id == r.c.created_by)
        .outerjoin(c, c.c.id == r.c.canvas_id)
        .outerjoin(pr, pr.c.id == r.c.problem_id)
        .outerjoin(op, op.c.id == r.c.opportunity_id)
        .outerjoin(sd, sd.c.id == r.c.srs_document_id)
    )


def _named(ref: str):
    g = tables.goals
    number = goals.number_of(ref)
    return g.c.number == number if number is not None else g.c.alias == ref


async def found(conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None):
    """Goal ``ref`` of the project at ``revision`` (its newest when None); None when there is no such one."""
    g, r = tables.goals, tables.goal_revisions
    at = g.c.revision if revision is None else revision
    query = _select().where(g.c.project_id == project_id, _named(ref), r.c.revision == at)
    return (await conn.execute(query)).first()


def view(project: str, row) -> ProjectGoal:
    return ProjectGoal(
        project=project,
        code=goals.code(row.number),
        number=row.number,
        alias=row.alias,
        revision=row.revision,
        statement=row.statement,
        metric=row.metric,
        deadline=row.deadline,
        canvas=row.canvas,
        problem=_problem_code(row),
        opportunity=_opportunity_code(row),
        srs_document=row.srs_document,
        srs_heading=row.srs_heading,
        rank=row.rank,
        owner=row.owner,
        status=row.status,
        close_reason=row.close_reason,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _problem_code(row) -> str | None:
    return problem_model.code(row.problem_number) if row.problem_number is not None else None


def _opportunity_code(row) -> str | None:
    return opportunity_model.code(row.opportunity_number) if row.opportunity_number is not None else None


def fields_of(row) -> dict:
    """The fields of goal ``row`` as a write names them (``ProjectGoalWrite``), the deadline as text."""
    return {
        "statement": row.statement,
        "metric": dict(row.metric) if row.metric is not None else None,
        "deadline": row.deadline.isoformat() if row.deadline is not None else None,
        "canvas": row.canvas,
        "problem": _problem_code(row),
        "opportunity": _opportunity_code(row),
        "srs_document": row.srs_document,
        "srs_heading": row.srs_heading,
        "rank": row.rank,
        "owner": row.owner,
        "status": row.status,
        "close_reason": row.close_reason,
    }


def _stored(row) -> dict:
    """What revision ``row`` holds, as ``_values`` gives it."""
    return {
        "statement": row.statement,
        "metric": row.metric,
        "deadline": row.deadline,
        "canvas_id": row.canvas_id,
        "problem_id": row.problem_id,
        "opportunity_id": row.opportunity_id,
        "srs_document_id": row.srs_document_id,
        "srs_heading": row.srs_heading,
        "rank": row.rank,
        "owner_id": row.owner_id,
        "status": row.status,
        "close_reason": row.close_reason,
    }


async def _next_number(conn: AsyncConnection, project_id: int) -> int:
    g = tables.goals
    return (
        int(await conn.scalar(select(func.coalesce(func.max(g.c.number), 0)).where(g.c.project_id == project_id))) + 1
    )


async def next_code(conn: AsyncConnection, project_id: int) -> str:
    """The code the project's next goal takes."""
    return goals.code(await _next_number(conn, project_id))


async def _next_rank(conn: AsyncConnection, project_id: int) -> int:
    """After the project's goals that are not closed."""
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(func.coalesce(func.max(r.c.rank), 0))
        .join_from(g, r, and_(r.c.goal_id == g.c.id, r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id, r.c.status != goals.CLOSED)
    )
    return min(int(await conn.scalar(query)) + 1, goals.MAX_RANK)


# The goals of the Curator's charter


def _charter_goals(body: dict) -> list[tuple[str, str]]:
    """(id, text) of each goal of a charter's body that a goal may keep as its alias, in the charter's order."""
    listed = []
    for goal in body.get("goals") or []:
        alias = goal.get("id") if isinstance(goal, dict) else None
        if isinstance(alias, str) and ALIAS.fullmatch(alias):
            text = str(goal.get("what") or "").strip()[: goals.MAX_STATEMENT_CHARS] or alias  # as long as a goal's
            listed.append((alias, text))
    return listed


async def _missing_aliases(conn: AsyncConnection, project_id: int, wanted: list[tuple[str, str]]) -> list:
    g = tables.goals
    query = select(g.c.alias).where(g.c.project_id == project_id, g.c.alias.in_([alias for alias, _ in wanted]))
    taken = set((await conn.execute(query)).scalars())
    return [(alias, text) for alias, text in wanted if alias not in taken]


async def adopt_charter_goals(conn: AsyncConnection, project_id: int, project: str) -> int:
    """Make a goal of each goal of the project's newest charter that no goal keeps as its alias yet (see the module);
    how many it made."""
    c = tables.charters
    query = select(c.c.revision, c.c.body, c.c.created_by).where(c.c.project_id == project_id)
    charter = (await conn.execute(query.order_by(c.c.revision.desc()).limit(1))).first()
    wanted = _charter_goals(dict(charter.body)) if charter is not None else []
    if not wanted or not await _missing_aliases(conn, project_id, wanted):
        return 0
    await _lock(conn, project_id)
    missing = await _missing_aliases(conn, project_id, wanted)  # another reader may have made them meanwhile
    number, rank = await _next_number(conn, project_id), await _next_rank(conn, project_id)
    for offset, (alias, text) in enumerate(missing):
        values = {
            "statement": text,
            "metric": None,
            "deadline": None,
            "canvas_id": None,
            "problem_id": None,
            "opportunity_id": None,
            "srs_document_id": None,
            "srs_heading": None,
            "rank": min(rank + offset, goals.MAX_RANK),
            "owner_id": charter.created_by,
            "status": goals.ACTIVE,
            "close_reason": None,
        }
        note = f"made from goal {alias} of the Curator's charter, revision {charter.revision}"
        await _insert(conn, project_id, number + offset, alias, 1, values, note, charter.created_by)
        target = f"{project}/goal {goals.code(number + offset)} revision=1 fields=statement,rank,owner,status"
        await audit.record(
            conn,
            actor_id=None,
            token_id=None,
            action=audit.GOAL_SET,
            target=f"{target} charter={charter.revision}",
            project_id=project_id,
        )
    log.info("goals made from the charter", extra={"project": project, "goals": len(missing)})
    return len(missing)


async def active_goals(conn: AsyncConnection, project_id: int, project: str) -> list[ProjectGoal]:
    """The project's active goals at their newest revision, by rank then code; the goals of its charter made goals
    first."""
    await adopt_charter_goals(conn, project_id, project)
    g, r = tables.goals, tables.goal_revisions
    query = _select().where(g.c.project_id == project_id, r.c.revision == g.c.revision, r.c.status == goals.ACTIVE)
    rows = (await conn.execute(query.order_by(r.c.rank, g.c.number))).all()
    return [view(project, row) for row in rows]


async def curator_goals(conn: AsyncConnection, project_id: int, project: str) -> list[dict]:
    """The project's active goals, by rank, as the prompt of the Curator's review run lists them
    (``goals.curator_line``); the goals of its charter made goals first."""
    shown = await active_goals(conn, project_id, project)
    return [goals.curator_line(goal.model_dump(mode="json")) for goal in shown]


# Writing


def refusal(user: Principal, access: ProjectAccess) -> str | None:
    """Why ``user`` may not write the project's goals; None when they may."""
    if user.acts_for_run is not None:
        return "the agent of a run never writes a goal: @builder proposes one, which an admin approves"
    if has_role(access.role, "admin"):
        return None
    held = access.role or "no grant"
    return f"writing a goal of project {access.name} needs the admin role on it; {user.login} holds {held}"


async def _lock(conn: AsyncConnection, project_id: int) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the project's goals."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"goals:{project_id}", 0))))


async def owner_of(
    conn: AsyncConnection, access: ProjectAccess, login: str, of: str = "a goal's"
) -> tuple[int | None, list[str]]:
    """The id of member ``login`` as the owner of a goal (or of what ``of`` names), and why they cannot be one."""
    u, gr = tables.users, tables.grants
    query = (
        select(u.c.id, gr.c.role)
        .join_from(u, gr, and_(gr.c.user_id == u.c.id, gr.c.project_id == access.project_id), isouter=True)
        .where(func.lower(u.c.login) == login.lower())
    )
    row = (await conn.execute(query)).first()
    if row is not None and has_role(row.role, "writer"):
        return row.id, []
    held = row.role if row is not None and row.role else "no grant"
    return None, [f"owner: {login} holds {held} on project {access.name}; {of} owner holds the writer role or above"]


async def _canvas(conn: AsyncConnection, access: ProjectAccess, slug: str, held: int | None) -> tuple[int | None, list]:
    c = tables.canvas.canvases
    query = select(c.c.id, c.c.archived_at).where(c.c.project_id == access.project_id, c.c.slug == slug)
    row = (await conn.execute(query)).first()
    if row is None:
        return None, [f"canvas: project {access.name} has no canvas {slug}"]
    if row.archived_at is not None and row.id != held:
        return None, [f"canvas: canvas {slug} is archived"]
    return row.id, []


async def metric_problems(
    conn: AsyncConnection, access: ProjectAccess, key: str, held: dict | None, of: str = "a goal's"
) -> list[str]:
    """Why ``key`` cannot be the key of the metric of a goal (or of what ``of`` names) whose metric is ``held`` now."""
    k = tables.signal_keys
    query = select(k.c.kind, k.c.archived_at).where(k.c.project_id == access.project_id, k.c.name == key)
    row = (await conn.execute(query)).first()
    if row is None:
        return [
            f"metric.key: the registry of project {access.name} has no key {key}: an admin adds it with "
            "`evo-agents hub signal keys add`"
        ]
    if row.kind != NUMBER_KIND:
        return [f"metric.key: {key} is a key of kind {row.kind}; {of} metric is a key of kind number"]
    if row.archived_at is not None and (held or {}).get("key") != key:
        return [f"metric.key: {key} is archived"]
    return []


async def _problem(conn: AsyncConnection, access: ProjectAccess, ref: str, held: int | None) -> tuple[int | None, list]:
    """The id of problem ``ref`` of the project, for a goal whose problem is ``held`` now, and why it cannot be."""
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(p.c.id, r.c.status)
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == access.project_id, p.c.number == problem_model.number_of(ref))
    )
    row = (await conn.execute(query)).first()
    if row is None:
        return None, [f"problem: project {access.name} has no problem {ref}: see `evo-agents hub problem list`"]
    if row.status == problem_model.DROPPED and row.id != held:
        return None, [f"problem: problem {ref} is dropped; a goal answers a problem that is open or solved"]
    return row.id, []


async def _opportunity(
    conn: AsyncConnection, access: ProjectAccess, ref: str, held: int | None
) -> tuple[int | None, list]:
    """The id of opportunity ``ref`` of the project, for a goal whose opportunity is ``held`` now, and why it cannot
    be."""
    o, r = tables.opportunities, tables.opportunity_revisions
    query = (
        select(o.c.id, r.c.status)
        .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
        .where(o.c.project_id == access.project_id, o.c.number == opportunity_model.number_of(ref))
    )
    row = (await conn.execute(query)).first()
    if row is None:
        return None, [
            f"opportunity: project {access.name} has no opportunity {ref}: see `evo-agents hub opportunity list`"
        ]
    if row.status == opportunity_model.DROPPED and row.id != held:
        return None, [f"opportunity: opportunity {ref} is dropped; a goal answers one that is not"]
    return row.id, []


async def _srs_document(
    conn: AsyncConnection, access: ProjectAccess, slug: str, held: int | None
) -> tuple[int | None, list]:
    """The id of SRS document ``slug`` of the project, for a goal whose document is ``held`` now, and why it cannot
    be."""
    d = tables.srs.documents
    query = select(d.c.id, d.c.archived_at).where(d.c.project_id == access.project_id, d.c.slug == slug)
    row = (await conn.execute(query)).first()
    if row is None:
        return None, [f"srs_document: project {access.name} has no SRS document {slug}"]
    if row.archived_at is not None and row.id != held:
        return None, [f"srs_document: document {slug} is archived"]
    return row.id, []


async def _values(
    conn: AsyncConnection, access: ProjectAccess, body: ProjectGoalWrite, held, writer_id: int
) -> tuple[dict, list[str]]:
    """What the next revision of goal ``held`` (None for a new one) holds once ``body`` is written by ``writer_id``,
    and each problem with it."""
    problems: list[str] = []
    owner_id = held.owner_id if held is not None else writer_id
    if body.owner is not None and (held is None or body.owner.lower() != held.owner.lower()):
        owner_id, said = await owner_of(conn, access, body.owner)
        problems += said
    canvas_id = None
    if body.canvas is not None:
        canvas_id, said = await _canvas(conn, access, body.canvas, held.canvas_id if held is not None else None)
        problems += said
    problem_id = None
    if body.problem is not None:
        problem_id, said = await _problem(conn, access, body.problem, held.problem_id if held is not None else None)
        problems += said
    opportunity_id = None
    if body.opportunity is not None:
        if body.problem is not None:
            problems.append("opportunity: a goal answers a problem or an opportunity, not both")
        else:
            opportunity_id, said = await _opportunity(
                conn, access, body.opportunity, held.opportunity_id if held is not None else None
            )
            problems += said
    srs_document_id = None
    if body.srs_document is not None:
        srs_document_id, said = await _srs_document(
            conn, access, body.srs_document, held.srs_document_id if held is not None else None
        )
        problems += said
    if body.srs_heading is not None and body.srs_document is None:
        problems.append("srs_heading: a heading belongs to the SRS document the goal aims at; name srs_document")
    metric = body.metric.model_dump(exclude_none=True) if body.metric is not None else None
    if body.metric is not None and body.metric.key == BACKLOG_METRIC:  # the hub computes it: no key of the registry
        if body.srs_document is None:
            problems.append(f"metric.key: {BACKLOG_METRIC} counts the backlog of an SRS document; name srs_document")
    elif body.metric is not None:
        problems += await metric_problems(conn, access, body.metric.key, held.metric if held is not None else None)
    rank = body.rank or (held.rank if held is not None else await _next_rank(conn, access.project_id))
    values = {
        "statement": body.statement,
        "metric": metric,
        "deadline": body.deadline,
        "canvas_id": canvas_id,
        "problem_id": problem_id,
        "opportunity_id": opportunity_id,
        "srs_document_id": srs_document_id,
        "srs_heading": body.srs_heading,
        "rank": rank,
        "owner_id": owner_id,
        "status": body.status,
        "close_reason": body.close_reason,
    }
    return values, problems


async def _number_of_new(conn: AsyncConnection, access: ProjectAccess, ref: str) -> int:
    """The number a new goal named ``ref`` takes: the next one; 404 for an alias, 422 for another code."""
    following = await _next_number(conn, access.project_id)
    number = goals.number_of(ref)
    if number is None:
        raise HTTPException(
            404, f"project {access.name} has no goal {ref}; a new goal takes the code {goals.code(following)}"
        )
    if number != following:
        raise HTTPException(
            422, f"the next goal of project {access.name} is {goals.code(following)}, not {ref}; nothing changed"
        )
    return number


async def check(conn: AsyncConnection, access: ProjectAccess, ref: str, body: ProjectGoalWrite) -> list[str]:
    """Each problem with ``body`` as the next revision of goal ``ref``, as ``write`` would find it (an HTTPException
    for a new goal whose code is not the next one); nothing written."""
    held = await found(conn, access.project_id, ref)
    if held is None:
        await _number_of_new(conn, access, ref)
    return (await _values(conn, access, body, held, writer_id=0))[1]  # the writer only owns a new goal by default


async def _insert(
    conn: AsyncConnection, project_id: int, number: int, alias: str | None, revision: int, values: dict, note, writer
) -> int:
    """Write revision ``revision`` of goal ``number`` of the project, the goal itself with its first; the goal's id."""
    g, r = tables.goals, tables.goal_revisions
    if revision == 1:
        row = {"project_id": project_id, "number": number, "alias": alias, "revision": 1}
        goal_id = (await conn.execute(insert(g).values(row).returning(g.c.id))).scalar_one()
    else:
        query = update(g).where(g.c.project_id == project_id, g.c.number == number).values(revision=revision)
        goal_id = (await conn.execute(query.returning(g.c.id))).scalar_one()
    written = {**values, "goal_id": goal_id, "revision": revision, "note": note, "created_by": writer}
    await conn.execute(insert(r).values(written))
    return goal_id


async def write(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, ref: str, body: ProjectGoalWrite, *, via: str = ""
) -> tuple[ProjectGoal, bool]:
    """Write ``body`` as the next revision of goal ``ref`` of the project, once it is right (422 naming each problem
    otherwise) and while the goal is at ``body.if_revision`` (409 otherwise); (the goal now, whether a revision was
    written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked ``refusal``."""
    await _lock(conn, access.project_id)
    held = await found(conn, access.project_id, ref)
    now = 0 if held is None else held.revision
    if body.if_revision is not None and body.if_revision != now:
        raise HTTPException(
            409, f"goal {ref} of project {access.name} is at revision {now}, not {body.if_revision}; nothing changed"
        )
    number = held.number if held is not None else await _number_of_new(conn, access, ref)
    values, problems = await _values(conn, access, body, held, user.user_id)
    if problems:
        raise HTTPException(422, f"{'; '.join(problems)}; nothing changed")
    if held is not None and values == _stored(held):
        return view(access.name, held), False
    note = " ".join((body.note or "").split()) or None
    alias = held.alias if held is not None else None
    await _insert(conn, access.project_id, number, alias, now + 1, values, note, user.user_id)
    after = await found(conn, access.project_id, goals.code(number))
    keys = goals.changed(fields_of(held) if held is not None else {}, fields_of(after))
    aimed = [name for name in BRAINSTORMED_FOR if name in keys]
    stale = await brainstorms.stale_for_goal(conn, held.id) if held is not None and aimed else 0
    if stale:
        extra = {"project": access.name, "goal": goals.code(number), "revision": now + 1, "changed": aimed}
        log.info("brainstorm proposals stale", extra={**extra, "stale": stale})
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.GOAL_SET,
        target=f"{access.name}/goal {goals.code(number)} revision={now + 1} fields={','.join(keys)}{via}",
        project_id=access.project_id,
    )
    log.info("goal written", extra={"project": access.name, "goal": goals.code(number), "revision": now + 1})
    return view(access.name, after), True


def closing(row, reason: str, note: str | None, if_revision: int | None) -> ProjectGoalWrite:
    """Goal ``row`` closed for ``reason``, as a write of its next revision."""
    fields = {**fields_of(row), "status": goals.CLOSED, "close_reason": reason}
    return ProjectGoalWrite(**fields, note=note, if_revision=row.revision if if_revision is None else if_revision)


# Routes


async def _reader(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the goals of project {access.name} needs a grant on it")
    await adopt_charter_goals(conn, access.project_id, access.name)
    return access


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess, changed: bool) -> None:
    """Queue a build of the project's knowledge graph, which holds the goals that answer a problem
    (``evo_agents.hub.kg_problems``), once a write changed a goal."""
    if changed:
        await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


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
        raise HTTPException(404, f"project {access.name} has no goal {ref}{at}")
    return row


@router.get("/{project}/goals", response_model=list[ProjectGoal], responses=REFUSALS)
async def list_goals(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[Literal[goals.STATUSES] | None, Query(description="those of this status alone")] = None,
) -> list[ProjectGoal]:
    """The project's goals as they are now, by rank, then by code."""
    g, r = tables.goals, tables.goal_revisions
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        query = _select().where(g.c.project_id == access.project_id, r.c.revision == g.c.revision)
        if status is not None:
            query = query.where(r.c.status == status)
        rows = (await conn.execute(query.order_by(r.c.rank, g.c.number))).all()
    return [view(access.name, row) for row in rows]


@router.get("/{project}/goals/{ref}", response_model=ProjectGoal, responses=REFUSALS)
async def show_goal(
    request: Request,
    project: ProjectName,
    ref: GoalRef,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, le=MAX_REVISION, description="an older revision")] = None,
) -> ProjectGoal:
    """One goal of the project, at its newest revision or at ``revision``."""
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        return view(access.name, await _one(conn, access, ref, revision))


@router.get("/{project}/goals/{ref}/revisions", response_model=list[GoalRevision], responses=REFUSALS)
async def goal_revisions(request: Request, project: ProjectName, ref: GoalRef, user: CurrentUser) -> list[GoalRevision]:
    """Every revision of a goal, the newest first, each with the fields whose value it changed."""
    g, r = tables.goals, tables.goal_revisions
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        held = await _one(conn, access, ref)
        query = _select().where(g.c.id == held.id).order_by(r.c.revision)
        rows = (await conn.execute(query)).all()
    listed, before = [], {}
    for row in rows:
        after = fields_of(row)
        listed.append(
            GoalRevision(
                revision=row.revision,
                status=row.status,
                note=row.note,
                created_by=row.created_by,
                created_at=row.created_at,
                changed=goals.changed(before, after),
            )
        )
        before = after
    return listed[::-1]


@router.put("/{project}/goals/{ref}", response_model=ProjectGoalWritten, responses=WRITE_REFUSALS)
async def write_goal(
    request: Request, project: ProjectName, ref: GoalRef, body: ProjectGoalWrite, user: CurrentUser
) -> ProjectGoalWritten:
    """Write a goal whole as its next revision, made when the project has none of code ``ref``; an admin of the
    project alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await _writer(conn, user, project)
        shown, changed = await write(conn, user, access, ref, body)
        await _graph_changed(request, conn, access, changed)
    return ProjectGoalWritten(**shown.model_dump(), changed=changed)


@router.post("/{project}/goals/{ref}/close", response_model=ProjectGoalWritten, responses=WRITE_REFUSALS)
async def close_goal(
    request: Request, project: ProjectName, ref: GoalRef, body: ProjectGoalClose, user: CurrentUser
) -> ProjectGoalWritten:
    """Close a goal for a reason, as its next revision; an admin of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await _writer(conn, user, project)
        await adopt_charter_goals(conn, access.project_id, access.name)
        held = await _one(conn, access, ref)
        if held.status == goals.CLOSED:
            raise HTTPException(
                409, f"goal {ref} of project {access.name} is closed already ({held.close_reason}); nothing changed"
            )
        shown, changed = await write(conn, user, access, ref, closing(held, body.reason, body.note, body.if_revision))
        await _graph_changed(request, conn, access, changed)
    return ProjectGoalWritten(**shown.model_dump(), changed=changed)

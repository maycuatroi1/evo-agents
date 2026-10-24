"""The opportunities of a project, kept as revisions (plan opportunity-intake, criterion a1; the model in
``evo_agents.hub.opportunities``). Tables opportunities, opportunity_revisions and opportunity_answers, schema 0143.

- GET /v1/projects/{project}/opportunities: the project's opportunities as they are now, by rank, for a member with a
  grant on the project; ``?status=`` keeps those of one status. GET .../opportunities/{ref}: one opportunity, by its
  code (O-<number>), at its newest revision or at ``?revision=``, with the count of its lines of evidence and of the
  independent sources they come from. GET .../opportunities/{ref}/revisions: its revisions, newest first, each with the
  fields whose value it changed.
- PUT .../opportunities/{ref}: the opportunity's next revision, for a writer of the project, made when the project has
  no such opportunity yet: a new one takes the next code. The body is the opportunity whole; ``rank`` and ``owner``
  left out keep their value, and a new opportunity goes after the project's open ones and is owned by its writer. A
  body equal to the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision now is
  409, and 0 creates the opportunity only. POST .../opportunities/{ref}/close: the next revision, captured, missed or
  dropped with a reason.
- The agent of a run never writes an opportunity: @builder proposes one (proposal kind ``opportunity``,
  ``evo_agents.core_plugins.builder``), which a writer approves, and the hub writes it through ``write``.

``answers`` lists the problems the opportunity answers, at most 20: ``P-3`` is a problem of its own project, and
``<project>/P-3`` one of another project. A code of another project is accepted only while the writer holds a grant on
that project, the problem exists and is not dropped (422 naming the code otherwise); a code the opportunity holds
already may stay. A reader sees the codes of the projects they hold a grant on, and ``hidden_answers`` counts the
rest, without their code or content.

A member without a grant on the project gets 404, as for every route of a project. A metric names a key of kind number
of the project's registry of signals, and has no target; the owner is a member with the writer role or above. Writes of
one project's opportunities wait for each other (a transaction-level advisory lock). Each revision adds an audit row
``opportunity.set`` naming the opportunity, the revision and the fields whose value it changed, never a value.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from types import SimpleNamespace
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import opportunities, problems, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server import goals as project_goals
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.opportunity_deadlines import closing_filter
from evo_agents.hub.server.problems import ProblemMetric
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_REVISION = 2**31 - 1
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
OF = "an opportunity's"  # how the checks shared with the goals name what they check
OpportunityRef = Annotated[str, Path(pattern=opportunities.REF, description="O-<number>")]
AnswerCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        max_length=120,
        pattern=r"^(?:[a-z0-9][a-z0-9-]{0,99}/)?P-[1-9][0-9]{0,8}$",
    ),
]

router = APIRouter(prefix="/v1/projects", tags=["opportunities"], responses={401: {"model": ErrorBody}})


class ProjectOpportunity(BaseModel):
    """An opportunity of a project at one revision."""

    project: str
    code: str = Field(description="O-<number>, unique in the project and never given again")
    number: int
    revision: int
    statement: str = Field(description="what the project could win")
    beneficiary: str = Field(description="who benefits")
    leverage: str = Field(description="what makes it reachable now")
    window_until: date | None = Field(description="the date after which it is gone; null: none known")
    window_note: str | None = Field(description="what the window is")
    answers: list[str] = Field(
        description="the problems it answers that the reader sees: P-<n>, or <project>/P-<n> for another project's"
    )
    hidden_answers: int = Field(
        description="the problems of other projects it answers that the reader holds no grant on"
    )
    metric: ProblemMetric | None = Field(description="null: no metric")
    rank: int = Field(description="its place among the project's opportunities, 1 first")
    owner: str
    status: Literal[opportunities.STATUSES]
    close_reason: str | None = Field(description="why it was captured, missed or dropped; null while it is open")
    note: str | None = Field(description="what the writer said of this revision")
    created_by: str = Field(description="who wrote this revision")
    created_at: datetime


class ProjectOpportunityShown(ProjectOpportunity):
    evidence_count: int = Field(description="the lines of evidence of the opportunity")
    source_count: int = Field(
        description="the independent sources those lines come from: a source is a kind and an address"
    )


class ProjectOpportunityWritten(ProjectOpportunity):
    changed: bool = Field(description="false when the newest revision is this opportunity already: nothing was written")


def _said_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("says something")
    return value


class ProjectOpportunityWrite(BaseModel):
    """The next revision of an opportunity: the whole of it, as its writer wants it."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=opportunities.MAX_STATEMENT_CHARS, description="what could be won")
    beneficiary: str = Field(min_length=1, max_length=opportunities.MAX_TEXT_CHARS, description="who benefits")
    leverage: str = Field(
        min_length=1, max_length=opportunities.MAX_TEXT_CHARS, description="what makes it reachable now"
    )
    window_until: date | None = Field(None, description="the date after which it is gone")
    window_note: str | None = Field(None, min_length=1, max_length=opportunities.MAX_TEXT_CHARS)
    answers: list[AnswerCode] = Field(
        default_factory=list,
        max_length=opportunities.MAX_ANSWERS,
        description="the problems it answers: P-3, or <project>/P-3 for another project's; the whole list",
    )
    metric: ProblemMetric | None = None
    rank: int | None = Field(
        None,
        ge=1,
        le=opportunities.MAX_RANK,
        description="null: kept, or after the project's open opportunities for a new one",
    )
    owner: str | None = Field(
        None,
        pattern=LOGIN_NAME,
        description="a member with the writer role or above; null: kept, or the writer for a new opportunity",
    )
    status: Literal[opportunities.STATUSES] = opportunities.OPEN
    close_reason: str | None = Field(
        None,
        min_length=1,
        max_length=opportunities.MAX_REASON_CHARS,
        description="why it was captured, missed or dropped, for a closed opportunity and only for one",
    )
    note: str | None = Field(None, max_length=opportunities.MAX_NOTE_CHARS, description="why, for the history")
    if_revision: int | None = Field(
        None, ge=0, description="409 unless the opportunity is at this revision now; 0: new only"
    )

    @field_validator(*opportunities.TEXTS)
    @classmethod
    def _said(cls, text: str) -> str:
        return _said_text(text)

    @field_validator("window_note")
    @classmethod
    def _note_said(cls, text: str | None) -> str | None:
        return None if text is None else _said_text(text)

    @field_validator("close_reason")
    @classmethod
    def _reason_said(cls, reason: str | None) -> str | None:
        return None if reason is None else _said_text(" ".join(reason.split()))

    @model_validator(mode="after")
    def _reason_of_closed(self):
        if (self.status != opportunities.OPEN) != (self.close_reason is not None):
            raise ValueError("a closed opportunity has a close_reason, and an open one has none")
        return self


class ProjectOpportunityClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[opportunities.CLOSED] = Field(description="captured, missed or dropped")
    reason: str = Field(min_length=1, max_length=opportunities.MAX_REASON_CHARS, description="why")
    note: str | None = Field(None, max_length=opportunities.MAX_NOTE_CHARS, description="for the history")
    if_revision: int | None = Field(None, ge=1, description="409 unless the opportunity is at this revision now")

    @field_validator("reason")
    @classmethod
    def _said(cls, reason: str) -> str:
        return _said_text(" ".join(reason.split()))


class OpportunityRevision(BaseModel):
    revision: int
    status: Literal[opportunities.STATUSES]
    note: str | None
    created_by: str
    created_at: datetime
    changed: list[str] = Field(description="the fields whose value differs from the revision before")


class Answer(NamedTuple):
    """A problem a revision answers: where it lives and its number."""

    problem_id: int
    project_id: int
    project: str
    number: int

    def text(self, own: int) -> str:
        return opportunities.answer_text(None if self.project_id == own else self.project, self.number)


# Reading


def _select():
    """An opportunity at a revision, with its owner's and writer's logins."""
    o, r = tables.opportunities, tables.opportunity_revisions
    owner, writer = tables.users.alias("owner"), tables.users.alias("writer")
    return (
        select(
            o.c.id,
            o.c.number,
            r.c.revision,
            r.c.statement,
            r.c.beneficiary,
            r.c.leverage,
            r.c.window_until,
            r.c.window_note,
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
        .join_from(o, r, r.c.opportunity_id == o.c.id)
        .join(owner, owner.c.id == r.c.owner_id)
        .join(writer, writer.c.id == r.c.created_by)
    )


async def answers_of(conn: AsyncConnection, opportunity_id: int, revision: int) -> list[Answer]:
    """The problems revision ``revision`` of the opportunity answers, in the order the writer gave them."""
    a, p, j = tables.opportunity_answers, tables.problems, tables.projects
    query = (
        select(p.c.id, j.c.id.label("project_id"), j.c.name, p.c.number)
        .join_from(a, p, p.c.id == a.c.problem_id)
        .join(j, j.c.id == p.c.project_id)
        .where(a.c.opportunity_id == opportunity_id, a.c.revision == revision)
        .order_by(a.c.position)
    )
    return [Answer(row.id, row.project_id, row.name, row.number) for row in await conn.execute(query)]


async def found(conn: AsyncConnection, project_id: int, ref: str, revision: int | None = None):
    """Opportunity ``ref`` of the project at ``revision`` (its newest when None), its row with ``answers``; None when
    there is no such one."""
    o = tables.opportunities
    at = o.c.revision if revision is None else revision
    number = opportunities.number_of(ref)
    query = _select().where(
        o.c.project_id == project_id, o.c.number == number, tables.opportunity_revisions.c.revision == at
    )
    row = (await conn.execute(query)).first()
    if row is None:
        return None
    return SimpleNamespace(**row._mapping, answers=await answers_of(conn, row.id, row.revision))


class Sight:
    """Which projects ``user`` holds a grant on, asked once each, for the answers of one read."""

    def __init__(self, conn: AsyncConnection, user: Principal, access: ProjectAccess):
        self.conn, self.user, self.access = conn, user, access
        self.seen: dict[str, bool] = {}

    async def sees(self, answer: Answer) -> bool:
        if answer.project_id == self.access.project_id:
            return True
        if answer.project not in self.seen:
            try:
                other = await project_access(self.conn, self.user, answer.project)
                self.seen[answer.project] = other.role is not None
            except HTTPException:
                self.seen[answer.project] = False
        return self.seen[answer.project]


async def view(conn: AsyncConnection, sight: Sight, row) -> ProjectOpportunity:
    """Opportunity ``row`` (from ``found``) as the reader of ``sight`` sees it."""
    shown = [answer.text(sight.access.project_id) for answer in row.answers if await sight.sees(answer)]
    return ProjectOpportunity(
        project=sight.access.name,
        code=opportunities.code(row.number),
        number=row.number,
        revision=row.revision,
        statement=row.statement,
        beneficiary=row.beneficiary,
        leverage=row.leverage,
        window_until=row.window_until,
        window_note=row.window_note,
        answers=shown,
        hidden_answers=len(row.answers) - len(shown),
        metric=row.metric,
        rank=row.rank,
        owner=row.owner,
        status=row.status,
        close_reason=row.close_reason,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def fields_of(row, own: int) -> dict:
    """The fields of opportunity ``row`` (from ``found``) as a write names them (``ProjectOpportunityWrite``)."""
    return {
        "statement": row.statement,
        "beneficiary": row.beneficiary,
        "leverage": row.leverage,
        "window_until": row.window_until.isoformat() if row.window_until is not None else None,
        "window_note": row.window_note,
        "answers": [answer.text(own) for answer in row.answers],
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
        "beneficiary": row.beneficiary,
        "leverage": row.leverage,
        "window_until": row.window_until,
        "window_note": row.window_note,
        "answers": [answer.problem_id for answer in row.answers],
        "metric": row.metric,
        "rank": row.rank,
        "owner_id": row.owner_id,
        "status": row.status,
        "close_reason": row.close_reason,
    }


async def evidence_counts(conn: AsyncConnection, opportunity_id: int) -> tuple[int, int]:
    """(lines of evidence of the opportunity, the independent sources they come from)."""
    e = tables.opportunity_evidence
    rows = (
        await conn.execute(select(e.c.source_kind, e.c.source_uri).where(e.c.opportunity_id == opportunity_id))
    ).all()
    return len(rows), problems.sources_of([row._mapping for row in rows])


async def _next_number(conn: AsyncConnection, project_id: int) -> int:
    o = tables.opportunities
    query = select(func.coalesce(func.max(o.c.number), 0)).where(o.c.project_id == project_id)
    return int(await conn.scalar(query)) + 1


async def next_code(conn: AsyncConnection, project_id: int) -> str:
    """The code the project's next opportunity takes."""
    return opportunities.code(await _next_number(conn, project_id))


async def _next_rank(conn: AsyncConnection, project_id: int) -> int:
    """After the project's open opportunities."""
    o, r = tables.opportunities, tables.opportunity_revisions
    query = (
        select(func.coalesce(func.max(r.c.rank), 0))
        .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
        .where(o.c.project_id == project_id, r.c.status == opportunities.OPEN)
    )
    return min(int(await conn.scalar(query)) + 1, opportunities.MAX_RANK)


# Writing


def refusal(user: Principal, access: ProjectAccess) -> str | None:
    """Why ``user`` may not write the project's opportunities; None when they may."""
    if user.scope is not None:
        return "the agent of a run never writes an opportunity: @builder proposes one, which a writer approves"
    if has_role(access.role, "writer"):
        return None
    held = access.role or "no grant"
    return f"writing an opportunity of project {access.name} needs the writer role on it; {user.login} holds {held}"


async def _lock(conn: AsyncConnection, project_id: int) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the project's opportunities."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"opportunities:{project_id}", 0))))


async def _problem_of(conn: AsyncConnection, project_id: int, number: int):
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(p.c.id, r.c.status)
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id, p.c.number == number)
    )
    return (await conn.execute(query)).first()


async def _resolved(
    conn: AsyncConnection, user: Principal | None, access: ProjectAccess, refs: list[str], held: list[Answer]
) -> tuple[list[int], list[str]]:
    """The ids of the problems ``refs`` name, in order and once each, and each code that cannot be an answer: a
    problem of another project needs a grant of ``user`` on it (not asked when ``user`` is None), and every problem
    must exist and not be dropped, unless the opportunity holds it already."""
    kept = {answer.text(access.project_id): answer.problem_id for answer in held}
    ids: list[int] = []
    wrong: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        project, number = opportunities.answer_of(ref) or (None, 0)
        project = None if project == access.name else project
        text = opportunities.answer_text(project, number)
        if text in seen:
            continue
        seen.add(text)
        if text in kept:
            ids.append(kept[text])
            continue
        if project is None:
            project_id = access.project_id
        elif user is None:
            continue  # nobody to ask for a grant: the writer who approves is asked then
        else:
            try:
                other = await project_access(conn, user, project)
            except HTTPException:
                other = None
            project_id = other.project_id if other is not None and other.role is not None else None
        row = await _problem_of(conn, project_id, number) if project_id is not None else None
        if row is None or row.status == problems.DROPPED:
            grant = "" if project is None else f", or the writer holds no grant on project {project}"
            wrong.append(f"answers {text}: the problem does not exist, or it is dropped{grant}")
        else:
            ids.append(row.id)
    if len(ids) > opportunities.MAX_ANSWERS:
        wrong.append(f"an opportunity answers at most {opportunities.MAX_ANSWERS} problems")
    return await _with_hidden(conn, user, access, ids, held), wrong


async def _with_hidden(
    conn: AsyncConnection, user: Principal | None, access: ProjectAccess, ids: list[int], held: list[Answer]
) -> list[int]:
    """``ids`` once more with the problems of other projects the opportunity holds and ``user`` holds no grant on: the
    writer cannot see them, so cannot have left them out. The held order stays when nothing else changed."""
    if user is None:
        return ids
    sight = Sight(conn, user, access)
    hidden = [answer.problem_id for answer in held if not await sight.sees(answer)]
    if not hidden:
        return ids
    visible = [answer.problem_id for answer in held if answer.problem_id not in hidden]
    return [answer.problem_id for answer in held] if ids == visible else ids + hidden


async def _values(
    conn: AsyncConnection, user: Principal | None, access: ProjectAccess, body: ProjectOpportunityWrite, held
) -> tuple[dict, list[str]]:
    """What the next revision of opportunity ``held`` (None for a new one) holds once ``body`` is written by ``user``,
    and each thing wrong with it. Without a ``user`` the answers of other projects are not resolved."""
    wrong: list[str] = []
    owner_id = held.owner_id if held is not None else (user.user_id if user is not None else 0)
    if body.owner is not None and (held is None or body.owner.lower() != held.owner.lower()):
        owner_id, said = await project_goals.owner_of(conn, access, body.owner, OF)
        wrong += said
    metric = body.metric.model_dump(exclude_none=True) if body.metric is not None else None
    if body.metric is not None:
        held_metric = held.metric if held is not None else None
        wrong += await project_goals.metric_problems(conn, access, body.metric.key, held_metric, OF)
    ids, said = await _resolved(conn, user, access, body.answers, held.answers if held is not None else [])
    wrong += said
    rank = body.rank or (held.rank if held is not None else await _next_rank(conn, access.project_id))
    values = {
        "statement": body.statement,
        "beneficiary": body.beneficiary,
        "leverage": body.leverage,
        "window_until": body.window_until,
        "window_note": body.window_note,
        "answers": ids,
        "metric": metric,
        "rank": rank,
        "owner_id": owner_id,
        "status": body.status,
        "close_reason": body.close_reason,
    }
    return values, wrong


async def _number_of_new(conn: AsyncConnection, access: ProjectAccess, ref: str) -> int:
    """The number a new opportunity named ``ref`` takes: the next one; 422 for another code."""
    following = await _next_number(conn, access.project_id)
    if opportunities.number_of(ref) != following:
        raise HTTPException(
            422,
            f"the next opportunity of project {access.name} is {opportunities.code(following)}, not {ref}; nothing "
            "changed",
        )
    return following


async def check(conn: AsyncConnection, access: ProjectAccess, ref: str, body: ProjectOpportunityWrite) -> list[str]:
    """Each thing wrong with ``body`` as the next revision of opportunity ``ref``, as ``write`` would find it (an
    HTTPException for a new opportunity whose code is not the next one); nothing written. The answers of other
    projects are not checked: whoever approves the write is."""
    held = await found(conn, access.project_id, ref)
    if held is None:
        await _number_of_new(conn, access, ref)
    return (await _values(conn, None, access, body, held))[1]


async def _insert(conn: AsyncConnection, project_id: int, number: int, revision: int, values: dict, note, writer):
    """Write revision ``revision`` of opportunity ``number`` of the project, the opportunity itself with its first."""
    o, r, a = tables.opportunities, tables.opportunity_revisions, tables.opportunity_answers
    if revision == 1:
        row = {"project_id": project_id, "number": number, "revision": 1}
        opportunity_id = (await conn.execute(insert(o).values(row).returning(o.c.id))).scalar_one()
    else:
        query = update(o).where(o.c.project_id == project_id, o.c.number == number).values(revision=revision)
        opportunity_id = (await conn.execute(query.returning(o.c.id))).scalar_one()
    answers = values["answers"]
    own = {k: v for k, v in values.items() if k != "answers"}
    written = {**own, "opportunity_id": opportunity_id, "revision": revision, "note": note, "created_by": writer}
    await conn.execute(insert(r).values(written))
    for position, problem_id in enumerate(answers, start=1):
        row = {"opportunity_id": opportunity_id, "revision": revision, "position": position, "problem_id": problem_id}
        await conn.execute(insert(a).values(row))


async def write(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    ref: str,
    body: ProjectOpportunityWrite,
    *,
    via: str = "",
) -> tuple[ProjectOpportunity, bool]:
    """Write ``body`` as the next revision of opportunity ``ref`` of the project, once it is right (422 naming each
    thing wrong otherwise) and while the opportunity is at ``body.if_revision`` (409 otherwise); (the opportunity now,
    whether a revision was written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked
    ``refusal``."""
    await _lock(conn, access.project_id)
    held = await found(conn, access.project_id, ref)
    now = 0 if held is None else held.revision
    if body.if_revision is not None and body.if_revision != now:
        raise HTTPException(
            409,
            f"opportunity {ref} of project {access.name} is at revision {now}, not {body.if_revision}; nothing changed",
        )
    number = held.number if held is not None else await _number_of_new(conn, access, ref)
    values, wrong = await _values(conn, user, access, body, held)
    if wrong:
        raise HTTPException(422, f"{'; '.join(wrong)}; nothing changed")
    sight = Sight(conn, user, access)
    if held is not None and values == _stored(held):
        return await view(conn, sight, held), False
    note = " ".join((body.note or "").split()) or None
    await _insert(conn, access.project_id, number, now + 1, values, note, user.user_id)
    after = await found(conn, access.project_id, opportunities.code(number))
    keys = opportunities.changed(
        fields_of(held, access.project_id) if held is not None else {}, fields_of(after, access.project_id)
    )
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.OPPORTUNITY_SET,
        target=f"{access.name}/opportunity {opportunities.code(number)} revision={now + 1} fields={','.join(keys)}{via}",
        project_id=access.project_id,
    )
    log.info(
        "opportunity written",
        extra={"project": access.name, "opportunity": opportunities.code(number), "revision": now + 1},
    )
    return await view(conn, sight, after), True


def closing(
    row, own: int, status: str, reason: str, note: str | None, if_revision: int | None
) -> ProjectOpportunityWrite:
    """Opportunity ``row`` closed as ``status`` for ``reason``, as a write of its next revision."""
    fields = {**fields_of(row, own), "status": status, "close_reason": reason}
    return ProjectOpportunityWrite(
        **fields, note=note, if_revision=row.revision if if_revision is None else if_revision
    )


# Routes


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess, changed: bool) -> None:
    """Queue a build of the project's knowledge graph, which holds its opportunities (``evo_agents.hub.kg_opportunities``),
    once a write changed one."""
    if changed:
        await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


async def reader(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    """``user``'s access to the project's opportunities: 404 without a grant (``project_access``), 403 for a hub admin
    without one."""
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the opportunities of project {access.name} needs a grant on it")
    return access


async def writer(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    refused = refusal(user, access)
    if refused is not None:
        raise HTTPException(403, refused)
    return access


async def one(conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None):
    row = await found(conn, access.project_id, ref, revision)
    if row is None:
        at = "" if revision is None else f" revision {revision}"
        raise HTTPException(404, f"project {access.name} has no opportunity {ref}{at}")
    return row


async def listed(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    status: str | None = None,
    closing_within: int | None = None,
) -> list[ProjectOpportunity]:
    """The project's opportunities as they are now, by rank, then by code; those of ``status`` alone when given;
    with ``closing_within`` the open ones whose window ends within that many days, nearest end first."""
    o, r = tables.opportunities, tables.opportunity_revisions
    query = _select().where(o.c.project_id == access.project_id, r.c.revision == o.c.revision)
    if status is not None:
        query = query.where(r.c.status == status)
    if closing_within is not None:
        query = closing_filter(query, closing_within).order_by(r.c.window_until, r.c.rank, o.c.number)
    else:
        query = query.order_by(r.c.rank, o.c.number)
    rows = (await conn.execute(query)).all()
    sight = Sight(conn, user, access)
    return [
        await view(conn, sight, SimpleNamespace(**row._mapping, answers=await answers_of(conn, row.id, row.revision)))
        for row in rows
    ]


async def shown(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, ref: str, revision: int | None = None
) -> ProjectOpportunityShown:
    """Opportunity ``ref`` at ``revision`` (its newest when None), with its evidence counted."""
    row = await one(conn, access, ref, revision)
    lines, sources = await evidence_counts(conn, row.id)
    seen = await view(conn, Sight(conn, user, access), row)
    return ProjectOpportunityShown(**seen.model_dump(), evidence_count=lines, source_count=sources)


@router.get("/{project}/opportunities", response_model=list[ProjectOpportunity], responses=REFUSALS)
async def list_opportunities(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[Literal[opportunities.STATUSES] | None, Query(description="those of this status alone")] = None,
    closing_within: Annotated[
        int | None,
        Query(
            ge=0,
            le=3650,
            description="the open ones whose window ends within this many days (one already past included), "
            "nearest end first; with it, status is open",
        ),
    ] = None,
) -> list[ProjectOpportunity]:
    """The project's opportunities as they are now, by rank, then by code."""
    async with request.app.state.engine.begin() as conn:
        access = await reader(conn, user, project)
        return await listed(conn, user, access, status, closing_within)


@router.get("/{project}/opportunities/{ref}", response_model=ProjectOpportunityShown, responses=REFUSALS)
async def show_opportunity(
    request: Request,
    project: ProjectName,
    ref: OpportunityRef,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, le=MAX_REVISION, description="an older revision")] = None,
) -> ProjectOpportunityShown:
    """One opportunity of the project, at its newest revision or at ``revision``, with its evidence counted."""
    async with request.app.state.engine.begin() as conn:
        access = await reader(conn, user, project)
        return await shown(conn, user, access, ref, revision)


@router.get("/{project}/opportunities/{ref}/revisions", response_model=list[OpportunityRevision], responses=REFUSALS)
async def opportunity_revisions(
    request: Request, project: ProjectName, ref: OpportunityRef, user: CurrentUser
) -> list[OpportunityRevision]:
    """Every revision of an opportunity, the newest first, each with the fields whose value it changed."""
    o, r = tables.opportunities, tables.opportunity_revisions
    async with request.app.state.engine.begin() as conn:
        access = await reader(conn, user, project)
        held = await one(conn, access, ref)
        rows = (await conn.execute(_select().where(o.c.id == held.id).order_by(r.c.revision))).all()
        revisions = []
        for row in rows:
            answers = await answers_of(conn, row.id, row.revision)
            revisions.append((row, fields_of(SimpleNamespace(**row._mapping, answers=answers), access.project_id)))
    listed_revisions, before = [], {}
    for row, after in revisions:
        listed_revisions.append(
            OpportunityRevision(
                revision=row.revision,
                status=row.status,
                note=row.note,
                created_by=row.created_by,
                created_at=row.created_at,
                changed=opportunities.changed(before, after),
            )
        )
        before = after
    return listed_revisions[::-1]


@router.put("/{project}/opportunities/{ref}", response_model=ProjectOpportunityWritten, responses=WRITE_REFUSALS)
async def write_opportunity(
    request: Request, project: ProjectName, ref: OpportunityRef, body: ProjectOpportunityWrite, user: CurrentUser
) -> ProjectOpportunityWritten:
    """Write an opportunity whole as its next revision, made when the project has none of code ``ref``; a writer of
    the project may."""
    async with request.app.state.engine.begin() as conn:
        access = await writer(conn, user, project)
        written, changed = await write(conn, user, access, ref, body)
        await _graph_changed(request, conn, access, changed)
    return ProjectOpportunityWritten(**written.model_dump(), changed=changed)


@router.post("/{project}/opportunities/{ref}/close", response_model=ProjectOpportunityWritten, responses=WRITE_REFUSALS)
async def close_opportunity(
    request: Request, project: ProjectName, ref: OpportunityRef, body: ProjectOpportunityClose, user: CurrentUser
) -> ProjectOpportunityWritten:
    """Close an opportunity as captured, missed or dropped, for a reason, as its next revision; a writer of the
    project may."""
    async with request.app.state.engine.begin() as conn:
        access = await writer(conn, user, project)
        held = await one(conn, access, ref)
        if held.status != opportunities.OPEN:
            raise HTTPException(
                409,
                f"opportunity {ref} of project {access.name} is {held.status} already ({held.close_reason}); nothing "
                "changed",
            )
        closed = closing(held, access.project_id, body.status, body.reason, body.note, body.if_revision)
        written, changed = await write(conn, user, access, ref, closed)
        await _graph_changed(request, conn, access, changed)
    return ProjectOpportunityWritten(**written.model_dump(), changed=changed)

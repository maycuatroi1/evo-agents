"""The evidence of an opportunity (plan opportunity-intake, criterion a2; the model in
``evo_agents.hub.opportunities``). Table opportunity_evidence, schema 0143.

A line of evidence is something a source says that shows the opportunity is there, with the fields of a problem's line
(``evo_agents.hub.server.problem_evidence``): the kind of the source and its address, where in it, who spoke, an
excerpt of at most 300 characters, the signal it shows, its label (public or internal), who added it, and who confirmed
it and when. Its signal is one of the eight a problem's line shows (complaint, workaround, repeated_request, blocker,
commitment, proposed_solution, contradiction, decision_without_reason) or one of seven that name an opening: wish,
external_demand, window, reusable_asset, pull, intro, idle_capacity. The hub refuses a line labelled personal and an
excerpt longer than 300 characters with 422: such text stays on the member's machine. An opportunity's lines are
numbered within it (E-1, E-2, ...).

- GET /v1/projects/{project}/opportunities/{ref}/evidence: the opportunity's lines by number, for a member with a
  grant on the project (404 without one, as the opportunity's own routes answer).
- POST .../evidence: a line added, for a writer of the project; 409 when the opportunity has a line of the same
  source, place and excerpt already. POST .../evidence/{code}/confirm: the line confirmed by a writer, once (409 after).
- The agent of a run never writes one: @builder proposes an opportunity with its lines (proposal kind ``opportunity``,
  key ``evidence``), and the lines are made when a writer approves it (``add_lines``, ``remove_proposed``).

``GET .../opportunities/{ref}`` counts the lines and the independent sources they come from. Each add, confirm or
removal adds an audit row ``opportunity.evidence`` naming the opportunity and the lines, never an excerpt, and queues
a build of the project's knowledge graph.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import opportunities, problems, tables
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server import opportunities as project_opportunities
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.problem_evidence import ProblemEvidenceWrite
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
OpportunityRef = project_opportunities.OpportunityRef
EvidenceRef = Annotated[str, Path(pattern=problems.EVIDENCE_REF, description="E-<number>")]

router = APIRouter(prefix="/v1/projects", tags=["opportunities"], responses={401: {"model": ErrorBody}})


class OpportunityEvidenceWrite(ProblemEvidenceWrite):
    """One line of evidence of an opportunity, as a writer adds it or @builder proposes it: a problem's line, whose
    signal may also be one of an opportunity's."""

    signal: str = Field(description=f"what it shows: {', '.join(opportunities.SIGNALS)}")

    @field_validator("signal")
    @classmethod
    def _signal(cls, signal: str) -> str:
        if signal not in opportunities.SIGNALS:
            raise ValueError(f"signal is one of {', '.join(opportunities.SIGNALS)}")
        return signal


class OpportunityEvidence(BaseModel):
    """A line of the evidence of an opportunity."""

    project: str
    opportunity: str = Field(description="O-<number>")
    code: str = Field(description="E-<number>, unique in the opportunity")
    number: int
    source_kind: str
    source_uri: str
    locator: str | None
    speaker: str | None
    excerpt: str
    signal: str
    label: str
    added_by: str
    added_at: datetime
    confirmed_by: str | None = Field(description="the writer who confirmed it; null before")
    confirmed_at: datetime | None
    proposal_id: int | None = Field(description="the proposal of @builder that made it; null for a line a writer added")


def _select():
    e, o = tables.opportunity_evidence, tables.opportunities
    added, confirmed = tables.users.alias("added"), tables.users.alias("confirmed")
    return (
        select(
            e.c.opportunity_id,
            e.c.number,
            o.c.number.label("opportunity"),
            e.c.source_kind,
            e.c.source_uri,
            e.c.locator,
            e.c.speaker,
            e.c.excerpt,
            e.c.signal,
            e.c.label,
            added.c.login.label("added_by"),
            e.c.created_at.label("added_at"),
            confirmed.c.login.label("confirmed_by"),
            e.c.confirmed_at,
            e.c.proposal_id,
        )
        .join_from(e, o, o.c.id == e.c.opportunity_id)
        .join(added, added.c.id == e.c.added_by)
        .outerjoin(confirmed, confirmed.c.id == e.c.confirmed_by)
    )


def view(project: str, row) -> OpportunityEvidence:
    return OpportunityEvidence(
        project=project,
        opportunity=opportunities.code(row.opportunity),
        code=problems.evidence_code(row.number),
        number=row.number,
        source_kind=row.source_kind,
        source_uri=row.source_uri,
        locator=row.locator,
        speaker=row.speaker,
        excerpt=row.excerpt,
        signal=row.signal,
        label=row.label,
        added_by=row.added_by,
        added_at=row.added_at,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        proposal_id=row.proposal_id,
    )


async def listed(conn: AsyncConnection, access: ProjectAccess, ref: str) -> list[OpportunityEvidence]:
    """The lines of opportunity ``ref``'s evidence by number; 404 when the project has no such opportunity."""
    held = await project_opportunities.one(conn, access, ref)
    e = tables.opportunity_evidence
    rows = (await conn.execute(_select().where(e.c.opportunity_id == held.id).order_by(e.c.number))).all()
    return [view(access.name, row) for row in rows]


def _same(line: OpportunityEvidenceWrite) -> tuple:
    """What makes a line the same as another: its source, the place in it and the excerpt."""
    return (line.source_kind, line.source_uri, line.locator, line.excerpt)


async def wrong_lines(
    conn: AsyncConnection, opportunity_id: int | None, lines: list[OpportunityEvidenceWrite]
) -> list[str]:
    """Each thing wrong with ``lines`` as additions to opportunity ``opportunity_id`` (None: a new opportunity): a line
    twice, a line the opportunity has already, more lines than an opportunity holds."""
    wrong: list[str] = []
    held: dict[tuple, str] = {}
    count = 0
    if opportunity_id is not None:
        e = tables.opportunity_evidence
        query = select(e.c.number, e.c.source_kind, e.c.source_uri, e.c.locator, e.c.excerpt)
        for row in (await conn.execute(query.where(e.c.opportunity_id == opportunity_id))).all():
            held[(row.source_kind, row.source_uri, row.locator, row.excerpt)] = problems.evidence_code(row.number)
            count += 1
    seen: set[tuple] = set()
    for at, line in enumerate(lines, start=1):
        key = _same(line)
        if key in held:
            wrong.append(f"evidence {at}: the opportunity has this line already as {held[key]}")
        elif key in seen:
            wrong.append(f"evidence {at}: the same line is given twice")
        seen.add(key)
    if count + len(lines) > opportunities.MAX_EVIDENCE:
        wrong.append(f"an opportunity holds at most {opportunities.MAX_EVIDENCE} lines of evidence")
    return wrong


async def add_lines(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    opportunity_id: int,
    lines: list[OpportunityEvidenceWrite],
    *,
    proposal_id: int | None = None,
) -> list[str]:
    """Add ``lines`` to opportunity ``opportunity_id`` as ``user`` (422 naming each thing wrong, nothing written
    otherwise); their codes. The caller checked ``refusal`` and holds the lock of the project's opportunities."""
    wrong = await wrong_lines(conn, opportunity_id, lines)
    if wrong:
        raise HTTPException(422, f"{'; '.join(wrong)}; nothing changed")
    e = tables.opportunity_evidence
    first = int(
        await conn.scalar(select(func.coalesce(func.max(e.c.number), 0)).where(e.c.opportunity_id == opportunity_id))
    )
    codes = []
    for at, line in enumerate(lines, start=first + 1):
        values = {**line.model_dump(), "opportunity_id": opportunity_id, "number": at, "added_by": user.user_id}
        await conn.execute(insert(e).values({**values, "proposal_id": proposal_id}))
        codes.append(problems.evidence_code(at))
    if codes:
        number = await conn.scalar(
            select(tables.opportunities.c.number).where(tables.opportunities.c.id == opportunity_id)
        )
        via = f" proposal={proposal_id}" if proposal_id is not None else ""
        await _audit(conn, user, access, number, f"{','.join(codes)} added{via}")
    return codes


async def remove_proposed(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, opportunity_id: int, proposal_id: int
) -> int:
    """Take away the lines proposal ``proposal_id`` of @builder made in opportunity ``opportunity_id``, as its rollback
    does; how many."""
    e = tables.opportunity_evidence
    query = delete(e).where(e.c.opportunity_id == opportunity_id, e.c.proposal_id == proposal_id)
    gone = (await conn.execute(query.returning(e.c.number))).scalars().all()
    if gone:
        number = await conn.scalar(
            select(tables.opportunities.c.number).where(tables.opportunities.c.id == opportunity_id)
        )
        codes = ",".join(problems.evidence_code(n) for n in sorted(gone))
        await _audit(conn, user, access, number, f"{codes} removed proposal={proposal_id}")
    return len(gone)


async def _audit(conn: AsyncConnection, user: Principal, access: ProjectAccess, number: int, what: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.OPPORTUNITY_EVIDENCE,
        target=f"{access.name}/opportunity {opportunities.code(number)} evidence={what}",
        project_id=access.project_id,
    )


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess) -> None:
    await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


# Routes


@router.get("/{project}/opportunities/{ref}/evidence", response_model=list[OpportunityEvidence], responses=REFUSALS)
async def list_evidence(
    request: Request, project: ProjectName, ref: OpportunityRef, user: CurrentUser
) -> list[OpportunityEvidence]:
    """The evidence of an opportunity by number, for a member with a grant on the project."""
    async with request.app.state.engine.begin() as conn:
        access = await project_opportunities.reader(conn, user, project)
        return await listed(conn, access, ref)


@router.post(
    "/{project}/opportunities/{ref}/evidence",
    response_model=OpportunityEvidence,
    status_code=201,
    responses=WRITE_REFUSALS,
)
async def add_evidence(
    request: Request, project: ProjectName, ref: OpportunityRef, body: OpportunityEvidenceWrite, user: CurrentUser
) -> OpportunityEvidence:
    """Add a line of evidence to an opportunity; a writer of the project may. 422 for a personal label or an excerpt of
    more than 300 characters, 409 for a line the opportunity has already."""
    async with request.app.state.engine.begin() as conn:
        access = await project_opportunities.writer(conn, user, project)
        await project_opportunities._lock(conn, access.project_id)
        held = await project_opportunities.one(conn, access, ref)
        try:
            (code,) = await add_lines(conn, user, access, held.id, [body])
        except HTTPException as exc:
            if exc.status_code == 422 and "has this line already" in str(exc.detail):
                raise HTTPException(409, str(exc.detail)) from None
            raise
        await _graph_changed(request, conn, access)
        e = tables.opportunity_evidence
        row = (await conn.execute(_select().where(e.c.opportunity_id == held.id, e.c.number == int(code[2:])))).one()
    return view(access.name, row)


@router.post(
    "/{project}/opportunities/{ref}/evidence/{code}/confirm",
    response_model=OpportunityEvidence,
    responses=WRITE_REFUSALS,
)
async def confirm_evidence(
    request: Request, project: ProjectName, ref: OpportunityRef, code: EvidenceRef, user: CurrentUser
) -> OpportunityEvidence:
    """Confirm a line of evidence, once; a writer of the project may."""
    e = tables.opportunity_evidence
    number = int(code[2:])
    async with request.app.state.engine.begin() as conn:
        access = await project_opportunities.writer(conn, user, project)
        await project_opportunities._lock(conn, access.project_id)
        held = await project_opportunities.one(conn, access, ref)
        row = (await conn.execute(_select().where(e.c.opportunity_id == held.id, e.c.number == number))).first()
        if row is None:
            raise HTTPException(404, f"opportunity {ref} of project {access.name} has no evidence {code}")
        if row.confirmed_by is not None:
            raise HTTPException(
                409,
                f"evidence {code} of opportunity {ref} was confirmed by {row.confirmed_by} already; nothing changed",
            )
        stamp = {"confirmed_by": user.user_id, "confirmed_at": func.now()}
        await conn.execute(update(e).where(e.c.opportunity_id == held.id, e.c.number == number).values(stamp))
        await _audit(conn, user, access, held.number, f"{code} confirmed")
        await _graph_changed(request, conn, access)
        row = (await conn.execute(_select().where(e.c.opportunity_id == held.id, e.c.number == number))).one()
    return view(access.name, row)

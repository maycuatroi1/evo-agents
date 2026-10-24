"""The evidence of a problem (plan problem-intake, criterion a1; the model in ``evo_agents.hub.problems``). Table
problem_evidence, schema 0137.

A line of evidence is something a source says that shows the problem hurts: the kind of the source and its address (a
URI or a path), where in it (a message id, a time, a page), who spoke when someone did, an excerpt of at most 300
characters, the signal it shows (complaint, workaround, repeated_request, blocker, commitment, proposed_solution,
contradiction, decision_without_reason), its label (public or internal), who added it, and who confirmed it and when.
The hub refuses a line labelled personal and an excerpt longer than 300 characters with 422: such text stays on the
member's machine. A problem's lines are numbered within it (E-1, E-2, ...).

- GET /v1/projects/{project}/problems/{ref}/evidence: the problem's lines by number, for a member with a grant on the
  project (404 without one, as the problem's own routes answer).
- POST .../evidence: a line added, for a writer of the project; 409 when the problem has a line of the same source,
  place and excerpt already. POST .../evidence/{code}/confirm: the line confirmed by a writer, once (409 after).
- The agent of a run never writes one: @builder proposes a problem with its lines (proposal kind ``problem``, key
  ``evidence``), and the lines are made when a writer approves it (``add_lines``, ``remove_proposed``).

``GET .../problems/{ref}`` counts the lines and the independent sources they come from (a source is a kind and an
address). Writes of one project's problems and evidence wait for each other (the lock of ``problems.write``). Each add,
confirm or removal adds an audit row ``problem.evidence`` naming the problem and the lines, never an excerpt, and
queues a build of the project's knowledge graph, which holds the lines as edges of the problem's node.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import problems, tables
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server import problems as project_problems
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
ProblemRef = project_problems.ProblemRef
EvidenceRef = Annotated[str, Path(pattern=problems.EVIDENCE_REF, description="E-<number>")]
Said = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

router = APIRouter(prefix="/v1/projects", tags=["problems"], responses={401: {"model": ErrorBody}})


class ProblemEvidenceWrite(BaseModel):
    """One line of evidence, as a writer adds it or @builder proposes it."""

    model_config = ConfigDict(extra="forbid")

    source_kind: str = Field(description=f"where it was read: {', '.join(problems.SOURCE_KINDS)}")
    source_uri: Said = Field(max_length=problems.MAX_URI_CHARS, description="the source's URI or path")
    locator: Said | None = Field(
        None, max_length=problems.MAX_LOCATOR_CHARS, description="where in the source: a message id, a time, a page"
    )
    speaker: Said | None = Field(None, max_length=problems.MAX_SPEAKER_CHARS, description="who spoke, when someone did")
    excerpt: Said = Field(max_length=problems.MAX_EXCERPT_CHARS, description="at most 300 characters of the source")
    signal: str = Field(description=f"what it shows: {', '.join(problems.SIGNALS)}")
    label: str = Field(description="public or internal; a personal line stays on the member's machine")

    @field_validator("source_kind")
    @classmethod
    def _kind(cls, kind: str) -> str:
        if kind not in problems.SOURCE_KINDS:
            raise ValueError(f"source_kind is one of {', '.join(problems.SOURCE_KINDS)}")
        return kind

    @field_validator("signal")
    @classmethod
    def _signal(cls, signal: str) -> str:
        if signal not in problems.SIGNALS:
            raise ValueError(f"signal is one of {', '.join(problems.SIGNALS)}")
        return signal

    @field_validator("label")
    @classmethod
    def _label(cls, label: str) -> str:
        if label == "personal":
            raise ValueError("a personal line stays on the member's machine: label it public or internal, or keep it")
        if label not in problems.EVIDENCE_LABELS:
            raise ValueError(f"label is one of {', '.join(problems.EVIDENCE_LABELS)}")
        return label


class ProblemEvidence(BaseModel):
    """A line of the evidence of a problem."""

    project: str
    problem: str = Field(description="P-<number>")
    code: str = Field(description="E-<number>, unique in the problem")
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
    e, p = tables.problem_evidence, tables.problems
    added, confirmed = tables.users.alias("added"), tables.users.alias("confirmed")
    return (
        select(
            e.c.problem_id,
            e.c.number,
            p.c.number.label("problem"),
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
        .join_from(e, p, p.c.id == e.c.problem_id)
        .join(added, added.c.id == e.c.added_by)
        .outerjoin(confirmed, confirmed.c.id == e.c.confirmed_by)
    )


def view(project: str, row) -> ProblemEvidence:
    return ProblemEvidence(
        project=project,
        problem=problems.code(row.problem),
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


async def listed(conn: AsyncConnection, access: ProjectAccess, ref: str) -> list[ProblemEvidence]:
    """The lines of problem ``ref``'s evidence by number; 404 when the project has no such problem."""
    held = await project_problems._one(conn, access, ref)
    e = tables.problem_evidence
    rows = (await conn.execute(_select().where(e.c.problem_id == held.id).order_by(e.c.number))).all()
    return [view(access.name, row) for row in rows]


def _same(line: ProblemEvidenceWrite) -> tuple:
    """What makes a line the same as another: its source, the place in it and the excerpt."""
    return (line.source_kind, line.source_uri, line.locator, line.excerpt)


async def wrong_lines(conn: AsyncConnection, problem_id: int | None, lines: list[ProblemEvidenceWrite]) -> list[str]:
    """Each thing wrong with ``lines`` as additions to problem ``problem_id`` (None: a new problem): a line twice, a
    line the problem has already, more lines than a problem holds."""
    wrong: list[str] = []
    held: dict[tuple, str] = {}
    count = 0
    if problem_id is not None:
        e = tables.problem_evidence
        query = select(e.c.number, e.c.source_kind, e.c.source_uri, e.c.locator, e.c.excerpt)
        for row in (await conn.execute(query.where(e.c.problem_id == problem_id))).all():
            held[(row.source_kind, row.source_uri, row.locator, row.excerpt)] = problems.evidence_code(row.number)
            count += 1
    seen: set[tuple] = set()
    for at, line in enumerate(lines, start=1):
        key = _same(line)
        if key in held:
            wrong.append(f"evidence {at}: the problem has this line already as {held[key]}")
        elif key in seen:
            wrong.append(f"evidence {at}: the same line is given twice")
        seen.add(key)
    if count + len(lines) > problems.MAX_EVIDENCE:
        wrong.append(f"a problem holds at most {problems.MAX_EVIDENCE} lines of evidence")
    return wrong


async def add_lines(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    problem_id: int,
    lines: list[ProblemEvidenceWrite],
    *,
    proposal_id: int | None = None,
) -> list[str]:
    """Add ``lines`` to problem ``problem_id`` as ``user`` (422 naming each thing wrong, nothing written otherwise);
    their codes. The caller checked ``refusal`` and holds the lock of the project's problems."""
    wrong = await wrong_lines(conn, problem_id, lines)
    if wrong:
        raise HTTPException(422, f"{'; '.join(wrong)}; nothing changed")
    e = tables.problem_evidence
    first = int(await conn.scalar(select(func.coalesce(func.max(e.c.number), 0)).where(e.c.problem_id == problem_id)))
    codes = []
    for at, line in enumerate(lines, start=first + 1):
        values = {**line.model_dump(), "problem_id": problem_id, "number": at, "added_by": user.user_id}
        await conn.execute(insert(e).values({**values, "proposal_id": proposal_id}))
        codes.append(problems.evidence_code(at))
    if codes:
        number = await conn.scalar(select(tables.problems.c.number).where(tables.problems.c.id == problem_id))
        via = f" proposal={proposal_id}" if proposal_id is not None else ""
        await _audit(conn, user, access, number, f"{','.join(codes)} added{via}")
    return codes


async def remove_proposed(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, problem_id: int, proposal_id: int
) -> int:
    """Take away the lines proposal ``proposal_id`` of @builder made in problem ``problem_id``, as its rollback does;
    how many."""
    e = tables.problem_evidence
    gone = (
        (
            await conn.execute(
                delete(e).where(e.c.problem_id == problem_id, e.c.proposal_id == proposal_id).returning(e.c.number)
            )
        )
        .scalars()
        .all()
    )
    if gone:
        number = await conn.scalar(select(tables.problems.c.number).where(tables.problems.c.id == problem_id))
        codes = ",".join(problems.evidence_code(n) for n in sorted(gone))
        await _audit(conn, user, access, number, f"{codes} removed proposal={proposal_id}")
    return len(gone)


async def _audit(conn: AsyncConnection, user: Principal, access: ProjectAccess, number: int, what: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.PROBLEM_EVIDENCE,
        target=f"{access.name}/problem {problems.code(number)} evidence={what}",
        project_id=access.project_id,
    )


async def _graph_changed(request: Request, conn: AsyncConnection, access: ProjectAccess) -> None:
    await queue_if_configured(request.app.state.jobs, conn, access.project_id, access.name)


# Routes


@router.get("/{project}/problems/{ref}/evidence", response_model=list[ProblemEvidence], responses=REFUSALS)
async def list_evidence(
    request: Request, project: ProjectName, ref: ProblemRef, user: CurrentUser
) -> list[ProblemEvidence]:
    """The evidence of a problem by number, for a member with a grant on the project."""
    async with request.app.state.engine.begin() as conn:
        access = await project_problems.reader(conn, user, project)
        return await listed(conn, access, ref)


@router.post(
    "/{project}/problems/{ref}/evidence",
    response_model=ProblemEvidence,
    status_code=201,
    responses=WRITE_REFUSALS,
)
async def add_evidence(
    request: Request, project: ProjectName, ref: ProblemRef, body: ProblemEvidenceWrite, user: CurrentUser
) -> ProblemEvidence:
    """Add a line of evidence to a problem; a writer of the project may. 422 for a personal label or an excerpt of more
    than 300 characters, 409 for a line the problem has already."""
    async with request.app.state.engine.begin() as conn:
        access = await project_problems._writer(conn, user, project)
        await project_problems._lock(conn, access.project_id)
        held = await project_problems._one(conn, access, ref)
        try:
            (code,) = await add_lines(conn, user, access, held.id, [body])
        except HTTPException as exc:
            if exc.status_code == 422 and "has this line already" in str(exc.detail):
                raise HTTPException(409, str(exc.detail)) from None
            raise
        await _graph_changed(request, conn, access)
        row = (
            await conn.execute(
                _select().where(
                    tables.problem_evidence.c.problem_id == held.id,
                    tables.problem_evidence.c.number == int(code[2:]),
                )
            )
        ).one()
    return view(access.name, row)


@router.post(
    "/{project}/problems/{ref}/evidence/{code}/confirm", response_model=ProblemEvidence, responses=WRITE_REFUSALS
)
async def confirm_evidence(
    request: Request, project: ProjectName, ref: ProblemRef, code: EvidenceRef, user: CurrentUser
) -> ProblemEvidence:
    """Confirm a line of evidence, once; a writer of the project may."""
    e = tables.problem_evidence
    number = int(code[2:])
    async with request.app.state.engine.begin() as conn:
        access = await project_problems._writer(conn, user, project)
        await project_problems._lock(conn, access.project_id)
        held = await project_problems._one(conn, access, ref)
        row = (await conn.execute(_select().where(e.c.problem_id == held.id, e.c.number == number))).first()
        if row is None:
            raise HTTPException(404, f"problem {ref} of project {access.name} has no evidence {code}")
        if row.confirmed_by is not None:
            raise HTTPException(
                409, f"evidence {code} of problem {ref} was confirmed by {row.confirmed_by} already; nothing changed"
            )
        stamp = {"confirmed_by": user.user_id, "confirmed_at": func.now()}
        await conn.execute(update(e).where(e.c.problem_id == held.id, e.c.number == number).values(stamp))
        await _audit(conn, user, access, held.number, f"{code} confirmed")
        await _graph_changed(request, conn, access)
        row = (await conn.execute(_select().where(e.c.problem_id == held.id, e.c.number == number))).one()
    return view(access.name, row)

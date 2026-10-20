"""What earlier review nights of a project found, and what became of it: so a Reviewer looks through a lens knowing
what was found through it before, which of it is closed, and which proposals the owner rejected lately, and does not
record a closed finding again without new evidence.

A finding's status comes from the proposals that name it (``finding_ids``), those the reader reads by their label
through the sink alone, and their changes and ledgers (``proposal_status``): ``merged`` once its change merged
(``kept`` or ``reverted`` once the ledger's outcome says so), ``rejected`` for a proposal its owner rejected or that
was dropped as a repeat, ``proposed`` otherwise. A finding takes the first of kept, merged, proposed, reverted and
rejected its proposals have, and ``open`` with none. CLOSED are the statuses whose work is done: a change kept or
merged, or a proposal rejected; a reverted change opens its finding again.

``for_prompt`` gives the prompt of a review run (``evo_agents.hub.review.build_review_prompt``), as the run's owner
reads them through the project's hub sink: for each lens of the night its latest PER_LENS findings with their status,
and the proposals rejected in the last REJECTED_DAYS days with the owner's note. ``closed_like`` is what the hub checks
before it records a finding (``evo_agents.hub.server.proposals.record_finding``): a closed finding of the same lens
whose title is like the new one's (SIMILAR of the words, or the same evidence) and which cited every piece of the new
one's evidence already; the hub then refuses the new finding (409), naming the closed one.

GET /v1/projects/{p}/curator/earlier (reader) lists the findings the caller may read through its sink, newest first,
filtered by lens and status, with their proposals, and the proposals rejected lately; the MCP tool curator_findings and
``evo-agents hub curator earlier`` read it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import review, tables, tiers
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.security import CurrentUser, Principal

STATUSES = ("open", "proposed", "rejected", "merged", "kept", "reverted")
CLOSED = ("rejected", "merged", "kept")
PRECEDENCE = ("kept", "merged", "proposed", "reverted", "rejected")  # the status a finding takes from its proposals
PER_LENS = 5  # findings of one lens in the prompt
REJECTED_SHOWN = 10  # rejected proposals in the prompt
SIMILAR = 0.5  # of the words of two titles, for a finding to be like another
SCAN = 2000  # findings one read examines
MAX_LIMIT = 100
REPEATED = (
    "finding #{id} ({lens}, {status}) of an earlier night found this and is closed, and it cited every piece of "
    "evidence of this one: cite evidence it did not, or leave it; nothing was recorded"
)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


class EarlierProposal(BaseModel):
    id: int
    kind: str
    tier: int
    state: str = Field(description="the proposal's state")
    change_state: str | None = Field(description="the state of the change accepting it made, if any")
    outcome: str | None = Field(description="the ledger's outcome of its merged change: keep, revert or unclear")
    status: Literal[STATUSES] = Field(description="what it says of the findings it names")
    note: str | None = Field(description="the owner's note with the answer")


class EarlierFinding(BaseModel):
    id: int
    run_id: int
    night: date | None = Field(description="the night of the review run that found it")
    lens: str
    severity: str
    title: str
    status: Literal[STATUSES]
    closed: bool = Field(description="kept, merged or rejected: not recorded again without new evidence")
    proposals: list[EarlierProposal]
    evidence: list[str] = Field(description="its evidence, as the command line writes it")


class RejectedProposal(BaseModel):
    id: int
    lens: str
    kind: str
    title: str
    note: str | None
    answered_at: datetime | None


class EarlierView(BaseModel):
    project: str
    findings: list[EarlierFinding] = Field(description="newest first")
    rejected: list[RejectedProposal] = Field(
        description=f"rejected in the last {tiers.REJECTED_DAYS} days, newest first"
    )


# The rules


def title_words(title: str) -> set[str]:
    return set(re.findall(r"\w{3,}", (title or "").lower()))


def similar(a: str, b: str) -> bool:
    """Whether titles ``a`` and ``b`` share SIMILAR of their words."""
    left, right = title_words(a), title_words(b)
    return bool(left and right) and len(left & right) / len(left | right) >= SIMILAR


def proposal_status(state: str, change_state: str | None, merged_at, outcome: str | None) -> str:
    if merged_at is not None or change_state == "merged":
        return {"keep": "kept", "revert": "reverted"}.get(outcome or "", "merged")
    if state in ("rejected", "dropped"):
        return "rejected"
    return "proposed"


def finding_status(statuses: list[str]) -> str:
    return next((status for status in PRECEDENCE if status in statuses), "open")


def texts_of(evidence) -> list[str]:
    return [review.evidence_text(item) for item in evidence or [] if isinstance(item, dict)]


# Reading


def _findings(project_id: int, lenses: list[str] | None):
    f, r = tables.findings, tables.runs
    query = (
        select(
            f.c.id,
            f.c.run_id,
            f.c.lens,
            f.c.severity,
            f.c.title,
            f.c.evidence,
            f.c.label,
            f.c.created_at,
            r.c.schedule_night,
        )
        .join_from(f, r, r.c.id == f.c.run_id)
        .where(f.c.project_id == project_id)
    )
    if lenses is not None:
        query = query.where(f.c.lens.in_(lenses))
    return query.order_by(f.c.id.desc()).limit(SCAN)


async def _proposals_of(
    conn: AsyncConnection, access: ProjectAccess, through: str | None, ids: list[int]
) -> dict[int, list[EarlierProposal]]:
    """The proposals readable through ``through`` that name each of findings ``ids``, oldest first."""
    if through is None or not ids:
        return {}
    p, c, lg = tables.proposals, tables.curator_changes, tables.curator_ledger
    outcome = (
        select(lg.c.outcome)
        .where(lg.c.change_id == c.c.id, lg.c.action == "outcome")
        .order_by(lg.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(
            p.c.id,
            p.c.kind,
            p.c.tier,
            p.c.state,
            p.c.note,
            p.c.finding_ids,
            p.c.label,
            c.c.state.label("change_state"),
            c.c.merged_at,
            outcome.label("outcome"),
        )
        .select_from(p.outerjoin(c, c.c.proposal_id == p.c.id))
        .where(p.c.project_id == access.project_id, p.c.finding_ids.overlap(ids))
        .order_by(p.c.id)
    )
    found: dict[int, list[EarlierProposal]] = {}
    for row in await conn.execute(query):
        if not access.visible(row.label, through):
            continue
        status = proposal_status(row.state, row.change_state, row.merged_at, row.outcome)
        view = EarlierProposal(
            id=row.id,
            kind=row.kind,
            tier=row.tier,
            state=row.state,
            change_state=row.change_state,
            outcome=row.outcome,
            status=status,
            note=row.note,
        )
        for finding_id in row.finding_ids or []:
            if finding_id in ids:
                found.setdefault(finding_id, []).append(view)
    return found


def _view(row, proposals: list[EarlierProposal]) -> EarlierFinding:
    status = finding_status([item.status for item in proposals])
    return EarlierFinding(
        id=row.id,
        run_id=row.run_id,
        night=row.schedule_night or row.created_at.date(),
        lens=row.lens,
        severity=row.severity,
        title=row.title,
        status=status,
        closed=status in CLOSED,
        proposals=proposals,
        evidence=texts_of(row.evidence),
    )


async def findings_of(
    conn: AsyncConnection,
    access: ProjectAccess,
    through: str | None,
    *,
    lenses: list[str] | None = None,
    status: str | None = None,
    limit: int = 20,
    per_lens: int | None = None,
) -> list[EarlierFinding]:
    """The findings of the project readable through ``through``, newest first, each with its status."""
    rows = [
        row
        for row in await conn.execute(_findings(access.project_id, lenses))
        if through is not None and access.visible(row.label, through)
    ]
    proposals = await _proposals_of(conn, access, through, [row.id for row in rows])
    found, per = [], {}
    for row in rows:
        view = _view(row, proposals.get(row.id, []))
        if status is not None and view.status != status:
            continue
        if per_lens is not None:
            per[row.lens] = per.get(row.lens, 0) + 1
            if per[row.lens] > per_lens:
                continue
        found.append(view)
        if len(found) >= limit:
            break
    return found


async def rejected_lately(conn: AsyncConnection, access: ProjectAccess, through: str | None, limit: int) -> list:
    p = tables.proposals
    query = (
        select(p.c.id, p.c.lens, p.c.kind, p.c.title, p.c.note, p.c.answered_at, p.c.label)
        .where(
            p.c.project_id == access.project_id,
            p.c.state == "rejected",
            p.c.answered_at >= func.now() - timedelta(days=tiers.REJECTED_DAYS),
        )
        .order_by(p.c.answered_at.desc(), p.c.id.desc())
        .limit(SCAN)
    )
    found = []
    for row in await conn.execute(query):
        if through is not None and access.visible(row.label, through):
            found.append(RejectedProposal(**{key: getattr(row, key) for key in RejectedProposal.model_fields}))
            if len(found) >= limit:
                break
    return found


def _hub_sink(access: ProjectAccess) -> str | None:
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


async def for_prompt(conn: AsyncConnection, access: ProjectAccess, lenses: list[str]) -> dict:
    """What the prompt of a review run of ``lenses`` shows of earlier nights, as its owner (``access``) reads them."""
    through = _hub_sink(access)
    found = await findings_of(conn, access, through, lenses=lenses, limit=PER_LENS * len(lenses), per_lens=PER_LENS)
    by_lens = {lens: [] for lens in lenses}
    for item in found:
        by_lens.setdefault(item.lens, []).append(item.model_dump(mode="json"))
    rejected = await rejected_lately(conn, access, through, REJECTED_SHOWN)
    return {"findings": by_lens, "rejected": [item.model_dump(mode="json") for item in rejected]}


async def closed_like(conn: AsyncConnection, access: ProjectAccess, lens: str, title: str, evidence: list[dict]):
    """The closed finding of ``lens`` the new one repeats (see the module): its view, or None."""
    texts = set(texts_of(evidence))
    for found in await findings_of(conn, access, _hub_sink(access), lenses=[lens], status=None, limit=SCAN):
        cited = set(found.evidence)
        if found.closed and texts <= cited and (similar(title, found.title) or texts == cited):
            return found
    return None


async def earlier_view(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    sink: str | None,
    *,
    lens: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> EarlierView:
    """The findings ``user`` may read through ``sink``, with their status, and the proposals rejected lately."""
    access = await project_access(conn, user, project)
    need_reader(access)
    through = plan_routes._sink(access, sink)
    lenses = [lens] if lens is not None else None
    found = await findings_of(conn, access, through, lenses=lenses, status=status, limit=limit)
    rejected = [item for item in await rejected_lately(conn, access, through, limit) if lens in (None, item.lens)]
    return EarlierView(project=access.name, findings=found, rejected=rejected)


@router.get("/{project}/curator/earlier", response_model=EarlierView, responses=READ_REFUSALS)
async def show_earlier(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    lens: Annotated[Literal[tuple(review.LENSES)] | None, Query()] = None,
    status: Annotated[Literal[STATUSES] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> EarlierView:
    """What earlier review nights found, newest first, with what became of it, and the proposals rejected lately."""
    async with request.app.state.engine.begin() as conn:
        return await earlier_view(conn, user, project, sink, lens=lens, status=status, limit=limit)

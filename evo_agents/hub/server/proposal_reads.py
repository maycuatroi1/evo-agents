"""Reading the findings and proposals of a project, and the owner's answer to a proposal: the routes GET
/v1/projects/{p}/curator/findings, .../findings/{id}, .../proposals, .../proposals/{id} and POST
.../proposals/{id}/answer, and ``answer_proposal_as``, which the answer route, the Telegram channel and the project's
agent answer through. ``evo_agents.hub.server.proposals`` says what each does and records what a review run writes."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import review, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, memory_promotion
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server import probes as stored_probes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.change_views import Change, change_view
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.memory_audience import Reader, label_reaches
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposal_models import (
    Finding,
    FindingList,
    ItemId,
    Proposal,
    ProposalAnswer,
    ProposalCounts,
    ProposalList,
    ProposalSummary,
)
from evo_agents.hub.server.proposals import (
    READ_REFUSALS,
    REFUSALS,
    finding_view,
    findings_query,
    planned_tiers_of,
    proposal_view,
    proposals_query,
    require_reader,
)
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

PROPOSAL = "curator.proposal"  # the owner answered: "<project> proposal:<id> run:<id> answer=<accept|reject|defer>"
MAX_LIST = 200
MAX_OFFSET = 100_000

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})


def _summary(project: str, row) -> ProposalSummary:
    data = {key: row._mapping[key] for key in ProposalSummary.model_fields if key != "project"}
    return ProposalSummary(project=project, **data)


@router.get("/{project}/curator/findings", response_model=FindingList, responses=READ_REFUSALS)
async def list_findings(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID, description="of this review run")] = None,
    lens: Annotated[review.Lens | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> FindingList:
    """The findings of the project's review runs the caller may read, newest first."""
    f = tables.findings
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        query = findings_query().where(f.c.project_id == access.project_id)
        if run_id is not None:
            query = query.where(f.c.run_id == run_id)
        if lens is not None:
            query = query.where(f.c.lens == lens)
        rows = (await conn.execute(query.order_by(f.c.id.desc()).limit(MAX_OFFSET))).all()
    through = plan_routes._sink(access, sink)
    visible = [finding_view(access.name, row) for row in rows if through and access.visible(row.label, through)]
    return FindingList(findings=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


@router.get("/{project}/curator/findings/{finding_id}", response_model=Finding, responses=READ_REFUSALS)
async def show_finding(
    request: Request,
    project: ProjectName,
    finding_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Finding:
    f = tables.findings
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        query = findings_query().where(f.c.project_id == access.project_id, f.c.id == finding_id)
        row = (await conn.execute(query)).one_or_none()
    through = plan_routes._sink(access, sink)
    if row is None or through is None or not access.visible(row.label, through):
        raise HTTPException(404, f"project {project} has no finding {finding_id}")
    return finding_view(access.name, row)


@router.get("/{project}/curator/proposals", response_model=ProposalList, responses=READ_REFUSALS)
async def list_proposals(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    state: Annotated[list[review.ProposalState], Query(description="any of these; repeat it")] = [],  # noqa: B006
    tier: Annotated[list[Annotated[int, Field(ge=0, le=3)]], Query(description="any of these tiers; repeat it")] = [],  # noqa: B006
    lens: Annotated[review.Lens | None, Query()] = None,
    run_id: Annotated[int | None, Query(ge=1, le=MAX_ID, description="of this review run")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ProposalList:
    """The proposals of the project's review runs the caller may read, newest first."""
    p = tables.proposals
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        query = proposals_query().where(p.c.project_id == access.project_id)
        if run_id is not None:
            query = query.where(p.c.run_id == run_id)
        rows = (await conn.execute(query.order_by(p.c.id.desc()).limit(MAX_OFFSET))).all()
        through, reader = plan_routes._sink(access, sink), Reader(conn, user)
        readable = [row for row in rows if through and await label_reaches(reader, access, row.label, through)]
    states, tiers_asked = set(state), set(tier)

    def kept(row, but: str | None = None) -> bool:
        return (
            (but == "state" or not states or row.state in states)
            and (but == "tier" or not tiers_asked or row.tier in tiers_asked)
            and (but == "lens" or lens is None or row.lens == lens)
        )

    def counted(facet: str, values) -> dict[str, int]:
        found = Counter(str(getattr(row, facet)) for row in readable if kept(row, facet))
        return {str(value): found.get(str(value), 0) for value in values}

    counts = ProposalCounts(
        state=counted("state", review.PROPOSAL_STATES),
        tier=counted("tier", range(4)),
        lens=counted("lens", review.LENSES),
    )
    visible = [_summary(access.name, row) for row in readable if kept(row)]
    return ProposalList(
        proposals=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset, counts=counts
    )


async def readable_proposal(
    conn, access: ProjectAccess, proposal_id: int, sink: str | None, user: Principal, *, lock: bool = False
):
    """Proposal ``proposal_id`` of the project, its row locked with ``lock``, when its label reaches ``user`` through
    ``sink`` (else the project's hub sink), its audience included; 404 otherwise."""
    p = tables.proposals
    query = proposals_query().where(p.c.project_id == access.project_id, p.c.id == proposal_id)
    if lock:
        query = query.with_for_update(of=p)
    row = (await conn.execute(query)).one_or_none()
    through = plan_routes._sink(access, sink)
    if row is None or through is None or not await label_reaches(Reader(conn, user), access, row.label, through):
        raise HTTPException(404, f"project {access.name} has no proposal {proposal_id}")
    return row


async def _change_of(conn, access: ProjectAccess, proposal_id: int, sink: str | None) -> Change | None:
    """The change accepting proposal ``proposal_id`` made, when the caller may read its plan through ``sink`` (as GET
    .../curator/changes shows it); None without one."""
    c = tables.curator_changes
    query = select(c.c.id, c.c.plan_id).where(c.c.project_id == access.project_id, c.c.proposal_id == proposal_id)
    found = (await conn.execute(query)).one_or_none()
    if found is None or (found.plan_id is not None and found.plan_id not in await visible_plans(conn, access, sink)):
        return None
    return await change_view(conn, found.id)


@router.get("/{project}/curator/proposals/{proposal_id}", response_model=Proposal, responses=READ_REFUSALS)
async def show_proposal(
    request: Request,
    project: ProjectName,
    proposal_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Proposal:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        row = await readable_proposal(conn, access, proposal_id, sink, user)
        change = await _change_of(conn, access, proposal_id, sink)
        shown = stored_probes.verify_shown(access, user)
        probe = await stored_probes.of_proposal(conn, access.name, proposal_id, shown=shown)
    return proposal_view(access.name, row, change, probe)


async def answer_proposal_as(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    proposal_id: int,
    body: ProposalAnswer,
    *,
    via: str | None = None,
    agent: tuple[str, int] | None = None,
) -> Proposal:
    """Accept, reject or defer proposal ``proposal_id`` of ``project`` as ``user``, an admin of it, in the caller's
    transaction, and read its notifications. The answer route, the Telegram channel (``via="telegram"``, which the
    audit row names) and the project's agent for ``user`` (``agent``: its handle and chat run, ``curator_threads``)
    answer through it; it raises the HTTPException the route answers with."""
    if agent is not None:
        via = f"@{agent[0]} in chat run {agent[1]}"
    p, n = tables.proposals, tables.notifications
    access = await project_access(conn, user, project)
    require_reader(access)
    if not has_role(access.role, "admin"):
        raise HTTPException(
            403, f"answering a proposal of project {access.name} needs the admin role on it; you hold {access.role}"
        )
    row = await readable_proposal(conn, access, proposal_id, None, user, lock=True)
    if row.state not in ("open", "deferred"):
        raise HTTPException(409, f"proposal {proposal_id} is {row.state}: only an open or deferred one is answered")
    state = review.ANSWERED[body.action]
    days = body.defer_days or review.DEFAULT_DEFER_DAYS
    values = {
        "state": state,
        "answered_by": user.user_id,
        "answered_at": func.now(),
        "note": body.note,
        "deferred_until": func.now() + timedelta(days=days) if body.action == "defer" else None,
    }
    await conn.execute(update(p).values(**values).where(p.c.id == proposal_id))
    await ledger_lines.proposal_answered(
        conn,
        project_id=access.project_id,
        proposal_id=proposal_id,
        user_id=user.user_id,
        login=user.login,
        action=body.action,
        note=body.note,
        days=days if body.action == "defer" else None,
        via=via,
        agent=None if agent is None else f"@{agent[0]}",
        chat_run_id=None if agent is None else agent[1],
    )
    if body.action == "accept" and row.promotion is not None:
        await memory_promotion.promote(conn, access, user, proposal_id, row.promotion)
    elif body.action == "accept" and row.tier in await planned_tiers_of(conn, access.project_id):
        from evo_agents.hub.server.proposal_plans import plan_from_proposal  # it reads proposals through this module

        await plan_from_proposal(conn, access, user, row)
    await conn.execute(
        update(n).values(read_at=func.now()).where(n.c.proposal_id == proposal_id, n.c.read_at.is_(None))
    )
    target = f"{access.name} proposal:{proposal_id} run:{row.run_id} answer={body.action}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=PROPOSAL,
        target=target if via is None else f"{target} via={via}",
        project_id=access.project_id,
    )
    row = await readable_proposal(conn, access, proposal_id, None, user)
    shown = stored_probes.verify_shown(access, user)
    probe = await stored_probes.of_proposal(conn, access.name, proposal_id, shown=shown)
    return proposal_view(access.name, row, await _change_of(conn, access, proposal_id, None), probe)


@router.post("/{project}/curator/proposals/{proposal_id}/answer", response_model=Proposal, responses=REFUSALS)
async def answer_proposal(
    request: Request, project: ProjectName, proposal_id: ItemId, body: ProposalAnswer, user: CurrentUser
) -> Proposal:
    """Accept, reject or defer a proposal; the admins of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        answered = await answer_proposal_as(conn, user, project, proposal_id, body)
    log.info("proposal answered", extra={"proposal_id": proposal_id, "action": body.action, "login": user.login})
    return answered

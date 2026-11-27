"""Reading the findings and proposals of a project, and the owner's answer to a proposal: ``answer_proposal_as``,
which the answer route, the Telegram channel and the project's agent answer through
(``evo_agents.hub.server.proposals.service.record`` says what each does; the routes:
``evo_agents.hub.server.proposals.routes.reads``)."""

from __future__ import annotations

from collections import Counter

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import review
from evo_agents.hub.server import audit
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.changes.models.views import Change
from evo_agents.hub.server.changes.repository.views import change_view
from evo_agents.hub.server.memories.service import promotion as memory_promotion
from evo_agents.hub.server.memories.service.audience import Reader, label_reaches
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.probes.service import probes as stored_probes
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposals.models import (
    Finding,
    FindingList,
    ProposalAnswer,
    ProposalCounts,
    ProposalList,
    ProposalSummary,
)
from evo_agents.hub.server.proposals.repository import reads as store
from evo_agents.hub.server.proposals.service.record import (
    finding_view,
    planned_tiers_of,
    proposal_view,
    require_reader,
)
from evo_agents.hub.server.proposals.service.shapes import Proposal
from evo_agents.hub.server.runs.service.views import visible_plans
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.systems.service import curation as system_curation

PROPOSAL = "curator.proposal"  # the owner answered: "<project> proposal:<id> run:<id> answer=<accept|reject|defer>"
MAX_OFFSET = 100_000


def _summary(project: str, row) -> ProposalSummary:
    data = {key: row._mapping[key] for key in ProposalSummary.model_fields if key != "project"}
    return ProposalSummary(project=project, **data)


async def list_findings(
    engine: AsyncEngine, user: Principal, project: str, run_id, lens, limit: int, offset: int, sink: str | None
) -> FindingList:
    """The findings of the project's review runs the caller may read, newest first."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        rows = await store.project_findings(conn, access.project_id, run_id, lens, MAX_OFFSET)
    through = plan_service.read_sink(access, sink)
    visible = [finding_view(access.name, row) for row in rows if through and access.visible(row.label, through)]
    return FindingList(findings=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


async def show_finding(
    engine: AsyncEngine, user: Principal, project: str, finding_id: int, sink: str | None
) -> Finding:
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        row = await store.project_finding(conn, access.project_id, finding_id)
    through = plan_service.read_sink(access, sink)
    if row is None or through is None or not access.visible(row.label, through):
        raise HTTPException(404, f"project {project} has no finding {finding_id}")
    return finding_view(access.name, row)


async def list_proposals(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    asked: tuple[list, list, object, int | None],
    limit: int,
    offset: int,
    sink: str | None,
) -> ProposalList:
    """The proposals of the project's review runs the caller may read, newest first; ``asked`` is (states, tiers,
    lens, run_id) the caller filters by."""
    state, tier, lens, run_id = asked
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        rows = await store.project_proposals(conn, access.project_id, run_id, MAX_OFFSET)
        through, reader = plan_service.read_sink(access, sink), Reader(conn, user)
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
    row = await store.project_proposal(conn, access.project_id, proposal_id, lock=lock)
    through = plan_service.read_sink(access, sink)
    if row is None or through is None or not await label_reaches(Reader(conn, user), access, row.label, through):
        raise HTTPException(404, f"project {access.name} has no proposal {proposal_id}")
    return row


async def _change_of(conn, access: ProjectAccess, proposal_id: int, sink: str | None) -> Change | None:
    """The change accepting proposal ``proposal_id`` made, when the caller may read its plan through ``sink`` (as GET
    .../curator/changes shows it); None without one."""
    found = await store.change_of_proposal(conn, access.project_id, proposal_id)
    if found is None or (found.plan_id is not None and found.plan_id not in await visible_plans(conn, access, sink)):
        return None
    return await change_view(conn, found.id)


async def show_proposal(
    engine: AsyncEngine, user: Principal, project: str, proposal_id: int, sink: str | None
) -> Proposal:
    async with engine.begin() as conn:
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
    access = await project_access(conn, user, project)
    require_reader(access)
    if not holds(access.role, "admin"):
        raise HTTPException(
            403, f"answering a proposal of project {access.name} needs the admin role on it; you hold {access.role}"
        )
    row = await readable_proposal(conn, access, proposal_id, None, user, lock=True)
    if row.state not in ("open", "deferred"):
        raise HTTPException(409, f"proposal {proposal_id} is {row.state}: only an open or deferred one is answered")
    state = review.ANSWERED[body.action]
    days = body.defer_days or review.DEFAULT_DEFER_DAYS
    await store.answer(conn, proposal_id, user.user_id, body.action, state, body.note, days)
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
    elif body.action == "accept" and row.system_change is not None:
        await system_curation.apply(conn, access, user, proposal_id, row.run_id, row.system_change)
    elif body.action == "accept" and row.tier in await planned_tiers_of(conn, access.project_id):
        from evo_agents.hub.server.proposal_plans.service import (
            plan_from_proposal,  # it reads proposals through this module
        )

        await plan_from_proposal(conn, access, user, row)
    await store.read_notifications(conn, proposal_id)
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


async def answer_proposal(
    engine: AsyncEngine, user: Principal, project: str, proposal_id: int, body: ProposalAnswer
) -> Proposal:
    """Accept, reject or defer a proposal in a transaction of its own (``answer_proposal_as``)."""
    async with engine.begin() as conn:
        return await answer_proposal_as(conn, user, project, proposal_id, body)

"""The evidence of an opportunity: what a line shows as (``view``), what is wrong with lines added to an opportunity
(``wrong_lines``), and what the routes (``read``, ``add``, ``confirm``), @builder's kind ``opportunity`` and the intake
(``add_lines``, ``remove_proposed``) do with them. Each opens its transaction or writes in its caller's, waits for the
lock of the project's opportunities before a write, and records each add, confirm or removal as an audit row
``opportunity.evidence``."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import opportunities, problems
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable, refuse
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.opportunities import service as project_opportunities
from evo_agents.hub.server.opportunity_evidence.models import OpportunityEvidence, OpportunityEvidenceWrite
from evo_agents.hub.server.opportunity_evidence.repository import STORE
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# Reading


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
    return [view(access.name, row) for row in await STORE.lines(conn, held.id)]


async def read(engine: AsyncEngine, user: Principal, project: str, ref: str) -> list[OpportunityEvidence]:
    """The evidence of opportunity ``ref`` by number, for a member with a grant on the project, as the route reads
    it."""
    async with UnitOfWork(engine) as conn:
        return await listed(conn, await project_opportunities.reader(conn, user, project), ref)


# Writing


async def wrong_lines(
    conn: AsyncConnection, opportunity_id: int | None, lines: list[OpportunityEvidenceWrite]
) -> list[str]:
    """Each thing wrong with ``lines`` as additions to opportunity ``opportunity_id`` (None: a new opportunity): a line
    twice, a line the opportunity has already, more lines than an opportunity holds."""
    wrong: list[str] = []
    held: dict[tuple, str] = {}
    count = 0
    if opportunity_id is not None:
        for row in await STORE.held(conn, opportunity_id):
            held[(row.source_kind, row.source_uri, row.locator, row.excerpt)] = problems.evidence_code(row.number)
            count += 1
    seen: set[tuple] = set()
    for at, line in enumerate(lines, start=1):
        key = line.same()
        if key in held:
            wrong.append(f"evidence {at}: the opportunity has this line already as {held[key]}")
        elif key in seen:
            wrong.append(f"evidence {at}: the same line is given twice")
        seen.add(key)
    if count + len(lines) > opportunities.MAX_EVIDENCE:
        wrong.append(f"an opportunity holds at most {opportunities.MAX_EVIDENCE} lines of evidence")
    return wrong


async def _audit(conn: AsyncConnection, user: Principal, access: ProjectAccess, number: int, what: str) -> None:
    target = f"{access.name}/opportunity {opportunities.code(number)} evidence={what}"
    await Auditor.of(conn, user, access).record(audit.OPPORTUNITY_EVIDENCE, target)


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
    refuse(await wrong_lines(conn, opportunity_id, lines))
    first = await STORE.last_number(conn, opportunity_id)
    codes = []
    for at, line in enumerate(lines, start=first + 1):
        values = {**line.model_dump(), "opportunity_id": opportunity_id, "number": at, "added_by": user.user_id}
        await STORE.insert(conn, {**values, "proposal_id": proposal_id})
        codes.append(problems.evidence_code(at))
    if codes:
        via = f" proposal={proposal_id}" if proposal_id is not None else ""
        number = await STORE.number_of(conn, opportunity_id)
        await _audit(conn, user, access, number, f"{','.join(codes)} added{via}")
    return codes


async def remove_proposed(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, opportunity_id: int, proposal_id: int
) -> int:
    """Take away the lines proposal ``proposal_id`` of @builder made in opportunity ``opportunity_id``, as its rollback
    does; how many."""
    gone = await STORE.remove_proposed(conn, opportunity_id, proposal_id)
    if gone:
        codes = ",".join(problems.evidence_code(n) for n in sorted(gone))
        number = await STORE.number_of(conn, opportunity_id)
        await _audit(conn, user, access, number, f"{codes} removed proposal={proposal_id}")
    return len(gone)


async def _graph_changed(jobs, conn: AsyncConnection, access: ProjectAccess) -> None:
    """Queue a build of the project's knowledge graph, which holds its opportunities and their lines."""
    await queue_if_configured(jobs, conn, access.project_id, access.name)


async def add(
    engine: AsyncEngine, jobs, user: Principal, project: str, ref: str, body: OpportunityEvidenceWrite
) -> OpportunityEvidence:
    """Add line ``body`` to opportunity ``ref`` as a writer of the project, as the route does: 409 for a line the
    opportunity has already, 422 for another thing wrong."""
    async with UnitOfWork(engine) as conn:
        access = await project_opportunities.writer(conn, user, project)
        await project_opportunities.lock(conn, access.project_id)
        held = await project_opportunities.one(conn, access, ref)
        try:
            (code,) = await add_lines(conn, user, access, held.id, [body])
        except Unprocessable as exc:
            if "has this line already" in exc.message:
                raise Conflict(exc.message) from None
            raise
        await _graph_changed(jobs, conn, access)
        row = await STORE.line(conn, held.id, int(code[2:]))
    return view(access.name, row)


async def confirm(engine: AsyncEngine, jobs, user: Principal, project: str, ref: str, code: str) -> OpportunityEvidence:
    """Confirm line ``code`` of opportunity ``ref`` as a writer of the project, once: 404 for a line it has not, 409
    for one confirmed already."""
    number = int(code[2:])
    async with UnitOfWork(engine) as conn:
        access = await project_opportunities.writer(conn, user, project)
        await project_opportunities.lock(conn, access.project_id)
        held = await project_opportunities.one(conn, access, ref)
        row = await STORE.line(conn, held.id, number)
        if row is None:
            raise NotFound(f"opportunity {ref} of project {access.name} has no evidence {code}")
        if row.confirmed_by is not None:
            raise Conflict(
                f"evidence {code} of opportunity {ref} was confirmed by {row.confirmed_by} already; nothing changed"
            )
        await STORE.confirm(conn, held.id, number, user.user_id)
        await _audit(conn, user, access, held.number, f"{code} confirmed")
        await _graph_changed(jobs, conn, access)
        row = await STORE.line(conn, held.id, number)
    return view(access.name, row)

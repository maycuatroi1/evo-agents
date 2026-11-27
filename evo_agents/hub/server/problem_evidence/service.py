"""The evidence of a problem: what a line shows as (``view``), what is wrong with lines added to a problem
(``wrong_lines``), and what the routes (``read``, ``add``, ``confirm``) and @builder's kind ``problem`` (``add_lines``,
``remove_proposed``) do with them. Each opens its transaction or writes in its caller's, waits for the lock of the
project's problems before a write, and records each add, confirm or removal as an audit row ``problem.evidence``."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import problems
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable, refuse
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.problem_evidence.models import ProblemEvidence, ProblemEvidenceWrite
from evo_agents.hub.server.problem_evidence.repository import STORE
from evo_agents.hub.server.problems import service as project_problems
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# Reading


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
    held = await project_problems.one(conn, access, ref)
    return [view(access.name, row) for row in await STORE.lines(conn, held.id)]


async def read(engine: AsyncEngine, user: Principal, project: str, ref: str) -> list[ProblemEvidence]:
    """The evidence of problem ``ref`` by number, for a member with a grant on the project, as the route reads it."""
    async with UnitOfWork(engine) as conn:
        return await listed(conn, await project_problems.reader(conn, user, project), ref)


# Writing


async def wrong_lines(conn: AsyncConnection, problem_id: int | None, lines: list[ProblemEvidenceWrite]) -> list[str]:
    """Each thing wrong with ``lines`` as additions to problem ``problem_id`` (None: a new problem): a line twice, a
    line the problem has already, more lines than a problem holds."""
    wrong: list[str] = []
    held: dict[tuple, str] = {}
    count = 0
    if problem_id is not None:
        for row in await STORE.held(conn, problem_id):
            held[(row.source_kind, row.source_uri, row.locator, row.excerpt)] = problems.evidence_code(row.number)
            count += 1
    seen: set[tuple] = set()
    for at, line in enumerate(lines, start=1):
        key = line.same()
        if key in held:
            wrong.append(f"evidence {at}: the problem has this line already as {held[key]}")
        elif key in seen:
            wrong.append(f"evidence {at}: the same line is given twice")
        seen.add(key)
    if count + len(lines) > problems.MAX_EVIDENCE:
        wrong.append(f"a problem holds at most {problems.MAX_EVIDENCE} lines of evidence")
    return wrong


async def _audit(conn: AsyncConnection, user: Principal, access: ProjectAccess, number: int, what: str) -> None:
    target = f"{access.name}/problem {problems.code(number)} evidence={what}"
    await Auditor.of(conn, user, access).record(audit.PROBLEM_EVIDENCE, target)


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
    refuse(await wrong_lines(conn, problem_id, lines))
    first = await STORE.last_number(conn, problem_id)
    codes = []
    for at, line in enumerate(lines, start=first + 1):
        values = {**line.model_dump(), "problem_id": problem_id, "number": at, "added_by": user.user_id}
        await STORE.insert(conn, {**values, "proposal_id": proposal_id})
        codes.append(problems.evidence_code(at))
    if codes:
        via = f" proposal={proposal_id}" if proposal_id is not None else ""
        await _audit(conn, user, access, await STORE.number_of(conn, problem_id), f"{','.join(codes)} added{via}")
    return codes


async def remove_proposed(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, problem_id: int, proposal_id: int
) -> int:
    """Take away the lines proposal ``proposal_id`` of @builder made in problem ``problem_id``, as its rollback does;
    how many."""
    gone = await STORE.remove_proposed(conn, problem_id, proposal_id)
    if gone:
        codes = ",".join(problems.evidence_code(n) for n in sorted(gone))
        await _audit(
            conn, user, access, await STORE.number_of(conn, problem_id), f"{codes} removed proposal={proposal_id}"
        )
    return len(gone)


async def _graph_changed(jobs, conn: AsyncConnection, access: ProjectAccess) -> None:
    """Queue a build of the project's knowledge graph, which holds the lines as edges of the problem's node."""
    await queue_if_configured(jobs, conn, access.project_id, access.name)


async def add(
    engine: AsyncEngine, jobs, user: Principal, project: str, ref: str, body: ProblemEvidenceWrite
) -> ProblemEvidence:
    """Add line ``body`` to problem ``ref`` as a writer of the project, as the route does: 409 for a line the problem
    has already, 422 for another thing wrong."""
    async with UnitOfWork(engine) as conn:
        access = await project_problems.writer(conn, user, project)
        await project_problems.lock(conn, access.project_id)
        held = await project_problems.one(conn, access, ref)
        try:
            (code,) = await add_lines(conn, user, access, held.id, [body])
        except Unprocessable as exc:
            if "has this line already" in exc.message:
                raise Conflict(exc.message) from None
            raise
        await _graph_changed(jobs, conn, access)
        row = await STORE.line(conn, held.id, int(code[2:]))
    return view(access.name, row)


async def confirm(engine: AsyncEngine, jobs, user: Principal, project: str, ref: str, code: str) -> ProblemEvidence:
    """Confirm line ``code`` of problem ``ref`` as a writer of the project, once: 404 for a line it has not, 409 for
    one confirmed already."""
    number = int(code[2:])
    async with UnitOfWork(engine) as conn:
        access = await project_problems.writer(conn, user, project)
        await project_problems.lock(conn, access.project_id)
        held = await project_problems.one(conn, access, ref)
        row = await STORE.line(conn, held.id, number)
        if row is None:
            raise NotFound(f"problem {ref} of project {access.name} has no evidence {code}")
        if row.confirmed_by is not None:
            raise Conflict(
                f"evidence {code} of problem {ref} was confirmed by {row.confirmed_by} already; nothing changed"
            )
        await STORE.confirm(conn, held.id, number, user.user_id)
        await _audit(conn, user, access, held.number, f"{code} confirmed")
        await _graph_changed(jobs, conn, access)
        row = await STORE.line(conn, held.id, number)
    return view(access.name, row)

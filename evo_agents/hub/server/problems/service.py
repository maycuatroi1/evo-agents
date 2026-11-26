"""The problems of a project: who may read and write them (``READERS``, ``WRITERS``), what a problem shows (its
evidence, the goals, claims and plans that point to it) and what a write checks and records (``write``)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, problems
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, refuse
from evo_agents.hub.server.base.scope import Noun, ProjectScope, Scope
from evo_agents.hub.server.goals import service as project_goals
from evo_agents.hub.server.problems import repository
from evo_agents.hub.server.problems.models import (  # noqa: F401 (ProblemRef: the path of a problem, for its callers)
    ProblemClaim,
    ProblemGoal,
    ProblemPlan,
    ProblemRef,
    ProblemRevision,
    ProjectProblem,
    ProjectProblemClose,
    ProjectProblemShown,
    ProjectProblemWrite,
)
from evo_agents.hub.server.problems.repository import STORE
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

OF = "a problem's"  # how the checks shared with the goals name what they check
NOUN = Noun("a problem", "the problems")
READERS = ProjectScope("reader", NOUN)
WRITERS = ProjectScope("writer", NOUN)

found = STORE.found
lock = STORE.lock
next_code = STORE.next_code
refusal = WRITERS.refusal
reader = READERS.check  # 404 without a grant (``project_access``), 403 for a hub admin without one
writer = WRITERS.check


# Reading


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


async def evidence_counts(conn: AsyncConnection, problem_id: int) -> tuple[int, int]:
    """(lines of evidence of the problem, the independent sources they come from)."""
    rows = await repository.evidence_sources(conn, problem_id)
    return len(rows), problems.sources_of([row._mapping for row in rows])


async def answered_by(conn: AsyncConnection, problem_id: int) -> list[ProblemGoal]:
    """The goals whose newest revision answers problem ``problem_id``, by code."""
    return [
        ProblemGoal(code=goals.code(row.number), revision=row.revision, statement=row.statement, status=row.status)
        for row in await repository.answering_goals(conn, problem_id)
    ]


def _hub_sink(access: ProjectAccess) -> str | None:
    """The sink a read of the problem's claims and plans goes through: the project's hub sink."""
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


async def claimed_by(conn: AsyncConnection, access: ProjectAccess, code: str) -> list[ProblemClaim]:
    """The claims of the project's canvases that link problem ``code``, neither archived nor in an archived canvas, that
    the member sees, by canvas and number."""
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
        for row in await repository.linking_claims(conn, access.project_id, code)
        if through is not None and access.visible(row.label, through)
    ]


async def named_by(conn: AsyncConnection, access: ProjectAccess, code: str) -> list[ProblemPlan]:
    """The plans of the project whose body names problem ``code`` in its problems, that the member sees, by id."""
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
        for row in await repository.naming_plans(conn, access.project_id, code)
        if through is not None and access.visible(row.label, through)
    ]


async def one(conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None):
    """Problem ``ref`` of the project at ``revision`` (its newest when None); 404 when there is no such one."""
    row = await found(conn, access.project_id, ref, revision)
    if row is None:
        at = "" if revision is None else f" revision {revision}"
        raise NotFound(f"project {access.name} has no problem {ref}{at}")
    return row


async def listed(conn: AsyncConnection, access: ProjectAccess, status: str | None = None) -> list[ProjectProblem]:
    """The project's problems as they are now, by rank, then by code; those of ``status`` alone when given."""
    return [view(access.name, row) for row in await STORE.listed(conn, access.project_id, status)]


async def shown(
    conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None
) -> ProjectProblemShown:
    """Problem ``ref`` at ``revision`` (its newest when None), with the goals that answer it now, and the claims and
    plans that point to it now."""
    row = await one(conn, access, ref, revision)
    code = problems.code(row.number)
    lines, sources = await evidence_counts(conn, row.id)
    return ProjectProblemShown(
        **view(access.name, row).model_dump(),
        evidence_count=lines,
        source_count=sources,
        goals=await answered_by(conn, row.id),
        claims=await claimed_by(conn, access, code),
        plans=await named_by(conn, access, code),
    )


async def revisions(scope: Scope, ref: str) -> list[ProblemRevision]:
    """Every revision of problem ``ref``, the newest first, each with the fields whose value it changed."""
    held = await one(scope.conn, scope.access, ref)
    listed_revisions, before = [], {}
    for row in await STORE.revisions_of(scope.conn, held.id):
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


# Writing


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
    rank = body.rank or (held.rank if held is not None else await STORE.next_rank(conn, access.project_id))
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


async def check(conn: AsyncConnection, access: ProjectAccess, ref: str, body: ProjectProblemWrite) -> list[str]:
    """Each thing wrong with ``body`` as the next revision of problem ``ref``, as ``write`` would find it (an
    HTTPException for a new problem whose code is not the next one); nothing written."""
    held = await found(conn, access.project_id, ref)
    if held is None:
        await STORE.number_of_new(conn, access, ref)
    return (await _values(conn, access, body, held, writer_id=0))[1]  # the writer only owns a new problem by default


async def write(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, ref: str, body: ProjectProblemWrite, *, via: str = ""
) -> tuple[ProjectProblem, bool]:
    """Write ``body`` as the next revision of problem ``ref`` of the project, once it is right (422 naming each thing
    wrong otherwise) and while the problem is at ``body.if_revision`` (409 otherwise); (the problem now, whether a
    revision was written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked ``refusal``."""

    async def values_of(held) -> dict:
        values, wrong = await _values(conn, access, body, held, user.user_id)
        refuse(wrong)
        return values

    revised = await STORE.revise(conn, access, ref, body.if_revision, values_of, note=body.note, writer=user.user_id)
    held, after = revised.before, revised.after
    if not revised.changed:
        return view(access.name, held), False
    code = problems.code(after.number)
    keys = problems.changed(fields_of(held) if held is not None else {}, fields_of(after))
    target = f"{access.name}/problem {code} revision={after.revision} fields={','.join(keys)}{via}"
    await Auditor.of(conn, user, access).record(audit.PROBLEM_SET, target)
    log.info("problem written", extra={"project": access.name, "problem": code, "revision": after.revision})
    return view(access.name, after), True


def closing(row, status: str, reason: str, note: str | None, if_revision: int | None) -> ProjectProblemWrite:
    """Problem ``row`` closed as ``status`` (solved or dropped) for ``reason``, as a write of its next revision."""
    fields = {**fields_of(row), "status": status, "close_reason": reason}
    return ProjectProblemWrite(**fields, note=note, if_revision=row.revision if if_revision is None else if_revision)


async def _graph_changed(scope: Scope, changed: bool) -> None:
    """Queue a build of the project's knowledge graph, which holds its problems (``evo_agents.hub.kg_problems``), once
    a write changed one."""
    if changed:
        await queue_if_configured(scope.jobs, scope.conn, scope.access.project_id, scope.access.name)


async def put(scope: Scope, ref: str, body: ProjectProblemWrite) -> tuple[ProjectProblem, bool]:
    """Write a problem whole as its next revision, as the route does."""
    written, changed = await write(scope.conn, scope.user, scope.access, ref, body)
    await _graph_changed(scope, changed)
    return written, changed


async def close(scope: Scope, ref: str, body: ProjectProblemClose) -> tuple[ProjectProblem, bool]:
    """Close problem ``ref`` as solved or dropped, for a reason, as its next revision; 409 when it is closed already."""
    held = await one(scope.conn, scope.access, ref)
    if held.status != problems.OPEN:
        raise Conflict(
            f"problem {ref} of project {scope.access.name} is {held.status} already ({held.close_reason}); nothing "
            "changed"
        )
    closed = closing(held, body.status, body.reason, body.note, body.if_revision)
    written, changed = await write(scope.conn, scope.user, scope.access, ref, closed)
    await _graph_changed(scope, changed)
    return written, changed

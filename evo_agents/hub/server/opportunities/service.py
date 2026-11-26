"""The opportunities of a project: who may read and write them (``READERS``, ``WRITERS``), which answers a reader sees
(``Sight``) and what a write checks and records (``write``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import opportunities, problems
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, refuse
from evo_agents.hub.server.base.scope import Noun, ProjectScope, Scope
from evo_agents.hub.server.goals import service as project_goals
from evo_agents.hub.server.opportunities import repository
from evo_agents.hub.server.opportunities.models import (  # noqa: F401 (OpportunityRef: the path of one, for its callers)
    Answer,
    OpportunityRef,
    OpportunityRevision,
    ProjectOpportunity,
    ProjectOpportunityClose,
    ProjectOpportunityShown,
    ProjectOpportunityWrite,
)
from evo_agents.hub.server.opportunities.repository import STORE
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

OF = "an opportunity's"  # how the checks shared with the goals name what they check
NOUN = Noun("an opportunity", "the opportunities")
READERS = ProjectScope("reader", NOUN)
WRITERS = ProjectScope("writer", NOUN)

found = STORE.found
lock = STORE.lock
next_code = STORE.next_code
refusal = WRITERS.refusal
reader = READERS.check  # 404 without a grant (``project_access``), 403 for a hub admin without one
writer = WRITERS.check


# Reading


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


async def evidence_counts(conn: AsyncConnection, opportunity_id: int) -> tuple[int, int]:
    """(lines of evidence of the opportunity, the independent sources they come from)."""
    rows = await repository.evidence_sources(conn, opportunity_id)
    return len(rows), problems.sources_of([row._mapping for row in rows])


async def one(conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None):
    """Opportunity ``ref`` of the project at ``revision`` (its newest when None); 404 when there is no such one."""
    row = await found(conn, access.project_id, ref, revision)
    if row is None:
        at = "" if revision is None else f" revision {revision}"
        raise NotFound(f"project {access.name} has no opportunity {ref}{at}")
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
    rows = await STORE.listed(conn, access.project_id, status, closing_within)
    sight = Sight(conn, user, access)
    return [await view(conn, sight, row) for row in rows]


async def shown(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, ref: str, revision: int | None = None
) -> ProjectOpportunityShown:
    """Opportunity ``ref`` at ``revision`` (its newest when None), with its evidence counted."""
    row = await one(conn, access, ref, revision)
    lines, sources = await evidence_counts(conn, row.id)
    seen = await view(conn, Sight(conn, user, access), row)
    return ProjectOpportunityShown(**seen.model_dump(), evidence_count=lines, source_count=sources)


async def revisions(scope: Scope, ref: str) -> list[OpportunityRevision]:
    """Every revision of opportunity ``ref``, the newest first, each with the fields whose value it changed."""
    held = await one(scope.conn, scope.access, ref)
    listed_revisions, before = [], {}
    for row in await STORE.revisions_of(scope.conn, held.id):
        after = fields_of(row, scope.access.project_id)
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


# Writing


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
            project_id = await _granted(conn, user, project)
        row = await repository.problem_of(conn, project_id, number) if project_id is not None else None
        if row is None or row.status == problems.DROPPED:
            grant = "" if project is None else f", or the writer holds no grant on project {project}"
            wrong.append(f"answers {text}: the problem does not exist, or it is dropped{grant}")
        else:
            ids.append(row.id)
    if len(ids) > opportunities.MAX_ANSWERS:
        wrong.append(f"an opportunity answers at most {opportunities.MAX_ANSWERS} problems")
    return await _with_hidden(conn, user, access, ids, held), wrong


async def _granted(conn: AsyncConnection, user: Principal, project: str) -> int | None:
    """The id of ``project`` when ``user`` holds a grant on it; None otherwise."""
    try:
        other = await project_access(conn, user, project)
    except HTTPException:
        return None
    return other.project_id if other.role is not None else None


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


async def values(
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
    rank = body.rank or (held.rank if held is not None else await STORE.next_rank(conn, access.project_id))
    written = {
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
    return written, wrong


async def check(conn: AsyncConnection, access: ProjectAccess, ref: str, body: ProjectOpportunityWrite) -> list[str]:
    """Each thing wrong with ``body`` as the next revision of opportunity ``ref``, as ``write`` would find it (an
    HTTPException for a new opportunity whose code is not the next one); nothing written. The answers of other
    projects are not checked: whoever approves the write is."""
    held = await found(conn, access.project_id, ref)
    if held is None:
        await STORE.number_of_new(conn, access, ref)
    return (await values(conn, None, access, body, held))[1]


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

    async def values_of(held) -> dict:
        written, wrong = await values(conn, user, access, body, held)
        refuse(wrong)
        return written

    revised = await STORE.revise(conn, access, ref, body.if_revision, values_of, note=body.note, writer=user.user_id)
    held, after = revised.before, revised.after
    sight = Sight(conn, user, access)
    if not revised.changed:
        return await view(conn, sight, held), False
    code = opportunities.code(after.number)
    own = access.project_id
    keys = opportunities.changed(fields_of(held, own) if held is not None else {}, fields_of(after, own))
    target = f"{access.name}/opportunity {code} revision={after.revision} fields={','.join(keys)}{via}"
    await Auditor.of(conn, user, access).record(audit.OPPORTUNITY_SET, target)
    log.info("opportunity written", extra={"project": access.name, "opportunity": code, "revision": after.revision})
    return await view(conn, sight, after), True


def closing(
    row, own: int, status: str, reason: str, note: str | None, if_revision: int | None
) -> ProjectOpportunityWrite:
    """Opportunity ``row`` closed as ``status`` for ``reason``, as a write of its next revision."""
    fields = {**fields_of(row, own), "status": status, "close_reason": reason}
    return ProjectOpportunityWrite(
        **fields, note=note, if_revision=row.revision if if_revision is None else if_revision
    )


async def _graph_changed(scope: Scope, changed: bool) -> None:
    """Queue a build of the project's knowledge graph, which holds its opportunities (``evo_agents.hub.kg_opportunities``),
    once a write changed one."""
    if changed:
        await queue_if_configured(scope.jobs, scope.conn, scope.access.project_id, scope.access.name)


async def put(scope: Scope, ref: str, body: ProjectOpportunityWrite) -> tuple[ProjectOpportunity, bool]:
    """Write an opportunity whole as its next revision, as the route does."""
    written, changed = await write(scope.conn, scope.user, scope.access, ref, body)
    await _graph_changed(scope, changed)
    return written, changed


async def close(scope: Scope, ref: str, body: ProjectOpportunityClose) -> tuple[ProjectOpportunity, bool]:
    """Close opportunity ``ref`` as captured, missed or dropped, for a reason, as its next revision; 409 when it is
    closed already."""
    held = await one(scope.conn, scope.access, ref)
    if held.status != opportunities.OPEN:
        raise Conflict(
            f"opportunity {ref} of project {scope.access.name} is {held.status} already ({held.close_reason}); nothing "
            "changed"
        )
    own = scope.access.project_id
    closed = closing(held, own, body.status, body.reason, body.note, body.if_revision)
    written, changed = await write(scope.conn, scope.user, scope.access, ref, closed)
    await _graph_changed(scope, changed)
    return written, changed

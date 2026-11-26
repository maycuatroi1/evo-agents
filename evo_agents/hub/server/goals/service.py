"""The goals of a project: who may read and write them (``READERS``, ``ADMINS``), what a write checks and records
(``write``), the goals of the Curator's charter (``adopt_charter_goals``) and what the Curator reads
(``curator_goals``)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals
from evo_agents.hub import opportunities as opportunity_model
from evo_agents.hub import problems as problem_model
from evo_agents.hub.access import has_role
from evo_agents.hub.kg_build import queue_if_configured
from evo_agents.hub.server import audit, brainstorms
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound, refuse
from evo_agents.hub.server.base.scope import Noun, ProjectScope, Scope
from evo_agents.hub.server.goals import repository
from evo_agents.hub.server.goals.models import (
    ALIAS,
    BRAINSTORMED_FOR,
    NUMBER_KIND,
    GoalRevision,
    ProjectGoal,
    ProjectGoalClose,
    ProjectGoalWrite,
)
from evo_agents.hub.server.goals.repository import STORE
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.hub.srs_backlog import BACKLOG_METRIC

log = logging.getLogger(__name__)

NOUN = Noun("a goal", "the goals")
READERS = ProjectScope("reader", NOUN)
ADMINS = ProjectScope("admin", NOUN)

found = STORE.found
next_code = STORE.next_code
refusal = ADMINS.refusal


# Reading


def view(project: str, row) -> ProjectGoal:
    return ProjectGoal(
        project=project,
        code=goals.code(row.number),
        number=row.number,
        alias=row.alias,
        revision=row.revision,
        statement=row.statement,
        metric=row.metric,
        deadline=row.deadline,
        canvas=row.canvas,
        problem=_problem_code(row),
        opportunity=_opportunity_code(row),
        srs_document=row.srs_document,
        srs_heading=row.srs_heading,
        rank=row.rank,
        owner=row.owner,
        status=row.status,
        close_reason=row.close_reason,
        note=row.note,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _problem_code(row) -> str | None:
    return problem_model.code(row.problem_number) if row.problem_number is not None else None


def _opportunity_code(row) -> str | None:
    return opportunity_model.code(row.opportunity_number) if row.opportunity_number is not None else None


def fields_of(row) -> dict:
    """The fields of goal ``row`` as a write names them (``ProjectGoalWrite``), the deadline as text."""
    return {
        "statement": row.statement,
        "metric": dict(row.metric) if row.metric is not None else None,
        "deadline": row.deadline.isoformat() if row.deadline is not None else None,
        "canvas": row.canvas,
        "problem": _problem_code(row),
        "opportunity": _opportunity_code(row),
        "srs_document": row.srs_document,
        "srs_heading": row.srs_heading,
        "rank": row.rank,
        "owner": row.owner,
        "status": row.status,
        "close_reason": row.close_reason,
    }


async def one(conn: AsyncConnection, access: ProjectAccess, ref: str, revision: int | None = None):
    """Goal ``ref`` of the project at ``revision`` (its newest when None); 404 when there is no such one."""
    row = await found(conn, access.project_id, ref, revision)
    if row is None:
        at = "" if revision is None else f" revision {revision}"
        raise NotFound(f"project {access.name} has no goal {ref}{at}")
    return row


async def listed(scope: Scope, status: str | None = None) -> list[ProjectGoal]:
    """The project's goals as they are now, by rank, then by code; the goals of its charter made goals first."""
    await adopt_charter_goals(scope.conn, scope.access.project_id, scope.access.name)
    rows = await STORE.listed(scope.conn, scope.access.project_id, status)
    return [view(scope.access.name, row) for row in rows]


async def shown(scope: Scope, ref: str, revision: int | None = None) -> ProjectGoal:
    await adopt_charter_goals(scope.conn, scope.access.project_id, scope.access.name)
    return view(scope.access.name, await one(scope.conn, scope.access, ref, revision))


async def revisions(scope: Scope, ref: str) -> list[GoalRevision]:
    """Every revision of goal ``ref``, the newest first, each with the fields whose value it changed."""
    await adopt_charter_goals(scope.conn, scope.access.project_id, scope.access.name)
    held = await one(scope.conn, scope.access, ref)
    listed_revisions, before = [], {}
    for row in await STORE.revisions_of(scope.conn, held.id):
        after = fields_of(row)
        listed_revisions.append(
            GoalRevision(
                revision=row.revision,
                status=row.status,
                note=row.note,
                created_by=row.created_by,
                created_at=row.created_at,
                changed=goals.changed(before, after),
            )
        )
        before = after
    return listed_revisions[::-1]


# The goals of the Curator's charter


def _charter_goals(body: dict) -> list[tuple[str, str]]:
    """(id, text) of each goal of a charter's body that a goal may keep as its alias, in the charter's order."""
    listed_goals = []
    for goal in body.get("goals") or []:
        alias = goal.get("id") if isinstance(goal, dict) else None
        if isinstance(alias, str) and ALIAS.fullmatch(alias):
            text = str(goal.get("what") or "").strip()[: goals.MAX_STATEMENT_CHARS] or alias  # as long as a goal's
            listed_goals.append((alias, text))
    return listed_goals


async def _missing_aliases(conn: AsyncConnection, project_id: int, wanted: list[tuple[str, str]]) -> list:
    taken = await STORE.aliases_taken(conn, project_id, [alias for alias, _ in wanted])
    return [(alias, text) for alias, text in wanted if alias not in taken]


async def adopt_charter_goals(conn: AsyncConnection, project_id: int, project: str) -> int:
    """Make a goal of each goal of the project's newest charter that no goal keeps as its alias yet (see the
    package); how many it made."""
    charter = await STORE.newest_charter(conn, project_id)
    if charter is None:
        return 0
    wanted = _charter_goals(dict(charter.body))
    if not wanted or not await _missing_aliases(conn, project_id, wanted):
        return 0
    await STORE.lock(conn, project_id)
    missing = await _missing_aliases(conn, project_id, wanted)  # another reader may have made them meanwhile
    number, rank = await STORE.next_number(conn, project_id), await STORE.next_rank(conn, project_id)
    auditor = Auditor(conn, project_id)  # the hub makes them: no actor
    for offset, (alias, text) in enumerate(missing):
        values = {
            "statement": text,
            "metric": None,
            "deadline": None,
            "canvas_id": None,
            "problem_id": None,
            "opportunity_id": None,
            "srs_document_id": None,
            "srs_heading": None,
            "rank": min(rank + offset, goals.MAX_RANK),
            "owner_id": charter.created_by,
            "status": goals.ACTIVE,
            "close_reason": None,
        }
        note = f"made from goal {alias} of the Curator's charter, revision {charter.revision}"
        await STORE.insert(conn, project_id, number + offset, 1, values, note, charter.created_by, {"alias": alias})
        target = f"{project}/goal {goals.code(number + offset)} revision=1 fields=statement,rank,owner,status"
        await auditor.record(audit.GOAL_SET, f"{target} charter={charter.revision}")
    log.info("goals made from the charter", extra={"project": project, "goals": len(missing)})
    return len(missing)


async def active_goals(conn: AsyncConnection, project_id: int, project: str) -> list[ProjectGoal]:
    """The project's active goals at their newest revision, by rank then code; the goals of its charter made goals
    first."""
    await adopt_charter_goals(conn, project_id, project)
    return [view(project, row) for row in await STORE.listed(conn, project_id, goals.ACTIVE)]


async def curator_goals(conn: AsyncConnection, project_id: int, project: str) -> list[dict]:
    """The project's active goals, by rank, as the prompt of the Curator's review run lists them
    (``goals.curator_line``); the goals of its charter made goals first."""
    shown_goals = await active_goals(conn, project_id, project)
    return [goals.curator_line(goal.model_dump(mode="json")) for goal in shown_goals]


# Writing


async def owner_of(
    conn: AsyncConnection, access: ProjectAccess, login: str, of: str = "a goal's"
) -> tuple[int | None, list[str]]:
    """The id of member ``login`` as the owner of a goal (or of what ``of`` names), and why they cannot be one."""
    row = await repository.member_role(conn, access.project_id, login)
    if row is not None and has_role(row.role, "writer"):
        return row.id, []
    held = row.role if row is not None and row.role else "no grant"
    return None, [f"owner: {login} holds {held} on project {access.name}; {of} owner holds the writer role or above"]


async def _canvas(conn: AsyncConnection, access: ProjectAccess, slug: str, held: int | None) -> tuple[int | None, list]:
    row = await repository.canvas(conn, access.project_id, slug)
    if row is None:
        return None, [f"canvas: project {access.name} has no canvas {slug}"]
    if row.archived_at is not None and row.id != held:
        return None, [f"canvas: canvas {slug} is archived"]
    return row.id, []


async def metric_problems(
    conn: AsyncConnection, access: ProjectAccess, key: str, held: dict | None, of: str = "a goal's"
) -> list[str]:
    """Why ``key`` cannot be the key of the metric of a goal (or of what ``of`` names) whose metric is ``held`` now."""
    row = await repository.signal_key(conn, access.project_id, key)
    if row is None:
        return [
            f"metric.key: the registry of project {access.name} has no key {key}: an admin adds it with "
            "`evo-agents hub signal keys add`"
        ]
    if row.kind != NUMBER_KIND:
        return [f"metric.key: {key} is a key of kind {row.kind}; {of} metric is a key of kind number"]
    if row.archived_at is not None and (held or {}).get("key") != key:
        return [f"metric.key: {key} is archived"]
    return []


async def _problem(conn: AsyncConnection, access: ProjectAccess, ref: str, held: int | None) -> tuple[int | None, list]:
    """The id of problem ``ref`` of the project, for a goal whose problem is ``held`` now, and why it cannot be."""
    row = await repository.problem(conn, access.project_id, ref)
    if row is None:
        return None, [f"problem: project {access.name} has no problem {ref}: see `evo-agents hub problem list`"]
    if row.status == problem_model.DROPPED and row.id != held:
        return None, [f"problem: problem {ref} is dropped; a goal answers a problem that is open or solved"]
    return row.id, []


async def _opportunity(
    conn: AsyncConnection, access: ProjectAccess, ref: str, held: int | None
) -> tuple[int | None, list]:
    """The id of opportunity ``ref`` of the project, for a goal whose opportunity is ``held`` now, and why it cannot
    be."""
    row = await repository.opportunity(conn, access.project_id, ref)
    if row is None:
        return None, [
            f"opportunity: project {access.name} has no opportunity {ref}: see `evo-agents hub opportunity list`"
        ]
    if row.status == opportunity_model.DROPPED and row.id != held:
        return None, [f"opportunity: opportunity {ref} is dropped; a goal answers one that is not"]
    return row.id, []


async def _srs_document(
    conn: AsyncConnection, access: ProjectAccess, slug: str, held: int | None
) -> tuple[int | None, list]:
    """The id of SRS document ``slug`` of the project, for a goal whose document is ``held`` now, and why it cannot
    be."""
    row = await repository.srs_document(conn, access.project_id, slug)
    if row is None:
        return None, [f"srs_document: project {access.name} has no SRS document {slug}"]
    if row.archived_at is not None and row.id != held:
        return None, [f"srs_document: document {slug} is archived"]
    return row.id, []


async def _aims(conn: AsyncConnection, access: ProjectAccess, body: ProjectGoalWrite, held) -> tuple[dict, list[str]]:
    """The ids of the canvas, problem, opportunity and SRS document ``body`` aims at, and each problem with them."""
    problems: list[str] = []
    canvas_id = None
    if body.canvas is not None:
        canvas_id, said = await _canvas(conn, access, body.canvas, held.canvas_id if held is not None else None)
        problems += said
    problem_id = None
    if body.problem is not None:
        problem_id, said = await _problem(conn, access, body.problem, held.problem_id if held is not None else None)
        problems += said
    opportunity_id = None
    if body.opportunity is not None:
        if body.problem is not None:
            problems.append("opportunity: a goal answers a problem or an opportunity, not both")
        else:
            opportunity_id, said = await _opportunity(
                conn, access, body.opportunity, held.opportunity_id if held is not None else None
            )
            problems += said
    srs_document_id = None
    if body.srs_document is not None:
        srs_document_id, said = await _srs_document(
            conn, access, body.srs_document, held.srs_document_id if held is not None else None
        )
        problems += said
    if body.srs_heading is not None and body.srs_document is None:
        problems.append("srs_heading: a heading belongs to the SRS document the goal aims at; name srs_document")
    ids = {
        "canvas_id": canvas_id,
        "problem_id": problem_id,
        "opportunity_id": opportunity_id,
        "srs_document_id": srs_document_id,
    }
    return ids, problems


async def _values(
    conn: AsyncConnection, access: ProjectAccess, body: ProjectGoalWrite, held, writer_id: int
) -> tuple[dict, list[str]]:
    """What the next revision of goal ``held`` (None for a new one) holds once ``body`` is written by ``writer_id``,
    and each problem with it."""
    problems: list[str] = []
    owner_id = held.owner_id if held is not None else writer_id
    if body.owner is not None and (held is None or body.owner.lower() != held.owner.lower()):
        owner_id, said = await owner_of(conn, access, body.owner)
        problems += said
    ids, said = await _aims(conn, access, body, held)
    problems += said
    metric = body.metric.model_dump(exclude_none=True) if body.metric is not None else None
    if body.metric is not None and body.metric.key == BACKLOG_METRIC:  # the hub computes it: no key of the registry
        if body.srs_document is None:
            problems.append(f"metric.key: {BACKLOG_METRIC} counts the backlog of an SRS document; name srs_document")
    elif body.metric is not None:
        problems += await metric_problems(conn, access, body.metric.key, held.metric if held is not None else None)
    rank = body.rank or (held.rank if held is not None else await STORE.next_rank(conn, access.project_id))
    values = {
        "statement": body.statement,
        "metric": metric,
        "deadline": body.deadline,
        "canvas_id": ids["canvas_id"],
        "problem_id": ids["problem_id"],
        "opportunity_id": ids["opportunity_id"],
        "srs_document_id": ids["srs_document_id"],
        "srs_heading": body.srs_heading,
        "rank": rank,
        "owner_id": owner_id,
        "status": body.status,
        "close_reason": body.close_reason,
    }
    return values, problems


async def check(conn: AsyncConnection, access: ProjectAccess, ref: str, body: ProjectGoalWrite) -> list[str]:
    """Each problem with ``body`` as the next revision of goal ``ref``, as ``write`` would find it (an HTTPException
    for a new goal whose code is not the next one); nothing written."""
    held = await found(conn, access.project_id, ref)
    if held is None:
        await STORE.number_of_new(conn, access, ref)
    return (await _values(conn, access, body, held, writer_id=0))[1]  # the writer only owns a new goal by default


async def write(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, ref: str, body: ProjectGoalWrite, *, via: str = ""
) -> tuple[ProjectGoal, bool]:
    """Write ``body`` as the next revision of goal ``ref`` of the project, once it is right (422 naming each problem
    otherwise) and while the goal is at ``body.if_revision`` (409 otherwise); (the goal now, whether a revision was
    written). ``via`` ends the audit target (`` proposal=<id>``). The caller checked ``refusal``."""

    async def values_of(held) -> dict:
        values, problems = await _values(conn, access, body, held, user.user_id)
        refuse(problems)
        return values

    revised = await STORE.revise(conn, access, ref, body.if_revision, values_of, note=body.note, writer=user.user_id)
    held, after = revised.before, revised.after
    if not revised.changed:
        return view(access.name, held), False
    code = goals.code(after.number)
    keys = goals.changed(fields_of(held) if held is not None else {}, fields_of(after))
    aimed = [name for name in BRAINSTORMED_FOR if name in keys]
    stale = await brainstorms.stale_for_goal(conn, held.id) if held is not None and aimed else 0
    if stale:
        extra = {"project": access.name, "goal": code, "revision": after.revision, "changed": aimed}
        log.info("brainstorm proposals stale", extra={**extra, "stale": stale})
    target = f"{access.name}/goal {code} revision={after.revision} fields={','.join(keys)}{via}"
    await Auditor.of(conn, user, access).record(audit.GOAL_SET, target)
    log.info("goal written", extra={"project": access.name, "goal": code, "revision": after.revision})
    return view(access.name, after), True


def closing(row, reason: str, note: str | None, if_revision: int | None) -> ProjectGoalWrite:
    """Goal ``row`` closed for ``reason``, as a write of its next revision."""
    fields = {**fields_of(row), "status": goals.CLOSED, "close_reason": reason}
    return ProjectGoalWrite(**fields, note=note, if_revision=row.revision if if_revision is None else if_revision)


async def _graph_changed(scope: Scope, changed: bool) -> None:
    """Queue a build of the project's knowledge graph, which holds the goals that answer a problem
    (``evo_agents.hub.kg_problems``), once a write changed a goal."""
    if changed:
        await queue_if_configured(scope.jobs, scope.conn, scope.access.project_id, scope.access.name)


async def put(scope: Scope, ref: str, body: ProjectGoalWrite) -> tuple[ProjectGoal, bool]:
    """Write a goal whole as its next revision, as the route does."""
    shown_goal, changed = await write(scope.conn, scope.user, scope.access, ref, body)
    await _graph_changed(scope, changed)
    return shown_goal, changed


async def close(scope: Scope, ref: str, body: ProjectGoalClose) -> tuple[ProjectGoal, bool]:
    """Close goal ``ref`` for a reason, as its next revision; 409 when it is closed already."""
    await adopt_charter_goals(scope.conn, scope.access.project_id, scope.access.name)
    held = await one(scope.conn, scope.access, ref)
    if held.status == goals.CLOSED:
        raise Conflict(
            f"goal {ref} of project {scope.access.name} is closed already ({held.close_reason}); nothing changed"
        )
    closed = closing(held, body.reason, body.note, body.if_revision)
    shown_goal, changed = await write(scope.conn, scope.user, scope.access, ref, closed)
    await _graph_changed(scope, changed)
    return shown_goal, changed

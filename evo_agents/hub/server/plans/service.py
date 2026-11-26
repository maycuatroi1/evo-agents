"""The plans of a project: who may read and write them (``READERS``, ``WRITERS``), what a read lets through (the sink
and the label rule, ``visible``), what every write checks and records (``store``), the writes of PUT, PATCH,
complete, abandon and reopen (``write_plan``, ``apply_patch``), the problems a plan names (``missing_problems``) and
the warnings it is stored with (``semantic_warnings``). Each function a route calls opens its own transaction
(``UnitOfWork``), so a ``PlanError`` raised in it rolls back what it wrote before the route answers the refusal
(``refusal``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.harness import load_schema, plan_body, plan_digest, plan_semantics
from evo_agents.hub import plan_step_status
from evo_agents.hub.access import has_role
from evo_agents.hub.handoff import build_handoff
from evo_agents.hub.plan_diff import plan_diff
from evo_agents.hub.plans import (
    PlanProblem,
    PlanTooLarge,
    abandoned_body,
    check_body,
    glance,
    reopened_body,
    settled_area,
    step_index,
    step_key,
    steps_marked_done,
    summarize,
    undone_steps,
    unrunnable_verify,
    update_item,
    update_summary,
    verify_refusal,
)
from evo_agents.hub.problems import number_of
from evo_agents.hub.server import system_traces
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.scope import Noun, ProjectScope
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.errors import CODES
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.plans import Conflict, Written
from evo_agents.hub.server.plans.models import (
    NO_PROBLEMS,
    PLAN_ABANDON,
    PLAN_COMPLETE,
    PLAN_CREATE,
    PLAN_PATCH,
    PLAN_PUT,
    PLAN_REOPEN,
    PROBLEMS_PATH,
    REVISION_CONFLICT,
    SHOWN_PROBLEMS,
    Held,
    PlanAbandon,
    PlanComplete,
    PlanDiff,
    PlanDiffHunk,
    PlanDiffLine,
    PlanError,
    PlanHandoff,
    PlanPatch,
    PlanPut,
    PlanReopen,
    PlanSummary,
    Problem,
    Revision,
    RevisionBody,
)
from evo_agents.hub.server.plans.repository import STORE
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

NOUN = Noun("a plan", "the plans")


def read_refusal(access: ProjectAccess) -> str | None:
    """Reading plans needs a grant on the project; only a hub admin without one gets this far (``project_access``
    answers 404 to anyone else without one), and it manages the project without reading what it holds."""
    return None if access.role is not None else f"reading {NOUN.all} of project {access.name} needs a grant on it"


class PlanScope(ProjectScope):
    """The scope of the plan routes: a reader needs a grant on the project, a writer the writer role on it. The agent
    of a run writes too, as the member it acts for: a run marks the steps of its plan."""

    def refusal(self, user: Principal, access: ProjectAccess) -> str | None:
        if self.role == "reader":
            return read_refusal(access)
        if has_role(access.role, self.role):
            return None
        return f"pushing to project {access.name} needs the writer role on it"


READERS = PlanScope("reader", NOUN)
WRITERS = PlanScope("writer", NOUN)


def not_found(project: str, plan_id: str) -> str:
    return f"no plan {plan_id} in project {project} that you can see: see `evo-agents hub plan list`"


def refusal(request: Request, exc: PlanError) -> JSONResponse:
    """The answer to ``exc``: its status, with its problems as ``detail`` and the plan as held as ``current``."""
    body = Conflict(
        error=exc.code or CODES.get(exc.status, f"http_{exc.status}"),
        message=exc.message,
        request_id=getattr(request.state, "request_id", None),
        detail=exc.detail,
        current=exc.current,
    )
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=exc.status)


# Reading


async def held(conn: AsyncConnection, access: ProjectAccess, plan_id: str, *, lock: bool = False) -> Held | None:
    """The plan as held, its row locked until the transaction ends with ``lock`` (``PlanStore.held``)."""
    return await STORE.held(conn, access.project_id, plan_id, lock=lock)


def read_sink(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink (what was pushed to the hub may
    come back from it to anyone whose grant reaches its level)."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def sees(access: ProjectAccess, label: dict, sink: str | None) -> bool:
    """Whether the caller sees what carries ``label`` through ``sink`` (``read_sink``)."""
    return access.visible(label, read_sink(access, sink))  # pyright: ignore[reportArgumentType]  # None: no hub sink


def reader(access: ProjectAccess) -> None:
    """403 when the caller may not read the project's plans (``read_refusal``)."""
    refused = read_refusal(access)
    if refused is not None:
        raise Forbidden(refused)


async def visible(
    conn: AsyncConnection, access: ProjectAccess, plan_id: str, sink: str | None, *, lock: bool = False
) -> Held:
    """The plan as held, when the caller may see it through ``sink``; 404 otherwise, as for a plan that does not
    exist."""
    found = await held(conn, access, plan_id, lock=lock)
    if found is None or not sees(access, found.label, sink):
        raise NotFound(not_found(access.name, plan_id))
    return found


async def listed(
    engine: AsyncEngine, user: Principal, project: str, area: str | None, sink: str | None, page: Paging
) -> list[PlanSummary]:
    """The plans of the project the caller sees, those of ``area`` alone when given. The label rule filters the plans
    after the query: the page is cut from what it lets through, and counts that."""
    async with UnitOfWork(engine) as conn:
        access = await READERS.check(conn, user, project)
        rows = await STORE.listed(conn, access.project_id, area)
        traces = await system_traces.plan_traces(conn, access.project_id) if area != "completed" else {}
    return page.of(
        [
            PlanSummary(**{**row._mapping, **glance(row.goal, row.steps, row.repos)}, trace=traces.get(row.plan_id))
            for row in rows
            if sees(access, row.label, sink)
        ]
    )


async def shown(engine: AsyncEngine, user: Principal, project: str, plan_id: str, sink: str | None) -> dict:
    async with UnitOfWork(engine) as conn:
        access = await READERS.check(conn, user, project)
        found = await visible(conn, access, plan_id, sink)
    return found.view(project)


async def revisions(
    conn: AsyncConnection, access: ProjectAccess, plan_id: str, sink: str | None, revision: int | None = None
):
    """The revisions of the plan the caller sees, with what RevisionBody shows by column name, the latest first;
    revision ``revision`` alone when given."""
    await visible(conn, access, plan_id, sink)
    rows = await STORE.revisions(conn, access.project_id, plan_id, revision)
    return [row for row in rows if sees(access, row.label, sink)]


async def history(engine: AsyncEngine, user: Principal, project: str, plan_id: str, sink: str | None) -> list[Revision]:
    async with UnitOfWork(engine) as conn:
        access = await READERS.check(conn, user, project)
        rows = await revisions(conn, access, plan_id, sink)
    return [Revision(**row._mapping) for row in rows]


async def revision_body(
    engine: AsyncEngine, user: Principal, project: str, plan_id: str, revision: int, sink: str | None
) -> RevisionBody:
    async with UnitOfWork(engine) as conn:
        access = await READERS.check(conn, user, project)
        rows = await revisions(conn, access, plan_id, sink, revision)
    if not rows:
        raise NotFound(f"plan {plan_id} of project {project} has no revision {revision} that you can see")
    return RevisionBody(**rows[0]._mapping)


async def _one_revision(conn: AsyncConnection, access: ProjectAccess, plan_id: str, sink: str | None, revision: int):
    rows = await revisions(conn, access, plan_id, sink, revision)
    if not rows:
        raise NotFound(f"plan {plan_id} of project {access.name} has no revision {revision} that you can see")
    return Revision(**rows[0]._mapping), rows[0].body


async def diff(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    plan_id: str,
    revisions_of: tuple[int, int],
    context: int,
    sink: str | None,
) -> PlanDiff:
    """The lines that changed from one revision of the plan to the other (``revisions_of``: from, to), as their git
    copies read; 404 when either revision is not one the caller can see."""
    async with UnitOfWork(engine) as conn:
        access = await READERS.check(conn, user, project)
        older, old_body = await _one_revision(conn, access, plan_id, sink, revisions_of[0])
        newer, new_body = await _one_revision(conn, access, plan_id, sink, revisions_of[1])
    found = await asyncio.to_thread(plan_diff, old_body, new_body, context)
    return PlanDiff(
        plan_id=plan_id,
        from_revision=older,
        to_revision=newer,
        context=context,
        added=found.added,
        removed=found.removed,
        hunks=[
            PlanDiffHunk(
                old_start=h.old_start,
                old_lines=h.old_lines,
                new_start=h.new_start,
                new_lines=h.new_lines,
                lines=[PlanDiffLine(kind=x.kind, old=x.old, new=x.new, text=x.text) for x in h.lines],
            )
            for h in found.hunks
        ],
    )


async def handoff(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    plan_id: str,
    step: str | None,
    budget_tokens: int,
    sink: str | None,
) -> PlanHandoff:
    """What a new agent session needs to take the plan up, built from the plan as the hub holds it, within
    ``budget_tokens`` (``evo_agents.hub.handoff``); 404 for a step the plan lacks."""
    async with UnitOfWork(engine) as conn:
        access = await READERS.check(conn, user, project)
        found = await visible(conn, access, plan_id, sink)
        from evo_agents.hub.server.token_strategy import recommend  # it reads the gateway, which reaches plans

        routed = await recommend(conn, access.project_id, user.user_id, found.body, step) if step else {}
    try:
        built = await asyncio.to_thread(
            build_handoff, found.body, project=project, revision=found.revision, step=step, budget_tokens=budget_tokens
        )
    except PlanProblem as exc:
        raise NotFound(f"plan {plan_id}: {exc}") from None
    return PlanHandoff(
        project=project,
        plan_id=plan_id,
        revision=found.revision,
        step=step,
        budget_tokens=budget_tokens,
        tokens=built.tokens,
        cut=built.cut,
        text=built.text,
        **routed,
    )


# The problems a plan names (``problems`` of plan.schema.json, plan problem-oriented-hub, criterion a4). A write that
# names a problem anew, one the plan held before does not, names a problem of its project (``evo_agents.hub.problems``):
# a code the project lacks refuses the write. A code once held is not looked up again, as a problem is closed and never
# deleted. A PUT that writes a plan or a revision naming no problem is stored with the warning NO_PROBLEMS: naming one
# is asked, never required.


def named_problems(body) -> list[str]:
    """The codes of problems a plan body names, as written (plan.schema.json keeps them P-<number>, each once)."""
    found = body.get("problems") if isinstance(body, dict) else None
    return [code for code in found if isinstance(code, str)] if isinstance(found, list) else []


async def missing_problems(conn: AsyncConnection, project_id: int, old: dict | None, new: dict) -> list[str]:
    """The codes ``new`` names that ``old`` did not and the project has no problem of, in the order ``new`` names
    them."""
    held_codes = set(named_problems(old))
    added = [code for code in named_problems(new) if code not in held_codes]
    numbers = {code: number_of(code) for code in added}
    wanted = [number for number in numbers.values() if number is not None]
    found = await STORE.problem_numbers(conn, project_id, wanted) if wanted else set()
    return [code for code in added if numbers[code] not in found]


def problems_refusal(project: str, plan_id: str, codes: list[str]) -> tuple[str, list[dict]]:
    """The message and detail of the 422 of a write naming ``codes``, which project ``project`` does not have."""
    which = f"problem {codes[0]}" if len(codes) == 1 else f"problems {', '.join(codes)}"
    message = (
        f"plan {plan_id} names {which}, which project {project} does not have: see `evo-agents hub problem list`. "
        "Nothing was written"
    )
    return message, [{"path": PROBLEMS_PATH, "message": f"no problem {code} in project {project}"} for code in codes]


def problem_warnings(body: dict) -> list[Problem]:
    """The warning of a plan written with no problem named; none for a plan that names one."""
    return [] if named_problems(body) else [Problem(path=PROBLEMS_PATH, message=NO_PROBLEMS)]


# The warnings a plan is stored with: those of plan.schema.json and of ``plan_semantics``, which checks the repos a
# plan names against the repos registered for its project and its harness repo.


class RegisteredRepos:
    """What ``plan_semantics`` asks of a harness, from what the project registered."""

    def __init__(self, names: set[str]):
        self.names = names

    def known_repos(self) -> set[str]:
        return self.names


async def semantic_warnings(conn: AsyncConnection, access: ProjectAccess, body: dict, area: str) -> list[Problem]:
    """The warnings of plan.schema.json and ``plan_semantics`` for ``body`` written to ``area``: a plan is stored
    with them."""
    path = PurePosixPath("plans", area, f"{body['id']}.yaml")
    registered = RegisteredRepos(await STORE.known_repos(conn, access.project_id))
    semantics = plan_semantics(body, path, registered)  # pyright: ignore[reportArgumentType]  # duck-types Path, Harness
    issues = validate(body, load_schema("plan")) + semantics
    return [Problem(path=i.path, message=i.message) for i in issues if i.severity == "warning"]


# Writing


def schema_checked(body: dict, plan_id: str) -> None:
    """422 (413 for size) when the body is not a plan the hub can keep; before anything is read or written."""
    try:
        check_body(body, plan_id)
    except PlanTooLarge as exc:
        raise PlanError(413, str(exc)) from None
    except PlanProblem as exc:
        raise PlanError(422, f"plan {plan_id} cannot be stored: {exc}") from None
    found = errors(validate(body, load_schema("plan")))
    if found:
        shown_problems = "; ".join(f"{i.path or '<root>'}: {i.message}" for i in found[:SHOWN_PROBLEMS])
        more = f" and {len(found) - SHOWN_PROBLEMS} more" if len(found) > SHOWN_PROBLEMS else ""
        raise PlanError(
            422,
            f"plan {plan_id} does not match plan.schema.json: {shown_problems}{more}; nothing was written",
            detail=[{"path": i.path, "message": i.message} for i in found],
        )


def stale(access: ProjectAccess, found: Held, given: int | None) -> PlanError:
    """The 409 ``revision_conflict`` of a write made against revision ``given`` of a plan held at another."""
    if not given:
        message = (
            f"plan {found.plan_id} is on the hub at revision {found.revision}: pass if_revision {found.revision} to "
            "replace it"
        )
    else:
        message = (
            f"plan {found.plan_id} is at revision {found.revision} on the hub, not {given}: someone changed it since "
            "you read it. Read it again and retry; nothing was written"
        )
    return PlanError(409, message, code=REVISION_CONFLICT, current=found.view(access.name))


def at_revision(access: ProjectAccess, found: Held, given: int | None) -> None:
    """The 409 of ``stale`` when the writer named a revision ``given`` other than the one held."""
    if given is not None and given != found.revision:
        raise stale(access, found, given)


async def _write_refused(conn: AsyncConnection, access: ProjectAccess, found: Held | None, body: dict, **curator):
    """The 422 of a verify or a problem ``body`` names anew, or the 409 of a write of a plan of the Curator's."""
    from evo_agents.hub.server.change_views import plan_write_refusal  # it reads plans through this package

    before = None if found is None else found.body
    unrunnable = unrunnable_verify(before, body)
    if unrunnable:
        raise PlanError(422, verify_refusal(body["id"], unrunnable))
    missing = await missing_problems(conn, access.project_id, before, body)
    if missing:
        message, detail = problems_refusal(access.name, body["id"], missing)
        raise PlanError(422, message, detail=detail)
    refused = await plan_write_refusal(conn, access.project_id, body["id"], before, body, **curator)
    if refused is not None:
        raise PlanError(409, refused)


async def store(
    conn,
    access,
    user,
    found: Held | None,
    *,
    area,
    label,
    body,
    summary,
    action,
    made_by_curator: bool = False,
    run_id: int | None = None,
) -> Held | None:
    """Write the next revision of a plan (the first when ``found`` is None); None when another request created the
    plan first. The stored body is read back and must carry the digest of the body given. A write of a plan the
    Curator made that names another repo or branch, or changes more than the progress of its steps, and a new plan
    with an id of the Curator's that the hub does not make from a proposal (``made_by_curator``), are a 409
    (``changes.plan_write_refusal``). A verify that is new against ``found`` and cannot be one shell command line, on
    a step not done, is a 422 (``plans.unrunnable_verify``): every write of a plan comes through here, so here each
    step whose status the write sets or changes emits ``plan.step`` (``evo_agents.hub.plan_step_status``).
    ``run_id`` is the run that writes it, an author run's, recorded on the revision."""
    await _write_refused(conn, access, found, body, made_by_curator=made_by_curator)
    digest = plan_digest(body)
    revision = 1 if found is None else found.revision + 1
    values = {"area": area, "label": label, "body": body, "revision": revision, "digest": digest}
    row = await STORE.write(conn, access.project_id, body["id"], found, values, user.user_id)
    if row is None:
        return None
    stored, created_at, updated_at = row
    if plan_digest(stored) != digest:  # a number Postgres does not keep as written, such as 1e400
        raise PlanError(
            422,
            f"plan {body['id']} holds a number that does not survive storage unchanged: write it as a string. "
            "Nothing was written",
        )
    kept = {"plan_id": body["id"], **values, "summary": summary, "actor_id": user.user_id, "run_id": run_id}
    await STORE.add_revision(conn, access.project_id, kept)
    await Auditor.of(conn, user, access).record(action, f"{access.name}/{body['id']}@{revision}")
    before = None if found is None else found.body
    marked = await plan_step_status.emit(conn, access.project_id, body["id"], revision, before, body)
    acting = getattr(user, "acts_for_run", None)
    if marked and found is not None and acting is not None:  # a run of a trace marked a step: the trace works on it
        await system_traces.take_plan(conn, acting, body["id"])
    return Held(body["id"], area, label, stored, revision, digest, created_at, updated_at, user.login)


def written(access, found: Held, *, created: bool, changed: bool, warnings: list[Problem]) -> Written:
    return Written(**found.view(access.name), created=created, changed=changed, warnings=warnings)


def _logged(project: str, found: Held, outcome: str, user) -> None:
    log.info(
        "plan write",
        extra={
            "project": project,
            "plan_id": found.plan_id,
            "revision": found.revision,
            "outcome": outcome,
            "login": user.login,
        },
    )


async def _settle(conn, access, user, found: Held, run_id: int | None = None) -> Held:
    """After a write of ``found``, in its transaction: move the plan to the area its steps put it in
    (``plans.settled_area``) with a revision of its own, as POST .../complete (audit ``plan.complete``) or .../reopen
    (audit ``plan.reopen``) would; the plan as held afterwards, ``found`` itself when it stays where it is."""
    area = settled_area(found.area, found.body)
    if area == found.area:
        return found
    if area == "completed":
        summary, action, outcome = "completed: moved from active", PLAN_COMPLETE, area
    else:
        summary, action, outcome = "reopened: back to active", PLAN_REOPEN, "reopened"
    stored = await store(
        conn,
        access,
        user,
        found,
        area=area,
        label=found.label,
        body=found.body,
        summary=summary,
        action=action,
        run_id=run_id,
    )
    assert stored is not None  # the plan is held, its row locked: the write cannot lose a race to a create
    _logged(access.name, stored, outcome, user)
    return stored


async def _create(conn, access: ProjectAccess, user, body: dict, *, area, label, run_id) -> Written | None:
    """``write_plan``'s create of a plan the hub does not hold; None when another request created it first."""
    new_area = area or "active"
    warnings = await semantic_warnings(conn, access, body, new_area)
    stored = await store(
        conn,
        access,
        user,
        None,
        area=new_area,
        label=label,
        body=body,
        summary=summarize(None, body),
        action=PLAN_CREATE,
        run_id=run_id,
    )
    if stored is None:
        return None
    _logged(access.name, stored, "created", user)
    warnings += problem_warnings(body)
    return written(access, stored, created=True, changed=True, warnings=warnings)


async def write_plan(
    conn,
    access: ProjectAccess,
    user,
    body: dict,
    *,
    area: str | None = None,
    if_revision: int | None = None,
    label: dict | None = None,
    run_id: int | None = None,
    guard: Callable[[Held | None], None] | None = None,
) -> Written:
    """PUT's write, in the caller's transaction, for a ``user`` whose writer role the caller checked and a ``body``
    that passed ``schema_checked``: create plan ``body["id"]``, or replace the revision ``if_revision`` names. Raises
    PlanError (409 ``revision_conflict``, 409, 413, 422) and HTTPException (403 or 422 for a label the push refuses).
    ``guard`` sees the plan as held (None before it exists), its row locked, before anything is written, and raises
    PlanError to refuse the write; ``run_id`` is the run that writes it, recorded on the revision. An author run's
    write (``evo_agents.hub.server.runs``) goes through here as the member who dispatched the run."""
    plan_id, project = body["id"], access.name
    given_label = access.push_label(label)
    for _ in range(2):  # a create that loses a race with another create reads the winner's plan
        found = await held(conn, access, plan_id, lock=True)
        if found is None:
            if if_revision:
                raise PlanError(
                    409,
                    f"plan {plan_id} is not on the hub, so there is no revision {if_revision} to "
                    "replace: leave out if_revision to create it",
                    code=REVISION_CONFLICT,
                )
            if guard is not None:
                guard(None)
            created = await _create(conn, access, user, body, area=area, label=given_label, run_id=run_id)
            if created is None:
                continue
            return created
        if not sees(access, found.label, None):
            raise PlanError(409, f"plan {plan_id} already exists in project {project}")
        new_label = given_label if label is not None else access.push_label(found.label)
        warnings = await semantic_warnings(conn, access, body, found.area)
        if plan_digest(body) == found.digest and new_label == found.label:
            _logged(project, found, "unchanged", user)
            return written(access, found, created=False, changed=False, warnings=warnings)
        if area and area not in (found.area, settled_area(found.area, body)):
            raise PlanError(
                422,
                f"plan {plan_id} is {found.area} on the hub, and a put does not move it: "
                f"`evo-agents hub plan complete` does, and its copy belongs in plans/{found.area}/",
            )
        if if_revision != found.revision:
            raise stale(access, found, if_revision)
        if guard is not None:
            guard(found)
        stored = await store(
            conn,
            access,
            user,
            found,
            area=found.area,
            label=new_label,
            body=body,
            summary=summarize(found.body, body) if body != found.body else "changed the label",
            action=PLAN_PUT,
            run_id=run_id,
        )
        assert stored is not None  # the plan is held, its row locked
        _logged(project, stored, "replaced", user)
        stored = await _settle(conn, access, user, stored, run_id)
        warnings += problem_warnings(body)
        return written(access, stored, created=False, changed=True, warnings=warnings)
    raise PlanError(409, f"plan {plan_id} was created and changed while this request ran; try again")


async def _local_writer(conn, access, user, run_id: int | None) -> int | None:
    """``run_id`` when it is a local run of the project that ``user`` owns, its row locked; None for no run; 404 for
    any other (``local_runs.writing_run``)."""
    if run_id is None:
        return None
    from evo_agents.hub.server.local_runs import writing_run  # it reads plans through this package

    return await writing_run(conn, access, user, run_id)


async def _local_progress(conn, run_id: int | None, plan_id: str, revision: int, step: str | None) -> None:
    """Tell the local run ``run_id`` (the one ``_local_writer`` took) which plan, revision
    and step its session wrote."""
    if run_id is None:
        return
    from evo_agents.hub.server.local_runs import note_progress

    await note_progress(conn, run_id, plan_id, revision, step)


async def put(engine: AsyncEngine, user: Principal, project: str, plan_id: str, payload: PlanPut) -> Written:
    """Create the plan, or replace the revision ``if_revision`` names with ``body``."""
    body = plan_body(payload.body)
    schema_checked(body, plan_id)
    async with UnitOfWork(engine) as conn:
        access = await WRITERS.check(conn, user, project)
        local = await _local_writer(conn, access, user, payload.run_id)
        stored = await write_plan(
            conn,
            access,
            user,
            body,
            area=payload.area,
            if_revision=payload.if_revision,
            label=payload.label,
            run_id=local,
        )
        await _local_progress(conn, local, plan_id, stored.revision, None)
        return stored


async def patch(engine: AsyncEngine, user: Principal, project: str, plan_id: str, payload: PlanPatch) -> Written:
    """Set keys of one item, as ``evo harness step``, ``repo``, ``debt`` and ``question`` do in a file."""
    if (payload.index is None) == (payload.step is None):
        raise Unprocessable("name the item with exactly one of index and step")
    if payload.step is not None and payload.section != "steps":
        raise Unprocessable("step names an item of the steps section only; give index for other sections")
    async with UnitOfWork(engine) as conn:
        access = await WRITERS.check(conn, user, project)
        local = await _local_writer(conn, access, user, payload.run_id)
        stored, changed, warnings = await apply_patch(
            conn,
            access,
            user,
            plan_id,
            payload.section,
            payload.updates,
            if_revision=payload.if_revision,
            index=payload.index,
            step=payload.step,
            run_id=local,
        )
        step = None
        if payload.section == "steps":
            where = payload.index if payload.step is None else step_index(stored.body, payload.step)
            step = step_key(stored.body["steps"][where], where)  # pyright: ignore[reportArgumentType]  # the caller gives index or step
        await _local_progress(conn, local, plan_id, stored.revision, step)
        return written(access, stored, created=False, changed=changed, warnings=warnings)


async def apply_patch(
    conn,
    access: ProjectAccess,
    user,
    plan_id: str,
    section: str,
    updates: dict[str, str],
    *,
    if_revision: int,
    index: int | None = None,
    step=None,
    run_id: int | None = None,
    hub: bool = False,
) -> tuple[Held, bool, list[Problem]]:
    """PATCH's write, in the caller's transaction, for a ``user`` whose writer role the caller checked: set
    ``updates`` on one item of ``section``, named by ``index`` or (for steps) by ``step``, when ``if_revision`` is
    the revision the hub holds. Returns the plan as held afterwards, whether it changed, and the warnings of
    ``plan_semantics``. Raises PlanError: 409 ``revision_conflict`` for another revision, 422 for an update the plan
    refuses; and HTTPException 404 for a plan ``user`` cannot see, 403 or 422 when its label refuses the push. The
    hub's own writes of a run's step (``evo_agents.hub.server.run_state``) go through here as the dispatcher;
    ``run_id`` is the local run whose session writes it, recorded on the revision, and ``hub`` lets them set
    ``plans.HUB_UPDATABLE`` too (a step's proofs)."""
    found = await visible(conn, access, plan_id, None, lock=True)
    label = access.push_label(found.label)
    if if_revision != found.revision:
        raise stale(access, found, if_revision)
    try:
        position = index if step is None else step_index(found.body, step)
        body, old = update_item(found.body, section, position, updates, hub=hub)  # pyright: ignore[reportArgumentType]  # the caller gives index or step
    except PlanProblem as exc:
        raise PlanError(422, f"plan {plan_id}: {exc}; nothing was written") from None
    schema_checked(body, plan_id)
    warnings = await semantic_warnings(conn, access, body, found.area)
    if plan_digest(body) == found.digest:
        _logged(access.name, found, "unchanged", user)
        return found, False, warnings
    summary = update_summary(body, section, position, old, updates)  # pyright: ignore[reportArgumentType]  # the caller gives index or step
    stored = await store(
        conn,
        access,
        user,
        found,
        area=found.area,
        label=label,
        body=body,
        summary=summary,
        action=PLAN_PATCH,
        run_id=run_id,
    )
    assert stored is not None  # the plan is held, its row locked
    _logged(access.name, stored, "patched", user)
    return await _settle(conn, access, user, stored, run_id), True, warnings


async def _locked(conn, user: Principal, project: str, plan_id: str, given: int | None):
    """The writer's access, the plan as held, its row locked, at revision ``given`` when named (409 otherwise), and
    the label a write of it pushes."""
    access = await WRITERS.check(conn, user, project)
    found = await visible(conn, access, plan_id, None, lock=True)
    label = access.push_label(found.label)
    at_revision(access, found, given)
    return access, found, label


async def complete(
    engine: AsyncEngine, user: Principal, project: str, plan_id: str, payload: PlanComplete | None
) -> Written:
    """Move the plan to the completed area; refused while a step is not done, as evo-cli's complete_plan, unless
    ``mark_steps_done`` marks those steps done in the same revision."""
    given = payload.if_revision if payload else None
    mark = bool(payload and payload.mark_steps_done)
    async with UnitOfWork(engine) as conn:
        access, found, label = await _locked(conn, user, project, plan_id, given)
        if found.area == "completed":
            _logged(project, found, "unchanged", user)
            return written(access, found, created=False, changed=False, warnings=[])
        marked = undone_steps(found.body) if mark else []
        body = steps_marked_done(found.body, datetime.now(UTC).strftime("%Y-%m-%d")) if marked else found.body
        undone = undone_steps(body)
        if undone:
            raise PlanError(
                409,
                f"plan {plan_id} has steps that are not done: {', '.join(undone)}. A plan is completed once every "
                "step is done; nothing was written",
            )
        stored = await store(
            conn,
            access,
            user,
            found,
            area="completed",
            label=label,
            body=body,
            summary=f"completed: moved from active, {len(marked)} steps marked done"
            if marked
            else "completed: moved from active",
            action=PLAN_COMPLETE,
        )
        assert stored is not None  # the plan is held, its row locked
        _logged(project, stored, "completed", user)
        return written(access, stored, created=False, changed=True, warnings=[])


async def _superseding(conn, access: ProjectAccess, plan_id: str, superseded_by: str | None) -> None:
    """422 for a plan superseded by itself, 404 for one superseded by a plan the caller cannot see."""
    if superseded_by is None:
        return
    if superseded_by == plan_id:
        raise PlanError(422, "a plan cannot be superseded by itself")
    other = await held(conn, access, superseded_by)
    if other is None or not sees(access, other.label, None):
        raise PlanError(404, not_found(access.name, superseded_by))


async def abandon(engine: AsyncEngine, user: Principal, project: str, plan_id: str, payload: PlanAbandon) -> Written:
    """Move an active or completed plan to the completed area as abandoned, or superseded when ``superseded_by``
    names the plan that replaces it. The date and reason are kept on the plan; no step changes status. A plan that is
    abandoned already is left as it is."""
    async with UnitOfWork(engine) as conn:
        access, found, label = await _locked(conn, user, project, plan_id, payload.if_revision)
        if found.body.get("status") in ("abandoned", "superseded") and found.area == "completed":
            _logged(project, found, "unchanged", user)
            return written(access, found, created=False, changed=False, warnings=[])
        await _superseding(conn, access, plan_id, payload.superseded_by)
        try:
            body = abandoned_body(
                found.body, payload.reason, datetime.now(UTC).strftime("%Y-%m-%d"), payload.superseded_by
            )
            check_body(body, plan_id)
        except PlanProblem as exc:
            raise PlanError(422, str(exc)) from exc
        warnings = await semantic_warnings(conn, access, body, "completed")
        stored = await store(
            conn,
            access,
            user,
            found,
            area="completed",
            label=label,
            body=body,
            summary=f"{body['status']}: {payload.reason[:80]}",
            action=PLAN_ABANDON,
        )
        assert stored is not None  # the plan is held, its row locked
        _logged(project, stored, body["status"], user)
        return written(access, stored, created=False, changed=True, warnings=warnings)


async def reopen(
    engine: AsyncEngine, user: Principal, project: str, plan_id: str, payload: PlanReopen | None
) -> Written:
    """Put a completed plan back in the active area with status active, an abandoned or superseded one without its
    abandon keys; an active plan that is not abandoned is a 409. A write that leaves a step open on a completed plan
    reopens it too (``_settle``); this reopens one whose steps are all done as well."""
    given = payload.if_revision if payload else None
    async with UnitOfWork(engine) as conn:
        access, found, label = await _locked(conn, user, project, plan_id, given)
        try:
            body = reopened_body(found.body, completed=found.area == "completed")
            check_body(body, plan_id)
        except PlanProblem as exc:
            raise PlanError(409, str(exc)) from exc
        warnings = await semantic_warnings(conn, access, body, "active")
        stored = await store(
            conn,
            access,
            user,
            found,
            area="active",
            label=label,
            body=body,
            summary="reopened: back to active",
            action=PLAN_REOPEN,
        )
        assert stored is not None  # the plan is held, its row locked
        _logged(project, stored, "reopened", user)
        return written(access, stored, created=False, changed=True, warnings=warnings)

"""A plan run's plan and its steps, and an author run's plan: the worker holding the run reads the plan as the hub
holds it now (GET /v1/worker/runs/{id}/plan), reports each step (POST .../steps/{key}), and an author run puts the
plan it wrote (PUT .../plan), each as the member who dispatched the run."""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.harness import plan_body
from evo_agents.hub import author, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import StepNotWritten, report_updates, run_step, write_event, write_step
from evo_agents.hub.server.runs.models import NOT_HELD, AuthoredPlan, StepReport, StepWritten
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.views import current_plan, text_or_none
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split


async def held_plan_run(
    conn: AsyncConnection, user: Principal, run_id: int, *, lock: bool = False, kinds: tuple[str, ...] = ("plan",)
):
    """(worker name, row) of a run of ``kinds`` (a plan run) the worker of ``user`` holds, its row locked with
    ``lock``: state, worker_id, kind, project_id, the project's name, plan_id, dispatched_by, the dispatcher's login
    and repos. 404 for any other run, a run of one step included."""
    worker_id, _, name, *_ = await worker_of(conn, user)
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(
            r.c.state,
            r.c.worker_id,
            r.c.kind,
            r.c.project_id,
            p.c.name,
            r.c.plan_id,
            r.c.dispatched_by,
            u.c.login,
            r.c.repos,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )
    if lock:
        query = query.with_for_update(of=r)
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind not in kinds:
        what = {"review": "a review run", "judge": "a judge run", "author": "an author run"}.get(
            row.kind, "a run of one step"
        )
        raise HTTPException(
            404,
            f"run {run_id} is {what}, which has no plan to read or steps to report: report its state with "
            f"POST /v1/worker/runs/{run_id}/state",
        )
    return name, row


async def _dispatcher_access(conn: AsyncConnection, row, user: Principal) -> ProjectAccess:
    """The access to the run's project of the member who dispatched it, as whom the worker reads and writes the plan."""
    actor = Principal(row[6], row[7], False, user.token_id, MACHINE, "")
    access = await project_access(conn, actor, row[4])
    plan_routes._reader(access)
    return access


async def run_plan(engine, user: Principal, run_id: int) -> plan_routes.Plan:
    """The plan of a plan run or an author run the worker of ``user`` holds, as the hub holds it now, read as the
    member who dispatched the run; 404 for an author run that has written no plan yet and was dispatched on none."""
    async with engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, kinds=("plan", "author"))
        if row.plan_id is None:
            raise HTTPException(
                404,
                f"author run {run_id} writes a new plan and has put none on the hub yet: put it with "
                f"`{author.PUT_COMMAND} FILE`",
            )
        access = await _dispatcher_access(conn, row, user)
        held = await plan_routes._visible(conn, access, row[5], None)
    return held.view(row[4])


async def put_authored_plan(engine, user: Principal, run_id: int, payload: AuthoredPlan) -> plan_routes.Written:
    """Put the plan the author run ``run_id``, which the worker of ``user`` holds, wrote on the hub, as PUT
    /v1/worker/runs/{id}/plan says; ``plan_routes.PlanError`` for a plan refused, which the route answers as PUT
    /v1/projects/{p}/plans/{id} answers it."""
    body = plan_body(payload.body)
    plan_id = body.get("id")
    if not isinstance(plan_id, str) or not re.fullmatch(plan_routes.PLAN_ID, plan_id):
        raise plan_routes.PlanError(
            422,
            f"the plan's id is {plan_id!r}: a plan has an id of lowercase letters, digits and -, at most 100 "
            "characters, and is stored under it; nothing was written",
        )
    plan_routes._schema_checked(body, plan_id)
    async with engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=("author",))
        project = row.name
        if row.plan_id is not None and row.plan_id != plan_id:
            raise plan_routes.PlanError(
                422,
                f"author run {run_id} writes plan {row.plan_id}, not {plan_id}: a plan keeps its id, and an author "
                "run writes one plan; nothing was written",
            )
        if row.plan_id is None and payload.if_revision:
            raise plan_routes.PlanError(
                422,
                f"author run {run_id} writes a new plan, so it replaces none: leave out --if-revision and give "
                f"the plan an id no plan of project {project} has; nothing was written",
            )
        try:
            access = await _dispatcher_access(conn, row, user)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise HTTPException(
                403, f"{row.login}, who dispatched run {run_id}, no longer has a grant on project {project}"
            ) from None
        if not has_role(access.role, "writer"):
            raise HTTPException(
                403,
                f"{row.login}, who dispatched run {run_id}, no longer has the writer role on project {project}, "
                "which putting a plan on the hub needs; nothing was written",
            )

        def unchanged_progress(held) -> None:
            problem = author.progress_problem(None if held is None else held.body, body)
            if problem:
                raise plan_routes.PlanError(422, f"plan {plan_id}: {problem}; nothing was written")

        actor = Principal(row.dispatched_by, row.login, False, user.token_id, MACHINE, "")
        written = await plan_routes.write_plan(
            conn,
            access,
            actor,
            body,
            if_revision=payload.if_revision,
            label=payload.label,
            run_id=run_id,
            guard=unchanged_progress,
        )
        if written.changed:
            r = tables.runs
            await conn.execute(
                update(r).where(r.c.id == run_id).values(plan_id=plan_id, plan_revision=written.revision)
            )
            verb = "Created" if written.created else "Revised"
            await write_event(
                conn,
                run_id,
                {
                    "text": f"{verb} plan {plan_id}: revision {written.revision}",
                    "plan_written": {"plan_id": plan_id, "revision": written.revision, "created": written.created},
                },
            )
    log.info(
        "author run plan put",
        extra={"run_id": run_id, "plan_id": plan_id, "revision": written.revision, "changed": written.changed},
    )
    return written


def _check_step_report(run_id: int, key: str, body: StepReport) -> None:
    """422 for a step reported done without verify results, or with one that exited other than 0."""
    if body.status != "done":
        return
    if not body.verify:
        raise HTTPException(
            422,
            f"run {run_id}: step {key} is done only with the verify commands the worker ran again, each with its exit "
            "code; report them, or report the step pending",
        )
    failed = [item for item in body.verify if item.exit_code != 0]
    if failed:
        shown = "; ".join(f"`{' '.join(item.command.split())}` exited {item.exit_code}" for item in failed)
        raise HTTPException(
            422, f"run {run_id}: step {key} is not done, since {shown}; fix it and verify again, or report it pending"
        )


def _report_repo(run_id: int, body: StepReport, plan: dict, step, repos: list | None) -> dict | None:
    """The run's repo the step was done in: the one the report names (422 when the run has no such repo), else the
    one the plan gives the step, when the run has it."""
    by_name = {entry["repo"]: entry for entry in repos or [] if isinstance(entry, dict)}
    if body.repo is not None:
        if body.repo not in by_name:
            raise HTTPException(422, f"run {run_id} works in {', '.join(by_name) or 'no repo'}, not in {body.repo}")
        return by_name[body.repo]
    name = (runs.plan_repo(plan, step if isinstance(step, dict) else {}) or {}).get("repo")
    return by_name.get(name) if isinstance(name, str) else None


def _step_status(plan: dict, key: str) -> str | None:
    try:
        step = plan["steps"][step_index(plan, key)]
    except (PlanProblem, KeyError, TypeError):
        return None
    return text_or_none(step.get("status", "pending")) if isinstance(step, dict) else None


async def write_step_report(engine, user: Principal, run_id: int, key: str, body: StepReport) -> StepWritten:
    """Write a step of the plan of a plan run this worker holds, as the member who dispatched the run."""
    async with engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True)
        _, _, _, project_id, project, plan_id, dispatcher_id, _, repos = row
        _check_step_report(run_id, key, body)
        current = await current_plan(conn, project_id, plan_id)
        if current is None:
            raise HTTPException(404, f"plan {plan_id} of run {run_id} is not on the hub any more")
        plan = current[0]
        try:
            index = step_index(plan, key)
        except PlanProblem as exc:
            raise HTTPException(404, f"{exc}: run {run_id} reports only the steps of its plan") from None
        step = plan["steps"][index]
        repo = _report_repo(run_id, body, plan, step, repos) or {}
        verify = [item.model_dump(exclude_none=True) for item in body.verify or []]
        found = replace(
            await run_step(conn, run_id),
            step_key=key,
            repo=repo.get("repo"),
            branch=repo.get("branch"),
            commit_sha=body.commit_sha,
            diffstat=None,
            verify=verify,
        )
        updates = report_updates(found, body.status, body.evidence)

        def updates_for(held_step):
            if held_step.get("status") != "done":
                return updates
            if body.status == "done":  # a resend: the step is done already
                return None
            raise HTTPException(409, f"step {key} of plan {plan_id} is done already, and the hub never sets it back")

        try:
            revision = await write_step(conn, found, updates_for, token_id=user.token_id, strict=True)
        except StepNotWritten as exc:
            raise HTTPException(exc.status, f"step {key} of plan {plan_id} was not written: {exc.message}") from None
        if revision is not None:
            shown = f"step {key}: {body.status}"
            if body.commit_sha and repo:
                shown += f" ({repo['repo']}@{body.commit_sha[:12]})"
            report = {"step": key, "status": body.status, "repo": repo.get("repo"), "commit_sha": body.commit_sha}
            await write_event(conn, run_id, {"text": shown, "step_report": report})
            target = f"{run_target(project, plan_id, key, run_id)} status={body.status}"
            await audit.record(
                conn,
                actor_id=dispatcher_id,
                token_id=user.token_id,
                action=audit.RUN_STEP_REPORT,
                target=target,
                project_id=project_id,
            )
        after = await current_plan(conn, project_id, plan_id)
    log.info(
        "plan run step reported",
        extra={"run_id": run_id, "step": key, "status": body.status, "written": revision is not None},
    )
    return StepWritten(
        run_id=run_id,
        plan_id=plan_id,
        step_key=key,
        status=_step_status(after[0], key),
        revision=after[1],
        written=revision is not None,
    )

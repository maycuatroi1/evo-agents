"""What the agent of a triage thread does on its own, behind ``evo-agents worker triage rerun|plan``
(``evo_agents.hub.triage_rules`` is the frame; ``docs/workers.md``, Failure triage, is the protocol).

Both routes take the worker token of the worker that holds a chat run (404 for another), whose thread is one of a
failed run's triage (``run_triage``; 404 for any other chat run) and whose dispatcher is that run's owner (403 for
another member, who may talk to the agent but not rerun the owner's run). They act as the owner, as the chain of the
chat run does, and the hub checks the frame again on each call, so an agent that ignored its instructions is refused:

- POST /v1/worker/runs/{id}/triage/rerun: run the failed step again (``controls.rerun_in``), on a worker, runtime or
  account that is a fit candidate (``candidates.candidates_of``), for the causes of ``triage_rules.RERUN_CAUSES``, within
  the chain cap and the chat's budget.
- POST /v1/worker/runs/{id}/triage/plan: change the ``verify`` or narrow the ``what`` of a pending step of the failed
  run's plan, as a new revision under the owner's name (``plans.write_plan``), only as ``triage_rules.edit_problems``
  allows; with ``rerun`` the rerun follows in the same transaction, so a refused rerun leaves the plan as it was.

Each success posts a line that starts with ``Decision:`` in the thread, in the agent's name, and writes an audit row
(``run.triage_rerun``, ``run.triage_plan_edit``) that names the failed run, the agent and the chat run. Nothing here
writes ``acceptance``, ``goal``, ``non_goals`` or a step that is not pending: ``edit_problems`` compares the whole plan.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import curator, runs, tables, triage_rules
from evo_agents.hub.plans import MAX_VALUE_CHARS, PlanProblem, step_index
from evo_agents.hub.server import audit, chat_agents
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.chat_runs import agent_access, chat_run, chat_spend, post_agent
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import REFUSALS, AccountName, Rerun, RunId
from evo_agents.hub.server.runs.service.candidates import RerunCandidates, candidates_of
from evo_agents.hub.server.runs.service.controls import rerun_in
from evo_agents.hub.server.runs.service.dispatch import check_dispatcher, refuse_curator_plan
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal
from evo_agents.hub.server.triage import chain_of

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})

REASON_CHARS = 600
OLD_VALUE_CHARS = 300  # how much of the old verify or what the Decision line keeps, to undo the edit by
Reason = Annotated[str, Field(min_length=1, max_length=REASON_CHARS, description="why, for the Decision: line")]


class RerunOptions(BaseModel):
    worker: str | None = Field(None, max_length=200, description="a worker of the owner, by name; a fit candidate")
    runtime: str | None = Field(None, max_length=100, description="a runtime that worker has, for the rerun")
    account: AccountName | None = Field(None, description="one of the owner's accounts ready on that worker")
    cause_confirmed: bool = Field(
        False, description="true after the agent read the run's log and the cause the hub guessed is the right one"
    )


class TriageRerunIn(RerunOptions):
    reason: Reason


class TriagePlanIn(RerunOptions):
    step: str = Field(min_length=1, max_length=100, description="the step's id, a step that is pending")
    verify: str | None = Field(None, max_length=MAX_VALUE_CHARS, description="the new verify: no command fewer")
    what: str | None = Field(None, max_length=MAX_VALUE_CHARS, description="the new what: shorter than the old one")
    if_revision: int = Field(ge=1, description="the revision of the plan the edit was made against")
    reason: Reason
    rerun: bool = Field(False, description="run the failed step again once the plan is edited, in the same step")

    @model_validator(mode="after")
    def _something(self) -> TriagePlanIn:
        if self.verify is None and self.what is None:
            raise ValueError("give verify, what, or both")
        return self


class TriageActed(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the agent's name")
    run_id: int | None = Field(description="the run queued again; null for a plan edit without a rerun")
    plan_revision: int | None = Field(description="the revision the edit wrote; null for a rerun alone")


@dataclass(frozen=True)
class Frame:
    """What a triage call acts on: the chat run, the thread, the failed run, its owner and the owner's access."""

    agent: chat_agents.AgentRun
    chat_run_id: int
    chat_id: int
    root_id: int
    triage_id: int
    inferred: bool
    auto_reruns: int
    failed: object  # the row of the failed run
    project: str
    owner: Principal
    access: ProjectAccess


async def _frame(conn: AsyncConnection, user: Principal, run_id: int) -> Frame:
    agent = await chat_agents.agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if agent.kind == "chat" else None
    if found is None:
        raise HTTPException(404, f"run {run_id} is a {agent.kind} run: only the agent of a chat run triages a failure")
    t, r, p = tables.run_triage, tables.runs, tables.projects
    thread = (
        await conn.execute(
            select(t.c.id, t.c.run_id, t.c.cause_inferred, t.c.auto_reruns)
            .where(t.c.chat_id == found.chat_id, t.c.root_message_id == found.root_id)
            .with_for_update()
        )
    ).one_or_none()
    if thread is None:
        raise HTTPException(404, f"chat run {run_id} is in no thread the hub opened for a failed run")
    failed = (
        await conn.execute(
            select(
                r.c.id,
                r.c.state,
                r.c.kind,
                r.c.project_id,
                r.c.plan_id,
                r.c.step_key,
                r.c.repo,
                r.c.repos,
                r.c.worker_id,
                r.c.runtime,
                r.c.failure_cause,
                r.c.dispatched_by,
                r.c.dispatched_via,
                p.c.name.label("project"),
                p.c.triage_enabled,
            )
            .join_from(r, p, p.c.id == r.c.project_id)
            .where(r.c.id == thread.run_id)
        )
    ).one()
    if failed.project_id != agent.project_id:
        raise HTTPException(409, f"run {failed.id} is in another project than the agent of chat run {run_id}")
    if not failed.triage_enabled:
        raise HTTPException(409, f"triage is off for project {failed.project}: ask the owner in the thread")
    if failed.state != "failed":
        raise HTTPException(409, f"run {failed.id} is {failed.state} now, not failed: there is nothing to triage")
    if failed.dispatched_by != agent.principal.user_id:
        raise HTTPException(
            403,
            f"run {failed.id} belongs to another member than {agent.principal.login}, who dispatched this chat run: "
            "the agent acts on a run only for its owner",
        )
    owner = agent.principal
    access = await project_access(conn, owner, failed.project)
    return Frame(
        agent,
        run_id,
        found.chat_id,
        found.root_id,
        thread.id,
        thread.cause_inferred,
        thread.auto_reruns,
        failed,
        failed.project,
        owner,
        access,
    )


def _repos(failed) -> set[str]:
    named = {entry["repo"] for entry in failed.repos or () if isinstance(entry, dict) and entry.get("repo")}
    return named | ({failed.repo} if failed.repo else set())


async def _said(conn: AsyncConnection, frame: Frame, text: str) -> int:
    """The line of the thread, in the agent's name; it opens with ``Decision:``."""
    chat = await agent_access(conn, frame.chat_id, frame.agent.profile, frame.owner.user_id)
    if chat is None:
        raise HTTPException(409, f"@{frame.agent.profile.handle} is not in chat {frame.chat_id} any more")
    message_id, _ = await post_agent(
        conn, chat, frame.agent.profile, frame.chat_run_id, runs.clip(text, 4000), frame.root_id
    )
    return message_id


async def _audit(conn: AsyncConnection, frame: Frame, action: str, tail: str) -> None:
    failed = frame.failed
    where = f"{frame.project}/{failed.plan_id}" if failed.plan_id else f"{frame.project}/author"
    where += f"#{failed.step_key}" if failed.step_key else ""
    target = f"{where} run:{failed.id} agent:@{frame.agent.profile.handle} triage_run:{frame.chat_run_id}{tail}"
    await audit.record(
        conn,
        actor_id=frame.owner.user_id,
        token_id=frame.owner.token_id,
        action=action,
        target=target,
        project_id=frame.access.project_id,
    )


async def _budget(conn: AsyncConnection, frame: Frame) -> None:
    spent, budget = await chat_spend(conn, frame.chat_id)
    if spent >= budget:
        raise HTTPException(
            409,
            f"the chat's budget of {curator.money(budget)} for {rules.BUDGET_HOURS} hours is spent "
            f"({curator.money(spent)}): the agent reruns nothing until it frees up; ask the owner",
        )


def _unfit(found: RerunCandidates, options: RerunOptions, failed) -> tuple[int | None, str | None]:
    """(the worker id the rerun is pinned to, or None to keep the run's pin; the runtime to ask for, or None): from the
    options, against the candidates; 422 for a worker, runtime or account that is no candidate or no fit."""
    cause = failed.failure_cause
    name = options.worker
    if name is None and cause in triage_rules.SAME_WORKER:
        name = next((w.worker for w in found.workers if w.worker_id == failed.worker_id), None)
        if name is None:
            raise HTTPException(
                422, f"cause {cause} is rerun on the worker the run was on, which the owner has no more"
            )
    chosen = None
    if name is not None:
        chosen = next((w for w in found.workers if w.worker == name), None)
        if chosen is None:
            known = ", ".join(w.worker for w in found.workers) or "none"
            raise HTTPException(422, f"the owner has no worker {name}: their workers are {known}")
        if not chosen.fit:
            raise HTTPException(422, f"worker {name} is no fit to run it: {'; '.join(chosen.reasons)}")
        if cause in triage_rules.SAME_WORKER and chosen.worker_id != failed.worker_id:
            raise HTTPException(422, f"cause {cause} is rerun on the worker the run was on, not on {name}")
    pool = [chosen] if chosen is not None else [w for w in found.workers if w.fit]
    if options.runtime is not None and not any(options.runtime in w.runtimes for w in pool):
        raise HTTPException(
            422,
            f"no fit worker has runtime {options.runtime}: {', '.join(sorted({x for w in pool for x in w.runtimes}))}",
        )
    if options.account is not None:
        account = next((a for a in found.accounts if a.name == options.account), None)
        if account is None or account.state != "ready" or not any(options.account in w.accounts for w in pool):
            raise HTTPException(422, f"account {options.account} is not ready on a worker that can take the rerun")
    if cause in triage_rules.NEW_ACCOUNT:
        used = {a.name for a in found.accounts if a.used_by_run}
        new_account = options.account is not None and options.account not in used
        new_runtime = options.runtime is not None and options.runtime != failed.runtime
        if not new_account and not new_runtime:
            raise HTTPException(
                422,
                f"cause {cause} is rerun with an account the run did not use, or another runtime than {failed.runtime}",
            )
    return (None if chosen is None else chosen.worker_id), options.runtime


async def _rerun(conn: AsyncConnection, frame: Frame, options: RerunOptions, *, edited: bool) -> int:
    """Run the failed run again as ``options`` say, once the frame allows it; the id of the new run."""
    failed = frame.failed
    if failed.kind != "step":
        raise HTTPException(
            422,
            f"run {failed.id} is a {failed.kind} run: the agent reruns a step run only; ask the owner to dispatch the "
            f"{'plan' if failed.kind == 'plan' else 'author run'} again",
        )
    if frame.auto_reruns >= 1:
        raise HTTPException(409, f"run {failed.id} was rerun by the agent once already: ask the owner")
    problem = triage_rules.rerun_problem(
        failed.failure_cause, inferred=frame.inferred, confirmed=options.cause_confirmed, edited=edited
    )
    if problem:
        raise HTTPException(422, problem)
    depth, causes = await chain_of(conn, failed.id)
    refusal = triage_rules.chain_problem(depth, causes, failed.failure_cause)
    if refusal:
        raise HTTPException(409, refusal)
    await _budget(conn, frame)
    found = await candidates_of(
        conn,
        project=frame.project,
        project_id=frame.access.project_id,
        role=frame.access.role,
        owner_id=frame.owner.user_id,
        kind=failed.kind,
        state="failed",
        run_id=failed.id,
    )
    worker_id, runtime = _unfit(found, options, failed)
    body = Rerun(account=options.account) if options.account else None
    view = await rerun_in(conn, frame.owner, frame.project, failed.id, body, worker_id=worker_id, runtime=runtime)
    t = tables.run_triage
    await conn.execute(update(t).where(t.c.id == frame.triage_id).values(auto_reruns=t.c.auto_reruns + 1))
    return view.id


def _where(options: RerunOptions, run) -> str:
    parts = [f"worker {run.worker}"] if run.worker else []
    parts += [f"runtime {run.runtime}"]
    parts += [f"account {options.account}"] if options.account else []
    return ", ".join(parts)


@worker_router.post("/runs/{run_id}/triage/rerun", status_code=201, response_model=TriageActed, responses={**REFUSALS})
async def rerun(request: Request, run_id: RunId, body: TriageRerunIn, user: CurrentUser) -> TriageActed:
    """Run the failed run of this triage thread again for its owner, inside the frame; 422 for a cause with no
    automatic rerun, 409 when the chain of reruns, the chat's budget or an earlier rerun forbids it."""
    async with request.app.state.engine.begin() as conn:
        frame = await _frame(conn, user, run_id)
        new_id = await _rerun(conn, frame, body, edited=False)
        text = (
            f"Decision: ran run #{frame.failed.id} again as run #{new_id} "
            f"(cause {frame.failed.failure_cause}): {body.reason} Undo: cancel run #{new_id}."
        )
        message_id = await _said(conn, frame, text)
        await _audit(conn, frame, audit.RUN_TRIAGE_RERUN, f" rerun:{new_id}")
    log.info("triage rerun", extra={"run_id": frame.failed.id, "rerun": new_id, "agent_run_id": run_id})
    return TriageActed(message_id=message_id, run_id=new_id, plan_revision=None)


def _edited(held_body: dict, key: str, body: TriagePlanIn) -> dict:
    """The plan with the ``verify`` or ``what`` of step ``key`` set as the call says; nothing else differs."""
    try:
        index = step_index(held_body, key)
    except PlanProblem as exc:
        raise HTTPException(422, str(exc)) from None
    new = copy.deepcopy(held_body)
    for name in triage_rules.EDITABLE:
        value = getattr(body, name)
        if value is not None:
            new["steps"][index][name] = value
    return new


def _was(step: dict, body: TriagePlanIn) -> str:
    parts = []
    for name in triage_rules.EDITABLE:
        if getattr(body, name) is not None:
            old = " ".join(str(step.get(name, "")).split())
            parts.append(f"old {name}: {runs.clip(old, OLD_VALUE_CHARS)}")
    return "; ".join(parts)


async def _edit_plan(conn: AsyncConnection, frame: Frame, body: TriagePlanIn) -> tuple[int, int, str]:
    """Write the edit as a revision under the owner's name: (the old revision, the new one, what the old step said)."""
    failed = frame.failed
    if failed.plan_id is None:
        raise HTTPException(422, f"run {failed.id} has no plan to edit: it is a run that writes one; ask the owner")
    check_dispatcher(frame.access)
    await refuse_curator_plan(conn, frame.access, failed.plan_id)
    held = await plan_routes._visible(conn, frame.access, failed.plan_id, None, lock=True)
    if held.revision != body.if_revision:
        raise plan_routes._stale(frame.access, held, body.if_revision)
    new = _edited(held.body, body.step, body)
    problems = triage_rules.edit_problems(held.body, new, _repos(failed))
    if problems:
        raise plan_routes.PlanError(422, f"plan {held.plan_id}: {'; '.join(problems)}; nothing was written")
    plan_routes._schema_checked(new, held.plan_id)
    owner = Principal(frame.owner.user_id, frame.owner.login, False, frame.owner.token_id, MACHINE, "")
    written = await plan_routes.write_plan(conn, frame.access, owner, new, if_revision=held.revision)
    old_step = held.body["steps"][step_index(held.body, body.step)]
    return held.revision, written.revision, _was(old_step, body)


@worker_router.post(
    "/runs/{run_id}/triage/plan",
    status_code=201,
    response_model=TriageActed,
    responses={**REFUSALS, **plan_routes.REFUSALS},
)
async def edit_plan(request: Request, run_id: RunId, body: TriagePlanIn, user: CurrentUser):
    """Change the ``verify`` or narrow the ``what`` of a pending step of the failed run's plan for its owner, as a new
    revision under their name, inside the frame (``triage_rules.edit_problems``); with ``rerun`` the failed step is run
    again in the same transaction, and a refused rerun leaves the plan as it was."""
    try:
        async with request.app.state.engine.begin() as conn:
            frame = await _frame(conn, user, run_id)
            was, now, old = await _edit_plan(conn, frame, body)
            new_id = await _rerun(conn, frame, body, edited=True) if body.rerun else None
            fields = " and ".join(name for name in triage_rules.EDITABLE if getattr(body, name) is not None)
            text = (
                f"Decision: changed {fields} of step {body.step} of plan {frame.failed.plan_id} (revision {was} to "
                f"{now}): {body.reason} {old}. Undo: `evo-agents hub plan history` has revision {was}."
                + (f" Ran run #{frame.failed.id} again as run #{new_id}." if new_id else "")
            )
            message_id = await _said(conn, frame, text)
            tail = f" revision:{now}" + (f" rerun:{new_id}" if new_id else "")
            await _audit(conn, frame, audit.RUN_TRIAGE_PLAN_EDIT, f" step:{body.step}{tail}")
    except plan_routes.PlanError as exc:
        return plan_routes._refusal(request, exc)
    log.info("triage plan edit", extra={"run_id": frame.failed.id, "revision": now, "agent_run_id": run_id})
    return TriageActed(message_id=message_id, run_id=new_id, plan_revision=now)

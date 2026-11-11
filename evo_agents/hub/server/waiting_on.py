"""What a wait (a decision of category wait, ``run_waits``) waits for, as every view of a decision shows it in
``waiting_on``: null for any other decision.

``kind`` says which of six it is, and the field of that name holds what the hub knows of it:

- ``merge_check``: the merge check whose verdict answers it (``merge_check._open_wait``, merge_checks.decision_id): its
  pull request, repo, head, state and the state of each Judge;
- ``agent_answer``: the run of another agent whose end answers it (``agent_asks.open_wait``, decisions.awaits_run_id):
  the agent's handle and project, that run and its state, the question the waiting run asked and when that run last did
  something;
- ``workflow_run``: a run of a GitHub Actions workflow (``workflow_waits``): the hub's latest read of it, made by the
  poll of the reaper and kept in workflow_run_reads, never read from GitHub for a view; before the first read, only the
  repo and run id the wait names;
- ``plan_step``: a step of a plan (event ``plan.step``): the plan, the step and the status it waits for, and, when the
  caller reads that plan, the step's title and status now and the run working on it;
- ``event``: any other event of the hub: its kind, a name for a kind the hub knows, and the pairs of ``contains``;
- ``time``: a wait on its time alone.

The views read it in a few queries a page (``fill``), whatever the number of waits on it.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import hub_events, runs, tables
from evo_agents.hub.plans import step_key

QUESTION_BYTES = 1000  # of the question an agent asked another, in UTF-8
EVENT_NAMES = {  # a name a person reads for a kind of hub event the hub knows
    hub_events.CLAIM: "A claim of a Lean Canvas",
    hub_events.BASELINE: "A baseline of a Lean Canvas, approved",
    hub_events.SIGNALS: "Signals posted to the project",
    hub_events.SRS_APPROVED: "A version of an SRS document, approved",
    hub_events.SRS_USE_CASE_APPROVED: "A use case of an SRS document, approved",
    hub_events.TASK_STATUS: "A task's status, set or changed",
    hub_events.PLAN_STEP: "A step's status of a plan, set or changed",
    hub_events.WORKFLOW_RUN: "A run of a GitHub Actions workflow",
}
Kind = Literal["workflow_run", "plan_step", "agent_answer", "merge_check", "time", "event"]


class RunRef(BaseModel):
    id: int
    kind: str
    state: str


class WorkflowStep(BaseModel):
    number: int
    name: str | None
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None
    url: str | None = Field(description="the step on GitHub: its job's page at #step:N:1")


class WorkflowJob(BaseModel):
    id: int
    name: str | None
    status: str | None
    conclusion: str | None
    started_at: datetime | None
    completed_at: datetime | None
    url: str | None = Field(description="the job's page on GitHub")
    steps: list[WorkflowStep]
    steps_total: int = Field(description="the steps GitHub lists for the job; steps keeps at most 60")


class WorkflowRunWait(BaseModel):
    repo: str = Field(description="owner/name")
    run_id: int
    attempt: int | None
    workflow: str | None = Field(description="the workflow's name")
    title: str | None = Field(description="the run's title, such as its commit's subject")
    branch: str | None
    sha: str | None
    event: str | None = Field(description="what started the run, such as push")
    status: str | None = Field(description="as GitHub says it: queued, in_progress, completed ...")
    conclusion: str | None
    url: str | None
    created_at: datetime | None
    started_at: datetime | None
    updated_at: datetime | None
    read_at: datetime | None = Field(
        description="when the hub last read the run from GitHub; null before its first read"
    )
    typical_seconds: int | None = Field(
        description="the median time of the latest runs of the workflow that succeeded; null when not known"
    )
    jobs: list[WorkflowJob] = Field(
        description="the jobs of its latest attempt, in GitHub's order; past 40, those that failed or run are kept"
    )
    jobs_total: int | None = Field(description="the jobs GitHub lists for that attempt; null before the first read")


class PlanStepWait(BaseModel):
    plan: str
    step: str
    wanted: str | None = Field(description="the status the wait waits for, such as done; null: any change")
    title: str | None = Field(description="the step's title; null when the caller does not read the plan")
    status: str | None = Field(description="the step's status now; null when the caller does not read the plan")
    run: RunRef | None = Field(description="the active run of the plan working on it, when the caller reads the plan")


class AgentAnswerWait(BaseModel):
    handle: str | None = Field(description="the agent asked, @ left out")
    project: str | None = Field(description="the agent's project")
    run: RunRef | None = Field(description="the agent's run whose end answers the wait")
    question: str | None = Field(description="what the waiting run asked it")
    chat_id: int | None = Field(description="the chat the question went to")
    thread_id: int | None = Field(description="the message that roots the thread of it")
    last_activity_at: datetime | None = Field(description="when the agent's run last wrote an event")


class MergeJudge(BaseModel):
    round: Literal["first", "second"]
    state: Literal["pending", "judging", "passed", "failed"]
    run_id: int | None = Field(description="the judge run")
    model: str | None


class MergeCheckWait(BaseModel):
    id: int
    repo: str
    number: int = Field(description="the pull request's number")
    title: str | None
    url: str | None
    head_sha: str
    state: Literal["judging", "passed", "failed"]
    reason: str | None
    judges: list[MergeJudge]


class TimeWait(BaseModel):
    wake_at: datetime | None


class EventPair(BaseModel):
    key: str
    value: str = Field(description="the value as text, JSON for one that is not a text")


class EventWait(BaseModel):
    kind: str
    name: str | None = Field(description="what the kind is, for a kind the hub knows")
    contains: list[EventPair]


class WaitingOn(BaseModel):
    kind: Kind
    workflow_run: WorkflowRunWait | None = None
    plan_step: PlanStepWait | None = None
    agent_answer: AgentAnswerWait | None = None
    merge_check: MergeCheckWait | None = None
    time: TimeWait | None = None
    event: EventWait | None = None


def _pairs(contains: dict) -> list[EventPair]:
    return [
        EventPair(key=key, value=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        for key, value in contains.items()
    ]


def _judges(row) -> list[MergeJudge]:
    verdict = row.verdict if isinstance(row.verdict, dict) else {}
    found: list[MergeJudge] = []
    for round_ in ("first", "second"):
        entry = verdict.get(round_)
        if isinstance(entry, dict):
            state = "passed" if entry.get("passed") else "failed"
            found.append(MergeJudge(round=round_, state=state, run_id=entry.get("run_id"), model=entry.get("model")))
            continue
        if round_ == "second" and not (found and found[0].state == "passed" and row.state == "judging"):
            break
        state = "judging" if row.judge_run_id is not None and row.state == "judging" else "pending"
        found.append(
            MergeJudge(round=round_, state=state, run_id=row.judge_run_id if state == "judging" else None, model=None)
        )
        break
    return found


async def _merge_checks(conn: AsyncConnection, ids: list[int]) -> dict[int, MergeCheckWait]:
    m = tables.merge_checks
    query = select(m).where(m.c.decision_id.in_(ids))
    return {
        row.decision_id: MergeCheckWait(
            id=row.id,
            repo=row.repo,
            number=row.number,
            title=row.title,
            url=row.web_url,
            head_sha=row.head_sha,
            state=row.state,
            reason=row.reason,
            judges=_judges(row),
        )
        for row in await conn.execute(query)
    }


async def _agent_answers(conn: AsyncConnection, waits: dict[int, tuple]) -> dict[int, AgentAnswerWait]:
    """The run asked of each wait of ``waits`` (id: (run_id, awaits_run_id, asked_at)) that names one."""
    r, p, m, e = tables.runs, tables.agent_profiles, tables.chat_messages, tables.run_events
    asked = {awaits for _, awaits, _ in waits.values() if awaits is not None}
    if not asked:
        return {}
    last = select(func.max(e.c.at)).where(e.c.run_id == r.c.id).scalar_subquery()
    query = (
        select(
            r.c.id,
            r.c.kind,
            r.c.state,
            r.c.chat_id,
            r.c.chat_message_id,
            p.c.handle,
            tables.projects.c.name,
            last.label("last"),
        )
        .select_from(
            r.outerjoin(p, p.c.id == r.c.profile_id).outerjoin(tables.projects, tables.projects.c.id == p.c.project_id)
        )
        .where(r.c.id.in_(asked))
    )
    children = {row.id: row for row in await conn.execute(query)}
    found = {}
    for decision_id, (run_id, awaits, asked_at) in waits.items():
        child = children.get(awaits)
        if child is None:
            continue
        question = thread = None
        if child.chat_id is not None:  # the waiting run's message to the agent, else the one that started its run
            said = select(m.c.body, func.coalesce(m.c.thread_root_id, m.c.id)).order_by(m.c.id.desc()).limit(1)
            mine = said.where(m.c.chat_id == child.chat_id, m.c.run_id == run_id, m.c.created_at <= asked_at)
            row = (await conn.execute(mine)).one_or_none()
            if row is None and child.chat_message_id is not None:
                row = (await conn.execute(said.where(m.c.id == child.chat_message_id))).one_or_none()
            if row is not None:
                question, thread = runs.clip(row[0], QUESTION_BYTES), row[1]
        found[decision_id] = AgentAnswerWait(
            handle=child.handle,
            project=child.name,
            run=RunRef(id=child.id, kind=child.kind, state=child.state),
            question=question,
            chat_id=child.chat_id,
            thread_id=thread,
            last_activity_at=child.last,
        )
    return found


async def _workflow_runs(conn: AsyncConnection, waits: dict[int, tuple[int, dict]]) -> dict[int, WorkflowRunWait]:
    """The latest read of the run each wait of ``waits`` (id: (project_id, contains)) names."""
    w = tables.workflow_run_reads
    named = {
        decision_id: (project_id, contains.get("repo"), contains.get("run_id"))
        for decision_id, (project_id, contains) in waits.items()
        if isinstance(contains.get("repo"), str) and type(contains.get("run_id")) is int
    }
    reads = {}
    if named:
        keys = sorted(set(named.values()))
        query = select(w).where(tuple_(w.c.project_id, w.c.repo, w.c.run_id).in_(keys))
        reads = {(row.project_id, row.repo, row.run_id): row for row in await conn.execute(query)}
    found = {}
    for decision_id, key in named.items():
        row = reads.get(key)
        if row is None:
            found[decision_id] = WorkflowRunWait.model_validate(
                {"repo": key[1], "run_id": key[2], "read_at": None, "typical_seconds": None, "jobs": []}
                | dict.fromkeys(("attempt", "jobs_total", "created_at", "started_at", "updated_at"))
                | dict.fromkeys(("workflow", "title", "branch", "sha", "event", "status", "conclusion", "url"))
            )
            continue
        data = dict(row.data) | {"repo": key[1], "run_id": key[2]}
        data |= {"read_at": row.read_at, "typical_seconds": row.typical_seconds}
        found[decision_id] = WorkflowRunWait.model_validate(data)
    return found


async def _plan_steps(
    conn: AsyncConnection, waits: dict[int, tuple[int, dict]], plans: Collection[str] | None
) -> dict[int, PlanStepWait]:
    """The step each wait of ``waits`` (id: (project_id, contains)) names, with its title, status and run when the
    caller reads its plan (``plans``; None: the caller reads none of it)."""
    pl, r = tables.plans, tables.runs
    found = {}
    for decision_id, (project_id, contains) in waits.items():
        plan, step = contains.get("plan") or contains.get("plan_id"), contains.get("step")
        if not isinstance(plan, str) or step is None:
            continue
        step, wanted = str(step), contains.get("status") if isinstance(contains.get("status"), str) else None
        view = PlanStepWait(plan=plan, step=step, wanted=wanted, title=None, status=None, run=None)
        if plans is not None and plan in plans:
            query = select(pl.c.body).where(pl.c.project_id == project_id, pl.c.plan_id == plan)
            body = (await conn.execute(query)).scalar_one_or_none() or {}
            steps = body.get("steps") if isinstance(body.get("steps"), list) else []
            item = next((s for i, s in enumerate(steps) if step_key(s, i) == step and isinstance(s, dict)), None)
            if item is not None:
                view.title = item.get("title") if isinstance(item.get("title"), str) else None
                view.status = item.get("status") if isinstance(item.get("status"), str) else None
            on_step = (r.c.kind == "step") & (r.c.step_key == step)
            working = (
                select(r.c.id, r.c.kind, r.c.state)
                .where(
                    r.c.project_id == project_id,
                    r.c.plan_id == plan,
                    r.c.state.in_(tables.RUN_ACTIVE),
                    on_step | (r.c.kind == "plan"),
                )
                .order_by(case((on_step, 0), else_=1), r.c.id.desc())
                .limit(1)
            )
            run = (await conn.execute(working)).one_or_none()
            view.run = None if run is None else RunRef(id=run.id, kind=run.kind, state=run.state)
        found[decision_id] = view
    return found


async def fill(conn: AsyncConnection, decisions: list, plans: Collection[str] | None = None) -> None:
    """Set ``waiting_on`` of each wait of ``decisions`` (``decision_models.Decision``); ``plans`` are the plans the
    caller reads, None when the view goes to no reader of plans (a worker, an answer's response)."""
    waits = [decision for decision in decisions if decision.category == runs.WAIT_DECISION_CATEGORY]
    if not waits:
        return
    d = tables.decisions
    ids = [decision.id for decision in waits]
    query = select(d.c.id, d.c.project_id, d.c.run_id, d.c.awaits_run_id, d.c.asked_at, d.c.wake_on).where(
        d.c.id.in_(ids)
    )
    rows = {row.id: row for row in await conn.execute(query)}
    checks = await _merge_checks(conn, ids)
    asked = await _agent_answers(
        conn, {i: (row.run_id, row.awaits_run_id, row.asked_at) for i, row in rows.items() if row.awaits_run_id}
    )
    on = {
        i: (row.project_id, row.wake_on["kind"], row.wake_on.get("contains") or {})
        for i, row in rows.items()
        if row.wake_on
    }
    workflows = await _workflow_runs(
        conn, {i: (p, c) for i, (p, kind, c) in on.items() if kind == hub_events.WORKFLOW_RUN}
    )
    steps = await _plan_steps(
        conn, {i: (p, c) for i, (p, kind, c) in on.items() if kind == hub_events.PLAN_STEP}, plans
    )
    for decision in waits:
        i = decision.id
        if i in checks:
            decision.waiting_on = WaitingOn(kind="merge_check", merge_check=checks[i])
        elif i in asked:
            decision.waiting_on = WaitingOn(kind="agent_answer", agent_answer=asked[i])
        elif i in workflows:
            decision.waiting_on = WaitingOn(kind="workflow_run", workflow_run=workflows[i])
        elif i in steps:
            decision.waiting_on = WaitingOn(kind="plan_step", plan_step=steps[i])
        elif i in on:
            kind, contains = on[i][1], on[i][2]
            event = EventWait(kind=kind, name=EVENT_NAMES.get(kind), contains=_pairs(contains))
            decision.waiting_on = WaitingOn(kind="event", event=event)
        else:
            decision.waiting_on = WaitingOn(kind="time", time=TimeWait(wake_at=decision.wake_at))

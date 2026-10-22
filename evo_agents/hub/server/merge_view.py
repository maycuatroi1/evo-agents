"""The routes of the hub's merge queue on GitLab (plan gitlab-merge-queue): GET /v1/projects/{p}/merge-queue lists, to a
member with a grant on the project, the merge requests of its queue (``repo``'s alone when given), newest first, each
with where it comes from, its state, why it waits or is blocked (the gate first), the risk and the Judges' verdicts,
the results of its checks after its merge, and its ledger (merge_events), the newest MAX_EVENTS lines first; the audit
samples, those waiting for an answer first; and the breaker of each repo (``evo_agents.hub.server.merge_breaker``).
GET .../merge-queue/figures counts, for each repo, agent and model,
the merges of the last ``days``, the reverts, the rollbacks, the Judge's passes found wrong and the time in the queue
(``post_merge.figures``).

An admin of the project answers what the hub asks after a merge (``evo_agents.hub.server.post_merge``): POST
.../merge-queue/samples/{id} answers an audit sample ``ok`` or ``faulty`` with a note, POST .../merge-queue/{id}/release
approves the PROD release of a merge when the policy releases its repo by hand, and POST .../merge-queue/{id}/revert
asks the hub to revert a merge it followed, one of the suspects it could not tell apart. POST .../merge-queue/repos/
{repo}/resume resets the breaker of a repo that tripped: its paused merge requests wait for their turn again. POST
.../merge-queue/{id}/pass passes, with a reason, the hold of a merge request at a gate that holds it for a person
(gate 3, score hacking; ``evo_agents.hub.server.merge_pass``): the queue goes on from that gate at the same head.
``evo_agents.hub.server.merge_gate`` moves the merge requests; no value of a secret is ever in a row.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_breaker, merge_gate, post_merge, tables
from evo_agents.hub.server import merge_breaker as breaker_rows
from evo_agents.hub.server import merge_pass
from evo_agents.hub.server import post_merge as after_merge
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.autonomy_policies import effective_policy
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

MAX_LIST = 200
MAX_EVENTS = 100  # of a merge request's ledger, the newest, on the route
MAX_SAMPLES = 50  # on the route, those waiting for an answer first
MAX_DAYS = 365
MAX_ID = 2**63 - 1

router = APIRouter(prefix="/v1/projects", tags=["merge queue"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
ANSWER_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}
RowId = Annotated[int, Path(ge=1, le=MAX_ID)]
RepoName = Annotated[str, Path(min_length=1, max_length=200)]


class MergeEvent(BaseModel):
    id: int
    kind: str
    gate: int | None = Field(description="the gate's number, 0 to 7, for kind gate")
    gate_name: str | None
    passed: bool | None
    reason: str | None
    sha: str | None
    run_id: int | None
    detail: dict[str, Any]
    created_at: datetime


class QueuedMergeRequest(BaseModel):
    """A merge request of the queue: where it comes from, where it stands and why, and its ledger."""

    id: int
    repo: str
    iid: int | None = Field(description="null while the hub could not open it")
    source: str = Field(description=f"one of {', '.join(merge_gate.SOURCES)}")
    target_branch: str
    source_branch: str
    run_id: int | None
    plan_id: str | None
    steps: list[str]
    change_id: int | None
    profile: str | None
    model: str | None
    title: str | None
    web_url: str | None
    state: str = Field(description=f"one of {', '.join(merge_gate.STATES)}")
    reason: str | None = Field(description="why it waits or is blocked, the gate first")
    head_sha: str | None
    fingerprint: str | None
    risk: dict[str, Any] | None
    verdict: dict[str, Any] | None
    judge_run_id: int | None
    queued_at: datetime | None
    merged_at: datetime | None
    created_at: datetime
    updated_at: datetime
    events: list[MergeEvent] = Field(description=f"its ledger, the newest {MAX_EVENTS} first")
    before_sha: str | None = Field(None, description="the head of the target branch its last turn rebased it onto")
    rebases: int = Field(0, description="its rebases again in a row since its gates last passed")
    after_sha: str | None = Field(None, description="its commit on the target branch, once merged")
    reverts_id: int | None = Field(None, description="a revert's: the merge request of the queue it undoes")
    uat_result: str | None = Field(None, description=f"its own UAT check: one of {', '.join(post_merge.RESULTS)}")
    prod_result: str | None = Field(None, description=f"its own PROD check: one of {', '.join(post_merge.RESULTS)}")


class MergeSample(BaseModel):
    """A merge request drawn for an audit sample, and an admin's answer."""

    id: int
    queue_id: int
    repo: str
    iid: int | None
    title: str | None
    web_url: str | None
    result: str | None = Field(
        description=f"null until an admin answers: one of {', '.join(post_merge.SAMPLE_RESULTS)}"
    )
    note: str | None
    answered_by: str | None
    answered_at: datetime | None
    created_at: datetime


class MergeBreaker(BaseModel):
    """The breaker of a repo of the queue: armed, or tripped until an admin resets it."""

    repo: str
    state: str = Field(description=f"one of {', '.join(merge_breaker.STATES)}")
    cause: str | None = Field(description=f"what tripped it last: one of {', '.join(merge_breaker.CAUSES)}")
    reason: str | None = Field(description="why it tripped last, each thing that tripped it")
    tripped_at: datetime | None
    trips: int = Field(description="how often it tripped")
    resumed_by: str | None = Field(description="the admin who reset it last")
    resumed_at: datetime | None = Field(description="what trips it counts from then")
    red_since: datetime | None = Field(description="the hub saw its target branch turn red then, none passing since")


class BreakerReset(MergeBreaker):
    """A breaker an admin reset, and the merge requests waiting for their turn again."""

    resumed: list[int] = Field(description="the iids of the merge requests it had paused, ready again")


class MergeQueueView(BaseModel):
    project: str
    requests: list[QueuedMergeRequest] = Field(description="newest first")
    samples: list[MergeSample] = Field([], description=f"the audit samples, those waiting first, at most {MAX_SAMPLES}")
    breakers: list[MergeBreaker] = Field([], description="the breaker of each repo the queue passed over, by repo")


class SampleAnswer(BaseModel):
    result: Literal[post_merge.SAMPLE_RESULTS] = Field(description="faulty counts as a pass of the Judge found wrong")
    note: str | None = Field(None, max_length=after_merge.MAX_NOTE_CHARS)


class HoldPass(BaseModel):
    reason: str = Field(
        min_length=1,
        max_length=merge_pass.MAX_REASON_CHARS,
        description="why the hold may go on: the ledger and the audit keep it with the admin's login",
    )


class MergeFigure(BaseModel):
    """The merges of one repo, agent and model in the window."""

    repo: str
    agent: str | None = Field(description="the agent profile of the run that made them, or person for a person's")
    model: str | None
    merged: int
    reverted: int = Field(description="merged, then found the culprit of a failed check")
    rolled_back: int = Field(description="merged, then PROD went back for their deploy")
    judge_misses: int = Field(description="passed by the Judge, then reverted, rolled back or sampled faulty")
    wait_seconds: float | None = Field(description="the mean time from ready to merged")
    wait_seconds_max: float | None
    uat_seconds: float | None = Field(description="the mean time from merged to the UAT check passed")
    prod_seconds: float | None = Field(description="the mean time from merged to the PROD check passed")


class MergeFigures(BaseModel):
    project: str
    days: int
    figures: list[MergeFigure]


async def _reader(conn: AsyncConnection, user, project: str):
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the merge queue of project {access.name} needs a grant on it")
    return access


def _entry(row, events: list) -> QueuedMergeRequest:
    keys = set(QueuedMergeRequest.model_fields) - {"events"}
    return QueuedMergeRequest(
        **{key: getattr(row, key) for key in keys},
        events=[
            MergeEvent(
                id=event.id,
                kind=event.kind,
                gate=event.gate,
                gate_name=merge_gate.GATES.get(event.gate) if event.gate is not None else None,
                passed=event.passed,
                reason=event.reason,
                sha=event.sha,
                run_id=event.run_id,
                detail=event.detail,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


async def _events(conn: AsyncConnection, ids: list[int]) -> dict[int, list]:
    e = tables.merge_events
    events: dict[int, list] = {row_id: [] for row_id in ids}
    if ids:
        ranked = select(e, func.row_number().over(partition_by=e.c.queue_id, order_by=e.c.id.desc()).label("n"))
        ranked = ranked.where(e.c.queue_id.in_(ids)).subquery()
        found = select(ranked).where(ranked.c.n <= MAX_EVENTS).order_by(ranked.c.queue_id, ranked.c.id.desc())
        for event in await conn.execute(found):
            events[event.queue_id].append(event)
    return events


def _samples_query(project_id: int):
    s, q, u = tables.merge_samples, tables.merge_queue, tables.users
    return (
        select(s, q.c.repo, q.c.iid, q.c.title, q.c.web_url, u.c.login.label("answered_login"))
        .join_from(s, q, q.c.id == s.c.queue_id)
        .join(u, u.c.id == s.c.answered_by, isouter=True)
        .where(s.c.project_id == project_id)
    )


def _breaker(row) -> MergeBreaker:
    keys = set(MergeBreaker.model_fields) - {"resumed_by"}
    return MergeBreaker(**{key: getattr(row, key) for key in keys}, resumed_by=row.resumed_login)


def _sample(row) -> MergeSample:
    return MergeSample(
        id=row.id,
        queue_id=row.queue_id,
        repo=row.repo,
        iid=row.iid,
        title=row.title,
        web_url=row.web_url,
        result=row.result,
        note=row.note,
        answered_by=row.answered_login,
        answered_at=row.answered_at,
        created_at=row.created_at,
    )


@router.get("/{project}/merge-queue", response_model=MergeQueueView, responses=REFUSALS)
async def show_queue(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    repo: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=MAX_LIST),
) -> MergeQueueView:
    """The merge requests of the project's queue (``repo``'s alone when given), newest first, each with its ledger, and
    its audit samples."""
    q, s = tables.merge_queue, tables.merge_samples
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        query = select(q).where(q.c.project_id == access.project_id)
        if repo is not None:
            query = query.where(q.c.repo == repo)
        rows = (await conn.execute(query.order_by(q.c.id.desc()).limit(limit))).all()
        events = await _events(conn, [row.id for row in rows])
        samples = _samples_query(access.project_id)
        if repo is not None:
            samples = samples.where(q.c.repo == repo)
        samples = samples.order_by(s.c.result.is_not(None), s.c.id.desc()).limit(MAX_SAMPLES)
        found = (await conn.execute(samples)).all()
        breakers = await breaker_rows.breakers(conn, access.project_id, repo)
    return MergeQueueView(
        project=access.name,
        requests=[_entry(row, events[row.id]) for row in rows],
        samples=[_sample(row) for row in found],
        breakers=[_breaker(row) for row in breakers],
    )


def _seconds(start, end):
    return func.extract("epoch", end - start)


@router.get("/{project}/merge-queue/figures", response_model=MergeFigures, responses=REFUSALS)
async def queue_figures(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    repo: str | None = Query(None, max_length=200),
    days: int = Query(30, ge=1, le=MAX_DAYS),
) -> MergeFigures:
    """For each repo, agent and model, the merges of the last ``days``: how many, reverted, rolled back, passed by
    the Judge and found wrong, and the time in the queue and to the checks."""
    q, e, s = tables.merge_queue, tables.merge_events, tables.merge_samples
    async with request.app.state.engine.begin() as conn:
        access = await _reader(conn, user, project)
        since = func.now() - timedelta(days=days)
        query = select(
            q.c.id,
            q.c.repo,
            q.c.source,
            q.c.profile,
            q.c.model,
            q.c.state,
            q.c.verdict[("first", "passed")].as_boolean().label("judged"),
            _seconds(q.c.queued_at, q.c.merged_at).label("waited"),
            _seconds(q.c.merged_at, q.c.uat_at).label("to_uat"),
            _seconds(q.c.merged_at, q.c.prod_at).label("to_prod"),
        ).where(
            q.c.project_id == access.project_id,
            q.c.merged_at >= since,
            q.c.source != post_merge.REVERT,
        )
        if repo is not None:
            query = query.where(q.c.repo == repo)
        rows = [dict(row._mapping) for row in await conn.execute(query)]
        ids = [row["id"] for row in rows]
        rolled = select(e.c.queue_id).where(e.c.queue_id.in_(ids), e.c.kind == "rollback").distinct()
        faulty = select(s.c.queue_id).where(s.c.queue_id.in_(ids), s.c.result == "faulty")
        rolled_back = {row_id for (row_id,) in await conn.execute(rolled)} if ids else set()
        found_faulty = {row_id for (row_id,) in await conn.execute(faulty)} if ids else set()
    for row in rows:
        row["judged"] = bool(row["judged"])
        for key in ("waited", "to_uat", "to_prod"):
            row[key] = float(row[key]) if row[key] is not None else None
    figures = post_merge.figures(rows, rolled_back=rolled_back, faulty=found_faulty)
    return MergeFigures(project=access.name, days=days, figures=[MergeFigure(**item) for item in figures])


@router.post("/{project}/merge-queue/samples/{sample_id}", response_model=MergeSample, responses=ANSWER_REFUSALS)
async def answer_sample(
    request: Request, project: ProjectName, sample_id: RowId, body: SampleAnswer, user: CurrentUser
) -> MergeSample:
    """An admin of the project answers an audit sample: ``ok``, or ``faulty``, which counts against the Judge."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await after_merge.answer_sample(conn, user, access, sample_id, body.result, body.note)
        s = tables.merge_samples
        row = (await conn.execute(_samples_query(access.project_id).where(s.c.id == sample_id))).one()
    return _sample(row)


async def _answered(conn: AsyncConnection, row) -> QueuedMergeRequest:
    return _entry(row, (await _events(conn, [row.id]))[row.id])


@router.post("/{project}/merge-queue/{queue_id}/release", response_model=QueuedMergeRequest, responses=ANSWER_REFUSALS)
async def approve_release(
    request: Request, project: ProjectName, queue_id: RowId, user: CurrentUser
) -> QueuedMergeRequest:
    """An admin approves the PROD release of a merge whose repo the policy releases by hand (``prod_release``
    manual): the hub's next pass runs its deploy job, carrying the merges before it."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        q = tables.merge_queue
        repo = (await conn.execute(select(q.c.repo).where(q.c.id == queue_id))).scalar_one_or_none()
        policy = await effective_policy(conn, access.project_id)
        release = policy["repos"].get(repo, {}).get("prod_release", "immediate") if repo else "immediate"
        row = await after_merge.approve_release(conn, user, access, queue_id, release)
        return await _answered(conn, row)


@router.post("/{project}/merge-queue/{queue_id}/revert", response_model=QueuedMergeRequest, responses=ANSWER_REFUSALS)
async def ask_revert(request: Request, project: ProjectName, queue_id: RowId, user: CurrentUser) -> QueuedMergeRequest:
    """An admin asks the hub to revert a merge it followed after its merge, such as one of the suspects of a failed
    check it could not tell apart: the hub's next pass opens its revert, first in the queue."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = await after_merge.ask_revert(conn, user, access, queue_id)
        return await _answered(conn, row)


@router.post("/{project}/merge-queue/{queue_id}/pass", response_model=QueuedMergeRequest, responses=ANSWER_REFUSALS)
async def pass_hold(
    request: Request, project: ProjectName, queue_id: RowId, body: HoldPass, user: CurrentUser
) -> QueuedMergeRequest:
    """An admin of the project passes the hold of a merge request at a gate that holds it for a person (gate 3, score
    hacking), with a reason: it is gated again at the same head, and the queue goes on from that gate; the later gates,
    the Judges, its turn and its pipeline still apply, and a new push takes the pass back."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = await merge_pass.pass_hold(conn, user, access, queue_id, body.reason)
        return await _answered(conn, row)


@router.post("/{project}/merge-queue/repos/{repo}/resume", response_model=BreakerReset, responses=ANSWER_REFUSALS)
async def resume_repo(request: Request, project: ProjectName, repo: RepoName, user: CurrentUser) -> BreakerReset:
    """An admin of the project resets the breaker of a repo that tripped: its paused merge requests wait for their turn
    again, at their place, and what trips it counts from now."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        resumed = await breaker_rows.resume(conn, user, access, repo)
        (row,) = await breaker_rows.breakers(conn, access.project_id, repo)
    return BreakerReset(**_breaker(row).model_dump(), resumed=resumed)

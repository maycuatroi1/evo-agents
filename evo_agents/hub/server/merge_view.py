"""The route that shows the hub's merge queue on GitLab (plan gitlab-merge-queue): GET /v1/projects/{p}/merge-queue
lists, to a member with a grant on the project, the merge requests of its queue (``repo``'s alone when given), newest
first, each with where it comes from, its state, why it waits or is blocked (the gate first), the risk and the Judges'
verdicts, and its ledger (merge_events), the newest MAX_EVENTS lines first. ``evo_agents.hub.server.merge_gate`` moves
them; no value of a secret is ever in a row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from evo_agents.hub import merge_gate, tables
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

MAX_LIST = 200
MAX_EVENTS = 100  # of a merge request's ledger, the newest, on the route

router = APIRouter(prefix="/v1/projects", tags=["merge queue"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


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


class MergeQueueView(BaseModel):
    project: str
    requests: list[QueuedMergeRequest] = Field(description="newest first")


@router.get("/{project}/merge-queue", response_model=MergeQueueView, responses=REFUSALS)
async def show_queue(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    repo: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=MAX_LIST),
) -> MergeQueueView:
    """The merge requests of the project's queue (``repo``'s alone when given), newest first, each with its ledger."""
    q, e = tables.merge_queue, tables.merge_events
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the merge queue of project {access.name} needs a grant on it")
        query = select(q).where(q.c.project_id == access.project_id)
        if repo is not None:
            query = query.where(q.c.repo == repo)
        rows = (await conn.execute(query.order_by(q.c.id.desc()).limit(limit))).all()
        events: dict[int, list] = {row.id: [] for row in rows}
        if rows:
            ranked = select(e, func.row_number().over(partition_by=e.c.queue_id, order_by=e.c.id.desc()).label("n"))
            ranked = ranked.where(e.c.queue_id.in_(list(events))).subquery()
            found = select(ranked).where(ranked.c.n <= MAX_EVENTS).order_by(ranked.c.queue_id, ranked.c.id.desc())
            for event in await conn.execute(found):
                events[event.queue_id].append(event)
    keys = set(QueuedMergeRequest.model_fields) - {"events"}
    return MergeQueueView(
        project=access.name,
        requests=[
            QueuedMergeRequest(
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
                    for event in events[row.id]
                ],
            )
            for row in rows
        ],
    )

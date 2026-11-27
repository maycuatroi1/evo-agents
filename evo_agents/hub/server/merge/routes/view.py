"""The routes of the hub's merge queue on GitLab (plan gitlab-merge-queue): GET /v1/projects/{p}/merge-queue lists, to a
member with a grant on the project, the merge requests of its queue (``repo``'s alone when given), newest first, each
with where it comes from, its state, why it waits or is blocked (the gate first), the risk and the Judges' verdicts,
the results of its checks after its merge, and its ledger (merge_events), the newest MAX_EVENTS lines first; the audit
samples, those waiting for an answer first; and the breaker of each repo (``evo_agents.hub.server.merge.service.breaker``).
GET .../merge-queue/figures counts, for each repo, agent and model,
the merges of the last ``days``, the reverts, the rollbacks, the Judge's passes found wrong and the time in the queue
(``post_merge.figures``).

An admin of the project answers what the hub asks after a merge (``evo_agents.hub.server.merge.service.post_merge``): POST
.../merge-queue/samples/{id} answers an audit sample ``ok`` or ``faulty`` with a note, POST .../merge-queue/{id}/release
approves the PROD release of a merge when the policy releases its repo by hand, and POST .../merge-queue/{id}/revert
asks the hub to revert a merge it followed, one of the suspects it could not tell apart. POST .../merge-queue/repos/
{repo}/resume resets the breaker of a repo that tripped: its paused merge requests wait for their turn again. POST
.../merge-queue/{id}/pass passes, with a reason, the hold of a merge request at a gate that holds it for a person
(gate 3, score hacking; ``evo_agents.hub.server.merge.service.passes``): the queue goes on from that gate at the same head.
``evo_agents.hub.server.merge.service.gate`` moves the merge requests; no value of a secret is ever in a row. The
shapes are ``models.view``'s, what the routes read and answer ``service.view``'s.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.merge.models.view import (
    BreakerReset,
    HoldPass,
    MergeFigures,
    MergeQueueView,
    MergeSample,
    QueuedMergeRequest,
    SampleAnswer,
)
from evo_agents.hub.server.merge.service import view
from evo_agents.hub.server.security import CurrentUser

MAX_LIST = 200
MAX_DAYS = 365
MAX_ID = 2**63 - 1

router = APIRouter(prefix="/v1/projects", tags=["merge queue"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
ANSWER_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}
RowId = Annotated[int, Path(ge=1, le=MAX_ID)]
RepoName = Annotated[str, Path(min_length=1, max_length=200)]


@router.get("/{project}/merge-queue", response_model=MergeQueueView, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def show_queue(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    repo: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=MAX_LIST),
) -> MergeQueueView:
    """The merge requests of the project's queue (``repo``'s alone when given), newest first, each with its ledger, and
    its audit samples."""
    return await view.queue_view(request.app.state.engine, user, project, repo, limit)


@router.get("/{project}/merge-queue/figures", response_model=MergeFigures, responses=REFUSALS)  # pyright: ignore[reportArgumentType]
async def queue_figures(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    repo: str | None = Query(None, max_length=200),
    days: int = Query(30, ge=1, le=MAX_DAYS),
) -> MergeFigures:
    """For each repo, agent and model, the merges of the last ``days``: how many, reverted, rolled back, passed by
    the Judge and found wrong, and the time in the queue and to the checks."""
    return await view.figures(request.app.state.engine, user, project, repo, days)


@router.post("/{project}/merge-queue/samples/{sample_id}", response_model=MergeSample, responses=ANSWER_REFUSALS)  # pyright: ignore[reportArgumentType]
async def answer_sample(
    request: Request, project: ProjectName, sample_id: RowId, body: SampleAnswer, user: CurrentUser
) -> MergeSample:
    """An admin of the project answers an audit sample: ``ok``, or ``faulty``, which counts against the Judge."""
    return await view.answer_sample(request.app.state.engine, user, project, sample_id, body.result, body.note)


@router.post("/{project}/merge-queue/{queue_id}/release", response_model=QueuedMergeRequest, responses=ANSWER_REFUSALS)  # pyright: ignore[reportArgumentType]
async def approve_release(
    request: Request, project: ProjectName, queue_id: RowId, user: CurrentUser
) -> QueuedMergeRequest:
    """An admin approves the PROD release of a merge whose repo the policy releases by hand (``prod_release``
    manual): the hub's next pass runs its deploy job, carrying the merges before it."""
    return await view.approve_release(request.app.state.engine, user, project, queue_id)


@router.post("/{project}/merge-queue/{queue_id}/revert", response_model=QueuedMergeRequest, responses=ANSWER_REFUSALS)  # pyright: ignore[reportArgumentType]
async def ask_revert(request: Request, project: ProjectName, queue_id: RowId, user: CurrentUser) -> QueuedMergeRequest:
    """An admin asks the hub to revert a merge it followed after its merge, such as one of the suspects of a failed
    check it could not tell apart: the hub's next pass opens its revert, first in the queue."""
    return await view.ask_revert(request.app.state.engine, user, project, queue_id)


@router.post("/{project}/merge-queue/{queue_id}/pass", response_model=QueuedMergeRequest, responses=ANSWER_REFUSALS)  # pyright: ignore[reportArgumentType]
async def pass_hold(
    request: Request, project: ProjectName, queue_id: RowId, body: HoldPass, user: CurrentUser
) -> QueuedMergeRequest:
    """An admin of the project passes the hold of a merge request at a gate that holds it for a person (gate 3, score
    hacking), with a reason: it is gated again at the same head, and the queue goes on from that gate; the later gates,
    the Judges, its turn and its pipeline still apply, and a new push takes the pass back."""
    return await view.pass_hold(request.app.state.engine, user, project, queue_id, body.reason)


@router.post("/{project}/merge-queue/repos/{repo}/resume", response_model=BreakerReset, responses=ANSWER_REFUSALS)  # pyright: ignore[reportArgumentType]
async def resume_repo(request: Request, project: ProjectName, repo: RepoName, user: CurrentUser) -> BreakerReset:
    """An admin of the project resets the breaker of a repo that tripped: its paused merge requests wait for their turn
    again, at their place, and what trips it counts from now."""
    return await view.resume_repo(request.app.state.engine, user, project, repo)

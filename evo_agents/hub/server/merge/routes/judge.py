"""The worker routes of a judge run of the merge queue or of a merge check (``service.judge``): what it reads and
its verdict, both with the run's own key."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.changes.models.views import JudgeKey
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.merge.models.judge import MergeJudgeInputs, MergeVerdict, MergeVerdictIn
from evo_agents.hub.server.merge.service import judge
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}


@worker_router.get("/runs/{run_id}/merge-judge", response_model=MergeJudgeInputs, responses=REFUSALS)
async def merge_judge_inputs(
    request: Request, run_id: RunId, user: CurrentUser, response: Response, judge_key: JudgeKey = None
) -> MergeJudgeInputs:
    """What the judge run of the queue this worker holds reads, with the run's own key."""
    found = await judge.inputs(request.app.state.engine, user, run_id, judge_key)
    response.headers.update(NO_STORE)
    return found


@worker_router.post("/runs/{run_id}/merge-verdict", response_model=MergeVerdict, responses=REFUSALS)
async def merge_verdict(
    request: Request, run_id: RunId, body: MergeVerdictIn, user: CurrentUser, judge_key: JudgeKey = None
) -> MergeVerdict:
    """The verdict of the judge run of the queue this worker holds, with the run's own key: the gates it closes."""
    return await judge.verdict(request.app.state.engine, user, run_id, body, judge_key)

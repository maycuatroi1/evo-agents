"""The answer jobs of a project (plan kg-zip-to-answers, criterion a6): a file of questions asked of its graph by Claude
Code agents in batches. ``evo_agents.hub.kg_answers`` holds the rules without the database.

- POST /v1/kg/{project}/answers (``evo-agents hub kg answer --project P --questions FILE``, a writer): keep the file as
  a job, or find the job of the same file (``kg_answers.digest``) and ask again its questions without an agent's reply.
  The agent of a run cannot, as it would hand agents work, unless it runs as its owner (a run's token, ``evr_...``) and
  the owner opened the run reach ``answers`` (``run_access_rules``): the job is then the owner's, as a file they sent.
- GET /v1/kg/{project}/answers (``--status``): the project's jobs, newest first, with their counts. GET .../{job_id}:
  one job. GET .../{job_id}/lines: its answers in the file's order, as answers.jsonl holds them: the model of the run
  that asked each and its share of the run's tokens. A grant on the project reads them.
- POST .../{job_id}/replies (``hub kg answer --job J --reply FILE``): the agent of the run asking a batch sends its
  answers, or an error for a question, while the run is active; only for the questions of its batch.
- POST .../{job_id}/stop (``hub kg answer --job J --stop``, a writer): stop a running job: its run is cancelled, its
  questions without an answer get an error, and it ends failed; the same file sent again asks them again.
- ``advance``, called each minute by ``curator.collect``: for each running job, the questions of a run that ended
  without a reply get an error; the next batch is queued as a review run with no repo (``kg_init.queue_repoless``),
  dispatched by the sender of the file, on the project's KG pool, under the review cost cap. One review run of a
  project is active at a time, so a job waits for the others; a job no run could be queued for in
  ``kg_answers.QUEUE_WAIT`` fails, its questions not asked with an error. A job with no question left is done.
- ``prompt_of``: the prompt of the run of a batch (``collect.review_prompt``).

Logs and audit rows name the project, the job and the runs, and count; never a question or an answer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.answers import (
    MAX_LINES,
    MAX_LISTED,
    REFUSALS,
    KgAnswerAsk,
    KgAnswerJob,
    KgAnswerJobs,
    KgAnswerLines,
    KgAnswerReplied,
    KgAnswerReply,
)
from evo_agents.hub.server.kg.repository.answers import answer_runs as answer_runs
from evo_agents.hub.server.kg.service import answers as service
from evo_agents.hub.server.kg.service.answers import prompt_of as prompt_of
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

JobId = Annotated[int, PathParam(ge=1, description="the answer job, as `hub kg answer --status` lists it")]


# Reading


@router.get("/{project}/answers", response_model=KgAnswerJobs, responses=REFUSALS)
async def list_jobs(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 20,
) -> KgAnswerJobs:
    """The answer jobs of the project, newest first, with their counts."""
    return await service.list_jobs(request.app.state.engine, user, project, limit)


@router.get("/{project}/answers/{job_id}", response_model=KgAnswerJob, responses=REFUSALS)
async def get_job(request: Request, project: ProjectName, job_id: JobId, user: CurrentUser) -> KgAnswerJob:
    """One answer job with its counts."""
    return await service.get_job(request.app.state.engine, user, project, job_id)


@router.get("/{project}/answers/{job_id}/lines", response_model=KgAnswerLines, responses=REFUSALS)
async def job_lines(
    request: Request,
    project: ProjectName,
    job_id: JobId,
    user: CurrentUser,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LINES)] = MAX_LINES,
) -> KgAnswerLines:
    """The job's answers in the file's order, as answers.jsonl holds them: a question not answered has an error, or
    neither while it waits for a run."""
    return await service.job_lines(request.app.state.engine, user, project, job_id, offset, limit)


# Asking


@router.post("/{project}/answers", response_model=KgAnswerJob, status_code=201, responses=REFUSALS)
async def ask(request: Request, project: ProjectName, body: KgAnswerAsk, user: CurrentUser) -> KgAnswerJob:
    """Ask the questions of a file of the project's graph: a new job, or the job of the same file sent before, which
    asks again only its questions without an agent's reply. Agents answer them in batches as the hub moves the job."""
    return await service.ask(request.app.state.engine, user, project, body)


# Replies


@router.post("/{project}/answers/{job_id}/replies", response_model=KgAnswerReplied, responses=REFUSALS)
async def reply(
    request: Request, project: ProjectName, job_id: JobId, body: KgAnswerReply, user: CurrentUser
) -> KgAnswerReplied:
    """The agent of the run asking a batch sends answers, or errors, for questions of its batch; an id sent again
    replaces its answer. The lines that are not kept come back with why, by position."""
    return await service.reply(request.app.state.engine, user, project, job_id, body)


# The job that moves them


@router.post("/{project}/answers/{job_id}/stop", response_model=KgAnswerJob, responses=REFUSALS)
async def stop(request: Request, project: ProjectName, job_id: JobId, user: CurrentUser) -> KgAnswerJob:
    """Stop a running job: its run is cancelled (a queued one at once, a held one by its worker), its questions without
    an answer get the error that it was stopped, and it ends failed. The same file sent again asks them again."""
    return await service.stop(request.app.state.engine, user, project, job_id)


async def advance(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """One pass over the running answer jobs, each in a transaction of its own: how many ended in each outcome
    (asking, queued, waiting, done, failed)."""
    from evo_agents.hub.server.kg_init import asker_of, queue_repoless

    return await service.advance(engine, now=now, asker_of=asker_of, queue_repoless=queue_repoless)

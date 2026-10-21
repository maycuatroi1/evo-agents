"""The knowledge graph routes of the worker on duty for a project's night shift (feature kg_sync of its daemon,
``evo_agents.worker.kg_sync``): the push of ``evo_agents.hub.server.kg`` and the uploads of
``evo_agents.hub.server.blobs`` under /v1/worker/kg/{project}, with the worker's own token, and what its sync reads of
the runs the hub holds before it reads a source itself.

Every route takes only a worker on duty for the project (``kg_duties.duty_refusal``): one that serves the project and
whose night shift names it. Any other worker gets 403, and a machine token or a web session the 403 of the guard of
/v1/worker/*. The routes belong to feature kg, as those of /v1/kg/{project} do (``evo_agents.hub.features``). The
worker acts as its owner, so the owner's grant decides the rest as for a member's push: the writer role, a usable hub
sink and every label within what the hub sink clears. A worker never sends the knowledge config: its runs go into the
config the members sent (PUT /v1/kg/{project}/config), and a project with none answers 422 as it does to anyone.

- GET /v1/worker/kg/{p}/runs, ``?source=`` for one source's: the runs the hub holds, ingested and pending, as GET
  /v1/kg/{p}/runs answers them.
- GET /v1/worker/kg/{p}/runs/{run_id}/log: an ingested run's source, its log's SHA-256 and size, and a presigned GET
  URL of the log, valid for an hour. The worker merges the runs other machines pushed of a source before it reads the
  source, so the deletions its own run derives cover every item the hub holds (``evo_agents.kg.sync.merge_pulled``).
- POST /v1/worker/kg/{p}/runs, POST .../runs/{run_id}/commit, POST .../blobs/check: as under /v1/kg/{p}.
- POST /v1/worker/kg/{p}/blobs/uploads and POST .../blobs/commit: as POST /v1/blobs/uploads and /v1/blobs/commit for
  project p, of blobs of kind kg-blob and kg-log alone; a body that names another project is 422.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, select

from evo_agents.hub import tables
from evo_agents.hub.server import blobs, kg
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.kg_duties import duty_refusal
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

WORKER_KINDS = frozenset({"kg-blob", "kg-log"})  # the blobs a worker uploads: the logs of its runs and their items
SOURCE = Annotated[str | None, Query(min_length=1, max_length=200, description="only the runs of this source")]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

worker_router = APIRouter(prefix="/v1/worker/kg", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class RunLog(BaseModel):
    run_id: str
    source: str
    log_sha256: str
    log_size: int
    url: str = Field(description="presigned GET of the log, as the corpus that ran it wrote it (jsonl.gz)")


async def on_duty(request: Request, user: Principal, project: str) -> None:
    """403 unless the worker whose token made the request is on duty for ``project``."""
    async with request.app.state.engine.begin() as conn:
        worker = await worker_of(conn, user)
        refusal = await duty_refusal(conn, worker.id, worker.name, project)
    if refusal is not None:
        log.warning("worker kg request refused", extra={"project": project, "worker_id": worker.id})
        raise HTTPException(403, refusal)


def _same_project(project: str, named: str | None) -> None:
    if named != project:
        raise HTTPException(422, f"the body names project {named}, not {project}, whose routes these are")


@worker_router.get("/{project}/runs", response_model=kg.Runs, responses=REFUSALS)
async def list_runs(request: Request, project: ProjectName, user: CurrentUser, source: SOURCE = None) -> kg.Runs:
    """The runs the hub holds of the project, or of one of its sources."""
    await on_duty(request, user, project)
    ingests, pending = tables.kg_ingests, tables.kg_pending_runs
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)

        def ids(runs):
            run_id = cast(runs.c.run_id, Text).label("run_id")
            query = select(run_id).where(runs.c.project_id == access.project_id)
            return query.where(runs.c.source == source) if source is not None else query

        ingested = (await conn.execute(ids(ingests).order_by("run_id"))).scalars().all()
        held = (await conn.execute(ids(pending).order_by("run_id"))).scalars().all()
    return kg.Runs(ingested=list(ingested), pending=list(held))


@worker_router.get("/{project}/runs/{run_id}/log", response_model=RunLog, responses=REFUSALS)
async def run_log(request: Request, project: ProjectName, run_id: kg.RunId, user: CurrentUser) -> RunLog:
    """Where to fetch the log of an ingested run of the project."""
    await on_duty(request, user, project)
    store = blobs.blob_store(request)
    ingests = tables.kg_ingests
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = (
            await conn.execute(
                select(ingests.c.source, ingests.c.log_sha256, ingests.c.log_size).where(
                    ingests.c.project_id == access.project_id, ingests.c.run_id == run_id
                )
            )
        ).one_or_none()
    if row is None:
        raise HTTPException(404, f"project {project} has no ingested run {run_id}")
    url = await asyncio.to_thread(store.presign_get, row.log_sha256)
    return RunLog(run_id=str(run_id), source=row.source, log_sha256=row.log_sha256, log_size=row.log_size, url=url)


@worker_router.post("/{project}/runs", response_model=kg.RunState, responses=REFUSALS)
async def push_run(request: Request, project: ProjectName, body: kg.RunPush, user: CurrentUser):
    """Check the log of a run of the worker's and keep it, as POST /v1/kg/{project}/runs does."""
    await on_duty(request, user, project)
    return await kg.push_run(request, project, body, user)


@worker_router.post("/{project}/runs/{run_id}/commit", response_model=kg.RunCommitted, responses=REFUSALS)
async def commit_run(request: Request, project: ProjectName, run_id: kg.RunId, user: CurrentUser):
    """Make a run of the worker's part of the project's corpus and queue a build, as for a member's run."""
    await on_duty(request, user, project)
    return await kg.commit_run(request, project, run_id, user)


@worker_router.post("/{project}/blobs/check", response_model=kg.MissingBlobs, responses=REFUSALS)
async def check_blobs(request: Request, project: ProjectName, body: kg.BlobCheck, user: CurrentUser):
    """The hashes of ``sha256`` the project does not hold."""
    await on_duty(request, user, project)
    return await kg.check_blobs(request, project, body, user)


@worker_router.post("/{project}/blobs/uploads", response_model=blobs.Uploads, responses=REFUSALS | {413: {}})
async def request_uploads(request: Request, project: ProjectName, body: blobs.UploadRequest, user: CurrentUser):
    """Presigned PUT URLs for the blobs of a run of the worker's that the project does not hold yet."""
    _same_project(project, body.project)
    others = sorted({item.kind for item in body.items} - WORKER_KINDS)
    if others:
        raise HTTPException(422, f"a worker uploads blobs of kind kg-blob and kg-log only, not {', '.join(others)}")
    await on_duty(request, user, project)
    return await blobs.issue_uploads(request, user, project, body.items)


@worker_router.post("/{project}/blobs/commit", response_model=blobs.Committed, responses=REFUSALS)
async def commit_blobs(request: Request, project: ProjectName, body: blobs.CommitRequest, user: CurrentUser):
    """Check the uploads of the items of a run of the worker's and record the blobs in the project."""
    _same_project(project, body.project)
    await on_duty(request, user, project)
    ids = [str(upload_id) for upload_id in body.upload_ids]
    try:
        return await blobs.commit_uploads(request, user, project, ids, kinds=frozenset({"kg-blob"}))
    except blobs.Mismatch as exc:
        message = (
            f"{len(exc.problems)} upload(s) do not match what was declared, such as {exc.problems[0]['upload_id']}: "
            f"{exc.problems[0]['problem']}. Nothing was committed and every upload of the request was discarded; "
            "ask for new uploads"
        )
        return error_response(request, 422, message, detail=exc.problems)

"""Knowledge graphs on the hub: machines push the logs of their connector runs, the worker merges and builds, and the
api answers the kg_* tools from the latest build.

Pushing (the writer role, and a hub sink in the project's knowledge.yaml; ``evo-agents hub kg push`` does it all):

- PUT /v1/kg/{project}/config (``kg_config``) sends the knowledge config the build reads (``evo_agents.hub.kg_ingest``).
  It must keep the registered ladder, and every source the hub holds runs of, pending ones included, must stay
  cleared by the hub sink.
- GET /v1/kg/{project}/runs lists the runs the hub has (``ingested``) and those still waiting for blobs.
- A run is pushed in three steps. (1) The client uploads its log, ``<run_id>.jsonl.gz`` as the corpus wrote it,
  through the blob uploads (kind kg-log) without committing it. (2) POST /v1/kg/{project}/runs {run_id,
  log_upload_id} seals the upload, reads the sealed copy whole and labels every item as the build will; when an item
  carries a label the hub sink does not clear, the answer is 422 naming the items, the upload and its objects are
  deleted, and nothing is written. Otherwise the log is committed as a blob of the project, the run waits in
  kg_pending_runs, and the answer lists the blobs the log refers to that the project does not hold. A run whose log
  the project holds already (an earlier attempt) is named by ``log_sha256`` instead of an upload. (3) Once the
  client has uploaded and committed those blobs, POST /v1/kg/{project}/runs/{run_id}/commit checks that the project
  holds every one, then in one transaction writes kg_ingests, an audit row and a queued build. Every step is
  idempotent by run id: a run the hub has answers ``ingested`` and writes nothing.
- POST /v1/kg/{project}/blobs/check returns the hashes of a list that the project does not hold.
- GET /v1/kg/{project}/sources/{source}/state (writer) is what the hub holds of a source, which ``kg sync`` reads
  instead of a corpus: its newest runs, cursor, lineage and epoch, and live items (``evo_agents.hub.kg_source_state``).
  Step (2) keeps what the log says of its source (``kg_ingest.RunFacts``) with the run.
- DELETE /v1/kg/{project}/sources/{source} (``evo-agents hub kg forget-source``, ``kg_config``) takes every run of a
  source off the hub. A source the config no longer declares takes the highest label, so PUT config refuses to drop
  a source the hub holds runs of and names this command. In one transaction holding the blob lock exclusively
  (``evo_agents.hub.blob_gc``) it deletes the source's kg_pending_runs and kg_ingests rows and the project's
  ``blobs`` rows of their logs, records the logs nothing else refers to for deletion (the next prune deletes the
  objects), writes an audit row ``kg.forget`` and queues a build. The blobs the runs refer to stay the project's, as
  those of superseded runs do. A source the config still declares comes back with the next push of a machine that
  holds its runs. The uploads of an uploaded archive's source (``evo_agents.hub.kg_upload_sources``) go too, with
  their archives and items.

Building: POST /v1/kg/{project}/builds (writer) queues a build; GET /v1/kg/{project}/builds lists the builds, newest
first, and the jobs of the project still in the queue; GET /v1/kg/{project}/builds/{id} is one build. A build keeps
its graph in the hub's tables (``graph_kept``) and uploads no file. An older build may still name the graph file it
uploaded (``artifact_sha256``), or the build whose file it reused (``artifact_reused_from``), until the retention
deletes it (``artifact_pruned_at``, ``evo_agents.hub.kg_prune``). A build job
holds the lock and the queueing lock ``kg:<project>``, so one runs at a time and at most one waits; queueing while one
waits returns the waiting build. The waiting job's row is locked until the queueing transaction commits, and the
worker takes jobs with SKIP LOCKED, so a build that is already queued always sees the run that asked for it. A build
of a project whose graph holds its group chats (``kg_sources.reads_chats``) shows no ``nodes`` and ``edges``: its
totals count messages most members may not read, and kg_status and the web's graph pages count what each reader sees.

Reading: POST /v1/kg/{project}/tools/{tool} {arguments, sink} answers one of the seven kg_* tools of ``kg serve`` for a
member, from the latest successful build whose graph the hub's tables hold (``evo_agents.hub.kg_graph``), with the hub's read rule:
the meet of the member's grant and the sink's clearance, holding the audience the database gives the member at this
call (``evo_agents.hub.server.kg_audience``). The sink defaults to Claude Code's; ``cli``, when the project
declares no such sink, reads through the hub sink, as the local ``cli`` sink reads everything on the machine. The answer
is the MCP tool result, an error included; a graph that cannot be fetched is 503, and one whose bytes are not those the
build recorded is 502 and is never opened. The kg_* tools of /mcp (``evo_agents.hub.server.mcp``) answer through the
same ``tool_result``, so the REST route and MCP read and filter alike; for the agent of a run, the audience a result
reveals is noted as what the run was shown before the result goes back (``run_audience``). ``unknown_refs`` reads the
same graph with the same rule for a plugin's tool that only needs to know which node ids and paths it lacks
(``plugin_calls.CoreContext.graph_refs``).

The package has four layers, which lint-imports keeps in this order (contract hub-kg-layers in pyproject.toml): the
routes here pass the request's parts and the app's engine to ``service``, which opens the transaction, checks and
decides, and reads and writes through ``repository``; ``models`` holds the request and response models and the
limits. The names other modules import from here (``built_graphs``, ``tool_result``, ``member``, ...) live there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, Upload, blob_key, sealed_key
from evo_agents.hub.kg_graph import TOOL_NAMES
from evo_agents.hub.kg_ingest import LogProblem, RunLog, read_run_log, refusal
from evo_agents.hub.kg_source_state import SourceState
from evo_agents.hub.server import kg_run_access
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import Mismatch, blob_store, commit_uploads
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.kg.models.graph import BUILDS_LIMIT as BUILDS_LIMIT
from evo_agents.hub.server.kg.models.graph import CLI_SINK as CLI_SINK
from evo_agents.hub.server.kg.models.graph import GRAPHS_TRIED as GRAPHS_TRIED
from evo_agents.hub.server.kg.models.graph import LOG_KIND as LOG_KIND
from evo_agents.hub.server.kg.models.graph import MAX_CHECK as MAX_CHECK
from evo_agents.hub.server.kg.models.graph import MISSING_SHOWN as MISSING_SHOWN
from evo_agents.hub.server.kg.models.graph import BlobCheck as BlobCheck
from evo_agents.hub.server.kg.models.graph import Build as Build
from evo_agents.hub.server.kg.models.graph import Builds as Builds
from evo_agents.hub.server.kg.models.graph import Job as Job
from evo_agents.hub.server.kg.models.graph import MissingBlobs as MissingBlobs
from evo_agents.hub.server.kg.models.graph import Queued as Queued
from evo_agents.hub.server.kg.models.graph import RunCommitted as RunCommitted
from evo_agents.hub.server.kg.models.graph import RunPush as RunPush
from evo_agents.hub.server.kg.models.graph import Runs as Runs
from evo_agents.hub.server.kg.models.graph import RunState as RunState
from evo_agents.hub.server.kg.models.graph import Sha256 as Sha256
from evo_agents.hub.server.kg.models.graph import Sink as Sink
from evo_agents.hub.server.kg.models.graph import ToolCall as ToolCall
from evo_agents.hub.server.kg.models.graph import ToolResult as ToolResult
from evo_agents.hub.server.kg.repository.graph import built_graphs as built_graphs
from evo_agents.hub.server.kg.repository.graph import find_builds as find_builds
from evo_agents.hub.server.kg.repository.graph import graph_kept as graph_kept
from evo_agents.hub.server.kg.service import graph as service
from evo_agents.hub.server.kg.service.graph import hub_ceiling as hub_ceiling
from evo_agents.hub.server.kg.service.graph import member as member
from evo_agents.hub.server.kg.service.graph import writer as writer
from evo_agents.hub.server.kg.service.tools import delivery_evidence as delivery_evidence
from evo_agents.hub.server.kg.service.tools import read_sink as read_sink
from evo_agents.hub.server.kg.service.tools import snapshot_view as snapshot_view
from evo_agents.hub.server.kg.service.tools import tool_result as tool_result
from evo_agents.hub.server.kg.service.tools import unknown_refs as unknown_refs
from evo_agents.hub.server.kg.service.tools import visible_nodes as visible_nodes
from evo_agents.hub.server.kg_audience import reader_audience as reader_audience
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.policy import Label, Policy

log = logging.getLogger(__name__)

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

RunId = Annotated[uuid.UUID, PathParam(description="the connector run, as its log file is named")]
SourceId = Annotated[
    str, PathParam(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f/]+$", description="a source's id")
]

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})


# Runs


@router.get("/{project}/runs", response_model=Runs)
async def list_runs(request: Request, project: ProjectName, user: CurrentUser) -> Runs:
    return await service.list_runs(request.app.state.engine, user, project)


Scope = Annotated[str | None, Query(min_length=1, max_length=500, description="only the items whose id starts so")]
WithItems = Annotated[bool, Query(description="false leaves the items out, for a caller that reads only the runs")]


@router.get("/{project}/sources/{source}/state", response_model=SourceState, responses=REFUSALS)
async def read_source_state(
    request: Request,
    project: ProjectName,
    source: SourceId,
    user: CurrentUser,
    scope: Scope = None,
    items: WithItems = True,
) -> SourceState:
    """What the hub holds of one source, which ``kg sync`` reads instead of a corpus (``kg_source_state``): the
    writer role, as for a push."""
    return await service.read_source_state(request.app.state.engine, user, project, source, scope, items=items)


@router.post("/{project}/blobs/check", response_model=MissingBlobs, responses=REFUSALS)
async def check_blobs(request: Request, project: ProjectName, body: BlobCheck, user: CurrentUser) -> MissingBlobs:
    """The hashes of ``sha256`` the project does not hold."""
    return await service.check_blobs(request.app.state.engine, user, project, body.sha256)


def _tmp_dir(request: Request) -> Path:
    path = request.app.state.config.data_dir / "kg" / "tmp"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _read_log(
    store: BlobStore, key: str, sha256: str, size: int, tmp_dir: Path, project: str, run_id: str, policy, cleared
) -> RunLog:
    """Fetch the object at ``key`` (blob ``sha256`` of ``size`` bytes) and read it as the log of ``run_id``. Raises
    LogProblem, BlobStoreUnavailable."""
    fd, name = tempfile.mkstemp(dir=tmp_dir, prefix="run-", suffix=".jsonl.gz")
    os.close(fd)
    path = Path(name)
    try:
        with open(path, "wb") as handle:
            found = store.fetch(key, handle, size)
        if found is None:
            raise LogProblem("the log is not in the blob store")
        if found != (sha256, size):
            raise LogProblem("the log in the blob store does not have its recorded SHA-256")
        return read_run_log(path, project, run_id, policy, cleared)
    finally:
        path.unlink(missing_ok=True)


async def _discard_upload(request: Request, user, project_id: int, upload_id: uuid.UUID | None) -> str | None:
    """Delete an upload the caller asked for and no longer needs, row and objects; its declared SHA-256."""
    if upload_id is None:
        return None
    declared = await service.drop_upload(request.app.state.engine, user, project_id, upload_id)
    if declared is not None:
        try:
            await asyncio.to_thread(blob_store(request).discard, [str(upload_id)])
        except Exception:  # the hourly cleanup removes what is left
            log.warning("uploaded objects not deleted; the hourly cleanup removes them", extra={"uploads": 1})
    return declared


async def _pushed_before(request: Request, user, project_id: int, body: RunPush, ingested, pending) -> RunState:
    """The state of a run the hub holds already, once the upload it no longer needs is deleted. Raises 409 for another
    log than the one held."""
    run_id = str(body.run_id)
    declared = await _discard_upload(request, user, project_id, body.log_upload_id) or body.log_sha256
    held = (ingested or pending)[0]
    if declared is not None and declared != held:
        raise HTTPException(409, f"the hub holds another log for run {run_id}: a run's log never changes")
    if ingested is not None:
        return RunState(run_id=run_id, status="ingested", log_sha256=held, blobs=0, missing=[])
    missing = await service.missing_blobs(request.app.state.engine, project_id, pending[1])
    return RunState(run_id=run_id, status="pending", log_sha256=held, blobs=len(pending[1]), missing=missing)


@router.post("/{project}/runs", response_model=RunState, responses=REFUSALS)
async def push_run(request: Request, project: ProjectName, body: RunPush, user: CurrentUser):
    """Check the log of a run and keep it; the blobs it refers to that the project still lacks."""
    engine = request.app.state.engine
    run_id = str(body.run_id)
    access, ceiling, knowledge, ingested, pending = await service.push_access(engine, user, project, body.run_id)
    if ingested is not None or pending is not None:  # pushed before: the new upload is not needed
        return await _pushed_before(request, user, access.project_id, body, ingested, pending)

    store = blob_store(request)
    policy = Policy(project, knowledge)
    sink = access.rules.hub_sink.sink
    tmp_dir = _tmp_dir(request)
    found: dict = {}

    def cleared(label: Label) -> bool:
        return label.below(ceiling)

    async def check(log_key: str, sha256: str, size: int) -> None:
        try:
            run = await asyncio.to_thread(
                _read_log, store, log_key, sha256, size, tmp_dir, project, run_id, policy, cleared
            )
        except LogProblem as exc:
            raise HTTPException(422, f"run {run_id} cannot be pushed: {exc}; nothing was written") from None
        reason = refusal(run, policy, sink)
        if reason is not None:
            log.warning(
                "kg run refused: labels above the hub sink",
                extra={"project": project, "login": user.login, "items": run.refused_count},
            )
            raise HTTPException(422, reason)
        found.update(run=run, sha256=sha256, size=size)

    if body.log_upload_id is not None:

        async def inspect(uploads: list[Upload]) -> None:
            (upload,) = uploads
            await check(sealed_key(upload.upload_id), upload.sha256, upload.size)

        try:
            await commit_uploads(
                request, user, project, [str(body.log_upload_id)], kinds=frozenset({LOG_KIND}), inspect=inspect
            )
        except Mismatch as exc:
            message = f"the log of run {run_id} was not uploaded as declared: {exc.problems[0]['problem']}"
            return error_response(request, 422, message + "; nothing was written", detail=exc.problems)
    else:
        assert body.log_sha256 is not None  # one of the two, as RunPush checks
        size = await service.log_size(engine, access.project_id, body.log_sha256)
        if size is None:
            raise HTTPException(422, f"project {project} holds no blob {body.log_sha256}: upload the log of the run")
        try:
            await check(blob_key(body.log_sha256), body.log_sha256, size)
        except BlobStoreUnavailable:
            raise HTTPException(503, "the blob store did not answer; nothing was written, try again shortly") from None

    return await service.keep_pending(engine, user, project, access.project_id, body.run_id, found)


# Builds


async def queue(request: Request, conn, project_id: int, project: str, requested_by: int | None):
    """``service.queue`` through the app's jobs, in the caller's transaction ``conn``."""
    return await service.queue(request.app.state.jobs, conn, project_id, project, requested_by)


@router.post("/{project}/runs/{run_id}/commit", response_model=RunCommitted, responses=REFUSALS)
async def commit_run(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser):
    """Make a pushed run part of the project's corpus, once every blob it refers to is there, and queue a build."""
    state = request.app.state
    committed = await service.commit_run(state.engine, state.jobs, user, project, run_id)
    if isinstance(committed, RunCommitted):
        return committed
    message = (
        f"run {run_id} refers to {len(committed)} blob(s) project {project} does not hold, such as {committed[0]}: "
        "upload and commit them, then commit the run again; nothing was written"
    )
    return error_response(request, 422, message, detail=[{"sha256": h} for h in committed[:MISSING_SHOWN]])


@router.get("/{project}/builds", response_model=Builds)
async def list_builds(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=BUILDS_LIMIT)] = 20,
    status: Literal["queued", "running", "succeeded", "failed"] | None = None,
) -> Builds:
    """The project's builds, newest first (only those with ``status`` when given), and its build jobs still in the
    queue."""
    return await service.list_builds(request.app.state.engine, user, project, status=status, limit=limit)


@router.get("/{project}/builds/{build_id}", response_model=Build)
async def show_build(request: Request, project: ProjectName, build_id: int, user: CurrentUser) -> Build:
    return await service.show_build(request.app.state.engine, user, project, build_id)


@router.post("/{project}/builds", status_code=202, response_model=Queued, responses=REFUSALS)
async def request_build(request: Request, project: ProjectName, user: CurrentUser) -> Queued:
    """Queue a build of the project's graph from the runs the hub holds."""
    state = request.app.state
    return await service.request_build(state.engine, state.jobs, user, project)


# The tools


@router.post("/{project}/tools/{tool}", response_model=ToolResult, responses=REFUSALS | {502: {"model": ErrorBody}})
async def call_tool(
    request: Request,
    project: ProjectName,
    tool: Annotated[Literal[TOOL_NAMES], PathParam()],
    body: ToolCall,
    user: CurrentUser,
):
    """One kg_* tool of ``evo-agents kg serve``, answered from the project's latest successful build."""
    sink = kg_run_access.sink(user, body.sink)  # a KG run's token reads as the agents do, whatever it names
    return JSONResponse(await tool_result(request.app.state, user, project, tool, body.arguments, sink))

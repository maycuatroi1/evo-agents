"""Knowledge graphs on the hub: machines push the logs of their connector runs, the worker merges and builds, and the
api answers the kg_* tools from the latest build.

Pushing (the writer role, and a hub sink in the project's knowledge.yaml; ``evo-agents hub kg push`` does it all):

- PUT /v1/kg/{project}/config sends the knowledge config the build reads (``evo_agents.hub.kg_ingest``). It must
  keep the registered ladder, and every source the hub holds runs of, pending ones included, must stay cleared by
  the hub sink.
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

Building: POST /v1/kg/{project}/builds (writer) queues a build; GET /v1/kg/{project}/builds lists the builds, newest
first, and the jobs of the project still in the queue; GET /v1/kg/{project}/builds/{id} is one build. A build whose
content did not change names the build whose artifact it reuses (``artifact_reused_from``), and a build whose artifact
the retention deleted has ``artifact_pruned_at`` and no ``artifact_sha256`` (``evo_agents.hub.kg_prune``). A build job
holds the lock and the queueing lock ``kg:<project>``, so one runs at a time and at most one waits; queueing while one
waits returns the waiting build. The waiting job's row is locked until the queueing transaction commits, and the
worker takes jobs with SKIP LOCKED, so a build that is already queued always sees the run that asked for it.

Reading: POST /v1/kg/{project}/tools/{tool} {arguments, sink} answers one of the seven kg_* tools of ``kg serve`` for a
member, from the latest successful build holding an artifact (``evo_agents.hub.kg_graph``), with the hub's read rule:
the meet of the member's grant and the sink's clearance. The sink defaults to Claude Code's; ``cli``, when the project
declares no such sink, reads through the hub sink, as the local ``cli`` sink reads everything on the machine. The answer
is the MCP tool result, an error included; a graph that cannot be fetched is 503, and one whose bytes are not those the
build recorded is 502 and is never opened. The kg_* tools of /mcp (``evo_agents.hub.server.mcp``) answer through the
same ``tool_result``, so the REST route and MCP read and filter alike.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import UUID4, BaseModel, Field, model_validator

from evo_agents.hub.access import Refused, has_role
from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, Upload, blob_key, sealed_key
from evo_agents.hub.db import legacy
from evo_agents.hub.jobs import kg_lock
from evo_agents.hub.kg_build import QueueBusy, queue_build
from evo_agents.hub.kg_graph import (
    GRAPHS,
    TOOL_NAMES,
    ArtifactMismatch,
    BuiltGraph,
    GraphUnavailable,
    answer,
)
from evo_agents.hub.kg_ingest import (
    LogProblem,
    RunLog,
    config_digest,
    config_problems,
    read_run_log,
    refusal,
)
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import Mismatch, blob_store, commit_uploads
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.kg.policy import Label, Policy

log = logging.getLogger(__name__)

MAX_CHECK = 10_000  # hashes in one blobs/check
MISSING_SHOWN = 100  # missing blobs named in a refused commit
BUILDS_LIMIT = 100
GRAPHS_TRIED = 10  # builds holding an artifact a tool call may fall back on when the newest cannot be fetched
CLI_SINK = "cli"
LOG_KIND = "kg-log"
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="hex SHA-256")]
Sink = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")]
RunId = Annotated[uuid.UUID, PathParam(description="the connector run, as its log file is named")]

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})


class KgConfig(BaseModel):
    knowledge: dict = Field(
        description="policy, identifiers, ontology file name and sources {id, connector, label, code}"
    )
    ontology: dict | None = Field(None, description="the ontology extension knowledge.yaml points at, if any")


class ConfigSaved(BaseModel):
    digest: str
    changed: bool = Field(description="false when the hub held exactly this config")


class Runs(BaseModel):
    ingested: list[str] = Field(description="run ids in the project's corpus")
    pending: list[str] = Field(description="run ids whose log the hub has, waiting for their blobs and the commit")


class BlobCheck(BaseModel):
    sha256: list[Sha256] = Field(min_length=1, max_length=MAX_CHECK)


class MissingBlobs(BaseModel):
    missing: list[str] = Field(description="the hashes of the request the project does not hold")


class RunPush(BaseModel):
    run_id: uuid.UUID
    log_upload_id: UUID4 | None = Field(None, description="the upload of the log, asked for with kind kg-log")
    log_sha256: Sha256 | None = Field(None, description="instead of an upload: a log the project holds already")

    @model_validator(mode="after")
    def one_log(self):
        if (self.log_upload_id is None) == (self.log_sha256 is None):
            raise ValueError("give exactly one of log_upload_id and log_sha256")
        return self


class RunState(BaseModel):
    run_id: str
    status: Literal["pending", "ingested"]
    log_sha256: str | None
    blobs: int = Field(description="blobs the log refers to")
    missing: list[str] = Field(description="of those, the ones the project does not hold yet: upload them, then commit")


class Build(BaseModel):
    id: int
    status: Literal["queued", "running", "succeeded", "failed"]
    job_id: int | None
    requested_by: str | None = Field(description="the member who queued it; null when a pushed run did")
    config_digest: str | None
    runs: int | None
    artifact_sha256: str | None = Field(
        description="the graph file in the blob store; null until the build succeeded, and once it was pruned"
    )
    artifact_size: int | None
    artifact_reused_from: int | None = Field(
        description="the build whose artifact this one points at, uploading nothing, because their content is the same"
    )
    artifact_pruned_at: datetime | None = Field(
        description="when the retention deleted the artifact; the build's content hash and counts still describe it"
    )
    content_hash: str | None
    nodes: int | None
    edges: int | None
    error: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class Job(BaseModel):
    job_id: int
    status: Literal["todo", "doing"]
    build_id: int | None


class Builds(BaseModel):
    builds: list[Build]
    jobs: list[Job] = Field(description="the project's build jobs still in the queue, waiting (todo) or running")


class Queued(BaseModel):
    build: Build | None
    queued: bool = Field(description="false when a build of the project was waiting already, which counts as queued")


class RunCommitted(Queued):
    run_id: str
    status: Literal["ingested"]
    created: bool = Field(description="false when the run was in the corpus already")


class ToolCall(BaseModel):
    arguments: dict = Field(default_factory=dict)
    sink: Sink = AGENT_SINK


class ToolResult(BaseModel):
    content: list[dict]
    structuredContent: dict | None = None
    isError: bool | None = None


# Access


def _member(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the knowledge graph of project {access.name} needs a grant on it")


def _writer(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"{doing} project {access.name} needs the writer role on it")


def _hub_ceiling(access: ProjectAccess) -> Label:
    """The writer role and a usable hub sink, as for any push; the label the hub sink clears."""
    rules = access.rules
    try:
        rules.check_push({"level": rules.levels[0]}, access.role)
    except Refused as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return Label(rules.hub_sink.level, rules.hub_sink.location, "U", frozenset({access.name}))


async def _config(conn, access: ProjectAccess) -> dict:
    row = await (
        await legacy(conn, "SELECT knowledge FROM kg_configs WHERE project_id = %s", (access.project_id,))
    ).fetchone()
    if row is None:
        raise HTTPException(
            422,
            f"project {access.name} has no knowledge config on the hub yet: `evo-agents hub kg push` sends it first",
        )
    return row[0]


# The config


@router.put("/{project}/config", response_model=ConfigSaved, responses=REFUSALS)
async def put_config(request: Request, project: ProjectName, body: KgConfig, user: CurrentUser) -> ConfigSaved:
    """Save the knowledge config the project's graph is built with."""
    config = body.model_dump()
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        ceiling = _hub_ceiling(access)
        access.push_label(None)  # the config is the harness source's: its label must leave the machine too
        problems = config_problems(project, access.rules.levels, access.rules.locations, config)
        if problems:
            raise HTTPException(
                422, f"the knowledge config of project {project} cannot be used: " + "; ".join(problems)
            )
        policy = Policy(project, config["knowledge"])
        cursor = await legacy(
            conn,
            "SELECT source FROM kg_ingests WHERE project_id = %(p)s UNION SELECT source FROM kg_pending_runs "
            "WHERE project_id = %(p)s ORDER BY source",
            {"p": access.project_id},
        )
        uncleared = [row[0] for row in await cursor.fetchall() if not policy.source_label(row[0]).below(ceiling)]
        if uncleared:
            raise HTTPException(
                422,
                f"sources {', '.join(uncleared)} would be labelled above what the hub sink of project {project} "
                "clears, but the hub holds runs of them: lower their labels or raise the sink's clearance",
            )
        digest = config_digest(config)
        held = await (
            await legacy(conn, "SELECT digest FROM kg_configs WHERE project_id = %s FOR UPDATE", (access.project_id,))
        ).fetchone()
        if held is not None and held[0] == digest:
            return ConfigSaved(digest=digest, changed=False)
        await legacy(
            conn,
            "INSERT INTO kg_configs (project_id, digest, knowledge, ontology, updated_by) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (project_id) DO UPDATE SET digest = excluded.digest, knowledge = excluded.knowledge, "
            "ontology = excluded.ontology, updated_by = excluded.updated_by, updated_at = now()",
            (
                access.project_id,
                digest,
                Jsonb(config["knowledge"]),
                None if config["ontology"] is None else Jsonb(config["ontology"]),
                user.user_id,
            ),
        )
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_CONFIG, target=project)
    log.info("kg config saved", extra={"project": project, "login": user.login, "digest": digest})
    return ConfigSaved(digest=digest, changed=True)


# Runs


@router.get("/{project}/runs", response_model=Runs)
async def list_runs(request: Request, project: ProjectName, user: CurrentUser) -> Runs:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _member(access)
        ingested = await (
            await legacy(
                conn, "SELECT run_id::text FROM kg_ingests WHERE project_id = %s ORDER BY run_id", (access.project_id,)
            )
        ).fetchall()
        pending = await (
            await legacy(
                conn,
                "SELECT run_id::text FROM kg_pending_runs WHERE project_id = %s ORDER BY run_id",
                (access.project_id,),
            )
        ).fetchall()
    return Runs(ingested=[row[0] for row in ingested], pending=[row[0] for row in pending])


MISSING = """
SELECT r.sha256 FROM unnest(%s::text[]) AS r (sha256)
 WHERE NOT EXISTS (SELECT 1 FROM blobs b WHERE b.project_id = %s AND b.sha256 = r.sha256)
 ORDER BY 1
"""


async def _missing(conn, project_id: int, hashes) -> list[str]:
    cursor = await legacy(conn, MISSING, (sorted(set(hashes)), project_id))
    return [row[0] for row in await cursor.fetchall()]


@router.post("/{project}/blobs/check", response_model=MissingBlobs, responses=REFUSALS)
async def check_blobs(request: Request, project: ProjectName, body: BlobCheck, user: CurrentUser) -> MissingBlobs:
    """The hashes of ``sha256`` the project does not hold."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _writer(access, "pushing to")
        return MissingBlobs(missing=await _missing(conn, access.project_id, body.sha256))


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
    async with request.app.state.engine.begin() as conn:
        row = await (
            await legacy(
                conn,
                "DELETE FROM blob_uploads WHERE upload_id = %s AND project_id = %s AND created_by = %s "
                "RETURNING sha256",
                (upload_id, project_id, user.user_id),
            )
        ).fetchone()
    if row is not None:
        try:
            await asyncio.to_thread(blob_store(request).discard, [str(upload_id)])
        except Exception:  # the hourly cleanup removes what is left
            log.warning("uploaded objects not deleted; the hourly cleanup removes them", extra={"uploads": 1})
    return row[0] if row else None


PENDING_RUN = "SELECT log_sha256, blobs FROM kg_pending_runs WHERE project_id = %s AND run_id = %s"


@router.post("/{project}/runs", response_model=RunState, responses=REFUSALS)
async def push_run(request: Request, project: ProjectName, body: RunPush, user: CurrentUser):
    """Check the log of a run and keep it; the blobs it refers to that the project still lacks."""
    engine = request.app.state.engine
    run_id = str(body.run_id)
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        ceiling = _hub_ceiling(access)
        knowledge = await _config(conn, access)
        ingested = await (
            await legacy(
                conn,
                "SELECT log_sha256 FROM kg_ingests WHERE project_id = %s AND run_id = %s",
                (access.project_id, run_id),
            )
        ).fetchone()
        pending = await (await legacy(conn, PENDING_RUN, (access.project_id, run_id))).fetchone()
    if ingested is not None or pending is not None:  # pushed before: the new upload is not needed
        declared = await _discard_upload(request, user, access.project_id, body.log_upload_id) or body.log_sha256
        held = (ingested or pending)[0]
        if declared is not None and declared != held:
            raise HTTPException(409, f"the hub holds another log for run {run_id}: a run's log never changes")
        if ingested is not None:
            return RunState(run_id=run_id, status="ingested", log_sha256=held, blobs=0, missing=[])
        async with engine.begin() as conn:
            missing = await _missing(conn, access.project_id, pending[1])
        return RunState(run_id=run_id, status="pending", log_sha256=held, blobs=len(pending[1]), missing=missing)

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
        async with engine.begin() as conn:
            row = await (
                await legacy(
                    conn,
                    "SELECT size FROM blobs WHERE project_id = %s AND sha256 = %s",
                    (access.project_id, body.log_sha256),
                )
            ).fetchone()
        if row is None:
            raise HTTPException(422, f"project {project} holds no blob {body.log_sha256}: upload the log of the run")
        try:
            await check(blob_key(body.log_sha256), body.log_sha256, row[0])
        except BlobStoreUnavailable:
            raise HTTPException(503, "the blob store did not answer; nothing was written, try again shortly") from None

    run: RunLog = found["run"]
    blobs = sorted(run.blobs)
    async with engine.begin() as conn:
        await legacy(
            conn,
            "INSERT INTO kg_pending_runs (project_id, run_id, source, log_sha256, log_size, blobs, pushed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, run_id) DO NOTHING",
            (access.project_id, run_id, run.source, found["sha256"], found["size"], blobs, user.user_id),
        )
        missing = await _missing(conn, access.project_id, blobs)
    log.info(
        "kg run log accepted",
        extra={
            "project": project,
            "login": user.login,
            "items": run.items,
            "blobs": len(blobs),
            "missing": len(missing),
        },
    )
    return RunState(run_id=run_id, status="pending", log_sha256=found["sha256"], blobs=len(blobs), missing=missing)


# Builds


BUILD_COLUMNS = """
b.id, b.status, b.job_id, u.login, b.config_digest, b.runs, b.artifact_sha256, b.artifact_size, b.artifact_reused_from,
b.artifact_pruned_at, b.content_hash, b.nodes, b.edges, b.error, b.queued_at, b.started_at, b.finished_at
"""
BUILD_FIELDS = tuple(Build.model_fields)


async def _builds(
    conn, project_id: int, *, build_id: int | None = None, status: str | None = None, limit: int = BUILDS_LIMIT
) -> list[Build]:
    cursor = await legacy(
        conn,
        f"SELECT {BUILD_COLUMNS} FROM kg_builds b LEFT JOIN users u ON u.id = b.requested_by "
        "WHERE b.project_id = %s AND (%s::bigint IS NULL OR b.id = %s) AND (%s::text IS NULL OR b.status = %s) "
        "ORDER BY b.id DESC LIMIT %s",
        (project_id, build_id, build_id, status, status, limit),
    )
    return [Build(**dict(zip(BUILD_FIELDS, row, strict=True))) for row in await cursor.fetchall()]


async def _queue(request: Request, conn, project_id: int, project: str, requested_by: int | None):
    try:
        return await queue_build(request.app.state.jobs, conn, project_id, project, requested_by)
    except QueueBusy as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/{project}/runs/{run_id}/commit", response_model=RunCommitted, responses=REFUSALS)
async def commit_run(request: Request, project: ProjectName, run_id: RunId, user: CurrentUser):
    """Make a pushed run part of the project's corpus, once every blob it refers to is there, and queue a build."""
    run = str(run_id)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _hub_ceiling(access)
        pending = await (
            await legacy(
                conn,
                "SELECT source, log_sha256, log_size, blobs FROM kg_pending_runs WHERE project_id = %s AND run_id = %s "
                "FOR UPDATE",
                (access.project_id, run),
            )
        ).fetchone()
        if pending is None:
            held = await (
                await legacy(
                    conn, "SELECT 1 FROM kg_ingests WHERE project_id = %s AND run_id = %s", (access.project_id, run)
                )
            ).fetchone()
            if held is not None:
                return RunCommitted(run_id=run, status="ingested", created=False, build=None, queued=False)
            raise HTTPException(
                422, f"run {run} has no checked log on the hub: POST /v1/kg/{project}/runs with its log first"
            )
        source, log_sha256, log_size, blobs = pending
        missing = await _missing(conn, access.project_id, blobs)
        if missing:
            message = (
                f"run {run} refers to {len(missing)} blob(s) project {project} does not hold, such as {missing[0]}: "
                "upload and commit them, then commit the run again; nothing was written"
            )
            return error_response(request, 422, message, detail=[{"sha256": h} for h in missing[:MISSING_SHOWN]])
        await legacy(
            conn,
            "INSERT INTO kg_ingests (project_id, run_id, source, log_sha256, log_size, pushed_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, run_id) DO NOTHING",
            (access.project_id, run, source, log_sha256, log_size, user.user_id),
        )
        await legacy(
            conn, "DELETE FROM kg_pending_runs WHERE project_id = %s AND run_id = %s", (access.project_id, run)
        )
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_INGEST, target=project)
        build_id, queued = await _queue(request, conn, access.project_id, project, None)
        build = (await _builds(conn, access.project_id, build_id=build_id))[0] if build_id is not None else None
    log.info(
        "kg run ingested",
        extra={"project": project, "login": user.login, "blobs": len(blobs), "build_id": build_id, "queued": queued},
    )
    return RunCommitted(run_id=run, status="ingested", created=True, build=build, queued=queued)


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
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _member(access)
        builds = await _builds(conn, access.project_id, status=status, limit=limit)
        cursor = await legacy(
            conn,
            "SELECT j.id, j.status::text, b.id FROM procrastinate_jobs j LEFT JOIN kg_builds b ON b.job_id = j.id "
            "WHERE j.lock = %s AND j.status IN ('todo', 'doing') ORDER BY j.id",
            (kg_lock(project),),
        )
        jobs = [Job(job_id=row[0], status=row[1], build_id=row[2]) for row in await cursor.fetchall()]
    return Builds(builds=builds, jobs=jobs)


@router.get("/{project}/builds/{build_id}", response_model=Build)
async def show_build(request: Request, project: ProjectName, build_id: int, user: CurrentUser) -> Build:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _member(access)
        found = await _builds(conn, access.project_id, build_id=build_id, limit=1)
    if not found:
        raise HTTPException(404, f"project {project} has no build {build_id}")
    return found[0]


@router.post("/{project}/builds", status_code=202, response_model=Queued, responses=REFUSALS)
async def request_build(request: Request, project: ProjectName, user: CurrentUser) -> Queued:
    """Queue a build of the project's graph from the runs the hub holds."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _writer(access, "building")
        build_id, queued = await _queue(request, conn, access.project_id, project, user.user_id)
        if queued:
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_BUILD, target=project
            )
        build = (await _builds(conn, access.project_id, build_id=build_id))[0] if build_id is not None else None
    log.info("kg build requested", extra={"project": project, "login": user.login, "queued": queued})
    return Queued(build=build, queued=queued)


# The tools


def _read_sink(access: ProjectAccess, sink: str) -> str:
    if sink == CLI_SINK and CLI_SINK not in access.rules.policy.sinks and access.rules.hub_sink is not None:
        return access.rules.hub_sink.sink
    return sink


async def tool_result(state, user: Principal, project: str, tool: str, arguments: dict, sink: str) -> dict:
    """One kg_* tool of ``evo-agents kg serve`` for ``user``, answered from the project's latest successful build
    through ``sink``: the MCP tool result, an error included. ``state`` is the app's. Raises HTTPException: 404 for a
    project the caller cannot see, 403 without a grant on it, 503 when no graph can be fetched, 502 for an artifact
    whose bytes are not the build's."""
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _member(access)
        cursor = await legacy(conn, GRAPHS, (access.project_id, GRAPHS_TRIED))
        graphs = [BuiltGraph(*row) for row in await cursor.fetchall()]
    if not graphs:
        message = (
            f"error: project {project} has no graph on the hub yet: push its runs with `evo-agents hub kg push` "
            "(a build follows)"
        )
        return {"content": [{"type": "text", "text": message}], "isError": True}
    rules = access.rules
    ceiling = rules.ceiling(access.max_level, _read_sink(access, sink))
    try:
        return await asyncio.to_thread(
            answer,
            state.kg_graphs,
            state.blobs,
            state.kg_handles,
            (user.user_id, project),
            project,
            rules.policy,
            ceiling,
            sink,
            graphs,
            tool,
            arguments,
        )
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except ArtifactMismatch as exc:
        log.error("kg graph artifact refused", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(502, str(exc)) from None


@router.post("/{project}/tools/{tool}", response_model=ToolResult, responses=REFUSALS | {502: {"model": ErrorBody}})
async def call_tool(
    request: Request,
    project: ProjectName,
    tool: Annotated[Literal[TOOL_NAMES], PathParam()],
    body: ToolCall,
    user: CurrentUser,
):
    """One kg_* tool of ``evo-agents kg serve``, answered from the project's latest successful build."""
    return JSONResponse(await tool_result(request.app.state, user, project, tool, body.arguments, body.sink))

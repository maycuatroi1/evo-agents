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
- DELETE /v1/kg/{project}/sources/{source} (``evo-agents hub kg forget-source``) takes every run of a source off the
  hub. A source the config no longer declares takes the highest label, so PUT config refuses to drop a source the hub
  holds runs of and names this command. In one transaction holding the blob lock exclusively
  (``evo_agents.hub.blob_gc``) it deletes the source's kg_pending_runs and kg_ingests rows and the project's
  ``blobs`` rows of their logs, records the logs nothing else refers to for deletion (the next prune deletes the
  objects), writes an audit row ``kg.forget`` and queues a build. The blobs the runs refer to stay the project's, as
  those of superseded runs do. A source the config still declares comes back with the next push of a machine that
  holds its runs.

Building: POST /v1/kg/{project}/builds (writer) queues a build; GET /v1/kg/{project}/builds lists the builds, newest
first, and the jobs of the project still in the queue; GET /v1/kg/{project}/builds/{id} is one build. A build whose
content did not change names the build whose artifact it reuses (``artifact_reused_from``), and a build whose artifact
the retention deleted has ``artifact_pruned_at`` and no ``artifact_sha256`` (``evo_agents.hub.kg_prune``). A build job
holds the lock and the queueing lock ``kg:<project>``, so one runs at a time and at most one waits; queueing while one
waits returns the waiting build. The waiting job's row is locked until the queueing transaction commits, and the
worker takes jobs with SKIP LOCKED, so a build that is already queued always sees the run that asked for it. A build
of a project whose graph holds its group chats (``kg_sources.reads_chats``) shows no ``nodes`` and ``edges``: its
totals count messages most members may not read, and kg_status and the web's graph pages count what each reader sees.

Reading: POST /v1/kg/{project}/tools/{tool} {arguments, sink} answers one of the seven kg_* tools of ``kg serve`` for a
member, from the latest successful build holding an artifact (``evo_agents.hub.kg_graph``), with the hub's read rule:
the meet of the member's grant and the sink's clearance, holding the audience the database gives the member at this
call (``evo_agents.hub.server.kg_audience``). The sink defaults to Claude Code's; ``cli``, when the project
declares no such sink, reads through the hub sink, as the local ``cli`` sink reads everything on the machine. The answer
is the MCP tool result, an error included; a graph that cannot be fetched is 503, and one whose bytes are not those the
build recorded is 502 and is never opened. The kg_* tools of /mcp (``evo_agents.hub.server.mcp``) answer through the
same ``tool_result``, so the REST route and MCP read and filter alike; for the agent of a run, the audience a result
reveals is noted as what the run was shown before the result goes back (``run_audience``). ``unknown_refs`` reads the
same graph with the same rule for a plugin's tool that only needs to know which node ids and paths it lacks
(``plugin_calls.CoreContext.graph_refs``).
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
from pydantic import UUID4, BaseModel, Field, model_validator
from sqlalchemy import Text, cast, delete, exists, func, literal, null, select, union
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.access import Refused, has_role
from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, Upload, blob_key, sealed_key
from evo_agents.hub.jobs import kg_lock
from evo_agents.hub.kg_build import QueueBusy, procrastinate_jobs, queue_build
from evo_agents.hub.kg_graph import (
    TOOL_NAMES,
    ArtifactMismatch,
    BuiltGraph,
    GraphOutdated,
    GraphUnavailable,
    UnknownRefs,
    answer,
    mentioning,
    unknown,
)
from evo_agents.hub.kg_ingest import (
    LogProblem,
    RunLog,
    config_digest,
    config_problems,
    read_run_log,
    refusal,
)
from evo_agents.hub.kg_sources import reads_chats
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.server import audit, rankers, run_audience
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import Mismatch, blob_store, commit_uploads
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.serve import ToolError

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
SourceId = Annotated[
    str, PathParam(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f/]+$", description="a source's id")
]

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}, 404: {"model": ErrorBody}})


class KgConfig(BaseModel):
    knowledge: dict = Field(
        description="policy, identifiers, ontology file name and sources {id, connector, label, code, repo, ref}"
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


class SourceForgotten(Queued):
    source: str
    ingested: int = Field(description="runs of the source taken out of the project's corpus")
    pending: int = Field(description="runs of the source that waited for their blobs, dropped")
    logs: int = Field(description="their logs nothing else refers to, deleted from the blob store by the next prune")
    declared: bool = Field(
        description="whether the knowledge config on the hub still declares the source: a machine holding its runs "
        "then pushes them again"
    )


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
    return rules.clearance_label(rules.hub_sink)  # the highest audience: an item with one may be pushed


async def _config(conn: AsyncConnection, access: ProjectAccess) -> dict:
    configs = tables.kg_configs
    row = (
        await conn.execute(select(configs.c.knowledge).where(configs.c.project_id == access.project_id))
    ).one_or_none()
    if row is None:
        raise HTTPException(
            422,
            f"project {access.name} has no knowledge config on the hub yet: `evo-agents hub kg push` sends it first",
        )
    return row[0]


# The config


def _held_sources(project_id: int):
    """The sources of the runs the hub holds for the project, ingested or pending, each once, by name."""
    ingests, pending = tables.kg_ingests, tables.kg_pending_runs
    sources = union(
        select(ingests.c.source).where(ingests.c.project_id == project_id),
        select(pending.c.source).where(pending.c.project_id == project_id),
    )
    return sources.order_by(sources.selected_columns.source)


def forget_command(project: str, sources: list[str]) -> str:
    """The command that takes every run of ``sources`` off the hub (``forget_source``)."""
    return f"`evo-agents hub kg forget-source --project {project} " + " ".join(f"--source {s}" for s in sources) + "`"


def _declared(knowledge: dict) -> set:
    return {source.get("id") for source in knowledge.get("sources") or [] if isinstance(source, dict)}


def _uncleared_message(project: str, knowledge: dict, uncleared: list[str]) -> str:
    """Why a config is refused whose ``uncleared`` sources the hub holds runs of: a source the config no longer
    declares takes the highest label, so dropping one needs its runs taken off the hub first."""
    declared = _declared(knowledge)
    dropped = [source for source in uncleared if source not in declared]
    raised = [source for source in uncleared if source in declared]
    parts = []
    if dropped:
        parts.append(
            f"the config drops sources {', '.join(dropped)}, but the hub holds runs of them: take them off the hub "
            f"first with {forget_command(project, dropped)}, then push again"
        )
    if raised:
        parts.append(
            f"sources {', '.join(raised)} would be labelled above what the hub sink of project {project} clears, but "
            "the hub holds runs of them: lower their labels, raise the sink's clearance, or take their runs off the "
            f"hub with {forget_command(project, raised)}"
        )
    return "; ".join(parts)


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
        held_sources = await conn.execute(_held_sources(access.project_id))
        uncleared = [source for source in held_sources.scalars() if not policy.source_label(source).below(ceiling)]
        if uncleared:
            raise HTTPException(422, _uncleared_message(project, config["knowledge"], uncleared))
        digest = config_digest(config)
        configs = tables.kg_configs
        held = (
            await conn.execute(
                select(configs.c.digest).where(configs.c.project_id == access.project_id).with_for_update()
            )
        ).one_or_none()
        if held is not None and held[0] == digest:
            return ConfigSaved(digest=digest, changed=False)
        saved = pg_insert(configs).values(
            project_id=access.project_id,
            digest=digest,
            knowledge=config["knowledge"],
            ontology=null() if config["ontology"] is None else config["ontology"],  # SQL NULL, not JSON null
            updated_by=user.user_id,
        )
        await conn.execute(
            saved.on_conflict_do_update(
                index_elements=[configs.c.project_id],
                set_={
                    "digest": saved.excluded.digest,
                    "knowledge": saved.excluded.knowledge,
                    "ontology": saved.excluded.ontology,
                    "updated_by": saved.excluded.updated_by,
                    "updated_at": func.now(),
                },
            )
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
        ingested = (await conn.execute(_run_ids(tables.kg_ingests, access.project_id))).scalars().all()
        pending = (await conn.execute(_run_ids(tables.kg_pending_runs, access.project_id))).scalars().all()
    return Runs(ingested=list(ingested), pending=list(pending))


def _run_ids(runs, project_id: int):
    """The run ids of the project in ``runs`` (kg_ingests or kg_pending_runs), as text, in order."""
    run_id = cast(runs.c.run_id, Text).label("run_id")
    return select(run_id).where(runs.c.project_id == project_id).order_by(run_id)


async def _missing(conn: AsyncConnection, project_id: int, hashes) -> list[str]:
    """The hashes of ``hashes`` the project holds no blob of, sorted, each once."""
    blobs = tables.blobs
    wanted = func.unnest(literal(sorted(set(hashes)), ARRAY(Text))).table_valued("sha256").render_derived("r")
    held = exists().where(blobs.c.project_id == project_id, blobs.c.sha256 == wanted.c.sha256)
    query = select(wanted.c.sha256).where(~held).order_by(wanted.c.sha256)
    return list((await conn.execute(query)).scalars())


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
    uploads = tables.blob_uploads
    async with request.app.state.engine.begin() as conn:
        row = (
            await conn.execute(
                delete(uploads)
                .where(
                    uploads.c.upload_id == upload_id,
                    uploads.c.project_id == project_id,
                    uploads.c.created_by == user.user_id,
                )
                .returning(uploads.c.sha256)
            )
        ).one_or_none()
    if row is not None:
        try:
            await asyncio.to_thread(blob_store(request).discard, [str(upload_id)])
        except Exception:  # the hourly cleanup removes what is left
            log.warning("uploaded objects not deleted; the hourly cleanup removes them", extra={"uploads": 1})
    return row[0] if row else None


def _held_log(runs, project_id: int, run_id: uuid.UUID, *columns):
    """``columns`` of the run in ``runs`` (kg_ingests or kg_pending_runs)."""
    return select(*columns).where(runs.c.project_id == project_id, runs.c.run_id == run_id)


@router.post("/{project}/runs", response_model=RunState, responses=REFUSALS)
async def push_run(request: Request, project: ProjectName, body: RunPush, user: CurrentUser):
    """Check the log of a run and keep it; the blobs it refers to that the project still lacks."""
    engine = request.app.state.engine
    run_id = str(body.run_id)
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        ceiling = _hub_ceiling(access)
        knowledge = await _config(conn, access)
        ingests, pending_runs = tables.kg_ingests, tables.kg_pending_runs
        ingested = (
            await conn.execute(_held_log(ingests, access.project_id, body.run_id, ingests.c.log_sha256))
        ).one_or_none()
        pending = (
            await conn.execute(
                _held_log(pending_runs, access.project_id, body.run_id, pending_runs.c.log_sha256, pending_runs.c.blobs)
            )
        ).one_or_none()
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
        blobs = tables.blobs
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(blobs.c.size).where(
                        blobs.c.project_id == access.project_id, blobs.c.sha256 == body.log_sha256
                    )
                )
            ).one_or_none()
        if row is None:
            raise HTTPException(422, f"project {project} holds no blob {body.log_sha256}: upload the log of the run")
        try:
            await check(blob_key(body.log_sha256), body.log_sha256, row[0])
        except BlobStoreUnavailable:
            raise HTTPException(503, "the blob store did not answer; nothing was written, try again shortly") from None

    run: RunLog = found["run"]
    blobs = sorted(run.blobs)
    pending_runs = tables.kg_pending_runs
    async with engine.begin() as conn:
        await conn.execute(
            pg_insert(pending_runs)
            .values(
                project_id=access.project_id,
                run_id=body.run_id,
                source=run.source,
                log_sha256=found["sha256"],
                log_size=found["size"],
                blobs=blobs,
                pushed_by=user.user_id,
            )
            .on_conflict_do_nothing(index_elements=[pending_runs.c.project_id, pending_runs.c.run_id])
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


async def _builds(
    conn: AsyncConnection,
    project_id: int,
    *,
    build_id: int | None = None,
    status: str | None = None,
    limit: int = BUILDS_LIMIT,
) -> list[Build]:
    """The project's builds, newest first: only ``build_id`` and only those with ``status`` when given."""
    b, users = tables.kg_builds, tables.users
    query = (
        select(
            b.c.id,
            b.c.status,
            b.c.job_id,
            users.c.login.label("requested_by"),
            b.c.config_digest,
            b.c.runs,
            b.c.artifact_sha256,
            b.c.artifact_size,
            b.c.artifact_reused_from,
            b.c.artifact_pruned_at,
            b.c.content_hash,
            b.c.nodes,
            b.c.edges,
            b.c.error,
            b.c.queued_at,
            b.c.started_at,
            b.c.finished_at,
        )
        .join_from(b, users, users.c.id == b.c.requested_by, isouter=True)
        .where(b.c.project_id == project_id)
    )
    if build_id is not None:
        query = query.where(b.c.id == build_id)
    if status is not None:
        query = query.where(b.c.status == status)
    rows = await conn.execute(query.order_by(b.c.id.desc()).limit(limit))
    builds = [Build(**row._mapping) for row in rows]
    if builds and await reads_chats(conn, project_id):  # totals that count chats would tell who is in none of them
        return [build.model_copy(update={"nodes": None, "edges": None}) for build in builds]
    return builds


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
        ingests, pending_runs = tables.kg_ingests, tables.kg_pending_runs
        p = pending_runs.c
        pending = (
            await conn.execute(
                _held_log(
                    pending_runs, access.project_id, run_id, p.source, p.log_sha256, p.log_size, p.blobs
                ).with_for_update()
            )
        ).one_or_none()
        if pending is None:
            held = (
                await conn.execute(_held_log(ingests, access.project_id, run_id, literal(1)).select_from(ingests))
            ).one_or_none()
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
        await conn.execute(
            pg_insert(ingests)
            .values(
                project_id=access.project_id,
                run_id=run_id,
                source=source,
                log_sha256=log_sha256,
                log_size=log_size,
                pushed_by=user.user_id,
            )
            .on_conflict_do_nothing(index_elements=[ingests.c.project_id, ingests.c.run_id])
        )
        await conn.execute(
            delete(pending_runs).where(pending_runs.c.project_id == access.project_id, pending_runs.c.run_id == run_id)
        )
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.KG_INGEST, target=project)
        build_id, queued = await _queue(request, conn, access.project_id, project, None)
        build = (await _builds(conn, access.project_id, build_id=build_id))[0] if build_id is not None else None
    log.info(
        "kg run ingested",
        extra={"project": project, "login": user.login, "blobs": len(blobs), "build_id": build_id, "queued": queued},
    )
    return RunCommitted(run_id=run, status="ingested", created=True, build=build, queued=queued)


def _queued_jobs(project: str):
    """The build jobs of the project still in the queue, waiting or running, with the build each one runs."""
    j, b = procrastinate_jobs, tables.kg_builds
    return (
        select(j.c.id.label("job_id"), cast(j.c.status, Text).label("status"), b.c.id.label("build_id"))
        .join_from(j, b, b.c.job_id == j.c.id, isouter=True)
        .where(j.c.lock == kg_lock(project), j.c.status.in_(["todo", "doing"]))
        .order_by(j.c.id)
    )


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
        rows = await conn.execute(_queued_jobs(project))
        jobs = [Job(**row._mapping) for row in rows]
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


# Forgetting a source


async def _drop_source(conn: AsyncConnection, project_id: int, source: str) -> tuple[int, int, int]:
    """Delete the project's runs of ``source``, pending ones first (a commit in progress finishes before), then
    ingested ones, and the project's rows of their logs; record the logs nothing else refers to for deletion. In
    ``conn``'s transaction, which holds the blob lock exclusively. (ingested, pending, logs recorded)."""
    ingests, pending_runs, blobs = tables.kg_ingests, tables.kg_pending_runs, tables.blobs
    logs = {}
    counts = []
    for runs in (pending_runs, ingests):
        dropped = await conn.execute(
            delete(runs)
            .where(runs.c.project_id == project_id, runs.c.source == source)
            .returning(runs.c.log_sha256, runs.c.log_size)
        )
        rows = dropped.all()
        logs.update((row.log_sha256, row.log_size) for row in rows)
        counts.append(len(rows))
    if logs:  # a log names its run, so no other run of the project refers to it
        hashes = sorted(logs)
        await conn.execute(
            delete(blobs).where(
                blobs.c.project_id == project_id, blobs.c.kind == LOG_KIND, blob_gc.hashes_in(blobs.c.sha256, hashes)
            )
        )
        recorded = await blob_gc.forget(conn, {sha: (logs[sha], LOG_KIND) for sha in hashes})
    else:
        recorded = []
    return counts[1], counts[0], len(recorded)


@router.delete("/{project}/sources/{source}", response_model=SourceForgotten, responses=REFUSALS)
async def forget_source(request: Request, project: ProjectName, source: SourceId, user: CurrentUser) -> SourceForgotten:
    """Take every run of a source off the hub, ingested or pending, and queue a build without them: how a source the
    hub holds runs of leaves knowledge.yaml (PUT config refuses to drop it before)."""
    async with request.app.state.engine.connect() as conn, conn.begin():
        access = await project_access(conn, user, project)
        _writer(access, "forgetting a source of")
        configs = tables.kg_configs
        knowledge = (
            await conn.execute(select(configs.c.knowledge).where(configs.c.project_id == access.project_id))
        ).scalar_one_or_none()
        declared = source in _declared(knowledge or {})
        await blob_gc.lock_exclusive(conn)
        ingested, pending, logs = await _drop_source(conn, access.project_id, source)
        build, queued = None, False
        if ingested or pending:
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.KG_FORGET,
                target=f"{project} source={source} runs={ingested + pending}",
                project_id=access.project_id,
            )
            build_id, queued = await _queue(request, conn, access.project_id, project, user.user_id)
            build = (await _builds(conn, access.project_id, build_id=build_id))[0] if build_id is not None else None
    log.info(
        "kg source forgotten",
        extra={"project": project, "login": user.login, "runs": ingested + pending, "logs": logs, "queued": queued},
    )
    return SourceForgotten(
        source=source,
        ingested=ingested,
        pending=pending,
        logs=logs,
        declared=declared,
        build=build,
        queued=queued,
    )


# The tools


async def built_graphs(conn: AsyncConnection, project_id: int, limit: int) -> list[BuiltGraph]:
    """The successful builds of a project that still hold an artifact, newest first, at most ``limit``: what the
    tools here and the web's graph pages (``evo_agents.hub.server.kg_web``) read from. Several builds may share one
    artifact (a build with unchanged content reuses it), and the retention takes away the artifact of older ones
    (``evo_agents.hub.kg_prune``). It lives here rather than in ``evo_agents.hub.kg_graph``, which imports no database
    library."""
    builds = tables.kg_builds
    query = (
        select(
            builds.c.id.label("build_id"),
            builds.c.artifact_sha256.label("sha256"),
            builds.c.artifact_size.label("size"),
            builds.c.content_hash,
            builds.c.nodes,
            builds.c.edges,
            builds.c.finished_at,
        )
        .where(
            builds.c.project_id == project_id,
            builds.c.status == "succeeded",
            builds.c.artifact_sha256.is_not(None),
        )
        .order_by(builds.c.id.desc())
        .limit(limit)
    )
    return [BuiltGraph(**row._mapping) for row in await conn.execute(query)]


async def rebuild_outdated(state, project_id: int, project: str) -> None:
    """Queue a build of a project whose newest graph an older store schema wrote (GraphOutdated), unless one waits
    already: the new build writes the schema this hub reads. It needs no new run, so nobody has to push."""
    try:
        async with state.engine.begin() as conn:
            _, queued = await queue_build(state.jobs, conn, project_id, project, None)
    except QueueBusy:
        return
    if queued:
        log.warning("kg graph of an older store schema: a new build is queued", extra={"project": project})


def _read_sink(access: ProjectAccess, sink: str) -> str:
    if sink == CLI_SINK and CLI_SINK not in access.rules.policy.sinks and access.rules.hub_sink is not None:
        return access.rules.hub_sink.sink
    return sink


async def tool_result(
    state,
    user: Principal,
    project: str,
    tool: str,
    arguments: dict,
    sink: str,
    *,
    audience: frozenset | None = None,
    explore: bool = False,
) -> dict:
    """One kg_* tool of ``evo-agents kg serve`` for ``user``, answered from the project's latest successful build
    through ``sink``, holding ``user``'s audience as the database says now, or ``audience`` when given, exploring the
    ranking when ``explore`` (``evo_agents.kg.explore``): the MCP tool result, an error included. ``state`` is the app's. Raises HTTPException: 404 for a project the caller cannot see,
    403 without a grant on it, 503 when no graph can be fetched, 502 for an artifact whose bytes are not the build's."""
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _member(access)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        if graphs and audience is None:
            audience = await reader_audience(conn, user)
    if not graphs:
        message = (
            f"error: project {project} has no graph on the hub yet: push its runs with `evo-agents hub kg push` "
            "(a build follows)"
        )
        return {"content": [{"type": "text", "text": message}], "isError": True}
    rules = access.rules
    ceiling = rules.ceiling(access.max_level, _read_sink(access, sink), audience)
    try:
        result = await asyncio.to_thread(
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
            explore,
        )
    except GraphOutdated as exc:
        await rebuild_outdated(state, access.project_id, project)
        raise HTTPException(503, str(exc)) from None
    except GraphUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except ArtifactMismatch as exc:
        log.error("kg graph artifact refused", extra={"project": project, "build_id": graphs[0].build_id})
        raise HTTPException(502, str(exc)) from None
    if tool == "kg_search" and isinstance(arguments.get("query"), str):
        result = await rankers.rerank_kg(state, access, arguments["query"], result)  # plugins' rankers (their point)
    shown = run_audience.revealed(result) if user.scope is not None else frozenset()
    if shown:  # noted before the result goes back, so a memory the agent writes after reading it carries it
        async with state.engine.begin() as conn:
            await run_audience.note(conn, user.scope.run_id, shown)
    return result


async def unknown_refs(
    state, user: Principal, project: str, sink: str | None, ids: list[str], paths: list[str]
) -> UnknownRefs:
    """Which of node ``ids`` and ``paths`` (``repo:path``, or a path alone) the project's latest successful build
    lacks, as ``user`` reads it through ``sink`` (the project's hub sink when None) with ``tool_result``'s rule, and the
    author emails of the newest commits of the files they name (``kg_graph.HubSession.unknown``). Never raises for the
    graph: when it cannot look them up (no grant, no graph yet, none readable now) every one counts as lacking, and
    ``why`` says so."""
    if not ids and not paths:
        return UnknownRefs((), ())
    every = (tuple(ids), tuple(paths))
    async with state.engine.begin() as conn:
        try:
            access = await project_access(conn, user, project)
            _member(access)
        except HTTPException as exc:
            return UnknownRefs(*every, str(exc.detail))
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    if not graphs:
        return UnknownRefs(*every, f"project {project} has no knowledge graph on the hub yet")
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else CLI_SINK)
    ceiling = access.rules.ceiling(access.max_level, _read_sink(access, through), audience)
    looked = (state.kg_graphs, state.blobs, project, access.rules.policy, ceiling, through, graphs, ids, paths)
    try:
        return await asyncio.to_thread(unknown, *looked)
    except GraphOutdated as exc:
        await rebuild_outdated(state, access.project_id, project)
        return UnknownRefs(*every, str(exc))
    except (GraphUnavailable, ArtifactMismatch) as exc:
        return UnknownRefs(*every, str(exc))


async def delivery_evidence(
    state, user: Principal, project: str, codes: list[str]
) -> tuple[dict[str, list] | None, str | None]:
    """(what the project's latest readable graph shows of the delivery of each use case code of ``codes``, why not):
    ``mentioning`` as ``user`` reads it through the project's hub sink (``evo_agents.hub.kg_graph.HubSession.mentions``),
    for the baseline of a goal. Never raises for the graph: with no grant, no graph yet, none readable now or a graph
    of an older schema, the evidence is None and the reason says so."""
    if not codes:
        return {}, None
    async with state.engine.begin() as conn:
        try:
            access = await project_access(conn, user, project)
            _member(access)
        except HTTPException as exc:
            return None, str(exc.detail)
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        audience = await reader_audience(conn, user) if graphs else frozenset()
    if not graphs:
        return None, f"project {project} has no knowledge graph on the hub yet"
    through = access.rules.hub_sink.sink if access.rules.hub_sink else CLI_SINK
    ceiling = access.rules.ceiling(access.max_level, _read_sink(access, through), audience)
    looked = (state.kg_graphs, state.blobs, project, access.rules.policy, ceiling, through, graphs, codes)
    try:
        return await asyncio.to_thread(mentioning, *looked), None
    except GraphOutdated as exc:
        await rebuild_outdated(state, access.project_id, project)
        return None, str(exc)
    except (GraphUnavailable, ArtifactMismatch, ToolError) as exc:
        return None, str(exc)


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

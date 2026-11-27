"""The shapes and limits of pushing runs, building and reading a project's knowledge graph
(``evo_agents.hub.server.kg``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import UUID4, BaseModel, Field, model_validator

from evo_agents.hub.memory import AGENT_SINK

MAX_CHECK = 10_000  # hashes in one blobs/check
MISSING_SHOWN = 100  # missing blobs named in a refused commit
BUILDS_LIMIT = 100
GRAPHS_TRIED = 10  # kept graphs a tool call may find its snapshot among: the newest, or one it still holds
CLI_SINK = "cli"
LOG_KIND = "kg-log"

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="hex SHA-256")]
Sink = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")]


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
    graph_kept: bool = Field(
        description="its graph is in the hub's tables, or was until a newer build replaced it: the newest such build "
        "is the graph the reads answer from"
    )
    artifact_sha256: str | None = Field(
        description="the graph file an older build put in the blob store; null for a build since the hub keeps the "
        "graph in its tables, and once it was pruned"
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

"""The shape of the flow after an archive's upload (``evo_agents.hub.server.kg_pipeline``) and how far its reads and
its notices look."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MAX_TOLD = 200  # uploads a pass of ``tell`` looks at
MAX_REQUEUED = 20  # builds stopped and queued again one after the other that a read follows
MAX_EARLIER_INITS = 20  # kg_init runs before the newest that a read looks through for the rebuild


class KgPipelinePhase(BaseModel):
    name: Literal["uploading", "extracting", "building", "entities", "questions", "curator"]
    state: Literal["pending", "running", "done", "skipped", "failed"]
    detail: str | None = Field(description="what it does or waits for, what it gave, or why it failed or was skipped")


class KgPipeline(BaseModel):
    upload_id: int
    project: str
    state: Literal["running", "ready", "failed", "replaced"]
    phase: str | None = Field(description="the phase running, or the one that failed; null once ready or replaced")
    phases: list[KgPipelinePhase] = Field(description="in the order they run")
    error: str | None = Field(description="why the failed phase failed")
    build_id: int | None = Field(description="the build the upload queued")
    init_id: int | None = Field(description="the kg_init run of the Curator after that build")
    rebuild_id: int | None = Field(description="the build the Curator's decision queued")
    told_at: datetime | None = Field(description="when the sender and the admins got the notice kg_pipeline")

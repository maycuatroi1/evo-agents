"""The shapes of the default system of a project and of the ops of a system: the bodies of the routes of
``routes.ops``."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import system_spec
from evo_agents.hub.server.errors import ErrorBody

REFUSALS: dict = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
BatchState = Literal["open", "ready", "empty", "running", "done"]
MAX_REASON_CHARS = 200


class OpsSettings(BaseModel):
    enabled: bool = Field(description="the project's operations events go to the queue of its default system")
    debounce_minutes: int = Field(description="a batch closes this long after it opened")
    max_items: int = Field(description="or once it holds this many items")
    plan_stall_minutes: int = Field(
        system_spec.OPS["plan_stall_minutes"][0],
        description="a plan a system ran stalls, and a chat run has queued too long, after this many quiet minutes",
    )

    quiet_workers: list[int] = Field(
        default_factory=list, description="ids of workers whose queued runs write no run.queued_long"
    )


class DefaultSystem(BaseModel):
    project: str
    slug: str | None = Field(description="the default system; null when the project has none")
    name: str | None
    ops: OpsSettings | None = Field(description="the ops of its spec, defaults filled in; off for a spec with none")
    limits: dict | None = Field(description="the limits of its spec, ops_daily_usd filled in")
    can_change: bool = Field(description="the caller holds the admin role on the project")


class DefaultPut(BaseModel):
    slug: str = Field(min_length=1, max_length=63, pattern=system_spec.SLUG_PATTERN)


class SystemOps(BaseModel):
    project: str
    slug: str
    default: bool = Field(description="it is the project's default system, whose queue the hub fills")
    ops: OpsSettings
    ops_daily_usd: float = Field(description="what the runs of its ops threads may spend in 24 hours; 0: no limit")
    paused: bool
    paused_at: datetime | None
    paused_reason: str | None
    spent_usd: float = Field(0, description="what the runs of its ops threads queued in the last 24 hours spent")
    batches: dict[str, int] = Field(description="the batches of the project's queue by state; empty when not default")


class OpsPause(BaseModel):
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS, description="why, shown with the pause")


class OpsBatch(BaseModel):
    id: int
    key: str = Field(description="kind|cause|subject: the cause key its items share")
    kind: str
    cause: str
    subject: str
    state: BatchState
    items: int
    pending: int = Field(description="its items tier 0 has not handled")
    opened_at: datetime
    closes_at: datetime
    closed_at: datetime | None
    run_id: int | None
    recurring: bool = Field(False, description="its cause key closed 3 batches in 7 days: the system needs a fix")
    thread: str | None = Field(None, description="the hub web's path of its ops thread; null before it opened one")

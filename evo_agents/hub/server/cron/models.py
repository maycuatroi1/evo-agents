"""The bodies of the cron jobs of a project (``cron.routes``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.tables_cron import KINDS

REVISION_CONFLICT = "revision_conflict"
Kind = Literal["agent", "plan", "command"]
assert set(KINDS) == {"agent", "plan", "command"}


class CronFireOut(BaseModel):
    id: int
    scheduled_for: datetime = Field(description="the minute it was due; for run now, the moment it was pressed")
    manual: bool = Field(description="someone pressed run now")
    actor: str | None = Field(description="who pressed run now; null for a fire of the schedule")
    outcome: Literal["started", "busy", "missed", "refused"] | None = Field(
        description="started a run; busy: the run of a fire before was still queued or running; missed: the hub was "
        "down at its minute; refused, with the reason; null until the hub handled it"
    )
    reason: str | None
    run_id: int | None = Field(description="the run it started")
    created_at: datetime
    handled_at: datetime | None


class CronJobOut(BaseModel):
    project: str
    name: str
    cron: str = Field(description="five fields: minute, hour, day of month, month, day of week")
    timezone: str = Field(description="the IANA time zone its cron reads in")
    kind: Kind = Field(description="agent: a message to an agent in a new thread; plan: a plan run; command: a shell")
    agent: str | None = Field(description="kind agent: the handle of the agent it messages")
    chat_id: int | None = Field(description="kind agent: the chat of its threads; null: the owner's direct chat")
    prompt: str | None = Field(description="kind agent: the message")
    plan: str | None = Field(description="kind plan: the plan it runs")
    command: str | None = Field(description="kind command: the shell command line")
    repo: str | None = Field(description="kind command: the repo it runs in; null: an empty directory")
    timeout_minutes: int | None = Field(description="kind command: how long it may run")
    owner: str = Field(description="the login of who made it: its runs run as them")
    revision: int
    paused: bool
    paused_at: datetime | None
    paused_by: str | None = Field(description="who paused it; null when the hub did, or it runs")
    paused_reason: str | None
    next_fire_at: datetime | None = Field(description="its next minute, in UTC; null while paused")
    last_fire: CronFireOut | None
    cost_24h_usd: float = Field(description="what the runs of its fires cost in the last 24 hours")
    can_change: bool = Field(description="the caller may change what it starts: its owner, holding writer")
    can_manage: bool = Field(description="the caller may pause, resume, fire and delete it: its owner, or an admin")
    created_at: datetime
    updated_at: datetime
    updated_by: str | None


class CronJobWritten(CronJobOut):
    created: bool
    changed: bool = Field(description="false when the hub held exactly this: no revision was added")


class CronJobPut(BaseModel):
    cron: str = Field(description="five fields: minute, hour, day of month, month, day of week")
    timezone: str = Field("UTC", description="an IANA time zone, such as Asia/Ho_Chi_Minh")
    kind: Kind
    agent: str | None = Field(None, description="kind agent: the handle of an agent of the project")
    chat_id: int | None = Field(None, description="kind agent: a chat of yours; none: your direct chat with the agent")
    prompt: str | None = Field(None, description="kind agent: the message")
    plan: str | None = Field(None, description="kind plan: a plan of the project")
    command: str | None = Field(None, description="kind command: the shell command line")
    repo: str | None = Field(None, description="kind command: a repo of the project; none: an empty directory")
    timeout_minutes: int | None = Field(None, description="kind command: 1 to 240 minutes (30)")
    if_revision: int | None = Field(
        None, ge=0, description="the revision it is at now; none or 0 creates it, and is 409 when it exists"
    )


class CronJobPause(BaseModel):
    reason: str | None = Field(None, max_length=500, description="why, shown with the pause")


class CronJobDeleted(BaseModel):
    project: str
    deleted: str


class CronJobConflict(ErrorBody):
    current: CronJobOut | None = Field(None, description="the job as the hub holds it")

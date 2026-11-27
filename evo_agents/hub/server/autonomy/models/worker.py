"""The bodies of what the worker tells the hub of a run's autonomy (``autonomy.routes.worker``)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import approval_heads

BACKUP_FRESH = timedelta(hours=24)  # the floor S2: a backup made within this long before a deletion
SKEW = timedelta(minutes=5)  # how far in the future a backup's time may be, the clocks apart


class BackupIn(BaseModel):
    target: str = Field(min_length=1, max_length=500, description="what was backed up: a database, a table, a bucket")
    location: str = Field(
        min_length=1, max_length=1000, description="where the backup is: a path, a URL, a snapshot id"
    )
    made_at: datetime | None = Field(default=None, description="when it was made, with its time zone; now when absent")
    restore: str | None = Field(default=None, max_length=2000, description="the command that restores it")


class BackupOut(BaseModel):
    seq: int = Field(description="the run event that records the backup")
    made_at: datetime
    fresh: bool = Field(description="made within the last 24 hours: it opens the floor S2 of K9")


class AutonomyActIn(BaseModel):
    klass: Literal["K5", "K6", "K8", "K9", "K10", "K12"] = Field(description="the class of the command")
    what: str = Field(min_length=1, max_length=500, description="the command, cut, never a credential's value")
    decision_id: int | None = Field(
        default=None,
        ge=1,
        description="for a class in ask: the decision of the run, of the class's category, that the agent asked first",
    )
    head: str | None = Field(
        default=None,
        pattern=approval_heads.HEAD.pattern,
        description="for a merge: the head of the request now, as the hook read it; null when it could not",
    )
    scope: str | None = Field(
        default=None,
        pattern=approval_heads.SCOPE.pattern,
        description="for a merge: the hash of the request's scope now (approval_heads.scope_of)",
    )


class AutonomyActOut(BaseModel):
    mode: Literal["deny", "ask", "notify", "auto"] | None = Field(
        description="the class's mode for the run's principal now, the stricter of its snapshot and the value now; "
        "null for a run without a snapshot"
    )
    told: bool = Field(description="whether the hub told the principal (mode notify)")
    answered: bool = Field(
        description="whether decision_id is a decision of the run, of the class, answered, and its approval holds for "
        "head and scope"
    )
    stale: str | None = Field(
        None,
        description="why the answer of decision_id no longer opens the gate: it was given for another head or scope "
        "of the request; ask again",
    )

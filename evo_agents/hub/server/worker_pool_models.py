"""The models of the pool API of ``evo_agents.hub.server.worker_pools``: what creating and changing a pool take, what a
pool and its nodes show, and the join tokens of a pool, with the bounds schema 0147 holds them to."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub import runs
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.worker_doctor import CODE_PATTERN

POOL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
# A label or a taint of a pool: a word, or key=value; the keys the hub gives labels itself are not one's to set.
POOL_LABEL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}(=[A-Za-z0-9][A-Za-z0-9._-]{0,59})?$"
RESERVED_KEYS = ("pool", "os", "arch", "runtime", "feature")
MAX_POOL_LABELS = 16
MAX_ACCEPTED = 32
REAP_DAYS = (1, 90)
DEFAULT_REAP_DAYS = 7
TOKEN_HOURS = (1, 30 * 24)
DEFAULT_TOKEN_HOURS = 24
MAX_USES = (1, 100)
TOKEN_STATES = ("live", "expired", "revoked", "used")

Label = Annotated[str, Field(pattern=POOL_LABEL)]
Project = Annotated[str, Field(pattern=PROJECT_NAME)]
Code = Annotated[str, Field(pattern=CODE_PATTERN)]


def _unreserved(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    for value in values:
        key = value.split("=", 1)[0].lower()
        if key in RESERVED_KEYS:
            raise ValueError(f"{key} is a key the hub gives labels itself ({', '.join(RESERVED_KEYS)}): choose another")
    return list(dict.fromkeys(values))


class PoolSettings(BaseModel):
    """What a pool gives every node, each field optional: what PATCH changes."""

    projects: list[Project] | None = Field(
        None, min_length=1, max_length=100, description="the projects its nodes take runs of; the writer role on each"
    )
    slots: int | None = Field(None, ge=1, le=runs.MAX_SLOTS, description="runs each node holds at once")
    labels: list[Label] | None = Field(
        None,
        max_length=MAX_POOL_LABELS,
        description="labels every node has, a word or key=value; the hub adds pool=<name> itself",
    )
    taints: list[Label] | None = Field(
        None,
        max_length=MAX_POOL_LABELS,
        description="taints every node has: a node takes only the runs that tolerate each of them",
    )
    accepted_findings: list[Code] | None = Field(
        None,
        max_length=MAX_ACCEPTED,
        description="codes of high doctor findings a node may have and still be ready (`evo-agents worker doctor`)",
    )
    reap_after_days: int | None = Field(
        None, ge=REAP_DAYS[0], le=REAP_DAYS[1], description="days a node stays offline before the hub revokes it"
    )

    @field_validator("labels", "taints")
    @classmethod
    def _unreserved_keys(cls, value: list[str] | None) -> list[str] | None:
        return _unreserved(value)

    @field_validator("projects", "accepted_findings")
    @classmethod
    def _once(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(value))


class PoolRequest(PoolSettings):
    name: str = Field(pattern=POOL_NAME, description="unique among the owner's pools; it starts its nodes' names")
    projects: list[Project] = Field(
        min_length=1, max_length=100, description="the projects its nodes take runs of; the writer role on each"
    )
    slots: int = Field(1, ge=1, le=runs.MAX_SLOTS, description="runs each node holds at once")
    labels: list[Label] = Field(default_factory=list, max_length=MAX_POOL_LABELS)
    taints: list[Label] = Field(default_factory=list, max_length=MAX_POOL_LABELS)
    accepted_findings: list[Code] = Field(default_factory=list, max_length=MAX_ACCEPTED)
    reap_after_days: int = Field(DEFAULT_REAP_DAYS, ge=REAP_DAYS[0], le=REAP_DAYS[1])


class PoolUpdate(PoolSettings):
    @model_validator(mode="after")
    def _some_field(self) -> PoolUpdate:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError(
                "name at least one of projects, slots, labels, taints, accepted_findings and reap_after_days to change"
            )
        return self


class PoolNode(BaseModel):
    id: int
    name: str
    hostname: str
    status: Literal[runs.WORKER_STATUSES]
    agent_version: str
    labels: list[str] = Field(description="its own labels, its pool's, and pool=<name>")
    ready: bool = Field(
        True,
        description="whether it takes runs: its newest doctor report is at most 48 hours old and has no high finding "
        "but those the pool accepts",
    )
    ready_reason: str | None = Field(None, description="why it takes no run, while ready is false; null when ready")
    created_at: datetime
    last_heartbeat_at: datetime | None


class Pool(BaseModel):
    id: int
    name: str
    owner: str = Field(description="the login of the member who made it")
    projects: list[str]
    slots: int
    labels: list[str]
    taints: list[str]
    accepted_findings: list[str]
    reap_after_days: int
    created_at: datetime
    updated_at: datetime
    nodes: list[PoolNode] = Field(description="its nodes that are not revoked, newest first")


class JoinTokenRequest(BaseModel):
    hours: int = Field(
        DEFAULT_TOKEN_HOURS, ge=TOKEN_HOURS[0], le=TOKEN_HOURS[1], description="how long it works: 1 hour to 30 days"
    )
    max_uses: int = Field(1, ge=MAX_USES[0], le=MAX_USES[1], description="machines that may join with it, 1 to 100")


class JoinToken(BaseModel):
    id: int
    pool: str
    status: Literal[TOKEN_STATES]
    created_by: str
    created_at: datetime
    expires_at: datetime
    max_uses: int
    uses: int = Field(description="machines that joined with it")
    last_used_at: datetime | None
    revoked_at: datetime | None


class IssuedJoinToken(JoinToken):
    token: str = Field(description="evj_...; shown once, the hub keeps only its SHA-256")

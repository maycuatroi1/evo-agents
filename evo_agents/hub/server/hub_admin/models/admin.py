"""The shapes and limits of hub administration (``evo_agents.hub.server.admin``): users and their grants, a grant
given, the retention of built graphs, the scrub of secrets, and the overview of what needs an admin."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.config import MAX_KG_KEEP_ARTIFACTS
from evo_agents.hub.server.base.models import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.identity.models.auth import GrantInfo

SHA256 = r"^[0-9a-f]{64}$"
MISSING = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}

ACTIVE_DAYS = 30  # a member the hub saw within this many days is active
TOKEN_EXPIRING_DAYS = 14  # a live token expiring within this many days is about to expire
TOKEN_UNUSED_DAYS = 90  # a token not used (or, never used, not issued) for this many days is unused
FAILED_BUILD_DAYS = 7  # graph builds that failed within this many days
AUDIT_HOURS = 24  # audit rows of the last this many hours


class UserGrant(GrantInfo):
    granted_by: str | None = Field(description="the login of the admin who gave or last changed the grant")
    granted_at: datetime


class UserRow(BaseModel):
    login: str
    admin: bool
    signed_in: bool = Field(description="false for a login granted access before its first sign-in")
    created_at: datetime
    last_seen_at: datetime | None
    active_tokens: int
    grants: list[UserGrant]


class GrantRequest(BaseModel):
    role: Literal["reader", "writer", "admin"]
    max_level: str = Field(min_length=1, max_length=100, description="a level of the project's label ladder")


class Grant(BaseModel):
    project: str
    login: str
    role: str
    max_level: str
    created: bool = Field(description="false when an existing grant was changed")


class PruneRequest(BaseModel):
    project: str | None = Field(None, pattern=PROJECT_NAME, description="left out for every project")
    keep: int | None = Field(
        None,
        ge=1,
        le=MAX_KG_KEEP_ARTIFACTS,
        description="the newest graphs of each project whose artifact stays; default EVO_HUB_KG_KEEP_ARTIFACTS",
    )
    dry_run: bool = Field(False, description="answer what would be deleted, and change nothing")
    graph_files: bool = Field(
        False,
        description="also delete every graph file of a project whose graph the hub's tables hold, the newest included",
    )


class ProjectPruned(BaseModel):
    project: str
    artifacts: int = Field(
        description="distinct artifacts the project's builds pointed at, and kg-graph blobs no build did"
    )
    kept: int
    pruned: int = Field(description="artifacts dropped")
    pruned_bytes: int
    builds: int = Field(description="builds whose artifact was dropped: they keep their content hash and counts")


class KgPruned(BaseModel):
    dry_run: bool
    keep: int
    graph_files: bool = Field(description="every graph file of a project whose graph the tables hold was pruned")
    projects: list[ProjectPruned]
    deleted: int = Field(description="objects deleted from the bucket; for a dry run, those that would be")
    deleted_bytes: int
    pending: int = Field(description="blobs whose object still waits to be deleted; the next prune tries again")


class MemberCounts(BaseModel):
    total: int = Field(description="every user: people who signed in, and logins granted access before signing in")
    active: int = Field(description="users the hub saw within active_days: any token or web session of theirs used")
    not_signed_in: int = Field(description="logins granted access that have not signed in yet")
    active_days: int


class TokenCounts(BaseModel):
    live: int = Field(description="tokens and web sessions neither revoked nor expired")
    expiring: int = Field(
        description="live ones that expire within expiring_days; GET /v1/admin/tokens?state=expiring lists them"
    )
    unused: int = Field(
        description="tokens not revoked whose last use, or issue when never used, is unused_days old or more; "
        "GET /v1/admin/tokens?state=unused lists them"
    )
    expiring_days: int
    unused_days: int


class ProjectGrantCounts(BaseModel):
    project: str
    admins: int
    writers: int
    readers: int


class StorageCounts(BaseModel):
    objects: int = Field(description="distinct blobs in the bucket: projects holding the same bytes share one object")
    bytes: int = Field(description="their size")
    pending_deletions: int = Field(
        description="objects nothing refers to any more whose deletion from the bucket has not succeeded yet; "
        "the next prune tries again"
    )
    pending_bytes: int


class ProjectFailedBuilds(BaseModel):
    project: str
    failed: int = Field(description="builds of the project that failed within the window")
    last_failed_id: int
    last_failed_at: datetime
    latest_id: int = Field(description="the project's newest build, whatever its status")
    latest_status: Literal["queued", "running", "succeeded", "failed"]


class FailedBuildCounts(BaseModel):
    failed: int = Field(description="graph builds of every project that failed within days")
    days: int
    projects: list[ProjectFailedBuilds] = Field(description="the projects with such a build, latest failure first")


class WorkerCounts(BaseModel):
    live: int = Field(description="workers not revoked")
    offline: int = Field(description="live workers without a heartbeat for over offline_after_seconds, or without any")
    offline_after_seconds: int


class AuditCounts(BaseModel):
    rows: int
    hours: int


class AdminOverview(BaseModel):
    members: MemberCounts
    tokens: TokenCounts
    grants: list[ProjectGrantCounts] = Field(description="every project, by name, with its grants by role")
    storage: StorageCounts
    kg_builds: FailedBuildCounts
    workers: WorkerCounts
    audit: AuditCounts

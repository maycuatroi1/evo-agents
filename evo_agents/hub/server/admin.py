"""Hub administration, for the logins listed in EVO_HUB_ADMINS only: users, grants, what needs an admin, row counts,
and the retention of built graphs.

GET /v1/admin/overview answers what an admin may have to act on, in one read: members and how many were seen lately,
tokens about to expire or long unused, the grants of each project, the bytes the blob store holds and the objects
waiting to leave it, graph builds that failed lately, offline workers, and the audit rows of the last day. Each count
names its window, so the web says it without knowing the hub's settings, and the token counts use the same conditions
as GET /v1/admin/tokens?state=expiring and ?state=unused, which list them. GET /v1/admin/stats counts the rows of every
table, for diagnostics.

A grant gives a login a role on a project (reader, writer or admin) and the highest level of that project's label
ladder it may see. The ladder is the project's own, as ``evo_agents.kg.policy.Policy`` reads it from the levels
registered with the project, so a max level outside it is refused. A login that has not signed in yet can be
granted: its users row waits for the first sign-in, which claims it. Granting and revoking add an audit row
naming the project, the login and the role, never anything the grant gives access to. Revoking a grant, or lowering it
to reader, gives back the credential leases still out of every run the member dispatched in the project
(``credentials.end_member_leases``), whose worker can no longer ask for them.

POST /v1/admin/kg/prune runs the retention of built graphs now (``evo_agents.hub.kg_prune``), as the worker does every
hour: for one project or every one, keeping the artifacts of the ``keep`` newest graphs of each (default
EVO_HUB_KG_KEEP_ARTIFACTS). With ``dry_run`` it answers what it would delete and changes nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from psycopg import sql
from pydantic import BaseModel, Field

from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.config import MAX_KG_KEEP_ARTIFACTS, HubConfig
from evo_agents.hub.runs import OFFLINE_AFTER_SECONDS
from evo_agents.hub.server import audit
from evo_agents.hub.server.auth import GrantInfo
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import AdminUser, admin
from evo_agents.kg.policy import Policy

PROJECT_NAME = r"^[a-z0-9][a-z0-9-]{0,99}$"  # as the projects table accepts it
LOGIN_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
MISSING = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}

ACTIVE_DAYS = 30  # a member the hub saw within this many days is active
TOKEN_EXPIRING_DAYS = 14  # a live token expiring within this many days is about to expire
TOKEN_UNUSED_DAYS = 90  # a token not used (or, never used, not issued) for this many days is unused
FAILED_BUILD_DAYS = 7  # graph builds that failed within this many days
AUDIT_HOURS = 24  # audit rows of the last this many hours

# The conditions on a token row ``t`` that the overview counts and GET /v1/admin/tokens?state= lists, so a count and the
# list it links to agree. A token expires TOKEN_TTL after its last use, so with TOKEN_TTL at 90 days an unused token is
# also an expired one that nobody revoked.
TOKEN_LIVE = "t.revoked_at IS NULL AND t.expires_at > now()"
TOKEN_EXPIRING = f"{TOKEN_LIVE} AND t.expires_at <= now() + make_interval(days => {TOKEN_EXPIRING_DAYS})"
TOKEN_UNUSED = (
    "t.revoked_at IS NULL "
    f"AND coalesce(t.last_used_at, t.created_at) <= now() - make_interval(days => {TOKEN_UNUSED_DAYS})"
)
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Login = Annotated[str, Path(pattern=LOGIN_NAME)]

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(admin)], responses={401: {"model": ErrorBody}}
)


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


USERS = """
SELECT u.id, u.login, u.github_id IS NOT NULL, u.created_at, u.last_seen_at,
       (SELECT count(*) FROM tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL AND t.expires_at > now())
  FROM users u ORDER BY lower(u.login)
"""
USER_GRANTS = """
SELECT g.user_id, p.name, g.role, g.max_level, b.login, g.granted_at
  FROM grants g JOIN projects p ON p.id = g.project_id LEFT JOIN users b ON b.id = g.granted_by
 ORDER BY p.name
"""
UPSERT_GRANT = """
INSERT INTO grants (user_id, project_id, role, max_level, granted_by) VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_id, project_id) DO UPDATE
   SET role = EXCLUDED.role, max_level = EXCLUDED.max_level, granted_by = EXCLUDED.granted_by, granted_at = now()
RETURNING xmax = 0
"""


@router.get("/users", response_model=list[UserRow], responses={403: {"model": ErrorBody}})
async def users(request: Request) -> list[UserRow]:
    config: HubConfig = request.app.state.config
    async with request.app.state.pool.connection() as conn:
        rows = await (await conn.execute(USERS)).fetchall()
        grant_rows = await (await conn.execute(USER_GRANTS)).fetchall()
    grants: dict[int, list[UserGrant]] = {}
    for user_id, project, role, max_level, granted_by, granted_at in grant_rows:
        grant = UserGrant(project=project, role=role, max_level=max_level, granted_by=granted_by, granted_at=granted_at)
        grants.setdefault(user_id, []).append(grant)
    return [
        UserRow(
            login=login,
            admin=config.is_admin(login),
            signed_in=signed_in,
            created_at=created_at,
            last_seen_at=last_seen_at,
            active_tokens=active,
            grants=grants.get(user_id, []),
        )
        for user_id, login, signed_in, created_at, last_seen_at, active in rows
    ]


async def _project(conn, name: str) -> tuple[int, list[str]]:
    """The id and label ladder of a registered project; 404 naming the command that registers one."""
    row = await (await conn.execute("SELECT id, levels, locations FROM projects WHERE name = %s", (name,))).fetchone()
    if row is None:
        raise HTTPException(404, f"project {name} is not registered on this hub: run `evo-agents hub project register`")
    project_id, levels, locations = row
    return project_id, Policy(name, {"policy": {"levels": levels, "locations": locations}}).levels


@router.put(
    "/projects/{project}/grants/{login}", response_model=Grant, responses={**MISSING, 422: {"model": ErrorBody}}
)
async def grant(request: Request, body: GrantRequest, project: ProjectName, login: Login, user: AdminUser) -> Grant:
    """Give ``login`` a role on ``project``, or change the one it has."""
    async with request.app.state.pool.connection() as conn:
        project_id, ladder = await _project(conn, project)
        if body.max_level not in ladder:
            levels = ", ".join(ladder)
            raise HTTPException(422, f"max-level must be a level of project {project} ({levels}), not {body.max_level}")
        await conn.execute("INSERT INTO users (login) VALUES (%s) ON CONFLICT DO NOTHING", (login,))
        cursor = await conn.execute("SELECT id, login FROM users WHERE lower(login) = lower(%s)", (login,))
        grantee_id, grantee = await cursor.fetchone()
        cursor = await conn.execute(UPSERT_GRANT, (grantee_id, project_id, body.role, body.max_level, user.user_id))
        (created,) = await cursor.fetchone()
        target = f"{project}/{grantee} role={body.role} max_level={body.max_level}"
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.GRANT_PUT, target=target)
        taken = []
        if not has_role(body.role, "writer"):
            taken = await _take_back_leases(conn, user, project_id, grantee_id, f"grant-{body.role}")
    await _revoke_taken(request, taken)
    return Grant(project=project, login=grantee, role=body.role, max_level=body.max_level, created=created)


async def _take_back_leases(conn, user, project_id: int, member_id: int, by: str) -> list[int]:
    """The leases still out of the runs ``member_id`` dispatched in the project, given back now that the member no
    longer holds writer on it; the runs, for ``_revoke_taken`` once the transaction commits."""
    from evo_agents.hub.server import credentials  # it reads ProjectName of this module

    return await credentials.end_member_leases(
        conn, user_id=member_id, project_id=project_id, actor_id=user.user_id, token_id=user.token_id, by=by
    )


async def _revoke_taken(request: Request, run_ids: list[int]) -> None:
    """Revoke at GitHub the tokens of the leases ``_take_back_leases`` gave back; GitHub failing leaves them to the
    reaper."""
    from evo_agents.hub.server import credentials

    state = request.app.state
    for run_id in run_ids:
        await credentials.revoke_tokens(state.pool, state.sealer, state.github_app, run_id=run_id)


@router.delete("/projects/{project}/grants/{login}", status_code=204, response_class=Response, responses=MISSING)
async def revoke(request: Request, project: ProjectName, login: Login, user: AdminUser) -> Response:
    """Take away the role ``login`` has on ``project``."""
    async with request.app.state.pool.connection() as conn:
        project_id, _ = await _project(conn, project)
        cursor = await conn.execute(
            "DELETE FROM grants g USING users u WHERE g.user_id = u.id AND g.project_id = %s "
            "AND lower(u.login) = lower(%s) RETURNING u.login, u.id",
            (project_id, login),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(404, f"{login} has no grant on project {project}")
        target = f"{project}/{row[0]}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.GRANT_DELETE, target=target
        )
        taken = await _take_back_leases(conn, user, project_id, row[1], "grant-deleted")
    await _revoke_taken(request, taken)
    return Response(status_code=204)


@router.get("/stats", response_model=dict[str, int], responses={403: {"model": ErrorBody}})
async def stats(request: Request) -> dict[str, int]:
    """The number of rows of every hub table, by table name; tables added by later revisions show up by themselves."""
    async with request.app.state.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename <> 'alembic_version' "
            "ORDER BY tablename"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        query = sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT {}, count(*) FROM {}").format(sql.Literal(table), sql.Identifier(table)) for table in tables
        )
        counts = await (await conn.execute(query)).fetchall() if tables else []
    return {table: count for table, count in counts}


# One row of every count. Each table is read once; blobs are counted by object, as several projects may hold one.
OVERVIEW = f"""
WITH members AS (
    SELECT count(*) AS total,
           count(*) FILTER (WHERE last_seen_at > now() - make_interval(days => {ACTIVE_DAYS})) AS active,
           count(*) FILTER (WHERE github_id IS NULL) AS not_signed_in
      FROM users
), tokens AS (
    SELECT count(*) FILTER (WHERE {TOKEN_LIVE}) AS live,
           count(*) FILTER (WHERE {TOKEN_EXPIRING}) AS expiring,
           count(*) FILTER (WHERE {TOKEN_UNUSED}) AS unused
      FROM tokens t
     WHERE t.revoked_at IS NULL
), stored AS (
    SELECT count(*) AS objects, coalesce(sum(size), 0)::bigint AS bytes
      FROM (SELECT max(size) AS size FROM blobs GROUP BY sha256) AS objects
), doomed AS (
    SELECT count(*) AS objects, coalesce(sum(size), 0)::bigint AS bytes FROM blob_deletions WHERE deleted_at IS NULL
), fleet AS (
    SELECT count(*) AS live,
           count(*) FILTER (
               WHERE last_heartbeat_at IS NULL
                  OR last_heartbeat_at < now() - make_interval(secs => {OFFLINE_AFTER_SECONDS})
           ) AS offline
      FROM workers
     WHERE revoked_at IS NULL
), trail AS (
    SELECT count(*) AS rows FROM audit WHERE at > now() - make_interval(hours => {AUDIT_HOURS})
)
SELECT members.total, members.active, members.not_signed_in, tokens.live, tokens.expiring, tokens.unused,
       stored.objects, stored.bytes, doomed.objects, doomed.bytes, fleet.live, fleet.offline, trail.rows
  FROM members, tokens, stored, doomed, fleet, trail
"""
OVERVIEW_FIELDS = (
    "members",
    "active_members",
    "not_signed_in",
    "live_tokens",
    "expiring_tokens",
    "unused_tokens",
    "objects",
    "bytes",
    "pending_deletions",
    "pending_bytes",
    "live_workers",
    "offline_workers",
    "audit_rows",
)
GRANT_COUNTS = """
SELECT p.name,
       count(*) FILTER (WHERE g.role = 'admin'),
       count(*) FILTER (WHERE g.role = 'writer'),
       count(*) FILTER (WHERE g.role = 'reader')
  FROM projects p LEFT JOIN grants g ON g.project_id = p.id
 GROUP BY p.id, p.name
 ORDER BY p.name
"""
# The newest build of each project comes from kg_builds_project_idx, one probe per project with a failure.
FAILED_BUILDS = f"""
SELECT p.name, f.failed, f.last_id, f.last_at, latest.id, latest.status
  FROM (
    SELECT project_id, count(*) AS failed,
           (array_agg(id ORDER BY finished_at DESC, id DESC))[1] AS last_id, max(finished_at) AS last_at
      FROM kg_builds
     WHERE status = 'failed' AND finished_at > now() - make_interval(days => {FAILED_BUILD_DAYS})
     GROUP BY project_id
  ) AS f
  JOIN projects p ON p.id = f.project_id
  CROSS JOIN LATERAL (
    SELECT b.id, b.status FROM kg_builds b WHERE b.project_id = f.project_id ORDER BY b.id DESC LIMIT 1
  ) AS latest
 ORDER BY f.last_at DESC, p.name
"""


@router.get("/overview", response_model=AdminOverview, responses={403: {"model": ErrorBody}})
async def overview(request: Request) -> AdminOverview:
    """What may need an admin: members, tokens to rotate, grants by project, storage, failed graph builds, offline
    workers and the last day of the audit trail."""
    async with request.app.state.pool.connection() as conn:
        counts = await (await conn.execute(OVERVIEW)).fetchone()
        grants = await (await conn.execute(GRANT_COUNTS)).fetchall()
        failed = await (await conn.execute(FAILED_BUILDS)).fetchall()
    count = dict(zip(OVERVIEW_FIELDS, counts, strict=True))
    projects = [
        ProjectFailedBuilds(
            project=project,
            failed=failed_builds,
            last_failed_id=last_id,
            last_failed_at=last_at,
            latest_id=latest_id,
            latest_status=latest_status,
        )
        for project, failed_builds, last_id, last_at, latest_id, latest_status in failed
    ]
    return AdminOverview(
        members=MemberCounts(
            total=count["members"],
            active=count["active_members"],
            not_signed_in=count["not_signed_in"],
            active_days=ACTIVE_DAYS,
        ),
        tokens=TokenCounts(
            live=count["live_tokens"],
            expiring=count["expiring_tokens"],
            unused=count["unused_tokens"],
            expiring_days=TOKEN_EXPIRING_DAYS,
            unused_days=TOKEN_UNUSED_DAYS,
        ),
        grants=[
            ProjectGrantCounts(project=project, admins=admins, writers=writers, readers=readers)
            for project, admins, writers, readers in grants
        ],
        storage=StorageCounts(
            objects=count["objects"],
            bytes=count["bytes"],
            pending_deletions=count["pending_deletions"],
            pending_bytes=count["pending_bytes"],
        ),
        kg_builds=FailedBuildCounts(
            failed=sum(item.failed for item in projects), days=FAILED_BUILD_DAYS, projects=projects
        ),
        workers=WorkerCounts(
            live=count["live_workers"], offline=count["offline_workers"], offline_after_seconds=OFFLINE_AFTER_SECONDS
        ),
        audit=AuditCounts(rows=count["audit_rows"], hours=AUDIT_HOURS),
    )


@router.post("/kg/prune", response_model=KgPruned, responses={**MISSING, 503: {"model": ErrorBody}})
async def prune_kg(request: Request, body: PruneRequest, user: AdminUser) -> KgPruned:
    """Delete the artifacts of graphs older than each project's ``keep`` newest; the builds keep their records."""
    from evo_agents.hub.kg_prune import UnknownProject, prune

    state = request.app.state
    if state.blobs is None:
        missing = ", ".join(state.config.blob_store_missing())
        raise HTTPException(503, f"the blob store is not configured on this hub: set {missing}")
    keep = body.keep or state.config.kg_keep_artifacts
    try:
        report = await prune(
            state.pool,
            state.blobs,
            keep,
            body.project,
            dry_run=body.dry_run,
            actor_id=user.user_id,
            token_id=user.token_id,
        )
    except UnknownProject as exc:
        raise HTTPException(404, f"{exc}: run `evo-agents hub project register`") from None
    except BlobStoreUnavailable:
        raise HTTPException(
            503,
            "the blob store did not answer: the builds were marked and their artifacts wait to be deleted by the next "
            "prune",
        ) from None
    return KgPruned(**report.to_json())

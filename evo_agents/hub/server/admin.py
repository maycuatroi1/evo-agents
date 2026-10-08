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

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    and_,
    cast,
    column,
    delete,
    func,
    literal,
    or_,
    select,
    table,
    true,
    union_all,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by, array_agg
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
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

# The conditions on a row of tokens that the overview counts and GET /v1/admin/tokens?state= lists, so a count and the
# list it links to agree. A token expires TOKEN_TTL after its last use, so with TOKEN_TTL at 90 days an unused token is
# also an expired one that nobody revoked.
_tokens = tables.tokens
TOKEN_LIVE = and_(_tokens.c.revoked_at.is_(None), _tokens.c.expires_at > func.now())
TOKEN_EXPIRING = and_(TOKEN_LIVE, _tokens.c.expires_at <= func.now() + timedelta(days=TOKEN_EXPIRING_DAYS))
TOKEN_UNUSED = and_(
    _tokens.c.revoked_at.is_(None),
    func.coalesce(_tokens.c.last_used_at, _tokens.c.created_at) <= func.now() - timedelta(days=TOKEN_UNUSED_DAYS),
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


def _users():
    """Every user by login whatever its case, with the number of their live tokens."""
    users, tokens = tables.users, tables.tokens
    live = select(func.count()).select_from(tokens).where(tokens.c.user_id == users.c.id, TOKEN_LIVE)
    return select(
        users.c.id,
        users.c.login,
        users.c.github_id.is_not(None).label("signed_in"),
        users.c.created_at,
        users.c.last_seen_at,
        live.scalar_subquery().label("active_tokens"),
    ).order_by(func.lower(users.c.login))


def _user_grants():
    """Every grant by project, with the login of the admin who gave it."""
    grants, projects, users = tables.grants, tables.projects, tables.users
    return (
        select(
            grants.c.user_id,
            projects.c.name.label("project"),
            grants.c.role,
            grants.c.max_level,
            users.c.login.label("granted_by"),
            grants.c.granted_at,
        )
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .outerjoin(users, users.c.id == grants.c.granted_by)
        .order_by(projects.c.name)
    )


@router.get("/users", response_model=list[UserRow], responses={403: {"model": ErrorBody}})
async def users(request: Request) -> list[UserRow]:
    config: HubConfig = request.app.state.config
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(_users())).all()
        grant_rows = (await conn.execute(_user_grants())).all()
    grants: dict[int, list[UserGrant]] = {}
    for row in grant_rows:
        grant = UserGrant(
            project=row.project,
            role=row.role,
            max_level=row.max_level,
            granted_by=row.granted_by,
            granted_at=row.granted_at,
        )
        grants.setdefault(row.user_id, []).append(grant)
    return [
        UserRow(
            login=row.login,
            admin=config.is_admin(row.login),
            signed_in=row.signed_in,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            active_tokens=row.active_tokens,
            grants=grants.get(row.id, []),
        )
        for row in rows
    ]


async def _project(conn: AsyncConnection, name: str) -> tuple[int, list[str]]:
    """The id and label ladder of a registered project; 404 naming the command that registers one."""
    projects = tables.projects
    found = await conn.execute(
        select(projects.c.id, projects.c.levels, projects.c.locations).where(projects.c.name == name)
    )
    row = found.first()
    if row is None:
        raise HTTPException(404, f"project {name} is not registered on this hub: run `evo-agents hub project register`")
    return row.id, Policy(name, {"policy": {"levels": row.levels, "locations": row.locations}}).levels


def _upsert_grant(user_id: int, project_id: int, role: str, max_level: str, granted_by: int):
    """Give the grant, or change the one held; RETURNING whether the row is new (``xmax`` is 0 only for an insert)."""
    grants = tables.grants
    given = pg_insert(grants).values(
        user_id=user_id, project_id=project_id, role=role, max_level=max_level, granted_by=granted_by
    )
    return given.on_conflict_do_update(
        index_elements=[grants.c.user_id, grants.c.project_id],
        set_={
            "role": given.excluded.role,
            "max_level": given.excluded.max_level,
            "granted_by": given.excluded.granted_by,
            "granted_at": func.now(),
        },
    ).returning((column("xmax") == 0).label("created"))


@router.put(
    "/projects/{project}/grants/{login}", response_model=Grant, responses={**MISSING, 422: {"model": ErrorBody}}
)
async def grant(request: Request, body: GrantRequest, project: ProjectName, login: Login, user: AdminUser) -> Grant:
    """Give ``login`` a role on ``project``, or change the one it has."""
    async with request.app.state.engine.begin() as conn:
        project_id, ladder = await _project(conn, project)
        if body.max_level not in ladder:
            levels = ", ".join(ladder)
            raise HTTPException(422, f"max-level must be a level of project {project} ({levels}), not {body.max_level}")
        users = tables.users
        await conn.execute(pg_insert(users).values(login=login).on_conflict_do_nothing())
        found = await conn.execute(
            select(users.c.id, users.c.login).where(func.lower(users.c.login) == func.lower(login))
        )
        grantee_id, grantee = found.one()
        upserted = await conn.execute(_upsert_grant(grantee_id, project_id, body.role, body.max_level, user.user_id))
        created = upserted.scalar_one()
        target = f"{project}/{grantee} role={body.role} max_level={body.max_level}"
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.GRANT_PUT, target=target)
        taken = []
        if not has_role(body.role, "writer"):
            taken = await _take_back_leases(conn, user, project_id, grantee_id, f"grant-{body.role}")
    await _revoke_taken(request, taken)
    return Grant(project=project, login=grantee, role=body.role, max_level=body.max_level, created=created)


async def _take_back_leases(conn: AsyncConnection, user, project_id: int, member_id: int, by: str) -> list[int]:
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
        await credentials.revoke_tokens(state.engine, state.sealer, credentials.revoker(state), run_id=run_id)


@router.delete("/projects/{project}/grants/{login}", status_code=204, response_class=Response, responses=MISSING)
async def revoke(request: Request, project: ProjectName, login: Login, user: AdminUser) -> Response:
    """Take away the role ``login`` has on ``project``."""
    async with request.app.state.engine.begin() as conn:
        project_id, _ = await _project(conn, project)
        grants, users = tables.grants, tables.users
        deleted = await conn.execute(
            delete(grants)
            .where(
                grants.c.user_id == users.c.id,
                grants.c.project_id == project_id,
                func.lower(users.c.login) == func.lower(login),
            )
            .returning(users.c.login, users.c.id)
        )
        row = deleted.first()
        if row is None:
            raise HTTPException(404, f"{login} has no grant on project {project}")
        target = f"{project}/{row.login}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.GRANT_DELETE, target=target
        )
        taken = await _take_back_leases(conn, user, project_id, row.id, "grant-deleted")
    await _revoke_taken(request, taken)
    return Response(status_code=204)


def _table_names():
    """The tables of the hub's schema by name, but Alembic's record of the revision."""
    pg_tables = table("pg_tables", column("schemaname"), column("tablename"))
    return (
        select(pg_tables.c.tablename)
        .where(pg_tables.c.schemaname == func.current_schema(), pg_tables.c.tablename != "alembic_version")
        .order_by(pg_tables.c.tablename)
    )


def _row_counts(names: list[str]):
    """One row per table of ``names``: its name and its number of rows."""
    return union_all(*(select(literal(name), func.count()).select_from(table(name)) for name in names))


@router.get("/stats", response_model=dict[str, int], responses={403: {"model": ErrorBody}})
async def stats(request: Request) -> dict[str, int]:
    """The number of rows of every hub table, by table name; tables added by later revisions show up by themselves."""
    async with request.app.state.engine.begin() as conn:
        names = (await conn.execute(_table_names())).scalars().all()
        counts = (await conn.execute(_row_counts(names))).all() if names else []
    return {name: rows for name, rows in counts}


def _counts():
    """One row of every count. Each table is read once; blobs are counted by object, as several projects may hold one.
    Each count is a CTE of one row, and the CTEs are joined on true."""
    users, tokens, blobs = tables.users, tables.tokens, tables.blobs
    blob_deletions, workers, audit_trail = tables.blob_deletions, tables.workers, tables.audit
    members = select(
        func.count().label("total"),
        func.count().filter(users.c.last_seen_at > func.now() - timedelta(days=ACTIVE_DAYS)).label("active"),
        func.count().filter(users.c.github_id.is_(None)).label("not_signed_in"),
    ).cte("members")
    credentials = (
        select(
            func.count().filter(TOKEN_LIVE).label("live"),
            func.count().filter(TOKEN_EXPIRING).label("expiring"),
            func.count().filter(TOKEN_UNUSED).label("unused"),
        )
        .where(tokens.c.revoked_at.is_(None))
        .cte("credentials")
    )
    objects = select(func.max(blobs.c.size).label("size")).group_by(blobs.c.sha256).subquery("objects")
    stored = select(
        func.count().label("objects"),
        cast(func.coalesce(func.sum(objects.c.size), 0), BigInteger).label("bytes"),
    ).cte("stored")
    doomed = (
        select(
            func.count().label("objects"),
            cast(func.coalesce(func.sum(blob_deletions.c.size), 0), BigInteger).label("bytes"),
        )
        .where(blob_deletions.c.deleted_at.is_(None))
        .cte("doomed")
    )
    silent = or_(
        workers.c.last_heartbeat_at.is_(None),
        workers.c.last_heartbeat_at < func.now() - timedelta(seconds=OFFLINE_AFTER_SECONDS),
    )
    fleet = (
        select(func.count().label("live"), func.count().filter(silent).label("offline"))
        .where(workers.c.revoked_at.is_(None))
        .cte("fleet")
    )
    trail = (
        select(func.count().label("rows"))
        .where(audit_trail.c.at > func.now() - timedelta(hours=AUDIT_HOURS))
        .cte("trail")
    )
    return select(
        members.c.total.label("members"),
        members.c.active.label("active_members"),
        members.c.not_signed_in,
        credentials.c.live.label("live_tokens"),
        credentials.c.expiring.label("expiring_tokens"),
        credentials.c.unused.label("unused_tokens"),
        stored.c.objects,
        stored.c.bytes,
        doomed.c.objects.label("pending_deletions"),
        doomed.c.bytes.label("pending_bytes"),
        fleet.c.live.label("live_workers"),
        fleet.c.offline.label("offline_workers"),
        trail.c.rows.label("audit_rows"),
    ).select_from(
        members.join(credentials, true())
        .join(stored, true())
        .join(doomed, true())
        .join(fleet, true())
        .join(trail, true())
    )


def _grant_counts():
    """Every project by name, with its grants by role."""
    projects, grants = tables.projects, tables.grants
    return (
        select(
            projects.c.name.label("project"),
            func.count().filter(grants.c.role == "admin").label("admins"),
            func.count().filter(grants.c.role == "writer").label("writers"),
            func.count().filter(grants.c.role == "reader").label("readers"),
        )
        .select_from(projects.outerjoin(grants, grants.c.project_id == projects.c.id))
        .group_by(projects.c.id, projects.c.name)
        .order_by(projects.c.name)
    )


def _failed_builds():
    """The projects with a build that failed within FAILED_BUILD_DAYS, latest failure first, each with its newest
    build, which comes from kg_builds_project_idx: one probe per project with a failure."""
    projects, kg_builds = tables.projects, tables.kg_builds
    # PostgreSQL's own aggregate_order_by: indexing Core's form renders array_agg(...)[1], which Postgres refuses
    # without parentheses around the call.
    last_id = array_agg(aggregate_order_by(kg_builds.c.id, kg_builds.c.finished_at.desc(), kg_builds.c.id.desc()))
    failed = (
        select(
            kg_builds.c.project_id,
            func.count().label("failed"),
            last_id[1].label("last_id"),
            func.max(kg_builds.c.finished_at).label("last_at"),
        )
        .where(
            kg_builds.c.status == "failed",
            kg_builds.c.finished_at > func.now() - timedelta(days=FAILED_BUILD_DAYS),
        )
        .group_by(kg_builds.c.project_id)
        .subquery("f")
    )
    newest = kg_builds.alias("b")
    latest = (
        select(newest.c.id, newest.c.status)
        .where(newest.c.project_id == failed.c.project_id)
        .order_by(newest.c.id.desc())
        .limit(1)
        .lateral("latest")
    )
    return (
        select(
            projects.c.name.label("project"),
            failed.c.failed,
            failed.c.last_id.label("last_failed_id"),
            failed.c.last_at.label("last_failed_at"),
            latest.c.id.label("latest_id"),
            latest.c.status.label("latest_status"),
        )
        .select_from(failed.join(projects, projects.c.id == failed.c.project_id).join(latest, true()))
        .order_by(failed.c.last_at.desc(), projects.c.name)
    )


@router.get("/overview", response_model=AdminOverview, responses={403: {"model": ErrorBody}})
async def overview(request: Request) -> AdminOverview:
    """What may need an admin: members, tokens to rotate, grants by project, storage, failed graph builds, offline
    workers and the last day of the audit trail."""
    async with request.app.state.engine.begin() as conn:
        count = (await conn.execute(_counts())).one()._mapping
        grants = (await conn.execute(_grant_counts())).all()
        failed = (await conn.execute(_failed_builds())).all()
    projects = [ProjectFailedBuilds(**row._mapping) for row in failed]
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
        grants=[ProjectGrantCounts(**row._mapping) for row in grants],
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
            state.engine,
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

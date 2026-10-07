"""Hub administration, for the logins listed in EVO_HUB_ADMINS only: users, grants, row counts, and the retention of
built graphs.

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
from evo_agents.hub.server import audit
from evo_agents.hub.server.auth import GrantInfo
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import AdminUser, admin
from evo_agents.kg.policy import Policy

PROJECT_NAME = r"^[a-z0-9][a-z0-9-]{0,99}$"  # as the projects table accepts it
LOGIN_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
MISSING = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
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

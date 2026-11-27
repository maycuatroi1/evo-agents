"""Hub administration (``evo_agents.hub.server.admin``): the users with their grants, a project and its label ladder,
giving and taking a grant with its audit row and the credential leases it takes back, the row counts of every table,
and the overview of what may need an admin."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.config import HubConfig
from evo_agents.hub.runs import OFFLINE_AFTER_SECONDS
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.hub_admin.models.admin import (
    ACTIVE_DAYS,
    AUDIT_HOURS,
    FAILED_BUILD_DAYS,
    TOKEN_EXPIRING_DAYS,
    TOKEN_UNUSED_DAYS,
    AdminOverview,
    AuditCounts,
    FailedBuildCounts,
    Grant,
    GrantRequest,
    MemberCounts,
    ProjectFailedBuilds,
    ProjectGrantCounts,
    StorageCounts,
    TokenCounts,
    UserGrant,
    UserRow,
    WorkerCounts,
)
from evo_agents.hub.server.hub_admin.repository import admin as repository
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.security import admin_login, holds
from evo_agents.schema.policy import Policy


async def users(engine: AsyncEngine, config: HubConfig, page: Paging) -> list[UserRow]:
    """The page ``page`` of every user by login, each with its grants and whether ``config`` makes it an admin."""
    async with UnitOfWork(engine) as conn:
        rows, grant_rows = await repository.users_page(conn, page, admin_login())
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
            admin=config.is_admin(row.admin_login),
            signed_in=row.signed_in,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            active_tokens=row.active_tokens,
            grants=grants.get(row.id, []),
        )
        for row in rows
    ]


async def project_ladder(conn: AsyncConnection, name: str) -> tuple[int, list[str]]:
    """The id and label ladder of a registered project; 404 naming the command that registers one."""
    row = await repository.project_row(conn, name)
    if row is None:
        raise HTTPException(404, f"project {name} is not registered on this hub: run `evo-agents hub project register`")
    return row.id, Policy(name, {"policy": {"levels": row.levels, "locations": row.locations}}).levels


async def give_grant(
    conn: AsyncConnection, user, project: str, login: str, body: GrantRequest
) -> tuple[Grant, list[int]]:
    """Give ``login`` the grant ``body`` on ``project``, or change the one it holds, as ``user`` does, with its audit
    row; the grant, and the runs whose credential leases were taken back (``revoke_taken`` gives them back at GitHub
    once the transaction commits)."""
    project_id, ladder = await project_ladder(conn, project)
    if body.max_level not in ladder:
        levels = ", ".join(ladder)
        raise HTTPException(422, f"max-level must be a level of project {project} ({levels}), not {body.max_level}")
    made_id = await repository.add_login(conn, login)
    if made_id is not None:  # a login that never signed in: its seat at the root comes with its row
        await org_store.place_user(conn, made_id)
    grantee_id, grantee = await repository.user_of_login(conn, login)
    created = await repository.put_grant(conn, grantee_id, project_id, body.role, body.max_level, user.user_id)
    target = f"{project}/{grantee} role={body.role} max_level={body.max_level}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.GRANT_PUT, target=target)
    taken = []
    if not holds(body.role, "writer"):
        taken = await take_back_leases(conn, user, project_id, grantee_id, f"grant-{body.role}")
    return Grant(project=project, login=grantee, role=body.role, max_level=body.max_level, created=created), taken


async def grant(engine: AsyncEngine, user, project: str, login: str, body: GrantRequest) -> tuple[Grant, list[int]]:
    """``give_grant`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await give_grant(conn, user, project, login, body)


async def take_back_leases(conn: AsyncConnection, user, project_id: int, member_id: int, by: str) -> list[int]:
    """The leases still out of the runs ``member_id`` dispatched in the project, given back now that the member no
    longer holds writer on it; the runs, for ``revoke_taken`` once the transaction commits."""
    from evo_agents.hub.server import lease_ending  # through runs and projects it imports this module

    return await lease_ending.end_member_leases(
        conn, user_id=member_id, project_id=project_id, actor_id=user.user_id, token_id=user.token_id, by=by
    )


async def revoke_taken(state, run_ids: list[int]) -> None:
    """Revoke at GitHub the tokens of the leases ``take_back_leases`` gave back, with the app's ``state``; GitHub
    failing leaves them to the reaper."""
    from evo_agents.hub.server import lease_ending

    for run_id in run_ids:
        await lease_ending.revoke_tokens(state.engine, state.sealer, lease_ending.revoker(state), run_id=run_id)


async def take_grant(conn: AsyncConnection, user, project: str, login: str) -> list[int]:
    """Take away the role ``login`` has on ``project``, as ``user`` does, with its audit row; the runs whose credential
    leases were given back, for ``revoke_taken``. 404 when ``login`` holds none."""
    project_id, _ = await project_ladder(conn, project)
    row = await repository.delete_grant(conn, project_id, login)
    if row is None:
        raise HTTPException(404, f"{login} has no grant on project {project}")
    target = f"{project}/{row.login}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.GRANT_DELETE, target=target)
    return await take_back_leases(conn, user, project_id, row.id, "grant-deleted")


async def revoke(engine: AsyncEngine, user, project: str, login: str) -> list[int]:
    """``take_grant`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await take_grant(conn, user, project, login)


async def stats(engine: AsyncEngine) -> dict[str, int]:
    """The number of rows of every hub table, by table name."""
    async with UnitOfWork(engine) as conn:
        counts = await repository.row_counts(conn)
    return {name: rows for name, rows in counts}


async def overview(engine: AsyncEngine) -> AdminOverview:
    """What may need an admin: members, tokens to rotate, grants by project, storage, failed graph builds, offline
    workers and the last day of the audit trail."""
    async with UnitOfWork(engine) as conn:
        count, grants, failed = await repository.overview_rows(conn)
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

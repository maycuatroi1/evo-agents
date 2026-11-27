"""The queries of the leases of a run (``service.leases``, ``service.survey``): the run a worker holds with its owner
and agent profile, the owner's grant, the secrets and pool accounts it may be leased, the GitHub tokens it holds, and
the rows of credential_leases the leases write."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.credentials.models.leases import REFRESH


def _run_row(run_id: int, *, lock: bool):
    """The run ``run_id`` with its project's name and its owner, the member who dispatched it; with ``lock`` the
    run's row is locked."""
    r, projects, users, ap, v = (
        tables.runs,
        tables.projects,
        tables.users,
        tables.agent_profiles,
        tables.agent_profile_versions,
    )
    credentials = (
        select(v.c.body["permissions"]["credential"])
        .where(v.c.profile_id == r.c.profile_id, v.c.version == r.c.profile_version)
        .scalar_subquery()
    )
    statement = (
        select(
            r.c.state,
            r.c.worker_id,
            r.c.kind,
            r.c.project_id,
            projects.c.name.label("project"),
            r.c.plan_id,
            r.c.step_key,
            r.c.dispatched_by,
            r.c.principal_id,
            users.c.login.label("owner"),
            r.c.repo,
            r.c.repos,
            users.c.github_id,
            r.c.profile_id,
            r.c.profile_version,
            ap.c.handle.label("profile"),
            func.coalesce(ap.c.is_default, False).label("profile_default"),
            credentials.label("credentials"),
            ap.c.project_id.label("profile_project_id"),
            r.c.as_owner,
            ap.c.account_pool_id,
            select(tables.account_pools.c.name)
            .where(tables.account_pools.c.id == ap.c.account_pool_id)
            .scalar_subquery()
            .label("account_pool"),
        )
        .join_from(r, projects, projects.c.id == r.c.project_id)
        .join(users, users.c.id == r.c.dispatched_by)
        .outerjoin(ap, ap.c.id == r.c.profile_id)
        .where(r.c.id == run_id)
    )
    return statement.with_for_update(of=r) if lock else statement


async def run_row(conn: AsyncConnection, run_id: int, *, lock: bool):
    """The row of run ``run_id`` a lease reads (``_run_row``), None for none."""
    return (await conn.execute(_run_row(run_id, lock=lock))).one_or_none()


async def owner_role(conn: AsyncConnection, owner_id: int, project_id: int, *, lock: bool) -> str | None:
    """The role ``owner_id`` holds on ``project_id``, None for none; with ``lock`` the grant's row is share-locked."""
    grants = tables.grants
    statement = select(grants.c.role).where(grants.c.user_id == owner_id, grants.c.project_id == project_id)
    statement = statement.with_for_update(read=True) if lock else statement
    return (await conn.execute(statement)).scalar_one_or_none()


async def rows(conn: AsyncConnection, statement) -> list:
    """The rows of ``statement``, a query of the secrets a run may be leased that ``share_leases`` or
    ``pool_accounts`` builds (``own_secrets``, ``pool_env_accounts``)."""
    return list((await conn.execute(statement)).all())


async def held_tokens(conn: AsyncConnection, run_id: int, worker_id: int) -> list:
    """The GitHub tokens the run holds on this worker, not given back, with REFRESH or more left; the newest first."""
    cl = tables.credential_leases
    statement = (
        select(cl.c.id, cl.c.target, cl.c.expires_at, cl.c.sealed_value, cl.c.nonce, cl.c.key_id)
        .where(
            cl.c.run_id == run_id,
            cl.c.worker_id == worker_id,
            cl.c.provider == "github-app",
            cl.c.revoked_at.is_(None),
            cl.c.sealed_value.is_not(None),
            cl.c.expires_at > func.now() + REFRESH,
        )
        .order_by(cl.c.id.desc())
    )
    return list(await conn.execute(statement))


async def insert_lease(conn: AsyncConnection, **values) -> int:
    cl = tables.credential_leases
    return (await conn.execute(insert(cl).values(**values).returning(cl.c.id))).scalar_one()


async def held_secret_leases(conn: AsyncConnection, run_id: int, worker_id: int) -> dict[tuple[int, str], int]:
    """(secret id, target) -> id of each lease of a secret the run holds on the worker, not given back."""
    cl = tables.credential_leases
    live = select(cl.c.secret_id, cl.c.target, cl.c.id).where(
        cl.c.run_id == run_id,
        cl.c.worker_id == worker_id,
        cl.c.provider == "secret",
        cl.c.revoked_at.is_(None),
    )
    return {(row.secret_id, row.target): row.id for row in await conn.execute(live)}


async def follow_end(conn: AsyncConnection, lease_id: int, expires_at: datetime | None) -> None:
    """The lease ``lease_id`` ends when its secret now does."""
    cl = tables.credential_leases
    await conn.execute(update(cl).where(cl.c.id == lease_id).values(expires_at=expires_at))


async def seal_lease(conn: AsyncConnection, lease_id: int, ciphertext: bytes, nonce: bytes, key_id: str) -> None:
    """Keep the sealed token of the lease ``lease_id`` in its row."""
    cl = tables.credential_leases
    await conn.execute(
        update(cl).values(sealed_value=ciphertext, nonce=nonce, key_id=key_id).where(cl.c.id == lease_id)
    )


async def run_worker(conn: AsyncConnection, run_id: int):
    """The row (worker_id) of run ``run_id``, locked; None for no such run."""
    r = tables.runs
    return (await conn.execute(select(r.c.worker_id).where(r.c.id == run_id).with_for_update())).one_or_none()

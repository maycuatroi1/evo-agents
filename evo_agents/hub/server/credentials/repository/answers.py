"""The queries of an answer to a credential request (``service.answers``): the run's owner and worker, the owner's
secrets for the variable, and the share that lends an admin's secret to one run."""

from __future__ import annotations

from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credential_shares import USER
from evo_agents.hub.server.credentials.models.answers import KIND


async def run_owner(conn: AsyncConnection, run_id: int):
    """(dispatched_by, login) of run ``run_id``."""
    r, u = tables.runs, tables.users
    found = select(r.c.dispatched_by, u.c.login).join_from(r, u, u.c.id == r.c.dispatched_by).where(r.c.id == run_id)
    return (await conn.execute(found)).one()


async def own_worker(conn: AsyncConnection, owner_id: int, run_id: int) -> int | None:
    """The worker run ``run_id`` is on (or was parked on), when it is one of ``owner_id``'s that is not revoked."""
    r, w = tables.runs, tables.workers
    found = (
        select(w.c.id)
        .join_from(r, w, w.c.id == r.c.worker_id)
        .where(r.c.id == run_id, w.c.owner_id == owner_id, w.c.revoked_at.is_(None))
    )
    return (await conn.execute(found)).scalar_one_or_none()


async def same_secret(conn: AsyncConnection, owner_id: int, project_id: int, env_var: str, proposed: str):
    """(name, environment) of the owner's live secret of kind env for ``env_var`` bound to the project, the one named
    ``proposed`` first, else the first by name; None for none."""
    s, b = tables.secrets, tables.secret_bindings
    live = (s.c.owner_id == owner_id, s.c.deleted_at.is_(None))
    bound_here = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    same = (
        select(s.c.name, s.c.environment)
        .where(*live, s.c.kind == KIND, s.c.env_var == env_var, bound_here)
        .order_by(s.c.name != proposed, s.c.name)
        .limit(1)
    )
    return (await conn.execute(same)).one_or_none()


async def add_binding(conn: AsyncConnection, secret_id: int, project_id: int, worker_id: int | None) -> None:
    """Bind the secret to the project on ``worker_id`` too, its other bindings kept."""
    row = {"secret_id": secret_id, "project_id": project_id, "worker_id": worker_id, "pool_id": None}
    await conn.execute(insert(tables.secret_bindings).values(row))


async def named_secret(conn: AsyncConnection, owner_id: int, name: str):
    """(kind, env_var, environment) of the live secret ``name`` of ``owner_id``; None for none."""
    s = tables.secrets
    named = select(s.c.kind, s.c.env_var, s.c.environment).where(
        s.c.owner_id == owner_id, s.c.deleted_at.is_(None), s.c.name == name
    )
    return (await conn.execute(named)).one_or_none()


async def holds_secret(conn: AsyncConnection, owner_id: int, name: str) -> bool:
    """Whether ``owner_id`` keeps a live secret ``name``."""
    s = tables.secrets
    held = select(s.c.id).where(s.c.owner_id == owner_id, s.c.name == name, s.c.deleted_at.is_(None))
    return (await conn.execute(held)).first() is not None


async def run_principal(conn: AsyncConnection, run_id: int):
    """(id, login) of the member run ``run_id`` runs as: its principal, else its dispatcher."""
    r, u = tables.runs, tables.users
    principal = func.coalesce(r.c.principal_id, r.c.dispatched_by)
    return (
        await conn.execute(select(u.c.id, u.c.login).join_from(r, u, u.c.id == principal).where(r.c.id == run_id))
    ).one()


async def live_run_share(conn: AsyncConnection, secret_id: int, run_id: int) -> int | None:
    """The id of the live share of the secret for run ``run_id`` alone; None for none."""
    cs = tables.credential_shares
    live = select(cs.c.id).where(cs.c.secret_id == secret_id, cs.c.run_id == run_id, cs.c.revoked_at.is_(None))
    return (await conn.execute(live)).scalar_one_or_none()


async def insert_run_share(
    conn: AsyncConnection, secret_id: int, grantee_id: int, run_id: int, project_id: int, created_by: int
) -> int:
    """A new share of the secret to ``grantee_id`` for run ``run_id`` alone and its project, with no limits; its id."""
    cs, csp = tables.credential_shares, tables.credential_share_projects
    values = {"secret_id": secret_id, "grantee_kind": USER, "grantee_user_id": grantee_id, "run_id": run_id}
    made = insert(cs).values(**values, created_by=created_by).returning(cs.c.id)
    share_id = (await conn.execute(made)).scalar_one()
    await conn.execute(insert(csp).values(share_id=share_id, project_id=project_id))
    return share_id

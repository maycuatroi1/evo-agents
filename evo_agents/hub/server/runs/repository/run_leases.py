"""The SQL of the owner's view of a run's leases (``evo_agents.hub.server.run_leases``): who dispatched the run, and
every lease it got with the names of its worker, secret, token and profile."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from evo_agents.hub import tables


def _owner_query(run_id: int):
    r, users = tables.runs, tables.users
    return (
        select(r.c.dispatched_by, users.c.login)
        .join_from(r, users, users.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )


def _leases_query(run_id: int):
    """Every lease of run ``run_id``, with the worker's name and the secret's name and kind (none for a GitHub token),
    in the order they were made."""
    cl, w, s, ap, t = tables.credential_leases, tables.workers, tables.secrets, tables.agent_profiles, tables.tokens
    owner = aliased(tables.agent_profiles)  # the agent that owns the secret, when one does
    return (
        select(
            cl.c.id,
            s.c.name.label("secret"),
            cl.c.provider,
            s.c.kind.label("secret_kind"),
            cl.c.target,
            w.c.name.label("worker"),
            cl.c.issued_at,
            cl.c.expires_at,
            cl.c.revoked_at,
            ap.c.handle.label("profile"),
            owner.c.handle.label("agent"),
            cl.c.profile_id,
            cl.c.profile_version,
            t.c.host.label("token_host"),
        )
        .join_from(cl, w, w.c.id == cl.c.worker_id)
        .outerjoin(s, s.c.id == cl.c.secret_id)
        .outerjoin(t, t.c.id == cl.c.token_id)
        .outerjoin(ap, ap.c.id == cl.c.profile_id)
        .outerjoin(owner, owner.c.id == s.c.profile_id)
        .where(cl.c.run_id == run_id)
        .order_by(cl.c.id)
    )


async def run_owner(conn: AsyncConnection, run_id: int) -> tuple[int, str]:
    """(user id, login) of the member who dispatched run ``run_id``."""
    owner_id, login = (await conn.execute(_owner_query(run_id))).one()
    return owner_id, login


async def run_leases(conn: AsyncConnection, run_id: int) -> list:
    return list((await conn.execute(_leases_query(run_id))).all())

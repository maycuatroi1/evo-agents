"""The queries of what the hub finds for a credential request (``service.resolver``): the owner's live secrets of
kind env for a variable, whether each is bound to the run's project on its worker, and the bindings it adds."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.credentials.models.answers import KIND
from evo_agents.hub.server.workers.repository.placement import bound_on


def _live_env(owner_id: int, env_var: str):
    s = tables.secrets
    return (
        s.c.owner_id == owner_id,
        s.c.kind == KIND,
        s.c.env_var == env_var,
        s.c.deleted_at.is_(None),
        or_(s.c.expires_at.is_(None), s.c.expires_at > func.now()),
    )


def _bound_here(project_id: int, worker_id: int | None):
    """A secret of ``tables.secrets`` is bound to the project on the worker (or on any worker of its owner)."""
    s, b = tables.secrets, tables.secret_bindings
    where = [b.c.secret_id == s.c.id, b.c.project_id == project_id]
    if worker_id is None:
        where.append(and_(b.c.worker_id.is_(None), b.c.pool_id.is_(None)))
    else:
        where.append(bound_on(b, worker_id))
    return exists().where(*where)


async def env_secrets(conn: AsyncConnection, owner_id: int, env_var: str, project_id: int, worker_id: int | None):
    """(id, name, kind, env_var, environment, bound) of each live secret of kind env of ``owner_id`` that sets
    ``env_var``; bound: it is bound to the project on the worker."""
    s = tables.secrets
    statement = select(
        s.c.id,
        s.c.name,
        s.c.kind,
        s.c.env_var,
        s.c.environment,
        _bound_here(project_id, worker_id).label("bound"),
    ).where(*_live_env(owner_id, env_var))
    return (await conn.execute(statement)).all()


async def chosen_secrets(conn: AsyncConnection, owner_id: int, env_var: str, project_id: int, run_id: int):
    """(id, name, bound) of each live secret of kind env of ``owner_id`` that sets ``env_var``; bound: it is bound to
    the project on the worker of run ``run_id``."""
    r, s = tables.runs, tables.secrets
    worker_id = (await conn.execute(select(r.c.worker_id).where(r.c.id == run_id))).scalar_one()
    statement = select(s.c.id, s.c.name, _bound_here(project_id, worker_id).label("bound")).where(
        *_live_env(owner_id, env_var)
    )
    return (await conn.execute(statement)).all()


async def add_binding(conn: AsyncConnection, secret_id: int, project_id: int, worker_id: int | None) -> None:
    """Bind the secret to the project on ``worker_id`` (any worker for None) unless it is bound there already."""
    b = tables.secret_bindings
    there = select(b.c.secret_id).where(
        b.c.secret_id == secret_id,
        b.c.project_id == project_id,
        b.c.worker_id.is_(None) if worker_id is None else b.c.worker_id == worker_id,
        b.c.pool_id.is_(None),
    )
    if (await conn.execute(there)).first() is None:
        row = {"secret_id": secret_id, "project_id": project_id, "worker_id": worker_id, "pool_id": None}
        await conn.execute(insert(b).values(row))

"""The queries of worker pools (``tables.worker_pools``, ``tables.worker_pool_projects``), their nodes (workers of a
pool) and their join tokens (``tables.worker_join_tokens``)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.workers.repository.placement import all_labels, columns

# Pools


async def agent_pool_row(conn: AsyncConnection, name: str, *, lock: bool = False):
    """The pool of the live agent ``name`` names, in any case, with the name of the agent's project; None for none."""
    p, ap, projects = tables.worker_pools, tables.agent_profiles, tables.projects
    statement = (
        select(p, projects.c.name.label("project"))
        .join_from(p, ap, ap.c.id == p.c.agent_id)
        .join(projects, projects.c.id == ap.c.project_id)
        .where(func.lower(p.c.name) == name.lower(), ap.c.deleted_at.is_(None))
    )
    if lock:
        statement = statement.with_for_update(of=p)
    return (await conn.execute(statement)).one_or_none()


async def own_pool_row(conn: AsyncConnection, owner_id: int, name: str, *, lock: bool = False):
    """The row of the pool ``name`` of ``owner_id`` that is no agent's, in any case; None for none."""
    p = tables.worker_pools
    statement = select(p).where(p.c.owner_id == owner_id, p.c.agent_id.is_(None), func.lower(p.c.name) == name.lower())
    if lock:
        statement = statement.with_for_update()
    return (await conn.execute(statement)).one_or_none()


async def listed(conn: AsyncConnection, user_id: int, *, admin: bool) -> list:
    """The pools of ``user_id`` and of the agents whose project they administer (every agent's for ``admin``), by
    name."""
    p, ap, g = tables.worker_pools, tables.agent_profiles, tables.grants
    administered = (
        select(ap.c.id)
        .join_from(ap, g, (g.c.project_id == ap.c.project_id) & (g.c.user_id == user_id))
        .where(ap.c.deleted_at.is_(None), g.c.role == "admin")
    )
    if admin:
        administered = select(ap.c.id).where(ap.c.deleted_at.is_(None))
    mine = (p.c.owner_id == user_id) & p.c.agent_id.is_(None)
    statement = select(p).where(mine | p.c.agent_id.in_(administered)).order_by(func.lower(p.c.name))
    return list((await conn.execute(statement)).all())


async def pool_projects(conn: AsyncConnection, pool_id: int) -> dict[str, int]:
    """The projects of a pool, name -> id, by name."""
    pp, projects = tables.worker_pool_projects, tables.projects
    names = (
        select(projects.c.name, projects.c.id)
        .join_from(pp, projects, projects.c.id == pp.c.project_id)
        .where(pp.c.pool_id == pool_id)
        .order_by(projects.c.name)
    )
    return dict((await conn.execute(names)).all())


async def node_rows(conn: AsyncConnection, pool_id: int) -> list:
    """The pool's nodes that are not revoked, newest first, with their placement columns and the database's now."""
    w = tables.workers
    statement = (
        select(
            w.c.id,
            w.c.name,
            w.c.hostname,
            w.c.agent_version,
            all_labels(w).label("labels"),
            w.c.created_at,
            w.c.last_heartbeat_at,
            w.c.drained_at,
            w.c.revoked_at,
            func.now().label("now"),
            *columns(w),
        )
        .where(w.c.pool_id == pool_id, w.c.revoked_at.is_(None))
        .order_by(w.c.created_at.desc(), w.c.id.desc())
    )
    return list(await conn.execute(statement))


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    users = tables.users
    return (await conn.execute(select(users.c.login).where(users.c.id == user_id))).scalar_one()


async def handle_of(conn: AsyncConnection, agent_id: int) -> str:
    ap = tables.agent_profiles
    return (await conn.execute(select(ap.c.handle).where(ap.c.id == agent_id))).scalar_one()


async def name_taken(conn: AsyncConnection, owner_id: int, name: str) -> bool:
    """Whether ``owner_id`` has a pool named ``name`` in any case."""
    p = tables.worker_pools
    taken = select(p.c.id).where(p.c.owner_id == owner_id, func.lower(p.c.name) == name.lower())
    return (await conn.execute(taken)).first() is not None


async def insert_pool(conn: AsyncConnection, owner_id: int, values: dict[str, Any]) -> int | None:
    """A new pool of ``owner_id``; its id, or None when another request took its name meanwhile."""
    p = tables.worker_pools
    try:
        return (await conn.execute(insert(p).values(owner_id=owner_id, **values).returning(p.c.id))).scalar_one()
    except sa_exc.IntegrityError as exc:
        if not isinstance(exc.orig, psycopg.errors.UniqueViolation):
            raise
        return None


async def add_projects(conn: AsyncConnection, pool_id: int, project_ids) -> None:
    pp = tables.worker_pool_projects
    await conn.execute(insert(pp), [{"pool_id": pool_id, "project_id": project_id} for project_id in project_ids])


async def drop_projects(conn: AsyncConnection, pool_id: int, project_ids: list[int]) -> None:
    pp = tables.worker_pool_projects
    await conn.execute(pp.delete().where(pp.c.pool_id == pool_id, pp.c.project_id.in_(project_ids)))


async def live_nodes(conn: AsyncConnection, pool_id: int) -> list[tuple[int, str]]:
    """(id, name) of the pool's nodes that are not revoked, their rows locked as a worker's PATCH locks it."""
    w = tables.workers
    statement = (
        select(w.c.id, w.c.name).where(w.c.pool_id == pool_id, w.c.revoked_at.is_(None)).order_by(w.c.id)
    ).with_for_update()
    return [(row[0], row[1]) for row in await conn.execute(statement)]


async def set_pool(conn: AsyncConnection, pool_id: int, columns: dict[str, Any]) -> None:
    p = tables.worker_pools
    await conn.execute(update(p).values(**columns, updated_at=func.now()).where(p.c.id == pool_id))


async def set_node_slots(conn: AsyncConnection, pool_id: int, slots: int) -> None:
    w = tables.workers
    live = (w.c.pool_id == pool_id) & w.c.revoked_at.is_(None)
    await conn.execute(update(w).values(slots=slots).where(live))


async def delete_pool(conn: AsyncConnection, pool_id: int) -> None:
    """Delete the pool and its join tokens; its nodes stay, in no pool."""
    p, w = tables.worker_pools, tables.workers
    await conn.execute(update(w).values(pool_id=None).where(w.c.pool_id == pool_id))
    await conn.execute(delete(p).where(p.c.id == pool_id))


async def node_names(conn: AsyncConnection, owner_id: int, base: str) -> set[str]:
    """The names, lowercase, of the live workers of ``owner_id`` that start with ``base`` in any case."""
    w = tables.workers
    like = func.lower(w.c.name).startswith(base.lower(), autoescape=True)
    names = select(func.lower(w.c.name)).where(w.c.owner_id == owner_id, w.c.revoked_at.is_(None), like)
    return set((await conn.execute(names)).scalars())


# Join tokens


def tokens_of(pool_id: int):
    """The join tokens of a pool, with the login of their creator and whether they expired."""
    t, users = tables.worker_join_tokens, tables.users
    shown = (
        t.c.id,
        users.c.login.label("created_by"),
        t.c.created_at,
        t.c.expires_at,
        t.c.max_uses,
        t.c.uses,
        t.c.last_used_at,
        t.c.revoked_at,
        (t.c.expires_at <= func.now()).label("expired"),
    )
    return select(*shown).join_from(t, users, users.c.id == t.c.created_by).where(t.c.pool_id == pool_id)


async def token_row(conn: AsyncConnection, pool_id: int, token_id: int):
    t = tables.worker_join_tokens
    return (await conn.execute(tokens_of(pool_id).where(t.c.id == token_id))).one()


async def token_rows(conn: AsyncConnection, pool_id: int) -> list:
    """The join tokens of the pool, newest first."""
    t = tables.worker_join_tokens
    return list(await conn.execute(tokens_of(pool_id).order_by(t.c.created_at.desc(), t.c.id.desc())))


async def insert_token(
    conn: AsyncConnection, pool_id: int, token_hash: str, created_by: int, hours: int, max_uses: int
) -> int:
    t = tables.worker_join_tokens
    statement = insert(t).values(
        pool_id=pool_id,
        token_hash=token_hash,
        created_by=created_by,
        expires_at=func.now() + timedelta(hours=hours),
        max_uses=max_uses,
    )
    return (await conn.execute(statement.returning(t.c.id))).scalar_one()


async def locked_token(conn: AsyncConnection, pool_id: int, token_id: int):
    """(revoked_at,) of join token ``token_id`` of the pool, its row locked; None for none."""
    t = tables.worker_join_tokens
    locked = select(t.c.revoked_at).where(t.c.id == token_id, t.c.pool_id == pool_id).with_for_update()
    return (await conn.execute(locked)).one_or_none()


async def revoke_token(conn: AsyncConnection, token_id: int) -> None:
    t = tables.worker_join_tokens
    await conn.execute(update(t).values(revoked_at=func.now()).where(t.c.id == token_id))


async def take_token(conn: AsyncConnection, token_hash: str):
    """(id, pool_id, name, owner_id, slots) of the live join token whose hash is ``token_hash``, its row and its
    pool's locked, with the join counted against it; None for none, which changes nothing."""
    t, p = tables.worker_join_tokens, tables.worker_pools
    # a node of an agent's pool is the token creator's worker: an admin of the agent's project, while the member who
    # created the agent may hold no grant on it any more
    owner = case((p.c.agent_id.is_(None), p.c.owner_id), else_=t.c.created_by).label("owner_id")
    statement = (
        select(t.c.id, p.c.id.label("pool_id"), p.c.name, owner, p.c.slots)
        .join_from(t, p, p.c.id == t.c.pool_id)
        .where(
            t.c.token_hash == token_hash,
            t.c.revoked_at.is_(None),
            t.c.expires_at > func.now(),
            t.c.uses < t.c.max_uses,
        )
        .with_for_update(of=[t, p])
    )
    row = (await conn.execute(statement)).one_or_none()
    if row is not None:
        await conn.execute(update(t).values(uses=t.c.uses + 1, last_used_at=func.now()).where(t.c.id == row.id))
    return row

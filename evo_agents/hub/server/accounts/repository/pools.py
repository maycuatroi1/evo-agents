"""The SQL of account pools (``evo_agents.hub.server.accounts.service.pools`` says what a pool is): the pools of a
project, their accounts and the agents that use them, and the writes of a pool and its list of accounts."""

from __future__ import annotations

from sqlalchemy import delete, exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.accounts.models.pools import MEMBER_KINDS


async def pools(conn: AsyncConnection, project_id: int) -> list:
    """The account pools of project ``project_id``, by name in any case."""
    p = tables.account_pools
    return list(
        (await conn.execute(select(p).where(p.c.project_id == project_id).order_by(func.lower(p.c.name)))).all()
    )


async def pool_row(conn: AsyncConnection, project_id: int, name: str, *, lock: bool = False):
    """The pool ``name`` (in any case) of project ``project_id``, locked with ``lock``; None when there is none."""
    p = tables.account_pools
    query = select(p).where(p.c.project_id == project_id, func.lower(p.c.name) == name.lower())
    return (await conn.execute(query.with_for_update(of=p) if lock else query)).one_or_none()


async def member_rows(conn: AsyncConnection, pool_ids: list[int]) -> list:
    """The live accounts of the pools ``pool_ids``, by name, with who added them, their owner or agent, their provider
    in the catalog, and their pause, cooldown and last usage."""
    m, s, a, u, owner, ap, pa = (
        tables.account_pool_members,
        tables.secrets,
        tables.accounts,
        tables.users.alias("adder"),
        tables.users.alias("owner"),
        tables.agent_profiles,
        tables.provider_accounts,
    )
    query = (
        select(
            m.c.pool_id,
            m.c.added_at,
            u.c.login.label("added_by"),
            s.c.id,
            s.c.name,
            s.c.kind,
            s.c.env_var,
            s.c.expires_at,
            owner.c.login.label("owner"),
            ap.c.handle.label("agent"),
            pa.c.provider.label("catalog"),
            a.c.paused_at,
            (a.c.cooldown_until > func.now()).label("cooling"),
            a.c.cooldown_until,
            a.c.last_usage,
        )
        .select_from(
            m.join(s, s.c.id == m.c.secret_id)
            .join(u, u.c.id == m.c.added_by)
            .outerjoin(owner, owner.c.id == s.c.owner_id)
            .outerjoin(ap, ap.c.id == s.c.profile_id)
            .outerjoin(pa, pa.c.secret_id == s.c.id)
            .outerjoin(a, a.c.secret_id == s.c.id)
        )
        .where(m.c.pool_id.in_(pool_ids), s.c.deleted_at.is_(None))
        .order_by(s.c.name, s.c.id)
    )
    return list((await conn.execute(query)).all())


async def users_of(conn: AsyncConnection, pool_ids: list[int]) -> list:
    """(account_pool_id, handle) of each agent that uses one of the pools ``pool_ids``, by handle."""
    ap = tables.agent_profiles
    query = select(ap.c.account_pool_id, ap.c.handle).where(ap.c.account_pool_id.in_(pool_ids)).order_by(ap.c.handle)
    return list(await conn.execute(query))


async def logins(conn: AsyncConnection, user_ids: set[int]) -> dict[int, str]:
    """The login of each of the users ``user_ids``."""
    u = tables.users
    return dict((await conn.execute(select(u.c.id, u.c.login).where(u.c.id.in_(user_ids)))).all())


async def name_taken(conn: AsyncConnection, project_id: int, name: str) -> bool:
    """Whether project ``project_id`` has a pool named ``name`` in any case."""
    p = tables.account_pools
    return bool(
        await conn.scalar(select(exists().where(p.c.project_id == project_id, func.lower(p.c.name) == name.lower())))
    )


async def create(conn: AsyncConnection, project_id: int, created_by: int, values: dict) -> int:
    """A new pool of project ``project_id``; its id."""
    p = tables.account_pools
    return (
        await conn.execute(insert(p).values(project_id=project_id, created_by=created_by, **values).returning(p.c.id))
    ).scalar_one()


async def set_policy(conn: AsyncConnection, pool_id: int, values: dict):
    """Pool ``pool_id``'s policy changed to ``values``; its strategy and thresholds after."""
    p = tables.account_pools
    return (
        await conn.execute(
            p.update()
            .where(p.c.id == pool_id)
            .values(**values)
            .returning(p.c.strategy, p.c.hard_reserve_pct, p.c.five_hour_ceiling_pct)
        )
    ).one()


async def agents_using(conn: AsyncConnection, pool_id: int) -> list[str]:
    """The handles of the agents that use pool ``pool_id``."""
    ap = tables.agent_profiles
    return list((await conn.execute(select(ap.c.handle).where(ap.c.account_pool_id == pool_id))).scalars().all())


async def delete_pool(conn: AsyncConnection, pool_id: int) -> None:
    """Pool ``pool_id`` and its list of accounts gone; the accounts stay."""
    m = tables.account_pool_members
    await conn.execute(delete(m).where(m.c.pool_id == pool_id))
    await conn.execute(delete(tables.account_pools).where(tables.account_pools.c.id == pool_id))


def _addable(name: str):
    s = tables.secrets
    return select(s.c.id, s.c.name, s.c.kind).where(
        s.c.name == name, s.c.kind.in_(MEMBER_KINDS), s.c.deleted_at.is_(None)
    )


async def own_secret(conn: AsyncConnection, owner_id: int, name: str):
    """(id, name, kind) of the live env or provider account ``name`` of member ``owner_id``; None when none."""
    s = tables.secrets
    return (await conn.execute(_addable(name).where(s.c.owner_id == owner_id))).one_or_none()


async def agent_secret(conn: AsyncConnection, project_id: int, handle: str, name: str):
    """(id, name, kind) of the live env or provider account ``name`` of agent @``handle`` of project ``project_id``;
    None when none."""
    s, ap = tables.secrets, tables.agent_profiles
    owner = select(ap.c.id).where(ap.c.project_id == project_id, ap.c.handle == handle).scalar_subquery()
    return (await conn.execute(_addable(name).where(s.c.profile_id == owner))).one_or_none()


async def same_named(conn: AsyncConnection, pool_id: int, name: str) -> list[int]:
    """The secret ids of the live accounts named ``name`` pool ``pool_id`` holds."""
    m, s = tables.account_pool_members, tables.secrets
    same = select(m.c.secret_id).join_from(m, s, s.c.id == m.c.secret_id)
    same = same.where(m.c.pool_id == pool_id, s.c.name == name, s.c.deleted_at.is_(None))
    return list((await conn.execute(same)).scalars().all())


async def add_member(conn: AsyncConnection, pool_id: int, secret_id: int, added_by: int) -> None:
    """Secret ``secret_id`` added to pool ``pool_id``."""
    m = tables.account_pool_members
    await conn.execute(insert(m).values(pool_id=pool_id, secret_id=secret_id, added_by=added_by))


async def remove_member(conn: AsyncConnection, pool_id: int, secret_id: int) -> bool:
    """Secret ``secret_id`` out of pool ``pool_id``; False when the pool did not hold it."""
    m = tables.account_pool_members
    gone = await conn.execute(
        delete(m).where(m.c.pool_id == pool_id, m.c.secret_id == secret_id).returning(m.c.secret_id)
    )
    return gone.scalar_one_or_none() is not None


async def attach(conn: AsyncConnection, profile_id: int, pool_id: int | None) -> None:
    """Pool ``pool_id`` (or none) made the pool of agent ``profile_id``."""
    ap = tables.agent_profiles
    await conn.execute(ap.update().values(account_pool_id=pool_id).where(ap.c.id == profile_id))

"""The SQL of a member's accounts and their policies (``evo_agents.hub.server.accounts.service.accounts`` says what
they are): the accounts of a member, those other members share with them, the writes of an account row, and the
policies of a member by project."""

from __future__ import annotations

from sqlalchemy import Text, case, delete, func, select
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import PROVIDER_KIND

# Accounts


def bound_names(target, key):
    """The names of the rows of ``target`` (projects or workers) the secret's bindings name by ``key``, each once and
    sorted, as an array; correlated to the secret of the enclosing query (``secrets.bound_names``, which reaches the
    service of this package through ``admin``)."""
    b = tables.secret_bindings
    names = (
        select(target.c.name)
        .distinct()
        .join_from(b, target, target.c.id == key)
        .where(b.c.secret_id == tables.secrets.c.id)
        .order_by(target.c.name)
    )
    return func.array(names.scalar_subquery(), type_=ARRAY(Text))


def _accounts(owner_id: int, name: str | None = None):
    s, a, b, p = tables.secrets, tables.accounts, tables.secret_bindings, tables.provider_accounts
    statement = (
        select(
            s.c.id,
            s.c.name,
            s.c.env_var,
            p.c.provider.label("gateway_provider"),
            p.c.base_url,
            p.c.api_format,
            func.coalesce(func.jsonb_array_length(p.c.models), 0).label("model_count"),
            bound_names(tables.projects, b.c.project_id).label("projects"),
            bound_names(tables.workers, b.c.worker_id).label("workers"),
            bound_names(tables.worker_pools, b.c.pool_id).label("pools"),
            case((a.c.oauth_sealed.is_not(None), "oauth"), else_="token").label("auth"),
            func.coalesce(a.c.weight, 1).label("weight"),
            func.coalesce(a.c.priority, 0).label("priority"),
            a.c.paused_at,
            a.c.cooldown_until,
            a.c.cooldown_reason,
            a.c.last_leased_at,
            s.c.expires_at,
            a.c.weekly_budget_usd,
            a.c.weekly_reset_at,
            func.now().label("now"),
        )
        .outerjoin_from(s, a, a.c.secret_id == s.c.id)
        .outerjoin(p, p.c.secret_id == s.c.id)
        .where(s.c.owner_id == owner_id, s.c.kind.in_(("env", PROVIDER_KIND)), s.c.deleted_at.is_(None))
        .order_by(s.c.env_var, s.c.name)
    )
    return statement if name is None else statement.where(s.c.name == name)


async def accounts(conn: AsyncConnection, owner_id: int) -> list:
    """The live env secrets and provider accounts of ``owner_id``, by variable and name; never a value."""
    return list((await conn.execute(_accounts(owner_id))).all())


async def account(conn: AsyncConnection, owner_id: int, name: str):
    """``owner_id``'s account ``name``; None when there is none."""
    return (await conn.execute(_accounts(owner_id, name))).one_or_none()


async def covering_shares(conn: AsyncConnection, secret_id: int, shares) -> set[int]:
    """The ids of the shares ``shares`` keeps (live, with a grantee, the service's clauses of ``share_leases``) for
    every run that lend secret ``secret_id``."""
    cs = tables.credential_shares
    covering = select(cs.c.id).where(
        cs.c.secret_id == secret_id,
        *shares,
        cs.c.run_id.is_(None),  # one for a single run lends the account to no run of theirs but that one
    )
    return set((await conn.execute(covering)).scalars())


async def took_one(conn: AsyncConnection, secret_id: int, principal_id: int) -> bool:
    """Whether a run made for ``principal_id`` got a lease of secret ``secret_id``."""
    cl = tables.credential_leases
    took = select(cl.c.id).where(cl.c.secret_id == secret_id, cl.c.principal_id == principal_id).limit(1)
    return (await conn.execute(took)).first() is not None


async def lent(conn: AsyncConnection, principal_id: int, shares) -> list:
    """(id, owner_id, login, share_id, project) of each env account other members lend ``principal_id`` through a
    share for every run that ``shares`` keeps (live, with them as grantee), for each project of that share its owner
    binds it to, by project."""
    s, u, cs, csp, b, p = (
        tables.secrets,
        tables.users,
        tables.credential_shares,
        tables.credential_share_projects,
        tables.secret_bindings,
        tables.projects,
    )
    statement = (
        select(s.c.id, s.c.owner_id, u.c.login, cs.c.id.label("share_id"), p.c.name.label("project"))
        .join_from(cs, s, s.c.id == cs.c.secret_id)
        .join(u, u.c.id == s.c.owner_id)
        .join(csp, csp.c.share_id == cs.c.id)
        .join(p, p.c.id == csp.c.project_id)
        .where(
            *shares,
            cs.c.run_id.is_(None),  # one for a single run lends the account to no run of theirs but that one
            s.c.owner_id != principal_id,
            s.c.kind == "env",
            s.c.deleted_at.is_(None),
            select(b.c.secret_id).where(b.c.secret_id == s.c.id, b.c.project_id == p.c.id).exists(),
        )
        .order_by(p.c.name)
    )
    return list(await conn.execute(statement))


async def write_account(conn: AsyncConnection, secret_id: int, values: dict, paused: bool | None) -> None:
    """The accounts row of secret ``secret_id``, made when missing, with ``values``; ``paused`` true pauses it (when it
    was paused, then), false resumes it, None leaves it."""
    a = tables.accounts

    def pausing(kept):
        return {} if paused is None else {"paused_at": func.coalesce(kept, func.now()) if paused else None}

    made = insert(a).values(secret_id=secret_id, **values, **pausing(None))
    changed = {**values, **pausing(a.c.paused_at), "updated_at": func.now()}
    await conn.execute(made.on_conflict_do_update(index_elements=[a.c.secret_id], set_=changed))


# Policies


def _policies(owner_id: int):
    p, projects = tables.account_policies, tables.projects
    return (
        select(
            projects.c.name.label("project"),
            p.c.strategy,
            p.c.cooldown_seconds,
            p.c.failover,
            p.c.max_failovers,
            p.c.hard_reserve_pct,
            p.c.five_hour_ceiling_pct,
            p.c.updated_at,
        )
        .join_from(p, projects, projects.c.id == p.c.project_id)
        .where(p.c.owner_id == owner_id)
        .order_by(projects.c.name)
    )


async def policies(conn: AsyncConnection, owner_id: int) -> list:
    """The policies of ``owner_id``, by project name."""
    return list(await conn.execute(_policies(owner_id)))


async def policy(conn: AsyncConnection, owner_id: int, project: str):
    """``owner_id``'s policy for the project named ``project``; None when they set none."""
    return (await conn.execute(_policies(owner_id).where(tables.projects.c.name == project))).one_or_none()


async def put_policy(conn: AsyncConnection, owner_id: int, project_id: int, values: dict) -> None:
    """``owner_id``'s policy for ``project_id`` set whole to ``values``."""
    p = tables.account_policies
    made = insert(p).values(owner_id=owner_id, project_id=project_id, **values)
    set_ = {**values, "updated_at": func.now()}
    await conn.execute(made.on_conflict_do_update(index_elements=[p.c.owner_id, p.c.project_id], set_=set_))


async def drop_policy(conn: AsyncConnection, owner_id: int, project_id: int) -> bool:
    """``owner_id``'s policy for ``project_id`` gone; False when they set none."""
    p = tables.account_policies
    dropped = await conn.execute(
        delete(p).where(p.c.owner_id == owner_id, p.c.project_id == project_id).returning(p.c.owner_id)
    )
    return dropped.first() is not None

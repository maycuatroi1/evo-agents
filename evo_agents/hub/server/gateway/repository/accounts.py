"""The queries of the account rules of a run on a gateway model (``service.accounts``): the provider accounts of a
prefix an agent's pool, an agent, or a dispatcher holds, their pacing figures, whether any is known at all, and the
condition of a claim on a daemon's version."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, tables
from evo_agents.hub.credentials import PROVIDER_KIND


def claim_gate(agent_version: str | None):
    """What a daemon of ``agent_version`` may claim of the gateway's runs: all of them from GATEWAY_RUN_AGENT on, as
    every runtime of the core takes one (``pick_runtime`` turns any into one of them); none before."""
    if gateway_runs.takes_gateway_runs(agent_version):
        return true()
    return tables.runs.c.gateway_provider.is_(None)


def of_prefix(prefix: str):
    """The provider accounts whose models ``prefix`` names (a condition on provider_accounts)."""
    p = tables.provider_accounts
    clause = p.c.provider.in_(gateway_runs.providers_of(prefix))
    custom = gateway_runs.custom_id(prefix)
    return clause if custom is None else and_(clause, p.c.secret_id == custom)


async def account_pool(conn: AsyncConnection, profile_id: int | None) -> tuple[bool, int | None]:
    """(whether the asker has an agent, its account pool's id)."""
    if profile_id is None:
        return False, None
    ap = tables.agent_profiles
    row = (await conn.execute(select(ap.c.account_pool_id).where(ap.c.id == profile_id))).first()
    return row is not None, (None if row is None else row.account_pool_id)


def _rows(owner, project_id: int | None, on_worker, prefix: str, *, bound: bool = True):
    """The live provider accounts ``owner`` says, of ``prefix``, bound to the project where ``on_worker`` says."""
    s, p, a, b = tables.secrets, tables.provider_accounts, tables.accounts, tables.secret_bindings
    where_bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id, on_worker)
    where_bound = where_bound if bound else true()  # an account pool's accounts are not bound to the project
    return (
        select(
            s.c.id,
            s.c.name,
            func.coalesce(a.c.paused_at.is_not(None), False).label("paused"),
            func.coalesce(s.c.expires_at <= func.now(), False).label("ended"),
        )
        .join_from(s, p, p.c.secret_id == s.c.id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(owner, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None), where_bound, of_prefix(prefix))
        .order_by(s.c.name, s.c.id)
    )


async def pool_rows(conn: AsyncConnection, pool_id: int, project_id: int, prefix: str) -> list:
    """The provider accounts of ``prefix`` in account pool ``pool_id``, bound or not."""
    s, m = tables.secrets, tables.account_pool_members
    of_pool = s.c.id.in_(select(m.c.secret_id).where(m.c.pool_id == pool_id))
    return list((await conn.execute(_rows(of_pool, project_id, true(), prefix, bound=False))).all())


async def agent_rows(
    conn: AsyncConnection, profile_id: int | None, project_id: int | None, worker_id: int | None, prefix: str
) -> list:
    """An agent's own provider accounts of ``prefix`` bound to its project on worker ``worker_id`` (None: any)."""
    b, s = tables.secret_bindings, tables.secrets
    on_worker = true() if worker_id is None else b.c.worker_id == worker_id
    return list((await conn.execute(_rows(s.c.profile_id == profile_id, project_id, on_worker, prefix))).all())


async def own_rows(conn: AsyncConnection, owner_id: int, project_id: int, worker_id: int | None, prefix: str) -> list:
    """The dispatcher's provider accounts of ``prefix`` bound to the project on any worker or on ``worker_id``."""
    b, s = tables.secret_bindings, tables.secrets
    any_worker = or_(b.c.worker_id.is_(None), true() if worker_id is None else b.c.worker_id == worker_id)
    return list((await conn.execute(_rows(s.c.owner_id == owner_id, project_id, any_worker, prefix))).all())


async def of_prefix_ids(conn: AsyncConnection, ids: list[int], prefix: str) -> set[int]:
    """Those of the secrets ``ids`` that are provider accounts of ``prefix``."""
    p = tables.provider_accounts
    return set((await conn.execute(select(p.c.secret_id).where(p.c.secret_id.in_(ids), of_prefix(prefix)))).scalars())


async def pacing_rows(conn: AsyncConnection, ids: list[int]) -> dict:
    """The pacing figures of the accounts ``ids``, by secret id, with whether each is cooling down now."""
    a = tables.accounts
    query = select(
        a.c.secret_id,
        a.c.weight,
        a.c.priority,
        a.c.rotation,
        a.c.last_usage,
        (a.c.cooldown_until > func.now()).label("cooling"),
    ).where(a.c.secret_id.in_(ids))
    return {row.secret_id: row for row in await conn.execute(query)}


async def known(
    conn: AsyncConnection, owner_id: int, project_id: int, prefix: str, *, agent_owned: bool, profile_id: int | None
) -> bool:
    """Whether ``owner_id``, the agent ``profile_id`` (when ``agent_owned``) or a share for the project has any provider
    account of ``prefix``, usable now or not."""
    s, p, cs, csp = tables.secrets, tables.provider_accounts, tables.credential_shares, tables.credential_share_projects
    shared = exists().where(
        cs.c.secret_id == s.c.id,
        cs.c.revoked_at.is_(None),
        exists().where(csp.c.share_id == cs.c.id, csp.c.project_id == project_id),
    )
    whose = [s.c.owner_id == owner_id, shared]
    if agent_owned:
        whose.append(s.c.profile_id == profile_id)
    statement = select(
        exists().where(
            p.c.secret_id == s.c.id,
            s.c.kind == PROVIDER_KIND,
            s.c.deleted_at.is_(None),
            or_(*whose),
            of_prefix(prefix),
        )
    )
    return bool((await conn.execute(statement)).scalar_one())

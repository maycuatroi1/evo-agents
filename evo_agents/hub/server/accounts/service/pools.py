"""Account pools: ``/v1/projects/{p}/account-pools``, where an admin of the project (or of the hub) keeps a named set
of accounts for the agents of the project, and ``PUT /v1/projects/{p}/agents/{handle}/account-pool`` (in
``agent_profiles``) attaches one to an agent (plan agent-dedicated-pools, criterion a4; ``docs/credentials.md``).

A pool lists accounts, each a secret of kind env (a Claude account is the env account of CLAUDE_CODE_OAUTH_TOKEN) or
provider: an account of the admin's own, which ``PUT .../accounts`` names by its name, or an account of an agent of the
project, which it names with ``agent``. A pool holds a name once. An agent has one account pool, and one pool may serve
many agents. A run of an agent in pool mode leases the model accounts of that pool alone, chosen by weight, priority
and cooldown as any account is (``choice``): when none is usable the run waits in the queue
(``repository.pool_runs.claim_gate``), and nothing of its dispatcher's or of the node's login stands in
(``credentials.survey_run``).

A pool has a policy of its own: the strategy its accounts are chosen by (round-robin by default; quota-pacing among
them, ``evo_agents.hub.accounts.pick``) and the two thresholds of quota-pacing, which ``PATCH .../account-pools/{pool}``
sets. The rest (cooldown, failover) is DEFAULT_POLICY's. Each account shows its last usage with the window of its
weekly budget (``budgets``), as quota-pacing weighs it.

Reading and changing a pool need the admin role on the project or on the hub: a pool names the accounts of its
members. A pool that an agent uses cannot be deleted (409). Each change adds one audit row in the project
(``account-pool.*``), whose target names the pool by its id and an account by its id, never a name or a value.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import has_role
from evo_agents.hub.account_providers import MODEL_ENV_VARS, provider_of
from evo_agents.hub.server import audit
from evo_agents.hub.server.accounts.models.pools import (
    AccountAdd,
    AccountPool,
    PoolAccount,
    PoolPolicy,
    PoolWrite,
)
from evo_agents.hub.server.accounts.repository import pools as repository
from evo_agents.hub.server.accounts.service.budgets import budget_windows, with_budgets
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal

# Reading


def _admin(access: ProjectAccess, user: Principal, doing: str) -> None:
    if not user.admin and not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise Forbidden(f"{doing} account pools of project {access.name} needs the admin role on it; you hold {held}")


async def _admitted(conn: AsyncConnection, user: Principal, project: str, doing: str) -> ProjectAccess:
    access = await project_access(conn, user, project)
    _admin(access, user, doing)
    return access


async def _pool_row(conn: AsyncConnection, access: ProjectAccess, name: str, *, lock: bool = False):
    row = await repository.pool_row(conn, access.project_id, name, lock=lock)
    if row is None:
        raise NotFound(f"project {access.name} has no account pool {name}")
    return row


async def _accounts(conn: AsyncConnection, pool_ids: list[int]) -> dict[int, list[PoolAccount]]:
    found: dict[int, list[PoolAccount]] = {pool_id: [] for pool_id in pool_ids}
    rows = await repository.member_rows(conn, pool_ids)
    ids = {row.id for row in rows}
    usages = with_budgets({row.id: row.last_usage for row in rows}, await budget_windows(conn, ids) if ids else {})
    for row in rows:
        env = row.kind == "env"
        provider = provider_of(row.env_var) if env else (row.catalog or "provider")
        found[row.pool_id].append(
            PoolAccount(
                id=row.id,
                name=row.name,
                kind=row.kind,
                env_var=row.env_var,
                provider=provider,
                category="model" if (not env or row.env_var in MODEL_ENV_VARS) else "service",
                owner=row.owner,
                agent=row.agent,
                paused=row.paused_at is not None,
                cooldown_until=row.cooldown_until if row.cooling else None,
                expires_at=row.expires_at,
                added_by=row.added_by,
                added_at=row.added_at,
                last_usage=usages.get(row.id),
            )
        )
    return found


async def _views(conn: AsyncConnection, access: ProjectAccess, rows) -> list[AccountPool]:
    if not rows:
        return []
    ids = [row.id for row in rows]
    accounts = await _accounts(conn, ids)
    used: dict[int, list[str]] = {pool_id: [] for pool_id in ids}
    for row in await repository.users_of(conn, ids):
        used[row.account_pool_id].append(row.handle)
    logins = await repository.logins(conn, {row.created_by for row in rows})
    return [
        AccountPool(
            id=row.id,
            project=access.name,
            name=row.name,
            strategy=row.strategy,
            hard_reserve_pct=row.hard_reserve_pct,
            five_hour_ceiling_pct=row.five_hour_ceiling_pct,
            created_by=logins[row.created_by],
            created_at=row.created_at,
            accounts=accounts[row.id],
            agents=used[row.id],
        )
        for row in rows
    ]


async def pool_view(conn: AsyncConnection, access: ProjectAccess, name: str) -> AccountPool:
    return (await _views(conn, access, [await _pool_row(conn, access, name)]))[0]


async def _record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name} {target}",
        project_id=access.project_id,
    )


# What the routes do


async def list_pools(conn: AsyncConnection, user: Principal, project: str) -> list[AccountPool]:
    """The account pools of the project, by name, each with its accounts and the agents that use it."""
    access = await _admitted(conn, user, project, "reading")
    return await _views(conn, access, await repository.pools(conn, access.project_id))


async def create(conn: AsyncConnection, user: Principal, project: str, body: PoolWrite) -> tuple[AccountPool, int]:
    """An empty account pool named ``name``, and its id; 409 when the project has one of that name."""
    access = await _admitted(conn, user, project, "making")
    if await repository.name_taken(conn, access.project_id, body.name):
        raise Conflict(f"project {project} has an account pool {body.name} already")
    pool_id = await repository.create(conn, access.project_id, user.user_id, body.model_dump())
    await _record(conn, user, access, audit.ACCOUNT_POOL_CREATE, f"account-pool:{pool_id}")
    return await pool_view(conn, access, body.name), pool_id


async def read(conn: AsyncConnection, user: Principal, project: str, pool: str) -> AccountPool:
    """Pool ``pool`` of the project."""
    access = await _admitted(conn, user, project, "reading")
    return await pool_view(conn, access, pool)


def _policy_target(pool_id: int, row) -> str:
    return (
        f"account-pool:{pool_id} strategy={row.strategy} hard_reserve_pct={row.hard_reserve_pct}"
        f" five_hour_ceiling_pct={row.five_hour_ceiling_pct}"
    )


async def set_policy(conn: AsyncConnection, user: Principal, project: str, pool: str, body: PoolPolicy) -> AccountPool:
    """The pool's strategy and thresholds of quota-pacing set; a field left out stays."""
    access = await _admitted(conn, user, project, "changing")
    row = await _pool_row(conn, access, pool, lock=True)
    values = body.model_dump(exclude_none=True)
    if values:
        changed = await repository.set_policy(conn, row.id, values)
        await _record(conn, user, access, audit.ACCOUNT_POOL_POLICY, _policy_target(row.id, changed))
    return await pool_view(conn, access, pool)


async def delete(conn: AsyncConnection, user: Principal, project: str, pool: str) -> None:
    """The pool and its list of accounts gone (the accounts stay); 409 while an agent uses it."""
    access = await _admitted(conn, user, project, "deleting")
    row = await _pool_row(conn, access, pool, lock=True)
    users = await repository.agents_using(conn, row.id)
    if users:
        raise Conflict(
            f"account pool {row.name} is attached to {', '.join('@' + h for h in sorted(users))}: detach it first"
        )
    await repository.delete_pool(conn, row.id)
    await _record(conn, user, access, audit.ACCOUNT_POOL_DELETE, f"account-pool:{row.id}")


async def _secret_to_add(conn: AsyncConnection, user: Principal, access: ProjectAccess, body: AccountAdd):
    """The secret ``body`` names: the caller's own, or an agent's of this project; 404 when there is none."""
    if body.agent is None:
        found = await repository.own_secret(conn, user.user_id, body.name)
        if found is None:
            raise NotFound(
                f"you have no env or provider account {body.name}: name one of yours, or pass the agent that owns it"
            )
        return found
    found = await repository.agent_secret(conn, access.project_id, body.agent, body.name)
    if found is None:
        raise NotFound(f"agent @{body.agent} of project {access.name} has no account {body.name}")
    return found


async def add(conn: AsyncConnection, user: Principal, project: str, pool: str, body: AccountAdd) -> AccountPool:
    """An account added to the pool; 409 when the pool holds an account of that name already."""
    access = await _admitted(conn, user, project, "changing")
    row = await _pool_row(conn, access, pool, lock=True)
    secret = await _secret_to_add(conn, user, access, body)
    held = await repository.same_named(conn, row.id, secret.name)
    if secret.id in held:
        return await pool_view(conn, access, pool)
    if held:
        raise Conflict(f"account pool {row.name} holds an account named {secret.name} already")
    await repository.add_member(conn, row.id, secret.id, user.user_id)
    await _record(conn, user, access, audit.ACCOUNT_POOL_ADD, f"account-pool:{row.id} secret:{secret.id}")
    return await pool_view(conn, access, pool)


async def remove(conn: AsyncConnection, user: Principal, project: str, pool: str, secret_id: int) -> AccountPool:
    """An account taken out of the pool; 404 when the pool does not hold it."""
    access = await _admitted(conn, user, project, "changing")
    row = await _pool_row(conn, access, pool, lock=True)
    if not await repository.remove_member(conn, row.id, secret_id):
        raise NotFound(f"account pool {row.name} holds no account {secret_id}")
    await _record(conn, user, access, audit.ACCOUNT_POOL_REMOVE, f"account-pool:{row.id} secret:{secret_id}")
    return await pool_view(conn, access, pool)


async def attach(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, profile_id: int, handle: str, name: str | None
) -> None:
    """Make the account pool ``name`` of the project the pool of agent ``profile_id``, or none for ``None``; 404 for a
    pool the project lacks. The caller has checked the role."""
    pool_id = None if name is None else (await _pool_row(conn, access, name)).id
    await repository.attach(conn, profile_id, pool_id)
    target = f"account-pool:{pool_id} agent:@{handle}" if pool_id is not None else f"account-pool:none agent:@{handle}"
    await _record(conn, user, access, audit.ACCOUNT_POOL_ATTACH, target)

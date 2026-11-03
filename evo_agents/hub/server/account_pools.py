"""Account pools: ``/v1/projects/{p}/account-pools``, where an admin of the project (or of the hub) keeps a named set
of accounts for the agents of the project, and ``PUT /v1/projects/{p}/agents/{handle}/account-pool`` (in
``agent_profiles``) attaches one to an agent (plan agent-dedicated-pools, criterion a4; ``docs/credentials.md``).

A pool lists accounts, each a secret of kind env (a Claude account is the env account of CLAUDE_CODE_OAUTH_TOKEN) or
provider: an account of the admin's own, which ``PUT .../accounts`` names by its name, or an account of an agent of the
project, which it names with ``agent``. A pool holds a name once. An agent has one account pool, and one pool may serve
many agents. A run of an agent in pool mode leases the model accounts of that pool alone, chosen by weight, priority
and cooldown as any account is (``account_choice``): when none is usable the run waits in the queue
(``pool_accounts.claim_gate``), and nothing of its dispatcher's or of the node's login stands in (``credentials.survey_run``).

Reading and changing a pool need the admin role on the project or on the hub: a pool names the accounts of its
members. A pool that an agent uses cannot be deleted (409). Each change adds one audit row in the project
(``account-pool.*``), whose target names the pool by its id and an account by its id, never a name or a value.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.account_providers import MODEL_ENV_VARS, provider_of
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.pool_accounts import NEEDS_POOL  # noqa: F401  (the text a refusal reads)
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

POOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
MEMBER_KINDS = ("env", PROVIDER_KIND)
PoolName = Annotated[str, Path(pattern=POOL_NAME.pattern, description="the pool's name, 1 to 40 characters")]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

router = APIRouter(prefix="/v1/projects", tags=["account pools"], responses={401: {"model": ErrorBody}})


class PoolWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=POOL_NAME.pattern, description="1 to 40 characters, unique in the project in any case")


class AccountAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="the name of the account: one of yours, or an agent's")
    agent: str | None = Field(
        None, description="the handle of the agent of the project that owns the account; null: your own account"
    )


class PoolAccount(BaseModel):
    id: int = Field(description="the id of its secret, which audit lines name it by")
    name: str
    kind: str = Field(description="env or provider")
    env_var: str | None = Field(description="the variable an env account sets; null for a provider account")
    provider: str = Field(description="who it signs in to: claude, anthropic, openai ... or a catalog id")
    category: str = Field(description="model: the runtime's own sign-in or a provider account; service: any other")
    owner: str | None = Field(description="the member who owns it; null for an agent's account")
    agent: str | None = Field(description="the agent that owns it; null for a member's account")
    paused: bool
    cooldown_until: datetime | None
    expires_at: datetime | None
    added_by: str
    added_at: datetime


class AccountPool(BaseModel):
    id: int
    project: str
    name: str
    created_by: str
    created_at: datetime
    accounts: list[PoolAccount]
    agents: list[str] = Field(description="the handles of the agents that use it")


# Reading


def _admin(access: ProjectAccess, user: Principal, doing: str) -> None:
    if not user.admin and not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"{doing} account pools of project {access.name} needs the admin role on it; you hold {held}"
        )


async def _pool_row(conn: AsyncConnection, access: ProjectAccess, name: str, *, lock: bool = False):
    p = tables.account_pools
    query = select(p).where(p.c.project_id == access.project_id, func.lower(p.c.name) == name.lower())
    row = (await conn.execute(query.with_for_update(of=p) if lock else query)).one_or_none()
    if row is None:
        raise HTTPException(404, f"project {access.name} has no account pool {name}")
    return row


async def _accounts(conn: AsyncConnection, pool_ids: list[int]) -> dict[int, list[PoolAccount]]:
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
    found: dict[int, list[PoolAccount]] = {pool_id: [] for pool_id in pool_ids}
    for row in await conn.execute(query):
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
            )
        )
    return found


async def _views(conn: AsyncConnection, access: ProjectAccess, rows) -> list[AccountPool]:
    if not rows:
        return []
    ids = [row.id for row in rows]
    accounts = await _accounts(conn, ids)
    ap, u = tables.agent_profiles, tables.users
    used: dict[int, list[str]] = {pool_id: [] for pool_id in ids}
    query = select(ap.c.account_pool_id, ap.c.handle).where(ap.c.account_pool_id.in_(ids)).order_by(ap.c.handle)
    for row in await conn.execute(query):
        used[row.account_pool_id].append(row.handle)
    logins = dict(
        (await conn.execute(select(u.c.id, u.c.login).where(u.c.id.in_({row.created_by for row in rows})))).all()
    )
    return [
        AccountPool(
            id=row.id,
            project=access.name,
            name=row.name,
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


# Routes


@router.get("/{project}/account-pools", response_model=list[AccountPool], responses=REFUSALS)
async def list_account_pools(request: Request, project: ProjectName, user: CurrentUser) -> list[AccountPool]:
    """The account pools of the project, by name, each with its accounts and the agents that use it (an admin of the
    project or of the hub)."""
    p = tables.account_pools
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "reading")
        rows = (
            await conn.execute(select(p).where(p.c.project_id == access.project_id).order_by(func.lower(p.c.name)))
        ).all()
        return await _views(conn, access, rows)


@router.post("/{project}/account-pools", response_model=AccountPool, status_code=201, responses=REFUSALS)
async def create_account_pool(
    request: Request, project: ProjectName, body: PoolWrite, user: CurrentUser
) -> AccountPool:
    """Make an empty account pool named ``name`` (an admin of the project or of the hub); 409 when the project has one
    of that name."""
    p = tables.account_pools
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "making")
        taken = await conn.scalar(
            select(exists().where(p.c.project_id == access.project_id, func.lower(p.c.name) == body.name.lower()))
        )
        if taken:
            raise HTTPException(409, f"project {project} has an account pool {body.name} already")
        pool_id = (
            await conn.execute(
                insert(p)
                .values(project_id=access.project_id, name=body.name, created_by=user.user_id)
                .returning(p.c.id)
            )
        ).scalar_one()
        await _record(conn, user, access, audit.ACCOUNT_POOL_CREATE, f"account-pool:{pool_id}")
        view = await pool_view(conn, access, body.name)
    log.info("account pool made", extra={"project": project, "pool_id": pool_id})
    return view


@router.get("/{project}/account-pools/{pool}", response_model=AccountPool, responses=REFUSALS)
async def get_account_pool(request: Request, project: ProjectName, pool: PoolName, user: CurrentUser) -> AccountPool:
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "reading")
        return await pool_view(conn, access, pool)


@router.delete("/{project}/account-pools/{pool}", status_code=204, responses=REFUSALS)
async def delete_account_pool(request: Request, project: ProjectName, pool: PoolName, user: CurrentUser) -> Response:
    """Delete a pool and its list of accounts (the accounts stay); 409 while an agent uses it."""
    ap, m = tables.agent_profiles, tables.account_pool_members
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "deleting")
        row = await _pool_row(conn, access, pool, lock=True)
        users = (await conn.execute(select(ap.c.handle).where(ap.c.account_pool_id == row.id))).scalars().all()
        if users:
            raise HTTPException(
                409,
                f"account pool {row.name} is attached to {', '.join('@' + h for h in sorted(users))}: detach it first",
            )
        await conn.execute(delete(m).where(m.c.pool_id == row.id))
        await conn.execute(delete(tables.account_pools).where(tables.account_pools.c.id == row.id))
        await _record(conn, user, access, audit.ACCOUNT_POOL_DELETE, f"account-pool:{row.id}")
    return Response(status_code=204)


async def _secret_to_add(conn: AsyncConnection, user: Principal, access: ProjectAccess, body: AccountAdd):
    """The secret ``body`` names: the caller's own, or an agent's of this project; 404 when there is none."""
    s, ap = tables.secrets, tables.agent_profiles
    query = select(s.c.id, s.c.name, s.c.kind).where(
        s.c.name == body.name, s.c.kind.in_(MEMBER_KINDS), s.c.deleted_at.is_(None)
    )
    if body.agent is None:
        found = (await conn.execute(query.where(s.c.owner_id == user.user_id))).one_or_none()
        if found is None:
            raise HTTPException(
                404,
                f"you have no env or provider account {body.name}: name one of yours, or pass the agent that owns it",
            )
        return found
    owner = select(ap.c.id).where(ap.c.project_id == access.project_id, ap.c.handle == body.agent).scalar_subquery()
    found = (await conn.execute(query.where(s.c.profile_id == owner))).one_or_none()
    if found is None:
        raise HTTPException(404, f"agent @{body.agent} of project {access.name} has no account {body.name}")
    return found


@router.put("/{project}/account-pools/{pool}/accounts", response_model=AccountPool, responses=REFUSALS)
async def add_account(
    request: Request, project: ProjectName, pool: PoolName, body: AccountAdd, user: CurrentUser
) -> AccountPool:
    """Add an account to the pool: an env account (a Claude account among them) or a provider account of yours, or of an
    agent of the project. 409 when the pool holds an account of that name already."""
    m, s = tables.account_pool_members, tables.secrets
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "changing")
        row = await _pool_row(conn, access, pool, lock=True)
        secret = await _secret_to_add(conn, user, access, body)
        same = select(m.c.secret_id).join_from(m, s, s.c.id == m.c.secret_id)
        same = same.where(m.c.pool_id == row.id, s.c.name == secret.name, s.c.deleted_at.is_(None))
        held = (await conn.execute(same)).scalars().all()
        if secret.id in held:
            return await pool_view(conn, access, pool)
        if held:
            raise HTTPException(409, f"account pool {row.name} holds an account named {secret.name} already")
        await conn.execute(insert(m).values(pool_id=row.id, secret_id=secret.id, added_by=user.user_id))
        await _record(conn, user, access, audit.ACCOUNT_POOL_ADD, f"account-pool:{row.id} secret:{secret.id}")
        return await pool_view(conn, access, pool)


@router.delete("/{project}/account-pools/{pool}/accounts/{secret_id}", response_model=AccountPool, responses=REFUSALS)
async def remove_account(
    request: Request, project: ProjectName, pool: PoolName, secret_id: int, user: CurrentUser
) -> AccountPool:
    """Take an account out of the pool; the account stays where it is. The runs that hold it keep it."""
    m = tables.account_pool_members
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, user, "changing")
        row = await _pool_row(conn, access, pool, lock=True)
        gone = await conn.execute(
            delete(m).where(m.c.pool_id == row.id, m.c.secret_id == secret_id).returning(m.c.secret_id)
        )
        if gone.scalar_one_or_none() is None:
            raise HTTPException(404, f"account pool {row.name} holds no account {secret_id}")
        await _record(conn, user, access, audit.ACCOUNT_POOL_REMOVE, f"account-pool:{row.id} secret:{secret_id}")
        return await pool_view(conn, access, pool)


async def attach(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, profile_id: int, handle: str, name: str | None
) -> None:
    """Make the account pool ``name`` of the project the pool of agent ``profile_id``, or none for ``None``; 404 for a
    pool the project lacks. The caller has checked the role."""
    ap = tables.agent_profiles
    pool_id = None if name is None else (await _pool_row(conn, access, name)).id
    await conn.execute(ap.update().values(account_pool_id=pool_id).where(ap.c.id == profile_id))
    target = f"account-pool:{pool_id} agent:@{handle}" if pool_id is not None else f"account-pool:none agent:@{handle}"
    await _record(conn, user, access, audit.ACCOUNT_POOL_ATTACH, target)


# What a run reads

"""A member's accounts and their policies: GET and PATCH under /v1/accounts, GET, PUT and DELETE under
/v1/account-policies (``docs/credentials.md``, section "Accounts"; ``evo_agents.hub.accounts`` holds the rules).

An account is one of the caller's env secrets (``evo_agents.hub.server.secrets``) seen as one a run may take among
several for the same variable. GET /v1/accounts lists the caller's own, live: name, variable, bindings, weight,
priority, when it was paused, until when it cools down and why, when a run last took it, its end, its state
(``accounts.ACCOUNT_STATES``) and its figures (``account_views``: its last usage, its runs and their cost over 24
hours and 7 days, the runs holding it, the same by principal) and its shares not revoked (``share_views``); never a
value. GET /v1/accounts/{name} gives one of them; with ``?owner=<login>``, one another member shared with the caller,
whose figures are those of the caller's own runs alone and whose shares are those that lend it to the caller (a share
not revoked covers them, or a run of theirs took it). PATCH
/v1/accounts/{name} changes its weight or priority, or pauses (``paused: true``) or resumes it; a name that is not one
of the caller's live env secrets is 404, whoever's it is.

A policy is the caller's for one project: GET /v1/account-policies lists the caller's own; GET
/v1/account-policies/{project} gives the caller's policy for a project they can see, the defaults
(``accounts.DEFAULT_POLICY``, ``updated_at`` null) when they set none; PUT sets it whole, and needs the writer role on
the project, as binding a secret to it does; DELETE drops it, the defaults holding again.

Every member sees and changes their own accounts and policies alone, a hub admin too: the routes take the caller's
id, never a login. Each write adds one audit row (``audit.ACCOUNT_SET``, ``ACCOUNT_POLICY_PUT``,
``ACCOUNT_POLICY_DELETE``) naming the secret by its id (``secret:<id>``) and a policy by its project, never a value.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.account_providers import Category, Provider, category_of, provider_of
from evo_agents.hub.accounts import (
    ACCOUNT_STATES,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_MAX_FAILOVERS,
    DEFAULT_POLICY,
    DEFAULT_STRATEGY,
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PRIORITY,
    MAX_WEIGHT,
    MIN_COOLDOWN_SECONDS,
    STRATEGIES,
    account_state,
)
from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.server import audit, org_store, route_autonomy
from evo_agents.hub.server.account_choice import RELOGIN_REASON
from evo_agents.hub.server.account_views import AccountUsage, Figures, HeldBy, PrincipalCost, account_figures, figured
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.secrets import audit_target, bound_names
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.share_leases import grantee_clause, live_share
from evo_agents.hub.server.share_views import CredentialShare, shares_of

log = logging.getLogger(__name__)

NO_ACCOUNT = "you have no env secret named {name} on this hub: see GET /v1/accounts"
AccountName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="one of the caller's env secrets")]
OwnerLogin = Annotated[
    str | None,
    Query(pattern=LOGIN_NAME, description="the member who shared the account with the caller; omit for one's own"),
]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}

router = APIRouter(
    prefix="/v1", tags=["accounts"], dependencies=[route_autonomy.K8], responses={401: {"model": ErrorBody}}
)


# Models


class Account(BaseModel):
    """One of the caller's env secrets as an account: everything but its value."""

    name: str
    env_var: str = Field(description="the variable it sets")
    provider: Provider = Field(description="who it signs in to, read from its variable (``account_providers``)")
    category: Category = Field(description="model: the runtime's own sign-in, whose runs cost; service: any other")
    projects: list[str] = Field(description="the projects whose runs may take it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    auth: Literal["token", "oauth"] = Field(
        description="oauth: it signed in with OAuth and the hub keeps its refresh token sealed; token: a token as given"
    )
    weight: int = Field(description=f"0 to {MAX_WEIGHT}: its share in a weighted-round-robin pool; 0 leaves it")
    priority: int = Field(description=f"0 to {MAX_PRIORITY}: the lower, the sooner a fill-first pool takes it")
    paused_at: datetime | None = Field(description="when its owner paused it; null while it may be taken")
    cooldown_until: datetime | None = Field(description="until when it cools down after a usage limit")
    cooldown_reason: str | None = Field(description="why it cools down")
    last_leased_at: datetime | None = Field(description="when a run last took it")
    expires_at: datetime | None
    state: Literal[ACCOUNT_STATES] = Field(
        description="ready, cooling, paused, expired or needs-relogin (an OAuth account whose refresh token was "
        "refused: run oauth-login again), now"
    )
    last_usage: AccountUsage | None = Field(description="what a run's CLI last said of its usage; null before any")
    last_usage_at: datetime | None = Field(description="when a run reported that usage")
    runs_24h: int = Field(description="the runs that took it in the last 24 hours")
    runs_7d: int = Field(description="the runs that took it in the last 7 days")
    cost_24h_usd: float | None = Field(
        description="what those runs cost in the last 24 hours, in USD, from their usage; null for a service account"
    )
    cost_7d_usd: float | None = Field(
        description="what those runs cost in the last 7 days, in USD, an estimate; null for a service account"
    )
    held_by: list[HeldBy] = Field(description="the runs a worker holds now with it")
    shares: list[CredentialShare] = Field(description="its shares not revoked, newest first")
    cost_by_principal: list[PrincipalCost] = Field(
        description="its runs and their cost by the member each was made for; for a member it was shared with, theirs"
    )


class AccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 0 leaves it")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner in a fill-first pool")
    paused: bool | None = Field(None, description="true pauses it, so no new run takes it; false resumes it")


class AccountPolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal[STRATEGIES] = Field(DEFAULT_STRATEGY, description="how a run picks among the accounts")
    cooldown_seconds: int = Field(
        DEFAULT_COOLDOWN_SECONDS,
        ge=MIN_COOLDOWN_SECONDS,
        le=MAX_COOLDOWN_SECONDS,
        description="how long an account cools down after a usage limit that gave no reset time",
    )
    failover: bool = Field(True, description="whether a run that hits a usage limit goes on with another account")
    max_failovers: int = Field(
        DEFAULT_MAX_FAILOVERS, ge=0, le=MAX_FAILOVERS, description="at most how many times a run moves on"
    )


class AccountPolicy(AccountPolicyWrite):
    project: str
    updated_at: datetime | None = Field(description="when the caller set it; null for the defaults")


# Accounts


def _accounts(owner_id: int, name: str | None = None):
    s, a, b = tables.secrets, tables.accounts, tables.secret_bindings
    statement = (
        select(
            s.c.id,
            s.c.name,
            s.c.env_var,
            bound_names(tables.projects, b.c.project_id).label("projects"),
            bound_names(tables.workers, b.c.worker_id).label("workers"),
            case((a.c.oauth_sealed.is_not(None), "oauth"), else_="token").label("auth"),
            func.coalesce(a.c.weight, 1).label("weight"),
            func.coalesce(a.c.priority, 0).label("priority"),
            a.c.paused_at,
            a.c.cooldown_until,
            a.c.cooldown_reason,
            a.c.last_leased_at,
            s.c.expires_at,
            func.now().label("now"),
        )
        .outerjoin_from(s, a, a.c.secret_id == s.c.id)
        .where(s.c.owner_id == owner_id, s.c.kind == "env", s.c.deleted_at.is_(None))
        .order_by(s.c.env_var, s.c.name)
    )
    return statement if name is None else statement.where(s.c.name == name)


def _account(row, figures: Figures | None = None, shares: list[CredentialShare] | None = None) -> Account:
    values = dict(row._mapping)
    del values["id"]
    now = values.pop("now")
    oauth = row.auth == "oauth"
    # The end of an OAuth account is its access token's, which the hub renews when a run leases it: not an expiry.
    state = account_state(row.paused_at, row.cooldown_until, None if oauth else row.expires_at, now)
    if oauth and state == "cooling" and (row.cooldown_reason or "").startswith(RELOGIN_REASON):
        state = "needs-relogin"
    provider = provider_of(row.env_var)
    category = category_of(provider)
    shared = shares or []
    return Account(
        **values, provider=provider, category=category, state=state, shares=shared, **figured(figures, category)
    )


@router.get("/accounts", response_model=list[Account])
async def list_accounts(request: Request, user: CurrentUser, response: Response) -> list[Account]:
    """The caller's own env secrets as accounts, by variable and name, without their values, with their figures
    (``account_views``); a hub admin's own too."""
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(_accounts(user.user_id))).all()
        figures = await account_figures(conn, user.user_id)
        shares = await shares_of(conn, [row.id for row in rows])
    response.headers.update(NO_STORE)
    return [_account(row, figures.get(row.id), shares.get(row.id)) for row in rows]


@router.get("/accounts/{name}", response_model=Account, responses=REFUSALS)
async def get_account(
    request: Request, name: AccountName, user: CurrentUser, response: Response, owner: OwnerLogin = None
) -> Account:
    """The caller's account ``name`` with its figures, or with ``owner`` the one that member shared with the caller;
    404 for a name that is neither."""
    async with request.app.state.engine.begin() as conn:
        found = await (_own_account(conn, user, name) if owner is None else _shared_account(conn, user, owner, name))
    response.headers.update(NO_STORE)
    return found


async def _own_account(conn: AsyncConnection, user: Principal, name: str) -> Account:
    row = (await conn.execute(_accounts(user.user_id, name))).one_or_none()
    if row is None:
        raise HTTPException(404, NO_ACCOUNT.format(name=name))
    figures = await account_figures(conn, user.user_id, [row.id])
    return _account(row, figures.get(row.id), (await shares_of(conn, [row.id])).get(row.id))


async def _shared_account(conn: AsyncConnection, user: Principal, owner: str, name: str) -> Account:
    """Account ``name`` of ``owner`` as the caller, whom it was shared with, sees it: their runs alone, and the shares
    that lend it to them; 404 when no share not revoked covers them and no run of theirs took it."""
    cs, cl = tables.credential_shares, tables.credential_leases
    member = await org_store.user_by_login(conn, owner)
    if member is not None and member.id == user.user_id:
        return await _own_account(conn, user, name)
    owner_id = None if member is None else member.id
    row = None if owner_id is None else (await conn.execute(_accounts(owner_id, name))).one_or_none()
    refusal = HTTPException(404, f"{owner} shared no account named {name} with you")
    if row is None:
        raise refusal
    covering = select(cs.c.id).where(cs.c.secret_id == row.id, live_share(), await grantee_clause(conn, user.user_id))
    ids = set((await conn.execute(covering)).scalars())
    took = select(cl.c.id).where(cl.c.secret_id == row.id, cl.c.principal_id == user.user_id).limit(1)
    if not ids and (await conn.execute(took)).first() is None:
        raise refusal
    figures = await account_figures(conn, owner_id, [row.id], principal_id=user.user_id)
    shares = [share for share in (await shares_of(conn, [row.id])).get(row.id, []) if share.id in ids]
    return _account(row, figures.get(row.id), shares).model_copy(update={"workers": []})


def _paused(change: AccountChange, kept):
    return {} if change.paused is None else {"paused_at": func.coalesce(kept, func.now()) if change.paused else None}


@router.patch("/accounts/{name}", response_model=Account, responses=REFUSALS)
async def change_account(
    request: Request, name: AccountName, body: AccountChange, user: CurrentUser, response: Response
) -> Account:
    """Change the weight or priority of the caller's account ``name``, or pause or resume it."""
    a = tables.accounts
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(_accounts(user.user_id, name))).one_or_none()
        if row is None:
            raise HTTPException(404, NO_ACCOUNT.format(name=name))
        values = body.model_dump(include={"weight", "priority"}, exclude_none=True)
        made = insert(a).values(secret_id=row.id, **values, **_paused(body, None))
        changed = {**values, **_paused(body, a.c.paused_at), "updated_at": func.now()}
        await conn.execute(made.on_conflict_do_update(index_elements=[a.c.secret_id], set_=changed))
        figures = await account_figures(conn, user.user_id, [row.id])
        shares = await shares_of(conn, [row.id])
        found = _account(
            (await conn.execute(_accounts(user.user_id, name))).one(), figures.get(row.id), shares.get(row.id)
        )
        paused = "yes" if found.paused_at is not None else "no"
        target = f"{audit_target(row.id)} weight={found.weight} priority={found.priority} paused={paused}"
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.ACCOUNT_SET, target=target)
    log.info("account changed", extra={"secret": name, "login": user.login, "state": found.state})
    response.headers.update(NO_STORE)
    return found


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
            p.c.updated_at,
        )
        .join_from(p, projects, projects.c.id == p.c.project_id)
        .where(p.c.owner_id == owner_id)
        .order_by(projects.c.name)
    )


async def _policy(conn: AsyncConnection, user: Principal, project: str) -> AccountPolicy:
    row = (await conn.execute(_policies(user.user_id).where(tables.projects.c.name == project))).one_or_none()
    if row is None:
        return AccountPolicy(project=project, updated_at=None, **DEFAULT_POLICY)
    return AccountPolicy(**row._mapping)


@router.get("/account-policies", response_model=list[AccountPolicy])
async def list_account_policies(request: Request, user: CurrentUser, response: Response) -> list[AccountPolicy]:
    """The caller's own policies, by project; a project without one has the defaults."""
    async with request.app.state.engine.begin() as conn:
        found = [AccountPolicy(**row._mapping) for row in await conn.execute(_policies(user.user_id))]
    response.headers.update(NO_STORE)
    return found


@router.get("/account-policies/{project}", response_model=AccountPolicy, responses=REFUSALS)
async def get_account_policy(request: Request, project: ProjectName, user: CurrentUser) -> AccountPolicy:
    """The caller's policy for ``project``, the defaults when they set none."""
    async with request.app.state.engine.begin() as conn:
        await project_access(conn, user, project)
        return await _policy(conn, user, project)


def _policy_target(project: str, body: AccountPolicyWrite) -> str:
    failover = "yes" if body.failover else "no"
    return (
        f"{project} strategy={body.strategy} cooldown={body.cooldown_seconds} failover={failover} "
        f"max_failovers={body.max_failovers}"
    )


@router.put("/account-policies/{project}", response_model=AccountPolicy, responses=REFUSALS)
async def put_account_policy(
    request: Request, project: ProjectName, body: AccountPolicyWrite, user: CurrentUser
) -> AccountPolicy:
    """Set the caller's policy for ``project`` whole; the writer role on it."""
    p = tables.account_policies
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "writer"):
            role = access.role or "no role"
            raise HTTPException(403, f"a policy for project {project} needs the writer role on it; you hold {role}")
        values = body.model_dump()
        made = insert(p).values(owner_id=user.user_id, project_id=access.project_id, **values)
        set_ = {**values, "updated_at": func.now()}
        await conn.execute(made.on_conflict_do_update(index_elements=[p.c.owner_id, p.c.project_id], set_=set_))
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.ACCOUNT_POLICY_PUT,
            target=_policy_target(project, body),
            project_id=access.project_id,
        )
        found = await _policy(conn, user, project)
    log.info("account policy set", extra={"project": project, "login": user.login, "strategy": body.strategy})
    return found


@router.delete("/account-policies/{project}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_account_policy(request: Request, project: ProjectName, user: CurrentUser) -> Response:
    """Drop the caller's policy for ``project``: the defaults hold again. 404 when they set none."""
    p = tables.account_policies
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        dropped = await conn.execute(
            delete(p).where(p.c.owner_id == user.user_id, p.c.project_id == access.project_id).returning(p.c.owner_id)
        )
        if dropped.first() is None:
            raise HTTPException(404, f"you set no policy for project {project}: the defaults hold")
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.ACCOUNT_POLICY_DELETE,
            target=project,
            project_id=access.project_id,
        )
    log.info("account policy dropped", extra={"project": project, "login": user.login})
    return Response(status_code=204)

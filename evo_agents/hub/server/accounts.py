"""A member's accounts and their policies: GET and PATCH under /v1/accounts, GET, PUT and DELETE under
/v1/account-policies (``docs/credentials.md``, section "Accounts"; ``evo_agents.hub.accounts`` holds the rules).

An account is one of the caller's env secrets (``evo_agents.hub.server.secrets``) seen as one a run may take among
several for the same variable. GET /v1/accounts lists the caller's own, live: name, variable, bindings, weight,
priority, when it was paused, until when it cools down and why, when a run last took it, its end, its state
(``accounts.ACCOUNT_STATES``) and its figures (``account_views``: its last usage, its runs and their cost over 24
hours and 7 days, the runs holding it, the same by principal) and its shares not revoked (``share_views``); never a
value. A provider account (``provider_accounts``) is listed too, its variable read ``gateway:<prefix>``, with
``gateway``: its catalog provider, the prefix of its models, the host of its base URL, its API format, how many models
it serves and what the model gateway last said of it (cooling while a rate limit lasts, failing when its last test
failed, with the gateway's error code alone as the reason, never its text). GET /v1/accounts/{name} gives one of them; with ``?owner=<login>``, one another member shared with the caller,
whose figures are those of the caller's own runs alone and whose shares are those that lend it to the caller (a share
not revoked covers them, or a run of theirs took it). GET /v1/shared-accounts lists the env accounts other members
share with the caller for every run, with the projects their shares lend them on, as the account picker of a dispatch
offers them by ``<login>/<name>`` (``runs.service.accounts``). PATCH /v1/accounts/{name} changes its weight or
priority, pauses (``paused: true``) or resumes it, or gives it a weekly budget (``weekly_budget_usd``, null drops it,
with ``weekly_reset_at``, the instant its weeks start from), which the hub turns into its ``seven_day`` usage window
(``account_budgets``); a name that is not one of the caller's live env secrets is 404, whoever's it is.

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
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.account_providers import Category, Provider, category_of, provider_of
from evo_agents.hub.accounts import (
    ACCOUNT_STATES,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FIVE_HOUR_CEILING_PCT,
    DEFAULT_HARD_RESERVE_PCT,
    DEFAULT_MAX_FAILOVERS,
    DEFAULT_POLICY,
    DEFAULT_STRATEGY,
    MAX_COOLDOWN_SECONDS,
    MAX_FAILOVERS,
    MAX_PCT,
    MAX_PRIORITY,
    MAX_WEIGHT,
    MIN_COOLDOWN_SECONDS,
    QUOTA_PACING,
    STRATEGIES,
    account_state,
)
from evo_agents.hub.credentials import PROVIDER_KIND, SECRET_NAME
from evo_agents.hub.gateway_runs import PICK_PREFIX
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.account_choice import RELOGIN_REASON, end_cooldown
from evo_agents.hub.server.account_views import AccountUsage, Figures, HeldBy, PrincipalCost, account_figures, figured
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.credentials.repository.secrets import bound_names
from evo_agents.hub.server.credentials.service.secrets import audit_target
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
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
FAILING = ("error", "unavailable", "expired", "banned", "credits_exhausted")  # testStatus of a connection that fails
MAX_BUDGET_USD = 1_000_000
REASON, MAX_REASON = re.compile(r"[^A-Za-z0-9_.:-]+"), 80  # a reason as shown: a code, never the provider's text

router = APIRouter(
    prefix="/v1", tags=["accounts"], dependencies=[route_autonomy.K8], responses={401: {"model": ErrorBody}}
)


# Models


class GatewayAccount(BaseModel):
    """What a provider account calls, through the model gateway (``evo_agents.hub.gateway_runs``)."""

    provider: str = Field(description="the id of its entry in the catalog")
    prefix: str | None = Field(description="what its models are named with, <prefix>/<model>; null when not served")
    base_url_host: str | None = Field(description="the host of the endpoint it calls")
    api_format: str
    models: int = Field(description="how many models it serves")
    status: Literal["ok", "cooling", "failing", "unknown"] = Field(
        description="what the gateway last said of it: ok, cooling after a rate limit, failing, or unknown before any"
    )
    reason: str | None = Field(description="the gateway's error code or type when cooling or failing; never its text")


class Account(BaseModel):
    """One of the caller's env secrets or provider accounts as an account: everything but its value or key."""

    name: str
    env_var: str = Field(description="the variable it sets")
    provider: Provider = Field(description="who it signs in to, read from its variable (``account_providers``)")
    category: Category = Field(description="model: the runtime's own sign-in, whose runs cost; service: any other")
    projects: list[str] = Field(description="the projects whose runs may take it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    pools: list[str] = Field([], description="the worker pools it is bound to, each node of each")
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
    gateway: GatewayAccount | None = Field(
        None, description="a provider account's endpoint and what the model gateway last said of it; null for a secret"
    )
    weekly_budget_usd: float | None = Field(
        None, description="the USD its owner gives it a week, turned into its seven_day window; null for none"
    )
    weekly_reset_at: datetime | None = Field(
        None, description="an instant its budget's weeks start from, every 7 days; null: Monday 00:00 UTC"
    )


class SharedAccount(Account):
    """An env account another member shares with the caller, as GET /v1/accounts/{name}?owner= gives it: its figures
    are the caller's own runs alone, its shares those that lend it to the caller, and its projects those of these
    shares its owner binds it to."""

    owner: str = Field(description="the login of the member who owns it and shares it")
    pick: str = Field(description="how ``account`` of a dispatch names it: <owner>/<name>")


class AccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 0 leaves it")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner in a fill-first pool")
    paused: bool | None = Field(None, description="true pauses it, so no new run takes it; false resumes it")
    weekly_budget_usd: float | None = Field(
        None, gt=0, le=MAX_BUDGET_USD, description="the USD it may spend a week, for a model account; null drops it"
    )
    weekly_reset_at: AwareDatetime | None = Field(
        None, description="an instant its budget's weeks start from, every 7 days; null: Monday 00:00 UTC"
    )


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
    hard_reserve_pct: int = Field(
        DEFAULT_HARD_RESERVE_PCT,
        ge=0,
        le=MAX_PCT,
        description="quota-pacing: the share of a weekly window kept back while another account has more",
    )
    five_hour_ceiling_pct: int = Field(
        DEFAULT_FIVE_HOUR_CEILING_PCT,
        ge=1,
        le=MAX_PCT,
        description="quota-pacing: the five-hour use above which an account waits while a cooler one is ready",
    )


class AccountPolicy(AccountPolicyWrite):
    project: str
    updated_at: datetime | None = Field(description="when the caller set it; null for the defaults")


# Accounts


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


def _gateway(row, seen: dict | None) -> GatewayAccount:
    """The gateway part of provider account ``row``, with ``seen``, the gateway's connections by secret id
    (``gateway_sync.GatewaySync.status``): cooling while its rate limit lasts, failing when its last test failed."""
    found = (seen or {}).get(row.id)
    status, reason = ("unknown", None) if found is None else ("ok", None)
    if found is not None:
        why = found.error_code or found.error_type
        if (_until(found.limited_until) or row.now) > row.now:
            status, reason = "cooling", why or "rate limited"
        elif found.test_status in FAILING:
            status, reason = "failing", why or found.test_status
    return GatewayAccount(
        provider=row.gateway_provider,
        prefix=gateway_runs.prefix_of(row.gateway_provider, row.id),
        base_url_host=urlsplit(row.base_url).hostname,
        api_format=row.api_format,
        models=row.model_count,
        status=status,
        reason=None if reason is None else REASON.sub("-", str(reason))[:MAX_REASON],
    )


def _until(when: str | None) -> datetime | None:
    """The moment a connection rests until (ISO 8601, UTC when it names no zone); None when there is none."""
    try:
        found = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=UTC)


def _account(
    row, figures: Figures | None = None, shares: list[CredentialShare] | None = None, seen: dict | None = None
) -> Account:
    values = dict(row._mapping)
    for key in ("id", "gateway_provider", "base_url", "api_format", "model_count"):
        del values[key]
    now = values.pop("now")
    budget = values.pop("weekly_budget_usd")
    values["weekly_budget_usd"] = None if budget is None else float(budget)
    oauth = row.auth == "oauth"
    # The end of an OAuth account is its access token's, which the hub renews when a run leases it: not an expiry.
    state = account_state(row.paused_at, row.cooldown_until, None if oauth else row.expires_at, now)
    if oauth and state == "cooling" and (row.cooldown_reason or "").startswith(RELOGIN_REASON):
        state = "needs-relogin"
    gateway = None if row.gateway_provider is None else _gateway(row, seen)
    if gateway is not None:
        values["env_var"] = f"{PICK_PREFIX}{gateway.prefix or row.gateway_provider}"
        if state == "ready" and gateway.status == "cooling":
            state, values["cooldown_until"] = "cooling", _until(seen[row.id].limited_until)
            values["cooldown_reason"] = f"the model gateway: {gateway.reason}"
    provider = "other" if gateway is not None else provider_of(row.env_var)
    category = "model" if gateway is not None else category_of(provider)
    shared = shares or []
    return Account(
        **values,
        provider=provider,
        category=category,
        state=state,
        shares=shared,
        gateway=gateway,
        **figured(figures, category),
    )


def _seen(request: Request) -> dict | None:
    """The gateway's connections of the last round, by secret id; None when the hub runs no gateway."""
    found = getattr(request.app.state, "gateway_sync", None)
    return None if found is None else found.status


@router.get("/accounts", response_model=list[Account])
async def list_accounts(request: Request, user: CurrentUser, response: Response) -> list[Account]:
    """The caller's own env secrets as accounts, by variable and name, without their values, with their figures
    (``account_views``); a hub admin's own too."""
    async with request.app.state.engine.begin() as conn:
        rows = (await conn.execute(_accounts(user.user_id))).all()
        figures = await account_figures(conn, user.user_id)
        shares = await shares_of(conn, [row.id for row in rows])
    response.headers.update(NO_STORE)
    seen = _seen(request)
    return [_account(row, figures.get(row.id), shares.get(row.id), seen) for row in rows]


@router.get("/accounts/{name}", response_model=Account, responses=REFUSALS)
async def get_account(
    request: Request, name: AccountName, user: CurrentUser, response: Response, owner: OwnerLogin = None
) -> Account:
    """The caller's account ``name`` with its figures, or with ``owner`` the one that member shared with the caller;
    404 for a name that is neither."""
    async with request.app.state.engine.begin() as conn:
        seen = _seen(request)
        own = owner is None
        found = await (_own_account(conn, user, name, seen) if own else _shared_account(conn, user, owner, name, seen))
    response.headers.update(NO_STORE)
    return found


async def _own_account(conn: AsyncConnection, user: Principal, name: str, seen: dict | None = None) -> Account:
    row = (await conn.execute(_accounts(user.user_id, name))).one_or_none()
    if row is None:
        raise HTTPException(404, NO_ACCOUNT.format(name=name))
    figures = await account_figures(conn, user.user_id, [row.id])
    return _account(row, figures.get(row.id), (await shares_of(conn, [row.id])).get(row.id), seen)


async def _shared_account(
    conn: AsyncConnection, user: Principal, owner: str, name: str, seen: dict | None = None
) -> Account:
    """Account ``name`` of ``owner`` as the caller, whom it was shared with, sees it: their runs alone, and the shares
    that lend it to them; 404 when no share not revoked covers them and no run of theirs took it."""
    cs, cl = tables.credential_shares, tables.credential_leases
    member = await org_store.user_by_login(conn, owner)
    if member is not None and member.id == user.user_id:
        return await _own_account(conn, user, name, seen)
    owner_id = None if member is None else member.id
    row = None if owner_id is None else (await conn.execute(_accounts(owner_id, name))).one_or_none()
    refusal = HTTPException(404, f"{owner} shared no account named {name} with you")
    if row is None:
        raise refusal
    covering = select(cs.c.id).where(
        cs.c.secret_id == row.id,
        live_share(),
        cs.c.run_id.is_(None),  # one for a single run lends the account to no run of theirs but that one
        await grantee_clause(conn, user.user_id),
    )
    ids = set((await conn.execute(covering)).scalars())
    took = select(cl.c.id).where(cl.c.secret_id == row.id, cl.c.principal_id == user.user_id).limit(1)
    if not ids and (await conn.execute(took)).first() is None:
        raise refusal
    figures = await account_figures(conn, owner_id, [row.id], principal_id=user.user_id)
    shares = [share for share in (await shares_of(conn, [row.id])).get(row.id, []) if share.id in ids]
    return _account(row, figures.get(row.id), shares, seen).model_copy(update={"workers": [], "pools": []})


@router.get("/shared-accounts", response_model=list[SharedAccount])
async def list_shared_accounts(request: Request, user: CurrentUser, response: Response) -> list[SharedAccount]:
    """The env accounts other members share with the caller through a live share for every run, by variable, owner
    and name, as ``_shared_account`` gives each one, with the projects of those shares its owner binds it to; one bound
    to none of them is left out."""
    s, u, cs, csp, b, p = (
        tables.secrets,
        tables.users,
        tables.credential_shares,
        tables.credential_share_projects,
        tables.secret_bindings,
        tables.projects,
    )
    async with request.app.state.engine.begin() as conn:
        statement = (
            select(s.c.id, s.c.owner_id, u.c.login, cs.c.id.label("share_id"), p.c.name.label("project"))
            .join_from(cs, s, s.c.id == cs.c.secret_id)
            .join(u, u.c.id == s.c.owner_id)
            .join(csp, csp.c.share_id == cs.c.id)
            .join(p, p.c.id == csp.c.project_id)
            .where(
                live_share(),
                cs.c.run_id.is_(None),  # one for a single run lends the account to no run of theirs but that one
                await grantee_clause(conn, user.user_id),
                s.c.owner_id != user.user_id,
                s.c.kind == "env",
                s.c.deleted_at.is_(None),
                select(b.c.secret_id).where(b.c.secret_id == s.c.id, b.c.project_id == p.c.id).exists(),
            )
            .order_by(p.c.name)
        )
        lent: dict[int, dict] = {}
        for row in await conn.execute(statement):
            found = lent.setdefault(
                row.id, {"owner": row.owner_id, "login": row.login, "shares": set(), "projects": []}
            )
            found["shares"].add(row.share_id)
            if row.project not in found["projects"]:
                found["projects"].append(row.project)
        seen = _seen(request)
        accounts: list[SharedAccount] = []
        for owner_id in sorted({found["owner"] for found in lent.values()}):
            ids = [secret_id for secret_id, found in lent.items() if found["owner"] == owner_id]
            rows = [row for row in (await conn.execute(_accounts(owner_id))).all() if row.id in lent]
            figures = await account_figures(conn, owner_id, ids, principal_id=user.user_id)
            shares = await shares_of(conn, ids)
            for row in rows:
                found = lent[row.id]
                covering = [share for share in shares.get(row.id, []) if share.id in found["shares"]]
                account = _account(row, figures.get(row.id), covering, seen)
                values = account.model_dump() | {"workers": [], "pools": [], "projects": found["projects"]}
                accounts.append(SharedAccount(**values, owner=found["login"], pick=f"{found['login']}/{row.name}"))
    response.headers.update(NO_STORE)
    return sorted(accounts, key=lambda account: (account.env_var, account.owner.lower(), account.name))


def _paused(change: AccountChange, kept):
    return {} if change.paused is None else {"paused_at": func.coalesce(kept, func.now()) if change.paused else None}


@router.patch("/accounts/{name}", response_model=Account, responses=REFUSALS)
async def change_account(
    request: Request, name: AccountName, body: AccountChange, user: CurrentUser, response: Response
) -> Account:
    """Change the weight, priority or weekly budget of the caller's account ``name``, or pause or resume it; a resume
    also ends its cooldown, save one that needs a new sign-in (``end_cooldown``)."""
    a = tables.accounts
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(_accounts(user.user_id, name))).one_or_none()
        if row is None:
            raise HTTPException(404, NO_ACCOUNT.format(name=name))
        values = body.model_dump(include={"weight", "priority"}, exclude_none=True)
        values |= body.model_dump(include={"weekly_budget_usd", "weekly_reset_at"} & body.model_fields_set)
        made = insert(a).values(secret_id=row.id, **values, **_paused(body, None))
        changed = {**values, **_paused(body, a.c.paused_at), "updated_at": func.now()}
        await conn.execute(made.on_conflict_do_update(index_elements=[a.c.secret_id], set_=changed))
        cooled = body.paused is False and await end_cooldown(conn, row.id)
        figures = await account_figures(conn, user.user_id, [row.id])
        shares = await shares_of(conn, [row.id])
        found = _account(
            (await conn.execute(_accounts(user.user_id, name))).one(),
            figures.get(row.id),
            shares.get(row.id),
            _seen(request),
        )
        paused = "yes" if found.paused_at is not None else "no"
        target = f"{audit_target(row.id)} weight={found.weight} priority={found.priority} paused={paused}"
        if "weekly_budget_usd" in body.model_fields_set:
            target += f" weekly_budget_usd={found.weekly_budget_usd}"
        if cooled:
            target += " cooldown=ended"
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
            p.c.hard_reserve_pct,
            p.c.five_hour_ceiling_pct,
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
    text = (
        f"{project} strategy={body.strategy} cooldown={body.cooldown_seconds} failover={failover} "
        f"max_failovers={body.max_failovers}"
    )
    if body.strategy == QUOTA_PACING:
        text += f" hard_reserve_pct={body.hard_reserve_pct} five_hour_ceiling_pct={body.five_hour_ceiling_pct}"
    return text


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

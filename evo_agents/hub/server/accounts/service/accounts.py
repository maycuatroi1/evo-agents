"""A member's accounts and their policies: GET and PATCH under /v1/accounts, GET, PUT and DELETE under
/v1/account-policies (``docs/credentials.md``, section "Accounts"; ``evo_agents.hub.accounts`` holds the rules).

An account is one of the caller's env secrets (``evo_agents.hub.server.credentials.service.secrets``) seen as one a run may take among
several for the same variable. GET /v1/accounts lists the caller's own, live: name, variable, bindings, weight,
priority, when it was paused, until when it cools down and why, when a run last took it, its end, its state
(``accounts.ACCOUNT_STATES``) and its figures (``views``: its last usage, its runs and their cost over 24
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
(``budgets``); a name that is not one of the caller's live env secrets is 404, whoever's it is.

A policy is the caller's for one project: GET /v1/account-policies lists the caller's own; GET
/v1/account-policies/{project} gives the caller's policy for a project they can see, the defaults
(``accounts.DEFAULT_POLICY``, ``updated_at`` null) when they set none; PUT sets it whole, and needs the writer role on
the project, as binding a secret to it does; DELETE drops it, the defaults holding again.

Every member sees and changes their own accounts and policies alone, a hub admin too: the routes take the caller's
id, never a login. Each write adds one audit row (``audit.ACCOUNT_SET``, ``ACCOUNT_POLICY_PUT``,
``ACCOUNT_POLICY_DELETE``) naming the secret by its id (``secret:<id>``) and a policy by its project, never a value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs
from evo_agents.hub.account_providers import category_of, provider_of
from evo_agents.hub.accounts import DEFAULT_POLICY, QUOTA_PACING, account_state
from evo_agents.hub.gateway_runs import PICK_PREFIX
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.accounts.models.accounts import (
    FAILING,
    MAX_REASON,
    NO_ACCOUNT,
    REASON,
    Account,
    AccountChange,
    AccountPolicy,
    AccountPolicyWrite,
    GatewayAccount,
    SharedAccount,
)
from evo_agents.hub.server.accounts.models.choice import RELOGIN_REASON
from evo_agents.hub.server.accounts.models.views import Figures
from evo_agents.hub.server.accounts.repository import accounts as repository
from evo_agents.hub.server.accounts.repository.choice import end_cooldown
from evo_agents.hub.server.accounts.service.views import account_figures, figured
from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.credentials.service.secrets import audit_target
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.share_leases import grantee_clause, live_share
from evo_agents.hub.server.share_views import CredentialShare, shares_of

# Accounts


def _gateway(row, seen: dict | None) -> GatewayAccount:
    """The gateway part of provider account ``row``, with ``seen``, the gateway's connections by secret id
    (``gateway.service.sync.GatewaySync.status``): cooling while its rate limit lasts, failing when its last test failed."""
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
        found = datetime.fromisoformat(when.replace("Z", "+00:00"))  # pyright: ignore[reportOptionalMemberAccess]
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
            state, values["cooldown_until"] = "cooling", _until(seen[row.id].limited_until)  # pyright: ignore[reportOptionalSubscript]
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


async def list_own(conn: AsyncConnection, user: Principal, seen: dict | None) -> list[Account]:
    """The caller's own env secrets as accounts, by variable and name, with their figures."""
    rows = await repository.accounts(conn, user.user_id)
    figures = await account_figures(conn, user.user_id)
    shares = await shares_of(conn, [row.id for row in rows])
    return [_account(row, figures.get(row.id), shares.get(row.id), seen) for row in rows]


async def read(conn: AsyncConnection, user: Principal, name: str, owner: str | None, seen: dict | None) -> Account:
    """The caller's account ``name``, or with ``owner`` the one that member shared with the caller; 404 for a name
    that is neither."""
    if owner is None:
        return await _own_account(conn, user, name, seen)
    return await _shared_account(conn, user, owner, name, seen)


async def _shared_with(conn: AsyncConnection, principal_id: int) -> tuple:
    """The clauses on credential_shares of a share that lends to ``principal_id`` now: live, with them as grantee."""
    return live_share(), await grantee_clause(conn, principal_id)


async def _own_account(conn: AsyncConnection, user: Principal, name: str, seen: dict | None = None) -> Account:
    row = await repository.account(conn, user.user_id, name)
    if row is None:
        raise NotFound(NO_ACCOUNT.format(name=name))
    figures = await account_figures(conn, user.user_id, [row.id])
    return _account(row, figures.get(row.id), (await shares_of(conn, [row.id])).get(row.id), seen)


async def _shared_account(
    conn: AsyncConnection, user: Principal, owner: str, name: str, seen: dict | None = None
) -> Account:
    """Account ``name`` of ``owner`` as the caller, whom it was shared with, sees it: their runs alone, and the shares
    that lend it to them; 404 when no share not revoked covers them and no run of theirs took it."""
    member = await org_store.user_by_login(conn, owner)
    if member is not None and member.id == user.user_id:
        return await _own_account(conn, user, name, seen)
    owner_id = None if member is None else member.id
    row = None if owner_id is None else await repository.account(conn, owner_id, name)
    refusal = NotFound(f"{owner} shared no account named {name} with you")
    if row is None or owner_id is None:
        raise refusal
    ids = await repository.covering_shares(conn, row.id, await _shared_with(conn, user.user_id))
    if not ids and not await repository.took_one(conn, row.id, user.user_id):
        raise refusal
    figures = await account_figures(conn, owner_id, [row.id], principal_id=user.user_id)
    shares = [share for share in (await shares_of(conn, [row.id])).get(row.id, []) if share.id in ids]
    return _account(row, figures.get(row.id), shares, seen).model_copy(update={"workers": [], "pools": []})


async def list_shared(conn: AsyncConnection, user: Principal, seen: dict | None) -> list[SharedAccount]:
    """The env accounts other members share with the caller through a live share for every run, by variable, owner
    and name, as ``_shared_account`` gives each one, with the projects of those shares its owner binds it to."""
    lent: dict[int, dict] = {}
    for row in await repository.lent(conn, user.user_id, await _shared_with(conn, user.user_id)):
        found = lent.setdefault(row.id, {"owner": row.owner_id, "login": row.login, "shares": set(), "projects": []})
        found["shares"].add(row.share_id)
        if row.project not in found["projects"]:
            found["projects"].append(row.project)
    accounts: list[SharedAccount] = []
    for owner_id in sorted({found["owner"] for found in lent.values()}):
        ids = [secret_id for secret_id, found in lent.items() if found["owner"] == owner_id]
        rows = [row for row in await repository.accounts(conn, owner_id) if row.id in lent]
        figures = await account_figures(conn, owner_id, ids, principal_id=user.user_id)
        shares = await shares_of(conn, ids)
        for row in rows:
            found = lent[row.id]
            covering = [share for share in shares.get(row.id, []) if share.id in found["shares"]]
            account = _account(row, figures.get(row.id), covering, seen)
            values = account.model_dump() | {"workers": [], "pools": [], "projects": found["projects"]}
            accounts.append(SharedAccount(**values, owner=found["login"], pick=f"{found['login']}/{row.name}"))
    return sorted(accounts, key=lambda account: (account.env_var, account.owner.lower(), account.name))


async def change(conn: AsyncConnection, user: Principal, name: str, body: AccountChange, seen: dict | None) -> Account:
    """The caller's account ``name`` with its weight, priority or weekly budget changed, or paused or resumed; a
    resume also ends its cooldown, save one that needs a new sign-in (``end_cooldown``)."""
    row = await repository.account(conn, user.user_id, name)
    if row is None:
        raise NotFound(NO_ACCOUNT.format(name=name))
    values = body.model_dump(include={"weight", "priority"}, exclude_none=True)
    values |= body.model_dump(include={"weekly_budget_usd", "weekly_reset_at"} & body.model_fields_set)
    await repository.write_account(conn, row.id, values, body.paused)
    cooled = body.paused is False and await end_cooldown(conn, row.id)
    figures = await account_figures(conn, user.user_id, [row.id])
    shares = await shares_of(conn, [row.id])
    found = _account(await repository.account(conn, user.user_id, name), figures.get(row.id), shares.get(row.id), seen)
    paused = "yes" if found.paused_at is not None else "no"
    target = f"{audit_target(row.id)} weight={found.weight} priority={found.priority} paused={paused}"
    if "weekly_budget_usd" in body.model_fields_set:
        target += f" weekly_budget_usd={found.weekly_budget_usd}"
    if cooled:
        target += " cooldown=ended"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.ACCOUNT_SET, target=target)
    return found


# Policies


async def list_policies(conn: AsyncConnection, user: Principal) -> list[AccountPolicy]:
    """The caller's own policies, by project."""
    return [AccountPolicy(**row._mapping) for row in await repository.policies(conn, user.user_id)]


async def _policy(conn: AsyncConnection, user: Principal, project: str) -> AccountPolicy:
    row = await repository.policy(conn, user.user_id, project)
    if row is None:
        return AccountPolicy(project=project, updated_at=None, **DEFAULT_POLICY)
    return AccountPolicy(**row._mapping)


async def read_policy(conn: AsyncConnection, user: Principal, project: str) -> AccountPolicy:
    """The caller's policy for ``project``, the defaults when they set none."""
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


async def put_policy(conn: AsyncConnection, user: Principal, project: str, body: AccountPolicyWrite) -> AccountPolicy:
    """The caller's policy for ``project`` set whole; the writer role on it."""
    access = await project_access(conn, user, project)
    if not holds(access.role, "writer"):
        role = access.role or "no role"
        raise Forbidden(f"a policy for project {project} needs the writer role on it; you hold {role}")
    await repository.put_policy(conn, user.user_id, access.project_id, body.model_dump())
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.ACCOUNT_POLICY_PUT,
        target=_policy_target(project, body),
        project_id=access.project_id,
    )
    return await _policy(conn, user, project)


async def drop_policy(conn: AsyncConnection, user: Principal, project: str) -> None:
    """The caller's policy for ``project`` dropped: the defaults hold again. 404 when they set none."""
    access = await project_access(conn, user, project)
    if not await repository.drop_policy(conn, user.user_id, access.project_id):
        raise NotFound(f"you set no policy for project {project}: the defaults hold")
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.ACCOUNT_POLICY_DELETE,
        target=project,
        project_id=access.project_id,
    )

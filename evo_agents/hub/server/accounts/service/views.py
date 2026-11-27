"""What a member reads of their accounts at work (``docs/credentials.md``, section "Accounts"): the figures GET
/v1/accounts adds to each account (and the agent-account routes to each account of an agent,
``agent_account_figures``), and GET /v1/projects/{project}/runs/{run_id}/accounts, the accounts a run took.

The figures of an account: the last usage a run's CLI reported for it (``accounts.last_usage``, kept by
``rate_limits``, or read from Z.ai by ``quota``; its weekly budget's window as its ``seven_day`` one), how many runs
took it and what they cost in the last 24 hours and 7 days, the runs that hold it now, and the same split by principal,
the member each run was made for (``cost_by_principal``: credential_leases.principal_id, else the run's dispatcher),
over 24 hours, 7 days and the month so far (UTC, ``month_start``). For a member another shared the account with
(``principal_id``), the figures are those of their own runs alone. A run took an account when it got a lease of it
(credential_leases), at its first such lease; a run on a provider account's model when its key of the model gateway
reached the account (``gateway.service.leases``), which holds it while that lease is not revoked, and whose cost the gateway
reports into its usage once it ends; its cost is the one its usage reports (``total_cost_usd``, else ``cost``, as
``curator.run_cost`` reads it), so an estimate: a run that failed over counts with each account it took, and a session
resumed by several runs counts in each.

The accounts of a run, for the member who dispatched it alone, as the run's credentials are: for each variable, the
account it has now, by name, how it came by it (``accounts.CHOICES``) and how many times it failed over, with each
usage limit the run hit (the ``system`` events ``rate_limits`` wrote, which name accounts by id) told by name. Never a
value: no query reads one.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.server.accounts.models.views import (
    AccountUsage,
    Figures,
    HeldBy,
    PrincipalCost,
    RunAccount,
    RunAccountEvent,
)
from evo_agents.hub.server.accounts.repository import views as repository
from evo_agents.hub.server.accounts.service.budgets import budget_windows, with_budgets
from evo_agents.hub.server.base.errors import Forbidden
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal

# The figures of a member's accounts


def _cost(row) -> PrincipalCost:
    return PrincipalCost(
        principal=row.login,
        runs_24h=row.runs_24h,
        runs_7d=row.runs_7d,
        runs_month=row.runs_month,
        cost_24h_usd=round(float(row.cost_24h), 6),
        cost_7d_usd=round(float(row.cost_7d), 6),
        cost_month_usd=round(float(row.cost_month), 6),
    )


def _usage(stored) -> AccountUsage | None:
    if not isinstance(stored, dict):
        return None
    try:
        return AccountUsage.model_validate(stored)
    except ValidationError:
        return None


async def _usages(conn: AsyncConnection, owned) -> dict[int, Figures]:
    """The last usage of each account, its weekly budget's window as its seven_day one (``budgets``)."""
    rows = {row.secret_id: row for row in await repository.last_usages(conn, owned)}
    stored = with_budgets({i: row.last_usage for i, row in rows.items()}, await budget_windows(conn, owned))
    return {
        i: Figures(last_usage=_usage(found), last_usage_at=rows[i].last_usage_at if i in rows else None)
        for i, found in stored.items()
    }


async def _figures(conn: AsyncConnection, owned) -> dict[int, Figures]:
    found = await _usages(conn, owned)
    for row in await repository.rows(conn, repository.taken(owned)):
        figures = found.setdefault(row.secret_id, Figures())
        figures.runs_24h, figures.runs_7d = row.runs_24h, row.runs_7d
        figures.cost_24h_usd, figures.cost_7d_usd = round(float(row.cost_24h), 6), round(float(row.cost_7d), 6)
    for row in await repository.rows(conn, repository.held(owned, None)):
        held = HeldBy(run_id=row.id, project=row.name, state=row.state)
        found.setdefault(row.secret_id, Figures()).held_by.append(held)
    for row in await repository.rows(conn, repository.by_principal(owned, None)):
        found.setdefault(row.secret_id, Figures()).cost_by_principal.append(_cost(row))
    return found


async def _principal_figures(conn: AsyncConnection, owned, principal_id: int) -> dict[int, Figures]:
    found = await _usages(conn, owned)
    for row in await repository.rows(conn, repository.by_principal(owned, principal_id)):
        figures, cost = found.setdefault(row.secret_id, Figures()), _cost(row)
        figures.runs_24h, figures.runs_7d = cost.runs_24h, cost.runs_7d
        figures.cost_24h_usd, figures.cost_7d_usd = cost.cost_24h_usd, cost.cost_7d_usd
        figures.cost_by_principal.append(cost)
    for row in await repository.rows(conn, repository.held(owned, principal_id)):
        held = HeldBy(run_id=row.id, project=row.name, state=row.state)
        found.setdefault(row.secret_id, Figures()).held_by.append(held)
    return found


async def account_figures(
    conn: AsyncConnection, owner_id: int, secret_ids: list[int] | None = None, principal_id: int | None = None
) -> dict[int, Figures]:
    """The figures of ``owner_id``'s accounts (``secret_ids`` alone, when given), by secret id; an account no run
    took has none, so ``Figures()``. With ``principal_id``, a member the owner shared them with, the runs, costs and
    holders are those of that member's runs alone."""
    owned = repository.owned(tables.secrets.c.owner_id == owner_id, secret_ids)
    if principal_id is not None:
        return await _principal_figures(conn, owned, principal_id)
    return await _figures(conn, owned)


async def agent_account_figures(
    conn: AsyncConnection, profile_ids: list[int], secret_ids: list[int] | None = None
) -> dict[int, Figures]:
    """The figures of the accounts the agent profiles ``profile_ids`` own, read as a member's are."""
    return await _figures(conn, repository.owned(tables.secrets.c.profile_id.in_(profile_ids), secret_ids))


def figured(figures: Figures | None, category: str) -> dict:
    """The figures of an account as its view gives them: no cost for a service account, whose runs' cost is one number
    kept by their model account (``account_providers``)."""
    values = vars(figures or Figures()).copy()
    if category == "service":
        values |= {"cost_24h_usd": None, "cost_7d_usd": None}
    return values


# The accounts of a run


def _event(at: datetime, body: dict, names: dict[int, str]) -> RunAccountEvent | None:
    action = body.get("account_event")
    if action not in ("failover", "stop"):
        return None
    until = body.get("cooldown_until")
    return RunAccountEvent(
        at=at,
        action=action,
        account=names.get(body.get("secret_id")),  # pyright: ignore[reportArgumentType]
        to=names.get(body.get("to_secret_id")) if action == "failover" else None,  # pyright: ignore[reportArgumentType]
        cooldown_until=datetime.fromisoformat(until) if isinstance(until, str) else None,
    )


async def accounts_of_run(
    engine: AsyncEngine, user: Principal, project: str, run_id: int, sink: str | None
) -> list[RunAccount]:
    """The accounts run ``run_id`` took, by variable, and each usage limit it hit; 403 for any member but the one who
    dispatched it."""
    async with UnitOfWork(engine) as conn:
        await readable_run(conn, user, project, run_id, sink)
        owner_id, login = await repository.run_owner(conn, run_id)
        if owner_id != user.user_id:
            raise Forbidden(f"only {login}, who dispatched run {run_id}, sees the accounts it took")
        names = await repository.secret_names(conn, owner_id)
        taken = await repository.run_accounts(conn, run_id)
        limits = await repository.usage_limits(conn, run_id)
        found = []
        for row in taken:
            events = [_event(at, body, names) for at, body in limits if body.get("env_var") == row.env_var]
            found.append(
                RunAccount(
                    env_var=row.env_var,
                    account=names.get(row.secret_id),
                    how=row.how,
                    failovers=row.failovers,
                    chosen_at=row.chosen_at,
                    events=[event for event in events if event is not None],
                )
            )
        return found

"""What a member reads of their accounts at work (``docs/credentials.md``, section "Accounts"): the figures GET
/v1/accounts adds to each account, and GET /v1/projects/{project}/runs/{run_id}/accounts, the accounts a run took.

The figures of an account: the last usage a run's CLI reported for it (``accounts.last_usage``, kept by
``rate_limits``), how many runs took it and what they cost in the last 24 hours and 7 days, the runs that hold it
now, and the same split by principal, the member each run was made for (``cost_by_principal``: credential_leases.
principal_id, else the run's dispatcher), over 24 hours, 7 days and the month so far (UTC, ``month_start``). For a
member another shared the account with (``principal_id``), the figures are those of their own runs alone. A run took an account when it got a lease of it (credential_leases), at its first such lease; its cost is the
one its usage reports (``total_cost_usd``, else ``cost``, as ``curator.run_cost`` reads it), so an estimate: a run
that failed over counts with each account it took, and a session resumed by several runs counts in each.

The accounts of a run, for the member who dispatched it alone, as the run's credentials are: for each variable, the
account it has now, by name, how it came by it (``accounts.CHOICES``) and how many times it failed over, with each
usage limit the run hit (the ``system`` events ``rate_limits`` wrote, which name accounts by id) told by name. Never a
value: no query here reads one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Numeric, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.accounts import CHOICES
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["accounts"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
DAY, WEEK = timedelta(days=1), timedelta(days=7)
MAX_SWITCHES = 50  # usage limits of one run the route lists, the latest


# Models


class AccountUsageWindow(BaseModel):
    utilization: float | None = None
    resets_at: datetime | None = None


class AccountUsage(BaseModel):
    """What the agent's CLI last said of an account's usage, in a rate_limit_event."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["allowed", "allowed_warning", "rejected"]
    rate_limit_type: str | None = None
    resets_at: datetime | None = None
    utilization: float | None = None
    windows: dict[str, AccountUsageWindow] | None = None


class HeldBy(BaseModel):
    run_id: int
    project: str
    state: str


class RunAccountEvent(BaseModel):
    at: datetime
    action: Literal["failover", "stop"] = Field(description="the run moved to another account, or it stopped")
    account: str | None = Field(description="the account that hit its usage limit, by name")
    to: str | None = Field(description="with failover: the account the run went on with")
    cooldown_until: datetime | None = Field(description="until when the account cools down")


class RunAccount(BaseModel):
    env_var: str
    account: str | None = Field(description="the account the run has now, by name")
    how: Literal[CHOICES] = Field(
        description="picked by hand, bound to the worker, by the charter, the pool, cooling, shared by another member"
    )
    failovers: int = Field(description="the times the run moved to another account")
    chosen_at: datetime
    events: list[RunAccountEvent] = Field(description="each usage limit the run hit with it, oldest first")


class PrincipalCost(BaseModel):
    principal: str | None = Field(description="the member the runs were made for, by login; null for one now gone")
    runs_24h: int
    runs_7d: int
    runs_month: int = Field(description="the runs that took it since the month began, UTC")
    cost_24h_usd: float
    cost_7d_usd: float
    cost_month_usd: float = Field(description="what those runs cost since the month began, in USD, an estimate")


@dataclass
class Figures:
    last_usage: AccountUsage | None = None
    last_usage_at: datetime | None = None
    runs_24h: int = 0
    runs_7d: int = 0
    cost_24h_usd: float = 0.0
    cost_7d_usd: float = 0.0
    held_by: list[HeldBy] = field(default_factory=list)
    cost_by_principal: list[PrincipalCost] = field(default_factory=list)


# The figures of a member's accounts


def month_start():
    """When the month began, UTC."""
    return func.timezone("UTC", func.date_trunc("month", func.timezone("UTC", func.now())))


def usage_cost(usage):
    """A run's cost from its usage, as ``curator.run_cost`` reads it: total_cost_usd, else cost, else 0."""
    total, other = usage["total_cost_usd"], usage["cost"]
    return case(
        (func.jsonb_typeof(total) == "number", func.greatest(cast(total, Numeric), 0)),
        (func.jsonb_typeof(other) == "number", func.greatest(cast(other, Numeric), 0)),
        else_=0,
    )


def _owned(owner_id: int, secret_ids: list[int] | None):
    s = tables.secrets
    found = select(s.c.id).where(s.c.owner_id == owner_id, s.c.kind == "env", s.c.deleted_at.is_(None))
    return found if secret_ids is None else found.where(s.c.id.in_(secret_ids))


def _taken(owner_id: int, secret_ids: list[int] | None):
    """Per account, the runs that took it in the last 7 days and what they cost, in the last 24 hours too."""
    cl, r = tables.credential_leases, tables.runs
    took = (
        select(cl.c.secret_id, cl.c.run_id, func.min(cl.c.issued_at).label("at"))
        .where(cl.c.provider == "secret", cl.c.secret_id.in_(_owned(owner_id, secret_ids)))
        .group_by(cl.c.secret_id, cl.c.run_id)
        .having(func.min(cl.c.issued_at) >= func.now() - WEEK)
        .subquery()
    )
    cost = usage_cost(r.c.usage)
    recent = took.c.at >= func.now() - DAY
    return (
        select(
            took.c.secret_id,
            func.count().filter(recent).label("runs_24h"),
            func.count().label("runs_7d"),
            func.coalesce(func.sum(cost).filter(recent), 0).label("cost_24h"),
            func.coalesce(func.sum(cost), 0).label("cost_7d"),
        )
        .join_from(took, r, r.c.id == took.c.run_id)
        .group_by(took.c.secret_id)
    )


def _by_principal(owner_id: int, secret_ids: list[int] | None, principal_id: int | None):
    """Per account and principal, the runs that took it in the last 24 hours, 7 days and this month, and their cost."""
    cl, r, users = tables.credential_leases, tables.runs, tables.users
    principal = func.coalesce(cl.c.principal_id, r.c.dispatched_by)
    took = (
        select(cl.c.secret_id, cl.c.run_id, principal.label("principal_id"), func.min(cl.c.issued_at).label("at"))
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.provider == "secret", cl.c.secret_id.in_(_owned(owner_id, secret_ids)))
        .group_by(cl.c.secret_id, cl.c.run_id, principal)
        .having(func.min(cl.c.issued_at) >= func.least(month_start(), func.now() - WEEK))
    )
    if principal_id is not None:
        took = took.where(principal == principal_id)
    took = took.subquery()
    cost = usage_cost(r.c.usage)
    day, week, month = (took.c.at >= func.now() - DAY, took.c.at >= func.now() - WEEK, took.c.at >= month_start())
    return (
        select(
            took.c.secret_id,
            users.c.login,
            *(func.count().filter(when).label(f"runs_{span}") for span, when in (("24h", day), ("7d", week))),
            func.count().filter(month).label("runs_month"),
            *(
                func.coalesce(func.sum(cost).filter(when), 0).label(f"cost_{span}")
                for span, when in (("24h", day), ("7d", week), ("month", month))
            ),
        )
        .join_from(took, r, r.c.id == took.c.run_id)
        .outerjoin(users, users.c.id == took.c.principal_id)
        .group_by(took.c.secret_id, users.c.login)
        .order_by(took.c.secret_id, users.c.login)
    )


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


def _held(owner_id: int, secret_ids: list[int] | None, principal_id: int | None):
    ra, r, projects = tables.run_accounts, tables.runs, tables.projects
    statement = (
        select(ra.c.secret_id, r.c.id, projects.c.name, r.c.state)
        .join_from(ra, r, r.c.id == ra.c.run_id)
        .join(projects, projects.c.id == r.c.project_id)
        .where(ra.c.secret_id.in_(_owned(owner_id, secret_ids)), r.c.state.in_(runs.HELD_STATES))
        .order_by(r.c.id)
    )
    return statement if principal_id is None else statement.where(r.c.dispatched_by == principal_id)


def _usage(stored) -> AccountUsage | None:
    if not isinstance(stored, dict):
        return None
    try:
        return AccountUsage.model_validate(stored)
    except ValidationError:
        return None


async def _usages(conn: AsyncConnection, owner_id: int, secret_ids: list[int] | None) -> dict[int, Figures]:
    a = tables.accounts
    usage = select(a.c.secret_id, a.c.last_usage, a.c.last_usage_at).where(
        a.c.secret_id.in_(_owned(owner_id, secret_ids)), a.c.last_usage_at.is_not(None)
    )
    return {
        row.secret_id: Figures(last_usage=_usage(row.last_usage), last_usage_at=row.last_usage_at)
        for row in await conn.execute(usage)
    }


async def account_figures(
    conn: AsyncConnection, owner_id: int, secret_ids: list[int] | None = None, principal_id: int | None = None
) -> dict[int, Figures]:
    """The figures of ``owner_id``'s accounts (``secret_ids`` alone, when given), by secret id; an account no run
    took has none, so ``Figures()``. With ``principal_id``, a member the owner shared them with, the runs, costs and
    holders are those of that member's runs alone."""
    if principal_id is not None:
        return await _principal_figures(conn, owner_id, secret_ids, principal_id)
    found = await _usages(conn, owner_id, secret_ids)
    for row in await conn.execute(_taken(owner_id, secret_ids)):
        figures = found.setdefault(row.secret_id, Figures())
        figures.runs_24h, figures.runs_7d = row.runs_24h, row.runs_7d
        figures.cost_24h_usd, figures.cost_7d_usd = round(float(row.cost_24h), 6), round(float(row.cost_7d), 6)
    for row in await conn.execute(_held(owner_id, secret_ids, None)):
        held = HeldBy(run_id=row.id, project=row.name, state=row.state)
        found.setdefault(row.secret_id, Figures()).held_by.append(held)
    for row in await conn.execute(_by_principal(owner_id, secret_ids, None)):
        found.setdefault(row.secret_id, Figures()).cost_by_principal.append(_cost(row))
    return found


async def _principal_figures(
    conn: AsyncConnection, owner_id: int, secret_ids: list[int] | None, principal_id: int
) -> dict[int, Figures]:
    found = await _usages(conn, owner_id, secret_ids)
    for row in await conn.execute(_by_principal(owner_id, secret_ids, principal_id)):
        figures, cost = found.setdefault(row.secret_id, Figures()), _cost(row)
        figures.runs_24h, figures.runs_7d = cost.runs_24h, cost.runs_7d
        figures.cost_24h_usd, figures.cost_7d_usd = cost.cost_24h_usd, cost.cost_7d_usd
        figures.cost_by_principal.append(cost)
    for row in await conn.execute(_held(owner_id, secret_ids, principal_id)):
        held = HeldBy(run_id=row.id, project=row.name, state=row.state)
        found.setdefault(row.secret_id, Figures()).held_by.append(held)
    return found


# The accounts of a run


def _names(owner_id: int):
    """The names of all of ``owner_id``'s secrets, deleted ones too, so a run's past accounts are told by name."""
    s = tables.secrets
    return select(s.c.id, s.c.name).where(s.c.owner_id == owner_id)


def _limits(run_id: int):
    e = tables.run_events
    return (
        select(e.c.at, e.c.body)
        .where(e.c.run_id == run_id, e.c.kind == "system", e.c.body.has_key("account_event"))
        .order_by(e.c.seq.desc())
        .limit(MAX_SWITCHES)
    )


def _event(at: datetime, body: dict, names: dict[int, str]) -> RunAccountEvent | None:
    action = body.get("account_event")
    if action not in ("failover", "stop"):
        return None
    until = body.get("cooldown_until")
    return RunAccountEvent(
        at=at,
        action=action,
        account=names.get(body.get("secret_id")),
        to=names.get(body.get("to_secret_id")) if action == "failover" else None,
        cooldown_until=datetime.fromisoformat(until) if isinstance(until, str) else None,
    )


@router.get("/{project}/runs/{run_id}/accounts", response_model=list[RunAccount], responses=REFUSALS)
async def run_accounts(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> list[RunAccount]:
    """The accounts run ``run_id`` took, by variable, and each usage limit it hit; for the member who dispatched it."""
    r, ra, users = tables.runs, tables.run_accounts, tables.users
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        owner = select(r.c.dispatched_by, users.c.login).join_from(r, users, users.c.id == r.c.dispatched_by)
        owner_id, login = (await conn.execute(owner.where(r.c.id == run_id))).one()
        if owner_id != user.user_id:
            raise HTTPException(403, f"only {login}, who dispatched run {run_id}, sees the accounts it took")
        names = {row.id: row.name for row in await conn.execute(_names(owner_id))}
        taken = (await conn.execute(select(ra).where(ra.c.run_id == run_id).order_by(ra.c.env_var))).all()
        limits = list(reversed((await conn.execute(_limits(run_id))).all()))
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

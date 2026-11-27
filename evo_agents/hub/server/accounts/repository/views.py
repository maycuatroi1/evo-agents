"""The SQL of what a member reads of their accounts at work: the figures of their accounts (the runs that took each
and their cost, the runs that hold it, its last usage) and the accounts a run took, with its usage limits
(``evo_agents.hub.server.accounts.service.views`` says what they mean). Never a value: no query here reads one."""

from __future__ import annotations

from sqlalchemy import Numeric, case, cast, func, select, union
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.gateway_runs import GATEWAY
from evo_agents.hub.server.accounts.models.views import DAY, MAX_SWITCHES, TAKEN_BY, WEEK

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


def owned(owner, secret_ids: list[int] | None):
    """The ids of the live env secrets and provider accounts ``owner`` (a condition on secrets: a member's, or agent
    profiles') says."""
    s = tables.secrets
    found = select(s.c.id).where(owner, s.c.kind.in_(("env", PROVIDER_KIND)), s.c.deleted_at.is_(None))
    return found if secret_ids is None else found.where(s.c.id.in_(secret_ids))


def taken(owned):
    """Per account, the runs that took it in the last 7 days and what they cost, in the last 24 hours too."""
    cl, r = tables.credential_leases, tables.runs
    took = (
        select(cl.c.secret_id, cl.c.run_id, func.min(cl.c.issued_at).label("at"))
        .where(cl.c.provider.in_(TAKEN_BY), cl.c.secret_id.in_(owned))
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


def by_principal(owned, principal_id: int | None):
    """Per account and principal, the runs that took it in the last 24 hours, 7 days and this month, and their cost."""
    cl, r, users = tables.credential_leases, tables.runs, tables.users
    principal = func.coalesce(cl.c.principal_id, r.c.dispatched_by)
    took = (
        select(cl.c.secret_id, cl.c.run_id, principal.label("principal_id"), func.min(cl.c.issued_at).label("at"))
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.provider.in_(TAKEN_BY), cl.c.secret_id.in_(owned))
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


def held(owned, principal_id: int | None):
    """The runs a worker holds now with each account: those that took it (run_accounts), and those whose key of the
    model gateway reaches it (a gateway lease of the account not revoked)."""
    ra, cl, r, projects = tables.run_accounts, tables.credential_leases, tables.runs, tables.projects

    def holding(table, *more):
        statement = (
            select(table.c.secret_id, r.c.id, projects.c.name, r.c.state)
            .join_from(table, r, r.c.id == table.c.run_id)
            .join(projects, projects.c.id == r.c.project_id)
            .where(table.c.secret_id.in_(owned), r.c.state.in_(runs.HELD_STATES), *more)
        )
        return statement if principal_id is None else statement.where(r.c.dispatched_by == principal_id)

    found = union(holding(ra), holding(cl, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))).subquery()
    return select(found).order_by(found.c.id, found.c.secret_id)


async def last_usages(conn: AsyncConnection, owned) -> list:
    """The last usage of each account ``owned`` says that has one: secret id, usage and when it was reported."""
    a = tables.accounts
    usage = select(a.c.secret_id, a.c.last_usage, a.c.last_usage_at).where(
        a.c.secret_id.in_(owned), a.c.last_usage_at.is_not(None)
    )
    return list(await conn.execute(usage))


async def rows(conn: AsyncConnection, statement) -> list:
    """The rows of one of the statements above."""
    return list(await conn.execute(statement))


# The accounts of a run


async def run_owner(conn: AsyncConnection, run_id: int) -> tuple[int, str]:
    """(id, login) of the member who dispatched run ``run_id``."""
    r, users = tables.runs, tables.users
    owner = select(r.c.dispatched_by, users.c.login).join_from(r, users, users.c.id == r.c.dispatched_by)
    owner_id, login = (await conn.execute(owner.where(r.c.id == run_id))).one()
    return owner_id, login


async def secret_names(conn: AsyncConnection, owner_id: int) -> dict[int, str]:
    """The names of all of ``owner_id``'s secrets, deleted ones too, so a run's past accounts are told by name."""
    s = tables.secrets
    return {row.id: row.name for row in await conn.execute(select(s.c.id, s.c.name).where(s.c.owner_id == owner_id))}


async def run_accounts(conn: AsyncConnection, run_id: int) -> list:
    """The run_accounts rows of run ``run_id``, by variable."""
    ra = tables.run_accounts
    return list((await conn.execute(select(ra).where(ra.c.run_id == run_id).order_by(ra.c.env_var))).all())


async def usage_limits(conn: AsyncConnection, run_id: int) -> list:
    """The ``system`` events of run ``run_id`` that tell an account event, the latest MAX_SWITCHES, oldest first."""
    e = tables.run_events
    statement = (
        select(e.c.at, e.c.body)
        .where(e.c.run_id == run_id, e.c.kind == "system", e.c.body.has_key("account_event"))
        .order_by(e.c.seq.desc())
        .limit(MAX_SWITCHES)
    )
    return list(reversed((await conn.execute(statement)).all()))

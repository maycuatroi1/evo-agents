"""The SQL of the choice of an account for a run (``evo_agents.hub.server.accounts.service.choice`` says how it
chooses): the policies it chooses by, the accounts rows it locks and weighs, what a run took, a choice recorded, and
the cooldowns of an account."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, not_, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.accounts import DEFAULT_POLICY
from evo_agents.hub.server.accounts.models.choice import RELOGIN_REASON, Choice


async def account_policy(conn: AsyncConnection, owner_id: int, project_id: int) -> dict:
    """The policy of ``owner_id`` for ``project_id``: their account_policies row, else DEFAULT_POLICY."""
    p = tables.account_policies
    row = (
        await conn.execute(
            select(
                p.c.strategy,
                p.c.cooldown_seconds,
                p.c.failover,
                p.c.max_failovers,
                p.c.hard_reserve_pct,
                p.c.five_hour_ceiling_pct,
            ).where(p.c.owner_id == owner_id, p.c.project_id == project_id)
        )
    ).one_or_none()
    return dict(row._mapping) if row is not None else dict(DEFAULT_POLICY)


async def pool_policy(conn: AsyncConnection, pool_id: int | None) -> dict:
    """The policy the accounts of account pool ``pool_id`` are chosen by: its strategy and thresholds, DEFAULT_POLICY
    for the rest (and for all of it with no pool)."""
    if pool_id is None:
        return dict(DEFAULT_POLICY)
    ap = tables.account_pools
    row = (
        await conn.execute(
            select(ap.c.strategy, ap.c.hard_reserve_pct, ap.c.five_hour_ceiling_pct).where(ap.c.id == pool_id)
        )
    ).one_or_none()
    return dict(DEFAULT_POLICY) | (dict(row._mapping) if row is not None else {})


async def lock_accounts(conn: AsyncConnection, ordered: list[int]) -> list:
    """The accounts rows of the secrets ``ordered`` (sorted ids), made when missing and locked until the transaction
    ends, in that order."""
    a = tables.accounts
    await conn.execute(insert(a).values([{"secret_id": i} for i in ordered]).on_conflict_do_nothing())
    rows = await conn.execute(
        select(
            a.c.secret_id,
            a.c.weight,
            a.c.priority,
            a.c.paused_at,
            a.c.rotation,
            (a.c.cooldown_until > func.now()).label("cooling"),
            a.c.cooldown_until,
            a.c.last_usage,
        )
        .where(a.c.secret_id.in_(ordered))
        .order_by(a.c.secret_id)
        .with_for_update()
    )
    return list(rows.all())


async def kept(conn: AsyncConnection, run_id: int) -> dict[str, int]:
    """Variable -> the secret run ``run_id`` took for it, or the run it resumes took."""
    ra, r = tables.run_accounts, tables.runs
    resumed = select(r.c.resume_of_run_id).where(r.c.id == run_id).scalar_subquery()
    rows = await conn.execute(
        select(ra.c.env_var, ra.c.secret_id)
        .where(ra.c.run_id.in_([run_id, resumed]))
        .order_by((ra.c.run_id == run_id).desc())
    )
    found: dict[str, int] = {}
    for row in rows:
        found.setdefault(row.env_var, row.secret_id)
    return found


async def record(
    conn: AsyncConnection, run_id: int, env_var: str, choice: Choice, failovers: int | None = None
) -> None:
    """Record that run ``run_id`` took ``choice`` for ``env_var``: the account's lease time and place, the run_accounts
    row."""
    a, ra = tables.accounts, tables.run_accounts
    assert choice.secret is not None
    values: dict = {"last_leased_at": func.now()}
    if choice.rotation is not None:
        values["rotation"] = choice.rotation
    await conn.execute(update(a).where(a.c.secret_id == choice.secret.id).values(**values))
    took = {"secret_id": choice.secret.id, "how": choice.how, "chosen_at": func.now()}
    if failovers is not None:
        took["failovers"] = failovers
    statement = insert(ra).values(run_id=run_id, env_var=env_var, **took)
    await conn.execute(statement.on_conflict_do_update(index_elements=[ra.c.run_id, ra.c.env_var], set_=took))


async def taken(conn: AsyncConnection, run_id: int, env_var: str) -> tuple[int | None, str | None, int]:
    """(secret id, how, failovers) of the account run ``run_id`` took for ``env_var``, or the run it resumes took."""
    ra, r = tables.run_accounts, tables.runs
    resumed = select(r.c.resume_of_run_id).where(r.c.id == run_id).scalar_subquery()
    row = (
        await conn.execute(
            select(ra.c.run_id, ra.c.secret_id, ra.c.how, ra.c.failovers)
            .where(ra.c.run_id.in_([run_id, resumed]), ra.c.env_var == env_var)
            .order_by((ra.c.run_id == run_id).desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None, None, 0
    return row.secret_id, row.how, row.failovers if row.run_id == run_id else 0


async def cool(conn: AsyncConnection, secret_id: int, until: datetime, reason: str) -> datetime:
    """Put the account in cooldown until ``until`` at least; the end of its cooldown."""
    a = tables.accounts
    statement = (
        update(a)
        .where(a.c.secret_id == secret_id)
        .values(
            cooldown_until=func.greatest(func.coalesce(a.c.cooldown_until, until), until),
            cooldown_reason=reason,
            updated_at=func.now(),
        )
        .returning(a.c.cooldown_until)
    )
    return (await conn.execute(statement)).scalar_one()


async def clear_relogin(conn: AsyncConnection, secret_id: int) -> None:
    """A new sign-in of account ``secret_id`` ends the cooldown ``mark_relogin`` set; a cooldown of a usage limit stays."""
    a = tables.accounts
    await conn.execute(
        update(a)
        .where(a.c.secret_id == secret_id, a.c.cooldown_reason == RELOGIN_REASON)
        .values(cooldown_until=None, cooldown_reason=None, updated_at=func.now())
    )


async def end_cooldown(conn: AsyncConnection, secret_id: int) -> bool:
    """The owner's resume of account ``secret_id`` ends its cooldown, whatever limit set it, save the one only a new
    sign-in ends (``mark_relogin``): the owner knows the limit reset before the time its runtime gave. The last usage
    goes too, since the report that cooled the account no longer holds; the next run reports it again. True when a
    cooldown ended."""
    a = tables.accounts
    ended = await conn.execute(
        update(a)
        .where(
            a.c.secret_id == secret_id,
            a.c.cooldown_until > func.now(),
            or_(a.c.cooldown_reason.is_(None), not_(a.c.cooldown_reason.startswith(RELOGIN_REASON, autoescape=True))),
        )
        .values(cooldown_until=None, cooldown_reason=None, last_usage=null(), last_usage_at=None, updated_at=func.now())
    )
    return ended.rowcount > 0


async def revoke_secret_lease(conn: AsyncConnection, run_id: int, secret_id: int) -> None:
    """Revoke run ``run_id``'s live lease of secret ``secret_id``."""
    cl = tables.credential_leases
    await conn.execute(
        update(cl)
        .where(
            cl.c.run_id == run_id,
            cl.c.secret_id == secret_id,
            cl.c.provider == "secret",
            cl.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )

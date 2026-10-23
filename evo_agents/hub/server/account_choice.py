"""Which of its owner's accounts a run gets for each variable (``docs/credentials.md``, section "Accounts"), chosen in
the transaction of ``credentials.lease_credentials`` that leases them, while the run's row is locked.

``choose_accounts`` takes the env secrets the run may be leased (``share_leases``: the owner's, bound to the run's
project on any of their workers or on this one, live and not past their end, and those other members shared with the
owner that their shares lend now; for a run of the Curator only those its charter lists) and gives back one for each
variable:

1. the account the run took already (run_accounts), the one its owner picked when dispatching it among them, or the
   one the run it resumes took, while it is still one of those secrets: a run keeps its account on every ask for its
   leases, whatever happened to the account since; else the secret of a lease the run holds for that variable;
2. else among the accounts bound to this worker (for a run of the Curator: the accounts its charter lists), then
   among the others bound to the project, by the strategy of the owner's policy for the project
   (``evo_agents.hub.accounts.pick``), skipping each account its owner paused or that cools down; then, the same way,
   among the accounts shared with the owner (how ``shared``): the owner's own come first;
3. else, when every account of the owner's left cools down, the one whose cooldown ends first, and a ``system`` event
   of the run says so; when every account is paused, none, and an event says that.

The accounts of an agent profile (``agent_accounts``) come in the same list, and ``credentials.survey_run`` leaves out
of it every secret of the dispatcher for a variable the agent has an account for on this worker, so a variable's group
is the agent's or the member's, never both. An agent's accounts are chosen by DEFAULT_POLICY (its strategy, cooldown,
failover and ``max_failovers``), by the same tiers (all of them are bound to this worker), and fail over among the
agent's accounts alone. Every account of the agent for a variable past its end (``ended``): the run gets none for it,
and an event says so.

Each event names an account by its secret's id, never its name: the run's log is read by every reader of the project.

Two runs asking at once cannot take the same turn of a rotation: before it reads them, the ask creates the accounts
rows of the secrets it weighs (none existing) and locks them, in the order of their ids, until it commits.

``fail_over`` is what the hub does when a run's account hits its usage limit (``rate_limits``): the account cools down
until the reset its runtime gave, else for the policy's ``cooldown_seconds``; then, unless the account was picked by
hand, the policy turns failover off or the run failed over ``max_failovers`` times already, the run takes the next
ready account by the same tiers and strategy, its run_accounts row moves to it and counts one more failover. With no
account ready it stays and stops, and the answer says when the first one is ready again.

An OAuth account whose refresh the token endpoint refused (``account_refresh``: the refresh token was revoked or has
ended) needs a new sign-in: ``mark_relogin`` puts it in a cooldown that lasts ``RELOGIN_SPAN``, with the reason
``RELOGIN_REASON``, and ``fail_over_relogin`` moves the runs that held it to another account as ``fail_over`` does.
The member ends the cooldown by running ``evo-agents hub account oauth-login`` again (``clear_relogin``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.accounts import DEFAULT_POLICY, Candidate, pick, usable


class Secret(Protocol):
    """What ``choose_accounts`` reads of a secret (``credentials._Secret``)."""

    id: int
    name: str
    kind: str
    env_var: str | None
    on_worker: bool
    share_id: int | None  # the share another member lent it under; None for the owner's own
    agent: str | None  # the handle of the agent profile that owns it; None for a member's


class Run(Protocol):
    id: int
    owner_id: int
    project_id: int
    curator: object | None


@dataclass(frozen=True)
class _State:
    weight: int
    priority: int
    paused: bool
    cooling_until: datetime | None  # set while it cools down
    rotation: float


@dataclass(frozen=True)
class _Choice:
    secret: Secret | None
    how: str | None  # None: the run keeps what it took, nothing to record
    rotation: float | None = None  # the account's place once taken, for a rotating strategy
    note: str | None = None  # the text of the run's event


async def account_policy(conn: AsyncConnection, owner_id: int, project_id: int) -> dict:
    """The policy of ``owner_id`` for ``project_id``: their account_policies row, else DEFAULT_POLICY."""
    p = tables.account_policies
    row = (
        await conn.execute(
            select(p.c.strategy, p.c.cooldown_seconds, p.c.failover, p.c.max_failovers).where(
                p.c.owner_id == owner_id, p.c.project_id == project_id
            )
        )
    ).one_or_none()
    return dict(row._mapping) if row is not None else dict(DEFAULT_POLICY)


async def _lock_accounts(conn: AsyncConnection, ids: list[int]) -> dict[int, _State]:
    """The accounts rows of ``ids``, made when missing and locked until the transaction ends, by secret id."""
    a = tables.accounts
    ordered = sorted(set(ids))
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
        )
        .where(a.c.secret_id.in_(ordered))
        .order_by(a.c.secret_id)
        .with_for_update()
    )
    return {
        row.secret_id: _State(
            weight=row.weight,
            priority=row.priority,
            paused=row.paused_at is not None,
            cooling_until=row.cooldown_until if row.cooling else None,
            rotation=row.rotation,
        )
        for row in rows
    }


async def _kept(conn: AsyncConnection, run_id: int) -> dict[str, int]:
    """Variable -> the secret run ``run_id`` took for it, or the run it resumes took."""
    ra, r = tables.run_accounts, tables.runs
    resumed = select(r.c.resume_of_run_id).where(r.c.id == run_id).scalar_subquery()
    rows = await conn.execute(
        select(ra.c.env_var, ra.c.secret_id)
        .where(ra.c.run_id.in_([run_id, resumed]))
        .order_by((ra.c.run_id == run_id).desc())
    )
    kept: dict[str, int] = {}
    for row in rows:
        kept.setdefault(row.env_var, row.secret_id)
    return kept


def _candidate(secret: Secret, state: _State) -> Candidate:
    return Candidate(secret.id, secret.name, state.weight, state.priority, state.rotation)


def _tiers(secrets: list[Secret], curator: bool) -> list[tuple[str, list[Secret]]]:
    if curator:
        return [("charter", secrets)]
    own = [s for s in secrets if s.share_id is None]
    return [
        ("worker", [s for s in own if s.on_worker]),
        ("pool", [s for s in own if not s.on_worker]),
        ("shared", [s for s in secrets if s.share_id is not None]),
    ]


def _fallback(env_var: str, secrets: list[Secret], states: dict[int, _State], strategy: str) -> _Choice:
    """Nothing is ready: the owner's own account whose cooldown ends first, else none since every one is paused. An
    account shared with the owner is never taken cooling: its owner's runs go first (``share_leases``)."""
    secrets = [s for s in secrets if s.share_id is None]
    if not secrets:
        return _Choice(None, None, note=f"no account for {env_var} is ready: the run gets none")
    allowed = {c.id for c in usable([_candidate(s, states[s.id]) for s in secrets], strategy)}
    cooling = [s for s in secrets if s.id in allowed and not states[s.id].paused and states[s.id].cooling_until]
    if not cooling:
        return _Choice(None, None, note=f"every account for {env_var} is paused: the run gets none")
    secret = min(cooling, key=lambda s: (states[s.id].cooling_until, s.id))
    until = states[secret.id].cooling_until.isoformat()
    note = f"every account for {env_var} is cooling down: the run took secret {secret.id}, ready first, at {until}"
    return _Choice(secret, "cooling", note=note)


def _decide(
    env_var: str, secrets: list[Secret], states: dict[int, _State], kept: int | None, held: set[int], context: tuple
) -> _Choice:
    strategy, curator = context
    by_id = {secret.id: secret for secret in secrets}
    if kept in by_id:
        return _Choice(by_id[kept], None)
    tiers = _tiers(secrets, curator)
    for how, tier in tiers:
        for secret in tier:
            if secret.id in held:
                return _Choice(secret, how)
    for how, tier in tiers:
        ready = [s for s in tier if not states[s.id].paused and states[s.id].cooling_until is None]
        found = pick([_candidate(s, states[s.id]) for s in ready], strategy)
        if found is not None:
            rotation = found[1] if strategy != "fill-first" else None
            return _Choice(by_id[found[0].id], how, rotation)
    return _fallback(env_var, secrets, states, strategy)


async def _record(
    conn: AsyncConnection, run_id: int, env_var: str, choice: _Choice, failovers: int | None = None
) -> None:
    a, ra = tables.accounts, tables.run_accounts
    values = {"last_leased_at": func.now()}
    if choice.rotation is not None:
        values["rotation"] = choice.rotation
    await conn.execute(update(a).where(a.c.secret_id == choice.secret.id).values(**values))
    took = {"secret_id": choice.secret.id, "how": choice.how, "chosen_at": func.now()}
    if failovers is not None:
        took["failovers"] = failovers
    statement = insert(ra).values(run_id=run_id, env_var=env_var, **took)
    await conn.execute(statement.on_conflict_do_update(index_elements=[ra.c.run_id, ra.c.env_var], set_=took))


async def _ended_event(conn: AsyncConnection, run: Run, env_var: str) -> None:
    from evo_agents.hub.server.run_state import write_event  # it imports the routes that import this module

    handle = getattr(run, "profile", None)
    text = f"every account of agent @{handle} for {env_var} has ended: the run gets none, and not its dispatcher's"
    await write_event(conn, run.id, {"text": text, "env_var": env_var, "secret_id": None})


async def choose_accounts(
    conn: AsyncConnection, run: Run, secrets: list[Secret], held: set[int], ended: tuple[str, ...] = ()
) -> list[Secret]:
    """One env secret of ``secrets`` for each variable, by the variable's name, as the module says; ``held`` holds the
    ids of the env secrets the run holds a lease of, ``ended`` the variables whose accounts of the agent all ended.
    Records each new choice and writes the run's events."""
    by_var: dict[str, list[Secret]] = {}
    for secret in sorted((s for s in secrets if s.kind == "env"), key=lambda s: s.name):
        by_var.setdefault(secret.env_var, []).append(secret)
    if not by_var and not ended:
        return []
    ids = [secret.id for group in by_var.values() for secret in group]
    states = await _lock_accounts(conn, ids) if ids else {}
    kept = await _kept(conn, run.id)
    member = (await account_policy(conn, run.owner_id, run.project_id))["strategy"]
    chosen: list[Secret] = []
    for env_var in sorted(set(by_var) | set(ended)):
        if env_var not in by_var:  # every account of the agent for it has ended, and the dispatcher's is not leased
            await _ended_event(conn, run, env_var)
            continue
        owned = by_var[env_var][0].agent is not None  # the agent's accounts follow DEFAULT_POLICY, never a member's
        context = (DEFAULT_POLICY["strategy"] if owned else member, run.curator is not None and not owned)
        choice = _decide(env_var, by_var[env_var], states, kept.get(env_var), held, context)
        if choice.note is not None:
            from evo_agents.hub.server.run_state import write_event  # it imports the routes that import this module

            await write_event(conn, run.id, {"text": choice.note, "env_var": env_var, "secret_id": _id(choice)})
        if choice.secret is None:
            continue
        if choice.how is not None:
            await _record(conn, run.id, env_var, choice)
        chosen.append(choice.secret)
    return chosen


def _id(choice: _Choice) -> int | None:
    return choice.secret.id if choice.secret is not None else None


# When a run's account hits its usage limit


@dataclass(frozen=True)
class Failover:
    """What ``fail_over`` decided: the account the run moves to, or why it stays and when one is ready first."""

    secret: Secret | None  # None: the run cannot move
    failovers: int  # the times the run moved, this one included
    cooling_until: datetime  # until when the limited account cools down
    reason: str | None = None  # why the run cannot move
    ready_at: datetime | None = None  # when it cannot: the earliest end of a cooldown among its accounts


async def _taken(conn: AsyncConnection, run_id: int, env_var: str) -> tuple[int | None, str | None, int]:
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


async def _cool(conn: AsyncConnection, secret_id: int, until: datetime, reason: str) -> datetime:
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


def _why_not(how: str | None, policy: dict, failovers: int, whose: str = "its owner's") -> str | None:
    if how == "manual":
        return "its account was picked by hand"
    if not policy["failover"]:
        policy_of = "the owner's" if whose == "its owner's" else whose  # the words a member's run always had
        return f"{policy_of} policy turns failover off"
    if failovers >= policy["max_failovers"]:
        return f"it failed over {failovers} times, the most {whose} policy allows"
    return None


async def fail_over(
    conn: AsyncConnection, run: Run, secrets: list[Secret], env_var: str, current: int, limit: tuple
) -> Failover:
    """Cool account ``current`` down, which run ``run`` holds for ``env_var`` and which hit its usage limit, and move
    the run to another one of ``secrets`` when it may, as the module says. ``limit`` is (the reset the runtime gave or
    None, the cooldown's reason). A run that moved already answers the account it moved to, as a retry would."""
    resets_at, reason = limit
    own = [s for s in secrets if s.kind == "env" and s.env_var == env_var]
    owned = any(s.id == current and s.agent is not None for s in own)  # an agent's accounts: DEFAULT_POLICY
    policy = dict(DEFAULT_POLICY) if owned else await account_policy(conn, run.owner_id, run.project_id)
    states = await _lock_accounts(conn, [s.id for s in own] + [current])
    now = datetime.now(UTC)
    until = (
        resets_at if resets_at is not None and resets_at > now else now + timedelta(seconds=policy["cooldown_seconds"])
    )
    until = await _cool(conn, current, until, reason)
    taken, how, failovers = await _taken(conn, run.id, env_var)
    others = [s for s in own if s.id != current]
    if failovers and taken != current and taken in {s.id for s in others}:
        return Failover(next(s for s in others if s.id == taken), failovers, until)
    why = _why_not(how, policy, failovers, "the default" if owned else "its owner's")
    if why is None:
        for tier_how, tier in _tiers(others, run.curator is not None and not owned):
            ready = [s for s in tier if not states[s.id].paused and states[s.id].cooling_until is None]
            found = pick([_candidate(s, states[s.id]) for s in ready], policy["strategy"])
            if found is not None:
                rotation = found[1] if policy["strategy"] != "fill-first" else None
                secret = next(s for s in ready if s.id == found[0].id)
                await _record(conn, run.id, env_var, _Choice(secret, tier_how, rotation), failovers + 1)
                return Failover(secret, failovers + 1, until)
        why = f"no other account for {env_var} is ready"
    cooling = [states[s.id].cooling_until for s in others if not states[s.id].paused and states[s.id].cooling_until]
    return Failover(None, failovers, until, why, min([until, *cooling]))


# An OAuth account whose refresh token is dead


RELOGIN_REASON = "needs-relogin: the OAuth refresh token was refused; run evo-agents hub account oauth-login again"
RELOGIN_SPAN = timedelta(days=3650)  # a cooldown only a new sign-in ends


async def mark_relogin(conn: AsyncConnection, secret_id: int) -> None:
    """Account ``secret_id`` needs a new sign-in: no run is given it until the member logs in again."""
    await _cool(conn, secret_id, datetime.now(UTC) + RELOGIN_SPAN, RELOGIN_REASON)


async def clear_relogin(conn: AsyncConnection, secret_id: int) -> None:
    """A new sign-in of account ``secret_id`` ends the cooldown ``mark_relogin`` set; a cooldown of a usage limit stays."""
    a = tables.accounts
    await conn.execute(
        update(a)
        .where(a.c.secret_id == secret_id, a.c.cooldown_reason == RELOGIN_REASON)
        .values(cooldown_until=None, cooldown_reason=None, updated_at=func.now())
    )


async def fail_over_relogin(conn: AsyncConnection, run: Run, secrets: list[Secret], rejected: set[int]) -> None:
    """Move run ``run`` off each account in ``rejected`` that it holds, as ``fail_over`` does for a usage limit; its
    lease of that account is revoked. A run that cannot move (picked by hand, failover off, no other account ready)
    keeps the account, whose token the lease leaves out once it has ended."""
    cl = tables.credential_leases
    for secret in sorted((s for s in secrets if s.id in rejected and s.kind == "env"), key=lambda s: s.name):
        taken, _, _ = await _taken(conn, run.id, secret.env_var)
        if taken != secret.id:
            continue
        limit = (datetime.now(UTC) + RELOGIN_SPAN, RELOGIN_REASON)
        moved = await fail_over(conn, run, secrets, secret.env_var, secret.id, limit)
        if moved.secret is not None:
            await conn.execute(
                update(cl)
                .where(
                    cl.c.run_id == run.id,
                    cl.c.secret_id == secret.id,
                    cl.c.provider == "secret",
                    cl.c.revoked_at.is_(None),
                )
                .values(revoked_at=func.now())
            )

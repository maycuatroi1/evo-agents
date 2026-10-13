"""Which of its owner's accounts a run gets for each variable (``docs/credentials.md``, section "Accounts"), chosen in
the transaction of ``credentials.lease_credentials`` that leases them, while the run's row is locked.

``choose_accounts`` takes the env secrets the run may be leased (``credentials._bound_secrets``: the owner's, bound to
the run's project on any of their workers or on this one, live and not past their end; for a run of the Curator only
those its charter lists) and gives back one for each variable:

1. the account the run took already (run_accounts), the one its owner picked when dispatching it among them, or the
   one the run it resumes took, while it is still one of those secrets: a run keeps its account on every ask for its
   leases, whatever happened to the account since; else the secret of a lease the run holds for that variable;
2. else among the accounts bound to this worker (for a run of the Curator: the accounts its charter lists), then
   among the others bound to the project, by the strategy of the owner's policy for the project
   (``evo_agents.hub.accounts.pick``), skipping each account its owner paused or that cools down;
3. else, when every account left cools down, the one whose cooldown ends first, and a ``system`` event of the run says
   so; when every account is paused, none, and an event says that.

Each event names an account by its secret's id, never its name: the run's log is read by every reader of the project.

Two runs asking at once cannot take the same turn of a rotation: before it reads them, the ask creates the accounts
rows of the secrets it weighs (none existing) and locks them, in the order of their ids, until it commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    return [("worker", [s for s in secrets if s.on_worker]), ("pool", [s for s in secrets if not s.on_worker])]


def _fallback(env_var: str, secrets: list[Secret], states: dict[int, _State], strategy: str) -> _Choice:
    """Nothing is ready: the account whose cooldown ends first, else none since every one is paused."""
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


async def _record(conn: AsyncConnection, run_id: int, env_var: str, choice: _Choice) -> None:
    a, ra = tables.accounts, tables.run_accounts
    values = {"last_leased_at": func.now()}
    if choice.rotation is not None:
        values["rotation"] = choice.rotation
    await conn.execute(update(a).where(a.c.secret_id == choice.secret.id).values(**values))
    took = {"secret_id": choice.secret.id, "how": choice.how, "chosen_at": func.now()}
    statement = insert(ra).values(run_id=run_id, env_var=env_var, **took)
    await conn.execute(statement.on_conflict_do_update(index_elements=[ra.c.run_id, ra.c.env_var], set_=took))


async def choose_accounts(conn: AsyncConnection, run: Run, secrets: list[Secret], held: set[int]) -> list[Secret]:
    """One env secret of ``secrets`` for each variable, by the variable's name, as the module says; ``held`` holds the
    ids of the env secrets the run holds a lease of. Records each new choice and writes the run's events."""
    by_var: dict[str, list[Secret]] = {}
    for secret in sorted((s for s in secrets if s.kind == "env"), key=lambda s: s.name):
        by_var.setdefault(secret.env_var, []).append(secret)
    if not by_var:
        return []
    states = await _lock_accounts(conn, [secret.id for group in by_var.values() for secret in group])
    kept = await _kept(conn, run.id)
    context = ((await account_policy(conn, run.owner_id, run.project_id))["strategy"], run.curator is not None)
    chosen: list[Secret] = []
    for env_var in sorted(by_var):
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

"""The account a member picks by hand for a run (``docs/credentials.md``, section "Accounts"): ``account`` on the body
of a dispatch of steps, a plan run or an author run, and on a rerun, where none keeps the one picked for the run rerun.

A name is one of the dispatcher's own live env secrets, bound to the run's project: a name that is no env secret of
theirs is 404, whoever's it is; one past its end, paused, not bound to the project, or bound there only to workers
the run might not go to, is 422 saying why. The pick is written to run_accounts with how ``manual`` in the
transaction that queues the run, and ``account_choice.choose_accounts`` keeps it on every ask for the run's leases;
an account picked by hand never fails over to another one. Audit rows name it by its secret's id, never its name.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.models.placement import POOL_KEY
from evo_agents.hub.server.security import Principal

MANUAL = "manual"  # run_accounts.how of an account picked by hand (evo_agents.hub.accounts.CHOICES)


@dataclass(frozen=True)
class Picked:
    """An account picked by hand: its secret, the variable it sets and its owner's name for it."""

    secret_id: int
    env_var: str
    name: str

    def audit_text(self) -> str:
        return f" account=secret:{self.secret_id}"


def _secret_query(owner_id: int):
    s = tables.secrets
    return (
        select(
            s.c.id,
            s.c.name,
            s.c.kind,
            s.c.env_var,
            s.c.deleted_at,
            ((s.c.expires_at <= func.now()) & tables.accounts.c.oauth_sealed.is_(None)).label("ended"),
            s.c.expires_at,
            tables.accounts.c.paused_at,
        )
        .outerjoin_from(s, tables.accounts, tables.accounts.c.secret_id == s.c.id)
        .where(s.c.owner_id == owner_id)
    )


@dataclass(frozen=True)
class _Bound:
    """Where an account is bound for a project: ``workers`` by name, ``pools`` by name, ``anywhere`` for a binding to
    any worker of its owner."""

    workers: tuple[str, ...]
    pools: tuple[str, ...]
    anywhere: bool

    def __bool__(self) -> bool:
        return bool(self.workers or self.pools or self.anywhere)

    def text(self) -> str:
        on = [f"worker {', '.join(self.workers)}"] if self.workers else []
        return " or ".join(on + ([f"pool {', '.join(self.pools)}"] if self.pools else []))


async def _bindings(conn: AsyncConnection, secret_id: int, project_id: int) -> _Bound:
    """Where ``secret_id`` is bound for the project."""
    b, w, p = tables.secret_bindings, tables.workers, tables.worker_pools
    rows = await conn.execute(
        select(w.c.name.label("worker"), p.c.name.label("pool"))
        .select_from(b)
        .outerjoin(w, w.c.id == b.c.worker_id)
        .outerjoin(p, p.c.id == b.c.pool_id)
        .where(b.c.secret_id == secret_id, b.c.project_id == project_id)
    )
    found = rows.all()
    return _Bound(
        tuple(sorted(row.worker for row in found if row.worker)),
        tuple(sorted(row.pool for row in found if row.pool)),
        any(row.worker is None and row.pool is None for row in found),
    )


async def _pool_of(conn: AsyncConnection, pinned) -> str | None:
    w, p = tables.workers, tables.worker_pools
    query = select(p.c.name).join_from(w, p, p.c.id == w.c.pool_id).where(w.c.id == pinned.id)
    return (await conn.execute(query)).scalar_one_or_none()


def _asked_pool(selector) -> str | None:
    """The pool a run's selector asks (pool=<name>), or None."""
    return next((label.split("=", 1)[1] for label in selector or () if label.startswith(f"{POOL_KEY}=")), None)


def _unbound(name: str, project: str, bound: _Bound, pinned, pool: str | None = None) -> str | None:
    """Why account ``name`` cannot go with a run of ``project`` pinned to ``pinned`` (None: not pinned), or asking
    ``pool`` (the pool of the pinned worker, or the pool its selector names), or None."""
    if not bound:
        return (
            f"account {name} is not bound to project {project}: bind it with `evo-agents hub secret set {name} "
            f"--project {project}`"
        )
    if bound.anywhere:
        return None
    if pool is not None and pool.lower() in {name.lower() for name in bound.pools}:
        return None
    workers = bound.text()
    if pinned is None:
        return (
            f"account {name} is bound to project {project} on {workers} only, and the run is not pinned to a "
            "worker or sent to the pool: pin it to one of them, or bind the account to any worker of yours"
        )
    if pinned.name not in bound.workers:
        return f"account {name} is bound to project {project} on {workers} only, not on worker {pinned.name}"
    return None


async def pick_account(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    name: str,
    pinned=None,
    *,
    secret_id=None,
    selector=(),
) -> Picked:
    """The caller's account ``name`` (or, with ``secret_id``, the secret of that id, whatever its name now) for a run
    of the project pinned to ``pinned`` (None: any worker of theirs) and asking the labels ``selector``, or the
    HTTPException that says why not. An account bound to a pool goes with a run pinned to a node of it, or one whose
    selector asks pool=<name>."""
    s = tables.secrets
    query = _secret_query(user.user_id)
    query = query.where(s.c.id == secret_id) if secret_id is not None else query.where(s.c.name == name)
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.kind != "env" or row.deleted_at is not None:
        label = name if row is None else row.name
        raise HTTPException(
            404,
            f"you have no env secret named {label} on this hub, and an account is one of your env secrets: see "
            "`evo-agents hub account list`; nothing was dispatched",
        )
    problem = None
    if row.ended:
        problem = f"account {row.name} ended at {row.expires_at.isoformat()}: set a new value for it"
    elif row.paused_at is not None:
        problem = f"account {row.name} is paused: resume it with `evo-agents hub account resume {row.name}`"
    else:
        bound = await _bindings(conn, row.id, access.project_id)
        pool = await _pool_of(conn, pinned) if pinned is not None else _asked_pool(selector)
        problem = _unbound(row.name, access.name, bound, pinned, pool)
    if problem is not None:
        raise HTTPException(422, f"{problem}, or pick another account; nothing was dispatched")
    return Picked(row.id, row.env_var, row.name)


async def record_pick(conn: AsyncConnection, run_id: int, picked: Picked | None) -> None:
    """Write the account picked by hand for run ``run_id``, which its leases keep; nothing for None."""
    if picked is None:
        return
    ra = tables.run_accounts
    await conn.execute(insert(ra).values(run_id=run_id, env_var=picked.env_var, secret_id=picked.secret_id, how=MANUAL))


async def picked_for(conn: AsyncConnection, run_id: int) -> list[tuple[int, str]]:
    """(secret id, variable) of each account picked by hand for run ``run_id``, by variable."""
    ra = tables.run_accounts
    rows = await conn.execute(
        select(ra.c.secret_id, ra.c.env_var).where(ra.c.run_id == run_id, ra.c.how == MANUAL).order_by(ra.c.env_var)
    )
    return [(row.secret_id, row.env_var) for row in rows]


async def rerun_picks(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, run_id: int, body, pinned, selector=()
) -> list[Picked]:
    """The accounts a rerun of run ``run_id`` takes by hand: the one ``body`` names, none when it asks for the rules,
    else each one picked by hand for the run rerun, checked again as a dispatch checks it."""
    if body is not None and body.account is not None:
        return [await pick_account(conn, user, access, body.account, pinned, selector=selector)]
    if body is not None and body.account_by_rule:
        return []
    picks = []
    for secret_id, env_var in await picked_for(conn, run_id):
        try:
            picks.append(await pick_account(conn, user, access, "", pinned, secret_id=secret_id, selector=selector))
        except HTTPException as exc:
            raise HTTPException(
                exc.status_code,
                f"run {run_id} took its account for {env_var} by hand, and it cannot go with a rerun: {exc.detail}. "
                "Rerun it with another account, or with one picked by the rules (account_by_rule)",
            ) from None
    return picks

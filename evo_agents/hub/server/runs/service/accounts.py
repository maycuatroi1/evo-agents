"""The account a member picks by hand for a run (``docs/credentials.md``, section "Accounts"): ``account`` on the body
of a dispatch of steps, a plan run or an author run, and on a rerun, where none keeps the one picked for the run rerun.

A name is one of the dispatcher's own live env secrets, or provider accounts (``provider_accounts``), bound to the
run's project: a name that is neither of theirs is 404, whoever's it is; one past its end, paused, not bound to the project, or bound there only to workers
the run might not go to, is 422 saying why. ``<login>/<name>`` names the env secret ``name`` of member ``login`` that a
share for every run lends to the dispatcher on the run's project (``share_leases``: theirs, a unit of their primary
seat or above, or all): 404 when no such share covers them, 422 when its owner paused it, it ended or its owner bound
it to the project no more. It goes on any worker, as a share lends on any. Its share still decides each ask for the
run's leases: while it lends nothing (``share_leases._refusal``), the run takes an account by the rules, its own first. The pick is written to run_accounts with how ``manual`` in the
transaction that queues the run, and ``account_choice.choose_accounts`` keeps it on every ask for the run's leases;
an account picked by hand never fails over to another one. A provider account picked is written for the variable
``gateway:<prefix>`` (``gateway_runs.PICK_PREFIX``): the run's model must be one of its prefix, and its gateway key
reaches that account alone (``gateway_accounts``). Audit rows name it by its secret's id, never its name.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, tables
from evo_agents.hub.credentials import PROVIDER_KIND
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
    s, p = tables.secrets, tables.provider_accounts
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
            p.c.provider,
        )
        .outerjoin_from(s, tables.accounts, tables.accounts.c.secret_id == s.c.id)
        .outerjoin(p, p.c.secret_id == s.c.id)
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
    owner, bare = split_account(name)
    if secret_id is None and owner is not None and owner.lower() != user.login.lower():
        return await _pick_shared(conn, user, access, name=bare, owner=owner)
    query = _secret_query(user.user_id)
    query = query.where(s.c.id == secret_id) if secret_id is not None else query.where(s.c.name == bare)
    row = (await conn.execute(query)).one_or_none()
    if row is None and secret_id is not None:  # a rerun of a run that took a shared account by hand
        return await _pick_shared(conn, user, access, secret_id=secret_id)
    if row is None or row.kind not in ("env", PROVIDER_KIND) or row.deleted_at is not None:
        label = name if row is None else row.name
        raise HTTPException(
            404,
            f"you have no env secret named {label} on this hub, nor a provider account, and an account is one of "
            "them: see `evo-agents hub account list`; nothing was dispatched",
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
    if row.kind == PROVIDER_KIND:
        prefix = gateway_runs.prefix_of(row.provider or "", row.id)
        if prefix is None:
            raise HTTPException(422, f"account {row.name} serves no model through the gateway; nothing was dispatched")
        return Picked(row.id, gateway_runs.PICK_PREFIX + prefix, row.name)
    return Picked(row.id, row.env_var, row.name)


def split_account(name: str) -> tuple[str | None, str]:
    """(the login of ``<login>/<name>``, else None; the secret's name)."""
    owner, sep, bare = name.rpartition("/")
    return (owner, bare) if sep else (None, name)


async def _pick_shared(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, *, name=None, owner=None, secret_id=None
) -> Picked:
    """The env secret ``name`` of member ``owner`` (or, with ``secret_id``, the secret of that id) that a live share
    for every run lends the caller on the project, or the HTTPException that says why not."""
    # share_leases reaches the routes of runs, which import this module
    from evo_agents.hub.server.share_leases import grantee_clause, live_share

    s, cs, csp, a, u = (
        tables.secrets,
        tables.credential_shares,
        tables.credential_share_projects,
        tables.accounts,
        tables.users,
    )
    query = (
        select(
            s.c.id,
            s.c.name,
            s.c.env_var,
            s.c.expires_at,
            ((s.c.expires_at <= func.now()) & a.c.oauth_sealed.is_(None)).label("ended"),
            a.c.paused_at,
            u.c.login,
        )
        .join_from(cs, s, s.c.id == cs.c.secret_id)
        .join(u, u.c.id == s.c.owner_id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(
            live_share(),
            cs.c.run_id.is_(None),  # one for a single run is an admin's answer to that run's request
            exists().where(csp.c.share_id == cs.c.id, csp.c.project_id == access.project_id),
            await grantee_clause(conn, user.user_id),
            s.c.owner_id != user.user_id,
            s.c.kind == "env",
            s.c.deleted_at.is_(None),
        )
        .limit(1)
    )
    if secret_id is not None:
        query = query.where(s.c.id == secret_id)
    else:
        query = query.where(s.c.name == name, func.lower(u.c.login) == owner.lower())
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        whose = "that account" if secret_id is not None else f"account {owner}/{name}"
        raise HTTPException(
            404,
            f"no share lends {whose} to you on project {access.name}: see GET /v1/shared-accounts; nothing was "
            "dispatched",
        )
    label = f"{row.login}/{row.name}"
    problem = None
    if row.ended:
        problem = f"account {label} ended at {row.expires_at.isoformat()}"
    elif row.paused_at is not None:
        problem = f"its owner paused account {label}"
    elif not await _bindings(conn, row.id, access.project_id):
        problem = f"its owner no longer binds account {label} to project {access.name}"
    if problem is not None:
        raise HTTPException(422, f"{problem}: pick another account; nothing was dispatched")
    return Picked(row.id, row.env_var, label)


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

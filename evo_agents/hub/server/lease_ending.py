"""Giving back a run's leases, and revoking the GitHub tokens among them (``docs/credentials.md``, section "Life of a
lease"); moved out of ``credentials``, which re-exports every name here so its callers read ``credentials.end_leases``
and the like as before.

``end_leases`` marks revoked, in the caller's transaction, the leases of a run, of a worker or of that worker's run, and
adds one credential.revoke audit row per run; ``end_member_leases`` does it for every run a member dispatched in a
project. ``revoke_tokens`` then revokes at GitHub, once that transaction commits, each token of the leases marked
revoked that still holds its sealed value, and ``drop_expired`` drops the sealed values of tokens past their end. No
log line, audit row or error holds a value.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import BigInteger, distinct, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.github import GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad

log = logging.getLogger(__name__)

REVOKE_BATCH = 500  # GitHub tokens one pass of revoke_tokens takes


def _ids(values) -> str:
    return ",".join(str(value) for value in sorted(set(values))) or "-"


def _end_leases(run_id: int | None, worker_id: int | None):
    """One statement: mark revoked the leases not given back yet of run ``run_id`` and of worker ``worker_id`` (either
    may be None), in a data-modifying CTE, and per run they were of, its project, plan and step, how many leases ended,
    the secrets among them and how many GitHub tokens."""
    cl, r, projects = tables.credential_leases, tables.runs, tables.projects
    ending = update(cl).values(revoked_at=func.greatest(func.now(), cl.c.issued_at)).where(cl.c.revoked_at.is_(None))
    if run_id is not None:
        ending = ending.where(cl.c.run_id == run_id)
    if worker_id is not None:
        ending = ending.where(cl.c.worker_id == worker_id)
    ended = ending.returning(cl.c.run_id, cl.c.provider, cl.c.secret_id).cte("ended")
    secrets = func.array_remove(func.array_agg(distinct(ended.c.secret_id)), None, type_=ARRAY(BigInteger))
    return (
        select(
            ended.c.run_id,
            r.c.project_id,
            projects.c.name.label("project"),
            r.c.plan_id,
            r.c.step_key,
            func.count().label("leases"),
            secrets.label("secrets"),
            func.count().filter(ended.c.provider == "github-app").label("tokens"),
        )
        .join_from(ended, r, r.c.id == ended.c.run_id)
        .join(projects, projects.c.id == r.c.project_id)
        .group_by(ended.c.run_id, r.c.project_id, projects.c.name, r.c.plan_id, r.c.step_key)
        .order_by(ended.c.run_id)
    )


async def end_leases(
    conn: AsyncConnection,
    *,
    run_id: int | None = None,
    worker_id: int | None = None,
    actor_id: int | None = None,
    token_id: int | None = None,
    by: str,
) -> int:
    """Mark revoked, in the caller's transaction, the leases not given back yet of run ``run_id``, of worker
    ``worker_id``, or of that worker's run, and add a credential.revoke audit row per run naming ``by``; the actor is
    None for the hub itself. Their GitHub tokens keep their sealed values for ``revoke_tokens``, which revokes them at
    GitHub once the transaction commits. Returns how many leases were marked."""
    if run_id is None and worker_id is None:
        raise ValueError("end_leases needs a run, a worker or both")
    rows = (await conn.execute(_end_leases(run_id, worker_id))).all()
    for row in rows:
        target = (
            f"{run_target(row.project, row.plan_id, row.step_key, row.run_id)} leases={row.leases} "
            f"secrets={_ids(row.secrets)} github-app={row.tokens} by={by}"
        )
        await audit.record(
            conn,
            actor_id=actor_id,
            token_id=token_id,
            action=audit.CREDENTIAL_REVOKE,
            target=target,
            project_id=row.project_id,
        )
    return sum(row.leases for row in rows)


async def end_member_leases(
    conn: AsyncConnection, *, user_id: int, project_id: int, actor_id: int | None, token_id: int | None, by: str
) -> list[int]:
    """Mark revoked, in the caller's transaction, every lease still out of the runs member ``user_id`` dispatched in
    project ``project_id``, as ``end_leases`` does for each run: the member no longer holds writer there. Returns the
    runs, whose GitHub tokens the caller revokes once the transaction commits (``revoke_tokens``)."""
    cl, r = tables.credential_leases, tables.runs
    leased = (
        select(cl.c.run_id)
        .distinct()
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.revoked_at.is_(None), r.c.project_id == project_id, r.c.dispatched_by == user_id)
        .order_by(cl.c.run_id)
    )
    run_ids = list((await conn.execute(leased)).scalars())
    for run_id in run_ids:
        await end_leases(conn, run_id=run_id, actor_id=actor_id, token_id=token_id, by=by)
    return run_ids


def _pending_tokens(run_id: int | None, worker_id: int | None, batch: int):
    """The first ``batch`` leases marked revoked that still hold a sealed GitHub token that has not expired, of run
    ``run_id`` and of worker ``worker_id`` when given."""
    cl = tables.credential_leases
    statement = select(cl.c.id).where(
        cl.c.provider == "github-app",  # a gateway key is the gateway's to revoke (``gateway_leases.sweep``)
        cl.c.sealed_value.is_not(None),
        cl.c.revoked_at.is_not(None),
        cl.c.expires_at > func.now(),
    )
    if run_id is not None:
        statement = statement.where(cl.c.run_id == run_id)
    if worker_id is not None:
        statement = statement.where(cl.c.worker_id == worker_id)
    return statement.order_by(cl.c.id).limit(batch)


def _take_token(lease_id: int):
    """The sealed token of lease ``lease_id``, its row locked, unless another pass holds it or dropped it."""
    cl = tables.credential_leases
    return (
        select(cl.c.sealed_value, cl.c.nonce, cl.c.key_id)
        .where(cl.c.id == lease_id, cl.c.sealed_value.is_not(None), cl.c.revoked_at.is_not(None))
        .with_for_update(skip_locked=True)
    )


def _drop_sealed(*where):
    cl = tables.credential_leases
    return update(cl).values(sealed_value=None, nonce=None, key_id=None).where(*where)


def revoker(state) -> GitHubApp | None:
    """The client that revokes the leased GitHub tokens: either App's, since a token is revoked with itself."""
    return state.github_app or getattr(state, "curator_app", None)


async def revoke_tokens(
    engine: AsyncEngine,
    sealer: Sealer | None,
    app: GitHubApp | None,
    *,
    run_id: int | None = None,
    worker_id: int | None = None,
    batch: int = REVOKE_BATCH,
) -> dict:
    """Revoke at GitHub the tokens of leases marked revoked that still hold their sealed value and have not expired,
    of run ``run_id`` or worker ``worker_id`` when given, each in a transaction of its own that holds its row: the
    sealed value is dropped once GitHub revoked the token or no longer takes it. GitHub failing stops the pass and
    leaves the rest to the next one; a value that does not open is dropped, and its token ends at its expiry. Returns
    how many tokens were revoked, already gone, left for later and dropped unopened."""
    counts: Counter[str] = Counter()
    if sealer is None or app is None:
        return {"revoked": 0, "gone": 0, "left": 0, "unopened": 0}
    cl = tables.credential_leases
    async with engine.begin() as conn:
        pending = list((await conn.execute(_pending_tokens(run_id, worker_id, batch))).scalars())
    for index, lease_id in enumerate(pending):
        async with engine.begin() as conn:
            row = (await conn.execute(_take_token(lease_id))).one_or_none()
            if row is None:  # revoked meanwhile by another pass, or being revoked now
                continue
            try:
                token = sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(lease_id))
            except Unsealable as exc:
                why = {"lease_id": lease_id, "why": str(exc)}
                log.warning("a leased GitHub token does not open; it ends at its expiry", extra=why)
                await conn.execute(_drop_sealed(cl.c.id == lease_id))
                counts["unopened"] += 1
                continue
            try:
                revoked = await app.revoke(token)
            except GitHubUnavailable as exc:
                counts["left"] += len(pending) - index
                log.warning("GitHub tokens left to revoke later", extra={"left": counts["left"], "why": str(exc)})
                break
            await conn.execute(_drop_sealed(cl.c.id == lease_id))
            counts["revoked" if revoked else "gone"] += 1
    report = {key: counts.get(key, 0) for key in ("revoked", "gone", "left", "unopened")}
    if pending:
        log.info("leased GitHub tokens revoked", extra={**report, "run_id": run_id, "worker_id": worker_id})
    return report


async def drop_expired(conn: AsyncConnection) -> int:
    """Drop the sealed values of the GitHub tokens past their end, which GitHub no longer takes; how many."""
    cl = tables.credential_leases
    ended = (cl.c.provider == "github-app", cl.c.sealed_value.is_not(None), cl.c.expires_at <= func.now())
    return (await conn.execute(_drop_sealed(*ended))).rowcount

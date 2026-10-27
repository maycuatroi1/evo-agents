"""A run that runs as its owner: the member who dispatched it lets its agent call the hub's REST API as them, with
their grants on every project and hub admin when they are one, secrets, tokens and workers aside
(``docs/credentials.md``).

The owner chooses at dispatch (``as_me`` of POST /v1/projects/{p}/runs, .../plan-runs and .../runs/{id}/rerun, whose
rerun keeps the choice of the run it reruns when it names none) and later (PUT /v1/projects/{p}/runs/{id}/access,
``runs.service.controls.change_access``); the run that resumes a parked run and the next attempt of a lost one keep
it (runs.as_owner), and the run's view says it (``Run.as_owner``). Only the kinds of AS_OWNER_KINDS may, and never a run
of the Curator: the others are the hub's, its schedules' or a chat's, not one member's work.

While the worker holding such a run asks for its leases (``credentials.lease_credentials``), ``lease`` adds one of kind
HUB_KIND and provider RUN_ACCESS: the run's token ``evr_...``, a token of kind run of the owner in ``tokens`` (its
SHA-256 alone, as for every token, and ``run:<id>`` as its host), which the lease names (credential_leases.token_id) and
keeps sealed under its own id, so an ask made again hands the same token back; with the variable HUB_TOKEN_ENV and the
hub's URL as base_url. The token is live while its lease is out (``security._authenticate_run``) and ends with it, at
once: when the run leaves the held states or its worker gives it back or is revoked, when the owner loses writer on the
project (``lease_ending.end_leases`` revokes the token of each run-access lease it ends), when the owner stops letting
the run (``end_access``) and when the owner revokes the token itself (DELETE /v1/tokens/{id}, ``token_revoked``). A run
that resumes gets a token of its own at its first ask. A worker of an older release reads the lease, masks its value and
puts nothing of it into the agent's environment, which takes kind env alone.

What the token never reaches is ``run_access_rules``'s, and on the routes open to it every rule of the agent of a run
holds (``Principal.acts_for_run``): it answers no decision (``decisions.answer_decision``), starts no run and hands no
agent work (``run_access_rules.refuse_new_run``), approves nothing and lets no run run as its owner
(``refuse_onward``). The rerun the agent of a triage makes never runs as its owner (``triage_actions``), and a stream
the token opened stops at its next look once the token ended (``security.still_live``). Each grant and each stop is an
audit row run_access.grant or run_access.revoke, each token a lease names is in the credential.lease row
(``run-access=token:<id>``), and no log line, event, audit row or error holds the token.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import HUB_KIND, HUB_TOKEN_ENV, RUN_ACCESS, Lease
from evo_agents.hub.server import audit
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad
from evo_agents.hub.server.security import RUN, Principal, issue_token

log = logging.getLogger(__name__)

AS_OWNER_KINDS = ("step", "plan", "author")  # the kinds of run an owner may let run as them
ONWARD = (
    "a run's token (evr_...) never lets a run run as its owner, so no run passes the owner's account on: ask with "
    "as_me false, or let the owner choose with their own sign-in"
)


def may_run_as_owner(kind: str, curator: bool) -> str | None:
    """Why a run of ``kind`` (``curator``: a run of the Curator) may not run as its owner; None when it may."""
    if curator:
        return "a run of the Curator acts under its charter, never as a member"
    if kind not in AS_OWNER_KINDS:
        return f"a {kind} run never runs as its owner: only a run of a step, a plan run or an author run does"
    return None


def refuse_onward(user: Principal, as_owner: bool) -> None:
    """403 when ``user``, a run's token, asks that a run run as its owner (``ONWARD``)."""
    if as_owner and user.kind == RUN:
        raise HTTPException(403, ONWARD)


async def record(conn: AsyncConnection, user: Principal | None, project_id: int, target: str, *, granted: bool) -> None:
    """The audit row run_access.grant (``granted``) or run_access.revoke of the run ``target`` names, which ends with
    how it came (`` by=dispatch`` and the like); ``user`` None for the hub itself."""
    await audit.record(
        conn,
        actor_id=None if user is None else user.user_id,
        token_id=None if user is None else user.token_id,
        action=audit.RUN_ACCESS_GRANT if granted else audit.RUN_ACCESS_REVOKE,
        target=target,
        project_id=project_id,
    )


def token_host(run_id: int) -> str:
    """The host a run's token is listed with (tokens.host, which only a web session leaves empty): its run."""
    return f"run:{run_id}"


def _lease(lease_id: int, owner: str, token: str, root: str) -> Lease:
    return Lease(
        lease_id, HUB_KIND, RUN_ACCESS, f"{RUN_ACCESS}:{owner}", env_var=HUB_TOKEN_ENV, value=token, base_url=root
    )


def _held(run_id: int, worker_id: int):
    """The newest run-access lease run ``run_id`` holds on this worker whose token is live, with its sealed token."""
    cl, t = tables.credential_leases, tables.tokens
    return (
        select(cl.c.id, cl.c.token_id, cl.c.sealed_value, cl.c.nonce, cl.c.key_id)
        .join_from(cl, t, t.c.id == cl.c.token_id)
        .where(
            cl.c.run_id == run_id,
            cl.c.worker_id == worker_id,
            cl.c.provider == RUN_ACCESS,
            cl.c.revoked_at.is_(None),
            cl.c.sealed_value.is_not(None),
            t.c.revoked_at.is_(None),
            t.c.expires_at > func.now(),
        )
        .order_by(cl.c.id.desc())
        .limit(1)
    )


async def lease(
    conn: AsyncConnection, run, worker_id: int, sealer: Sealer, root: str
) -> tuple[list[Lease], int | None]:
    """The lease of the run's token, in the transaction of the ask of worker ``worker_id``, with the token's id: the
    lease this worker holds, its token opened again, else a new token and lease (any other run-access lease of the run
    on this worker ended first); none for a run its owner does not let run as them, or that may not
    (``may_run_as_owner``). ``run`` is the ask's ``credentials._Run``, ``root`` the hub's URL."""
    if not run.as_owner or may_run_as_owner(run.kind, run.curator is not None) is not None:
        return [], None
    held = (await conn.execute(_held(run.id, worker_id))).one_or_none()
    if held is not None:
        try:
            token = sealer.open(Sealed(held.sealed_value, held.nonce, held.key_id), lease_aad(held.id))
            return [_lease(held.id, run.owner, token, root)], held.token_id
        except Unsealable as exc:
            log.warning("a run's token does not open; a new one is made", extra={"lease_id": held.id, "why": str(exc)})
    await end_access(conn, run.id, worker_id=worker_id)
    issued = await issue_token(conn, run.owner_id, RUN, token_host(run.id))
    cl = tables.credential_leases
    lease_id = (
        await conn.execute(
            insert(cl)
            .values(
                run_id=run.id,
                worker_id=worker_id,
                secret_id=None,
                provider=RUN_ACCESS,
                target=HUB_TOKEN_ENV,
                token_id=issued.token_id,
                **run.lease_columns(),
            )
            .returning(cl.c.id)
        )
    ).scalar_one()
    sealed = sealer.seal(issued.token, lease_aad(lease_id))  # bound to the lease's id, so inserted first
    await conn.execute(
        update(cl)
        .values(sealed_value=sealed.ciphertext, nonce=sealed.nonce, key_id=sealed.key_id)
        .where(cl.c.id == lease_id)
    )
    return [_lease(lease_id, run.owner, issued.token, root)], issued.token_id


async def end_access(conn: AsyncConnection, run_id: int, *, worker_id: int | None = None) -> int:
    """End, in the caller's transaction, the run-access leases still out of run ``run_id`` (on worker ``worker_id``
    when given): each lease is marked revoked, its sealed token dropped and its token revoked. Returns how many."""
    cl, t = tables.credential_leases, tables.tokens
    ending = (
        update(cl)
        .values(revoked_at=func.greatest(func.now(), cl.c.issued_at), sealed_value=None, nonce=None, key_id=None)
        .where(cl.c.run_id == run_id, cl.c.provider == RUN_ACCESS, cl.c.revoked_at.is_(None))
        .returning(cl.c.token_id)
    )
    if worker_id is not None:
        ending = ending.where(cl.c.worker_id == worker_id)
    token_ids = [token_id for token_id in (await conn.execute(ending)).scalars() if token_id is not None]
    if token_ids:
        await conn.execute(
            update(t).values(revoked_at=func.now()).where(t.c.id.in_(token_ids), t.c.revoked_at.is_(None))
        )
    return len(token_ids)


async def token_revoked(conn: AsyncConnection, user: Principal, token_id: int) -> None:
    """The owner revoked the token of a run, token ``token_id``, in the caller's transaction: the run no longer runs as
    them, its run-access leases end, and the audit row run_access.revoke says so (``by=token``). Nothing for a token
    no run-access lease names."""
    cl, r, p = tables.credential_leases, tables.runs, tables.projects
    found = (
        await conn.execute(
            select(r.c.id, r.c.project_id, p.c.name, r.c.plan_id, r.c.step_key, r.c.as_owner)
            .join_from(cl, r, r.c.id == cl.c.run_id)
            .join(p, p.c.id == r.c.project_id)
            .where(cl.c.token_id == token_id)
        )
    ).one_or_none()
    if found is None:
        return
    await conn.execute(update(r).values(as_owner=False, as_owner_since=None).where(r.c.id == found.id))
    await end_access(conn, found.id)
    if found.as_owner:
        target = f"{run_target(found.name, found.plan_id, found.step_key, found.id)} by=token"
        await record(conn, user, found.project_id, target, granted=False)

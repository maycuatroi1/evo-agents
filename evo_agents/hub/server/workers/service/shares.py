"""Shared workers narrowed to units of the org chart (plan credential-sharing, criterion a5), under /v1/org/workers.

- GET /v1/org/workers: the caller's live workers, every live worker for a hub admin, each with whether it is shared,
  the units it is narrowed to and what the newest report of its doctor found (null until its daemon sends one).
- PUT /v1/org/workers/{worker_id}/units/{slug}: narrow the worker to unit ``slug``, which must be live (422 otherwise,
  ``credential_shares.live_unit``), and make it shared when it was not. A unit it is narrowed to already changes
  nothing and is not audited. 409 with the reason when its newest doctor report is older than 48 hours, missing or
  has a high finding (``doctor.share_refusal``).
- DELETE /v1/org/workers/{worker_id}/units/{slug}: take the unit back (404 when the worker is not narrowed to it).
  Taking back its last unit also turns sharing off, so narrowing a worker never ends with it shared with every writer.

A shared worker with no unit takes the runs of every member holding writer on a project it serves, as before; with
units, only those of its owner and of a member whose live primary seat is in one of them or below one
(``evo_agents.hub.server.runs.service.worker_fit.unit_admits``). PATCH /v1/workers/{id} with ``shared`` still turns
sharing on and off and keeps the units, which apply again once the worker is shared. Its owner and a hub admin write
(404 for anyone else, as for a worker the hub never gave out; 409 for a revoked worker); the agent of a run never
does. Each change adds an audit row worker.share or worker.unshare.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.credential_shares import live_unit
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.workers.models.shares import NO_WORKER, WorkerShares
from evo_agents.hub.server.workers.models.workers import revoked
from evo_agents.hub.server.workers.repository import shares as store
from evo_agents.hub.server.workers.service.doctor import share_refusal

log = logging.getLogger(__name__)


def writer(user: Principal) -> None:
    """403 for the agent of a run, which never changes whom a worker is shared with."""
    if user.acts_for_run is not None:
        raise HTTPException(403, "the agent of a run never changes whom a worker is shared with")


async def one(conn: AsyncConnection, worker_id: int) -> WorkerShares:
    return WorkerShares(**(await store.one(conn, worker_id))._mapping)


async def locked_worker(conn: AsyncConnection, user: Principal, worker_id: int):
    """The worker's (owner_id, owner login, name, shared, its newest doctor report), locked: one of the caller's, or any
    for a hub admin; 404 otherwise, 409 when it is revoked."""
    row = await store.locked_worker(conn, worker_id)
    if row is None or (row.owner_id != user.user_id and not user.admin):
        raise NotFound(NO_WORKER.format(id=worker_id))
    if row.revoked_at is not None:
        raise Conflict(revoked(worker_id, row.revoked_at))
    return row


async def record(conn: AsyncConnection, user: Principal, action: str, worker_id: int, row, slug: str, flip: str):
    by = "owner" if row.owner_id == user.user_id else "admin"
    target = f"worker:{worker_id} name={row.name} owner={row.login} unit={slug} by={by}{flip}"
    await Auditor(conn, None, user).record(action, target)


async def listed(conn: AsyncConnection, user: Principal) -> list[WorkerShares]:
    """The caller's live workers, every live worker for a hub admin, with whom each is shared."""
    found = await store.rows(conn, None if user.admin else user.user_id)
    return [WorkerShares(**row._mapping) for row in found]


async def share(conn: AsyncConnection, user: Principal, worker_id: int, slug: str) -> WorkerShares:
    """Narrow the worker to the live unit ``slug``, sharing it when it was not; 409 when its doctor report says no."""
    row = await locked_worker(conn, user, worker_id)
    unit_id = await live_unit(conn, slug)
    refusal = share_refusal(row.doctor_at, row.doctor_high, row.doctor_fresh)
    if refusal is not None:
        raise Conflict(f"worker {row.name} of {row.login} is not shared with unit {slug}: {refusal}")
    added = await store.add_unit(conn, worker_id, unit_id, user.user_id)
    if added or not row.shared:
        flip = ""
        if not row.shared:
            await store.set_shared(conn, worker_id, True)
            flip = " shared=false->true"
        await record(conn, user, audit.WORKER_SHARE, worker_id, row, slug, flip)
        log.info("worker narrowed to a unit", extra={"worker_id": worker_id, "unit": slug})
    return await one(conn, worker_id)


async def unshare(conn: AsyncConnection, user: Principal, worker_id: int, slug: str) -> WorkerShares:
    """Take unit ``slug`` back from the worker; with its last unit, sharing ends too. 404 when it is not narrowed to
    it."""
    row = await locked_worker(conn, user, worker_id)
    if not await store.drop_unit(conn, worker_id, slug):
        raise NotFound(f"worker {row.name} of {row.login} is not narrowed to unit {slug}")
    flip = ""
    if not await store.unit_count(conn, worker_id) and row.shared:
        await store.set_shared(conn, worker_id, False)
        flip = " shared=true->false"
    await record(conn, user, audit.WORKER_UNSHARE, worker_id, row, slug, flip)
    log.info("worker unit taken back", extra={"worker_id": worker_id, "unit": slug})
    return await one(conn, worker_id)

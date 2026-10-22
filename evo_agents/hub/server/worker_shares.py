"""Shared workers narrowed to units of the org chart (plan credential-sharing, criterion a5), under /v1/org/workers.

- GET /v1/org/workers: the caller's live workers, every live worker for a hub admin, each with whether it is shared,
  the units it is narrowed to and what the newest report of its doctor found (null until its daemon sends one).
- PUT /v1/org/workers/{worker_id}/units/{slug}: narrow the worker to unit ``slug``, which must be live (422 otherwise,
  ``credential_shares.live_unit``), and make it shared when it was not. A unit it is narrowed to already changes
  nothing and is not audited. 409 with the reason when its newest doctor report is older than 48 hours, missing or
  has a high finding (``evo_agents.hub.server.worker_doctor``).
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
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import Text

from evo_agents.hub import org, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.credential_shares import live_unit
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.worker_doctor import doctor_fresh, share_refusal

log = logging.getLogger(__name__)

MAX_WORKER_ID = 2**63 - 1
NO_WORKER = "you have no worker {id} on this hub: see GET /v1/org/workers"
WorkerId = Annotated[int, Path(ge=1, le=MAX_WORKER_ID)]
UnitSlug = Annotated[str, Path(pattern=org.SLUG_PATTERN)]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}

router = APIRouter(prefix="/v1/org/workers", tags=["org"], responses={401: {"model": ErrorBody}})


class WorkerShares(BaseModel):
    """Whom a worker is shared with: its owner's writers, narrowed to ``units`` when it names any."""

    id: int
    worker: str = Field(description="the worker's name, unique among its owner's live workers")
    owner: str = Field(description="the login of the member who registered it")
    shared: bool = Field(description="whether other members' runs may go to it")
    units: list[str] = Field(
        description="the slugs of the units it is narrowed to, sorted; none: shared with every writer of a project "
        "it serves"
    )
    doctor_at: datetime | None = Field(None, description="when its daemon sent the newest doctor report; null: none")
    doctor_high: int | None = Field(None, description="findings of severity high in that report; null: no report")
    doctor_medium: int | None = Field(None, description="findings of severity medium in that report; null: no report")


def _listed(*where):
    w, users, ws, u = tables.workers, tables.users, tables.worker_shares, tables.org_units
    units = select(u.c.slug).join_from(ws, u, u.c.id == ws.c.unit_id).where(ws.c.worker_id == w.c.id).order_by(u.c.slug)
    return (
        select(
            w.c.id,
            w.c.name.label("worker"),
            users.c.login.label("owner"),
            w.c.shared,
            func.array(units.scalar_subquery(), type_=ARRAY(Text)).label("units"),
            w.c.doctor_at,
            w.c.doctor_high,
            w.c.doctor_medium,
        )
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.revoked_at.is_(None), *where)
        .order_by(users.c.login, w.c.name, w.c.id)
    )


async def _one(conn: AsyncConnection, worker_id: int) -> WorkerShares:
    row = (await conn.execute(_listed(tables.workers.c.id == worker_id))).one()
    return WorkerShares(**row._mapping)


def _writer(user: Principal) -> None:
    if user.scope is not None:
        raise HTTPException(403, "the agent of a run never changes whom a worker is shared with")


async def _worker(conn: AsyncConnection, user: Principal, worker_id: int):
    """The worker's (owner_id, owner login, name, shared, its newest doctor report), locked: one of the caller's, or any
    for a hub admin; 404 otherwise, 409 when it is revoked."""
    w, users = tables.workers, tables.users
    fresh = doctor_fresh(w.c.doctor_at).label("doctor_fresh")
    query = (
        select(w.c.owner_id, users.c.login, w.c.name, w.c.shared, w.c.revoked_at, w.c.doctor_at, w.c.doctor_high, fresh)
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.id == worker_id)
        .with_for_update(of=w)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or (row.owner_id != user.user_id and not user.admin):
        raise HTTPException(404, NO_WORKER.format(id=worker_id))
    if row.revoked_at is not None:
        raise HTTPException(409, f"worker {worker_id} was revoked at {row.revoked_at.isoformat()}; it takes no runs")
    return row


async def _record(conn: AsyncConnection, user: Principal, action: str, worker_id: int, row, slug: str, flip: str):
    by = "owner" if row.owner_id == user.user_id else "admin"
    target = f"worker:{worker_id} name={row.name} owner={row.login} unit={slug} by={by}{flip}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)


@router.get("", response_model=list[WorkerShares])
async def list_worker_shares(request: Request, user: CurrentUser) -> list[WorkerShares]:
    """The caller's live workers, every live worker for a hub admin, with whom each is shared."""
    where = [] if user.admin else [tables.workers.c.owner_id == user.user_id]
    async with request.app.state.engine.begin() as conn:
        return [WorkerShares(**row._mapping) for row in await conn.execute(_listed(*where))]


@router.put("/{worker_id}/units/{slug}", response_model=WorkerShares, responses=REFUSALS)
async def share_worker(request: Request, worker_id: WorkerId, slug: UnitSlug, user: CurrentUser) -> WorkerShares:
    """Narrow the worker to a live unit of the org chart, sharing it when it was not."""
    _writer(user)
    ws, w = tables.worker_shares, tables.workers
    async with request.app.state.engine.begin() as conn:
        row = await _worker(conn, user, worker_id)
        unit_id = await live_unit(conn, slug)
        refusal = share_refusal(row.doctor_at, row.doctor_high, row.doctor_fresh)
        if refusal is not None:
            raise HTTPException(409, f"worker {row.name} of {row.login} is not shared with unit {slug}: {refusal}")
        added = await conn.execute(
            insert(ws)
            .values(worker_id=worker_id, unit_id=unit_id, created_by=user.user_id)
            .on_conflict_do_nothing(index_elements=[ws.c.worker_id, ws.c.unit_id])
            .returning(ws.c.unit_id)
        )
        if added.first() is not None or not row.shared:
            flip = ""
            if not row.shared:
                await conn.execute(update(w).values(shared=True).where(w.c.id == worker_id))
                flip = " shared=false->true"
            await _record(conn, user, audit.WORKER_SHARE, worker_id, row, slug, flip)
            log.info("worker narrowed to a unit", extra={"worker_id": worker_id, "unit": slug})
        return await _one(conn, worker_id)


@router.delete("/{worker_id}/units/{slug}", response_model=WorkerShares, responses=REFUSALS)
async def unshare_worker(request: Request, worker_id: WorkerId, slug: UnitSlug, user: CurrentUser) -> WorkerShares:
    """Take a unit back from the worker; with its last unit, sharing ends too."""
    _writer(user)
    ws, w, u = tables.worker_shares, tables.workers, tables.org_units
    async with request.app.state.engine.begin() as conn:
        row = await _worker(conn, user, worker_id)
        unit = select(u.c.id).where(u.c.slug == slug).scalar_subquery()
        gone = await conn.execute(delete(ws).where(ws.c.worker_id == worker_id, ws.c.unit_id == unit))
        if not gone.rowcount:
            raise HTTPException(404, f"worker {row.name} of {row.login} is not narrowed to unit {slug}")
        left = (await conn.execute(select(func.count()).where(ws.c.worker_id == worker_id))).scalar_one()
        flip = ""
        if not left and row.shared:
            await conn.execute(update(w).values(shared=False).where(w.c.id == worker_id))
            flip = " shared=true->false"
        await _record(conn, user, audit.WORKER_UNSHARE, worker_id, row, slug, flip)
        log.info("worker unit taken back", extra={"worker_id": worker_id, "unit": slug})
        return await _one(conn, worker_id)

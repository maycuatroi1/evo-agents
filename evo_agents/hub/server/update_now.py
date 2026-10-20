"""An owner's Update now of a worker, and the commit of the tag nightly the hub last read.

POST /v1/workers/{id}/update-now asks the worker's daemon to update itself to nightly now rather than once idle: it
claims no new run, lets the runs it holds end (none is stopped), updates, and claims again. POST
/v1/workers/{id}/update-now/cancel withdraws the request. Both take the rights and the refusals of drain
(``evo_agents.hub.server.workers``): the owner or a hub admin, 404 for anyone else, 409 for a revoked worker; and the
hub refuses a request with 409 and why when the worker's version is locked, when its daemon does not say update_now
in its features, or when it runs the commit of nightly the hub read already. Asking again while a request is open, or
cancelling when none is, changes nothing and is not audited; every other request and cancel is.

The heartbeat answer carries the open request (``update_now``) to a daemon that says update_now, and the hub ends the
request on the heartbeat that shows it ended (``settle``): current, the daemon runs the commit of nightly the hub
read; failed or impossible, as the daemon reports them in self_update.update_now for this request; impossible too
when the worker's version is locked, when the daemon says it is locked or cannot update itself (self_update.state),
or when it no longer says update_now. The worker keeps the last request with how and when it ended
(workers.update_*), which GET /v1/workers shows.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.runs.models import HeartbeatRequest, NightlyRead, UpdateNowRequest
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

LOCKED = "its owner locked the worker's version"
NOT_SAID = f"its daemon does not say {runs.UPDATE_NOW} in its features"
CANNOT = ("locked", "unsupported")  # the states of self_update in which the daemon cannot update itself


async def nightly_read(conn: AsyncConnection) -> NightlyRead | None:
    """Where nightly pointed when the hub last read it, or None before the first read."""
    nightly = tables.hub_nightly
    row = (await conn.execute(select(nightly.c.commit_sha, nightly.c.read_at))).one_or_none()
    return None if row is None else NightlyRead(commit=row.commit_sha, read_at=row.read_at)


def nightly_columns():
    """The columns of a worker's listing that show the hub's read of nightly: the same for every worker."""
    nightly = tables.hub_nightly
    return (
        select(nightly.c.commit_sha).scalar_subquery().label("nightly_commit"),
        select(nightly.c.read_at).scalar_subquery().label("nightly_read_at"),
    )


def update_columns():
    w = tables.workers
    return (w.c.update_requested_at, w.c.update_ended_at, w.c.update_outcome, w.c.update_reason)


def shown(fields: dict) -> None:
    """Fold a listed worker's columns of the request and of nightly into update_now and nightly, in place."""
    requested_at, ended_at, outcome, reason = (fields.pop(column.name) for column in update_columns())
    fields["update_now"] = (
        None
        if requested_at is None
        else {"requested_at": requested_at, "ended_at": ended_at, "outcome": outcome, "reason": reason}
    )
    commit, read_at = fields.pop("nightly_commit"), fields.pop("nightly_read_at")
    fields["nightly"] = None if commit is None else {"commit": commit, "read_at": read_at}


async def _end(conn: AsyncConnection, worker_id: int, outcome: str, reason: str | None) -> None:
    w = tables.workers
    values = {"update_ended_at": func.now(), "update_outcome": outcome, "update_reason": reason}
    await conn.execute(update(w).values(**values).where(w.c.id == worker_id))
    log.info("update now ended", extra={"worker_id": worker_id, "outcome": outcome})


def _ending(body: HeartbeatRequest, requested_at: datetime, locked: bool, nightly: NightlyRead | None):
    """(outcome, reason) when this heartbeat shows the open request ended, else None."""
    report = body.self_update
    if nightly is not None and body.agent_commit == nightly.commit:
        return "current", None
    asked = report.update_now if report is not None else None
    if asked is not None and asked.requested_at == requested_at and asked.ended in runs.UPDATE_NOW_DAEMON_ENDS:
        return asked.ended, report.error if asked.ended == "failed" else report.reason
    if locked:
        return "impossible", LOCKED
    if report is not None and report.state in CANNOT:
        return "impossible", report.reason or f"its daemon says {report.state}"
    if runs.UPDATE_NOW not in (body.features or ()):
        return "impossible", NOT_SAID
    return None


async def settle(
    conn: AsyncConnection, worker_id: int, body: HeartbeatRequest, nightly: NightlyRead | None
) -> UpdateNowRequest | None:
    """The worker's open request as the heartbeat answer carries it, once this heartbeat's ending of it, if any, is
    recorded; None when none is open."""
    w = tables.workers
    statement = select(w.c.update_requested_at, w.c.version_locked).where(
        w.c.id == worker_id, w.c.update_requested_at.is_not(None), w.c.update_ended_at.is_(None)
    )
    row = (await conn.execute(statement)).one_or_none()
    if row is None:
        return None
    ending = _ending(body, row.update_requested_at, row.version_locked, nightly)
    if ending is not None:
        await _end(conn, worker_id, *ending)
        return None
    return UpdateNowRequest(requested_at=row.update_requested_at)


def _refusal(worker_id: int, row, nightly: NightlyRead | None) -> str | None:
    if row.version_locked:
        return f"worker {worker_id} cannot update now: {LOCKED}; unlock it first (PATCH /v1/workers/{worker_id})"
    if runs.UPDATE_NOW not in (row.features or ()):
        return (
            f"worker {worker_id} cannot update now: {NOT_SAID}, too old a daemon for this; it still updates itself to "
            "nightly once idle"
        )
    if nightly is not None and row.agent_commit == nightly.commit:
        return f"worker {worker_id} runs {nightly.commit[:12]} already, the commit nightly points to"
    return None


async def set_request(conn: AsyncConnection, user: Principal, worker_id: int, target: str, *, open_it: bool) -> None:
    """Open the worker's request (``open_it``) or cancel it, audited when it changes; the caller checked the worker
    is one ``user`` may drain and not revoked, and holds its row locked."""
    w = tables.workers
    columns = (w.c.version_locked, w.c.features, w.c.agent_commit, w.c.update_requested_at, w.c.update_ended_at)
    row = (await conn.execute(select(*columns).where(w.c.id == worker_id))).one()
    is_open = row.update_requested_at is not None and row.update_ended_at is None
    if open_it:
        refusal = _refusal(worker_id, row, await nightly_read(conn))
        if refusal is not None:
            raise HTTPException(409, refusal)
        if is_open:
            return
        values = {"update_requested_at": func.now(), "update_ended_at": None, "update_outcome": None}
        await conn.execute(update(w).values(**values, update_reason=None).where(w.c.id == worker_id))
        action = audit.WORKER_UPDATE_NOW
    else:
        if not is_open:
            return
        await _end(conn, worker_id, "cancelled", None)
        action = audit.WORKER_UPDATE_NOW_CANCEL
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
    log.info("update now asked" if open_it else "update now cancelled", extra={"worker_id": worker_id})

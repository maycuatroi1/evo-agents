"""An owner's Install browser of a worker: Chromium put on it from the hub, as `evo-agents worker browser install` does.

POST /v1/workers/{id}/browser-install asks the worker's daemon to install Chromium; POST
/v1/workers/{id}/browser-install/cancel withdraws the request. Both take the rights and the refusals of drain
(``evo_agents.hub.server.workers``): the owner or a hub admin, 404 for anyone else, 409 for a revoked worker; and the
hub refuses a request with 409 and why when the worker's daemon does not say browser_install in its features, or when
its heartbeat lists the feature browser already. Asking again while a request is open, or cancelling when none is,
changes nothing and is not audited; every other request and cancel is.

The heartbeat answer carries the open request (``browser_install``) to a daemon that says browser_install, which
installs Chromium next to the runs it holds (none is stopped, and claims go on), and the hub ends the request on the
heartbeat that shows it ended (``settle``): installed, the heartbeat lists the feature browser; failed, as the daemon
reports it in browser_install for this request, with its error; impossible, when the daemon no longer says
browser_install. The worker keeps the last request with how and when it ended (workers.browser_install_*), which GET
/v1/workers shows.

The hub opens a request on its own too (``ask_for_queued``, on each pass of the reaper): for a queued run dispatched
with browser that no worker can take, the nearest worker of the run's owner that lacks nothing but Chromium, and whose
daemon says browser_install, gets one, audited with no actor and the run in its target, and the run waits for it
(``queue_fit``) instead of asking the owner to install Chromium. Not a worker whose last request ended without
Chromium (cancelled, failed or impossible) less than REASK_AFTER ago: its diagnosis says how that ended.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.runs.models import BrowserInstallRequest, HeartbeatRequest
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

NOT_SAID = f"its daemon does not say {runs.BROWSER_INSTALL} in its features"
REASK_AFTER = timedelta(hours=24)  # a request that ended without Chromium: the hub does not open another before
UNFINISHED_AFTER = timedelta(minutes=30)  # an open request older than this no longer counts as a wait (queue_fit)
ASK_BATCH = 50  # the queued runs with browser one pass of ``ask_for_queued`` reads
_WITHOUT_CHROMIUM = ("cancelled", "failed", "impossible")


def columns():
    w = tables.workers
    return (
        w.c.browser_install_requested_at,
        w.c.browser_install_ended_at,
        w.c.browser_install_outcome,
        w.c.browser_install_reason,
    )


def shown(fields: dict) -> None:
    """Fold a listed worker's columns of the request into browser_install, in place."""
    requested_at, ended_at, outcome, reason = (fields.pop(column.name) for column in columns())
    fields["browser_install"] = (
        None
        if requested_at is None
        else {"requested_at": requested_at, "ended_at": ended_at, "outcome": outcome, "reason": reason}
    )


async def _end(conn: AsyncConnection, worker_id: int, outcome: str, reason: str | None) -> None:
    w = tables.workers
    values = {
        "browser_install_ended_at": func.now(),
        "browser_install_outcome": outcome,
        "browser_install_reason": reason,
    }
    await conn.execute(update(w).values(**values).where(w.c.id == worker_id))
    log.info("browser install ended", extra={"worker_id": worker_id, "outcome": outcome})


def _ending(body: HeartbeatRequest, requested_at: datetime):
    """(outcome, reason) when this heartbeat shows the open request ended, else None."""
    features = body.features or ()
    if runs.BROWSER in features and body.browser is not None:
        return "installed", None
    report = body.browser_install
    if report is not None and report.requested_at == requested_at and report.ended in runs.BROWSER_INSTALL_DAEMON_ENDS:
        return report.ended, report.error
    if runs.BROWSER_INSTALL not in features:
        return "impossible", NOT_SAID
    return None


async def settle(conn: AsyncConnection, worker_id: int, body: HeartbeatRequest) -> BrowserInstallRequest | None:
    """The worker's open request as the heartbeat answer carries it, once this heartbeat's ending of it, if any, is
    recorded; None when none is open."""
    w = tables.workers
    statement = select(w.c.browser_install_requested_at).where(
        w.c.id == worker_id,
        w.c.browser_install_requested_at.is_not(None),
        w.c.browser_install_ended_at.is_(None),
    )
    requested_at = (await conn.execute(statement)).scalar_one_or_none()
    if requested_at is None:
        return None
    ending = _ending(body, requested_at)
    if ending is not None:
        await _end(conn, worker_id, *ending)
        return None
    return BrowserInstallRequest(requested_at=requested_at)


def _refusal(worker_id: int, row) -> str | None:
    features = row.features or ()
    if runs.BROWSER in features and row.browser is not None:
        return f"worker {worker_id} has Chromium {row.browser.get('chromium')} installed already"
    if runs.BROWSER_INSTALL not in features:
        return (
            f"worker {worker_id} cannot install a browser from the hub: {NOT_SAID}, too old a daemon for this; update it, "
            "or run `evo-agents worker browser install` on the machine"
        )
    return None


def is_open(row) -> bool:
    """Whether the worker ``row`` (with ``columns``) has a request no heartbeat ended yet."""
    return row.browser_install_requested_at is not None and row.browser_install_ended_at is None


def held_back(row, now: datetime | None = None) -> bool:
    """Whether the last request of the worker ``row`` (with ``columns``) ended without Chromium (cancelled, failed or
    impossible) less than REASK_AFTER before ``now``: the hub opens none over it before."""
    ended_at = row.browser_install_ended_at
    if ended_at is None or row.browser_install_outcome not in _WITHOUT_CHROMIUM:
        return False
    return (now or datetime.now(UTC)) - ended_at < REASK_AFTER


def askable(row, owner_id: int, now: datetime | None = None) -> bool:
    """Whether the hub asks the worker ``row`` (``queue_fit``'s, with ``columns``) to install Chromium for a run of
    ``owner_id``: online and its owner's, its daemon says browser_install and not browser, with no request open or
    held back."""
    features = row.features or ()
    return (
        bool(row.online)
        and row.owner_id == owner_id
        and runs.BROWSER not in features
        and runs.BROWSER_INSTALL in features
        and not is_open(row)
        and not held_back(row, now)
    )


async def _open(conn: AsyncConnection, worker_id: int) -> None:
    w = tables.workers
    values = {
        "browser_install_requested_at": func.now(),
        "browser_install_ended_at": None,
        "browser_install_outcome": None,
        "browser_install_reason": None,
    }
    await conn.execute(update(w).values(**values).where(w.c.id == worker_id))


async def set_request(conn: AsyncConnection, user: Principal, worker_id: int, target: str, *, open_it: bool) -> None:
    """Open the worker's request (``open_it``) or cancel it, audited when it changes; the caller checked the worker
    is one ``user`` may drain and not revoked, and holds its row locked."""
    w = tables.workers
    found = (w.c.features, w.c.browser, *columns())
    row = (await conn.execute(select(*found).where(w.c.id == worker_id))).one()
    if open_it:
        refusal = _refusal(worker_id, row)
        if refusal is not None:
            raise HTTPException(409, refusal)
        if is_open(row):
            return
        await _open(conn, worker_id)
        action = audit.WORKER_BROWSER_INSTALL
    else:
        if not is_open(row):
            return
        await _end(conn, worker_id, "cancelled", None)
        action = audit.WORKER_BROWSER_INSTALL_CANCEL
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
    log.info("browser install asked" if open_it else "browser install cancelled", extra={"worker_id": worker_id})


async def _ask(conn: AsyncConnection, worker_id: int, run) -> bool:
    """Open a request on the worker ``worker_id`` for the queued ``run``, audited with no actor; False when another
    pass holds its row, or it was revoked, has Chromium, says no browser_install or has a request open since."""
    w, u = tables.workers, tables.users
    owner = select(u.c.login).where(u.c.id == w.c.owner_id).scalar_subquery()
    query = (
        select(w.c.name, owner.label("owner"), w.c.revoked_at, w.c.features, w.c.browser, *columns())
        .where(w.c.id == worker_id)
        .with_for_update(of=w, skip_locked=True)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None or row.revoked_at is not None or _refusal(worker_id, row) is not None or is_open(row):
        return False
    if held_back(row):
        return False
    await _open(conn, worker_id)
    target = f"worker:{worker_id} name={row.name} owner={row.owner} run={run.id}"
    await audit.record(
        conn,
        actor_id=None,
        token_id=None,
        action=audit.WORKER_BROWSER_INSTALL,
        target=target,
        project_id=run.project_id,
    )
    log.info("browser install asked by the hub", extra={"worker_id": worker_id, "run_id": run.id})
    return True


async def ask_for_queued(engine: AsyncEngine, batch: int = ASK_BATCH) -> int:
    """For each queued run dispatched with browser (the oldest ``batch``) that no worker can take, open a request on
    the nearest worker of the run's owner that lacks nothing but Chromium and can install it from the hub
    (``askable``), each in a transaction of its own. Returns how many requests it opened."""
    from evo_agents.hub.server.runs.service import queue_fit  # it reads this module's columns

    r = tables.runs
    async with engine.begin() as conn:
        query = select(r.c.id).where(r.c.state == "queued", r.c.browser).order_by(r.c.id).limit(batch)
        queued = (await conn.execute(query)).scalars().all()
    asked: set[int] = set()
    for run_id in queued:
        async with engine.begin() as conn:
            found = await queue_fit.queue_fit(conn, run_id)
            if found.diagnosis is None:  # a worker takes it, or waits for nothing anyone does
                continue
            run, now = found.run, datetime.now(UTC)
            lacking = [
                v for v in found.verdicts if v.blocked_by == ["browser"] and askable(v.row, run.dispatched_by, now)
            ]
            if not lacking:
                continue
            worker_id = queue_fit.nearest(lacking).row.id
            if worker_id not in asked and await _ask(conn, worker_id, run):
                asked.add(worker_id)
    return len(asked)

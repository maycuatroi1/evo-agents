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
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.runs.models import BrowserInstallRequest, HeartbeatRequest
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

NOT_SAID = f"its daemon does not say {runs.BROWSER_INSTALL} in its features"


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


async def set_request(conn: AsyncConnection, user: Principal, worker_id: int, target: str, *, open_it: bool) -> None:
    """Open the worker's request (``open_it``) or cancel it, audited when it changes; the caller checked the worker
    is one ``user`` may drain and not revoked, and holds its row locked."""
    w = tables.workers
    found = (w.c.features, w.c.browser, w.c.browser_install_requested_at, w.c.browser_install_ended_at)
    row = (await conn.execute(select(*found).where(w.c.id == worker_id))).one()
    is_open = row.browser_install_requested_at is not None and row.browser_install_ended_at is None
    if open_it:
        refusal = _refusal(worker_id, row)
        if refusal is not None:
            raise HTTPException(409, refusal)
        if is_open:
            return
        values = {
            "browser_install_requested_at": func.now(),
            "browser_install_ended_at": None,
            "browser_install_outcome": None,
            "browser_install_reason": None,
        }
        await conn.execute(update(w).values(**values).where(w.c.id == worker_id))
        action = audit.WORKER_BROWSER_INSTALL
    else:
        if not is_open:
            return
        await _end(conn, worker_id, "cancelled", None)
        action = audit.WORKER_BROWSER_INSTALL_CANCEL
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
    log.info("browser install asked" if open_it else "browser install cancelled", extra={"worker_id": worker_id})

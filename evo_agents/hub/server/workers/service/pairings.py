"""Pairings: a code a machine joins with as a worker of the member who made it (``create``), at most
MAX_LIVE_PAIRINGS of them neither used nor expired per member, and what the web shows of one (``state``)."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.workers.models.codes import (
    DRAWS,
    MAX_LIVE_PAIRINGS,
    MAX_WRONG_TRIES,
    PAIRING_TTL,
    new_code,
    pairing_status,
    shown_code,
)
from evo_agents.hub.server.workers.models.workers import Pairing, PairingRequest, PairingState, name_taken
from evo_agents.hub.server.workers.repository import pairings as store
from evo_agents.hub.server.workers.repository import workers as worker_store
from evo_agents.hub.server.workers.service.codes import code_hash
from evo_agents.hub.server.workers.service.projects import writable
from evo_agents.hub.server.workers.service.views import unique

log = logging.getLogger(__name__)


async def create(conn: AsyncConnection, user: Principal, body: PairingRequest, secret: str) -> Pairing:
    """A pairing of ``user`` for ``body``, its code kept as its HMAC under ``secret``; the code shown once. 409 when
    ``user`` holds MAX_LIVE_PAIRINGS live ones already or a worker of theirs has the name, 503 when no free selector
    was drawn."""
    projects, labels = unique(body.projects), unique(body.labels)
    project_ids = await writable(conn, user, projects)
    await store.lock_member(conn, user.user_id)
    await store.prune(conn)
    if await store.live_count(conn, user.user_id) >= MAX_LIVE_PAIRINGS:
        raise Conflict(
            f"you have {MAX_LIVE_PAIRINGS} pairing codes waiting or locked already: join with one of them, or "
            f"wait until one expires ({int(PAIRING_TTL.total_seconds() // 60)} minutes after it was made)"
        )
    if await worker_store.name_taken(conn, user.user_id, body.name):
        raise Conflict(name_taken(body.name))
    for _ in range(DRAWS):
        code = new_code()
        row = await store.insert(conn, code, code_hash(secret, code), user.user_id, body, project_ids, labels)
        if row is not None:
            break
    else:
        raise HTTPException(503, "no free pairing code was found; try again shortly")
    pairing_id, expires_at = row
    await Auditor(conn, None, user).record(audit.WORKER_PAIR, f"pairing:{pairing_id} name={body.name}")
    log.info("worker pairing created", extra={"pairing_id": pairing_id, "login": user.login, "worker_name": body.name})
    return Pairing(
        id=pairing_id,
        code=shown_code(code),
        expires_at=expires_at,
        name=body.name,
        projects=projects,
        slots=body.slots,
        labels=labels,
        allow_web_terminal=body.allow_web_terminal,
    )


async def state(conn: AsyncConnection, user: Principal, pairing_id: int) -> PairingState:
    """Pairing ``pairing_id`` of ``user``: whether a machine has joined with it yet; 404 for another member's."""
    row = await store.state(conn, pairing_id)
    if row is None or row.owner_id != user.user_id:
        raise NotFound(f"you have no pairing {pairing_id} on this hub")
    projects = await store.project_names(conn, row.projects)
    return PairingState(
        id=pairing_id,
        status=pairing_status(row.used_at is not None, row.attempts, row.expired),
        name=row.name,
        projects=projects,
        slots=row.slots,
        labels=row.labels,
        allow_web_terminal=row.allow_web_terminal,
        tries_left=max(MAX_WRONG_TRIES - row.attempts, 0),
        created_at=row.created_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
        worker_id=row.worker_id,
    )

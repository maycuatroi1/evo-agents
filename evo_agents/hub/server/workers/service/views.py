"""What a Worker shows of a row of ``repository.workers.listed``: its placement, its status, what its daemon said of
its install, and, for a worker another member shares, only the projects the caller holds a grant on."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.server import update_now
from evo_agents.hub.server.browser.service import install as browser_install
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.workers.models.workers import Worker, WorkerCredential
from evo_agents.hub.server.workers.repository import workers as store
from evo_agents.hub.server.workers.repository.placement import Placement


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def worker(row) -> Worker:
    placed = Placement.of(row)
    fields = {key: value for key, value in row._mapping.items() if not key.startswith("place_")}
    fields |= {"taints": list(placed.taints), "auto_labels": list(placed.auto)}
    fields |= {"ready": placed.ready, "ready_reason": placed.unready}
    now = fields.pop("now")
    fields["features"] = list(fields["features"] or ())  # NULL: its daemon said none
    fields["labels"] = list(dict.fromkeys(fields["labels"]))  # its own, then its pool's
    report, since = fields.pop("self_update"), fields.pop("self_update_since")
    fields["self_update"] = None if report is None else {**report, "since": since}
    update_now.shown(fields)
    browser_install.shown(fields)
    status = runs.worker_status(
        last_heartbeat_at=fields["last_heartbeat_at"],
        drained_at=fields["drained_at"],
        revoked_at=fields["revoked_at"],
        now=now,
    )
    return Worker(**fields, status=status)


async def workers(conn: AsyncConnection, *where) -> list[Worker]:
    return [worker(row) for row in await store.rows(conn, *where)]


async def one_worker(conn: AsyncConnection, worker_id: int) -> Worker:
    (found,) = await workers(conn, store.by_id(worker_id))
    return found


async def seen_by(conn: AsyncConnection, user: Principal, found: list[Worker]) -> list[Worker]:
    """``found`` as ``user`` sees them: a worker another member shares shows only the projects of it on which ``user``
    holds a grant, and none of what its daemon did for the night shifts of projects (``kg_sync``); a hub admin and the
    owner see a worker whole."""
    if user.admin or all(worker.owner == user.login for worker in found):
        return found
    mine = await store.granted_names(conn, user.user_id)
    return [
        worker
        if worker.owner == user.login
        else worker.model_copy(update={"projects": [name for name in worker.projects if name in mine], "kg_sync": None})
        for worker in found
    ]


async def credential_of(conn: AsyncConnection, issued, worker_id: int) -> WorkerCredential:
    found = await one_worker(conn, worker_id)
    return WorkerCredential(token=issued.token, token_id=issued.token_id, expires_at=issued.expires_at, worker=found)

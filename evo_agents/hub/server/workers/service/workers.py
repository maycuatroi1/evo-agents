"""What the routes of ``/v1/workers`` check and decide: the worker a caller owns (404 for any other, as for an id the
hub never gave out), making a worker and its token, listing and showing workers, drain, Update now, Install
browser, who may dispatch to a worker, its settings, and its revocation (``end_worker``), which a token's revocation
does too (``evo_agents.hub.server.tokens``)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import audit, browser_install, credentials, update_now
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound
from evo_agents.hub.server.paging import Paging
from evo_agents.hub.server.run_reaper import release_runs
from evo_agents.hub.server.security import WORKER, Principal, issue_token
from evo_agents.hub.server.workers.models.workers import (
    NO_WORKER,
    HostFacts,
    Worker,
    WorkerCredential,
    WorkerRegistration,
    WorkerUpdate,
    name_taken,
    revoked,
)
from evo_agents.hub.server.workers.repository import projects as project_store
from evo_agents.hub.server.workers.repository import workers as store
from evo_agents.hub.server.workers.service.projects import serve, writable
from evo_agents.hub.server.workers.service.views import credential_of, one_worker, seen_by, unique, worker

log = logging.getLogger(__name__)


async def owned(conn: AsyncConnection, user: Principal, worker_id: int, *, lock: bool = False):
    """(owner_id, owner login, name, token_id, drained_at, revoked_at) of a worker ``user`` owns, or any worker for a
    hub admin; 404 otherwise, the same as for an id the hub never gave out."""
    row = await store.owner_row(conn, worker_id, lock=lock)
    if row is None or (row.owner_id != user.user_id and not user.admin):
        raise NotFound(NO_WORKER.format(id=worker_id))
    return row


def target(worker_id: int, name: str, owner: str) -> str:
    return f"worker:{worker_id} name={name} owner={owner}"


def shown(value) -> str:
    """A setting as an audit target shows it: labels in brackets, comma separated, and true or false as JSON does."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"[{','.join(value)}]" if isinstance(value, list) else str(value)


# Creating workers


async def create_worker(
    conn: AsyncConnection,
    owner_id: int,
    *,
    name: str,
    slots: int,
    labels: list[str],
    allow_web_terminal: bool,
    project_ids,
    host: HostFacts,
    pool_id: int | None = None,
):
    """A new worker of ``owner_id`` and its token, in the caller's transaction, a node of pool ``pool_id`` if one is
    given; (Issued, worker id)."""
    if await store.name_taken(conn, owner_id, name):
        raise Conflict(name_taken(name))
    issued = await issue_token(conn, owner_id, WORKER, host.hostname)
    values = {
        "owner_id": owner_id,
        "token_id": issued.token_id,
        "name": name,
        "slots": slots,
        "labels": labels,
        "allow_web_terminal": allow_web_terminal,
        "pool_id": pool_id,
        **host.model_dump(include={"hostname", "os", "arch", "agent_version"}),
    }
    worker_id = await store.insert_worker(conn, values, project_ids)
    if worker_id is None:  # another registration took the name meanwhile
        raise Conflict(name_taken(name))
    return issued, worker_id


async def register(conn: AsyncConnection, user: Principal, body: WorkerRegistration) -> WorkerCredential:
    """The calling machine as a worker of ``user``, with its machine token; its credential."""
    project_ids = await writable(conn, user, unique(body.projects))
    issued, worker_id = await create_worker(
        conn,
        user.user_id,
        name=body.name,
        slots=body.slots,
        labels=unique(body.labels),
        allow_web_terminal=body.allow_web_terminal,
        project_ids=project_ids,
        host=body,
    )
    await Auditor(conn, None, user).record(audit.WORKER_REGISTER, f"worker:{worker_id} name={body.name}")
    log.info("worker registered", extra={"worker_id": worker_id, "login": user.login, "token_id": issued.token_id})
    return await credential_of(conn, issued, worker_id)


# Reading workers


async def listed(conn: AsyncConnection, user: Principal, revoked: bool, page: Paging) -> list[Worker]:
    """The workers ``user`` lists, as they see them: their own and the live ones shared with them; every one for a hub
    admin."""
    where = store.live(revoked) if user.admin else store.visible_to(user.user_id, revoked=revoked)
    return await seen_by(conn, user, [worker(row) for row in await store.paged(conn, page, *where)])


async def shown_worker(conn: AsyncConnection, user: Principal, worker_id: int) -> Worker:
    """Worker ``worker_id`` as ``user`` sees it: theirs, one shared with them, or any for a hub admin; 404 otherwise."""
    if not await store.is_shared_with(conn, worker_id, user.user_id):
        await owned(conn, user, worker_id)  # 404 for a worker the caller neither owns nor is shared, as for none
    return (await seen_by(conn, user, [await one_worker(conn, worker_id)]))[0]


# Changing workers


async def set_drain(conn: AsyncConnection, user: Principal, worker_id: int, drain: bool) -> Worker:
    owner_id, owner, name, _, drained_at, revoked_at = await owned(conn, user, worker_id, lock=True)
    if not drain and owner_id != user.user_id:  # a hub admin may stop a worker, never set one going again
        raise Forbidden(
            f"only {owner}, who owns worker {worker_id}, may undrain it; a hub admin may drain or revoke it"
        )
    if revoked_at is not None:
        raise Conflict(revoked(worker_id, revoked_at))
    if (drained_at is not None) != drain:  # drained already, or not drained: nothing changes, nothing is audited
        await store.set_drained(conn, worker_id, drain)
        action = audit.WORKER_DRAIN if drain else audit.WORKER_UNDRAIN
        await Auditor(conn, None, user).record(action, target(worker_id, name, owner))
        log.info("worker drained" if drain else "worker undrained", extra={"worker_id": worker_id})
    return await one_worker(conn, worker_id)


async def set_update_now(conn: AsyncConnection, user: Principal, worker_id: int, open_it: bool) -> Worker:
    _, owner, name, _, _, revoked_at = await owned(conn, user, worker_id, lock=True)
    if revoked_at is not None:
        raise Conflict(revoked(worker_id, revoked_at))
    await update_now.set_request(conn, user, worker_id, target(worker_id, name, owner), open_it=open_it)
    return await one_worker(conn, worker_id)


async def set_browser_install(conn: AsyncConnection, user: Principal, worker_id: int, open_it: bool) -> Worker:
    _, owner, name, _, _, revoked_at = await owned(conn, user, worker_id, lock=True)
    if revoked_at is not None:
        raise Conflict(revoked(worker_id, revoked_at))
    await browser_install.set_request(conn, user, worker_id, target(worker_id, name, owner), open_it=open_it)
    return await one_worker(conn, worker_id)


async def set_dispatch_from(conn: AsyncConnection, user: Principal, worker_id: int, value: str) -> Worker:
    owner_id, owner, name, _, _, revoked_at = await owned(conn, user, worker_id, lock=True)
    if owner_id != user.user_id:
        raise Forbidden(f"only {owner}, who owns worker {worker_id}, may set who dispatches to it")
    if revoked_at is not None:
        raise Conflict(revoked(worker_id, revoked_at))
    if await store.dispatch_from(conn, worker_id) != value:  # the value it has already: nothing changes or is audited
        await store.set_dispatch_from(conn, worker_id, value)
        action = audit.WORKER_DISPATCH_FROM
        await Auditor(conn, None, user).record(action, f"{target(worker_id, name, owner)} dispatch_from={value}")
        log.info("worker dispatch_from set", extra={"worker_id": worker_id, "dispatch_from": value})
    return await one_worker(conn, worker_id)


def wanted_settings(body: WorkerUpdate) -> dict:
    """The settings ``body`` gives, lists without repeats and projects as a sorted set."""
    wanted = body.model_dump(exclude_none=True)
    for field in ("labels", "taints", "plugins"):
        if field in wanted:
            wanted[field] = unique(wanted[field])
    if "projects" in wanted:  # the projects a worker serves are a set: their order says nothing
        wanted["projects"] = sorted(set(wanted["projects"]))
    return wanted


async def update(conn: AsyncConnection, user: Principal, worker_id: int, body: WorkerUpdate) -> Worker:
    """Change the settings ``body`` gives of a worker ``user`` owns; giving the values it has changes nothing and is
    not audited."""
    wanted = wanted_settings(body)
    owner_id, owner, name, _, _, revoked_at = await owned(conn, user, worker_id, lock=True)
    if owner_id != user.user_id:
        raise Forbidden(f"only {owner}, who owns worker {worker_id}, may change its settings")
    if revoked_at is not None:
        raise Conflict(revoked(worker_id, revoked_at))
    projects = await project_store.served(conn, worker_id)
    now = {**await store.settings(conn, worker_id), "projects": list(projects)}
    changed = {field: value for field, value in wanted.items() if value != now[field]}
    if changed:  # the values it has already: nothing changes, nothing is audited
        if "name" in changed and await store.name_taken(conn, owner_id, changed["name"], besides=worker_id):
            raise Conflict(name_taken(changed["name"]))
        if "projects" in changed:
            await serve(conn, user, worker_id, name, projects, changed["projects"])
        columns = {field: value for field, value in changed.items() if field != "projects"}
        if columns and not await store.set_settings(conn, worker_id, columns):  # another worker took the name
            raise Conflict(name_taken(changed["name"]))
        changes = " ".join(f"{field}={shown(now[field])}->{shown(value)}" for field, value in changed.items())
        await Auditor(conn, None, user).record(audit.WORKER_UPDATE, f"{target(worker_id, name, owner)} {changes}")
        log.info("worker settings changed", extra={"worker_id": worker_id, "fields": sorted(changed)})
    return await one_worker(conn, worker_id)


async def revoke(conn: AsyncConnection, user: Principal, worker_id: int) -> Worker:
    """End a worker ``user`` owns (any for a hub admin) and its token, releasing its runs; 409 when it is revoked
    already."""
    _, owner, name, token_id, _, revoked_at = await owned(conn, user, worker_id, lock=True)
    if revoked_at is not None:
        raise Conflict(f"worker {worker_id} was revoked already, at {revoked_at.isoformat()}")
    released = await end_worker(conn, user, worker_id, name, owner, token_id)
    found = await one_worker(conn, worker_id)
    log.info("worker revoked", extra={"worker_id": worker_id, "by": user.login, "runs_released": released})
    return found


async def end_worker(
    conn: AsyncConnection,
    actor: Principal | None,
    worker_id: int,
    name: str,
    owner: str,
    token_id: int,
    *,
    why: str = "was revoked",
    by: str | None = None,
) -> int:
    """Revoke a live worker whose row the caller holds locked, and its token, release the runs it holds, give back
    every lease it still has and add the worker.revoke audit row, all in the caller's transaction; the caller revokes
    their GitHub tokens once it commits (``revoke_leased_tokens``). Returns how many runs were released. ``actor``
    None is the hub itself (the pool node reaper): the audit row has no actor and its target ends ``by=<by>``, and
    ``why`` is what the released runs' events say happened to the worker."""
    actor_id, actor_token = (None, None) if actor is None else (actor.user_id, actor.token_id)
    await store.revoke(conn, worker_id, token_id)
    released = await release_runs(conn, worker_id, f"its worker {name} {why}")
    # The runs it released gave theirs back as they moved; this takes the rest, of runs that left it otherwise.
    await credentials.end_leases(
        conn, worker_id=worker_id, actor_id=actor_id, token_id=actor_token, by="worker-revoked"
    )
    shown_target = target(worker_id, name, owner)
    if by is not None:
        shown_target = f"{shown_target} by={by}"
    await Auditor(conn, None, actor).record(audit.WORKER_REVOKE, shown_target)
    return released


async def revoke_leased_tokens(app_state, worker_id: int) -> None:
    """Once ``end_worker`` committed: revoke at GitHub the tokens the worker's leases held. GitHub failing leaves them
    to the reaper's next pass."""
    await credentials.revoke_tokens(
        app_state.engine, app_state.sealer, credentials.revoker(app_state), worker_id=worker_id
    )

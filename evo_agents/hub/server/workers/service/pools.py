"""Worker pools: machines a member adds with one join token instead of a pairing code each.

A member creates a pool (POST /v1/worker-pools, with a web session and its CSRF header or with their machine token)
naming it, unique among their pools in any case, with the projects its nodes serve (the writer role on each, as a
pairing takes them), the slots each node holds, labels and taints, the codes of doctor findings the pool accepts, and
the days a node stays offline before the hub revokes it (1 to 90, 7 by default). GET /v1/worker-pools lists the
caller's pools, GET /v1/worker-pools/{name} shows one with its nodes, and PATCH /v1/worker-pools/{name} changes its
settings: the nodes take the new slots and projects in their rows (``projects.serve`` for each, with its
refusals) and the new labels through ``node_labels``, so each daemon learns them from its next heartbeat answer as it
learns a worker's settings. DELETE /v1/worker-pools/{name} deletes a pool with no live node, and its join tokens; its
revoked nodes stay revoked, in no pool. A pool belongs to the member who made it, as a worker does; only they see or change it, and
another member's pool is 404, as a name the hub never had. Each of its nodes is a worker of that member, shared or not
by the rules of shared workers.

A join token (POST /v1/worker-pools/{name}/tokens) is ``evj_`` and 32 random bytes in base64url, shown once: the hub
keeps its hex SHA-256 alone. It lasts 1 hour to 30 days, takes 1 to 100 machines, and its owner revokes it with POST
.../tokens/{id}/revoke; the nodes that joined with it stay. A machine sends it to POST /v1/worker/join instead of a code
(``join``): ``pool_tokens.take_token`` finds it, locks it and its pool, and counts the join; a token that is unknown,
expired, revoked or used up gets the same 403 as a wrong code, counted against the address as wrong codes are. The node
is a new worker of the pool's owner (of the token's creator, an admin of the agent's project, for the pool of an
agent) with an ``evw_`` token of its own, named ``pool_tokens.node_name`` (the pool's name and the machine's hostname, with a
suffix when a live worker of the owner has that name already), with the pool's slots and projects; its labels are its
own (none at first), the pool's and ``pool=<name>``.

Every pool created or changed and every join token created or revoked adds an audit row naming the pool and the
token's id, never the token, which never reaches a log line either.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.profile_store import POOL_PREFIX
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.workers.models.pools import SETTINGS, Pool, PoolNode, PoolRequest, PoolUpdate, no_pool
from evo_agents.hub.server.workers.repository import pools as store
from evo_agents.hub.server.workers.repository import projects as project_store
from evo_agents.hub.server.workers.repository.placement import Placement
from evo_agents.hub.server.workers.service.projects import serve, writable

log = logging.getLogger(__name__)


# Reading pools


async def agent_pool_row(conn: AsyncConnection, user: Principal, name: str, *, lock: bool = False):
    """The pool of the agent ``name`` names (``agent-<handle>``) when the caller administers the agent's project or
    the hub; 403 for a member of the project who does not, 404 for anyone else, as for a name the hub never had."""
    row = await store.agent_pool_row(conn, name, lock=lock) if name.lower().startswith(POOL_PREFIX) else None
    if row is None:
        raise NotFound(no_pool(name))
    access = await project_access(conn, user, row.project)
    if not (user.admin or has_role(access.role, "admin")):
        held = access.role or "no grant"
        raise Forbidden(
            f"pool {row.name} is the pool of an agent of project {row.project}: managing it needs the admin role on "
            f"the project or on the hub; you hold {held}"
        )
    return row


async def pool_row(conn: AsyncConnection, user: Principal, name: str, *, lock: bool = False):
    """The row of the caller's pool ``name``, in any case, or of the agent's pool it names when the caller
    administers that agent; 404 for none."""
    row = await store.own_pool_row(conn, user.user_id, name, lock=lock)
    if row is None:
        return await agent_pool_row(conn, user, name, lock=lock)
    return row


async def nodes(conn: AsyncConnection, pool_id: int) -> list[PoolNode]:
    found = []
    for row in await store.node_rows(conn, pool_id):
        placed = Placement.of(row)
        fields = {key: value for key, value in row._mapping.items() if not key.startswith("place_")}
        status = runs.worker_status(
            last_heartbeat_at=fields["last_heartbeat_at"],
            drained_at=fields.pop("drained_at"),
            revoked_at=fields.pop("revoked_at"),
            now=fields.pop("now"),
        )
        fields["labels"] = list(dict.fromkeys(fields["labels"]))
        found.append(PoolNode(**fields, status=status, ready=placed.ready, ready_reason=placed.unready))
    return found


async def shown(conn: AsyncConnection, row) -> Pool:
    fields = {name: row._mapping[name] for name in ("id", "name", "slots", "reap_after_days", "created_at")}
    owner = await store.login_of(conn, row.owner_id)
    handle = None if row.agent_id is None else await store.handle_of(conn, row.agent_id)
    return Pool(
        **fields,
        updated_at=row.updated_at,
        owner=owner,
        agent=handle,
        projects=list(await store.pool_projects(conn, row.id)),
        labels=list(row.labels),
        taints=list(row.taints),
        accepted_findings=list(row.accepted_findings),
        nodes=await nodes(conn, row.id),
    )


async def listed(conn: AsyncConnection, user: Principal) -> list[Pool]:
    return [await shown(conn, row) for row in await store.listed(conn, user.user_id, admin=user.admin)]


async def one_pool(conn: AsyncConnection, user: Principal, name: str) -> Pool:
    return await shown(conn, await pool_row(conn, user, name))


# Creating and changing pools


def name_conflict(name: str) -> Conflict:
    return Conflict(f"you have a pool named {name} already: choose another name")


def target(pool_id: int, name: str) -> str:
    return f"pool:{pool_id} name={name}"


async def create(conn: AsyncConnection, user: Principal, body: PoolRequest) -> Pool:
    """A new pool of ``user``, serving ``body.projects`` (the writer role on each); 422 for a name of an agent's pool,
    409 for a name they have."""
    if body.name.lower().startswith(POOL_PREFIX):
        raise Unprocessable(
            f"{POOL_PREFIX} starts the names of the pools of agents, which the hub makes with the agent: "
            "choose another name"
        )
    values = body.model_dump(include={"name", "slots", "labels", "taints", "accepted_findings", "reap_after_days"})
    project_ids = await writable(conn, user, body.projects)
    if await store.name_taken(conn, user.user_id, body.name):
        raise name_conflict(body.name)
    pool_id = await store.insert_pool(conn, user.user_id, values)
    if pool_id is None:  # another request took the name meanwhile
        raise name_conflict(body.name)
    await store.add_projects(conn, pool_id, project_ids)
    await Auditor(conn, None, user).record(audit.WORKER_POOL_CREATE, target(pool_id, body.name))
    log.info("worker pool created", extra={"pool_id": pool_id, "login": user.login})
    return await one_pool(conn, user, body.name)


def shown_value(value) -> str:
    return f"[{','.join(value)}]" if isinstance(value, list) else str(value)


async def serve_projects(conn: AsyncConnection, user: Principal, pool_id: int, was: dict, projects: list[str]) -> None:
    """The pool and each of its live nodes serve exactly ``projects``: the writer role on each one added, and none
    dropped that a node still needs (``projects.serve``, whose 409 changes nothing)."""
    added_ids = await writable(conn, user, [name for name in projects if name not in was])
    for node_id, node in await store.live_nodes(conn, pool_id):
        await serve(conn, user, node_id, node, await project_store.served(conn, node_id), projects)
    dropped = [project_id for name, project_id in was.items() if name not in projects]
    if dropped:
        await store.drop_projects(conn, pool_id, dropped)
    if added_ids:
        await store.add_projects(conn, pool_id, added_ids)


async def update(conn: AsyncConnection, user: Principal, name: str, body: PoolUpdate) -> Pool:
    """Change the settings ``body`` gives of the pool ``name``; its live nodes take them. Giving the values it has
    changes nothing and is not audited."""
    wanted = body.model_dump(exclude_none=True)
    if "projects" in wanted:  # a set: the order says nothing
        wanted["projects"] = sorted(set(wanted["projects"]))
    row = await pool_row(conn, user, name, lock=True)
    if row.agent_id is not None and "projects" in wanted:
        raise Unprocessable(f"pool {row.name} serves the project of its agent alone: leave projects out")
    was_projects = await store.pool_projects(conn, row.id)
    now = {field: row._mapping[field] for field in SETTINGS if field != "projects"}
    now = {field: list(value) if isinstance(value, list) else value for field, value in now.items()}
    now["projects"] = sorted(was_projects)
    changed = {field: value for field, value in wanted.items() if value != now[field]}
    if changed:  # the values it has already: nothing changes, nothing is audited
        if "projects" in changed:
            await serve_projects(conn, user, row.id, was_projects, changed["projects"])
        await store.set_pool(conn, row.id, {field: value for field, value in changed.items() if field != "projects"})
        if "slots" in changed:
            await store.set_node_slots(conn, row.id, changed["slots"])
        changes = " ".join(
            f"{field}={shown_value(now[field])}->{shown_value(value)}" for field, value in changed.items()
        )
        await Auditor(conn, None, user).record(audit.WORKER_POOL_UPDATE, f"{target(row.id, row.name)} {changes}")
        log.info("worker pool changed", extra={"pool_id": row.id, "fields": sorted(changed)})
    return await one_pool(conn, user, name)


async def delete(conn: AsyncConnection, user: Principal, name: str) -> int:
    """Delete the pool ``name`` of ``user`` once it has no live node, with its join tokens; its id. 409 for an agent's
    pool or one with live nodes."""
    row = await pool_row(conn, user, name, lock=True)
    if row.agent_id is not None:
        raise Conflict(f"pool {row.name} is the pool of an agent: it goes when its agent is deleted")
    live = await store.live_nodes(conn, row.id)
    if live:
        nodes = ", ".join(f"{node} (#{worker_id})" for worker_id, node in live)
        raise Conflict(f"pool {row.name} still has live nodes: {nodes}; revoke them first (`evo-agents worker revoke`)")
    await store.delete_pool(conn, row.id)
    await Auditor(conn, None, user).record(audit.WORKER_POOL_DELETE, target(row.id, row.name))
    return row.id

"""Where a run of an agent goes (plan agent-dedicated-pools, criterion a2): a node of the agent's own pool, whoever
dispatched it and whatever its kind.

Every agent has the pool ``agent-<handle>`` (``profile_store.create_pool``). The hub, not the person who dispatches,
places each run of an agent:

- ``stamp`` gives the run the selector ``pool=agent-<handle>`` and the pool's taints as tolerations, in
  ``runs.service.dispatch.insert_run`` where every kind of run is made (step, plan, author, rerun, chat, task,
  question, triage, judge, the Curator's, loop, browser login, release).
- ``claim_gate`` and ``home_run`` (``agents.repository.placement``) are the claim's side (``runs.service.claims``): a run of an agent is taken by a
  node of its pool alone, which takes it whoever dispatched it; and a node of an agent's pool takes no run of another
  agent. A node of the pool must be ready (``worker_placement``) as for any pool.
- ``watch`` counts the nodes of the pool that are ready and online when a run queues; with none, the run waits and
  the admins of the agent's project get one notice ``agent_pool_empty`` for each time the pool runs out of nodes
  (``worker_pools.empty_notified_at``, cleared by ``recovered`` when a node is ready again).
- ``ready_nodes`` lists them, for the machine of a chat run.

A run with no agent, a ``local`` one, is no run of a pool. The one exception to both rules is the shared KG pool
(``kg_shared_pool``): its ready nodes take the KG runs of a project with no repo whose default agent's pool has no ready
node, when the project's creator owns that pool; such a run is stamped on it and tells no admin the agent's pool is
empty.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import kg_shared_pool, worker_placement
from evo_agents.hub.server.agents.models.placement import EMPTY_NOTICE, Home
from evo_agents.hub.server.agents.repository import placement as repository
from evo_agents.hub.server.profile_store import POOL_PREFIX
from evo_agents.hub.server.runs.models.placement import POOL_KEY, normalized


async def home_of(conn: AsyncConnection, profile_id: int | None) -> Home | None:
    """The pool of agent ``profile_id``; None for a profile with no pool, or no profile."""
    if profile_id is None:
        return None
    return await repository.home_of(conn, profile_id)


# The run's columns


def stamped(home: Home, selector=(), tolerations=()) -> dict:
    """The ``selector`` and ``tolerations`` columns of a run of the agent of ``home``: the labels it had with
    pool=<name> first, and the taints it tolerated with the pool's."""
    asked = [label for label in normalized(selector) if not label.startswith(f"{POOL_KEY}=")]
    return {
        "selector": normalized([home.selector, *asked]),
        "tolerations": normalized([*(tolerations or ()), *home.taints]),
    }


async def default_home(conn: AsyncConnection, project_id: int) -> Home | None:
    """The pool of the project's default agent: the agent the runs that name none go to."""
    from evo_agents.hub.server.profile_store import ensure_default

    return await home_of(conn, (await ensure_default(conn, project_id)).id)


async def stamp(conn: AsyncConnection, values: dict) -> dict:
    """``values`` of a run to insert, placed on the pool of its agent; the run queues
    with nothing to claim it, and the admins hear of it, when no node of the pool is ready (``watch``). A pin to a
    worker outside the pool is dropped: no such worker takes the run."""
    home = await home_of(conn, values.get("profile_id"))
    if home is None:
        return values
    shared = await kg_shared_pool.placed(conn, values)
    if shared is not None:
        return shared
    await watch(conn, home)
    placed = {**values, **stamped(home, values.get("selector"), values.get("tolerations"))}
    pin = values.get("pinned_worker_id")
    if pin is not None and not await repository.pinned_inside(conn, home, pin):
        placed["pinned_worker_id"] = None
    return placed


# The nodes


async def ready_nodes(conn: AsyncConnection, home: Home) -> list:
    """The rows (id, name, online, drained_at and the placement columns) of the nodes of the pool that are online, not
    draining and ready now, the oldest first."""
    rows = await repository.nodes(conn, home)
    return [row for row in rows if row.online and row.drained_at is None and worker_placement.Placement.of(row).ready]


async def watch(conn: AsyncConnection, home: Home) -> None:
    """Tell the admins of the agent's project, once, that the pool has no node ready; clear that when it has one."""
    ready = bool(await ready_nodes(conn, home))
    if ready:
        await repository.recovered(conn, home.pool_id)
        return
    if not await repository.mark_empty(conn, home.pool_id):
        return  # the admins were told of this outage already
    await _notify_admins(conn, home)


async def recovered_by(conn: AsyncConnection, worker_id: int) -> None:
    """The pool of the ready node ``worker_id``, which has just polled for a run, has a ready node: the next time it
    has none, the admins hear of it again."""
    await repository.recovered_by(conn, worker_id)


async def _notify_admins(conn: AsyncConnection, home: Home) -> None:
    from evo_agents.hub.server.notifications.service.notifier import (
        NOTIFIER,  # notifications import the claim, which imports this
    )

    admins = await repository.admins(conn, home.project_id)
    body = (
        f"A run of @{home.handle} queued and the pool {home.pool} has no node that is online, not draining and ready "
        "(a fresh doctor report with no high finding its pool does not accept). The run waits in the queue. Join a "
        f"node to the pool with a join token (`evo-agents worker pool token create {home.pool}`) or fix the ones it has "
        "(`evo-agents worker doctor` on the machine)."
    )
    for user_id in sorted(set(admins)):
        await NOTIFIER.notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=EMPTY_NOTICE,
            project_id=home.project_id,
            title=f"Pool {home.pool} has no node ready, so runs of @{home.handle} wait",
            body=body,
            details={"pool": home.pool, "pool_id": home.pool_id, "agent": home.handle},
        )


def is_agent_pool(name: str | None) -> bool:
    return bool(name) and name.startswith(POOL_PREFIX)

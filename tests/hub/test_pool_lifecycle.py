"""The life of a worker pool's nodes on the hub, step 4 of the worker-pools plan (criterion a5): the job
hub.reap_pool_nodes revokes each pool node offline longer than its pool's ``reap_after_days`` as POST
/v1/workers/{id}/revoke would (its token stops, its name is free again, the runs it held are released as a revoke
releases them, and the worker.revoke audit row names the reaper, with no actor); a node seen within its pool's days
stays, and a worker outside a pool is never revoked by it, however long it has been away (a9)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from evo_agents.hub import jobs, tables
from evo_agents.hub.server import audit
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_run_tables import add_run
from tests.hub.test_worker_pools import created, pool_joined, token_of
from tests.hub.test_workers import (  # noqa: F401 - the fixtures of the worker tests
    OWNER,
    admin,
    client,
    config,
    hub,
    joined,
    paired,
    seed_plan,
    worker_headers,
)


def reap(db) -> dict:
    """One pass of the job's work on ``db``, as the hub's worker runs it."""
    from evo_agents.hub.server.run_state import reap_pool_nodes

    async def go():
        engine = create_async_engine(db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            return await reap_pool_nodes(engine, None, None)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def away(db, worker_id: int, days: float, *, never_beat: bool = False) -> None:
    """Make worker ``worker_id`` last seen ``days`` ago: its last heartbeat, or its join when it never sent one."""
    w, back = tables.workers, timedelta(days=days)
    values = {"last_heartbeat_at": None, "created_at": datetime.now(UTC) - back} if never_beat else {}
    values = values or {"last_heartbeat_at": datetime.now(UTC) - back}
    sql(db, update(w).values(**values).where(w.c.id == worker_id))


def revoke_rows(db) -> list[tuple]:
    a = tables.audit
    return sql(db, select(a.c.target, a.c.actor_id, a.c.token_id).where(a.c.action == audit.WORKER_REVOKE))


def live_ids(db) -> set[int]:
    w = tables.workers
    return {row[0] for row in sql(db, select(w.c.id).where(w.c.revoked_at.is_(None)))}


def test_the_reaper_revokes_a_pool_node_offline_past_its_pools_days_as_a_revoke_would(client, hub, hub_db):
    created(client, hub["owner"])  # reap_after_days 7, the default
    token = token_of(client, hub["owner"], max_uses=5)["token"]
    node = pool_joined(client, token, hostname="box")
    node_id, node_token = node["worker"]["id"], node["token"]
    assert node["worker"]["name"] == "ci-box"
    fresh = pool_joined(client, token, hostname="fresh")["worker"]["id"]
    ids = seed_plan(hub_db) | {"worker": node_id}
    with live.connect(hub_db) as conn:
        retried = add_run(conn, ids, "running", step="1")
        pinned_queued = add_run(conn, ids, "queued", step="5", pinned_worker_id=node_id)
        elsewhere = add_run(conn, ids | {"worker": fresh}, "running", step="6")
    away(hub_db, node_id, 8)
    away(hub_db, fresh, 6)  # within its pool's 7 days: stays

    assert reap(hub_db) == {"revoked": 1, "released": 2}
    assert live_ids(hub_db) == {fresh}
    refused = client.post("/v1/worker/no-such-route", headers=worker_headers(node_token))
    assert refused.status_code == 401 and "worker token is revoked" in refused.json()["message"]
    tokens = tables.tokens
    assert sql(hub_db, select(tokens.c.revoked_at.is_not(None)).where(tokens.c.id == node["token_id"])) == [(True,)]
    runs = tables.runs
    states = dict(sql(hub_db, select(runs.c.id, runs.c.state)))
    assert (states[retried], states[pinned_queued], states[elsewhere]) == ("lost", "failed", "running")
    (error,) = sql(hub_db, select(runs.c.error).where(runs.c.id == pinned_queued))[0]
    assert error.startswith("its worker ci-box was offline more than 7 days and the reaper revoked it")
    next_attempt = select(runs.c.state, runs.c.worker_id).where(runs.c.parent_run_id == retried)
    assert sql(hub_db, next_attempt) == [("queued", None)]
    # the audit row of a revoke, with the reaper as actor: no member, no token
    assert revoke_rows(hub_db) == [(f"worker:{node_id} name=ci-box owner={OWNER} by=reaper", None, None)]
    listed = client.get(f"/v1/workers/{node_id}", headers=hub["owner"]).json()
    assert (listed["status"], listed["held_runs"]) == ("revoked", 0)

    # its name is free: the machine joins again under it, without a suffix; a second pass finds nothing more
    assert pool_joined(client, token, hostname="box")["worker"]["name"] == "ci-box"
    assert reap(hub_db) == {"revoked": 0, "released": 0}


def test_the_reaper_never_revokes_a_worker_outside_a_pool_and_keeps_each_pools_own_days(client, hub, hub_db):
    lone = joined(client, paired(client, hub["owner"], name="laptop")["code"])["worker"]["id"]
    away(hub_db, lone, 400)
    created(client, hub["owner"], name="slow", reap_after_days=30)
    slow = pool_joined(client, token_of(client, hub["owner"], pool="slow")["token"], hostname="a")["worker"]["id"]
    away(hub_db, slow, 20)
    created(client, hub["owner"], name="quick", reap_after_days=1)
    quick_token = token_of(client, hub["owner"], pool="quick", max_uses=2)["token"]
    quick = pool_joined(client, quick_token, hostname="b")["worker"]["id"]
    silent = pool_joined(client, quick_token, hostname="c")["worker"]["id"]
    away(hub_db, quick, 0.5)
    away(hub_db, silent, 2, never_beat=True)  # joined two days ago and never sent a heartbeat

    assert reap(hub_db) == {"revoked": 1, "released": 0}
    assert live_ids(hub_db) == {lone, slow, quick}
    assert [row[0].split()[0] for row in revoke_rows(hub_db)] == [f"worker:{silent}"]

    # a node revoked by hand meanwhile is not revoked again
    away(hub_db, quick, 3)
    assert client.post(f"/v1/workers/{quick}/revoke", headers=hub["owner"]).status_code == 200
    assert reap(hub_db) == {"revoked": 0, "released": 0}
    assert len(revoke_rows(hub_db)) == 2


def test_the_revoke_job_of_pool_nodes_runs_hourly_and_queues_at_most_one():
    from evo_agents.hub.worker import queue

    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.REAP_POOL_NODES].cron == "47 * * * *"
    assert queue.tasks[jobs.REAP_POOL_NODES].queueing_lock == jobs.REAP_POOL_NODES

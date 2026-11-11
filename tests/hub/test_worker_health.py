"""The health a worker's heartbeat reports (plan hub-steward, criterion a3): the hub keeps it on the worker, GET
/v1/workers shows it, and a change of its state writes one ``worker.health`` event into each project it serves."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select  # noqa: E402

from evo_agents.hub import tables  # noqa: E402
from tests.hub.live import sql  # noqa: E402
from tests.hub.test_plans import registration  # noqa: E402
from tests.hub.test_runs import PROJECT, add_worker, beat, client, hub  # noqa: E402, F401 (fixtures)

SECOND = "evo-agents-second"
HEALTHY = {"disk": {"free_gb": 120.5, "free_pct": 41.0}, "logins": {"claude-code": "ok", "codex": "unauthorized"}}


def events(db) -> list[tuple[int, dict]]:
    e, p = tables.hub_events, tables.projects
    query = (
        select(p.c.name, e.c.payload)
        .join(p, p.c.id == e.c.project_id)
        .where(e.c.kind == "worker.health")
        .order_by(e.c.id)
    )
    return sql(db, query)


def shown(client, headers, worker: dict) -> dict | None:  # noqa: F811
    (row,) = [row for row in client.get("/v1/workers", headers=headers).json() if row["id"] == worker["id"]]
    return row["health"]


def test_a_heartbeats_health_is_kept_shown_and_written_to_each_served_project_only_when_it_changes(
    client,  # noqa: F811
    hub,  # noqa: F811
    hub_db,
):
    assert client.put(f"/v1/projects/{SECOND}", json=registration(), headers=hub["admin"]).status_code == 200
    worker = add_worker(client, hub["owner"], "mac-mini")
    assert shown(client, hub["owner"], worker) is None and events(hub_db) == []  # an older daemon says nothing
    p = tables.projects
    second = sql(hub_db, select(p.c.id).where(p.c.name == SECOND))[0][0]
    sql(hub_db, insert(tables.worker_projects).values(worker_id=worker["id"], project_id=second))

    beat(client, worker, health=HEALTHY)
    assert shown(client, hub["owner"], worker) == HEALTHY
    payload = {"v": 1, "worker_id": worker["id"], "disk": "ok", "disk_free_pct": 41.0, "logins": HEALTHY["logins"]}
    assert sorted(events(hub_db), key=lambda row: row[0]) == sorted([(PROJECT, payload), (SECOND, payload)])

    beat(client, worker, health={**HEALTHY, "disk": {"free_gb": 118.0, "free_pct": 40.2}})  # the same state
    beat(client, worker)  # a heartbeat without health writes none and keeps none
    assert len(events(hub_db)) == 2 and shown(client, hub["owner"], worker) is None

    beat(client, worker, health={"disk": {"free_gb": 4.0, "free_pct": 1.5}, "logins": {"claude-code": "ok"}})
    written = events(hub_db)[2:]
    assert [name for name, _ in sorted(written, key=lambda row: row[0])] == [PROJECT, SECOND]
    assert all(row["disk"] == "low" and row["logins"] == {"claude-code": "ok"} for _, row in written)
    assert all(set(row) == {"v", "worker_id", "disk", "disk_free_pct", "logins"} for _, row in events(hub_db))


def test_a_heartbeat_with_a_login_of_no_runtime_or_a_bad_disk_is_refused(client, hub):  # noqa: F811
    worker = add_worker(client, hub["owner"], "mac-mini")
    for health in (
        {"logins": {"no-such-runtime": "ok"}},
        {"logins": {"claude-code": "signed in as someone\n"}},
        {"disk": {"free_gb": -1, "free_pct": 10}},
        {"disk": {"free_gb": 1, "free_pct": 101}},
    ):
        body = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 1, "health": health}
        refused = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
        assert refused.status_code == 422, health

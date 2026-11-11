"""The queue of a project's operations events (plan hub-steward, criterion a7; ``evo_agents.hub.ops_queue``): the
events of a project whose default system turns ``ops`` on, and that no rule of tier 0 handled, gather by cause key into
batches; a batch closes after ``debounce_minutes`` or at ``max_items``; an item tier 0 handled leaves its batch; a batch
left with no item closes empty, with no run.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import hub_events, ops_queue, tables
from evo_agents.hub.server.ops_queue import ops_pass
from tests.hub.live import sql
from tests.hub.test_runs import PROJECT, client, hub  # noqa: F401 (fixtures)


def project_id(db) -> int:
    p = tables.projects
    return sql(db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


def system(db, ops: dict | None, *, default: bool = True) -> None:
    """Give the project its system ``ops``, with ``ops`` as the key ops of its spec, its default unless ``default`` is
    false."""
    spec = {"schema": "evo-agent-system/1", "name": "Ops", "nodes": [], "edges": []}
    if ops is not None:
        spec["ops"] = ops
    s = tables.agent_systems
    found = sql(db, select(s.c.id).where(s.c.project_id == project_id(db), s.c.slug == "ops"))
    if found:
        sql(db, update(s).values(spec=spec, is_default=default).where(s.c.id == found[0][0]))
    else:
        values = {"project_id": project_id(db), "slug": "ops", "name": "Ops", "spec": spec, "is_default": default}
        sql(db, insert(s).values(**values))


def record(client, db, kind: str, **payload) -> int:
    async def write(engine):
        async with engine.begin() as conn:
            return await hub_events.record(conn, kind, project_id(db), {"v": 1, **payload})

    return client.portal.call(write, client.app.state.engine)


def failed(client, db, run_id: int, cause: str, worker_id: int = 7) -> int:
    payload = {"run_id": run_id, "run_kind": "chat", "state": "failed", "failure_cause": cause, "worker_id": worker_id}
    return record(client, db, hub_events.RUN_ENDED, **payload)


NO_TIER0 = {"redelivered": 0, "orchestrated": 0, "read": 0}  # what tier 0 did (ops_rules), nothing here


def run_pass(client, rules=None) -> dict:
    return client.portal.call(ops_pass, client.app.state.engine, rules)


def batches(db) -> list[tuple]:
    b, i = tables.ops_batches, tables.ops_items
    pending = select(func.count()).where(i.c.batch_id == b.c.id, i.c.state == "pending").scalar_subquery()
    return sql(db, select(b.c.key, b.c.state, pending, b.c.run_id).order_by(b.c.id))


def due(db) -> None:
    b = tables.ops_batches
    sql(db, update(b).values(closes_at=func.now()).where(b.c.state == "open"))


def test_off_by_default_and_turning_on_reads_no_past_event(client, hub, hub_db):
    system(hub_db, None)
    failed(client, hub_db, 101, "checkout")
    assert run_pass(client)["queued"] == 0
    assert not sql(hub_db, select(tables.ops_cursors.c.project_id))
    system(hub_db, {"enabled": True})
    assert run_pass(client)["queued"] == 0, "the pass that turns it on starts at the newest event"
    assert not batches(hub_db)
    failed(client, hub_db, 102, "checkout")
    assert run_pass(client)["queued"] == 1
    system(hub_db, {"enabled": False})
    failed(client, hub_db, 103, "checkout")
    assert run_pass(client)["queued"] == 0
    assert not sql(hub_db, select(tables.ops_cursors.c.project_id)), "off drops the cursor"


def test_only_the_default_system_turns_the_queue_on(client, hub, hub_db):
    system(hub_db, {"enabled": True}, default=False)
    run_pass(client)
    failed(client, hub_db, 151, "checkout")
    assert run_pass(client)["queued"] == 0, "ops on in a system that is not the default"
    assert not sql(hub_db, select(tables.ops_cursors.c.project_id))


def test_events_gather_by_cause_key_and_a_batch_closes_after_its_debounce(client, hub, hub_db):
    system(hub_db, {"enabled": True, "debounce_minutes": 5, "max_items": 20})
    run_pass(client)
    failed(client, hub_db, 201, "checkout", worker_id=7)
    failed(client, hub_db, 202, "checkout", worker_id=7)
    failed(client, hub_db, 203, "checkout", worker_id=8)
    record(client, hub_db, hub_events.RUN_PARKED, run_id=204, run_kind="chat", state="parked", worker_id=7)
    healthy = {"worker_id": 7, "disk": "ok", "disk_free_pct": 40, "logins": {"claude-code": "ok"}}
    record(client, hub_db, hub_events.WORKER_HEALTH, **healthy)
    record(client, hub_db, hub_events.WORKER_HEALTH, **{**healthy, "logins": {"codex": "unauthorized"}})
    assert run_pass(client)["queued"] == 4, "a parked run and a healthy worker are no item"
    assert batches(hub_db) == [
        ("run.ended|checkout|worker:7", "open", 2, None),
        ("run.ended|checkout|worker:8", "open", 1, None),
        ("worker.health|login:codex:unauthorized|worker:7", "open", 1, None),
    ]
    assert run_pass(client) == {**NO_TIER0, "queued": 0, "ready": 0, "empty": 0}, "not before the debounce"
    due(hub_db)
    assert run_pass(client) == {**NO_TIER0, "queued": 0, "ready": 3, "empty": 0}
    failed(client, hub_db, 205, "checkout", worker_id=7)
    run_pass(client)
    assert batches(hub_db)[-1] == ("run.ended|checkout|worker:7", "open", 1, None), "a new batch for the same key"


def test_a_batch_closes_at_max_items(client, hub, hub_db):
    system(hub_db, {"enabled": True, "max_items": 2})
    run_pass(client)
    for run_id in (301, 302, 303):
        failed(client, hub_db, run_id, "lease_lost")
    run_pass(client)
    assert [(state, pending) for _, state, pending, _ in batches(hub_db)] == [("ready", 2), ("open", 1)]


def test_items_tier_0_handled_leave_and_an_emptied_batch_closes_without_a_run(client, hub, hub_db):
    system(hub_db, {"enabled": True})
    run_pass(client)

    async def handles_404(conn, project, event):
        return event.payload.get("run_id") == 404

    failed(client, hub_db, 401, "network")
    failed(client, hub_db, 402, "network")
    failed(client, hub_db, 404, "network")
    assert run_pass(client, (handles_404,))["queued"] == 2, "an event a rule of tier 0 handled is no item"

    async def settle(engine):
        async with engine.begin() as conn:
            return await ops_queue.settle(conn, project_id(hub_db), by="tier0:rerun", run_ids=[401, 402])

    assert client.portal.call(settle, client.app.state.engine) == 2
    due(hub_db)
    assert run_pass(client) == {**NO_TIER0, "queued": 0, "ready": 0, "empty": 1}
    assert batches(hub_db) == [("run.ended|network|worker:7", "empty", 0, None)]
    i = tables.ops_items
    assert sql(hub_db, select(i.c.run_id, i.c.handled_by).order_by(i.c.id)) == [
        (401, "tier0:rerun"),
        (402, "tier0:rerun"),
    ]


def test_read_ops_defaults_and_bounds():
    assert ops_queue.read_ops(None) == ops_queue.OpsConfig(enabled=False, debounce_minutes=5, max_items=20)
    assert ops_queue.read_ops({"schema": "evo-agent-system/1"}).enabled is False, "a spec from before ops"
    found = ops_queue.read_ops({"ops": {"enabled": True, "debounce_minutes": -3, "max_items": "x"}})
    assert (found.enabled, found.debounce_minutes, found.max_items) == (True, 5, 20)


def test_a_login_the_worker_holds_by_lease_is_no_item_and_a_failing_one_is(client, hub, hub_db):
    """A worker whose runs authenticate with the hub's lease says ``leased`` for the runtime the machine has no login
    of: no item. One whose lease failed with a 401, or whose run had no lease, says the machine's login again."""
    system(hub_db, {"enabled": True})
    run_pass(client)
    payload = {"worker_id": 7, "disk": "ok", "disk_free_pct": 40}
    record(client, hub_db, hub_events.WORKER_HEALTH, **payload, logins={"claude-code": "leased", "opencode": "ok"})
    assert run_pass(client)["queued"] == 0, "leased is no item"
    assert not batches(hub_db)
    record(client, hub_db, hub_events.WORKER_HEALTH, **payload, logins={"claude-code": "unauthorized"})
    record(client, hub_db, hub_events.WORKER_HEALTH, **payload, logins={"claude-code": "missing", "codex": "leased"})
    assert run_pass(client)["queued"] == 2
    assert [key for key, *_ in batches(hub_db)] == [
        "worker.health|login:claude-code:unauthorized|worker:7",
        "worker.health|login:claude-code:missing|worker:7",
    ]
    assert ops_queue.health_causes({"logins": {"claude-code": "leased", "codex": "unknown"}}) == ["login:codex:unknown"]

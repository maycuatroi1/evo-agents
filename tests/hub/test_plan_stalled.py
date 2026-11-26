"""A plan that stopped moving and the traces that work on it (plan orchestrator-keeps-plans-moving, criteria a4 and
a5; ``evo_agents.hub.ops_stalls`` and ``system_traces.take_plan``): a trace with no plan takes the plan a brief refs; an
active plan a trace names, with a ready step and no active run, writes ``plan.stalled`` once a stall, which the next
pass of the ops queue queues.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import hub_events, system_context, tables
from evo_agents.hub.server.systems.service import traces as system_traces
from tests.hub.live import sql
from tests.hub.test_ops_batches import a_run, project_id, run_pass, system
from tests.hub.test_runs import OWNER, client, hub  # noqa: F401 (fixtures)

PLAN = "northwind-billing"
BODY = {
    "id": PLAN,
    "steps": [
        {"id": 1, "title": "Schema", "status": "done", "depends_on": []},
        {"id": 2, "title": "Invoices", "status": "pending", "depends_on": [1]},
    ],
}


def a_plan(db, plan_id: str = PLAN, *, revision: int = 3, age_minutes: int = 120) -> None:
    u = tables.users
    owner = sql(db, select(u.c.id).where(u.c.login == OWNER))[0][0]
    values = {"project_id": project_id(db), "plan_id": plan_id, "area": "active", "label": {}, "body": BODY}
    values |= {"revision": revision, "digest": "sha256:" + "0" * 64, "updated_by": owner}
    values["updated_at"] = func.now() - timedelta(minutes=age_minutes)
    sql(db, insert(tables.plans).values(**values))


def a_trace(db, run_id: int, *, plan_id: str | None = None, age_minutes: int = 0) -> int:
    """A trace of the project's system ``ops`` holding run ``run_id``, of plan ``plan_id``."""
    t, s = tables.system_traces, tables.system_spans
    values = {"project_id": project_id(db), "system_slug": "ops", "system_revision": 1, "plan_id": plan_id}
    values["last_at"] = func.now() - timedelta(minutes=age_minutes)
    (trace_id,) = sql(db, insert(t).values(**values).returning(t.c.id))[0]
    span = {"trace_id": trace_id, "run_id": run_id, "name": system_context.INVOKE, "agent": "orchestrator"}
    sql(db, insert(s).values(**span))
    return trace_id


def plan_of(db, trace_id: int) -> tuple:
    t = tables.system_traces
    return sql(db, select(t.c.plan_id, t.c.plan_revision).where(t.c.id == trace_id))[0]


def test_a_trace_whose_brief_refs_a_plan_takes_its_id(client, hub, hub_db):
    system(hub_db, None)
    a_plan(hub_db)
    a_plan(hub_db, "northwind-ledger", revision=7)
    asker, asked, other = (a_run(client, hub_db, "queued") for _ in range(3))
    free, held = a_trace(hub_db, asked), a_trace(hub_db, other, plan_id="northwind-ledger")

    async def brief(run_id: int, refs: list[str]) -> None:
        async with client.app.state.engine.begin() as conn:
            await system_traces.save_brief(conn, run_id, asker, {"goal": "Ship invoices", "refs": refs})

    client.portal.call(brief, asked, ["docs/hub.md", f"plan:{PLAN}"])
    assert plan_of(hub_db, free) == (PLAN, 3), "the plan and its revision now"
    client.portal.call(brief, other, [f"plan:{PLAN}"])
    assert plan_of(hub_db, held) == ("northwind-ledger", None), "a trace that has a plan keeps it"
    client.portal.call(brief, asked, ["plan:northwind-ledger"])
    assert plan_of(hub_db, free) == (PLAN, 3)


def stalls(db) -> list[dict]:
    e = tables.hub_events
    return [payload for (payload,) in sql(db, select(e.c.payload).where(e.c.kind == hub_events.PLAN_STALLED))]


def test_a_plan_with_a_ready_step_and_no_active_run_writes_plan_stalled(client, hub, hub_db):
    system(hub_db, {"enabled": True, "plan_stall_minutes": 30})
    run_pass(client)
    a_plan(hub_db)
    a_plan(hub_db, "northwind-fresh", age_minutes=5)  # a revision in the last 30 minutes
    a_plan(hub_db, "northwind-unrun")  # no trace names it: a person runs it
    a_plan(hub_db, "northwind-busy")
    done, busy = a_run(client, hub_db, "failed"), a_run(client, hub_db, "queued")
    a_trace(hub_db, done, plan_id=PLAN, age_minutes=60)
    a_trace(hub_db, a_run(client, hub_db, "failed"), plan_id="northwind-fresh", age_minutes=60)
    a_trace(hub_db, busy, plan_id="northwind-busy", age_minutes=60)
    assert run_pass(client)["stalled"] == 1
    assert stalls(hub_db) == [
        {"v": 1, "plan_id": PLAN, "revision": 3, "run_id": done, "steps": ["2"], "state": "stalled"}
    ]
    assert run_pass(client)["stalled"] == 0, "once a stall"
    i = tables.ops_items
    assert sql(hub_db, select(i.c.kind, i.c.cause, i.c.subject)) == [
        (hub_events.PLAN_STALLED, "stalled", f"plan:{PLAN}")
    ]
    sql(hub_db, update(tables.plans).values(revision=4).where(tables.plans.c.plan_id == PLAN))
    assert run_pass(client)["stalled"] == 1, "a new revision re-arms it"

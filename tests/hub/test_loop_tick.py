"""The tick of a project's Loop (``evo_agents.hub.server.loop``: ``tick``, ``view``, ``state``; ``evo_agents.hub.
loop_jobs``), as criteria a4 and a11 of plan loop-engine name them, on the hub with Postgres.

The Loop is off by default and comes on with a revision of the autonomy policy, its cursor at the project's newest
event: nothing from before is read again. Each event of a project whose Loop is on queues a tick, one waiting at a
time per project, and every 5 minutes each such project gets one. A tick reads the events past its cursor, the clock
and the work items not finished, and writes work items with their ancestors, a key that makes each once, and a ledger;
with no hand-out it hands out no run, it calls no model, and with nothing new it writes no work item. The chain here:
goal G-1 with a metric drives the canvas of Northwind Pantry, whose claim CL-1 links use case UC ORD-1 of the made-up
SRS document demo-shop; a baseline moves CL-1 to testing, someone-else approves version 1 of demo-shop, the plugin srs
makes its frame tasks, and the Loop's build waits on the frame task of UC ORD-1 until it is ready, then in the queue
for want of loop.worker (``tests.hub.test_loop_admission`` has the rest of admission). Reading the same events again,
and an event told twice, make nothing more. A claim invalidated, a goal closed and the Loop turned off cancel what is
left. A project with the Loop off is as before the plan: no state, no tick, no work item.
"""

from __future__ import annotations

import dataclasses
from functools import partial
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import column, func, select, table, update
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub import hub_events, jobs, loop_jobs, plugin_calls, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.loop.tick import fit_key, tick
from evo_agents.hub.tables_srs import frame_task_key
from evo_agents.hub.worker import queue
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_ba_agent import delivered, enable, granted, imported, members, review, submitted  # noqa: F401
from tests.hub.test_runs import PROJECT
from tests.hub.test_strategist_agent import BASE, CANVAS, CARD, SLUG, edit
from tests.hub.test_value_check import NUMBER_KEY, add_key

app_key = credentials_api.app_key  # a fixture
CORE = {
    name: EntryPoint(name, f"evo_agents.core_plugins.{name}:MANIFEST", ENTRY_POINT_GROUP)
    for name in ("canvas", "srs", "tasks")
}
BASELINES = f"{CANVAS}/baselines"
GOALS = f"/v1/projects/{PROJECT}/goals"
POLICY = f"/v1/projects/{PROJECT}/policy"
METRIC = {"key": NUMBER_KEY, "aggregate": "last", "comparator": ">=", "target": 0.5}
JOBS = table(
    "procrastinate_jobs", column("task_name"), column("args", JSONB), column("queueing_lock"), column("status")
)
FRAME = frame_task_key("demo-shop", 1, "UC ORD-1")


@pytest.fixture
def loop_core(monkeypatch):
    """The entry points of the core plugins with canvas's, srs's and tasks's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3, loop_core):  # noqa: F811
    config = dataclasses.replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, members) -> dict:  # noqa: F811
    """``members`` with plugins canvas, srs and tasks on, chi's draft of demo-shop, chi's canvas of Northwind Pantry
    with claim CL-1 linking use case UC ORD-1, a key of kind number, and goal G-1 of owner, whose metric is not met,
    driving the canvas."""
    for plugin, permissions in (("canvas", ()), ("srs", ()), ("tasks", ["plans:read"])):
        enable(client, members["owner"], plugin, permissions)
    made = client.post(f"{BASE}/canvases", json={"slug": SLUG, "name": "Northwind Pantry"}, headers=members["chi"])
    assert made.status_code == 201, made.text
    edit(client, members["chi"], {"cell": "problem", "text": "Corner shops run out of stock between deliveries"})
    imported(client, members["chi"])  # demo-shop's version 1, a draft, holds UC ORD-1
    edit(client, members["chi"], {"claim": "CL-1", "base_rev": 1, "use_cases": ["UC ORD-1"]})
    add_key(client, members, NUMBER_KEY, "number")
    goal = {"statement": "Corner shops reorder every week", "metric": METRIC, "canvas": SLUG}
    written = client.put(f"{GOALS}/G-1", json=goal, headers=members["owner"])
    assert written.status_code == 200, written.text
    return members


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def policy(client, hub, loop: dict) -> dict:
    written = client.put(POLICY, json={"policy": {"loop": loop} if loop else {}}, headers=hub["owner"])
    assert written.status_code == 200, written.text
    return written.json()


def ticked(client, db) -> dict:
    """One tick of the project's Loop as the worker runs hub.loop_tick for it, with no hand-out: the runs are
    ``tests.hub.test_loop_dispatch``'s."""
    return client.portal.call(partial(tick, client.app.state.engine, project_id(db), dispatch=None))


def state(db) -> tuple | None:
    s = tables.loop_states
    found = sql(db, select(s.c.enabled, s.c.cursor, s.c.breaker, s.c.ticked_at.is_not(None)))
    return found[0] if found else None


def items(db) -> list[tuple]:
    i = tables.loop_items
    return sql(db, select(i.c.key, i.c.kind, i.c.state, i.c.reason).order_by(i.c.id))


def item(db, key: str):
    i = tables.loop_items
    (found,) = sql(db, select(i).where(i.c.key == key))
    return dict(zip(i.columns.keys(), found, strict=True))


def ledger(db) -> list[tuple]:
    i, e = tables.loop_items, tables.loop_item_events
    query = select(i.c.key, e.c.kind, e.c.state, e.c.reason).join_from(e, i, i.c.id == e.c.item_id).order_by(e.c.id)
    return sql(db, query)


def counts(db) -> tuple[int, int, int]:
    """(work items, rows of their ledger, runs): what a tick may write."""
    return tuple(
        sql(db, select(func.count()).select_from(name))[0][0]
        for name in (tables.loop_items, tables.loop_item_events, tables.runs)
    )


def waiting_ticks(db) -> list[tuple]:
    query = select(JOBS.c.args, JOBS.c.queueing_lock, JOBS.c.status).where(JOBS.c.task_name == jobs.LOOP_TICK)
    return [(args, lock) for args, lock, status in sql(db, query) if status == "todo"]


def newest_event(db) -> int:
    e = tables.hub_events
    return sql(db, select(func.max(e.c.id)))[0][0]


def push_baseline(client, hub, ref: str) -> None:
    """chi opens a baseline of claim ``ref`` at once and someone-else accepts it: it is approved."""
    opened = client.post(BASELINES, json={"claim": ref}, headers=hub["chi"])
    assert opened.status_code == 201, opened.text
    number = opened.json()["number"]
    decided = client.post(
        f"{BASELINES}/{number}/items/{ref}/decide", json={"decision": "accepted"}, headers=hub["other"]
    )
    assert decided.status_code == 200, decided.text


def test_loop_tick_a_project_with_the_loop_off_has_no_state_no_tick_and_no_work_item(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    policy(client, hub, {})  # a policy with no loop section: the Loop stays off
    policy(client, hub, {"enabled": False, "worker": "studio"})
    assert state(hub_db) is None and waiting_ticks(hub_db) == []
    assert ticked(client, hub_db) == {"off": 1}
    assert counts(hub_db)[:2] == (0, 0)
    report = client.portal.call(partial(loop_jobs.queue_ticks, client.app.state.engine, client.app.state.jobs._manager))
    assert report == {"projects": 0, "queued": 0}


def test_loop_tick_comes_on_with_the_policy_reads_from_the_newest_event_and_queues_one_tick_at_a_time(
    client, hub, hub_db
):
    newest = newest_event(hub_db)
    written = policy(client, hub, {"enabled": True})
    assert written["effective"]["loop"]["enabled"] is True and written["effective"]["loop"]["stable_hours"] == 0
    pid = project_id(hub_db)
    assert state(hub_db) == (True, newest, "armed", False)  # no backfill: the cursor at the newest event
    assert waiting_ticks(hub_db) == [({"project_id": pid}, f"loop:{pid}")]
    # each event of the project queues a tick; while one waits, the next joins it
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    edit(client, hub["chi"], {"cell": "solution", "text": "A reorder button"})
    report = client.portal.call(partial(loop_jobs.queue_ticks, client.app.state.engine, client.app.state.jobs._manager))
    assert report == {"projects": 1, "queued": 0}  # one waits already
    assert waiting_ticks(hub_db) == [({"project_id": pid}, f"loop:{pid}")]
    # the worker's jobs: the periodic one every 5 minutes, the tick the server hands down
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.LOOP_TICKS].cron == "*/5 * * * *"
    assert queue.tasks[jobs.LOOP_TICKS].queueing_lock == jobs.LOOP_TICKS
    engine = client.app.state.engine
    bare = SimpleNamespace(additional_context={"hub": SimpleNamespace(engine=engine)})
    assert client.portal.call(partial(queue.tasks[jobs.LOOP_TICK].func, bare, project_id=pid)) == {"no_loop_tick": 1}
    handed = SimpleNamespace(additional_context={"hub": SimpleNamespace(engine=engine, loop_tick=tick)})
    found = client.portal.call(partial(queue.tasks[jobs.LOOP_TICK].func, handed, project_id=pid))
    assert found["events"] == 2 and state(hub_db)[1] == newest_event(hub_db) and state(hub_db)[3] is True
    # off: no tick reads anything any more
    policy(client, hub, {"enabled": False})
    assert state(hub_db)[0] is False and ticked(client, hub_db) == {"off": 1}


def test_loop_tick_runs_a_goal_to_the_builds_of_its_claim_once_however_often_it_reads_the_chain(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    pid, start = project_id(hub_db), state(hub_db)[1]
    # row 1: the goal's revision makes a brainstorm, at once and once
    assert ticked(client, hub_db)["changed"] == 1
    assert items(hub_db) == [("brainstorm:G-1:r1", "brainstorm", "queued", None)]
    brainstorm = item(hub_db, "brainstorm:G-1:r1")
    assert (brainstorm["owner"], brainstorm["engine"], brainstorm["cycle"]) == ("@strategist", "rules/1", "business")
    # nothing new: the tick writes no work item, no ledger row and no run
    before = counts(hub_db)
    assert ticked(client, hub_db) == {"events": 0, "changed": 0}
    assert counts(hub_db) == before

    # row 3: a baseline moves CL-1 to testing
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    assert ticked(client, hub_db)["changed"] == 1
    case = item(hub_db, f"use_case:{SLUG}:CL-1:b1")
    assert (case["kind"], case["state"], case["owner"], case["goal_id"]) == (
        "use_case",
        "queued",
        "@ba",
        brainstorm["goal_id"],
    )
    assert case["claim_id"] is not None and case["event_id"] is not None

    # row 6: version 1 of demo-shop, which adds UC ORD-1, approved: the use case done, a build waiting for its frame
    submitted(client, hub["chi"], 1)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    ticked(client, hub_db)
    assert items(hub_db)[1:] == [
        (f"use_case:{SLUG}:CL-1:b1", "use_case", "done", None),
        (f"build:{FRAME}", "build", "watching", "auto_tasks"),
    ]
    built = item(hub_db, f"build:{FRAME}")
    assert (built["use_case"], built["srs_version"], built["parent_id"], built["task_id"]) == (
        "UC ORD-1",
        1,
        case["id"],
        None,
    )
    assert (built["claim_id"], built["cycle"]) == (case["claim_id"], "development")
    # row 7: the plugin srs makes the frame tasks (no split run: @ba is off), and the build queues on its own; the
    # policy names no loop.worker, so admission holds it (criterion a5)
    delivered(client)
    ticked(client, hub_db)
    built = item(hub_db, f"build:{FRAME}")
    t = tables.tasks.items
    ((frame_id, number),) = sql(hub_db, select(t.c.id, t.c.number).where(t.c.external_key == FRAME))
    assert (built["state"], built["reason"], built["task_id"], built["detail"]["task"]) == (
        "queued",
        "worker",
        frame_id,
        number,
    )
    made = sql(hub_db, select(func.count()).select_from(t))[0][0]
    assert made == 3, "the Loop makes no task: the three frames are the plugin srs's"

    # reading the whole chain again, and the baseline told twice, make nothing more
    chain = counts(hub_db)
    s = tables.loop_states
    sql(hub_db, update(s).where(s.c.project_id == pid).values(cursor=start))
    (baseline_payload,) = [
        payload
        for (payload,) in sql(
            hub_db, select(tables.hub_events.c.payload).where(tables.hub_events.c.kind == "canvas.baseline")
        )
    ]

    async def told_again():
        async with client.app.state.engine.begin() as conn:
            await hub_events.record(conn, hub_events.BASELINE, pid, baseline_payload)

    client.portal.call(told_again)
    again = ticked(client, hub_db)
    assert again["events"] > 5 and again["changed"] == 0
    assert counts(hub_db) == chain

    # row 4: CL-1 invalidated cancels the build, notes it in the chain and tells the open frame task
    claim = client.get(f"{CANVAS}/claims/CL-1", headers=hub["chi"]).json()
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": claim["rev"], "status": "invalidated"})
    push_baseline(client, hub, "CL-1")
    ticked(client, hub_db)
    assert items(hub_db)[2] == (f"build:{FRAME}", "build", "cancelled", "claim_invalidated")
    noted = [row for row in ledger(hub_db) if row[1] == "noted"]
    assert noted == [(f"build:{FRAME}", "noted", "queued", "worker")]  # noted as the event is read, then cancelled
    c = tables.tasks.comments
    ((body,),) = sql(hub_db, select(c.c.body).where(c.c.task_id == frame_id))
    assert body.startswith(f"Claim CL-1 of canvas {SLUG} is invalidated since baseline 2")
    ticked(client, hub_db)
    assert len([row for row in ledger(hub_db) if row[1] == "noted"]) == 1

    # row 14: the goal closed cancels its brainstorm; the Loop off cancels whatever is left
    goal = client.post(f"{GOALS}/G-1/close", json={"reason": "dropped"}, headers=hub["owner"])
    assert goal.status_code == 200, goal.text
    ticked(client, hub_db)
    assert items(hub_db)[0] == ("brainstorm:G-1:r1", "brainstorm", "cancelled", "goal_closed")
    ledger_kinds = [kind for key, kind, _, _ in ledger(hub_db) if key == f"build:{FRAME}"]
    assert ledger_kinds == ["created", "ready", "waiting", "noted", "cancelled"]


def test_loop_tick_turning_the_loop_off_cancels_what_is_not_finished_and_a_tripped_breaker_holds_the_cursor(
    client, hub, hub_db
):
    policy(client, hub, {"enabled": True, "stable_hours": 24})
    ticked(client, hub_db)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    s = tables.loop_states
    sql(hub_db, update(s).values(breaker="tripped", breaker_reason="too many work items", tripped_at=func.now()))
    held = state(hub_db)[1]
    assert ticked(client, hub_db) == {"events": 0, "changed": 0, "breaker": 1, "waiting": 1}
    assert state(hub_db)[1] == held  # the events wait past the cursor; none is lost
    assert items(hub_db) == [("brainstorm:G-1:r1", "brainstorm", "queued", "breaker")]  # admission holds it
    sql(hub_db, update(s).values(breaker="armed"))
    ticked(client, hub_db)
    assert items(hub_db) == [
        ("brainstorm:G-1:r1", "brainstorm", "queued", None),
        (f"use_case:{SLUG}:CL-1:b1", "use_case", "watching", "stable"),  # 24 hours before a baseline counts
    ]
    policy(client, hub, {"enabled": False})
    assert [row[2:] for row in items(hub_db)] == [("cancelled", "loop_off"), ("cancelled", "loop_off")]
    assert [row[1:] for row in ledger(hub_db)][-2:] == [("cancelled", "cancelled", "loop_off")] * 2
    # on again: the cursor at the newest event, and the keys of the cancelled work items still taken
    policy(client, hub, {"enabled": True, "stable_hours": 24})
    assert state(hub_db)[:2] == (True, newest_event(hub_db))
    ticked(client, hub_db)
    assert len(items(hub_db)) == 2


def test_loop_tick_keeps_a_long_key_unique_within_its_column():
    short = "build:srs:demo-shop:1:UC ORD-1"
    assert fit_key(short) == short
    long_a, long_b = "build:" + "a" * 600, "build:" + "a" * 599 + "b"
    assert len(fit_key(long_a)) == 500 and fit_key(long_a) != fit_key(long_b)

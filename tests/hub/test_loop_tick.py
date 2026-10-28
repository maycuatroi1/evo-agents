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

The engine is rules/2 (plan problem-oriented-hub, criterion a6): once G-1 answers problem P-1, each work item carries
P-1; in a project whose solutions skip use cases (``loop.use_cases`` never) CL-1 makes a build straight from the
claim, a claim of the Problem cell a discover, and a claim of another cell no core work item; the routes name the
problem of each work item and of a chain. rules/2 goes on from the cursor rules/1 left and makes no work item again.

A deliver whose plan run merged the pull request of its branch on GitHub itself goes to done with no person: the tick
reads the pull request with the workers' App and marks the repo merged in the plan as the run's dispatcher.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from functools import partial
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import column, func, insert, select, table, update
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub import hub_events, jobs, loop_jobs, plugin_calls, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.loop import rules, srs_ledger, texts
from evo_agents.hub.server.loop.model import ItemView
from evo_agents.hub.server.loop.tick import fit_key, tick
from evo_agents.hub.server.loop.view import use_cases
from evo_agents.hub.tables_srs import frame_task_key
from evo_agents.hub.worker import queue
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import test_credentials_api as credentials_api
from tests.hub.fake_github import APP_PERMISSIONS, FakePull
from tests.hub.live import sql
from tests.hub.test_author_runs import HARNESS
from tests.hub.test_ba_agent import (  # noqa: F401 (members is a fixture)
    OPEN_SINKS,
    delivered,
    enable,
    granted,
    imported,
    members,
    review,
    submitted,
)
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
HARNESS_ORIGIN = f"https://git.example.org/evo/{HARNESS}.git"
LOOP_ITEMS = f"/v1/projects/{PROJECT}/loop/items"
SHORTAGE = {
    "statement": "Corner shops run out of milk between deliveries",
    "who": "the owners of corner shops",
    "when": "on the days before the weekly delivery",
    "workaround": "they buy milk at a supermarket at retail price",
}


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
    with claim CL-1 of the Solution cell linking use case UC ORD-1, a key of kind number, and goal G-1 of owner, whose
    metric is not met, driving the canvas. The project keeps an SRS document, so its solutions go through use cases
    (``loop.use_cases`` auto)."""
    for plugin, permissions in (("canvas", ()), ("srs", ()), ("tasks", ["plans:read"])):
        enable(client, members["owner"], plugin, permissions)
    made = client.post(f"{BASE}/canvases", json={"slug": SLUG, "name": "Northwind Pantry"}, headers=members["chi"])
    assert made.status_code == 201, made.text
    edit(client, members["chi"], {"cell": "solution", "text": "A reorder list built from the shop's last orders"})
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


def harness_listed(client, hub) -> None:
    """Register the project again with its harness among its repos, with an origin (``test_plans.registration``
    gives the harness's path alone): the Loop hands out an author run only then."""
    body = {
        **credentials_api.registration({**credentials_api.DISPATCHED, HARNESS: HARNESS_ORIGIN}),
        "sinks": OPEN_SINKS,
    }
    registered = client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"])
    assert registered.status_code == 200, registered.text


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
    assert (brainstorm["owner"], brainstorm["engine"], brainstorm["cycle"]) == ("@strategist", "rules/2", "business")
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


# rules/2: the Loop goes from the problem a goal answers


def answers_shortage(client, hub, db) -> int:
    """Problem P-1 of the project, which goal G-1 answers from its revision 2; P-1's id."""
    written = client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=SHORTAGE, headers=hub["chi"])
    assert written.status_code == 200, written.text
    goal = {"statement": "Corner shops reorder every week", "metric": METRIC, "canvas": SLUG, "problem": "P-1"}
    named = client.put(f"{GOALS}/G-1", json=goal, headers=hub["owner"])
    assert named.status_code == 200 and named.json()["problem"] == "P-1", named.text
    p = tables.problems
    return sql(db, select(p.c.id).where(p.c.number == 1))[0][0]


def problems_of(db) -> dict[str, int | None]:
    i = tables.loop_items
    return dict(sql(db, select(i.c.key, i.c.problem_id).order_by(i.c.id)))


def test_loop_tick_rules2_goes_from_a_problem_to_a_build_of_a_claim_and_a_discover_of_a_problem(client, hub, hub_db):
    problem_id = answers_shortage(client, hub, hub_db)
    policy(client, hub, {"enabled": True, "use_cases": "never"})
    start = state(hub_db)[1]
    ticked(client, hub_db)
    assert items(hub_db) == [("brainstorm:G-1:r2", "brainstorm", "queued", None)]
    assert item(hub_db, "brainstorm:G-1:r2")["engine"] == "rules/2"

    # CL-1, a solution, in testing: a build straight from the claim, no use case (the project keeps none for the Loop)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    # CL-2, a problem, in testing: a discover of @strategist; CL-3, a customer segment, makes no core work item
    edit(client, hub["chi"], {"cell": "problem", "text": "Shops cannot tell what sold out", "problems": ["P-1"]})
    edit(client, hub["chi"], {"cell": "customer_segments", "text": "Corner shops of one owner"})
    for ref in ("CL-2", "CL-3"):
        edit(client, hub["chi"], {"claim": ref, "base_rev": 1, "status": "testing", "test_card": CARD})
        push_baseline(client, hub, ref)
    ticked(client, hub_db)
    build_key, discover_key = f"build:claim:{SLUG}:CL-1:b1", f"discover:{SLUG}:CL-2:b2"
    assert items(hub_db)[1:] == [
        (build_key, "build", "queued", "worker"),  # no loop.worker: admission holds it
        (discover_key, "discover", "queued", None),
    ]
    built, found = item(hub_db, build_key), item(hub_db, discover_key)
    assert (built["cycle"], built["engine"], built["use_case"], built["task_id"]) == (
        "development",
        "rules/2",
        None,
        None,
    )
    assert built["detail"] == {
        "baseline": 1,
        "from": "assumption",
        "claim": "CL-1",
        "canvas": SLUG,
        "worker": None,
        "why": "the policy names no loop.worker",
    }
    assert (found["cycle"], found["owner"], found["claim_id"]) == ("business", "@strategist", built["claim_id"] + 1)
    assert set(problems_of(hub_db).values()) == {problem_id}, "each work item carries the problem of its goal"

    # the routes name the problem of each work item and of a chain, and list a problem's work items and a discover
    listed = client.get(LOOP_ITEMS, params={"problem": "P-1"}, headers=hub["reader"]).json()["items"]
    assert sorted((entry["kind"], entry["problem"]) for entry in listed) == [
        ("brainstorm", "P-1"),
        ("build", "P-1"),
        ("discover", "P-1"),
    ]
    assert client.get(LOOP_ITEMS, params={"problem": "P-9"}, headers=hub["reader"]).json()["items"] == []
    (shown,) = client.get(LOOP_ITEMS, params={"kind": "discover"}, headers=hub["reader"]).json()["items"]
    assert (shown["claim"], shown["owner"]) == ({"canvas": SLUG, "code": "CL-2"}, "@strategist")
    chain = client.get(f"{LOOP_ITEMS}/{found['id']}", headers=hub["reader"]).json()
    assert chain["stages"]["problem"] == {"code": "P-1", "statement": SHORTAGE["statement"], "status": "open"}
    assert chain["stages"]["claim"]["code"] == "CL-2"
    assert client.get(LOOP_ITEMS, params={"problem": "G-1"}, headers=hub["reader"]).status_code == 422

    # reading the chain again, with the events renumbered by telling them again, makes nothing more
    before = counts(hub_db)
    s, e = tables.loop_states, tables.hub_events
    sql(hub_db, update(s).values(cursor=start))
    assert ticked(client, hub_db)["changed"] == 0
    told = sql(hub_db, select(e.c.kind, e.c.payload).where(e.c.kind == hub_events.BASELINE).order_by(e.c.id))

    async def told_again():
        async with client.app.state.engine.begin() as conn:
            for kind, payload in told:
                await hub_events.record(conn, kind, project_id(hub_db), payload)

    client.portal.call(told_again)
    assert ticked(client, hub_db)["changed"] == 0
    assert counts(hub_db) == before


def test_loop_tick_rules2_goes_from_an_opportunity_a_goal_answers_and_lists_its_work_items(client, hub, hub_db):
    body = {"statement": "Sell the scanner to the depot", "beneficiary": "the night shift", "leverage": "two demos"}
    written = client.put(f"/v1/projects/{PROJECT}/opportunities/O-1", json=body, headers=hub["chi"])
    assert written.status_code == 200, written.text
    goal = {"statement": "Corner shops reorder every week", "metric": METRIC, "canvas": SLUG, "opportunity": "O-1"}
    named = client.put(f"{GOALS}/G-1", json=goal, headers=hub["owner"])
    assert named.status_code == 200 and named.json()["opportunity"] == "O-1", named.text
    policy(client, hub, {"enabled": True, "use_cases": "never"})
    ticked(client, hub_db)
    o, i = tables.opportunities, tables.loop_items
    opportunity_id = sql(hub_db, select(o.c.id).where(o.c.number == 1))[0][0]
    assert sql(hub_db, select(i.c.key, i.c.opportunity_id, i.c.problem_id)) == [
        ("brainstorm:G-1:r2", opportunity_id, None)
    ]
    listed = client.get(LOOP_ITEMS, params={"opportunity": "O-1"}, headers=hub["reader"]).json()["items"]
    assert [(entry["kind"], entry["opportunity"]) for entry in listed] == [("brainstorm", "O-1")]
    assert client.get(LOOP_ITEMS, params={"opportunity": "O-9"}, headers=hub["reader"]).json()["items"] == []
    assert client.get(LOOP_ITEMS, params={"opportunity": "P-1"}, headers=hub["reader"]).status_code == 422


def test_loop_tick_rules2_goes_on_from_the_cursor_rules1_left_and_moves_its_work_items(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    start = state(hub_db)[1]
    ticked_by_rules1 = partial(tick, client.app.state.engine, project_id(hub_db), rules=rules.RULES, dispatch=None)
    client.portal.call(ticked_by_rules1)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    client.portal.call(ticked_by_rules1)
    case = item(hub_db, f"use_case:{SLUG}:CL-1:b1")
    assert (case["engine"], case["state"], case["problem_id"]) == ("rules/1", "queued", None)
    made = counts(hub_db)

    # rules/2 from the cursor rules/1 left, and over the whole chain again: the same keys, nothing more
    assert ticked(client, hub_db)["changed"] == 0
    sql(hub_db, update(tables.loop_states).values(cursor=start))
    assert ticked(client, hub_db)["changed"] == 0
    assert counts(hub_db) == made

    # the goal answers a problem now: rules/1's use case moves as it did, and the build that comes of it carries P-1
    problem_id = answers_shortage(client, hub, hub_db)
    submitted(client, hub["chi"], 1)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    ticked(client, hub_db)
    assert item(hub_db, f"use_case:{SLUG}:CL-1:b1")["state"] == "done"
    built = item(hub_db, f"build:{FRAME}")
    assert (built["engine"], built["parent_id"], built["problem_id"]) == ("rules/2", case["id"], problem_id)
    listed = client.get(LOOP_ITEMS, headers=hub["reader"]).json()["items"]
    assert {entry["kind"]: entry["problem"] for entry in listed if entry["kind"] != "brainstorm"} == {
        "build": "P-1",
        "use_case": None,
    }


def test_loop_tick_use_cases_auto_follows_the_srs_documents_of_the_project(client, hub, hub_db):
    pid = project_id(hub_db)

    async def mode(value: str) -> bool:
        async with client.app.state.engine.begin() as conn:
            return await use_cases(conn, pid, {"use_cases": value})

    # the plugin srs on and demo-shop not archived: auto goes through use cases; always and never fix it
    assert [client.portal.call(mode, value) for value in ("auto", "always", "never")] == [True, True, False]
    assert policy(client, hub, {"enabled": True})["effective"]["loop"]["use_cases"] == "auto"
    d = tables.srs.documents
    sql(hub_db, update(d).values(archived_at=func.now()))
    assert [client.portal.call(mode, value) for value in ("auto", "always", "never")] == [False, True, False]
    refused = client.put(POLICY, json={"policy": {"loop": {"use_cases": "sometimes"}}}, headers=hub["owner"])
    assert refused.status_code == 422 and "loop.use_cases: one of always, never, auto" in refused.text

    # with no SRS document left, a solution in testing makes a build straight from its claim
    ticked(client, hub_db)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    ticked(client, hub_db)
    assert [row[:2] for row in items(hub_db)] == [
        ("brainstorm:G-1:r1", "brainstorm"),
        (f"build:claim:{SLUG}:CL-1:b1", "build"),
    ]


SRS_METRIC = {"key": "srs.backlog.open", "aggregate": "last", "comparator": "<=", "target": 0}


def srs_goal(client, hub, hub_db) -> tuple[int, int]:
    """Goal G-1 aimed at demo-shop, whose version 1 is approved before the Loop is on; (document, goal) ids."""
    body = {"statement": "Deliver the order service", "srs_document": "demo-shop", "metric": SRS_METRIC}
    assert client.put(f"{GOALS}/G-1", json=body, headers=hub["owner"]).status_code == 200
    submitted(client, hub["chi"], 1)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    document = sql(hub_db, select(tables.srs.documents.c.id))[0][0]
    return document, sql(hub_db, select(tables.goals.c.id))[0][0]


def confirm(hub_db, document: int, goal: int) -> None:
    sql(hub_db, insert(tables.srs_baselines).values(goal_id=goal, project_id=project_id(hub_db), document_id=document))


def ledger_of(db) -> dict[str, tuple]:
    s = tables.srs_delivery
    return {code: (state, source) for code, state, source in sql(db, select(s.c.code, s.c.state, s.c.source))}


def test_loop_tick_srs_goal_builds_each_group_of_its_confirmed_backlog_once_and_delivery_marks_it_delivered(
    client, hub, hub_db
):
    document, goal = srs_goal(client, hub, hub_db)
    policy(client, hub, {"enabled": True})  # on late: what was approved before it came is the backlog
    ticked(client, hub_db)
    assert items(hub_db) == [], "the baseline is not confirmed: no build, and an SRS goal makes no brainstorm"
    confirm(hub_db, document, goal)
    report = ticked(client, hub_db)
    built = [(key, kind, state) for key, kind, state, _ in items(hub_db)]
    assert report["changed"] >= 1 and len(built) >= 1 and {kind for _, kind, _ in built} == {"build"}
    assert all(key.startswith(f"build:srs:{document}:") and key.endswith(":1") for key, _, _ in built)
    assert len(keys := [key for key, _, _ in built]) == 2, "demo-shop has two headings with use cases"
    covered = sorted(case["code"] for key in keys for case in item(hub_db, key)["detail"]["backlog"]["use_cases"])
    assert covered == ["UC ORD-1", "UC ORD-2", "UC RET-1"], "every approved use case is in one build"
    first = item(hub_db, keys[0])
    assert (first["cycle"], first["srs_document_id"], first["use_case"], first["goal_id"]) == (
        "development",
        document,
        None,
        goal,
    )
    assert ledger_of(hub_db) == dict.fromkeys(covered, ("delivering", "deliver"))
    before = counts(hub_db)
    ticked(client, hub_db)
    assert counts(hub_db)[:2] == before[:2], "the same tick again makes nothing: the group has a work item"

    # the author run of the first build wrote a plan: a deliver of it, as for any build; done, its use cases are delivered
    codes = [case["code"] for case in first["detail"]["backlog"]["use_cases"]]
    i = tables.loop_items
    sql(hub_db, update(i).where(i.c.id == first["id"]).values(state="done", reason=None, finished_at=func.now()))
    deliver_row = {"project_id": project_id(hub_db), "kind": "deliver", "cycle": "development", "engine": "rules/2"}
    deliver_row |= {"key": "deliver:srs-plan", "goal_id": goal, "parent_id": first["id"], "plan_id": "srs-plan"}
    deliver = sql(hub_db, insert(i).values(**deliver_row, state="watching").returning(i.c.id))[0][0]
    ticked(client, hub_db)
    assert {ledger_of(hub_db)[code] for code in codes} == {("delivering", "deliver")}, "its deliver is not done"
    sql(hub_db, update(i).where(i.c.id == deliver).values(state="done", reason=None, finished_at=func.now()))
    ticked(client, hub_db)
    ledger = ledger_of(hub_db)
    assert {ledger[code] for code in codes} == {("delivered", "deliver")}
    s = tables.srs_delivery
    evidence = [e for (e,) in sql(hub_db, select(s.c.evidence).where(s.c.code.in_(codes)))]
    expected = {"plan": "srs-plan", "merges": [], "after_sha": [], "deliver": deliver, "build": first["id"]}
    assert evidence == [expected] * len(codes)
    open_codes = sorted(set(covered) - set(codes))
    assert {ledger[code] for code in open_codes} <= {("delivering", "deliver")}

    # a build cancelled puts its use cases back in the backlog, and the next tick makes the next round of its group
    second = item(hub_db, keys[1])
    cancelled = {"state": "cancelled", "reason": "person", "finished_at": func.now()}
    sql(hub_db, update(i).where(i.c.id == second["id"]).values(**cancelled))
    ticked(client, hub_db)
    assert [code for code, (state, _) in ledger_of(hub_db).items() if state == "undelivered"] == sorted(
        case["code"] for case in second["detail"]["backlog"]["use_cases"]
    )
    ticked(client, hub_db)
    assert [key for key, *_ in items(hub_db) if key.endswith(":2")] == [keys[1][:-1] + "2"]


def test_loop_tick_turning_the_loop_off_settles_the_use_cases_its_cancelled_builds_held(client, hub, hub_db):
    document, goal = srs_goal(client, hub, hub_db)
    policy(client, hub, {"enabled": True})
    confirm(hub_db, document, goal)
    ticked(client, hub_db)
    assert {state for state, _ in ledger_of(hub_db).values()} == {"delivering"}
    policy(client, hub, {"enabled": False})  # no tick runs after this: the same operation settles the ledger
    assert {row[2] for row in items(hub_db)} == {"cancelled"}
    assert {state for state, _ in ledger_of(hub_db).values()} == {"undelivered"}, "nothing is stuck as delivering"


def test_loop_tick_srs_goal_gives_the_author_run_every_use_case_of_its_group(client, hub, hub_db):
    document, goal = srs_goal(client, hub, hub_db)
    policy(client, hub, {"enabled": True})
    confirm(hub_db, document, goal)
    ticked(client, hub_db)
    row = item(hub_db, items(hub_db)[0][0])
    view = ItemView(
        row["id"], "build", row["key"], "queued", None, goal, srs_document_id=document, detail=row["detail"]
    )
    assert view.from_srs

    async def request():
        async with client.app.state.engine.begin() as conn:
            return await texts.srs_request(conn, project_id(hub_db), PROJECT, view)

    asked = client.portal.call(request)
    for said in ("demo-shop", "Deliver the order service", row["key"], "every use case below counts as delivered"):
        assert said in asked, asked
    for case in row["detail"]["backlog"]["use_cases"]:
        assert case["code"] in asked and f"version {case['version']}" in asked
    listed = client.get(LOOP_ITEMS, headers=hub["chi"]).json()["items"]
    (shown,) = [entry for entry in listed if entry["id"] == row["id"]]
    assert (shown["kind"], shown["goal"], shown["use_case"]["document"]) == ("build", "G-1", "demo-shop")


def test_loop_tick_a_goal_with_no_srs_document_and_a_project_with_the_loop_off_are_as_before(client, hub, hub_db):
    assert srs_ledger.hold and ticked(client, hub_db) == {"off": 1}
    policy(client, hub, {"enabled": True})
    ticked(client, hub_db)
    assert [row[:2] for row in items(hub_db)] == [("brainstorm:G-1:r1", "brainstorm")]
    assert ledger_of(hub_db) == {}


GITHUB_PLAN = "pantry-reorder"
GITHUB_BRANCH = "loop/pantry-reorder"


def merged_pull(github, number: int, at: datetime, sha: str) -> None:
    """Pull request ``number`` of GITHUB_BRANCH of evo-agents, merged at ``at`` as merge commit ``sha``."""
    github.pulls[(credentials_api.MINE, "evo-agents", number)] = FakePull(
        number,
        credentials_api.MINE,
        "evo-agents",
        GITHUB_BRANCH,
        "a" * 40,
        "main",
        state="closed",
        merged=True,
        merge_sha=sha,
        merged_at=at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        merged_by="owner",
    )


def test_loop_tick_a_deliver_whose_plan_run_merged_its_pull_request_on_github_is_done_with_no_person(
    client, hub, hub_db, github
):
    """evo-agents is on GitHub, a repo the queue does not merge. A deliver whose plan run is done waits for a person
    while no pull request of the run's branch merged since the run was queued (one merged before is not the run's); the
    tick that reads the run's pull request merged marks the repo merged in the plan as the run's dispatcher and moves
    the deliver to done in the same tick, and the ticks after it read nothing more."""
    policy(client, hub, {"enabled": True})
    body = {
        "id": GITHUB_PLAN,
        "title": "Reorder from the order page",
        "goal": "Corner shops reorder in one tap.",
        "repos": [{"repo": "evo-agents", "branch": GITHUB_BRANCH}],
        "steps": [{"id": 1, "title": "Reorder button", "repo": "evo-agents", "what": "the button", "status": "done"}],
    }
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{GITHUB_PLAN}", json={"body": body}, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    github.add_repo(credentials_api.MINE, "evo-agents")
    credentials_api.install(github, "evo-agents", permissions={**APP_PERMISSIONS, "pull_requests": "write"})
    now = datetime.now(UTC)
    owner = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == credentials_api.OWNER))[0][0]
    r, i = tables.runs, tables.loop_items
    run = {"project_id": project_id(hub_db), "kind": "plan", "plan_id": GITHUB_PLAN, "title": "Reorder button"}
    run |= {"plan_revision": pushed.json()["revision"]}
    run |= {"dispatched_by": owner, "requested_runtime": "claude-code", "runtime": "claude-code", "mode": "headless"}
    run |= {"approval": "auto", "timeout_s": 3600, "state": "done", "queued_at": now - timedelta(hours=1)}
    run |= {"repos": [{"repo": "evo-agents", "branch": GITHUB_BRANCH}], "finished_at": now}
    run |= {"worker_id": credentials_api.worker_of(client, hub["owner"], "studio")["id"], "leased_at": now}
    run_id = sql(hub_db, insert(r).values(run).returning(r.c.id))[0][0]
    goal = sql(hub_db, select(tables.goals.c.id))[0][0]
    deliver = {"project_id": project_id(hub_db), "kind": "deliver", "cycle": "development", "engine": "rules/2"}
    deliver |= {"key": f"deliver:{GITHUB_PLAN}", "goal_id": goal, "plan_id": GITHUB_PLAN, "run_id": run_id}
    sql(hub_db, insert(i).values(**deliver, state="running"))
    merged_pull(github, 1, now - timedelta(hours=2), "b" * 40)  # an older pull request of the same branch

    def tick_with_app() -> dict:
        state = client.app.state
        return client.portal.call(
            partial(tick, state.engine, project_id(hub_db), dispatch=None, github_app=state.github_app)
        )

    def plan_repo() -> dict:
        shown = client.get(f"/v1/projects/{PROJECT}/plans/{GITHUB_PLAN}", headers=hub["owner"])
        assert shown.status_code == 200, shown.text
        return shown.json()["body"]["repos"][0]

    assert "merged" not in tick_with_app()
    found = item(hub_db, f"deliver:{GITHUB_PLAN}")
    assert (found["state"], found["reason"]) == ("waiting_human", "merge_manual"), "no pull request of the run merged"
    assert "status" not in plan_repo()

    merged_pull(github, 2, now, "c" * 40)
    assert tick_with_app()["merged"] == 1
    found = item(hub_db, f"deliver:{GITHUB_PLAN}")
    assert (found["state"], found["reason"]) == ("done", None)
    repo = plan_repo()
    assert (repo["status"], repo["merged_at"]) == ("merged", now.date().isoformat())
    assert repo["note"].startswith(f"PR {credentials_api.MINE}/evo-agents#2 (ccccccc), merged ")
    assert repo["note"].endswith(f"read on GitHub by the Loop after plan run #{run_id}")
    p = tables.plans
    (updated_by,) = sql(hub_db, select(p.c.updated_by).where(p.c.plan_id == GITHUB_PLAN))[0]
    assert updated_by == owner, "the plan is written as the run's dispatcher"
    assert "merged" not in tick_with_app(), "a deliver done is read no more"

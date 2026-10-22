"""The work items of a project's Loop as its web pages read them (``evo_agents.hub.server.loop``: ``items``, ``chain``;
plan loop-engine, criterion a10), on the hub with Postgres.

GET /v1/projects/{p}/loop/items lists the work items, newest first, of a state, a kind or a goal when asked, each with
the figures of why it waits and the first work item of its chain; GET .../loop/items/{id} gives one with its chain,
the stages of its path from the goal to the release and its ledger; GET .../loop/goals measures each goal against its
target with its work items by state. Each ancestor shows as its own route shows it: a claim under its label, a plan
and its runs when the reader reads that plan. Anyone with a grant reads them; nobody else does. The chain here is
``tests.hub.test_loop_tick``'s: goal G-1 drives the canvas of Northwind Pantry, whose claim CL-1 links use case
UC ORD-1 of the made-up SRS document demo-shop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_ba_agent import members  # noqa: F401 (a fixture)
from tests.hub.test_decisions_page import a_run
from tests.hub.test_loop_admission import push_plan
from tests.hub.test_loop_tick import (  # noqa: F401 (client, hub and loop_core are fixtures)
    GOALS,
    METRIC,
    client,
    hub,
    loop_core,
    policy,
    project_id,
)
from tests.hub.test_runs import PROJECT
from tests.hub.test_strategist_agent import SLUG
from tests.hub.test_value_check import NUMBER_KEY, push

app_key = credentials_api.app_key  # a fixture
LOOP = f"/v1/projects/{PROJECT}/loop"
PLAN = "corner-shop-reorder"


def made(db, kind: str, key: str, state: str = "queued", reason: str | None = None, **values) -> int:
    """A work item of goal G-1, as the engine leaves one."""
    i = tables.loop_items
    goal = sql(db, select(tables.goals.c.id).where(tables.goals.c.number == 1))[0][0]
    cycle = "business" if kind in ("brainstorm", "use_case") else "development"
    row = {"project_id": project_id(db), "kind": kind, "cycle": cycle, "key": key, "goal_id": goal, "state": state}
    row |= {"reason": reason, "engine": "rules/1"}
    if state in ("done", "cancelled"):
        values.setdefault("finished_at", datetime.now(UTC))
    return sql(db, insert(i).values(**row, **values).returning(i.c.id))[0][0]


def noted(db, item_id: int, kind: str, state: str, reason: str | None = None, **values) -> None:
    e = tables.loop_item_events
    sql(db, insert(e).values(item_id=item_id, kind=kind, state=state, reason=reason, **values))


def claim_id(db) -> int:
    return sql(db, select(tables.canvas.claims.c.id).where(tables.canvas.claims.c.number == 1))[0][0]


def document_id(db) -> int:
    d = tables.srs.documents
    return sql(db, select(d.c.id).where(d.c.slug == "demo-shop"))[0][0]


def chain(client, hub, db) -> dict:
    """A use_case of CL-1 done with version 1 of demo-shop, the build of UC ORD-1 it made queued for want of a worker,
    and the deliver of its plan done after its merge request merged; a brainstorm of the goal running."""
    released = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
    use_case = made(
        db,
        "use_case",
        "use_case:northwind-pantry:CL-1:b1",
        "done",
        claim_id=claim_id(db),
        owner="@ba",
        detail={"document": "demo-shop", "version": 1},
        finished_at=released - timedelta(days=2),
    )
    worker = {"worker": "northwind-mini", "why": "no live worker northwind-mini serves the project", "cycle": "old"}
    build = made(
        db,
        "build",
        "build:srs:demo-shop:v1:UC ORD-1",
        "queued",
        "worker",
        claim_id=claim_id(db),
        srs_document_id=document_id(db),
        use_case="UC ORD-1",
        srs_version=1,
        parent_id=use_case,
        detail={**worker, "frame": "srs:demo-shop:v1:UC ORD-1"},
    )
    push_plan(client, hub, PLAN, ("evo-agents",))
    run = a_run(client, db)
    detail = {"after_sha": ["4f2c9a1d"], "released_at": released.isoformat()}
    ancestors = {"claim_id": claim_id(db), "srs_document_id": document_id(db), "use_case": "UC ORD-1", "srs_version": 1}
    ancestors |= {"parent_id": build, "plan_id": PLAN, "run_id": run}
    deliver = made(db, "deliver", f"deliver:{PLAN}", "done", detail=detail, **ancestors)
    noted(db, deliver, "created", "queued")
    noted(db, deliver, "dispatched", "running", run_id=run)
    noted(db, deliver, "done", "done")
    q = tables.merge_queue
    merge = {"project_id": project_id(db), "repo": "evo-agents", "iid": 40, "source": "plan", "plan_id": PLAN}
    merge |= {"target_branch": "main", "source_branch": "evo/reorder", "state": "done", "merged_at": released}
    merge |= {"web_url": "https://gitlab.example.org/northwind/evo-agents/-/merge_requests/40"}
    sql(db, insert(q).values(merge))
    brainstorm = made(db, "brainstorm", "brainstorm:G-1:r1", "running", owner="@strategist")
    return {"use_case": use_case, "build": build, "deliver": deliver, "brainstorm": brainstorm, "run": run}


def test_loop_items_list_work_items_by_state_kind_and_goal_with_their_chain_and_the_figures_they_wait_on(
    client, hub, hub_db
):
    policy(client, hub, {"enabled": True})
    ids = chain(client, hub, hub_db)

    listed = client.get(f"{LOOP}/items", headers=hub["reader"])
    assert listed.status_code == 200, listed.text
    found = listed.json()
    assert found["project"] == PROJECT and found["more"] is False
    order = [ids[name] for name in ("brainstorm", "deliver", "build", "use_case")]
    assert [entry["id"] for entry in found["items"]] == order, "the newest first"
    by_id = {entry["id"]: entry for entry in found["items"]}
    build = by_id[ids["build"]]
    assert (build["kind"], build["cycle"], build["state"], build["reason"]) == (
        "build",
        "development",
        "queued",
        "worker",
    )
    assert build["waiting"] == {"worker": "northwind-mini", "why": "no live worker northwind-mini serves the project"}
    assert (build["goal"], build["claim"], build["task"], build["plan"]) == (
        "G-1",
        {"canvas": SLUG, "code": "CL-1"},
        None,
        None,
    )
    assert build["use_case"] == {"document": "demo-shop", "code": "UC ORD-1", "version": 1}
    assert (build["parent"], build["root"], build["hidden"]) == (ids["use_case"], ids["use_case"], [])
    deliver, use_case = by_id[ids["deliver"]], by_id[ids["use_case"]]
    assert (deliver["root"], deliver["plan"], deliver["run"], deliver["waiting"]) == (
        ids["use_case"],
        PLAN,
        ids["run"],
        {},
    )
    assert use_case["use_case"] == {"document": "demo-shop", "code": None, "version": 1}, "the version it drafted"
    assert by_id[ids["brainstorm"]]["root"] == ids["brainstorm"] and by_id[ids["brainstorm"]]["owner"] == "@strategist"

    def ids_of(query: str) -> list[int]:
        answered = client.get(f"{LOOP}/items?{query}", headers=hub["reader"])
        assert answered.status_code == 200, answered.text
        return [entry["id"] for entry in answered.json()["items"]]

    assert ids_of("state=queued") == [ids["build"]]
    assert ids_of("kind=deliver") == [ids["deliver"]]
    assert ids_of("open=true") == [ids["brainstorm"], ids["build"]]
    assert ids_of("goal=G-1") == order and ids_of("goal=G-2") == []
    limited = client.get(f"{LOOP}/items?limit=1", headers=hub["reader"]).json()
    assert [entry["id"] for entry in limited["items"]] == [ids["brainstorm"]] and limited["more"] is True
    assert client.get(f"{LOOP}/items?state=asleep", headers=hub["reader"]).status_code == 422


def test_loop_items_chain_runs_from_the_goal_to_the_release_with_the_ledger_of_the_work_item(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    ids = chain(client, hub, hub_db)

    shown = client.get(f"{LOOP}/items/{ids['build']}", headers=hub["reader"])
    assert shown.status_code == 200, shown.text
    found = shown.json()
    assert found["item"]["id"] == ids["build"]
    assert [entry["id"] for entry in found["chain"]] == [ids["use_case"], ids["build"], ids["deliver"]]
    assert found["path"] == [ids["use_case"], ids["build"], ids["deliver"]], "on through the only work item below"
    stages = found["stages"]
    assert stages["goal"] == {"code": "G-1", "statement": "Corner shops reorder every week", "status": "active"}
    assert stages["claim"]["canvas"] == SLUG and stages["claim"]["code"] == "CL-1"
    assert stages["use_case"] == {"document": "demo-shop", "code": "UC ORD-1", "version": 1, "state": "draft"}
    assert stages["task"] is None and stages["proposals"] is None
    assert stages["plan"] == {"id": PLAN, "goal": "Corner shops reorder every week."}
    assert [(run["id"], run["kind"], run["work_item"]) for run in stages["runs"]] == [
        (ids["run"], "author", ids["deliver"])
    ]
    (merge,) = stages["merges"]
    assert (merge["repo"], merge["iid"], merge["state"]) == ("evo-agents", 40, "done")
    assert stages["released_at"].startswith("2026-10-20T09:30:00") and stages["after_sha"] == ["4f2c9a1d"]
    assert stages["hidden"] == [] and found["events"] == []

    deliver = client.get(f"{LOOP}/items/{ids['deliver']}", headers=hub["reader"]).json()
    assert [(event["kind"], event["state"], event["run"]) for event in deliver["events"]] == [
        ("created", "queued", None),
        ("dispatched", "running", ids["run"]),
        ("done", "done", None),
    ]
    brainstorm = client.get(f"{LOOP}/items/{ids['brainstorm']}", headers=hub["reader"]).json()
    assert brainstorm["path"] == [ids["brainstorm"]] and brainstorm["stages"]["claim"] is None
    assert client.get(f"{LOOP}/items/999999", headers=hub["reader"]).status_code == 404


def test_loop_items_keep_from_a_reader_what_the_route_of_each_ancestor_keeps(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    ids = chain(client, hub, hub_db)
    secret = {"level": "secret", "location": "any"}
    c, p = tables.canvas.claims, tables.plans
    sql(hub_db, update(c).where(c.c.id == claim_id(hub_db)).values(label=secret))
    sql(hub_db, update(p).where(p.c.plan_id == PLAN).values(label=secret))

    listed = {entry["id"]: entry for entry in client.get(f"{LOOP}/items", headers=hub["reader"]).json()["items"]}
    build, deliver = listed[ids["build"]], listed[ids["deliver"]]
    assert (build["claim"], build["hidden"]) == (None, ["claim"])
    assert (deliver["plan"], deliver["hidden"]) == (None, ["claim", "plan"])
    stages = client.get(f"{LOOP}/items/{ids['build']}", headers=hub["reader"]).json()["stages"]
    assert (stages["claim"], stages["plan"], stages["merges"]) == (None, None, [])
    assert stages["hidden"] == ["claim", "plan", "merges"]
    assert stages["use_case"]["code"] == "UC ORD-1", "the document's label is the reader's"
    i = tables.loop_items
    gone = {"document": "demo-shop-old", "version": 1}
    sql(hub_db, update(i).where(i.c.id == ids["use_case"]).values(detail=gone))
    listed = {entry["id"]: entry for entry in client.get(f"{LOOP}/items", headers=hub["reader"]).json()["items"]}
    assert listed[ids["use_case"]]["use_case"] is None, "a document the project no longer has is no ancestor"
    assert listed[ids["use_case"]]["hidden"] == ["claim"]


def test_loop_goals_measure_each_goal_against_its_target_with_its_work_items_by_state(client, hub, hub_db):
    second = {"statement": "Shops pay within a week", "metric": {**METRIC, "target": 0.2}}
    assert client.put(f"{GOALS}/G-2", json=second, headers=hub["owner"]).status_code == 200
    third = {"statement": "Shops tell a friend"}
    assert client.put(f"{GOALS}/G-3", json=third, headers=hub["owner"]).status_code == 200
    push(client, hub["owner"], {"key": NUMBER_KEY, "value": 0.3})
    made(hub_db, "brainstorm", "brainstorm:G-1:r1", "running")
    made(hub_db, "brainstorm", "brainstorm:G-1:2026-W42", "done")

    found = client.get(f"{LOOP}/goals", headers=hub["reader"])
    assert found.status_code == 200, found.text
    goals = {goal["code"]: goal for goal in found.json()}
    assert list(goals) == ["G-1", "G-2", "G-3"]
    assert (goals["G-1"]["value"], goals["G-1"]["reached"]) == (0.3, False)
    assert (goals["G-2"]["value"], goals["G-2"]["reached"]) == (0.3, True)
    assert (goals["G-3"]["value"], goals["G-3"]["reached"]) == (None, None)
    assert goals["G-1"]["items"] == {
        "queued": 0,
        "running": 1,
        "watching": 0,
        "waiting_human": 0,
        "done": 1,
        "cancelled": 0,
    }
    assert sum(goals["G-2"]["items"].values()) == 0


def test_loop_items_need_a_grant_on_the_project(client, hub, hub_db):
    for path in ("items", "items/1", "goals"):
        assert client.get(f"{LOOP}/{path}", headers=hub["admin"]).status_code == 403, path

"""Admission of the Loop's work items in a tick (``evo_agents.hub.server.loop.admit``, ``caps``), as criterion a5 of
plan loop-engine names it, on the hub with Postgres.

Each tick checks the queued work items, oldest first, against the breaker, the work items of their cycle running, the
repos a deliver's plan would hold, the runs handed out in the last hour and worker loop.worker; the first check that
fails is the work item's reason and it stays queued, written once while it holds. One that passes goes to the
hand-out, which the plan's step 6 writes; the hand-out here is a stand-in that hands a task run to the agent type
through ``hand_own_task`` and inserts an author or a plan run with the caps of ``loop.caps``. A run handed out moves
the work item to running with a ledger row ``dispatched`` and counts against the work items after it. A task run is
capped by its agent type's daily_usd and refused once that is spent. The chain is ``tests.hub.test_loop_tick``'s:
goal G-1 drives the canvas of Northwind Pantry, whose claim CL-1 links use case UC ORD-1; the plans are made up.
"""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.loop.admit import Handed, open_repos
from evo_agents.hub.server.loop.caps import AGENT_OF, hand_task, run_budget, timeout_s
from evo_agents.hub.server.loop.tick import tick
from evo_agents.hub.server.runs.service.dispatch import insert_run
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_ba_agent import members  # noqa: F401
from tests.hub.test_loop_tick import (  # noqa: F401
    client,
    counts,
    hub,
    item,
    ledger,
    loop_core,
    policy,
    project_id,
    push_baseline,
)
from tests.hub.test_runs import PROJECT, add_worker
from tests.hub.test_strategist_agent import CARD, OWNER, SLUG, edit
from tests.hub.test_strategist_agent import TYPE as STRATEGIST_TYPE

app_key = credentials_api.app_key  # a fixture
REPO, NOTES = "evo-agents", "notes"
CASE = f"use_case:{SLUG}:CL-1:b1"


def ticked(client, db, dispatch=None) -> dict:
    """One tick of the project's Loop handing out runs with ``dispatch``."""
    return client.portal.call(partial(tick, client.app.state.engine, project_id(db), dispatch=dispatch))


async def _loop_run(conn, project_id: int, work, view, worker) -> int:
    """The author run of a build or the plan run of a deliver, as step 6 will hand it out: on worker loop.worker in
    its owner's name, with the caps of loop.caps."""
    values = {
        "kind": "plan" if work.kind == "deliver" else "author",
        "project_id": project_id,
        "title": f"Loop {work.key}",
        "dispatched_by": worker.owner_id,
        "dispatched_via": "machine",
        "pinned_worker_id": worker.id,
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": timeout_s(work.kind, view.policy),
        "budget": run_budget(work.kind, view.policy),
    }
    if work.kind == "deliver":
        p = tables.plans
        query = select(p.c.revision, p.c.body).where(p.c.project_id == project_id, p.c.plan_id == work.plan_id)
        revision, body = (await conn.execute(query)).one()
        repos = [{"repo": name} for name in open_repos(body)]
        values |= {"plan_id": work.plan_id, "plan_revision": revision, "repos": repos}
    else:
        values |= {"repos": [{"repo": REPO}], "request": f"Write the plan of {work.key}"}
    return await insert_run(conn, values)


def hand_out(*kinds: str):
    """A stand-in for step 6's hand-out of ``kinds``: nothing for the others."""

    async def dispatch(conn, project_id, work, view, worker):
        if work.kind not in kinds:
            return None
        if work.kind in AGENT_OF:
            return await hand_task(conn, project_id, work.kind, f"Work on {work.key}", work.key)
        return Handed(run_id=await _loop_run(conn, project_id, work, view, worker), owner=OWNER)

    return dispatch


def enable_strategist(client, hub, daily_usd: float = 5) -> None:
    typed = client.put(STRATEGIST_TYPE, json={"run_as": OWNER, "daily_usd": daily_usd}, headers=hub["owner"])
    assert typed.status_code == 200, typed.text


def waits(db, key: str) -> list[tuple]:
    """(kind, reason, detail, run) of each row of the ledger of work item ``key``."""
    i, e = tables.loop_items, tables.loop_item_events
    query = select(e.c.kind, e.c.reason, e.c.detail, e.c.run_id).join_from(e, i, i.c.id == e.c.item_id)
    return sql(db, query.where(i.c.key == key).order_by(e.c.id))


def run_row(db, run_id: int) -> tuple:
    r = tables.runs
    query = select(r.c.kind, r.c.budget, r.c.timeout_s, r.c.pinned_worker_id, r.c.dispatched_by, r.c.repos, r.c.state)
    return sql(db, query.where(r.c.id == run_id))[0]


def set_run(db, run_id: int, **values) -> None:
    sql(db, update(tables.runs).where(tables.runs.c.id == run_id).values(**values))


def queued(db, kind: str, key: str, **values) -> None:
    """A work item of the development cycle queued for goal G-1, as the engine leaves one."""
    goal = sql(db, select(tables.goals.c.id))[0][0]
    row = {"project_id": project_id(db), "kind": kind, "cycle": "development", "key": key, "goal_id": goal}
    sql(db, insert(tables.loop_items).values(**row, engine="rules/1", **values))


def push_plan(client, hub, plan_id: str, repos: tuple[str, ...]) -> None:
    steps = [
        {"id": number, "title": f"Change {repo}", "repo": repo, "what": f"the change in {repo}", "status": "pending"}
        for number, repo in enumerate(repos, start=1)
    ]
    body = {"id": plan_id, "goal": "Corner shops reorder every week.", "repos": [{"repo": REPO}], "steps": steps}
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{plan_id}", json={"body": body}, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text


def test_loop_admission_holds_business_work_items_at_the_cycle_cap_the_rate_the_type_and_the_breaker(
    client, hub, hub_db
):
    enable_strategist(client, hub)
    loop = {"enabled": True, "business": {"max_running": 1}, "dispatches_per_hour": 1}
    policy(client, hub, loop)
    dispatch = hand_out("brainstorm", "use_case")
    # the goal's brainstorm passes every check: a task run of @strategist within its type's daily cap
    assert ticked(client, hub_db, dispatch)["dispatched"] == 1
    brainstorm = item(hub_db, "brainstorm:G-1:r1")
    assert (brainstorm["state"], brainstorm["reason"], brainstorm["attempts"]) == ("running", None, 1)
    kind, budget, *_ = run_row(hub_db, brainstorm["run_id"])
    assert (kind, budget) == ("task", {"max_usd": 5.0, "max_seconds": 1800})
    assert [(row[0], row[3]) for row in waits(hub_db, "brainstorm:G-1:r1")] == [
        ("created", None),
        ("dispatched", brainstorm["run_id"]),
    ]

    # a use case comes while the business cycle is full: it waits, its reason written once
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    found = ticked(client, hub_db, dispatch)
    assert (found["changed"], found["waiting"], found.get("dispatched")) == (1, 1, None)
    assert (item(hub_db, CASE)["state"], item(hub_db, CASE)["reason"]) == ("queued", "concurrency")
    assert waits(hub_db, CASE)[-1][:3] == (
        "waiting",
        "concurrency",
        {"cycle": "business", "running": 1, "max_running": 1},
    )
    before = counts(hub_db)
    assert ticked(client, hub_db, dispatch) == {"events": 0, "changed": 0}
    assert counts(hub_db) == before

    # room in the cycle: the hour's one hand-out holds it
    loop["business"] = {"max_running": 2}
    policy(client, hub, loop)
    assert ticked(client, hub_db, dispatch)["waiting"] == 1
    assert waits(hub_db, CASE)[-1][:3] == ("waiting", "rate", {"dispatched": 1, "dispatches_per_hour": 1})

    # room in the hour: @ba, not enabled, takes no task, and the use case says why
    loop["dispatches_per_hour"] = 6
    policy(client, hub, loop)
    ticked(client, hub_db, dispatch)
    case = item(hub_db, CASE)
    assert (case["state"], case["reason"], case["run_id"], case["attempts"]) == ("queued", "type_refused", None, 0)
    why = waits(hub_db, CASE)[-1][2]["why"]
    assert why.startswith("agent type ba of plugin srs is not enabled"), why

    # the breaker tripped: everything queued holds, the run in flight goes on
    s = tables.loop_states
    sql(hub_db, update(s).values(breaker="tripped", breaker_reason="too many work items", tripped_at=func.now()))
    found = ticked(client, hub_db, dispatch)
    assert (found["breaker"], found["waiting"]) == (1, 1)
    assert item(hub_db, CASE)["reason"] == "breaker" and item(hub_db, "brainstorm:G-1:r1")["state"] == "running"


def test_loop_admission_holds_builds_and_delivers_for_the_worker_and_the_repos_their_plans_hold(client, hub, hub_db):
    push_plan(client, hub, "pantry-reorder", (REPO,))
    push_plan(client, hub, "pantry-alerts", (REPO, NOTES))
    policy(client, hub, {"enabled": True, "worker": "studio", "development": {"max_running": 2}})
    queued(hub_db, "deliver", "deliver:pantry-reorder", plan_id="pantry-reorder")
    queued(hub_db, "deliver", "deliver:pantry-alerts", plan_id="pantry-alerts")
    queued(hub_db, "build", "build:srs:demo-shop:1:UC ORD-2")
    build = "build:srs:demo-shop:1:UC ORD-2"
    dispatch = hand_out("build", "deliver")

    # no worker studio serves the project: nothing of the development cycle runs
    assert ticked(client, hub_db, dispatch)["waiting"] == 3
    for key in ("deliver:pantry-reorder", "deliver:pantry-alerts", build):
        assert waits(hub_db, key) == [
            (
                "waiting",
                "worker",
                {"worker": "studio", "why": "no live worker studio (the policy's loop.worker) serves the project"},
                None,
            )
        ]
    assert item(hub_db, "brainstorm:G-1:r1")["reason"] is None  # a task run needs no loop.worker

    # studio, one slot: the first deliver's plan run takes it and holds repo evo-agents
    worker = add_worker(client, hub["owner"], "studio", slots=1)
    found = ticked(client, hub_db, dispatch)
    assert (found["dispatched"], found["waiting"]) == (1, 1)  # the build's reason stays worker: no write
    first = item(hub_db, "deliver:pantry-reorder")
    assert (first["state"], first["owner"], first["attempts"]) == ("running", OWNER, 1)
    kind, budget, timeout, pinned, _, repos, _ = run_row(hub_db, first["run_id"])
    assert (kind, budget, timeout) == ("plan", {"max_usd": None, "max_turns": 600, "max_seconds": 14400}, 14700)
    assert (pinned, repos) == (worker["id"], [{"repo": REPO}])
    assert waits(hub_db, "deliver:pantry-alerts")[-1][:3] == (
        "waiting",
        "concurrency",
        {"repos": [REPO], "repo_max_running": 1},
    )
    assert item(hub_db, build)["reason"] == "worker"

    # the plan run done, its merge request not merged still holds the repo; the slot frees for the build
    ran = {"worker_id": worker["id"], "leased_at": func.now(), "finished_at": func.now()}
    set_run(hub_db, first["run_id"], state="done", **ran)
    i = tables.loop_items
    sql(hub_db, update(i).where(i.c.id == first["id"]).values(state="watching", reason="merge_queue"))
    merge = {"project_id": project_id(hub_db), "repo": REPO, "source": "plan", "target_branch": "main"}
    merge |= {"source_branch": "loop/pantry-reorder", "plan_id": "pantry-reorder", "state": "ready"}
    sql(hub_db, insert(tables.merge_queue).values(merge))
    assert ticked(client, hub_db, dispatch)["dispatched"] == 1
    built = item(hub_db, build)
    kind, budget, timeout, pinned, dispatched_by, *_ = run_row(hub_db, built["run_id"])
    assert (kind, budget, timeout) == ("author", {"max_usd": None, "max_turns": 200, "max_seconds": 3600}, 3900)
    assert (built["state"], pinned) == ("running", worker["id"])
    assert item(hub_db, "deliver:pantry-alerts")["reason"] == "concurrency"

    # merged: the first deliver is done and frees the repo; the build's run waiting for studio holds its slot
    q = tables.merge_queue
    sql(hub_db, update(q).values(state="merged", merged_at=func.now()))
    found = ticked(client, hub_db, dispatch)
    assert item(hub_db, "deliver:pantry-reorder")["state"] == "done"
    assert waits(hub_db, "deliver:pantry-alerts")[-1][:3] == (
        "waiting",
        "worker",
        {"worker": "studio", "why": "worker studio has no free slot"},
    )

    # the build's run cancelled by a person: the slot is free and the second deliver's plan run takes it
    set_run(hub_db, built["run_id"], state="cancelled", finished_at=func.now())
    found = ticked(client, hub_db, dispatch)
    assert found["dispatched"] == 1
    assert (item(hub_db, build)["state"], item(hub_db, build)["reason"]) == ("cancelled", "person")
    second = item(hub_db, "deliver:pantry-alerts")
    assert second["state"] == "running" and run_row(hub_db, second["run_id"])[5] == [{"repo": REPO}, {"repo": NOTES}]
    assert [row[:2] for row in waits(hub_db, "deliver:pantry-alerts")] == [
        ("waiting", "worker"),
        ("waiting", "concurrency"),
        ("waiting", "worker"),
        ("dispatched", None),
    ]


def test_loop_caps_a_task_run_the_loop_hands_out_keeps_the_daily_cap_of_its_agent_type(client, hub, hub_db):
    enable_strategist(client, hub)
    pid = project_id(hub_db)

    async def hand(kind: str = "brainstorm") -> Handed:
        async with client.app.state.engine.begin() as conn:
            return await hand_task(conn, pid, kind, "Brainstorm assumptions for goal G-1", "Loop brainstorm of G-1")

    first = client.portal.call(hand)
    assert first.reason is None and run_row(hub_db, first.run_id)[1] == {"max_usd": 5.0, "max_seconds": 1800}
    set_run(hub_db, first.run_id, usage={"total_cost_usd": 4.0})
    second = client.portal.call(hand)
    assert run_row(hub_db, second.run_id)[1] == {"max_usd": 1.0, "max_seconds": 1800}  # what is left of the day
    set_run(hub_db, second.run_id, usage={"total_cost_usd": 1.5})
    spent = client.portal.call(hand)
    assert (spent.run_id, spent.reason) == (None, "type_refused")
    assert "reached its cap of $5.00" in spent.why
    refused = client.portal.call(partial(hand, "use_case"))
    assert (refused.run_id, refused.reason) == (None, "type_refused")
    assert refused.why.startswith("agent type ba of plugin srs is not enabled")


def test_loop_a_deliver_whose_held_merge_request_a_person_merged_on_gitlab_is_done(client, hub, hub_db):
    """The queue closed the merge request of the deliver's plan, which a person merged by hand on GitLab while it was
    held at gate 3: the ledger's line closed says so, and the deliver is done with the merge GitLab named."""
    push_plan(client, hub, "pantry-reorder", (REPO,))
    policy(client, hub, {"enabled": True})
    pid, q, e = project_id(hub_db), tables.merge_queue, tables.merge_events
    merge = {"project_id": pid, "repo": REPO, "iid": 9, "source": "plan", "target_branch": "main"}
    merge |= {"source_branch": "loop/pantry-reorder", "plan_id": "pantry-reorder", "state": "blocked"}
    merge |= {"reason": "gate 3 (score hacking): the diff shows signs of score hacking (ci_changed)"}
    ((merge_id,),) = sql(hub_db, insert(q).values(merge).returning(q.c.id))
    detail = {"merges": [merge_id]}
    queued(hub_db, "deliver", "deliver:pantry-reorder", plan_id="pantry-reorder")
    i = tables.loop_items
    held = update(i).where(i.c.key == "deliver:pantry-reorder")
    sql(hub_db, held.values(state="waiting_human", reason="merge_blocked", detail=detail))
    ticked(client, hub_db)
    assert item(hub_db, "deliver:pantry-reorder")["reason"] == "merge_blocked"

    gone = {"state": "closed", "reason": "it left the queue: it is merged on GitLab"}
    sql(hub_db, update(q).where(q.c.id == merge_id).values(gone))
    line = {"project_id": pid, "queue_id": merge_id, "kind": "closed", "reason": gone["reason"]}
    sql(hub_db, insert(e).values({**line, "detail": {"gitlab": "merged", "merge_commit_sha": "9c1d2e3f"}}))
    ticked(client, hub_db)
    deliver = item(hub_db, "deliver:pantry-reorder")
    assert (deliver["state"], deliver["detail"]["after_sha"]) == ("done", ["9c1d2e3f"])

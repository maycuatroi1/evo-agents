"""The hand-out of the Loop (``evo_agents.hub.server.loop.dispatch``, ``texts``, ``author_mode``, ``runs_of``), as
criterion a6 of plan loop-engine names it, on the hub with Postgres.

Each kind of work item goes to a run kind the hub has: a brainstorm to a task run of @strategist, a use case to a task
run of @ba whose version ``srs__propose`` writes as the goal's owner, a build to an author run of its frame task in
Loop mode on worker loop.worker in its owner's name (none while no project of the owner's lists an origin for the
harness: the worker's preflight would fail it), and a deliver to a plan run of the plan that author run wrote.
The Loop makes no task: an approval leaves only the tasks of the plugin srs's auto_tasks, however often it is read. A
claim validated and a test card past its deadline make nothing of the Loop. The chain is ``tests.hub.test_loop_tick``'s:
goal G-1 of owner drives the canvas of Northwind Pantry, whose claim CL-1 links use case UC ORD-1 of demo-shop.
"""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub import hub_events, tables
from evo_agents.hub.server.loop import texts
from evo_agents.hub.server.loop.model import ItemView
from evo_agents.hub.server.loop.tick import tick
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_author_plans import put
from tests.hub.test_author_runs import HARNESS, author_worker, publish_skill
from tests.hub.test_ba_agent import (  # noqa: F401 (members is a fixture)
    OTHER,
    agent_of,
    delivered,
    enable_ba,
    members,
    review,
    running,
    submitted,
    tool,
    version,
)
from tests.hub.test_ba_agent import item as srs_item
from tests.hub.test_chat_runs import chat_worker
from tests.hub.test_loop_admission import enable_strategist, waits
from tests.hub.test_loop_tick import (  # noqa: F401 (client, hub and loop_core are fixtures)
    FRAME,
    POLICY,
    client,
    harness_listed,
    hub,
    item,
    items,
    ledger,
    loop_core,
    policy,
    project_id,
    push_baseline,
)
from tests.hub.test_plugin_runtimes import set_plugins
from tests.hub.test_runs import PROJECT, add_worker, audit_rows, claim, moved
from tests.hub.test_strategist_agent import CARD, OWNER, SLUG, edit
from tests.hub.test_value_check import NUMBER_KEY

app_key = credentials_api.app_key  # a fixture
TASKS = f"/v1/x/tasks/{PROJECT}"
PLAN = "pantry-reorder"
NEW_CASE = "UC ORD-4"


def ticked(client, db) -> dict:
    """One tick of the project's Loop with its hand-out, as the worker runs hub.loop_tick for it."""
    return client.portal.call(partial(tick, client.app.state.engine, project_id(db)))


def run_of(db, run_id: int) -> dict:
    r, u, t = tables.runs, tables.users, tables.project_agent_types
    query = (
        select(r, u.c.login.label("owner"), t.c.name.label("agent_type"))
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .outerjoin(t, t.c.id == r.c.agent_type_id)
        .where(r.c.id == run_id)
    )
    (row,) = sql(db, query)
    return dict(zip([*r.columns.keys(), "owner", "agent_type"], row, strict=True))


def first_event(db, run_id: int) -> str:
    e = tables.run_events
    query = select(e.c.body["text"].astext).where(e.c.run_id == run_id).order_by(e.c.seq).limit(1)
    return sql(db, query)[0][0]


def tasks(db) -> list[tuple]:
    t = tables.tasks.items
    return sql(db, select(t.c.number, t.c.external_key, t.c.parent_id).order_by(t.c.number))


def done(db, run_id: int, worker: dict, state: str = "done") -> None:
    """Run ``run_id`` ended ``state`` on ``worker``, as its reports would leave it."""
    r = tables.runs
    ended = {"worker_id": worker["id"], "runtime": "claude-code", "leased_at": func.now(), "finished_at": func.now()}
    sql(db, update(r).where(r.c.id == run_id).values(state=state, **ended))


def plan_body() -> dict:
    return {
        "id": PLAN,
        "title": "Reorder from the order page",
        "goal": "Corner shops reorder in one tap.",
        "repos": [{"repo": "evo-agents", "branch": "loop/pantry-reorder"}],
        "steps": [
            {"id": 1, "title": "Reorder button", "repo": "evo-agents", "what": "the button", "status": "pending"},
        ],
    }


def said(client, worker: dict, run_id: int, text: str) -> None:
    response = client.post(f"/v1/worker/runs/{run_id}/chat", json={"text": text}, headers=worker["headers"])
    assert response.status_code == 201, response.text


def author_notices(db) -> int:
    n = tables.notifications
    return sql(db, select(func.count()).where(n.c.notice_kind == "author_waiting"))[0][0]


def frame_ready(client, hub, db, tmp_path) -> tuple[dict, int, int]:
    """A Loop that is on with worker studio of owner as loop.worker, and the frame task of UC ORD-1, with a child
    task, ready for its build: (studio, the frame task's id, its number)."""
    publish_skill(client, hub, tmp_path, "The author mode.")
    studio = author_worker(client, hub["owner"], "studio")
    policy(client, hub, {"enabled": True, "worker": "studio"})
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    ticked(client, db)  # @strategist and @ba are off: their work items wait (type_refused)
    submitted(client, hub["chi"], 1)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    ticked(client, db)
    delivered(client)  # three frame tasks, no split run: @ba is off
    t = tables.tasks.items
    ((frame_id, frame),) = sql(db, select(t.c.id, t.c.number).where(t.c.external_key == FRAME))
    child = client.post(
        f"{TASKS}/tasks", json={"title": "Reorder button on the order page", "parent": f"T-{frame}"}, headers=hub["chi"]
    )
    assert child.status_code == 201, child.text
    return studio, frame_id, frame


def test_loop_dispatch_hands_a_brainstorm_to_strategist_and_a_use_case_to_ba_whose_version_the_goal_owner_writes(
    client, hub, hub_db
):
    submitted(client, hub["chi"], 1)  # version 1 approved before the Loop is on: its tasks are auto_tasks's alone
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    delivered(client)
    assert len(tasks(hub_db)) == 3
    enable_strategist(client, hub)
    enable_ba(client, hub, run_as=OTHER)
    studio = add_worker(client, hub["owner"], "studio")
    policy(client, hub, {"enabled": True})

    # a brainstorm: a task run of @strategist, its task the goal, its metric and the canvas it drives
    assert ticked(client, hub_db)["dispatched"] == 1
    brainstorm = item(hub_db, "brainstorm:G-1:r1")
    run = run_of(hub_db, brainstorm["run_id"])
    assert (run["kind"], run["agent_type"], run["owner"], brainstorm["state"]) == (
        "task",
        "strategist",
        OWNER,
        "running",
    )
    for said_ in ("goal G-1", "Corner shops reorder every week", NUMBER_KEY, f"the Lean Canvas {SLUG}"):
        assert said_ in run["request"], run["request"]
    assert (
        first_event(hub_db, run["id"])
        == f"Queued by the Loop of project {PROJECT} for work item #{brainstorm['id']} (brainstorm:G-1:r1)."
    )
    done(hub_db, run["id"], studio)
    ticked(client, hub_db)
    brainstorm = item(hub_db, "brainstorm:G-1:r1")
    assert (brainstorm["state"], brainstorm["detail"]["proposals"]) == ("done", 0)

    # a claim in testing: a task run of @ba, run as someone-else, its task the claim, its card and the goal
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    assert ticked(client, hub_db)["dispatched"] == 1
    case = item(hub_db, f"use_case:{SLUG}:CL-1:b1")
    run = run_of(hub_db, case["run_id"])
    assert (run["kind"], run["agent_type"], run["owner"], case["owner"]) == ("task", "ba", OTHER, "@ba")
    assert (
        f"claim CL-1 of canvas {SLUG}" in run["request"] and f"written as {OWNER}, the goal's owner" in run["request"]
    )
    assert "Corner shops reorder weekly" in run["request"], "the claim's test card"

    # its agent proposes version 2 with a new use case: written as the goal's owner, drafted by the run
    lumen = chat_worker(client, hub["other"], "lumen-box", slots=3)
    assert set_plugins(client, hub["other"], lumen, ["srs"]).status_code == 200
    spec = running(client, lumen, "task")
    assert spec["id"] == case["run_id"]
    order = srs_item(client, hub["chi"], "UC ORD-1")
    body = "| Item | Content |\n|---|---|\n| Actor | Shop owner |\n| Trigger | The shop owner presses Reorder |"
    new = {"kind": "use_case", "code": NEW_CASE, "title": "Reorder", "parent": order["parent"], "body": body}
    text, error = tool(
        client, agent_of(lumen, spec["id"]), "srs__propose", document="demo-shop", base_version=1,
        summary="Reorder for CL-1", changes=[{"new": new}],
    )  # fmt: skip
    assert not error, text
    assert f"drafted by run #{spec['id']} and written as {OWNER}, who therefore does not approve it" in text, text
    proposed = version(client, hub["reader"], 2)
    assert (proposed["state"], proposed["author"], proposed["drafted_by_run"]) == ("in_review", OWNER, spec["id"])
    moved(client, lumen, spec["id"], "verifying", "done", summary="Version 2 in review.")

    # its run done, the use case waits for the review of its version: a person's turn, no failure
    ticked(client, hub_db)
    case = item(hub_db, f"use_case:{SLUG}:CL-1:b1")
    assert (case["state"], case["reason"], case["failures"]) == ("waiting_human", "srs_review", 0)
    assert case["detail"]["srs"] == {"document": "demo-shop", "version": 2}

    # a task run the Loop did not hand out proposes nothing: @ba's review of version 2
    review_run = running(client, lumen, "task")
    assert "\nReview version 2 of" in review_run["prompt"], review_run["prompt"]
    text, error = tool(
        client, agent_of(lumen, review_run["id"]), "srs__propose", document="demo-shop", base_version=2,
        summary="x", changes=[{"key": order["key"], "title": "Place an order now"}],
    )  # fmt: skip
    assert error and "or of a task run of @ba the Loop of project" in text, text
    moved(client, lumen, review_run["id"], "verifying", "done", summary="Nothing found.")

    # approved: the use case done, a build for the use case it added, and only the plugin srs's frame task
    assert review(client, hub["owner"], 2).status_code == 403, "the goal's owner wrote it"
    assert review(client, hub["chi"], 2).json()["state"] == "approved"
    ticked(client, hub_db)
    case = item(hub_db, f"use_case:{SLUG}:CL-1:b1")
    assert case["state"] == "done"
    build = item(hub_db, "build:srs:demo-shop:2:UC ORD-4")
    assert (build["state"], build["reason"], build["parent_id"]) == ("watching", "auto_tasks", case["id"])
    delivered(client)
    ticked(client, hub_db)
    assert [row[1] for row in tasks(hub_db)][3:] == ["srs:demo-shop:2:UC ORD-4"], "one frame task, auto_tasks's"
    build = item(hub_db, "build:srs:demo-shop:2:UC ORD-4")
    assert (build["state"], build["reason"]) == ("watching", "split"), "@ba splits the frame task first"

    # the chain read again, and the approval told twice, make no task and no work item
    chain = (len(tasks(hub_db)), len(items(hub_db)), len(ledger(hub_db)))
    s, e = tables.loop_states, tables.hub_events
    sql(hub_db, update(s).values(cursor=s.c.cursor - 3))
    ((payload,),) = sql(
        hub_db, select(e.c.payload).where(e.c.kind == hub_events.SRS_APPROVED).order_by(e.c.id.desc()).limit(1)
    )

    async def told_again():
        async with client.app.state.engine.begin() as conn:
            await hub_events.record(conn, hub_events.SRS_APPROVED, project_id(hub_db), payload)

    client.portal.call(told_again)
    delivered(client)
    ticked(client, hub_db)
    assert (len(tasks(hub_db)), len(items(hub_db)), len(ledger(hub_db))) == chain
    d = tables.decisions
    assert sql(hub_db, select(func.count()).where(d.c.category == "loop"))[0][0] == 0


def test_loop_dispatch_a_frame_task_gets_one_author_run_in_loop_mode_then_a_plan_run_of_its_plan(
    client, hub, hub_db, tmp_path
):
    harness_listed(client, hub)
    studio, frame_id, frame = frame_ready(client, hub, hub_db, tmp_path)

    # the frame task ready: one author run, on studio in its owner's name, with the caps of loop.caps.author
    assert ticked(client, hub_db)["dispatched"] == 1
    build = item(hub_db, f"build:{FRAME}")
    assert (build["state"], build["owner"], build["attempts"], build["task_id"]) == ("running", OWNER, 1, frame_id)
    run = run_of(hub_db, build["run_id"])
    assert (run["kind"], run["owner"], run["pinned_worker_id"], run["dispatched_via"]) == (
        "author",
        OWNER,
        studio["id"],
        "machine",
    )
    assert (run["budget"], run["timeout_s"]) == ({"max_usd": None, "max_turns": 200, "max_seconds": 3600}, 3900)
    assert [entry["repo"] for entry in run["repos"]] == ["evo-agents-harness", "evo-agents"]
    for said_ in (f"frame task #{frame}", "UC ORD-1", "Place an order", "Child tasks, each a step:", "Reorder button"):
        assert said_ in run["request"], run["request"]
    assert f"Goal G-1 of {OWNER}" in run["request"] and "Claim CL-1 of canvas northwind-pantry" in run["request"]
    k = tables.tasks.links
    assert sql(hub_db, select(k.c.kind, k.c.run_id).where(k.c.task_id == frame_id, k.c.kind == "run")) == [
        ("run", run["id"])
    ]
    assert audit_rows(hub_db, "loop") == [
        ("loop.dispatch", f"{PROJECT} item:{build['id']} run:{run['id']} kind=build", OWNER, PROJECT)
    ]

    # its prompt is Loop mode; a turn that ends with a question waits for a person, who is told
    spec = claim(client, studio)
    assert spec["id"] == run["id"]
    assert "This author run is the Loop's" in spec["prompt"] and "# The request of the Loop" in spec["prompt"]
    moved(client, studio, run["id"], "running")
    said(client, studio, run["id"], "Should the button reorder the last order, or let the shop pick lines?")
    assert moved(client, studio, run["id"], "waiting")["finish_requested_at"] is None
    assert author_notices(hub_db) == 1
    ticked(client, hub_db)
    assert (item(hub_db, f"build:{FRAME}")["state"], item(hub_db, f"build:{FRAME}")["reason"]) == (
        "waiting_human",
        "author_question",
    )
    answered = client.post(
        f"/v1/projects/{PROJECT}/runs/{run['id']}/messages", json={"text": "The last order."}, headers=hub["owner"]
    )
    assert answered.status_code == 201, answered.text
    moved(client, studio, run["id"], "running")
    ticked(client, hub_db)
    assert item(hub_db, f"build:{FRAME}")["state"] == "running"

    # the plan on the hub and a turn that asks nothing: the hub ends the run, and tells nobody
    written = put(client, studio, run["id"], plan_body())
    assert written.status_code == 200, written.text
    said(client, studio, run["id"], f"Put plan {PLAN} at revision 1.")
    assert moved(client, studio, run["id"], "waiting")["finish_requested_at"] is not None
    assert author_notices(hub_db) == 1
    e = tables.run_events
    texts = [text for (text,) in sql(hub_db, select(e.c.body["text"].astext).where(e.c.run_id == run["id"]))]
    assert f"The Loop ends this author run: plan {PLAN} is on the hub and the turn left no question open." in texts
    moved(client, studio, run["id"], "running", "verifying", "done", summary=f"Wrote plan {PLAN}.")

    # written and ended: the build done, and a plan run of its plan for the deliver, in the same name
    assert ticked(client, hub_db)["dispatched"] == 1
    assert item(hub_db, f"build:{FRAME}")["state"] == "done"
    deliver = item(hub_db, f"deliver:{PLAN}")
    plan_run = run_of(hub_db, deliver["run_id"])
    assert (deliver["state"], deliver["plan_id"], deliver["task_id"]) == ("running", PLAN, frame_id)
    assert (plan_run["kind"], plan_run["plan_id"], plan_run["owner"], plan_run["pinned_worker_id"]) == (
        "plan",
        PLAN,
        OWNER,
        studio["id"],
    )
    assert plan_run["repos"] == [{"repo": "evo-agents", "branch": "loop/pantry-reorder"}]
    assert plan_run["budget"] == {"max_usd": None, "max_turns": 600, "max_seconds": 14400}
    links = sql(hub_db, select(k.c.run_id).where(k.c.task_id == frame_id, k.c.kind == "run").order_by(k.c.id))
    assert [run_id for (run_id,) in links] == [run["id"], plan_run["id"]]


def test_loop_dispatch_hands_out_no_author_run_while_no_project_lists_an_origin_for_the_harness(
    client, hub, hub_db, tmp_path
):
    frame_ready(client, hub, hub_db, tmp_path)  # the project lists the harness as no repo, so with no origin
    assert ticked(client, hub_db).get("dispatched") is None
    build = item(hub_db, f"build:{FRAME}")
    assert (build["state"], build["reason"], build["run_id"], build["attempts"]) == ("queued", "worker", None, 0)
    kind, reason, detail, run_id = waits(hub_db, f"build:{FRAME}")[-1]
    assert (kind, reason, run_id) == ("waiting", "worker", None)
    assert detail == {
        "why": f"the author run needs the harness {HARNESS}, for which neither project {PROJECT} nor another project "
        "of the owner of worker studio lists an origin"
    }
    r = tables.runs
    assert sql(hub_db, select(func.count()).where(r.c.kind == "author")) == [(0,)], "nothing for the preflight to fail"

    # once the project lists the harness with its origin, the next tick hands the author run out
    harness_listed(client, hub)
    assert ticked(client, hub_db)["dispatched"] == 1
    build = item(hub_db, f"build:{FRAME}")
    assert (build["state"], build["reason"], build["attempts"]) == ("running", None, 1)
    run = run_of(hub_db, build["run_id"])
    assert (run["kind"], run["repos"][0]["repo"]) == ("author", HARNESS)


def test_loop_dispatch_a_claim_validated_makes_no_build_and_a_card_past_its_deadline_no_decision(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    ticked(client, hub_db)
    c = tables.canvas.claims
    past = literal({"deadline": "2026-10-01"}, JSONB)
    sql(hub_db, update(c).values(test_card=c.c.test_card.op("||")(past)))  # its test card past its deadline
    ((claim_id,),) = sql(hub_db, select(c.c.id).where(c.c.number == 1))
    ((canvas_id,),) = sql(hub_db, select(tables.canvas.canvases.c.id))
    validated = {
        "v": 1,
        "canvas": SLUG,
        "canvas_id": canvas_id,
        "baseline_id": 1,
        "baseline": 2,
        "transitions": [{"claim_id": claim_id, "claim": "CL-1", "from": "testing", "to": "validated", "evidence": []}],
    }

    async def baseline():
        async with client.app.state.engine.begin() as conn:
            await hub_events.record(conn, hub_events.BASELINE, project_id(hub_db), validated)

    client.portal.call(baseline)
    before = [row[:2] for row in items(hub_db)]
    ticked(client, hub_db)
    assert [row[:2] for row in items(hub_db)] == before, "no build, no work item"
    assert [row for row in ledger(hub_db) if row[1] == "noted"] == [
        (f"use_case:{SLUG}:CL-1:b1", "noted", "queued", "type_refused")
    ]
    d = tables.decisions
    assert sql(hub_db, select(func.count()).where(d.c.category == "loop"))[0][0] == 0
    assert tasks(hub_db) == []


def test_loop_dispatch_the_author_request_says_what_the_merge_queue_asks_of_the_plan(client, hub, hub_db):
    """A repo of the project in merge mode auto: the author run is told that the queue opens the merge request, runs
    its pipeline and merges it, that each step ends at a pushed branch with its local tests passing, and that a change
    of CI configuration waits at gate 3 for a person. A project whose queue merges nothing is told nothing of it."""
    work = ItemView(41, "build", "build:srs:demo-shop:1:UC ORD-9", "queued", None, None)
    pid = project_id(hub_db)

    async def request() -> str:
        async with client.app.state.engine.begin() as conn:
            return await texts.author_request(conn, pid, PROJECT, work)

    assert "merge queue" not in client.portal.call(request)
    body = {"policy": {"repos": {"lumen-kb-docs": {"mode": "auto", "target_branch": "dev"}}}}
    assert client.put(POLICY, json=body, headers=hub["owner"]).status_code == 200
    said = client.portal.call(request)
    assert "\n".join(texts.merge_queue_lines(["lumen-kb-docs"])) in said
    for words in (
        "The hub's merge queue merges the merge requests of lumen-kb-docs (merge mode auto)",
        "the queue opens its merge request, runs its pipeline and merges it",
        "Each step ends at its branch pushed with its local tests passing",
        "No step opens a merge request, merges one, or waits for a pipeline",
        "No step edits CI configuration (.gitlab-ci.yml, .github/workflows) unless the goal is about CI",
        "gate 3 (score hacking) for a person",
    ):
        assert words in said, said
    assert said.index("merge queue") < said.index("It serves no goal")  # before the parts members wrote

"""The knowledge graph of a project on the hub that enables the core plugin canvas beside srs and tasks, as criterion
a7 of plan living-lean-canvas names it: the worker's build reads the plugin's source (``evo_agents.core_plugins.canvas.
kg``) beside those of srs and tasks and the runs machines push.

Checked here, end to end on the hub: making a canvas and editing a claim each queue a build, which joins the one that
waits; the build holds the claim as a Claim node, its section with its text, the use case it links implementing it and
the signal key it links measuring it; kg_path goes from the claim to the use case, the task that implements it and the
plan step that implements the task; the claim carries the canvas's label and a member whose grant is below it sees
none of it; a claim archived, and a canvas archived, leave the graph. The product, Northwind Pantry, is made up; the
api and the worker run in this process and the blob store is moto's, as in ``tests.hub.test_kg``.

As criterion a5 of plan problem-oriented-hub names it: the graph holds a problem of the project as a Problem node found
by its code, and its section by the words of its statement; the plan that names it, the claim that links it and the
goal that answers it each address it, kg_context of the plan lists it, and a write of a problem or a goal queues a
build.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import plugin_calls
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.test_kg import PROJECT, build_rows, call_tool, grant, open_hub, put_plan, run_jobs, tool_ok
from tests.hub.test_srs_tasks_plugin_kg import PLAN, STEP, TICKET, USE_CASE, approve_sample, machine, node, queued

CORE = {
    "srs": EntryPoint("srs", "evo_agents.core_plugins.srs:MANIFEST", ENTRY_POINT_GROUP),
    "tasks": EntryPoint("tasks", "evo_agents.core_plugins.tasks:MANIFEST", ENTRY_POINT_GROUP),
    "canvas": EntryPoint("canvas", "evo_agents.core_plugins.canvas:MANIFEST", ENTRY_POINT_GROUP),
}
SLUG = "northwind-pantry"
CANVAS = f"plugin.canvas:canvas:{SLUG}"
CLAIM = "claim:CL-1"
STOCKOUTS = "pantry.stock.outs_per_week"
SIGNAL_KEY = f"signalkey:{STOCKOUTS}"


@pytest.fixture
def plugins_core(monkeypatch):
    """The entry points of the core plugins with srs's, tasks's and canvas's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def hub(hub_db, tmp_path, s3, plugins_core):
    """Project alpha of ``tests.hub.test_kg`` with an admin, owner, who enabled srs, tasks (granted plans:read) and
    canvas, and declared the signal key of stock-outs."""
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        grant(client, found.admin, "owner", "admin", "internal")
        found.owner = live.bearer(live.insert_token(hub_db, "owner"))
        for plugin, permissions in (("srs", []), ("tasks", ["plans:read"]), ("canvas", [])):
            enabled = client.post(
                f"/v1/projects/{PROJECT}/plugins/{plugin}/enable",
                json={"permissions": permissions},
                headers=found.owner,
            )
            assert enabled.status_code == 200, enabled.text
        key = {"name": STOCKOUTS, "kind": "number"}
        declared = client.post(f"/v1/projects/{PROJECT}/signal-keys", json=key, headers=found.owner)
        assert declared.status_code == 201, declared.text
        yield found
    finally:
        client.__exit__(None, None, None)


def canvas_post(hub, path: str, body: dict, member: str = "alice", expect: int = 200) -> dict:
    answer = hub.client.post(f"/v1/x/canvas/{PROJECT}/canvases{path}", json=body, headers=getattr(hub, member))
    assert answer.status_code == expect, answer.text
    return answer.json()


def test_canvas_plugin_graph_on_the_hub_traces_a_claim_to_its_use_case_task_and_step(hub, tmp_path):
    put_plan(hub, PLAN)
    machine(hub, tmp_path)  # the knowledge config, and a build of the plan
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    approve_sample(hub)
    links = {"use_cases": ["UC ORD-1"], "steps": ["rollout#1"]}
    made = hub.client.post(
        f"/v1/x/tasks/{PROJECT}/tasks", json={"title": "Checkout", "links": links}, headers=hub.alice
    )
    assert made.status_code == 201, made.text
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert queued(hub) == 0

    canvas_post(hub, "", {"slug": SLUG, "name": "Northwind Pantry"}, expect=201)
    assert queued(hub) == 1  # making a canvas queued a build
    claim = {"cell": "solution", "text": "Ordering from the shelf with one tap cuts stock-outs"}
    claim |= {"use_cases": ["UC-ORD-001"], "signal_keys": [STOCKOUTS]}
    canvas_post(hub, f"/{SLUG}/edit", {"changes": [claim]})
    assert queued(hub) == 1  # the edit's build joined the one that waits
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert build_rows(hub)[0]["status"] == "succeeded", build_rows(hub)[0].get("error")

    found = node(hub, CLAIM)
    assert (found["kind"], found["status"], found["label"]["level"]) == ("Claim", "declared", "internal")
    assert node(hub, f"{CANVAS}#CL-1")["kind"] == "Section"
    assert node(hub, SIGNAL_KEY)["kind"] == "SignalKey"
    path = tool_ok(hub, "kg_path", {"from": CLAIM, "to": STEP})["structuredContent"]
    assert path["found"] and [step["id"] for step in path["nodes"]] == [CLAIM, USE_CASE, TICKET, STEP]
    chains = tool_ok(hub, "kg_path", {"from": CLAIM})["structuredContent"]
    assert {USE_CASE, TICKET, STEP} <= {step["id"] for step in chains["nodes"]}
    measured = tool_ok(hub, "kg_path", {"from": CLAIM, "to": SIGNAL_KEY})["structuredContent"]
    assert measured["found"] and measured["edges"][0]["rel"] == "measured_by"
    for hidden in (CLAIM, f"{CANVAS}#CL-1", CANVAS, SIGNAL_KEY):  # carol's grant stops at public
        assert call_tool(hub, "kg_node", {"id": hidden}, member="carol").json()["isError"], hidden

    canvas_post(hub, f"/{SLUG}/edit", {"changes": [{"claim": "CL-1", "base_rev": 1, "archived": True}]})
    assert queued(hub) == 1
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert call_tool(hub, "kg_node", {"id": CLAIM}).json()["isError"]  # an archived claim leaves the graph
    assert node(hub, CANVAS)["kind"] == "Document"
    archived = hub.client.post(f"/v1/x/canvas/{PROJECT}/canvases/{SLUG}/archive", headers=hub.owner)
    assert archived.status_code == 200, archived.text
    assert queued(hub) == 1
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert call_tool(hub, "kg_node", {"id": CANVAS}).json()["isError"]  # so does an archived canvas


# Problems (plan problem-oriented-hub, criterion a5)

PROBLEM = "problem:P-1"
STOCK_OUTS = {
    "statement": "Corner shops run out of milk between deliveries",
    "who": "the owners of corner shops",
    "when": "by Thursday of a week with one delivery",
    "workaround": "they drive to a cash and carry",
}
RESTOCK = {
    "id": "restock",
    "title": "Restock",
    "goal": "Shops never run dry.",
    "problems": ["P-1"],
    "steps": [{"id": 1, "title": "Reorder", "what": "the reorder button", "status": "pending"}],
}


def test_problem_graph_on_the_hub_finds_a_problem_by_code_or_text_with_the_plan_claim_and_goal_that_address_it(
    hub, tmp_path
):
    made = hub.client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=STOCK_OUTS, headers=hub.alice)
    assert made.status_code == 200, made.text
    put_plan(hub, RESTOCK)
    machine(hub, tmp_path)  # the knowledge config, and a build of the plan and the problem
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert build_rows(hub)[0]["status"] == "succeeded", build_rows(hub)[0].get("error")

    found = node(hub, PROBLEM)
    assert (found["kind"], found["name"], found["label"]["level"]) == ("Problem", "P-1", "internal")
    by_code = tool_ok(hub, "kg_search", {"query": "P-1"})["structuredContent"]["results"]
    assert by_code[0]["id"] == PROBLEM
    by_text = tool_ok(hub, "kg_search", {"query": "cash and carry"})["structuredContent"]["results"]
    assert "plugin.hub_problems:doc:problems#P-1" in [hit["id"] for hit in by_text]  # the section that defines it
    context = tool_ok(hub, "kg_context", {"ids": ["plan:restock"]})["structuredContent"]
    assert PROBLEM in [item["id"] for item in context["nodes"]]
    planned = tool_ok(hub, "kg_path", {"from": "plan:restock", "to": PROBLEM})["structuredContent"]
    assert planned["found"] and [edge["rel"] for edge in planned["edges"]] == ["addresses"]
    assert call_tool(hub, "kg_node", {"id": PROBLEM}, member="carol").json()["isError"]  # below the project's label

    canvas_post(hub, "", {"slug": SLUG, "name": "Northwind Pantry"}, expect=201)
    claim = {"cell": "problem", "text": "Shops run out of milk by Thursday", "problems": ["P-1"]}
    canvas_post(hub, f"/{SLUG}/edit", {"changes": [claim]})
    goal = {"statement": "No shop runs dry in a week", "problem": "P-1"}
    named = hub.client.put(f"/v1/projects/{PROJECT}/goals/G-1", json=goal, headers=hub.owner)
    assert named.status_code == 200, named.text
    assert queued(hub) == 1  # the canvas, the claim and the goal joined one build
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    for start in (CLAIM, "goal:G-1"):
        path = tool_ok(hub, "kg_path", {"from": start, "to": PROBLEM})["structuredContent"]
        assert path["found"] and [edge["rel"] for edge in path["edges"]] == ["addresses"], start
    assert node(hub, "goal:G-1")["kind"] == "Goal"

    closed = hub.client.post(
        f"/v1/projects/{PROJECT}/problems/P-1/close",
        json={"status": "solved", "reason": "a second delivery on Wednesday"},
        headers=hub.alice,
    )
    assert closed.status_code == 200, closed.text
    assert queued(hub) == 1  # a problem's write queues a build
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    solved = tool_ok(hub, "kg_search", {"query": "second delivery on Wednesday"})["structuredContent"]["results"]
    assert "plugin.hub_problems:doc:problems#P-1" in [hit["id"] for hit in solved]


def test_problem_evidence_joins_the_graph_as_edges_of_the_problem_with_the_label_of_the_project(hub, tmp_path):
    made = hub.client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=STOCK_OUTS, headers=hub.alice)
    assert made.status_code == 200, made.text
    line = {
        "source_kind": "email",
        "source_uri": "mailto:ines@northwind.example",
        "locator": "thread 7",
        "excerpt": "We ring the depot every Thursday because the shelf is empty.",
        "signal": "workaround",
        "label": "internal",
    }
    machine(hub, tmp_path)
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    added = hub.client.post(f"/v1/projects/{PROJECT}/problems/P-1/evidence", json=line, headers=hub.alice)
    assert added.status_code == 201, added.text
    assert queued(hub) == 1  # an added line queues a build
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    found = tool_ok(hub, "kg_search", {"query": "ring the depot"})["structuredContent"]["results"]
    assert "plugin.hub_problems:doc:evidence#P-1/E-1" in [hit["id"] for hit in found]
    path = tool_ok(hub, "kg_path", {"from": "plugin.hub_problems:doc:evidence#P-1/E-1", "to": PROBLEM})
    assert path["structuredContent"]["found"] and [e["rel"] for e in path["structuredContent"]["edges"]] == [
        "evidences"
    ]

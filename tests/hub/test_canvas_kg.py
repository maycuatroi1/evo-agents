"""The knowledge graph of a project on the hub that enables the core plugin canvas beside srs and tasks, as criterion
a7 of plan living-lean-canvas names it: the worker's build reads the plugin's source (``evo_agents.core_plugins.canvas.
kg``) beside those of srs and tasks and the runs machines push.

Checked here, end to end on the hub: making a canvas and editing a claim each queue a build, which joins the one that
waits; the build holds the claim as a Claim node, its section with its text, the use case it links implementing it and
the signal key it links measuring it; kg_path goes from the claim to the use case, the task that implements it and the
plan step that implements the task; the claim carries the canvas's label and a member whose grant is below it sees
none of it; a claim archived, and a canvas archived, leave the graph. The product, Northwind Pantry, is made up; the
api and the worker run in this process and the blob store is moto's, as in ``tests.hub.test_kg``.
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
    assert build_rows(hub)[0]["status"] == "succeeded", build_rows(hub)[0]

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

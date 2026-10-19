"""The knowledge graph of a project on the hub that enables the core plugins srs and tasks, as criterion a4 of plan
srs-tasks-plugins names it: the worker's build reads the plugins' sources (``evo_agents.hub.kg_sources``) beside the
runs machines push.

Checked here, end to end on the hub: approving a version and creating a task each queue a build, which joins the one
that waits; the build holds the use cases of the approved version and the task, and kg_path goes from a use case code
to the task, the plan step it links and the commit the step's evidence names; the plugins' data carries the project's
label and a member whose grant is below it sees none of it; a build of unchanged data points at the artifact of the
build before, and ``kg build --verify`` on the worker's store gives its hash; a changed task is built again, and a
plugin the project turns off leaves the graph.

The api and the worker run in this process and the blob store is moto's, as in ``tests.hub.test_kg``; the machine is a
harness of project alpha that reads the hub's plans and pushes its runs.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.hub import kg_plans, plugin_calls, tables
from evo_agents.hub.kg_build import worker_project
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import sync_project
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.test_kg import (
    HUB_HARNESS_KNOWLEDGE,
    PROJECT,
    InProcessHub,
    build_rows,
    call_tool,
    grant,
    open_hub,
    push,
    put_plan,
    query,
    run_jobs,
    tool_ok,
)

CORE = {
    "srs": EntryPoint("srs", "evo_agents.core_plugins.srs:MANIFEST", ENTRY_POINT_GROUP),
    "tasks": EntryPoint("tasks", "evo_agents.core_plugins.tasks:MANIFEST", ENTRY_POINT_GROUP),
}
SAMPLE_SRS = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
SHA = "4f1c2e7a9b3d5f60718293a4b5c6d7e8f9012345"
PLAN = {
    "id": "rollout",
    "goal": "Customers order books.",
    "steps": [
        {
            "id": 1,
            "title": "Place orders",
            "what": "the checkout",
            "repo": "alpha-app",
            "status": "done",
            "evidence": f"alpha-app@{SHA}: ships",
        }
    ],
}
USE_CASE = "usecase:UC ORD-1"
TICKET = "plugin.tasks:ticket:T-1"
STEP = "plan:rollout/step:1"
COMMIT = f"commit:alpha-app@{SHA}"


@pytest.fixture
def plugins_core(monkeypatch):
    """The entry points of the core plugins with srs's and tasks's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def hub(hub_db, tmp_path, s3, plugins_core):
    """Project alpha of ``tests.hub.test_kg`` with an admin, owner, who enabled srs and tasks (granted plans:read)."""
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        grant(client, found.admin, "owner", "admin", "internal")
        found.owner = live.bearer(live.insert_token(hub_db, "owner"))
        for plugin, permissions in (("srs", []), ("tasks", ["plans:read"])):
            enabled = client.post(
                f"/v1/projects/{PROJECT}/plugins/{plugin}/enable",
                json={"permissions": permissions},
                headers=found.owner,
            )
            assert enabled.status_code == 200, enabled.text
        yield found
    finally:
        client.__exit__(None, None, None)


def machine(hub, tmp_path: Path):
    """A harness of project alpha whose plans the hub holds: synced through the hub, and pushed."""
    root = tmp_path / "alpha-harness"
    (root / "plans/active").mkdir(parents=True)
    (root / "harness.yaml").write_text(
        "name: alpha\nrepos: []\nknowledge_file: knowledge.yaml\nhub: {project: alpha}\n"
    )
    (root / "knowledge.yaml").write_text(HUB_HARNESS_KNOWLEDGE)
    project = load_project_at(root, tmp_path / "kg-home")
    (result,) = sync_project(project, plans=kg_plans.reader(InProcessHub(hub.client, hub.alice)))
    assert result.ok, result.issues
    assert push(hub, SimpleNamespace(project=lambda: project), "alice").ok
    return project


def queued(hub) -> int:
    b = tables.kg_builds
    return query(hub.db, select(func.count()).select_from(b).where(b.c.status == "queued"))[0][0]


def approve_sample(hub) -> None:
    """Import the sample SRS as alice, submit it, and have dave, who wrote none of it, approve it."""
    base = f"/v1/x/srs/{PROJECT}"
    body = {"markdown": SAMPLE_SRS.read_text(encoding="utf-8"), "slug": "demo-shop"}
    assert hub.client.post(f"{base}/import", json=body, headers=hub.alice).status_code == 200
    doc = f"{base}/documents/demo-shop/versions/1"
    assert hub.client.post(f"{doc}/submit", headers=hub.alice).status_code == 200
    approved = hub.client.post(f"{doc}/reviews", json={"decision": "approve"}, headers=hub.dave)
    assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text


def node(hub, node_id: str, member: str = "alice") -> dict:
    return tool_ok(hub, "kg_node", {"id": node_id}, member=member)["structuredContent"]["node"]


def test_srs_tasks_plugin_graph_on_the_hub_traces_a_use_case_to_its_task_step_and_commit(hub, tmp_path):
    put_plan(hub, PLAN)
    machine(hub, tmp_path)  # the knowledge config, and a build of the plan
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert build_rows(hub)[0]["status"] == "succeeded"
    assert call_tool(hub, "kg_node", {"id": USE_CASE}).json()["isError"]  # no version approved yet

    approve_sample(hub)
    assert queued(hub) == 1  # the approval queued a build
    links = {"use_cases": ["UC ORD-1"], "steps": ["rollout#1"]}
    made = hub.client.post(
        f"/v1/x/tasks/{PROJECT}/tasks", json={"title": "Checkout", "links": links}, headers=hub.alice
    )
    assert made.status_code == 201, made.text
    assert queued(hub) == 1  # the task's build joined the one that waits
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    built = build_rows(hub)[0]
    assert built["status"] == "succeeded", built

    use_case = node(hub, USE_CASE)
    assert use_case["kind"] == "UseCase" and use_case["status"] == "declared"
    path = tool_ok(hub, "kg_path", {"from": USE_CASE, "to": COMMIT})["structuredContent"]
    assert path["found"] and [found["id"] for found in path["nodes"]] == [USE_CASE, TICKET, STEP, COMMIT]
    ticket = node(hub, TICKET)
    assert ticket["kind"] == "Ticket" and ticket["label"]["level"] == "internal"  # the project's label
    for hidden in (TICKET, USE_CASE, "plugin.srs:document:demo-shop"):  # carol's grant stops at public
        assert call_tool(hub, "kg_node", {"id": hidden}, member="carol").json()["isError"], hidden

    again = hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice)  # nothing changed since
    assert again.status_code == 202, again.text
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    same = build_rows(hub)[0]
    assert same["id"] != built["id"] and same["content_hash"] == built["content_hash"]
    assert same["artifact_reused_from"] == built["id"]
    checked = build_project(worker_project(hub.worker_dir, PROJECT), verify=True)
    assert checked.ok and checked.verify["match"] and checked.content_hash == built["content_hash"]

    edited = hub.client.patch(f"/v1/x/tasks/{PROJECT}/tasks/T-1", json={"status": "done"}, headers=hub.alice)
    assert edited.status_code == 200, edited.text
    assert queued(hub) == 1
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert node(hub, TICKET)["props"]["status"] == "done"

    off = hub.client.post(f"/v1/projects/{PROJECT}/plugins/tasks/disable", headers=hub.owner)
    assert off.status_code == 200, off.text
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert call_tool(hub, "kg_node", {"id": TICKET}).json()["isError"]  # the plugin's tickets left with it
    assert node(hub, USE_CASE)["status"] == "declared"

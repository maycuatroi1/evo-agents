"""The event ``plan.step`` (``evo_agents.hub.plan_step_status``): a step's status of a plan set or changed on the hub,
whatever wrote it, recorded into hub_events beside the deliveries to plugins.

The checks: steps are matched by how plans name them (id, else order, else position), and only a status the write
set or changed emits, a step dropped emits nothing; on the hub, a PUT that creates a plan emits each step that has a
status, a PUT of the same body nothing, an edit of the body each step whose status it changed or added, a PATCH of a
step's status one event and a PATCH of another key none, the hub's write of a run's step (``evo-agents worker step``)
one, and a complete that marks the steps left done one for each; each event carries the plan, the step as text, the
statuses and the revision. The plans, northwind-launch among them, are made up.
"""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import plan_step_status, tables
from tests.hub.live import sql
from tests.hub.test_plan_runs import (  # noqa: F401
    PLAN,
    client,
    held_plan,
    hub,
    plan_body,
    push,
    reported,
    started,
)
from tests.hub.test_run_waits import marked
from tests.hub.test_runs import PROJECT


def test_changes_name_each_step_as_plans_do_and_keep_only_a_status_set_or_changed():
    before = {
        "steps": [
            {"id": 1, "status": "done"},
            {"id": "b", "status": "pending"},
            {"order": 3.5, "status": "pending"},
            {"what": "no id or order", "status": "pending"},
            {"id": 9, "status": "pending"},
        ]
    }
    after = {
        "steps": [
            {"id": 1, "status": "done"},  # unchanged
            {"id": "b", "status": "in_progress"},
            {"order": 3.5, "status": "done"},
            {"what": "no id or order", "status": "blocked"},  # its position, 3
            {"id": 10, "status": "pending"},  # new; step 9 was dropped
            {"id": 11},  # new, with no status
            "a bare string",
        ]
    }
    assert plan_step_status.changes(before, after) == [
        ("b", "pending", "in_progress"),
        ("3.5", "pending", "done"),
        ("3", "pending", "blocked"),
        ("10", None, "pending"),
    ]
    assert plan_step_status.changes(None, {"steps": [{"id": 1, "status": "done"}, {"id": 2}]}) == [("1", None, "done")]
    assert plan_step_status.changes(after, after) == []
    assert plan_step_status.payload("northwind-launch", 4, "8", "in_progress", "done") == {
        "v": 1,
        "plan": "northwind-launch",
        "plan_id": "northwind-launch",
        "step": "8",
        "status": "done",
        "from": "in_progress",
        "to": "done",
        "revision": 4,
    }


def recorded(db, after: int = 0) -> list[tuple]:
    """(id, plan, step, from, to, revision) of each plan.step event in hub_events past ``after``, oldest first."""
    e = tables.hub_events
    query = select(e.c.id, e.c.payload).where(e.c.kind == "plan.step", e.c.id > after).order_by(e.c.id)
    found = []
    for event_id, payload in sql(db, query):
        assert payload["plan"] == payload["plan_id"] and payload["status"] == payload["to"] and payload["v"] == 1
        found.append((event_id, payload["plan"], payload["step"], payload["from"], payload["to"], payload["revision"]))
    return found


def last_id(db) -> int:
    found = recorded(db)
    return found[-1][0] if found else 0


def changed(db, after: int) -> list[tuple]:
    return [row[1:] for row in recorded(db, after)]


def test_every_write_that_sets_or_changes_a_step_status_records_plan_step(client, hub, hub_db):
    # the fixture pushed fleet: a create emits each of its five steps, from no status
    assert [row for row in changed(hub_db, 0) if row[0] == PLAN] == [
        (PLAN, "1", None, "done", 1),
        (PLAN, "2", None, "pending", 1),
        (PLAN, "3", None, "pending", 1),
        (PLAN, "4", None, "in_progress", 1),
        (PLAN, "5", None, "done", 1),
    ]

    body = plan_body("northwind-launch")
    cursor = last_id(hub_db)
    push(client, hub["owner"], body)
    assert len(changed(hub_db, cursor)) == 5

    cursor = last_id(hub_db)
    push(client, hub["owner"], body)  # the same body: nothing written, nothing emitted
    assert changed(hub_db, cursor) == []

    # an import or edit of the body: the step it moved and the step it added, not the title it changed
    edited = {**body, "title": "Launch northwind", "steps": [dict(step) for step in body["steps"]]}
    edited["steps"][1]["status"] = "in_progress"
    edited["steps"].append({"id": 6, "title": "Notes", "repo": "evo-agents", "what": "the notes", "status": "pending"})
    response = client.put(
        f"/v1/projects/{PROJECT}/plans/northwind-launch",
        json={"body": edited, "if_revision": 1},
        headers=hub["owner"],
    )
    assert response.status_code == 200, response.text
    assert changed(hub_db, cursor) == [
        ("northwind-launch", "2", "pending", "in_progress", 2),
        ("northwind-launch", "6", None, "pending", 2),
    ]

    # `evo-agents hub plan step` and the MCP tool plan_step: a status emits, another key does not
    cursor = last_id(hub_db)
    marked(client, hub["owner"], "northwind-launch", 2, "done")
    revision = held_plan(client, hub["owner"], "northwind-launch")["revision"]
    response = client.patch(
        f"/v1/projects/{PROJECT}/plans/northwind-launch",
        json={"section": "steps", "step": 2, "updates": {"note": "shipped"}, "if_revision": revision},
        headers=hub["owner"],
    )
    assert response.status_code == 200, response.text
    assert changed(hub_db, cursor) == [("northwind-launch", "2", "in_progress", "done", 3)]

    # `evo-agents worker step`: the hub writes the run's step as its dispatcher
    cursor = last_id(hub_db)
    worker, run = started(client, hub)
    reported(client, worker, run["id"], 2, "in_progress")
    revision = held_plan(client, hub["owner"])["revision"]
    assert changed(hub_db, cursor) == [(PLAN, "2", "pending", "in_progress", revision)]

    # complete marking the steps left done: one event for each step it marked
    cursor = last_id(hub_db)
    response = client.post(
        f"/v1/projects/{PROJECT}/plans/northwind-launch/complete",
        json={"mark_steps_done": True},
        headers=hub["owner"],
    )
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    assert changed(hub_db, cursor) == [
        ("northwind-launch", "3", "pending", "done", revision),
        ("northwind-launch", "4", "in_progress", "done", revision),
        ("northwind-launch", "6", "pending", "done", revision),
    ]

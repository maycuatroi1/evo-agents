"""The event tasks.status (``evo_agents.hub.task_status``), as criterion a3 of plan loop-engine names it.

Every status a task takes emits tasks.status in the transaction that set it: a task made on the web, the CLI or for a
member by an agent (cause create, ``from`` None), an edit of its status (edit; an edit that leaves the status emits
nothing), an import once its parents are set (import), and on_steps_done moving it when the hub delivers plan.step
(steps_done, the hub its actor). The payload names the task by id, number and ref, the two statuses with their groups,
who, why, its parent and its links to SRS items and plan steps; ids, codes and states, never a title. Plugins granted
tasks:read take it, at least once, and hub_events keeps it, which the Loop reads by cursor. The stack is
``tests.hub.test_tasks_plugin``'s: tasks and srs on, the sample SRS imported.
"""

from __future__ import annotations

import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import hub_events, plugin_events, tables
from evo_agents.plugins import protocol
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_plugin_points import FakeHost, install, sample_manifest
from tests.hub.test_runs import PLAN
from tests.hub.test_tasks_plugin import (  # noqa: F401 (client, hub, members and tasks_core are fixtures)
    client,
    configure,
    created,
    deliver,
    edited,
    enable,
    hub,
    imported,
    members,
    sample_tasks,
    set_step,
    tasks_core,
)

STATUS = hub_events.TASK_STATUS


def stored(db) -> list[dict]:
    """The payloads of tasks.status in hub_events, oldest first."""
    e = tables.hub_events
    return [row[0] for row in sql(db, select(e.c.payload).where(e.c.kind == STATUS).order_by(e.c.id))]


def ids_of(db) -> dict[int, int]:
    """number -> id of each task."""
    t = tables.tasks.items
    return dict(sql(db, select(t.c.number, t.c.id)))


def test_tasks_events_a_task_made_edited_and_imported_emits_tasks_status_to_plugins_granted_tasks_read(
    client, hub, hub_db
):
    install(hub_db, sample_manifest(permissions=["tasks:read"]))  # takes every kind its grant lets it read
    enable(client, hub["owner"], "sample", ["tasks:read"])
    links = {"use_cases": ["UC ORD-1"], "steps": [f"{PLAN}#2"]}
    created(client, hub["alice"], title="Queue the orders", links=links)
    created(client, hub["alice"], title="Retry a payment", parent="T-1", status="in_progress")
    edited(client, hub["bao"], "T-2", title="Retry a declined payment")  # the status stays: no event
    edited(client, hub["bao"], "T-2", status="done")

    made, child, done = stored(hub_db)
    ids = ids_of(hub_db)
    assert {key: made[key] for key in ("v", "number", "ref", "from", "to", "from_group", "to_group")} == {
        "v": 1,
        "number": 1,
        "ref": "T-1",
        "from": None,
        "to": "backlog",
        "from_group": None,
        "to_group": "todo",
    }
    assert (made["task_id"], made["actor"], made["login"], made["cause"], made["source"]) == (
        ids[1],
        "person",
        "alice",
        "create",
        None,
    )
    assert [link["code"] for link in made["srs_items"]] == ["UC ORD-1"] and isinstance(made["srs_items"][0]["id"], int)
    assert made["plan_steps"] == [{"plan_id": PLAN, "step": "2"}] and (made["parent"], made["parent_id"]) == (
        None,
        None,
    )
    assert (child["ref"], child["to"], child["to_group"], child["parent"], child["parent_id"]) == (
        "T-2",
        "in_progress",
        "doing",
        "T-1",
        ids[1],
    )
    assert (done["from"], done["to"], done["from_group"], done["to_group"]) == ("in_progress", "done", "doing", "done")
    assert (done["cause"], done["login"], done["parent"]) == ("edit", "bao", "T-1")
    assert "Queue the orders" not in json.dumps([made, child, done]), "ids, codes and states, never a title"

    # an import emits each task's status once its parent is set
    imported(client, hub["alice"], sample_tasks())
    by_import = stored(hub_db)[3:]
    assert [(e["ref"], e["from"], e["to"], e["cause"], e["parent"]) for e in by_import] == [
        ("T-3", None, "in_progress", "import", None),
        ("T-4", None, "done", "import", "T-3"),
        ("T-5", None, "todo", "import", None),
    ]

    # the plugin granted tasks:read takes the same payloads, at least once
    d = tables.plugin_deliveries
    found = sql(hub_db, select(d.c.kind, d.c.payload).where(d.c.kind != "notification").order_by(d.c.id))
    assert found == [(STATUS, payload) for payload in stored(hub_db)]
    fake = FakeHost()
    client.app.state.plugin_host = fake
    report = client.portal.call(lambda: plugin_events.deliver(client.app.state.engine, host=fake))
    assert (report["delivered"], report["retried"], report["failed"]) == (6, 0, 0)
    sent = [call.params for call in fake.calls if call.method == "event"]
    assert [params["kind"] for params in sent] == [STATUS] * 6
    assert all(protocol.params_problems("event", params) == [] for params in sent)


def test_tasks_events_on_steps_done_emits_tasks_status_with_the_hub_its_actor_once(client, hub, hub_db):
    configure(client, hub["owner"], "tasks", {"on_steps_done": "in_review"})
    created(client, hub["alice"], title="Queue and daemon", links={"steps": [f"{PLAN}#2"]})
    set_step(client, hub["owner"], 2, "done")
    assert len(stored(hub_db)) == 1, "the plan's step moves the task when plan.step is delivered"
    deliver(client)
    moved = stored(hub_db)[-1]
    assert (moved["ref"], moved["from"], moved["to"], moved["from_group"], moved["to_group"]) == (
        "T-1",
        "backlog",
        "in_review",
        "todo",
        "review",
    )
    assert (moved["cause"], moved["actor"], moved["login"], moved["user_id"]) == ("steps_done", "hub", None, None)
    assert moved["source"] == {"plan_id": PLAN, "step": "2"} and moved["plan_steps"] == [{"plan_id": PLAN, "step": "2"}]
    # plan.step delivered again writes no second move, and no second event
    d = tables.plugin_deliveries
    with live.engine(hub_db).begin() as conn:
        conn.execute(d.update().values(state="pending", delivered_at=None))
    deliver(client)
    assert len(stored(hub_db)) == 2

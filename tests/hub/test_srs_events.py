"""The event srs.version.approved (``evo_agents.hub.server.srs.events``) and the tasks the plugin srs makes from it,
as criterion a3 of plan loop-engine names it.

Approving a version emits srs.version.approved in the approval's transaction: the document, the version, the version
approved before it, the approver, and the codes of the items it added, changed and dropped by kind; ids and codes,
never a text. Plugins granted srs:read take it, at least once, as the events before it, and hub_events keeps it. The
plugin srs takes it ungranted through its events contribution approvals, whose handler calls ``auto_tasks.approved``
with the payload: the route no longer calls it, so the tasks of a version come when the hub delivers the event, and a
delivery again makes nothing more. A frame task made and a task cancelled emit tasks.status with the cause
srs_approval, the hub its actor. A version approved before the project enabled the plugin tasks makes no task. The
stack is ``tests.hub.test_ba_agent``'s: owner runs @ba on the worker studio, chi writes, someone-else approves.
"""

from __future__ import annotations

import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import plugin_events, tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.srs.events import on_version_approved
from evo_agents.plugins import protocol
from tests.hub.live import sql
from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
    OTHER,
    SRS,
    app_key,
    client,
    delivered,
    enable,
    enable_ba,
    hub,
    imported,
    item,
    members,
    owners_worker,
    review,
    running,
    runs_of,
    saved,
    srs_core,
    submitted,
)
from tests.hub.test_plugin_points import FakeHost, install, sample_manifest
from tests.hub.test_runs import PROJECT, moved
from tests.hub.test_srs_auto_tasks import approval_rows, approved, counts, reviewed, tasks

APPROVED = "srs.version.approved"
FIRST_USE_CASES = ["UC ORD-1", "UC ORD-2", "UC RET-1"]  # of the sample, tests/hub/fixtures/srs/demo-shop-srs.md
ACTOR = "| Item | Content |\n|---|---|\n| Actor | Customer |"  # the body of a new use case


def stored(db, kind: str | None = None) -> list[dict]:
    """The payloads of hub_events, of ``kind`` when given, oldest first."""
    e = tables.hub_events
    query = select(e.c.payload).order_by(e.c.id)
    return [row[0] for row in sql(db, query.where(e.c.kind == kind) if kind else query)]


def deliveries(db) -> list[tuple[str, str, str, dict]]:
    """(plugin, contribution, kind, payload) of each delivery of an event, oldest first."""
    d, p = tables.plugin_deliveries, tables.plugins
    query = select(p.c.name, d.c.contribution, d.c.kind, d.c.payload).join(p, p.c.id == d.c.plugin_id)
    return [tuple(row) for row in sql(db, query.where(d.c.kind != "notification").order_by(d.c.id))]


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def test_srs_events_an_approval_emits_the_codes_it_changed_to_plugins_granted_srs_read_and_hub_events(
    client, hub, hub_db
):
    install(hub_db, sample_manifest(permissions=["srs:read"]))  # takes every kind its grant lets it read
    runs_only = sample_manifest(
        id="runs-only", permissions=["runs:read"], contributes={"events": [{"name": "srs", "on": [APPROVED]}]}
    )
    install(hub_db, runs_only)
    enable(client, hub["owner"], "sample", ["srs:read"])
    enable(client, hub["owner"], "runs-only", ["runs:read"])
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)  # @ba is off: no review holds the approval
    assert stored(hub_db) == [], "a draft and a submit emit nothing"
    assert review(client, hub["other"], 1).json()["state"] == "approved"

    (first,) = stored(hub_db, APPROVED)
    added = first["items"]["added"]
    assert {key: first[key] for key in ("v", "document", "version", "previous", "approver")} == {
        "v": 1,
        "document": "demo-shop",
        "version": 1,
        "previous": None,
        "approver": OTHER,
    }
    assert all(isinstance(first[key], int) for key in ("document_id", "version_id", "approver_id"))
    assert added["use_case"] == FIRST_USE_CASES and "BR ORD1-1" in added["business_rule"]
    assert (first["items"]["changed"], first["items"]["dropped"]) == ({}, {})
    assert "Place an order" not in json.dumps(first), "ids and codes, never a text"
    # srs takes its own event ungranted, sample with srs:read, runs-only not at all
    sent_all = [row for row in deliveries(hub_db) if row[2] == APPROVED]
    assert sent_all == [("sample", "run-changed", APPROVED, first), ("srs", "approvals", APPROVED, first)]

    fake = FakeHost()
    client.app.state.plugin_host = fake
    report = client.portal.call(lambda: plugin_events.deliver(client.app.state.engine, host=fake))
    assert (report["delivered"], report["retried"], report["failed"]) == (5, 0, 0)  # and the 3 use cases
    (sent,) = [c for c in fake.calls if c.method == "event" and c.params["kind"] == APPROVED]
    assert (sent.plugin, sent.params["kind"], sent.params["payload"]) == ("sample", APPROVED, first)
    assert protocol.params_problems("event", sent.params) == []

    # version 3 changes a business rule of UC ORD-1, adds UC ORD-3, and drops UC RET-1 (version 2 is its draft)
    order, cart = item(client, hub["chi"], "UC ORD-1"), item(client, hub["chi"], "BR ORD1-1")
    new = {"kind": "use_case", "code": "UC ORD-3", "title": "Cancel an order", "parent": order["parent"], "body": ACTOR}
    saved(client, hub["chi"], 1, {"key": cart["key"], "body": "- A cart holds at most 40 lines."}, {"new": new})
    retired = client.post(f"{SRS}/documents/demo-shop/retire", json={"codes": ["UC RET-1"]}, headers=hub["owner"])
    assert retired.status_code == 200 and retired.json()["version"] == 3, retired.text
    submitted(client, hub["chi"], 3)
    assert review(client, hub["other"], 3).json()["state"] == "approved"
    third = stored(hub_db, APPROVED)[-1]
    assert (third["version"], third["previous"]) == (3, 1)
    assert third["items"]["added"] == {"use_case": ["UC ORD-3"]}
    assert third["items"]["changed"]["business_rule"] == ["BR ORD1-1"]
    assert third["items"]["dropped"]["use_case"] == ["UC RET-1"]


async def _pending_again(engine) -> None:
    """Every delivery of srs.version.approved pending and due again, as after a hub that stopped between the plugin's
    answer and the commit."""
    d = tables.plugin_deliveries
    async with engine.begin() as conn:
        again = {"state": "pending", "delivered_at": None, "next_at": func.now()}
        await conn.execute(update(d).where(d.c.kind == APPROVED).values(again))


def test_srs_events_the_tasks_of_an_approval_come_from_its_delivery_and_a_replay_makes_nothing(client, hub, hub_db):
    enable(client, hub["owner"], "tasks", ["plans:read"])
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    reviewed(client, studio)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    # the route no longer keeps the tasks: until the event is delivered there is none, and no split run
    assert counts(hub_db) == (0, 0, 0, 1) and approval_rows(hub_db) == []
    assert [
        kind for (kind,) in sql(hub_db, select(tables.hub_events.c.kind).where(tables.hub_events.c.kind == APPROVED))
    ] == [APPROVED]

    delivered(client)
    assert [t["ref"] for t in tasks(client, hub["reader"])] == ["T-1", "T-2", "T-3"]
    split = running(client, studio, "task")
    assert "Split the frame tasks of the SRS document demo-shop" in split["prompt"]
    made = stored(hub_db, "tasks.status")
    assert [(e["ref"], e["from"], e["to"], e["to_group"], e["cause"], e["actor"]) for e in made] == [
        (f"T-{n}", None, "backlog", "todo", "srs_approval", "hub") for n in (1, 2, 3)
    ]
    assert [[link["code"] for link in e["srs_items"]] for e in made] == [[code] for code in FIRST_USE_CASES]
    (first, *_) = made
    assert (first["source"]["document"], first["source"]["version"], first["parent"], first["login"]) == (
        "demo-shop",
        1,
        None,
        None,
    )

    # delivered again, twice, and called again with the same params: no task, comment, history row, run or event more
    before, events_before = counts(hub_db), len(stored(hub_db, APPROVED))
    for _ in range(2):
        client.portal.call(_pending_again, client.app.state.engine)
        assert delivered(client)["delivered"] == 1
    d = tables.plugin_deliveries
    ((delivery_id, payload),) = sql(hub_db, select(d.c.id, d.c.payload).where(d.c.kind == APPROVED))
    params = {"event": "approvals", "project": PROJECT, "payload": payload, "kind": APPROVED, "delivery": delivery_id}
    context = CoreContext("srs", client.app.state.engine, None, PROJECT, project_id(hub_db))
    assert client.portal.call(on_version_approved, params, context) == {}
    assert counts(hub_db) == before == (3, 0, 6, 2) and len(stored(hub_db, APPROVED)) == events_before
    assert len(approval_rows(hub_db)) == 1

    # version 2 drops UC RET-1: its open frame task T-3 is cancelled, and tasks.status says so
    moved(client, studio, split["id"], "verifying", "done", summary="Nothing split.")
    retired = client.post(f"{SRS}/documents/demo-shop/retire", json={"codes": ["UC RET-1"]}, headers=hub["owner"])
    assert retired.status_code == 200 and retired.json()["version"] == 2, retired.text
    approved(client, hub, studio, 2)
    (cancelled,) = [e for e in stored(hub_db, "tasks.status") if e["to"] == "cancelled"]
    assert (cancelled["ref"], cancelled["from"], cancelled["from_group"], cancelled["to_group"]) == (
        "T-3",
        "backlog",
        "todo",
        "cancelled",
    )
    assert (cancelled["cause"], cancelled["actor"], cancelled["source"]["version"]) == ("srs_approval", "hub", 2)


def test_srs_events_a_version_approved_before_the_project_enabled_tasks_makes_no_task(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    enable(client, hub["owner"], "tasks", ["plans:read"])  # after the approval, before its delivery
    delivered(client)
    assert tasks(client, hub["reader"]) == [] and approval_rows(hub_db) == [] and runs_of(hub_db, "task") == []
    # the next version, approved while tasks is on, makes the frame task of its new use case
    order = item(client, hub["chi"], "UC ORD-1")
    new = {"kind": "use_case", "code": "UC ORD-3", "title": "Cancel an order", "parent": order["parent"], "body": ACTOR}
    saved(client, hub["chi"], 1, {"new": new})
    submitted(client, hub["chi"], 2)
    assert review(client, hub["other"], 2).json()["state"] == "approved"
    delivered(client)
    assert [(t["ref"], t["title"]) for t in tasks(client, hub["reader"])] == [("T-1", "UC ORD-3: Cancel an order")]
    assert [row[:3] for row in approval_rows(hub_db)] == [(2, 1, OTHER)]

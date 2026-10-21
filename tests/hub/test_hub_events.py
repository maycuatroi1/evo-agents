"""The hub's events (``evo_agents.hub.hub_events``) and the three plugin events of plan living-lean-canvas
(``evo_agents.hub.plugin_events``), as criterion a6 names them.

A rev of a claim emits canvas.claim, an approved baseline canvas.baseline, a batch of signals signal.posted, each in
the transaction that made it: to the plugins granted canvas:read or signals:read that take them, at least once, as the
events before them, and into hub_events, which is only added to and which the Loop reads by cursor per project. The
four events before keep their names and payloads and stay out of hub_events. A project's events are written one
transaction after the other, so their ids grow in the order they become visible. The product, Northwind Pantry, is
made up.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import hub_events, plugin_events, tables
from evo_agents.plugins import protocol
from tests.hub.live import bearer, sql
from tests.hub.test_ba_agent import agent_of, enable, granted, members, mention, running, tool  # noqa: F401
from tests.hub.test_plugin_points import FakeHost, install, sample_manifest
from tests.hub.test_signals import ingest_key
from tests.hub.test_strategist_agent import (  # noqa: F401
    CANVAS,
    CARD,
    app_key,
    client,
    edit,
    hub,
    strategist_core,
)
from tests.hub.test_value_check import NUMBER_KEY, add_key, project_id, push

CANVAS_AND_SIGNALS = ["runs:read", "canvas:read", "signals:read"]


def stored(db) -> list[tuple[int, str, dict]]:
    e = tables.hub_events
    return [tuple(row) for row in sql(db, select(e.c.id, e.c.kind, e.c.payload).order_by(e.c.id))]


def deliveries(db) -> list[tuple[str, str, dict]]:
    d, p = tables.plugin_deliveries, tables.plugins
    query = select(p.c.name, d.c.kind, d.c.payload).join(p, p.c.id == d.c.plugin_id).order_by(d.c.id)
    return [tuple(row) for row in sql(db, query.where(d.c.kind != "notification"))]


def test_hub_events_keep_each_rev_and_batch_of_a_project_in_order_read_by_cursor(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": CARD})
    add_key(client, hub, NUMBER_KEY, "number")
    push(client, hub["other"], {"key": NUMBER_KEY, "value": 0.4}, {"key": NUMBER_KEY, "value": 0.6})
    service = bearer(ingest_key(client, hub["owner"])["token"])
    push(client, service, {"key": NUMBER_KEY, "value": 0.5, "tenant": "northwind", "external_id": "kb-1"})
    push(client, service, {"key": NUMBER_KEY, "value": 0.5, "tenant": "northwind", "external_id": "kb-1"})  # a repeat
    chat = client.post("/v1/chats", json={"title": "Talk", "people": [], "agents": ["reviewer"]}, headers=hub["owner"])
    assert chat.status_code == 201, chat.text
    said = client.post(f"/v1/chats/{chat.json()['id']}/messages", json={"body": "hello"}, headers=hub["owner"])
    assert said.status_code == 201, said.text

    found = stored(hub_db)
    assert [kind for _, kind, _ in found] == ["canvas.claim", "canvas.claim", "signal.posted", "signal.posted"]
    assert [event_id for event_id, _, _ in found] == sorted(event_id for event_id, _, _ in found)
    made, moved = found[0][2], found[1][2]
    assert {key: made[key] for key in ("v", "canvas", "claim", "rev", "change", "from", "to", "actor", "login")} == {
        "v": 1,
        "canvas": "northwind-pantry",
        "claim": "CL-1",
        "rev": 1,
        "change": "create",
        "from": None,
        "to": "assumption",
        "actor": "person",
        "login": "chi",
    }
    assert (moved["rev"], moved["change"], moved["from"], moved["to"], moved["cell"]) == (
        2,
        "edit",
        "assumption",
        "testing",
        "problem",
    )
    by_member, by_key = found[2][2], found[3][2]
    assert (by_member["via"], by_member["login"], by_member["ingest_key"], by_member["accepted"]) == (
        "member",
        "someone-else",
        None,
        2,
    )
    (counted,) = by_member["keys"]
    assert (counted["key"], counted["count"]) == (NUMBER_KEY, 2)
    assert counted["first_measured_at"] <= counted["last_measured_at"]
    assert (by_key["via"], by_key["login"], by_key["ingest_key"], by_key["accepted"]) == (
        "ingest",
        None,
        "northwind-kb",
        1,
    )
    text = json.dumps([payload for _, _, payload in found])
    assert "Corner shops" not in text and "0.4" not in text and "hello" not in text, "ids and states, never a text"

    # the Loop reads one project's events past its cursor, oldest first
    app, pid = client.app, project_id(hub_db)

    async def read(cursor: int, limit: int = 1000):
        async with app.state.engine.begin() as conn:
            return [(event.id, event.kind) for event in await hub_events.after(conn, pid, cursor, limit)]

    every = client.portal.call(read, 0)
    assert every == [(event_id, kind) for event_id, kind, _ in found]
    assert client.portal.call(read, every[1][0]) == every[2:]
    assert client.portal.call(read, 0, 1) == every[:1]
    assert client.portal.call(read, every[-1][0]) == []


def test_hub_events_of_one_project_are_written_one_transaction_after_the_other(client, hub, hub_db):
    app, pid = client.app, project_id(hub_db)

    async def race():
        engine = app.state.engine
        first = await engine.connect()
        await first.begin()
        earlier = await hub_events.record(first, hub_events.CLAIM, pid, {"v": 1})
        second = await engine.connect()
        await second.begin()
        later = asyncio.create_task(hub_events.record(second, hub_events.SIGNALS, pid, {"v": 1}))
        await asyncio.sleep(0.3)
        waited = not later.done()  # the project's lock holds the second until the first commits
        await first.commit()
        await first.close()
        later_id = await asyncio.wait_for(later, 5)
        await second.commit()
        await second.close()
        return earlier, waited, later_id

    earlier, waited, later = client.portal.call(race)
    assert waited and later > earlier
    with pytest.raises(ValueError):
        asyncio.run(hub_events.record(None, "run.state", pid, {}))  # the old events stay out of hub_events


def test_plugin_events_of_canvas_and_signals_reach_only_plugins_granted_their_permission(client, hub, hub_db):
    install(hub_db, sample_manifest(permissions=CANVAS_AND_SIGNALS))  # takes every kind its grant lets it read
    runs_only = sample_manifest(
        id="runs-only",
        permissions=["runs:read"],
        contributes={"events": [{"name": "claims", "on": ["canvas.claim", "signal.posted", "run.state"]}]},
    )
    install(hub_db, runs_only)
    enable(client, hub["owner"], "sample", CANVAS_AND_SIGNALS)
    enable(client, hub["owner"], "runs-only", ["runs:read"])
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "text": "Corner shops run out between deliveries"})
    add_key(client, hub, NUMBER_KEY, "number")
    push(client, hub["other"], {"key": NUMBER_KEY, "value": 0.4})

    found = deliveries(hub_db)
    assert [(name, kind) for name, kind, _ in found] == [("sample", "canvas.claim"), ("sample", "signal.posted")]
    events = {kind: payload for _, kind, payload in stored(hub_db)}
    assert found[0][2] == events["canvas.claim"] and found[1][2] == events["signal.posted"], "the same payload"

    fake = FakeHost()
    client.app.state.plugin_host = fake
    report = client.portal.call(lambda: plugin_events.deliver(client.app.state.engine, host=fake))
    assert (report["delivered"], report["failed"]) == (2, 0)
    sent = [(call.plugin, call.params["event"], call.params["kind"]) for call in fake.calls if call.method == "event"]
    assert sent == [("sample", "run-changed", "canvas.claim"), ("sample", "run-changed", "signal.posted")]
    assert all(protocol.params_problems("event", call.params) == [] for call in fake.calls)
    assert plugin_events.PERMISSION["canvas.claim"] == plugin_events.PERMISSION["canvas.baseline"] == "canvas:read"
    assert plugin_events.PERMISSION["signal.posted"] == "signals:read"
    assert {kind: plugin_events.PERMISSION[kind] for kind in protocol.EVENT_KINDS[:4]} == {
        "run.state": "runs:read",
        "chat.message": "chats:read",
        "plan.step": "plans:read",
        "task.result": "runs:dispatch",
    }

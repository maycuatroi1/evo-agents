"""Baselines of the core plugin canvas (``evo_agents.hub.server.canvas``: ``baselines``, ``decisions``, ``schedule``,
``notices``, ``events``), as criterion a6 of plan living-lean-canvas names them.

The plugin's job ``baseline`` proposes each canvas's baseline once a week at the time of the config's ``baseline``, in
``baseline.timezone``, else the Curator charter's zone, else UTC: every change of status since the status in effect,
the value check's proposals, test cards past their deadline without real evidence (extended or retired), and the new
claims, included as they are. The writers who may decide get the notice canvas_baseline with a link; the member who
changed a status, accepted the proposal that did, or opened a baseline at once, never decides it unless the config's
self_approval is on, and a run never does. A rejection takes the claim back to the status in effect with a rev of kind
baseline; once every item is decided the baseline is approved with a snapshot that never changes, and canvas.baseline
names the changes that took effect. The product, Northwind Pantry, is made up.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select

from evo_agents.hub import plugin_jobs, tables
from evo_agents.hub.server.canvas import WEEKDAYS, decisions, schedule, server, writes
from evo_agents.hub.server.canvas.content import CanvasError
from evo_agents.hub.server.security import Principal, RunScope
from tests.hub.live import sql
from tests.hub.test_ba_agent import agent_of, enable, granted, members, mention, running, tool  # noqa: F401
from tests.hub.test_runs import PROJECT
from tests.hub.test_strategist_agent import (  # noqa: F401
    CANVAS,
    CARD,
    TOOLS,
    app_key,
    claim_of,
    client,
    edit,
    enable_strategist,
    hub,
    listed,
    owners_worker,
    strategist_core,
)
from tests.hub.test_value_check import NUMBER_KEY, add_key, checked, project_id, push

BASELINES = f"{CANVAS}/baselines"
TODAY = datetime.now(UTC).date()  # the day it is for the hub, in UTC
GONE = (TODAY - timedelta(days=3)).isoformat()  # a deadline that passed
LATER = (TODAY + timedelta(days=30)).isoformat()


def due_now(client, hub, **extra) -> None:
    """The project's baselines due at 00:00 today, in UTC unless ``extra`` says otherwise."""
    today = WEEKDAYS[datetime.now(UTC).weekday()]
    values = {"baseline": {"weekday": today, "time": "00:00", "timezone": "UTC", **extra}}
    written = client.post(
        f"/v1/projects/{PROJECT}/plugins/canvas/config", json={"values": values}, headers=hub["owner"]
    )
    assert written.status_code == 200, written.text


def run_job(client) -> dict:
    """One run of the plugin's job baseline, as the hub's job worker runs it: what it did in the project."""
    app = client.app

    async def work():
        return await plugin_jobs.run_job(app.state.engine, app.state.config, "canvas", schedule.JOB)

    return client.portal.call(work)["result"]["projects"][PROJECT]


def baseline(client, headers, number: int) -> dict:
    answer = client.get(f"{BASELINES}/{number}", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def decide(client, headers, number: int, ref: str, decision: str, expect: int = 200, **body):
    answer = client.post(
        f"{BASELINES}/{number}/items/{ref}/decide", json={"decision": decision, **body}, headers=headers
    )
    assert answer.status_code == expect, answer.text
    return answer.json()


def opened(client, headers, ref: str, expect: int = 201) -> dict:
    answer = client.post(BASELINES, json={"claim": ref}, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def items(found: dict) -> dict[str, dict]:
    return {item["claim"]: item for item in found["items"]}


def told(db) -> list[tuple[str, str, str]]:
    """(login, title, link) of every canvas_baseline notice, by login."""
    n, users = tables.notifications, tables.users
    query = (
        select(users.c.login, n.c.title, n.c.link)
        .join_from(n, users, users.c.id == n.c.user_id)
        .where(n.c.notice_kind == "canvas_baseline")
        .order_by(users.c.login, n.c.id)
    )
    return [tuple(row) for row in sql(db, query)]


def events(db, kind: str) -> list[dict]:
    e = tables.hub_events
    return [row[0] for row in sql(db, select(e.c.payload).where(e.c.kind == kind).order_by(e.c.id))]


# The weekly baseline


def test_canvas_baseline_week_of_the_schedule_comes_at_its_weekday_and_time():
    friday = schedule.Schedule(WEEKDAYS.index("friday"), datetime.min.time().replace(hour=16), "UTC", "utc")
    assert schedule.week_of(datetime(2026, 10, 23, 15, 59), friday) is None  # a Friday, a minute early
    assert schedule.week_of(datetime(2026, 10, 23, 16, 0), friday) == ("2026-W43", datetime(2026, 10, 23, 16, 0))
    assert schedule.week_of(datetime(2026, 10, 25, 9, 0), friday) == ("2026-W43", datetime(2026, 10, 23, 16, 0))
    assert schedule.week_of(datetime(2026, 10, 26, 9, 0), friday) is None  # Monday: this week's is still to come
    sunday = schedule.Schedule(WEEKDAYS.index("sunday"), datetime.min.time(), "UTC", "utc")
    assert schedule.week_of(datetime(2027, 1, 3, 0, 0), sunday) == ("2026-W53", datetime(2027, 1, 3, 0, 0))


def test_canvas_baseline_schedule_takes_the_config_zone_else_the_charters_else_utc(client, hub, hub_db):
    app = client.app

    def zone_of() -> tuple:
        async def work():
            async with app.state.engine.begin() as conn:
                found = await schedule.schedule_of(conn, project_id(hub_db))
                return found.weekday, found.at.isoformat(timespec="minutes"), found.zone, found.zone_from

        return client.portal.call(work)

    assert zone_of() == (WEEKDAYS.index("friday"), "16:00", "UTC", "utc")  # the defaults
    worker = owners_worker(client, hub)
    body = {"window": {"start": "22:00", "end": "06:00", "timezone": "Asia/Ho_Chi_Minh"}}
    owner = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == "owner"))[0][0]
    charter = {"project_id": project_id(hub_db), "revision": 1, "body": body, "created_by": owner}
    sql(hub_db, insert(tables.charters).values(**charter, worker_id=worker["id"]))
    assert zone_of()[2:] == ("Asia/Ho_Chi_Minh", "charter")
    due_now(client, hub, timezone="Europe/Berlin", weekday="monday", time="09:30")
    assert zone_of() == (0, "09:30", "Europe/Berlin", "config")
    due_now(client, hub, timezone="Mars/Olympus_Mons")  # a zone Postgres does not know: the charter's
    assert zone_of()[2:] == ("Asia/Ho_Chi_Minh", "charter")


def test_canvas_baseline_job_proposes_once_a_week_tells_the_writers_and_another_writer_approves(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": CARD})
    edit(client, hub["other"], {"cell": "solution", "text": "A reorder button on the delivery note"})
    due_now(client, hub)
    assert run_job(client) == {"baselines": 1, "proposed": 1, "told": 2}
    assert run_job(client) == {}, "one a week, however often the job runs"
    week = "{}-W{:02d}".format(*datetime.now(UTC).date().isocalendar()[:2])
    (listed_one,) = client.get(BASELINES, headers=hub["reader"]).json()
    assert (listed_one["number"], listed_one["kind"], listed_one["week"], listed_one["status"]) == (
        1,
        "weekly",
        week,
        "proposed",
    )
    assert (listed_one["time_zone"], listed_one["item_count"], listed_one["waiting"]) == ("UTC", 2, 1)
    found = items(baseline(client, hub["other"], 1))
    changed = found["CL-1"]
    assert (changed["kind"], changed["from_status"], changed["at_status"], changed["to_status"]) == (
        "status",
        "assumption",
        "testing",
        "testing",
    )
    assert (changed["proposers"], changed["decisions"]) == (["chi"], ["accepted", "rejected", "deferred"])
    new = found["CL-2"]
    assert (new["kind"], new["decision"], new["decided_by"], new["decisions"]) == ("new", "included", None, [])
    assert items(baseline(client, hub["chi"], 1))["CL-1"]["decisions"] == [], "chi changed it"
    assert items(baseline(client, hub["reader"], 1))["CL-1"]["decisions"] == [], "a reader decides nothing"
    link = f"/p/{PROJECT}/x/canvas/canvases/northwind-pantry/baselines/1"
    title = f"Baseline 1 of canvas northwind-pantry in {PROJECT} waits for your review"
    assert told(hub_db) == [("owner", title, link), ("someone-else", title, link)]  # not chi, nor the reader

    refused = decide(client, hub["chi"], 1, "CL-1", "accepted", expect=403)
    assert "another writer decides it" in refused["message"]
    decide(client, hub["reader"], 1, "CL-1", "accepted", expect=403)
    decide(client, hub["other"], 1, "CL-2", "accepted", expect=422)  # a new claim joins as it is
    approved = decide(client, hub["other"], 1, "CL-1", "accepted")
    assert (approved["status"], approved["approved_by"], approved["waiting"]) == ("approved", ["someone-else"], 0)
    snapshot = {entry["claim"]: entry for entry in approved["snapshot"]}
    assert {code: (entry["rev"], entry["status"]) for code, entry in snapshot.items()} == {
        "CL-1": (2, "testing"),
        "CL-2": (1, "assumption"),
    }
    assert all(len(entry["sha256"]) == 64 for entry in snapshot.values())
    (approval,) = events(hub_db, "canvas.baseline")
    assert approval["transitions"] == [
        {
            "claim_id": snapshot["CL-1"]["claim_id"],
            "claim": "CL-1",
            "from": "assumption",
            "to": "testing",
            "evidence": [],
        },
        {"claim_id": snapshot["CL-2"]["claim_id"], "claim": "CL-2", "from": None, "to": "assumption", "evidence": []},
    ]
    assert (approval["v"], approval["baseline"], approval["kind"], approval["approved_by"]) == (
        1,
        1,
        "weekly",
        ["someone-else"],
    )

    # approved never changes: no decision, and a later edit leaves the snapshot as it was
    decide(client, hub["owner"], 1, "CL-1", "rejected", expect=409)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "text": "Corner shops run out between deliveries"})
    assert baseline(client, hub["reader"], 1)["snapshot"] == approved["snapshot"]


def test_canvas_baseline_rejecting_takes_the_claim_back_with_a_rev_of_kind_baseline_that_undoes(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": CARD})
    due_now(client, hub)
    run_job(client)
    decided = decide(client, hub["owner"], 1, "CL-1", "rejected", reason="no test planned yet")
    assert decided["status"] == "approved"
    item = items(decided)["CL-1"]
    assert (item["decision"], item["decided_by"], item["reason"], item["rev"]) == (
        "rejected",
        "owner",
        "no test planned yet",
        3,
    )
    claim = claim_of(client, hub["reader"], "CL-1")
    last = claim["revs"][-1]
    assert (claim["status"], last["change"], last["baseline_id"], last["provenance"]["login"]) == (
        "assumption",
        "baseline",
        sql(hub_db, select(tables.canvas.baselines.c.id))[0][0],
        "owner",
    )
    assert last["reason"] == "baseline 1: CL-1 rejected: no test planned yet"
    assert decided["snapshot"][0]["status"] == "assumption"
    assert [moved["to"] for moved in events(hub_db, "canvas.baseline")[0]["transitions"]] == []
    # the rev is a rev like any other: chi takes it back
    undone = client.post(f"{CANVAS}/undo", json={"revs": [{"claim": "CL-1", "rev": 3}]}, headers=hub["chi"])
    assert undone.status_code == 200, undone.text
    assert claim_of(client, hub["reader"], "CL-1")["status"] == "testing"


def test_canvas_baseline_runs_the_rule_of_validated_again_and_defers(client, hub, hub_db):
    added = client.post(
        f"{CANVAS}/claims/CL-1/evidence",
        json={"kind": "interview", "direction": "supports", "summary": "Seven of ten shop owners reorder weekly"},
        headers=hub["chi"],
    )
    assert added.status_code == 201, added.text
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "validated"})
    due_now(client, hub)
    run_job(client)
    item = items(baseline(client, hub["owner"], 1))["CL-1"]
    assert (item["to_status"], [piece["id"] for piece in item["evidence"]]) == ("validated", [added.json()["id"]])
    withdrawn = client.post(
        f"{CANVAS}/claims/CL-1/evidence/{added.json()['id']}/withdraw", json={"reason": "made up"}, headers=hub["chi"]
    )
    assert withdrawn.status_code == 200, withdrawn.text
    refused = decide(client, hub["owner"], 1, "CL-1", "accepted", expect=422)
    assert "goes to validated only with real evidence" in refused["message"]
    deferred = decide(client, hub["owner"], 1, "CL-1", "deferred")
    assert deferred["status"] == "approved" and deferred["snapshot"][0]["status"] == "assumption"
    assert claim_of(client, hub["reader"], "CL-1")["status"] == "validated", "deferring writes nothing"


def test_canvas_baseline_asks_about_the_value_checks_proposal_any_writer_but_the_changer_accepts(client, hub, hub_db):
    add_key(client, hub, NUMBER_KEY, "number")
    card = {**CARD, "metric": NUMBER_KEY, "aggregate": "mean", "comparator": ">=", "threshold": 0.3}
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": card})
    push(client, hub["other"], {"key": NUMBER_KEY, "value": 0.5, "tenant": "northwind"})
    assert checked(client, hub_db)["proposals"] == 1
    (proposal,) = listed(client, hub["reader"], "open")
    due_now(client, hub)
    run_job(client)
    item = items(baseline(client, hub["owner"], 1))["CL-1"]
    assert (item["kind"], item["from_status"], item["at_status"], item["to_status"], item["proposal_id"]) == (
        "value_check",
        "assumption",
        "testing",
        "validated",
        proposal["id"],
    )
    assert item["proposers"] == ["chi"], "chi moved it to testing on the way"
    approved = decide(client, hub["owner"], 1, "CL-1", "accepted")
    claim = claim_of(client, hub["reader"], "CL-1")
    assert (claim["status"], claim["revs"][-1]["change"], claim["revs"][-1]["provenance"]["login"]) == (
        "validated",
        "baseline",
        "owner",
    )
    (accepted,) = listed(client, hub["reader"], "accepted")
    assert (accepted["id"], accepted["decided_by"], accepted["rev"]) == (proposal["id"], "owner", claim["rev"])
    assert approved["snapshot"][0]["status"] == "validated"
    (moved,) = events(hub_db, "canvas.baseline")[0]["transitions"]
    assert (moved["from"], moved["to"], moved["evidence"]) == ("assumption", "validated", item_evidence(item))


def item_evidence(item: dict) -> list[int]:
    return [piece["id"] for piece in item["evidence"]]


def test_canvas_baseline_extends_or_retires_an_overdue_test_card_any_writer_decides(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "test_card": {**CARD, "deadline": GONE}})
    edit(
        client,
        hub["chi"],
        {"cell": "channels", "text": "Sales reps visit monthly", "test_card": {**CARD, "deadline": GONE}},
    )
    edit(
        client,
        hub["chi"],
        {"cell": "channels", "text": "Shops order by phone", "test_card": {**CARD, "deadline": LATER}},
    )
    due_now(client, hub)
    run_job(client)
    found = items(baseline(client, hub["chi"], 1))
    assert {code: (item["kind"], item["deadline"]) for code, item in found.items()} == {
        "CL-1": ("overdue", GONE),
        "CL-2": ("overdue", GONE),
        "CL-3": ("new", None),
    }
    assert found["CL-1"]["decisions"] == ["extended", "retired", "deferred"], "the hub proposed it: chi decides too"
    decide(client, hub["chi"], 1, "CL-1", "extended", expect=422)  # an extension takes its new deadline
    decide(client, hub["chi"], 1, "CL-1", "extended", deadline=TODAY.isoformat(), expect=422)
    decide(client, hub["chi"], 1, "CL-1", "accepted", expect=422)  # an overdue card is extended or retired
    decide(client, hub["chi"], 1, "CL-1", "extended", deadline=LATER)
    assert claim_of(client, hub["reader"], "CL-1")["test_card"]["deadline"] == LATER
    approved = decide(client, hub["other"], 1, "CL-2", "retired")
    assert {entry["claim"]: entry["status"] for entry in approved["snapshot"]} == {
        "CL-1": "assumption",
        "CL-2": "retired",
        "CL-3": "assumption",
    }
    assert claim_of(client, hub["reader"], "CL-2")["status"] == "retired"
    transitions = events(hub_db, "canvas.baseline")[0]["transitions"]
    assert [(moved["claim"], moved["from"], moved["to"]) for moved in transitions] == [
        ("CL-2", "assumption", "retired"),
        ("CL-3", None, "assumption"),
    ]


# A baseline at once, the next week's, and who never decides


def test_canvas_baseline_opened_at_once_waits_for_another_writer_and_the_next_week_expires_it(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": CARD})
    edit(client, hub["chi"], {"cell": "solution", "text": "A reorder button"})
    opened(client, hub["reader"], "CL-1", expect=403)
    opened(client, hub["chi"], "CL-2", expect=409)  # asks nothing: an assumption that no snapshot holds yet
    made = opened(client, hub["chi"], "CL-1")
    assert (made["kind"], made["number"], made["created_by"], made["week"]) == ("push", 1, "chi", None)
    assert list(items(made)) == ["CL-1"] and items(made)["CL-1"]["proposers"] == ["chi"]
    opened(client, hub["other"], "CL-1", expect=409)  # baseline 1 holds it
    assert [login for login, _, _ in told(hub_db)] == ["owner", "someone-else"]
    # the week's comes before anyone decided: it expires baseline 1 and asks CL-1 again
    due_now(client, hub)
    assert run_job(client) == {"baselines": 1, "expired": 1, "proposed": 1, "told": 2}
    assert [row["status"] for row in client.get(BASELINES, headers=hub["reader"]).json()] == ["proposed", "expired"]
    decide(client, hub["other"], 1, "CL-1", "accepted", expect=409)  # expired takes no decision
    assert set(items(baseline(client, hub["reader"], 2))) == {"CL-1", "CL-2"}


def self_approving(client, hub, body: dict) -> None:
    """The project's layer of the plugin's config, as its admin writes it: ``body`` sets or unsets self_approval."""
    written = client.post(f"/v1/projects/{PROJECT}/plugins/canvas/config", json=body, headers=hub["owner"])
    assert written.status_code == 200, written.text


def test_canvas_baseline_with_self_approval_on_the_proposer_decides_and_the_item_says_who_did(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": CARD})
    self_approving(client, hub, {"values": {"self_approval": True}})
    made = opened(client, hub["chi"], "CL-1")  # chi changed CL-1's status and opened the baseline
    assert (items(made)["CL-1"]["proposers"], items(made)["CL-1"]["decisions"]) == (
        [],
        ["accepted", "rejected", "deferred"],
    )
    assert [login for login, _, _ in told(hub_db)] == ["chi", "owner", "someone-else"]  # chi may decide, so is told
    i, users = tables.canvas.baseline_items, tables.users
    (proposers,) = sql(hub_db, select(i.c.proposers))[0]
    assert sql(hub_db, select(users.c.login).where(users.c.id.in_(proposers))) == [("chi",)], "kept as proposed"

    # turned off again, the four-eyes rule is back for the same item
    self_approving(client, hub, {"unset": ["self_approval"]})
    assert items(baseline(client, hub["chi"], 1))["CL-1"]["decisions"] == []
    assert items(baseline(client, hub["reader"], 1))["CL-1"]["proposers"] == ["chi"]
    assert "another writer decides it" in decide(client, hub["chi"], 1, "CL-1", "accepted", expect=403)["message"]

    # on: every other rule stays (a reader decides nothing), and chi's decision is recorded as chi's
    self_approving(client, hub, {"values": {"self_approval": True}})
    decide(client, hub["reader"], 1, "CL-1", "accepted", expect=403)
    approved = decide(client, hub["chi"], 1, "CL-1", "accepted", reason="the reorder test starts Monday")
    decided = items(approved)["CL-1"]
    assert (approved["status"], approved["approved_by"], approved["waiting"]) == ("approved", ["chi"], 0)
    assert (decided["decision"], decided["decided_by"], decided["reason"], decided["decisions"]) == (
        "accepted",
        "chi",
        "the reorder test starts Monday",
        [],
    )
    (approval,) = events(hub_db, "canvas.baseline")
    assert approval["approved_by"] == ["chi"]
    assert [(moved["claim"], moved["from"], moved["to"]) for moved in approval["transitions"]] == [
        ("CL-1", "assumption", "testing")
    ]


def test_canvas_baseline_is_never_decided_by_an_agent_or_a_run(client, hub, hub_db):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": CARD})
    made = opened(client, hub["chi"], "CL-1")
    scope = RunScope(run_id=7, project=PROJECT, role="writer", max_level="internal")
    agent = Principal(602, "someone-else", False, 9, "worker", "a" * 64, scope=scope)
    with pytest.raises(CanvasError) as refused:
        server._no_run(agent, "decides an item of a baseline")
    assert refused.value.status == 403 and "the agent of a run never decides" in refused.value.message
    as_agent = writes.Writer(None, writes.Author("agent", 1, run_id=7))
    with pytest.raises(CanvasError) as refused:
        asyncio.run(
            decisions.decide(None, as_agent, None, made["number"], decisions.Decision("CL-1", "accepted"), TODAY)
        )
    assert refused.value.status == 403
    assert not [name for name in TOOLS if "baseline" in name or "decide" in name], "@strategist has no such tool"

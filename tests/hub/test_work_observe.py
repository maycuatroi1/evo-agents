"""What the Observe of an agent system reads of its work (``evo_agents.hub.server.work_observe``; plan work-chats,
step 4): ``work set`` moves the trace so its system's trace stream sends it again, a trace carries its timeline by
agent (each run cut into queued, running, parked and waiting, and the decisions that ask a person), and the numbers of
the system's work over a window count what is open, needs a person, failed and is done, the time to close and the
cost.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select

from evo_agents.hub import tables
from evo_agents.hub.server import system_traces
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401
    OWNER,
    app_key,
    client,
    hub,
    in_chats,
    moved,
)
from tests.hub.test_system_traces import traces_path
from tests.hub.test_work_chats import asked, claimed, desk, opened, rows, runs_of_trace, state_to

SUMMARY = traces_path().rsplit("/", 1)[0] + "/work/summary"


def last_at(db, trace_id: int):
    t = tables.system_traces
    return rows(db, select(t.c.last_at).where(t.c.id == trace_id))[0][0]


def test_work_set_moves_the_trace_and_wakes_the_trace_stream_of_its_system(client, hub, hub_db, monkeypatch):
    studio, _, _, _, parent = desk(client, hub)
    asked(client, studio, parent, "fixer")
    trace_id, _ = opened(hub_db)
    (system_id,) = rows(hub_db, select(tables.system_traces.c.system_id))[0]
    woken: list[int | None] = []
    notify = system_traces.notify

    async def spy(conn, system: int | None) -> None:
        woken.append(system)
        await notify(conn, system)

    monkeypatch.setattr(system_traces, "notify", spy)
    for body in ({"title": "Retry loop of the wait helper"}, {"title": "Retry loop of the wait helper"}):
        before = last_at(hub_db, trace_id)
        done = in_chats(client, studio, parent, "work", body)
        assert done.status_code == 200, done.text
        assert last_at(hub_db, trace_id) > before, "the trace moved, its card changed or not"
        assert woken and woken[-1] == system_id, "the stream of its system is woken"
        woken.clear()


def test_a_trace_carries_its_timeline_by_agent_with_the_decisions_that_ask_a_person(client, hub, hub_db):
    studio, fixer, _, _, parent = desk(client, hub)
    child = asked(client, studio, parent, "fixer")["run_id"]
    trace_id, _ = opened(hub_db)
    detail = client.get(f"{traces_path()}/{trace_id}", headers=hub["owner"])
    assert detail.status_code == 200, detail.text
    timeline = detail.json()["timeline"]
    lanes = {lane["run_id"]: lane for lane in timeline["runs"]}
    assert [lane["run_id"] for lane in timeline["runs"]] == runs_of_trace(hub_db, trace_id)
    assert lanes[parent]["agent"] == "reviewer" and lanes[child]["agent"] == "fixer"
    assert [s["kind"] for s in lanes[child]["segments"]] == ["queued"], "not taken yet"
    assert lanes[child]["segments"][0]["ended_at"] is None
    assert [s["kind"] for s in lanes[parent]["segments"]][-1] == "running"
    assert lanes[parent]["segments"][-1]["ended_at"] is None
    assert timeline["marks"] == []

    assert claimed(client, fixer) == child
    moved(client, fixer, child, "running", session_id="sess-fix")
    r = tables.runs
    sql(hub_db, r.update().where(r.c.id == child).values(state="waiting", waiting_since=func.now()))
    sql(hub_db, r.update().where(r.c.id == parent).values(state="parked", parked_at=func.now()))
    (owner_id,) = rows(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0]
    (project_id,) = rows(hub_db, select(tables.system_traces.c.project_id))[0]
    d = tables.decisions
    question = {
        "run_id": child,
        "project_id": project_id,
        "category": "triage",
        "question": "Keep the old policy?",
        "options": [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}],
        "asked_of": owner_id,
    }
    (decision_id,) = rows(hub_db, insert(d).values(**question).returning(d.c.id))[0]
    sql(hub_db, insert(d).values(**{**question, "asked_of": None}).returning(d.c.id))  # asks nobody: no mark

    timeline = client.get(f"{traces_path()}/{trace_id}", headers=hub["owner"]).json()["timeline"]
    lanes = {lane["run_id"]: lane for lane in timeline["runs"]}
    assert [s["kind"] for s in lanes[child]["segments"]] == ["queued", "running", "waiting"]
    assert [s["ended_at"] is None for s in lanes[child]["segments"]] == [False, False, True]
    parked = lanes[parent]["segments"]
    assert [s["kind"] for s in parked][-2:] == ["running", "parked"] and parked[-1]["ended_at"] is None
    assert lanes[parent]["state"] == "parked"
    ((mark),) = timeline["marks"]
    assert (mark["decision_id"], mark["run_id"], mark["agent"], mark["login"]) == (decision_id, child, "fixer", OWNER)
    assert (mark["state"], mark["answered_at"]) == ("open", None)

    state_to(hub_db, runs_of_trace(hub_db, trace_id), "done")
    sql(
        hub_db,
        d.update()
        .where(d.c.id == decision_id)
        .values(state="answered", answer_option="yes", answered_by=owner_id, answered_at=func.now()),
    )
    timeline = client.get(f"{traces_path()}/{trace_id}", headers=hub["owner"]).json()["timeline"]
    assert all(s["ended_at"] is not None for lane in timeline["runs"] for s in lane["segments"]), "all ended"
    assert timeline["marks"][0]["answered_at"] is not None


def test_the_work_summary_counts_the_work_of_the_window(client, hub, hub_db):
    studio, fixer, _, _, parent = desk(client, hub)
    child = asked(client, studio, parent, "fixer")["run_id"]
    trace_id, _ = opened(hub_db)
    assert claimed(client, fixer) == child
    moved(client, fixer, child, "running", session_id="sess-fix")

    def summary(**params) -> dict:
        response = client.get(SUMMARY, params=params, headers=hub["owner"])
        assert response.status_code == 200, response.text
        return response.json()

    shown = summary()
    assert shown["hours"] == 24 and (shown["traces"], shown["withheld"]) == (1, 0)
    assert (shown["open"], shown["needs_person"], shown["failed"], shown["done"]) == (1, 0, 0, 0)
    assert shown["closed_p50_seconds"] is None and shown["usd"] >= 0

    state_to(hub_db, [child], "done")
    state_to(hub_db, [parent], "failed")
    shown = summary(hours=1)
    assert (shown["open"], shown["failed"], shown["done"]) == (0, 1, 0)

    state_to(hub_db, [parent], "done")
    shown = summary()
    assert (shown["open"], shown["failed"], shown["done"]) == (0, 0, 1)
    assert shown["closed_p50_seconds"] is not None and shown["closed_p50_seconds"] >= 0

    t = tables.system_traces
    sql(hub_db, t.update().where(t.c.id == trace_id).values(last_at=func.now() - func.make_interval(0, 0, 0, 2)))
    assert summary()["traces"] == 0, "moved two days ago, no run active: out of the window"
    assert summary(hours=72)["traces"] == 1
    assert client.get(SUMMARY, params={"hours": 0}, headers=hub["owner"]).status_code == 422
    assert client.get(SUMMARY, headers=hub["stranger"]).status_code == 404, "no grant"

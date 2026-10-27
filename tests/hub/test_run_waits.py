"""A plan run waits for a time or an event of the hub instead of asking its owner when it came, and a decision stays
open on an option that says not yet (``evo_agents.hub.server.run_waits``, ``decisions._hold``).

The checks: a wait is an open decision of category wait that notifies nobody and lets the run wait; the reaper wakes it
when its time comes, or sooner on an event of its project of its kind whose payload contains what it names, written
after it was asked, and never on another; a run that waits only for a wait that wakes later than the minutes a run keeps
its slot is parked at once, so it holds no worker, and one that wakes sooner keeps the slot; a parked run resumes, by
the reaper, when its wait wakes, and is not cancelled while its wait has not come; the owner may wake it sooner; the
route refuses a wait it cannot keep; and an option that waits leaves the decision open, tells the agent nothing and
takes no words."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import hub_events, runs, tables
from evo_agents.hub.server.app import create_app
from tests.hub.live import sql
from tests.hub.test_decisions import (
    OPTIONS,
    SESSION,
    answer,
    answered,
    asked,
    decision,
    event_bodies,
    inbox,
    last_reason,
    park,
    run_view,
    set_run,
)
from tests.hub.test_plan_runs import held_plan, plan_body, push, reported, started
from tests.hub.test_runs import PROJECT, audit_rows, claim, members, moved, moves, recover

REASON = "Baseline W44 of canvas evo-agents, with CL-90 and CL-91, is approved on 2026-10-30."


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    return headers


def ask_wait(client, worker: dict, run_id: int, **body):
    return client.post(
        f"/v1/worker/runs/{run_id}/waits", json={"reason": REASON, "step_key": "2", **body}, headers=worker["headers"]
    )


def waited(client, worker: dict, run_id: int, **body) -> dict:
    response = ask_wait(client, worker, run_id, **body)
    assert response.status_code == 201, response.text
    return response.json()


def soon(hours: float = 1) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def project_id(hub_db) -> int:
    p = tables.projects
    return sql(hub_db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


def record_event(client, hub_db, kind: str, payload: dict) -> int:
    """An event of the project in hub_events, as a core plugin's emit writes it."""

    async def go():
        async with client.app.state.engine.begin() as conn:
            return await hub_events.record(conn, kind, project_id(hub_db), payload)

    return client.portal.call(go)


def came(hub_db, decision_id: int) -> None:
    """The wait's time came."""
    d = tables.decisions
    sql(hub_db, update(d).values(wake_at=func.now() - timedelta(seconds=1)).where(d.c.id == decision_id))


def a_waiting_run(client, hub, **body) -> tuple[dict, dict, dict]:
    """A plan run of fleet, started with a session, on step 2, that waits for ``body``."""
    worker, run = started(client, hub)
    moved(client, worker, run["id"], "running", session_id=SESSION)
    reported(client, worker, run["id"], 2, "in_progress")
    wait = waited(client, worker, run["id"], **body)
    assert moved(client, worker, run["id"], "waiting")["state"] == "waiting"
    return worker, run, wait


# Waits


def test_a_wait_is_an_open_decision_that_notifies_nobody_and_wakes_when_its_time_comes(client, hub, hub_db):
    until = soon(5 / 60)  # within the minutes a run keeps its slot: it is not parked
    worker, run, wait = a_waiting_run(client, hub, until=until)
    assert (wait["category"], wait["state"], wait["question"], wait["step_key"]) == ("wait", "open", REASON, "2")
    assert datetime.fromisoformat(wait["wake_at"]) == datetime.fromisoformat(until)
    assert wait["wake_on"] is None and wait["held_at"] is None
    assert [option["key"] for option in wait["options"]] == ["now", "stop"]
    assert "Its time:" in wait["context"]
    # the hub answers a wait itself: neither option is its default, so it recommends none
    assert (wait["recommended"], wait["recommended_reason"], wait["evidence"]) == (None, None, [])
    n = tables.notifications
    assert sql(hub_db, select(func.count()).select_from(n).where(n.c.decision_id == wait["id"])) == [(0,)]

    recover(client)
    assert decision(client, hub["owner"], wait["id"])["state"] == "open"  # its time has not come
    assert inbox(client, worker, run["id"]) == []
    assert run_view(client, hub["owner"], run["id"])["state"] == "waiting"

    came(hub_db, wait["id"])
    recover(client)
    woke = decision(client, hub["owner"], wait["id"])
    assert (woke["state"], woke["answered_by"], woke["answer_option"]) == ("answered", None, None)
    assert woke["answer_text"].startswith(f"Decision #{wait['id']} (wait) woke: its time came (")
    (message,) = inbox(client, worker, run["id"])
    assert message["decision_id"] == wait["id"] and message["text"] == woke["answer_text"]
    assert REASON in message["text"]
    assert run_view(client, hub["owner"], run["id"])["state"] == "waiting"  # the worker takes it from here
    recover(client)
    assert decision(client, hub["owner"], wait["id"])["answered_at"] == woke["answered_at"]  # it wakes once


def test_a_wait_wakes_on_an_event_of_its_kind_that_contains_what_it_names_and_came_after_it(client, hub, hub_db):
    contains = {"canvas": "evo-agents"}
    record_event(client, hub_db, hub_events.BASELINE, {"v": 1, "canvas": "evo-agents", "number": 1})  # before it
    worker, run, wait = a_waiting_run(client, hub, until=soon(48), on=hub_events.BASELINE, contains=contains)
    assert wait["wake_on"] == {"kind": hub_events.BASELINE, "contains": contains}
    assert "An event `canvas.baseline` of project" in wait["context"]

    record_event(client, hub_db, hub_events.CLAIM, {"v": 1, "canvas": "evo-agents", "claim": "CL-90"})  # another kind
    record_event(client, hub_db, hub_events.BASELINE, {"v": 1, "canvas": "lumen", "number": 4})  # another canvas
    recover(client)
    assert decision(client, hub["owner"], wait["id"])["state"] == "open"
    assert run_view(client, hub["owner"], run["id"])["state"] == "parked"  # it wakes in days: no slot held meanwhile

    event_id = record_event(client, hub_db, hub_events.BASELINE, {"v": 1, "canvas": "evo-agents", "number": 2})
    recover(client)
    woke = decision(client, hub["owner"], wait["id"])
    assert woke["state"] == "answered"
    assert woke["answer_text"].startswith(f"Decision #{wait['id']} (wait) woke: event canvas.baseline #{event_id} ")
    assert '"number": 2' in woke["answer_text"]
    new_id = woke["answer_run_id"]
    assert claim(client, worker)["id"] == new_id
    moved(client, worker, new_id, "running")
    (message,) = inbox(client, worker, new_id)
    assert message["decision_id"] == wait["id"]
    assert [body["decision"] for (body,) in event_bodies(hub_db, new_id, "system") if "decision" in body][-1] == {
        "id": wait["id"],
        "category": "wait",
    }


def marked(client, headers, plan_id: str, step, status: str) -> dict:
    """Step ``step`` of plan ``plan_id`` set to ``status`` as `evo-agents hub plan step` does: a PATCH of the revision
    the hub holds."""
    revision = held_plan(client, headers, plan_id)["revision"]
    body = {"section": "steps", "step": step, "updates": {"status": status}, "if_revision": revision}
    response = client.patch(f"/v1/projects/{PROJECT}/plans/{plan_id}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_wait_on_a_step_of_another_plan_wakes_once_that_step_is_done_and_on_nothing_else(client, hub, hub_db):
    push(client, hub["owner"], plan_body("northwind-launch"))  # the plan whose step 3 the run waits on
    push(client, hub["owner"], plan_body("northwind-ops"))
    # what `--contains plan=northwind-launch --contains step=3 --contains status=done` sends: 3 read as JSON
    contains = {"plan": "northwind-launch", "step": 3, "status": "done"}
    worker, run, wait = a_waiting_run(client, hub, until=soon(12), on=hub_events.PLAN_STEP, contains=contains)
    assert wait["wake_on"] == {"kind": "plan.step", "contains": {**contains, "step": "3"}}, "a step is text"

    marked(client, hub["owner"], "northwind-launch", 2, "done")  # another step
    marked(client, hub["owner"], "northwind-launch", 3, "in_progress")  # another status
    marked(client, hub["owner"], "northwind-ops", 3, "done")  # another plan
    recover(client)
    assert decision(client, hub["owner"], wait["id"])["state"] == "open"
    assert run_view(client, hub["owner"], run["id"])["state"] == "parked"

    revision = marked(client, hub["owner"], "northwind-launch", 3, "done")["revision"]
    recover(client)
    woke = decision(client, hub["owner"], wait["id"])
    assert woke["state"] == "answered"
    e = tables.hub_events
    query = select(e.c.id, e.c.payload).where(e.c.kind == "plan.step").order_by(e.c.id.desc()).limit(1)
    ((event_id, payload),) = sql(hub_db, query)
    assert payload == {
        "v": 1,
        "plan": "northwind-launch",
        "plan_id": "northwind-launch",
        "step": "3",
        "status": "done",
        "from": "in_progress",
        "to": "done",
        "revision": revision,
    }
    assert woke["answer_text"].startswith(f"Decision #{wait['id']} (wait) woke: event plan.step #{event_id} ")
    assert '"plan": "northwind-launch"' in woke["answer_text"]
    new_id = woke["answer_run_id"]
    assert claim(client, worker)["id"] == new_id
    moved(client, worker, new_id, "running")
    (message,) = inbox(client, worker, new_id)
    assert message["decision_id"] == wait["id"]


def test_a_parked_run_resumes_by_the_reaper_when_its_wait_wakes_and_stays_parked_until_then(client, hub, hub_db):
    worker, run, wait = a_waiting_run(client, hub, until=soon(100))
    parked_id = run["id"]
    recover(client)  # the first pass parks it: nobody answers a wait
    assert run_view(client, hub["owner"], parked_id)["state"] == "parked"
    assert last_reason(hub_db, parked_id) == [("it waits for a time or an event of the hub, which resumes it",)]

    set_run(hub_db, parked_id, parked_at=func.now() - timedelta(days=runs.PARKED_DAYS, seconds=1))
    recover(client)
    assert run_view(client, hub["owner"], parked_id)["state"] == "parked"  # its wait has not come

    came(hub_db, wait["id"])
    recover(client)
    woke = decision(client, hub["owner"], wait["id"])
    new_id = woke["answer_run_id"]
    assert new_id is not None and new_id > parked_id
    assert moves(hub_db, parked_id)[-1][1:] == ("parked", "done", "reaper")
    assert last_reason(hub_db, parked_id) == [(f"resumed as #{new_id}",)]
    new = run_view(client, hub["owner"], new_id)
    assert (new["state"], new["resume_of_run_id"], new["pinned_worker_id"]) == ("queued", parked_id, worker["id"])
    assert claim(client, worker)["id"] == new_id
    moved(client, worker, new_id, "running")
    (message,) = inbox(client, worker, new_id)
    assert message["decision_id"] == wait["id"]


def test_a_run_with_a_question_open_beside_its_wait_keeps_its_slot_the_minutes_any_run_does(client, hub, hub_db):
    worker, run = started(client, hub)
    moved(client, worker, run["id"], "running", session_id=SESSION)
    waited(client, worker, run["id"], until=soon(100))
    asked(client, worker, run["id"])
    moved(client, worker, run["id"], "waiting")
    recover(client)
    assert run_view(client, hub["owner"], run["id"])["state"] == "waiting"  # its owner may answer at once
    park(hub_db, run["id"])
    recover(client)
    assert last_reason(hub_db, run["id"]) == [("nobody answered its decision within 10 minutes",)]


def test_a_parked_run_whose_wait_came_is_cancelled_a_week_on_as_any(client, hub, hub_db):
    _, run, wait = a_waiting_run(client, hub, until=soon(100))
    park(hub_db, run["id"])
    recover(client)
    d = tables.decisions
    sql(hub_db, update(d).values(state="cancelled").where(d.c.id == wait["id"]))  # say its run's owner ended it
    set_run(hub_db, run["id"], parked_at=func.now() - timedelta(days=runs.PARKED_DAYS, seconds=1))
    recover(client)
    assert run_view(client, hub["owner"], run["id"])["state"] == "cancelled"


def test_the_owner_wakes_a_wait_sooner_with_an_answer(client, hub, hub_db):
    worker, run, wait = a_waiting_run(client, hub, on=hub_events.TASK_STATUS)
    late = datetime.fromisoformat(wait["wake_at"]) - datetime.now(UTC)
    assert timedelta(days=runs.MAX_WAIT_DAYS) - late < timedelta(minutes=1)  # on alone: the longest wait
    done = answered(client, hub["owner"], wait["id"], option="now", text="Approved it already.")
    assert (done["state"], done["answered_by"]) == ("answered", "owner")
    (message,) = inbox(client, worker, run["id"])
    assert message["text"].startswith(f"Answer to decision #{wait['id']} (wait): ")
    assert message["text"].endswith("Approved it already.")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"until": "2020-01-01T00:00:00Z"},
        {"until": (datetime.now(UTC) + timedelta(days=runs.MAX_WAIT_DAYS, hours=1)).isoformat()},
        {"until": "2026-10-30T09:00:00"},
        {"until": soon(), "contains": {"canvas": "x"}},
        {"on": "canvas.nothing"},
        {"on": "run.state"},
        {"until": soon(), "reason": "   "},
    ],
    ids=["neither", "past", "too-far", "naive", "contains-alone", "unknown-kind", "plugin-kind", "blank-reason"],
)
def test_a_wait_the_hub_cannot_keep_gets_422(client, hub, hub_db, body):
    worker, run = started(client, hub)
    response = ask_wait(client, worker, run["id"], **body)
    assert response.status_code == 422, response.text
    d = tables.decisions
    assert sql(hub_db, select(func.count()).select_from(d)) == [(0,)]


def test_a_wait_of_a_plugins_own_event_kind_is_taken(client, hub):
    worker, run = started(client, hub)
    assert waited(client, worker, run["id"], on="plugin.marketing.post_published")["wake_on"]["kind"] == (
        "plugin.marketing.post_published"
    )


# An option that says not yet


def test_an_option_that_waits_leaves_the_decision_open_and_tells_the_agent_nothing(client, hub, hub_db):
    worker, run = started(client, hub)
    moved(client, worker, run["id"], "running", session_id=SESSION)
    options = [*OPTIONS, {"key": "later", "label": "Not yet, I answer after the 30th", "waits": True}]
    found = asked(client, worker, run["id"], options=options)
    assert [option["waits"] for option in found["options"]] == [False, False, True]
    moved(client, worker, run["id"], "waiting")

    held = answered(client, hub["owner"], found["id"], option="later")
    assert (held["state"], held["answer_option"], held["answered_by"]) == ("open", None, None)
    assert held["held_at"] is not None
    assert inbox(client, worker, run["id"]) == []
    n = tables.notifications
    unread = select(func.count()).select_from(n).where(n.c.decision_id == found["id"], n.c.read_at.is_(None))
    assert sql(hub_db, unread) == [(0,)]
    assert audit_rows(hub_db, "decision")[-1][1].endswith(f"decision:{found['id']} run:{run['id']} option=later held")
    assert run_view(client, hub["owner"], run["id"])["state"] == "waiting"

    words = answer(client, hub["owner"], found["id"], option="later", text="Ask me after the 30th.")
    assert words.status_code == 422 and "leaves it open" in words.text
    done = answered(client, hub["owner"], found["id"], option="postgres")
    assert done["state"] == "answered"
    (message,) = inbox(client, worker, run["id"])
    assert message["text"].endswith("Chosen option: postgres, Move to Postgres.")


def test_a_decision_whose_options_all_wait_gets_422(client, hub, hub_db):
    worker, run = started(client, hub)
    options = [{**option, "waits": True} for option in OPTIONS]
    response = client.post(
        f"/v1/worker/runs/{run['id']}/decisions",
        json={"category": "scope", "question": "Later?", "options": options},
        headers=worker["headers"],
    )
    assert response.status_code == 422 and "every option" in response.text

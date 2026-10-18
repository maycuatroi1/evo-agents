"""A chat run asks its owner decisions (category ``triage``) and waits: it goes ``waiting`` on its slot, is parked after
ten minutes, and the answer, or a reply in the thread, resumes it in the same session, worktree and branch, as a new
run that goes on from the parked one (criteria a4 and a8 of the plan run-failure-triage)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.hub import runs, tables
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401  (fixtures)
    app_key,
    chat_runs_of,
    chat_worker,
    client,
    created,
    hub,
    run_of,
    sent,
    started,
)
from tests.hub.test_runs import PROJECT, claim, moved, recover, set_run

OPTIONS = [
    {"key": "retry", "label": "Retry the flaky test", "detail": "Run it three more times."},
    {"key": "skip", "label": "Skip it"},
]


def triage(client, worker: dict, run_id: int, **extra):
    body = {
        "category": "triage",
        "question": "The wait test fails twice in three. Retry or skip it?",
        "context": "## Why\nThe failure is timing related.",
        "options": OPTIONS,
        "recommended": "retry",
        **extra,
    }
    return client.post(f"/v1/worker/runs/{run_id}/decisions", json=body, headers=worker["headers"])


def waiting_chat(client, hub, worker: dict):
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    found = triage(client, worker, spec["id"])
    assert found.status_code == 201, found.text
    moved(client, worker, spec["id"], "waiting")
    return chat, mention, spec, found.json()


def answer(client, headers, decision_id: int, **body):
    return client.post(f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer", json=body, headers=headers)


def wait_for(hub_db, run_id: int, **age) -> None:
    set_run(hub_db, run_id, waiting_since=func.now() - timedelta(**age))


def test_a_chat_run_asks_triage_decisions_and_other_runs_do_not(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    _, spec = started(client, hub, worker, chat)
    assert triage(client, worker, spec["id"]).status_code == 201
    refused = triage(client, worker, spec["id"], category="architecture")
    assert refused.status_code == 422, refused.text
    assert runs.CHAT_DECISION_CATEGORY == "triage"


def test_a_chat_run_waiting_keeps_its_slot_and_the_answer_reaches_its_inbox(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    _, _, spec, found = waiting_chat(client, hub, worker)
    run = run_of(client, hub["owner"], spec["id"])
    assert run["state"] == "waiting" and run["lease_expires_at"] is not None
    assert recover(client)["parked"] == 0
    done = answer(client, hub["owner"], found["id"], option="retry", text="three more times")
    assert done.status_code == 200, done.text
    assert done.json()["answer_run_id"] == spec["id"], "a waiting run is answered in place"
    inbox = client.post(f"/v1/worker/runs/{spec['id']}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    assert len(inbox) == 1 and "retry" in inbox[0]["text"]
    assert [row[1] for row in chat_runs_of(hub_db)] == ["waiting"]


def test_a_chat_run_waiting_is_parked_after_ten_minutes_not_before(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    _, _, spec, _ = waiting_chat(client, hub, worker)
    wait_for(hub_db, spec["id"], minutes=9)
    assert recover(client)["parked"] == 0
    wait_for(hub_db, spec["id"], minutes=10, seconds=1)
    assert recover(client)["parked"] == 1
    assert run_of(client, hub["owner"], spec["id"])["state"] == "parked"
    reason = sql(
        hub_db,
        select(tables.run_events.c.body["reason"].astext)
        .where(tables.run_events.c.run_id == spec["id"], tables.run_events.c.kind == "state")
        .order_by(tables.run_events.c.seq.desc())
        .limit(1),
    )
    assert "10 minutes" in reason[0][0]


def test_the_answer_to_a_parked_chat_run_resumes_its_session_on_its_worker_and_branch(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat, mention, spec, found = waiting_chat(client, hub, worker)
    wait_for(hub_db, spec["id"], minutes=11)
    recover(client)
    done = answer(client, hub["owner"], found["id"], option="skip")
    assert done.status_code == 200, done.text
    new_id = done.json()["answer_run_id"]
    assert new_id != spec["id"]
    assert run_of(client, hub["owner"], spec["id"])["state"] == "done"
    new = run_of(client, hub["owner"], new_id)
    assert (new["kind"], new["state"], new["resume_of_run_id"], new["session_id"]) == (
        "chat",
        "queued",
        spec["id"],
        "sess-1",
    )
    assert (new["pinned_worker_id"], new["branch"], new["chat_message_id"]) == (
        worker["id"],
        f"chat/{spec['id']}",
        mention["id"],
    )
    assert claim(client, worker)["id"] == new_id
    assert [(row[0], row[1]) for row in chat_runs_of(hub_db)][0] == (spec["id"], "done")
    inbox = client.post(f"/v1/worker/runs/{new_id}/inbox", json={}, headers=worker["headers"]).json()["messages"]
    assert len(inbox) == 1 and "skip" in inbox[0]["text"]


def test_a_reply_in_the_thread_resumes_a_parked_chat_run_with_its_decision_open(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat, mention, spec, found = waiting_chat(client, hub, worker)
    wait_for(hub_db, spec["id"], minutes=11)
    recover(client)
    later = sent(client, hub["owner"], chat["id"], "just retry it", mention["id"])
    assert later["id"]
    rows = chat_runs_of(hub_db)
    assert len(rows) == 2, rows
    new_id = rows[-1][0]
    assert run_of(client, hub["owner"], spec["id"])["state"] == "done"
    assert run_of(client, hub["owner"], new_id)["resume_of_run_id"] == spec["id"]
    moved_to = sql(
        hub_db, select(tables.decisions.c.run_id, tables.decisions.c.state).where(tables.decisions.c.id == found["id"])
    )
    assert moved_to == [(new_id, "open")]


def test_a_chat_run_decision_is_in_the_overview_and_the_notifications_of_its_owner(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    _, _, _, found = waiting_chat(client, hub, worker)
    overview = client.get("/v1/me/overview", headers=hub["owner"]).json()
    asked = [d for d in overview["open_decisions"] if d["id"] == found["id"]]
    assert [(d["category"], d["yours"], d["run_state"]) for d in asked] == [("triage", True, "waiting")]
    assert overview["counts"]["waiting_on_you"] >= 1
    notes = client.get("/v1/me/notifications", headers=hub["owner"]).json()["notifications"]
    assert [(n["kind"], n["decision_state"]) for n in notes if n["decision_id"] == found["id"]] == [
        ("decision", "open")
    ]

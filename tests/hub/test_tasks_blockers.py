"""Blockers of a task (``evo_agents.hub.server.tasks.blockers``), as criterion a1 of plan pm-daily-tracking names
them: a blocker is a flag beside the status, set by a writer against a member, another task or a text, cleared or
edited later, with one row of the task's history each time; an open decision of a run that belongs to the task is a
blocker read at the time; the tasks list says which tasks are blocked and keeps them with ``?blocked=true``. A sprint
records when it turned active.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_runs import PLAN, PROJECT, dispatched
from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    BASE,
    client,
    created,
    edited,
    hub,
    members,
    shown,
    sprint,
    tasks_core,
)

OPTIONS = [{"key": "yes", "label": "Go on"}, {"key": "no", "label": "Stop here"}]


def block(client, headers, ref: str, expect: int = 201, **body) -> dict:
    answer = client.post(f"{BASE}/tasks/{ref}/blockers", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def unblock(client, headers, ref: str, blocker: int, expect: int = 200, **body) -> dict:
    answer = client.patch(f"{BASE}/tasks/{ref}/blockers/{blocker}", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def blocked(client, headers) -> list[str]:
    listed = client.get(f"{BASE}/tasks", params={"blocked": "true"}, headers=headers)
    assert listed.status_code == 200, listed.text
    return [task["ref"] for task in listed.json()]


def ask(hub_db, run_id: int) -> int:
    """An open decision of run ``run_id``."""
    d, r = tables.decisions, tables.runs
    values = {
        "run_id": r.c.id,
        "project_id": r.c.project_id,
        "plan_id": r.c.plan_id,
        "step_key": r.c.step_key,
        "category": literal("scope"),
        "question": literal("Ship the queue first?"),
        "options": literal(OPTIONS, JSONB),
    }
    found = select(*values.values()).where(r.c.id == run_id)
    return sql(hub_db, insert(d).from_select(list(values), found).returning(d.c.id))[0][0]


def test_tasks_blocker_a_writer_blocks_a_task_on_a_member_a_task_or_a_text(client, hub):
    created(client, hub["alice"], title="Cancel an order")
    created(client, hub["alice"], title="Payment webhook")
    member = block(client, hub["alice"], "T-1", reason="needs the API key", waiting_member="bao")
    assert (member["kind"], member["waiting_kind"], member["waiting_on"], member["set_by"]) == (
        "manual",
        "member",
        "bao",
        "alice",
    )
    assert member["cleared_at"] is None and member["decision_id"] is None
    other = block(client, hub["bao"], "1", reason="the webhook comes first", waiting_task="T-2")
    assert (other["waiting_kind"], other["waiting_on"]) == ("task", "T-2")
    text = block(client, hub["alice"], "T-1", reason="legal review", waiting_text="the bank")
    assert (text["waiting_kind"], text["waiting_on"]) == ("text", "the bank")

    task = shown(client, hub["eve"], "T-1")
    assert task["blocked"] is True and [b["id"] for b in task["blockers"]] == [
        member["id"],
        other["id"],
        text["id"],
    ]
    assert shown(client, hub["eve"], "T-2")["blocked"] is False
    assert blocked(client, hub["eve"]) == ["T-1"]
    flags = {t["ref"]: t["blocked"] for t in client.get(f"{BASE}/tasks", headers=hub["eve"]).json()}
    assert flags == {"T-1": True, "T-2": False}
    shown_in_history = [(e["actor"], e["from_value"], e["to_value"]) for e in task["events"] if e["field"] == "blocker"]
    assert shown_in_history[0] == ("alice", None, "waiting on bao: needs the API key")
    assert len(shown_in_history) == 3


def test_tasks_blocker_refuses_a_reader_a_member_without_a_grant_the_task_itself_and_a_missing_task(client, hub):
    created(client, hub["alice"], title="Cancel an order")
    reasons = {"reason": "stuck"}
    assert block(client, hub["eve"], "T-1", 403, **reasons, waiting_text="x")["message"]
    for body, said in (
        ({"waiting_member": "mallory"}, "no grant there: mallory"),
        ({"waiting_task": "T-1"}, "never waits on itself"),
        ({"waiting_task": "T-99"}, "no task T-99"),
        ({}, "give exactly one"),
        ({"waiting_text": "a", "waiting_member": "bao"}, "give exactly one"),
        ({"waiting_text": "   "}, "a text of 1 to"),
    ):
        refused = client.post(f"{BASE}/tasks/T-1/blockers", json={**reasons, **body}, headers=hub["alice"])
        assert refused.status_code == 422 and said in refused.json()["message"], refused.text
    assert (
        client.post(
            f"{BASE}/tasks/T-9/blockers", json={**reasons, "waiting_text": "x"}, headers=hub["alice"]
        ).status_code
        == 404
    )
    assert shown(client, hub["eve"], "T-1")["blocked"] is False


def test_tasks_blocker_is_edited_cleared_and_each_change_is_history(client, hub):
    created(client, hub["alice"], title="Cancel an order")
    created(client, hub["alice"], title="Payment webhook")
    first = block(client, hub["alice"], "T-1", reason="needs the API key", waiting_member="bao")
    second = block(client, hub["alice"], "T-1", reason="legal review", waiting_text="the bank")

    assert (
        client.patch(f"{BASE}/tasks/T-1/blockers/{first['id']}", json={"cleared": True}, headers=hub["eve"]).status_code
        == 403
    )
    moved = unblock(client, hub["bao"], "T-1", first["id"], reason="the key was lost", waiting_task="T-2")
    assert (moved["reason"], moved["waiting_kind"], moved["waiting_on"]) == ("the key was lost", "task", "T-2")
    assert unblock(client, hub["bao"], "T-1", first["id"], expect=422, waiting_task="T-1")
    unblock(client, hub["bao"], "T-1", 999, expect=404, cleared=True)
    unblock(client, hub["bao"], "T-2", first["id"], expect=404, cleared=True)  # another task's blocker

    gone = unblock(client, hub["bao"], "T-1", first["id"], cleared=True)
    assert gone["cleared_at"] is not None and gone["cleared_by"] == "bao"
    unblock(client, hub["bao"], "T-1", first["id"], expect=409, reason="again")
    task = shown(client, hub["eve"], "T-1")
    assert [b["id"] for b in task["blockers"]] == [second["id"]] and task["blocked"] is True
    unblock(client, hub["alice"], "T-1", second["id"], cleared=True)
    assert shown(client, hub["eve"], "T-1")["blocked"] is False and blocked(client, hub["eve"]) == []
    events = [(e["actor"], e["from_value"], e["to_value"]) for e in shown(client, hub["eve"], "T-1")["events"]]
    changes = [e for e in events if e[1] and "waiting on" in e[1] or e[2] and "waiting on" in e[2]]
    assert changes == [
        ("alice", None, "waiting on bao: needs the API key"),
        ("alice", None, "waiting on the bank: legal review"),
        ("bao", "waiting on bao: needs the API key", "waiting on T-2: the key was lost"),
        ("bao", "waiting on T-2: the key was lost", None),
        ("alice", "waiting on the bank: legal review", None),
    ]


def test_tasks_blocker_an_open_decision_of_a_linked_run_or_step_blocks_until_it_is_answered(client, hub, hub_db):
    [run] = dispatched(client, hub["owner"], [2])
    created(client, hub["alice"], title="By run", links={"runs": [run["id"]]})
    created(client, hub["alice"], title="By step", links={"steps": [f"{PLAN}#2"]})
    created(client, hub["alice"], title="Unrelated")
    assert blocked(client, hub["eve"]) == []
    decision = ask(hub_db, run["id"])
    assert blocked(client, hub["eve"]) == ["T-1", "T-2"]
    for ref in ("T-1", "T-2"):
        task = shown(client, hub["eve"], ref)
        [found] = task["blockers"]
        assert (found["kind"], found["id"], found["decision_id"], found["run_id"]) == (
            "decision",
            None,
            decision,
            run["id"],
        )
        assert (found["waiting_kind"], found["waiting_on"], found["reason"]) == (
            "member",
            "owner",
            "Ship the queue first?",
        )
        assert found["since"] and found["set_by"] is None and task["blocked"] is True
    assert shown(client, hub["eve"], "T-3")["blocked"] is False
    block(client, hub["alice"], "T-1", reason="and the key", waiting_text="ops")
    assert [b["kind"] for b in shown(client, hub["eve"], "T-1")["blockers"]] == ["manual", "decision"]

    answered = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision}/answer", json={"option": "yes"}, headers=hub["owner"]
    )
    assert answered.status_code == 200, answered.text
    assert blocked(client, hub["eve"]) == ["T-1"]  # only the blocker set by hand is left
    assert [b["kind"] for b in shown(client, hub["eve"], "T-2")["blockers"]] == []


def test_tasks_blocker_sprint_records_when_it_turned_active(client, hub):
    planned = sprint(client, hub["alice"], "Sprint 6")
    assert planned["activated_at"] is None
    active = sprint(client, hub["alice"], "Sprint 7", "active")
    assert active["activated_at"] is not None
    started = client.patch(f"{BASE}/sprints/Sprint%206", json={"state": "closed"}, headers=hub["alice"]).json()
    assert started["activated_at"] is None and started["closed_at"] is not None
    client.patch(f"{BASE}/sprints/Sprint%207", json={"state": "closed"}, headers=hub["alice"])
    reopened = sprint(client, hub["alice"], "Sprint 8")
    on = client.patch(f"{BASE}/sprints/Sprint%208", json={"state": "active"}, headers=hub["alice"]).json()
    assert reopened["activated_at"] is None and on["activated_at"] is not None
    kept = client.patch(f"{BASE}/sprints/Sprint%208", json={"goal": "ship it"}, headers=hub["alice"]).json()
    assert kept["activated_at"] == on["activated_at"]


@pytest.mark.parametrize(
    "change",
    [
        lambda k: update(k.blockers).values(waiting_text="the bank"),  # a member and a text
        lambda k: update(k.blockers).values(waiting_user_id=None),  # nobody
        lambda k: update(k.blockers).values(waiting_task_id=k.blockers.c.task_id, waiting_user_id=None),  # itself
        lambda k: update(k.blockers).values(reason=""),
        lambda k: update(k.blockers).values(cleared_at=k.blockers.c.since),  # cleared by nobody
    ],
    ids=["two-sides", "no-side", "itself", "reason", "cleared-unsigned"],
)
def test_tasks_blocker_table_refuses_rows_its_checks_forbid(client, hub, hub_db, change):
    created(client, hub["alice"], title="Cancel an order")
    block(client, hub["alice"], "T-1", reason="needs the API key", waiting_member="bao")
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, change(tables.tasks))

"""Tier 0 of the queue of operations events (plan hub-steward, criterion a6; ``evo_agents.hub.server.ops_rules``): a
chat or task run that failed because of its machine is handed again once its worker is healthy, and only once; a run
an Orchestrator took, or that failed of its work, is not; notices whose work is done are marked read. No model.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, func, insert, select, update

from evo_agents.hub import hub_events, runs, tables, triage_rules
from evo_agents.hub.server.ops_rules import ORCHESTRATOR, REDELIVER, healthy
from tests.hub.account_pool_setup import TOKEN_VAR
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401 (fixtures)
    EAGER,
    OWNER,
    app_key,
    chat_worker,
    client,
    created,
    hub,
    started,
    thread,
)
from tests.hub.test_ops_batches import project_id, record, run_pass, system
from tests.hub.test_runs import claim, moved, set_run
from tests.hub.test_task_runs import enabled, handed, node, plugin_key, set_up, user_id

CHECKOUT = {"error": "git fetch of evo-agents failed: no route to host", "failure_cause": "checkout"}


def health_beat(client, worker: dict, free_gb: float = 120.5, login: str = "ok") -> None:
    """A heartbeat of ``worker`` that reports its health: ``free_gb`` of free disk, every runtime's login ``login``."""
    health = {"disk": {"free_gb": free_gb, "free_pct": 40.0}, "logins": dict.fromkeys(worker["runtimes"], login)}
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "run_kinds": list(runs.RUN_KINDS),
        "features": worker.get("features", list(EAGER)),
        "health": health,
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text


def items(db) -> list[tuple]:
    i = tables.ops_items
    query = select(i.c.run_id, i.c.state, i.c.handled_by).where(i.c.run_id.is_not(None)).order_by(i.c.id)
    return sql(db, query)


def started_by(db, message_id: int) -> list[tuple]:
    r = tables.runs
    return sql(db, select(r.c.id, r.c.state).where(r.c.chat_message_id == message_id))


def test_a_chat_run_failed_of_its_machine_is_said_again_in_its_thread_once_its_worker_is_healthy(client, hub, hub_db):
    system(hub_db, {"enabled": True})
    worker = chat_worker(client, hub["owner"], "studio")
    run_pass(client)
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, worker, chat)
    moved(client, worker, spec["id"], "failed", **CHECKOUT)
    assert run_pass(client)["redelivered"] == 0, "no heartbeat since the failure"
    health_beat(client, worker, free_gb=5.0)
    assert run_pass(client)["redelivered"] == 0, "its disk is under the reserve"
    health_beat(client, worker)
    assert run_pass(client)["redelivered"] == 1
    said = thread(client, hub["owner"], chat["id"], mention["id"])[-1]
    assert said["author"]["kind"] == "hub" and said["body"].endswith("\n\n@reviewer")
    ((again, state),) = started_by(hub_db, said["id"])
    assert state == "queued"
    assert (spec["id"], "handled", REDELIVER) in items(hub_db)
    health_beat(client, worker)
    assert run_pass(client)["redelivered"] == 0, "handed again once"

    assert claim(client, worker)["id"] == again
    moved(client, worker, again, "running", "failed", **CHECKOUT)
    health_beat(client, worker)
    assert run_pass(client)["redelivered"] == 0, "the work was handed again before: the Orchestrator's"
    assert (again, "pending", None) in items(hub_db)
    bodies = [m["body"] for m in thread(client, hub["owner"], chat["id"], mention["id"])]
    assert sum("the hub hands the work again" in body for body in bodies) == 1


def test_a_login_a_hub_account_stands_for_is_no_fault_of_the_worker(client, hub, hub_db):
    system(hub_db, {"enabled": True})
    worker = chat_worker(client, hub["owner"], "studio")
    run_pass(client)
    chat = created(client, hub["owner"])
    _, spec = started(client, hub, worker, chat)
    set_run(hub_db, spec["id"], runtime="claude-code")
    ra = tables.run_accounts
    sql(hub_db, delete(ra).where(ra.c.run_id == spec["id"]))  # a run that took no hub account
    moved(client, worker, spec["id"], "failed", **CHECKOUT)
    health_beat(client, worker, login="unauthorized")
    assert run_pass(client)["redelivered"] == 0, "the run logs in with the machine's login, which is out"

    _, second = started(client, hub, worker, created(client, hub["owner"]))
    set_run(hub_db, second["id"], runtime="claude-code")
    sec = tables.secrets
    (secret_id,) = sql(hub_db, select(sec.c.id).where(sec.c.name == "token-reviewer"))[0]  # the fixture's account
    sql(hub_db, insert(ra).values(run_id=second["id"], env_var=TOKEN_VAR, secret_id=secret_id, how="pool"))
    moved(client, worker, second["id"], "failed", **CHECKOUT)
    health_beat(client, worker, login="unauthorized")
    assert run_pass(client)["redelivered"] == 1, "its lease gave it a hub account: the machine's login is no matter"
    health = {"worker_id": worker["id"], "disk": "ok", "logins": {"claude-code": "unauthorized", "codex": "missing"}}
    w = tables.workers
    offered = {**worker["runtimes"], "codex": {"available": True}}  # codex taken off it would be no item either
    sql(hub_db, update(w).values(runtimes=offered).where(w.c.id == worker["id"]))
    record(client, hub_db, hub_events.WORKER_HEALTH, **health)
    run_pass(client)
    b = tables.ops_batches
    keys = [key for (key,) in sql(hub_db, select(b.c.key).order_by(b.c.id))]
    assert keys[-1] == f"worker.health|login:codex:missing|worker:{worker['id']}", "the leased login is no item"


def test_a_run_an_orchestrator_took_or_that_failed_of_its_work_is_not_handed_again(client, hub, hub_db):
    system(hub_db, {"enabled": True})
    worker = chat_worker(client, hub["owner"], "studio")
    run_pass(client)
    chat = created(client, hub["owner"])
    taken, unanswered, work = [], [], []  # (message, run) of each
    for into, body in ((taken, CHECKOUT), (unanswered, CHECKOUT), (work, {"error": "the tests fail"})):
        mention, spec = started(client, hub, worker, chat, "@reviewer please look at the wait test")
        moved(client, worker, spec["id"], "failed", **body)
        into.extend([mention["id"], spec["id"]])
    s, t = tables.agent_systems, tables.orchestrator_threads
    ((system_id,),) = sql(hub_db, select(s.c.id).where(s.c.project_id == project_id(hub_db), s.c.is_default))
    for (_, run_id), outcome in ((taken, "answer"), (unanswered, None)):
        row = {"system_id": system_id, "project_id": project_id(hub_db), "subject": "@reviewer"}
        key = f"{triage_rules.FAILED}:run:{run_id}"
        values = {**row, "subject_run_id": run_id, "rule": triage_rules.FAILED, "key": key, "outcome": outcome}
        sql(hub_db, insert(t).values(**values))
    health_beat(client, worker)
    found = run_pass(client)
    assert (found["redelivered"], found["orchestrated"]) == (1, 1)
    assert (taken[1], "handled", ORCHESTRATOR) in items(hub_db)
    assert (unanswered[1], "handled", REDELIVER) in items(hub_db), "a thread with no outcome and no run hands it again"
    assert (work[1], "pending", None) in items(hub_db), "a failure of the work is the Orchestrator's"
    assert started_by(hub_db, taken[0]) == [(taken[1], "failed")]


@pytest.fixture
def plugin_hub(client, github, hub_db) -> dict:
    return set_up(client, github, hub_db)


def notice(db, user: int, kind: str, run_id: int | None) -> int:
    n = tables.notifications
    values = {"user_id": user, "kind": "notice", "notice_kind": kind, "project_id": project_id(db), "run_id": run_id}
    ((found,),) = sql(db, insert(n).values(**values, title=kind).returning(n.c.id))
    return found


def unread(db) -> list[str]:
    n = tables.notifications
    query = select(n.c.notice_kind).where(n.c.read_at.is_(None), n.c.notice_kind.in_(("run_failed", "curator_paused")))
    return sorted(sql(db, query))


def test_a_task_failed_of_its_machine_is_handed_again_for_its_plugin_and_done_notices_are_read(
    client, plugin_hub, hub_db
):
    system(hub_db, {"enabled": True})
    enabled(client, plugin_hub["owner"])
    laptop = node(client, hub_db, plugin_hub["other"], "laptop")
    first = handed(client, plugin_key(client, plugin_hub["plugin_id"]))["run_id"]
    run_pass(client)
    assert claim(client, laptop)["id"] == first
    moved(client, laptop, first, "running", "failed", **{**CHECKOUT, "failure_cause": "network"})
    owner = user_id(hub_db, "someone-else")
    notice(hub_db, owner, "run_failed", first)
    s = tables.schedules
    ((schedule,),) = sql(
        hub_db,
        insert(s)
        .values(
            project_id=project_id(hub_db),
            kind="night_shift",
            owner_id=owner,
            paused_at=func.now(),
            pause_reason="jobs went wrong",
        )
        .returning(s.c.id),
    )
    notice(hub_db, owner, "curator_paused", None)
    health_beat(client, laptop)
    assert run_pass(client)["redelivered"] == 1
    r = tables.runs
    ((second, kind, branch, state),) = sql(
        hub_db, select(r.c.id, r.c.kind, r.c.branch, r.c.state).where(r.c.parent_run_id == first)
    )
    assert (kind, state) == ("task", "queued") and branch.endswith(f"/{first}"), "the same task"
    assert unread(hub_db) == [("curator_paused",), ("run_failed",)]

    assert claim(client, laptop)["id"] == second
    moved(client, laptop, second, "running", "verifying", "done")
    set_run(
        hub_db, second, schedule_id=schedule, dispatched_via="schedule", schedule_night=date(2026, 11, 11)
    )  # stands for a night-shift run
    assert run_pass(client)["read"] == 1, "the run was done again; the night shift is still paused"
    assert unread(hub_db) == [("curator_paused",)]
    sql(hub_db, update(s).values(paused_at=None, pause_reason=None).where(s.c.id == schedule))
    assert run_pass(client)["read"] == 1
    assert unread(hub_db) == []


def test_a_runtime_whose_runs_authenticate_with_a_lease_counts_as_logged_in():
    disk = {"free_gb": 120.5, "free_pct": 40.0}
    assert healthy({"disk": disk, "logins": {"claude-code": "leased"}}, "claude-code")
    assert healthy({"disk": disk, "logins": {"claude-code": "leased"}}, "any")
    for state in ("unauthorized", "missing", "unknown"):
        assert not healthy({"disk": disk, "logins": {"claude-code": state}}, "claude-code")
        assert not healthy({"disk": disk, "logins": {"claude-code": state}}, None)

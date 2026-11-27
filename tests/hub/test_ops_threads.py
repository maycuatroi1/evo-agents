"""The ops threads of a project's default system (plan hub-steward, criteria a8 to a10; ``ops_threads`` and
``ops_actions``): a batch closed with items opens one thread to the system's Orchestrator, with a summary and no model
call, and a new session; the Orchestrator acts on the batch inside a frame, with one decision of the owner a batch;
the spend of ops threads stops at ``limits.ops_daily_usd``; two ops runs failed of their agent pause the system's ops;
a cause key seen three times in seven days is marked recurring; the owner gets one digest a day, none on a day with
nothing.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import hub_events, tables
from evo_agents.hub.server.ops.service import threads as ops_threads
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401 (fixtures)
    app_key,
    chat_worker,
    claim,
    client,
    created,
    hub,
    in_chats,
    moved,
    sent,
    started,
)
from tests.hub.test_ops_batches import project_id, record, run_pass
from tests.hub.test_runs import PROJECT, set_run
from tests.hub.test_system_orchestrator import LED, audited, in_thread, led_desk, threads_of
from tests.hub.test_system_traces import push_plan

KEY = "run.ended:agent:@reviewer"


def ops_on(db, limits: dict | None = None) -> None:
    """Make the system review (led by @lead) the project's default, its ops on, with ``limits``."""
    s = tables.agent_systems
    spec = {**LED, "ops": {"enabled": True}, **({"limits": limits} if limits else {})}
    sql(db, update(s).values(spec=spec, is_default=True).where(s.c.project_id == project_id(db), s.c.slug == "review"))


def batch(
    db, run_id: int | None = None, *, state: str = "ready", key: str = KEY, decision_id: int | None = None
) -> int:
    """A batch of ``key`` closed ``state``, with one pending item: run ``run_id`` that failed, or wait
    ``decision_id``."""
    b, i = tables.ops_batches, tables.ops_items
    row = {"project_id": project_id(db), "kind": "run.ended", "cause": "agent", "subject": "@reviewer"}
    values = {**row, "key": key, "state": state, "closes_at": func.now(), "closed_at": func.now()}
    ((batch_id,),) = sql(db, insert(b).values(**values).returning(b.c.id))
    sql(db, insert(i).values(**row, batch_id=batch_id, run_id=run_id, decision_id=decision_id))
    return batch_id


def item_batch(db, kind: str, cause: str, subject: str, payload: dict, run_id: int | None = None) -> int:
    """A ready batch of one pending item of ``kind``, written from a hub event of ``payload``."""
    b, i, e = tables.ops_batches, tables.ops_items, tables.hub_events
    row = {"project_id": project_id(db), "kind": kind, "cause": cause, "subject": subject}
    values = {**row, "key": f"{kind}:{cause}:{subject}", "state": "ready", "closes_at": func.now()}
    ((batch_id,),) = sql(db, insert(b).values(**values, closed_at=func.now()).returning(b.c.id))
    event = {"project_id": row["project_id"], "kind": kind, "payload": payload}
    ((event_id,),) = sql(db, insert(e).values(**event).returning(e.c.id))
    sql(db, insert(i).values(**row, batch_id=batch_id, event_id=event_id, run_id=run_id))
    return batch_id


def failed(db, run_id: int, error: str) -> None:
    """Run ``run_id`` failed of its agent, written straight, so no rule of the Orchestrator opens a thread for it."""
    set_run(db, run_id, state="failed", error=error, finished_at=func.now())


def ops_threads_of(db) -> list[dict]:
    return [thread for thread in threads_of(db) if thread["rule"] == "ops"]


def threads_pass(client) -> dict:
    return client.portal.call(ops_threads.threads_pass, client.app.state.engine)


def batch_row(db, batch_id: int) -> dict:
    b = tables.ops_batches
    (row,) = sql(db, select(b).where(b.c.id == batch_id))
    return dict(zip(b.c.keys(), row, strict=True))


def notices(db, kind: str) -> list[str]:
    n = tables.notifications
    return [row[0] for row in sql(db, select(n.c.title).where(n.c.notice_kind == kind).order_by(n.c.id))]


def runs_count(db) -> int:
    return sql(db, select(func.count()).select_from(tables.runs))[0][0]


def root_of(db, thread: dict) -> str:
    m = tables.chat_messages
    return sql(db, select(m.c.body).where(m.c.id == thread["root_message_id"]))[0][0]


def lead_claims(client, lead: dict, run_id: int) -> None:
    spec = claim(client, lead)
    assert spec is not None and spec["id"] == run_id, spec
    moved(client, lead, run_id, "running", session_id=f"sess-{run_id}")


def ops(client, worker: dict, run_id: int, action: str, body: dict, status: int = 200) -> dict:
    response = in_chats(client, worker, run_id, f"ops/{action}", body)
    assert response.status_code == status, response.text
    return response.json()


def inbox(client, headers: dict, **params) -> dict[int, dict | None]:
    """The member's notices, as the Inbox lists them: id to the ops batch that holds each."""
    response = client.get("/v1/me/notifications", params={"kind": "notice", **params}, headers=headers)
    assert response.status_code == 200, response.text
    return {item["id"]: item["ops"] for item in response.json()["notifications"]}


def notice_of(db, login: str, run_id: int | None) -> int:
    """A run_failed notice of run ``run_id`` for ``login``, written straight."""
    n, u = tables.notifications, tables.users
    (user_id,) = sql(db, select(u.c.id).where(u.c.login == login))[0]
    row = {"user_id": user_id, "kind": "notice", "notice_kind": "run_failed", "project_id": project_id(db)}
    return sql(db, insert(n).values(**row, run_id=run_id, title="A run failed").returning(n.c.id))[0][0]


def test_a_ready_batch_opens_one_thread_and_a_new_session_and_no_batch_no_run(client, hub, hub_db):
    _, _, _, parent = led_desk(client, hub)
    ops_on(hub_db)
    before = runs_count(hub_db)
    assert threads_pass(client) == {} and runs_count(hub_db) == before, "no batch, no run"
    failed(hub_db, parent, "the retry test fails on the second attempt")
    first = batch(hub_db, parent)
    assert threads_pass(client)["opened"] == 1
    (thread,) = ops_threads_of(hub_db)
    row = batch_row(hub_db, first)
    assert (thread["rule"], thread["subject"], thread["key"]) == ("ops", "lead", f"ops:batch:{first}")
    assert (row["state"], row["thread_id"], row["run_id"]) == ("running", thread["id"], thread["orchestrator_run_id"])
    root = root_of(hub_db, thread)
    assert root.startswith("Rule: ops:") and root.endswith("\n\n@lead"), root
    assert f"run #{parent} (chat, @reviewer) failed: the retry test fails" in root
    assert f"Next: `evo-agents hub inbox --show r{parent}`" in root and "Recurring" not in root
    r, u = tables.runs, tables.users
    ((profile, by),) = sql(
        hub_db,
        select(r.c.profile_id, u.c.login)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == thread["orchestrator_run_id"]),
    )
    s = tables.agent_systems
    owner = sql(hub_db, select(u.c.login).join_from(s, u, u.c.id == s.c.created_by).where(s.c.slug == "review"))[0][0]
    assert by == owner, "the Orchestrator works for the system's owner, under the owner's autonomy card"
    assert threads_pass(client).get("opened", 0) == 0, "one thread a batch"

    path = f"/chats/{thread['chat_id']}?thread={thread['root_message_id']}"
    held, alone = notice_of(hub_db, owner, parent), notice_of(hub_db, owner, None)
    listed = inbox(client, hub["owner"])
    assert listed[held] == {"batch_id": first, "state": "running", "thread": path} and listed[alone] is None
    assert set(inbox(client, hub["owner"], handler="ops")) == {held}
    assert alone in inbox(client, hub["owner"], handler="person") and held not in inbox(
        client, hub["owner"], handler="person"
    )
    shown = client.get(f"/v1/projects/{PROJECT}/systems/review/ops/batches", headers=hub["owner"]).json()
    assert [(b["id"], b["thread"]) for b in shown] == [(first, path)]

    second = batch(hub_db, parent)
    assert threads_pass(client)["opened"] == 1
    one, two = ops_threads_of(hub_db)
    assert batch_row(hub_db, second)["thread_id"] == two["id"]
    assert one["root_message_id"] != two["root_message_id"] and one["orchestrator_run_id"] != two["orchestrator_run_id"]


def test_the_orchestrator_acts_on_its_batch_inside_the_frame_with_one_decision_a_batch(client, hub, hub_db):
    studio, _, lead, parent = led_desk(client, hub)
    ops_on(hub_db)
    failed(hub_db, parent, "the retry test fails")
    batch_id = batch(hub_db, parent)
    threads_pass(client)
    (thread,) = ops_threads_of(hub_db)
    run_id = thread["orchestrator_run_id"]
    lead_claims(client, lead, run_id)

    _, other = started(client, hub, studio, created(client, hub["owner"], agents=["reviewer", "fixer"]))
    ops(client, studio, other["id"], "rerun", {"run_id": parent, "reason": "r"}, 404)  # a run in no ops thread
    ops(client, lead, run_id, "rerun", {"run_id": run_id, "reason": "r"}, 404)  # not an item of the batch
    ops(client, lead, run_id, "answer-wait", {"decision_id": 1, "text": "go", "reason": "r"}, 404)
    ops(client, lead, run_id, "curator-resume", {"reason": "r"}, 409)  # no schedule the breaker paused
    acted = ops(client, lead, run_id, "rerun", {"run_id": parent, "reason": "the test is flaky, not the code"})
    assert acted["run_id"] and acted["handled"] == 1
    i = tables.ops_items
    assert sql(hub_db, select(i.c.state, i.c.handled_by).where(i.c.batch_id == batch_id)) == [
        ("handled", "orchestrator")
    ]
    (target,) = audited(hub_db, "ops.rerun")
    assert f"thread:{thread['id']} rule:ops subject:@lead orchestrator_run:{run_id} run:{parent}" in target
    body = {
        "lens": "failed_runs",
        "severity": "medium",
        "title": "Flaky retry test",
        "body": "Pin the clock.",
        "reason": "r",
    }
    found = ops(client, lead, run_id, "finding", body)
    f = tables.findings
    ((by_run, evidence),) = sql(hub_db, select(f.c.run_id, f.c.evidence).where(f.c.id == found["finding_id"]))
    assert by_run == run_id and evidence[0]["batch_id"] == batch_id and evidence[1]["run_id"] == parent

    answered = in_chats(client, lead, run_id, "triage/answer", {"text": "x", "reason": "y"})
    assert answered.status_code == 409, answered.text
    ask = {"question": "Raise the retry cap?", "options": [{"key": "y", "label": "Yes"}, {"key": "n", "label": "No"}]}
    asked = in_chats(client, lead, run_id, "triage/ask-owner", {**ask, "reason": "a cost the owner decides"})
    assert asked.status_code == 200, asked.text
    again = in_chats(client, lead, run_id, "triage/ask-owner", {**ask, "reason": "again"})
    assert again.status_code == 409 and "one decision" in again.text, again.text


def test_at_the_cap_no_thread_opens_the_batch_waits_and_one_ops_cap_notice(client, hub, hub_db):
    _, _, _, parent = led_desk(client, hub)
    ops_on(hub_db, {"ops_daily_usd": 1})
    failed(hub_db, parent, "the retry test fails")
    batch(hub_db, parent)
    threads_pass(client)
    (thread,) = ops_threads_of(hub_db)
    spend = {"session": "", "model": "", "estimated_usd": 1.5, "reports": 1, "ends": {}}
    sql(hub_db, insert(tables.run_spend).values(run_id=thread["orchestrator_run_id"], **spend))
    waiting = batch(hub_db, parent)
    assert threads_pass(client)["capped"] == 1
    assert threads_pass(client)["capped"] == 1
    assert batch_row(hub_db, waiting)["state"] == "ready" and len(ops_threads_of(hub_db)) == 1
    assert len(notices(hub_db, "ops_cap")) == 1, "one notice a day"
    shown = client.get(f"/v1/projects/{PROJECT}/systems/review/ops", headers=hub["owner"]).json()
    assert (shown["spent_usd"], shown["ops_daily_usd"]) == (1.5, 1), "the web reads the cap reached"


def test_two_ops_runs_failed_of_their_agent_trip_the_breaker_and_resume_lets_them_run(client, hub, hub_db):
    _, _, lead, parent = led_desk(client, hub)
    ops_on(hub_db)
    failed(hub_db, parent, "the retry test fails")
    s = tables.agent_systems
    state = select(s.c.ops_failures, s.c.ops_paused_at.is_not(None)).where(s.c.slug == "review")
    for at, failures in ((0, (1, False)), (1, (0, True))):
        batch(hub_db, parent)
        threads_pass(client)
        run_id = ops_threads_of(hub_db)[at]["orchestrator_run_id"]
        lead_claims(client, lead, run_id)
        moved(client, lead, run_id, "failed", error="the model refused the frame")
        settled = threads_pass(client)
        assert sql(hub_db, state) == [failures], settled
    assert len(notices(hub_db, "ops_paused")) == 1
    held = batch(hub_db, parent)
    assert threads_pass(client)["paused"] == 1 and batch_row(hub_db, held)["state"] == "ready"
    response = client.post(f"/v1/projects/{PROJECT}/systems/review/ops/resume", headers=hub["owner"])
    assert response.status_code == 200, response.text
    assert threads_pass(client)["opened"] == 1 and batch_row(hub_db, held)["state"] == "running"


def test_a_recurring_cause_key_asks_for_a_fix_and_the_digest_comes_once_a_day(client, hub, hub_db):
    _, _, _, parent = led_desk(client, hub)
    ops_on(hub_db)
    threads_pass(client)
    assert notices(hub_db, "ops_digest") == [], "no notice on a day with nothing"
    failed(hub_db, parent, "the retry test fails")
    batch(hub_db, parent, state="done")
    batch(hub_db, parent, state="done")
    batch(hub_db, parent, key="run.ended:agent:@fixer", state="done")
    third = batch(hub_db, parent)
    assert threads_pass(client)["opened"] == 1
    assert batch_row(hub_db, third)["recurring"] is True
    root = root_of(hub_db, ops_threads_of(hub_db)[0])
    assert "Recurring: this cause key closed 3 batches in 7 days" in root and "write a finding" in root
    assert len(notices(hub_db, "ops_digest")) == 1
    n = tables.notifications
    ((body, details),) = sql(hub_db, select(n.c.body, n.c.details).where(n.c.notice_kind == "ops_digest"))
    assert details["threads"] == 1 and "Waits on you:" in body, body
    threads_pass(client)
    assert len(notices(hub_db, "ops_digest")) == 1, "one a day"


def test_a_batch_no_node_of_the_lead_can_take_stays_ready_with_no_thread_until_one_can(client, hub, hub_db):
    _, _, lead, parent = led_desk(client, hub)
    ops_on(hub_db)
    failed(hub_db, parent, "the retry test fails")
    batch_id = batch(hub_db, parent)
    w = tables.workers
    sql(hub_db, update(w).values(dispatch_from="web").where(w.c.id == lead["id"]))  # no node takes the ops run
    assert threads_pass(client).get("opened", 0) == 0
    row = batch_row(hub_db, batch_id)
    assert (row["state"], row["thread_id"], row["run_id"]) == ("ready", None, None), "no run, no thread"
    assert ops_threads_of(hub_db) == []
    threads_pass(client)
    assert batch_row(hub_db, batch_id)["state"] == "ready", "the next pass does not settle it done with its item unread"

    sql(hub_db, update(w).values(dispatch_from="any").where(w.c.id == lead["id"]))
    assert threads_pass(client)["opened"] == 1
    (thread,) = ops_threads_of(hub_db)
    row = batch_row(hub_db, batch_id)
    assert (row["state"], row["run_id"]) == ("running", thread["orchestrator_run_id"]) and row["run_id"]


def test_the_events_of_an_ops_run_are_no_item_of_the_queue(client, hub, hub_db):
    _, _, _, parent = led_desk(client, hub)
    ops_on(hub_db)
    failed(hub_db, parent, "the retry test fails")
    batch(hub_db, parent)
    threads_pass(client)
    (thread,) = ops_threads_of(hub_db)
    ops_run = thread["orchestrator_run_id"]
    run_pass(client)  # the pass that turns the queue on starts at the newest event
    asked = {"run_id": ops_run, "decision_id": 1, "category": "triage", "asked_of": 1}
    record(client, hub_db, hub_events.DECISION_OPENED, **asked)
    record(client, hub_db, hub_events.RUN_ENDED, run_id=ops_run, run_kind="chat", state="failed", worker_id=7)
    record(client, hub_db, hub_events.DECISION_OPENED, **{**asked, "run_id": parent, "decision_id": 2})
    assert run_pass(client)["queued"] == 1, "the Orchestrator's own decision comes back to it as no batch"


def test_a_mention_an_ops_run_makes_that_no_node_takes_writes_no_mention_refused(client, hub, hub_db):
    _, fixer, lead, parent = led_desk(client, hub)
    ops_on(hub_db)
    failed(hub_db, parent, "the retry test fails")
    batch(hub_db, parent)
    threads_pass(client)
    (thread,) = ops_threads_of(hub_db)
    run_id = thread["orchestrator_run_id"]
    lead_claims(client, lead, run_id)
    joined = client.put(f"/v1/chats/{thread['chat_id']}/agents/fixer", headers=hub["owner"])
    assert joined.status_code == 200, joined.text
    w, e = tables.workers, tables.hub_events
    sql(hub_db, update(w).values(dispatch_from="web").where(w.c.id == fixer["id"]))  # no node takes @fixer
    refused = select(func.count()).select_from(e).where(e.c.kind == hub_events.MENTION_REFUSED)

    handed = in_chats(client, lead, run_id, "chat/mentions", {"handle": "fixer", "body": "run the retry test again"})
    assert handed.status_code == 201 and handed.json()["refusal"], handed.text
    assert sql(hub_db, refused)[0][0] == 0, "the ops run's own refusal would come back to it as a batch, and again"
    said = sent(client, hub["owner"], thread["chat_id"], "@fixer run it again")
    assert said["mentions"][0]["refusal"]
    assert sql(hub_db, refused)[0][0] == 1, "a person's mention no node takes still tells the Orchestrator"


def test_the_orchestrator_dispatches_a_stalled_plan_again(client, hub, hub_db):
    _, _, lead, _ = led_desk(client, hub)
    ops_on(hub_db)
    plan = credentials_api.PLAN
    push_plan(client, hub["owner"])
    payload = {"v": 1, "plan_id": plan, "revision": 1, "run_id": None, "steps": ["1"], "state": "stalled"}
    batch_id = item_batch(hub_db, hub_events.PLAN_STALLED, "stalled", f"plan:{plan}", payload)
    threads_pass(client)
    (thread,) = ops_threads_of(hub_db)
    root = root_of(hub_db, thread)
    assert f"plan {plan} (revision 1) has ready steps 1" in root, root
    assert f"`evo-agents hub plan handoff {plan} --step 1`" in root and f"ops plan-dispatch {plan}`" in root, root
    run_id = thread["orchestrator_run_id"]
    lead_claims(client, lead, run_id)

    ops(client, lead, run_id, "plan-dispatch", {"plan_id": "no-such-plan", "reason": "r"}, 404)  # not in the batch
    acted = ops(client, lead, run_id, "plan-dispatch", {"plan_id": plan, "reason": "step 1 waits on no one"})
    assert acted["run_id"] and acted["handled"] == 1
    t = tables.system_traces
    ((trace_id, revision),) = sql(hub_db, select(t.c.id, t.c.plan_revision).where(t.c.plan_id == plan))
    assert revision == 1
    r = tables.runs
    assert sql(hub_db, select(r.c.kind, r.c.state).where(r.c.id == acted["run_id"])) == [("chat", "queued")]
    i = tables.ops_items
    assert sql(hub_db, select(i.c.state, i.c.handled_by).where(i.c.batch_id == batch_id)) == [
        ("handled", "orchestrator")
    ]
    (target,) = audited(hub_db, "ops.plan_dispatch")
    assert f"orchestrator_run:{run_id} plan:{plan} run:{acted['run_id']} trace:{trace_id}" in target
    assert len(audited(hub_db, "run.dispatch_system")) == 1, "dispatched as dispatch_to_system does"
    lines = in_thread(hub_db, thread["root_message_id"])
    assert any(line.startswith(f"Decision: handed plan {plan} (revision 1) again") for line in lines), lines

    again = ops(client, lead, run_id, "plan-dispatch", {"plan_id": plan, "reason": "r"}, 409)
    assert f"trace #{trace_id}" in again["message"], again  # its trace runs it now: the same refusal as a person's


def test_the_orchestrator_sets_a_queued_runs_priority(client, hub, hub_db):
    _, _, lead, _ = led_desk(client, hub)
    ops_on(hub_db)
    chat = created(client, hub["owner"], title="Fixes", agents=["fixer"])
    sent(client, hub["owner"], chat["id"], "@fixer please fix the flaky wait test")
    r = tables.runs
    ((queued, worker_id),) = sql(
        hub_db, select(r.c.id, r.c.pinned_worker_id).where(r.c.state == "queued", r.c.kind == "chat")
    )
    payload = {"v": 1, "run_id": queued, "run_kind": "chat", "state": "queued", "worker_id": worker_id}
    batch_id = item_batch(hub_db, hub_events.RUN_QUEUED_LONG, "queued", "-", {**payload, "priority": "medium"}, queued)
    threads_pass(client)
    (thread,) = ops_threads_of(hub_db)
    root = root_of(hub_db, thread)
    assert f"run #{queued} (chat, @fixer) queued for a slot of " in root and "priority medium" in root, root
    assert f"`evo-agents worker ops priority {queued} LEVEL`" in root, root
    run_id = thread["orchestrator_run_id"]
    lead_claims(client, lead, run_id)

    ops(client, lead, run_id, "priority", {"run_id": queued, "level": "asap", "reason": "r"}, 422)
    ops(client, lead, run_id, "priority", {"run_id": run_id, "level": "high", "reason": "r"}, 409)  # running
    acted = ops(client, lead, run_id, "priority", {"run_id": queued, "level": "urgent", "reason": "the owner waits"})
    assert (acted["run_id"], acted["handled"]) == (queued, 1)
    assert sql(hub_db, select(r.c.priority).where(r.c.id == queued)) == [("urgent",)]
    i = tables.ops_items
    assert sql(hub_db, select(i.c.state, i.c.handled_by).where(i.c.batch_id == batch_id)) == [
        ("handled", "orchestrator")
    ]
    (target,) = audited(hub_db, "ops.priority")
    assert f"orchestrator_run:{run_id} run:{queued} priority:medium->urgent" in target
    lines = in_thread(hub_db, thread["root_message_id"])
    assert f"Decision: set the priority of queued run #{queued} to urgent, from medium: the owner waits" in lines

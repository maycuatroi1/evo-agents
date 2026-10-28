"""The thread a failed run opens in the triage chat (``evo_agents.hub.server.triage``, migration 0044), as criteria a2
and a7 of the plan run-failure-triage name them.

a2: a failed run of kind step, plan or author opens a thread in the triage chat of its project (one for all its plans;
``tests.hub.test_triage_chats``), whose first message the hub writes: the state, the cause and whether the hub guessed
it, the error, the step, the repo and branch, the last verify, the links of the run, and who could run it again; it
mentions the project's default agent, which makes a chat run for the owner; the notice run_failed links to the thread.

a7: one thread a run however often its failure is handled; a failed chat run of triage opens none; what the chat's runs
cost holds no triage run back; a project's switch turns it off and the notice is the one it always was; review, judge, chat and local
runs open none.

A run the hub stopped on its owner's accounts for a variable, none of which is ready, gets the thread and the time the
first is ready again, and no agent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.server import triage
from evo_agents.hub.server.run_state import run_step, write_event
from tests.hub.live import sql
from tests.hub.test_chat_runs import chat_worker, thread
from tests.hub.test_runs import PLAN, PROJECT, claim, client, dispatched, hub, moved, notices, report  # noqa: F401

ERROR = "no credential reads evo-agents: git could not read Username"
VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def failed_step(
    client, hub, worker, key: int = 2, *, cause="credentials", error=ERROR, plan=PLAN, who="owner", claims=2
) -> int:
    """Dispatch step ``key`` of ``plan`` as member ``who``, have ``worker`` (theirs) claim it, in at most ``claims``
    claims, and report it failed; its run id."""
    run_id = dispatched(client, hub[who], [key], plan_id=plan)[0]["id"]
    claimed = [claim(client, worker) for _ in range(claims)]  # the chat runs earlier failures queued may come first
    assert run_id in [spec["id"] for spec in claimed if spec]
    body = {"error": error} | ({"failure_cause": cause} if cause else {})
    assert report(client, worker, run_id, "failed", **body).status_code == 200
    return run_id


def stopped_on_accounts(client, hub, hub_db, worker, *minutes: int | None) -> int:
    """A step run the hub stopped on its owner's accounts for VAR, bound to the project, one cooling down for each of
    ``minutes`` (None: one that is ready), and reported failed with cause rate_limit; its id."""
    run_id = dispatched(client, hub["owner"], [2])[0]["id"]
    assert run_id in [spec["id"] for spec in (claim(client, worker) for _ in range(2)) if spec]
    r, s = tables.runs, tables.secrets
    ((owner_id, project_id),) = sql(hub_db, select(r.c.dispatched_by, r.c.project_id).where(r.c.id == run_id))
    ids = []
    for index, wait in enumerate(minutes):
        values = {"owner_id": owner_id, "name": f"claude-{index}", "kind": "env", "env_var": VAR}
        sealed = {"sealed": bytes(32), "nonce": bytes(12), "key_id": "0" * 8}  # no lease reads it
        ((secret_id,),) = sql(hub_db, insert(s).values(**values, **sealed).returning(s.c.id))
        sql(hub_db, insert(tables.secret_bindings).values(secret_id=secret_id, project_id=project_id))
        until = None if wait is None else func.now() + timedelta(minutes=wait)
        sql(hub_db, insert(tables.accounts).values(secret_id=secret_id, cooldown_until=until))
        ids.append(secret_id)

    async def stop():
        async with client.app.state.engine.begin() as conn:
            body = {"text": "the run stops", "env_var": VAR, "secret_id": ids[0], "account_event": "stop"}
            await write_event(conn, run_id, body)

    client.portal.call(stop)
    said = report(client, worker, run_id, "failed", error="usage limit", failure_cause="rate_limit")
    assert said.status_code == 200, said.text
    return run_id


def count(db, table) -> int:
    return sql(db, select(func.count()).select_from(table))[0][0]


def row_of(db, run_id: int):
    t = tables.run_triage
    return sql(
        db,
        select(t.c.chat_id, t.c.root_message_id, t.c.cause, t.c.cause_inferred, t.c.agent_run_id).where(
            t.c.run_id == run_id
        ),
    )


def root_of(client, hub, chat_id: int, root_id: int) -> dict:
    return thread(client, hub["owner"], chat_id, root_id)[0]


def test_a_failed_step_run_opens_a_thread_with_the_facts_that_mentions_the_agent_and_the_notice_links_to_it(
    client, hub, hub_db
):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker)
    ((chat_id, root_id, cause, inferred, agent_run),) = row_of(hub_db, run_id)
    assert (cause, inferred) == ("credentials", False) and agent_run is not None
    root = root_of(client, hub, chat_id, root_id)
    body = root["body"]
    for line in (
        f"Run #{run_id} of step 2 of {PLAN} failed.",
        "Cause: credentials (as the worker reported it).",
        f"Error: {ERROR}",
        "Last verify: none reported.",
        f"/p/{PROJECT}/runs/{run_id}",
        f"/v1/projects/{PROJECT}/runs/{run_id}/events",
        f"/v1/projects/{PROJECT}/runs/{run_id}/diff",
        "Run again with:",
    ):
        assert line in body, line
    assert body.endswith(f"@{PROJECT}") and root["author"]["kind"] == "hub"
    assert [(m["name"], m["run_id"]) for m in root["mentions"]] == [(PROJECT, agent_run)]
    c, r = tables.chats, tables.runs
    assert sql(hub_db, select(c.c.title)) == [(f"Triage: {PROJECT}",)]
    assert sql(hub_db, select(r.c.kind, r.c.state, r.c.chat_id, r.c.hop).where(r.c.id == agent_run)) == [
        ("chat", "queued", chat_id, 1)
    ]
    ((_, kind, title, _, details),) = notices(hub_db, run_id)
    assert (kind, details["failure_cause"]) == ("run_failed", "credentials")
    n = tables.notifications
    assert sql(hub_db, select(n.c.link).where(n.c.run_id == run_id)) == [(f"/chats/{chat_id}?message={root_id}",)]


def test_a_cause_the_hub_guessed_is_said_so_and_a_cause_of_work_sends_no_notice(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker, cause=None, error="verify command `pytest` exited 1")
    ((chat_id, root_id, cause, inferred, _),) = row_of(hub_db, run_id)
    assert inferred is True and cause is not None
    assert f"Cause: {cause} (guessed by the hub from the error)." in root_of(client, hub, chat_id, root_id)["body"]
    assert notices(hub_db, run_id) == [] or cause in ("origin", "credentials", "missing_tool")


def test_the_chat_of_the_project_is_used_again_and_each_run_has_one_thread_however_often_it_is_handled(
    client, hub, hub_db
):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    first = failed_step(client, hub, worker, 2)
    second = failed_step(client, hub, worker, 4)
    (chat_a, root_a, *_), (chat_b, root_b, *_) = row_of(hub_db, first)[0], row_of(hub_db, second)[0]
    assert chat_a == chat_b and root_a != root_b
    assert (count(hub_db, tables.triage_chats), count(hub_db, tables.run_triage)) == (1, 2)

    async def again():
        async with client.app.state.engine.begin() as conn:
            return await triage.on_failed(conn, await run_step(conn, first))

    found = client.portal.call(again)
    assert (found.chat_id, found.root_id) == (chat_a, root_a)
    assert (count(hub_db, tables.triage_chats), count(hub_db, tables.run_triage)) == (1, 2)


def test_a_failed_chat_run_of_triage_opens_no_thread(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker)
    ((_, _, _, _, agent_run),) = row_of(hub_db, run_id)
    spec = claim(client, worker)
    assert (spec["id"], spec["kind"]) == (agent_run, "chat")
    moved(client, worker, agent_run, "running", "failed", error="the agent stopped")
    assert (count(hub_db, tables.run_triage), count(hub_db, tables.triage_chats)) == (1, 1)


def test_a_failed_plan_run_opens_a_thread_too(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    made = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"]).json()
    assert claim(client, worker)["id"] == made["id"]
    moved(client, worker, made["id"], "running", "failed", error="the agent stopped")
    ((chat_id, root_id, *_),) = row_of(hub_db, made["id"])
    assert f"Plan run #{made['id']} of {PLAN} failed." in root_of(client, hub, chat_id, root_id)["body"]
    n = tables.notifications
    assert sql(hub_db, select(n.c.link).where(n.c.run_id == made["id"])) == [(f"/chats/{chat_id}?message={root_id}",)]


def test_what_the_chat_runs_cost_holds_no_triage_run_back(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    failed_step(client, hub, worker, 2)
    r = tables.runs
    sql(hub_db, update(r).values(usage={"total_cost_usd": 100.0}).where(r.c.kind == "chat"))
    run_id = failed_step(client, hub, worker, 4)
    ((chat_id, root_id, _, _, agent_run),) = row_of(hub_db, run_id)
    assert agent_run is not None
    assert sql(hub_db, select(r.c.budget).where(r.c.id == agent_run)) == [({"max_seconds": 1800},)]
    said = thread(client, hub["owner"], chat_id, root_id)
    assert [item["author"]["kind"] for item in said] == ["hub"]  # the facts, and no refusal after them


def test_a_run_stopped_on_accounts_none_of_which_is_ready_asks_no_agent_and_says_when_one_is(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = stopped_on_accounts(client, hub, hub_db, worker, 90, 30)
    ((chat_id, root_id, cause, _, agent_run),) = row_of(hub_db, run_id)
    assert (cause, agent_run) == ("rate_limit", None)
    a = tables.accounts
    ((soonest,),) = sql(hub_db, select(func.min(a.c.cooldown_until)))
    root = root_of(client, hub, chat_id, root_id)
    assert (
        f"No account of the owner for {VAR} is ready before {soonest.isoformat()}: the hub asks no agent"
        in root["body"]
    )
    assert not root["body"].endswith(f"@{PROJECT}") and root["mentions"] == []
    r = tables.runs
    assert sql(hub_db, select(func.count()).select_from(r).where(r.c.kind == "chat")) == [(0,)]


def test_a_run_stopped_on_accounts_one_of_which_is_ready_again_asks_the_agent(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = stopped_on_accounts(client, hub, hub_db, worker, 90, None)
    ((chat_id, root_id, _, _, agent_run),) = row_of(hub_db, run_id)
    assert agent_run is not None
    root = root_of(client, hub, chat_id, root_id)
    assert "No account of the owner" not in root["body"] and root["body"].endswith(f"@{PROJECT}")


def test_a_project_with_triage_off_gets_the_notice_it_always_had(client, hub, hub_db):
    sql(hub_db, update(tables.projects).values(triage_enabled=False))
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker)
    assert row_of(hub_db, run_id) == []
    assert (count(hub_db, tables.chats), count(hub_db, tables.triage_chats)) == (0, 0)
    n = tables.notifications
    assert sql(hub_db, select(n.c.link).where(n.c.run_id == run_id)) == [(f"/p/{PROJECT}/runs/{run_id}",)]


def test_a_thread_that_cannot_open_leaves_the_run_failed_with_the_old_notice(client, hub, hub_db, monkeypatch):
    async def broken(conn, found):
        raise RuntimeError("no thread today")

    monkeypatch.setattr(triage, "open_thread", broken)
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker)
    r, n = tables.runs, tables.notifications
    assert sql(hub_db, select(r.c.state).where(r.c.id == run_id)) == [("failed",)]
    assert sql(hub_db, select(n.c.link).where(n.c.run_id == run_id)) == [(f"/p/{PROJECT}/runs/{run_id}",)]
    assert count(hub_db, tables.run_triage) == 0


@pytest.mark.parametrize("kind", ["review", "judge", "chat", "local"])
def test_a_run_of_another_kind_opens_no_thread(client, hub, hub_db, kind):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = failed_step(client, hub, worker)
    sql(hub_db, tables.run_triage.delete())

    async def again():
        async with client.app.state.engine.begin() as conn:
            return await triage.on_failed(conn, replace(await run_step(conn, run_id), kind=kind))

    assert client.portal.call(again) is None
    assert count(hub_db, tables.run_triage) == 0


def test_the_candidates_of_a_failed_run_say_whether_the_cause_was_guessed_and_where_its_thread_is(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    said = failed_step(client, hub, worker)
    ((chat_id, root_id, *_),) = row_of(hub_db, said)
    url = f"/v1/projects/{PROJECT}/runs/{said}/rerun-candidates"
    answer = client.get(url, headers=hub["owner"]).json()
    assert answer["cause_inferred"] is False
    assert answer["triage"] == {
        "chat_id": chat_id,
        "root_message_id": root_id,
        "link": f"/chats/{chat_id}?message={root_id}",
    }


def test_the_candidates_of_a_run_whose_cause_the_hub_guessed_say_so(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    guessed = failed_step(client, hub, worker, cause=None, error="verify command `pytest` exited 1")
    answer = client.get(f"/v1/projects/{PROJECT}/runs/{guessed}/rerun-candidates", headers=hub["owner"]).json()
    assert answer["cause_inferred"] is True and answer["triage"] is not None


# A run that stops moving (plan hub-attention-runtime, a5): one running or verifying with no event for
# triage.STALE_MINUTES while its worker heartbeats, and one the reaper fails on its last attempt, open a thread with
# the diagnosis, once a run.


def running_step(client, hub, worker, key: int = 2) -> int:
    """Step ``key`` dispatched by the owner, claimed by ``worker`` and running; its run id."""
    run_id = dispatched(client, hub["owner"], [key])[0]["id"]
    assert run_id in [spec["id"] for spec in (claim(client, worker) for _ in range(2)) if spec]
    moved(client, worker, run_id, "running")
    return run_id


def quiet_for(hub_db, run_id: int, minutes: int) -> None:
    """Make run ``run_id`` have started ``minutes`` ago and said nothing since: its events, which are never updated,
    are replaced by its start that long ago."""
    then = func.now() - timedelta(minutes=minutes)
    e, r = tables.run_events, tables.runs
    sql(hub_db, e.delete().where(e.c.run_id == run_id))
    start = {"from": "leased", "to": "running", "actor": "worker", "reason": "the agent started"}
    sql(hub_db, insert(e).values(run_id=run_id, seq=1, at=then, kind="state", body=start))
    sql(hub_db, update(r).where(r.c.id == run_id).values(started_at=then, leased_at=then))


def stale_pass(client) -> int:
    return client.portal.call(triage.open_stale, client.app.state.engine)


def reaper_pass(client) -> dict:
    from evo_agents.hub.server.run_state import recover_runs

    return client.portal.call(recover_runs, client.app.state.engine)


def test_a_stale_run_whose_worker_heartbeats_opens_one_thread_with_the_diagnosis(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = running_step(client, hub, worker)
    quiet_for(hub_db, run_id, triage.STALE_MINUTES - 5)
    assert stale_pass(client) == 0 and row_of(hub_db, run_id) == []  # not quiet long enough yet
    quiet_for(hub_db, run_id, triage.STALE_MINUTES + 5)
    reaper_pass(client)  # the reaper's pass opens it
    ((chat_id, root_id, cause, inferred, agent_run),) = row_of(hub_db, run_id)
    assert (cause, inferred) == (triage.STALE_CAUSE, False) and agent_run is not None
    body = root_of(client, hub, chat_id, root_id)["body"]
    for line in (
        f"Run #{run_id} of step 2 of {PLAN} has written no event for {triage.STALE_MINUTES} minutes while its "
        "worker still sends heartbeats.",
        "Cause: stale (seen by the hub; the run still holds its worker).",
        "Last event: #",
        "leased -> running",
        "min ago",
        "Worker: mac-mini, last heartbeat ",
        "Lease: held until ",
        "Attempt: 1 of 3.",
        "`evo-agents worker triage cancel`",
        "Run again with:",
    ):
        assert line in body, line
    assert body.endswith(f"@{PROJECT}")
    r = tables.runs
    assert sql(hub_db, select(r.c.state).where(r.c.id == run_id)) == [("running",)]  # the hub stops nothing itself
    assert stale_pass(client) == 0 and reaper_pass(client) is not None
    assert count(hub_db, tables.run_triage) == 1  # once a run


def test_a_stale_run_on_a_worker_that_sends_no_heartbeat_or_waiting_opens_no_thread(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = running_step(client, hub, worker)
    quiet_for(hub_db, run_id, triage.STALE_MINUTES + 5)
    w, r = tables.workers, tables.runs
    gone = func.now() - timedelta(seconds=600)
    sql(hub_db, update(w).where(w.c.id == worker["id"]).values(last_heartbeat_at=gone))
    assert stale_pass(client) == 0 and row_of(hub_db, run_id) == []  # the reaper's lease rules take that run
    sql(hub_db, update(w).where(w.c.id == worker["id"]).values(last_heartbeat_at=func.now()))
    sql(hub_db, update(r).where(r.c.id == run_id).values(state="waiting", waiting_since=func.now()))
    assert stale_pass(client) == 0 and row_of(hub_db, run_id) == []  # a run waiting for an answer is not stuck


def test_a_stale_run_of_a_project_with_triage_off_opens_no_thread(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    run_id = running_step(client, hub, worker)
    quiet_for(hub_db, run_id, triage.STALE_MINUTES + 5)
    p = tables.projects
    sql(hub_db, update(p).where(p.c.name == PROJECT).values(triage_enabled=False))
    assert stale_pass(client) == 0 and count(hub_db, tables.run_triage) == 0


def test_a_run_lost_on_its_last_attempt_opens_a_thread_with_the_diagnosis_and_one_with_attempts_left_none(
    client, hub, hub_db
):
    worker = chat_worker(client, hub["owner"], "mac-mini", slots=4)
    r = tables.runs
    lapsed = func.now() - timedelta(minutes=1)
    early = running_step(client, hub, worker, 2)
    sql(hub_db, update(r).where(r.c.id == early).values(lease_expires_at=lapsed))
    last = running_step(client, hub, worker, 4)
    sql(hub_db, update(r).where(r.c.id == last).values(max_attempts=1, lease_expires_at=lapsed))
    reaper_pass(client)
    assert sql(hub_db, select(r.c.state).where(r.c.id == early)) == [("lost",)] and row_of(hub_db, early) == []
    assert sql(hub_db, select(r.c.state).where(r.c.id == last)) == [("failed",)]
    ((chat_id, root_id, cause, inferred, agent_run),) = row_of(hub_db, last)
    assert (cause, inferred) == ("lease_lost", True) and agent_run is not None
    body = root_of(client, hub, chat_id, root_id)["body"]
    for line in (
        f"Run #{last} of step 4 of {PLAN} failed.",
        "Cause: lease_lost (guessed by the hub from the error).",
        "stopped extending the lease",
        "Last event: #",
        "leased -> running",
        "Worker: mac-mini, last heartbeat ",
        "Lease: ended with the run at ",
        "Attempt: 1 of 1 (the last).",
    ):
        assert line in body, line
    assert "-> failed" not in body.split("Last event:")[1].split("\n")[0]  # the hub's own move is not the last event
    reaper_pass(client)
    assert count(hub_db, tables.run_triage) == 1

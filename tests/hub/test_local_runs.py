"""Local runs on the hub: a Claude Code session on a member's machine, which the hub watches and asks decisions for.

Registering is idempotent on (project, Claude session id) and belongs to one member; the run is private to its member
across every list, log, stream and decision; its state, plan and step, log, decisions and inbox work with the member's
token; the first answer wins whether it came from the web, from the machine or from Telegram's route; a plan write that
names the run records it on the revision; and no pass of the reaper (lost, park, timeout), no slot and no Curator
figure takes a local run, while a silent one becomes ended."""

from datetime import UTC, datetime

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import tables
from evo_agents.hub.server import collect, local_runs
from evo_agents.hub.server.app import create_app
from tests.hub.live import sql
from tests.hub.test_runs import PLAN, PROJECT, add_worker, claim, members, recover, state_of

SESSION = "0199a3c1-0000-7000-8000-0000000000a1"
OPTIONS = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]
REGISTER = {"machine": "mac-mini", "cwd": "/Users/me/evo-agents", "claude_email": "me@example.com"}


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    return members(client, github)


def path(*parts) -> str:
    return "/".join(["/v1/projects", PROJECT, "local-runs", *map(str, parts)])


def register(client, headers, session: str = SESSION, **extra):
    return client.put(path(session), json={**REGISTER, **extra}, headers=headers)


def registered(client, headers, session: str = SESSION, **extra) -> dict:
    response = register(client, headers, session, **extra)
    assert response.status_code in (200, 201), response.text
    return response.json()


def ask(client, headers, run_id: int, **extra):
    body = {"question": "Ship it?", "options": OPTIONS, **extra}
    return client.post(path(run_id, "decisions"), json=body, headers=headers)


def asked(client, headers, run_id: int, **extra) -> dict:
    response = ask(client, headers, run_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def run_of(client, headers, run_id: int):
    return client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)


def of_run(db, run_id: int, *columns):
    r = tables.runs
    return sql(db, select(*(r.c[name] for name in columns)).where(r.c.id == run_id))[0]


def test_registering_is_idempotent_and_keeps_one_run_per_session(client, hub, hub_db):
    first = register(client, hub["owner"])
    assert first.status_code == 201, first.text
    run = first.json()
    assert (run["kind"], run["state"], run["machine"], run["cwd"], run["claude_email"]) == (
        "local",
        "running",
        "mac-mini",
        "/Users/me/evo-agents",
        "me@example.com",
    )
    assert (run["worker_id"], run["plan_id"], run["runtime"], run["mode"]) == (None, "", "claude-code", "interactive")
    again = register(client, hub["owner"], machine="mac-studio", claude_profile="work", title="Fix the queue")
    assert again.status_code == 200, again.text
    assert (again.json()["id"], again.json()["machine"], again.json()["claude_profile"]) == (
        run["id"],
        "mac-studio",
        "work",
    )
    assert again.json()["title"] == "Fix the queue"
    count = sql(hub_db, select(tables.runs.c.id).where(tables.runs.c.kind == "local"))
    assert len(count) == 1
    other = registered(client, hub["owner"], session="0199a3c1-0000-7000-8000-0000000000a2")
    assert other["id"] != run["id"]


def test_registering_needs_the_writer_role_and_a_session_no_other_member_holds(client, hub):
    assert register(client, hub["reader"]).status_code == 403
    assert register(client, hub["stranger"]).status_code == 404
    registered(client, hub["owner"])
    taken = register(client, hub["other"])
    assert taken.status_code == 403
    assert "another member" in taken.json()["message"]
    # the other member's own session of the same id is no different run: it was taken
    assert register(client, hub["owner"]).status_code == 200


def test_a_local_run_is_private_to_its_member_in_every_read(client, hub):
    run = registered(client, hub["owner"])
    asked(client, hub["owner"], run["id"])
    base = f"/v1/projects/{PROJECT}"
    for name in ("other", "reader", "admin"):
        headers = hub[name]
        listed = client.get(f"{base}/runs", headers=headers)
        if listed.status_code == 200:
            assert run["id"] not in [item["id"] for item in listed.json()["runs"]], name
            assert listed.json()["counts"]["waiting"] == 0
        assert run_of(client, headers, run["id"]).status_code in (403, 404), name
        assert client.get(f"{base}/runs/{run['id']}/events", headers=headers).status_code in (403, 404), name
        assert client.get(f"{base}/runs/{run['id']}/stream", headers=headers).status_code in (403, 404), name
        decisions = client.get(f"{base}/decisions", headers=headers)
        if decisions.status_code == 200:
            assert decisions.json()["total"] == 0, name
        assert client.get(f"{base}/decisions/1", headers=headers).status_code in (403, 404), name
        home = client.get("/v1/me/overview", headers=headers)
        assert home.status_code == 200
        assert run["id"] not in [item["id"] for item in home.json()["active_runs"]], name
        assert home.json()["open_decisions"] == [], name
    mine = client.get(f"{base}/runs", headers=hub["owner"]).json()
    assert [item["id"] for item in mine["runs"]] == [run["id"]]
    assert mine["counts"]["waiting"] == 1
    assert client.get(f"{base}/runs?kind=local&account=ME@example.com", headers=hub["owner"]).json()["total"] == 1
    assert client.get(f"{base}/runs?account=nobody@example.com", headers=hub["owner"]).json()["total"] == 0
    assert client.get(f"{base}/runs?q=mac-mini", headers=hub["owner"]).json()["total"] == 1


def test_no_other_member_reads_or_writes_a_local_run_through_its_routes(client, hub):
    run = registered(client, hub["owner"])
    decision = asked(client, hub["owner"], run["id"])
    for name in ("other", "reader", "stranger", "admin"):
        headers = hub[name]
        for response in (
            client.post(path(run["id"], "state"), json={"state": "idle"}, headers=headers),
            client.post(path(run["id"], "events"), json={"events": []}, headers=headers),
            ask(client, headers, run["id"]),
            client.get(path(run["id"], "decisions", decision["id"]), headers=headers),
            client.post(
                path(run["id"], "decisions", decision["id"], "answered"), json={"option": "yes"}, headers=headers
            ),
            client.post(path(run["id"], "inbox"), headers=headers),
        ):
            assert response.status_code in (403, 404), (name, response.text)
    other_answer = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision['id']}/answer", json={"option": "yes"}, headers=hub["other"]
    )
    assert other_answer.status_code in (403, 404)
    assert of_run_state(client, hub, run["id"]) == "waiting"


def of_run_state(client, hub, run_id: int) -> str:
    return run_of(client, hub["owner"], run_id).json()["state"]


def test_the_state_the_plan_and_the_step_follow_the_session(client, hub, hub_db):
    run = registered(client, hub["owner"])
    state = client.post(
        path(run["id"], "state"), json={"state": "idle", "title": "Review the plan"}, headers=hub["owner"]
    )
    assert state.status_code == 200, state.text
    assert (state.json()["state"], state.json()["title"]) == ("idle", "Review the plan")
    before = of_run(hub_db, run["id"], "last_activity_at")[0]
    working = client.post(
        path(run["id"], "state"), json={"state": "running", "plan_id": PLAN, "step_key": "2"}, headers=hub["owner"]
    )
    assert working.status_code == 200, working.text
    assert (working.json()["state"], working.json()["plan_id"], working.json()["step_key"]) == ("running", PLAN, "2")
    assert of_run(hub_db, run["id"], "last_activity_at")[0] >= before
    missing = client.post(path(run["id"], "state"), json={"plan_id": "nope"}, headers=hub["owner"])
    assert missing.status_code == 422
    no_step = client.post(path(run["id"], "state"), json={"plan_id": PLAN, "step_key": "99"}, headers=hub["owner"])
    assert no_step.status_code == 422
    assert client.post(path(run["id"], "state"), json={"step_key": "2"}, headers=hub["owner"]).status_code == 422
    ended = client.post(path(run["id"], "state"), json={"state": "ended"}, headers=hub["owner"])
    assert ended.status_code == 200, ended.text
    assert ended.json()["state"] == "ended" and ended.json()["finished_at"]
    refused = client.post(path(run["id"], "state"), json={"state": "running"}, headers=hub["owner"])
    assert refused.status_code == 409
    revived = register(client, hub["owner"])
    assert revived.status_code == 200 and revived.json()["state"] == "running"
    assert revived.json()["finished_at"] is None
    log = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}/events", headers=hub["owner"]).json()
    moves = [(e["body"]["from"], e["body"]["to"]) for e in log["events"] if e["kind"] == "state"]
    assert moves == [
        ("none", "running"),
        ("running", "idle"),
        ("idle", "running"),
        ("running", "ended"),
        ("ended", "running"),
    ]


def test_the_log_is_stored_as_a_workers_and_the_owner_reads_it(client, hub):
    run = registered(client, hub["owner"])
    event = {"seq": 1, "at": datetime.now(UTC).isoformat(), "kind": "agent_message_chunk", "body": {"text": "hello"}}
    stored = client.post(path(run["id"], "events"), json={"events": [event]}, headers=hub["owner"])
    assert stored.status_code == 200, stored.text
    assert stored.json() == {"ack_seq": 1, "stored": 1}
    again = client.post(path(run["id"], "events"), json={"events": [event]}, headers=hub["owner"])
    assert again.json() == {"ack_seq": 1, "stored": 0}
    page = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}/events", headers=hub["owner"]).json()
    assert [e["kind"] for e in page["events"]][-1] == "agent_message_chunk"
    assert page["state"] == "running"


def test_a_decision_notifies_the_member_and_the_first_answer_wins(client, hub, hub_db):
    run = registered(client, hub["owner"])
    decision = asked(client, hub["owner"], run["id"], context="Because.")
    assert (decision["category"], decision["run_state"], decision["state"]) == ("question", "waiting", "open")
    assert of_run_state(client, hub, run["id"]) == "waiting"
    notices = client.get("/v1/me/notifications", headers=hub["owner"]).json()
    assert [n["decision_id"] for n in notices["notifications"]] == [decision["id"]]
    assert "Local run" in notices["notifications"][0]["title"]
    home = client.get("/v1/me/overview", headers=hub["owner"]).json()
    assert [(d["id"], d["parks_at"], d["yours"]) for d in home["open_decisions"]] == [(decision["id"], None, True)]
    assert home["counts"]["waiting_on_you"] == 1
    assert [r["id"] for r in home["active_runs"]] == [run["id"]]
    assert home["active_runs"][0]["machine"] == "mac-mini"
    web = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision['id']}/answer", json={"option": "yes"}, headers=hub["owner"]
    )
    assert web.status_code == 200, web.text
    assert web.json()["answer_option"] == "yes"
    late = client.post(
        path(run["id"], "decisions", decision["id"], "answered"), json={"option": "no"}, headers=hub["owner"]
    )
    assert late.status_code == 409
    assert "answered first" in late.json()["message"]
    again = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision['id']}/answer", json={"option": "no"}, headers=hub["owner"]
    )
    assert again.status_code == 409
    assert of_run_state(client, hub, run["id"]) == "running"
    inbox = client.post(path(run["id"], "inbox"), headers=hub["owner"]).json()["messages"]
    assert [m["decision_id"] for m in inbox] == [decision["id"]]
    assert "Chosen option: yes" in inbox[0]["text"]
    acked = client.post(path(run["id"], "inbox"), json={"ack": inbox[0]["id"]}, headers=hub["owner"]).json()
    assert acked["messages"] == []
    taken = client.get(path(run["id"], "decisions", decision["id"]), headers=hub["owner"]).json()
    assert (taken["state"], taken["answer_option"]) == ("answered", "yes") and taken["delivered_at"]


def test_an_answer_on_the_spot_wins_over_a_later_one_on_the_web(client, hub):
    run = registered(client, hub["owner"])
    decision = asked(client, hub["owner"], run["id"])
    spot = client.post(
        path(run["id"], "decisions", decision["id"], "answered"), json={"text": "did it here"}, headers=hub["owner"]
    )
    assert spot.status_code == 200, spot.text
    assert (spot.json()["state"], spot.json()["answer_text"]) == ("answered", "did it here")
    assert spot.json()["delivered_at"]
    web = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision['id']}/answer", json={"option": "yes"}, headers=hub["owner"]
    )
    assert web.status_code == 409
    assert "not open" in web.json()["message"]
    assert client.post(path(run["id"], "inbox"), headers=hub["owner"]).json()["messages"] == []
    bad = asked(client, hub["owner"], run["id"])
    assert (
        client.post(
            path(run["id"], "decisions", bad["id"], "answered"), json={"option": "maybe"}, headers=hub["owner"]
        ).status_code
        == 422
    )


def test_the_machine_long_polls_for_the_answer_and_asks_again_after_it(client, hub):
    run = registered(client, hub["owner"])
    decision = asked(client, hub["owner"], run["id"])
    url = path(run["id"], "decisions", decision["id"])
    open_now = client.get(url, headers=hub["owner"])
    assert open_now.status_code == 200 and open_now.json()["state"] == "open"
    held = client.get(url + "?wait_s=1", headers=hub["owner"])
    assert held.status_code == 200 and held.json()["state"] == "open"
    assert client.get(url + "?wait_s=60", headers=hub["owner"]).status_code == 422
    assert client.get(path(run["id"], "decisions", 99999), headers=hub["owner"]).status_code == 404
    answered = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision['id']}/answer", json={"text": "go"}, headers=hub["owner"]
    )
    assert answered.status_code == 200
    quick = client.get(url + "?wait_s=25", headers=hub["owner"])
    assert quick.json()["state"] == "answered" and quick.json()["answer_text"] == "go"


def test_decisions_of_an_ended_run_are_refused_and_its_open_ones_cancelled(client, hub):
    run = registered(client, hub["owner"])
    decision = asked(client, hub["owner"], run["id"])
    client.post(path(run["id"], "state"), json={"state": "ended"}, headers=hub["owner"])
    assert ask(client, hub["owner"], run["id"]).status_code == 409
    state = client.get(f"/v1/projects/{PROJECT}/decisions/{decision['id']}", headers=hub["owner"]).json()["state"]
    assert state == "cancelled"
    late = client.post(
        f"/v1/projects/{PROJECT}/decisions/{decision['id']}/answer", json={"option": "yes"}, headers=hub["owner"]
    )
    assert late.status_code == 409


def test_a_plan_write_that_names_the_run_records_it_on_the_revision(client, hub, hub_db):
    run = registered(client, hub["owner"])
    plan = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=hub["owner"]).json()
    patched = client.patch(
        f"/v1/projects/{PROJECT}/plans/{PLAN}",
        json={
            "section": "steps",
            "step": 2,
            "updates": {"status": "in_progress"},
            "if_revision": plan["revision"],
            "run_id": run["id"],
        },
        headers=hub["owner"],
    )
    assert patched.status_code == 200, patched.text
    revision = patched.json()["revision"]
    rev = tables.plan_revisions
    stored = sql(hub_db, select(rev.c.run_id).where(rev.c.plan_id == PLAN, rev.c.revision == revision))
    assert stored == [(run["id"],)]
    shown = run_of(client, hub["owner"], run["id"]).json()
    assert (shown["plan_id"], shown["plan_revision"], shown["step_key"]) == (PLAN, revision, "2")
    other = client.patch(
        f"/v1/projects/{PROJECT}/plans/{PLAN}",
        json={
            "section": "steps",
            "step": 2,
            "updates": {"status": "done"},
            "if_revision": revision,
            "run_id": run["id"],
        },
        headers=hub["other"],
    )
    assert other.status_code == 404
    put = client.put(
        f"/v1/projects/{PROJECT}/plans/{PLAN}",
        json={"body": patched_body(client, hub), "if_revision": revision, "run_id": run["id"]},
        headers=hub["owner"],
    )
    assert put.status_code == 200, put.text
    assert sql(hub_db, select(rev.c.run_id).where(rev.c.plan_id == PLAN, rev.c.revision == put.json()["revision"])) == [
        (run["id"],)
    ]


def patched_body(client, hub) -> dict:
    return client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=hub["owner"]).json()["body"]


def test_a_local_run_is_not_a_run_of_the_worker_protocol(client, hub, hub_db):
    run = registered(client, hub["owner"])
    asked(client, hub["owner"], run["id"])
    worker = add_worker(client, hub["owner"], "mac")
    assert claim(client, worker) is None
    for action in ("cancel", "takeover", "handback", "approve", "rerun"):
        response = client.post(f"/v1/projects/{PROJECT}/runs/{run['id']}/{action}", headers=hub["owner"])
        assert response.status_code == 409, (action, response.text)
        assert "local" in response.json()["message"]
    message = client.post(
        f"/v1/projects/{PROJECT}/runs/{run['id']}/messages", json={"text": "hi"}, headers=hub["owner"]
    )
    assert message.status_code in (404, 409)
    assert state_of(hub_db, run["id"]) == "waiting"


def test_the_reaper_neither_loses_nor_parks_nor_times_out_a_local_run_and_ends_a_silent_one(client, hub, hub_db):
    waiting = registered(client, hub["owner"])
    decision = asked(client, hub["owner"], waiting["id"])
    running = registered(client, hub["owner"], session="0199a3c1-0000-7000-8000-0000000000a3")
    r, d = tables.runs, tables.decisions
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    sql(
        hub_db,
        update(r).values(queued_at=long_ago, waiting_since=long_ago).where(r.c.kind == "local"),
    )
    swept = recover(client)
    assert swept == {"lost": 0, "failed": 0, "cancelled": 0, "parked": 0}
    assert state_of(hub_db, waiting["id"]) == "waiting"
    assert state_of(hub_db, running["id"]) == "running"
    assert sql(hub_db, select(d.c.state).where(d.c.id == decision["id"])) == [("open",)]
    assert client.portal.call(local_runs.end_silent, client.app.state.engine) == 0
    sql(hub_db, update(r).values(last_activity_at=long_ago).where(r.c.id == waiting["id"]))
    assert client.portal.call(local_runs.end_silent, client.app.state.engine) == 1
    assert state_of(hub_db, waiting["id"]) == "ended"
    assert of_run(hub_db, waiting["id"], "finished_at")[0] is not None
    assert state_of(hub_db, running["id"]) == "running"
    assert sql(hub_db, select(d.c.state).where(d.c.id == decision["id"])) == [("expired",)]
    assert client.portal.call(local_runs.end_silent, client.app.state.engine) == 0
    assert register(client, hub["owner"]).json()["state"] == "running"


def test_the_curator_figures_leave_local_runs_out(client, hub, hub_db):
    run = registered(client, hub["owner"], session="0199a3c1-0000-7000-8000-0000000000a4")
    decision = asked(client, hub["owner"], run["id"])
    client.post(path(run["id"], "state"), json={"state": "running", "plan_id": PLAN}, headers=hub["owner"])
    client.post(path(run["id"], "state"), json={"state": "ended"}, headers=hub["owner"])
    r = tables.runs
    ended = collect._ended_runs(1, [PLAN], datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
    assert sql(hub_db, select(r.c.id).where(*ended, r.c.id == run["id"])) == []
    assert decision["id"]

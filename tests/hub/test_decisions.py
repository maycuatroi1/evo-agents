"""Decisions of plan runs on the hub: the agent asking its owner, the owner answering, waiting and parking.

The checks step 4 of the plan-runs-and-decisions plan names: a worker's token cannot answer a decision, another writer
gets 403, a second answer gets 409; the answer goes to the run's inbox and the heartbeat counts it; waiting does not
use up the run's timeout; the reaper parks a run that waited 24 hours and cancels one parked 7 days; answering a parked
run queues a run that resumes it, pinned to the same worker, with its session id."""

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import runs
from evo_agents.hub.config import ConfigError, load_config
from evo_agents.hub.server.app import create_app
from tests.hub.fake_github import Account
from tests.hub.live import sql
from tests.hub.test_plan_runs import (
    PLAN,
    REPOS,
    dispatch_steps,
    dispatched_plan,
    fleet_step,
    fleet_worker,
    plan_body,
    push,
    ready,
    reported,
    started,
)
from tests.hub.test_runs import (
    GITHUB_IDS,
    OWNER,
    PROJECT,
    PROTOCOL,
    WEB_ONLY,
    audit_rows,
    beat,
    claim,
    control,
    dispatch_from,
    members,
    moved,
    moves,
    recover,
    report,
    state_of,
)
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

SESSION = "0199a3c1-0000-7000-8000-00000000000c"
QUESTION = "Which database should the dashboard read?"
OPTIONS = [
    {"key": "sqlite", "label": "Keep SQLite"},
    {"key": "postgres", "label": "Move to Postgres", "description": "One more service to run."},
]
ANSWER_TEXT = "Postgres, and keep the SQLite file as a backup for a week."
NOTHING_RECOVERED = {"lost": 0, "failed": 0, "cancelled": 0, "parked": 0}


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """test_plan_runs' ``hub``: the project, its members, and the plan fleet pushed by owner."""
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    return headers


def ask(client, worker: dict, run_id: int, **extra):
    body = {
        "category": "architecture",
        "question": QUESTION,
        "context": "## Why\nThe plan leaves it open.",
        "options": OPTIONS,
        "recommended": "postgres",
        "step_key": "2",
        **extra,
    }
    return client.post(f"/v1/worker/runs/{run_id}/decisions", json=body, headers=worker["headers"])


def asked(client, worker: dict, run_id: int, **extra) -> dict:
    response = ask(client, worker, run_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def answer(client, headers, decision_id: int, **body):
    return client.post(f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer", json=body, headers=headers)


def answered(client, headers, decision_id: int, **body) -> dict:
    response = answer(client, headers, decision_id, **body)
    assert response.status_code == 200, response.text
    return response.json()


def decision(client, headers, decision_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def run_view(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def inbox(client, worker: dict, run_id: int, ack: int | None = None) -> list[dict]:
    body = {} if ack is None else {"ack": ack}
    response = client.post(f"/v1/worker/runs/{run_id}/inbox", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def waiting(client, hub) -> tuple[dict, dict, dict]:
    """A plan run of fleet, started with a session, that reported step 2 in progress, asked a decision and waits."""
    worker, run = started(client, hub)
    moved(client, worker, run["id"], "running", session_id=SESSION)  # the same state: only the session id is kept
    reported(client, worker, run["id"], 2, "in_progress")
    asked_decision = asked(client, worker, run["id"])
    assert moved(client, worker, run["id"], "waiting")["state"] == "waiting"
    return worker, run, asked_decision


def park(hub_db, run_id: int) -> None:
    sql(hub_db, "UPDATE runs SET waiting_since = now() - interval '24 hours 1 second' WHERE id = %s", (run_id,))


# Asking


def test_the_agent_asks_a_decision_of_its_plan_run_and_the_owner_is_notified(client, hub, hub_db):
    worker, run = started(client, hub)
    run_id = run["id"]
    found = asked(client, worker, run_id)
    assert {key: found[key] for key in ("project", "run_id", "run_state", "plan_id", "step_key", "category")} == {
        "project": PROJECT,
        "run_id": run_id,
        "run_state": "running",
        "plan_id": PLAN,
        "step_key": "2",
        "category": "architecture",
    }
    assert found["options"] == [
        {"key": "sqlite", "label": "Keep SQLite", "description": None, "recommended": False},
        {
            "key": "postgres",
            "label": "Move to Postgres",
            "description": "One more service to run.",
            "recommended": True,
        },
    ]
    assert (found["recommended"], found["state"], found["owner"], found["answer_run_id"]) == (
        "postgres",
        "open",
        OWNER,
        None,
    )
    assert found["question"] == QUESTION and found["context"] == "## Why\nThe plan leaves it open."
    # readers of the plan see it; someone without a grant does not
    assert decision(client, hub["reader"], found["id"]) == found
    listed = client.get(f"/v1/projects/{PROJECT}/decisions", params={"state": "open"}, headers=hub["reader"]).json()
    assert ([item["id"] for item in listed["decisions"]], listed["total"]) == ([found["id"]], 1)
    assert client.get(f"/v1/projects/{PROJECT}/decisions/{found['id']}", headers=hub["stranger"]).status_code == 404
    assert client.get(f"/v1/projects/{PROJECT}/decisions/999999", headers=hub["reader"]).status_code == 404
    # the owner gets a notification of it, on the web; no one else does
    mine = client.get("/v1/me/notifications", headers=hub["owner"]).json()["notifications"]
    assert [(item["kind"], item["decision_id"], item["decision_state"], item["link"]) for item in mine] == [
        ("decision", found["id"], "open", f"/inbox?decision={found['id']}")
    ]
    assert mine[0]["title"] == f"Run #{run_id} asks: {QUESTION}"
    assert client.get("/v1/me/notifications", headers=hub["other"]).json()["notifications"] == []
    deliveries = sql(hub_db, "SELECT channel_id, state FROM notification_deliveries")
    assert deliveries == [(None, "pending")]
    events = sql(hub_db, "SELECT body FROM run_events WHERE run_id = %s AND kind = 'system'", (run_id,))
    assert events[-1][0]["decision"] == {"id": found["id"], "category": "architecture", "step": "2"}


@pytest.mark.parametrize(
    "extra",
    [
        {"category": "refactor"},
        {"options": OPTIONS[:1]},
        {"options": [{"key": f"k{n}", "label": f"Option {n}"} for n in range(7)]},
        {"options": [OPTIONS[0], {**OPTIONS[1], "key": "sqlite"}]},
        {"options": [OPTIONS[0], {**OPTIONS[1], "key": "two words"}]},
        {"options": [OPTIONS[0], {**OPTIONS[1], "label": "two\nlines"}]},
        {"recommended": "mysql"},
        {
            "options": [{"key": f"k{n}", "label": "Option", "description": "€" * 1000} for n in range(6)],
            "recommended": None,
        },
        {"question": "   "},
        {"context": "é" * (8 * 1024 + 1)},
        {"step_key": "99"},
    ],
)
def test_a_decision_the_route_does_not_take_gets_422(client, hub, hub_db, extra):
    worker, run = started(client, hub)
    response = ask(client, worker, run["id"], **extra)
    assert response.status_code == 422, response.text
    assert sql(hub_db, "SELECT count(*) FROM decisions") == [(0,)]


def test_only_the_worker_holding_a_plan_run_asks_and_a_run_keeps_at_most_twenty_open(client, hub, hub_db):
    worker, run = started(client, hub)
    other = fleet_worker(client, hub["owner"], "linux-box")
    assert ask(client, other, run["id"]).status_code == 404
    assert ask(client, worker, 999999).status_code == 404
    as_member = {**hub["owner"], **PROTOCOL}
    assert client.post(f"/v1/worker/runs/{run['id']}/decisions", json={}, headers=as_member).status_code == 403
    (single,) = dispatch_steps(client, hub["owner"], [2], plan_id="rollout").json()
    assert claim(client, other)["id"] == single["id"]
    one_step = ask(client, other, single["id"], step_key=None)
    assert one_step.status_code == 404 and "run of one step" in one_step.json()["message"]
    for _ in range(20):
        asked(client, worker, run["id"], step_key=None)
    refused = ask(client, worker, run["id"])
    assert refused.status_code == 409 and "20 decisions open" in refused.json()["message"]


# Answering


def test_only_the_runs_owner_answers_and_a_decision_is_answered_once(client, hub, hub_db):
    worker, run, found = waiting(client, hub)
    decision_id = found["id"]
    # the worker's own token never answers: the agent cannot decide for its owner
    by_worker = answer(client, worker["headers"], decision_id, option="postgres")
    assert by_worker.status_code == 403, by_worker.text
    for who, status in (("other", 403), ("reader", 403), ("admin", 403), ("stranger", 404)):
        assert answer(client, hub[who], decision_id, option="postgres").status_code == status, who
    assert "only owner, who dispatched its run" in answer(client, hub["other"], decision_id, option="sqlite").text
    for body in ({}, {"option": "mysql"}, {"text": "  "}, {"text": "x" * (4 * 1024 + 1)}):
        assert answer(client, hub["owner"], decision_id, **body).status_code == 422, body
    assert decision(client, hub["owner"], decision_id)["state"] == "open"

    done = answered(client, hub["owner"], decision_id, option="postgres", text=ANSWER_TEXT)
    assert {key: done[key] for key in ("state", "answer_option", "answer_text", "answered_by", "answer_run_id")} == {
        "state": "answered",
        "answer_option": "postgres",
        "answer_text": ANSWER_TEXT,
        "answered_by": OWNER,
        "answer_run_id": run["id"],
    }
    assert done["answered_at"] is not None and done["delivered_at"] is None
    again = answer(client, hub["owner"], decision_id, option="sqlite")
    assert again.status_code == 409 and "answered, not open" in again.json()["message"]
    # the audit row names the decision, its run and the option, never the text
    rows = [row for row in audit_rows(hub_db, "decision")]
    assert rows == [
        (
            "decision.answer",
            f"{PROJECT}/{PLAN}#2 decision:{decision_id} run:{run['id']} option=postgres",
            OWNER,
            PROJECT,
        )
    ]
    assert not [row for row in sql(hub_db, "SELECT target FROM audit") if "SQLite" in row[0] or "backup" in row[0]]
    # answering reads the decision's notification
    unread = client.get("/v1/me/notifications/count", headers=hub["owner"]).json()
    assert unread == {"unread": 0, "open_decisions": 0}


def test_the_answer_goes_to_the_inbox_the_heartbeat_counts_it_and_the_run_goes_on(client, hub, hub_db):
    worker, run, found = waiting(client, hub)
    run_id = run["id"]
    held = run_view(client, hub["owner"], run_id)
    assert held["state"] == "waiting" and held["waiting_since"] is not None
    (control_,) = beat(client, worker, runs_held=[run_id])["runs"]
    assert (control_["held"], control_["state"], control_["inbox"], control_["decisions"], control_["park"]) == (
        True,
        "waiting",
        0,
        1,
        False,
    )
    answered(client, hub["owner"], found["id"], text=ANSWER_TEXT)
    (control_,) = beat(client, worker, runs_held=[run_id])["runs"]
    assert (control_["inbox"], control_["decisions"]) == (1, 0)
    (message,) = inbox(client, worker, run_id)
    assert message["decision_id"] == found["id"] and message["sent_by"] == OWNER
    assert message["text"] == (
        f"Answer to decision #{found['id']} (architecture): {QUESTION}\nThe owner's answer:\n{ANSWER_TEXT}"
    )
    events = sql(hub_db, "SELECT body FROM run_events WHERE run_id = %s AND kind = 'user_message'", (run_id,))
    assert events == [
        ({"text": message["text"], "from": OWNER, "message_id": message["id"], "decision_id": found["id"]},)
    ]
    # the worker hands it to the agent, acknowledges it, and the run goes on
    assert inbox(client, worker, run_id, ack=message["id"]) == []
    assert decision(client, hub["owner"], found["id"])["delivered_at"] is not None
    going = moved(client, worker, run_id, "running")
    assert going["state"] == "running" and going["waiting_since"] is None
    assert [move[1:] for move in moves(hub_db, run_id)[-2:]] == [
        ("running", "waiting", "worker"),
        ("waiting", "running", "worker"),
    ]


def test_a_run_waits_only_for_an_answer_to_a_decision_of_its_own(client, hub, hub_db):
    worker, run = started(client, hub)
    refused = report(client, worker, run["id"], "waiting")
    assert refused.status_code == 409 and "no open decision" in refused.json()["message"]
    found = asked(client, worker, run["id"])
    answered(client, hub["owner"], found["id"], option="sqlite")
    # answered before the turn ended: the answer still waits for the agent, so the run may wait for it
    assert moved(client, worker, run["id"], "waiting")["state"] == "waiting"
    (message,) = inbox(client, worker, run["id"])
    assert message["text"].endswith("Chosen option: sqlite, Keep SQLite.")
    inbox(client, worker, run["id"], ack=message["id"])
    moved(client, worker, run["id"], "running")
    assert report(client, worker, run["id"], "waiting").status_code == 409


def test_waiting_does_not_count_toward_the_timeout(client, hub, hub_db):
    worker = fleet_worker(client, hub["owner"], "mac-mini")
    run = dispatched_plan(client, hub["owner"], timeout_h=2)
    run_id = run["id"]
    claim(client, worker)
    moved(client, worker, run_id, "running")
    # the agent ran an hour, then asked and ended its turn
    sql(hub_db, "UPDATE runs SET counted_at = now() - interval '3600 seconds' WHERE id = %s", (run_id,))
    found = asked(client, worker, run_id)
    moved(client, worker, run_id, "waiting")
    spent = run_view(client, hub["owner"], run_id)["run_seconds"]
    assert 3600 <= spent <= 3660
    # nearly a day of waiting, with heartbeats, uses none of the two hours
    sql(hub_db, "UPDATE runs SET waiting_since = now() - interval '23 hours' WHERE id = %s", (run_id,))
    beat(client, worker, runs_held=[run_id])
    assert recover(client) == NOTHING_RECOVERED
    assert run_view(client, hub["owner"], run_id)["run_seconds"] == spent
    assert state_of(hub_db, run_id) == "waiting"
    answered(client, hub["owner"], found["id"], option="postgres")
    moved(client, worker, run_id, "running")
    assert recover(client) == NOTHING_RECOVERED
    # the second hour of running time uses up the rest
    sql(hub_db, "UPDATE runs SET counted_at = now() - make_interval(secs => %s) WHERE id = %s", (7201 - spent, run_id))
    assert recover(client) == {**NOTHING_RECOVERED, "failed": 1}
    assert sql(hub_db, "SELECT state, error FROM runs WHERE id = %s", (run_id,)) == [
        ("failed", "it ran past its timeout of 120 minutes")
    ]


# Parking


def test_the_reaper_parks_a_run_waiting_a_day_and_cancels_one_parked_a_week(client, hub, hub_db):
    worker, run, found = waiting(client, hub)
    run_id = run["id"]
    assert recover(client) == NOTHING_RECOVERED
    park(hub_db, run_id)
    assert recover(client) == {**NOTHING_RECOVERED, "parked": 1}
    parked = run_view(client, hub["owner"], run_id)
    assert (parked["state"], parked["lease_expires_at"], parked["waiting_since"]) == ("parked", None, None)
    assert parked["parked_at"] is not None and parked["session_id"] == SESSION
    assert moves(hub_db, run_id)[-1][1:] == ("waiting", "parked", "reaper")
    reason = sql(
        hub_db,
        "SELECT body ->> 'reason' FROM run_events WHERE run_id = %s AND kind = 'state' ORDER BY seq DESC LIMIT 1",
        (run_id,),
    )
    assert reason == [("nobody answered its decision within 24 hours",)]
    # the heartbeat lets the worker go of it without cancelling, and its slot is free for another run
    (control_,) = beat(client, worker, runs_held=[run_id])["runs"]
    assert {key: control_[key] for key in ("held", "state", "cancel", "park", "decisions")} == {
        "held": False,
        "state": "parked",
        "cancel": False,
        "park": True,
        "decisions": 1,
    }
    (single,) = dispatch_steps(client, hub["owner"], [2], plan_id="rollout").json()
    assert claim(client, worker)["id"] == single["id"]
    assert decision(client, hub["owner"], found["id"])["run_state"] == "parked"
    assert ready(client, hub["reader"])["plan_run"]["state"] == "parked"  # still the plan's active run
    assert fleet_step(client, hub["owner"], 2)["status"] == "in_progress"

    sql(hub_db, "UPDATE runs SET parked_at = now() - interval '6 days 23 hours' WHERE id = %s", (run_id,))
    assert recover(client) == NOTHING_RECOVERED
    sql(hub_db, "UPDATE runs SET parked_at = now() - interval '7 days 1 second' WHERE id = %s", (run_id,))
    assert recover(client) == {**NOTHING_RECOVERED, "cancelled": 1}
    assert state_of(hub_db, run_id) == "cancelled"
    assert moves(hub_db, run_id)[-1][1:] == ("parked", "cancelled", "reaper")
    assert decision(client, hub["owner"], found["id"])["state"] == "expired"
    back = fleet_step(client, hub["owner"], 2)
    assert (back["status"], back["note"]) == (
        "pending",
        f"run #{run_id} was cancelled: it stayed parked for 7 days without an answer",
    )
    late = answer(client, hub["owner"], found["id"], option="postgres")
    assert late.status_code == 409 and "expired" in late.json()["message"]
    assert ready(client, hub["reader"])["plan_run"] is None


def test_a_waiting_run_whose_cancel_was_asked_is_cancelled_instead_of_parked(client, hub, hub_db):
    worker, run, found = waiting(client, hub)
    assert control(client, hub["owner"], run["id"], "cancel").json()["state"] == "waiting"
    park(hub_db, run["id"])
    assert recover(client) == {**NOTHING_RECOVERED, "cancelled": 1}
    assert state_of(hub_db, run["id"]) == "cancelled"
    assert decision(client, hub["owner"], found["id"])["state"] == "cancelled"


def test_answering_a_parked_run_queues_a_run_that_resumes_it_on_the_same_worker(client, hub, hub_db):
    worker, run, found = waiting(client, hub)
    parked_id = run["id"]
    later = asked(client, worker, parked_id, category="deploy", question="Deploy to staging now?", step_key=None)
    park(hub_db, parked_id)
    recover(client)
    spent = run_view(client, hub["owner"], parked_id)["run_seconds"]

    done = answered(client, hub["owner"], found["id"], option="postgres")
    new_id = done["answer_run_id"]
    assert new_id is not None and new_id > parked_id
    assert (done["state"], done["run_id"]) == ("answered", parked_id)
    # the parked run is done, resumed by the new one
    old = run_view(client, hub["owner"], parked_id)
    assert old["state"] == "done" and old["finished_at"] is not None
    assert moves(hub_db, parked_id)[-1][1:] == ("parked", "done", "owner")
    reason = sql(
        hub_db,
        "SELECT body ->> 'reason' FROM run_events WHERE run_id = %s AND kind = 'state' ORDER BY seq DESC LIMIT 1",
        (parked_id,),
    )
    assert reason == [(f"resumed as #{new_id}",)]
    new = run_view(client, hub["owner"], new_id)
    assert {
        key: new[key]
        for key in (
            "kind",
            "state",
            "pinned_worker_id",
            "resume_of_run_id",
            "session_id",
            "repos",
            "run_seconds",
            "runtime",
            "requested_runtime",
            "attempt",
            "timeout_min",
            "dispatched_by",
            "dispatched_via",
        )
    } == {
        "kind": "plan",
        "state": "queued",
        "pinned_worker_id": worker["id"],
        "resume_of_run_id": parked_id,
        "session_id": SESSION,
        "repos": REPOS,
        "run_seconds": spent,
        "runtime": "claude-code",
        "requested_runtime": "claude-code",
        "attempt": 1,
        "timeout_min": 4 * 60,
        "dispatched_by": OWNER,
        "dispatched_via": "machine",  # the parked run's, which its owner dispatched with a token
    }
    assert ready(client, hub["reader"])["plan_run"]["id"] == new_id
    # the decision still open goes with the run that resumes, and may be answered there
    assert decision(client, hub["owner"], later["id"])["run_id"] == new_id
    assert fleet_step(client, hub["owner"], 2)["status"] == "in_progress"  # the work goes on where it was
    assert audit_rows(hub_db, "decision")[-1][1] == (
        f"{PROJECT}/{PLAN}#2 decision:{found['id']} run:{parked_id} option=postgres resumed as run:{new_id}"
    )
    # the heartbeat lets go of the old run without cancelling it, since the new one needs its worktrees
    (control_,) = beat(client, worker, runs_held=[parked_id])["runs"]
    assert (control_["held"], control_["state"], control_["cancel"], control_["park"]) == (False, "done", False, True)

    # only the worker the parked run was on claims it, with the session to resume
    elsewhere = fleet_worker(client, hub["owner"], "linux-box")
    assert claim(client, elsewhere) is None
    spec = claim(client, worker)
    assert (spec["id"], spec["kind"], spec["resume_of_run_id"], spec["session_id"]) == (
        new_id,
        "plan",
        parked_id,
        SESSION,
    )
    moved(client, worker, new_id, "running")
    (message,) = inbox(client, worker, new_id)
    assert message["decision_id"] == found["id"]
    assert message["text"].endswith("Chosen option: postgres, Move to Postgres.")
    assert decision(client, hub["owner"], later["id"])["state"] == "open"
    answered(client, hub["owner"], later["id"], option="sqlite")
    assert [item["decision_id"] for item in inbox(client, worker, new_id)] == [found["id"], later["id"]]


def test_a_run_that_ends_another_way_cancels_its_open_decisions(client, hub, hub_db):
    worker, run = started(client, hub)
    found = asked(client, worker, run["id"])
    moved(client, worker, run["id"], "failed", error="the agent stopped")
    assert decision(client, hub["owner"], found["id"])["state"] == "cancelled"
    refused = answer(client, hub["owner"], found["id"], option="postgres")
    assert refused.status_code == 409 and "cancelled, not open" in refused.json()["message"]

    # the owner cancels a parked run: its decisions are cancelled with it
    second = dispatched_plan(client, hub["owner"])
    assert claim(client, worker)["id"] == second["id"]
    moved(client, worker, second["id"], "running")
    other = asked(client, worker, second["id"])
    moved(client, worker, second["id"], "waiting")
    park(hub_db, second["id"])
    recover(client)
    assert control(client, hub["owner"], second["id"], "cancel").json()["state"] == "cancelled"
    assert decision(client, hub["owner"], other["id"])["state"] == "cancelled"


def test_revoking_a_worker_cancels_the_runs_parked_on_it(client, hub, hub_db):
    worker, run, found = waiting(client, hub)
    park(hub_db, run["id"])
    recover(client)
    assert client.post(f"/v1/workers/{worker['id']}/revoke", headers=hub["owner"]).status_code == 200
    assert state_of(hub_db, run["id"]) == "cancelled"
    assert decision(client, hub["owner"], found["id"])["state"] == "cancelled"
    reason = sql(
        hub_db,
        "SELECT body ->> 'reason' FROM run_events WHERE run_id = %s AND kind = 'state' ORDER BY seq DESC LIMIT 1",
        (run["id"],),
    )
    assert reason == [("its worker mac-mini was revoked, and only it has the session of the parked run",)]


# A worker set to take runs dispatched from the web only


@pytest.fixture
def web_client(hub_db, tmp_path, github):
    """``client`` on a hub that also signs in on the web."""
    config = live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def web_hub(web_client, github) -> dict:
    """``hub`` on ``web_client``, with ``owner_web``: the owner's web session and its CSRF header."""
    headers = members(web_client, github)
    push(web_client, headers["owner"], plan_body())
    session = web_sign_in(web_client, github, Account(OWNER, GITHUB_IDS[OWNER]))
    return headers | {"owner_web": {**cookie(session), "X-Evo-CSRF": csrf_for(web_client, session)}}


def message(client, headers, run_id: int, text: str = "Use Postgres."):
    return client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/messages", json={"text": text}, headers=headers)


def test_a_token_cannot_steer_a_run_on_a_worker_that_takes_runs_from_the_web_only(web_client, web_hub, hub_db):
    client, hub = web_client, web_hub
    worker, run, found = waiting(client, hub)  # dispatched with a token before the owner set the worker to web
    run_id = run["id"]
    dispatch_from(client, hub, worker, "web")
    held_by = f"run {run_id} is held by worker mac-mini, which {WEB_ONLY}, as its owner set it, so a token cannot"

    # Held there: neither a message nor an answer from a token reaches its agent.
    refused = answer(client, hub["owner"], found["id"], option="postgres")
    assert refused.status_code == 403
    assert refused.json()["message"] == f"{held_by} answer its decisions: answer on the web; nothing was answered"
    refused = message(client, hub["owner"], run_id)
    assert refused.status_code == 403
    assert refused.json()["message"] == f"{held_by} send its agent a message: send it from the web; nothing was sent"
    assert decision(client, hub["owner"], found["id"])["state"] == "open"
    assert inbox(client, worker, run_id) == []
    actions = [row[0] for row in audit_rows(hub_db, "decision") + audit_rows(hub_db, "run")]
    assert "decision.answer" not in actions and "run.message" not in actions

    # Parked there: a token's answer would resume it on that worker, so nothing is answered and nothing queued.
    park(hub_db, run_id)
    recover(client)
    refused = answer(client, hub["owner"], found["id"], option="postgres")
    assert refused.status_code == 403
    assert refused.json()["message"].startswith(f"run {run_id} was parked on worker mac-mini, which {WEB_ONLY}")
    assert state_of(hub_db, run_id) == "parked"
    assert sql(hub_db, "SELECT count(*) FROM runs WHERE resume_of_run_id = %s", (run_id,)) == [(0,)]

    # From the web: the answer resumes it, pinned to the worker, where a token's message is refused too.
    done = answered(client, hub["owner_web"], found["id"], option="postgres")
    new_id = done["answer_run_id"]
    refused = message(client, hub["owner"], new_id)
    assert refused.status_code == 403 and refused.json()["message"].startswith(
        f"run {new_id} is pinned to worker mac-mini, which {WEB_ONLY}"
    )
    assert message(client, hub["owner_web"], new_id).status_code == 201

    # Once the owner lets the worker take runs from anywhere again, a token may steer it.
    dispatch_from(client, hub, worker, "any")
    assert message(client, hub["owner"], new_id).status_code == 201


def test_the_decision_limits_are_the_models():
    assert (runs.DECISION_WAIT_SECONDS, runs.PARKED_DAYS, runs.MAX_ANSWER_BYTES) == (24 * 3600, 7, 4 * 1024)


def test_evo_hub_decision_wait_seconds_sets_how_long_a_run_waits_before_the_reaper_parks_it():
    env = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(env).decision_wait_seconds == runs.DECISION_WAIT_SECONDS
    assert load_config({**env, "EVO_HUB_DECISION_WAIT_SECONDS": "3"}).decision_wait_seconds == 3
    for value in ("0", "604801", "soon"):
        with pytest.raises(ConfigError) as caught:
            load_config({**env, "EVO_HUB_DECISION_WAIT_SECONDS": value})
        assert caught.value.variable == "EVO_HUB_DECISION_WAIT_SECONDS"

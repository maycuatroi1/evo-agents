"""The chat of an author run on the hub: the agent's messages (POST /v1/worker/runs/{id}/chat) and its owner's (POST
.../runs/{id}/messages), the wait for a reply, parking and resuming, the owner's end of the chat (POST
.../runs/{id}/finish), the chat as GET .../runs/{id}/chat returns it, and the notice author_waiting.

The checks step 3 of the hub-plan-authoring plan names on the hub: each message of the agent and of its owner is one
message of the run's chat, in order, the agent's between the owner's; a turn that ends moves the run to waiting with no
decision of its own, the wait does not count toward its timeout, and the reaper parks it after a day as a run waiting
for a decision; the owner's reply goes to the agent's session, and to a parked run through the run that resumes it on
the same worker; the owner's end of the chat ends the run done; the run's start of a wait sends its owner the notice
author_waiting, on the web and on Telegram, a restricted project's with its name, the kind and the link alone; and a
message to a run of another kind keeps the rule it had."""

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.hub import author, tables
from evo_agents.hub.server import telegram as server_telegram
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_author_runs import CHECKOUTS, KINDS, author_worker, dispatched_author, publish_skill
from tests.hub.test_decisions import NOTHING_RECOVERED, inbox, last_reason, park, run_view, set_run
from tests.hub.test_runs import (
    OWNER,
    PROJECT,
    add_worker,
    audit_rows,
    claim,
    control,
    dispatched,
    members,
    moved,
    moves,
    recover,
    report,
    state_of,
)
from tests.hub.test_telegram import (
    bot,  # noqa: F401 (a fixture)
    deliver,
    linked,
    telegram_config,
    unrestricted,
    web,
)

SESSION = "0199a3c1-0000-7000-8000-0000000000aa"
QUESTION = "Should the wait helper poll, or subscribe to the run's events?"
REPLY = "Poll every second; the events come later."
PUT_SAID = "Put plan wait-helper at revision 1. I chose the order of the steps myself."


@pytest.fixture
def client(hub_db, tmp_path, github, s3, bot):  # noqa: F811
    config = telegram_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        client.app.state.telegram_inbound = server_telegram.InboundLimit(updates=1000)
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """test_runs' members, the project's hub sink not restricted, and a web session of the owner's."""
    headers = members(client, github)
    unrestricted(hub_db)
    headers["owner_web"] = web(client, hub_db, OWNER)
    return headers


def running(client, hub, tmp_path) -> tuple[dict, int]:
    """An author run of the owner's on their worker, claimed and running in session SESSION: (worker, run id)."""
    publish_skill(client, hub, tmp_path, "The author mode: ask in the chat.")
    worker = author_worker(client, hub["owner"], "mac-mini")
    run_id = dispatched_author(client, hub["owner"], worker["id"])["id"]
    assert claim(client, worker)["id"] == run_id
    moved(client, worker, run_id, "running", session_id=SESSION)
    return worker, run_id


def beat(client, worker: dict, runs_held=()) -> dict:
    """A heartbeat of ``worker``, whose daemon runs author runs, naming the runs it holds; the hub's answer."""
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": list(runs_held),
        "run_kinds": KINDS,
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def said(client, worker: dict, run_id: int, text: str):
    return client.post(f"/v1/worker/runs/{run_id}/chat", json={"text": text}, headers=worker["headers"])


def asked(client, worker: dict, run_id: int, text: str = QUESTION) -> int:
    """The agent's turn ends with ``text``: the worker posts it to the chat and reports waiting; its seq."""
    response = said(client, worker, run_id, text)
    assert response.status_code == 201, response.text
    assert moved(client, worker, run_id, "waiting")["state"] == "waiting"
    return response.json()["seq"]


def send(client, headers, run_id: int, text: str = REPLY):
    return client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/messages", json={"text": text}, headers=headers)


def sent(client, headers, run_id: int, text: str = REPLY) -> dict:
    response = send(client, headers, run_id, text)
    assert response.status_code == 201, response.text
    return response.json()


def chat(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/chat", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def lines(found: dict) -> list[tuple]:
    return [(item["run_id"], item["author"], item["login"], item["text"]) for item in found["messages"]]


def notices(client, headers) -> list[dict]:
    response = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=headers)
    assert response.status_code == 200, response.text
    return [item for item in response.json()["notifications"] if item["notice_kind"] == "author_waiting"]


# The chat


def test_the_agents_last_messages_and_the_owners_replies_make_the_author_chat_in_order(client, hub, hub_db, tmp_path):
    worker, run_id = running(client, hub, tmp_path)
    first = asked(client, worker, run_id)
    reply = sent(client, hub["owner"], run_id)
    assert (reply["run_id"], reply["sent_by"], reply["text"]) == (run_id, OWNER, REPLY)
    assert reply["seq"] > first
    (message,) = inbox(client, worker, run_id)
    assert message["text"] == REPLY and message["decision_id"] is None
    moved(client, worker, run_id, "running")
    inbox(client, worker, run_id, ack=message["id"])
    asked(client, worker, run_id, PUT_SAID)

    found = chat(client, hub["reader"], run_id)  # a reader of the run reads its chat
    assert lines(found) == [
        (run_id, "agent", None, QUESTION),
        (run_id, "owner", OWNER, REPLY),
        (run_id, "agent", None, PUT_SAID),
    ]
    assert [item["seq"] for item in found["messages"]] == sorted(item["seq"] for item in found["messages"])
    assert (found["run_id"], found["state"], found["status"], found["owner"], found["more"]) == (
        run_id,
        "waiting",
        "waiting",
        OWNER,
        False,
    )
    assert found["plan_id"] is None and found["plan_revision"] is None
    assert_json_keys("hub run chat", found)
    # the agent's messages are in the run's log too, as the hub's own system events
    events = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/events", headers=hub["reader"]).json()["events"]
    assert [e["body"]["text"] for e in events if e["kind"] == "system" and e["body"].get("chat") == "agent"] == [
        QUESTION,
        PUT_SAID,
    ]

    stranger = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/chat", headers=hub["stranger"])
    assert stranger.status_code == 404
    over = said(client, worker, run_id, "é" * (author.MAX_CHAT_BYTES // 2 + 1))
    assert over.status_code == 422
    assert said(client, worker, run_id, " \n ").status_code == 422


def test_only_the_worker_holding_an_author_run_posts_to_a_chat_and_a_run_of_another_kind_has_none(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)
    other = author_worker(client, hub["other"], "their-mac")
    assert said(client, other, run_id, QUESTION).status_code == 404
    (step,) = dispatched(client, hub["owner"], [2])
    steps_worker = add_worker(client, hub["owner"], "linux-box", checkouts=CHECKOUTS)
    refused = said(client, steps_worker, step["id"], QUESTION)
    assert refused.status_code == 404
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{step['id']}/chat", headers=hub["owner"])
    assert shown.status_code == 404 and "only an author run has a chat" in shown.json()["message"]
    assert lines(chat(client, hub["owner"], run_id)) == []


# Waiting, parking and resuming


def test_an_author_run_waits_for_a_reply_without_a_decision_and_the_wait_does_not_count_toward_its_timeout(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)
    set_run(hub_db, run_id, counted_at=func.now() - timedelta(seconds=1800))
    asked(client, worker, run_id)
    spent = run_view(client, hub["owner"], run_id)["run_seconds"]
    assert 1800 <= spent <= 1860
    shown = run_view(client, hub["owner"], run_id)
    assert shown["waiting_since"] is not None and shown["state"] == "waiting"
    # nearly a day of waiting, with heartbeats, keeps the slot and uses none of the run's time
    set_run(hub_db, run_id, waiting_since=func.now() - timedelta(hours=23))
    (control_,) = beat(client, worker, runs_held=[run_id])["runs"]
    assert (control_["held"], control_["state"], control_["park"], control_["finish"]) == (
        True,
        "waiting",
        False,
        False,
    )
    assert recover(client) == NOTHING_RECOVERED
    assert run_view(client, hub["owner"], run_id)["run_seconds"] == spent
    dispatched_author(client, hub["owner"], worker["id"])
    assert claim(client, worker) is None, "the waiting run holds the worker's one slot"
    # the reply sets it going again
    sent(client, hub["owner"], run_id)
    (control_,) = beat(client, worker, runs_held=[run_id])["runs"]
    assert control_["inbox"] == 1
    assert moved(client, worker, run_id, "running")["state"] == "running"


def test_the_reaper_parks_an_author_run_waiting_a_day_and_a_reply_resumes_it_in_its_session_on_the_same_worker(
    client, hub, hub_db, tmp_path
):
    worker, parked_id = running(client, hub, tmp_path)
    asked(client, worker, parked_id)
    park(hub_db, parked_id)
    assert recover(client) == {**NOTHING_RECOVERED, "parked": 1}
    assert state_of(hub_db, parked_id) == "parked"
    assert moves(hub_db, parked_id)[-1][1:] == ("waiting", "parked", "reaper")
    assert last_reason(hub_db, parked_id) == [("nobody replied in its chat within 24 hours",)]
    (control_,) = beat(client, worker, runs_held=[parked_id])["runs"]
    assert (control_["held"], control_["state"], control_["cancel"], control_["park"]) == (False, "parked", False, True)
    assert chat(client, hub["owner"], parked_id)["status"] == "waiting"
    spent = run_view(client, hub["owner"], parked_id)["run_seconds"]

    reply = sent(client, hub["owner"], parked_id)
    new_id = reply["run_id"]
    assert new_id > parked_id
    old = run_view(client, hub["owner"], parked_id)
    assert old["state"] == "done" and moves(hub_db, parked_id)[-1][1:] == ("parked", "done", "owner")
    assert last_reason(hub_db, parked_id) == [(f"resumed as #{new_id}",)]
    new = run_view(client, hub["owner"], new_id)
    assert {
        key: new[key]
        for key in ("kind", "state", "pinned_worker_id", "resume_of_run_id", "session_id", "run_seconds", "request")
    } == {
        "kind": "author",
        "state": "queued",
        "pinned_worker_id": worker["id"],
        "resume_of_run_id": parked_id,
        "session_id": SESSION,
        "run_seconds": spent,
        "request": old["request"],
    }
    assert (new["repos"], new["title"], new["model"], new["timeout_min"]) == (
        old["repos"],
        old["title"],
        old["model"],
        old["timeout_min"],
    )
    (row,) = [row for row in audit_rows(hub_db, "run") if row[0] == "run.message"]
    assert row[1].endswith(f"resumed as run:{new_id}")

    # only the worker the parked run was on claims it, with the session to go on in, and the reply in its inbox
    elsewhere = author_worker(client, hub["owner"], "linux-box")
    assert claim(client, elsewhere) is None
    spec = claim(client, worker)
    assert (spec["id"], spec["kind"], spec["resume_of_run_id"], spec["session_id"]) == (
        new_id,
        "author",
        parked_id,
        SESSION,
    )
    moved(client, worker, new_id, "running")
    (message,) = inbox(client, worker, new_id)
    assert message["text"] == REPLY
    asked(client, worker, new_id, PUT_SAID)

    # one chat over both runs, read from either
    for shown in (parked_id, new_id):
        found = chat(client, hub["owner"], shown)
        assert lines(found) == [
            (parked_id, "agent", None, QUESTION),
            (new_id, "owner", OWNER, REPLY),
            (new_id, "agent", None, PUT_SAID),
        ]
        assert (found["run_id"], found["state"], found["status"]) == (new_id, "waiting", "waiting")


def test_an_author_run_parked_a_week_is_cancelled_and_a_late_reply_is_409(client, hub, hub_db, tmp_path):
    worker, run_id = running(client, hub, tmp_path)
    asked(client, worker, run_id)
    park(hub_db, run_id)
    recover(client)
    set_run(hub_db, run_id, parked_at=func.now() - timedelta(days=7, seconds=1))
    assert recover(client) == {**NOTHING_RECOVERED, "cancelled": 1}
    late = send(client, hub["owner"], run_id)
    assert late.status_code == 409
    assert late.json()["message"] == (
        f"run {run_id} is cancelled: its chat is over; dispatch a new author run to go on"
    )
    assert chat(client, hub["owner"], run_id)["status"] == "ended"


# The end of the chat


def test_the_owner_ends_the_chat_of_an_author_run_and_it_ends_done(client, hub, hub_db, tmp_path):
    worker, run_id = running(client, hub, tmp_path)
    asked(client, worker, run_id)
    assert control(client, hub["other"], run_id, "finish").status_code == 403
    ended = control(client, hub["owner"], run_id, "finish")
    assert ended.status_code == 200, ended.text
    assert ended.json()["state"] == "waiting" and ended.json()["finish_requested_at"] is not None
    assert control(client, hub["owner"], run_id, "finish").status_code == 200  # asked again: nothing changes
    assert [row for row in audit_rows(hub_db, "run") if row[0] == "run.finish"] == [
        ("run.finish", f"{PROJECT}/author run:{run_id}", OWNER, PROJECT)
    ]
    (control_,) = beat(client, worker, runs_held=[run_id])["runs"]
    assert (control_["held"], control_["finish"], control_["cancel"]) == (True, True, False)
    refused = send(client, hub["owner"], run_id)
    assert refused.status_code == 409 and "you ended its chat" in refused.json()["message"]

    # the worker ends it done; a wait after the end is refused
    moved(client, worker, run_id, "running")
    again = report(client, worker, run_id, "waiting")
    assert again.status_code == 409 and "its owner ended the chat" in again.json()["message"]
    done = moved(client, worker, run_id, "verifying", "done", summary="Wrote plan wait-helper.")
    assert done["state"] == "done" and done["error"] is None
    assert chat(client, hub["owner"], run_id)["status"] == "ended"
    assert control(client, hub["owner"], run_id, "finish").status_code == 409


def test_ending_the_chat_of_a_parked_author_run_ends_it_done_at_once_and_a_queued_one_is_cancelled_instead(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)
    asked(client, worker, run_id)
    park(hub_db, run_id)
    recover(client)
    ended = control(client, hub["owner"], run_id, "finish")
    assert ended.status_code == 200 and ended.json()["state"] == "done"
    assert moves(hub_db, run_id)[-1][1:] == ("parked", "done", "owner")
    assert last_reason(hub_db, run_id) == [(f"{OWNER} ended the chat",)]

    queued = dispatched_author(client, hub["owner"], worker["id"])["id"]
    refused = control(client, hub["owner"], queued, "finish")
    assert refused.status_code == 409 and refused.json()["message"].endswith("cancel it instead")
    assert state_of(hub_db, queued) == "queued"


def test_a_message_or_an_end_of_chat_to_a_run_of_another_kind_keeps_the_rule_it_had(client, hub, hub_db):
    (step,) = dispatched(client, hub["owner"], [2])
    assert send(client, hub["owner"], step["id"]).status_code == 201  # queued: its agent reads it once it starts
    refused = control(client, hub["owner"], step["id"], "finish")
    assert refused.status_code == 409
    assert refused.json()["message"] == (
        f"run {step['id']} is a step run: only an author run has a chat to end; cancel it instead"
    )
    control(client, hub["owner"], step["id"], "cancel")
    late = send(client, hub["owner"], step["id"])
    assert late.status_code == 409
    assert late.json()["message"] == (
        f"run {step['id']} is cancelled: a message goes to a run that is queued or held by its worker"
    )


# The notice


def test_an_author_run_that_waits_notifies_its_owner_on_the_web_and_on_telegram(client, hub, hub_db, tmp_path, bot):  # noqa: F811
    linked(client, hub["owner_web"])
    worker, run_id = running(client, hub, tmp_path)
    asked(client, worker, run_id)
    (notice,) = notices(client, hub["owner"])
    link = f"/p/{PROJECT}/runs/{run_id}?tab=chat"
    assert (notice["kind"], notice["title"], notice["body"], notice["link"], notice["run_id"]) == (
        "notice",
        f"Author run #{run_id} waits for your reply",
        QUESTION,
        link,
        run_id,
    )
    assert notices(client, hub["other"]) == []
    assert deliver(client)["failed"] == 0
    message = bot.fake.sent()[-1]
    assert message["text"].startswith(f"<b>An author run waits for your reply</b> in {PROJECT}")
    assert "Should the wait helper poll" in message["text"] and f"https://hub.test{link}" in message["text"]

    # the next wait reads the one before: one notice of the run waits at a time
    moved(client, worker, run_id, "running")
    asked(client, worker, run_id, PUT_SAID)
    listed = notices(client, hub["owner"])
    assert [(item["body"], item["read_at"] is None) for item in listed] == [(PUT_SAID, True), (QUESTION, False)]


def test_a_restricted_projects_author_notice_on_telegram_holds_its_name_the_kind_and_the_link_alone(
    client,
    hub,
    hub_db,
    tmp_path,
    bot,  # noqa: F811
):
    linked(client, hub["owner_web"])
    for clearance in ({"level": "customer", "location": "any"}, {"level": "internal", "location": "domestic-only"}):
        unrestricted(hub_db, clearance)
        worker = author_worker(client, hub["owner"], f"mac-{clearance['level']}")
        if not sql(hub_db, select(tables.skills.c.id).limit(1)):
            publish_skill(client, hub, tmp_path, "The author mode.")
        run_id = dispatched_author(client, hub["owner"], worker["id"])["id"]
        assert claim(client, worker)["id"] == run_id
        moved(client, worker, run_id, "running")
        asked(client, worker, run_id)
        assert deliver(client)["failed"] == 0
        message = bot.fake.sent()[-1]
        url = f"https://hub.test/p/{PROJECT}/runs/{run_id}?tab=chat"
        assert message["text"] == f"<b>{PROJECT}</b>: An author run waits for your reply.\n\n{url}", clearance
        assert QUESTION not in message["text"] and "Author run #" not in message["text"]
        assert message["reply_markup"] == {"inline_keyboard": [[{"text": "Open on the hub", "url": url}]]}
        # the web, on the hub itself, shows the agent's message
        assert notices(client, hub["owner"])[0]["body"] == QUESTION


def test_home_lists_the_owners_author_runs_waiting_for_a_reply_with_the_agents_last_message(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)

    def waiting(headers) -> list[dict]:
        response = client.get("/v1/me/overview", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["author_waiting"]

    assert waiting(hub["owner"]) == [], "an author run whose agent works waits for nobody"
    asked(client, worker, run_id)
    (shown,) = waiting(hub["owner"])
    assert (shown["id"], shown["project"], shown["state"], shown["plan_id"]) == (run_id, PROJECT, "waiting", None)
    assert (shown["message"], shown["worker"]) == (QUESTION, "mac-mini")
    assert shown["waiting_since"] is not None and shown["parked_at"] is None
    assert waiting(hub["other"]) == [], "only its owner replies, so it waits for nobody else"
    park(hub_db, run_id)
    assert recover(client) == {**NOTHING_RECOVERED, "parked": 1}
    (parked,) = waiting(hub["owner"])
    assert (parked["id"], parked["state"]) == (run_id, "parked") and parked["parked_at"] is not None
    new_id = sent(client, hub["owner"], run_id)["run_id"]
    assert waiting(hub["owner"]) == [], f"the reply set it going again in run {new_id}"

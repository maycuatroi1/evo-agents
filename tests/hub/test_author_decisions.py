"""The decisions of an author run on the hub: its agent asks them as a plan run's does, before it puts a plan or after.

The checks the author-run-decisions plan names on the hub (criteria a1, a2 and the hub's part of a4): an author run
the worker holds asks a decision with the same bounds as a plan run, on the plan it has when it asks, none (plan_id
null) before its first put, a step_key only once it has a plan with that step (422 otherwise), while a step, review or
judge run still gets 404; whoever reads the run reads its decisions, on the list, the page of one, Home and the MCP
tool decision_list, no more and no fewer; its owner alone answers, and the answer goes to the run's inbox, not to its
chat; a run that ends another way cancels its open decisions. The notification of a decision without a plan names the
author run and its title on the web and on Telegram, with a button per option; a run that waits with a decision open
sends no author_waiting notice and stands on Home through its decision alone. A parked author run is resumed only when
none of its decisions is open, or on a reply in its chat, and the run that resumes it takes every answer given while it
was parked."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.core_plugins.telegram import server as server_telegram
from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub.live import bearer, sql
from tests.hub.test_author_chat import (
    QUESTION as SAID,
)
from tests.hub.test_author_chat import (
    REPLY,
    asked,
    chat,
    lines,
    notices,
    running,
    sent,
)
from tests.hub.test_author_plans import new_plan, put
from tests.hub.test_author_runs import CHECKOUTS, dispatched_author
from tests.hub.test_decisions import (
    NOTHING_RECOVERED,
    QUESTION,
    answer,
    answered,
    decision,
    inbox,
    park,
    run_view,
)
from tests.hub.test_decisions import ask as ask_of
from tests.hub.test_mcp import MCP_HEADERS
from tests.hub.test_runs import (
    OWNER,
    PROJECT,
    add_worker,
    claim,
    control,
    dispatched,
    members,
    moved,
    recover,
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


@pytest.fixture
def client(hub_db, tmp_path, github, s3, bot):  # noqa: F811
    config = telegram_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        client.app.state.telegram_inbound = server_telegram.InboundLimit(updates=1000)
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """test_author_chat's ``hub``: test_runs' members, the project's hub sink not restricted, a web session of the
    owner's."""
    headers = members(client, github)
    unrestricted(hub_db)
    headers["owner_web"] = web(client, hub_db, OWNER)
    return headers


def ask(client, worker: dict, run_id: int, **extra):
    """The agent of ``run_id`` asks a decision about no step, unless ``extra`` names one."""
    return ask_of(client, worker, run_id, **{"step_key": None, **extra})


def asks(client, worker: dict, run_id: int, **extra) -> dict:
    response = ask(client, worker, run_id, **extra)
    assert response.status_code == 201, response.text
    return response.json()


def another(client, hub, worker: dict) -> int:
    """Another author run of the owner's on ``worker``, claimed and running: its id."""
    run_id = dispatched_author(client, hub["owner"], worker["id"])["id"]
    assert claim(client, worker)["id"] == run_id
    moved(client, worker, run_id, "running")
    return run_id


def listed(client, headers, **params) -> tuple[list[int], int]:
    response = client.get(f"/v1/projects/{PROJECT}/decisions", params=params, headers=headers)
    assert response.status_code == 200, response.text
    found = response.json()
    return [item["id"] for item in found["decisions"]], found["total"]


def overview(client, headers) -> dict:
    response = client.get("/v1/me/overview", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def mcp_decisions(client, headers) -> dict:
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "decision_list", "arguments": {}}}
    response = client.post("/mcp", json=message, headers={**headers, **MCP_HEADERS, "X-Evo-Project": PROJECT})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result


def author_runs(db) -> int:
    r = tables.runs
    return sql(db, select(func.count()).select_from(r).where(r.c.kind == "author"))[0][0]


# Asking


def test_an_author_run_asks_a_decision_with_no_plan_before_its_first_put_and_with_its_plan_after(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)
    found = asks(client, worker, run_id)
    assert {key: found[key] for key in ("project", "run_id", "run_state", "plan_id", "step_key", "category")} == {
        "project": PROJECT,
        "run_id": run_id,
        "run_state": "running",
        "plan_id": None,
        "step_key": None,
        "category": "architecture",
    }
    assert (found["state"], found["owner"], found["recommended"]) == ("open", OWNER, "postgres")
    # no plan, so no step to name
    refused = ask(client, worker, run_id, step_key="2")
    assert refused.status_code == 422
    assert refused.json()["message"] == (
        f"author run {run_id} has put no plan on the hub yet, so it has no step '2': leave out step_key, "
        "or put the plan first"
    )
    # the bounds of a plan run's decisions
    assert ask(client, worker, run_id, category="refactor").status_code == 422
    assert ask(client, worker, run_id, options=[{"key": "one", "label": "One"}]).status_code == 422

    # once it put a plan, its decisions are of that plan, and a step of it is named
    assert put(client, worker, run_id, new_plan()).status_code == 200
    of_plan = asks(client, worker, run_id, step_key="2")
    assert (of_plan["plan_id"], of_plan["step_key"]) == ("wait-helper", "2")
    assert ask(client, worker, run_id, step_key="9").status_code == 422
    assert decision(client, hub["owner"], found["id"])["plan_id"] is None, "the first keeps the plan it had: none"

    # at most twenty open at once
    for _ in range(18):
        asks(client, worker, run_id)
    over = ask(client, worker, run_id)
    assert over.status_code == 409 and "20 decisions open" in over.json()["message"]


def test_only_the_worker_holding_an_author_run_asks_a_decision_and_a_step_run_gets_404(client, hub, hub_db, tmp_path):
    worker, run_id = running(client, hub, tmp_path)
    elsewhere = add_worker(client, hub["other"], "their-box", checkouts=CHECKOUTS)
    assert ask(client, elsewhere, run_id).status_code == 404

    (step,) = dispatched(client, hub["owner"], [2])
    steps_worker = add_worker(client, hub["owner"], "linux-box", checkouts=CHECKOUTS)
    assert claim(client, steps_worker)["id"] == step["id"]
    refused = ask(client, steps_worker, step["id"])
    assert refused.status_code == 404 and "is a run of one step" in refused.json()["message"]
    assert sql(hub_db, select(func.count()).select_from(tables.decisions)) == [(0,)]


# Reading


def test_whoever_reads_an_author_run_reads_its_decisions_on_the_list_the_page_home_and_mcp(
    client, hub, hub_db, tmp_path, github
):
    public_reader = bearer(live.sign_in(client, github, "public-reader", 650)["token"])
    grant = {"role": "reader", "max_level": "public"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/public-reader", json=grant, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    worker, run_id = running(client, hub, tmp_path)
    found = asks(client, worker, run_id)
    path = f"/v1/projects/{PROJECT}/decisions/{found['id']}"

    for name in ("owner", "other", "reader"):
        assert run_view(client, hub[name], run_id)["id"] == run_id
        assert decision(client, hub[name], found["id"]) == found, name
        assert listed(client, hub[name]) == ([found["id"]], 1), name
        assert listed(client, hub[name], run_id=run_id, state="open") == ([found["id"]], 1), name
        (shown,) = overview(client, hub[name])["open_decisions"]
        assert (shown["id"], shown["run_id"], shown["plan_id"], shown["yours"]) == (
            found["id"],
            run_id,
            None,
            name == "owner",
        )
        result = mcp_decisions(client, hub[name])
        text = result["content"][0]["text"]
        assert f"#{found['id']} architecture (open, author run #{run_id}, no plan): {QUESTION}" in text, name
        assert "None" not in text and "null" not in text
    # a member who does not read the run reads none of its decisions; nor does someone without a grant
    assert client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=public_reader).status_code == 404
    assert client.get(path, headers=public_reader).status_code == 404
    assert listed(client, public_reader) == ([], 0)
    assert overview(client, public_reader)["open_decisions"] == []
    assert mcp_decisions(client, public_reader)["structuredContent"]["total"] == 0
    assert client.get(path, headers=hub["stranger"]).status_code == 404

    # once the run put a plan, its decision without one is read as the run is: through the run's plan
    assert put(client, worker, run_id, new_plan()).status_code == 200
    assert decision(client, hub["reader"], found["id"])["plan_id"] is None
    assert listed(client, hub["reader"], plan_id="wait-helper") == ([], 0), "the filter is on the decision's plan"


# Answering


def test_only_the_owner_answers_an_author_decision_and_the_answer_goes_to_its_inbox_not_its_chat(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)
    found = asks(client, worker, run_id)
    asked(client, worker, run_id)
    assert answer(client, hub["other"], found["id"], option="postgres").status_code == 403
    assert answer(client, hub["reader"], found["id"], option="postgres").status_code == 403
    done = answered(client, hub["owner"], found["id"], option="postgres", text="Keep the file a week.")
    assert (done["state"], done["answer_option"], done["answer_run_id"]) == ("answered", "postgres", run_id)
    assert answer(client, hub["owner"], found["id"], option="sqlite").status_code == 409
    (message,) = inbox(client, worker, run_id)
    assert message["decision_id"] == found["id"] and "Keep the file a week." in message["text"]
    assert lines(chat(client, hub["owner"], run_id)) == [(run_id, "agent", None, SAID)]
    assert state_of(hub_db, run_id) == "waiting", "the worker sets it going again, as for a reply"


def test_an_author_run_that_ends_another_way_cancels_its_open_decisions(client, hub, hub_db, tmp_path):
    # the owner ends the chat, and the worker ends the run done
    worker, run_id = running(client, hub, tmp_path)
    ended_chat = asks(client, worker, run_id)
    asked(client, worker, run_id)
    assert control(client, hub["owner"], run_id, "finish").status_code == 200
    moved(client, worker, run_id, "running")
    moved(client, worker, run_id, "verifying", "done", summary="Wrote no plan.")
    assert decision(client, hub["owner"], ended_chat["id"])["state"] == "cancelled"
    refused = answer(client, hub["owner"], ended_chat["id"], option="postgres")
    assert refused.status_code == 409 and "cancelled, not open" in refused.json()["message"]

    # its agent fails
    failing = another(client, hub, worker)
    failed = asks(client, worker, failing)
    moved(client, worker, failing, "failed", error="the agent stopped")
    assert decision(client, hub["owner"], failed["id"])["state"] == "cancelled"

    # the owner cancels it, parked
    cancelled = another(client, hub, worker)
    parked = asks(client, worker, cancelled)
    asked(client, worker, cancelled)
    park(hub_db, cancelled)
    assert recover(client) == {**NOTHING_RECOVERED, "parked": 1}
    assert control(client, hub["owner"], cancelled, "cancel").json()["state"] == "cancelled"
    assert decision(client, hub["owner"], parked["id"])["state"] == "cancelled"


# The notification, Telegram and Home


def test_an_author_decision_without_a_plan_notifies_its_owner_naming_the_run_and_its_title_on_web_and_telegram(
    client,
    hub,
    hub_db,
    tmp_path,
    bot,  # noqa: F811
):
    linked(client, hub["owner_web"])
    worker, run_id = running(client, hub, tmp_path)
    title = run_view(client, hub["owner"], run_id)["title"]
    assert title
    found = asks(client, worker, run_id, category="scope")
    mine = client.get("/v1/me/notifications", params={"kind": "decision"}, headers=hub["owner"]).json()
    (notification,) = mine["notifications"]
    assert (notification["decision_id"], notification["run_id"], notification["link"]) == (
        found["id"],
        run_id,
        f"/inbox?decision={found['id']}",
    )
    assert notification["title"] == f"Author run #{run_id} asks: {QUESTION}"
    assert notification["body"] == (
        f"{QUESTION}\n\nscope decision for author run #{run_id}, {title} in project {PROJECT}."
    )
    assert client.get("/v1/me/notifications", headers=hub["other"]).json()["notifications"] == []

    assert deliver(client)["failed"] == 0
    message = bot.fake.sent()[-1]
    assert message["text"].startswith(f"<b>Author run #{run_id} of {PROJECT} asks a decision</b> (scope, {title})")
    assert QUESTION in message["text"] and "None" not in message["text"]
    assert message["reply_markup"]["inline_keyboard"] == [
        [{"text": "Keep SQLite", "callback_data": f"d:{found['id']}:sqlite"}],
        [{"text": "Move to Postgres (recommended)", "callback_data": f"d:{found['id']}:postgres"}],
        [{"text": "Open on the hub", "url": f"https://hub.test/inbox?decision={found['id']}"}],
    ]

    # once it put a plan, a decision names the plan and the step, under the author run
    assert put(client, worker, run_id, new_plan()).status_code == 200
    asks(client, worker, run_id, step_key="2")
    assert deliver(client)["failed"] == 0
    assert bot.fake.sent()[-1]["text"].startswith(
        f"<b>Author run #{run_id} of {PROJECT} asks a decision</b> (architecture, plan wait-helper, step 2)"
    )


def test_an_author_run_waiting_with_a_decision_open_sends_no_notice_and_home_shows_it_once_through_the_decision(
    client, hub, hub_db, tmp_path
):
    worker, run_id = running(client, hub, tmp_path)
    found = asks(client, worker, run_id)
    asked(client, worker, run_id)
    assert notices(client, hub["owner"]) == [], "the decision's notification says it all"
    home = overview(client, hub["owner"])
    assert [item["id"] for item in home["open_decisions"]] == [found["id"]]
    assert home["author_waiting"] == [], "it stands under Needs you through its decision"
    assert home["counts"]["waiting_on_you"] == 1

    # answered, it goes on; waiting with no decision open, it notifies and stands on Home as before
    answered(client, hub["owner"], found["id"], option="postgres")
    moved(client, worker, run_id, "running")
    asked(client, worker, run_id, REPLY)
    (notice,) = notices(client, hub["owner"])
    assert (notice["title"], notice["body"]) == (f"Author run #{run_id} waits for your reply", REPLY)
    home = overview(client, hub["owner"])
    assert home["open_decisions"] == [] and [item["id"] for item in home["author_waiting"]] == [run_id]


# Parked author runs


def test_a_parked_author_run_resumes_on_the_last_open_decision_answered_with_every_answer_in_its_inbox(
    client, hub, hub_db, tmp_path
):
    worker, parked_id = running(client, hub, tmp_path)
    first = asks(client, worker, parked_id)
    second = asks(client, worker, parked_id, category="scope")
    asked(client, worker, parked_id)
    park(hub_db, parked_id)
    assert recover(client) == {**NOTHING_RECOVERED, "parked": 1}
    before = author_runs(hub_db)

    # one answer alone wakes nobody: it waits in the parked run's inbox
    one = answered(client, hub["owner"], first["id"], option="sqlite")
    assert one["answer_run_id"] == parked_id
    assert state_of(hub_db, parked_id) == "parked" and author_runs(hub_db) == before
    assert decision(client, hub["owner"], second["id"])["run_id"] == parked_id

    # the last one resumes it on the same worker, and the new run's inbox holds both
    last = answered(client, hub["owner"], second["id"], option="postgres")
    new_id = last["answer_run_id"]
    assert new_id > parked_id and author_runs(hub_db) == before + 1
    assert state_of(hub_db, parked_id) == "done"
    new = run_view(client, hub["owner"], new_id)
    assert (new["kind"], new["state"], new["resume_of_run_id"], new["pinned_worker_id"]) == (
        "author",
        "queued",
        parked_id,
        worker["id"],
    )
    assert claim(client, worker)["id"] == new_id
    moved(client, worker, new_id, "running")
    assert [message["decision_id"] for message in inbox(client, worker, new_id)] == [first["id"], second["id"]]


def test_a_reply_in_the_chat_resumes_a_parked_author_run_with_a_decision_open_and_the_answers_given(
    client, hub, hub_db, tmp_path
):
    worker, parked_id = running(client, hub, tmp_path)
    first = asks(client, worker, parked_id)
    second = asks(client, worker, parked_id)
    asked(client, worker, parked_id)
    park(hub_db, parked_id)
    recover(client)
    answered(client, hub["owner"], first["id"], option="postgres")
    assert state_of(hub_db, parked_id) == "parked"

    new_id = sent(client, hub["owner"], parked_id)["run_id"]
    assert new_id > parked_id and state_of(hub_db, parked_id) == "done"
    assert decision(client, hub["owner"], second["id"])["run_id"] == new_id, "the open one goes to the new run"
    assert claim(client, worker)["id"] == new_id
    moved(client, worker, new_id, "running")
    messages = inbox(client, worker, new_id)
    assert [(item["decision_id"], item["text"] == REPLY) for item in messages] == [(first["id"], False), (None, True)]

    # the open decision, answered now, goes to the running run's inbox and resumes nothing
    later = answered(client, hub["owner"], second["id"], option="sqlite")
    assert later["answer_run_id"] == new_id and author_runs(hub_db) == 2

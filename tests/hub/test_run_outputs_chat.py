"""The outputs a chat message carries (plan run-outputs, step 1, the hub's part of a5): the agent of a run attaches
outputs of its own run to its message, never an image taken out of a tool's result; a chat run's files that it sent go
under its next message, and once it ended under its last one, which the chat's streams hear of as message_changed;
the members of the chat who read the message read their bytes, nobody else. The people, projects and files are
fictional, as in ``tests.hub.test_chat_runs``, whose world this reuses.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.hub import runs, tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.test_chat_runs import OTHER, chat_worker, created, ended, hub, in_chats, reply, started, thread
from tests.hub.test_run_outputs import PNG, REPORT, VIDEO, item, sent

app_key = credentials_api.app_key  # a fixture
__all__ = ["hub"]  # a fixture of test_chat_runs, on this module's client


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3):  # noqa: F811
    config = replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def chat_events(client, headers, chat_id: int, after: int = 0) -> list[dict]:
    response = client.get(f"/v1/chats/{chat_id}/events", params={"after": after}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["events"]


def test_a_chat_message_carries_the_outputs_its_agent_attached_and_chat_members_read_their_bytes(client, hub):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    run_id, prefix = spec["id"], f"/v1/worker/runs/{spec['id']}"
    shot, tool = sent(
        client,
        prefix,
        worker["headers"],
        [(PNG, item(PNG, "fixed.png", "image/png")), (VIDEO, item(VIDEO, "browser.png", "image/png", "tool"))],
    )["outputs"]
    refused = in_chats(client, worker, run_id, "chat/messages", {"body": "Look.", "output_ids": [tool["id"]]})
    assert refused.status_code == 422 and "none goes to a chat" in refused.json()["message"]
    posted = in_chats(
        client, worker, run_id, "chat/messages", {"body": "Fixed, see the shot.", "output_ids": [shot["id"]]}
    )
    assert posted.status_code == 201, posted.text
    (carried,) = posted.json()["outputs"]
    url = f"/v1/chats/{chat['id']}/outputs/{shot['id']}/content"
    assert (carried["id"], carried["name"], carried["url"]) == (shot["id"], "fixed.png", url)
    shown = {message["id"]: message for message in thread(client, hub["owner"], chat["id"], mention["id"])}
    assert [o["id"] for o in shown[posted.json()["id"]]["outputs"]] == [shot["id"]]
    assert shown[mention["id"]]["outputs"] == []
    for who in ("owner", "other"):
        answer = client.get(url, headers=hub[who])
        assert (answer.status_code, answer.content) == (200, PNG), who
        assert answer.headers["x-content-type-options"] == "nosniff"
    assert client.get(url, headers=hub["stranger"]).status_code == 404
    not_carried = f"/v1/chats/{chat['id']}/outputs/{tool['id']}/content"
    assert client.get(not_carried, headers=hub["owner"]).status_code == 404, "the tool image goes to no chat"


def test_a_chat_runs_files_go_to_its_thread_and_send_no_notice_of_files(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    _, spec = started(client, hub, worker, chat)
    sent(client, f"/v1/worker/runs/{spec['id']}", worker["headers"], [(PNG, item(PNG, "fixed.png", "image/png"))])
    n = tables.notifications
    noticed = select(func.count()).select_from(n).where(n.c.notice_kind == runs.OUTPUT_NOTICE)
    assert live.sql(hub_db, noticed) == [(0,)]


def test_a_chat_runs_files_go_under_its_next_message_and_once_it_ended_under_its_last_one(client, hub):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    run_id, prefix = spec["id"], f"/v1/worker/runs/{spec['id']}"
    (report,) = sent(client, prefix, worker["headers"], [(REPORT, item(REPORT, "report.md", "text/markdown"))])[
        "outputs"
    ]
    reply(client, worker, run_id, "Here is the report.")
    messages = thread(client, hub["owner"], chat["id"], mention["id"])
    answer = next(m for m in messages if m["body"] == "Here is the report.")
    assert [o["id"] for o in answer["outputs"]] == [report["id"]]
    ended(client, worker, run_id)
    seen = chat_events(client, hub["owner"], chat["id"])[-1]["seq"]
    # a file of its outputs directory, sent once the run ended, goes under its last message
    (late,) = sent(client, prefix, worker["headers"], [(PNG, item(PNG, "after.png", "image/png", "outputs"))])[
        "outputs"
    ]
    (changed,) = [e for e in chat_events(client, hub["owner"], chat["id"], seen) if e["kind"] == "message_changed"]
    assert changed["message"]["id"] == answer["id"]
    assert [o["id"] for o in changed["message"]["outputs"]] == [report["id"], late["id"]]
    deleted = client.delete(
        f"/v1/projects/{credentials_api.PROJECT}/runs/{run_id}/outputs/{late['id']}", headers=hub["owner"]
    )
    assert deleted.status_code == 200, deleted.text
    after = [e for e in chat_events(client, hub["owner"], chat["id"], changed["seq"]) if e["kind"] == "message_changed"]
    assert [o["id"] for o in after[-1]["message"]["outputs"]] == [report["id"]]

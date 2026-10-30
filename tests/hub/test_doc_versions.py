"""The versions of a document in a chat and in a run (plan chat-doc-tracked-changes, step 1, criterion a6): the hub
lists every file of one name, by the last part of its name without case, that the messages of a chat carry (threads
included) or that a run took in its inbox and wrote, oldest first, each with its source, its sender and the URL of its
bytes; to a member of the chat who clears the label of the message, or a reader of the run; nobody else. The people,
projects and files are fictional, as in ``tests.hub.test_chat_runs``, whose world this reuses.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.doc_versions import doc_key
from tests.hub import test_credentials_api as credentials_api
from tests.hub.test_attachments import a_chat_run, run_path, to_chat, to_run, uploaded
from tests.hub.test_chat_runs import OTHER, OWNER, STRANGER, chat_worker, claim, created, hub, reply, thread
from tests.hub.test_run_outputs import item
from tests.hub.test_run_outputs import sent as outputs_sent
from tests.hub.test_runs import moved

app_key = credentials_api.app_key  # a fixture
__all__ = ["hub"]  # a fixture of test_chat_runs, on this module's client

FIRST = b"# Requirements\n\nThe app sends one mail a day.\n"
SECOND = b"# Requirements\n\nThe app sends one mail a week.\n"
THIRD = "# Requirements\n\nThe app sends one mail a week, in Vietnamese: Thư hàng tuần.\n".encode()
OTHER_DOC = b"# Something else\n"


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3):  # noqa: F811
    config = replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def chat_versions(client, headers, chat_id: int, name: str):
    return client.get(f"/v1/chats/{chat_id}/doc-versions", params={"name": name}, headers=headers)


def run_versions(client, headers, run_id: int, name: str):
    return client.get(f"{run_path(run_id)}/doc-versions", params={"name": name}, headers=headers)


def test_doc_version_key_is_the_last_part_of_the_name_without_case():
    assert doc_key("Requirements.MD") == "requirements.md"
    assert doc_key(" drafts/v2/Requirements.md ") == "requirements.md"
    assert doc_key("Yêu Cầu.md") == "yêu cầu.md"
    assert doc_key("dir/") == "dir/"


def test_doc_versions_of_a_chat_list_the_attached_file_and_the_agents_outputs_of_its_name_oldest_first(client, hub):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    target = f"/v1/chats/{chat['id']}"
    first = uploaded(client, hub["owner"], target, "Requirements.md", FIRST, media_type="text/markdown")
    other = uploaded(client, hub["owner"], target, "notes.md", OTHER_DOC, media_type="text/markdown")
    posted = to_chat(client, hub["owner"], chat["id"], "@reviewer please fix this", [first["id"], other["id"]])
    assert posted.status_code == 201, posted.text
    mention = posted.json()
    spec = started_from(client, hub, worker)
    run_id, prefix = spec["id"], f"/v1/worker/runs/{spec['id']}"
    second, third = outputs_sent(
        client,
        prefix,
        worker["headers"],
        [
            (SECOND, item(SECOND, "requirements.md", "text/markdown")),
            (THIRD, item(THIRD, "drafts/REQUIREMENTS.md", "text/markdown", "outputs")),
        ],
    )["outputs"]
    reply(client, worker, run_id, "Fixed: once a week now.")
    answer = next(m for m in thread(client, hub["owner"], chat["id"], mention["id"]) if m["body"].startswith("Fixed"))
    assert answer["thread_root_id"] == mention["id"], "the reply is in the thread of the mention"

    for who in ("owner", "other"):
        response = chat_versions(client, hub[who], chat["id"], "requirements.MD")
        assert response.status_code == 200, response.text
        listed = response.json()
        assert listed["key"] == "requirements.md"
        versions = listed["versions"]
        assert [(v["source"], v["id"]) for v in versions] == [
            ("attached", first["id"]),
            ("sent", second["id"]),
            ("outputs", third["id"]),
        ], who
        attached, fixed, kept = versions
        assert (attached["message_id"], attached["seq"], attached["thread_root_id"]) == (
            mention["id"],
            mention["seq"],
            None,
        )
        assert (attached["author"]["kind"], attached["author"]["name"]) == ("person", OWNER)
        assert (attached["run_id"], attached["caption"], attached["viewer"]) == (None, None, "text")
        assert attached["url"] == f"{target}/attachments/{first['id']}/content"
        assert (fixed["message_id"], fixed["thread_root_id"], fixed["run_id"]) == (answer["id"], mention["id"], run_id)
        assert fixed["author"]["kind"] == "agent"
        assert fixed["url"] == f"{target}/outputs/{second['id']}/content"
        assert kept["name"] == "drafts/REQUIREMENTS.md"
        for version, data in ((attached, FIRST), (fixed, SECOND), (kept, THIRD)):
            assert (version["size"], version["media_type"]) == (len(data), "text/markdown")
            got = client.get(version["url"], headers=hub[who])
            assert (got.status_code, got.content) == (200, data), version["url"]

    nothing = chat_versions(client, hub["owner"], chat["id"], "missing.md")
    assert nothing.status_code == 200 and nothing.json()["versions"] == []
    for refused in (
        chat_versions(client, hub["stranger"], chat["id"], "requirements.md"),
        chat_versions(client, hub["owner"], chat["id"] + 1000, "requirements.md"),
    ):
        assert refused.status_code == 404, refused.text
    assert chat_versions(client, hub["owner"], chat["id"], "").status_code == 422


def started_from(client, hub, worker: dict) -> dict:
    """The run the owner's last mention of @reviewer made, claimed by ``worker`` and running."""
    spec = claim(client, worker)
    assert spec is not None and spec["kind"] == "chat", spec
    moved(client, worker, spec["id"], "running", session_id="sess-1")
    return spec


def test_doc_versions_of_a_chat_leave_out_the_messages_whose_label_the_reader_does_not_clear(client, hub):
    chat = created(client, hub["owner"])
    target = f"/v1/chats/{chat['id']}"
    inner = uploaded(client, hub["owner"], target, "plan.md", FIRST, media_type="text/markdown")
    before = to_chat(client, hub["owner"], chat["id"], "internal draft", [inner["id"]])
    assert before.status_code == 201 and before.json()["label"], before.text
    # with no agent the chat holds no project's work, and someone without a grant may join it
    assert client.delete(f"{target}/agents/reviewer", headers=hub["owner"]).status_code == 204
    assert client.put(f"{target}/people/{STRANGER}", headers=hub["owner"]).status_code == 200
    outer = uploaded(client, hub["owner"], target, "PLAN.md", SECOND, media_type="text/markdown")
    after = to_chat(client, hub["owner"], chat["id"], "public draft", [outer["id"]])
    assert after.status_code == 201 and after.json()["label"] == [], after.text

    owner = chat_versions(client, hub["owner"], chat["id"], "plan.md").json()["versions"]
    assert [v["id"] for v in owner] == [inner["id"], outer["id"]]
    stranger = chat_versions(client, hub["stranger"], chat["id"], "plan.md")
    assert stranger.status_code == 200, stranger.text
    assert [v["id"] for v in stranger.json()["versions"]] == [outer["id"]]


def test_doc_versions_of_a_chat_include_a_reply_in_a_thread(client, hub):
    _, chat, mention, _ = a_chat_run(client, hub)
    target = f"/v1/chats/{chat['id']}"
    first = uploaded(client, hub["owner"], target, "spec.md", FIRST, media_type="text/markdown")
    replied = to_chat(client, hub["owner"], chat["id"], "here it is", [first["id"]], reply_to=mention["id"])
    assert replied.status_code == 201, replied.text
    (version,) = chat_versions(client, hub["owner"], chat["id"], "Spec.md").json()["versions"]
    assert (version["id"], version["thread_root_id"]) == (first["id"], mention["id"])
    assert [m["id"] for m in thread(client, hub["owner"], chat["id"], mention["id"])][-1] == replied.json()["id"]


def test_doc_versions_of_a_run_list_its_messages_files_and_its_outputs_to_its_readers(client, hub):
    worker, _, _, spec = a_chat_run(client, hub)
    run_id, prefix = spec["id"], f"/v1/worker/runs/{spec['id']}"
    first = uploaded(client, hub["owner"], run_path(run_id), "config.yaml", FIRST, media_type="application/yaml")
    other = uploaded(client, hub["owner"], run_path(run_id), "other.yaml", OTHER_DOC, media_type="application/yaml")
    message = to_run(client, hub["owner"], run_id, "fix the schedule", [first["id"], other["id"]])
    assert message.status_code == 201, message.text
    (second,) = outputs_sent(
        client, prefix, worker["headers"], [(SECOND, item(SECOND, "Config.yaml", "application/yaml"))]
    )["outputs"]

    response = run_versions(client, hub["owner"], run_id, "config.yaml")
    assert response.status_code == 200, response.text
    attached, fixed = response.json()["versions"]
    assert (attached["source"], attached["id"], attached["author"]["name"]) == ("attached", first["id"], OWNER)
    assert (attached["message_id"], attached["seq"], attached["run_id"]) == (None, None, None)
    assert attached["url"] == f"{run_path(run_id)}/attachments/{first['id']}/content"
    assert (fixed["source"], fixed["id"], fixed["run_id"], fixed["author"]) == ("sent", second["id"], run_id, None)
    assert fixed["url"] == f"{run_path(run_id)}/outputs/{second['id']}/content"
    assert fixed["created_at"] >= attached["created_at"]
    for version, data in ((attached, FIRST), (fixed, SECOND)):
        got = client.get(version["url"], headers=hub["owner"])
        assert (got.status_code, got.content) == (200, data)

    refused = run_versions(client, hub["stranger"], run_id, "config.yaml")
    assert refused.status_code in (403, 404), refused.text
    assert run_versions(client, hub["owner"], run_id + 1000, "config.yaml").status_code == 404


def test_doc_versions_of_a_chat_run_leave_the_files_of_its_opening_message_to_the_chats_list(client, hub):
    """The run's bytes route serves the files of its inbox alone, so the run's list names those; the files of the
    chat message that opened a chat run are versions of the chat's list, served by the chat's route."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    first = uploaded(client, hub["owner"], f"/v1/chats/{chat['id']}", "brief.md", FIRST, media_type="text/markdown")
    posted = to_chat(client, hub["owner"], chat["id"], "@reviewer read the brief", [first["id"]])
    assert posted.status_code == 201, posted.text
    spec = started_from(client, hub, worker)
    assert run_versions(client, hub["owner"], spec["id"], "brief.md").json()["versions"] == []
    (version,) = chat_versions(client, hub["owner"], chat["id"], "brief.md").json()["versions"]
    assert (version["id"], version["message_id"]) == (first["id"], posted.json()["id"])

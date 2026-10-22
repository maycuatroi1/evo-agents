"""The files people attach to chat messages (``evo_agents.hub.server.chat_files``): a member uploads a file, the
message that names it carries it, only the chat's members who read that message read its bytes, a file waits for its
uploader's message alone, a message may be files without text, and the prompt of the agent's run names each file with
the command that saves it. Migration 0101 adds chat_files."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import chat_runs, chats, tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_chats as group_chats
from tests.hub.live import sql
from tests.hub.test_runs import OTHER, audit_rows

hub = group_chats.hub  # project evo-agents, owner its admin, someone-else a writer, the agent @reviewer
created = group_chats.created

PDF = b"%PDF-1.7\n" + secrets.token_bytes(256)
TEXT = b"name,score\nada,3\n"


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upload(client, headers, chat_id: int, name: str, data: bytes, media_type: str | None = "application/pdf"):
    extra = {"Content-Type": media_type} if media_type else {}
    return client.post(f"/v1/chats/{chat_id}/files", params={"name": name}, content=data, headers={**headers, **extra})


def uploaded(client, headers, chat_id: int, name: str, data: bytes, media_type: str = "application/pdf") -> dict:
    response = upload(client, headers, chat_id, name, data, media_type)
    assert response.status_code == 201, response.text
    return response.json()


def send(client, headers, chat_id: int, body: str, file_ids: list[int]):
    return client.post(f"/v1/chats/{chat_id}/messages", json={"body": body, "file_ids": file_ids}, headers=headers)


def test_a_member_uploads_a_file_and_the_message_that_names_it_carries_it(client, hub, hub_db, s3):
    chat = created(client, hub["owner"], people=[OTHER])
    found = uploaded(client, hub["owner"], chat["id"], "reports/Q3 plan.pdf", PDF)
    assert found["name"] == "Q3 plan.pdf" and found["media_type"] == "application/pdf" and found["viewer"] == "pdf"
    assert found["size"] == len(PDF) and found["sha256"] == sha(PDF) and found["uploaded_by"] == "owner"
    assert found["url"] == f"/v1/chats/{chat['id']}/files/{found['id']}/content"
    assert s3.get(f"blobs/sha256/{sha(PDF)}") == PDF
    mine = client.get(found["url"], headers=hub["owner"])  # its uploader reads it before it is sent
    assert mine.status_code == 200 and mine.content == PDF
    assert client.get(found["url"], headers=hub["other"]).status_code == 404  # nobody else, yet
    message = send(client, hub["owner"], chat["id"], "the plan", [found["id"]])
    assert message.status_code == 201, message.text
    assert [item["id"] for item in message.json()["files"]] == [found["id"]]
    listed = client.get(f"/v1/chats/{chat['id']}/messages", headers=hub["other"]).json()["messages"]
    assert [[item["name"] for item in m["files"]] for m in listed] == [["Q3 plan.pdf"]]
    served = client.get(found["url"], headers=hub["other"])
    assert served.status_code == 200 and served.content == PDF
    assert served.headers["etag"] == f'"{sha(PDF)}"' and served.headers["x-content-type-options"] == "nosniff"
    saved = client.get(found["url"], params={"download": "true"}, headers=hub["other"])
    assert saved.headers["content-disposition"].startswith("attachment")
    for who in ("reader", "stranger", "admin"):  # not in the chat: the chat does not exist
        assert client.get(found["url"], headers=hub[who]).status_code == 404
        assert upload(client, hub[who], chat["id"], "x.pdf", PDF).status_code == 404
    audited = [row for row in audit_rows(hub_db, "chat") if row[0] == "chat.file"]
    assert audited == [("chat.file", f"chat:{chat['id']} file:{found['id']} sha256:{sha(PDF)[:16]}", "owner", None)]


def test_a_message_may_be_files_alone_but_not_nothing(client, hub):
    chat = created(client, hub["owner"])
    first = uploaded(client, hub["owner"], chat["id"], "scores.csv", TEXT, "text/csv; charset=utf-8")
    second = uploaded(client, hub["owner"], chat["id"], "plan.pdf", PDF)
    assert first["media_type"] == "text/csv" and first["viewer"] == "text"
    assert send(client, hub["owner"], chat["id"], "  ", []).status_code == 422
    message = send(client, hub["owner"], chat["id"], "", [second["id"], first["id"]])
    assert message.status_code == 201, message.text
    assert message.json()["body"] == "" and [f["id"] for f in message.json()["files"]] == [first["id"], second["id"]]
    again = send(client, hub["owner"], chat["id"], "again", [first["id"]])  # sent already
    assert again.status_code == 422 and "upload it again" in again.json()["message"]


def test_a_file_waits_for_its_uploaders_message_in_its_own_chat(client, hub, hub_db):
    chat = created(client, hub["owner"], people=[OTHER])
    other_chat = created(client, hub["owner"], title="Elsewhere")
    found = uploaded(client, hub["owner"], chat["id"], "plan.pdf", PDF)
    assert send(client, hub["other"], chat["id"], "mine now", [found["id"]]).status_code == 422
    assert send(client, hub["owner"], other_chat["id"], "here", [found["id"]]).status_code == 422
    too_many = send(client, hub["owner"], chat["id"], "many", list(range(1, chats.MAX_MESSAGE_FILES + 2)))
    assert too_many.status_code == 422
    f = tables.chat_files
    assert sql(hub_db, select(f.c.message_id).where(f.c.id == found["id"])) == [(None,)]
    assert sql(hub_db, select(func.count()).select_from(tables.chat_messages)) == [(0,)]  # nothing half-sent


def test_files_over_the_limit_empty_or_badly_named_are_refused(client, hub, hub_db, s3, monkeypatch):
    chat = created(client, hub["owner"])
    monkeypatch.setattr(chats, "MAX_FILE_BYTES", 64)
    big = upload(client, hub["owner"], chat["id"], "big.bin", bytes(65))
    assert big.status_code == 413 and "over the 64" in big.json()["message"], big.text
    assert upload(client, hub["owner"], chat["id"], "empty.txt", b"", "text/plain").status_code == 422
    assert upload(client, hub["owner"], chat["id"], "..", b"x", "text/plain").status_code == 422
    assert upload(client, hub["owner"], chat["id"], "x.bin", b"x", "not a type").status_code == 422
    plain = uploaded(client, hub["owner"], chat["id"], "x.bin", b"x", "")
    assert plain["media_type"] == "application/octet-stream" and plain["viewer"] == "none"
    assert s3.keys("blobs/") == [f"blobs/sha256/{sha(b'x')}"]


def test_a_file_no_message_carried_for_a_day_goes_with_its_blob(client, hub, hub_db):
    chat = created(client, hub["owner"])
    stale = uploaded(client, hub["owner"], chat["id"], "old.pdf", PDF)
    f = tables.chat_files
    sql(hub_db, update(f).values(created_at=func.now() - timedelta(days=2)).where(f.c.id == stale["id"]))
    fresh = uploaded(client, hub["owner"], chat["id"], "new.csv", TEXT, "text/csv")
    assert [row[0] for row in sql(hub_db, select(f.c.id).order_by(f.c.id))] == [fresh["id"]]
    d = tables.blob_deletions
    assert sql(hub_db, select(d.c.sha256, d.c.kind)) == [(sha(PDF), chats.FILE_KIND)]


def test_the_prompt_names_each_file_with_the_command_that_saves_it():
    message = {"id": 7, "author": "owner", "at": None, "body": "see the plan"}
    message["files"] = [chat_runs.file_fact(3, "Q3 plan.pdf", "application/pdf", 2048)]
    text = chat_runs.render_message(message)
    assert text.splitlines()[1:] == [
        "see the plan",
        "[attached file 3: 'Q3 plan.pdf' (application/pdf, 2.0 KiB); `evo-agents worker chat file 3` saves it]",
    ]
    alone = chat_runs.render_message({**message, "body": ""})
    assert alone.splitlines()[1].startswith("[attached file 3:")
    assert chat_runs.render_message({"id": 8, "author": "owner", "body": "hi"}).endswith("\nhi")

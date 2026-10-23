"""Files people attach to messages (plan chat-attachments, step 1): the owner of a run uploads files for it while it
takes messages and a member of a chat for it, each streamed into the blob store with its SHA-256, at most 50 MiB; a
message names at most ten of the sender's own unsent uploads for that very target, and the hub ties them to it in the
same transaction. The run's inbox, its user_message event, the chat's messages, the claim of a chat run and the chat
read of its agent name the files; their bytes go to the run's readers, the chat's members who read the message and
the agent of a run that took or may read it, nobody else. An upload no message took goes after a day, the files of a
run's messages with the run's events, and those of chat messages stay. The people, projects and files are fictional,
as in ``tests.hub.test_chat_runs``, whose world this reuses.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.hub import attachment_prune, attachments, blob_gc, blobs, cli_chats, run_cli_send, tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.test_chat_runs import OTHER, PROJECT, beat, chat_worker, claim, created, ended, hub, sent, started
from tests.hub.test_run_cli import cli, ok, write_credentials

app_key = credentials_api.app_key  # a fixture
__all__ = ["hub"]  # a fixture of test_chat_runs, on this module's client

NOTES = b"retry the wait three times\n"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3):  # noqa: F811
    config = replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_path(run_id: int) -> str:
    return f"/v1/projects/{PROJECT}/runs/{run_id}"


def upload(client, headers, target: str, name: str, data: bytes, media_type: str = "text/plain"):
    return client.post(
        f"{target}/attachments", params={"name": name}, content=data, headers={**headers, "Content-Type": media_type}
    )


def uploaded(client, headers, target: str, name: str = "notes.txt", data: bytes = NOTES, **kwargs) -> dict:
    response = upload(client, headers, target, name, data, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def to_run(client, headers, run_id: int, text: str, ids: list[int]):
    body = {"text": text, "attachment_ids": ids}
    return client.post(f"{run_path(run_id)}/messages", json=body, headers=headers)


def to_chat(client, headers, chat_id: int, text: str, ids: list[int], reply_to: int | None = None):
    body = {"body": text, "attachment_ids": ids} | ({"reply_to": reply_to} if reply_to else {})
    return client.post(f"/v1/chats/{chat_id}/messages", json=body, headers=headers)


def inbox(client, worker: dict, run_id: int) -> list[dict]:
    response = client.post(f"/v1/worker/runs/{run_id}/inbox", json={}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()["messages"]


def worker_reads(client, worker: dict, run_id: int, attachment_id: int):
    return client.get(f"/v1/worker/runs/{run_id}/attachments/{attachment_id}/content", headers=worker["headers"])


def events(client, headers, run_id: int, kind: str) -> list[dict]:
    response = client.get(f"{run_path(run_id)}/events", params={"after": 0}, headers=headers)
    assert response.status_code == 200, response.text
    return [e["body"] for e in response.json()["events"] if e["kind"] == kind]


def a_chat_run(client, hub) -> tuple[dict, dict, dict, dict]:
    """A chat of owner with OTHER, and the run of its mention of @reviewer, running: (worker, chat, mention, spec)."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec = started(client, hub, worker, chat)
    return worker, chat, mention, spec


# A run's messages


def test_an_attachment_goes_with_a_message_of_no_text_to_a_run_its_inbox_and_its_worker(client, hub):
    worker, _, _, spec = a_chat_run(client, hub)
    run_id = spec["id"]
    shown = uploaded(client, hub["owner"], run_path(run_id))
    assert (shown["name"], shown["media_type"], shown["size"], shown["sha256"]) == (
        "notes.txt",
        "text/plain",
        len(NOTES),
        sha(NOTES),
    )
    message = to_run(client, hub["owner"], run_id, "", [shown["id"]])
    assert message.status_code == 201, message.text
    assert [f["id"] for f in message.json()["attachments"]] == [shown["id"]]
    (said,) = events(client, hub["owner"], run_id, "user_message")[-1:]
    assert said["attachments"] == [{"id": shown["id"], "name": "notes.txt", "media_type": "text/plain", "size": 27}]
    beat(client, worker, held=[run_id])
    (taken,) = [m for m in inbox(client, worker, run_id) if m["text"] == ""]
    assert [(f["id"], f["sha256"]) for f in taken["attachments"]] == [(shown["id"], sha(NOTES))]
    read = worker_reads(client, worker, run_id, shown["id"])
    assert (read.status_code, read.content) == (200, NOTES)
    assert "attachment" in read.headers["content-disposition"]
    own = client.get(f"{run_path(run_id)}/attachments/{shown['id']}/content", headers=hub["owner"])
    assert (own.status_code, own.content, own.headers["x-content-type-options"]) == (200, NOTES, "nosniff")
    ranged = client.get(
        f"{run_path(run_id)}/attachments/{shown['id']}/content", headers={**hub["owner"], "Range": "bytes=0-4"}
    )
    assert (ranged.status_code, ranged.content) == (206, NOTES[:5])
    stranger = client.get(f"{run_path(run_id)}/attachments/{shown['id']}/content", headers=hub["stranger"])
    assert stranger.status_code in (403, 404)


def test_an_attachment_is_sent_once_by_its_uploader_to_its_own_target(client, hub):
    worker, chat, _, spec = a_chat_run(client, hub)
    run_id = spec["id"]
    mine = uploaded(client, hub["owner"], run_path(run_id))["id"]
    for_chat = uploaded(client, hub["owner"], f"/v1/chats/{chat['id']}")["id"]
    others = uploaded(client, hub["other"], f"/v1/chats/{chat['id']}")["id"]
    wrong_target = to_run(client, hub["owner"], run_id, "see", [for_chat])
    assert wrong_target.status_code == 422 and f"chat {chat['id']}" in wrong_target.json()["message"]
    not_mine = to_chat(client, hub["owner"], chat["id"], "see", [others])
    assert not_mine.status_code == 422 and "not an upload of yours" in not_mine.json()["message"]
    twice = to_run(client, hub["owner"], run_id, "see", [mine, mine])
    assert twice.status_code == 422
    assert to_run(client, hub["owner"], run_id, "see", [mine]).status_code == 201
    again = to_run(client, hub["owner"], run_id, "again", [mine])
    assert again.status_code == 422 and "sent already" in again.json()["message"]
    assert to_run(client, hub["owner"], run_id, " ", []).status_code == 422, "no text and no file"
    # only the owner uploads for a run
    assert upload(client, hub["other"], run_path(run_id), "x.txt", b"x").status_code in (403, 404)
    assert upload(client, hub["stranger"], f"/v1/chats/{chat['id']}", "x.txt", b"x").status_code in (403, 404)
    ended(client, worker, run_id)
    late = upload(client, hub["owner"], run_path(run_id), "x.txt", b"x")
    assert late.status_code == 409, "a run that took its last message takes no file"


def test_attachment_limits_size_count_and_name(client, hub):
    _, chat, _, spec = a_chat_run(client, hub)
    target = f"/v1/chats/{chat['id']}"
    big = upload(client, hub["owner"], target, "big.bin", b"\0" * (attachments.MAX_FILE_BYTES + 1))
    assert big.status_code == 413, big.text
    for name in ("../x", "a/b", ".."):
        assert upload(client, hub["owner"], target, name, b"x").status_code == 422, name
    ids = [uploaded(client, hub["owner"], target, f"f{n}.txt", b"%d" % n)["id"] for n in range(11)]
    too_many = to_chat(client, hub["owner"], chat["id"], "files", ids)
    assert too_many.status_code == 422
    assert to_chat(client, hub["owner"], chat["id"], "", ids[:10]).status_code == 201


def test_an_attachments_uploader_deletes_it_until_it_is_sent(client, hub, s3):
    _, chat, _, _ = a_chat_run(client, hub)
    target = f"/v1/chats/{chat['id']}"
    gone = uploaded(client, hub["owner"], target, data=b"drop me")
    assert s3.get(blobs.blob_key(sha(b"drop me"))) == b"drop me"
    assert client.delete(f"{target}/attachments/{gone['id']}", headers=hub["other"]).status_code == 404
    assert client.delete(f"{target}/attachments/{gone['id']}", headers=hub["owner"]).status_code == 204
    assert client.get(f"{target}/attachments/{gone['id']}/content", headers=hub["owner"]).status_code == 404
    assert s3.get(blobs.blob_key(sha(b"drop me"))) is None
    kept = uploaded(client, hub["owner"], target)
    assert to_chat(client, hub["owner"], chat["id"], "kept", [kept["id"]]).status_code == 201
    assert client.delete(f"{target}/attachments/{kept['id']}", headers=hub["owner"]).status_code == 409


# Chat messages


def test_a_chat_messages_attachments_go_to_its_readers_its_chat_run_claim_and_its_agents_read(client, hub):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    target = f"/v1/chats/{chat['id']}"
    shot = uploaded(client, hub["owner"], target, "diagram.svg", SVG, media_type="image/svg+xml")
    posted = to_chat(client, hub["owner"], chat["id"], "@reviewer please read this", [shot["id"]])
    assert posted.status_code == 201, posted.text
    (carried,) = posted.json()["attachments"]
    url = f"{target}/attachments/{shot['id']}/content"
    assert (carried["id"], carried["name"], carried["url"]) == (shot["id"], "diagram.svg", url)
    for who in ("owner", "other"):
        answer = client.get(url, headers=hub[who])
        assert (answer.status_code, answer.content) == (200, SVG), who
        assert answer.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in answer.headers.get("content-security-policy", ""), "an svg runs no script"
    assert client.get(url, headers=hub["stranger"]).status_code in (403, 404)
    spec = claim(client, worker)
    assert spec["kind"] == "chat" and [f["id"] for f in spec["attachments"]] == [shot["id"]]
    message_id = posted.json()["id"]
    assert spec["chat_message_id"] == message_id, "the worker saves the files under .evo-run/inbox/chat-<it>/"
    assert (
        f"[files attached: diagram.svg (image/svg+xml, {len(SVG)} bytes). The worker saves them under "
        in (spec["prompt"])
    )
    assert f".evo-run/inbox/chat-{message_id}/" in spec["prompt"]
    read = client.post(
        f"/v1/worker/runs/{spec['id']}/chat/read", json={"id": str(posted.json()["id"])}, headers=worker["headers"]
    )
    assert read.status_code == 200, read.text
    assert [f["id"] for f in read.json()["messages"][0]["attachments"]] == [shot["id"]]
    assert f"diagram.svg (image/svg+xml, {len(SVG)} bytes, attachment {shot['id']})" in read.json()["text"]
    answer = worker_reads(client, worker, spec["id"], shot["id"])
    assert (answer.status_code, answer.content) == (200, SVG)


def test_a_reply_with_attachments_reaches_the_inbox_of_the_running_chat_run(client, hub):
    worker, chat, mention, spec = a_chat_run(client, hub)
    notes = uploaded(client, hub["owner"], f"/v1/chats/{chat['id']}")
    reply = to_chat(client, hub["owner"], chat["id"], "@reviewer and these notes", [notes["id"]], mention["id"])
    assert reply.status_code == 201, reply.text
    beat(client, worker, held=[spec["id"]])
    (taken,) = inbox(client, worker, spec["id"])
    assert [f["id"] for f in taken["attachments"]] == [notes["id"]]
    assert worker_reads(client, worker, spec["id"], notes["id"]).content == NOTES
    said = events(client, hub["owner"], spec["id"], "user_message")[-1]
    assert [f["id"] for f in said["attachments"]] == [notes["id"]]
    elsewhere = chat_worker(client, hub["other"], "laptop")
    assert worker_reads(client, elsewhere, spec["id"], notes["id"]).status_code in (403, 404, 409)


def test_a_text_only_message_carries_no_attachment(client, hub):
    worker, chat, mention, spec = a_chat_run(client, hub)
    assert mention["attachments"] == []
    assert spec["attachments"] == [] and spec["chat_message_id"] == mention["id"]
    assert "[files attached:" not in spec["prompt"], "a text-only message's prompt is as before"
    message = to_run(client, hub["owner"], spec["id"], "go on", [])
    assert message.status_code == 201 and message.json()["attachments"] == []
    assert "attachments" not in events(client, hub["owner"], spec["id"], "user_message")[-1]
    assert sent(client, hub["owner"], chat["id"], "@reviewer thanks", mention["id"])["attachments"] == []
    beat(client, worker, held=[spec["id"]])
    assert all(m["attachments"] == [] for m in inbox(client, worker, spec["id"]))


# Cleaning up


def count(hub_db) -> int:
    return live.sql(hub_db, select(func.count()).select_from(tables.attachments))[0][0]


def test_an_attachment_no_message_took_goes_after_a_day_and_a_chats_stay(client, hub, hub_db, s3):
    _, chat, _, _ = a_chat_run(client, hub)
    target = f"/v1/chats/{chat['id']}"
    stale = uploaded(client, hub["owner"], target, data=b"never sent")
    kept = uploaded(client, hub["owner"], target, data=b"sent")
    assert to_chat(client, hub["owner"], chat["id"], "here", [kept["id"]]).status_code == 201
    engine, store = client.app.state.engine, client.app.state.blobs
    now = client.portal.call(attachment_prune.remove_stale, engine, store, datetime.now(UTC))
    assert now == {"attachments": 0}
    later = datetime.now(UTC) + attachments.STALE_AFTER + timedelta(hours=1)
    assert client.portal.call(attachment_prune.remove_stale, engine, store, later) == {"attachments": 1}
    assert s3.get(blobs.blob_key(sha(b"never sent"))) is None
    assert s3.get(blobs.blob_key(sha(b"sent"))) == b"sent"
    assert count(hub_db) == 1 and stale["id"] != kept["id"]


def test_a_runs_attachments_go_with_its_events_and_blob_gc_keeps_what_an_attachment_names(client, hub, hub_db, s3):
    worker, _, _, spec = a_chat_run(client, hub)
    run_id = spec["id"]
    sent_file = uploaded(client, hub["owner"], run_path(run_id), data=b"for the run")
    assert to_run(client, hub["owner"], run_id, "", [sent_file["id"]]).status_code == 201

    async def forgotten():
        async with client.app.state.engine.begin() as conn:
            await blob_gc.lock_exclusive(conn)
            return await blob_gc.forget(conn, {sha(b"for the run"): (11, attachments.KIND)})

    assert client.portal.call(forgotten) == [], "an attachment still names it"
    engine, store = client.app.state.engine, client.app.state.blobs
    assert client.portal.call(attachment_prune.prune_run_files, engine, store, 0) == {"attachments": 0}
    beat(client, worker, held=[run_id])
    (taken,) = [m for m in inbox(client, worker, run_id) if m["attachments"]]
    acked = client.post(f"/v1/worker/runs/{run_id}/inbox", json={"ack": taken["id"]}, headers=worker["headers"])
    assert acked.status_code == 200, acked.text
    ended(client, worker, run_id)  # the message was taken: its file stays with this run
    assert client.portal.call(attachment_prune.prune_run_files, engine, store, 0) == {"attachments": 1}
    assert count(hub_db) == 0 and s3.get(blobs.blob_key(sha(b"for the run"))) is None


# The command line


class FakeHub:
    """The hub as the CLI calls it: each upload answers the next id, each call the message it was sent."""

    def __init__(self, answer: dict):
        self.uploads: list[tuple[str, str, str]] = []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.answer = answer

    def upload(self, path: str, file: Path, media_type: str):
        self.uploads.append((path, file.name, media_type))
        return {"id": len(self.uploads)}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        files = [{"id": i, "name": f"f{i}", "size": 3} for i in (body or {}).get("attachment_ids", [])]
        return {**self.answer, "attachments": files}


def test_attachment_cli_run_send_and_chat_send_upload_each_file_first(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.test", "owner", "evh_test")
    (tmp_path / "notes.txt").write_text("abc")
    (tmp_path / "shot.png").write_bytes(b"png")
    message = {"id": 7, "run_id": 12, "seq": 3}
    hub = FakeHub(message)
    monkeypatch.setattr(run_cli_send, "signed_in", lambda: (hub, None))
    files = ("--attach", str(tmp_path / "notes.txt"), "--attach", str(tmp_path / "shot.png"))
    said = ok(cli(monkeypatch, capsys, home, "hub", "run", "send", "12", *files, "--project", "demo")).out
    assert hub.uploads == [
        ("/v1/projects/demo/runs/12/attachments?name=notes.txt", "notes.txt", "text/plain"),
        ("/v1/projects/demo/runs/12/attachments?name=shot.png", "shot.png", "image/png"),
    ]
    assert hub.calls == [("POST", "/v1/projects/demo/runs/12/messages", {"text": "", "attachment_ids": [1, 2]})]
    assert "with f1 (3 bytes); f2 (3 bytes)" in said

    chat_hub = FakeHub(
        {
            **message,
            "chat_id": 4,
            "thread_root_id": None,
            "reply_count": 0,
            "author": {"kind": "person", "login": "owner"},
        }
    )
    chat_hub.answer |= {"body": "see", "mentions": [], "reactions": [], "created_at": "2026-10-22T10:00:00+00:00"}
    monkeypatch.setattr(cli_chats, "signed_in", lambda: (chat_hub, None))
    shown = cli(monkeypatch, capsys, home, "hub", "chat", "send", "4", "see", "--attach", str(tmp_path / "notes.txt"))
    assert shown.code == 0, shown.err
    assert chat_hub.calls == [("POST", "/v1/chats/4/messages", {"body": "see", "attachment_ids": [1]})]
    assert "files: f1 (3 bytes)" in shown.out


def test_attachment_cli_refuses_a_missing_file_or_no_text_before_any_upload(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.test", "owner", "evh_test")
    hub = FakeHub({})
    monkeypatch.setattr(run_cli_send, "signed_in", lambda: (hub, None))
    monkeypatch.setattr(cli_chats, "signed_in", lambda: (hub, None))
    missing = cli(monkeypatch, capsys, home, "hub", "run", "send", "12", "--attach", "nope.txt", "--project", "demo")
    assert missing.code == 2 and "nope.txt: no such file" in missing.err
    bare = cli(monkeypatch, capsys, home, "hub", "run", "send", "12", "--project", "demo")
    assert bare.code == 2 and "--attach" in bare.err
    chat_bare = cli(monkeypatch, capsys, home, "hub", "chat", "send", "4")
    assert chat_bare.code == 2
    assert hub.uploads == [] and hub.calls == []


def test_attachment_names_show_in_hub_run_chat(monkeypatch, capsys, tmp_path):
    from evo_agents.hub import run_cli

    home = write_credentials(tmp_path / "home", "https://hub.test", "owner", "evh_test")
    said = {"author": "owner", "login": "owner", "at": "2026-10-22T10:00:00+00:00", "run_id": 12, "text": ""}
    said["attachments"] = [{"id": 1, "name": "notes.txt", "media_type": "text/plain", "size": 3}]
    chat = {"run_id": 12, "state": "running", "status": "working", "plan_id": None, "plan_revision": None}
    chat |= {"messages": [said], "more": False, "owner": "owner"}
    monkeypatch.setattr(run_cli, "_signed_in", lambda: (SimpleNamespace(call=lambda *a, **k: chat), None))
    shown = ok(cli(monkeypatch, capsys, home, "hub", "run", "chat", "12", "--project", "demo")).out
    assert "(files: notes.txt (3 bytes))" in shown

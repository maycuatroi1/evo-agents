"""The image of a group chat (``evo_agents.hub.server.chat_images``), as criterion a3 of the plan agent-direct-chats
names it: any member of a group chat sets its image or takes it off, a PNG, JPEG or WebP image of at most 512 KiB
checked by its first bytes; only members read it (404 for anyone else, as for the chat), with an ETag and a URL that
changes with it in Chat and ChatSummary (``image_url``, null without one); a direct chat takes none; the chat's row
holds the blob, so the collection of unreferenced blobs leaves it, and the old image is released when it is replaced or
taken off; each change is audited and is an event of the chat. Migration 0051 adds the columns and the event kind."""

from __future__ import annotations

import base64
import hashlib
import secrets
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import insert, select

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_chats as group_chats
from tests.hub.live import sql
from tests.hub.test_migrate import ALL, columns, move_to, one, query, seed
from tests.hub.test_runs import OTHER, OWNER, audit_rows

hub = group_chats.hub  # project evo-agents, owner its admin, someone-else a writer, the agent @reviewer
created = group_chats.created

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + secrets.token_bytes(64)
JPEG = b"\xff\xd8\xff\xe0" + secrets.token_bytes(64)
WEBP = b"RIFF" + (76).to_bytes(4, "little") + b"WEBPVP8 " + secrets.token_bytes(64)
MAX_BYTES = 512 * 1024


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put_image(client, headers, chat_id: int, data: bytes):
    return client.put(f"/v1/chats/{chat_id}/image", json={"data": base64.b64encode(data).decode()}, headers=headers)


def image_set(client, headers, chat_id: int, data: bytes) -> dict:
    response = put_image(client, headers, chat_id, data)
    assert response.status_code == 200, response.text
    return response.json()


def summary(client, headers, chat_id: int) -> dict:
    response = client.get("/v1/chats", headers=headers)
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}[chat_id]


def image_events(client, headers, chat_id: int) -> list[dict]:
    response = client.get(f"/v1/chats/{chat_id}/events", headers=headers)
    assert response.status_code == 200, response.text
    return [
        (event["image"]["image_url"], event["image"]["by"]) for event in response.json()["events"] if event["image"]
    ]


def pending(db) -> list[tuple]:
    d = tables.blob_deletions
    return sql(db, select(d.c.sha256, d.c.kind).where(d.c.deleted_at.is_(None)).order_by(d.c.sha256))


def collect(client):
    """Delete the objects of the blobs recorded for deletion, as the hourly prune does."""
    return client.portal.call(partial(blob_gc.delete_pending, client.app.state.engine, client.app.state.blobs))


def image_audit(db) -> list[tuple]:
    return [row for row in audit_rows(db, "chat") if row[0] == "chat.image"]


def chat_image(db, chat_id: int) -> tuple:
    c = tables.chats
    return sql(db, select(c.c.image_sha256, c.c.image_size).where(c.c.id == chat_id))[0]


# Setting and reading


def test_a_member_sets_the_chat_image_and_only_members_read_it(client, hub, hub_db, s3):
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])
    assert chat["image_url"] is None and summary(client, hub["owner"], chat["id"])["image_url"] is None
    view = image_set(client, hub["other"], chat["id"], PNG)  # a member who did not create it
    url = f"/v1/chats/{chat['id']}/image?v={sha(PNG)[:16]}"
    assert view["image_url"] == url and view["kind"] == "group"
    assert summary(client, hub["owner"], chat["id"])["image_url"] == url
    assert client.get(f"/v1/chats/{chat['id']}", headers=hub["owner"]).json()["image_url"] == url
    served = client.get(url, headers=hub["owner"])
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["etag"] == f'"{sha(PNG)}"'
    assert served.headers["cache-control"] == "private, max-age=86400"
    assert served.headers["x-content-type-options"] == "nosniff"
    kept = client.get(url, headers={**hub["owner"], "If-None-Match": f'"{sha(PNG)}"'})
    assert kept.status_code == 304 and kept.content == b"" and kept.headers["etag"] == f'"{sha(PNG)}"'
    assert s3.get(f"blobs/sha256/{sha(PNG)}") == PNG
    assert chat_image(hub_db, chat["id"]) == (sha(PNG), len(PNG))
    assert (
        sql(hub_db, select(tables.blobs.c.sha256).where(tables.blobs.c.sha256 == sha(PNG))) == []
    )  # the chat holds it
    for who in ("reader", "stranger", "admin"):  # not in it: a chat that does not exist, its image too
        for answer in (
            client.get(url, headers=hub[who]),
            put_image(client, hub[who], chat["id"], JPEG),
            client.delete(f"/v1/chats/{chat['id']}/image", headers=hub[who]),
        ):
            assert answer.status_code == 404 and "that you are a member of" in answer.json()["message"], answer.text
    assert chat_image(hub_db, chat["id"]) == (sha(PNG), len(PNG))
    assert image_events(client, hub["owner"], chat["id"]) == [(url, OTHER)]
    assert image_audit(hub_db) == [("chat.image", f"chat:{chat['id']} image:{sha(PNG)[:16]}", OTHER, None)]


def test_jpeg_and_webp_are_images_too_and_setting_the_same_one_again_changes_nothing(client, hub, hub_db):
    chat = created(client, hub["owner"], agents=["reviewer"])
    for data, kind in ((JPEG, "image/jpeg"), (WEBP, "image/webp")):
        url = image_set(client, hub["owner"], chat["id"], data)["image_url"]
        served = client.get(url, headers=hub["owner"])
        assert served.status_code == 200 and served.headers["content-type"] == kind and served.content == data
    again = image_set(client, hub["owner"], chat["id"], WEBP)
    assert again["image_url"].endswith(sha(WEBP)[:16])
    assert len(image_events(client, hub["owner"], chat["id"])) == 2
    assert [row[1] for row in image_audit(hub_db)] == [
        f"chat:{chat['id']} image:{sha(JPEG)[:16]}",
        f"chat:{chat['id']} image:{sha(WEBP)[:16]}",
    ]


def test_an_image_of_another_format_or_over_512_kib_is_refused_and_nothing_changes(client, hub, hub_db, s3):
    chat = created(client, hub["owner"], agents=["reviewer"])
    svg = put_image(client, hub["owner"], chat["id"], b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    assert svg.status_code == 422 and "PNG, JPEG, WebP" in svg.json()["message"], svg.text
    gif = put_image(client, hub["owner"], chat["id"], b"GIF89a" + secrets.token_bytes(32))
    assert gif.status_code == 422, gif.text
    big = b"\x89PNG\r\n\x1a\n" + bytes(MAX_BYTES + 1 - 8)
    too_big = put_image(client, hub["owner"], chat["id"], big)
    assert too_big.status_code == 413 and f"over the {MAX_BYTES}" in too_big.json()["message"], too_big.text
    not_base64 = client.put(f"/v1/chats/{chat['id']}/image", json={"data": "not base64!"}, headers=hub["owner"])
    assert not_base64.status_code == 422
    assert chat_image(hub_db, chat["id"]) == (None, None)
    assert s3.keys("blobs/") == []
    assert image_events(client, hub["owner"], chat["id"]) == []
    assert image_audit(hub_db) == []
    assert client.get(f"/v1/chats/{chat['id']}/image", headers=hub["owner"]).status_code == 404


def test_a_direct_chat_takes_no_image(client, hub, hub_db, s3):
    direct = client.put("/v1/chats/direct/reviewer", headers=hub["owner"])
    assert direct.status_code == 201, direct.text
    chat_id = direct.json()["id"]
    assert direct.json()["image_url"] is None
    for answer in (
        put_image(client, hub["owner"], chat_id, PNG),
        client.delete(f"/v1/chats/{chat_id}/image", headers=hub["owner"]),
    ):
        assert answer.status_code == 409 and "its agent's avatar" in answer.json()["message"], answer.text
    assert client.get(f"/v1/chats/{chat_id}/image", headers=hub["owner"]).status_code == 404
    assert summary(client, hub["owner"], chat_id)["image_url"] is None
    assert chat_image(hub_db, chat_id) == (None, None) and s3.keys("blobs/") == []
    with pytest.raises(errors.CheckViolation):  # the database refuses one too
        c = tables.chats
        sql(hub_db, c.update().values(image_sha256=sha(PNG), image_size=len(PNG)).where(c.c.id == chat_id))


# The blob


def test_replacing_or_taking_off_the_image_releases_the_old_blob(client, hub, hub_db, s3):
    chat = created(client, hub["owner"], agents=["reviewer"])
    image_set(client, hub["owner"], chat["id"], PNG)
    replaced = image_set(client, hub["owner"], chat["id"], JPEG)
    assert replaced["image_url"].endswith(sha(JPEG)[:16])
    assert pending(hub_db) == [(sha(PNG), "chat-image")]
    assert collect(client).objects == 1
    assert s3.get(f"blobs/sha256/{sha(PNG)}") is None and s3.get(f"blobs/sha256/{sha(JPEG)}") == JPEG
    dropped = client.delete(f"/v1/chats/{chat['id']}/image", headers=hub["owner"])
    assert dropped.status_code == 200 and dropped.json()["image_url"] is None
    assert client.get(f"/v1/chats/{chat['id']}/image", headers=hub["owner"]).status_code == 404
    assert pending(hub_db) == [(sha(JPEG), "chat-image")]
    collect(client)
    assert s3.keys("blobs/") == []
    again = client.delete(f"/v1/chats/{chat['id']}/image", headers=hub["owner"])  # none to take off: no change
    assert again.status_code == 200 and again.json()["image_url"] is None
    url = f"/v1/chats/{chat['id']}/image?v="
    assert image_events(client, hub["owner"], chat["id"]) == [
        (url + sha(PNG)[:16], OWNER),
        (url + sha(JPEG)[:16], OWNER),
        (None, OWNER),
    ]
    assert [row[1] for row in image_audit(hub_db)][-1] == f"chat:{chat['id']} image:none"


def test_an_image_another_chat_shows_is_kept_and_one_set_again_is_put_back(client, hub, hub_db, s3):
    first = created(client, hub["owner"], title="First", agents=["reviewer"])
    second = created(client, hub["owner"], title="Second", agents=["reviewer"])
    image_set(client, hub["owner"], first["id"], PNG)
    image_set(client, hub["owner"], second["id"], PNG)
    assert client.delete(f"/v1/chats/{first['id']}/image", headers=hub["owner"]).status_code == 200
    assert pending(hub_db) == []  # the second chat still shows it
    collect(client)
    assert s3.get(f"blobs/sha256/{sha(PNG)}") == PNG
    assert client.delete(f"/v1/chats/{second['id']}/image", headers=hub["owner"]).status_code == 200
    assert pending(hub_db) == [(sha(PNG), "chat-image")]
    image_set(client, hub["owner"], first["id"], PNG)  # set again while its deletion waits: no longer pending
    assert pending(hub_db) == []
    collect(client)
    assert client.get(f"/v1/chats/{first['id']}/image", headers=hub["owner"]).content == PNG
    # once its object is gone, setting it again puts the bytes back
    assert client.delete(f"/v1/chats/{first['id']}/image", headers=hub["owner"]).status_code == 200
    collect(client)
    assert s3.get(f"blobs/sha256/{sha(PNG)}") is None
    image_set(client, hub["owner"], second["id"], PNG)
    assert s3.get(f"blobs/sha256/{sha(PNG)}") == PNG and pending(hub_db) == []
    assert client.get(f"/v1/chats/{second['id']}/image", headers=hub["owner"]).content == PNG


def test_the_blob_collection_counts_a_chat_image_as_a_reference(client, hub, hub_db):
    chat = created(client, hub["owner"], agents=["reviewer"])
    image_set(client, hub["owner"], chat["id"], WEBP)
    loose = "e" * 64

    async def referenced(engine):
        async with engine.begin() as conn:
            return await blob_gc.referenced(conn, [sha(WEBP), loose])

    assert client.portal.call(partial(referenced, client.app.state.engine)) == {sha(WEBP)}


# Migration 0051


@pytest.mark.empty_db
def test_0051_gives_group_chats_an_image_and_chat_events_the_image_event(hub_db):
    move_to(hub_db, "0050")
    t = tables
    chats = t.chats
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        group = one(conn, insert(chats).values(title="Release talk", created_by=ids["user"]).returning(chats.c.id))
        profile = {"project_id": ids["project"], "handle": "reviewer", "name": "Reviewer", "color": "#475569"}
        profile |= {"is_default": False, "permissions": {}, "created_by": ids["user"]}
        profile_id = one(conn, insert(t.agent_profiles).values(profile).returning(t.agent_profiles.c.id))
        pair = {"title": "Reviewer", "created_by": ids["user"], "kind": "direct", "direct_user_id": ids["user"]}
        direct = one(conn, insert(chats).values({**pair, "direct_profile_id": profile_id}).returning(chats.c.id))
        with pytest.raises(errors.CheckViolation):  # no image event before 0051
            conn.execute(insert(t.chat_events).values(chat_id=group, seq=1, kind="image_changed", body={}))
    move_to(hub_db, "0051")
    assert {"image_sha256", "image_size"} <= columns(hub_db, "chats")
    assert query(hub_db, select(chats.c.id, chats.c.image_sha256, chats.c.image_size).order_by(chats.c.id)) == [
        (group, None, None),
        (direct, None, None),
    ]
    image = {"image_sha256": "a" * 64, "image_size": 10}
    with live.connect(hub_db) as conn:
        conn.execute(chats.update().values(image).where(chats.c.id == group))
        event = {"sha256": "a" * 64, "by": "Octo"}
        conn.execute(insert(t.chat_events).values(chat_id=group, seq=1, kind="image_changed", body=event))
        conn.execute(insert(t.chat_events).values(chat_id=group, seq=2, kind="message", body={"message_id": 1}))
    for bad, chat_id in (
        (image, direct),  # a direct chat's face is its agent's
        ({"image_sha256": "A" * 64, "image_size": 10}, group),
        ({"image_sha256": "a" * 64, "image_size": 0}, group),
        ({"image_sha256": None, "image_size": 10}, group),
        ({"image_sha256": "a" * 64, "image_size": None}, group),
    ):
        with pytest.raises(errors.CheckViolation), live.connect(hub_db) as conn:
            conn.execute(chats.update().values(bad).where(chats.c.id == chat_id))
    move_to(hub_db, "0050", down=True)
    assert not {"image_sha256", "image_size"} & columns(hub_db, "chats")
    assert query(hub_db, select(t.chat_events.c.seq, t.chat_events.c.kind)) == [(2, "message")]
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0051") :]

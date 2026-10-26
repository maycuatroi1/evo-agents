"""The feature docs of the hub (``evo_agents.hub.server.docs``): a project the feature is on in keeps documents,
Markdown with images. A writer uploads the images to the hub's own blob store (PNG, JPEG, WebP or GIF, checked by
their first bytes, at most 10 MiB, kept once per project) and saves a document, each save the next version and a save
that changes nothing none; a stale base version is a 409. Readers read a document at any version with the images it
shows, which the hub serves from its own origin to them alone; a document carries the project's label, or one an
admin gives it, and reads follow it. The tool docs__read reads them on /mcp. A project the feature is off in answers
404.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.docs import images
from tests.hub import live
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_mcp import call
from tests.hub.test_plans import registration
from tests.hub.test_plugin_points import rpc, tool_names
from tests.hub.test_runs import PROJECT

BASE = f"/v1/x/docs/{PROJECT}"
GUIDE = f"{BASE}/documents/guide"
MEMBERS = {  # login -> (github id, role, max level)
    "owner": (701, "admin", "internal"),
    "alice": (702, "writer", "internal"),
    "bao": (703, "writer", "internal"),
    "chi": (704, "writer", "public"),
    "eve": (705, "reader", "internal"),
    "outsider": (706, "reader", "public"),
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + secrets.token_bytes(64)
GIF = b"GIF89a" + secrets.token_bytes(64)
JPEG = b"\xff\xd8\xff\xe0" + secrets.token_bytes(64)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
BODY = '# Guide\n\n![The board](shots/board.png "Tasks > Board")\n\nRead the board first.\n'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, public_url="https://hub.test", **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def members(client, github) -> dict:
    """The project with its members of MEMBERS and the hub admin; the headers of each."""
    headers = {"admin": bearer(live.sign_in(client, github, ADMIN, 302)["token"])}
    registered = client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"])
    assert registered.status_code == 200, registered.text
    for login, (github_id, role, level) in MEMBERS.items():
        headers[login] = bearer(live.sign_in(client, github, login, github_id)["token"])
        grant = {"role": role, "max_level": level}
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
    return headers


def enable(client, headers) -> None:
    enabled = client.post(f"/v1/projects/{PROJECT}/plugins/docs/enable", json={"permissions": []}, headers=headers)
    assert enabled.status_code == 200, enabled.text


@pytest.fixture
def hub(client, members) -> dict:
    """``members`` with the feature docs on in the project."""
    enable(client, members["owner"])
    return members


def upload(client, headers, data: bytes, **extra):
    return client.post(f"{BASE}/images", content=data, headers={**headers, **extra})


def uploaded(client, headers, data: bytes) -> dict:
    answer = upload(client, headers, data)
    assert answer.status_code == 201, answer.text
    return answer.json()


def save(client, headers, path: str = GUIDE, **body):
    return client.put(path, json={"title": "Guide", "body": BODY, **body}, headers=headers)


def saved(client, headers, path: str = GUIDE, **body) -> dict:
    answer = save(client, headers, path, **body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def shown(client, headers, path: str = GUIDE, **params) -> dict:
    answer = client.get(path, params=params, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def board(data: bytes = PNG) -> list[dict]:
    return [{"name": "shots/board.png", "sha256": sha(data)}]


# The feature


def test_docs_answer_404_where_the_feature_is_off_and_enabling_it_turns_them_on(client, members):
    for path in (f"{BASE}/documents", GUIDE):
        assert client.get(path, headers=members["alice"]).status_code == 404
    assert save(client, members["alice"]).status_code == 404
    assert "docs__read" not in tool_names(client, members["alice"])
    enable(client, members["owner"])
    assert client.get(f"{BASE}/documents", headers=members["alice"]).json() == []
    assert "docs__read" in tool_names(client, members["alice"])


# Images


def test_a_writer_uploads_images_kept_once_by_their_hash_and_only_images(client, hub, hub_db, s3):
    first = uploaded(client, hub["alice"], PNG)
    assert first == {"sha256": sha(PNG), "media_type": "image/png", "size": len(PNG), "held": False}
    assert uploaded(client, hub["chi"], PNG)["held"] is True  # the project holds it: one row, one object
    assert uploaded(client, hub["alice"], GIF)["media_type"] == "image/gif"
    assert uploaded(client, hub["alice"], JPEG)["media_type"] == "image/jpeg"
    assert s3.get(f"blobs/sha256/{sha(PNG)}") == PNG
    i = tables.docs.images
    rows = sql(hub_db, select(i.c.sha256, i.c.media_type).order_by(i.c.media_type))
    assert rows == [(sha(GIF), "image/gif"), (sha(JPEG), "image/jpeg"), (sha(PNG), "image/png")]
    for data in (SVG, b"", b"plain text, not an image"):  # an SVG can carry a script: never an image here
        refused = upload(client, hub["alice"], data)
        assert refused.status_code == 422 and "PNG, JPEG, WebP or GIF" in refused.json()["message"], refused.text
    refused = upload(client, hub["eve"], PNG)
    assert refused.status_code == 403 and "writer role" in refused.json()["message"]
    # an image of a document is referred to, so the collection of unreferenced blobs keeps it
    with live.connect(hub_db) as conn:
        assert sha(PNG) in conn.execute(blob_gc._referenced([sha(PNG)])).scalars().all()


def test_an_image_over_the_limit_is_refused_before_anything_is_kept(client, hub, hub_db, monkeypatch):
    monkeypatch.setattr(images, "MAX_IMAGE_BYTES", 64)
    refused = upload(client, hub["alice"], PNG)
    assert refused.status_code == 413 and "at most 64 bytes" in refused.json()["message"], refused.text
    assert sql(hub_db, select(tables.docs.images.c.sha256)) == []


# Saving and reading


def test_a_save_is_the_next_version_and_one_that_changes_nothing_writes_nothing(client, hub):
    uploaded(client, hub["alice"], PNG)
    first = saved(client, hub["alice"], images=board(), summary="first draft")
    assert (first["created"], first["changed"], first["version"]) == (True, True, 1)
    assert first["document"]["slug"] == "guide" and first["document"]["created_by"] == "alice"
    assert first["document"]["label"]["level"] == "internal"  # the project's default
    again = saved(client, hub["alice"], images=board(), summary="nothing new")
    assert (again["created"], again["changed"], again["version"]) == (False, False, 1)
    second = saved(client, hub["bao"], title="Guide for the board", body=BODY + "\nThen the chat.\n", images=board())
    assert (second["changed"], second["version"], second["document"]["updated_by"]) == (True, 2, "bao")

    doc = shown(client, hub["eve"])  # a reader reads it
    assert (doc["title"], doc["latest_version"], doc["created_by"], doc["updated_by"]) == (
        "Guide for the board",
        2,
        "alice",
        "bao",
    )
    assert [(v["number"], v["author"], v["summary"]) for v in doc["versions"]] == [
        (2, "bao", ""),
        (1, "alice", "first draft"),
    ]
    assert doc["shown"]["number"] == 2 and doc["shown"]["body"].endswith("Then the chat.\n")
    url = f"{GUIDE}/images/{sha(PNG)}"
    assert doc["shown"]["images"] == [
        {"name": "shots/board.png", "sha256": sha(PNG), "media_type": "image/png", "size": len(PNG), "url": url}
    ]
    old = shown(client, hub["eve"], version=1)["shown"]
    assert (old["number"], old["title"], old["body"]) == (1, "Guide", BODY)
    missing = client.get(GUIDE, params={"version": 3}, headers=hub["eve"])
    assert missing.status_code == 404 and "has no version 3" in missing.json()["message"]
    listed = client.get(f"{GUIDE}/versions", headers=hub["eve"]).json()
    assert [v["number"] for v in listed] == [2, 1]
    assert [d["slug"] for d in client.get(f"{BASE}/documents", headers=hub["eve"]).json()] == ["guide"]


def test_a_save_on_a_stale_version_is_refused_and_images_must_be_the_projects(client, hub):
    uploaded(client, hub["alice"], PNG)
    saved(client, hub["alice"], images=board())
    saved(client, hub["bao"], body="Bao's text", base_version=1)
    stale = save(client, hub["alice"], body="Alice's text", base_version=1)
    assert stale.status_code == 409 and "is at version 2, not 1" in stale.json()["message"], stale.text
    assert shown(client, hub["alice"])["shown"]["body"] == "Bao's text"
    unknown = save(client, hub["alice"], images=board(GIF))  # never uploaded
    assert unknown.status_code == 422 and "no image of project" in unknown.json()["message"], unknown.text
    twice = save(client, hub["alice"], images=board() + board())
    assert twice.status_code == 422, twice.text
    for name in ("../secret.png", "/etc/passwd", "shots/.hidden.png", "a b.png"):
        bad = save(client, hub["alice"], images=[{"name": name, "sha256": sha(PNG)}])
        assert bad.status_code == 422, (name, bad.text)
    new = save(client, hub["alice"], f"{BASE}/documents/notes", base_version=1)
    assert new.status_code == 409 and "does not exist" in new.json()["message"]
    taken = save(client, hub["alice"], base_version=0)  # a new document, and guide exists
    assert taken.status_code == 409 and "exists already" in taken.json()["message"], taken.text
    assert saved(client, hub["alice"], f"{BASE}/documents/notes", base_version=0)["created"] is True
    reader = save(client, hub["eve"], f"{BASE}/documents/notes")
    assert reader.status_code == 403 and "writer role" in reader.json()["message"]


def test_the_hub_serves_an_image_a_version_shows_to_the_documents_readers_alone(client, hub):
    uploaded(client, hub["alice"], PNG)
    uploaded(client, hub["alice"], GIF)
    saved(client, hub["alice"], images=board())
    url = f"{GUIDE}/images/{sha(PNG)}"
    served = client.get(url, headers=hub["eve"])
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["etag"] == f'"{sha(PNG)}"'
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["cross-origin-resource-policy"] == "same-origin"
    assert served.headers["content-security-policy"] == "default-src 'none'; sandbox"
    kept = client.get(url, headers={**hub["eve"], "If-None-Match": f'"{sha(PNG)}"'})
    assert kept.status_code == 304 and kept.content == b""
    # an image of the project that no version of this document shows is not served through it
    other = client.get(f"{GUIDE}/images/{sha(GIF)}", headers=hub["eve"])
    assert other.status_code == 404 and "no version of document guide shows" in other.json()["message"]
    # the document is internal: a reader up to public sees neither it nor its images
    for path in (GUIDE, url, f"{GUIDE}/versions"):
        hidden = client.get(path, headers=hub["outsider"])
        assert hidden.status_code == 404 and "no document guide" in hidden.json()["message"], path
    assert client.get(f"{BASE}/documents", headers=hub["outsider"]).json() == []
    assert client.get(url).status_code == 401


def test_an_admin_gives_a_new_document_its_label_and_a_writer_cannot_make_one_they_cannot_read(client, hub):
    refused = save(client, hub["alice"], f"{BASE}/documents/open", label={"level": "public"})
    assert refused.status_code == 403 and "only an admin" in refused.json()["message"], refused.text
    made = saved(client, hub["owner"], f"{BASE}/documents/open", label={"level": "public"})
    assert made["document"]["label"]["level"] == "public"
    assert [d["slug"] for d in client.get(f"{BASE}/documents", headers=hub["outsider"]).json()] == ["open"]
    edited = saved(client, hub["chi"], f"{BASE}/documents/open", body="Chi edits what she reads")
    assert (edited["version"], edited["document"]["label"]["level"]) == (2, "public")
    relabel = save(client, hub["owner"], f"{BASE}/documents/open", body="x", label={"level": "internal"})
    assert relabel.status_code == 422 and "label was given when it was created" in relabel.json()["message"]
    blind = save(client, hub["chi"], f"{BASE}/documents/internal-note")  # the project's label is above her grant
    assert blind.status_code == 422 and "your grant does not read" in blind.json()["message"], blind.text


# The tool


def test_docs_read_lists_the_documents_and_reads_a_version_as_markdown(client, hub):
    uploaded(client, hub["alice"], PNG)
    saved(client, hub["alice"], images=board())
    saved(client, hub["alice"], body="Second text", images=[])
    listing = rpc(client, hub["eve"], call(9, "docs__read", {}))
    assert not listing["isError"]
    assert listing["content"][0]["text"] == (f"The documents of project {PROJECT}:\n- guide: Guide; version 2 by alice")
    first = rpc(client, hub["eve"], call(9, "docs__read", {"document": "guide", "version": 1}))["content"][0]["text"]
    assert first.startswith("guide: Guide\nVersion 1 of 2, by alice, ")
    assert "Images it shows: shots/board.png (image/png)" in first and first.endswith(BODY)
    latest = rpc(client, hub["eve"], call(9, "docs__read", {"document": "guide"}))["content"][0]["text"]
    assert latest.endswith("\n\nSecond text") and "Images it shows" not in latest
    hidden = rpc(client, hub["outsider"], call(9, "docs__read", {"document": "guide"}))
    assert hidden["isError"] and "no document guide" in hidden["content"][0]["text"]

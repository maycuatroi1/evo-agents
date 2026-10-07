"""The blob store: uploads through presigned PUTs to a fake S3 (moto, in this process), commits that read the bytes
back and hash them, the size limit of each kind, the writer role, health with R2 down, retries, the cleanup of
uploads nobody committed, and S3 credentials kept out of every log line. Also what bounds a commit's time on a store
as slow to answer as R2: the calls it makes per upload, and the uploads all commits of a process work on at once.

Each test gets a Postgres database and a bucket of its own; moto checks no signature (see ``tests.hub.s3``)."""

import asyncio
import hashlib
import io
import json
import logging
import re
import threading
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.hub import live, pg
from tests.hub.s3 import get_url, put_presigned

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from botocore.awsrequest import AWSResponse
from botocore.exceptions import ResponseStreamingError
from fastapi.testclient import TestClient
from psycopg import errors

from evo_agents import __version__
from evo_agents.hub.blobs import (
    KIND_LIMITS,
    BlobStore,
    BlobStoreUnavailable,
    Upload,
    blob_key,
    new_upload_id,
    upload_key,
)
from evo_agents.hub.config import S3_VARIABLES, ConfigError, HubConfig, load_config
from evo_agents.hub.db import open_pool
from evo_agents.hub.log import JsonFormatter, scrub
from evo_agents.hub.migrate import head_revision, migrate
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import remove_stale_uploads

HEAD = head_revision()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_config(db, tmp_path, s3=None) -> HubConfig:
    return HubConfig(
        dsn=db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=5.0,
        admins=frozenset({live.ADMIN}),
        **(s3.config() if s3 else {}),
    )


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    app = create_app(make_config(hub_db, tmp_path, s3))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, db=hub_db, s3=s3, store=app.state.blobs)


def member(db, project_id: int, login: str, role: str | None) -> dict:
    """The Authorization header of ``login``, holding ``role`` on the project (no grant when None)."""
    token = live.insert_token(db, login)
    if role is not None:
        live.sql(
            db,
            "INSERT INTO grants (user_id, project_id, role, max_level, granted_by) SELECT u.id, p.id, %s, "
            "p.levels[1], p.created_by FROM users u, projects p WHERE u.login = %s AND p.id = %s",
            (role, login, project_id),
        )
    return live.bearer(token)


def item(data: bytes, kind: str = "kg-blob") -> dict:
    return {"sha256": sha(data), "size": len(data), "kind": kind}


def ask(hub, headers, project: str, *items: dict):
    return hub.client.post("/v1/blobs/uploads", json={"project": project, "items": list(items)}, headers=headers)


def commit(hub, headers, project: str, *upload_ids: str):
    return hub.client.post(
        "/v1/blobs/commit", json={"project": project, "upload_ids": list(upload_ids)}, headers=headers
    )


def ticket(hub, headers, project: str, data: bytes, kind: str = "kg-blob") -> dict:
    response = ask(hub, headers, project, item(data, kind))
    assert response.status_code == 200, response.text
    (found,) = response.json()["uploads"]
    return found


def upload(hub, headers, project: str, data: bytes, kind: str = "kg-blob") -> dict:
    """Ask, PUT and commit one blob; the commit's answer."""
    found = ticket(hub, headers, project, data, kind)
    assert put_presigned(found["url"], data) == 200
    response = commit(hub, headers, project, found["upload_id"])
    assert response.status_code == 200, response.text
    return response.json()


def blob_rows(db) -> list[tuple]:
    return live.sql(
        db,
        "SELECT p.name, b.sha256, b.size, b.kind, u.login, b.verified_at IS NOT NULL FROM blobs b "
        "JOIN projects p ON p.id = b.project_id LEFT JOIN users u ON u.id = b.created_by ORDER BY 1, 2",
    )


def pending(db) -> int:
    return live.sql(db, "SELECT count(*) FROM blob_uploads")[0][0]


def head(s3, key: str) -> tuple:
    found = s3.client().head_object(Bucket=s3.bucket, Key=key)
    return found["ETag"], found["LastModified"], found["ContentLength"]


# Uploads and commits


def test_an_upload_through_a_presigned_put_then_commit_writes_exactly_one_blob_row(hub):
    alpha = live.add_project(hub.db, "alpha")
    writer = member(hub.db, alpha, "writer-1", "writer")
    data = b"skill bundle bytes\n" * 200
    asked = ask(hub, writer, "alpha", item(data, "skill-bundle"))
    assert asked.status_code == 200, asked.text
    body = asked.json()
    (found,) = body["uploads"]
    assert body["present"] == [] and found["sha256"] == sha(data)
    left = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)
    url = urlsplit(found["url"])
    assert url.path == f"/{hub.s3.bucket}/uploads/{found['upload_id']}"
    query = parse_qs(url.query)
    assert query["X-Amz-Expires"] == ["900"] and query["X-Amz-SignedHeaders"] == ["content-length;host"]
    assert pending(hub.db) == 1 and blob_rows(hub.db) == []

    assert put_presigned(found["url"], data) == 200
    committed = commit(hub, writer, "alpha", found["upload_id"])
    assert committed.status_code == 200, committed.text
    assert committed.json() == {"blobs": [{"sha256": sha(data), "size": len(data), "kind": "skill-bundle"}], "added": 1}
    assert blob_rows(hub.db) == [("alpha", sha(data), len(data), "skill-bundle", "writer-1", True)]
    assert hub.s3.get(blob_key(sha(data))) == data
    assert hub.s3.keys("uploads/") == []  # the upload and its sealed copy are gone
    assert pending(hub.db) == 0
    filed = "SELECT a.action, a.target, p.name FROM audit a LEFT JOIN projects p ON p.id = a.project_id"
    assert live.sql(hub.db, filed) == [("blob.commit", "alpha", "alpha")]

    again = commit(hub, writer, "alpha", found["upload_id"])  # an upload commits once
    assert again.status_code == 422 and "unknown uploads" in again.json()["message"]
    assert len(blob_rows(hub.db)) == 1


def test_a_staged_object_unlike_its_declared_sha256_is_422_deleted_and_leaves_another_projects_blob_alone(hub):
    alpha, beta = live.add_project(hub.db, "alpha"), live.add_project(hub.db, "beta")
    alice = member(hub.db, alpha, "alice", "writer")
    bob = member(hub.db, beta, "bob", "writer")
    good = b"the bytes alpha uploaded and the hub verified\n" * 40
    upload(hub, alice, "alpha", good)
    before = head(hub.s3, blob_key(sha(good)))

    # beta declares alpha's hash but PUTs other bytes of the same size
    forged = bytes(reversed(good))
    claimed = ticket(hub, bob, "beta", good)
    assert put_presigned(claimed["url"], forged) == 200
    refused = commit(hub, bob, "beta", claimed["upload_id"])
    assert refused.status_code == 422
    body = refused.json()
    assert body["error"] == "invalid_request" and "Nothing was committed" in body["message"]
    assert body["detail"] == [
        {
            "upload_id": claimed["upload_id"],
            "sha256": sha(good),
            "problem": "the bytes uploaded do not have the declared SHA-256",
        }
    ]
    assert hub.s3.keys("uploads/") == [] and pending(hub.db) == 0  # the staged object and its sealed copy are gone
    assert hub.s3.get(blob_key(sha(good))) == good and head(hub.s3, blob_key(sha(good))) == before
    assert hub.s3.get(blob_key(sha(forged))) is None
    assert [row[0] for row in blob_rows(hub.db)] == ["alpha"]

    # One upload that does not match fails the whole commit: the good one is discarded with it.
    other = b"a blob of beta's own"
    tickets = ask(hub, bob, "beta", item(other), item(good)).json()["uploads"]
    by_hash = {t["sha256"]: t for t in tickets}
    put_presigned(by_hash[sha(other)]["url"], other)
    put_presigned(by_hash[sha(good)]["url"], good[:-1] + b"?")
    both = commit(hub, bob, "beta", *[t["upload_id"] for t in tickets])
    assert both.status_code == 422 and [d["sha256"] for d in both.json()["detail"]] == [sha(good)]
    assert [row[0] for row in blob_rows(hub.db)] == ["alpha"]
    assert hub.s3.keys("uploads/") == [] and hub.s3.get(blob_key(sha(other))) is None

    # Another size, or nothing uploaded at all, is refused the same way.
    short = ticket(hub, bob, "beta", good)
    put_presigned(short["url"], good[:10])
    problem = commit(hub, bob, "beta", short["upload_id"]).json()["detail"][0]["problem"]
    assert problem == f"10 bytes were uploaded, {len(good)} were declared"
    never = ticket(hub, bob, "beta", good)
    assert commit(hub, bob, "beta", never["upload_id"]).json()["detail"][0]["problem"] == "nothing was uploaded"
    assert head(hub.s3, blob_key(sha(good))) == before and pending(hub.db) == 0


def store_calls(store) -> list[str]:
    """The S3 operations ``store`` sends from now on, by name, as botocore sends them (retries included)."""
    sent: list[str] = []

    def hook(event_name, **kwargs):
        sent.append(event_name.rsplit(".", 1)[-1])

    store._s3.meta.events.register("before-send.s3", hook)
    return sent


def staged(hub, headers, project: str, blobs: list[bytes]) -> list[str]:
    """Ask for uploads of ``blobs`` and PUT them; the upload ids, ready to commit."""
    asked = ask(hub, headers, project, *(item(data) for data in blobs))
    assert asked.status_code == 200, asked.text
    tickets = asked.json()["uploads"]
    by_hash = {sha(data): data for data in blobs}
    for found in tickets:
        assert put_presigned(found["url"], by_hash[found["sha256"]]) == 200
    return [found["upload_id"] for found in tickets]


def test_a_commit_makes_three_store_calls_per_upload_and_one_delete(hub):
    """On R2 every call took about 250 ms: a commit of 459 uploads at five calls each, 16 at a time, took 40 s in
    production while the push waited 30. Now: copy to the sealed key, read it, copy it to the blob key, or for a blob
    the hub records already ask whether its object is there instead of copying."""
    alpha, beta = live.add_project(hub.db, "alpha"), live.add_project(hub.db, "beta")
    alice = member(hub.db, alpha, "alice", "writer")
    bob = member(hub.db, beta, "bob", "writer")
    blobs = [f"blob {i} of a run\n".encode() * (i + 1) for i in range(40)]
    ids = staged(hub, alice, "alpha", blobs)
    sent = store_calls(hub.store)
    committed = commit(hub, alice, "alpha", *ids)
    assert committed.status_code == 200, committed.text and committed.json()["added"] == 40
    assert sorted(sent) == sorted(["CopyObject"] * 80 + ["GetObject"] * 40 + ["DeleteObjects"])
    assert all(hub.s3.get(blob_key(sha(data))) == data for data in blobs)

    ids = staged(hub, bob, "beta", blobs[:10])  # beta must upload what alpha holds, and the hub checks it again
    sent.clear()
    assert commit(hub, bob, "beta", *ids).json()["added"] == 10
    assert sorted(sent) == sorted(["CopyObject"] * 10 + ["GetObject"] * 10 + ["HeadObject"] * 10 + ["DeleteObjects"])
    assert hub.s3.keys("uploads/") == []


def test_commits_running_at_once_share_one_bound_on_the_uploads_they_work_on(hub):
    """Each commit used to run 16 uploads at once, so three commits ran 48 against the store. The store's slots
    (EVO_HUB_BLOB_CONCURRENCY, 32 by default) are shared by every commit of the process."""
    alpha = live.add_project(hub.db, "alpha")
    alice = member(hub.db, alpha, "alice", "writer")
    batches = [[f"commit {c} blob {i}\n".encode() for i in range(40)] for c in range(3)]
    ids = [staged(hub, alice, "alpha", blobs) for blobs in batches]
    lock = threading.Lock()
    calls = SimpleNamespace(now=0, peak=0)

    def slow(**kwargs):  # every call takes a while, as on R2, and is counted while it does
        with lock:
            calls.now += 1
            calls.peak = max(calls.peak, calls.now)
        time.sleep(0.03)
        with lock:
            calls.now -= 1

    for operation in ("CopyObject", "GetObject", "HeadObject"):  # what an upload takes; the delete comes after
        hub.store._s3.meta.events.register(f"before-send.s3.{operation}", slow)
    start = threading.Barrier(3)
    answers = []

    def run(upload_ids):
        start.wait()
        answers.append(commit(hub, alice, "alpha", *upload_ids))

    threads = [threading.Thread(target=run, args=(upload_ids,)) for upload_ids in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert [a.status_code for a in answers] == [200, 200, 200], [a.text for a in answers]
    assert sum(a.json()["added"] for a in answers) == 120
    assert 16 < calls.peak <= 32, calls.peak  # parallel, within the shared bound


def test_the_store_takes_its_bound_from_the_configuration(s3, hub_db, tmp_path):
    config = make_config(hub_db, tmp_path, s3)
    store = BlobStore.from_config(config)
    assert store.concurrency == 32 and store._s3.meta.config.max_pool_connections >= 32
    store.close()
    narrow = BlobStore.from_config(
        load_config({"EVO_HUB_DSN": hub_db.dsn, "EVO_HUB_BLOB_CONCURRENCY": "4", **s3.env()})
    )
    assert narrow.concurrency == 4
    narrow.close()
    with pytest.raises(ValueError, match="between 1 and 256"):
        BlobStore(s3.endpoint, s3.bucket, s3.access_key_id, s3.secret_access_key, concurrency=0)


def test_an_upload_larger_than_declared_is_refused_from_its_size_without_reading_it(hub, monkeypatch):
    declared, sent = b"x" * 100, b"y" * 5000
    upload_ = Upload(new_upload_id(), sha(declared), len(declared), "kg-blob")
    hub.s3.put(upload_key(upload_.upload_id), sent)
    real = hub.store._s3.get_object
    read = []

    def counting(**kwargs):
        response = real(**kwargs)
        body = response["Body"]
        original = body.iter_chunks

        def chunks(size):
            for chunk in original(size):
                read.append(len(chunk))
                yield chunk

        body.iter_chunks = chunks
        return response

    monkeypatch.setattr(hub.store._s3, "get_object", counting)
    assert hub.store.seal(upload_).problem == "5000 bytes were uploaded, 100 were declared"
    assert read == []


def test_bytes_put_again_after_the_check_never_reach_the_blob(hub):
    """The URL works for 15 minutes; a second PUT between the check and the copy changes only the upload, since the
    hub copies its sealed copy, which it hashed."""
    good, forged = b"checked bytes" * 10, b"swapped bytes" * 10
    upload_ = Upload(new_upload_id(), sha(good), len(good), "kg-blob")
    hub.s3.put(upload_key(upload_.upload_id), good)
    assert hub.store.seal(upload_).problem is None
    hub.s3.put(upload_key(upload_.upload_id), forged)
    assert hub.store.publish(upload_) is True
    assert hub.s3.get(blob_key(sha(good))) == good


def test_a_blob_the_project_holds_gets_no_url_again(hub):
    alpha = live.add_project(hub.db, "alpha")
    writer = member(hub.db, alpha, "writer-1", "writer")
    held, new = b"held already", b"not yet"
    upload(hub, writer, "alpha", held)
    asked = ask(hub, writer, "alpha", item(held), item(new), item(held))
    assert asked.status_code == 200
    body = asked.json()
    assert body["present"] == [sha(held)]
    assert [t["sha256"] for t in body["uploads"]] == [sha(new)]
    assert pending(hub.db) == 1


def test_a_blob_only_another_project_holds_must_be_uploaded_and_checked_again(hub):
    alpha, beta = live.add_project(hub.db, "alpha"), live.add_project(hub.db, "beta")
    alice = member(hub.db, alpha, "alice", "writer")
    bob = member(hub.db, beta, "bob", "writer")
    data = b"bytes both projects use\n" * 30
    upload(hub, alice, "alpha", data)

    asked = ask(hub, bob, "beta", item(data))
    assert asked.json()["present"] == [] and len(asked.json()["uploads"]) == 1  # knowing the hash is not enough
    unsent = asked.json()["uploads"][0]
    refused = commit(hub, bob, "beta", unsent["upload_id"])
    assert refused.status_code == 422 and refused.json()["detail"][0]["problem"] == "nothing was uploaded"
    assert [row[0] for row in blob_rows(hub.db)] == ["alpha"]

    assert upload(hub, bob, "beta", data)["added"] == 1
    assert [row[:2] for row in blob_rows(hub.db)] == [("alpha", sha(data)), ("beta", sha(data))]
    assert hub.s3.get(blob_key(sha(data))) == data and hub.s3.keys("uploads/") == []
    assert ask(hub, bob, "beta", item(data)).json()["present"] == [sha(data)]


def test_a_reader_asking_for_upload_urls_gets_403_and_nothing_is_issued(hub):
    alpha = live.add_project(hub.db, "alpha")
    reader = member(hub.db, alpha, "reader-1", "reader")
    stranger = member(hub.db, alpha, "stranger", None)
    admin = member(hub.db, alpha, live.ADMIN, None)  # a hub admin without a grant manages, but pushes nothing
    data = b"anything"
    refused = ask(hub, reader, "alpha", item(data))
    assert refused.status_code == 403 and refused.json()["error"] == "forbidden"
    assert "needs the writer role" in refused.json()["message"] and "url" not in refused.text
    assert ask(hub, admin, "alpha", item(data)).status_code == 403
    assert ask(hub, stranger, "alpha", item(data)).status_code == 404
    assert ask(hub, reader, "no-such-project", item(data)).status_code == 404
    assert hub.client.post("/v1/blobs/uploads", json={"project": "alpha", "items": [item(data)]}).status_code == 401
    assert commit(hub, reader, "alpha", str(uuid.uuid4())).status_code == 403
    assert pending(hub.db) == 0 and hub.s3.keys() == []

    writer = member(hub.db, alpha, "writer-1", "writer")
    found = ticket(hub, writer, "alpha", data)
    put_presigned(found["url"], data)
    assert commit(hub, reader, "alpha", found["upload_id"]).status_code == 403  # a reader commits nothing either
    assert blob_rows(hub.db) == [] and pending(hub.db) == 1


def test_an_item_over_the_limit_of_its_kind_is_refused_before_any_url_is_issued(hub):
    alpha = live.add_project(hub.db, "alpha")
    writer = member(hub.db, alpha, "writer-1", "writer")
    limit = KIND_LIMITS["skill-bundle"]
    assert limit == 10 * 1024 * 1024
    over = {"sha256": "a" * 64, "size": limit + 1, "kind": "skill-bundle"}
    fine = {"sha256": "b" * 64, "size": 10, "kind": "kg-blob"}
    refused = ask(hub, writer, "alpha", fine, over)
    assert refused.status_code == 413
    body = refused.json()
    assert body["error"] == "too_large" and "no upload was issued" in body["message"]
    assert body["detail"] == [{"sha256": "a" * 64, "kind": "skill-bundle", "size": limit + 1, "limit": limit}]
    assert "X-Amz" not in refused.text and pending(hub.db) == 0

    at_limit = ask(hub, writer, "alpha", {"sha256": "c" * 64, "size": limit, "kind": "skill-bundle"})
    assert at_limit.status_code == 200 and len(at_limit.json()["uploads"]) == 1

    for bad in (
        {"sha256": "d" * 64, "size": 1, "kind": "movie"},
        {"sha256": "d" * 64, "size": -1, "kind": "kg-blob"},
        {"sha256": "D" * 64, "size": 1, "kind": "kg-blob"},
        {"sha256": "d" * 63, "size": 1, "kind": "kg-blob"},
    ):
        assert ask(hub, writer, "alpha", bad).status_code == 422, bad
    conflicting = ask(hub, writer, "alpha", {**fine, "size": 10}, {**fine, "size": 11})
    assert conflicting.status_code == 422 and "declared twice" in conflicting.json()["message"]
    too_many = [{"sha256": f"{n:064x}", "size": 1, "kind": "kg-blob"} for n in range(1001)]
    assert ask(hub, writer, "alpha", *too_many).status_code == 422
    assert pending(hub.db) == 1


def test_uploads_of_another_user_or_project_or_older_than_a_day_are_unknown_and_left_alone(hub):
    alpha, beta = live.add_project(hub.db, "alpha"), live.add_project(hub.db, "beta")
    alice = member(hub.db, alpha, "alice", "writer")
    carol = member(hub.db, alpha, "carol", "writer")
    bob = member(hub.db, beta, "bob", "writer")
    data = b"alice's upload"
    found = ticket(hub, alice, "alpha", data)
    put_presigned(found["url"], data)
    staged = upload_key(found["upload_id"])
    for headers, project in ((carol, "alpha"), (bob, "beta")):
        refused = commit(hub, headers, project, found["upload_id"])
        assert refused.status_code == 422 and "unknown uploads" in refused.json()["message"]
    assert commit(hub, alice, "alpha", str(uuid.uuid4())).status_code == 422
    assert commit(hub, alice, "alpha", "not-an-id").status_code == 422
    assert hub.s3.get(staged) == data and pending(hub.db) == 1

    live.sql(hub.db, "UPDATE blob_uploads SET created_at = now() - interval '25 hours'")
    assert commit(hub, alice, "alpha", found["upload_id"]).status_code == 422
    assert hub.s3.get(staged) == data and blob_rows(hub.db) == []


def test_presign_get_hands_out_a_short_lived_url_to_one_blob_and_no_route_reads_blobs(hub):
    alpha = live.add_project(hub.db, "alpha")
    writer = member(hub.db, alpha, "writer-1", "writer")
    data = b"read me through a presigned GET"
    upload(hub, writer, "alpha", data)
    url = hub.store.presign_get(sha(data))
    query = parse_qs(urlsplit(url).query)
    assert query["X-Amz-Expires"] == ["300"] and urlsplit(url).path.endswith(f"/blobs/sha256/{sha(data)}")
    assert get_url(url) == data
    assert parse_qs(urlsplit(hub.store.presign_get(sha(data), timedelta(seconds=30))).query)["X-Amz-Expires"] == ["30"]
    for expires in (timedelta(hours=1), timedelta(0)):
        with pytest.raises(ValueError):
            hub.store.presign_get(sha(data), expires)
    with pytest.raises(ValueError):
        hub.store.presign_get("../uploads/x")
    assert hub.client.get(f"/v1/blobs/{sha(data)}", headers=writer).status_code in (404, 405)
    assert not any(path.startswith("/v1/blobs") and "get" in ops for path, ops in hub.app.openapi()["paths"].items())


# R2 going away


def test_health_is_503_naming_r2_while_the_moto_server_is_stopped(hub):
    healthy = hub.client.get("/v1/health")
    assert healthy.status_code == 200
    assert healthy.json() == {
        "status": "ok",
        "version": __version__,
        "schema": HEAD,
        "db": "ok",
        "r2": "ok",
        "failed": [],
    }
    alpha = live.add_project(hub.db, "alpha")
    writer = member(hub.db, alpha, "writer-1", "writer")
    data = b"uploaded just before R2 went away"
    found = ticket(hub, writer, "alpha", data)
    put_presigned(found["url"], data)

    hub.s3.stop()
    started = time.monotonic()
    down = hub.client.get("/v1/health")
    assert time.monotonic() - started < 10
    assert down.status_code == 503 and down.headers["content-type"].startswith("application/json")
    assert down.json() == {
        "status": "unavailable",
        "version": __version__,
        "schema": HEAD,
        "db": "ok",
        "r2": "unavailable",
        "failed": ["r2"],
    }
    assert hub.client.get("/v1/health/live").status_code == 200
    failed = commit(hub, writer, "alpha", found["upload_id"])
    assert failed.status_code == 503 and failed.json()["error"] == "unavailable"
    assert "try again" in failed.json()["message"]
    assert pending(hub.db) == 1 and blob_rows(hub.db) == []  # the upload is kept for the retry

    hub.s3.start()  # same port, same objects
    assert hub.client.get("/v1/health").json()["r2"] == "ok"
    retried = commit(hub, writer, "alpha", found["upload_id"])
    assert retried.status_code == 200, retried.text
    assert [row[1] for row in blob_rows(hub.db)] == [sha(data)]


def test_without_the_blob_store_the_routes_answer_503_and_health_says_unconfigured(hub_db, tmp_path):
    app = create_app(make_config(hub_db, tmp_path))
    with TestClient(app) as client:
        health = client.get("/v1/health")
        assert health.status_code == 200 and health.json()["r2"] == "unconfigured"
        alpha = live.add_project(hub_db, "alpha")
        writer = member(hub_db, alpha, "writer-1", "writer")
        refused = client.post("/v1/blobs/uploads", json={"project": "alpha", "items": [item(b"x")]}, headers=writer)
        assert refused.status_code == 503 and refused.json()["error"] == "unavailable"
        assert ", ".join(S3_VARIABLES) in refused.json()["message"]
        body = {"project": "alpha", "upload_ids": [str(uuid.uuid4())]}
        assert client.post("/v1/blobs/commit", json=body, headers=writer).status_code == 503


class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def stream(self, **kwargs):
        yield self.body


def slow_down(attempts: list, failures: int):
    """A botocore before-send hook answering the first ``failures`` attempts with 503 SlowDown."""

    def hook(request, **kwargs):
        attempts.append(time.monotonic())
        if len(attempts) <= failures:
            body = b"<Error><Code>SlowDown</Code><Message>Please reduce your request rate.</Message></Error>"
            return AWSResponse(request.url, 503, {}, _Raw(body))
        return None

    return hook


def test_transient_errors_are_retried_with_backoff_and_then_fail_loudly(s3):
    s3.put("some/key", b"12345")
    store = BlobStore(s3.endpoint, s3.bucket, s3.access_key_id, s3.secret_access_key, attempts=3)
    attempts: list = []
    store._s3.meta.events.register("before-send.s3.HeadObject", slow_down(attempts, failures=2))
    assert store.size("some/key") == 5
    assert len(attempts) == 3  # two 503s, then the answer

    attempts.clear()
    store = BlobStore(s3.endpoint, s3.bucket, s3.access_key_id, s3.secret_access_key, attempts=3)
    store._s3.meta.events.register("before-send.s3.HeadObject", slow_down(attempts, failures=99))
    with pytest.raises(BlobStoreUnavailable, match=r"HeadObject \(SlowDown\)"):
        store.size("some/key")
    assert len(attempts) == 3


def test_a_body_cut_off_while_reading_is_read_again(s3, monkeypatch):
    data = b"read twice" * 1000
    s3.put("some/key", data)
    store = BlobStore(s3.endpoint, s3.bucket, s3.access_key_id, s3.secret_access_key)
    real = store._s3.get_object
    reads = []

    class CutOff:
        def iter_chunks(self, size):
            yield b"partial"
            raise ResponseStreamingError(error="connection reset")

        def close(self):
            pass

    def flaky(**kwargs):
        response = real(**kwargs)
        reads.append(1)
        if len(reads) == 1:
            response["Body"].close()
            response["Body"] = CutOff()
        return response

    monkeypatch.setattr(store._s3, "get_object", flaky)
    assert store.hash("some/key", len(data)) == (sha(data), len(data))
    assert len(reads) == 2

    reads.clear()  # fetch writes the bytes it hashes: the cut-off first read is not left in the sink
    sink = io.BytesIO()
    assert store.fetch("some/key", sink, len(data)) == (sha(data), len(data))
    assert sink.getvalue() == data and len(reads) == 2
    larger = store.fetch("some/key", sink, 100)  # over the limit: the size says so, the sink keeps no more
    assert larger is not None and larger[1] > 100 and len(sink.getvalue()) <= 100
    assert store.fetch("no/such/key", io.BytesIO(), 100) is None


# Cleanup


def test_uploads_left_for_a_day_are_removed_with_their_rows(hub, tmp_path):
    alpha = live.add_project(hub.db, "alpha")
    writer = member(hub.db, alpha, "writer-1", "writer")
    kept = b"committed"
    upload(hub, writer, "alpha", kept)
    old, fresh = ticket(hub, writer, "alpha", b"old"), ticket(hub, writer, "alpha", b"fresh")
    put_presigned(old["url"], b"old")
    put_presigned(fresh["url"], b"fresh")
    live.sql(
        hub.db,
        "UPDATE blob_uploads SET created_at = now() - interval '25 hours' WHERE upload_id = %s",
        (old["upload_id"],),
    )
    config = make_config(hub.db, tmp_path, hub.s3)

    async def clean(now):
        pool = await open_pool(config)
        try:
            return await remove_stale_uploads(hub.store, pool, now)
        finally:
            await pool.close()

    # Now: the row a day old goes; every object is younger than a day and stays.
    assert asyncio.run(clean(datetime.now(UTC))) == {"objects": 0, "rows": 1}
    assert live.sql(hub.db, "SELECT upload_id::text FROM blob_uploads") == [(fresh["upload_id"],)]
    assert len(hub.s3.keys("uploads/")) == 2
    # A day later: everything under uploads/ goes, and the blob stays.
    assert asyncio.run(clean(datetime.now(UTC) + timedelta(hours=25))) == {"objects": 2, "rows": 1}
    assert hub.s3.keys("uploads/") == [] and pending(hub.db) == 0
    assert hub.s3.keys() == [blob_key(sha(kept))]


# Credentials and configuration


def test_the_blob_store_takes_all_four_variables_or_none(s3):
    dsn = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(dsn).blob_store_missing() == list(S3_VARIABLES)
    config = load_config({**dsn, **s3.env()})
    assert config.blob_store_missing() == [] and config.s3_bucket == s3.bucket
    assert s3.secret_access_key not in repr(config) and s3.access_key_id not in repr(config)
    store = BlobStore.from_config(config)
    assert s3.secret_access_key not in repr(store) and s3.access_key_id not in repr(store)
    for variable in S3_VARIABLES:
        partial = {**dsn, **s3.env()}
        del partial[variable]
        with pytest.raises(ConfigError, match=f"{variable} is not set") as caught:
            load_config(partial)
        assert caught.value.variable == variable
    for variable, value in (("EVO_HUB_S3_BUCKET", "Not_A_Bucket"), ("EVO_HUB_S3_ENDPOINT", "r2.example.org")):
        with pytest.raises(ConfigError) as caught:
            load_config({**dsn, **s3.env(), variable: value})
        assert caught.value.variable == variable


def test_the_log_filter_masks_presigned_urls_and_botocore_debug_lines(s3):
    config = load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub", **s3.env()})  # registers the key pair
    store = BlobStore.from_config(config)
    url = store.presign_put(new_upload_id(), 3)
    signature = parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]
    masked = scrub(url)
    assert signature not in masked and s3.access_key_id not in masked and "X-Amz-Signature=***" in masked
    header = f"AWS4-HMAC-SHA256 Credential={s3.access_key_id}/20261004/auto/s3/aws4_request, Signature={'ab' * 32}"
    assert scrub(header) == "AWS4-HMAC-SHA256 Credential=***/20261004/auto/s3/aws4_request, Signature=***"

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    botocore = logging.getLogger("botocore")
    level = botocore.level
    botocore.addHandler(handler)
    botocore.setLevel(logging.DEBUG)  # the hub keeps botocore at WARNING; at DEBUG it logs every request header
    try:
        store.size("missing/key")
        s3.put("some/key", b"x")
        store.hash("some/key", 1)
    finally:
        botocore.removeHandler(handler)
        botocore.setLevel(level)
    text = stream.getvalue()
    assert "Authorization" in text  # botocore did log the signed headers
    pg.log_lines(text)
    for secret in (s3.secret_access_key, s3.access_key_id):
        assert secret not in text
    assert not re.search(r"Signature[=:]\s*[0-9a-f]{64}", text)  # header signatures, botocore.auth's included


def http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_hub_serve_logs_neither_the_s3_key_pair_nor_a_presigned_url(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_LOG_LEVEL="DEBUG", **s3.env()) as server:
        alpha = live.add_project(hub_db, "alpha")
        token = live.insert_token(hub_db, "writer-1")
        member(hub_db, alpha, "writer-1", "writer")
        data = b"logged nowhere"
        status, asked = http(
            "POST", f"{server.url}/v1/blobs/uploads", token, {"project": "alpha", "items": [item(data)]}
        )
        assert status == 200, asked
        (found,) = asked["uploads"]
        put_presigned(found["url"], data)
        status, done = http(
            "POST", f"{server.url}/v1/blobs/commit", token, {"project": "alpha", "upload_ids": [found["upload_id"]]}
        )
        assert status == 200 and done["added"] == 1
        assert http("GET", f"{server.url}/v1/health")[1]["r2"] == "ok"
        s3.stop()
        status, health = http("GET", f"{server.url}/v1/health")
        assert status == 503 and health["failed"] == ["r2"]
    log = server.log()
    signature = parse_qs(urlsplit(found["url"]).query)["X-Amz-Signature"][0]
    for secret in (s3.secret_access_key, s3.access_key_id, signature, found["url"], token, hub_db.password):
        assert secret not in log
    messages = [line["msg"] for line in pg.log_lines(log)]
    assert "blob uploads issued" in messages and "blobs committed" in messages
    assert "health check failed: blob store unavailable" in messages


# Schema


@pytest.mark.parametrize(
    "values, error",
    [
        ("{project}, repeat('A', 64), 1, 'kg-blob', NULL", errors.CheckViolation),
        ("{project}, repeat('a', 64), -1, 'kg-blob', NULL", errors.CheckViolation),
        ("{project}, repeat('a', 64), 1, 'Kg Blob', NULL", errors.CheckViolation),
        ("{project}, repeat('b', 64), 1, 'kg-blob', NULL", errors.UniqueViolation),
        ("{project} + 1000, repeat('a', 64), 1, 'kg-blob', NULL", errors.ForeignKeyViolation),
    ],
)
def test_the_blobs_table_refuses_bad_rows(hub_db, values, error):
    migrate(hub_db.dsn)
    project = live.add_project(hub_db, "alpha")
    with pg.admin(hub_db.admin_dsn) as conn:
        conn.execute(
            "INSERT INTO blobs (project_id, sha256, size, kind) VALUES (%s, repeat('b', 64), 0, 'kg-blob')", (project,)
        )
        with pytest.raises(error):
            conn.execute(
                "INSERT INTO blobs (project_id, sha256, size, kind, created_by) VALUES ("
                + values.format(project=project)
                + ")"
            )
        with pytest.raises(errors.ForeignKeyViolation):  # a project holding blobs is not deleted by accident
            conn.execute("DELETE FROM projects WHERE id = %s", (project,))

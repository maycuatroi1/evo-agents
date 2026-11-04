"""Archives sent as sources of a project's graph (plan kg-upload-archive, step 1): ``hub kg upload`` sends the bytes
straight to the blob store by S3 multipart upload, one presigned URL per part, never through the API; a run cut off
sends only the parts the store lacks; the size limit and the label are checked before any part; ``hub kg uploads``
lists what was sent; the cleanup fails an upload left unfinished and drops its parts.

Each test gets a Postgres database and a bucket of its own (moto, in this process, its part minimum lowered so parts
stay small); moto checks no signature (see ``tests.hub.s3``)."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import moto.s3.models
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from evo_agents.hub import blobs, client, kg_upload_cli, kg_upload_prune, tables
from evo_agents.hub.client import HubError
from evo_agents.hub.config import DEFAULT_KG_UPLOAD_MAX_BYTES, ConfigError, load_config
from evo_agents.hub.db import make_engine, open_pool
from evo_agents.hub.server import kg_uploads
from evo_agents.hub.server.app import create_app
from tests.hub.test_blobs import make_config, member

PROJECT = "northwind"
PART = 1024  # the part size in these tests
WRITER, READER = "lumen-writer", "lumen-reader"


class AppHub:
    """``Hub.call`` answered by the app in this process, as the CLI calls it; the parts still go over HTTP to the
    fake S3, through their presigned URLs."""

    def __init__(self, test_client: TestClient, headers: dict):
        self.client, self.headers, self.calls = test_client, headers, []

    def call(self, method: str, path: str, body=None, *, timeout=None):
        self.calls.append((method, path.split("?")[0]))
        response = self.client.request(method, path, json=body, headers=self.headers)
        if not 200 <= response.status_code < 300:
            raise HubError(response.json().get("message") or response.text, response.status_code)
        return response.json()


@pytest.fixture
def world(hub_db, tmp_path, s3, monkeypatch):
    monkeypatch.setattr(moto.s3.models, "S3_UPLOAD_PART_MIN_SIZE", 256)
    monkeypatch.setattr(blobs, "PART_SIZE", PART)
    config = make_config(hub_db, tmp_path, s3)
    config = type(config)(**{**config.__dict__, "kg_upload_max_bytes": 64 * PART})
    project_id = live.add_project(hub_db, PROJECT)
    live.sql(
        hub_db,
        update(tables.projects).where(tables.projects.c.id == project_id).values(default_label={"level": "internal"}),
    )
    live.sql(
        hub_db,
        insert(tables.project_sinks).values(
            project_id=project_id, sink_id="hub", kind="hub", clearance={"level": "customer"}
        ),
    )
    writer = member(hub_db, project_id, WRITER, "writer")
    g, users = tables.grants, tables.users
    writer_id = select(users.c.id).where(users.c.login == WRITER).scalar_subquery()
    live.sql(hub_db, update(g).where(g.c.user_id == writer_id).values(max_level="secret"))
    app = create_app(config)
    with TestClient(app) as test_client:
        yield SimpleNamespace(
            client=test_client,
            app=app,
            db=hub_db,
            s3=s3,
            store=app.state.blobs,
            project_id=project_id,
            writer=writer,
            reader=member(hub_db, project_id, READER, "reader"),
            tmp=tmp_path,
            config=config,
        )


def archive(world, size: int, name: str = "handbook.zip") -> Path:
    path = world.tmp / name
    path.write_bytes(bytes((i * 7) % 251 for i in range(size)))
    return path


def rows(world) -> list:
    u = tables.kg_uploads
    return live.sql(world.db, select(u.c.source, u.c.state, u.c.size, u.c.label).order_by(u.c.id))


def puts(monkeypatch) -> list:
    """The parts the CLI PUTs, by number, recorded on the way to the store."""
    sent = []
    real = client.put_presigned_part

    def counting(url, path, offset, size, timeout=client.TIMEOUT):
        sent.append(offset // PART + 1)
        return real(url, path, offset, size, timeout)

    monkeypatch.setattr(kg_upload_cli, "put_presigned_part", counting)
    return sent


def test_upload_sends_every_part_to_the_store_and_the_hub_joins_them(world, monkeypatch):
    path = archive(world, 3 * PART + 100)
    sent = puts(monkeypatch)
    hub = AppHub(world.client, world.writer)
    done, count, kept = kg_upload_cli.upload(hub, PROJECT, path)
    assert (count, kept, sorted(sent)) == (4, 0, [1, 2, 3, 4])
    assert done["state"] == "extracting" and done["source"] == "handbook" and done["size"] == path.stat().st_size
    assert done["label"]["level"] == "internal", "the project's default label without --label"
    ((key,),) = live.sql(world.db, select(tables.kg_uploads.c.object_key))
    assert key.startswith(blobs.ARCHIVE_PREFIX)
    assert world.s3.get(key) == path.read_bytes(), "the parts joined are the archive"
    assert not any(path_ == "/v1/blobs/uploads" for _, path_ in hub.calls), "no byte goes through the API"
    audit = live.sql(world.db, select(tables.audit.c.target).where(tables.audit.c.action == "kg.upload"))
    assert audit == [(f"{PROJECT} source=handbook upload={done['id']} bytes={path.stat().st_size}",)]


def test_a_run_cut_off_sends_only_the_parts_the_store_lacks(world, monkeypatch):
    path = archive(world, 5 * PART)
    hub = AppHub(world.client, world.writer)
    real = client.put_presigned_part
    sent = []

    def flaky(url, path_, offset, size, timeout=client.TIMEOUT):
        number = offset // PART + 1
        if number >= 3:
            raise HubError("the connection dropped", 400)
        sent.append(number)
        return real(url, path_, offset, size, timeout)

    monkeypatch.setattr(kg_upload_cli, "put_presigned_part", flaky)
    with pytest.raises(HubError):
        kg_upload_cli.upload(hub, PROJECT, path)
    assert rows(world)[0][1] == "uploading"
    again = puts(monkeypatch)
    done, count, kept = kg_upload_cli.upload(hub, PROJECT, path)
    assert (count, kept) == (3, 2) and sorted(again) == [3, 4, 5]
    assert done["state"] == "extracting" and len(rows(world)) == 1, "the second run resumed the first upload"
    ((key,),) = live.sql(world.db, select(tables.kg_uploads.c.object_key))
    assert world.s3.get(key) == path.read_bytes()


def test_complete_refuses_while_parts_are_missing(world):
    path = archive(world, 2 * PART)
    body = {"name": path.name, "size": 2 * PART, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    started = world.client.post(f"/v1/kg/{PROJECT}/uploads", json=body, headers=world.writer).json()
    assert (started["parts"], started["done"], started["part_size"]) == (2, [], PART)
    answer = world.client.post(f"/v1/kg/{PROJECT}/uploads/{started['id']}/complete", headers=world.writer)
    assert answer.status_code == 409 and "lacks 2 of its 2 parts" in answer.text
    other = world.client.post(
        f"/v1/kg/{PROJECT}/uploads/{started['id']}/parts", json={"numbers": [1]}, headers=world.reader
    )
    assert other.status_code in (403, 404), "only the sender continues an upload"
    wrong = world.client.post(
        f"/v1/kg/{PROJECT}/uploads/{started['id']}/parts", json={"numbers": [3]}, headers=world.writer
    )
    assert wrong.status_code == 422


@pytest.mark.parametrize(
    ("label", "status"),
    [("secret", 422), ("nonsense", 422)],
)
def test_a_label_the_hub_sink_does_not_clear_is_refused_before_any_part(world, monkeypatch, label, status):
    path = archive(world, 2 * PART)
    sent = puts(monkeypatch)
    hub = AppHub(world.client, world.writer)
    with pytest.raises(HubError) as refused:
        kg_upload_cli.upload(hub, PROJECT, path, label=label)
    assert refused.value.status == status
    assert sent == [] and rows(world) == [] and world.store.list_multipart() == []


def test_the_reader_role_and_the_size_limit_are_checked_before_any_part(world, monkeypatch):
    sent = puts(monkeypatch)
    with pytest.raises(HubError) as refused:
        kg_upload_cli.upload(AppHub(world.client, world.reader), PROJECT, archive(world, PART))
    assert refused.value.status == 403
    with pytest.raises(HubError) as large:
        kg_upload_cli.upload(AppHub(world.client, world.writer), PROJECT, archive(world, 64 * PART + 1))
    assert large.value.status == 413 and "EVO_HUB_KG_UPLOAD_MAX_BYTES" in str(large.value)
    body = {"name": "notes.rar", "size": 10, "sha256": "a" * 64}
    assert world.client.post(f"/v1/kg/{PROJECT}/uploads", json=body, headers=world.writer).status_code == 422
    assert sent == [] and rows(world) == [] and world.store.list_multipart() == []


def test_uploads_lists_state_size_files_skipped_and_build_by_grant(world, monkeypatch, capsys):
    hub = AppHub(world.client, world.writer)
    puts(monkeypatch)
    public, _, _ = kg_upload_cli.upload(hub, PROJECT, archive(world, PART, "faq.tgz"), source="faq", label="public")
    kg_upload_cli.upload(hub, PROJECT, archive(world, PART + 1, "deals.tar.gz"))  # internal, the default
    u = tables.kg_uploads
    report = {"files": 12, "skipped": {"binary": 2, "too_large": 1}}
    live.sql(world.db, update(u).where(u.c.id == public["id"]).values(state="extracted", report=report))
    listed = world.client.get(f"/v1/kg/{PROJECT}/uploads", headers=world.writer).json()["uploads"]
    assert [(i["source"], i["state"]) for i in listed] == [("deals", "extracting"), ("faq", "extracted")]
    faq = listed[1]
    assert (faq["files"], faq["skipped"], faq["build_id"], faq["sender"]) == (12, report["skipped"], None, WRITER)
    seen = world.client.get(f"/v1/kg/{PROJECT}/uploads", headers=world.reader).json()["uploads"]
    assert [i["source"] for i in seen] == ["faq"], "a grant up to public does not see an internal upload"

    monkeypatch.setattr(kg_upload_cli, "signed_in", lambda: (hub, None))
    assert kg_upload_cli.cmd_uploads(argparse.Namespace(project=PROJECT, limit=50, json=False)) == 0
    out = capsys.readouterr().out
    assert "faq extracted 1.0 KiB files 12 skipped binary 2, too_large 1 build -" in out
    assert kg_upload_cli.cmd_uploads(argparse.Namespace(project=PROJECT, limit=50, json=True)) == 0
    assert [i["source"] for i in json.loads(capsys.readouterr().out)["uploads"]] == ["deals", "faq"]


def test_cleanup_fails_an_upload_left_unfinished_and_drops_its_parts(world):
    path = archive(world, 2 * PART)
    body = {"name": path.name, "size": 2 * PART, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    started = world.client.post(f"/v1/kg/{PROJECT}/uploads", json=body, headers=world.writer).json()
    assert len(world.store.list_multipart()) == 1
    now = datetime.now(UTC)

    async def sweep(when):
        pool = await open_pool(world.config)
        try:
            return await kg_upload_prune.remove_stale(world.store, make_engine(pool), when)
        finally:
            await pool.close()

    assert asyncio.run(sweep(now)) == {"kg_uploads_failed": 0, "kg_multipart_aborted": 0}, "nothing is stale yet"
    # A multipart upload no row names, as a rollback leaves; moto dates every one 2010, so it is made only now.
    orphan = world.store.create_multipart(blobs.archive_key(blobs.new_upload_id()))
    found = asyncio.run(sweep(now + blobs.STALE_AFTER + timedelta(minutes=1)))
    assert found == {"kg_uploads_failed": 1, "kg_multipart_aborted": 2}
    assert world.store.list_multipart() == [] and orphan
    assert rows(world)[0][1] == "failed"
    gone = world.client.post(
        f"/v1/kg/{PROJECT}/uploads/{started['id']}/parts", json={"numbers": [1]}, headers=world.writer
    )
    assert gone.status_code == 409


def test_default_source_and_part_sizes():
    assert kg_uploads.default_source("Lumen Ops docs.tar.gz") == "Lumen-Ops-docs"
    assert kg_uploads.default_source(".zip") == "archive"
    assert kg_uploads.part_sizes(2 * PART + 1, PART) == {1: PART, 2: PART, 3: 1}
    assert kg_uploads.part_size(blobs.MAX_PARTS * blobs.PART_SIZE + 1) > blobs.PART_SIZE


def test_the_size_limit_is_a_config_variable_of_20_gib_by_default():
    base = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}  # no store: the limit is read all the same
    assert DEFAULT_KG_UPLOAD_MAX_BYTES == 20 * 1024**3
    assert load_config(base).kg_upload_max_bytes == DEFAULT_KG_UPLOAD_MAX_BYTES
    assert load_config({**base, "EVO_HUB_KG_UPLOAD_MAX_BYTES": "1000"}).kg_upload_max_bytes == 1000
    with pytest.raises(ConfigError):
        load_config({**base, "EVO_HUB_KG_UPLOAD_MAX_BYTES": str(601 * 1024**3)})

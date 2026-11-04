"""The worker's reading of an uploaded archive into its source's items (plan kg-upload-archive, step 2): the job
``hub.extract_uploads`` checks the archive's SHA-256, walks it without unpacking it, stores the items next to it,
releases the uploads of the same source it replaces and queues a build, after which kg_search finds what only the PDF
and only the docx say. Its log holds upload ids, counts and reasons; the paths of its report reach only a grant that
sees the upload's label.

The api and the worker run in this process and the blob store is moto's, as in ``tests.hub.test_kg``; the archive is
the fixture of ``scripts/kg_upload_fixture.py``."""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import io
import json
import logging
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import jobs, kg_upload_cli, kg_upload_extract, tables
from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, items_key
from evo_agents.hub.worker import HubContext, connector, queue
from tests.hub.test_kg import (
    PROJECT,
    InProcessHub,
    Machine,
    doc,
    latest,
    make_config,
    open_hub,
    push,
    query,
    run_jobs,
    tool_ok,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "northwind-handbook"
GRAPH_SOURCE = "upload.northwind-handbook"


def fixture_module():
    spec = importlib.util.spec_from_file_location("kg_upload_fixture", ROOT / "scripts" / "kg_upload_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = fixture_module()


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        yield found
    finally:
        client.__exit__(None, None, None)


def configure(hub, tmp_path: Path) -> Machine:
    """Push a run of project alpha so the hub holds its knowledge config, and build it."""
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"
    return laptop


def upload(hub, path: Path, *, source: str = SOURCE, member: str = "alice", label: str | None = None) -> dict:
    done, _, _ = kg_upload_cli.upload(
        InProcessHub(hub.client, getattr(hub, member)), PROJECT, path, source=source, label=label
    )
    assert done["state"] == "extracting", done
    return done


def extract(hub) -> dict:
    """One pass of the job ``hub.extract_uploads``, as the worker runs it."""
    config = make_config(hub.db, hub.worker_dir, hub.s3)

    async def main():
        store = BlobStore.from_config(config)
        try:
            with queue.replace_connector(connector(config, 1)):
                await queue.open_async()
                try:
                    context = HubContext(config, queue.connector.pool, store, config.data_dir)
                    return await kg_upload_extract.extract_pending(context, queue.job_manager)
                finally:
                    await queue.close_async()
        finally:
            store.close()

    return asyncio.run(main())


def upload_row(hub, upload_id: int):
    u = tables.kg_uploads
    names = ("state", "report", "error", "build_id", "object_key", "ended_at")
    (row,) = query(hub.db, select(*(u.c[name] for name in names)).where(u.c.id == upload_id))
    return SimpleNamespace(**dict(zip(names, row, strict=True)))


def zip_of(files: dict[str, bytes], path: Path) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as packed:
        for name, data in files.items():
            packed.writestr(name, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return path


def found_ids(hub, text: str, member: str = "alice") -> list[str]:
    results = tool_ok(hub, "kg_search", {"query": text}, member=member)["structuredContent"]["results"]
    return [str(node.get("id")) for node in results]


def stored_items(hub, object_key: str) -> list[dict]:
    data = hub.s3.get(items_key(object_key))
    assert data is not None
    return [json.loads(line) for line in gzip.decompress(data).decode().splitlines()]


def test_an_upload_is_read_into_items_built_and_kg_search_finds_the_pdf_and_docx_sentences(hub, tmp_path):
    pytest.importorskip("markitdown")
    configure(hub, tmp_path)
    archive = FIXTURE.write(tmp_path / "fixture", seed=7)
    done = upload(hub, archive)

    assert extract(hub) == {"extracted": 1}
    row = upload_row(hub, done["id"])
    assert row.state == "extracted" and row.error is None and row.ended_at is not None
    expected = FIXTURE.files(7)
    assert row.report["files"] == len(expected)
    assert row.report["skipped"] == {}
    assert row.report["assets"] == {"binary": 1, "no_text_layer": 1}
    assert {entry["path"] for entry in row.report["paths"]} == {"images/logo.png", "docs/scan.pdf"}
    assert row.build_id is not None, "a build follows the extraction"
    items = stored_items(hub, row.object_key)
    assert row.report["items"]["count"] == len(items)
    ids = {item["id"] for item in items}
    assert f"{GRAPH_SOURCE}:file:docs/ferry.pdf" in ids
    assert f"{GRAPH_SOURCE}:file:{FIXTURE.VIETNAMESE_NAME}" in ids
    assert f"{GRAPH_SOURCE}:file:{FIXTURE.VIETNAMESE_FLAGLESS}" in ids
    assert f"{GRAPH_SOURCE}:file:archive/tides.zip/tides/summary.md" in ids
    assert any(item["kind"] == "table" for item in items) and any(item["kind"] == "record" for item in items)

    run_jobs(hub.db, hub.s3, hub.worker_dir)
    build = latest(hub)
    assert build["status"] == "succeeded" and build["id"] == row.build_id
    assert any("docs/ferry.pdf" in node for node in found_ids(hub, FIXTURE.PDF_SENTENCE))
    assert any("docs/lanterns.docx" in node for node in found_ids(hub, FIXTURE.DOCX_SENTENCE))
    assert extract(hub) == {}, "an upload is read once"


def test_an_archive_that_is_not_the_one_sent_or_is_gone_fails_its_upload(hub, tmp_path):
    first = upload(hub, zip_of({"a.md": b"# A\n"}, tmp_path / "a" / "one.zip"), source="one")
    second = upload(hub, zip_of({"b.md": b"# B\n"}, tmp_path / "b" / "two.zip"), source="two")
    key = upload_row(hub, first["id"]).object_key
    hub.s3.put(key, b"PK" + b"\0" * 40)
    hub.s3.client().delete_object(Bucket=hub.s3.bucket, Key=upload_row(hub, second["id"]).object_key)

    assert extract(hub) == {"failed": 2}
    one, two = upload_row(hub, first["id"]), upload_row(hub, second["id"])
    assert (one.state, one.error) == ("failed", kg_upload_extract.NOT_SENT)
    assert (two.state, two.error) == ("failed", kg_upload_extract.NOT_STORED)
    assert hub.s3.get(items_key(key)) is None


def test_the_blob_store_not_answering_leaves_the_upload_for_the_next_pass(hub, tmp_path, monkeypatch):
    done = upload(hub, zip_of({"a.md": b"# A\n"}, tmp_path / "a.zip"))

    def down(*args, **kwargs):
        raise BlobStoreUnavailable("the store did not answer")

    monkeypatch.setattr(BlobStore, "fetch", down)
    assert extract(hub) == {"waiting": 1}
    assert upload_row(hub, done["id"]).state == "extracting"
    monkeypatch.undo()
    assert extract(hub) == {"extracted": 1}
    assert upload_row(hub, done["id"]).build_id is None, "no knowledge config on the hub: no build"


def test_a_reupload_is_a_new_snapshot_and_releases_the_old_archive(hub, tmp_path):
    configure(hub, tmp_path)
    old = upload(
        hub,
        zip_of(
            {"kept.md": b"# Kept\n\nThe beacon at Saltmere is lit at dusk.\n", "gone.md": b"# Gone\n\nOrion quay.\n"},
            tmp_path / "v1" / "handbook.zip",
        ),
    )
    assert extract(hub) == {"extracted": 1}
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert found_ids(hub, "Orion quay")
    old_key = upload_row(hub, old["id"]).object_key

    new = upload(
        hub,
        zip_of({"kept.md": b"# Kept\n\nThe beacon at Saltmere is lit at dawn.\n"}, tmp_path / "v2" / "handbook.zip"),
    )
    assert extract(hub) == {"extracted": 1}
    released = upload_row(hub, old["id"])
    assert released.state == "extracted" and released.report["replaced_by"] == new["id"]
    assert "items" not in released.report
    assert hub.s3.get(old_key) is None and hub.s3.get(items_key(old_key)) is None, "the old archive is released"
    assert hub.s3.get(upload_row(hub, new["id"]).object_key) is not None

    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"
    assert not any(GRAPH_SOURCE in node for node in found_ids(hub, "Orion quay")), "a file the new archive lacks goes"
    assert any("kept.md" in node for node in found_ids(hub, "beacon dawn"))
    listed = hub.client.get(f"/v1/kg/{PROJECT}/uploads", headers=hub.alice).json()["uploads"]
    assert {item["id"]: item["replaced_by"] for item in listed} == {new["id"]: None, old["id"]: new["id"]}


def test_the_log_of_an_extraction_holds_only_ids_counts_and_reasons(hub, tmp_path, caplog):
    archive = FIXTURE.write(tmp_path / "fixture", seed=7)
    done = upload(hub, archive)
    with caplog.at_level(logging.DEBUG, logger="evo_agents.hub.kg_upload_extract"):
        assert extract(hub) == {"extracted": 1}
    records = [r for r in caplog.records if r.name == "evo_agents.hub.kg_upload_extract"]
    assert records
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    secrets = [*FIXTURE.files(7), "ferry", "lanterns", FIXTURE.PDF_SENTENCE, FIXTURE.DOCX_SENTENCE, SOURCE]
    for record in records:
        extra = {key: value for key, value in record.__dict__.items() if key not in standard}
        text = record.getMessage() + json.dumps(extra, default=str, ensure_ascii=False)
        assert not [secret for secret in secrets if secret in text], text
        assert extra["upload_id"] == done["id"]
        assert set(extra) <= {"upload_id", "files", "items", "skipped", "assets", "released", "error", "reason"}


def test_privacy_the_report_paths_reach_only_a_grant_that_sees_the_upload_label(hub, tmp_path):
    archive = FIXTURE.write(tmp_path / "fixture", seed=7)
    done = upload(hub, archive)
    assert extract(hub) == {"extracted": 1}

    def listed(member: str) -> list[dict]:
        response = hub.client.get(f"/v1/kg/{PROJECT}/uploads", headers=getattr(hub, member))
        assert response.status_code == 200, response.text
        return response.json()["uploads"]

    (seen,) = listed("dave")
    assert seen["id"] == done["id"] and {entry["path"] for entry in seen["paths"]} >= {"images/logo.png"}
    assert seen["assets"] == {"binary": 1, "no_text_layer": 1}
    assert listed("carol") == [], "a grant below the label sees neither the upload nor its paths"

    response = hub.client.get(f"/v1/kg/{PROJECT}/uploads", headers=hub.carol)
    assert response.status_code == 200 and "logo.png" not in response.text and "scan.pdf" not in response.text


def test_the_extraction_runs_every_minute_one_pass_at_a_time():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.EXTRACT_UPLOADS].cron == "* * * * *"
    task = queue.tasks[jobs.EXTRACT_UPLOADS]
    assert task.lock == jobs.EXTRACT_UPLOADS and task.queueing_lock == jobs.EXTRACT_UPLOADS

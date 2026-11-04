"""The worker's reading of each uploaded archive (``hub kg upload``) into the items of its source: the job
``hub.extract_uploads``, every minute, one pass at a time.

An upload the hub took in full waits in state ``extracting`` (``evo_agents.hub.server.kg_uploads``). The pass takes
each, oldest first:

1. it reads the archive from the blob store into a temporary file under the hub's data directory, its SHA-256
   counted on the way; an archive gone, or not the size and SHA-256 sent, fails the upload;
2. it walks the archive entry by entry (``evo_agents.kg.archive``), never unpacking it, and turns each file into
   items (``evo_agents.kg.extract_office``), written as gzipped JSON lines next to the archive (``blobs.items_key``);
3. in one transaction, the upload becomes ``extracted`` with its report, and the uploads of the same source read
   before it are released: their report says which upload replaced them and loses its items, so the source holds the
   new snapshot only, and a file the new archive lacks leaves the graph with the next build. A build is queued when
   the project has a knowledge config (``kg_build.queue_build``);
4. once that commits, the released archives and their items leave the blob store.

The report: ``files`` read, ``skipped`` entries by reason, ``assets`` (files kept by path and size only) by reason,
``items`` (their count and the size of the stored file) and ``paths``, the first ``archive.MAX_NOTED`` entries
skipped or kept as assets with their reason. The paths are shown only to members whose grant sees the upload's label
(``server.kg_uploads``); the log holds upload ids, counts and reasons only, never a path or a file's text.

The blob store not answering leaves an upload in ``extracting`` for the next pass; any other error fails it.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import tarfile
import tempfile
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from procrastinate.manager import JobManager
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable, items_key
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.kg_build import QueueBusy, queue_build
from evo_agents.hub.kg_upload_sources import graph_source
from evo_agents.kg import archive, extract_office

log = logging.getLogger(__name__)

NOT_STORED = "the archive is not in the blob store"
NOT_SENT = "the archive in the blob store is not the one sent: its size or SHA-256 differs"
UNREADABLE = "the archive could not be read"
READ_ERRORS = (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, RuntimeError, zlib.error)


@dataclass(frozen=True)
class Extracted:
    """What reading one archive gave: its report, or why it failed."""

    report: dict | None = None
    error: str | None = None


def read_archive(path: Path, source: str, out: Path, spool: Path) -> dict:
    """Write the items of the archive at ``path`` as source ``source`` gives them into ``out`` (gzipped JSON lines,
    one item each, an id once); the report of the read, without the stored size. Blocking."""
    skipped: Counter = Counter()
    assets: Counter = Counter()
    noted: list[dict] = []
    files = count = 0
    ids: set[str] = set()
    with gzip.open(out, "wt", encoding="utf-8") as sink:
        for entry in archive.walk(path, skipped, spool=spool, noted=noted):
            try:
                found = extract_office.read(source, entry.path, entry.size, entry.mtime, entry.read)
            except READ_ERRORS:
                skipped["corrupt"] += 1
                _note(noted, entry.path, "corrupt")
                continue
            files += 1
            if found.asset:
                assets[found.asset] += 1
                _note(noted, entry.path, found.asset)
            for item in found.items:
                if item["id"] not in ids:
                    ids.add(item["id"])
                    sink.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                    count += 1
    return {
        "files": files,
        "skipped": dict(sorted(skipped.items())),
        "assets": dict(sorted(assets.items())),
        "items": {"count": count},
        "paths": noted,
    }


def _note(noted: list[dict], path: str, reason: str) -> None:
    if len(noted) < archive.MAX_NOTED:
        noted.append({"path": path, "reason": reason})


def extract(store: BlobStore, row, work: Path) -> Extracted:
    """Read upload ``row``'s archive from the blob store, check it, and store its items next to it. Blocking; raises
    BlobStoreUnavailable when the store does not answer."""
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work, prefix="upload-") as tmp:
        folder = Path(tmp)
        path = folder / "archive"
        with open(path, "wb") as sink:
            fetched = store.fetch(row.object_key, sink, row.size)
        if fetched is None:
            return Extracted(error=NOT_STORED)
        sha256, size = fetched
        if size != row.size or sha256 != str(row.sha256).lower():
            return Extracted(error=NOT_SENT)
        out = folder / "items.jsonl.gz"
        report = read_archive(path, graph_source(row.source), out, folder)
        path.unlink()
        store.put_object(items_key(row.object_key), out)
        report["items"]["size"] = out.stat().st_size
        return Extracted(report=report)


async def _fail(engine: AsyncEngine, upload_id: int, error: str) -> None:
    u = tables.kg_uploads
    async with engine.begin() as conn:
        await conn.execute(
            update(u)
            .values(state="failed", error=error, ended_at=func.now(), updated_at=func.now())
            .where(u.c.id == upload_id, u.c.state == "extracting")
        )


async def _queue(manager: JobManager, conn: AsyncConnection, project_id: int, upload_id: int) -> int | None:
    """The build queued for the project, when it has a knowledge config on the hub."""
    configs, projects = tables.kg_configs, tables.projects
    found = await conn.execute(
        select(projects.c.name)
        .join_from(configs, projects, projects.c.id == configs.c.project_id)
        .where(configs.c.project_id == project_id)
    )
    name = found.scalar_one_or_none()
    if name is None:
        return None
    try:
        build_id, _ = await queue_build(JobQueue(manager), conn, project_id, name, None)
    except QueueBusy:
        log.warning("no build queued after an upload was read; the next push builds", extra={"upload_id": upload_id})
        return None
    return build_id


def _released(report: dict | None, object_key: str, by: int) -> tuple[dict, list[str]]:
    """A released upload's report, without its items, naming the upload that replaced it; its keys to delete."""
    kept = {key: value for key, value in (report or {}).items() if key != "items"}
    return {**kept, "replaced_by": by}, [object_key, items_key(object_key)]


async def finish(engine: AsyncEngine, manager: JobManager, upload_id: int, object_key: str, report: dict) -> list[str]:
    """Record upload ``upload_id`` as read with ``report``, release the uploads of its source it replaces, and queue a
    build, in one transaction; the keys to delete from the blob store once it commits."""
    u = tables.kg_uploads
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(u.c.id, u.c.project_id, u.c.source, u.c.state).where(u.c.id == upload_id).with_for_update()
            )
        ).one_or_none()
        if row is None:  # forgotten meanwhile: its items have no row left
            return [items_key(object_key)]
        if row.state != "extracting":
            return []
        source = graph_source(row.source)
        others = await conn.execute(
            select(u.c.id, u.c.source, u.c.object_key, u.c.report)
            .where(u.c.project_id == row.project_id, u.c.state == "extracted", u.c.id != upload_id)
            .order_by(u.c.id)
            .with_for_update()
        )
        same = [other for other in others if graph_source(other.source) == source and "items" in (other.report or {})]
        newer = [other.id for other in same if other.id > upload_id]
        keys: list[str] = []
        if newer:  # a later upload of the source was read first: this one is released at once
            report, keys = _released(report, object_key, max(newer))
        for other in same:
            if other.id < upload_id:
                released, gone = _released(other.report, other.object_key, max([upload_id, *newer]))
                await conn.execute(update(u).values(report=released, updated_at=func.now()).where(u.c.id == other.id))
                keys += gone
        build_id = None if newer else await _queue(manager, conn, row.project_id, upload_id)
        await conn.execute(
            update(u)
            .values(
                state="extracted",
                report=report,
                error=None,
                build_id=build_id,
                ended_at=func.now(),
                updated_at=func.now(),
            )
            .where(u.c.id == upload_id)
        )
    return keys


async def extract_one(context, manager: JobManager, upload_id: int) -> str:
    """Read one upload waiting in ``extracting`` (see the module): ``extracted``, ``failed``, ``waiting`` (the blob
    store did not answer) or ``gone``."""
    engine, store = context.engine, context.blobs
    u = tables.kg_uploads
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(u.c.id, u.c.source, u.c.size, u.c.sha256, u.c.object_key, u.c.state).where(u.c.id == upload_id)
            )
        ).one_or_none()
    if row is None or row.state != "extracting":
        return "gone"
    work = Path(context.data_dir) / "kg" / "uploads"
    try:
        found = await asyncio.to_thread(extract, store, row, work)
    except BlobStoreUnavailable:
        log.warning("upload not read: the blob store did not answer", extra={"upload_id": upload_id})
        return "waiting"
    except Exception as exc:  # whatever the archive did to a reader: the upload fails, the pass goes on
        log.warning("upload not read", extra={"upload_id": upload_id, "error": type(exc).__name__})
        await _fail(engine, upload_id, UNREADABLE)
        return "failed"
    if found.error:
        log.warning("upload not read", extra={"upload_id": upload_id, "reason": "stored archive"})
        await _fail(engine, upload_id, found.error)
        return "failed"
    report = found.report
    keys = await finish(engine, manager, upload_id, row.object_key, report)
    if keys:
        await asyncio.to_thread(store.delete, keys)
    log.info(
        "upload read",
        extra={
            "upload_id": upload_id,
            "files": report["files"],
            "items": report["items"]["count"],
            "skipped": report["skipped"],
            "assets": report["assets"],
            "released": len(keys) // 2,
        },
    )
    return "extracted"


async def extract_pending(context, manager: JobManager) -> dict:
    """``extract_one`` of each upload waiting in ``extracting``, oldest first; how many ended each way."""
    u = tables.kg_uploads
    async with context.engine.connect() as conn:
        ids = list((await conn.execute(select(u.c.id).where(u.c.state == "extracting").order_by(u.c.id))).scalars())
    outcomes: Counter = Counter()
    for upload_id in ids:
        outcomes[await extract_one(context, manager, upload_id)] += 1
    return dict(outcomes)


__all__ = ["Extracted", "extract", "extract_one", "extract_pending", "finish", "read_archive"]

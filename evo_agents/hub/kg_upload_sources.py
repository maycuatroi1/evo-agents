"""The sources of a project's graph that uploaded archives are (``hub kg upload``), which the hub holds as it holds a
core plugin's source (``evo_agents.hub.kg_sources``).

An upload's ``source`` names a source of the graph, ``upload.<source>`` in lowercase (``graph_source``); two uploads
whose names differ only in case are one source. Once the worker read an upload's archive
(``evo_agents.hub.kg_upload_extract``), its items wait in the blob store next to it (``blobs.items_key``), and the
newest upload of each source read so far is what the source holds:

- a build (``evo_agents.hub.kg_build``) adds each such source to the config it builds with, in place of one the
  config declares under the same id, as it adds a plugin's: so pushing a config, which never names it, neither drops
  it nor is refused for it. Its items take the upload's label, and its cursor names the upload, its time and its
  label, which kg_status shows; the build's coverage says the same of it (``annotate``);
- ``hub kg forget-source --source upload.<source>`` (or the upload's own source name) deletes its uploads, their
  archives and items, and queues a build without it (``forget``, then ``release``).
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.blobs import BlobStore, items_key
from evo_agents.hub.kg_sources import UPLOAD_PREFIX, PluginSource, SourceFailure, checked
from evo_agents.kg.ids import utc_text

log = logging.getLogger(__name__)

PLUGIN = "upload"  # what PluginSource.plugin names for an upload's source
NOT_SOURCE = re.compile(r"[^a-z0-9_.-]+")


def graph_source(source: str) -> str:
    """The id in a project's graph of upload source ``source``: ``upload.<source>`` in lowercase letters, digits and
    ``._-``."""
    return UPLOAD_PREFIX + (NOT_SOURCE.sub("-", source.lower()).strip("-.") or "archive")


@dataclass(frozen=True)
class Held:
    """The newest upload read of one source: its id, the source's graph id, its label (by names), where its items are,
    their size, and when it was sent."""

    upload_id: int
    source: str
    label: dict
    key: str
    size: int
    uploaded_at: datetime


def _label_text(label: dict) -> str:
    level = str(label.get("level") or "-")
    return f"{level}/{label['location']}" if label.get("location") else level


async def latest(conn: AsyncConnection, project_id: int) -> list[Held]:
    """The newest upload read of each upload source of project ``project_id``, by source."""
    u = tables.kg_uploads
    rows = await conn.execute(
        select(u.c.id, u.c.source, u.c.label, u.c.object_key, u.c.report, u.c.uploaded_at, u.c.created_at)
        .where(u.c.project_id == project_id, u.c.state == "extracted")
        .order_by(u.c.id.desc())
    )
    found: dict[str, Held] = {}
    for row in rows:
        items = (row.report or {}).get("items")
        source = graph_source(row.source)
        if source in found or not isinstance(items, dict) or not isinstance(items.get("size"), int):
            continue
        at = row.uploaded_at or row.created_at
        found[source] = Held(row.id, source, row.label, items_key(row.object_key), items["size"], at)
    return [found[source] for source in sorted(found)]


def _items(path: Path, label: dict):
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        for line in lines:
            if line.strip():
                yield {**json.loads(line), "label": label}


def load(store: BlobStore, held: Held, knowledge: dict, project: str, spool: Path) -> PluginSource:
    """The source ``held`` is, as a build uses it: its items from the blob store, each with the upload's label, and
    its cursor. Raises SourceFailure when its items are gone or not what the extraction wrote. Blocking."""
    with tempfile.NamedTemporaryFile(dir=spool, suffix=".jsonl.gz") as sink:
        fetched = store.fetch(held.key, sink, held.size)
        sink.flush()
        if fetched is None or fetched[1] != held.size:
            raise SourceFailure(f"the items of upload {held.upload_id} are not in the blob store")
        try:
            items = list(_items(Path(sink.name), held.label))
        except (OSError, EOFError, ValueError):
            raise SourceFailure(f"the items of upload {held.upload_id} cannot be read") from None
    found = checked(held.source, PLUGIN, {"items": items}, knowledge, project)
    cursor = {
        "item": f"{held.source}:upload:{held.upload_id}",
        "at": utc_text(held.uploaded_at),
        "label": _label_text(held.label),
    }
    if found.left_out:
        log.warning("upload items left out", extra={"upload_id": held.upload_id, "items": found.left_out})
    return dataclasses.replace(found, cursor=cursor)


def load_all(store: BlobStore, data_dir: Path, held: list[Held], knowledge: dict, project: str) -> list[PluginSource]:
    """``load`` of each of ``held``, spooled under ``data_dir``. Blocking."""
    if not held:
        return []
    spool = Path(data_dir) / "kg" / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    return [load(store, each, knowledge, project, spool) for each in held]


def annotate(coverage: dict, held: list[Held]) -> None:
    """Put into the entry of each upload source of a build's coverage (``kg_coverage.build_coverage``) its upload:
    id, label and time."""
    by_source = {each.source: each for each in held}
    for entry in coverage.get("sources") or []:
        found = by_source.get(entry.get("id"))
        if found is not None:
            entry["upload"] = {
                "id": found.upload_id,
                "label": _label_text(found.label),
                "at": utc_text(found.uploaded_at),
            }


@dataclass(frozen=True)
class Forgotten:
    """The uploads ``forget`` deleted, the objects to delete and the multipart uploads to abort after it commits."""

    uploads: int
    keys: tuple[str, ...]
    multiparts: tuple[tuple[str, str], ...]


async def forget(conn: AsyncConnection, project_id: int, source: str) -> Forgotten:
    """Delete every upload of project ``project_id`` whose source is ``source``, by its graph id or by the name
    uploads give it, in ``conn``'s transaction; what to take out of the blob store once it commits (``release``)."""
    u = tables.kg_uploads
    rows = (
        await conn.execute(
            select(u.c.id, u.c.source, u.c.object_key, u.c.multipart_id)
            .where(u.c.project_id == project_id)
            .with_for_update()
        )
    ).all()
    gone = [row for row in rows if source in (row.source, graph_source(row.source))]
    if not gone:
        return Forgotten(0, (), ())
    await conn.execute(delete(u).where(u.c.id.in_([row.id for row in gone])))
    keys = tuple(key for row in gone for key in (row.object_key, items_key(row.object_key)))
    multiparts = tuple((row.object_key, row.multipart_id) for row in gone if row.multipart_id)
    return Forgotten(len(gone), keys, multiparts)


def release(store: BlobStore, forgotten: Forgotten) -> None:
    """Take what ``forget`` deleted out of the blob store. Blocking."""
    for key, multipart_id in forgotten.multiparts:
        store.abort_multipart(key, multipart_id)
    if forgotten.keys:
        store.delete(forgotten.keys)


__all__ = [
    "PLUGIN",
    "Forgotten",
    "Held",
    "annotate",
    "forget",
    "graph_source",
    "latest",
    "load",
    "load_all",
    "release",
]

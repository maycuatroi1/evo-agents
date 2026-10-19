"""Deleting the content of session transcripts, for an admin of the project (``evo_agents.hub.server.transcripts``)
and for the retention (the job hub.prune_transcripts of ``evo_agents.hub.worker``).

``drop`` deletes the parts of the transcripts a condition selects and marks their rows deleted, keeping the rows, so a
backfill that asks the hub learns the transcript is gone and does not send it again (``evo_agents.hub.transcript_sync``).
The project's blobs of kind transcript that no part of the project names any longer lose their ``blobs`` row, and
``blob_gc.forget`` records each one nothing refers to for deletion, in the same transaction, holding the blob lock
exclusively (``evo_agents.hub.blob_gc``); ``blob_gc.delete_pending`` then deletes the objects, here when the store
answers, else at the next pass of hub.prune_kg_artifacts (hourly).

``prune_transcripts`` drops the transcripts that got no part for ``days`` days (EVO_HUB_TRANSCRIPT_DAYS, 365 by
default), and the blobs of kind transcript a project has held for longer than STALE_AFTER that no part of it names: the
upload of a part the hub then did not record (another member's transcript, one deleted meanwhile). The words indexed
for search go with the content (``evo_agents.hub.history_index.drop_transcripts``); nothing else computed from
transcripts is touched.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blob_gc, history_index, tables
from evo_agents.hub import transcripts as local
from evo_agents.hub.blobs import STALE_AFTER, BlobStore, BlobStoreUnavailable

log = logging.getLogger(__name__)


def _named(project_id):
    """Whether a part of a transcript of ``project_id`` (a value or the blob's column) names the blob of the row
    being deleted."""
    t, p, b = tables.session_transcripts, tables.transcript_parts, tables.blobs
    return (
        select(p.c.transcript_id)
        .join_from(p, t, t.c.id == p.c.transcript_id)
        .where(p.c.sha256 == b.c.sha256, t.c.project_id == project_id)
        .correlate(b)
        .exists()
    )


@dataclass
class Dropped:
    transcripts: int = 0
    parts: int = 0
    bytes: int = 0


async def drop(conn: AsyncConnection, where) -> Dropped:
    """Delete the content of the transcripts ``where`` selects that the hub still holds, keeping their rows marked
    deleted, and the project's blobs of their parts that no other part of the project names, recorded for deletion
    when nothing else refers to them (``blob_gc.forget``). Holding the blob lock exclusively."""
    t, p, b = tables.session_transcripts, tables.transcript_parts, tables.blobs
    doomed = (await conn.execute(select(t.c.id, t.c.project_id).where(where, t.c.deleted_at.is_(None)))).all()
    if not doomed:
        return Dropped()
    ids = [row.id for row in doomed]
    held = (
        await conn.execute(
            select(t.c.project_id, p.c.sha256, p.c.size)
            .join_from(p, t, t.c.id == p.c.transcript_id)
            .where(p.c.transcript_id.in_(ids))
        )
    ).all()
    await conn.execute(delete(p).where(p.c.transcript_id.in_(ids)))
    await history_index.drop_transcripts(conn, ids)
    await conn.execute(
        update(t)
        .values(deleted_at=func.now(), updated_at=func.now(), parts=0, size=0, raw_size=0, sha256=None)
        .where(t.c.id.in_(ids))
    )
    by_project: dict[int, dict[str, int]] = {}
    for row in held:
        by_project.setdefault(row.project_id, {})[row.sha256] = row.size
    left: dict[str, tuple[int, str]] = {}
    for project_id, hashes in by_project.items():
        gone = await conn.execute(
            delete(b)
            .where(b.c.project_id == project_id, b.c.kind == local.KIND, blob_gc.hashes_in(b.c.sha256, sorted(hashes)))
            .where(~_named(project_id))
            .returning(b.c.sha256, b.c.size)
        )
        left.update({row.sha256: (row.size, local.KIND) for row in gone})
    await blob_gc.forget(conn, left)
    return Dropped(len(ids), len(held), sum(row.size for row in held))


async def delete_objects(engine: AsyncEngine, store: BlobStore | None) -> None:
    """Delete the objects recorded for deletion now; a later pass (hub.prune_kg_artifacts, hourly) does it otherwise."""
    if store is None:
        return
    try:
        await blob_gc.delete_pending(engine, store)
    except BlobStoreUnavailable:
        log.warning("transcript blobs not deleted from the store yet; a later pass deletes them")


async def prune_transcripts(engine: AsyncEngine, store: BlobStore | None, days: int) -> dict:
    """Delete the content of the transcripts that got no part for ``days`` days, and the blobs of kind transcript a
    project has held for longer than STALE_AFTER that no part of it names (a part that was never recorded)."""
    t, b = tables.session_transcripts, tables.blobs
    async with engine.begin() as conn:
        await blob_gc.lock_exclusive(conn)
        dropped = await drop(conn, t.c.updated_at < func.now() - timedelta(days=days))
        orphans = (
            await conn.execute(
                delete(b)
                .where(b.c.kind == local.KIND, b.c.verified_at < func.now() - STALE_AFTER, ~_named(b.c.project_id))
                .returning(b.c.sha256, b.c.size)
            )
        ).all()
        await blob_gc.forget(conn, {row.sha256: (row.size, local.KIND) for row in orphans})
    if dropped.transcripts or orphans:
        await delete_objects(engine, store)
    result = {**asdict(dropped), "orphans": len(orphans), "days": days}
    log.info("transcripts pruned", extra=result)
    return result

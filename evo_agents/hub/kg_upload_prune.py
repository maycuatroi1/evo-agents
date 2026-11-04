"""The hourly cleanup of archive uploads (``hub kg upload``, ``evo_agents.hub.server.kg_uploads``), which the worker's
``hub.cleanup_uploads`` job runs: an upload still ``uploading`` and untouched for STALE_AFTER is marked failed and its
parts dropped from the blob store, as the objects under ``uploads/`` are removed after as long."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.blobs import STALE_AFTER, BlobStore

log = logging.getLogger(__name__)


async def remove_stale(store: BlobStore, engine: AsyncEngine, now: datetime) -> dict:
    """Mark failed every upload still ``uploading`` and untouched for STALE_AFTER and abort its multipart upload, then
    abort each multipart upload under ``kg-archives/`` started before that which no upload in flight names (one whose
    row a rollback lost). An upload a client still sends parts to is touched by each call for URLs, so it stays."""
    u = tables.kg_uploads
    cutoff = now - STALE_AFTER
    async with engine.begin() as conn:
        stale = (
            await conn.execute(
                select(u.c.id, u.c.object_key, u.c.multipart_id)
                .where(u.c.state == "uploading", u.c.updated_at < cutoff)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if stale:
            await conn.execute(
                update(u)
                .where(u.c.id.in_([row.id for row in stale]))
                .values(state="failed", ended_at=func.now(), error="not finished in time", multipart_id=None)
            )
        live = set((await conn.execute(select(u.c.multipart_id).where(u.c.state == "uploading"))).scalars())
    gone = [(row.object_key, row.multipart_id) for row in stale if row.multipart_id]
    started = await asyncio.to_thread(store.list_multipart)
    gone += [(key, mid) for key, mid, when in started if when < cutoff and mid not in live and (key, mid) not in gone]
    for key, mid in gone:
        await asyncio.to_thread(store.abort_multipart, key, mid)
    log.info("stale kg uploads removed", extra={"rows": len(stale), "aborted": len(gone)})
    return {"kg_uploads_failed": len(stale), "kg_multipart_aborted": len(gone)}

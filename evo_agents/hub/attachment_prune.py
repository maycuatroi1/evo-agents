"""Deleting files people attached to messages (``evo_agents.hub.attachments``): one its uploader deletes before sending
it (``evo_agents.hub.server.attachments``), those no message took within STALE_AFTER (the job hub.cleanup_uploads),
and those of the messages to runs that ended more than EVO_HUB_RUN_LOG_DAYS ago, which go with the runs' events (the
job hub.prune_run_events, through ``evo_agents.hub.server.run_state.prune_run_events``). The files of chat messages
stay with the chat.

``drop`` deletes the rows a condition selects, holding the blob lock exclusively, and ``blob_gc.forget`` records each
blob that nothing refers to any longer for deletion in the same transaction (``evo_agents.hub.blob_gc``); its object
goes now when the store answers (``output_prune.delete_objects``), else at the next pass of hub.prune_kg_artifacts.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import attachments, blob_gc, tables
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.output_prune import delete_objects

log = logging.getLogger(__name__)


async def drop(conn: AsyncConnection, where) -> list:
    """Delete the attachments ``where`` selects, and record for deletion each of their blobs nothing refers to any
    longer; (id, sha256, size) of each row deleted. Holding the blob lock exclusively."""
    a = tables.attachments
    gone = (await conn.execute(delete(a).where(where).returning(a.c.id, a.c.sha256, a.c.size))).all()
    if gone:
        await blob_gc.forget(conn, {row.sha256: (row.size, attachments.KIND) for row in gone})
    return gone


def unsent():
    """The attachments no message holds."""
    a = tables.attachments
    return and_(a.c.inbox_id.is_(None), a.c.chat_message_id.is_(None))


async def remove_stale(engine: AsyncEngine, store: BlobStore | None, now: datetime | None = None) -> dict:
    """Delete the attachments no message took within STALE_AFTER of their upload, and their blobs nothing else
    refers to."""
    cutoff = (now or datetime.now(UTC)) - attachments.STALE_AFTER
    a = tables.attachments
    async with engine.begin() as conn:
        await blob_gc.lock_exclusive(conn)
        gone = await drop(conn, and_(unsent(), a.c.created_at < cutoff))
    if gone:
        await delete_objects(engine, store)
        log.info("stale attachments removed", extra={"attachments": len(gone), "cutoff": cutoff.isoformat()})
    return {"attachments": len(gone)}


def of_runs_ended_before(days: int):
    """The attachments of the messages to runs that ended more than ``days`` days ago, which an inbox row of such a
    run holds."""
    a, r, i = tables.attachments, tables.runs, tables.run_inbox
    old = select(i.c.id).join_from(i, r, r.c.id == i.c.run_id)
    old = old.where(r.c.finished_at.is_not(None), r.c.finished_at < func.now() - timedelta(days=days))
    return a.c.inbox_id.in_(old)


async def prune_run_files(engine: AsyncEngine, store: BlobStore | None, days: int) -> dict:
    """Delete the files of the messages to runs that ended more than ``days`` days ago (see the module)."""
    async with engine.begin() as conn:
        await blob_gc.lock_exclusive(conn)
        gone = await drop(conn, of_runs_ended_before(days))
    if gone:
        await delete_objects(engine, store)
        size = sum(row.size for row in gone)
        log.info("run attachments pruned", extra={"attachments": len(gone), "bytes": size, "days": days})
    return {"attachments": len(gone)}

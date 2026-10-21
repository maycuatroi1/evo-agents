"""Deleting outputs of runs (``evo_agents.hub.outputs``): one a member deletes (``evo_agents.hub.server.run_outputs``),
and those of source tool that go with their run's events (the job hub.prune_run_events, through
``evo_agents.hub.server.run_state.prune_run_events``).

``drop`` deletes the outputs a condition selects, their links to questions and chat messages with them. The project's
blobs of kind run-output that no output of the project names any longer lose their ``blobs`` row, and
``blob_gc.forget`` records each one nothing refers to for deletion, in the same transaction, holding the blob lock
exclusively (``evo_agents.hub.blob_gc``); ``blob_gc.delete_pending`` then deletes the objects, here when the store
answers, else at the next pass of hub.prune_kg_artifacts (hourly). A blobs row of another kind is another part's, which
keeps it: the bytes stay while anything refers to them.

``prune_tool_outputs`` drops the outputs of source tool of the runs that ended more than ``days`` days ago
(EVO_HUB_RUN_LOG_DAYS, as the events), but those a question or a chat message carries: a file attached there is kept,
as the outputs of the other sources are, until someone deletes it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import and_, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blob_gc, outputs, tables
from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable

log = logging.getLogger(__name__)


@dataclass
class Dropped:
    outputs: list = field(default_factory=list)  # (id, run_id, project_id, sha256, size) of each output deleted
    blobs: int = 0  # blobs of kind run-output the projects no longer hold

    @property
    def bytes(self) -> int:
        return sum(row.size for row in self.outputs)


def _named(project_id):
    """Whether an output of ``project_id`` (a value or the blob's column) names the blob of the row being deleted."""
    o, b = tables.run_outputs, tables.blobs
    return select(o.c.id).where(o.c.sha256 == b.c.sha256, o.c.project_id == project_id).correlate(b).exists()


async def drop(conn: AsyncConnection, where) -> Dropped:
    """Delete the outputs ``where`` selects, with their links, and the project's blobs of kind run-output that no
    output of the project names any longer, recorded for deletion when nothing else refers to them
    (``blob_gc.forget``). Holding the blob lock exclusively."""
    o, b = tables.run_outputs, tables.blobs
    gone = (
        await conn.execute(delete(o).where(where).returning(o.c.id, o.c.run_id, o.c.project_id, o.c.sha256, o.c.size))
    ).all()
    if not gone:
        return Dropped()
    by_project: dict[int, set[str]] = {}
    for row in gone:
        by_project.setdefault(row.project_id, set()).add(row.sha256)
    left: dict[str, tuple[int, str]] = {}
    for project_id, hashes in by_project.items():
        released = await conn.execute(
            delete(b)
            .where(
                b.c.project_id == project_id, b.c.kind == outputs.KIND, blob_gc.hashes_in(b.c.sha256, sorted(hashes))
            )
            .where(~_named(project_id))
            .returning(b.c.sha256, b.c.size)
        )
        left.update({row.sha256: (row.size, outputs.KIND) for row in released})
    await blob_gc.forget(conn, left)
    return Dropped(list(gone), len(left))


async def delete_objects(engine: AsyncEngine, store: BlobStore | None) -> None:
    """Delete the objects recorded for deletion now; a later pass (hub.prune_kg_artifacts, hourly) does it otherwise."""
    if store is None:
        return
    try:
        await blob_gc.delete_pending(engine, store)
    except BlobStoreUnavailable:
        log.warning("output blobs not deleted from the store yet; a later pass deletes them")


def tool_outputs_of_runs_ended_before(days: int):
    """The outputs of source tool, of runs that ended more than ``days`` days ago, that no question or chat message
    carries: what goes with the runs' events."""
    r, o, k = tables.runs, tables.run_outputs, tables.run_output_links
    old = select(r.c.id).where(r.c.finished_at.is_not(None), r.c.finished_at < func.now() - timedelta(days=days))
    return and_(o.c.source == outputs.TOOL, o.c.run_id.in_(old), ~exists().where(k.c.output_id == o.c.id))


async def prune_tool_outputs(engine: AsyncEngine, store: BlobStore | None, days: int) -> dict:
    """Delete the outputs of source tool of the runs that ended more than ``days`` days ago, but those a question or
    a chat message carries, and the blobs no output names any longer (see the module)."""
    async with engine.begin() as conn:
        await blob_gc.lock_exclusive(conn)
        dropped = await drop(conn, tool_outputs_of_runs_ended_before(days))
    if dropped.outputs:
        await delete_objects(engine, store)
        log.info(
            "tool outputs pruned",
            extra={"outputs": len(dropped.outputs), "bytes": dropped.bytes, "blobs": dropped.blobs, "days": days},
        )
    return {"tool_outputs": len(dropped.outputs), "tool_output_bytes": dropped.bytes}

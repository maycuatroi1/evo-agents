"""Deleting blobs nothing refers to any longer, without a moment when a row names an object that is gone.

A blob is referred to by a ``blobs`` row of any holder, a skill version, a kg build's artifact, or the log of an
ingested or pending kg run. Whoever drops references (the retention of built graphs, ``evo_agents.hub.kg_prune``)
does it in a transaction holding the blob lock exclusively, and in that same transaction ``forget`` records in
``blob_deletions`` each blob it left without any reference. ``delete_pending`` then deletes their objects in a
second transaction holding the lock exclusively, and marks the rows deleted. A deletion that fails rolls back: the
rows stay pending and the next run tries again, which deleting an object that is already gone does not mind.

Writing a reference to a blob whose object the writer did not just put in the bucket races with that deletion: a
commit of an upload the hub already holds skips the copy, and a build may find its artifact's key taken. Such a
writer holds the lock shared in the transaction that writes the reference, and calls ``revive`` there first: a hash
with a ``blob_deletions`` row may have lost its object, so the writer puts the bytes back before it commits (the lock
keeps every deletion out meanwhile) and the row goes. Writers hold the lock shared, so they never wait for each
other, only for a deletion in progress.

The lock is a transaction-level advisory lock: it goes with the transaction, whatever ends it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import BigInteger, Text, any_, column, delete, exists, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.blobs import BlobStore, blob_key

log = logging.getLogger(__name__)

LOCK_KEY = 0x65766F2D626C6F62  # "evo-blob" in ASCII; the migrations' lock is another key
KEEP_DELETED = timedelta(days=30)  # how long a deleted blob's row stays, for the record


def hashes_in(sha256, hashes: list[str]):
    """``sha256 = ANY(hashes)``, the list bound as one text[] parameter however long it is."""
    return sha256 == any_(literal(hashes, ARRAY(Text)))


def _referenced(hashes: list[str]):
    """The hashes of ``hashes`` that a blobs row, a skill version, a kg build's artifact or the log of an ingested or
    pending kg run refers to."""
    h = func.unnest(literal(hashes, ARRAY(Text))).table_valued(column("sha256", Text)).render_derived(name="h")
    references = (
        tables.blobs.c.sha256,
        tables.skill_versions.c.sha256,
        tables.kg_builds.c.artifact_sha256,
        tables.kg_ingests.c.log_sha256,
        tables.kg_pending_runs.c.log_sha256,
    )
    return select(h.c.sha256).where(or_(*(exists().where(sha256 == h.c.sha256) for sha256 in references)))


@dataclass(frozen=True)
class Deleted:
    objects: int  # objects deleted from the bucket
    bytes: int
    dropped: int  # rows dropped because the blob is referred to again


async def lock_exclusive(conn: AsyncConnection) -> None:
    """Hold the blob lock exclusively until ``conn``'s transaction ends: for dropping references and deleting."""
    await conn.execute(select(func.pg_advisory_xact_lock(literal(LOCK_KEY, BigInteger))))


async def lock_shared(conn: AsyncConnection) -> None:
    """Hold the blob lock shared until ``conn``'s transaction ends: for writing references (see the module)."""
    await conn.execute(select(func.pg_advisory_xact_lock_shared(literal(LOCK_KEY, BigInteger))))


async def referenced(conn: AsyncConnection, hashes) -> set[str]:
    """The hashes of ``hashes`` that something refers to."""
    return set((await conn.execute(_referenced(sorted(set(hashes))))).scalars())


async def forget(conn: AsyncConnection, blobs: dict[str, tuple[int, str]]) -> list[str]:
    """Record for deletion the blobs of ``blobs`` ({sha256: (size, kind)}) that nothing refers to any longer; the
    hashes recorded. In the transaction that dropped the references, holding the lock exclusively."""
    still = await referenced(conn, blobs)
    gone = sorted(sha for sha in blobs if sha not in still)
    if gone:
        deletions = tables.blob_deletions
        d = (
            func.unnest(
                literal(gone, ARRAY(Text)),
                literal([blobs[sha][0] for sha in gone], ARRAY(BigInteger)),
                literal([blobs[sha][1] for sha in gone], ARRAY(Text)),
            )
            .table_valued(column("sha256", Text), column("size", BigInteger), column("kind", Text))
            .render_derived(name="d")
        )
        recorded = pg_insert(deletions).from_select(["sha256", "size", "kind"], select(d.c.sha256, d.c.size, d.c.kind))
        again = {"size": recorded.excluded.size, "kind": recorded.excluded.kind}
        await conn.execute(
            recorded.on_conflict_do_update(
                index_elements=[deletions.c.sha256], set_={**again, "requested_at": func.now(), "deleted_at": None}
            )
        )
    return gone


async def revive(conn: AsyncConnection, hashes) -> list[str]:
    """The hashes of ``hashes`` recorded for deletion, whose rows this deletes: their objects may be gone, and the
    caller must put the bytes back before its transaction commits. Holding the lock shared."""
    deletions = tables.blob_deletions
    revived = delete(deletions).where(hashes_in(deletions.c.sha256, sorted(set(hashes)))).returning(deletions.c.sha256)
    return sorted((await conn.execute(revived)).scalars())


async def pending(conn: AsyncConnection) -> tuple[int, int]:
    """How many blobs wait for their objects to be deleted, and their bytes."""
    deletions = tables.blob_deletions
    waiting = select(func.count(), func.coalesce(func.sum(deletions.c.size), 0)).where(deletions.c.deleted_at.is_(None))
    count, size = (await conn.execute(waiting)).one()
    return int(count), int(size)


async def delete_pending(engine, store: BlobStore) -> Deleted:
    """Delete the objects of the blobs recorded for deletion, holding the lock exclusively, and mark their rows
    deleted; a blob referred to again meanwhile keeps its object and loses its row. Rows deleted over KEEP_DELETED
    ago go. Raises BlobStoreUnavailable when the bucket does not delete, leaving every row as it was."""
    deletions = tables.blob_deletions
    async with engine.begin() as conn:
        await lock_exclusive(conn)
        waiting = (
            select(deletions.c.sha256, deletions.c.size)
            .where(deletions.c.deleted_at.is_(None))
            .order_by(deletions.c.sha256)
            .with_for_update()
        )
        sizes = {row.sha256: row.size for row in await conn.execute(waiting)}
        again = await referenced(conn, sizes)
        if again:
            await conn.execute(delete(deletions).where(hashes_in(deletions.c.sha256, sorted(again))))
        doomed = sorted(sha for sha in sizes if sha not in again)
        if doomed:
            await asyncio.to_thread(store.delete, [blob_key(sha) for sha in doomed])
            deleted = update(deletions).values(deleted_at=func.now()).where(hashes_in(deletions.c.sha256, doomed))
            await conn.execute(deleted)
        await conn.execute(delete(deletions).where(deletions.c.deleted_at < func.now() - KEEP_DELETED))
    result = Deleted(len(doomed), sum(sizes[sha] for sha in doomed), len(again))
    if doomed or again:
        log.info(
            "unreferenced blobs deleted",
            extra={"objects": result.objects, "bytes": result.bytes, "referenced_again": result.dropped},
        )
    return result

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

from evo_agents.hub.blobs import BlobStore, blob_key

log = logging.getLogger(__name__)

LOCK_KEY = 0x65766F2D626C6F62  # "evo-blob" in ASCII; the migrations' lock is another key
KEEP_DELETED = timedelta(days=30)  # how long a deleted blob's row stays, for the record

REFERENCED = """
SELECT h.sha256 FROM unnest(%(hashes)s::text[]) AS h (sha256)
 WHERE EXISTS (SELECT 1 FROM blobs b WHERE b.sha256 = h.sha256)
    OR EXISTS (SELECT 1 FROM skill_versions s WHERE s.sha256 = h.sha256)
    OR EXISTS (SELECT 1 FROM kg_builds k WHERE k.artifact_sha256 = h.sha256)
    OR EXISTS (SELECT 1 FROM kg_ingests i WHERE i.log_sha256 = h.sha256)
    OR EXISTS (SELECT 1 FROM kg_pending_runs r WHERE r.log_sha256 = h.sha256)
"""
FORGET = """
INSERT INTO blob_deletions (sha256, size, kind)
SELECT sha256, size, kind FROM unnest(%s::text[], %s::bigint[], %s::text[]) AS d (sha256, size, kind)
    ON CONFLICT (sha256) DO UPDATE SET size = excluded.size, kind = excluded.kind, requested_at = now(),
       deleted_at = NULL
"""
PENDING = "SELECT sha256, size FROM blob_deletions WHERE deleted_at IS NULL ORDER BY sha256 FOR UPDATE"


@dataclass(frozen=True)
class Deleted:
    objects: int  # objects deleted from the bucket
    bytes: int
    dropped: int  # rows dropped because the blob is referred to again


async def lock_exclusive(conn) -> None:
    """Hold the blob lock exclusively until ``conn``'s transaction ends: for dropping references and deleting."""
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))


async def lock_shared(conn) -> None:
    """Hold the blob lock shared until ``conn``'s transaction ends: for writing references (see the module)."""
    await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (LOCK_KEY,))


async def referenced(conn, hashes) -> set[str]:
    """The hashes of ``hashes`` that something refers to."""
    cursor = await conn.execute(REFERENCED, {"hashes": sorted(set(hashes))})
    return {row[0] for row in await cursor.fetchall()}


async def forget(conn, blobs: dict[str, tuple[int, str]]) -> list[str]:
    """Record for deletion the blobs of ``blobs`` ({sha256: (size, kind)}) that nothing refers to any longer; the
    hashes recorded. In the transaction that dropped the references, holding the lock exclusively."""
    still = await referenced(conn, blobs)
    gone = sorted(sha for sha in blobs if sha not in still)
    if gone:
        await conn.execute(FORGET, (gone, [blobs[sha][0] for sha in gone], [blobs[sha][1] for sha in gone]))
    return gone


async def revive(conn, hashes) -> list[str]:
    """The hashes of ``hashes`` recorded for deletion, whose rows this deletes: their objects may be gone, and the
    caller must put the bytes back before its transaction commits. Holding the lock shared."""
    cursor = await conn.execute(
        "DELETE FROM blob_deletions WHERE sha256 = ANY(%s) RETURNING sha256", (sorted(set(hashes)),)
    )
    return sorted(row[0] for row in await cursor.fetchall())


async def pending(conn) -> tuple[int, int]:
    """How many blobs wait for their objects to be deleted, and their bytes."""
    row = await (
        await conn.execute("SELECT count(*), coalesce(sum(size), 0) FROM blob_deletions WHERE deleted_at IS NULL")
    ).fetchone()
    return int(row[0]), int(row[1])


async def delete_pending(pool, store: BlobStore) -> Deleted:
    """Delete the objects of the blobs recorded for deletion, holding the lock exclusively, and mark their rows
    deleted; a blob referred to again meanwhile keeps its object and loses its row. Rows deleted over KEEP_DELETED
    ago go. Raises BlobStoreUnavailable when the bucket does not delete, leaving every row as it was."""
    async with pool.connection() as conn:
        await lock_exclusive(conn)
        rows = await (await conn.execute(PENDING)).fetchall()
        sizes = dict(rows)
        again = await referenced(conn, sizes)
        if again:
            await conn.execute("DELETE FROM blob_deletions WHERE sha256 = ANY(%s)", (sorted(again),))
        doomed = sorted(sha for sha in sizes if sha not in again)
        if doomed:
            await asyncio.to_thread(store.delete, [blob_key(sha) for sha in doomed])
            await conn.execute("UPDATE blob_deletions SET deleted_at = now() WHERE sha256 = ANY(%s)", (doomed,))
        await conn.execute("DELETE FROM blob_deletions WHERE deleted_at < now() - %s", (KEEP_DELETED,))
    result = Deleted(len(doomed), sum(sizes[sha] for sha in doomed), len(again))
    if doomed or again:
        log.info(
            "unreferenced blobs deleted",
            extra={"objects": result.objects, "bytes": result.bytes, "referenced_again": result.dropped},
        )
    return result

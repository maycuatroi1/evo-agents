"""The SQL of blob uploads: the uploads asked for (blob_uploads) and the blobs a holder records (blobs)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Text, Uuid, any_, cast, column, delete, func, insert, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import STALE_AFTER, Upload


async def held_hashes(conn: AsyncConnection, project_id: int | None, hashes: list[str]) -> set[str]:
    """The hashes of ``hashes`` that holder ``project_id`` (None: the hub itself) records already."""
    blobs = tables.blobs
    held = select(blobs.c.sha256).where(
        blobs.c.project_id.is_not_distinct_from(project_id), blob_gc.hashes_in(blobs.c.sha256, hashes)
    )
    return set((await conn.execute(held)).scalars())


async def add_uploads(conn: AsyncConnection, project_id: int | None, user_id: int, uploads: list[Upload]) -> None:
    """One blob_uploads row per upload of ``uploads``, asked for by ``user_id`` in ``project_id``."""
    await conn.execute(
        insert(tables.blob_uploads),
        [
            {
                "upload_id": u.upload_id,
                "project_id": project_id,
                "sha256": u.sha256,
                "size": u.size,
                "kind": u.kind,
                "created_by": user_id,
            }
            for u in uploads
        ],
    )


def _uploads_in(upload_ids: list[str]):
    """``upload_id = ANY(upload_ids)``, the ids bound as one uuid[] parameter."""
    return tables.blob_uploads.c.upload_id == any_(literal(upload_ids, ARRAY(Uuid)))


def _pending(upload_ids: list[str], project_id: int | None, user_id: int):
    """The uploads of ``upload_ids`` that ``user_id`` asked for in ``project_id`` within STALE_AFTER, as Upload's
    fields by column name."""
    uploads = tables.blob_uploads
    return select(
        cast(uploads.c.upload_id, Text).label("upload_id"), uploads.c.sha256, uploads.c.size, uploads.c.kind
    ).where(
        _uploads_in(upload_ids),
        uploads.c.project_id.is_not_distinct_from(project_id),
        uploads.c.created_by == user_id,
        uploads.c.created_at > func.now() - STALE_AFTER,
    )


async def pending_uploads(conn: AsyncConnection, upload_ids: list[str], project_id: int | None, user_id: int):
    """The rows of ``_pending``: the uploads ``user_id`` may commit in ``project_id``."""
    return (await conn.execute(_pending(upload_ids, project_id, user_id))).all()


async def delete_uploads(conn: AsyncConnection, upload_ids: list[str]) -> None:
    await conn.execute(delete(tables.blob_uploads).where(_uploads_in(upload_ids)))


async def recorded_anywhere(conn: AsyncConnection, hashes: list[str]) -> frozenset[str]:
    """The hashes of ``hashes`` the hub records for any holder."""
    sha256 = tables.blobs.c.sha256
    recorded = select(sha256).distinct().where(blob_gc.hashes_in(sha256, hashes))
    return frozenset((await conn.execute(recorded)).scalars())


async def revived(conn: AsyncConnection, hashes: list[str]) -> set[str]:
    """The hashes of ``hashes`` whose objects a deletion took since (``evo_agents.hub.blob_gc``), holding the blob lock
    shared, so that no deletion runs until the transaction ends."""
    await blob_gc.lock_shared(conn)
    return set(await blob_gc.revive(conn, hashes))


def _recorded(project_id: int | None, user_id: int, blobs: list[Upload]):
    """One blobs row of ``project_id`` per blob of ``blobs`` that it does not hold yet, returning their hashes."""
    b = (
        func.unnest(
            literal([blob.sha256 for blob in blobs], ARRAY(Text)),
            literal([blob.size for blob in blobs], ARRAY(BigInteger)),
            literal([blob.kind for blob in blobs], ARRAY(Text)),
        )
        .table_valued(column("sha256", Text), column("size", BigInteger), column("kind", Text))
        .render_derived(name="b")
    )
    rows = select(literal(project_id, BigInteger), b.c.sha256, b.c.size, b.c.kind, literal(user_id, BigInteger))
    held = tables.blobs
    return (
        pg_insert(held)
        .from_select(["project_id", "sha256", "size", "kind", "created_by"], rows)
        .on_conflict_do_nothing(index_elements=[held.c.project_id, held.c.sha256])
        .returning(held.c.sha256)
    )


async def record_blobs(conn: AsyncConnection, project_id: int | None, user_id: int, blobs: list[Upload]) -> int:
    """Record ``blobs`` for ``project_id``; how many it did not hold yet."""
    return len((await conn.execute(_recorded(project_id, user_id, blobs))).all())

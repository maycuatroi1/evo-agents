"""Issuing and committing blob uploads: who may, what the blob store is asked, and what the routes of
``evo_agents.hub.server.blobs`` answer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.blobs import KIND_LIMITS, UPLOAD_TTL, BlobStore, BlobStoreUnavailable, Upload, new_upload_id
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.blob_store.models.uploads import (
    GLOBAL,
    GLOBAL_KINDS,
    UNAVAILABLE,
    Blob,
    Committed,
    Holder,
    Mismatch,
    OverLimit,
    UploadItem,
    Uploads,
    UploadTicket,
)
from evo_agents.hub.server.blob_store.repository import uploads as repo
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger("evo_agents.hub.server.blobs")
T = TypeVar("T")


def configured(store: BlobStore | None, config) -> BlobStore:
    """The hub's blob store ``store``; 503 naming the variables of ``config`` when it is not configured."""
    if store is None:
        missing = ", ".join(config.blob_store_missing())
        raise HTTPException(503, f"the blob store is not configured on this hub: set {missing}")
    return store


def _writer(access: ProjectAccess, doing: str) -> None:
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"{doing} blobs of project {access.name} needs the writer role on it")


async def holder(conn: AsyncConnection, user: Principal, project: str | None, doing: str) -> Holder:
    """The holder ``user`` is ``doing`` something to blobs of: ``project``, where ``user`` must be a writer, or the hub
    itself when ``project`` is None, for a hub admin. 403 (or the 404 of ``project_access``) otherwise."""
    if project is None:
        if not user.admin:
            raise HTTPException(
                403, f"{doing} blobs outside a project (the bundles of global skills) needs a hub admin"
            )
        return Holder(None, GLOBAL)
    access = await project_access(conn, user, project)
    _writer(access, doing)
    return Holder(access.project_id, access.name)


def _distinct(items: list[UploadItem]) -> list[UploadItem]:
    """``items`` without repeats; 422 when one hash is declared twice with another size or kind."""
    seen: dict[str, UploadItem] = {}
    for item in items:
        other = seen.setdefault(item.sha256, item)
        if (other.size, other.kind) != (item.size, item.kind):
            raise HTTPException(422, f"blob {item.sha256} is declared twice with different sizes or kinds")
    return list(seen.values())


def _over_limit(items: list[UploadItem]) -> OverLimit | None:
    over = [item for item in items if item.size > KIND_LIMITS[item.kind]]
    if not over:
        return None
    detail = [{"sha256": i.sha256, "kind": i.kind, "size": i.size, "limit": KIND_LIMITS[i.kind]} for i in over]
    first = over[0]
    message = (
        f"{len(over)} blob(s) exceed the size limit of their kind, such as {first.sha256} of kind {first.kind} with "
        f"{first.size} bytes over {KIND_LIMITS[first.kind]}; no upload was issued"
    )
    return OverLimit(message, detail)


async def issue_uploads(
    engine: AsyncEngine, store: BlobStore, user: Principal, project: str | None, items: list[UploadItem]
) -> Uploads | OverLimit:
    """The uploads of ``items`` of ``project`` (None: of the hub itself) as ``user``, or the refusal of the items over
    their limit. Raises the HTTPException of ``holder`` and the blob store."""
    items = _distinct(items)
    async with UnitOfWork(engine) as conn:
        access = await holder(conn, user, project, "uploading")
        if access.project_id is None and any(item.kind not in GLOBAL_KINDS for item in items):
            raise HTTPException(
                422, f"outside a project the hub holds only blobs of kind {', '.join(sorted(GLOBAL_KINDS))}"
            )
        refusal = _over_limit(items)
        if refusal is not None:
            return refusal
        present = await repo.held_hashes(conn, access.project_id, [item.sha256 for item in items])
        needed = [
            Upload(new_upload_id(), item.sha256, item.size, item.kind) for item in items if item.sha256 not in present
        ]
        if needed:
            await repo.add_uploads(conn, access.project_id, user.user_id, needed)
    expires_at = datetime.now(UTC) + UPLOAD_TTL  # taken before signing, so never later than the URLs

    def sign() -> list[UploadTicket]:
        return [
            UploadTicket(sha256=u.sha256, upload_id=u.upload_id, url=store.presign_put(u.upload_id, u.size))
            for u in needed
        ]

    tickets = await asyncio.to_thread(sign)
    log.info(
        "blob uploads issued",
        extra={"project": access.name, "login": user.login, "issued": len(needed), "present": len(present)},
    )
    return Uploads(uploads=tickets, present=sorted(present), expires_at=expires_at)


async def _discard(store: BlobStore, upload_ids: list[str]) -> None:
    """Delete the objects of these uploads; what a failure leaves behind, the worker removes after 24 hours."""
    try:
        await asyncio.to_thread(store.discard, upload_ids)
    except BlobStoreUnavailable:
        log.warning("uploaded objects not deleted; the hourly cleanup removes them", extra={"uploads": len(upload_ids)})


async def commit_uploads(
    engine: AsyncEngine,
    store: BlobStore,
    user: Principal,
    project: str | None,
    upload_ids: list[str],
    *,
    kinds: frozenset[str] | None = None,
    inspect: Callable[[list[Upload]], Awaitable[None]] | None = None,
) -> Committed:
    """Verify and commit ``upload_ids`` of ``project`` (None: of the hub itself) as ``user``, as
    ``evo_agents.hub.server.blobs.commit_uploads`` says."""
    ids = sorted(set(upload_ids))
    access, uploads = await _pending_uploads(engine, user, project, ids, kinds)
    await _sealed(store, engine, access, user, ids, uploads)
    if inspect is not None:
        await _inspected(store, engine, ids, uploads, inspect)
    async with UnitOfWork(engine) as conn:  # the blobs the hub records already: only those are looked for first
        held = await repo.recorded_anywhere(conn, [upload.sha256 for upload in uploads])
    written = await _unavailable_as_503(asyncio.to_thread(store.publish_all, uploads, held))
    access, blobs, added = await _record_blobs(store, engine, user, project, ids, uploads)
    await _discard(store, ids)
    log.info(
        "blobs committed",
        extra={
            "project": access.name,
            "login": user.login,
            "uploads": len(ids),
            "added": added,
            "stored": sum(written),
        },
    )
    blobs.sort(key=lambda upload: upload.sha256)
    return Committed(blobs=[Blob(sha256=b.sha256, size=b.size, kind=b.kind) for b in blobs], added=added)


async def _unavailable_as_503(work: Awaitable[T]) -> T:
    """``work``, with an outage of the blob store answered 503."""
    try:
        return await work
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None


async def _pending_uploads(
    engine: AsyncEngine, user: Principal, project: str | None, ids: list[str], kinds: frozenset[str] | None
):
    """The project's access and the pending uploads of ``ids``; 422 when one is not the caller's to commit."""
    async with UnitOfWork(engine) as conn:
        access = await holder(conn, user, project, "committing")
        rows = await repo.pending_uploads(conn, ids, access.project_id, user.user_id)
        uploads = [Upload(**row._mapping) for row in rows if kinds is None or row.kind in kinds]
    unknown = sorted(set(ids) - {upload.upload_id for upload in uploads})
    if unknown:
        raise HTTPException(
            422,
            f"unknown uploads {', '.join(unknown)}: an upload is committed once, in its project, by whoever asked "
            "for it, within 24 hours; nothing was committed",
        )
    return access, uploads


async def _drop_uploads(store: BlobStore, engine: AsyncEngine, ids: list[str]) -> None:
    async with UnitOfWork(engine) as conn:
        await repo.delete_uploads(conn, ids)
    await _discard(store, ids)


async def _sealed(
    store: BlobStore, engine: AsyncEngine, access, user: Principal, ids: list[str], uploads: list[Upload]
) -> None:
    """Seal every upload; when one does not match what was declared, discard them all and raise Mismatch."""
    verdicts = await _unavailable_as_503(asyncio.to_thread(store.seal_all, uploads))
    problems = [
        {"upload_id": v.upload.upload_id, "sha256": v.upload.sha256, "problem": v.problem}
        for v in verdicts
        if v.problem
    ]
    if problems:
        await _drop_uploads(store, engine, ids)
        log.warning(
            "blob commit refused: uploads do not match",
            extra={"project": access.name, "login": user.login, "uploads": len(ids), "mismatched": problems},
        )
        raise Mismatch(problems)


async def _inspected(store: BlobStore, engine: AsyncEngine, ids: list[str], uploads: list[Upload], inspect) -> None:
    """Await ``inspect``: whatever it raises discards every upload, except an outage of the store (503), after which
    the same commit can be sent again."""
    try:
        await inspect(uploads)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None  # the uploads stay, so the same commit can be sent again
    except Exception:
        await _drop_uploads(store, engine, ids)
        raise


async def _record_blobs(
    store: BlobStore, engine: AsyncEngine, user: Principal, project: str | None, ids, uploads: list[Upload]
):
    """Record the blobs in the project, once a hash, and drop the uploads' rows; returns the access, the blobs and how
    many were added."""
    async with UnitOfWork(engine) as conn:
        access = await holder(conn, user, project, "committing")  # the grant may have changed while the bytes were read
        blobs = list({upload.sha256: upload for upload in reversed(uploads)}.values())  # the first upload of a hash
        # A blob found held above may have lost its object to a deletion since (evo_agents.hub.blob_gc): copy those
        # sealed bytes again, while the lock keeps any deletion out until the rows are written.
        revived = await repo.revived(conn, [b.sha256 for b in blobs])
        if revived:
            await _unavailable_as_503(asyncio.to_thread(store.publish_all, [b for b in blobs if b.sha256 in revived]))
        added = await repo.record_blobs(conn, access.project_id, user.user_id, blobs)
        await repo.delete_uploads(conn, ids)
        if added:
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.BLOB_COMMIT, target=access.name
            )
    return access, blobs, added

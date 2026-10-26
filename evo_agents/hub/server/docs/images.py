"""The images of a project's documents (``evo_agents.hub.server.docs``), kept in the hub's own blob store.

A writer uploads an image as the body of a request, at most MAX_IMAGE_BYTES: the hub streams it into a temporary file
while it counts and hashes it, takes it only when its first bytes are those of a PNG, JPEG, WebP or GIF image (never
SVG, which can carry a script), puts it in the blob store unless the store has it, and records it for the project
(``docs_images``) once by its SHA-256, holding the blob lock shared so a deletion of the same bytes cannot run between
the two (``evo_agents.hub.blob_gc``). The row refers to the blob, and nothing drops it.

A reader of a document gets an image a version of it shows from the hub's origin (``serve``): its media type as
recorded, ``nosniff``, a Content-Security-Policy that runs nothing, ``Cross-Origin-Resource-Policy: same-origin``, its
SHA-256 as ETag (304 for If-None-Match), streamed from the blob store as it reads it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.tables_docs import MAX_IMAGE_BYTES

log = logging.getLogger(__name__)

SNIFFED = 16  # the bytes ``media_type`` reads
CACHE = "private, max-age=86400"
UNAVAILABLE = "the blob store did not answer; nothing was kept, upload the image again shortly"
KINDS = "PNG, JPEG, WebP or GIF"


def media_type(head: bytes) -> str | None:
    """The media type of an image by its first bytes, PNG, JPEG, WebP or GIF; None for anything else."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def too_large(size: int) -> str:
    return f"an image of a document is at most {MAX_IMAGE_BYTES} bytes (10 MiB); this one is {size}"


async def _receive(request: Request, path: Path) -> tuple[str, int, bytes]:
    """Stream the request's body into ``path``, hashing it: (sha256, size, its first SNIFFED bytes); 413 past
    MAX_IMAGE_BYTES."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise HTTPException(413, too_large(int(declared)))
    digest, size, head = hashlib.sha256(), 0, b""
    with path.open("wb") as handle:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise HTTPException(413, too_large(size))
            if len(head) < SNIFFED:
                head += chunk[: SNIFFED - len(head)]
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest(), size, head


async def _record(conn: AsyncConnection, project_id: int, user_id: int, found: dict) -> bool:
    """The project's row of the image; True when this call wrote it."""
    i = tables.docs.images
    added = (
        pg_insert(i)
        .values(project_id=project_id, uploaded_by=user_id, **found)
        .on_conflict_do_nothing(index_elements=[i.c.project_id, i.c.sha256])
        .returning(i.c.sha256)
    )
    return (await conn.execute(added)).first() is not None


async def upload(request: Request, user_id: int, check) -> tuple[dict, bool]:
    """Receive an image, put it in the blob store and record it for the project; ``check(conn)`` is the caller's
    test of who may upload, run before the bytes are read and again in the transaction that records them, and
    returns the project's id. (sha256, media_type, size) and whether the project held it already."""
    store = blob_store(request)
    engine = request.app.state.engine
    async with engine.begin() as conn:
        await check(conn)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "image"
        sha256, size, head = await _receive(request, path)
        kind = media_type(head)
        if size == 0 or kind is None:
            raise HTTPException(422, f"an image of a document is a {KINDS} image, and this one is none")
        found = {"sha256": sha256, "media_type": kind, "size": size}
        try:
            await asyncio.to_thread(store.put_file, sha256, path)
            async with engine.begin() as conn:
                project_id = await check(conn)
                await blob_gc.lock_shared(conn)
                if await blob_gc.revive(conn, [sha256]):  # its object may be gone: put it back before the row commits
                    await asyncio.to_thread(store.put_file, sha256, path, replace=True)
                added = await _record(conn, project_id, user_id, found)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None
    log.info("document image uploaded", extra={"size": size, "added": added, "media_type": kind})
    return found, not added


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


async def serve(request: Request, sha256: str, kind: str, if_none_match: str | None) -> Response:
    """The answer of the image ``sha256`` (see the module), for a caller who may read a document that shows it."""
    headers = {
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": CACHE,
        "ETag": f'"{sha256}"',
    }
    if _matches(if_none_match, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    try:
        opened = await asyncio.to_thread(blob_store(request).open_range, sha256)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    if opened is None:
        raise HTTPException(404, f"image {sha256} is not in the blob store")
    chunks, length = opened
    headers["Content-Length"] = str(length)
    return StreamingResponse(chunks, media_type=kind, headers=headers)

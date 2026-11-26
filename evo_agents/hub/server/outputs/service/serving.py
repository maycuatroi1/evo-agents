"""The answer of the bytes of an output, or of an attached file, for a caller who may read them: the Content-Type
``outputs.served_type`` gives, ``nosniff``, the Content-Security-Policy of ``outputs.content_policy`` (HTML and SVG
get an opaque origin and reach nothing), ``Cross-Origin-Resource-Policy: same-origin``, a Content-Disposition with a
safe name (an attachment with ``download=true``, and always for a type no viewer of the web shows), its SHA-256 as
ETag (304 for If-None-Match), and one byte range of a Range header (206, or 416 for one outside the file), so a
video seeks. The bytes stream from the blob store as it reads them; a store that does not answer is 503.

An interactive answer (``outputs.check_interactive``) is served under ``outputs.INTERACTIVE_POLICY`` when ``cdn``
says so (the chat's route, for a chat that is not restricted), else under HTML_POLICY, and kept no longer than a
revalidation (NO_CACHE), so a chat that became restricted takes back the CDNs at once.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from evo_agents.hub import outputs
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.blobs import blob_store

CACHE = "private, max-age=3600"
NO_CACHE = "private, no-cache"  # an interactive answer's policy follows the chat's restriction as it is now


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags


def _headers(row, download: bool, cdn: bool) -> dict[str, str]:
    attachment = download or not outputs.inline(row.media_type)
    interactive = bool(getattr(row, "interactive", False))  # an attachment (``attachments``) is never one
    return {
        "Content-Security-Policy": outputs.content_policy(row.media_type, interactive=cdn and interactive),
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "Content-Disposition": outputs.content_disposition(row.name, attachment),
        "Cache-Control": NO_CACHE if interactive else CACHE,
        "ETag": f'"{row.sha256}"',
        "Accept-Ranges": "bytes",
    }


async def serve(
    request: Request, row, download: bool, asked: str | None, if_none_match: str | None, *, cdn: bool = False
) -> Response:
    """The answer of output ``row``'s bytes (see the module), for a caller who may read them; ``cdn`` serves an
    interactive answer under INTERACTIVE_POLICY, which only the chat's route asks, for a chat that is not restricted."""
    headers = _headers(row, download, cdn)
    if _matches(if_none_match, headers["ETag"]):
        return Response(status_code=304, headers=headers)
    span = outputs.parse_range(asked, row.size)
    if span is False:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{row.size}"})
    first, last = span if isinstance(span, tuple) else (None, None)
    store = blob_store(request)
    try:
        opened = await asyncio.to_thread(store.open_range, row.sha256, first, last)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer; try again shortly") from None
    if opened is None:
        raise NotFound(f"the bytes of output {row.id} are not in the blob store")
    chunks, length = opened
    headers["Content-Length"] = str(length)
    if span:
        headers["Content-Range"] = f"bytes {first}-{last}/{row.size}"
    kind = row.media_type if download else outputs.served_type(row.media_type)
    return StreamingResponse(chunks, status_code=206 if span else 200, media_type=kind, headers=headers)

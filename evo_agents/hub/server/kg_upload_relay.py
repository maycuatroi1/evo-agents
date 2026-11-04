"""The web's way to send a part of an archive (plan kg-upload-archive): PUT
/v1/kg/{project}/uploads/{upload_id}/parts/{number} with the part's bytes as the body.

The CLI PUTs each part to the blob store through a presigned URL (``evo_agents.hub.server.kg_uploads``). A browser
cannot: the web's Content-Security-Policy keeps connect-src at 'self', and the bucket would need a CORS rule for the
hub's origin. So the web sends each part to the hub's own origin, as it sends a file attached to a message, and the hub
relays it to the store as that part of the multipart upload. A part is at most PART_SIZE (64 MiB), below the 100 MB a
proxy such as Cloudflare lets through.

Only the sender of an upload still ``uploading`` sends its parts, each of exactly its size (422 otherwise, 413 past
it). The hub streams the body into a temporary file while it counts it, outside any transaction, so parallel parts
do not wait on one another, then hands the file to the store. Sending a part again replaces it. POST .../complete then
joins the parts as for the CLI. The log carries counts and ids, never a path or a byte of the archive.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel
from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.blobs import MAX_PARTS, BlobStoreUnavailable
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg_uploads import REFUSALS, UploadId, part_sizes
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

PartNumber = Annotated[int, PathParam(ge=1, le=MAX_PARTS, description="the part, numbered from 1")]
BODY = {
    "requestBody": {
        "required": True,
        "description": "the part's bytes, exactly the size the upload gives it",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})


class KgPartSent(BaseModel):
    number: int
    size: int


async def _receive(request: Request, path: FilePath, size: int) -> None:
    """Stream the body into ``path``: 413 past ``size`` bytes, 422 short of it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > size:
        raise HTTPException(413, f"the part has {size} bytes, not {declared}")
    got = 0
    with path.open("wb") as handle:
        async for chunk in request.stream():
            got += len(chunk)
            if got > size:
                raise HTTPException(413, f"the part has {size} bytes, more came")
            handle.write(chunk)
    if got != size:
        raise HTTPException(422, f"the part has {size} bytes, {got} came")


@router.put(
    "/{project}/uploads/{upload_id}/parts/{number}",
    response_model=KgPartSent,
    responses=REFUSALS,
    openapi_extra=BODY,
)
async def send_part(
    request: Request, project: ProjectName, upload_id: UploadId, number: PartNumber, user: CurrentUser
) -> KgPartSent:
    """Relay part ``number`` of an upload in flight to the blob store, for a client that cannot reach the store."""
    store = blob_store(request)
    u = tables.kg_uploads
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = (
            await conn.execute(select(u).where(u.c.id == upload_id, u.c.project_id == access.project_id))
        ).one_or_none()
        if row is None or row.created_by != user.user_id:
            raise HTTPException(404, f"no upload {upload_id} of yours in project {access.name}")
        if row.state != "uploading" or row.multipart_id is None:
            raise HTTPException(409, f"upload {upload_id} is {row.state}, not uploading: start it again")
        sizes = part_sizes(row.size, row.part_size)
        if number not in sizes:
            raise HTTPException(422, f"upload {upload_id} has parts 1 to {len(sizes)}, not {number}")
        await conn.execute(update(u).where(u.c.id == row.id).values(updated_at=func.now()))
    size = sizes[number]
    with tempfile.TemporaryDirectory() as directory:
        path = FilePath(directory) / "part"
        await _receive(request, path, size)
        with path.open("rb") as body:
            try:
                sent = await asyncio.to_thread(store.upload_part, row.object_key, row.multipart_id, number, body, size)
            except BlobStoreUnavailable:
                raise HTTPException(503, "the blob store did not answer (UploadPart); send the part again") from None
    if not sent:
        raise HTTPException(409, f"upload {upload_id} lost its parts in the store: start it again")
    log.info("kg upload part relayed", extra={"project": project, "upload": upload_id, "part": number, "bytes": size})
    return KgPartSent(number=number, size=size)

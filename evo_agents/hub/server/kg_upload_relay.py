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

import logging
import tempfile
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam

from evo_agents.hub.blobs import MAX_PARTS
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.uploads import KgPartSent as KgPartSent
from evo_agents.hub.server.kg.service import upload_relay as service
from evo_agents.hub.server.kg_uploads import REFUSALS, UploadId
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
    row, size = await service.claim(request.app.state.engine, user, project, upload_id, number)
    with tempfile.TemporaryDirectory() as directory:
        path = FilePath(directory) / "part"
        await _receive(request, path, size)
        with path.open("rb") as body:
            await service.relay(store, row, number, body, size)
    log.info("kg upload part relayed", extra={"project": project, "upload": upload_id, "part": number, "bytes": size})
    return KgPartSent(number=number, size=size)

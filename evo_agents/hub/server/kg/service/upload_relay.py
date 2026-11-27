"""The web's way to send a part of an archive (``evo_agents.hub.server.kg_upload_relay``): the check that the sender
may send that part, in a transaction of its own before the body is read, and the hand-off of the received part to
the blob store as that part of the multipart upload."""

from __future__ import annotations

import asyncio
from typing import BinaryIO

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.blobs import BlobStore, BlobStoreUnavailable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.repository import uploads as repository
from evo_agents.hub.server.kg.service.uploads import part_sizes
from evo_agents.hub.server.projects import project_access


async def claim(engine: AsyncEngine, user, project: str, upload_id: int, number: int) -> tuple:
    """The upload ``upload_id`` of the project, which ``user`` sends and which is still uploading, touched, and the
    size of its part ``number``."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        row = await repository.find(conn, access.project_id, upload_id)
        if row is None or row.created_by != user.user_id:
            raise HTTPException(404, f"no upload {upload_id} of yours in project {access.name}")
        if row.state != "uploading" or row.multipart_id is None:
            raise HTTPException(409, f"upload {upload_id} is {row.state}, not uploading: start it again")
        sizes = part_sizes(row.size, row.part_size)
        if number not in sizes:
            raise HTTPException(422, f"upload {upload_id} has parts 1 to {len(sizes)}, not {number}")
        await repository.touch(conn, row.id)
    return row, sizes[number]


async def relay(store: BlobStore, row, number: int, body: BinaryIO, size: int) -> None:
    """Send ``body`` to the store as part ``number`` of the upload ``row``; 503 when the store does not answer, 409
    when it lost the upload's parts."""
    try:
        sent = await asyncio.to_thread(store.upload_part, row.object_key, row.multipart_id, number, body, size)
    except BlobStoreUnavailable:
        raise HTTPException(503, "the blob store did not answer (UploadPart); send the part again") from None
    if not sent:
        raise HTTPException(409, f"upload {row.id} lost its parts in the store: start it again")

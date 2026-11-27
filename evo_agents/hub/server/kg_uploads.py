"""Archives a writer uploads as sources of a project's graph (plan kg-upload-archive): ``evo-agents hub kg upload FILE
--project P [--source NAME] [--label L]`` sends a .zip, .tar, .tar.gz or .tgz of up to EVO_HUB_KG_UPLOAD_MAX_BYTES.

The bytes never pass through the API: they go to the blob store as an S3 multipart upload under ``kg-archives/``
(``evo_agents.hub.blobs``), one presigned URL per part of PART_SIZE, below the 100 MB a proxy such as Cloudflare lets
through. The routes, under /v1/kg/{project}/uploads:

- POST: start an upload, or resume the one in flight for the same archive (project, sender, SHA-256, source). With
  ``new_project`` (``hub kg upload --new-project``) it first creates the project, with no harness, in the same
  transaction (``evo_agents.hub.server.kg_upload_project``). The hub checks the writer role, that the project is not
  archived, the size against the hub's limit (413) and the label against what the hub sink clears (403 or 422, as a
  push), before the client sends any byte of the archive. It answers with the part size, the number of parts and the
  parts the store already has, so a client cut off before sends only the others.
- POST .../{id}/parts: presigned PUTs for some parts, at most MAX_PART_URLS a call, each working for UPLOAD_TTL.
- POST .../{id}/complete: once the store holds every part at its size, it joins them, checks the object's size and
  moves the upload to ``extracting``; 409 naming how many parts are missing otherwise. The hub does not read 20 GiB
  back here: the SHA-256 is checked when the archive is read.
- GET: the uploads of the project the caller's grant sees, newest first, with their state, size, the files read,
  skipped and kept as assets by reason, the first such paths (to a grant that sees the upload's label only), the
  upload that replaced it, and the build that followed. The worker fills these when it reads the archive
  (``evo_agents.hub.kg_upload_extract``).

Only the sender continues an upload. One still ``uploading`` and untouched for STALE_AFTER is marked failed and its
parts dropped by the worker's cleanup job (``evo_agents.hub.kg_upload_prune``).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.uploads import (
    MAX_LISTED,
    KgPartsAsk,
    KgPartUrls,
    KgUpload,
    KgUploadAsk,
    KgUploadList,
    KgUploadStarted,
)
from evo_agents.hub.server.kg.service import uploads as service
from evo_agents.hub.server.kg.service.uploads import default_source as default_source
from evo_agents.hub.server.kg.service.uploads import part_size as part_size
from evo_agents.hub.server.kg.service.uploads import part_sizes as part_sizes
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

UploadId = Annotated[int, PathParam(ge=1, description="the upload, as `hub kg uploads` lists it")]


@router.post("/{project}/uploads", response_model=KgUploadStarted, responses=REFUSALS)
async def start_upload(request: Request, project: ProjectName, ask: KgUploadAsk, user: CurrentUser) -> KgUploadStarted:
    """Start an archive's upload, or resume the one in flight for the same archive: every check before any byte."""
    store = blob_store(request)
    state = request.app.state
    started = await service.start(state.engine, store, user, project, ask, state.config.kg_upload_max_bytes)
    log.info("kg upload started", extra={"project": project, "login": user.login, "bytes": ask.size})
    return started


@router.post("/{project}/uploads/{upload_id}/parts", response_model=KgPartUrls, responses=REFUSALS)
async def part_urls(
    request: Request, project: ProjectName, upload_id: UploadId, ask: KgPartsAsk, user: CurrentUser
) -> KgPartUrls:
    """A presigned PUT for each part asked for, of exactly its size."""
    store = blob_store(request)
    return await service.part_urls(request.app.state.engine, store, user, project, upload_id, ask.numbers)


@router.post("/{project}/uploads/{upload_id}/complete", response_model=KgUpload, responses=REFUSALS)
async def complete_upload(request: Request, project: ProjectName, upload_id: UploadId, user: CurrentUser) -> KgUpload:
    """Join the parts once the store holds each at its size; the upload is then ``extracting``."""
    store = blob_store(request)
    item = await service.complete(request.app.state.engine, store, user, project, upload_id)
    log.info("kg upload complete", extra={"project": project, "login": user.login, "state": item.state})
    return item


@router.get("/{project}/uploads", response_model=KgUploadList, responses=REFUSALS)
async def list_uploads(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 50,
) -> KgUploadList:
    """The project's uploads the caller's grant sees, newest first."""
    return await service.listing(request.app.state.engine, user, project, limit)

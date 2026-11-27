"""Blob uploads: a client asks for presigned PUT URLs, PUTs the bytes to R2, then commits; the hub hashes what came.

POST /v1/blobs/uploads {project, items: [{sha256, size, kind}]} needs the writer role on the project. The blobs the
project holds already come back under ``present`` and get no URL. A blob that only other projects hold must be
uploaded and checked again, so knowing a hash gives nobody another project's blob. Every item is held against the
size limit of its kind before any URL is issued, and one item over its limit refuses the whole request with 413.
Each URL is a presigned PUT to ``uploads/<upload_id>`` that works for 15 minutes and for the declared size only.

POST /v1/blobs/commit {project, upload_ids} reads every upload back (see ``evo_agents.hub.blobs``) and compares its size
and SHA-256 with what was declared. When all of them match, each is copied to ``blobs/sha256/<sha256>`` unless the hub
records that blob already and its object exists, and one ``blobs`` row per blob is written with an audit row naming the
project. The rows are written holding the blob lock shared, and a blob the hub was deleting meanwhile (a built graph the
retention dropped, ``evo_agents.hub.blob_gc``) is copied again first. The time a commit takes grows with its uploads (a
few round trips to the store each, at most the store's ``concurrency`` at a time), so a client commits in batches;
``evo-agents hub kg push`` sends at most ``evo_agents.hub.kg_push.COMMIT_BATCH`` uploads per commit. When one does not
match, the answer is 422 naming it and nothing is written: every upload of the request is discarded and has to be asked
for again. Either way the uploaded objects are deleted. An upload can be committed by the user who asked for it, in its
project, within 24 hours; any other id is unknown (422) and left alone. When the blob store does not answer, the answer
is 503 and the uploads stay, so the same commit can be sent again.

A request without ``project`` is about blobs the hub holds itself, outside any project: the bundles of global skills
and of plugins, which belong to no project. Only a hub admin uploads or commits those, and only of kind skill-bundle or
plugin-bundle (GLOBAL_KINDS); logs and the audit trail name that holder GLOBAL.

``issue_uploads`` and ``commit_uploads`` are the two halves on their own, for routes that take uploads as part of
something larger, such as the worker's log and diff of a run (``run_events``). No route here reads a blob: a route
that has checked that the caller may see what refers to a blob hands out ``BlobStore.presign_get``, or reads it with
``BlobStore.fetch``. ``put_bytes`` and ``fetch_bytes`` store and read the small images the hub takes in JSON (an
agent's avatar, the branding's logo, a chat's image) without an upload.
"""

from __future__ import annotations

import io
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from evo_agents.hub.blobs import BlobStore, Upload, blob_key
from evo_agents.hub.server.blob_store.models.uploads import GLOBAL as GLOBAL
from evo_agents.hub.server.blob_store.models.uploads import GLOBAL_KINDS as GLOBAL_KINDS
from evo_agents.hub.server.blob_store.models.uploads import MAX_ITEMS as MAX_ITEMS
from evo_agents.hub.server.blob_store.models.uploads import UNAVAILABLE as UNAVAILABLE
from evo_agents.hub.server.blob_store.models.uploads import Blob as Blob
from evo_agents.hub.server.blob_store.models.uploads import CommitRequest, OverLimit, UploadRequest
from evo_agents.hub.server.blob_store.models.uploads import Committed as Committed
from evo_agents.hub.server.blob_store.models.uploads import Holder as Holder
from evo_agents.hub.server.blob_store.models.uploads import Mismatch as Mismatch
from evo_agents.hub.server.blob_store.models.uploads import ProjectField as ProjectField
from evo_agents.hub.server.blob_store.models.uploads import UploadItem as UploadItem
from evo_agents.hub.server.blob_store.models.uploads import Uploads as Uploads
from evo_agents.hub.server.blob_store.models.uploads import UploadTicket as UploadTicket
from evo_agents.hub.server.blob_store.service import uploads as service
from evo_agents.hub.server.blob_store.service.uploads import holder as holder
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

UPLOAD_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422, 503)}
COMMIT_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 422, 503)}

router = APIRouter(prefix="/v1/blobs", tags=["blobs"], responses={401: {"model": ErrorBody}})


def blob_store(request: Request) -> BlobStore:
    """The hub's blob store; 503 naming the variables when it is not configured."""
    return service.configured(request.app.state.blobs, request.app.state.config)


def put_bytes(store: BlobStore, sha256: str, data: bytes, *, replace: bool = False) -> None:
    """Store ``data``, whose SHA-256 the caller computed, as blob ``sha256`` unless it is there already; with
    ``replace``, whether it is there or not. Blocking: run it in a thread."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "blob"
        path.write_bytes(data)
        store.put_file(sha256, path, replace=replace)


def fetch_bytes(store: BlobStore, sha256: str, limit: int) -> bytes | None:
    """The bytes of blob ``sha256`` when it is there, at most ``limit`` of them and with that very SHA-256; None
    otherwise. Blocking: run it in a thread."""
    sink = io.BytesIO()
    found = store.fetch(blob_key(sha256), sink, limit)
    if found is None or found[0] != sha256:
        return None
    return sink.getvalue()


@router.post("/uploads", response_model=Uploads, responses=UPLOAD_REFUSALS)
async def request_uploads(request: Request, body: UploadRequest, user: CurrentUser) -> Uploads:
    """Presigned PUT URLs for the blobs of ``items`` that the project does not hold yet."""
    return await issue_uploads(request, user, body.project, body.items)  # pyright: ignore[reportReturnType]


async def issue_uploads(
    request: Request, user: Principal, project: str | None, items: list[UploadItem]
) -> Uploads | JSONResponse:
    """What POST /v1/blobs/uploads answers, for routes that take uploads as part of something larger: the uploads of
    ``items`` of ``project`` (None: of the hub itself) as ``user``, or the 413 response when one is over its limit.
    Raises the HTTPException of ``holder`` and the blob store."""
    store = blob_store(request)
    issued = await service.issue_uploads(request.app.state.engine, store, user, project, items)
    if isinstance(issued, OverLimit):
        return error_response(request, 413, issued.message, detail=issued.detail)
    return issued


async def commit_uploads(
    request: Request,
    user: Principal,
    project: str | None,
    upload_ids: list[str],
    *,
    kinds: frozenset[str] | None = None,
    inspect: Callable[[list[Upload]], Awaitable[None]] | None = None,
) -> Committed:
    """Verify and commit ``upload_ids`` of ``project`` (None: of the hub itself) as ``user``. Raises HTTPException
    (403, 404, 422 for unknown ids, 503), or Mismatch after discarding every upload of the request when one does not
    match.

    ``kinds`` limits the uploads to those kinds (another is unknown). ``inspect`` is awaited once every upload is
    sealed and matches, before anything is published: it may read the sealed copies (``sealed_key``), and whatever it
    raises discards every upload of the request, rows and objects, and goes to the caller."""
    store = blob_store(request)
    engine = request.app.state.engine
    return await service.commit_uploads(engine, store, user, project, upload_ids, kinds=kinds, inspect=inspect)


@router.post("/commit", response_model=Committed, responses=COMMIT_REFUSALS)
async def commit(request: Request, body: CommitRequest, user: CurrentUser):
    """Check the uploads against what was declared and record the blobs in the project."""
    try:
        return await commit_uploads(request, user, body.project, [str(upload_id) for upload_id in body.upload_ids])
    except Mismatch as exc:
        message = (
            f"{len(exc.problems)} upload(s) do not match what was declared, such as {exc.problems[0]['upload_id']}: "
            f"{exc.problems[0]['problem']}. Nothing was committed and every upload of the request was discarded; "
            "ask for new uploads"
        )
        return error_response(request, 422, message, detail=exc.problems)

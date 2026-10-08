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

A request without ``project`` is about blobs the hub holds itself, outside any project: the bundles of global skills,
which belong to no project. Only a hub admin uploads or commits those, and only of kind skill-bundle (GLOBAL_KINDS);
logs and the audit trail name that holder GLOBAL.

``issue_uploads`` and ``commit_uploads`` are the two halves on their own, for routes that take uploads as part of
something larger, such as the worker's log and diff of a run (``run_events``). No route here reads a blob: a route
that has checked that the caller may see what refers to a blob hands out ``BlobStore.presign_get``, or reads it with
``BlobStore.fetch``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, Field
from sqlalchemy import BigInteger, Text, Uuid, any_, cast, column, delete, func, insert, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import (
    KIND_LIMITS,
    STALE_AFTER,
    UPLOAD_TTL,
    BlobStore,
    BlobStoreUnavailable,
    Upload,
    new_upload_id,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_ITEMS = 1000
UPLOAD_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 413, 422, 503)}
COMMIT_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 422, 503)}
UNAVAILABLE = "the blob store did not answer; nothing was committed and the uploads are kept, try again shortly"
GLOBAL = "(global)"  # the hub as the holder of blobs in logs and audit rows; no project name has parentheses
GLOBAL_KINDS = frozenset({"skill-bundle"})  # what the hub holds outside any project: the bundles of global skills

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="hex SHA-256 of the bytes")]
ProjectField = Annotated[str, Field(pattern=PROJECT_NAME)]

router = APIRouter(prefix="/v1/blobs", tags=["blobs"], responses={401: {"model": ErrorBody}})


class UploadItem(BaseModel):
    sha256: Sha256
    size: int = Field(ge=0, description="bytes, at most the limit of the kind")
    kind: Literal[tuple(KIND_LIMITS)]


class UploadRequest(BaseModel):
    project: ProjectField | None = Field(
        None, description="left out for blobs the hub holds itself (bundles of global skills; a hub admin only)"
    )
    items: list[UploadItem] = Field(min_length=1, max_length=MAX_ITEMS)


class UploadTicket(BaseModel):
    sha256: str
    upload_id: str
    url: str = Field(description="presigned PUT: send exactly `size` bytes as application/octet-stream")


class Uploads(BaseModel):
    uploads: list[UploadTicket]
    present: list[str] = Field(description="blobs the project holds already, with nothing to upload")
    expires_at: datetime = Field(description="when the URLs stop working")


class CommitRequest(BaseModel):
    project: ProjectField | None = Field(None, description="left out for blobs the hub holds itself")
    upload_ids: list[UUID4] = Field(min_length=1, max_length=MAX_ITEMS)


class Blob(BaseModel):
    sha256: str
    size: int
    kind: str


class Committed(BaseModel):
    blobs: list[Blob]
    added: int = Field(description="blobs new to the project; it held the others already")


def blob_store(request: Request) -> BlobStore:
    """The hub's blob store; 503 naming the variables when it is not configured."""
    store = request.app.state.blobs
    if store is None:
        missing = ", ".join(request.app.state.config.blob_store_missing())
        raise HTTPException(503, f"the blob store is not configured on this hub: set {missing}")
    return store


def _writer(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "writer"):
        raise HTTPException(403, f"{doing} blobs of project {access.name} needs the writer role on it")


@dataclass(frozen=True)
class Holder:
    """What holds a blob: a project, or the hub itself (``project_id`` None) for the bundles of global skills."""

    project_id: int | None
    name: str  # the project's, or GLOBAL


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


def _over_limit(request: Request, items: list[UploadItem]) -> JSONResponse | None:
    over = [item for item in items if item.size > KIND_LIMITS[item.kind]]
    if not over:
        return None
    detail = [{"sha256": i.sha256, "kind": i.kind, "size": i.size, "limit": KIND_LIMITS[i.kind]} for i in over]
    first = over[0]
    message = (
        f"{len(over)} blob(s) exceed the size limit of their kind, such as {first.sha256} of kind {first.kind} with "
        f"{first.size} bytes over {KIND_LIMITS[first.kind]}; no upload was issued"
    )
    return error_response(request, 413, message, detail=detail)


@router.post("/uploads", response_model=Uploads, responses=UPLOAD_REFUSALS)
async def request_uploads(request: Request, body: UploadRequest, user: CurrentUser) -> Uploads:
    """Presigned PUT URLs for the blobs of ``items`` that the project does not hold yet."""
    return await issue_uploads(request, user, body.project, body.items)


async def issue_uploads(
    request: Request, user: Principal, project: str | None, items: list[UploadItem]
) -> Uploads | JSONResponse:
    """What POST /v1/blobs/uploads answers, for routes that take uploads as part of something larger: the uploads of
    ``items`` of ``project`` (None: of the hub itself) as ``user``, or the 413 response when one is over its limit.
    Raises the HTTPException of ``holder`` and the blob store."""
    store = blob_store(request)
    items = _distinct(items)
    async with request.app.state.engine.begin() as conn:
        access = await holder(conn, user, project, "uploading")
        if access.project_id is None and any(item.kind not in GLOBAL_KINDS for item in items):
            raise HTTPException(
                422, f"outside a project the hub holds only blobs of kind {', '.join(sorted(GLOBAL_KINDS))}"
            )
        refusal = _over_limit(request, items)
        if refusal is not None:
            return refusal
        blobs = tables.blobs
        held = select(blobs.c.sha256).where(
            blobs.c.project_id.is_not_distinct_from(access.project_id),
            blob_gc.hashes_in(blobs.c.sha256, [item.sha256 for item in items]),
        )
        present = set((await conn.execute(held)).scalars())
        needed = [
            Upload(new_upload_id(), item.sha256, item.size, item.kind) for item in items if item.sha256 not in present
        ]
        if needed:
            await conn.execute(
                insert(tables.blob_uploads),
                [
                    {
                        "upload_id": u.upload_id,
                        "project_id": access.project_id,
                        "sha256": u.sha256,
                        "size": u.size,
                        "kind": u.kind,
                        "created_by": user.user_id,
                    }
                    for u in needed
                ],
            )
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


async def _discard(store: BlobStore, upload_ids: list[str]) -> None:
    """Delete the objects of these uploads; what a failure leaves behind, the worker removes after 24 hours."""
    try:
        await asyncio.to_thread(store.discard, upload_ids)
    except BlobStoreUnavailable:
        log.warning("uploaded objects not deleted; the hourly cleanup removes them", extra={"uploads": len(upload_ids)})


class Mismatch(Exception):
    """Uploads whose bytes are not what was declared; their objects and rows are gone already."""

    def __init__(self, problems: list[dict]):
        super().__init__(f"{len(problems)} upload(s) do not match what was declared")
        self.problems = problems


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
    ids = sorted(set(upload_ids))
    async with engine.begin() as conn:
        access = await holder(conn, user, project, "committing")
        rows = (await conn.execute(_pending(ids, access.project_id, user.user_id))).all()
        uploads = [Upload(**row._mapping) for row in rows if kinds is None or row.kind in kinds]
    unknown = sorted(set(ids) - {upload.upload_id for upload in uploads})
    if unknown:
        raise HTTPException(
            422,
            f"unknown uploads {', '.join(unknown)}: an upload is committed once, in its project, by whoever asked "
            "for it, within 24 hours; nothing was committed",
        )
    try:
        verdicts = await asyncio.to_thread(store.seal_all, uploads)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    problems = [
        {"upload_id": v.upload.upload_id, "sha256": v.upload.sha256, "problem": v.problem}
        for v in verdicts
        if v.problem
    ]
    if problems:
        async with engine.begin() as conn:
            await conn.execute(delete(tables.blob_uploads).where(_uploads_in(ids)))
        await _discard(store, ids)
        log.warning(
            "blob commit refused: uploads do not match",
            extra={"project": access.name, "login": user.login, "uploads": len(ids), "mismatched": problems},
        )
        raise Mismatch(problems)
    if inspect is not None:
        try:
            await inspect(uploads)
        except BlobStoreUnavailable:
            raise HTTPException(503, UNAVAILABLE) from None  # the uploads stay, so the same commit can be sent again
        except Exception:
            async with engine.begin() as conn:
                await conn.execute(delete(tables.blob_uploads).where(_uploads_in(ids)))
            await _discard(store, ids)
            raise
    async with engine.begin() as conn:  # the blobs the hub records already: only those are looked for first
        sha256 = tables.blobs.c.sha256
        recorded = select(sha256).distinct().where(blob_gc.hashes_in(sha256, [upload.sha256 for upload in uploads]))
        held = frozenset((await conn.execute(recorded)).scalars())
    try:
        written = await asyncio.to_thread(store.publish_all, uploads, held)
    except BlobStoreUnavailable:
        raise HTTPException(503, UNAVAILABLE) from None
    async with engine.begin() as conn:
        access = await holder(conn, user, project, "committing")  # the grant may have changed while the bytes were read
        blobs = list({upload.sha256: upload for upload in reversed(uploads)}.values())  # the first upload of a hash
        # A blob found held above may have lost its object to a deletion since (evo_agents.hub.blob_gc): copy those
        # sealed bytes again, while the lock keeps any deletion out until the rows are written.
        await blob_gc.lock_shared(conn)
        revived = set(await blob_gc.revive(conn, [b.sha256 for b in blobs]))
        if revived:
            try:
                await asyncio.to_thread(store.publish_all, [b for b in blobs if b.sha256 in revived])
            except BlobStoreUnavailable:
                raise HTTPException(503, UNAVAILABLE) from None
        added = len((await conn.execute(_recorded(access.project_id, user.user_id, blobs))).all())
        await conn.execute(delete(tables.blob_uploads).where(_uploads_in(ids)))
        if added:
            await audit.record(
                conn, actor_id=user.user_id, token_id=user.token_id, action=audit.BLOB_COMMIT, target=access.name
            )
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

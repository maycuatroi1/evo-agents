"""Archives a writer uploads as sources of a project's graph (plan kg-upload-archive): ``evo-agents hub kg upload FILE
--project P [--source NAME] [--label L]`` sends a .zip, .tar, .tar.gz or .tgz of up to EVO_HUB_KG_UPLOAD_MAX_BYTES.

The bytes never pass through the API: they go to the blob store as an S3 multipart upload under ``kg-archives/``
(``evo_agents.hub.blobs``), one presigned URL per part of PART_SIZE, below the 100 MB a proxy such as Cloudflare lets
through. The routes, under /v1/kg/{project}/uploads:

- POST: start an upload, or resume the one in flight for the same archive (project, sender, SHA-256, source). The
  hub checks the writer role, that the project is not archived, the size against the hub's limit (413) and the label
  against what the hub sink clears (403 or 422, as a push), before the client sends any byte of the archive. It
  answers with the part size, the number of parts and the parts the store already has, so a client cut off before
  sends only the others.
- POST .../{id}/parts: presigned PUTs for some parts, at most MAX_PART_URLS a call, each working for UPLOAD_TTL.
- POST .../{id}/complete: once the store holds every part at its size, it joins them, checks the object's size and
  moves the upload to ``extracting``; 409 naming how many parts are missing otherwise. The hub does not read 20 GiB
  back here: the SHA-256 is checked when the archive is read.
- GET: the uploads of the project the caller's grant sees, newest first, with their state, size, the files read and
  skipped by reason (``report``) and the build that followed.

Only the sender continues an upload. One still ``uploading`` and untouched for STALE_AFTER is marked failed and its
parts dropped by the worker's cleanup job (``evo_agents.hub.kg_upload_prune``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blobs as blob_store_module
from evo_agents.hub import tables
from evo_agents.hub.blobs import (
    MAX_PARTS,
    BlobStore,
    BlobStoreUnavailable,
    archive_key,
    new_upload_id,
)
from evo_agents.hub.server import audit, project_archive
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

SUFFIXES = (".tar.gz", ".tgz", ".zip", ".tar")  # the longer first: a name ending in .tar.gz is not a .gz
MAX_PART_URLS = 100  # presigned URLs one call hands out
MAX_LISTED = 200
SOURCE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="hex SHA-256 of the whole archive")]
SourceName = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f/]+$")]
UploadId = Annotated[int, PathParam(ge=1, description="the upload, as `hub kg uploads` lists it")]


class KgUploadAsk(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f/\\]+$", description="the file's name")
    size: int = Field(ge=1, description="bytes")
    sha256: Sha256
    source: SourceName | None = Field(None, description="the source the archive becomes; from its name without one")
    label: str | None = Field(
        None, min_length=1, max_length=100, description="a level of the project; its default label without one"
    )


class KgUploadStarted(BaseModel):
    id: int
    source: str
    state: str
    size: int
    part_size: int
    parts: int = Field(description="how many parts the archive is sent in")
    done: list[int] = Field(description="the parts the store already holds, which the client does not send again")
    resumed: bool = Field(description="an upload of the same archive was in flight and continues")
    max_size: int = Field(description="the largest archive this hub takes")


class KgPartsAsk(BaseModel):
    numbers: list[int] = Field(min_length=1, max_length=MAX_PART_URLS)


class KgPartUrl(BaseModel):
    number: int
    size: int
    url: str


class KgPartUrls(BaseModel):
    parts: list[KgPartUrl]


class KgUpload(BaseModel):
    id: int
    source: str
    name: str
    state: str
    size: int
    label: dict
    sender: str | None
    created_at: datetime
    uploaded_at: datetime | None
    ended_at: datetime | None
    files: int | None = Field(description="files read from the archive; None until it is read")
    skipped: dict[str, int] = Field(description="files left out, counted by reason")
    build_id: int | None
    build_status: str | None
    error: str | None


class KgUploadList(BaseModel):
    uploads: list[KgUpload]


def default_source(name: str) -> str:
    """The source an archive becomes without --source: its name without the suffix, in letters, digits and ``._-``."""
    stem = name
    for suffix in SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return SOURCE_CHARS.sub("-", stem).strip("-.")[:200] or "archive"


def part_size(size: int) -> int:
    """The size of each part but the last: PART_SIZE, or more when the archive would need more than MAX_PARTS."""
    base = blob_store_module.PART_SIZE
    return max(base, -(-size // MAX_PARTS))


def part_sizes(size: int, each: int) -> dict[int, int]:
    """{part number: its size}, numbered from 1."""
    count = -(-size // each)
    return {n: min(each, size - (n - 1) * each) for n in range(1, count + 1)}


async def _in_thread(operation: str, func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except BlobStoreUnavailable:
        raise HTTPException(503, f"the blob store did not answer ({operation}); try again shortly") from None


def _held(listed: dict[int, tuple[int, str]] | None, sizes: dict[int, int]) -> dict[int, str]:
    """{number: ETag} of the parts the store holds at the size expected."""
    return {n: etag for n, (size, etag) in (listed or {}).items() if sizes.get(n) == size}


def _ask_checks(request: Request, ask: KgUploadAsk) -> None:
    if not ask.name.lower().endswith(SUFFIXES):
        raise HTTPException(422, f"{ask.name!r} is not an archive the hub reads: {', '.join(sorted(SUFFIXES))}")
    limit = request.app.state.config.kg_upload_max_bytes
    if ask.size > limit:
        raise HTTPException(
            413, f"the archive has {ask.size} bytes; this hub takes at most {limit} (EVO_HUB_KG_UPLOAD_MAX_BYTES)"
        )


async def _resume(conn: AsyncConnection, store: BlobStore, row, label: dict) -> tuple[list[int], bool]:
    """The parts of the upload in flight ``row`` the store holds; a multipart upload the store lost starts again."""
    sizes = part_sizes(row.size, row.part_size)
    listed = await _in_thread("ListParts", store.list_parts, row.object_key, row.multipart_id)
    values = {"label": label, "updated_at": func.now()}
    if listed is None:
        values["multipart_id"] = await _in_thread("CreateMultipartUpload", store.create_multipart, row.object_key)
    await conn.execute(update(tables.kg_uploads).where(tables.kg_uploads.c.id == row.id).values(values))
    return sorted(_held(listed, sizes)), True


@router.post("/{project}/uploads", response_model=KgUploadStarted, responses=REFUSALS)
async def start_upload(request: Request, project: ProjectName, ask: KgUploadAsk, user: CurrentUser) -> KgUploadStarted:
    """Start an archive's upload, or resume the one in flight for the same archive: every check before any byte."""
    store = blob_store(request)
    _ask_checks(request, ask)
    source = ask.source or default_source(ask.name)
    u = tables.kg_uploads
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.begin() as conn:
            access = await project_access(conn, user, project)
            await project_archive.refuse_archived(conn, access.project_id, "it takes no kg upload")
            label = access.push_label(None if ask.label is None else {"level": ask.label})
            each = part_size(ask.size)
            found = (
                await conn.execute(
                    select(u).where(
                        u.c.project_id == access.project_id,
                        u.c.created_by == user.user_id,
                        u.c.sha256 == ask.sha256,
                        u.c.source == source,
                        u.c.state == "uploading",
                    )
                )
            ).one_or_none()
            if found is not None and found.size == ask.size:
                done, resumed = await _resume(conn, store, found, label)
                upload_id, each = found.id, found.part_size
            else:
                if found is not None:  # the same hash with another size: a client's mistake, not this archive
                    raise HTTPException(409, f"upload {found.id} of this archive declared {found.size} bytes")
                key = archive_key(new_upload_id())
                multipart = await _in_thread("CreateMultipartUpload", store.create_multipart, key)
                row = {
                    "project_id": access.project_id,
                    "source": source,
                    "label": label,
                    "name": ask.name,
                    "size": ask.size,
                    "sha256": ask.sha256,
                    "part_size": each,
                    "object_key": key,
                    "multipart_id": multipart,
                    "state": "uploading",
                    "created_by": user.user_id,
                }
                upload_id = (await conn.execute(u.insert().values(row).returning(u.c.id))).scalar_one()
                done, resumed = [], False
    except IntegrityError:
        raise HTTPException(409, "another call is starting this upload; run the command again") from None
    log.info("kg upload started", extra={"project": project, "login": user.login, "bytes": ask.size})
    return KgUploadStarted(
        id=upload_id,
        source=source,
        state="uploading",
        size=ask.size,
        part_size=each,
        parts=len(part_sizes(ask.size, each)),
        done=done,
        resumed=resumed,
        max_size=request.app.state.config.kg_upload_max_bytes,
    )


async def _own(conn: AsyncConnection, access: ProjectAccess, user, upload_id: int):
    """The upload ``upload_id`` of the project, locked, which ``user`` sends and which is still uploading."""
    u = tables.kg_uploads
    row = (
        await conn.execute(select(u).where(u.c.id == upload_id, u.c.project_id == access.project_id).with_for_update())
    ).one_or_none()
    if row is None or row.created_by != user.user_id:
        raise HTTPException(404, f"no upload {upload_id} of yours in project {access.name}")
    if row.state != "uploading":
        raise HTTPException(409, f"upload {upload_id} is {row.state}, not uploading: start it again")
    return row


@router.post("/{project}/uploads/{upload_id}/parts", response_model=KgPartUrls, responses=REFUSALS)
async def part_urls(
    request: Request, project: ProjectName, upload_id: UploadId, ask: KgPartsAsk, user: CurrentUser
) -> KgPartUrls:
    """A presigned PUT for each part asked for, of exactly its size."""
    store = blob_store(request)
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = await _own(conn, access, user, upload_id)
        sizes = part_sizes(row.size, row.part_size)
        wrong = sorted(set(ask.numbers) - set(sizes))
        if wrong:
            raise HTTPException(422, f"upload {upload_id} has parts 1 to {len(sizes)}, not {wrong[0]}")
        u = tables.kg_uploads
        await conn.execute(update(u).where(u.c.id == row.id).values(updated_at=func.now()))
    urls = [
        KgPartUrl(number=n, size=sizes[n], url=store.presign_part(row.object_key, row.multipart_id, n, sizes[n]))
        for n in sorted(set(ask.numbers))
    ]
    return KgPartUrls(parts=urls)


@router.post("/{project}/uploads/{upload_id}/complete", response_model=KgUpload, responses=REFUSALS)
async def complete_upload(request: Request, project: ProjectName, upload_id: UploadId, user: CurrentUser) -> KgUpload:
    """Join the parts once the store holds each at its size; the upload is then ``extracting``."""
    store = blob_store(request)
    u = tables.kg_uploads
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        row = await _own(conn, access, user, upload_id)
        sizes = part_sizes(row.size, row.part_size)
        listed = await _in_thread("ListParts", store.list_parts, row.object_key, row.multipart_id)
        held = _held(listed, sizes)
        missing = len(sizes) - len(held)
        if missing:
            raise HTTPException(
                409, f"upload {upload_id} lacks {missing} of its {len(sizes)} parts: run the command again to send them"
            )
        joined = await _in_thread(
            "CompleteMultipartUpload", store.complete_multipart, row.object_key, row.multipart_id, held
        )
        size = await _in_thread("HeadObject", store.size, row.object_key) if joined else None
        values = {"multipart_id": None, "updated_at": func.now()}
        if size != row.size:
            values |= {"state": "failed", "ended_at": func.now(), "error": f"the store holds {size} bytes"}
        else:
            values |= {"state": "extracting", "uploaded_at": func.now()}
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.KG_UPLOAD,
                target=f"{project} source={row.source} upload={row.id} bytes={row.size}",
                project_id=access.project_id,
            )
        await conn.execute(update(u).where(u.c.id == row.id).values(values))
        item = (await _items(conn, access, upload_id=row.id))[0]
    log.info("kg upload complete", extra={"project": project, "login": user.login, "state": item.state})
    return item


async def _items(conn: AsyncConnection, access: ProjectAccess, *, upload_id: int | None = None, limit: int = 1):
    u, users, builds = tables.kg_uploads, tables.users, tables.kg_builds
    query = (
        select(u, users.c.login, builds.c.status.label("build_status"))
        .select_from(u.outerjoin(users, users.c.id == u.c.created_by).outerjoin(builds, builds.c.id == u.c.build_id))
        .where(u.c.project_id == access.project_id)
        .order_by(u.c.id.desc())
        .limit(limit)
    )
    if upload_id is not None:
        query = query.where(u.c.id == upload_id)
    items = []
    for row in (await conn.execute(query)).all():
        report = row.report or {}
        items.append(
            KgUpload(
                id=row.id,
                source=row.source,
                name=row.name,
                state=row.state,
                size=row.size,
                label=row.label,
                sender=row.login,
                created_at=row.created_at,
                uploaded_at=row.uploaded_at,
                ended_at=row.ended_at,
                files=report.get("files"),
                skipped={str(k): int(v) for k, v in (report.get("skipped") or {}).items()},
                build_id=row.build_id,
                build_status=row.build_status,
                error=row.error,
            )
        )
    return items


@router.get("/{project}/uploads", response_model=KgUploadList, responses=REFUSALS)
async def list_uploads(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 50,
) -> KgUploadList:
    """The project's uploads the caller's grant sees, newest first."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"listing the uploads of project {project} needs a grant on it")
        items = await _items(conn, access, limit=limit)
    return KgUploadList(uploads=[i for i in items if access.rules.visible_by_grant(i.label, access.max_level)])

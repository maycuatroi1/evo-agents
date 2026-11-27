"""The archives a writer uploads as sources of a project's graph (``evo_agents.hub.server.kg_uploads``): the checks
before any byte, the parts and their sizes, and the one transaction of each call, which also talks to the blob store
(``evo_agents.hub.blobs``) in a thread. The routes pass in the store and the hub's size limit."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import blobs as blob_store_module
from evo_agents.hub.blobs import MAX_PARTS, BlobStore, BlobStoreUnavailable, archive_key, new_upload_id
from evo_agents.hub.server import audit, kg_upload_project, project_archive
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.uploads import (
    SOURCE_CHARS,
    SUFFIXES,
    KgPartUrl,
    KgPartUrls,
    KgUpload,
    KgUploadAsk,
    KgUploadList,
    KgUploadPath,
    KgUploadStarted,
)
from evo_agents.hub.server.kg.repository import uploads as repository
from evo_agents.hub.server.projects import ProjectAccess, project_access


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


def _ask_checks(ask: KgUploadAsk, limit: int) -> None:
    if not ask.name.lower().endswith(SUFFIXES):
        raise HTTPException(422, f"{ask.name!r} is not an archive the hub reads: {', '.join(sorted(SUFFIXES))}")
    if ask.size > limit:
        raise HTTPException(
            413, f"the archive has {ask.size} bytes; this hub takes at most {limit} (EVO_HUB_KG_UPLOAD_MAX_BYTES)"
        )


async def _resume(conn: AsyncConnection, store: BlobStore, row, label: dict) -> tuple[list[int], bool]:
    """The parts of the upload in flight ``row`` the store holds; a multipart upload the store lost starts again."""
    sizes = part_sizes(row.size, row.part_size)
    listed = await _in_thread("ListParts", store.list_parts, row.object_key, row.multipart_id)
    values: dict = {"label": label}
    if listed is None:
        values["multipart_id"] = await _in_thread("CreateMultipartUpload", store.create_multipart, row.object_key)
    await repository.touch(conn, row.id, **values)
    return sorted(_held(listed, sizes)), True


async def _begin(
    conn: AsyncConnection, store: BlobStore, user, access: ProjectAccess, ask: KgUploadAsk, source: str, label: dict
) -> int:
    """A new upload of the archive, with a multipart upload of its own in the store; its id."""
    key = archive_key(new_upload_id())
    multipart = await _in_thread("CreateMultipartUpload", store.create_multipart, key)
    row = {
        "project_id": access.project_id,
        "source": source,
        "label": label,
        "name": ask.name,
        "size": ask.size,
        "sha256": ask.sha256,
        "part_size": part_size(ask.size),
        "object_key": key,
        "multipart_id": multipart,
        "state": "uploading",
        "created_by": user.user_id,
    }
    return await repository.insert(conn, row)


async def start(engine: AsyncEngine, store: BlobStore, user, project: str, ask: KgUploadAsk, limit: int):
    """Start an archive's upload, or resume the one in flight for the same archive, every check before any byte;
    ``limit`` is the largest archive the hub takes."""
    _ask_checks(ask, limit)
    source = ask.source or default_source(ask.name)
    try:
        async with UnitOfWork(engine) as conn:
            if ask.new_project:
                await kg_upload_project.create(conn, user, project, ask.label)
            access = await project_access(conn, user, project)
            await project_archive.refuse_archived(conn, access.project_id, "it takes no kg upload")
            label = access.push_label(None if ask.label is None else {"level": ask.label})
            each = part_size(ask.size)
            found = await repository.in_flight(conn, access.project_id, user.user_id, ask.sha256, source)
            if found is not None and found.size == ask.size:
                done, resumed = await _resume(conn, store, found, label)
                upload_id, each = found.id, found.part_size
            else:
                if found is not None:  # the same hash with another size: a client's mistake, not this archive
                    raise HTTPException(409, f"upload {found.id} of this archive declared {found.size} bytes")
                upload_id = await _begin(conn, store, user, access, ask, source, label)
                done, resumed = [], False
    except IntegrityError:
        raise HTTPException(409, "another call is starting this upload; run the command again") from None
    return KgUploadStarted(
        id=upload_id,
        source=source,
        state="uploading",
        size=ask.size,
        part_size=each,
        parts=len(part_sizes(ask.size, each)),
        done=done,
        resumed=resumed,
        max_size=limit,
    )


async def _own(conn: AsyncConnection, access: ProjectAccess, user, upload_id: int):
    """The upload ``upload_id`` of the project, locked, which ``user`` sends and which is still uploading."""
    row = await repository.find(conn, access.project_id, upload_id, lock=True)
    if row is None or row.created_by != user.user_id:
        raise HTTPException(404, f"no upload {upload_id} of yours in project {access.name}")
    if row.state != "uploading":
        raise HTTPException(409, f"upload {upload_id} is {row.state}, not uploading: start it again")
    return row


async def part_urls(
    engine: AsyncEngine, store: BlobStore, user, project: str, upload_id: int, numbers: list[int]
) -> KgPartUrls:
    """A presigned PUT for each part asked for, of exactly its size."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        row = await _own(conn, access, user, upload_id)
        sizes = part_sizes(row.size, row.part_size)
        wrong = sorted(set(numbers) - set(sizes))
        if wrong:
            raise HTTPException(422, f"upload {upload_id} has parts 1 to {len(sizes)}, not {wrong[0]}")
        await repository.touch(conn, row.id)
    urls = [
        KgPartUrl(number=n, size=sizes[n], url=store.presign_part(row.object_key, row.multipart_id, n, sizes[n]))
        for n in sorted(set(numbers))
    ]
    return KgPartUrls(parts=urls)


async def complete(engine: AsyncEngine, store: BlobStore, user, project: str, upload_id: int) -> KgUpload:
    """Join the parts once the store holds each at its size; the upload is then ``extracting``."""
    async with UnitOfWork(engine) as conn:
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
        if size != row.size:
            await repository.joined(conn, row.id, f"the store holds {size} bytes")
        else:
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.KG_UPLOAD,
                target=f"{project} source={row.source} upload={row.id} bytes={row.size}",
                project_id=access.project_id,
            )
            await repository.joined(conn, row.id, None)
        return (await _items(conn, access, upload_id=row.id))[0]


async def _items(conn: AsyncConnection, access: ProjectAccess, *, upload_id: int | None = None, limit: int = 1):
    items = []
    for row in await repository.listed(conn, access.project_id, upload_id=upload_id, limit=limit):
        report = row.report or {}
        seen = access.rules.visible_by_grant(row.label, access.max_level)
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
                assets={str(k): int(v) for k, v in (report.get("assets") or {}).items()},
                paths=[KgUploadPath(**entry) for entry in report.get("paths") or []] if seen else [],
                replaced_by=report.get("replaced_by"),
                build_id=row.build_id,
                build_status=row.build_status,
                error=row.error,
            )
        )
    return items


async def listing(engine: AsyncEngine, user, project: str, limit: int) -> KgUploadList:
    """The project's uploads the caller's grant sees, newest first."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"listing the uploads of project {project} needs a grant on it")
        items = await _items(conn, access, limit=limit)
    return KgUploadList(uploads=[i for i in items if access.rules.visible_by_grant(i.label, access.max_level)])

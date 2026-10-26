"""The routes of the feature docs, under /v1/x/docs/<project>/ for a member with a grant on a project the feature is on
in (the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project it is off in answers 404).

Readers read: the documents (GET .../documents), a document with every version and one version whole, its Markdown
and its images (GET .../documents/{slug}, ``version`` for an older one), the versions alone (GET .../versions), and an
image a version of the document shows (GET .../documents/{slug}/images/{sha256}). Writers upload images (POST
.../images, the image's bytes as the body) and save a document's next version, creating it at version 1 (PUT
.../documents/{slug}). Reads go through the sink X-Evo-Sink names, else the project's hub sink. A refusal is the hub's
error body; nothing is written.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.docs import images, store
from evo_agents.hub.server.docs.models import (
    DocsDocument,
    DocsDocumentSummary,
    DocsSave,
    DocsSaved,
    DocsUploadedImage,
    DocsVersion,
    detail_model,
    summary_model,
    version_model,
)
from evo_agents.hub.server.docs.store import DocsError
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_docs import MAX_IMAGE_BYTES, SLUG

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}
RACE = "another save of the document took the same version number at the same moment: nothing was written; try again"
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Slug = Annotated[str, Path(pattern=SLUG)]
Sha256 = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
Sink = Annotated[str | None, Header(alias=store.SINK_HEADER)]
Version = Annotated[int | None, Query(ge=1, description="a version's number; the latest when left out")]
IfNoneMatch = Annotated[str | None, Header(description="the ETag of the image the browser holds")]
IMAGE_BODY = {
    "requestBody": {
        "required": True,
        "description": f"the image's bytes, a PNG, JPEG, WebP or GIF image of at most {MAX_IMAGE_BYTES} bytes",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}
IMAGE = {
    200: {"content": {"image/png": {}}, "description": "the image's bytes, in its own media type"},
    304: {"description": "the image the browser holds, named by If-None-Match"},
    503: {"model": ErrorBody},
}

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    except IntegrityError:
        return error_response(request, 409, RACE)


# Reading


@router.get("/{project}/documents", response_model=list[DocsDocumentSummary])
async def list_documents(request: Request, project: ProjectName, user: CurrentUser, sink: Sink = None):
    """The documents of the project the caller sees, by title, each with its latest version."""

    async def work(conn: AsyncConnection):
        access = await project_access(conn, user, project)
        return [summary_model(row) for row in await store.documents(conn, access, sink)]

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}", response_model=DocsDocument)
async def show_document(
    request: Request, project: ProjectName, slug: Slug, user: CurrentUser, version: Version = None, sink: Sink = None
):
    """A document with every version, the newest first, and one version whole: the latest, or ``version``."""

    async def work(conn: AsyncConnection):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        shown = detail_model(project, slug, await store.version(conn, doc, version))
        versions = [version_model(row) for row in await store.versions(conn, doc.id)]
        return DocsDocument(**summary_model(doc).model_dump(), versions=versions, shown=shown)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/versions", response_model=list[DocsVersion])
async def list_versions(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Every version of a document, the newest first: its number, title, summary, author and time."""

    async def work(conn: AsyncConnection):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        return [version_model(row) for row in await store.versions(conn, doc.id)]

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/images/{sha256}", response_class=Response, responses=IMAGE)
async def show_image(
    request: Request,
    project: ProjectName,
    slug: Slug,
    sha256: Sha256,
    user: CurrentUser,
    if_none_match: IfNoneMatch = None,
    sink: Sink = None,
):
    """An image a version of the document shows, from the hub's own blob store, for the document's readers."""

    async def work(conn: AsyncConnection):
        access = await project_access(conn, user, project)
        doc = await store.document(conn, access, slug, sink)
        found = await store.shows_image(conn, doc, sha256)
        if found is None:
            raise DocsError(404, f"no version of document {slug} shows image {sha256}")
        return found

    found = await _answer(request, work)
    if isinstance(found, Response):
        return found
    return await images.serve(request, sha256, found[0], if_none_match)


# Writing


@router.post(
    "/{project}/images",
    status_code=201,
    response_model=DocsUploadedImage,
    responses={**REFUSALS, 503: {"model": ErrorBody}},
    openapi_extra=IMAGE_BODY,
)
async def upload_image(request: Request, project: ProjectName, user: CurrentUser):
    """Upload an image for the project's documents, kept once by its SHA-256; a version then shows it by a name."""

    async def check(conn: AsyncConnection) -> int:
        access = await project_access(conn, user, project)
        store.writer(access, "uploading an image of a document")
        return access.project_id

    try:
        found, held = await images.upload(request, user.user_id, check)
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    return DocsUploadedImage(**found, held=held)


@router.put("/{project}/documents/{slug}", response_model=DocsSaved)
async def save_document(
    request: Request, project: ProjectName, slug: Slug, body: DocsSave, user: CurrentUser, sink: Sink = None
):
    """Save the next version of a document, or create it at version 1; a save that changes nothing writes nothing."""

    async def work(conn: AsyncConnection):
        access = await project_access(conn, user, project)
        content = {"title": body.title, "body": body.body, "summary": body.summary.strip()}
        shown = {image.name: image.sha256 for image in body.images}
        label = body.label.model_dump(exclude_none=True) if body.label is not None else None
        saved = await store.save(
            conn, access, user.user_id, slug, content, shown, base_version=body.base_version, label=label, sink=sink
        )
        return DocsSaved(
            document=summary_model(saved.document), version=saved.version, created=saved.created, changed=saved.changed
        )

    return await _answer(request, work)

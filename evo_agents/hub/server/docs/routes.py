"""The routes of the feature docs, under /v1/x/docs/<project>/ for a member with a grant on a project the feature is on
in (the hub checks both first, ``evo_agents.hub.server.plugin_routes``; a project it is off in answers 404).

Readers read: the documents (GET .../documents), a document with every version and one version whole, its Markdown
and its images (GET .../documents/{slug}, ``version`` for an older one), the versions alone (GET .../versions), an
image a version of the document shows (GET .../documents/{slug}/images/{sha256}), and an HTML version as a page of its
own (GET .../documents/{slug}/page, ``version`` for an older one).

A page is served from the hub's origin as an interactive answer of a chat is (``evo_agents.hub.outputs``): UTF-8
text/html, ``nosniff``, no referrer, kept no longer than a revalidation, under a Content-Security-Policy whose
``sandbox`` gives it an opaque origin, so it reads no cookie and calls nothing; only the hub's pages frame it. Its
scripts, styles, fonts and images load from the CDNs of ``outputs.INTERACTIVE_POLICY`` when the project is not
restricted (``service.cdn_allowed``), and from nowhere (``outputs.HTML_POLICY``) when it is. Writers upload images (POST
.../images, the image's bytes as the body) and save a document's next version, creating it at version 1 (PUT
.../documents/{slug}). Reads go through the sink X-Evo-Sink names, else the project's hub sink. A refusal is the hub's
error body; nothing is written.

Each handler parses the request and hands it to ``service`` in one transaction (``UnitOfWork``); the service
checks and decides, and the repository reads and writes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.docs import images, service
from evo_agents.hub.server.docs.models import (
    IMAGE_BODY,
    RACE,
    SINK_HEADER,
    DocsDocument,
    DocsDocumentSummary,
    DocsError,
    DocsSave,
    DocsSaved,
    DocsUploadedImage,
    DocsVersion,
)
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.tables_docs import SLUG

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422)}
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
Slug = Annotated[str, Path(pattern=SLUG)]
Sha256 = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
Sink = Annotated[str | None, Header(alias=SINK_HEADER)]
Version = Annotated[int | None, Query(ge=1, description="a version's number; the latest when left out")]
IfNoneMatch = Annotated[str | None, Header(description="the ETag of the image the browser holds")]
PAGE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/html": {}}, "description": "the HTML version as a page, under a sandbox"}
}
IMAGE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"image/png": {}}, "description": "the image's bytes, in its own media type"},
    304: {"description": "the image the browser holds, named by If-None-Match"},
    503: {"model": ErrorBody},
}

router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}, **REFUSALS})


async def _answer(request: Request, work):
    """``work(conn)`` in one transaction; a refusal as the hub's error body, nothing written."""
    try:
        async with UnitOfWork(request.app.state.engine) as conn:
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
        return await service.listed(conn, user, project, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}", response_model=DocsDocument)
async def show_document(
    request: Request, project: ProjectName, slug: Slug, user: CurrentUser, version: Version = None, sink: Sink = None
):
    """A document with every version, the newest first, and one version whole: the latest, or ``version``."""

    async def work(conn: AsyncConnection):
        return await service.shown(conn, user, project, slug, version, sink)

    return await _answer(request, work)


@router.get("/{project}/documents/{slug}/versions", response_model=list[DocsVersion])
async def list_versions(request: Request, project: ProjectName, slug: Slug, user: CurrentUser, sink: Sink = None):
    """Every version of a document, the newest first: its number, title, summary, author and time."""

    async def work(conn: AsyncConnection):
        return await service.versions_of(conn, user, project, slug, sink)

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
        return await service.image_shown(conn, user, project, slug, sha256, sink)

    found = await _answer(request, work)
    if isinstance(found, Response):
        return found
    return await images.serve(request, sha256, found[0], if_none_match)


def page_headers(cdn: bool) -> dict[str, str]:
    """The headers of an HTML version's page (see the module); ``cdn`` lets it load libraries from the CDNs."""
    return {
        "Content-Security-Policy": outputs.INTERACTIVE_POLICY if cdn else outputs.HTML_POLICY,
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "private, no-cache",
    }


@router.get("/{project}/documents/{slug}/page", response_class=Response, responses=PAGE)
async def show_page(
    request: Request, project: ProjectName, slug: Slug, user: CurrentUser, version: Version = None, sink: Sink = None
):
    """An HTML version of the document as a page of its own, the latest unless ``version``, for the document's readers;
    a Markdown version is 404 here."""

    async def work(conn: AsyncConnection):
        return await service.page(conn, user, project, slug, version, sink)

    found = await _answer(request, work)
    if isinstance(found, Response):
        return found
    body, cdn = found
    return Response(body, media_type="text/html; charset=utf-8", headers=page_headers(cdn))


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
    try:
        found, held = await images.upload(request, user.user_id, service.uploader(user, project))
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    return DocsUploadedImage(**found, held=held)


@router.put("/{project}/documents/{slug}", response_model=DocsSaved)
async def save_document(
    request: Request, project: ProjectName, slug: Slug, body: DocsSave, user: CurrentUser, sink: Sink = None
):
    """Save the next version of a document, or create it at version 1; a save that changes nothing writes nothing."""

    async def work(conn: AsyncConnection):
        return await service.saved(conn, user, project, slug, body, sink)

    return await _answer(request, work)

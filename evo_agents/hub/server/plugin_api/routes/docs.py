"""The documents of a project an isolated plugin saves with its token (``docs/plugins.md``, section Permissions),
granted ``docs:write`` in a project that has the feature docs on (``service.docs``):

- POST /v1/plugin-api/projects/{p}/docs/images: upload an image for the project's documents, the image's bytes as the
  body, as a member's POST /v1/x/docs/{p}/images does (a PNG, JPEG, WebP or GIF image of at most 10 MiB, kept once by
  its SHA-256);
- PUT .../docs/documents/{slug}: save the next version of a document whose slug starts with ``<plugin>-``, or create it
  at version 1, as a member's PUT does: its title, Markdown body and summary, and the images it shows, each by the
  name its Markdown gives it and the SHA-256 an upload answered.

A refusal is the hub's error body; nothing is written.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.docs import images
from evo_agents.hub.server.docs.models import IMAGE_BODY, DocsError, DocsSave, DocsSaved, DocsUploadedImage
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plugin_api.models.docs import RACE
from evo_agents.hub.server.plugin_api.service import docs
from evo_agents.hub.server.task_dispatch import plugin_of_request
from evo_agents.hub.tables_docs import SLUG

Slug = Annotated[str, Path(pattern=SLUG, description="the document's slug, which starts with the plugin's id and -")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 413, 422)}
UPLOAD_REFUSALS: dict[int | str, dict[str, Any]] = {**REFUSALS, 503: {"model": ErrorBody}}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


@router.post(
    "/projects/{project}/docs/images",
    status_code=201,
    response_model=DocsUploadedImage,
    responses=UPLOAD_REFUSALS,
    openapi_extra=IMAGE_BODY,
)
async def upload_image(request: Request, project: ProjectName):
    """Upload an image for the project's documents, kept once by its SHA-256; a version then shows it by a name."""

    async def writing(conn: AsyncConnection):
        return await docs.writing(conn, await plugin_of_request(request, conn), project)

    try:
        who = await docs.acting_writer(request.app.state.engine, request, project)

        async def check(conn: AsyncConnection) -> int:
            return (await writing(conn)).access.project_id

        found, held = await images.upload(request, who.user_id, check)
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    return DocsUploadedImage(**found, held=held)


@router.put("/projects/{project}/docs/documents/{slug}", response_model=DocsSaved, responses=REFUSALS)
async def save_document(request: Request, project: ProjectName, slug: Slug, body: DocsSave):
    """Save the next version of a document of the plugin, or create it at version 1; a save that changes nothing
    writes nothing."""
    try:
        return await docs.save(request.app.state.engine, request, project, slug, body)
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    except IntegrityError:
        return error_response(request, 409, RACE)


__all__ = ["router"]

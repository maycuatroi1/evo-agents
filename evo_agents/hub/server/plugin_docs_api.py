"""The documents of a project an isolated plugin saves with its token (``docs/plugins.md``, section Permissions),
granted ``docs:write`` in a project that has the feature docs on (``plugin_api_access``):

- POST /v1/plugin-api/projects/{p}/docs/images: upload an image for the project's documents, the image's bytes as the
  body, as a member's POST /v1/x/docs/{p}/images does (a PNG, JPEG, WebP or GIF image of at most 10 MiB, kept once by
  its SHA-256);
- PUT .../docs/documents/{slug}: save the next version of a document whose slug starts with ``<plugin>-``, or create it
  at version 1, as a member's PUT does: its title, Markdown body and summary, and the images it shows, each by the
  name its Markdown gives it and the SHA-256 an upload answered. A slug without the prefix is a 422, so a plugin never
  writes a document a member or another plugin keeps; a ``label`` is a 422 too, as a plugin's document carries the
  project's.

Both run the code of a member's request (``evo_agents.hub.server.docs``) in the name of the admin who granted the
plugin its permissions (``plugin_api_access.acting``), the author of each version and the uploader of each image. A
refusal is the hub's error body; nothing is written.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.docs import images, store
from evo_agents.hub.server.docs.models import DocsSave, DocsSaved, DocsUploadedImage, summary_model
from evo_agents.hub.server.docs.server import IMAGE_BODY
from evo_agents.hub.server.docs.store import DocsError
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plugin_api_access import Acting, acting, feature_on, granted
from evo_agents.hub.server.task_dispatch import plugin_of_request
from evo_agents.hub.tables_docs import SLUG

log = logging.getLogger(__name__)

PERMISSION = "docs:write"
FEATURE = "docs"
RACE = "another save of the document took the same version number at the same moment: nothing was written; try again"
Slug = Annotated[str, Path(pattern=SLUG, description="the document's slug, which starts with the plugin's id and -")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404, 409, 413, 422)}
UPLOAD_REFUSALS: dict[int | str, dict[str, Any]] = {**REFUSALS, 503: {"model": ErrorBody}}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


async def _acting(request: Request, conn: AsyncConnection, project: str) -> Acting:
    plugin_id = await plugin_of_request(request, conn)
    enabled = await granted(conn, plugin_id, project, PERMISSION)
    await feature_on(conn, FEATURE, enabled)
    who = await acting(conn, enabled)
    store.writer(who.access, f"plugin {who.plugin} writing a document")
    return who


def slug_prefix(plugin: str) -> str:
    """What the slug of every document plugin ``plugin`` saves starts with."""
    return f"{plugin}-"


@router.post(
    "/projects/{project}/docs/images",
    status_code=201,
    response_model=DocsUploadedImage,
    responses=UPLOAD_REFUSALS,
    openapi_extra=IMAGE_BODY,
)
async def upload_image(request: Request, project: ProjectName):
    """Upload an image for the project's documents, kept once by its SHA-256; a version then shows it by a name."""
    try:
        async with request.app.state.engine.begin() as conn:
            who = await _acting(request, conn, project)

        async def check(conn: AsyncConnection) -> int:
            return (await _acting(request, conn, project)).access.project_id

        found, held = await images.upload(request, who.user_id, check)
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    return DocsUploadedImage(**found, held=held)


@router.put("/projects/{project}/docs/documents/{slug}", response_model=DocsSaved, responses=REFUSALS)
async def save_document(request: Request, project: ProjectName, slug: Slug, body: DocsSave):
    """Save the next version of a document of the plugin, or create it at version 1; a save that changes nothing
    writes nothing."""

    async def work(conn: AsyncConnection):
        who = await _acting(request, conn, project)
        prefix = slug_prefix(who.plugin)
        if not slug.startswith(prefix) or slug == prefix:
            raise DocsError(422, f"plugin {who.plugin} saves documents whose slug starts with {prefix}, not {slug}")
        if body.label is not None:
            raise DocsError(422, "a document a plugin saves carries the project's label: leave label out")
        content = {"title": body.title, "body": body.body, "summary": body.summary.strip()}
        shown = {image.name: image.sha256 for image in body.images}
        saved = await store.save(
            conn, who.access, who.user_id, slug, content, shown, base_version=body.base_version, sink=None
        )
        log.info("plugin document saved", extra={"project": project, "plugin": who.plugin, "version": saved.version})
        return DocsSaved(
            document=summary_model(saved.document), version=saved.version, created=saved.created, changed=saved.changed
        )

    try:
        async with request.app.state.engine.begin() as conn:
            return await work(conn)
    except DocsError as exc:
        return error_response(request, exc.status, exc.message)
    except IntegrityError:
        return error_response(request, 409, RACE)


__all__ = ["router", "slug_prefix"]

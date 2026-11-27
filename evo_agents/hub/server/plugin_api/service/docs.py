"""The documents of a project an isolated plugin saves (``routes.docs``), granted ``docs:write`` in a project that
has the feature docs on. Both routes run the code of a member's request (``evo_agents.hub.server.docs``) in the name
of the admin who granted the plugin its permissions (``service.access.acting``), the author of each version and the
uploader of each image. A slug without the prefix ``<plugin>-`` is a 422, so a plugin never writes a document a member
or another plugin keeps; a ``label`` is a 422 too, as a plugin's document carries the project's."""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.docs import service as docs
from evo_agents.hub.server.docs.models import DocsError, DocsSave, DocsSaved, summary_model
from evo_agents.hub.server.plugin_api.models.access import Acting
from evo_agents.hub.server.plugin_api.models.docs import FEATURE, PERMISSION, slug_prefix
from evo_agents.hub.server.plugin_api.service.access import acting, feature_on, granted
from evo_agents.hub.server.task_dispatch import plugin_of_request

log = logging.getLogger(__name__)


async def writing(conn: AsyncConnection, plugin_id: int, project: str) -> Acting:
    """The plugin acting in ``project`` with ``docs:write``, in the name of a writer of the project."""
    enabled = await granted(conn, plugin_id, project, PERMISSION)
    await feature_on(conn, FEATURE, enabled)
    who = await acting(conn, enabled)
    docs.writer(who.access, f"plugin {who.plugin} writing a document")
    return who


async def acting_writer(engine: AsyncEngine, request: Request, project: str) -> Acting:
    """The plugin whose token ``request`` carries, acting in ``project`` as ``writing`` checks, in a transaction of its
    own."""
    async with UnitOfWork(engine) as conn:
        return await writing(conn, await plugin_of_request(request, conn), project)


async def save(engine: AsyncEngine, request: Request, project: str, slug: str, body: DocsSave) -> DocsSaved:
    """Save the next version of document ``slug`` of the plugin, or create it at version 1."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        who = await writing(conn, plugin_id, project)
        prefix = slug_prefix(who.plugin)
        if not slug.startswith(prefix) or slug == prefix:
            raise DocsError(422, f"plugin {who.plugin} saves documents whose slug starts with {prefix}, not {slug}")
        if body.label is not None:
            raise DocsError(422, "a document a plugin saves carries the project's label: leave label out")
        content = {"title": body.title, "body": body.body, "summary": body.summary.strip()}
        shown = {image.name: image.sha256 for image in body.images}
        saved = await docs.save(
            conn, who.access, who.user_id, slug, content, shown, base_version=body.base_version, sink=None
        )
        log.info("plugin document saved", extra={"project": project, "plugin": who.plugin, "version": saved.version})
        return DocsSaved(
            document=summary_model(saved.document), version=saved.version, created=saved.created, changed=saved.changed
        )


__all__ = ["acting_writer", "save", "writing"]

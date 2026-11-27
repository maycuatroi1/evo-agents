"""The use cases of an SRS document an isolated plugin lists (``routes.srs``), granted ``srs:read`` in a project that
has the feature srs on. No text of the document leaves this way, as no text leaves in the event srs.version.approved;
an agent the plugin hands a task reads the text through its own run. The document is read as the admin who granted the
plugin its permissions reads it through the project's hub sink (``service.access.acting``): one whose label is above
what that reaches is the 404 of one that does not exist, as is one with no approved version yet."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.plugin_api.models.srs import FEATURE, PERMISSION, ApprovedUseCases
from evo_agents.hub.server.plugin_api.repository import srs as queries
from evo_agents.hub.server.plugin_api.service.access import acting, feature_on, granted
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.server.srs.repository import store
from evo_agents.hub.server.task_dispatch import plugin_of_request


async def approved_use_cases(engine: AsyncEngine, request: Request, project: str, slug: str) -> ApprovedUseCases:
    """The use cases of the version of document ``slug`` approved last; ``SrsError`` 404 when it has none yet."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        enabled = await granted(conn, plugin_id, project, PERMISSION)
        await feature_on(conn, FEATURE, enabled)
        who = await acting(conn, enabled, writes=False)
        doc = await store.document(conn, who.access, slug, None)
        if doc.approved_version is None:
            raise SrsError(404, f"document {slug} of project {project} has no approved version yet")
        version = await store.version(conn, doc, doc.approved_version)
        found = await queries.use_cases(conn, version.id)
        return ApprovedUseCases(document=doc.slug, version=version.number, use_cases=found)


__all__ = ["approved_use_cases"]

"""The use cases of an SRS document an isolated plugin lists with its token (``docs/plugins.md``, section
Permissions), granted ``srs:read`` in a project that has the feature srs on (``plugin_api_access``):

GET /v1/plugin-api/projects/{p}/srs/{slug}/use-cases: the use cases of the version of document ``slug`` approved last,
in the document's order, identifiers only: each use case's code and key (srs_items.key, which stays across versions
while its code may change), with the document's slug and that version's number. No text of the document leaves this
way, as no text leaves in the event srs.version.approved; an agent the plugin hands a task reads the text through its
own run. The document is read as the admin who granted the plugin its permissions reads it through the project's hub
sink (``plugin_api_access.acting``): one whose label is above what that reaches is the 404 of one that does not exist,
as is one with no approved version yet.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plugin_api_access import acting, feature_on, granted
from evo_agents.hub.server.srs import store
from evo_agents.hub.server.srs.doc import SrsError
from evo_agents.hub.server.task_dispatch import plugin_of_request
from evo_agents.hub.tables_srs import SLUG

PERMISSION = "srs:read"
FEATURE = "srs"
USE_CASE = "use_case"
Slug = Annotated[str, Path(pattern=SLUG, description="the document's slug")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


class ApprovedUseCase(BaseModel):
    code: str = Field(description="its code in that version, as UC POST-21")
    key: str = Field(description="the hub's key of the item, the same in every version of the document")


class ApprovedUseCases(BaseModel):
    """The use cases of the version of a document approved last, identifiers only."""

    document: str = Field(description="the document's slug")
    version: int = Field(description="the version approved last, which every use case below is read from")
    use_cases: list[ApprovedUseCase] = Field(description="in the document's order")


async def _use_cases(conn: AsyncConnection, version_id: int) -> list[ApprovedUseCase]:
    s = tables.srs
    iv, i = s.item_versions, s.items
    query = (
        select(i.c.code, i.c.key)
        .join_from(iv, i, i.c.id == iv.c.item_id)
        .where(iv.c.version_id == version_id, i.c.kind == USE_CASE, i.c.code.is_not(None))
        .order_by(iv.c.position)
    )
    return [ApprovedUseCase(code=row.code, key=row.key) for row in await conn.execute(query)]


@router.get("/projects/{project}/srs/{slug}/use-cases", response_model=ApprovedUseCases, responses=REFUSALS)
async def approved_use_cases(request: Request, project: ProjectName, slug: Slug):
    """The use cases of the version of document ``slug`` approved last: code and key of each."""
    try:
        async with request.app.state.engine.begin() as conn:
            plugin_id = await plugin_of_request(request, conn)
            enabled = await granted(conn, plugin_id, project, PERMISSION)
            await feature_on(conn, FEATURE, enabled)
            who = await acting(conn, enabled, writes=False)
            doc = await store.document(conn, who.access, slug, None)
            if doc.approved_version is None:
                raise SrsError(404, f"document {slug} of project {project} has no approved version yet")
            version = await store.version(conn, doc, doc.approved_version)
            found = await _use_cases(conn, version.id)
    except SrsError as exc:
        return error_response(request, exc.status, exc.message)
    return ApprovedUseCases(document=doc.slug, version=version.number, use_cases=found)


__all__ = ["router"]

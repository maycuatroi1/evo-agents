"""The use cases of an SRS document an isolated plugin lists with its token (``docs/plugins.md``, section
Permissions), granted ``srs:read`` in a project that has the feature srs on (``service.srs``):

GET /v1/plugin-api/projects/{p}/srs/{slug}/use-cases: the use cases of the version of document ``slug`` approved last,
in the document's order, identifiers only: each use case's code and key (srs_items.key, which stays across versions
while its code may change), with the document's slug and that version's number.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.plugin_api.models.srs import ApprovedUseCases
from evo_agents.hub.server.plugin_api.service import srs
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.tables_srs import SLUG

Slug = Annotated[str, Path(pattern=SLUG, description="the document's slug")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


@router.get("/projects/{project}/srs/{slug}/use-cases", response_model=ApprovedUseCases, responses=REFUSALS)
async def approved_use_cases(request: Request, project: ProjectName, slug: Slug):
    """The use cases of the version of document ``slug`` approved last: code and key of each."""
    try:
        return await srs.approved_use_cases(request.app.state.engine, request, project, slug)
    except SrsError as exc:
        return error_response(request, exc.status, exc.message)


__all__ = ["router"]

"""Where the flow after an archive's upload stands, and the notice once it ends (plan kg-zip-to-answers, step 4).
``evo_agents.hub.kg_pipeline`` holds the phases without the database; this module reads the rows they come from.

- GET /v1/kg/{project}/uploads/{upload_id}/pipeline (``evo-agents hub kg pipeline --project P [--upload ID]``, and
  ``hub kg upload`` as it waits): the flow's state, the phase running or failed, each phase with its state and detail,
  and the build, kg_init run and rebuild it went through. A grant on the project that sees the upload's label reads it.
- ``tell``, called each minute by ``curator.collect`` after the kg_init runs moved: for each upload whose flow ended
  ready or failed and that nobody was told of (``kg_uploads.told_at``), one notice NOTICE to its sender and to the
  project's admins, through their channels, linking the graph or the Upload data page. A replaced upload is marked
  told with no notice.

The build is the one the upload queued; the kg_init run is the newest of the project whose build is that one or later
(a run takes the newest build as it starts), so a run asked for again after one failed, or one that followed a later
build, replaces it and only the newest counts; and the rebuild is the one its decision queued, else the one the
newest earlier kg_init run on that build or later queued (a kg_init run asked for again while the first one's rebuild
runs, whose settings need none, does not end the flow before that rebuild). A build whose worker stopped, or whose job
ended without recording it, gives its place to the next build of the project (``kg_build.REPLACED``), which the flow
follows instead. Logs name the project and the upload, never an item.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from fastapi import Path as PathParam

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.kg.models.pipeline import KgPipeline as KgPipeline
from evo_agents.hub.server.kg.models.pipeline import KgPipelinePhase as KgPipelinePhase
from evo_agents.hub.server.kg.service import pipeline as service
from evo_agents.hub.server.kg.service.pipeline import tell  # noqa: F401 (curator.collect and the tests call it here)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

UploadId = Annotated[int, PathParam(ge=1, description="the upload, as `hub kg uploads` lists it")]


@router.get(
    "/{project}/uploads/{upload_id}/pipeline",
    response_model=KgPipeline,
    responses={code: {"model": ErrorBody} for code in (403, 404)},
)
async def pipeline(request: Request, project: ProjectName, upload_id: UploadId, user: CurrentUser) -> KgPipeline:
    """Where the flow after upload ``upload_id`` stands."""
    return await service.pipeline(request.app.state.engine, user, project, upload_id)

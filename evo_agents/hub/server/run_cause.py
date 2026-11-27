"""Take back the cause a core plugin gave a failed run: ``POST /v1/projects/{project}/runs/{run_id}/cause/restore``
(the writer role). The cause goes back to the one the run had before the plugin classified it (the hub's guess), the
run's log gets a ``system`` event that says who restored it and the audit log gets ``run.cause``. The classification
itself is written by ``evo_agents.hub.server.core_plugin_api`` (``classify_run_failure``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.runs.models.run_cause import CAUSE_KEY as CAUSE_KEY
from evo_agents.hub.server.runs.models.run_cause import CLASSIFIED as CLASSIFIED
from evo_agents.hub.server.runs.models.run_cause import RESTORED as RESTORED
from evo_agents.hub.server.runs.models.run_cause import CauseRestored as CauseRestored
from evo_agents.hub.server.runs.service import run_cause as service
from evo_agents.hub.server.runs.service.run_cause import classified  # noqa: F401 (core_plugin_api reads it here)
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})


@router.post("/{project}/runs/{run_id}/cause/restore", response_model=CauseRestored)
async def restore_cause(request: Request, project: ProjectName, run_id: int, user: CurrentUser) -> CauseRestored:
    """Give a failed run back the cause it had before a core plugin classified it; the run's log says who did."""
    if not 1 <= run_id <= MAX_ID:
        raise HTTPException(422, "run_id is out of range")
    return await service.restore(request.app.state.engine, user, project, run_id)

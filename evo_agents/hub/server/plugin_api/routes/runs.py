"""What an isolated plugin reads of the task runs of its own agent types, with its token (``docs/plugins.md``, section
Agent types), granted ``runs:dispatch`` in the project (``service.runs``):

- GET /v1/plugin-api/projects/{p}/runs/{run}/summary: the task run as GET .../tasks/{task} shows a task, with the
  evidence the hub kept when it ended (``summary``: the worker, the commits, each verify, and the agent's summary)
  and its error;
- GET .../runs/{run}/outputs: the files its agent made that the hub keeps (``evo_agents.hub.outputs``), oldest first,
  ``source`` and ``after`` filtering them as a member's list does, each with the URL of its bytes below;
- GET .../runs/{run}/outputs/{output}/content: the bytes of one, as a member's GET of them is answered
  (``outputs.service.serving.serve``: its media type, a Range, ETag and ``download=true``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response

from evo_agents.hub import outputs
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.outputs.models import CONTENT, Download, IfNoneMatch, RangeHeader
from evo_agents.hub.server.outputs.service.serving import serve
from evo_agents.hub.server.plugin_api.models.runs import TaskRunOutputs, TaskRunSummary
from evo_agents.hub.server.plugin_api.service import runs
from evo_agents.hub.server.runs.models import MAX_ID, RunId

OutputId = Annotated[int, Path(ge=1, le=MAX_ID)]
Sources = Annotated[list[Literal[outputs.SOURCES]], Query(description="only these sources; repeat it")]
After = Annotated[int, Query(ge=0, le=MAX_ID, description="only the outputs after this id")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


@router.get("/projects/{project}/runs/{run_id}/summary", response_model=TaskRunSummary, responses=REFUSALS)
async def run_summary(request: Request, project: ProjectName, run_id: RunId) -> TaskRunSummary:
    """A task run of an agent type of the plugin, with its evidence and error."""
    return await runs.summary(request.app.state.engine, request, project, run_id)


@router.get("/projects/{project}/runs/{run_id}/outputs", response_model=TaskRunOutputs, responses=REFUSALS)
async def run_outputs(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    source: Sources = [],  # noqa: B006
    after: After = 0,
) -> TaskRunOutputs:
    """The outputs of a task run of an agent type of the plugin, oldest first, each with the URL of its bytes."""
    return await runs.outputs(request.app.state.engine, request, project, run_id, [str(one) for one in source], after)


@router.get(
    "/projects/{project}/runs/{run_id}/outputs/{output_id}/content",
    response_class=Response,
    responses={**CONTENT, 401: {"model": ErrorBody}},
)
async def output_content(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    output_id: OutputId,
    download: Download = False,
    range_: RangeHeader = None,
    if_none_match: IfNoneMatch = None,
) -> Response:
    """The bytes of an output of a task run of an agent type of the plugin."""
    row = await runs.output(request.app.state.engine, request, project, run_id, output_id)
    return await serve(request, row, download, range_, if_none_match)


__all__ = ["router"]

"""What an isolated plugin reads of the task runs of its own agent types, with its token (``docs/plugins.md``, section
Agent types), granted ``runs:dispatch`` in the project (``plugin_api_access.granted``):

- GET /v1/plugin-api/projects/{p}/runs/{run}/summary: the task run as GET .../tasks/{task} shows a task, with the
  evidence the hub kept when it ended (``summary``: the worker, the commits, each verify, and the agent's summary)
  and its error;
- GET .../runs/{run}/outputs: the files its agent made that the hub keeps (``evo_agents.hub.outputs``), oldest first,
  ``source`` and ``after`` filtering them as a member's list does, each with the URL of its bytes below;
- GET .../runs/{run}/outputs/{output}/content: the bytes of one, as a member's GET of them is answered
  (``output_content.serve``: its media type, a Range, ETag and ``download=true``).

A run is one of these when it is a task run in the project of an agent type the plugin contributes, each attempt of a
task by its own id (the ``run_id`` of task.result); any other run is a 404, as is an output of another run.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import outputs, tables, task_runs
from evo_agents.hub.server import output_views
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.output_content import CONTENT, Download, IfNoneMatch, RangeHeader, serve
from evo_agents.hub.server.output_store import usage
from evo_agents.hub.server.output_views import RunOutput
from evo_agents.hub.server.plugin_api_access import granted
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.task_dispatch import Task, plugin_of_request, task_select, task_view

OutputId = Annotated[int, Path(ge=1, le=MAX_ID)]
Sources = Annotated[list[Literal[outputs.SOURCES]], Query(description="only these sources; repeat it")]
After = Annotated[int, Query(ge=0, le=MAX_ID, description="only the outputs after this id")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (401, 403, 404)}

router = APIRouter(prefix="/v1/plugin-api", tags=["plugin api"], responses={401: {"model": ErrorBody}})


class TaskRunSummary(Task):
    """A task run of an agent type of the plugin, with what the hub kept of its end."""

    summary: str | None = Field(
        description="the evidence of the run once it ended done or in review: the worker, the commits and their "
        "diffstat, each verify command with its exit code, then the agent's summary; null before"
    )
    error: str | None = Field(description="why it failed, as its worker reported it; null otherwise")


class TaskRunOutputs(BaseModel):
    run_id: int
    outputs: list[RunOutput] = Field(
        description="oldest first, those the filters keep; url is where the plugin reads them"
    )
    count: int = Field(description="every output the run holds")
    bytes: int = Field(description="their bytes")


def content_url(project: str, run_id: int, output_id: int) -> str:
    """Where the plugin API answers the bytes of output ``output_id`` of run ``run_id``."""
    return f"/v1/plugin-api/projects/{quote(project, safe='')}/runs/{run_id}/outputs/{output_id}/content"


async def _own_run(request: Request, conn: AsyncConnection, project: str, run_id: int):
    """The row of ``task_select`` of run ``run_id`` with its evidence and error, a task run in ``project`` of an agent
    type of the plugin whose token the request carries; 404 for any other."""
    plugin_id = await plugin_of_request(request, conn)
    enabled = await granted(conn, plugin_id, project, task_runs.DISPATCH_PERMISSION)
    r, t = tables.runs, tables.project_agent_types
    query = (
        task_select()
        .add_columns(r.c.evidence, r.c.error)
        .where(r.c.id == run_id, r.c.project_id == enabled.project_id, r.c.kind == "task", t.c.plugin_id == plugin_id)
    )
    found = (await conn.execute(query)).one_or_none()
    if found is None:
        raise HTTPException(404, f"no task run {run_id} of an agent type of the plugin in project {project}")
    return found


@router.get("/projects/{project}/runs/{run_id}/summary", response_model=TaskRunSummary, responses=REFUSALS)
async def run_summary(request: Request, project: ProjectName, run_id: RunId) -> TaskRunSummary:
    """A task run of an agent type of the plugin, with its evidence and error."""
    async with request.app.state.engine.begin() as conn:
        found = await _own_run(request, conn, project, run_id)
    return TaskRunSummary(**task_view(found), summary=found.evidence, error=found.error)


@router.get("/projects/{project}/runs/{run_id}/outputs", response_model=TaskRunOutputs, responses=REFUSALS)
async def run_outputs(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    source: Sources = [],  # noqa: B006
    after: After = 0,
) -> TaskRunOutputs:
    """The outputs of a task run of an agent type of the plugin, oldest first, each with the URL of its bytes."""
    o = tables.run_outputs
    async with request.app.state.engine.begin() as conn:
        found = await _own_run(request, conn, project, run_id)
        query = select(*output_views.columns()).where(o.c.run_id == run_id, o.c.id > after)
        if source:
            query = query.where(o.c.source.in_(list(dict.fromkeys(source))))
        rows = (await conn.execute(query.order_by(o.c.id))).all()
        count, size = await usage(conn, run_id)
    shown = [output_views.view(row, content_url(found.project, run_id, row.id)) for row in rows]
    return TaskRunOutputs(run_id=run_id, outputs=shown, count=count, bytes=size)


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
    o = tables.run_outputs
    async with request.app.state.engine.begin() as conn:
        await _own_run(request, conn, project, run_id)
        query = select(*output_views.columns()).where(o.c.id == output_id, o.c.run_id == run_id)
        row = (await conn.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, f"run {run_id} has no output {output_id}: see GET .../runs/{run_id}/outputs")
    return await serve(request, row, download, range_, if_none_match)


__all__ = ["content_url", "router"]

"""What an isolated plugin reads of the task runs of its own agent types (``routes.runs``), granted ``runs:dispatch``
in the project (``service.access.granted``). A run is one of these when it is a task run in the project of an agent
type the plugin contributes, each attempt of a task by its own id (the ``run_id`` of task.result); any other run is a
404, as is an output of another run."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import task_runs
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.outputs.models import view
from evo_agents.hub.server.outputs.service.store import usage
from evo_agents.hub.server.plugin_api.models.runs import TaskRunOutputs, TaskRunSummary, content_url
from evo_agents.hub.server.plugin_api.repository import runs as store
from evo_agents.hub.server.plugin_api.service.access import granted
from evo_agents.hub.server.task_dispatch import plugin_of_request, task_view


async def own_run(conn: AsyncConnection, plugin_id: int, project: str, run_id: int):
    """The row of ``task_select`` of run ``run_id`` with its evidence and error, a task run in ``project`` of an agent
    type of plugin ``plugin_id``; 404 for any other."""
    enabled = await granted(conn, plugin_id, project, task_runs.DISPATCH_PERMISSION)
    found = await store.task_run(conn, enabled.project_id, plugin_id, run_id)
    if found is None:
        raise HTTPException(404, f"no task run {run_id} of an agent type of the plugin in project {project}")
    return found


async def summary(engine: AsyncEngine, request: Request, project: str, run_id: int) -> TaskRunSummary:
    """The task run with its evidence and error."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        found = await own_run(conn, plugin_id, project, run_id)
        return TaskRunSummary(**task_view(found), summary=found.evidence, error=found.error)


async def outputs(
    engine: AsyncEngine, request: Request, project: str, run_id: int, sources: Sequence[str], after: int
) -> TaskRunOutputs:
    """The outputs of the task run after ``after``, of ``sources`` when given, each with the URL of its bytes."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        found = await own_run(conn, plugin_id, project, run_id)
        rows = await store.outputs(conn, run_id, after, list(dict.fromkeys(sources)))
        count, size = await usage(conn, run_id)
        shown = [view(row, content_url(found.project, run_id, row.id)) for row in rows]
        return TaskRunOutputs(run_id=run_id, outputs=shown, count=count, bytes=size)


async def output(engine: AsyncEngine, request: Request, project: str, run_id: int, output_id: int):
    """The row of output ``output_id`` of the task run; 404 for an output of another run, or none."""
    async with UnitOfWork(engine) as conn:
        plugin_id = await plugin_of_request(request, conn)
        await own_run(conn, plugin_id, project, run_id)
        row = await store.output(conn, run_id, output_id)
        if row is None:
            raise HTTPException(404, f"run {run_id} has no output {output_id}: see GET .../runs/{run_id}/outputs")
        return row


__all__ = ["output", "outputs", "own_run", "summary"]

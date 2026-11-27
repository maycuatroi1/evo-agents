"""The queries of the task runs of a plugin's agent types and of their outputs (``service.runs``)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.outputs.repository.views import columns
from evo_agents.hub.server.task_dispatch import task_select


async def task_run(conn: AsyncConnection, project_id: int, plugin_id: int, run_id: int):
    """The row of ``task_select`` of run ``run_id`` with its evidence and error, a task run in project ``project_id``
    of an agent type of plugin ``plugin_id``; None for any other."""
    r, t = tables.runs, tables.project_agent_types
    query = (
        task_select()
        .add_columns(r.c.evidence, r.c.error)
        .where(r.c.id == run_id, r.c.project_id == project_id, r.c.kind == "task", t.c.plugin_id == plugin_id)
    )
    return (await conn.execute(query)).one_or_none()


async def outputs(conn: AsyncConnection, run_id: int, after: int, sources: Sequence[str]):
    """The outputs of run ``run_id`` after id ``after``, of ``sources`` when given, oldest first."""
    o = tables.run_outputs
    query = select(*columns()).where(o.c.run_id == run_id, o.c.id > after)
    if sources:
        query = query.where(o.c.source.in_(list(sources)))
    return (await conn.execute(query.order_by(o.c.id))).all()


async def output(conn: AsyncConnection, run_id: int, output_id: int):
    """Output ``output_id`` of run ``run_id``, or None."""
    o = tables.run_outputs
    query = select(*columns()).where(o.c.id == output_id, o.c.run_id == run_id)
    return (await conn.execute(query)).one_or_none()


__all__ = ["output", "outputs", "task_run"]

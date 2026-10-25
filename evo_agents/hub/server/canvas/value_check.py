"""The value check of the core plugin canvas, the plugin's job ``value_check`` (criterion a4 of plan
living-lean-canvas): the test cards of a project's claims against its signals, as evidence and proposals, and the
feedback and evidence that is no number put before @strategist to judge. It never changes a claim's status.

When it runs. Hourly at minute 11, for every project that enables canvas (``hub.plugin_jobs``), and at once for one
project when a batch of its signals comes: ``evo_agents.hub.signals.push`` queues ``hub.value_check``, which runs the
plugins' jobs named value_check of that project (``evo_agents.hub.plugin_jobs.run_on_signals``). The hourly pass also
catches what no batch brings: a deadline that passed, items that waited for the type's cap.

Each canvas not archived goes in a transaction of its own, under the row lock every write of the canvas takes, so two
passes never write twice: first the check of each claim (``checks``), then a judging run when the canvas has items to
judge and none is going (``judging``).

``judging`` is loaded on first use (``_judging``): it reaches the hub's server modules (agent types, the dispatch of
tasks), which import one another in a cycle that resolves only when ``evo_agents.hub.server.runs`` comes first. The api
has loaded them all before a route runs; the job worker, which runs this job, may not have.
"""

from __future__ import annotations

import importlib
import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.canvas import VALUE_CHECK_CRON, VALUE_CHECK_JOB, checks, store
from evo_agents.hub.server.canvas.content import CanvasError

log = logging.getLogger(__name__)

JOB = VALUE_CHECK_JOB  # the plugin's job, and the one the hub runs on a batch of signals (plugin_jobs.SIGNAL_JOB)
CRON = VALUE_CHECK_CRON


def _judging():
    """The module ``judging``, its server modules loaded in the order their cycle needs (see the module)."""
    importlib.import_module("evo_agents.hub.server.runs")
    return importlib.import_module("evo_agents.hub.server.canvas.judging")


async def check_project(engine: AsyncEngine, config, project: str) -> dict:
    """One pass of the value check over the canvases of ``project``; what it did, counted."""
    judging = _judging()
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(tables.projects.c.id).where(tables.projects.c.name == project))
        canvases = await store.project_canvases(conn, project_id) if project_id is not None else []
    done = Counter()
    for found in canvases:
        try:
            async with engine.begin() as conn:
                canvas = await store.lock_canvas(conn, found.id)
                if canvas is None:
                    continue
                hub = await checks.hub_context(conn, project_id, config)
                done += await checks.check_canvas(conn, hub, canvas)
                done["judging_runs"] += await judging.dispatch(conn, hub, canvas, config)
        except CanvasError as exc:
            log.warning(
                "value check of a canvas failed", extra={"project": project, "canvas": found.slug, "why": exc.message}
            )
    done = +done  # what it did, nothing it did not
    if done:
        log.info("value check", extra={"project": project, **done})
    return dict(done)


async def run(params: dict, context: CoreContext) -> dict:
    """The job ``value_check``: one pass over each project ``params`` names (``projects``)."""
    return {
        "projects": {
            project: await check_project(context.engine, context.config, project)
            for project in params.get("projects") or []
        }
    }


__all__ = ["CRON", "JOB", "check_project", "run"]

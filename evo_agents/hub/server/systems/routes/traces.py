"""The traces of an agent system (``docs/hub.md``, Agent systems: reading a trace; ``system_traces``).

- GET /v1/projects/{p}/systems/{slug}/traces: its traces, the newest first, that the caller may read.
- GET .../systems/{slug}/traces/{id}: one trace, its spans in the order written, each with the tokens, cost, state
  model and state of its run (run_spend, runs), the metrics of its nodes and edges, and its timeline by agent
  (``work_observe``).
- GET .../systems/{slug}/metrics[?limit=N]: the metrics of each node and edge over its last N traces the caller may
  read.
- GET .../systems/{slug}/metrics/traces[?days=N]: the numbers of each trace the caller may read that started in the
  last N days, and their spread (``system_trace_stats``): cost, runs, deepest hop, budget refusals, overrun of
  limits.trace_usd, Orchestrator threads, off-design ratio per edge, p50 and p95.
- GET .../systems/{slug}/work/summary[?hours=N]: the numbers of its work over the last N hours (``work_observe``):
  open, needing a person, failed, done, the median time to close, and the cost.
- GET .../systems/{slug}/traces/stream: server-sent ``trace`` events (a ``TraceSummary``) each time a trace of the
  system moves, built on the process's one LISTEN (``system_traces.CHANNEL``); a ``: ping`` keeps it open.

Each route hands the app's engine and the request's parts to ``service.views``, which opens the transaction and says
who reads which trace.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from evo_agents.hub.server import work_observe
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.run_events import JSON_ERROR, RunStreams, StreamRoute
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser
from evo_agents.hub.server.systems.models.systems import Slug
from evo_agents.hub.server.systems.models.traces import (
    MAX_DAYS,
    MAX_HOURS,
    MAX_TRACES,
    READ_REFUSALS,
    Metrics,
    TraceDistribution,
    TraceId,
    TraceSummary,
)
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.hub.server.systems.service import views as service
from evo_agents.hub.server.systems.service.views import TraceOut

log = logging.getLogger(__name__)

STREAM_POLL_SECONDS = 5.0

router = APIRouter(
    prefix="/v1/projects", tags=["systems"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)
stream_router = APIRouter(
    prefix="/v1/projects",
    tags=["systems"],
    route_class=StreamRoute,
    responses={401: {"description": "Unauthorized", **JSON_ERROR}},
)


class TraceStreams(RunStreams):
    """The trace streams open in this api process, by system id, woken by ``system_traces.CHANNEL``."""

    def __init__(self, listener: Listener):
        super().__init__(listener, system_traces.CHANNEL)


# Reading


@router.get("/{project}/systems/{slug}/traces", response_model=list[TraceSummary], responses=READ_REFUSALS)
async def list_traces(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_TRACES)] = 50,
    before: Annotated[int | None, Query(ge=1, le=MAX_ID, description="only traces older than this id")] = None,
) -> list[TraceSummary]:
    """The traces of the system the caller may read, the newest first."""
    return await service.listed(request.app.state.engine, user, project, slug, limit, before)


@router.get("/{project}/systems/{slug}/metrics", response_model=Metrics, responses=READ_REFUSALS)
async def system_metrics(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_TRACES, description="over the last N traces the caller may read")] = 50,
) -> Metrics:
    """The numbers of each node and edge of the system over its last traces: count, errors, off-design crossings,
    median and last seconds, tokens and cost."""
    return await service.metrics(request.app.state.engine, user, project, slug, limit)


@router.get("/{project}/systems/{slug}/metrics/traces", response_model=TraceDistribution, responses=READ_REFUSALS)
async def trace_distribution(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS, description="the traces that started in the last N days")] = 30,
) -> TraceDistribution:
    """The numbers of each trace of the system that started in the last ``days`` days and the caller may read, and
    their spread: p50 and p95 of the cost and the runs, the traces past limits.trace_usd, the budget refusals, the
    loop, idle and rounds threads, and the off-design ratio per edge. It reads only."""
    return await service.distribution(request.app.state.engine, user, project, slug, days)


@router.get("/{project}/systems/{slug}/traces/{trace_id}", response_model=TraceOut, responses=READ_REFUSALS)
async def show_trace(request: Request, project: ProjectName, slug: Slug, trace_id: TraceId, user: CurrentUser):
    """One trace: its spans in the order written, with their runs' numbers, the metrics of its nodes and edges, and its
    timeline by agent."""
    return await service.shown(request.app.state.engine, user, project, slug, trace_id)


@router.get("/{project}/systems/{slug}/work/summary", response_model=work_observe.WorkSummary, responses=READ_REFUSALS)
async def work_summary(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    hours: Annotated[int, Query(ge=1, le=MAX_HOURS, description="the traces that moved in the last N hours")] = 24,
) -> work_observe.WorkSummary:
    """The numbers of the system's work the caller may read that moved in the last ``hours`` hours or has a run
    still active: open, needing a person, failed and done, the median time from start to close of the work done in
    the window, and the cost. It reads only."""
    return await service.work_summary(request.app.state.engine, user, project, slug, hours)


# The live stream


@stream_router.get(
    "/{project}/systems/{slug}/traces/stream",
    response_class=EventSourceResponse,
    responses={403: {"description": "Forbidden", **JSON_ERROR}, 404: {"description": "Not Found", **JSON_ERROR}},
)
async def stream_traces(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    after: Annotated[datetime | None, Query(description="traces that moved after this time come first")] = None,
) -> AsyncIterable[ServerSentEvent]:
    """The traces of the system as they move: a ``trace`` event (``TraceSummary``) for each trace the caller may read
    whose last_at passed, at once for those after ``after`` (none by default), then each time a span is written. An
    ``end`` event once the caller may no longer read the system's project."""
    system_id = await service.system_id(request.app.state.engine, user, project, slug)
    streams: TraceStreams = request.app.state.trace_streams
    streams.start()
    now = await service.now(request.app.state.engine)
    seen = after or now
    with streams.subscribe(system_id or 0) as wakeup:
        while True:
            refusal, moved = await service.poll(request.app.state.engine, user, project, slug, seen)
            if refusal is not None:
                yield ServerSentEvent(event="end", data={"reason": refusal})
                return
            for summary, _ in reversed(moved):
                yield ServerSentEvent(event="trace", data=summary, id=str(summary.id))
                seen = max(seen, summary.last_at)
            await wakeup.wait(STREAM_POLL_SECONDS)

"""The routes of the history of a project: where the history run stands and its clusters (``router``), the batches
and the report of the history run's worker (``worker_router``, with the run's own key), and the sessions, the search
and the lines of a transcript (``search_router``). Each hands the request to the service."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request

from evo_agents.hub import history, review
from evo_agents.hub import transcripts as local
from evo_agents.hub.server.admin import LOGIN_NAME, ProjectName
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.digests import SessionId
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.history.models import (
    KINDS,
    MAX_AROUND,
    MAX_LIMIT,
    MAX_SEARCH,
    QUERY_CHARS,
    SCAN_ROWS,
    SOURCES,
    FacetClusterList,
    HistoryBatch,
    HistoryNext,
    HistoryReport,
    HistoryReported,
    HistorySearch,
    HistorySessions,
    HistoryStatus,
    TranscriptLines,
)
from evo_agents.hub.server.history.service import readers, runs, search
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
search_router = APIRouter(prefix="/v1/projects", tags=["history"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}
SEARCH_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 422)}

Reading = Annotated[Scope, Depends(readers.READERS, scope="function")]
Sink = Annotated[str | None, Header(alias=SINK_HEADER)]
HistoryKey = Annotated[
    str | None,
    Header(
        alias=history.HISTORY_KEY_HEADER,
        max_length=200,
        description="the history run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Day = Annotated[date | None, Query(description="a UTC day, YYYY-MM-DD")]
FileKey = Annotated[str, Query(pattern=review.TRANSCRIPT_FILE, max_length=local.MAX_FILE_KEY)]


# The night's history run


@worker_router.post("/runs/{run_id}/history/next", response_model=HistoryBatch, responses=REFUSALS)
async def next_batch(
    request: Request, run_id: RunId, body: HistoryNext, user: CurrentUser, history_key: HistoryKey = None
) -> HistoryBatch:
    """The next batch of the history run this worker holds, with the run's key (see the module's docstring)."""
    state = request.app.state
    return await runs.next_batch(state.engine, getattr(state, "blobs", None), user, run_id, history_key, body.clusters)


@worker_router.post("/runs/{run_id}/history/report", response_model=HistoryReported, responses=REFUSALS)
async def report_history(
    request: Request, run_id: RunId, body: HistoryReport, user: CurrentUser, history_key: HistoryKey = None
) -> HistoryReported:
    """The facets the model wrote and the names it gave, of the history run this worker holds, with the run's key."""
    return await runs.report(request.app.state.engine, user, run_id, history_key, body)


@router.get("/{project}/curator/history", response_model=HistoryStatus, responses=REFUSALS)
async def history_status(project: ProjectName, scope: Reading, sink: Sink = None) -> HistoryStatus:
    """Where the project's history run stands: its cap and model, the facets of the model, its latest runs."""
    return await runs.status(scope.conn, readers.cleared(scope.access, project, sink))


@router.get("/{project}/curator/history/clusters", response_model=FacetClusterList, responses=REFUSALS)
async def list_clusters(
    project: ProjectName,
    scope: Reading,
    sink: Sink = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FacetClusterList:
    """The clusters of the project's facets of MIN_CLUSTER_SESSIONS sessions or more, as the hub last counted them."""
    return await runs.clusters(scope.conn, readers.cleared(scope.access, project, sink), limit)


# Reading the whole history


@search_router.get("/{project}/curator/history/sessions", response_model=HistorySessions, responses=SEARCH_REFUSALS)
async def history_sessions(
    project: ProjectName,
    scope: Reading,
    kind: Annotated[Literal[KINDS] | None, Query()] = None,  # pyright: ignore[reportInvalidTypeForm]
    since: Day = None,
    until: Day = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="of this member")] = None,
    runtime: Annotated[Literal[local.RUNTIMES] | None, Query()] = None,  # pyright: ignore[reportInvalidTypeForm]
    outcome: Annotated[str | None, Query(pattern=r"^[a-z_]{1,20}$", description="an outcome, or a run's state")] = None,
    cluster: Annotated[str | None, Query(pattern=history.KEY.pattern, description="a cluster's key")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    offset: Annotated[int, Query(ge=0, le=SCAN_ROWS)] = 0,
    sink: Sink = None,
) -> HistorySessions:
    """The sessions and runs of the project's whole history, the latest first, with the numbers of their facets."""
    return await search.list_history(
        scope.conn,
        scope.user,
        scope.access,
        sink,
        kind=kind,
        since=since,
        until=until,
        login=login,
        runtime=runtime,
        outcome=outcome,
        cluster=cluster,
        limit=limit,
        offset=offset,
    )


@search_router.get("/{project}/curator/history/search", response_model=HistorySearch, responses=SEARCH_REFUSALS)
async def history_search(
    request: Request,
    project: ProjectName,
    scope: Reading,
    q: Annotated[str, Query(min_length=1, max_length=QUERY_CHARS, description='words, "a phrase", or -excluded')],
    source: Annotated[list[Literal[SOURCES]], Query(description="these sources alone; repeat it")] = [],  # noqa: B006  # pyright: ignore[reportInvalidTypeForm]
    since: Day = None,
    until: Day = None,
    login: Annotated[str | None, Query(pattern=LOGIN_NAME, description="of this member")] = None,
    runtime: Annotated[Literal[local.RUNTIMES] | None, Query()] = None,  # pyright: ignore[reportInvalidTypeForm]
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH)] = 20,
    offset: Annotated[int, Query(ge=0, le=1000)] = 0,
    sink: Sink = None,
) -> HistorySearch:
    """The words of the query in the project's transcripts, run logs and group chats, best first."""
    return await search.search_history(
        scope.conn,
        scope.user,
        scope.access,
        sink,
        q,
        sources=list(source),
        since=since,
        until=until,
        login=login,
        runtime=runtime,
        limit=limit,
        offset=offset,
        state=request.app.state,
    )


@search_router.get(
    "/{project}/transcripts/{session_id}/lines",
    response_model=TranscriptLines,
    responses={**SEARCH_REFUSALS, 503: {"model": ErrorBody}},
)
async def show_lines(
    request: Request,
    project: ProjectName,
    session_id: SessionId,
    scope: Reading,
    line: Annotated[int, Query(ge=1, le=review.MAX_LINE, description="the line to read around")],
    file: FileKey = local.MAIN,
    before: Annotated[int, Query(ge=0, le=MAX_AROUND)] = 10,
    after: Annotated[int, Query(ge=0, le=MAX_AROUND)] = 10,
    sink: Sink = None,
) -> TranscriptLines:
    """The lines of a transcript around ``line``, each as its entries."""
    store = getattr(request.app.state, "blobs", None)
    return await search.transcript_lines(
        scope.conn,
        scope.access,
        store,
        project,
        session_id,
        line=line,
        file=file,
        before=before,
        after=after,
        sink=sink,
    )

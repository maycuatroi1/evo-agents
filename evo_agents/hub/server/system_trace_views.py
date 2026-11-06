"""The traces of an agent system (``docs/hub.md``, Agent systems: reading a trace; ``system_traces``).

- GET /v1/projects/{p}/systems/{slug}/traces: its traces, the newest first, that the caller may read.
- GET .../systems/{slug}/traces/{id}: one trace, its spans in the order written, each with the tokens, cost, state
  and model of its run (run_spend), and the metrics of its nodes and edges.
- GET .../systems/{slug}/metrics[?limit=N]: the metrics of each node and edge over its last N traces the caller may
  read.
- GET .../systems/{slug}/traces/stream: server-sent ``trace`` events (a ``TraceSummary``) each time a trace of the
  system moves, built on the process's one LISTEN (``system_traces.CHANNEL``); a ``: ping`` keeps it open.

A trace follows its chat's audience: a member of the system's project reads a trace whose chat they are in (and whose
label they clear), or one with no chat left. A span of a run in another project shows its content (its reason, its
message and the attributes the hub adds) only to a member with a grant on that project; the others see that it ran,
on which node, and its numbers. Content stays where it is: a span names its message and run, which their own routes
show.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context, system_spec, tables
from evo_agents.hub.server import agent_tasking, route_autonomy, system_traces
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.listen import Listener
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_events import JSON_ERROR, RunStreams, StreamRoute
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.security import CurrentUser, Principal, still_live

log = logging.getLogger(__name__)

MAX_TRACES = 200  # traces a list or the metrics read at most
STREAM_POLL_SECONDS = 5.0
SHOWN_ATTRIBUTES = ("gen_ai.",)  # the attributes a reader without a grant on a span's project still sees

Slug = Annotated[str, Path(min_length=1, max_length=63, pattern=system_spec.SLUG_PATTERN)]
TraceId = Annotated[int, Path(ge=1, le=MAX_ID)]
READ_REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}

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


class SpanOut(BaseModel):
    id: int
    parent_id: int | None = Field(description="the span it runs under; null for the first span of the trace")
    name: str = Field(
        description="invoke_agent (a turn of an agent) or execute_tool (an ask, a mention, a forward, a kg query)"
    )
    tool: str | None = Field(
        description="the tool of an execute_tool span: agent_ask, chat_mention, agent_forward, kg_*"
    )
    agent: str = Field(description="the handle of the agent")
    node: str | None = Field(description="its node in the spec; @handle for an external agent the spec lacks")
    from_node: str | None = Field(description="the node it came from: the edge it crossed")
    to_node: str | None = Field(description="the node an ask, a mention or a forward went to")
    run_id: int | None
    project: str | None = Field(description="the project of its run")
    runtime: str | None
    status: str = Field(description="running, ok, error or refused")
    off_design: bool = Field(description="the edge it crossed is not an interacts edge of the spec")
    reason: str | None = Field(description="why: the reason an off-edge ask gave, or why the hub refused")
    message_id: int | None = Field(description="the chat message it rests on, which the chat's routes show")
    attributes: dict = Field(description="OpenTelemetry GenAI attributes (gen_ai.*) and the hub's (evo.*)")
    started_at: datetime
    ended_at: datetime | None
    seconds: float | None = Field(description="how long it ran; null while it runs")
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = Field(0, description="what its run cost, reported else estimated; 0 for a tool span")
    model: str | None = None
    content_withheld: bool = Field(
        False,
        description="its run is of a project the reader holds no grant on: reason, message and evo.* are left out",
    )


class TraceSummary(BaseModel):
    id: int
    system: str = Field(description="the slug of the system")
    revision: int = Field(description="the revision of its spec the trace ran under")
    chat_id: int | None
    message_id: int | None = Field(description="the person's message that opened it")
    opened_by: str | None = Field(description="the login of who wrote that message")
    started_at: datetime
    last_at: datetime
    runs: int = Field(description="its invoke_agent spans")
    off_design: int = Field(description="its spans that crossed an edge the spec lacks")
    errors: int
    usd: float


class Metrics(BaseModel):
    nodes: list[dict] = Field(description="per node: count, errors, off_design, p50_seconds, last_seconds, tokens, usd")
    edges: list[dict] = Field(description="per edge (from_node, to_node): the same numbers")


class TraceOut(TraceSummary):
    spans: list[SpanOut]
    metrics: Metrics


# Reading


async def _system(conn: AsyncConnection, user: Principal, project: str, slug: str) -> tuple[ProjectAccess, int | None]:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the traces of project {access.name} needs a grant on it")
    s = tables.agent_systems
    query = select(s.c.id).where(s.c.project_id == access.project_id, s.c.slug == slug)
    system_id = (await conn.execute(query)).scalar_one_or_none()
    t = tables.system_traces
    if system_id is None:
        held = select(t.c.id).where(t.c.project_id == access.project_id, t.c.system_slug == slug).limit(1)
        if (await conn.execute(held)).first() is None:
            raise HTTPException(404, f"project {access.name} has no agent system {slug} and no trace of one")
    return access, system_id


async def _readable(conn: AsyncConnection, user: Principal, chat_id: int | None) -> bool:
    if chat_id is None:
        return True
    nested = await conn.begin_nested()
    try:
        await chat_access(conn, user, chat_id)
    except HTTPException:
        await nested.rollback()
        return False
    await nested.commit()
    return True


def _trace_query(access: ProjectAccess, slug: str):
    t, u = tables.system_traces, tables.users
    return (
        select(
            t.c.id,
            t.c.system_slug,
            t.c.system_revision,
            t.c.chat_id,
            t.c.message_id,
            u.c.login,
            t.c.started_at,
            t.c.last_at,
        )
        .outerjoin(u, u.c.id == t.c.opened_by)
        .where(t.c.project_id == access.project_id, t.c.system_slug == slug)
    )


async def _spans(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, list]:
    s, p = tables.system_spans, tables.projects
    query = (
        select(s, p.c.name.label("project"))
        .outerjoin(p, p.c.id == s.c.project_id)
        .where(s.c.trace_id.in_(trace_ids))
        .order_by(s.c.trace_id, s.c.id)
    )
    found: dict[int, list] = {trace_id: [] for trace_id in trace_ids}
    for row in await conn.execute(query):
        found[row.trace_id].append(row)
    return found


async def _granted(conn: AsyncConnection, user: Principal, project_ids: set[int]) -> set[int]:
    return {
        project_id
        for project_id in project_ids
        if await agent_tasking.project_role(conn, user.user_id, project_id) is not None
    }


def _span_out(row, numbers: dict, own_project: int, granted: set[int]) -> SpanOut:
    withheld = row.project_id is not None and row.project_id != own_project and row.project_id not in granted
    attributes = dict(row.attributes or {})
    if withheld:
        attributes = {key: value for key, value in attributes.items() if key.startswith(SHOWN_ATTRIBUTES)}
    seconds = (row.ended_at - row.started_at).total_seconds() if row.ended_at is not None else None
    run = numbers.get(row.run_id, {}) if row.name == system_context.INVOKE else {}
    return SpanOut(
        id=row.id,
        parent_id=row.parent_id,
        name=row.name,
        tool=row.tool,
        agent=row.agent,
        node=row.node,
        from_node=row.from_node,
        to_node=row.to_node,
        run_id=row.run_id,
        project=row.project,
        runtime=row.runtime,
        status=row.status,
        off_design=row.off_design,
        reason=None if withheld else row.reason,
        message_id=None if withheld else row.message_id,
        attributes=attributes,
        started_at=row.started_at,
        ended_at=row.ended_at,
        seconds=seconds,
        input_tokens=run.get("input_tokens", 0),
        output_tokens=run.get("output_tokens", 0),
        usd=run.get("usd", 0.0),
        model=run.get("model"),
        content_withheld=withheld,
    )


def _metric_rows(spans: list[SpanOut]) -> list[dict]:
    return [
        {
            "node": span.node,
            "from_node": span.from_node,
            "status": span.status,
            "off_design": span.off_design,
            "resume": system_traces.RESUME in span.attributes,
            "seconds": span.seconds,
            "started_at": span.started_at.isoformat(),
            "input_tokens": span.input_tokens,
            "output_tokens": span.output_tokens,
            "usd": span.usd,
        }
        for span in spans
        if span.name == system_context.INVOKE
    ]


def _summary(row, spans: list[SpanOut]) -> TraceSummary:
    invoked = [span for span in spans if span.name == system_context.INVOKE]
    return TraceSummary(
        id=row.id,
        system=row.system_slug,
        revision=row.system_revision,
        chat_id=row.chat_id,
        message_id=row.message_id,
        opened_by=row.login,
        started_at=row.started_at,
        last_at=row.last_at,
        runs=len(invoked),
        off_design=sum(1 for span in spans if span.off_design),
        errors=sum(1 for span in spans if span.status == "error"),
        usd=round(sum(span.usd for span in invoked), 6),
    )


async def read_traces(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, slug: str, query, limit: int
) -> list[tuple[TraceSummary, list[SpanOut]]]:
    """The traces ``query`` selects (``_trace_query``), the newest first, at most ``limit`` the caller may read, each
    with its spans as the caller may see them."""
    t = tables.system_traces
    rows = []
    for row in await conn.execute(query.order_by(t.c.id.desc()).limit(MAX_TRACES)):
        if await _readable(conn, user, row.chat_id):
            rows.append(row)
        if len(rows) >= limit:
            break
    spans = await _spans(conn, [row.id for row in rows])
    run_ids = sorted({span.run_id for found in spans.values() for span in found if span.run_id is not None})
    numbers = await system_traces.run_numbers(conn, run_ids)
    others = {span.project_id for found in spans.values() for span in found if span.project_id is not None}
    granted = await _granted(conn, user, others - {access.project_id})
    out = []
    for row in rows:
        shown = [_span_out(span, numbers, access.project_id, granted) for span in spans[row.id]]
        out.append((_summary(row, shown), shown))
    return out


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
    async with request.app.state.engine.begin() as conn:
        access, _ = await _system(conn, user, project, slug)
        query = _trace_query(access, slug)
        if before is not None:
            query = query.where(tables.system_traces.c.id < before)
        found = await read_traces(conn, user, access, slug, query, limit)
    return [summary for summary, _ in found]


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
    async with request.app.state.engine.begin() as conn:
        access, _ = await _system(conn, user, project, slug)
        found = await read_traces(conn, user, access, slug, _trace_query(access, slug), limit)
    rows = [row for _, spans in found for row in _metric_rows(spans)]
    return Metrics(**system_context.metrics(rows))


@router.get("/{project}/systems/{slug}/traces/{trace_id}", response_model=TraceOut, responses=READ_REFUSALS)
async def show_trace(request: Request, project: ProjectName, slug: Slug, trace_id: TraceId, user: CurrentUser):
    """One trace: its spans in the order written, with their runs' numbers, and the metrics of its nodes and edges."""
    async with request.app.state.engine.begin() as conn:
        access, _ = await _system(conn, user, project, slug)
        query = _trace_query(access, slug).where(tables.system_traces.c.id == trace_id)
        found = await read_traces(conn, user, access, slug, query, 1)
    if not found:
        raise HTTPException(404, f"system {slug} of project {project} has no trace {trace_id} you may read")
    summary, spans = found[0]
    metrics = Metrics(**system_context.metrics(_metric_rows(spans)))
    return TraceOut(**summary.model_dump(), spans=spans, metrics=metrics)


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
    async with request.app.state.engine.begin() as conn:
        _, system_id = await _system(conn, user, project, slug)
    streams: TraceStreams = request.app.state.trace_streams
    streams.start()
    t = tables.system_traces
    async with request.app.state.engine.begin() as conn:
        now = (await conn.execute(select(func.now()))).scalar_one()
    seen = after or now
    with streams.subscribe(system_id or 0) as wakeup:
        while True:
            refusal, moved = None, []
            async with request.app.state.engine.begin() as conn:
                try:
                    if not await still_live(conn, user):
                        raise HTTPException(401, "the token that opened this stream no longer holds")
                    access, _ = await _system(conn, user, project, slug)
                    query = _trace_query(access, slug).where(t.c.last_at > seen)
                    moved = await read_traces(conn, user, access, slug, query, MAX_TRACES)
                except HTTPException as refused:
                    refusal = refused.detail
            if refusal is not None:
                yield ServerSentEvent(event="end", data={"reason": refusal})
                return
            for summary, _ in reversed(moved):
                yield ServerSentEvent(event="trace", data=summary, id=str(summary.id))
                seen = max(seen, summary.last_at)
            await wakeup.wait(STREAM_POLL_SECONDS)

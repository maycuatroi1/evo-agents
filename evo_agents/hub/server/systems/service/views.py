"""The reads of the traces of an agent system (``docs/hub.md``, Agent systems: reading a trace): who reads which trace,
and what the routes of ``routes.traces`` give.

A trace follows its chat's audience: a member of the system's project reads a trace whose chat they are in (and whose
label they clear), or one with no chat left. A span of a run in another project shows its content (its reason, its
message and the attributes the hub adds) only to a member with a grant on that project; the others see that it ran,
on which node, and its numbers. Content stays where it is: a span names its message and run, which their own routes
show.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import system_context
from evo_agents.hub.server import work_observe, work_status
from evo_agents.hub.server.agents.repository import tasking as agent_tasking
from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, still_live
from evo_agents.hub.server.systems.models import traces as models
from evo_agents.hub.server.systems.models.traces import (
    MAX_TRACES,
    SHOWN_ATTRIBUTES,
    Metrics,
    SpanOut,
    TraceDistribution,
    TraceNumbers,
    TraceSpread,
    TraceSummary,
)
from evo_agents.hub.server.systems.repository import stats as system_trace_stats
from evo_agents.hub.server.systems.repository import views as rows_of
from evo_agents.hub.server.systems.service import traces as system_traces
from evo_agents.hub.server.work_status_models import WorkBrief


class TraceOut(TraceSummary):
    spans: list[SpanOut]
    metrics: Metrics
    timeline: work_observe.TraceTimeline = Field(
        default_factory=work_observe.TraceTimeline,
        description="each run of it cut into queued, running, parked and waiting, and the decisions that ask a person",
    )


async def system(conn: AsyncConnection, user: Principal, project: str, slug: str) -> tuple[ProjectAccess, int | None]:
    """The caller's access to ``project`` and the id of its system ``slug`` (None for a deleted system whose traces
    stay); refused (403) without a grant on the project, (404) when it holds no such system and no trace of one."""
    access = await project_access(conn, user, project)
    if access.role is None:
        raise Forbidden(f"reading the traces of project {access.name} needs a grant on it")
    system_id = await rows_of.system_id(conn, access.project_id, slug)
    if system_id is None and not await rows_of.has_trace(conn, access.project_id, slug):
        raise NotFound(f"project {access.name} has no agent system {slug} and no trace of one")
    return access, system_id


async def _reads(conn: AsyncConnection, user: Principal, chat_id: int) -> bool:
    nested = await conn.begin_nested()
    try:
        await chat_access(conn, user, chat_id)
    except HTTPException:
        await nested.rollback()
        return False
    await nested.commit()
    return True


async def _readable(
    conn: AsyncConnection, user: Principal, chat_id: int | None, work_chat_id: int | None = None
) -> bool:
    """Whether ``user`` reads a trace opened in chat ``chat_id`` (None: anyone who reads the system does) whose work
    chat is ``work_chat_id``: they read either chat."""
    if chat_id is None:
        return True
    if await _reads(conn, user, chat_id):
        return True
    return work_chat_id is not None and await _reads(conn, user, work_chat_id)


async def _kept(conn: AsyncConnection, user: Principal, rows: list) -> list:
    """Those of ``rows`` (traces with ``chat_id`` and ``work_chat_id``) that ``user`` reads, in their order."""
    readable: dict[tuple[int | None, int | None], bool] = {}
    kept = []
    for row in rows:
        chats_of = (row.chat_id, row.work_chat_id)
        if chats_of not in readable:
            readable[chats_of] = await _readable(conn, user, *chats_of)
        if readable[chats_of]:
            kept.append(row)
    return kept


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
        run_state=run.get("state"),
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


def _summary(row, spans: list[SpanOut], work: WorkBrief | None) -> TraceSummary:
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
        work_chat_id=row.work_chat_id,
        title=None if work is None else work.title,
        goal=row.goal,
        done_when=row.done_when,
        status=None if work is None else work.status,
        agent=None if work is None else work.agent,
        waiting=None if work is None else work.waiting,
    )


async def read_traces(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    slug: str,
    limit: int,
    *,
    before: int | None = None,
    trace_id: int | None = None,
    moved_after: datetime | None = None,
) -> list[tuple[TraceSummary, list[SpanOut]]]:
    """The traces of system ``slug`` (older than ``before``, only ``trace_id``, or moved after ``moved_after``, when
    given), the newest first, at most ``limit`` the caller may read, each with its spans as the caller may see them."""
    rows = []
    found_rows = await rows_of.traces(
        conn, access.project_id, slug, MAX_TRACES, before=before, trace_id=trace_id, moved_after=moved_after
    )
    for row in found_rows:
        if await _readable(conn, user, row.chat_id, row.work_chat_id):
            rows.append(row)
        if len(rows) >= limit:
            break
    spans = await rows_of.spans(conn, [row.id for row in rows])
    run_ids = sorted({span.run_id for found in spans.values() for span in found if span.run_id is not None})
    numbers = await system_traces.run_numbers(conn, run_ids)
    others = {span.project_id for found in spans.values() for span in found if span.project_id is not None}
    granted = await _granted(conn, user, others - {access.project_id})
    work = await work_status.briefs(conn, [row.id for row in rows])
    out = []
    for row in rows:
        shown = [_span_out(span, numbers, access.project_id, granted) for span in spans[row.id]]
        out.append((_summary(row, shown, work.get(row.id)), shown))
    return out


async def listed(
    conn: AsyncConnection, user: Principal, project: str, slug: str, limit: int, before: int | None
) -> list[TraceSummary]:
    """The traces of the system the caller may read, the newest first."""
    access, _ = await system(conn, user, project, slug)
    return [summary for summary, _ in await read_traces(conn, user, access, slug, limit, before=before)]


async def metrics(conn: AsyncConnection, user: Principal, project: str, slug: str, limit: int) -> Metrics:
    """The numbers of each node and edge of the system over its last ``limit`` traces the caller may read."""
    access, _ = await system(conn, user, project, slug)
    found = await read_traces(conn, user, access, slug, limit)
    return Metrics(**system_context.metrics([row for _, spans in found for row in _metric_rows(spans)]))


async def distribution(conn: AsyncConnection, user: Principal, project: str, slug: str, days: int) -> TraceDistribution:
    """The numbers of each trace of the system that started in the last ``days`` days and the caller may read, and
    their spread."""
    access, _ = await system(conn, user, project, slug)
    since = await rows_of.now(conn) - timedelta(days=days)
    window = models.MAX_WINDOW_TRACES
    rows = await rows_of.window(conn, access.project_id, slug, since, window + 1)
    truncated = len(rows) > window
    rows = rows[:window]
    kept = await _kept(conn, user, rows)
    found = await system_trace_stats.numbers(conn, [(row.id, row.system_id, row.system_revision) for row in kept])
    still = await rows_of.running(conn, [row.id for row in kept])
    traces = [
        TraceNumbers(
            id=row.id,
            revision=row.system_revision,
            started_at=row.started_at,
            last_at=row.last_at,
            open=row.id in still,
            **found[row.id],
        )
        for row in kept
    ]
    summary = TraceSpread(**system_context.distribution([found[row.id] for row in kept]))
    return TraceDistribution(
        system=slug,
        days=days,
        since=since,
        withheld=len(rows) - len(kept),
        truncated=truncated,
        summary=summary,
        traces=traces,
    )


async def shown(conn: AsyncConnection, user: Principal, project: str, slug: str, trace_id: int) -> TraceOut:
    """One trace: its spans in the order written, with their runs' numbers, the metrics of its nodes and edges, and its
    timeline by agent."""
    access, _ = await system(conn, user, project, slug)
    found = await read_traces(conn, user, access, slug, 1, trace_id=trace_id)
    if not found:
        raise NotFound(f"system {slug} of project {project} has no trace {trace_id} you may read")
    summary, spans = found[0]
    timeline = await work_observe.timeline(conn, trace_id, spans)
    found_metrics = Metrics(**system_context.metrics(_metric_rows(spans)))
    return TraceOut(**summary.model_dump(), spans=spans, metrics=found_metrics, timeline=timeline)


async def work_summary(
    conn: AsyncConnection, user: Principal, project: str, slug: str, hours: int
) -> work_observe.WorkSummary:
    """The numbers of the system's work the caller may read that moved in the last ``hours`` hours or has a run still
    active."""
    access, _ = await system(conn, user, project, slug)
    since = await rows_of.now(conn) - timedelta(hours=hours)
    active = work_observe.active_traces()
    rows = await rows_of.moved(conn, access.project_id, slug, since, active, models.MAX_WINDOW_TRACES)
    kept = await _kept(conn, user, rows)
    return await work_observe.summary(conn, kept, hours, since, len(rows) - len(kept))


async def now(conn: AsyncConnection) -> datetime:
    """The database's clock, from which a stream counts what moved."""
    return await rows_of.now(conn)


async def moved(
    conn: AsyncConnection, user: Principal, project: str, slug: str, seen: datetime
) -> list[tuple[TraceSummary, list[SpanOut]]]:
    """The traces of the system the caller may read that moved after ``seen``; refused (401) once the token that
    opened the stream no longer holds, and as ``system`` refuses."""
    if not await still_live(conn, user):
        raise HTTPException(401, "the token that opened this stream no longer holds")
    access, _ = await system(conn, user, project, slug)
    return await read_traces(conn, user, access, slug, MAX_TRACES, moved_after=seen)

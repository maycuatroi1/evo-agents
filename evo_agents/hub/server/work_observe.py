"""What the Observe of an agent system reads of its work (``docs/hub.md``, Agent systems: reading a trace), beside the
spans of ``system_trace_views``: the timeline of a trace by agent, and the numbers of the system's work over a window.

The timeline of a trace has a lane per run of it (each ``invoke_agent`` span with a run), cut into segments from the
run's own times: ``queued`` from queued_at to when a worker took it (leased_at or started_at), ``running`` from then to
its end, or to when it parked (``parked``, until its end: a parked chat run ends once a new run goes on in its session)
or began to wait for an answer while its worker holds it (``waiting``, until now). A segment still going has no end.
The marks are the decisions of the trace that ask a person (``work_status``: of its runs and of the Orchestrator's runs
on its threads, in the system's project, not a wait), when each was asked and answered.

The numbers of the work (``summary``) cover the traces that moved in the window or have a run still active: how many
are open (running or needing a person), need a person, failed or are done, the median time from start to last move of
those done in the window, and their cost as the budget counts it (``system_trace_stats.tree_usage``). Each reads the
same few queries however many traces it covers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, system_context, tables
from evo_agents.hub.server import system_trace_stats, work_status

SegmentKind = Literal["queued", "running", "parked", "waiting"]


class TimelineSegment(BaseModel):
    kind: SegmentKind = Field(description="queued for a worker, running, parked, or waiting for an answer held")
    started_at: datetime
    ended_at: datetime | None = Field(description="null while it goes on")


class TimelineRun(BaseModel):
    span_id: int = Field(description="its invoke_agent span")
    run_id: int
    agent: str = Field(description="the handle of the agent")
    node: str | None
    state: str = Field(description="the state of the run now")
    segments: list[TimelineSegment] = Field(description="oldest first")


class TimelineMark(BaseModel):
    decision_id: int
    run_id: int = Field(description="the run that asked")
    agent: str | None = Field(description="the handle of the agent of that run")
    login: str | None = Field(description="the person it asks")
    question: str
    state: str = Field(description="open, answered, expired or cancelled")
    asked_at: datetime
    answered_at: datetime | None


class TraceTimeline(BaseModel):
    runs: list[TimelineRun] = Field(default_factory=list, description="in the order of their spans")
    marks: list[TimelineMark] = Field(default_factory=list, description="the decisions that ask a person, oldest first")


class WorkSummary(BaseModel):
    hours: int
    since: datetime
    traces: int = Field(description="the traces counted: moved since `since`, or with a run still active")
    withheld: int = Field(description="such traces the caller may not read, left out")
    open: int = Field(description="running or needing a person")
    needs_person: int
    failed: int
    done: int
    closed_p50_seconds: float | None = Field(description="median start to last move of the work done in the window")
    usd: float = Field(description="what the traces counted cost, their spawned runs included")


def _segments(run, now: datetime) -> list[TimelineSegment]:
    """The segments of run row ``run`` (see the module's docstring), ``now`` the database's time."""
    end = run.finished_at
    began = run.started_at or run.leased_at
    found: list[tuple[str, datetime, datetime | None]] = [("queued", run.queued_at, began or end)]
    if began is not None:
        parked = run.parked_at if run.parked_at is not None and run.parked_at >= began else None
        if parked is not None and end is not None and parked > end:
            parked = None
        waiting = None
        if parked is None and run.state == "waiting" and run.waiting_since is not None and run.waiting_since >= began:
            waiting = run.waiting_since
        found.append(("running", began, parked or waiting or end))
        if parked is not None:
            found.append(("parked", parked, end))
        elif waiting is not None:
            found.append(("waiting", waiting, None))
    return [
        TimelineSegment(kind=kind, started_at=start, ended_at=stop)  # type: ignore[arg-type]  # one of SegmentKind
        for kind, start, stop in found
        if stop is None and start <= now or stop is not None and stop > start
    ]


async def timeline(conn: AsyncConnection, trace_id: int, spans: list) -> TraceTimeline:
    """The timeline of trace ``trace_id`` whose spans (``SpanOut``) are ``spans``."""
    lanes = [span for span in spans if span.name == system_context.INVOKE and span.run_id is not None]
    r = tables.runs
    found = {}
    if lanes:
        query = select(
            r.c.id,
            r.c.state,
            r.c.queued_at,
            r.c.leased_at,
            r.c.started_at,
            r.c.finished_at,
            r.c.parked_at,
            r.c.waiting_since,
        ).where(r.c.id.in_(sorted({span.run_id for span in lanes})))
        found = {row.id: row for row in await conn.execute(query)}
    now = (await conn.execute(select(func.now()))).scalar_one()
    out = TraceTimeline()
    for span in lanes:
        run = found.get(span.run_id)
        if run is None:
            continue
        out.runs.append(
            TimelineRun(
                span_id=span.id,
                run_id=span.run_id,
                agent=span.agent,
                node=span.node,
                state=run.state,
                segments=_segments(run, now),
            )
        )
    out.marks = await _marks(conn, trace_id)
    return out


async def _marks(conn: AsyncConnection, trace_id: int) -> list[TimelineMark]:
    asked = await work_status.person_decisions(conn, trace_id)
    if not asked:
        return []
    r, p = tables.runs, tables.agent_profiles
    query = select(r.c.id, p.c.handle).join(p, p.c.id == r.c.profile_id).where(r.c.id.in_({d.run_id for d in asked}))
    handles = {key: value for key, value in await conn.execute(query)}
    return [
        TimelineMark(
            decision_id=d.id,
            run_id=d.run_id,
            agent=handles.get(d.run_id),
            login=d.asked_login,
            question=d.question,
            state=d.state,
            asked_at=d.asked_at,
            answered_at=d.answered_at,
        )
        for d in asked
    ]


async def summary(conn: AsyncConnection, rows: list, hours: int, since: datetime, withheld: int) -> WorkSummary:
    """The numbers of the work of the traces ``rows`` (each with id, started_at and last_at), which the caller reads
    of those that moved since ``since`` or have a run active, ``withheld`` the others."""
    ids = [row.id for row in rows]
    briefs = await work_status.briefs(conn, ids)
    used = await system_trace_stats.tree_usage(conn, ids) if ids else {}
    statuses = [briefs[trace_id].status for trace_id in ids if trace_id in briefs]
    closed = [
        (row.last_at - row.started_at).total_seconds()
        for row in rows
        if row.id in briefs and briefs[row.id].status == "done" and row.last_at >= since
    ]
    return WorkSummary(
        hours=hours,
        since=since,
        traces=len(rows),
        withheld=withheld,
        open=sum(1 for status in statuses if status in ("running", "needs_person")),
        needs_person=statuses.count("needs_person"),
        failed=statuses.count("failed"),
        done=statuses.count("done"),
        closed_p50_seconds=system_context.percentile(closed, 0.5),
        usd=round(sum(used.get(trace_id, (0, 0.0, None))[1] for trace_id in ids), 6),
    )


def active_traces():
    """The traces with a run still active (``runs.ACTIVE_STATES``), as a subquery of their ids."""
    s, r = tables.system_spans, tables.runs
    return (
        select(s.c.trace_id)
        .join(r, r.c.id == s.c.run_id)
        .where(s.c.name == system_context.INVOKE, r.c.state.in_(runs.ACTIVE_STATES))
    )

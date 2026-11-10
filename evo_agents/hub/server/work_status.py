"""The status of the work of a trace of an agent system, and its work card (``docs/hub.md``, The work card),
derived from the trace's runs, decisions and Orchestrator threads alone: no model is asked.

The status (STATUSES), the first that holds:

- ``needs_person``: a decision is open that asks a person: of a run of the trace (or of an Orchestrator's run on one of
  its threads) in the system's project, not a wait (``runs.WAIT_DECISION_CATEGORY``, which asks nobody).
- ``failed``: the coordinator's (the system's entry agent's) latest run of the trace failed or was lost, or a thread of
  rule ``failed`` of the Orchestrator about the trace has no outcome yet.
- ``running``: a run of the trace is active (``runs.ACTIVE_STATES``).
- ``done``: none of these.

What it waits for (WAITS), the first that holds: ``decision``, the open decision that asks a person; ``person``, a run
a person drives (``interactive``) or reviews (``review``); ``run``, the run an open wait of a run of the trace awaits
(``decisions.awaits_run_id``, the run a wait spawned, or the Orchestrator's run of the thread whose wait it is); else
none.
``agent`` is the handle of the latest run of the trace at work (WORKING).

``briefs`` gives this for many traces at once, ``cards`` the whole card: what the entry agent set (``work set``; the
first line of the trace's first message is the title until then), the sub-tasks (each ask, escalation and reassignment:
the agent, the goal of its brief, its status, run, cost and thread), the decisions (the Orchestrator's threads with
their outcome, and the decisions that ask a person with their answer), the runs, cost and deepest hop against the
limits of the trace's revision, and the timing. Each takes the same few queries however many traces it reads (the
limits: one read of each revision of a system among them).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs, runs, system_context, tables
from evo_agents.hub.server import system_store, system_trace_stats, system_traces

WorkStatus = Literal["running", "needs_person", "failed", "done"]
STATUSES: tuple[str, ...] = get_args(WorkStatus)
WaitKind = Literal["run", "person", "decision"]
WAITS: tuple[str, ...] = get_args(WaitKind)
AskKind = Literal["ask", "escalate", "reassign"]
ASK_TOOLS = {
    system_context.TOOL_ASK: "ask",
    system_context.TOOL_ESCALATE: "escalate",
    system_context.TOOL_REASSIGN: "reassign",
}
WORKING = ("queued", "leased", "running", "interactive", "verifying")  # a run whose agent is at work
PERSON_STATES = ("interactive", "review")  # a run a person drives or reviews
FAILED_STATES = ("failed", "lost")
TITLE_CHARS = 80  # the default title: the first line of the trace's first message
CHILD = "evo.child_run_id"


class WorkSystem(BaseModel):
    id: int | None = Field(description="null once the system is deleted")
    slug: str
    name: str | None = Field(description="null once the system is deleted")
    project: str | None = Field(default=None, description="the name of its project")


class WorkWaiting(BaseModel):
    kind: WaitKind = Field(description="run: an agent's run; person: a person drives or reviews a run; decision")
    run_id: int | None = Field(default=None, description="the run awaited, or the run a person drives")
    agent: str | None = Field(default=None, description="the handle of the agent of that run")
    decision_id: int | None = Field(default=None, description="the open decision that asks a person")
    login: str | None = Field(default=None, description="the person the decision asks")
    question: str | None = None


class WorkBrief(BaseModel):
    """What a list shows of the work of a trace."""

    trace_id: int
    system: WorkSystem
    title: str = Field(description="what the entry agent set, else the first line of the trace's first message")
    status: WorkStatus
    agent: str | None = Field(description="the handle of the agent at work now; null when none is")
    waiting: WorkWaiting | None = Field(description="what the work waits for; null for nothing")
    coordinator: str | None = Field(
        default=None, description="the handle of the system's entry agent, which coordinates the work"
    )


class WorkThread(BaseModel):
    chat_id: int
    root_id: int


class WorkAsk(BaseModel):
    kind: AskKind = Field(description="ask: an agent's ask; escalate: to the Orchestrator; reassign: its reassignment")
    from_agent: str = Field(description="the handle of the agent that handed the work")
    agent: str | None = Field(description="the handle of the agent that took it; null when refused")
    brief: str | None = Field(description="the goal of its brief")
    status: str = Field(description="the state of the run it made; else its span's status (refused, error)")
    run_id: int | None
    usd: float
    thread: WorkThread | None = Field(description="the thread of its question")


class WorkDecision(BaseModel):
    kind: Literal["orchestrator", "person"] = Field(
        description="orchestrator: a thread of the Orchestrator; person: a decision that asks a person"
    )
    id: int = Field(description="the thread's id, or the decision's")
    rule: str | None = Field(description="the rule that opened the thread (failed, escalate, ...)")
    outcome: str | None = Field(description="answer, reassign or ask_owner; null while the thread is open")
    question: str | None
    state: str = Field(description="open or closed for a thread; the decision's state")
    answer: str | None = Field(description="the option and words of the answer")
    asked_of: str | None
    run_id: int | None = Field(description="the Orchestrator's run, or the run that asked")
    at: datetime


class WorkCard(WorkBrief):
    """The work card of a trace: its root-thread message carries it, GET /v1/chats/{id}/work answers it."""

    chat_id: int | None = Field(description="the chat of the message that opened the trace")
    message_id: int | None
    work_chat_id: int | None
    goal: str | None
    done_when: str | None
    asks: list[WorkAsk] = Field(description="the sub-tasks, oldest first")
    decisions: list[WorkDecision] = Field(description="oldest first")
    runs: int
    usd: float
    hop: int | None = Field(description="the deepest hop of its runs")
    max_hops: int
    trace_runs: int = Field(description="the runs a trace of its revision may make")
    trace_usd: float = Field(description="the USD a trace of its revision may spend")
    started_at: datetime
    ended_at: datetime | None = Field(description="its last span, once no run of it is active; null while running")
    seconds: float = Field(description="from its start to its end, or to now")


@dataclass
class _Read:
    """What the queries read of a batch of traces."""

    traces: dict[int, Any]  # trace: its row of _trace_query
    spans: dict[int, list] = field(default_factory=dict)  # trace: its invoke_agent spans, oldest first
    threads: dict[int, list] = field(default_factory=dict)
    decisions: dict[int, list] = field(default_factory=dict)
    awaited: dict[int, int] = field(default_factory=dict)  # open wait: the run it awaits
    handles: dict[int, str] = field(default_factory=dict)  # run: the handle of its agent
    first_lines: dict[int, str] = field(default_factory=dict)


def _trace_query(trace_ids: list[int]):
    t, s, m = tables.system_traces, tables.agent_systems, tables.chat_messages
    p, a = tables.projects, tables.agent_profiles
    return (
        select(
            t.c.id,
            t.c.system_id,
            t.c.system_slug,
            t.c.system_revision,
            t.c.project_id,
            t.c.chat_id,
            t.c.message_id,
            t.c.work_chat_id,
            t.c.title,
            t.c.goal,
            t.c.done_when,
            t.c.started_at,
            t.c.last_at,
            s.c.name.label("system_name"),
            s.c.entry_profile_id,
            m.c.body.label("first_body"),
            p.c.name.label("project_name"),
            a.c.handle.label("coordinator"),
        )
        .select_from(
            t.outerjoin(s, s.c.id == t.c.system_id)
            .outerjoin(m, m.c.id == t.c.message_id)
            .outerjoin(p, p.c.id == t.c.project_id)
            .outerjoin(a, a.c.id == s.c.entry_profile_id)
        )
        .where(t.c.id.in_(trace_ids))
    )


def first_line(body: str | None) -> str:
    """The default title of a trace whose first message is ``body``: its first line that holds words, clipped."""
    line = next((part.strip() for part in (body or "").splitlines() if part.strip()), "")
    return line if len(line) <= TITLE_CHARS else line[: TITLE_CHARS - 3].rstrip() + "..."


async def _spans(conn: AsyncConnection, read: _Read, trace_ids: list[int]) -> None:
    s, r = tables.system_spans, tables.runs
    query = (
        select(s.c.trace_id, s.c.run_id, s.c.agent, s.c.started_at, s.c.ended_at, r.c.state, r.c.profile_id)
        .join(r, r.c.id == s.c.run_id)
        .where(s.c.trace_id.in_(trace_ids), s.c.name == system_context.INVOKE)
        .order_by(s.c.trace_id, s.c.id)
    )
    for row in await conn.execute(query):
        read.spans.setdefault(row.trace_id, []).append(row)
        read.handles[row.run_id] = row.agent


async def _threads(conn: AsyncConnection, read: _Read, trace_ids: list[int]) -> None:
    o = tables.orchestrator_threads
    query = select(o).where(o.c.trace_id.in_(trace_ids)).order_by(o.c.trace_id, o.c.id)
    for row in await conn.execute(query):
        read.threads.setdefault(row.trace_id, []).append(row)


async def _decisions(conn: AsyncConnection, read: _Read) -> None:
    """The decisions of the runs of each trace and of the Orchestrator's runs on its threads, in its project."""
    owner: dict[int, int] = {}
    for trace_id, spans in read.spans.items():
        owner.update((span.run_id, trace_id) for span in spans)
    for trace_id, threads in read.threads.items():
        owner.update((t.orchestrator_run_id, trace_id) for t in threads if t.orchestrator_run_id is not None)
    if not owner:
        return
    d, u = tables.decisions, tables.users
    query = (
        select(d.c.id, d.c.run_id, d.c.project_id, d.c.category, d.c.question, d.c.state, d.c.answer_option)
        .add_columns(d.c.answer_text, d.c.awaits_run_id, d.c.asked_at, u.c.login.label("asked_login"))
        .select_from(d.outerjoin(u, u.c.id == d.c.asked_of))
        .where(d.c.run_id.in_(sorted(owner)))
        .order_by(d.c.id)
    )
    for row in await conn.execute(query):
        trace_id = owner[row.run_id]
        if row.project_id == read.traces[trace_id].project_id:
            read.decisions.setdefault(trace_id, []).append(row)


async def _awaited(conn: AsyncConnection, read: _Read) -> None:
    """The run each open wait of the traces awaits, and the handles of those runs."""
    waits = [d for found in read.decisions.values() for d in found if _is_wait(d) and d.state == "open"]
    read.awaited.update((d.id, d.awaits_run_id) for d in waits if d.awaits_run_id is not None)
    for threads in read.threads.values():
        read.awaited.update(
            (t.wait_id, t.orchestrator_run_id) for t in threads if t.wait_id is not None and t.orchestrator_run_id
        )
    r, p = tables.runs, tables.agent_profiles
    left = [d.id for d in waits if d.id not in read.awaited]
    if left:
        query = select(r.c.spawned_wait_id, func.max(r.c.id)).where(r.c.spawned_wait_id.in_(left))
        read.awaited.update({key: value for key, value in await conn.execute(query.group_by(r.c.spawned_wait_id))})
    unknown = sorted({run for run in read.awaited.values() if run not in read.handles})
    if unknown:
        query = select(r.c.id, p.c.handle).join(p, p.c.id == r.c.profile_id).where(r.c.id.in_(unknown))
        read.handles.update({key: value for key, value in await conn.execute(query)})


def _is_wait(decision) -> bool:
    return decision.category == runs.WAIT_DECISION_CATEGORY


async def _read(conn: AsyncConnection, trace_ids: list[int]) -> _Read:
    rows = (await conn.execute(_trace_query(trace_ids))).all()
    read = _Read(traces={row.id: row for row in rows})
    found = sorted(read.traces)
    if found:
        await _spans(conn, read, found)
        await _threads(conn, read, found)
        await _decisions(conn, read)
        await _awaited(conn, read)
    return read


def _asks_person(decision) -> bool:
    return decision.state == "open" and not _is_wait(decision) and decision.asked_login is not None


def status_of(read: _Read, trace_id: int) -> str:
    """The status of trace ``trace_id`` (see the module's docstring)."""
    trace, spans = read.traces[trace_id], read.spans.get(trace_id, [])
    if any(_asks_person(d) for d in read.decisions.get(trace_id, [])):
        return "needs_person"
    coordinator = trace.entry_profile_id
    own = [span for span in spans if coordinator is not None and span.profile_id == coordinator]
    if (own and own[-1].state in FAILED_STATES) or any(
        t.rule == "failed" and t.outcome is None and t.closed_at is None for t in read.threads.get(trace_id, [])
    ):
        return "failed"
    if any(span.state in runs.ACTIVE_STATES for span in spans):
        return "running"
    return "done"


def _waiting(read: _Read, trace_id: int) -> WorkWaiting | None:
    decisions = read.decisions.get(trace_id, [])
    asked = next((d for d in reversed(decisions) if _asks_person(d)), None)
    if asked is not None:
        return WorkWaiting(kind="decision", decision_id=asked.id, login=asked.asked_login, question=asked.question)
    driven = next((s for s in reversed(read.spans.get(trace_id, [])) if s.state in PERSON_STATES), None)
    if driven is not None:
        return WorkWaiting(kind="person", run_id=driven.run_id, agent=driven.agent)
    for wait in reversed(decisions):
        run = read.awaited.get(wait.id) if _is_wait(wait) and wait.state == "open" else None
        if run is not None:
            return WorkWaiting(kind="run", run_id=run, agent=read.handles.get(run))
    return None


def _brief(read: _Read, trace_id: int) -> WorkBrief:
    trace = read.traces[trace_id]
    row = trace._mapping
    working = [span for span in read.spans.get(trace_id, []) if span.state in WORKING]
    return WorkBrief(
        trace_id=trace_id,
        system=WorkSystem(
            id=row["system_id"], slug=row["system_slug"], name=row["system_name"], project=row["project_name"]
        ),
        title=row["title"] or first_line(row["first_body"]) or f"trace #{trace_id}",
        status=status_of(read, trace_id),  # type: ignore[arg-type]  # one of STATUSES
        agent=working[-1].agent if working else None,
        waiting=_waiting(read, trace_id),
        coordinator=row["coordinator"],
    )


async def briefs(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, WorkBrief]:
    """{trace: its ``WorkBrief``} of the traces of ``trace_ids`` that exist."""
    if not trace_ids:
        return {}
    read = await _read(conn, sorted(set(trace_ids)))
    return {trace_id: _brief(read, trace_id) for trace_id in read.traces}


# The card


async def _asks(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, list[WorkAsk]]:
    s, m, r, p, b = (
        tables.system_spans,
        tables.chat_messages,
        tables.runs,
        tables.agent_profiles,
        tables.agent_ask_briefs,
    )
    child = s.c.attributes[CHILD].as_integer()
    query = (
        select(s.c.trace_id, s.c.tool, s.c.agent, s.c.to_node, s.c.status, child.label("child"), r.c.state)
        .add_columns(p.c.handle, b.c.brief["goal"].as_string().label("goal"), m.c.chat_id)
        .add_columns(func.coalesce(m.c.thread_root_id, m.c.id).label("root_id"))
        .select_from(
            s.outerjoin(r, r.c.id == child)
            .outerjoin(p, p.c.id == r.c.profile_id)
            .outerjoin(b, b.c.run_id == r.c.id)
            .outerjoin(m, m.c.id == s.c.message_id)
        )
        .where(s.c.trace_id.in_(trace_ids), s.c.name == system_context.TOOL, s.c.tool.in_(sorted(ASK_TOOLS)))
        .order_by(s.c.trace_id, s.c.id)
    )
    rows = (await conn.execute(query)).all()
    spent = await system_traces.run_numbers(conn, sorted({row.child for row in rows if row.state is not None}))
    found: dict[int, list[WorkAsk]] = {}
    for row in rows:
        made = row.state is not None
        ask = WorkAsk(
            kind=ASK_TOOLS[row.tool],  # type: ignore[arg-type]  # one of AskKind
            from_agent=row.agent,
            agent=row.handle or (row.to_node if made else None),
            brief=row.goal,
            status=row.state if made else row.status,
            run_id=row.child if made else None,
            usd=float((spent.get(row.child) or {}).get("usd") or 0) if made else 0.0,
            thread=WorkThread(chat_id=row.chat_id, root_id=row.root_id) if row.chat_id is not None else None,
        )
        found.setdefault(row.trace_id, []).append(ask)
    return found


def _answer(decision) -> str | None:
    parts = [part for part in (decision.answer_option, decision.answer_text) if part]
    return ": ".join(parts) or None


def _decision_list(read: _Read, trace_id: int) -> list[WorkDecision]:
    found = [
        WorkDecision(
            kind="orchestrator",
            id=t.id,
            rule=t.rule,
            outcome=t.outcome,
            question=t.subject,
            state="open" if t.closed_at is None else "closed",
            answer=None,
            asked_of=None,
            run_id=t.orchestrator_run_id,
            at=t.created_at,
        )
        for t in read.threads.get(trace_id, [])
    ]
    found += [
        WorkDecision(
            kind="person",
            id=d.id,
            rule=None,
            outcome=None,
            question=d.question,
            state=d.state,
            answer=_answer(d),
            asked_of=d.asked_login,
            run_id=d.run_id,
            at=d.asked_at,
        )
        for d in read.decisions.get(trace_id, [])
        if not _is_wait(d) and d.asked_login is not None
    ]
    return sorted(found, key=lambda decision: decision.at)


async def _limits(conn: AsyncConnection, read: _Read) -> dict[tuple[int, int], tuple[float, int]]:
    found: dict[tuple[int, int], tuple[float, int]] = {}
    for trace in read.traces.values():
        system_id, revision = trace.system_id, trace.system_revision
        if system_id is not None and (system_id, revision) not in found:
            spec = await system_store.revision_spec(conn, system_id, revision) or {}
            found[(system_id, revision)] = system_context.limits_of(spec)
    return found


def _card(read: _Read, trace_id: int, asks: list[WorkAsk], used: tuple, limits: tuple[float, int]) -> WorkCard:
    brief = _brief(read, trace_id)
    row = read.traces[trace_id]._mapping
    active = any(span.state in runs.ACTIVE_STATES for span in read.spans.get(trace_id, []))
    ended = None if active or brief.status == "needs_person" else row["last_at"]
    seconds = ((ended or datetime.now(UTC)) - row["started_at"]).total_seconds()
    count, usd, hop = used
    return WorkCard(
        **brief.model_dump(),
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        work_chat_id=row["work_chat_id"],
        goal=row["goal"],
        done_when=row["done_when"],
        asks=asks,
        decisions=_decision_list(read, trace_id),
        runs=count,
        usd=round(usd, 6),
        hop=hop,
        max_hops=chat_runs.MAX_HOPS,
        trace_runs=limits[1],
        trace_usd=limits[0],
        started_at=row["started_at"],
        ended_at=ended,
        seconds=max(0.0, round(seconds, 3)),
    )


async def cards(conn: AsyncConnection, trace_ids: list[int]) -> dict[int, WorkCard]:
    """{trace: its ``WorkCard``} of the traces of ``trace_ids`` that exist."""
    if not trace_ids:
        return {}
    read = await _read(conn, sorted(set(trace_ids)))
    found = sorted(read.traces)
    if not found:
        return {}
    asks = await _asks(conn, found)
    used = await system_trace_stats.tree_usage(conn, found)
    limits = await _limits(conn, read)
    default = system_context.limits_of({})
    out = {}
    for trace_id in found:
        trace = read.traces[trace_id]
        key = (trace.system_id, trace.system_revision)
        out[trace_id] = _card(
            read, trace_id, asks.get(trace_id, []), used.get(trace_id, (0, 0.0, None)), limits.get(key, default)
        )
    return out


def card_state(card: WorkCard) -> str:
    """A short key of what ``card`` shows that a move changes: when it is the one stored, the streams are not told."""
    shown = card.model_dump(mode="json", include={"title", "goal", "done_when", "status", "agent", "waiting"})
    shown["asks"] = [(ask.status, ask.run_id) for ask in card.asks]
    shown["decisions"] = [(d.kind, d.id, d.state, d.outcome) for d in card.decisions]
    return hashlib.sha256(json.dumps(shown, sort_keys=True).encode()).hexdigest()[:32]

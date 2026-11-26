"""Traces of agent systems in the database (``evo_agents.hub.tables_traces``): every SQL statement of
``service.traces``, on system_traces, system_spans and agent_ask_briefs and what they read of runs, chat messages,
agent systems and plans, each taking the caller's connection."""

from __future__ import annotations

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, system_context, tables

# Systems and runs


async def touch(conn: AsyncConnection, trace_id: int) -> int | None:
    """Set the last_at of trace ``trace_id`` to now; the id of its system (None once deleted)."""
    t = tables.system_traces
    query = update(t).values(last_at=func.now()).where(t.c.id == trace_id).returning(t.c.system_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def system_by_entry(conn: AsyncConnection, profile_id: int):
    """The system (id, slug, project_id, revision, spec) whose entry agent is profile ``profile_id``; None for none."""
    s = tables.agent_systems
    query = select(s.c.id, s.c.slug, s.c.project_id, s.c.revision, s.c.spec).where(s.c.entry_profile_id == profile_id)
    return (await conn.execute(query)).first()


async def trace_system(conn: AsyncConnection, trace_id: int):
    """The system of trace ``trace_id`` as it holds it: system_id, system_slug, project_id, system_revision."""
    t = tables.system_traces
    query = select(t.c.system_id, t.c.system_slug, t.c.project_id, t.c.system_revision).where(t.c.id == trace_id)
    return (await conn.execute(query)).one()


async def chat_traces(conn: AsyncConnection, chat_id: int, limit: int) -> list[int]:
    """The traces of chat ``chat_id`` whose system is still there, the newest first, at most ``limit``."""
    t = tables.system_traces
    query = select(t.c.id).where(t.c.chat_id == chat_id, t.c.system_id.is_not(None)).order_by(t.c.id.desc())
    return list((await conn.execute(query.limit(limit))).scalars())


async def project_systems(conn: AsyncConnection, project_id: int) -> list:
    """The systems of the project (id, slug, revision, spec), the first made first."""
    s = tables.agent_systems
    query = select(s.c.id, s.c.slug, s.c.revision, s.c.spec).where(s.c.project_id == project_id).order_by(s.c.id)
    return list(await conn.execute(query))


async def invoke_span(conn: AsyncConnection, run_id: int):
    """The invoke_agent span of run ``run_id`` (id, trace_id, parent_id, node, from_node, off_design, reason,
    message_id); None for a run in no trace."""
    s = tables.system_spans
    query = select(
        s.c.id, s.c.trace_id, s.c.parent_id, s.c.node, s.c.from_node, s.c.off_design, s.c.reason, s.c.message_id
    ).where(s.c.run_id == run_id, s.c.name == system_context.INVOKE)
    return (await conn.execute(query)).one_or_none()


async def run_row(conn: AsyncConnection, run_id: int):
    """What a trace reads of run ``run_id``: its profile, project, runtime, chat, message, the run it resumes and its
    agent's handle; None for a run of no profile."""
    r, ap = tables.runs, tables.agent_profiles
    query = (
        select(
            r.c.profile_id,
            r.c.project_id,
            r.c.runtime,
            r.c.chat_id,
            r.c.chat_message_id,
            r.c.resume_of_run_id,
            ap.c.handle,
        )
        .join_from(r, ap, ap.c.id == r.c.profile_id)
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def message_run(conn: AsyncConnection, message_id: int):
    """The run that wrote chat message ``message_id`` and its chat (run_id, chat_id); None for no such message."""
    m = tables.chat_messages
    return (await conn.execute(select(m.c.run_id, m.c.chat_id).where(m.c.id == message_id))).one_or_none()


async def message_author(conn: AsyncConnection, message_id: int):
    """Who wrote chat message ``message_id`` (author_user_id, run_id) and the message it copies (copied_from_id)."""
    m = tables.chat_messages
    query = select(m.c.author_user_id, m.c.run_id, m.c.copied_from_id).where(m.c.id == message_id)
    return (await conn.execute(query)).one_or_none()


# Writing traces and spans


async def open_trace(conn: AsyncConnection, values: dict) -> int:
    t = tables.system_traces
    return (await conn.execute(insert(t).values(**values).returning(t.c.id))).scalar_one()


async def add_span(conn: AsyncConnection, values: dict, *, ended: bool = False) -> int:
    """Write a span of ``values``, ended now when ``ended``; its id."""
    s = tables.system_spans
    if ended:
        values = {**values, "ended_at": func.now()}
    return (await conn.execute(insert(s).values(**values).returning(s.c.id))).scalar_one()


async def span_project(conn: AsyncConnection, span_id: int | None) -> int | None:
    s = tables.system_spans
    return (await conn.execute(select(s.c.project_id).where(s.c.id == span_id))).scalar_one_or_none()


async def mark_reason(conn: AsyncConnection, run_id: int, reason: str) -> None:
    """Record ``reason`` on the off-design edge into run ``run_id``'s span."""
    s = tables.system_spans
    await conn.execute(
        update(s)
        .values(reason=reason)
        .where(s.c.run_id == run_id, s.c.name == system_context.INVOKE, s.c.off_design.is_(True))
    )


async def end_span(conn: AsyncConnection, run_id: int, status: str) -> int | None:
    """End the running invoke_agent span of run ``run_id`` with ``status``; the id of its trace, None when it had none
    running."""
    s = tables.system_spans
    query = (
        update(s)
        .values(status=status, ended_at=func.now())
        .where(s.c.run_id == run_id, s.c.name == system_context.INVOKE, s.c.status == "running")
        .returning(s.c.trace_id)
    )
    return (await conn.execute(query)).scalar_one_or_none()


# The budget


async def usage(conn: AsyncConnection, trace_id: int) -> tuple[int, float]:
    """(runs, USD) of trace ``trace_id``: its runs and every run their tree spawned (``spawned_by_run_id``), and what
    they cost (run_spend, the reported cost else the estimate)."""
    s, r, rs = tables.system_spans, tables.runs, tables.run_spend
    seeds = select(s.c.run_id.label("id")).where(
        s.c.trace_id == trace_id, s.c.name == system_context.INVOKE, s.c.run_id.is_not(None)
    )
    tree = seeds.cte("trace_tree", recursive=True)
    tree = tree.union(select(r.c.id).where(r.c.spawned_by_run_id == tree.c.id))
    count = (await conn.execute(select(func.count()).select_from(tree))).scalar_one()
    cost = func.coalesce(func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)), 0)
    usd = (await conn.execute(select(cost).where(rs.c.run_id.in_(select(tree.c.id))))).scalar_one()
    return int(count), float(usd or 0)


# The plan of a trace


async def plan_traces(conn: AsyncConnection, project_id: int, plan_id: str | None) -> list:
    """The trace of each plan of the project with a run active (plan_id, id, system_slug, run_id, state), with its
    latest active run; ``plan_id``'s alone when given."""
    t, s, r = tables.system_traces, tables.system_spans, tables.runs
    query = (
        select(t.c.plan_id, t.c.id, t.c.system_slug, r.c.id.label("run_id"), r.c.state)
        .join_from(t, s, and_(s.c.trace_id == t.c.id, s.c.name == system_context.INVOKE))
        .join(r, r.c.id == s.c.run_id)
        .where(t.c.project_id == project_id, t.c.plan_id.is_not(None), r.c.state.in_(runs.ACTIVE_STATES))
        .ext(distinct_on(t.c.plan_id))
        .order_by(t.c.plan_id, r.c.id.desc())
    )
    if plan_id is not None:
        query = query.where(t.c.plan_id == plan_id)
    return list(await conn.execute(query))


async def take_plan(conn: AsyncConnection, trace_id: int, plan_id: str) -> int | None:
    """Give trace ``trace_id`` plan ``plan_id`` of its project at the plan's revision now, when it has no plan yet and
    the project holds the plan; the trace's id when it took it."""
    t, p = tables.system_traces, tables.plans
    revision = select(p.c.revision).where(p.c.project_id == t.c.project_id, p.c.plan_id == plan_id).scalar_subquery()
    query = (
        update(t)
        .values(plan_id=plan_id, plan_revision=revision)
        .where(t.c.id == trace_id, t.c.plan_id.is_(None), revision.is_not(None))
        .returning(t.c.id)
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def plan_work(conn: AsyncConnection, run_id: int):
    """What the plan work of chat run ``run_id`` reads: the project and plan of its trace, its own project, and
    whether it is a run of the coordinator's session of the trace (``coordinates``); None for no such run."""
    t, s, r, sy = tables.system_traces, tables.system_spans, tables.runs, tables.agent_systems
    mine, first = tables.chat_messages.alias("mine"), tables.chat_messages.alias("first")
    query = (
        select(
            t.c.project_id,
            t.c.plan_id,
            r.c.project_id.label("own_project_id"),
            (
                (sy.c.entry_profile_id == r.c.profile_id)
                & (mine.c.chat_id == first.c.chat_id)
                & (func.coalesce(mine.c.thread_root_id, mine.c.id) == func.coalesce(first.c.thread_root_id, first.c.id))
            ).label("coordinates"),
        )
        .select_from(
            r.outerjoin(s, and_(s.c.run_id == r.c.id, s.c.name == system_context.INVOKE))
            .outerjoin(t, t.c.id == s.c.trace_id)
            .outerjoin(sy, sy.c.id == t.c.system_id)
            .outerjoin(mine, mine.c.id == r.c.chat_message_id)
            .outerjoin(first, first.c.id == t.c.message_id)
        )
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query.limit(1))).one_or_none()


# Briefs


async def save_brief(conn: AsyncConnection, run_id: int, asked_by: int, brief: dict) -> None:
    b = tables.agent_ask_briefs
    statement = pg_insert(b).values(run_id=run_id, asked_by_run_id=asked_by, brief=brief)
    await conn.execute(
        statement.on_conflict_do_update(index_elements=[b.c.run_id], set_={"brief": brief, "asked_by_run_id": asked_by})
    )


async def brief(conn: AsyncConnection, run_id: int):
    """The brief run ``run_id`` was given and the run that asked (brief, asked_by_run_id); None for none."""
    b = tables.agent_ask_briefs
    return (await conn.execute(select(b.c.brief, b.c.asked_by_run_id).where(b.c.run_id == run_id))).one_or_none()


async def handle_of_run(conn: AsyncConnection, run_id: int) -> str | None:
    r, ap = tables.runs, tables.agent_profiles
    query = select(ap.c.handle).join_from(r, ap, ap.c.id == r.c.profile_id)
    return (await conn.execute(query.where(r.c.id == run_id))).scalar_one_or_none()


async def resume_of(conn: AsyncConnection, run_id: int) -> int | None:
    """The run that run ``run_id`` goes on from; None for none."""
    r = tables.runs
    return (await conn.execute(select(r.c.resume_of_run_id).where(r.c.id == run_id))).scalar_one_or_none()


# Reading


async def run_numbers(conn: AsyncConnection, run_ids: list[int]) -> dict[int, dict]:
    """{run: {input_tokens, output_tokens, usd, state, model}} of chat runs ``run_ids``, from run_spend and runs."""
    if not run_ids:
        return {}
    r, rs = tables.runs, tables.run_spend
    spend = (
        select(
            rs.c.run_id,
            func.sum(rs.c.input_tokens).label("input_tokens"),
            func.sum(rs.c.output_tokens).label("output_tokens"),
            func.sum(func.coalesce(rs.c.reported_usd, rs.c.estimated_usd)).label("usd"),
        )
        .where(rs.c.run_id.in_(run_ids))
        .group_by(rs.c.run_id)
    )
    found = {row.run_id: dict(row._mapping) for row in await conn.execute(spend)}
    states = select(r.c.id, r.c.state, r.c.model).where(r.c.id.in_(run_ids))
    out = {}
    for row in await conn.execute(states):
        numbers = found.get(row.id, {})
        out[row.id] = {
            "state": row.state,
            "model": row.model,
            "input_tokens": int(numbers.get("input_tokens") or 0),
            "output_tokens": int(numbers.get("output_tokens") or 0),
            "usd": float(numbers.get("usd") or 0),
        }
    return out

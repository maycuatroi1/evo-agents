"""When the review run of a night shift is due under the charter's ``review.trigger`` (``curator.review_wait``), for
``collect.ensure_figures``: the previous review, and the new evidence since it, without any model. The evidence is
the runs of the project that failed of a cause of the agent's (``curator.is_agents``), the new traces of its agent
systems with an optimizes edge (``system_curation.new_traces``) and the turns of a person that correct the agent, as
the figures count them (``review.is_correction``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, review, tables
from evo_agents.hub.server import facet_figures, system_curation
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.runs.service.views import visible_plans


async def _last_review(conn: AsyncConnection, project_id: int) -> datetime | None:
    """When the project's previous review of a night was queued: the moment review.trigger counted it from
    (figures.trigger.at), else when its figures were counted; a review run that was cancelled is none. None: no
    review yet."""
    cf, r = tables.curator_figures, tables.runs
    query = (
        select(cf.c.until, cf.c.figures["trigger"]["at"].astext.label("at"))
        .join_from(cf, r, r.c.id == cf.c.run_id)
        .where(cf.c.project_id == project_id, r.c.state != "cancelled")
        .order_by(cf.c.night.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    return datetime.fromisoformat(row.at) if row.at else row.until


async def _failed_of_the_agent(conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime) -> int:
    """The runs of the project that failed or were lost in [since, until) of a cause of the agent's
    (``curator.is_agents``): never a local run, nor a review or history run of the Curator, nor one of a plan the
    owner may not read."""
    r = tables.runs
    plans = await visible_plans(conn, access, None)
    query = select(r.c.failure_cause, r.c.error, r.c.state).where(
        r.c.project_id == access.project_id,
        r.c.kind.not_in(("local", "review", "history")),
        r.c.state.in_(("failed", "lost")),
        r.c.plan_id.is_(None) | r.c.plan_id.in_(plans),
        r.c.finished_at >= since,
        r.c.finished_at < until,
    )
    rows = (await conn.execute(query)).all()
    return sum(1 for row in rows if curator.is_agents(review.run_cause(row.failure_cause, row.error, row.state)))


async def _corrections(conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime, owner_id) -> int:
    """The turns of a person in the sessions of [since, until) that correct the agent (``review.is_correction``), as
    the figures count them."""
    held = await facet_figures.sessions_of(conn, access, since, until, owner_id)
    return sum(
        1
        for session in held
        for turn in session.lists.get("user_turns") or ()
        if isinstance(turn, str) and review.is_correction(turn)
    )


async def review_due(conn: AsyncConnection, due, access: ProjectAccess, until: datetime) -> dict | None:
    """Under the charter's review.trigger, what makes the review of schedule ``due`` due now (``curator.review_wait``),
    kept as the figures' ``trigger``: ``at`` (now), ``since`` (the previous review, null for the first), ``why``
    (first, max_gap or min_new) and the new evidence counted (failed_runs, traces, corrections); None when it is not
    due yet. The counts stop as soon as they reach min_new."""
    trigger = curator.review_trigger_of(due.body)
    last = await _last_review(conn, due.project_id)
    wait = curator.review_wait(trigger, last, due.now)
    if wait == "gap":
        return None
    found: dict = {"at": due.now.isoformat(), "since": None if last is None else last.isoformat(), "why": wait}
    if last is None:
        return found
    since = min(last, until)
    need = int(trigger["min_new"])
    counts = {"failed_runs": await _failed_of_the_agent(conn, access, since, until)}
    counts["traces"] = await system_curation.new_traces(conn, due.project_id, since)
    if wait == "max_gap" or sum(counts.values()) < need:
        counts["corrections"] = await _corrections(conn, access, since, until, due.owner_id)
    if wait == "count" and sum(counts.values()) < need:
        return None
    return {**found, **counts, "why": "min_new" if wait == "count" else wait}

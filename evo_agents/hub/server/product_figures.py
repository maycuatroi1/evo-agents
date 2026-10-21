"""The night's ``product``: what the lens ``product`` of the Curator's review reads (``review.PRODUCT``; plan loop-engine,
criterion a9), which ``evo_agents.hub.server.collect`` adds to the figures of a span, counted without any model from
what the reader (the owner of the night shift) may read through the project's hub sink:

- ``goals``: the project's active goals by rank, each with its code, its metric as ``goals.metric_text`` reads it and
  the signal key it names, its deadline and the canvas it drives;
- ``signals``: each key of the project's registry of signals that is not archived, in name order, with its kind and
  unit, how many of its values of the span the reader may read, when the newest of them was measured, and for a key of
  kind number the smallest, the largest and the newest value;
- ``canvases``: when the project enables the plugin canvas, each canvas not archived the reader sees, with its claims
  not archived that the reader sees by status, those at testing whose test card's deadline passed (``overdue``), its
  open proposals and its newest baseline; None when the project does not enable it, so the lens leaves claims out.

The hub reads the plugin's tables (``tables.canvas``), never the plugin. The Loop's own numbers join these once it keeps
work items.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, signals, tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import goals as project_goals
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.signals import readable

CANVAS = "canvas"  # the plugin whose tables hold the canvases
TOP = 30  # goals, signal keys and canvases a figure lists at most
STATEMENT_CHARS = 300  # of a goal's statement in the figure; the prompt lists it whole


def _hub_sink(access: ProjectAccess) -> str | None:
    sink = access.rules.hub_sink
    return sink.sink if sink is not None else None


async def _goal_figures(conn: AsyncConnection, access: ProjectAccess) -> list[dict]:
    shown = []
    for goal in (await project_goals.active_goals(conn, access.project_id, access.name))[:TOP]:
        metric = goal.metric.model_dump() if goal.metric is not None else None
        statement = " ".join(goal.statement.split())
        shown.append(
            {
                "goal": goal.code,
                "statement": statement[:STATEMENT_CHARS] + ("..." if len(statement) > STATEMENT_CHARS else ""),
                "metric": goals.metric_text(metric) if metric else None,
                "key": metric["key"] if metric else None,
                "deadline": goal.deadline.isoformat() if goal.deadline is not None else None,
                "canvas": goal.canvas,
                "rank": goal.rank,
            }
        )
    return shown


async def _signal_figures(conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime) -> list[dict]:
    k, s = tables.signal_keys, tables.signals
    keys = (
        await conn.execute(
            select(k.c.id, k.c.name, k.c.kind, k.c.unit)
            .where(k.c.project_id == access.project_id, k.c.archived_at.is_(None))
            .order_by(k.c.name)
            .limit(TOP)
        )
    ).all()
    pairs = readable(access, None)
    if not keys:
        return []
    counted = {}
    if pairs:
        query = (
            select(s.c.key_id, func.count(), func.min(s.c.value), func.max(s.c.value))
            .where(
                s.c.project_id == access.project_id,
                s.c.key_id.in_([key.id for key in keys]),
                s.c.measured_at >= since,
                s.c.measured_at < until,
                tuple_(s.c.label["level"].astext, s.c.label["location"].astext).in_(pairs),
            )
            .group_by(s.c.key_id)
        )
        counted = {row[0]: row[1:] for row in await conn.execute(query)}
    shown = []
    for key in keys:
        values, low, high = counted.get(key.id, (0, None, None))
        item = {"key": key.name, "kind": key.kind, "unit": key.unit, "values": values}
        newest = []
        if values:
            newest = await signals.signal_rows(
                conn, access.project_id, pairs, key=key.name, since=since, until=until, limit=1
            )
        if newest and key.kind == "number":
            item |= {"min": low, "max": high, "newest": newest[0].value}
        if newest:
            item["newest_at"] = newest[0].measured_at.isoformat()
        shown.append(item)
    return shown


def _overdue(card, today: date) -> bool:
    deadline = card.get("deadline") if isinstance(card, dict) else None
    try:
        return deadline is not None and date.fromisoformat(str(deadline)) < today
    except ValueError:
        return False


async def _canvas_figure(conn: AsyncConnection, access: ProjectAccess, canvas, sink: str, today: date) -> dict:
    t = tables.canvas
    claims = (
        await conn.execute(
            select(t.claims.c.status, t.claims.c.test_card, t.claims.c.label).where(
                t.claims.c.canvas_id == canvas.id, t.claims.c.archived_at.is_(None)
            )
        )
    ).all()
    seen = [claim for claim in claims if access.visible(claim.label, sink)]
    by_status = Counter(claim.status for claim in seen)
    overdue = sum(1 for claim in seen if claim.status == "testing" and _overdue(claim.test_card, today))
    p, b = t.proposals, t.baselines
    opened = await conn.execute(select(p.c.label).where(p.c.canvas_id == canvas.id, p.c.status == "open"))
    newest = (
        await conn.execute(
            select(b.c.number, b.c.kind, b.c.week, b.c.status, b.c.created_at, b.c.approved_at)
            .where(b.c.canvas_id == canvas.id)
            .order_by(b.c.number.desc())
            .limit(1)
        )
    ).first()
    baseline = None
    if newest is not None:
        baseline = {
            "number": newest.number,
            "kind": newest.kind,
            "week": newest.week,
            "status": newest.status,
            "created_at": newest.created_at.isoformat(),
            "approved_at": newest.approved_at.isoformat() if newest.approved_at is not None else None,
        }
    return {
        "canvas": canvas.slug,
        "claims": dict(sorted(by_status.items())),
        "overdue": overdue,
        "open_proposals": sum(1 for row in opened if access.visible(row.label, sink)),
        "baseline": baseline,
    }


async def _canvas_figures(conn: AsyncConnection, access: ProjectAccess, until: datetime) -> list[dict] | None:
    if await enabled_in(conn, CANVAS, access.project_id) is None:
        return None
    sink = _hub_sink(access)
    if sink is None:
        return []
    c = tables.canvas.canvases
    rows = (
        await conn.execute(
            select(c.c.id, c.c.slug, c.c.label)
            .where(c.c.project_id == access.project_id, c.c.archived_at.is_(None))
            .order_by(c.c.slug)
        )
    ).all()
    shown = [row for row in rows if access.visible(row.label, sink)][:TOP]
    return [await _canvas_figure(conn, access, row, sink, until.date()) for row in shown]


async def figures(conn: AsyncConnection, access: ProjectAccess, since: datetime, until: datetime) -> dict:
    """The night's ``product`` of the project of ``access`` over [since, until) (see the module)."""
    return {
        "goals": await _goal_figures(conn, access),
        "signals": await _signal_figures(conn, access, since, until),
        "canvases": await _canvas_figures(conn, access, until),
    }

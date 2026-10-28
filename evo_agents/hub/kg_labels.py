"""The labels of the knowledge graph's searches on the hub: each kg_search a session or a run made, as its facet read
it (``evo_agents.hub.facets.QueryLabel``), in kg_query_labels (schema 0177).

``replace`` writes the labels of one facet in the transaction that writes the facet (``evo_agents.hub.facet_job``),
in place of those the facet made before, so a facet made again leaves one row per search. Each row carries the
sensitivity label of its session (the label its transcripts came with), or of its run (the label of the run's plan,
else the project's default label); the readers (``evo_agents.hub.server.kg_labels``) show a row only through a sink
whose clearance reads that label. Not the facet's body: a search's query and the files it named stay under the label.

``push_grep_after`` pushes the night's signal SIGNAL_KEY, the share of the chains of lookups a grep followed right
away (``kg_coverage.retrieval``), when the night's figures are counted (``evo_agents.hub.server.collect``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import config, facets, signals, tables

log = logging.getLogger(__name__)

KEY = "kg_labels"  # the facet's field that holds them, which facet_job does not keep in the body


def split(body: Mapping) -> tuple[dict, list[dict]]:
    """(the facet's body without its labels, the labels)."""
    found = body.get(KEY) if isinstance(body, Mapping) else None
    kept = {key: value for key, value in (body or {}).items() if key != KEY}
    return kept, [item for item in found or [] if isinstance(item, Mapping)][: facets.MAX_LABELS]


def _owner(t, kind: str, project_id: int, session_id: str | None, run_id: int | None):
    if kind == "run":
        return and_(t.c.kind == "run", t.c.run_id == run_id)
    return and_(t.c.project_id == project_id, t.c.kind == "session", t.c.session_id == session_id)


async def run_label(conn: AsyncConnection, run_id: int) -> dict | None:
    """The label of a run's searches: its plan's label, else its project's default label."""
    r, p, pr = tables.runs, tables.plans, tables.projects
    query = (
        select(p.c.label, pr.c.default_label)
        .select_from(
            r.join(pr, pr.c.id == r.c.project_id).outerjoin(
                p, and_(p.c.project_id == r.c.project_id, p.c.plan_id == r.c.plan_id)
            )
        )
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else row.label or row.default_label


async def project_label(conn: AsyncConnection, project_id: int) -> dict | None:
    pr = tables.projects
    return (await conn.execute(select(pr.c.default_label).where(pr.c.id == project_id))).scalar_one_or_none()


def _when(value) -> datetime | None:
    return facets.timestamp(value)


def _rows(labels: Iterable[Mapping]) -> list[dict]:
    rows = []
    for item in labels:
        rows.append(
            {
                "seq": facets.count(item.get("seq")) or 0,
                "at": None if item.get("at") is None else facets.cut(item.get("at"), 200),
                "searched_at": _when(item.get("searched_at")),
                "query": facets.cut(item.get("query"), facets.QUERY_CHARS),
                "results": list(item.get("results") or []),
                "clicks": list(item.get("clicks") or []),
                "grep_after": bool(item.get("grep_after")),
                "after_grep": list(item.get("after_grep") or []),
            }
        )
    return rows


async def replace(
    conn: AsyncConnection,
    *,
    kind: str,
    project_id: int,
    session_id: str | None,
    run_id: int | None,
    labels: list[Mapping],
    label: Mapping | None,
) -> int:
    """Put ``labels`` in place of the searches the session's or the run's facet had before; how many rows."""
    t = tables.kg_query_labels
    await conn.execute(delete(t).where(_owner(t, kind, project_id, session_id, run_id)))
    rows = _rows(labels)
    if not rows:
        return 0
    if label is None:
        label = await (run_label(conn, run_id) if kind == "run" else project_label(conn, project_id))
    owner = {"project_id": project_id, "kind": kind, "session_id": session_id if kind == "session" else None}
    owner |= {"run_id": run_id if kind == "run" else None, "label": dict(label or {})}
    await conn.execute(insert(t), [owner | row for row in rows])
    return len(rows)


# The night's signal

SIGNAL_KEY = "kg.query.grep_after_rate"  # of the night's chains of kg lookups, the percent a grep followed right away
SIGNAL_SOURCE = "hub.collect"
SIGNAL_WORDS = (
    "Of the chains of knowledge graph lookups agents made in the night's span, the percent a grep, rg or find "
    "followed right away (kg_health.retrieval.grep_after_rate), pushed by the hub each night."
)


@dataclass(frozen=True)
class _Item:
    """One item of a batch, as ``signals.push`` reads it."""

    key: str
    value: float
    external_id: str
    text: None = None
    source: str = SIGNAL_SOURCE
    measured_at: datetime | None = None
    tenant: None = None
    synthetic: bool = False
    level: None = None


def grep_after_percent(figures: Mapping | None) -> float | None:
    """The night's grep_after_rate in percent, from its figures' ``kg_health.retrieval``; None when no chain of
    lookups came in the span, or the facets are older than the labels."""
    health = figures.get("kg_health") if isinstance(figures, Mapping) else None
    found = health.get("retrieval") if isinstance(health, Mapping) else None
    if not isinstance(found, Mapping) or not facets.count(found.get("chains")):
        return None
    rate = found.get("grep_after_rate")
    return round(float(rate) * 100, 2) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None


async def push_grep_after(conn: AsyncConnection, project_id: int, owner_id: int | None, night: date, figures) -> bool:
    """Push the night's SIGNAL_KEY for the project as the night shift's owner (a member), the key added to the
    registry the first time; idempotent per night (its external_id). In a savepoint of ``conn``: a signal refused
    or past the day's cap leaves the figures as they are. Whether a signal was stored."""
    value = grep_after_percent(figures)
    if value is None or owner_id is None:
        return False
    sk = tables.signal_keys
    key = {"project_id": project_id, "name": SIGNAL_KEY, "kind": "number", "unit": "percent"}
    key |= {"description": SIGNAL_WORDS, "source": SIGNAL_SOURCE, "created_by": owner_id}
    item = _Item(SIGNAL_KEY, value, f"collect:{SIGNAL_KEY}:{night.isoformat()}")
    try:
        async with conn.begin_nested():
            await conn.execute(pg_insert(sk).values(key).on_conflict_do_nothing())
            pusher = signals.Pusher(
                project_id, await signals.project_rules(conn, project_id), "member", user_id=owner_id
            )
            done = await signals.push(conn, pusher, [item], config.DEFAULT_SIGNAL_DAYS)
    except signals.SignalError as exc:
        log.warning("night signal refused", extra={"project_id": project_id, "key": SIGNAL_KEY, "why": exc.message})
        return False
    for refused in done.rejected:
        log.warning(
            "night signal refused", extra={"project_id": project_id, "key": SIGNAL_KEY, "why": refused["reason"]}
        )
    return done.accepted > 0

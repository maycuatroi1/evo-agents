"""What a hub build of a project's graph learns from its searches (``evo_agents.kg.asked``): the project's newest
MAX_LABELS labels of kg_query_labels (``evo_agents.hub.kg_labels``), each with its sensitivity label read on the
project's ladder, and the questions of the holdout split of its latest eval set (kg_eval_sets), whose repeats are left
out. ``read`` reads them before the build; ``put`` puts the rows they give into the rows of a build."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_sources, tables
from evo_agents.kg import asked, graph_rows
from evo_agents.kg.evaluate import HOLDOUT, split_of
from evo_agents.kg.snapshot import StoreRows
from evo_agents.schema.policy import Policy, audience_of

MAX_LABELS = 20_000  # the newest labels a build reads


@dataclass(frozen=True)
class Learned:
    labels: list[asked.Label] = field(default_factory=list)
    holdout: list[str] = field(default_factory=list)
    weight: float | None = None  # the weight of an asked match the KG optimize loop left in effect; None: the default


def graph_label(label, project: str, policy: Policy) -> tuple[int, int, str | None] | None:
    """(level, location, audience as ``label_aud`` stores it) of a hub label on ``policy``'s ladder, as the build
    reads the label of an item (``kg_sources.kg_label``); None for a label that keeps the search out of the graph."""
    found = kg_sources.kg_label(label, project, policy.levels, policy.locations)
    if found is None:
        return None
    level = policy.levels.index(found["level"])
    location = policy.locations.index(found["location"]) if "location" in found else 0
    return level, location, asked.audience_text(audience_of(found.get("audience")))


async def read(conn: AsyncConnection, project_id: int, project: str, knowledge: dict) -> Learned:
    policy = Policy(project, knowledge or {})
    t = tables.kg_query_labels
    rows = await conn.execute(
        select(t.c.query, t.c.results, t.c.clicks, t.c.after_grep, t.c.searched_at, t.c.label)
        .where(t.c.project_id == project_id)
        .order_by(t.c.searched_at.desc().nulls_last(), t.c.id.desc())
        .limit(MAX_LABELS)
    )
    labels = []
    for row in rows:
        found = graph_label(row.label, project, policy)
        if found is None:
            continue
        labels.append(
            asked.Label(
                row.query,
                [r for r in row.results or [] if isinstance(r, dict)],
                [c for c in row.clicks or [] if isinstance(c, dict)],
                [p for p in row.after_grep or [] if isinstance(p, str)],
                row.searched_at,
                *found,
            )
        )
    s = tables.kg_eval_sets
    latest = (
        await conn.execute(
            select(s.c.questions).where(s.c.project_id == project_id).order_by(s.c.version.desc()).limit(1)
        )
    ).scalar_one_or_none()
    holdout = [
        str(q.get("question") or "")
        for q in latest or []
        if isinstance(q, dict) and q.get("id") is not None and split_of(str(q["id"])) == HOLDOUT
    ]
    return Learned(labels, holdout, await asked_weight(conn, project_id))


async def asked_weight(conn: AsyncConnection, project_id: int) -> float | None:
    """The weight of an asked match the project's newest KG optimize run that set one left in effect, unless it was
    reverted or failed (kg_optimize_runs, ``evo_agents.hub.server.kg_optimize``); None: ``search.ASKED_WEIGHT``."""
    t = tables.kg_optimize_runs
    return (
        await conn.execute(
            select(t.c.asked_weight)
            .where(
                t.c.project_id == project_id,
                t.c.asked_weight.is_not(None),
                t.c.state.not_in(("reverted", "failed")),
            )
            .order_by(t.c.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def put(rows: StoreRows, learned: Learned, now: datetime | None = None) -> tuple[StoreRows, str | None]:
    """``rows`` with the asked index ``learned`` gives (``evo_agents.kg.graph_rows.with_asked``), and the digest of the
    index, None when it holds nothing."""
    found = []
    if learned.labels:
        found = asked.rows(learned.labels, graph_rows.asked_nodes(rows), now or datetime.now(UTC), learned.holdout)
    return graph_rows.with_asked(rows, found, learned.weight)

"""What a hub build of a project's graph learns from its searches (``evo_agents.kg.asked``): the project's newest
MAX_LABELS labels of kg_query_labels (``evo_agents.hub.kg_labels``), each with its sensitivity label read on the
project's ladder, and the questions of the holdout split of its latest eval set (kg_eval_sets), whose repeats are left
out. ``read`` reads them before the build; ``write`` puts the rows they give into the built graph.sqlite."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_sources, tables
from evo_agents.kg import asked
from evo_agents.kg.evaluate import HOLDOUT, split_of
from evo_agents.kg.policy import Policy, audience_of

MAX_LABELS = 20_000  # the newest labels a build reads


@dataclass(frozen=True)
class Learned:
    labels: list[asked.Label] = field(default_factory=list)
    holdout: list[str] = field(default_factory=list)


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
    return Learned(labels, holdout)


def write(path: Path, learned: Learned, now: datetime | None = None) -> str | None:
    """Put the asked index ``learned`` gives into the store at ``path`` (out of WAL mode again after): the digest of
    the index, None when it holds nothing. Blocking."""
    from evo_agents.kg.store import Store

    store = Store(path)
    try:
        b = store.latest_ready()
        rows = []
        if b is not None and learned.labels:
            nodes = asked.Nodes.of_store(store, b)
            rows = asked.rows(learned.labels, nodes, now or datetime.now(UTC), learned.holdout)
        digest = store.write_asked(rows)
        store.leave_wal()
    finally:
        store.close()
    return digest

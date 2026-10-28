"""The labels of the knowledge graph's searches read back (kg_query_labels, ``evo_agents.hub.kg_labels``): only those
whose label the caller's grant reads through ``sink`` (``ProjectAccess.visible``), the latest first. The retrieval
evaluation and the Curator read them to learn what agents searched for and which results they opened."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.projects import ProjectAccess

MAX_READ = 500  # labels one read gives


async def visible_labels(
    conn: AsyncConnection,
    access: ProjectAccess,
    sink: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = MAX_READ,
) -> list[dict]:
    """The searches of the project's sessions and runs in [since, until) that ``access`` reads through ``sink``."""
    t = tables.kg_query_labels
    query = select(t).where(t.c.project_id == access.project_id)
    if since is not None:
        query = query.where(t.c.searched_at >= since)
    if until is not None:
        query = query.where(t.c.searched_at < until)
    query = query.order_by(t.c.searched_at.desc().nulls_last(), t.c.id.desc())
    found = []
    for row in await conn.execute(query):
        if not access.visible(row.label, sink):
            continue
        found.append(
            {
                "kind": row.kind,
                "session_id": row.session_id,
                "run_id": row.run_id,
                "seq": row.seq,
                "at": row.at,
                "searched_at": row.searched_at.isoformat() if row.searched_at else None,
                "query": row.query,
                "results": row.results,
                "clicks": row.clicks,
                "grep_after": row.grep_after,
                "after_grep": row.after_grep,
            }
        )
        if len(found) >= max(1, min(limit, MAX_READ)):
            break
    return found

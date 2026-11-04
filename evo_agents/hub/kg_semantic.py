"""Whether the hub's graph of a project is searched by meaning too (``evo_agents.kg.semantic``): the project's row of
kg_semantic, off without one. ``read`` gives the build its settings; ``write`` is how the KG optimize loop turns the
search by meaning on, to ``on`` only when the holdout of its eval set says it helps (plan kg-retrieval-quality, a12):
hit@5 at least five points above the search by words, tokens per answer file not up, p95 under a second.
``index`` puts the vectors the settings ask for into a built graph.sqlite, out of WAL mode again after."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.kg import semantic

DEFAULT_NEW_MODE = "index"  # a project registered now: vectors written, search by words until the holdout says on


async def start(conn: AsyncConnection, project_id: int) -> None:
    """Give a new project its row at mode index; a project that has a row keeps it (the owner's choice stands)."""
    t = tables.kg_semantic
    values = {"mode": DEFAULT_NEW_MODE, "model": semantic.DEFAULT_MODEL}
    await conn.execute(
        pg_insert(t).values(project_id=project_id, **values).on_conflict_do_nothing(index_elements=[t.c.project_id])
    )


async def read(conn: AsyncConnection, project_id: int) -> semantic.Settings:
    t = tables.kg_semantic
    row = (await conn.execute(select(t.c.mode, t.c.model).where(t.c.project_id == project_id))).one_or_none()
    return semantic.Settings() if row is None else semantic.Settings(row.mode, row.model)


async def write(
    conn: AsyncConnection, project_id: int, settings: semantic.Settings, user_id: int | None = None
) -> None:
    """Set the project's mode and model; its next build writes or drops the vectors."""
    t = tables.kg_semantic
    values = {"mode": settings.mode, "model": settings.model, "updated_by": user_id, "updated_at": func.now()}
    statement = pg_insert(t).values(project_id=project_id, **values)
    await conn.execute(statement.on_conflict_do_update(index_elements=[t.c.project_id], set_=values))


EMBED_BUDGET_SECONDS = 300.0  # a build embeds for 5 minutes at most, so it frees its worker; the rest wait for the next


def index(path: Path, settings: semantic.Settings, cache: Path) -> semantic.Indexed:
    """The vectors ``settings`` asks for, in the graph at ``path``; embedded only when ``cache`` lacks them, for at
    most EMBED_BUDGET_SECONDS (the model is downloaded first, here, never by a search). Blocking."""
    return semantic.index_file(path, settings, cache, leave_wal=True, budget=EMBED_BUDGET_SECONDS)


def model_available(model: str) -> bool:
    """Whether this process can load ``model`` without a download: what a search needs."""
    try:
        semantic.embedder(model)
    except semantic.SemanticUnavailable:
        return False
    return True

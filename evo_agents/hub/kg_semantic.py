"""Whether the hub's graph of a project is searched by meaning too (``evo_agents.kg.semantic``): the project's row of
kg_semantic, off without one. ``read`` gives the build its settings; ``write`` is how the KG optimize loop turns the
search by meaning on, to ``on`` only when the holdout of its eval set says it helps (plan kg-retrieval-quality, a12):
hit@5 at least five points above the search by words, tokens per answer file not up, p95 under a second.
``index`` puts the vectors the settings ask for into the rows of a build, embedding only what the project's
``HubCache`` lacks: its vectors by model and digest of their text, in kg_vector_cache."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.kg import graph_rows, semantic, store_vectors
from evo_agents.kg.snapshot import StoreRows

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


def index(
    rows: StoreRows, settings: semantic.Settings, cache: semantic.Cache | Path
) -> tuple[StoreRows, semantic.Indexed]:
    """``rows`` with the vectors ``settings`` asks for (``evo_agents.kg.graph_rows.RowsVectors``), and what was indexed;
    embedded only when ``cache`` (a HubCache, or the path of a vectors.sqlite) lacks them, for at most
    EMBED_BUDGET_SECONDS (the model is downloaded first, here, never by a search). Blocking."""
    graph = graph_rows.RowsVectors(rows)
    own = store_vectors.FileCache(cache) if isinstance(cache, Path) else None
    try:
        found = semantic.index(graph, settings, own if own is not None else cache, budget=EMBED_BUDGET_SECONDS)
    finally:
        if own is not None:
            own.close()
    return graph.held, found


SEED_FILES = (
    store_vectors.CACHE_FILE,
    "graph.sqlite",
)  # what a project's builds kept vectors in before kg_vector_cache
PUT_ROWS = 1000  # vectors one insert of the cache writes


class HubCache:
    """The vectors of project ``project_id`` by model and digest of their text, in kg_vector_cache
    (``semantic.Cache``). Its calls block: they run on ``loop``, the hub's, from the thread of a build."""

    def __init__(self, engine, loop: asyncio.AbstractEventLoop, project_id: int):
        self.engine, self.loop, self.project_id = engine, loop, project_id

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def get(self, model: str, digests: list[str]) -> dict[str, bytes]:
        return self._run(self._get(model, digests))

    def put(self, model: str, rows: Iterable[tuple[str, bytes]]) -> None:
        self._run(self._put(model, list(rows)))

    def keep(self, model: str, digests: set[str]) -> None:
        self._run(self._keep(model, digests))

    def held(self, model: str) -> bool:
        """Whether the cache has a vector of ``model``."""
        return self._run(self._held(model))

    def close(self) -> None:
        pass

    def seed(self, root: Path, model: str) -> int:
        """When the cache has no vector of ``model``, take the ones the project's directory ``root`` kept of it, in
        its vectors.sqlite and its graph.sqlite, so the first build after kg_vector_cache embeds nothing again. The
        files stay. How many vectors it took."""
        if self.held(model):
            return 0
        found: dict[str, bytes] = store_vectors.cache_file_vectors(root / SEED_FILES[0], model)
        found.update(store_vectors.cached_in_file(root / SEED_FILES[1], model))
        self.put(model, sorted(found.items()))
        return len(found)

    async def _get(self, model: str, digests: list[str]) -> dict[str, bytes]:
        t = tables.kg_vector_cache
        out: dict[str, bytes] = {}
        async with self.engine.connect() as conn:
            for i in range(0, len(digests), PUT_ROWS):
                chunk = digests[i : i + PUT_ROWS]
                rows = await conn.execute(
                    select(t.c.digest, t.c.vector).where(
                        t.c.project_id == self.project_id, t.c.model == model, t.c.digest.in_(chunk)
                    )
                )
                out.update((d, bytes(v)) for d, v in rows)
        return out

    async def _put(self, model: str, rows: list[tuple[str, bytes]]) -> None:
        t = tables.kg_vector_cache
        async with self.engine.begin() as conn:
            for i in range(0, len(rows), PUT_ROWS):
                values = [
                    {"project_id": self.project_id, "model": model, "digest": d, "vector": v}
                    for d, v in rows[i : i + PUT_ROWS]
                ]
                statement = pg_insert(t).values(values)
                await conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[t.c.project_id, t.c.model, t.c.digest],
                        set_={"vector": statement.excluded.vector},
                    )
                )

    async def _keep(self, model: str, digests: set[str]) -> None:
        t = tables.kg_vector_cache
        async with self.engine.begin() as conn:
            held = (
                await conn.execute(select(t.c.digest).where(t.c.project_id == self.project_id, t.c.model == model))
            ).scalars()
            gone = sorted(d for d in held if d not in digests)
            for i in range(0, len(gone), PUT_ROWS):
                await conn.execute(
                    delete(t).where(
                        t.c.project_id == self.project_id, t.c.model == model, t.c.digest.in_(gone[i : i + PUT_ROWS])
                    )
                )

    async def _held(self, model: str) -> bool:
        t = tables.kg_vector_cache
        async with self.engine.connect() as conn:
            found = await conn.execute(
                select(t.c.digest).where(t.c.project_id == self.project_id, t.c.model == model).limit(1)
            )
            return found.first() is not None


def model_available(model: str) -> bool:
    """Whether this process can load ``model`` without a download: what a search needs."""
    try:
        semantic.embedder(model)
    except semantic.SemanticUnavailable:
        return False
    return True

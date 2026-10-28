"""Whether the hub's graph of a project is searched by meaning too (``evo_agents.hub.kg_semantic``, schema 0180): off
without a row, set by the KG optimize loop, and the vectors a hub build puts into the graph it keeps out of WAL mode."""

import asyncio

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from evo_agents.hub import kg_semantic
from evo_agents.kg import semantic
from evo_agents.kg.store import Store
from tests.hub import live
from tests.hub.test_migrate import seed
from tests.kg.test_hybrid import FAKE, Words, with_garage
from tests.kg.test_store import sync_build


def roundtrip(db, project_id: int, settings: semantic.Settings | None, user_id: int) -> semantic.Settings:
    async def run():
        engine = create_async_engine(
            "postgresql+psycopg://", poolclass=NullPool, async_creator=lambda: psycopg.AsyncConnection.connect(db.dsn)
        )
        try:
            if settings is not None:
                async with engine.begin() as conn:
                    await kg_semantic.write(conn, project_id, settings, user_id)
            async with engine.connect() as conn:
                return await kg_semantic.read(conn, project_id)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_project_is_off_until_its_row_says_otherwise(hub_db):
    with live.engine(hub_db).begin() as conn:
        ids = seed(conn)
    project, user = ids["project"], ids["user"]
    assert roundtrip(hub_db, project, None, user) == semantic.Settings("off", semantic.DEFAULT_MODEL)
    assert roundtrip(hub_db, project, semantic.Settings("index"), user) == semantic.Settings("index")
    assert roundtrip(hub_db, project, semantic.Settings("on", FAKE), user) == semantic.Settings("on", FAKE)


def test_a_hub_build_writes_the_vectors_out_of_wal_and_off_drops_them(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg-home"))
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, Words)
    project = with_garage(tmp_path, tmp_path / "kg-home")
    sync_build(project)
    graph = project.root / "graph.sqlite"
    store = Store(graph)
    store.leave_wal()
    store.close()
    done = kg_semantic.index(graph, semantic.Settings("index", FAKE), tmp_path / "cache" / semantic.CACHE_FILE)
    assert done.digest and done.rows > 0 and done.embedded == done.rows and not done.error
    assert not graph.with_name("graph.sqlite-wal").exists()
    assert Store.open_file(graph, immutable=True).vector_info().digest == done.digest
    again = kg_semantic.index(graph, semantic.Settings("on", FAKE), tmp_path / "cache" / semantic.CACHE_FILE)
    assert (again.digest, again.embedded) == (done.digest, 0)
    assert (
        kg_semantic.index(graph, semantic.Settings("off", FAKE), tmp_path / "cache" / semantic.CACHE_FILE).digest
        is None
    )
    assert Store.open_file(graph).vector_info() is None
    semantic.forget_models()

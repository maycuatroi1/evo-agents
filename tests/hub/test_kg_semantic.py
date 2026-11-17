"""Whether the hub's graph of a project is searched by meaning too (``evo_agents.hub.kg_semantic``, schema 0181): off
without a row, set by the KG optimize loop, the vectors a hub build puts into the rows it keeps, and
the project's cache of them in kg_vector_cache (0254)."""

import asyncio
import threading
from dataclasses import replace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from evo_agents import sqlite_legacy
from evo_agents.hub import kg_semantic
from evo_agents.kg import semantic
from evo_agents.kg.snapshot import StoreRows
from tests.hub import live
from tests.hub.test_migrate import seed
from tests.kg import built
from tests.kg.built import old_vector_cache, sync_build
from tests.kg.test_hybrid import FAKE, Words, with_garage


def rows_of(project) -> StoreRows:
    """The rows of the project's latest build, as a hub build keeps them before it puts its vectors in."""
    return replace(built.rows_of(project), vectors=(), vector_info=None)


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


def test_a_hub_build_writes_the_vectors_into_its_rows_and_off_drops_them(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg-home"))
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, Words)
    project = with_garage(tmp_path, tmp_path / "kg-home")
    sync_build(project)
    cache = semantic.MemoryCache()
    rows, done = kg_semantic.index(rows_of(project), semantic.Settings("index", FAKE), cache)
    assert done.digest and done.rows > 0 and done.embedded == done.rows and not done.error
    assert len(rows.vectors) == done.rows and rows.vector_info["digest"] == done.digest
    again_rows, again = kg_semantic.index(rows_of(project), semantic.Settings("on", FAKE), cache)
    assert (again.digest, again.embedded) == (done.digest, 0) and again_rows.vectors == rows.vectors
    off, dropped = kg_semantic.index(rows, semantic.Settings("off", FAKE), cache)
    assert dropped.digest is None and off.vectors == () and off.vector_info is None
    semantic.forget_models()


def test_the_rows_set_aside_under_the_model_come_back_as_they_were(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg-home"))
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, Words)
    project = with_garage(tmp_path, tmp_path / "kg-home")
    sync_build(project)
    settings = semantic.Settings("index", FAKE)
    held, done = kg_semantic.index(rows_of(project), settings, None)
    light, aside = kg_semantic.set_aside(rows_of(project))
    assert light.nodes == () and light.wheres == () and light.code == {} and light.keyed
    light, found = kg_semantic.index(light, settings, None)
    assert (found.digest, found.rows) == (done.digest, done.rows)
    assert kg_semantic.taken_back(light, aside) == held
    semantic.forget_models()


def test_the_hub_cache_seeds_from_the_files_then_embeds_only_what_changed(hub_db, tmp_path, monkeypatch):
    with live.engine(hub_db).begin() as conn:
        project_id = seed(conn)["project"]
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg-home"))
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, Words)
    project = with_garage(tmp_path, tmp_path / "kg-home")
    sync_build(project)
    settings = semantic.Settings("index", FAKE)
    embedded = semantic.MemoryCache()
    _, local = kg_semantic.index(rows_of(project), settings, embedded)
    # what a build before kg_vector_cache left in the directory
    files = old_vector_cache(
        project.root / kg_semantic.SEED_FILES[0], FAKE, {d: v for (_, d), v in embedded.held.items()}
    )
    held = sqlite_legacy.vector_cache_rows(files, FAKE)
    assert local.embedded > 0 and len(held) == local.embedded
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    engine = create_async_engine(
        "postgresql+psycopg://", poolclass=NullPool, async_creator=lambda: psycopg.AsyncConnection.connect(hub_db.dsn)
    )
    try:
        cache = kg_semantic.HubCache(engine, loop, project_id)
        assert not cache.held(FAKE)
        assert cache.seed(project.root, FAKE) == len(held)
        assert cache.seed(project.root, FAKE) == 0  # seeded once: the files are not read again
        assert files.exists()  # and they stay
        _, first = kg_semantic.index(rows_of(project), settings, cache)
        assert (first.digest, first.embedded) == (local.digest, 0)
        assert cache.get(FAKE, sorted(held)) == held
        cache.put(FAKE, [("gone", b"\x00\x00")])
        _, again = kg_semantic.index(rows_of(project), settings, cache)  # rows without them, as a new build's are
        assert again.embedded == 0 and cache.get(FAKE, ["gone"]) == {}  # a build forgets what no row has
        assert not kg_semantic.HubCache(engine, loop, project_id + 1000).held(FAKE)
    finally:
        asyncio.run_coroutine_threadsafe(engine.dispose(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
        semantic.forget_models()


def test_the_seed_readers_take_nothing_from_a_file_that_is_not_one(tmp_path):
    bad = tmp_path / "vectors.sqlite"
    bad.write_bytes(b"not a database" * 100)
    assert sqlite_legacy.vector_cache_rows(bad, FAKE) == {} and sqlite_legacy.graph_vectors(bad, FAKE) == {}
    assert sqlite_legacy.vector_cache_rows(tmp_path / "none", FAKE) == {}

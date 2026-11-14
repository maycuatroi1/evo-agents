"""A build keeps its corpus items and its graph in the hub's tables (``evo_agents.hub.kg_graph_rows``, migration 0247,
plan kg-centralized-on-hub criterion a3): the graph read back hashes as the build, a second build writes only the rows
that changed, a deleted item leaves the corpus, and the readers listening on ``kg_graph`` hear of each build."""

from __future__ import annotations

import asyncio

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from evo_agents.hub import kg_graph_rows, tables
from evo_agents.kg.snapshot import Snapshot, store_rows
from evo_agents.kg.store import Store
from tests.hub.live import add_project
from tests.hub.test_kg import (  # noqa: F401 (hub is a fixture)
    Machine,
    doc,
    hub,
    latest,
    push,
    query,
    run_jobs,
)


def make_engine(db):
    return create_async_engine(db.dsn.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool)


def kept(caplog) -> dict:
    (record,) = [r for r in caplog.records if r.getMessage() == "kg graph kept"]
    return {k: getattr(record, k) for k in ("corpus", "kg_nodes", "kg_edges", "kg_text_rows")}


def build(hub, caplog) -> tuple[dict, dict]:
    caplog.clear()
    with caplog.at_level("INFO", logger="evo_agents.hub.kg_build"):
        run_jobs(hub.db, hub.s3, hub.worker_dir)
    found = latest(hub)
    assert found["status"] == "succeeded", found
    return found, kept(caplog)


def read_back(db, project_id: int):
    async def read():
        engine = make_engine(db)
        try:
            async with engine.connect() as conn:
                return await kg_graph_rows.read_graph(conn, project_id)
        finally:
            await engine.dispose()

    return asyncio.run(read())


def project_id(hub) -> int:
    projects = tables.projects
    return query(hub.db, select(projects.c.id).where(projects.c.name == "alpha"))[0][0]


def test_the_graph_a_build_keeps_hashes_as_the_build_and_reads_as_its_file(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    first, changed = build(hub, caplog)
    assert changed["corpus"] == {"new": 2, "changed": 0, "deleted": 0}
    assert changed["kg_nodes"]["new"] == first["nodes"] and changed["kg_edges"]["new"] == first["edges"]

    heads = tables.kg_graph_heads
    assert query(hub.db, select(heads.c.build_id, heads.c.content_hash)) == [(first["id"], first["content_hash"])]
    rows = read_back(hub.db, project_id(hub))
    assert rows is not None and rows.content_hash == first["content_hash"]
    path = hub.worker_dir / "kg" / "home" / "alpha" / "graph.sqlite"
    store = Store.open_file(path, immutable=True)
    try:
        own = store_rows(store, store.latest_ready())
    finally:
        store.close()
    assert sorted(rows.nodes) == sorted(own.nodes) and sorted(rows.edges) == sorted(own.edges)
    assert rows.keyed == own.keyed and rows.code == own.code and rows.top == own.top
    mine, theirs = Snapshot.from_stored(rows), Snapshot.from_stored(own)
    assert mine.ids == theirs.ids and mine.edges == theirs.edges


def test_a_second_build_writes_only_the_rows_that_changed_and_a_deleted_item_leaves(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it."), doc("faq", "Ask."))
    assert push(hub, laptop).ok
    first, _ = build(hub, caplog)

    laptop.sync("docs", doc("guide", "Edited.", rev="2"), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    second, changed = build(hub, caplog)
    assert second["content_hash"] != first["content_hash"]
    assert changed["corpus"] == {"new": 0, "changed": 1, "deleted": 1}
    nodes = changed["kg_nodes"]
    assert 0 < nodes["new"] + nodes["changed"] + nodes["deleted"] < first["nodes"], nodes
    corpus = tables.kg_corpus_items
    assert sorted(k.rsplit(":", 1)[1] for (k,) in query(hub.db, select(corpus.c.item_key))) == ["guide", "setup"]
    rows = read_back(hub.db, project_id(hub))
    assert rows.content_hash == second["content_hash"] and len(rows.nodes) == second["nodes"]


def test_a_build_notifies_the_readers_of_kg_graph(hub, tmp_path, caplog):
    import psycopg

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    with psycopg.connect(hub.db.dsn, autocommit=True) as listener:
        listener.execute(f"LISTEN {kg_graph_rows.CHANNEL}")
        found, _ = build(hub, caplog)
        heard = list(listener.notifies(timeout=5, stop_after=1))
    assert [n.channel for n in heard] == [kg_graph_rows.CHANNEL]
    assert heard[0].payload == f'{{"project_id": {project_id(hub)}, "branch_id": 0, "build_id": {found["id"]}}}'


def test_the_memo_keeps_the_first_result_of_a_row_hash(hub_db):
    pid = add_project(hub_db, "memo")

    async def run():
        engine = make_engine(hub_db)
        try:
            async with engine.begin() as conn:
                await kg_graph_rows.memo_put(conn, pid, "entities", {"h1": {"n": 1}})
                await kg_graph_rows.memo_put(conn, pid, "entities", {"h1": {"n": 2}, "h2": {"n": 3}})
                return await kg_graph_rows.memo_get(conn, pid, "entities", ["h1", "h2", "h3"])
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == {"h1": {"n": 1}, "h2": {"n": 3}}

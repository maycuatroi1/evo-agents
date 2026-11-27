"""A build keeps its corpus items and its graph in the hub's tables (``evo_agents.hub.kg_graph_rows``, migration 0248,
plan kg-centralized-on-hub criterion a3): the graph read back hashes as the build and holds the rows a build in memory
of the same corpus gives (``evo_agents.kg.build.build_rows``), its snapshot reads, searches and ranks as theirs, a second
build writes only the rows that changed, a deleted item leaves the corpus, and the readers listening on ``kg_graph``
hear of each build."""

from __future__ import annotations

import asyncio
import struct

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from evo_agents.hub import kg_graph_rows, tables
from evo_agents.kg import asked
from evo_agents.kg.graph_rows import with_asked
from evo_agents.kg.snapshot import Snapshot, SnapshotView, StoreRows, edge_row, node_row
from evo_agents.schema.policy import Label
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
from tests.kg import built as memory
from tests.kg.test_policy_dnf import _node, assembled


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


def built_here(hub, laptop) -> StoreRows:
    """The rows of the project as a build in memory of the machine's corpus gives them (``build_rows``), which hold
    what the hub built."""
    project = laptop.project()
    assert memory.build(project).content_hash == read_back(hub.db, project_id(hub)).summary["content_hash"]
    return memory.rows_of(project)


def placed(summary: dict) -> dict:
    """``summary`` without what the hub and a machine give a build each its own way: when, its id, the file, the
    inputs."""
    stats = {k: v for k, v in summary["stats"].items() if k not in ("state_digest", "inputs")}
    return {**{k: v for k, v in summary.items() if k not in ("built_at", "build_id", "file_bytes")}, "stats": stats}


def test_the_graph_a_build_keeps_hashes_as_the_build_and_holds_the_rows_of_a_build_in_memory(hub, tmp_path, caplog):
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
    assert not (hub.worker_dir / "kg" / "home" / "alpha" / "graph.sqlite").exists(), "the hub writes no graph file"
    assert memory.build(laptop.project()).content_hash == first["content_hash"]
    own = memory.rows_of(laptop.project())
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


def here_view(hub, laptop) -> SnapshotView:
    """The snapshot of the build in memory of the machine's corpus, as a reader with no audience reads it."""
    return Snapshot.from_stored(built_here(hub, laptop)).view()


def test_a_snapshot_of_the_tables_reads_as_the_snapshot_of_a_build_in_memory(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it, see guide."))
    assert push(hub, laptop).ok
    first, _ = build(hub, caplog)
    view = Snapshot.from_stored(read_back(hub.db, project_id(hub))).view()
    here = here_view(hub, laptop)
    b = here.latest_ready()
    nodes = sorted(here.alive_ids("nodes", b))
    elements = [*nodes, *sorted(here.alive_ids("edges", b))]
    units = sorted(here.alive_ids("units", b))
    assert units and nodes
    assert sorted(view.alive_ids("nodes", b)) == nodes and sorted(view.alive_ids("units", b)) == units
    assert placed(view.summary()) == placed(here.summary()) and view.latest_ready() == first["id"]
    assert view.seen(b) == here.seen(b)
    assert view.top_audience(b) == here.top_audience(b)
    assert [view.unit(u) for u in units] == [here.unit(u, b) for u in units]
    assert [view.where(e) for e in elements] == [here.where(e, b) for e in elements]
    assert [view.derivations(e) for e in elements] == [here.derivations(e, b) for e in elements]
    assert any(view.where(e) for e in elements) and any(view.derivations(e) for e in elements)
    assert [view.body(n) for n in nodes] == [here.body(n) for n in nodes]
    assert any(view.body(n) for n in nodes)
    assert [view.degree(n) for n in nodes] == [here.degree(n, b) for n in nodes]
    assert view.path_nodes() == here.path_nodes(b)
    assert view.file_symbols(nodes) == here.file_symbols(nodes, b)


def test_a_snapshot_of_the_tables_searches_and_counts_as_the_snapshot_of_a_build_in_memory(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it, see guide."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    view = Snapshot.from_stored(read_back(hub.db, project_id(hub))).view()
    here = here_view(hub, laptop)
    try:
        b = here.latest_ready()
        nodes = sorted(here.alive_ids("nodes", b))
        for query in ("guide", "install app", "works"):
            assert view.search(query, b) == here.search(query, b)
            assert view.search_scored(query, b) == here.search_scored(query, b)
            assert view.plain_search(query, 10) == here.plain_search(query, 10)
            assert view.plain_search(query, 10, ["Document"]) == here.plain_search(query, 10, ["Document"])
            assert view.matched(nodes, query) == here.matched(nodes, query)
        assert view.search("guide", b) and view.plain_search("guide", 10)
        assert any(m["words"] for m in view.matched(nodes, "guide").values())
        assert view.code_files(["guide"]) == here.code_files(["guide"])
        assert [view.code_lines(n, ["guide"]) for n in nodes] == [here.code_lines(n, ["guide"]) for n in nodes]
        assert view.asked_nodes(nodes) == here.asked_nodes(nodes) == set()
        assert view.asked_clearance is None and here.asked_clearance is None
        assert [view.alias_of(n, b) for n in nodes] == [here.alias_of(n, b) for n in nodes]
        assert view.alias_of("docs:doc:nowhere", b) is None
        assert view.degrees(nodes, b, (9, 9)) == here.degrees(nodes, b, (9, 9))
        assert view.degrees(nodes, b, (0, 0)) == here.degrees(nodes, b, (0, 0))
        assert sorted(view.kind_counts(b)) == sorted(here.kind_counts(b))
        assert sorted(view.shape_counts(b)) == sorted(here.shape_counts(b))
        assert view.shape_counts(b)
        assert view.build_row(b)["coverage"] == here.build_row(b)["coverage"]
        assert sorted(view.file_ids()) == sorted(here.file_ids())
        low, high = nodes[0], nodes[len(nodes) // 2]
        assert view.nodes_between(low, high) == here.nodes_between(low, high)
        for n in nodes:
            assert view.changed_by(n, 20) == here.changed_by(n, 20)
        assert sorted(view.changes_of(nodes)) == sorted(here.changes_of(nodes))
    finally:
        here.close()
        view.close()


def write_and_read(db, project_id: int, rows: StoreRows) -> StoreRows:
    async def run():
        engine = make_engine(db)
        try:
            async with engine.begin() as conn:
                await kg_graph_rows.write_graph(conn, project_id, None, rows)
            async with engine.connect() as conn:
                return await kg_graph_rows.read_graph(conn, project_id)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_snapshot_of_the_tables_reads_the_asked_rows_as_the_snapshot_of_a_build_in_memory(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."), doc("setup", "Install it, see guide."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    rows = built_here(hub, laptop)
    nodes = sorted(row[0] for row in rows.nodes)
    guide, setup = (next(n for n in nodes if n.endswith(name)) for name in ("guide", "setup"))
    found = [
        asked.Row(guide, 0, 0, None, 1.15, "install install app"),
        asked.Row(guide, 9, 9, None, 1.2, "works"),
        asked.Row(setup, 0, 0, '["chat:1"]', 1.1, "works works"),
    ]
    stored, _ = with_asked(rows, found, 2.0)
    back = write_and_read(hub.db, add_project(hub.db, "asked"), stored)
    assert back.asked == stored.asked and len(back.asked) == 3 and back.asked_weight == 2.0
    snap, own = Snapshot.from_stored(back), Snapshot.from_stored(stored)
    b = own.view().latest_ready()
    for reader in (frozenset(), frozenset({"chat:1"})):
        view, here = snap.view(reader), own.view(reader)
        assert view.asked_nodes(nodes) == here.asked_nodes(nodes) == {guide, setup}
        assert view.weight_of_asked() == here.weight_of_asked() == 2.0
        moved = 0
        for clearance in (None, (0, 0), (9, 9)):
            view.asked_clearance = here.asked_clearance = clearance
            for words in ("install app", "works", "guide"):
                assert view.search_scored(words, b) == here.search_scored(words, b), (reader, clearance, words)
                here.asked_clearance = None
                moved += clearance is not None and view.search_scored(words, b) != here.search_scored(words, b)
                here.asked_clearance = clearance
        assert moved  # the asked rows changed a ranking
        view.asked_weight = here.asked_weight = 0.5
        assert view.weight_of_asked() == 0.5
        assert view.search_scored("works", b) == here.search_scored("works", b)


def test_the_vectors_of_a_build_read_back_by_their_node_vec_key(hub_db):
    pid = add_project(hub_db, "vectors")
    vec = struct.pack("<2e", 0.5, -0.25)
    rows = StoreRows(
        "sha256:v",
        (),
        (),
        (),
        {},
        ((0, 0), frozenset()),
        vectors=(("k1", "docs:doc:a", "Document", None, vec), ("k2", "docs:doc:a", "Section", '["chat:1"]', vec)),
        vector_info={"model": "m", "dim": 2, "mode": "dense", "digest": "d"},
    )

    async def run():
        engine = make_engine(hub_db)
        try:
            async with engine.begin() as conn:
                await kg_graph_rows.write_graph(conn, pid, None, rows)
            async with engine.connect() as conn:
                return await kg_graph_rows.read_graph(conn, pid)
        finally:
            await engine.dispose()

    back = asyncio.run(run())
    assert back.vectors == rows.vectors and back.vector_info == rows.vector_info
    vectors = Snapshot.from_stored(back).vectors
    assert vectors.node_ids == ("docs:doc:a", "docs:doc:a") and vectors.auds == (None, '["chat:1"]')
    assert list(vectors.rows[0]) == [0.5, -0.25]


def test_writing_the_same_graph_again_changes_no_row(hub_db):
    """Postgres keeps a jsonb object's keys in its own order: a row with an object (a review item, a cluster's value,
    a file's code body) still compares equal once read back, so a build of an unchanged graph rewrites nothing."""
    pid = add_project(hub_db, "again")
    item = {"fingerprint": "f1", "title": "Two guides", "nodes": ["docs:doc:a", "docs:doc:b"], "kind": "duplicate"}
    rows = StoreRows(
        "sha256:again",
        (),
        (),
        (("docs:doc:a", ("guide", "how the app works")),),
        {"code:file:a": "def main(): pass"},
        ((0, 0), frozenset()),
        derivations=(("docs:doc:a", '{"zeta": 1, "alpha": 2}', "[]"),),
        asked=(("docs:doc:a", 0, 0, None, 1.1, "works"),),
        clusters=(("cluster", 1, {"zeta": ["docs:doc:a"], "alpha": "Guides"}),),
        review=(item,),
    )

    async def run():
        engine = make_engine(hub_db)
        try:
            counts = []
            for _ in range(2):
                async with engine.begin() as conn:
                    counts.append(await kg_graph_rows.write_graph(conn, pid, None, rows))
            return counts
        finally:
            await engine.dispose()

    first, second = asyncio.run(run())
    assert first["kg_review_rows"]["new"] == 2 and first["kg_text_rows"]["new"] == 2
    assert all(c == {"new": 0, "changed": 0, "deleted": 0} for c in second.values()), second


def test_the_head_of_a_graph_reads_its_clusters_and_none_of_its_other_rows(hub_db):
    pid = add_project(hub_db, "head")
    item = {"fingerprint": "f1", "title": "Two guides", "nodes": ["docs:doc:a", "docs:doc:b"], "kind": "duplicate"}
    rows = StoreRows(
        "sha256:head",
        (),
        (),
        (("docs:doc:a", ("guide", "how the app works")),),
        {"code:file:a": "def main(): pass"},
        ((1, 2), frozenset({"chat:1"})),
        derivations=(("docs:doc:a", "[]", "[]"),),
        summary={"ready": True, "content_hash": "sha256:head"},
        clusters=(("cluster", 1, {"label": "Guides"}),),
        review=(item,),
    )

    async def run():
        engine = make_engine(hub_db)
        try:
            async with engine.begin() as conn:
                await kg_graph_rows.write_graph(conn, pid, None, rows)
            async with engine.connect() as conn:
                return await kg_graph_rows.read_head(conn, pid), await kg_graph_rows.read_head(conn, pid + 1000)
        finally:
            await engine.dispose()

    head, none = asyncio.run(run())
    assert none is None
    assert (head.content_hash, head.summary, head.top) == (rows.content_hash, rows.summary, rows.top)
    assert (head.clusters, head.review) == (rows.clusters, rows.review)
    assert (head.nodes, head.keyed, head.code, head.derivations) == ((), (), {}, ())


def test_an_element_two_chats_reveal_keeps_its_guards_and_a_second_write_changes_no_row(hub_db):
    """Migration 0267: the guards of a node and an edge each chat states with its own props are kept in
    ``label_guards``, read back to the props of the chat their reader is in, and compare equal once read back."""
    units = {
        "u5": Label(1, 0, "U", frozenset({"demo"}), frozenset({"chat:5"})),
        "u6": Label(1, 0, "U", frozenset({"demo"}), frozenset({"chat:6"})),
    }
    facts = [_node(n, u, {"said": u}) for n in ("code:J", "code:K") for u in units]
    for u in units:
        facts.append({**_node("", u, {"said": u}), "t": "edge", "src": "code:J", "rel": "uses", "dst": "code:K"})
    graph = assembled(units, facts)
    edge, (nid, node) = next(iter(graph.edges.values())), next(iter(graph.nodes.items()))
    assert "guards" in node and "guards" in edge
    rows = StoreRows(
        "sha256:guards",
        tuple(node_row(n, x) for n, x in graph.nodes.items()),
        tuple(edge_row(e, x) for e, x in graph.edges.items()),
        (),
        {},
        ((0, 0), frozenset()),
    )
    pid = add_project(hub_db, "guards")

    async def run():
        engine = make_engine(hub_db)
        try:
            counts = []
            for _ in range(2):
                async with engine.begin() as conn:
                    counts.append(await kg_graph_rows.write_graph(conn, pid, None, rows))
            async with engine.connect() as conn:
                return counts, await kg_graph_rows.read_graph(conn, pid)
        finally:
            await engine.dispose()

    (_, second), back = asyncio.run(run())
    assert all(c == {"new": 0, "changed": 0, "deleted": 0} for c in second.values()), second
    snap = Snapshot.from_stored(back)
    for chat, said in (("chat:5", "u5"), ("chat:6", "u6")):
        view = snap.view(frozenset({chat}), (1, 0))
        assert view.node(nid)["props"] == {"said": said}
        (out,) = view.edges_of("code:J")
        assert out["props"] == {"said": said}
    assert snap.view(frozenset({"chat:7"}), (1, 0)).node(nid) is None

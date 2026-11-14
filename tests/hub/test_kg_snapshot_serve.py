"""The api reads a project's graph from an in-RAM snapshot made of the hub's tables (``evo_agents.hub.kg_snapshots``,
plan kg-centralized-on-hub criterion a4): it is loaded on first read, a build's notice on ``kg_graph`` swaps in the new
one while a reader keeps the old one, and each reader sees it through its clearance and audience."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import kg_graph_rows, kg_snapshots
from evo_agents.hub.kg_graph_rows import MAIN
from evo_agents.kg.snapshot import Snapshot, store_rows
from evo_agents.kg.snapshot_search import SnapshotSearch
from evo_agents.kg.store import Store
from tests.hub.live import add_project
from tests.hub.test_kg import Machine, doc, hub, push  # noqa: F401 (hub is a fixture)
from tests.hub.test_kg_postgres_build import build, make_engine, project_id

INTERNAL = (1, 0)  # the clearance (level, location) of a reader of internal, the label of the docs source
PUBLIC = (0, 0)


def found(snap: Snapshot, query: str, clearance=INTERNAL, audience: frozenset[str] = frozenset()) -> set[str]:
    results, _ = SnapshotSearch(snap, clearance, audience).search(query)
    return {n["name"] for n in results}


async def held(db, pid: int):
    engine = make_engine(db)
    try:
        return await kg_snapshots.SnapshotCoordinator(engine).get(pid)
    finally:
        await engine.dispose()


def test_a_snapshot_is_loaded_from_the_tables_of_the_latest_build(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    first, _ = build(hub, caplog)
    got = asyncio.run(held(hub.db, project_id(hub)))
    assert got.build_id == first["id"] and got.snapshot.content_hash == first["content_hash"]
    assert "guide" in found(got.snapshot, "kiwi")
    assert asyncio.run(held(hub.db, project_id(hub) + 1000)) is None  # a project without a build has no snapshot


def test_a_notice_swaps_in_the_new_snapshot_while_a_reader_keeps_the_old_one(hub, tmp_path, caplog, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    first, _ = build(hub, caplog)
    laptop.sync("docs", doc("guide", "How the kiwi app works."), doc("mango", "The mango release."))
    assert push(hub, laptop).ok
    pid = project_id(hub)
    release, building = threading.Event(), threading.Event()
    from_stored = Snapshot.from_stored

    def slow(rows, *args, **kwargs):
        if rows.content_hash != first["content_hash"]:
            building.set()
            assert release.wait(30)
        return from_stored(rows, *args, **kwargs)

    monkeypatch.setattr(kg_snapshots.Snapshot, "from_stored", slow)

    async def run():
        engine = make_engine(hub.db)
        coordinator = kg_snapshots.SnapshotCoordinator(engine)
        try:
            old = await coordinator.get(pid)
            second, _ = await asyncio.to_thread(build, hub, caplog)
            coordinator.notified(json.dumps({"project_id": pid, "branch_id": MAIN, "build_id": second["id"]}))
            assert await asyncio.to_thread(building.wait, 30)
            during = await asyncio.wait_for(coordinator.get(pid), 1)  # the reader does not wait for the build
            release.set()
            await asyncio.gather(*coordinator._tasks)
            return old, during, await coordinator.get(pid), second
        finally:
            release.set()
            await coordinator.close()
            await engine.dispose()

    old, during, new, second = asyncio.run(run())
    assert during is old and old.build_id == first["id"] and "mango" not in found(old.snapshot, "mango")
    assert new.build_id == second["id"] and new.snapshot.content_hash == second["content_hash"]
    assert "mango" in found(new.snapshot, "mango")
    assert "mango" not in found(old.snapshot, "mango")  # what a reader held before the swap stays as it was


def test_a_private_chat_item_is_invisible_to_a_member_outside_its_audience(hub_db, tmp_path, monkeypatch):
    from tests.hub import test_kg_audience as aud

    graph = aud.build(tmp_path, monkeypatch, 1, 2)  # chat items, each with the audience of its chat
    path = tmp_path / "graph.sqlite"
    path.write_bytes(graph.data)
    pid = add_project(hub_db, "aud")
    store = Store.open_file(path, immutable=True)
    try:
        b = store.latest_ready()
        rows = store_rows(store, b)
        nodes, units = sorted(store.alive_ids("nodes", b)), sorted(store.alive_ids("units", b))
        for reader in (frozenset(), frozenset({"chat:1"}), frozenset({"chat:1", aud.GB})):
            store.reader = reader  # a reader within some chats reads the snapshot as it reads the file
            view = Snapshot.from_stored(rows).view(reader)
            assert view.seen(b) == store.seen(b)
            assert [view.unit(u) for u in units] == [store.unit(u, b) for u in units]
            assert [view.body(n) for n in nodes] == [store.body(n) for n in nodes]
            assert [view.degree(n) for n in nodes] == [store.degree(n, b) for n in nodes]
    finally:
        store.close()

    async def keep():
        engine = make_engine(hub_db)
        try:
            async with engine.begin() as conn:
                await kg_graph_rows.write_graph(conn, pid, None, rows)
        finally:
            await engine.dispose()

    asyncio.run(keep())
    snap = asyncio.run(held(hub_db, pid)).snapshot
    assert found(snap, "kiwi rollout") == set()  # a member in no chat
    one = found(snap, "kiwi rollout", audience=frozenset({"chat:1"}))
    assert "#2 dave" in one and "#1 alice" not in one  # #1 was also sent to the agent of the other project
    assert "#1 alice" in found(snap, "kiwi rollout", audience=frozenset({"chat:1", aud.GB}))
    assert "#3 alice" not in found(snap, "mango", audience=frozenset({"chat:1"}))


def test_a_reader_below_the_label_of_the_docs_sees_nothing_of_them(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    snap = asyncio.run(held(hub.db, project_id(hub))).snapshot
    assert "guide" in found(snap, "kiwi", INTERNAL)
    assert found(snap, "kiwi", PUBLIC) == set()


def test_each_reader_gets_a_view_of_its_own_over_the_one_snapshot(hub, tmp_path, caplog):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    build(hub, caplog)

    async def run():
        engine = make_engine(hub.db)
        try:
            coordinator = kg_snapshots.SnapshotCoordinator(engine)
            pid = project_id(hub)
            return await coordinator.view(pid, frozenset()), await coordinator.view(pid, frozenset({"chat:1"}), (0, 0))
        finally:
            await engine.dispose()

    first, second = asyncio.run(run())
    assert first.snap is second.snap and first is not second
    assert (first.reader, first.ceiling) == (frozenset(), None)
    assert (second.reader, second.ceiling) == (frozenset({"chat:1"}), (0, 0))
    first.reader = frozenset({"chat:2"})  # a session sets the reader of its own view
    assert second.reader == frozenset({"chat:1"})

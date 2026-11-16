"""The api reads a project's graph from an in-RAM snapshot made of the hub's tables (``evo_agents.hub.kg_snapshots``,
plan kg-centralized-on-hub criterion a4): it is loaded on first read, a build's notice on ``kg_graph`` swaps in the new
one while a reader keeps the old one, and each reader sees it through its clearance and audience."""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import kg_graph_rows, kg_snapshots
from evo_agents.hub.kg_graph_rows import MAIN
from evo_agents.kg.snapshot import Snapshot
from evo_agents.kg.snapshot_search import SnapshotSearch
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
    rows = graph.rows
    pid = add_project(hub_db, "aud")
    items = ("chat:text:m1", "chat:text:m2", "chat:text:m3", "chat:text:plan")
    # what a reader within some chats reads of the items: the ones it may read, and the counts of what it sees
    reads = {
        frozenset(): ({"chat:text:plan"}, (2, 1, 1)),
        frozenset({"chat:1"}): ({"chat:text:m2", "chat:text:plan"}, (3, 3, 2)),
        frozenset({"chat:1", aud.GB}): ({"chat:text:m1", "chat:text:m2", "chat:text:plan"}, (5, 5, 3)),
    }
    for reader, (readable, counts) in reads.items():
        view = Snapshot.from_stored(rows).view(reader)
        seen = view.seen()
        assert (seen["nodes"], seen["edges"], seen["units"]) == counts, reader
        assert {n for n in items if view.unit(f"{n}#") is not None} == readable, reader
        assert {n for n in items if view.body(n) is not None} == readable, reader
        assert {n for n in items if view.node(n) is not None} == readable, reader
        assert all(view.degree(n) == 0 for n in items if n not in readable), reader

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


def aud_view_of(tmp_path, monkeypatch):
    """The graph of aud with the items of chats 1 and 2 (``tests.hub.test_kg_audience``), built in memory, and the
    ids of what each reader below sees of it: the plan and the source have no audience, #1 is in chat 1 and the grant
    of aud-b, #2 in chat 1 alone, #3 in chat 2 and the grant, K-9 is mentioned by #1 alone."""
    from tests.hub import test_kg_audience as aud
    from tests.kg import built

    project = aud.synced(tmp_path, monkeypatch, 1, 2)
    assert built.build(project).ok
    public = {aud.PLAN, "source:chat"}
    sees = {
        frozenset(): public,
        frozenset({"chat:1"}): public | {aud.M2},
        frozenset({"chat:1", aud.GB}): public | {aud.M1, aud.M2, aud.K9},
    }
    return built.snapshot_of(project), sees


def test_the_labelled_nodes_of_a_view_say_which_its_reader_sees(tmp_path, monkeypatch):
    from evo_agents.kg.labelled import labelled_nodes
    from tests.hub import test_kg_audience as aud

    snap, sees = aud_view_of(tmp_path, monkeypatch)
    every = {n["id"] for n in snap.nodes}
    assert {aud.M1, aud.M2, aud.M3, aud.K7, aud.K9} < every
    for reader, seen in sees.items():
        view = snap.view(reader)
        found = list(labelled_nodes(view))
        assert {n.node_id for n in found} == every
        assert {n.node_id for n in found if n.seen} == seen
        assert all((n.level, n.location) == INTERNAL for n in found)  # the label of the chat source
        assert [n.within(INTERNAL) for n in found] == [n.seen for n in found]
        assert not any(n.within(PUBLIC) or n.within(None) for n in found)
        (one,) = labelled_nodes(view, node_id=aud.M2)
        assert (one.node_id, one.seen) == (aud.M2, aud.M2 in seen)
        assert {n.node_id for n in labelled_nodes(view, ("Code",))} == {aud.K7, aud.K9}


def test_the_entity_map_reads_of_a_view_hold_what_its_reader_sees_under_the_ceiling(tmp_path, monkeypatch):
    from evo_agents.kg import entity_map_reads as reads
    from tests.hub import test_kg_audience as aud

    snap, sees = aud_view_of(tmp_path, monkeypatch)
    items = [aud.M1, aud.M2, aud.M3, aud.PLAN]
    mentions = {(aud.M1, aud.PLAN), (aud.M2, aud.PLAN), (aud.M3, aud.M2)}
    for reader, seen in sees.items():
        view = snap.view(reader)
        for ceiling in (None, PUBLIC, INTERNAL, (9, 9)):
            clears = ceiling is not None and ceiling >= INTERNAL  # every row is labelled internal
            assert {r[0] for r in reads.node_rows(view, ceiling)} == (seen if clears else set())
            assert [r[0] for r in reads.node_rows(view, ceiling, ("Code",))] == (
                [aud.K9] if clears and aud.K9 in seen else []
            )
            found = list(reads.item_rows(view, ceiling))
            assert [r[0] for r in found] == sorted(items)  # each item, the ones the reader does not see as 0
            assert {r[0] for r in found if r[3] == 1} == ({i for i in items if i in seen} if clears else set())
            assert set(reads.edge_rows(view, ceiling, "mentions")) == (
                {(src, dst) for src, dst in mentions if src in seen and dst in seen} if clears else set()
            )
        assert reads.cluster_rows(view) == []  # built without clusters


def test_the_coordinator_holds_the_most_recently_read_projects_and_no_finished_load(monkeypatch):
    loads = []

    async def load(engine, project_id):
        loads.append(project_id)
        return kg_snapshots.Held(project_id, None)

    monkeypatch.setattr(kg_snapshots, "load", load)
    monkeypatch.setattr(kg_snapshots, "HELD_PROJECTS", 2)

    async def run():
        coordinator = kg_snapshots.SnapshotCoordinator(object())
        for pid in (1, 2, 1, 3, 1):
            assert (await coordinator.get(pid)).build_id == pid
        return coordinator

    coordinator = asyncio.run(run())
    assert loads == [1, 2, 3]  # 1 was read again before 3 came, so 2 went
    assert list(coordinator._held) == [3, 1] and coordinator._loading == {}


def test_a_read_right_after_a_build_waits_for_its_snapshot_and_not_past_fresh_wait(monkeypatch):
    """A tool call right after a build answers from the snapshot of that build, which the notification began to load
    (an archived node is gone at once); a load slower than FRESH_WAIT leaves the older snapshot answering, a read
    coming once that load ran past FRESH_WAIT waits no more, and a plugin read (``held``) never waits."""
    newest, gate = [1], {}

    async def load(engine, project_id):
        build = newest[0]
        if build in gate:
            await gate[build].wait()
        else:
            await asyncio.sleep(0.2)  # a load of a small graph, still running as the next request comes
        return kg_snapshots.Held(build, None)

    monkeypatch.setattr(kg_snapshots, "load", load)
    monkeypatch.setattr(kg_snapshots, "FRESH_WAIT", 1.0, raising=False)

    async def run():
        coordinator = kg_snapshots.SnapshotCoordinator(object())
        try:
            first = (await coordinator.get(7)).build_id
            newest[0] = 2
            coordinator.notified(json.dumps({"project_id": 7}))  # the build of 2 notified
            fresh = (await coordinator.current(7, 2)).build_id
            newest[0], gate[3] = 3, asyncio.Event()  # the load of 3 hangs past FRESH_WAIT
            coordinator.notified(json.dumps({"project_id": 7}))
            plugin = (await asyncio.wait_for(coordinator.held(7, 3), 0.1)).build_id
            started = asyncio.get_running_loop().time()
            slow = (await coordinator.current(7, 3)).build_id
            waited = asyncio.get_running_loop().time() - started
            started = asyncio.get_running_loop().time()
            later = (await coordinator.current(7, 3)).build_id  # the load of 3 still runs, past FRESH_WAIT already
            again = asyncio.get_running_loop().time() - started
            gate[3].set()
            await asyncio.gather(*coordinator._tasks, *coordinator._loading.values())
            return first, fresh, plugin, slow, waited, later, again, (await coordinator.current(7, 3)).build_id
        finally:
            await coordinator.close()

    first, fresh, plugin, slow, waited, later, again, caught_up = asyncio.run(run())
    assert (first, fresh) == (1, 2)  # the old code answered 1 here: the archived node still read
    assert (plugin, slow, later, caught_up) == (2, 2, 2, 3) and 0.9 <= waited < 3
    assert again < 0.1  # not another FRESH_WAIT for each read while a load of minutes runs


def test_loads_making_snapshots_leave_the_pool_to_the_workers_heartbeats(hub, tmp_path, caplog, monkeypatch):
    """A snapshot is made with no connection held: loads of a large graph, which take minutes to make it, left the
    workers' heartbeats and claims without a connection of the pool, and runs lost their lease."""
    from psycopg_pool import AsyncConnectionPool
    from sqlalchemy import literal, select

    from evo_agents.hub.db import make_engine as pooled_engine

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    pid = project_id(hub)
    release, making = threading.Event(), threading.Semaphore(0)
    from_stored = Snapshot.from_stored

    def slow(*args, **kwargs):
        making.release()
        assert release.wait(30)
        return from_stored(*args, **kwargs)

    monkeypatch.setattr(kg_snapshots.Snapshot, "from_stored", slow)

    async def run():
        pool = AsyncConnectionPool(hub.db.dsn, min_size=0, max_size=2, timeout=2, close_returns=True, open=False)
        await pool.open()
        engine = pooled_engine(pool)
        loads = [asyncio.create_task(kg_snapshots.load(engine, pid)) for _ in range(3)]
        try:
            for _ in range(kg_snapshots.READS):
                assert await asyncio.to_thread(making.acquire, True, 30)
            async with engine.begin() as conn:  # a heartbeat, while the snapshots are made
                beat = await conn.scalar(select(literal(1)))
            release.set()
            done = await asyncio.gather(*loads)
        finally:
            release.set()
            await asyncio.gather(*loads, return_exceptions=True)
            await engine.dispose()
            await pool.close()
        return beat, done

    beat, done = asyncio.run(run())
    assert beat == 1
    assert len({held.snapshot.content_hash for held in done}) == 1 and "guide" in found(done[0].snapshot, "kiwi")


def test_reading_a_graph_gives_the_event_loop_back_between_its_rows(hub, tmp_path, caplog, monkeypatch):
    """The rows of a graph are fetched a batch at a time, the loop running other requests between two batches, and
    their JSON is decoded in a thread: the api read and decoded a large graph's hundreds of thousands of rows in one
    go on its event loop after each build, every request stalled for seconds, and the workers' heartbeats and
    claims timed out waiting for a connection of the pool."""
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", *(doc(name, f"How the {name} of the kiwi app works.") for name in ("guide", "notes", "intro")))
    assert push(hub, laptop).ok
    build(hub, caplog)
    pid = project_id(hub)
    monkeypatch.setattr(kg_graph_rows, "READ_BATCH", 2, raising=False)
    ticks, seen, loads = [0], [], set()
    pairs, rows_json = kg_graph_rows._row_pairs, kg_graph_rows._ROW_JSON

    def counted(name, part):
        for pair in pairs(name, part):
            if name == "kg_nodes":
                seen.append(ticks[0])
            yield pair

    def decoded(text):
        loads.add(threading.current_thread() is threading.main_thread())
        return rows_json.decode(text)

    monkeypatch.setattr(kg_graph_rows, "_row_pairs", counted)
    monkeypatch.setattr(kg_graph_rows, "_ROW_JSON", type("Json", (), {"decode": staticmethod(decoded)}))

    async def run():
        async def tick():
            while True:
                ticks[0] += 1
                await asyncio.sleep(0)

        ticker = asyncio.create_task(tick())
        engine = make_engine(hub.db)
        try:
            async with engine.connect() as conn:
                return await kg_graph_rows.read_graph(conn, pid)
        finally:
            ticker.cancel()
            await engine.dispose()

    rows = asyncio.run(run())
    assert len(rows.nodes) == len(seen) > 2
    assert seen[0] < seen[-1]  # the loop ran between the first batch of nodes and the last
    assert loads == {False}  # every row's JSON was decoded off the loop
    assert "guide" in found(Snapshot.from_stored(rows), "kiwi")


def test_a_row_read_back_parses_once_into_what_the_build_wrote():
    """A JSON row as Postgres keeps it (jsonb puts shorter keys first) parses, in one go, into the row ``_json`` wrote
    with every object's keys sorted: the snapshot reads the same row whichever way the tables were read."""
    written = ["r", "key", {"rule": "x", "evidence": ["a"], "bounds": {"required": 1, "max": 2}, "id": 3.5}]
    kept = '["r", "key", {"id": 3.5, "rule": "x", "bounds": {"max": 2, "required": 1}, "evidence": ["a"]}]'
    back = kg_graph_rows._ROW_JSON.decode(kept)
    assert back == json.loads(kg_graph_rows._json(written))
    assert repr(back) == repr(json.loads(kg_graph_rows._json(written)))  # the keys in the same order too


def test_a_load_runs_no_garbage_collection_and_leaves_the_snapshot_out_of_later_ones(
    hub, tmp_path, caplog, monkeypatch
):
    """No full collection of the cyclic garbage collector runs while a snapshot is made, and the snapshot is frozen
    out of the later ones: a large graph is millions of tracked objects, each full collection walked them all holding
    the GIL, the api's event loop stalled for seconds, and the workers' heartbeats and claims timed out on the pool."""
    import gc

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    pid = project_id(hub)
    collecting = []
    from_stored = Snapshot.from_stored

    def made(*args, **kwargs):
        collecting.append(gc.get_threshold()[2])
        return from_stored(*args, **kwargs)

    monkeypatch.setattr(kg_snapshots.Snapshot, "from_stored", made)
    thresholds = gc.get_threshold()
    gc.unfreeze()
    try:
        held = asyncio.run(held_on_engine(hub.db, pid))
        collected = [o for o in gc.get_objects() if o is held.snapshot or o is held.snapshot.text]
        assert gc.isenabled() and gc.get_threshold() == thresholds
    finally:
        gc.unfreeze()
    assert collecting == [kg_snapshots.ONLY_YOUNG]  # no full one ran while the snapshot was made
    assert collected == []  # and the later ones skip it
    assert "guide" in found(held.snapshot, "kiwi")


def test_the_garbage_made_during_a_load_is_collected_not_frozen():
    """The cyclic garbage the requests leave while a load runs is collected, by the young collections that go on or
    as the load ends, before what is left is frozen: frozen, it would never be, and the api would grow by what each
    load's requests left. A cycle that outlived a few young collections, in the old generation when it died, is
    collected too."""
    import gc
    import weakref

    class Node:
        pass

    gc.unfreeze()
    try:
        with kg_snapshots.uncollected():
            node = Node()
            node.me = node  # garbage only a collection frees
            gone = weakref.ref(node)
            del node
            old = Node()
            old.me = old
            aged = weakref.ref(old)
            churn = [[] for _ in range(50_000)]  # the build allocating meanwhile: young collections age the cycle
            del old, churn
        gc.collect()  # a frozen object is out of its reach
        assert gone() is None and aged() is None
        assert gc.isenabled()
    finally:
        gc.unfreeze()


def test_a_load_collects_and_freezes_as_it_goes_so_no_collection_walks_the_whole_snapshot(monkeypatch):
    """The thread making a snapshot collects everything and freezes what is left each time about SETTLE objects were
    made (``kg_snapshots.settle``), so no collection walks more than about that many: one at the end walked the whole
    snapshot, 9 seconds of a stalled event loop for the hub's largest graph. The cyclic garbage that died before one
    of them is collected, not frozen."""
    import gc
    import weakref

    from evo_agents.kg import pacing

    class Node:
        pass

    walked: list[int] = []
    collect = gc.collect

    def counted(*args):
        walked.append(len(gc.get_objects()))  # the objects a full collection walks: all but the frozen ones
        return collect(*args)

    monkeypatch.setattr(kg_snapshots.gc, "collect", counted)
    made = 6 * kg_snapshots.SETTLE

    def build():
        cycle = Node()
        cycle.me = cycle  # garbage only a collection frees, dead before the next one
        dead = weakref.ref(cycle)
        del cycle
        return dead, [[] for _ in pacing.paced(range(made))]  # the snapshot: objects the collector tracks

    async def load():
        with kg_snapshots.uncollected(), pacing.pacing(kg_snapshots.settle):
            return await asyncio.to_thread(build)

    collect()
    gc.freeze()
    try:
        dead, kept = asyncio.run(load())
        assert dead() is None
    finally:
        gc.unfreeze()
    assert len(kept) == made and len(walked) > 3  # collected as it went, and once more at the end
    assert max(walked) < 3 * kg_snapshots.SETTLE  # none walked the whole of it


def test_a_load_logs_how_long_each_phase_took_and_stalled_the_event_loop(hub, tmp_path, caplog, monkeypatch):
    """A load logs the seconds of each phase and the longest time the event loop ran nothing else meanwhile, and the
    collection before the freeze its own: on the hub, which phase holds up the heartbeats and claims is read there."""
    import gc
    import time

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    pid = project_id(hub)
    made = kg_snapshots._made

    async def blocking(*args):
        held = await made(*args)
        time.sleep(0.4)  # on the loop as the made phase ends, with no await before it does: the watch is not awake
        return held

    monkeypatch.setattr(kg_snapshots, "_made", blocking)
    caplog.clear()
    try:
        with caplog.at_level(logging.INFO, logger="evo_agents.hub.kg_snapshots"):
            asyncio.run(held_on_engine(hub.db, pid))
    finally:
        gc.unfreeze()
    timed = [r for r in caplog.records if r.getMessage() == "kg snapshot load timed"]
    frozen = [r for r in caplog.records if r.getMessage() == "kg snapshot frozen"]
    assert [r.project_id for r in timed] == [pid]
    phases = timed[0].phases
    assert list(phases) == ["slot", "rows", "made"]
    assert phases["made"]["s"] >= 0.4 and phases["made"]["stall_s"] >= 0.3
    assert phases["rows"]["stall_s"] < 0.3
    assert len(frozen) == 1 and frozen[0].collect_s >= 0


async def held_on_engine(db, pid: int):
    engine = make_engine(db)
    try:
        return await kg_snapshots.load(engine, pid)
    finally:
        await engine.dispose()


def test_a_plugin_read_of_a_project_not_in_memory_waits_for_no_load(hub, tmp_path, caplog, monkeypatch):
    """A core plugin reads the graph holding its own transaction (``kg_snapshots.held_for``): a project the api holds
    no snapshot of yet answers None at once and loads in the background, so plugin reads made meanwhile do not each
    hold a connection, idle, for the minutes the snapshot takes to make."""
    from psycopg_pool import AsyncConnectionPool
    from sqlalchemy import literal, select

    from evo_agents.hub.db import make_engine as pooled_engine

    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    first, _ = build(hub, caplog)
    pid = project_id(hub)
    release, making = threading.Event(), threading.Event()
    from_stored = Snapshot.from_stored

    def slow(*args, **kwargs):
        making.set()
        assert release.wait(30)
        return from_stored(*args, **kwargs)

    monkeypatch.setattr(kg_snapshots.Snapshot, "from_stored", slow)

    async def run():
        pool = AsyncConnectionPool(hub.db.dsn, min_size=0, max_size=3, timeout=2, close_returns=True, open=False)
        await pool.open()
        engine = pooled_engine(pool)
        coordinator = kg_snapshots.SnapshotCoordinator(engine)
        monkeypatch.setattr(kg_snapshots, "serving", coordinator)

        async def plugin_read():
            async with engine.begin() as conn:
                return await asyncio.wait_for(kg_snapshots.held_for(conn, pid, first["id"]), 1)

        try:
            cold = await asyncio.gather(*(plugin_read() for _ in range(3)))
            assert await asyncio.to_thread(making.wait, 30)
            async with engine.begin() as conn:  # a heartbeat, while the snapshot is made
                beat = await conn.scalar(select(literal(1)))
            release.set()
            await asyncio.gather(*coordinator._tasks)
            warm = await plugin_read()
        finally:
            release.set()
            await coordinator.close()
            await engine.dispose()
            await pool.close()
        return cold, beat, warm

    cold, beat, warm = asyncio.run(run())
    assert cold == [None, None, None] and beat == 1
    assert warm is not None and warm.build_id == first["id"]

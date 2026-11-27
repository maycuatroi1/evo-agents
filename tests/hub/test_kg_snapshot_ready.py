"""A tool call never waits the minutes a large graph's first load takes (``SnapshotCoordinator.ready``): past
FIRST_WAIT it is told the graph is loading and for how long, while the load goes on. And the api loads the snapshots of
the projects searched most once it opens (``SnapshotCoordinator.warm``), so the first call after a deploy finds them
held: on 2026-11-19 a kg_status right after a deploy took 226 s and the Curator's review gave up at 120 s."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("sqlalchemy")  # macOS and Windows install no hub-server extra

from sqlalchemy import insert

from evo_agents.hub import kg_snapshots, tables
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
if pg.DSN:
    from tests.hub.live import sql
    from tests.hub.test_kg import Machine, doc, hub, push  # noqa: F401 (hub is a fixture)
    from tests.hub.test_kg_postgres_build import build, make_engine, project_id


@needs_pg
def test_a_tool_call_waits_for_a_first_load_no_longer_than_first_wait(monkeypatch):
    gate = asyncio.Event()

    async def load(engine, project_id):
        await gate.wait()
        return kg_snapshots.Held(1, None)

    monkeypatch.setattr(kg_snapshots, "load", load)

    async def run():
        coordinator = kg_snapshots.SnapshotCoordinator(object())
        try:
            started = asyncio.get_running_loop().time()
            cold = await coordinator.ready(7, 1, wait=0.2)
            waited = asyncio.get_running_loop().time() - started
            ran, told = coordinator.loading(7), coordinator.unready(7)
            gate.set()
            await asyncio.gather(*coordinator._tasks)
            started = asyncio.get_running_loop().time()
            warm = await coordinator.ready(7, 1, wait=0.2)
            return cold, waited, ran, told, warm, asyncio.get_running_loop().time() - started, coordinator
        finally:
            await coordinator.close()

    cold, waited, ran, told, warm, again, coordinator = asyncio.run(run())
    assert cold is None and 0.15 <= waited < 1 and ran is not None and ran >= 0.15
    assert "loading this project's graph" in told and "call this tool again" in told
    assert warm.build_id == 1 and again < 0.1 and coordinator.loading(7) is None
    assert 7 in coordinator._took  # what the next load's message counts on
    coordinator._began[7], coordinator._loop = 0.0, asyncio.new_event_loop()
    try:
        assert "the last load took" in coordinator.unready(7)
    finally:
        coordinator._loop.close()


@needs_pg
def test_the_api_warms_the_projects_searched_most_that_have_a_graph(hub, tmp_path, caplog, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    first, _ = build(hub, caplog)
    pid = project_id(hub)
    row = {"project_id": pid, "kind": "session", "session_id": "s-1", "seq": 0, "query": "kiwi"}
    row |= {"results": [], "clicks": [], "grep_after": False, "after_grep": [], "label": {"level": "internal"}}
    sql(hub.db, insert(tables.kg_query_labels).values(row))

    async def run():
        engine = make_engine(hub.db)
        coordinator = kg_snapshots.SnapshotCoordinator(engine)
        try:
            await coordinator.warm(delay=0)
            return dict(coordinator._held)
        finally:
            await coordinator.close()
            await engine.dispose()

    held = asyncio.run(run())
    assert list(held) == [pid] and held[pid].build_id == first["id"]


@needs_pg
def test_the_api_warms_one_project_and_none_once_its_memory_is_past_warm_memory(hub, tmp_path, caplog, monkeypatch):
    """Two large snapshots, one held and one loading, took the api past its 7 GB limit on 2026-11-19 19:17, and it
    warmed both again after its restart: it warms one project, and none while its memory is past WARM_MEMORY."""
    assert kg_snapshots.WARM_PROJECTS == 1
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", doc("guide", "How the kiwi app works."))
    assert push(hub, laptop).ok
    build(hub, caplog)
    row = {"project_id": project_id(hub), "kind": "session", "session_id": "s-1", "seq": 0, "query": "kiwi"}
    row |= {"results": [], "clicks": [], "grep_after": False, "after_grep": [], "label": {"level": "internal"}}
    sql(hub.db, insert(tables.kg_query_labels).values(row))
    monkeypatch.setattr(kg_snapshots, "memory_limit", lambda: 100)
    monkeypatch.setattr(kg_snapshots, "resident", lambda: 31)

    async def run():
        engine = make_engine(hub.db)
        coordinator = kg_snapshots.SnapshotCoordinator(engine)
        try:
            await coordinator.warm(delay=0)
            return dict(coordinator._held)
        finally:
            await coordinator.close()
            await engine.dispose()

    assert asyncio.run(run()) == {}


def _claim(monkeypatch, *, held=(), over=False, resident=0, limit=None):
    """A coordinator whose load is a gate; returns it, the loads started and what its claim hook answered."""
    started, gate = [], asyncio.Event()

    async def load(engine, project_id):
        started.append(project_id)
        await gate.wait()
        return kg_snapshots.Held(1, None)

    monkeypatch.setattr(kg_snapshots, "load", load)
    monkeypatch.setattr(kg_snapshots, "_over", lambda: over)
    monkeypatch.setattr(kg_snapshots, "memory_limit", lambda: limit)
    monkeypatch.setattr(kg_snapshots, "resident", lambda: resident)

    async def run():
        coordinator = kg_snapshots.SnapshotCoordinator(object())
        for pid in held:
            coordinator._held[pid] = kg_snapshots.Held(1, None)
        try:
            task = coordinator.claimed(7)
            await asyncio.sleep(0.05)
            return task, list(started), coordinator.loading(7), dict(coordinator._held)
        finally:
            gate.set()
            await coordinator.close()

    return asyncio.run(run())


def test_a_claim_of_a_project_with_no_snapshot_starts_its_load_without_waiting(monkeypatch):
    task, started, loading, _ = _claim(monkeypatch)
    assert task is not None and started == [7] and loading is not None  # the load runs, gated, and the hook returned


def test_a_claim_under_memory_pressure_starts_no_load(monkeypatch):
    for kwargs in ({"over": True}, {"limit": 100, "resident": 31}):
        task, started, loading, held = _claim(monkeypatch, **kwargs)
        assert task is None and started == [] and loading is None and 7 not in held


def test_a_claim_of_a_project_already_held_changes_nothing(monkeypatch):
    task, started, loading, held = _claim(monkeypatch, held=(7,))
    assert task is None and started == [] and loading is None and list(held) == [7]

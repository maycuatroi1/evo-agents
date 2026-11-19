"""A tool call never waits the minutes a large graph's first load takes (``SnapshotCoordinator.ready``): past
FIRST_WAIT it is told the graph is loading and for how long, while the load goes on. And the api loads the snapshots of
the projects searched most once it opens (``SnapshotCoordinator.warm``), so the first call after a deploy finds them
held: on 2026-11-19 a kg_status right after a deploy took 226 s and the Curator's review gave up at 120 s."""

from __future__ import annotations

import asyncio

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert

from evo_agents.hub import kg_snapshots, tables
from tests.hub.live import sql
from tests.hub.test_kg import Machine, doc, hub, push  # noqa: F401 (hub is a fixture)
from tests.hub.test_kg_postgres_build import build, make_engine, project_id


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

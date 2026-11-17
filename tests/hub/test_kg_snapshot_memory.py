"""The api frees the snapshot a build replaced, whatever read it (``evo_agents.hub.kg_snapshots``). A load freezes the
objects of the requests running meanwhile with the snapshot it makes (``kg_snapshots.uncollected``): a reference cycle
among them is never collected, and one that holds a snapshot keeps the whole graph. A view of a snapshot that searched
was such a cycle (view, its ranker, the view): the hub's api grew by a graph at each build until the host ran out of
memory."""

from __future__ import annotations

import gc
import time

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_kg import Machine, doc, hub, latest, push, run_jobs, tool_ok  # noqa: F401 (hub is a fixture)
from tests.hub.test_kg_postgres_build import project_id

BUILDS = 3


def read_every_way(hub, query: str) -> None:
    """The graph reads of the kg tools an agent calls, each through the api's snapshot."""
    first = tool_ok(hub, "kg_search", {"query": query})["structuredContent"]["results"][0]["id"]
    tool_ok(hub, "kg_context", {"query": query, "budget_tokens": 4000})
    tool_ok(hub, "kg_context", {"ids": [first]})
    tool_ok(hub, "kg_node", {"id": first})
    tool_ok(hub, "kg_path", {"from": first})
    tool_ok(hub, "kg_impact", {"ids": [first]})
    tool_ok(hub, "kg_status")


def loaded(hub, coordinator, pid: int, build_id: int) -> None:
    for _ in range(100):  # a read that found the new build starts its load and waits a while for it
        tool_ok(hub, "kg_status")
        if coordinator._held[pid].build_id == build_id:
            return
        time.sleep(0.1)
    raise AssertionError(f"build {build_id} was not loaded")


def test_the_snapshots_builds_replaced_are_freed_though_their_readers_were_frozen(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    docs = [doc("guide", "How the kiwi app works.")]
    laptop.sync("docs", *docs)
    assert push(hub, laptop).ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    pid = project_id(hub)
    coordinator = hub.client.app.state.kg_snapshots
    read_every_way(hub, "guide")
    gc.collect()
    gc.disable()  # what a load froze no collection reaches: only reference counts free it
    try:
        for i in range(BUILDS):
            read_every_way(hub, "guide")
            gc.freeze()  # a load running while they read froze what they left
            docs.append(doc(f"release{i}", f"The release number {i}."))
            laptop.sync("docs", *docs)
            assert push(hub, laptop).ok
            run_jobs(hub.db, hub.s3, hub.worker_dir)
            loaded(hub, coordinator, pid, latest(hub)["id"])
            assert coordinator.lingering() == 0, f"build {i + 1} left the snapshot it replaced in memory"
    finally:
        gc.enable()
        gc.unfreeze()

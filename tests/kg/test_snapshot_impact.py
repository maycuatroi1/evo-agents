"""kg_impact on an in-RAM snapshot (``evo_agents.kg.snapshot_impact``): the dependents, documents and cut seeds a
session's kg_impact finds, for each clearance, and the hub limit and seed cut of the walk."""

from evo_agents.kg import snapshot_impact
from evo_agents.kg.serve import Session
from evo_agents.kg.snapshot import Snapshot
from evo_agents.kg.snapshot_impact import impact, impact_data
from evo_agents.kg.snapshot_search import SnapshotSearch
from evo_agents.kg.store import Store
from tests.kg.test_store import make_searchable, sync_build

SEARCH = "symbol:app:app/search.py::search"
SEEDS = [SEARCH, "app:file:app/search.py", "usecase:KB-01"]


def built(tmp_path, kg_env, **kw):
    project = make_searchable(tmp_path, kg_env, **kw)
    sync_build(project)
    store = Store.for_project(project)
    return project, Snapshot.from_store(store, store.latest_ready())


def shape(data: dict) -> dict:
    """What two impact results must agree on, whatever the label's rendering."""
    return {
        "seeds": [n["id"] for n in data["seeds"]],
        "impacted": [
            (r["hop"], r["node"]["id"], r["rel"], r["rels"], r["status"], r["to"], r["hub"]) for r in data["impacted"]
        ],
        "documents": [(d["node"]["id"], d["mentions"]) for d in data["documents"]],
        "cut": data["cut"],
    }


def test_impact_finds_what_a_session_finds_for_each_clearance(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env, app_level="customer")
    for sink in ("agent", "wide"):
        session = Session(project, sink)
        reader = SnapshotSearch(snap, session.clearance, session.audience)
        for seed in SEEDS:
            theirs = session.call("kg_impact", {"id": seed, "depth": 3})
            ours = impact_data(impact(reader, [seed], depth=3))
            if theirs.get("isError"):  # nothing the reader sees: the code is customer, the agent sink internal
                assert sink == "agent" and not ours["seeds"] and ours["not_found"] == 1
                continue
            assert shape(ours) == shape(theirs["structuredContent"]), (sink, seed)


def test_the_callers_of_a_function_depend_on_it(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env)
    session = Session(project, "agent")
    found = impact(SnapshotSearch(snap, session.clearance, session.audience), [SEARCH], depth=2)
    assert found.reached["symbol:app:app/server.py::Server.handle"]["edge"]["rel"] == "calls"
    assert found.reached["app:file:app/server.py"]["hop"] == 2
    assert "app:file:docs/guide.md#guide" in found.docs


def test_a_seed_by_path_and_one_unmatched(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env)
    session = Session(project, "agent")
    found = impact(
        SnapshotSearch(snap, session.clearance, session.audience), paths=["app:app/search.py", "app:nope.py"]
    )
    assert list(found.seeds) == ["app:file:app/search.py"] and found.unmatched == ["app:nope.py"]


def test_a_hub_is_marked_and_not_expanded_and_a_seed_with_too_many_edges_is_cut(tmp_path, kg_env, monkeypatch):
    project, snap = built(tmp_path, kg_env)
    session = Session(project, "agent")
    reader = SnapshotSearch(snap, session.clearance, session.audience)
    monkeypatch.setattr(snapshot_impact, "HUB_DEGREE", 1)
    found = impact(reader, [SEARCH], depth=3)
    assert found.reached and all(r["hub"] for r in found.reached.values() if r["hop"] == 1)
    assert all(r["hop"] == 1 for r in found.reached.values())
    monkeypatch.setattr(snapshot_impact, "IMPACT_SEED_EDGES", 1)
    found = impact(reader, ["usecase:KB-01"], depth=1)
    assert found.cut and found.cut[0]["seed"] == "usecase:KB-01" and found.cut[0]["read"] == 1

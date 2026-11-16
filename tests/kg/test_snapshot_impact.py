"""kg_impact on an in-RAM snapshot (``evo_agents.kg.snapshot_impact``): the dependents, documents and cut seeds a
session's kg_impact found on a graph file of the same build, for each clearance (kept here as literal expected
values), and the hub limit and seed cut of the walk."""

from evo_agents.kg import snapshot_impact
from evo_agents.kg.snapshot_impact import impact, impact_data
from evo_agents.kg.snapshot_search import SnapshotSearch
from tests.kg.built import snapshot_of, sync_build
from tests.kg.hub_graph import hub_session
from tests.kg.test_store import make_searchable

SEARCH = "symbol:app:app/search.py::search"
SEEDS = [SEARCH, "app:file:app/search.py", "usecase:KB-01"]

# The shape of what kg_impact of a local session gave on a graph file at depth 3, by (sink, seed), with the app's code customer;
# None where it answered an error (nothing the reader sees).
IMPACT = {
    ("agent", "symbol:app:app/search.py::search"): None,
    ("agent", "app:file:app/search.py"): {"seeds": [], "impacted": [], "documents": [], "cut": []},
    ("agent", "usecase:KB-01"): {
        "seeds": ["usecase:KB-01"],
        "impacted": [],
        "documents": [("plan:demo", [{"node": "usecase:KB-01", "status": "parsed"}])],
        "cut": [],
    },
    ("wide", "symbol:app:app/search.py::search"): {
        "seeds": ["symbol:app:app/search.py::search"],
        "impacted": [
            (1, "app:file:app/search.py", "defines", ["defines"], "parsed", "symbol:app:app/search.py::search", False),
            (
                1,
                "symbol:app:app/server.py::Server.handle",
                "calls",
                ["calls"],
                "resolved",
                "symbol:app:app/search.py::search",
                False,
            ),
            (2, "app:file:app/server.py", "imports", ["imports"], "parsed", "app:file:app/search.py", False),
        ],
        "documents": [
            ("app:file:docs/guide.md#guide", [{"node": "symbol:app:app/search.py::search", "status": "parsed"}]),
            ("app:file:docs/guide.md#setup", [{"node": "app:file:app/server.py", "status": "resolved"}]),
            ("app:file:docs/old.md#old", [{"node": "app:file:app/server.py", "status": "resolved"}]),
            ("plan:demo/step:1", [{"node": "app:file:app/search.py", "status": "resolved"}]),
            ("plan:demo/step:2", [{"node": "app:file:app/server.py", "status": "resolved"}]),
        ],
        "cut": [],
    },
    ("wide", "app:file:app/search.py"): {
        "seeds": ["app:file:app/search.py"],
        "impacted": [(1, "app:file:app/server.py", "imports", ["imports"], "parsed", "app:file:app/search.py", False)],
        "documents": [
            ("app:file:docs/guide.md#setup", [{"node": "app:file:app/server.py", "status": "resolved"}]),
            ("app:file:docs/old.md#old", [{"node": "app:file:app/server.py", "status": "resolved"}]),
            ("plan:demo/step:1", [{"node": "app:file:app/search.py", "status": "resolved"}]),
            ("plan:demo/step:2", [{"node": "app:file:app/server.py", "status": "resolved"}]),
        ],
        "cut": [],
    },
    ("wide", "usecase:KB-01"): {
        "seeds": ["usecase:KB-01"],
        "impacted": [
            (1, "symbol:app:app/search.py::search", "implements", ["implements"], "declared", "usecase:KB-01", False),
            (2, "app:file:app/search.py", "defines", ["defines"], "parsed", "symbol:app:app/search.py::search", False),
            (
                2,
                "symbol:app:app/server.py::Server.handle",
                "calls",
                ["calls"],
                "resolved",
                "symbol:app:app/search.py::search",
                False,
            ),
            (3, "app:file:app/server.py", "imports", ["imports"], "parsed", "app:file:app/search.py", False),
        ],
        "documents": [
            (
                "app:file:docs/guide.md#guide",
                [
                    {"node": "symbol:app:app/search.py::search", "status": "parsed"},
                    {"node": "usecase:KB-01", "status": "parsed"},
                ],
            ),
            ("app:file:docs/guide.md#setup", [{"node": "app:file:app/server.py", "status": "resolved"}]),
            ("app:file:docs/old.md#old", [{"node": "app:file:app/server.py", "status": "resolved"}]),
            ("plan:demo", [{"node": "usecase:KB-01", "status": "parsed"}]),
            ("plan:demo/step:1", [{"node": "app:file:app/search.py", "status": "resolved"}]),
            ("plan:demo/step:2", [{"node": "app:file:app/server.py", "status": "resolved"}]),
        ],
        "cut": [],
    },
}


def built(tmp_path, kg_env, **kw):
    project = make_searchable(tmp_path, kg_env, **kw)
    sync_build(project)
    return project, snapshot_of(project)


def reader(project, snap, sink: str = "agent") -> SnapshotSearch:
    """``snap`` as the hub's engine reads it for ``sink``: its clearance and audience."""
    hub = hub_session(project, sink)
    return SnapshotSearch(snap, hub.clearance, hub.audience)


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
        search = reader(project, snap, sink)
        for seed in SEEDS:
            ours = impact_data(impact(search, [seed], depth=3))
            expected = IMPACT[(sink, seed)]
            if expected is None:  # nothing the reader sees: the code is customer, the agent sink internal
                assert sink == "agent" and not ours["seeds"] and ours["not_found"] == 1
                continue
            assert shape(ours) == expected, (sink, seed)


def test_the_callers_of_a_function_depend_on_it(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env)
    found = impact(reader(project, snap), [SEARCH], depth=2)
    assert found.reached["symbol:app:app/server.py::Server.handle"]["edge"]["rel"] == "calls"
    assert found.reached["app:file:app/server.py"]["hop"] == 2
    assert "app:file:docs/guide.md#guide" in found.docs


def test_a_seed_by_path_and_one_unmatched(tmp_path, kg_env):
    project, snap = built(tmp_path, kg_env)
    found = impact(reader(project, snap), paths=["app:app/search.py", "app:nope.py"])
    assert list(found.seeds) == ["app:file:app/search.py"] and found.unmatched == ["app:nope.py"]


def test_a_hub_is_marked_and_not_expanded_and_a_seed_with_too_many_edges_is_cut(tmp_path, kg_env, monkeypatch):
    project, snap = built(tmp_path, kg_env)
    search = reader(project, snap)
    monkeypatch.setattr(snapshot_impact, "HUB_DEGREE", 1)
    found = impact(search, [SEARCH], depth=3)
    assert found.reached and all(r["hub"] for r in found.reached.values() if r["hop"] == 1)
    assert all(r["hop"] == 1 for r in found.reached.values())
    monkeypatch.setattr(snapshot_impact, "IMPACT_SEED_EDGES", 1)
    found = impact(search, ["usecase:KB-01"], depth=1)
    assert found.cut and found.cut[0]["seed"] == "usecase:KB-01" and found.cut[0]["read"] == 1

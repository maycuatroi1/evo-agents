"""The walk of kg_search and kg_context (``evo_agents.kg.ppr``): off unless the KG settings set ``walk``, HippoRAG 2's
seeds and damping, edges weighed by relation, status and confidence, never through a node or an edge the reader does
not see, and a key every settings surface takes and every bench shard tells apart."""

import json
import sys

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.kg import ppr, strategy
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.bench import run as bench_run
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

ALPHA = "app:file:docs/alpha/launch.md"
BETA = "app:file:docs/beta/tail.md"
ZEPHYR = "entity:Concept:zephyr"
QUASAR = "entity:Concept:quasar"

# A local model: the launch notes name Zephyr and Quasar (Zephyr depends on Quasar), the tail notes name Quasar only.
MODEL = """import json, sys
req = json.load(sys.stdin)
nodes, edges = [], []
if "Zephyr" in req["text"]:
    nodes += [{"kind": "Concept", "name": "Zephyr", "conf": 0.9}, {"kind": "Concept", "name": "Quasar", "conf": 0.9}]
    edges.append({"src": "Zephyr", "rel": "depends_on", "dst": "Quasar"})
elif "glowing" in req["text"]:
    nodes.append({"kind": "Concept", "name": "Quasar", "conf": 0.9})
print(json.dumps({"nodes": nodes, "edges": edges, "usage": {"input_tokens": 1, "output_tokens": 1, "cost": 0}}))
"""


def entity_project(tmp_path, kg_env):
    """The demo project with two notes the entities step reads: only the entity Quasar joins them."""
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/alpha/launch.md", "# Launch\n\nThe Zephyr launcher starts the engine at dawn.\n")
    commit(app, "launch notes")  # two commits: no commit joins the two notes
    write(app / "docs/beta/tail.md", "# Tail\n\nA glowing tail follows the comet across the sky.\n")
    commit(app, "tail notes")
    script = tmp_path / "model.py"
    script.write_text(MODEL)
    mapping = {
        "model": "fake-1",
        "command": [sys.executable, str(script)],
        "node_kinds": ["Concept"],
        "relations": ["depends_on"],
        "items": ["markdown"],
    }
    data = {
        "version": 1,
        "name": "ents",
        "knowledge": {"ontology": {"node_kinds": ["Concept"]}},
        "settings": {"entities": mapping},
        "steps": ["entities"],
    }
    chosen = project.harness.root / "strategies" / "ents.yaml"
    write(chosen, yaml.safe_dump(data))
    strategy.apply(strategy.load_file(chosen), project)
    project = load_project_at(project.harness.root, kg_env)
    assert all(r.ok for r in sync_project(project))
    report = build_project(project)
    assert report.ok, report.errors
    return project


def walk_on(project, **keys):
    tuning = kg_settings.read(project.root)
    kg_settings.write(project.root, kg_settings.of({"walk": keys or True}, tuning))


def search_ids(project, query, sink="agent"):
    """The documents of kg_search's results: a section counts as its document."""
    found = Session(project, sink).call("kg_search", {"query": query})
    assert not found.get("isError"), found
    return list(dict.fromkeys(r["id"].split("#", 1)[0] for r in found["structuredContent"]["results"]))


def hide(project, sql: str, *args, top=(2, 0), audience=None):
    """Change labels straight in the latest build, and its highest label, as a build of such sources would."""
    store = Store.for_project(project)
    b = store.latest_ready()
    with store.db:
        store.db.execute(sql, args)
        store.db.execute(
            "UPDATE builds SET stats = json_set(stats, '$.top_label', json(?)) WHERE build_id = ?",
            (json.dumps(list(top)), b),
        )
        if audience:
            store.db.execute(
                "UPDATE builds SET stats = json_set(stats, '$.top_audience', json(?)) WHERE build_id = ?",
                (json.dumps(audience), b),
            )
    store.close()


# -- the setting ----------------------------------------------------------------------------------------------------


def test_walk_is_off_by_default_and_a_query_key_every_surface_takes():
    assert kg_settings.DEFAULT.walk is None and ppr.of(kg_settings.DEFAULT.walk) is None
    on = kg_settings.of({"walk": True})
    assert on.walk == kg_settings.of({"walk": "on"}).walk == kg_settings.of({"walk": {}}).walk
    assert ppr.of(on.walk) == ppr.Walk() and ppr.Walk().damping == 0.5 and ppr.Walk().passage_seed == 0.05
    assert list(on.walk) == sorted(on.walk)  # every key, sorted: two settings that walk alike are equal
    assert kg_settings.of({"walk": None}).walk is None and kg_settings.of({"walk": "off"}).walk is None
    assert kg_settings.rebuild_keys(kg_settings.DEFAULT, on) == []  # a query key: measured on the newest build
    assert "walk" not in kg_settings.BUILD_KEYS
    held = kg_settings.of(json.loads(json.dumps(on.overrides())))  # as the hub's kg_settings row holds it
    assert held == on
    tuned = kg_settings.of({"walk": {"damping": 0.7, "hops": 3}})
    assert ppr.of(tuned.walk).damping == 0.7 and ppr.of(tuned.walk).hops == 3 and tuned.walk != on.walk


@pytest.mark.parametrize(
    "value", [{"nope": 1}, {"damping": 2}, {"hops": 1.5}, {"context": "yes"}, {"damping": "high"}, 3, "maybe"]
)
def test_a_walk_value_out_of_range_is_refused_with_the_key_named(value):
    with pytest.raises(kg_settings.SettingsError, match="walk"):
        kg_settings.of({"walk": value})


def test_kg_settings_sets_the_walk_and_undoes_it(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    argv = ["kg", "settings", "--project", str(project.harness.root), "--json"]
    assert main([*argv, "--set", 'walk={"damping": 0.6}', "--reason", "trial"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settings"]["walk"]["damping"] == 0.6 and out["needs_rebuild"] == []
    assert main([*argv, "--set", "walk=on"]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["walk"]["damping"] == 0.5
    assert main([*argv, "--set", 'walk={"hops": 9}']) == 2
    assert "walk.hops" in capsys.readouterr().err
    assert main([*argv, "--set", "walk=null"]) == 0
    assert json.loads(capsys.readouterr().out)["settings"]["walk"] is None


def test_bench_settings_take_the_walk_and_two_walks_never_share_a_shard(tmp_path, monkeypatch):
    from evo_agents.kg import cli_bench

    one, two = tmp_path / "one.json", tmp_path / "two.json"
    one.write_text(json.dumps({"settings": {"walk": {"damping": 0.5}}}))
    two.write_text(json.dumps({"walk": {"damping": 0.6}}))
    first, second = cli_bench._tuning_file(str(one)), cli_bench._tuning_file(str(two))
    assert first["walk"]["damping"] == 0.5 and second["walk"]["damping"] == 0.6
    asked = []

    def ask(cache, bench, tasks, options):
        asked.append(options.tuning)
        return bench_run.Asked({}, {})

    monkeypatch.setattr(bench_run, "ask_repo", ask)
    for tuning in (first, second, first, None):
        bench_run.ask_repo_kept(tmp_path / "cache", "b", [], bench_run.Options(tuning=tuning))
    assert asked == [first, second, None]  # the third is the first's shard


def test_the_ranking_digest_reads_the_walk_module(tmp_path, monkeypatch):
    before = bench_run.ranking_digest()
    changed = tmp_path / "ppr.py"
    changed.write_text("# another walk\n")
    monkeypatch.setattr(ppr, "__file__", str(changed))
    assert bench_run.ranking_digest() != before


# -- the walk itself ------------------------------------------------------------------------------------------------


def test_the_walk_restarts_at_its_seeds_by_the_damping_and_spreads_both_ways():
    adj = {"a": {"b": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"b": 1.0}}
    found = ppr.personalized(adj, {"a": 1.0}, ppr.Walk(damping=0.5, steps=60))
    assert found["a"] > found["b"] > found["c"] > 0
    assert sum(found.values()) == pytest.approx(1.0)
    restless = ppr.personalized(adj, {"a": 1.0}, ppr.Walk(damping=0.9, steps=200))
    assert restless["c"] > found["c"]  # less restart, farther reach


def test_an_edge_weighs_its_relation_status_and_confidence():
    walk = ppr.Walk()
    assert ppr.weight({"rel": "mentions", "status": "parsed", "conf": None}, walk) == 1.0
    assert ppr.weight({"rel": "calls", "status": "resolved", "conf": 0.8}, walk) == pytest.approx(0.6 * 0.8)
    assert ppr.weight({"rel": "calls", "status": "resolved", "conf": 0.3}, walk) == 0.0  # under the floor
    assert ppr.weight({"rel": "mentions", "status": "proposed", "conf": 1.0}, walk) == 0.0
    assert ppr.weight({"rel": "in_source", "status": "parsed", "conf": None}, walk) == 0.0
    assert ppr.weight({"rel": "orbits", "status": "extracted", "conf": 0.9}, walk) == pytest.approx(0.9)


class FakeReader:
    def __init__(self, mentions):
        self.mentions = mentions

    def visible(self, ids):
        return set(ids)


def test_an_entity_seed_is_divided_by_its_documents_and_a_passage_seeds_at_passage_seed(monkeypatch):
    monkeypatch.setattr(ppr, "_documents_of", lambda reader, entity: reader.mentions[entity])
    reader = FakeReader({"entity:C:one": 1, "entity:C:many": 4})
    base = {"entity:C:one": 1.0, "entity:C:many": 1.0, "doc:a": 0.5}
    seeds = ppr.seed(reader, base, ppr.Walk())
    assert seeds == {"entity:C:one": 1.0, "entity:C:many": 0.25, "doc:a": pytest.approx(0.05 * 0.5)}


def test_without_an_entity_among_the_matches_the_walk_seeds_at_the_matches(monkeypatch):
    monkeypatch.setattr(ppr, "subgraph", lambda reader, seeds, walk: {})
    seeds = ppr.seed(FakeReader({}), {"doc:a": 1.0, "doc:b": 0.5}, ppr.Walk())
    assert seeds == {"doc:a": pytest.approx(1.0), "doc:b": pytest.approx(0.125)}  # each by the cube of its match


# -- kg_search and kg_context ---------------------------------------------------------------------------------------


def test_the_walk_finds_a_note_through_the_entity_two_notes_share(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    plain = search_ids(project, "zephyr launcher")
    assert ALPHA in plain and BETA not in plain
    walk_on(project)
    walked = search_ids(project, "zephyr launcher")
    assert ALPHA in walked and BETA in walked
    assert walked.index(ALPHA) < walked.index(BETA)


def test_a_project_without_walk_ranks_as_before(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    queries = ("zephyr launcher", "search", "glowing tail")

    def answers():
        session = Session(project, "agent")
        return [session.call("kg_search", {"query": q}) for q in queries], session.call(
            "kg_context", {"query": "zephyr"}
        )

    before = answers()
    walk_on(project)
    assert answers() != before
    kg_settings.write(project.root, kg_settings.of({"walk": None}, kg_settings.read(project.root)))
    assert answers() == before  # off again: the same results, in the same format


def test_kg_context_fills_its_budget_by_the_walk_in_the_same_format(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    plain = Session(project, "agent").call("kg_context", {"query": "zephyr launcher"})["structuredContent"]
    walk_on(project)
    walked = Session(project, "agent").call("kg_context", {"query": "zephyr launcher"})["structuredContent"]
    assert set(walked) == set(plain)
    assert {"build", "seeds", "nodes", "edges", "left_out", "summary"} <= set(walked)
    nodes = {n["id"] for n in walked["nodes"]}
    assert {BETA, QUASAR} <= {n.split("#", 1)[0] for n in nodes}
    shown = {e["src"] for e in walked["edges"]} | {e["dst"] for e in walked["edges"]}
    assert shown <= nodes


def test_the_walk_never_passes_a_hidden_node(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    walk_on(project)
    hide(project, "UPDATE nodes SET label_lvl = 2 WHERE node_id = ?", QUASAR)
    assert BETA in search_ids(project, "zephyr launcher", "wide")
    for found in (
        Session(project, "agent").call("kg_search", {"query": "zephyr launcher"}),
        Session(project, "agent").call("kg_context", {"query": "zephyr launcher"}),
    ):
        text = json.dumps(found)
        assert "tail.md" not in text and "quasar" not in text.lower()


def test_the_walk_never_passes_a_hidden_edge(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    walk_on(project)
    hide(project, "UPDATE edges SET label_lvl = 2 WHERE dst = ? AND rel = 'mentions' AND src = ?", QUASAR, BETA)
    assert BETA in search_ids(project, "zephyr launcher", "wide")
    assert BETA not in search_ids(project, "zephyr launcher", "agent")
    context = Session(project, "agent").call("kg_context", {"query": "zephyr launcher"})
    assert "tail.md" not in json.dumps(context)


def test_the_walk_never_passes_a_node_hidden_by_its_audience(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    walk_on(project)
    hide(
        project,
        "UPDATE nodes SET label_aud = ? WHERE node_id = ?",
        json.dumps(["group:lumen-core"]),
        QUASAR,
        top=(1, 0),
        audience=["group:lumen-core"],
    )
    assert BETA not in search_ids(project, "zephyr launcher", "agent")
    assert "tail.md" not in json.dumps(Session(project, "agent").call("kg_context", {"query": "zephyr launcher"}))


def test_a_graph_without_entities_walks_from_its_text_matches(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project)) and build_project(project).ok
    plain = search_ids(project, "rank_results")
    walk_on(project)
    walked = search_ids(project, "rank_results")
    assert walked and walked[0] == plain[0] == "symbol:app:app/search.py::rank_results"
    context = Session(project, "agent").call("kg_context", {"query": "rank_results"})
    assert not context.get("isError") and context["structuredContent"]["nodes"]


# -- the ranking ----------------------------------------------------------------------------------------------------

# Three text matches a, b, c; a hub h joined to each of them and to four nodes the question never names; a node x
# joined to a alone.
HUB_ADJ = {
    "doc:a": {"doc:h": 1.0, "doc:x": 1.0},
    "doc:b": {"doc:h": 1.0},
    "doc:c": {"doc:h": 1.0},
    "doc:h": {"doc:a": 1.0, "doc:b": 1.0, "doc:c": 1.0, **{f"doc:o{n}": 1.0 for n in range(4)}},
    "doc:x": {"doc:a": 1.0},
    **{f"doc:o{n}": {"doc:h": 1.0} for n in range(4)},
}
HUB_SCORED = [("doc:a", 3.0), ("doc:b", 2.0), ("doc:c", 1.0)]


def hub_order(monkeypatch, walk, seeds=(1.0, 1.0, 1.0)):
    seeds = dict(zip(("doc:a", "doc:b", "doc:c"), seeds, strict=True))
    found = ppr.personalized(HUB_ADJ, seeds, walk)
    monkeypatch.setattr(ppr, "walked", lambda store, b, base, walk, seen: (found, HUB_ADJ))
    return found, ppr.order(None, 1, HUB_SCORED, walk, lambda e: True)


def test_a_hub_joined_to_every_seed_ranks_below_the_best_text_matches(monkeypatch):
    found, ids = hub_order(monkeypatch, ppr.Walk())
    assert found["doc:h"] > max(found[i] for i, _ in HUB_SCORED)  # the hub gathers the most mass
    assert ids[:3] == ["doc:a", "doc:b", "doc:c"]  # the best text matches, by text score, then the hub


def test_a_reached_node_ranks_by_its_walk_score_per_edge(monkeypatch):
    found, ids = hub_order(monkeypatch, ppr.Walk(top_matches=1), seeds=(1.0, 0.1, 0.1))
    assert found["doc:h"] > found["doc:x"]  # the hub gathers more mass than x, joined to a alone
    assert ids.index("doc:x") < ids.index("doc:h")  # divided by log2(2 + edges): 7 edges against 1


def test_a_walk_with_top_matches_0_orders_as_before(monkeypatch):
    found, ids = hub_order(monkeypatch, ppr.Walk(top_matches=0))
    assert ids == sorted(found, key=lambda i: (-found[i], i))  # the walk's own score alone
    assert ids[0] == "doc:h"


def test_top_matches_is_a_walk_key_of_0_to_20():
    assert kg_settings.of({"walk": True}).walk["top_matches"] == 3
    assert kg_settings.of({"walk": {"top_matches": 0}}).walk["top_matches"] == 0
    for bad in (21, -1, 1.5):
        with pytest.raises(kg_settings.SettingsError, match="walk.top_matches"):
            kg_settings.of({"walk": {"top_matches": bad}})

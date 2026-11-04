"""The entities of a build folded into one node each, joined to the one Symbol or File they name and, when the graph has
vectors, to their synonyms (evo_agents.kg.entity_links)."""

import json
import random
import sys
import zlib

import numpy as np
import pytest
import yaml

from evo_agents.kg import entity_links as links
from evo_agents.kg import semantic, strategy
from evo_agents.kg.build import build_project, verify_build
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

# The answer of the fake model to each markdown item, by a word only that item holds.
MODEL = """import json, sys
req = json.load(sys.stdin)
answers = json.load(open(sys.argv[1]))
for marker, answer in answers.items():
    if marker in req["text"]:
        print(json.dumps(answer))
        break
else:
    print(json.dumps({"nodes": []}))
"""

FAKE = "fake/entity-words"
DIM = 64
SAME = {"automobile": "car", "motorcar": "car"}


class Words(semantic.Embedder):
    """A vector of the words of a text, each word on one of DIM axes, the words of SAME on the same one."""

    name, dim = FAKE, DIM

    def __init__(self):
        self.texts = 0

    def embed(self, texts):
        self.texts += len(texts)
        out = []
        for text in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for word in text.lower().split():
                v[zlib.crc32(SAME.get(word, word).encode()) % DIM] += 1.0
            out.append(v / (np.linalg.norm(v) or 1.0))
        return out


@pytest.fixture
def words(monkeypatch):
    model = Words()
    semantic.forget_models()
    monkeypatch.setitem(semantic.FACTORIES, FAKE, lambda: model)
    yield model
    semantic.forget_models()


def node_of(kind, name, **kw):
    return {"kind": kind, "name": name, "evidence": "from the text", "conf": 0.8, **kw}


def setup(tmp_path, kg_env, answers, *, extra=None, level="internal", app_level="internal", wider=False):
    project = make_project(tmp_path, kg_env, level=level, app_level=app_level)
    if wider:  # a second search in another file: the name search names two symbols
        app = tmp_path / "demo-app"
        write(app / "lib/search.py", "def search(query):\n    return query\n")
        commit(app, "lib")
    script, answers_file = tmp_path / "model.py", tmp_path / "answers.json"
    script.write_text(MODEL)
    answers_file.write_text(json.dumps(answers))
    mapping = {
        "model": "fake-1",
        "command": [sys.executable, str(script), str(answers_file)],
        "node_kinds": ["Concept"],
        "relations": ["depends_on"],
        "items": ["markdown"],
        **(extra or {}),
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
    return project


def graph(project):
    """(entity nodes: id -> (name, status), edges touching an entity: (src, rel, dst) -> (status, conf, props))."""
    store = Store.open_file(project.root / "graph.sqlite")
    try:
        b = store.latest_ready()
        live = "tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)"
        nodes = {
            r[0]: (r[1], r[2])
            for r in store.db.execute(
                f"SELECT node_id, name, status FROM nodes WHERE node_id LIKE 'entity:%' AND {live}", (b, b)
            )
        }
        edges = {
            (r[0], r[1], r[2]): (r[3], r[4], json.loads(r[5]))
            for r in store.db.execute(
                "SELECT src, rel, dst, status, conf, props FROM edges"
                f" WHERE (src LIKE 'entity:%' OR dst LIKE 'entity:%') AND {live}",
                (b, b),
            )
        }
        return nodes, edges
    finally:
        store.close()


# -- folding ------------------------------------------------------------------------------------------------------


def test_names_that_differ_in_case_accents_spaces_or_number_read_as_one_key():
    assert links.key("Search  Server") == links.key("search-server") == links.key("SEARCH_server") == "search-server"
    assert links.key("Đường ống") == "duong-ong" and links.key("Café") == "cafe"
    assert links.singulars("search-servers") == ["search-server"] and links.singulars("boxes") == ["box", "boxe"]
    assert links.singulars("entries") == ["entry", "entri", "entrie"]
    assert links.singulars("class") == [] and links.singulars("status") == [] and links.singulars("bus") == []


def stated(kind, name, aliases=(), item="i", rank=(1, 0, 0), conf=0.8):
    return links.Stated(kind, name, tuple(aliases), conf, item, rank)


def test_case_accents_plurals_and_aliases_fold_into_one_node_and_kinds_stay_apart():
    got = links.group(
        [
            stated("Concept", "Search Server", item="a"),
            stated("Concept", "search servers", item="b"),
            stated("Concept", "Sérch", ["Search Server"], item="c"),
            stated("Tool", "Search Server", item="d"),
            stated("Concept", "Ranking", item="e"),
        ]
    )
    concept = got.id_of("Concept", "SEARCH SERVER")
    assert concept == "entity:Concept:search-server"
    assert got.id_of("Concept", "search servers") == got.id_of("Concept", "Sérch") == concept
    assert got.id_of("Tool", "Search Server") == "entity:Tool:search-server" != concept
    assert got.id_of("Concept", "Ranking") == "entity:Concept:ranking"
    assert got.names[concept] == ["Search Server", "Sérch", "search servers"]
    assert got.merged == 2, "three names of one concept: two folded into the first"


def test_the_folding_does_not_depend_on_the_order_of_the_answers():
    many = [
        stated("Concept", n, a, item=str(i))
        for i, (n, a) in enumerate(
            [("CI", ["Continuous Integration"]), ("continuous integration", []), ("Pipelines", []), ("pipeline", [])]
        )
    ]
    first = links.group(many)
    for seed in range(5):
        shuffled = many[:]
        random.Random(seed).shuffle(shuffled)
        again = links.group(shuffled)
        assert (again.of, again.name, again.names, again.merged) == (first.of, first.name, first.names, first.merged)
    assert len(first.name) == 2


def test_a_node_takes_the_name_of_the_most_public_item_that_states_it():
    secret, public = (2, 0, 0), (0, 0, 0)
    got = links.group(
        [stated("Concept", "Project Falcon", ["Search"], rank=secret), stated("Concept", "Search", rank=public)]
    )
    nid = got.id_of("Concept", "Project Falcon")
    assert nid == "entity:Concept:search" and got.name[nid] == "Search"


def test_a_build_folds_the_names_and_coverage_counts_them(tmp_path, kg_env):
    answers = {
        "Intro": {"nodes": [node_of("Concept", "Search Servers"), node_of("Concept", "Ranking")]},
        "Obsolete": {"nodes": [node_of("Concept", "search server", aliases=["Finder"])]},
        "Repos: app": {"nodes": [node_of("Concept", "Finder")], "edges": []},
    }
    project = setup(tmp_path, kg_env, answers)
    report = build_project(project)
    assert report.ok, report.errors
    nodes, edges = graph(project)
    one = "entity:Concept:finder"  # of the names stated once each, the first by order
    assert set(nodes) == {one, "entity:Concept:ranking"} and nodes[one][0] == "Finder"
    assert nodes[one][1] == "extracted"
    for doc in ("app:file:docs/guide.md", "app:file:docs/old.md", "harness:file:CLUSTER.md"):
        assert (doc, "mentions", one) in edges
    cov = report.coverage["entities"]
    assert cov["merged"] == 2 and cov["distinct"] == 2
    assert cov["synonyms"] == 0 and cov["synonym_status"].startswith("off")
    first = report.content_hash
    again = build_project(project)
    assert again.content_hash == first or again.skipped
    for cold in (False, True):
        assert verify_build(project, first, cold=cold)["match"]


def test_an_edge_may_name_its_node_by_an_alias(tmp_path, kg_env):
    answers = {
        "Intro": {
            "nodes": [node_of("Concept", "Continuous Integration", aliases=["CI"]), node_of("Concept", "Tests")],
            "edges": [{"src": "CI", "rel": "depends_on", "dst": "TESTS"}],
        }
    }
    project = setup(tmp_path, kg_env, answers)
    assert build_project(project).ok
    _, edges = graph(project)
    assert ("entity:Concept:continuous-integration", "depends_on", "entity:Concept:tests") in edges


@pytest.mark.parametrize(
    "level, app_level, name", [("internal", "customer", "Finder"), ("customer", "internal", "Seeker")]
)
def test_the_label_of_the_items_decides_the_name_a_reader_sees(tmp_path, kg_env, level, app_level, name):
    answers = {
        "Intro": {"nodes": [node_of("Concept", "Seeker", aliases=["Finder"])]},
        "Repos: app": {"nodes": [node_of("Concept", "Finder")]},
    }
    project = setup(tmp_path, kg_env, answers, level=level, app_level=app_level)
    assert build_project(project).ok
    nodes, _ = graph(project)
    assert [n for n, _ in nodes.values()] == [name]


# -- code ---------------------------------------------------------------------------------------------------------


def test_an_entity_that_names_one_symbol_or_file_is_linked_to_it_and_extracted(tmp_path, kg_env):
    answers = {
        "Intro": {
            "nodes": [
                node_of("Concept", "Server", conf=0.7),
                node_of("Concept", "Ranker", aliases=["rank_results()"]),
                node_of("Concept", "Guide file", aliases=["app/server.py"]),
                node_of("Concept", "Nothing in code"),
            ]
        }
    }
    project = setup(tmp_path, kg_env, answers)
    report = build_project(project)
    assert report.ok, report.errors
    _, edges = graph(project)
    server = edges[("entity:Concept:server", "refers_to", "symbol:app:app/server.py::Server")]
    assert server[0] == "extracted" and server[1] == pytest.approx(0.7) and server[2]["model"] == "fake-1"
    ranker = edges[("entity:Concept:ranker", "refers_to", "symbol:app:app/search.py::rank_results")]
    assert ranker[1] == pytest.approx(0.8)
    assert ("entity:Concept:guide-file", "refers_to", "app:file:app/server.py") in edges
    assert not [k for k in edges if k[0] == "entity:Concept:nothing-in-code" and k[1] == "refers_to"]
    assert report.coverage["entities"]["code_links"] == 3


def test_a_name_that_names_more_than_one_node_is_not_linked(tmp_path, kg_env):
    answers = {"Intro": {"nodes": [node_of("Concept", "search"), node_of("Concept", "Server.handle")]}}
    project = setup(tmp_path, kg_env, answers, wider=True)
    report = build_project(project)
    assert report.ok, report.errors
    _, edges = graph(project)
    assert not [k for k in edges if k[0] == "entity:Concept:search" and k[1] == "refers_to"]
    assert ("entity:Concept:server-handle", "refers_to", "symbol:app:app/server.py::Server.handle") in edges
    assert report.coverage["entities"]["code_ambiguous"] == 1


def test_a_case_folded_match_weighs_less_than_an_exact_one():
    index, names = links.code_index(
        {"f": [{"t": "node", "kind": "Symbol", "id": "symbol:a:x.py::Server", "name": "Server"}]}
    )
    assert links.code_link(["server"], index) == ("symbol:a:x.py::Server", "server", False)
    assert links.code_link(["Nope"], index) == (None, None, False)
    assert links.code_link(["app.Server"], index)[0] == "symbol:a:x.py::Server"


def test_impact_does_not_follow_the_edges_to_code(tmp_path, kg_env):
    from evo_agents.kg.serve import Session

    answers = {"Intro": {"nodes": [node_of("Concept", "Server")]}}
    project = setup(tmp_path, kg_env, answers)
    assert build_project(project).ok
    result = Session(project, "agent").call("kg_impact", {"id": "symbol:app:app/server.py::Server", "depth": 3})
    found = {r["node"]["id"] for r in result["structuredContent"]["impacted"]}
    assert not [n for n in found if n.startswith("entity:")]


# -- synonyms -----------------------------------------------------------------------------------------------------


CARS = {
    "Intro": {"nodes": [node_of("Concept", "Car"), node_of("Concept", "Garden")]},
    "Obsolete": {"nodes": [node_of("Concept", "Automobile")]},
    "Repos: app": {"nodes": [node_of("Concept", "Red Car")]},  # a cosine of 0.71 to Car
}


def test_near_entities_get_a_synonym_edge_when_the_graph_has_vectors(tmp_path, kg_env, words):
    project = setup(tmp_path, kg_env, CARS)
    semantic.write_settings(project.root, semantic.Settings("index", FAKE))
    report = build_project(project)
    assert report.ok, report.errors
    _, edges = graph(project)
    got = edges[("entity:Concept:automobile", "synonym_of", "entity:Concept:car")]
    assert got[0] == "extracted" and got[1] == pytest.approx(1.0) and got[2]["model"] == FAKE
    assert [k for k in edges if k[1] == "synonym_of"] == [
        ("entity:Concept:automobile", "synonym_of", "entity:Concept:car")
    ]
    cov = report.coverage["entities"]
    assert cov["synonyms"] == 1 and cov["synonym_status"] == "ok"
    embedded = words.texts
    for cold in (False, True):
        assert verify_build(project, report.content_hash, cold=cold)["match"]
    assert words.texts == embedded, "a verification reads the pairs from the memo"


def test_the_threshold_of_the_mapping_decides(tmp_path, kg_env, words):
    project = setup(tmp_path, kg_env, CARS, extra={"synonym_threshold": 0.7})
    semantic.write_settings(project.root, semantic.Settings("on", FAKE))
    report = build_project(project)
    assert report.coverage["entities"]["synonyms"] == 3
    _, edges = graph(project)
    assert edges[("entity:Concept:car", "synonym_of", "entity:Concept:red-car")][1] == pytest.approx(0.7071)
    from evo_agents.kg import settings as kg_settings

    with pytest.raises(kg_settings.SettingsError, match="synonym_threshold"):
        kg_settings.of({"entities": {"model": "m", "node_kinds": ["A"], "command": ["x"], "synonym_threshold": 0}})


def test_without_vectors_or_without_the_model_no_synonym_is_stated(tmp_path, kg_env, monkeypatch):
    project = setup(tmp_path, kg_env, CARS)
    report = build_project(project)
    assert report.coverage["entities"]["synonyms"] == 0
    assert report.coverage["entities"]["synonym_status"].startswith("off")
    semantic.forget_models()
    semantic.write_settings(project.root, semantic.Settings("index", "fake/absent-model"))

    def missing():
        raise semantic.SemanticUnavailable("no such model here")

    monkeypatch.setitem(semantic.FACTORIES, "fake/absent-model", missing)
    report = build_project(project)
    assert report.ok, report.errors
    assert report.coverage["entities"]["synonym_status"].startswith("unavailable")
    _, edges = graph(project)
    assert not [k for k in edges if k[1] == "synonym_of"]
    semantic.forget_models()

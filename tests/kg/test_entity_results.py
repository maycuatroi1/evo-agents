"""An entity node never takes the place of a document in kg_search and kg_context (``search.through_entities``): an
entity the question matches brings the documents that mention it, which name it as their reason, and a graph built
without the entities step ranks as before."""

import json

from evo_agents.kg import search
from evo_agents.kg.build import build_project
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write
from tests.kg.test_walk import ALPHA, BETA, QUASAR, ZEPHYR, entity_project, hide

QUERIES = ("quasar", "zephyr launcher", "glowing tail comet", "quasar zephyr engine dawn")


def search_of(project, query, limit=10, sink="agent"):
    found = Session(project, sink).call("kg_search", {"query": query, "limit": limit})
    assert not found.get("isError"), found
    return found


def documents(found) -> list[str]:
    """The documents of kg_search's results, in order: a section counts as its document, an entity as none."""
    ids = [r["id"] for r in found["structuredContent"]["results"] if not r["id"].startswith("entity:")]
    return list(dict.fromkeys(i.split("#", 1)[0] for i in ids))


def plain_project(tmp_path, kg_env):
    """The notes of ``entity_project`` in a project built without the entities step."""
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "docs/alpha/launch.md", "# Launch\n\nThe Zephyr launcher starts the engine at dawn.\n")
    commit(app, "launch notes")
    write(app / "docs/beta/tail.md", "# Tail\n\nA glowing tail follows the comet across the sky.\n")
    commit(app, "tail notes")
    assert all(r.ok for r in sync_project(project))
    report = build_project(project)
    assert report.ok, report.errors
    return project


def test_a_matched_entity_brings_its_documents_and_is_named_as_their_reason(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    found = search_of(project, "quasar")
    results = [r["id"] for r in found["structuredContent"]["results"]]
    assert set(results[:2]) == {ALPHA, BETA}  # no word of theirs matched the query: the entity brought them
    assert results.index(QUASAR) > max(results.index(ALPHA), results.index(BETA))  # the entity after its documents
    assert found["structuredContent"]["entities"][ALPHA] == [QUASAR]
    assert found["structuredContent"]["entities"][BETA] == [QUASAR]
    text = found["content"][0]["text"]
    assert "launch.md" in text and "entity Quasar" in text


def test_entities_never_lower_the_documents_of_the_first_k(tmp_path, kg_env):
    with_entities = entity_project(tmp_path / "ents", kg_env)
    without = plain_project(tmp_path / "plain", kg_env)
    for query in QUERIES:
        for k in (1, 2, 3, 5):
            on, off = search_of(with_entities, query, k), search_of(without, query, k)
            assert len(documents(on)) >= len(documents(off)), (query, k)
            assert not any(
                r["id"].startswith("entity:") for r in on["structuredContent"]["results"][: len(documents(on))]
            )


def test_a_graph_without_entities_ranks_as_before(tmp_path, kg_env):
    project = plain_project(tmp_path, kg_env)
    hits = [{"id": ALPHA}, {"id": BETA}]
    assert search.through_entities(None, 0, hits, lambda _: True) == (hits, {})  # no entity: the store is not read
    for query in QUERIES:
        found = search_of(project, query)["structuredContent"]
        assert found["entities"] == {}
        assert not any(r["id"].startswith("entity:") for r in found["results"])


def test_kg_context_seeds_the_documents_of_a_matched_entity(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    found = Session(project, "agent").call("kg_context", {"query": "quasar"})
    assert not found.get("isError"), found
    data = found["structuredContent"]
    assert set(data["seeds"][:2]) == {ALPHA, BETA}  # the entity seeds after its documents, in a slot left over
    assert data["entities"][ALPHA] == [QUASAR]
    assert f"seed [{ALPHA.replace(':file:', ':')}] for entity {QUASAR}" in found["content"][0]["text"]


def test_an_entity_never_brings_a_document_the_reader_does_not_see(tmp_path, kg_env):
    project = entity_project(tmp_path, kg_env)
    hide(project, "UPDATE nodes SET label_lvl = 2 WHERE node_id = ?", BETA)
    assert BETA in [r["id"] for r in search_of(project, "quasar", sink="wide")["structuredContent"]["results"]]
    for found in (search_of(project, "quasar"), Session(project, "agent").call("kg_context", {"query": "quasar"})):
        assert "tail.md" not in json.dumps(found)
    hide(project, "UPDATE edges SET label_lvl = 2 WHERE dst = ? AND rel = 'mentions' AND src = ?", ZEPHYR, ALPHA)
    found = search_of(project, "zephyr")["structuredContent"]
    assert ZEPHYR not in found["entities"].get(ALPHA, [])  # the hidden edge is no reason


def test_an_entity_many_documents_mention_brings_only_those_the_search_ranked(monkeypatch):
    sources = [f"doc:{i}" for i in range(search.ENTITY_SPREAD + 5)]

    class Store:
        def edges_of(self, node_id, b, direction, limit):
            return [{"src": s, "rel": "mentions", "dst": node_id, "conf": 0.9} for s in sources]

        def nodes(self, ids, b):
            return {i: {"id": i, "kind": "Document"} for i in ids}

    hits = [{"id": "entity:Place:paris"}, {"id": "doc:7"}, {"id": "other"}]
    out, why = search.through_entities(Store(), 0, hits, lambda _: True)
    assert [n["id"] for n in out] == ["doc:7", "other", "entity:Place:paris"]
    assert why == {"doc:7": ["entity:Place:paris"]}
    sources[:] = sources[:4]  # a few: the unranked ones come too, by confidence then id, up to ENTITY_DOCS
    out, why = search.through_entities(Store(), 0, [{"id": "entity:Place:paris"}], lambda _: True)
    assert [n["id"] for n in out] == ["doc:0", "doc:1", "doc:2", "entity:Place:paris"]


def test_an_entity_brings_a_commit_or_pull_request_only_when_the_search_ranked_it():
    kinds = {"commit:1": "Commit", "pr:2": "PullRequest", "doc:3": "Document"}

    class Store:
        def edges_of(self, node_id, b, direction, limit):
            return [{"src": s, "rel": "mentions", "dst": node_id, "conf": 0.9} for s in kinds]

        def nodes(self, ids, b):
            return {i: {"id": i, "kind": kinds[i]} for i in ids}

    out, why = search.through_entities(Store(), 0, [{"id": "entity:Place:paris"}], lambda _: True)
    assert [n["id"] for n in out] == ["doc:3", "entity:Place:paris"]  # the unranked commit and pull request stay out
    assert why == {"doc:3": ["entity:Place:paris"]}
    out, why = search.through_entities(Store(), 0, [{"id": "entity:Place:paris"}, {"id": "commit:1"}], lambda _: True)
    assert [n["id"] for n in out] == ["commit:1", "doc:3", "entity:Place:paris"]  # a ranked commit is still a source
    assert why == {"commit:1": ["entity:Place:paris"], "doc:3": ["entity:Place:paris"]}

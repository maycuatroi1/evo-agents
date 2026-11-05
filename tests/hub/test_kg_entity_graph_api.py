"""The entity graph the hub's web pages read (``evo_agents.hub.kg_entity_graph`` through ``WebSession``).

Checked on a store written here, without a hub: the overview ranks entities by the documents that mention them and
keeps the relations between those it shows; the neighbourhood stops at its depth and node limit and says what it
left out; an entity's detail quotes the documents that mention it with a link and a line, a relation's the documents
it was read from; and a reader whose grant stops at public never receives a secret entity, relation, document or
quote, nor counts one, by the overview, the neighbourhood or the details (404 alike for a hidden and a missing id).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("sqlalchemy", reason="the hub's graph needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_entity_graph
from evo_agents.hub.kg_graph import BuiltGraph
from evo_agents.hub.kg_web import NotVisible, WebSession
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.store import Graph, Store, edge_id
from evo_agents.kg.store_filters import label_columns

PROJECT = "ents"
PUBLIC = Label(0, 0, "U", frozenset({PROJECT}))
SECRET = Label(3, 0, "U", frozenset({PROJECT}))
MODEL = "test-model"


def _unit(graph: Graph, doc: str, label: Label) -> str:
    unit = f"{doc}#"
    graph.units[unit] = {
        "item_id": doc,
        "rev": "1",
        "source": "docs",
        "anchor": "",
        "hash": None,
        "span": None,
        **label_columns(label),
    }
    return unit


def _node(graph: Graph, nid: str, kind: str, name: str, label: Label, units: list[str], props: dict) -> None:
    graph.nodes[nid] = {"kind": kind, "name": name, "props": props, "status": "parsed", "conf": 0.9, "aliases": []}
    graph.nodes[nid]["label"] = label
    graph.derivations[nid] = [{"units": units, "needs": []}]


def _edge(graph: Graph, src: str, rel: str, dst: str, label: Label, units: list[str], props: dict) -> str:
    eid = edge_id(src, rel, dst)
    graph.edges[eid] = {"src": src, "rel": rel, "dst": dst, "props": props, "status": "parsed", "conf": 0.8}
    graph.edges[eid]["label"] = label
    graph.derivations[eid] = [{"units": units, "needs": [src, dst]}]
    return eid


def _graph() -> Graph:
    """Documents a1..a3 public and s1 secret. Alice (Actor) is mentioned by a1, a2, a3 and s1; Acme by a1 and a2;
    Paris by a3; Bond (a secret Actor) by s1 alone. Alice works at Acme (public, said by a1 and a2), lives in Paris
    (public), knows Bond (secret)."""
    graph = Graph()
    units = {}
    for doc, label in (("doc:a1", PUBLIC), ("doc:a2", PUBLIC), ("doc:a3", PUBLIC), ("doc:s1", SECRET)):
        units[doc] = _unit(graph, doc, label)
        props = {"path": f"{doc[4:]}.md", "uri": f"https://example.org/{doc[4:]}.md"}
        _node(graph, doc, "Document", doc[4:], label, [units[doc]], props)
    entities = {
        "entity:Actor:alice": ("Alice", PUBLIC, ["doc:a1", "doc:a2", "doc:a3", "doc:s1"]),
        "entity:Organization:acme": ("Acme", PUBLIC, ["doc:a1", "doc:a2"]),
        "entity:Place:paris": ("Paris", PUBLIC, ["doc:a3"]),
        "entity:Actor:bond": ("Bond", SECRET, ["doc:s1"]),
    }
    for nid, (name, label, docs) in entities.items():
        public = "secret" if label is SECRET else "public"
        props = {"model": MODEL, "item": docs[0], "description": f"{name}, {public}", "type": nid.split(":")[1]}
        _node(graph, nid, nid.split(":")[1], name, label, [units[d] for d in docs], props)
        for line, doc in enumerate(docs, start=3):
            doc_label = SECRET if doc == "doc:s1" else PUBLIC
            said = {"model": MODEL, "item": doc, "evidence": f"{name} in {doc}", "line": line}
            said["description"] = f"what {doc} says of {name}"
            _edge(graph, doc, "mentions", nid, doc_label, [units[doc]], said)
    work = {"model": MODEL, "item": "doc:a1", "evidence": "Alice works at Acme", "line": 7}
    work |= {"keywords": "employment", "description": "Alice works at Acme", "weight": 15.0, "sources": 2}
    _edge(graph, "entity:Actor:alice", "relates_to", "entity:Organization:acme", PUBLIC, [units["doc:a1"]], work)
    graph.derivations[edge_id("entity:Actor:alice", "relates_to", "entity:Organization:acme")].append(
        {"units": [units["doc:a2"]], "needs": ["entity:Actor:alice", "entity:Organization:acme"]}
    )
    home = {"model": MODEL, "item": "doc:a3", "evidence": "Alice lives in Paris", "strength": 4.0, "line": 2}
    _edge(graph, "entity:Actor:alice", "relates_to", "entity:Place:paris", PUBLIC, [units["doc:a3"]], home)
    spy = {"model": MODEL, "item": "doc:s1", "evidence": "Alice knows Bond", "keywords": "secret contact"}
    _edge(graph, "entity:Actor:alice", "relates_to", "entity:Actor:bond", SECRET, [units["doc:s1"]], spy)
    return graph


@pytest.fixture
def store(tmp_path):
    found = Store(tmp_path / "graph.sqlite")
    found.write(_graph(), state_digest="x", coverage={}, stats={}, policy=None)
    kg_entity_graph.VIEWS.clear()
    yield found
    found.close()


def _session(store: Store, ceiling: Label) -> WebSession:
    built = BuiltGraph(1, "sha", 1, "hash", 8, 12, datetime(2026, 11, 5, tzinfo=UTC))
    return WebSession(PROJECT, Policy(PROJECT, {}), ceiling, "web", store, built, built)


def _ids(items) -> list[str]:
    return [i["id"] for i in items]


def test_the_overview_ranks_entities_by_documents_and_keeps_the_relations_between_them(store):
    data = _session(store, SECRET).entities(2)
    assert _ids(data["nodes"]) == ["entity:Actor:alice", "entity:Organization:acme"]
    alice = data["nodes"][0]
    assert (alice["docs"], alice["degree"], alice["type"], alice["model"]) == (4, 3, "Actor", MODEL)
    assert alice["description"] == "Alice, public"
    assert [(e["src"], e["dst"]) for e in data["edges"]] == [("entity:Actor:alice", "entity:Organization:acme")]
    edge = data["edges"][0]
    assert (edge["keywords"], edge["weight"], edge["sources"], edge["model"]) == ("employment", 15.0, 2, MODEL)
    assert (data["total_nodes"], data["total_edges"], data["truncated"]) == (4, 3, True)


def test_a_public_reader_never_receives_or_counts_a_secret_entity_relation_or_document(store):
    session = _session(store, PUBLIC)
    data = session.entities(kg_entity_graph.MAX_ENTITY_NODES)
    assert "entity:Actor:bond" not in _ids(data["nodes"])
    assert (data["total_nodes"], data["total_edges"], data["truncated"]) == (3, 2, False)
    alice = data["nodes"][0]
    assert (alice["docs"], alice["degree"]) == (3, 2)
    around = session.entities_around("entity:Actor:alice", 3, 100)
    assert "entity:Actor:bond" not in _ids(around["nodes"])
    detail = session.entity("entity:Actor:alice")
    assert [c["document"] for c in detail["citations"]] == ["doc:a1", "doc:a2", "doc:a3"]
    assert detail["documents"] == 3
    assert "entity:Actor:bond" not in _ids(detail["neighbours"])
    assert all("Bond" not in str(e) for e in detail["relations"])
    for hidden in ("entity:Actor:bond", "entity:Actor:nobody"):
        with pytest.raises(NotVisible):
            session.entity(hidden)
        with pytest.raises(NotVisible):
            session.entities_around(hidden, 1, 10)
    secret = edge_id("entity:Actor:alice", "relates_to", "entity:Actor:bond")
    with pytest.raises(NotVisible):
        session.relation(secret)
    assert _session(store, SECRET).relation(secret)["edge"]["keywords"] == "secret contact"


def test_the_neighbourhood_stops_at_its_depth_and_limit_and_says_what_it_left_out(store):
    session = _session(store, SECRET)
    one = session.entities_around("entity:Place:paris", 1, 100)
    assert one["focus"] == "entity:Place:paris"
    assert {n["id"]: n["hop"] for n in one["nodes"]} == {"entity:Place:paris": 0, "entity:Actor:alice": 1}
    two = session.entities_around("entity:Place:paris", 2, 100)
    assert {n["id"] for n in two["nodes"] if n["hop"] == 2} == {"entity:Organization:acme", "entity:Actor:bond"}
    assert len(two["edges"]) == 3 and not two["truncated"]
    cut = session.entities_around("entity:Actor:alice", 3, 2)
    assert _ids(cut["nodes"]) == ["entity:Actor:alice", "entity:Organization:acme"]  # the heaviest relation first
    assert (cut["left_out"], cut["truncated"]) == (2, True)


def test_an_entity_and_a_relation_cite_their_documents_with_quote_line_and_link(store):
    session = _session(store, SECRET)
    detail = session.entity("entity:Organization:acme")
    first = detail["citations"][0]
    assert first == {
        "document": "doc:a1",
        "name": "a1",
        "path": "a1.md",
        "uri": "https://example.org/a1.md",
        "quote": "Acme in doc:a1",
        "line": 3,
        "description": "what doc:a1 says of Acme",
        "model": MODEL,
    }
    assert _ids(detail["relations"]) == [edge_id("entity:Actor:alice", "relates_to", "entity:Organization:acme")]
    relation = session.relation(detail["relations"][0]["id"])
    assert (relation["src"]["name"], relation["dst"]["name"], relation["documents"]) == ("Alice", "Acme", 2)
    cited = {c["document"]: (c["quote"], c["line"]) for c in relation["citations"]}
    assert cited == {"doc:a1": ("Alice works at Acme", 7), "doc:a2": (None, None)}


def test_the_routes_serve_150_entities_by_default_at_most_1000_and_a_depth_of_1_to_3():
    pytest.importorskip("fastapi")
    from evo_agents.hub.openapi import document

    paths = document()["paths"]

    def bounds(route: str) -> dict[str, dict]:
        return {p["name"]: p["schema"] for p in paths[f"/v1/kg/{{project}}/{route}"]["get"]["parameters"]}

    top, around = bounds("entity-graph"), bounds("entity-graph/around")
    assert (top["limit"]["default"], top["limit"]["maximum"]) == (150, 1000)
    assert (around["depth"]["minimum"], around["depth"]["maximum"], around["limit"]["maximum"]) == (1, 3, 1000)
    assert {"id"} <= set(bounds("entity")) and {"id"} <= set(bounds("relation"))

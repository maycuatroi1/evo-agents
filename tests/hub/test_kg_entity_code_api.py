"""The code of an entity the hub's web pages read (``evo_agents.hub.kg_entity_graph.code_links`` through
``WebSession``).

Checked on the store of ``test_kg_entity_graph_api`` with code added: Alice is implemented by a Directory, a File and a
Symbol (also named by her alias, ``refers_to``) and by a secret Symbol; Acme by the File. An entity's detail lists its
code by package with the reason, the confidence and the link at the commit the source read (a Symbol's file's, with
its line); a node of the graphs counts its code; ``code`` adds the code nodes of the entities shown; and a reader
whose grant stops at public never receives nor counts the secret Symbol.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="the hub's graph needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_entity_graph
from evo_agents.kg.store import Store
from evo_agents.kg.store_filters import label_columns
from tests.hub.test_kg_entity_graph_api import PUBLIC, SECRET, _edge, _graph, _ids, _node, _session

REPO = "r"
BRANCH = "https://github.com/o/r/blob/main/"
COMMIT = "https://github.com/o/r/blob/abc123/"
DIR = f"dir:{REPO}:evo_agents/hub"
FILE = f"{REPO}:evo_agents/hub/relay.py"
RELAY = "symbol:code:evo_agents/hub/relay.py::Relay"
VAULT_FILE = f"{REPO}:evo_agents/vault.py"
VAULT = "symbol:code:evo_agents/vault.py::Vault"
ALICE = "entity:Actor:alice"
ACME = "entity:Organization:acme"


def _code_unit(graph, item: str, label) -> str:
    unit = f"{item}#"
    graph.units[unit] = {
        "item_id": item,
        "rev": "1",
        "source": "code",
        "anchor": "",
        "hash": None,
        "span": None,
        **label_columns(label),
    }
    return unit


def _with_code():
    graph = _graph()
    relay, vault = _code_unit(graph, FILE, PUBLIC), _code_unit(graph, VAULT_FILE, SECRET)
    for fid, path, label, unit in (
        (FILE, "evo_agents/hub/relay.py", PUBLIC, relay),
        (VAULT_FILE, "evo_agents/vault.py", SECRET, vault),
    ):
        props = {"path": path, "repo": REPO, "source": "code", "uri": BRANCH + path}
        _node(graph, fid, "File", path, label, [unit], props)
    _node(graph, DIR, "Directory", "evo_agents/hub/", PUBLIC, [relay], {"path": "evo_agents/hub", "repo": REPO})
    for sid, qual, path, fid, label, unit in (
        (RELAY, "Relay", "evo_agents/hub/relay.py", FILE, PUBLIC, relay),
        (VAULT, "Vault", "evo_agents/vault.py", VAULT_FILE, SECRET, vault),
    ):
        props = {"kind": "class", "path": path, "line": 12, "file": fid, "key": [REPO, path, qual]}
        _node(graph, sid, "Symbol", qual, label, [unit], props)
    mapped = {"model": "claude-opus-5-5", "run": 1969}
    for dst, reason, conf in (
        (DIR, "the relay package", 0.7),
        (FILE, "the relay itself", 0.9),
        (RELAY, "its class", 0.8),
    ):
        eid = _edge(graph, ALICE, "implemented_by", dst, PUBLIC, [relay], {**mapped, "reason": reason, "conf": conf})
        graph.edges[eid]["conf"] = conf
    eid = _edge(graph, ALICE, "refers_to", RELAY, PUBLIC, [relay], {"name": "Relay", "models": ["m"]})
    graph.edges[eid]["conf"] = 1.0
    _edge(graph, ALICE, "implemented_by", VAULT, SECRET, [vault], {**mapped, "reason": "the vault", "conf": 0.9})
    _edge(graph, ACME, "implemented_by", FILE, PUBLIC, [relay], {**mapped, "reason": "acme's relay", "conf": 0.6})
    return graph


@pytest.fixture
def store(tmp_path):
    found = Store(tmp_path / "graph.sqlite")
    coverage = {"sources": [{"id": "code", "read": {"blob": {"branch": BRANCH, "commit": COMMIT}}}]}
    found.write(_with_code(), state_digest="x", coverage=coverage, stats={}, policy=None)
    kg_entity_graph.VIEWS.clear()
    yield found
    found.close()


def test_an_entity_lists_its_code_by_package_with_reason_confidence_and_link_at_the_commit(store):
    detail = _session(store, SECRET).entity(ALICE)
    assert detail["code_links"] == 4
    assert [(c["package"], c["kind"], c["id"]) for c in detail["code"]] == [
        ("evo_agents", "Symbol", VAULT),
        ("evo_agents/hub", "Directory", DIR),
        ("evo_agents/hub", "File", FILE),
        ("evo_agents/hub", "Symbol", RELAY),
    ]
    by_id = {c["id"]: c for c in detail["code"]}
    assert by_id[DIR]["uri"] == "https://github.com/o/r/tree/abc123/evo_agents/hub"
    assert (by_id[DIR]["reason"], by_id[DIR]["conf"], by_id[DIR]["rels"]) == (
        "the relay package",
        0.7,
        ["implemented_by"],
    )
    assert by_id[FILE]["uri"] == COMMIT + "evo_agents/hub/relay.py" and by_id[FILE]["line"] is None
    relay = by_id[RELAY]
    assert relay["rels"] == ["implemented_by", "refers_to"]
    assert (relay["reason"], relay["name_match"], relay["conf"]) == ("its class", "Relay", 1.0)
    assert (relay["uri"], relay["line"], relay["repo"], relay["model"]) == (
        COMMIT + "evo_agents/hub/relay.py",
        12,
        REPO,
        "claude-opus-5-5",
    )


def test_graph_nodes_count_their_code_and_code_true_adds_the_code_nodes_of_the_entities_shown(store):
    session = _session(store, SECRET)
    plain = session.entities(2)
    assert {n["id"]: n["code"] for n in plain["nodes"]} == {ALICE: 4, ACME: 1}
    assert (plain["code_nodes"], plain["code_edges"], plain["code_left_out"]) == ([], [], 0)
    drawn = session.entities(2, code=True)
    assert set(_ids(drawn["code_nodes"])) == {DIR, FILE, RELAY, VAULT}
    file = next(n for n in drawn["code_nodes"] if n["id"] == FILE)
    assert (file["entities"], file["package"], file["kind"]) == (2, "evo_agents/hub", "File")
    assert {(e["src"], e["dst"]) for e in drawn["code_edges"]} == {
        (ALICE, DIR),
        (ALICE, FILE),
        (ALICE, RELAY),
        (ALICE, VAULT),
        (ACME, FILE),
    }
    assert next(e for e in drawn["code_edges"] if e["dst"] == RELAY)["conf"] == 1.0
    around = session.entities_around("entity:Place:paris", 1, 100, code=True)
    assert {e["src"] for e in around["code_edges"]} == {ALICE}
    cut = kg_entity_graph.code_of(session._entity_view(*session._visible_store()), [ALICE, ACME], limit=1)
    assert (len(cut["nodes"]), cut["left_out"]) == (1, 3)


def test_a_public_reader_never_receives_nor_counts_secret_code(store):
    session = _session(store, PUBLIC)
    detail = session.entity(ALICE)
    assert detail["code_links"] == 3 and VAULT not in [c["id"] for c in detail["code"]]
    assert "Vault" not in str(detail)
    data = session.entities(10, code=True)
    assert next(n for n in data["nodes"] if n["id"] == ALICE)["code"] == 3
    assert VAULT not in _ids(data["code_nodes"]) and "vault" not in str(data["code_edges"])


def test_the_routes_take_code_and_answer_with_code_fields():
    pytest.importorskip("fastapi")
    from evo_agents.hub.openapi import document

    doc = document()
    for route in ("entity-graph", "entity-graph/around"):
        params = {p["name"]: p["schema"] for p in doc["paths"][f"/v1/kg/{{project}}/{route}"]["get"]["parameters"]}
        assert params["code"]["default"] is False
    schemas = doc["components"]["schemas"]
    detail = next(v for k, v in schemas.items() if k.startswith("EntityDetail"))["properties"]
    assert {"code", "code_links"} <= set(detail)
    link = next(v for k, v in schemas.items() if k.startswith("CodeLink"))["properties"]
    assert {"id", "kind", "package", "reason", "conf", "uri", "line", "label"} <= set(link)
